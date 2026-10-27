"""Background jobs for deriving feature views from saved run evidence."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable

from .alan import AlanRunner, RunStatus
from .analysis_report import build_analysis_report
from .database import RunDatabase
from .experiment import (
    EFFECTIVE_TIMEOUT,
    SKILL_RELATIVE_PATH,
    _engine_history_record,
    _read_workspace,
    _restore_disallowed_changes,
    _source_diff,
    _task_instruction,
)
from .mining import PullRequestCandidate
from .session_history import read_session_history
from .validation import make_task_snapshot, run_pytest


ACTIVE_STATUSES = frozenset({"queued", "running"})
LADDER_CONFIGS = (
    ("flash_no_skill", "deepseek/deepseek-v4.1-flash", "none"),
    ("flash_current_skill", "deepseek/deepseek-v4.1-flash", "current"),
    ("deepseek_pro_no_skill", "deepseek/deepseek-v4-pro", "none"),
)


class AnalysisBlocked(RuntimeError):
    """An analysis cannot run without verified local artifacts or credentials."""


class AnalysisManager:
    """Run saved-evidence analyses without making model or network calls."""

    def __init__(
        self,
        database: RunDatabase,
        *,
        project_root: Path,
        data_root: Path,
        runner_factory: Callable[[str], AlanRunner] | None = None,
    ) -> None:
        self.database = database
        self.project_root = project_root.resolve()
        self.data_root = data_root.resolve()
        self.runner_factory = runner_factory or (lambda model: AlanRunner(model=model))
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, run_id: str, kind: str, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
        job = self.database.create_analysis_job(run_id, kind, config=config)
        task = asyncio.create_task(self._execute(job["id"]), name=f"alangym-analysis:{job['id']}")
        self._tasks[job["id"]] = task
        task.add_done_callback(lambda _: self._tasks.pop(job["id"], None))
        return job

    def recover_interrupted_analyses(self) -> None:
        """Mark analysis jobs from an earlier server process as interrupted."""
        for run in self.database.list_runs(limit=100):
            for job in self.database.list_analysis_jobs(run["id"]):
                if job["status"] in ACTIVE_STATUSES:
                    self.database.update_analysis_job(
                        job["id"],
                        status="blocked",
                        message="Server restarted before this analysis finished; run it again to continue.",
                        error="interrupted",
                    )

    async def cancel(self, analysis_id: str) -> bool:
        job = self.database.get_analysis_job(analysis_id)
        if job["status"] not in ACTIVE_STATUSES:
            return False
        self.database.update_analysis_job(
            analysis_id,
            status="cancelled",
            progress=job["progress"],
            message="Cancelled by the user",
        )
        task = self._tasks.get(analysis_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return True

    async def shutdown(self) -> None:
        tasks = list(self._tasks.items())
        for analysis_id, task in tasks:
            if task.done():
                continue
            job = self.database.get_analysis_job(analysis_id)
            if job["status"] in ACTIVE_STATUSES:
                self.database.update_analysis_job(
                    analysis_id,
                    status="blocked",
                    message="Server is shutting down; run this analysis again to continue.",
                    error="interrupted",
                )
            task.cancel()
        if tasks:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)

    async def _execute(self, analysis_id: str) -> None:
        try:
            job = self.database.get_analysis_job(analysis_id)
            if job["status"] != "queued":
                return
            self.database.update_analysis_job(
                analysis_id, status="running", progress=0.1, message="Reading saved run evidence"
            )
            if job["kind"] == "ladder":
                await self._execute_ladder(analysis_id, job)
            report = build_analysis_report(_database_detail(self.database, job["run_id"]))
            self.database.update_analysis_job(
                analysis_id, status="running", progress=0.8, message="Building replay-safe report"
            )
            result = _select_result(report, job["kind"])
            self.database.update_analysis_job(
                analysis_id,
                status="complete",
                progress=1,
                message="Analysis complete",
                result=result,
            )
        except asyncio.CancelledError:
            # cancel() and shutdown() persist the appropriate terminal state first.
            raise
        except AnalysisBlocked as error:
            current = self.database.get_analysis_job(analysis_id)
            if current["status"] in ACTIVE_STATUSES:
                self.database.update_analysis_job(
                    analysis_id,
                    status="blocked",
                    message=str(error),
                    error="prerequisite_unavailable",
                )
        except Exception as error:
            current = self.database.get_analysis_job(analysis_id)
            if current["status"] in ACTIVE_STATUSES:
                self.database.update_analysis_job(
                    analysis_id,
                    status="failed",
                    message="Analysis failed while reading saved run evidence",
                    error=f"{type(error).__name__}: {error}",
                )

    async def _execute_ladder(self, analysis_id: str, job: dict[str, Any]) -> None:
        run = self.database.get_run(job["run_id"])
        if run["repo"].casefold() != "mahmoud/boltons":
            raise AnalysisBlocked("The saved ladder supports mahmoud/boltons only.")
        repository_path = self.data_root / "repos" / "mahmoud--boltons"
        if repository_path.is_symlink() or not repository_path.is_dir():
            raise AnalysisBlocked("The completed run's verified repository cache is missing.")
        python = repository_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            raise AnalysisBlocked("The completed run's locked test environment is missing.")
        tasks = [
            _candidate_from_task(row)
            for row in self.database.list_tasks(job["run_id"])
            if row.get("split") == "heldout"
        ]
        if len(tasks) != run["heldout_count"] or not tasks:
            raise AnalysisBlocked("The saved held-out task set is incomplete; the ladder will not shrink it.")
        skills = self.database.list_skill_versions(job["run_id"])
        if not skills:
            raise AnalysisBlocked("No saved Alan skill version is available for the ladder.")
        current_skill = skills[-1]["content"]
        current_version = skills[-1]["version"]
        run_dir = self.data_root / "runs" / job["run_id"] / "analyses" / analysis_id
        run_dir.mkdir(parents=True, exist_ok=True)
        calls = len(tasks) * len(LADDER_CONFIGS)
        semaphore = asyncio.Semaphore(3)

        async def run_call(config_id: str, model: str, skill_mode: str, task: PullRequestCandidate):
            async with semaphore:
                return await self._run_ladder_trial(
                    analysis_id, job["run_id"], run_dir, repository_path, python,
                    task, config_id, model, skill_mode,
                    current_skill if skill_mode == "current" else "",
                    current_version if skill_mode == "current" else "none",
                )

        pending = [
            asyncio.create_task(run_call(config_id, model, skill_mode, task))
            for config_id, model, skill_mode in LADDER_CONFIGS
            for task in tasks
        ]
        completed = 0
        try:
            for future in asyncio.as_completed(pending):
                trial = await future
                self.database.save_trial(job["run_id"], trial)
                completed += 1
                self.database.update_analysis_job(
                    analysis_id,
                    status="running",
                    progress=0.1 + 0.8 * completed / calls,
                    message=f"Ladder: saved {completed} of {calls} held-out trials",
                )
        except BaseException:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise

    async def _run_ladder_trial(
        self,
        analysis_id: str,
        run_id: str,
        run_dir: Path,
        repository_path: Path,
        python: Path,
        task: PullRequestCandidate,
        config_id: str,
        model: str,
        skill_mode: str,
        skill: str,
        skill_version: str,
    ) -> dict[str, Any]:
        trial_id = f"ladder:{analysis_id}:{config_id}:pr-{task.number}"
        workspace = run_dir / config_id / f"pr-{task.number}" / "workspace"
        make_task_snapshot(repository_path, task, workspace, with_fix=False)
        baseline_files = _read_workspace(workspace)
        if skill:
            skill_path = workspace / SKILL_RELATIVE_PATH
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(skill, encoding="utf-8")
        agent = await self.runner_factory(model).run(
            _task_instruction(task, python, skill, ""),
            workspace,
            timeout=EFFECTIVE_TIMEOUT,
            tool_path=python.parent,
        )
        engine_history = await asyncio.to_thread(read_session_history, workspace)
        history_record = _engine_history_record(engine_history)
        if agent.status is RunStatus.AUTH_REQUIRED:
            raise AnalysisBlocked("Alan Code credentials are unavailable for the model ladder.")

        policy_violation = _restore_disallowed_changes(
            workspace, baseline_files, set(task.source_paths), SKILL_RELATIVE_PATH
        )
        if agent.status is RunStatus.COMPLETED:
            grade = await run_pytest(workspace, task.test_paths, python, timeout=60)
            passed = grade.status == "passed" and not policy_violation
            passed_value = (
                False if policy_violation else passed
                if grade.status in {"passed", "failed"} else None
            )
            status = (
                "policy_violation" if policy_violation
                else "passed" if grade.status == "passed"
                else "failed" if grade.status == "failed"
                else f"grader_{grade.status}"
            )
            grader_output = grade.output
            elapsed = agent.elapsed_seconds + grade.elapsed_seconds
        else:
            passed_value = None
            status = f"agent_{agent.status.value}"
            grader_output = "Grading skipped because Alan did not complete the task."
            elapsed = agent.elapsed_seconds
        trace = json.dumps(
            {
                "agent_status": agent.status.value,
                "agent_stdout": agent.stdout,
                "agent_stderr": agent.stderr,
                "grader_output": grader_output,
                "source_diff": _source_diff(baseline_files, workspace, set(task.source_paths)),
                "unauthorized_changes_restored": policy_violation,
            },
            ensure_ascii=False,
        )
        usage = history_record.get("history", {}).get("usage", {})
        return {
            "id": trial_id,
            "task_id": f"pr-{task.number}",
            "pr_number": task.number,
            "phase": "ladder",
            "config_id": config_id,
            "skill_mode": skill_mode,
            "skill_version": skill_version,
            "status": status,
            "passed": passed_value,
            "elapsed_seconds": round(elapsed, 3),
            "cost_usd": None,
            "cost_verified": False,
            "model": model,
            "actual_model": history_record.get("history", {}).get("model_id"),
            "tokens_input": usage.get("input_tokens"),
            "tokens_output": usage.get("output_tokens"),
            "tokens_reasoning": usage.get("reasoning_tokens"),
            "engine_history": history_record,
            "trace": trace,
        }


def _select_result(report: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == "suite":
        return report
    result = report.get(kind)
    if isinstance(result, dict):
        return {"kind": kind, **result}
    return {"kind": kind, "status": "unavailable", "reason": "No saved results for this feature"}


def _database_detail(database: RunDatabase, run_id: str) -> dict[str, Any]:
    return {
        "run": database.get_run(run_id),
        "tasks": database.list_tasks(run_id),
        "trials": database.list_trials(run_id),
        "skills": database.list_skill_versions(run_id),
        "events": [
            {**event, "payload": json.loads(event.pop("payload_json"))}
            for event in database.list_events(run_id, limit=1_000)
        ],
    }


def _candidate_from_task(task: dict[str, Any]) -> PullRequestCandidate:
    return PullRequestCandidate(
        number=int(task["pr_number"]),
        title=str(task["title"]),
        body=str(task.get("body", "")),
        merged_at=str(task.get("merged_at", "")),
        merge_sha=str(task["merge_sha"]),
        base_sha=str(task["base_sha"]),
        source_paths=frozenset(str(path) for path in task.get("source_paths", [])),
        test_paths=tuple(str(path) for path in task.get("test_paths", [])),
        fixture_paths=tuple(str(path) for path in task.get("fixture_paths", [])),
    )
