"""Local, persisted experiment orchestration for mining and evolving an Alan skill."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

from .alan import DEFAULT_MODEL, AgentResult, AlanRunner, RunStatus, SAFE_ENVIRONMENT_VARIABLES
from .database import RunDatabase, TERMINAL_STATES
from .mining import (
    InsufficientTasksError,
    PullRequestCandidate,
    build_benchmark_split,
    select_candidates,
    split_tasks,
)
from .repository import PublicGitHubRepository, RepositoryError
from .replay import export_replay
from .session_history import HistoryStatus, SessionHistoryResult, read_session_history
from .validation import TaskValidationError, make_task_snapshot, run_pytest, validate_task


TRAINING_COUNT = 6
RETAINED_VALIDATION_COUNT = 4
FRESH_HELDOUT_COUNT = 4
FRESH_TASK_COUNT = TRAINING_COUNT + FRESH_HELDOUT_COUNT
HELDOUT_COUNT = RETAINED_VALIDATION_COUNT + FRESH_HELDOUT_COUNT
TASK_COUNT = TRAINING_COUNT + HELDOUT_COUNT
MAX_ROUNDS = 2
SKILL_RELATIVE_PATH = ".agents/skills/alangym/SKILL.md"
EFFECTIVE_TIMEOUT = 600
SKILL_LINE_LIMIT = 200
SKILL_BYTE_LIMIT = 16_000


class ExperimentBlocked(RuntimeError):
    """An unmet external prerequisite prevents a meaningful model run."""


class ExperimentManager:
    """Run one cancellable experiment at a time, saving every durable result."""

    def __init__(
        self,
        database: RunDatabase,
        *,
        project_root: Path,
        data_root: Path,
        runner: AlanRunner | None = None,
    ) -> None:
        self.database = database
        self.project_root = project_root.resolve()
        self.data_root = data_root.resolve()
        self.repository_cache = self.data_root / "repos"
        self.run_cache = self.data_root / "runs"
        self.runner = runner or AlanRunner(model=DEFAULT_MODEL)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def recover_interrupted_runs(self) -> None:
        """Mark live records from a previous server process as interrupted."""
        for run in self.database.list_runs(limit=100):
            if run["state"] not in TERMINAL_STATES:
                self.database.transition(
                    run["id"],
                    "blocked",
                    "Server restarted before this run finished; start a new run to continue.",
                    event_type="interrupted",
                )

    def start(self, repo: str, *, seed: int = 42) -> dict[str, Any]:
        run = self.database.create_run(
            repo,
            model=DEFAULT_MODEL,
            seed=seed,
            training_count=TRAINING_COUNT,
            heldout_count=HELDOUT_COUNT,
        )
        task = asyncio.create_task(self.execute(run["id"]), name=f"alangym:{run['id']}")
        self._tasks[run["id"]] = task
        task.add_done_callback(lambda _: self._tasks.pop(run["id"], None))
        return run

    async def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def execute(self, run_id: str) -> None:
        try:
            run = self.database.get_run(run_id)
            if run["training_count"] != TRAINING_COUNT or run["heldout_count"] != HELDOUT_COUNT:
                raise ExperimentBlocked("This build is fixed to 6 training and 8 held-out tasks.")
            self.run_cache.mkdir(parents=True, exist_ok=True)
            run_dir = self.run_cache / run_id
            run_dir.mkdir(parents=True, exist_ok=True)

            repo_client = PublicGitHubRepository(run["repo"], self.repository_cache)
            self._transition(run_id, "mining", "Checking the public GitHub repository and merged PRs")
            repository_path = await asyncio.to_thread(repo_client.prepare)
            candidates = await asyncio.to_thread(repo_client.candidates)
            self._progress(
                run_id,
                "candidate_count",
                f"Found {len(candidates)} small PRs with source and test changes",
                {"count": len(candidates)},
            )
            retained_validation = self._load_retained_validation_candidates(
                run_id, run["repo"], candidates
            )

            python = await self._prepare_test_environment(
                repository_path, run_id, run_dir, repo_slug=run["repo"]
            )
            self._transition(run_id, "validating", "Checking each task fails before and passes after its PR")
            verified, validation_failures = await self._validate_candidates(
                run_id,
                candidates,
                repository_path,
                python,
                run_dir / "validation",
                required=retained_validation,
                fresh_count=FRESH_TASK_COUNT,
            )
            retained_numbers = {candidate.number for candidate in retained_validation}
            fresh_tasks = [item for item in verified if item.number not in retained_numbers]
            training, retained_validation, new_heldout = build_benchmark_split(
                fresh_tasks, retained_validation, seed=run["seed"],
                train_count=TRAINING_COUNT, new_heldout_count=FRESH_HELDOUT_COUNT,
            )
            heldout = retained_validation + new_heldout
            verified = training + heldout

            # Keep seeded membership while running shorter tasks first for faster feedback.
            def task_priority(task: PullRequestCandidate) -> tuple[int, int, int, int]:
                return (
                    len(task.body.strip()),
                    len(task.source_paths),
                    len(task.test_paths),
                    task.number,
                )

            verified = sorted(verified, key=task_priority)
            training = sorted(training, key=task_priority)
            heldout = sorted(heldout, key=task_priority)
            train_numbers = {candidate.number for candidate in training}
            task_rows = [
                self._task_record(
                    candidate,
                    "train" if candidate.number in train_numbers else "heldout",
                    heldout_cohort=(
                        "retained_validation" if candidate.number in retained_numbers
                        else "fresh" if candidate.number not in train_numbers else None
                    ),
                )
                for candidate in verified
            ]
            self.database.save_tasks(run_id, task_rows)
            self._progress(
                run_id,
                "split_ready",
                "Validated 14 PRs and froze the 6/8 task split",
                {
                    "training": [item.number for item in training],
                    "heldout": [item.number for item in heldout],
                    "retained_validation": [item.number for item in retained_validation],
                    "fresh_heldout": [item.number for item in new_heldout],
                    "seed": run["seed"],
                },
            )

            skill_path = self.project_root / SKILL_RELATIVE_PATH
            if not skill_path.is_file():
                raise ExperimentBlocked(f"The starter Alan skill is missing: {skill_path}")
            current_skill = skill_path.read_text(encoding="utf-8")
            current_version = _skill_version(current_skill)
            self.database.save_skill_version(run_id, current_version, current_skill)

            self._transition(run_id, "baseline", "Running the baseline skill on all 14 tasks")
            baseline = await self._evaluate(
                run_id, repository_path, python, run_dir, verified, current_skill, current_version,
                validation_failures=validation_failures,
                phase="baseline", trial_prefix="baseline",
            )
            if baseline is None:
                return
            self._record_score(run_id, "baseline", baseline, verified)
            training_ids = {item.number for item in training}
            baseline_training = [item for item in baseline if item["pr_number"] in training_ids]
            baseline_heldout = [item for item in baseline if item["pr_number"] not in training_ids]
            self._record_metric(
                run_id, "training", "baseline", baseline_training, current_version, round_number=0
            )
            self._record_metric(
                run_id, "heldout", "baseline", baseline_heldout, current_version
            )

            for round_number in range(1, MAX_ROUNDS + 1):
                if self.database.get_run(run_id)["state"] != "training":
                    self._transition(
                        run_id, "training", f"Running training tasks for round {round_number}",
                        round_number=round_number,
                    )
                current_training = await self._evaluate(
                    run_id, repository_path, python, run_dir, training, current_skill, current_version,
                    validation_failures=validation_failures,
                    phase=f"round_{round_number}_current", trial_prefix=f"r{round_number}-current",
                    round_number=round_number,
                )
                if current_training is None:
                    return
                current_passes = _pass_count(current_training)
                self._transition(
                    run_id, "evolving",
                    f"Asking Alan to improve the skill from round {round_number} training failures",
                    round_number=round_number,
                    payload={"training_passes": current_passes, "training_total": len(training)},
                )
                proposal = await self._propose_skill(
                    run_id, run_dir, round_number, current_skill, training, current_training
                )
                if proposal is None:
                    self._record_metric(
                        run_id, "training", f"round_{round_number}", current_training,
                        current_version, round_number=round_number,
                    )
                    self._progress(
                        run_id, "proposal_skipped",
                        f"Round {round_number}: no valid skill proposal was produced",
                        {"round": round_number}, round_number=round_number,
                    )
                    continue

                candidate_skill, proposal_result = proposal
                candidate_version = _skill_version(candidate_skill)
                if candidate_version == current_version:
                    self._record_metric(
                        run_id, "training", f"round_{round_number}", current_training,
                        current_version, round_number=round_number,
                    )
                    self._progress(
                        run_id, "proposal_unchanged",
                        f"Round {round_number}: Alan left the skill unchanged",
                        {"round": round_number}, round_number=round_number,
                    )
                    continue
                candidate_training = await self._evaluate(
                    run_id, repository_path, python, run_dir, training, candidate_skill,
                    candidate_version, validation_failures=validation_failures,
                    phase=f"round_{round_number}_candidate",
                    trial_prefix=f"r{round_number}-candidate", round_number=round_number,
                )
                if candidate_training is None:
                    return
                candidate_passes = _pass_count(candidate_training)
                regressions = _prior_pass_regressions(current_training, candidate_training)
                if regressions:
                    task_by_number = {item.number: item for item in training}
                    retry_tasks = [task_by_number[number] for number in regressions]
                    retry_results = await self._evaluate(
                        run_id, repository_path, python, run_dir, retry_tasks, candidate_skill,
                        candidate_version, validation_failures=validation_failures,
                        phase=f"round_{round_number}_ratchet_retry",
                        trial_prefix=f"r{round_number}-ratchet-retry", round_number=round_number,
                    )
                    if retry_results is None:
                        return
                    flaky_numbers = _flaky_pr_numbers(
                        [result for result in candidate_training if result["pr_number"] in regressions],
                        retry_results,
                    )
                    self._record_metric(
                        run_id, "training", f"round_{round_number}", current_training,
                        current_version, round_number=round_number,
                    )
                    self._progress(
                        run_id,
                        "skill_rejected",
                        f"Round {round_number}: ratchet rejected a candidate that regressed prior passing tasks",
                        {
                            "round": round_number,
                            "version": candidate_version,
                            "parent_version": current_version,
                            "accepted": False,
                            "reason": "prior_pass_regression",
                            "current_passes": current_passes,
                            "candidate_passes": candidate_passes,
                            "flipped_pr_numbers": regressions,
                            "retry_outcomes": [
                                {"pr_number": result["pr_number"], "passed": result["passed"]}
                                for result in retry_results
                            ],
                            "flaky_pr_numbers": flaky_numbers,
                            "proposal_trace": _agent_trace(proposal_result),
                        },
                        round_number=round_number,
                    )
                elif (
                    candidate_passes > current_passes
                    and all(result.get("passed") in (True, False) for result in candidate_training)
                ):
                    skill_path = self.project_root / SKILL_RELATIVE_PATH
                    self._accept_skill(
                        run_id, skill_path, candidate_skill, current_skill, current_version,
                        candidate_version, candidate_passes, round_number, proposal_result,
                        source_pr_numbers=[
                            result["pr_number"] for result in current_training
                            if result.get("passed") is False
                        ],
                        current_passes=current_passes,
                    )
                    current_skill = candidate_skill
                    current_version = candidate_version
                    self._record_metric(
                        run_id, "training", f"round_{round_number}", candidate_training,
                        current_version, round_number=round_number,
                    )
                else:
                    self._record_metric(
                        run_id, "training", f"round_{round_number}", current_training,
                        current_version, round_number=round_number,
                    )
                    self._progress(
                        run_id, "skill_rejected",
                        f"Round {round_number}: kept the current skill ({candidate_passes}/6 did not improve on {current_passes}/6)",
                        {
                            "round": round_number,
                            "candidate_version": candidate_version,
                            "parent_version": current_version,
                            "candidate_passes": candidate_passes,
                            "current_passes": current_passes,
                            "accepted": False,
                            "reason": "no_training_improvement_or_unknown_result",
                            "flipped_pr_numbers": [],
                            "proposal_trace": _agent_trace(proposal_result),
                        },
                        round_number=round_number,
                    )

            self._transition(run_id, "evaluating", "Measuring the selected skill on held-out tasks")
            final = await self._evaluate(
                run_id, repository_path, python, run_dir, heldout, current_skill, current_version,
                validation_failures=validation_failures,
                phase="final", trial_prefix="final",
            )
            if final is None:
                return
            self._record_score(run_id, "final", final, heldout)
            self._record_metric(run_id, "heldout", "final", final, current_version)
            self._transition(
                run_id, "complete",
                "Experiment finished with baseline and final held-out results saved",
                payload={"skill_version": current_version},
            )
            self._publish_replay(run_id)
        except asyncio.CancelledError:
            self._mark_terminal(run_id, "cancelled", "Run cancelled by the user")
        except ExperimentBlocked as error:
            self._mark_terminal(run_id, "blocked", str(error))
        except (RepositoryError, InsufficientTasksError, TaskValidationError) as error:
            self._mark_terminal(run_id, "blocked", str(error))
        except Exception as error:
            self._mark_terminal(run_id, "failed", f"{type(error).__name__}: {error}")

    async def _prepare_test_environment(
        self, repository_path: Path, run_id: str, run_dir: Path, *, repo_slug: str
    ) -> Path:
        uv = shutil.which("uv")
        if uv is None:
            raise ExperimentBlocked("Install uv so AlanGym can create the pinned pytest environment.")
        self._progress(run_id, "environment", "Installing the repository's locked pytest environment", {})
        lockfile = repository_path / "uv.lock"
        if repo_slug.casefold() == "mahmoud/boltons":
            manifest = repository_path / "pyproject.toml"
            manifest_text = manifest.read_text(encoding="utf-8")
            compatibility_pin = "pytest>=8,<9; python_version >= '3.8'"
            if compatibility_pin not in manifest_text:
                pin_result = await _run_process(
                    [uv, "add", "--python", "3.13", "--dev", compatibility_pin, "--no-sync"],
                    repository_path,
                    timeout=300,
                )
                if pin_result[0] != 0:
                    raise ExperimentBlocked(
                        "Could not apply the local pytest 8 compatibility pin needed by the repo's conftest.\n"
                        + pin_result[1][-4_000:]
                    )
                lockfile = repository_path / "uv.lock"
        if not lockfile.is_file():
            lock_result = await _run_process(
                [uv, "lock", "--python", "3.13"], repository_path, timeout=300
            )
            if lock_result[0] != 0:
                raise ExperimentBlocked(
                    "Could not freeze the repository's dev dependencies.\n" + lock_result[1][-4_000:]
                )
        lock_copy = run_dir / "uv.lock"
        lock_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lockfile, lock_copy)
        shutil.copy2(repository_path / "pyproject.toml", run_dir / "test-pyproject.toml")
        lock_digest = hashlib.sha256(lock_copy.read_bytes()).hexdigest()
        process = await asyncio.create_subprocess_exec(
            uv, "sync", "--python", "3.13", "--locked", "--group", "dev", "--no-install-project",
            cwd=repository_path,
            env=_safe_process_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_creation_options(),
        )
        communication = asyncio.create_task(process.communicate())
        try:
            output_bytes, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=300)
        except TimeoutError as error:
            await _terminate_tree(process)
            await communication
            raise ExperimentBlocked("Installing the locked test environment exceeded 5 minutes.") from error
        except asyncio.CancelledError:
            await _terminate_tree(process)
            await communication
            raise
        output = output_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise ExperimentBlocked(
                "Could not install the repository's locked dev dependencies.\n" + output[-4_000:]
            )
        python = repository_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            raise ExperimentBlocked("uv completed but did not create the expected Python environment.")
        python_info = await _run_process(
            [str(python), "-c", "import platform, sys; print(platform.python_version()); print(sys.executable)"],
            repository_path,
            timeout=10,
        )
        uv_info = await _run_process([uv, "--version"], repository_path, timeout=10)
        python_version = python_info[1].splitlines()[0] if python_info[1].splitlines() else "unknown"
        self._progress(
            run_id,
            "environment_ready",
            "Locked pytest environment is ready",
            {
                "python_version": python_version,
                "uv": uv_info[1].strip(),
                "uv_lock_sha256": lock_digest,
            },
        )
        return python

    def _load_retained_validation_candidates(
        self,
        current_run_id: str,
        repo: str,
        candidates: list[PullRequestCandidate],
    ) -> list[PullRequestCandidate]:
        """Recover the prior four-task validation cohort only on exact PR identity."""
        candidate_by_number = {candidate.number: candidate for candidate in candidates}

        def match_rows(rows: list[dict[str, Any]]) -> list[PullRequestCandidate] | None:
            if len(rows) != RETAINED_VALIDATION_COUNT:
                return None
            matched: list[PullRequestCandidate] = []
            for row in rows:
                number = int(row["pr_number"])
                candidate = candidate_by_number.get(number)
                if candidate is None:
                    return None
                if (
                    candidate.merge_sha != row.get("merge_sha")
                    or candidate.base_sha != row.get("base_sha")
                    or sorted(candidate.source_paths) != sorted(row.get("source_paths", []))
                    or list(candidate.test_paths) != list(row.get("test_paths", []))
                ):
                    return None
                matched.append(candidate)
            return matched

        for previous in self.database.list_runs(limit=100):
            if (
                previous["id"] == current_run_id
                or previous["repo"].casefold() != repo.casefold()
                or previous["state"] != "complete"
            ):
                continue
            task_rows = self.database.list_tasks(previous["id"])
            retained = [
                row for row in task_rows
                if row.get("split") == "heldout"
                and row.get("heldout_cohort") == "retained_validation"
            ]
            if not retained and previous["heldout_count"] == RETAINED_VALIDATION_COUNT:
                retained = [row for row in task_rows if row.get("split") == "heldout"]
            matched = match_rows(retained)
            if matched is not None:
                return matched

        replay_path = self.project_root / "web" / "public" / "data" / "demo-run.json"
        try:
            replay = json.loads(replay_path.read_text(encoding="utf-8"))
            previous_run = replay.get("run", {})
            task_rows = replay.get("tasks", [])
        except (OSError, json.JSONDecodeError):
            previous_run, task_rows = {}, []
        if previous_run.get("repo", "").casefold() == repo.casefold():
            retained = [
                row for row in task_rows
                if row.get("split") == "heldout"
                and row.get("heldout_cohort") == "retained_validation"
            ]
            if not retained and previous_run.get("heldout_count") == RETAINED_VALIDATION_COUNT:
                retained = [row for row in task_rows if row.get("split") == "heldout"]
            matched = match_rows(retained)
            if matched is not None:
                return matched

        raise ExperimentBlocked(
            "Could not recover four exact held-out validation tasks from a completed local run "
            "or the saved public replay. No tasks were relabeled to fill the cohort."
        )

    async def _validate_candidates(
        self,
        run_id: str,
        candidates: list[PullRequestCandidate],
        repository_path: Path,
        python: Path,
        validation_root: Path,
        *,
        required: list[PullRequestCandidate],
        fresh_count: int,
    ) -> tuple[list[PullRequestCandidate], dict[int, str]]:
        required_numbers = {candidate.number for candidate in required}
        required_sources = set().union(*(candidate.source_paths for candidate in required))
        remaining = [
            candidate for candidate in candidates
            if candidate.number not in required_numbers
            and not candidate.source_paths.intersection(required_sources)
        ]
        fresh_selection = select_candidates(remaining, count=fresh_count)
        validated: set[int] = set()
        validation_failures: dict[int, str] = {}
        while True:
            selection = required + fresh_selection
            failed_candidate = False
            for candidate in selection:
                if candidate.number in validated:
                    continue
                self._progress(
                    run_id, "validating_task",
                    f"Validating PR #{candidate.number}: {candidate.title}",
                    {"pr_number": candidate.number},
                )
                try:
                    validation = await validate_task(
                        repository_path, candidate,
                        validation_root / f"pr-{candidate.number}", python,
                    )
                except TaskValidationError as error:
                    self._progress(
                        run_id, "task_rejected",
                        f"Skipped PR #{candidate.number}: its changed tests are not a clean regression",
                        {"pr_number": candidate.number, "reason": str(error)[:2_000]},
                    )
                    if candidate.number in required_numbers:
                        raise ExperimentBlocked(
                            f"Retained held-out PR #{candidate.number} no longer validates: {error}"
                        ) from error
                    remaining = [item for item in remaining if item.number != candidate.number]
                    fresh_selection = select_candidates(remaining, count=fresh_count)
                    failed_candidate = True
                    break
                self._progress(
                    run_id, "task_validated",
                    f"PR #{candidate.number} fails before and passes after its fix",
                    {
                        "pr_number": candidate.number,
                        "before_failed": validation.before_regression_count,
                        "after_passed": validation.after.passed_count,
                    },
                )
                validation_failures[candidate.number] = validation.before.output
                validated.add(candidate.number)
            if not failed_candidate:
                return selection, validation_failures

    async def _evaluate(
        self,
        run_id: str,
        repository_path: Path,
        python: Path,
        run_dir: Path,
        tasks: list[PullRequestCandidate],
        skill: str,
        skill_version: str,
        *,
        validation_failures: dict[int, str],
        phase: str,
        trial_prefix: str,
        round_number: int | None = None,
    ) -> list[dict[str, Any]] | None:
        results: list[dict[str, Any]] = []
        for candidate in tasks:
            trial_id = f"{trial_prefix}:pr-{candidate.number}"
            self._progress(
                run_id, "trial_started",
                f"{phase}: PR #{candidate.number} — {candidate.title}",
                {"trial_id": trial_id, "phase": phase, "pr_number": candidate.number},
                round_number=round_number,
            )
            workspace = run_dir / "trials" / trial_id.replace(":", "-") / "workspace"
            make_task_snapshot(repository_path, candidate, workspace, with_fix=False)
            baseline_files = _read_workspace(workspace)
            skill_path = workspace / SKILL_RELATIVE_PATH
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(skill, encoding="utf-8")
            agent = await self.runner.run(
                _task_instruction(
                    candidate, python, skill, validation_failures.get(candidate.number, "")
                ),
                workspace,
                timeout=EFFECTIVE_TIMEOUT, tool_path=python.parent,
            )
            engine_history = await asyncio.to_thread(read_session_history, workspace)
            if agent.status is RunStatus.AUTH_REQUIRED:
                trial = self._new_trial(
                    trial_id, candidate, phase, skill_version, "auth_required", None,
                    agent.elapsed_seconds, _agent_trace(agent),
                    engine_history=_engine_history_record(engine_history),
                )
                self.database.save_trial(run_id, trial)
                raise ExperimentBlocked(
                    "Alan Code needs a one-time device login. Run alan --login with the Alan Pro account, then start a new run."
                )

            policy_violation = _restore_disallowed_changes(
                workspace, baseline_files, set(candidate.source_paths), SKILL_RELATIVE_PATH
            )
            grade = await run_pytest(workspace, candidate.test_paths, python, timeout=60)
            passed = grade.status == "passed" and not policy_violation
            if grade.status in {"invalid", "timed_out"}:
                passed_value: bool | None = False if policy_violation else None
            else:
                passed_value = passed
            status = (
                "policy_violation" if policy_violation
                else "passed" if grade.status == "passed"
                else "failed" if grade.status == "failed"
                else f"grader_{grade.status}"
            )
            trace = json.dumps(
                {
                    "agent_status": agent.status.value,
                    "agent_stdout": agent.stdout,
                    "agent_stderr": agent.stderr,
                    "grader_output": grade.output,
                    "source_diff": _source_diff(baseline_files, workspace, set(candidate.source_paths)),
                    "unauthorized_changes_restored": policy_violation,
                },
                ensure_ascii=False,
            )
            trial = self._new_trial(
                trial_id, candidate, phase, skill_version, status, passed_value,
                    agent.elapsed_seconds + grade.elapsed_seconds, trace,
                    engine_history=_engine_history_record(engine_history),
            )
            self.database.save_trial(run_id, trial)
            results.append(trial)
            self._progress(
                run_id, "trial_finished",
                f"{phase}: PR #{candidate.number} {'passed' if passed else 'did not pass'}",
                {
                    "trial_id": trial_id, "phase": phase, "pr_number": candidate.number,
                    "passed": passed_value, "status": status,
                    "elapsed_seconds": trial["elapsed_seconds"],
                },
                round_number=round_number,
            )
        return results

    async def _propose_skill(
        self,
        run_id: str,
        run_dir: Path,
        round_number: int,
        current_skill: str,
        training: list[PullRequestCandidate],
        results: list[dict[str, Any]],
    ) -> tuple[str, AgentResult] | None:
        workspace = run_dir / "evolution" / f"round-{round_number}"
        if workspace.exists():
            shutil.rmtree(workspace)
        skill_path = workspace / SKILL_RELATIVE_PATH
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(current_skill, encoding="utf-8")
        by_number = {task.number: task for task in training}
        failures: list[str] = []
        for result in results:
            if result["passed"] is not False:
                continue
            task = by_number[result["pr_number"]]
            failures.append(
                f"## PR #{task.number}: {task.title}\n{task.body[:1_200]}\n\n"
                f"Observed trial and grader output (trimmed):\n{str(result['trace'])[-4_000:]}"
            )
        report = "\n\n---\n\n".join(failures) or (
            "All current training trials passed. Look for a small simplification or leave the skill unchanged."
        )
        (workspace / "TRAINING_FAILURES.txt").write_text(report, encoding="utf-8")
        instruction = (
            "Improve the Alan skill using only the attached training task outcomes. "
            "Edit only .agents/skills/alangym/SKILL.md; keep its Alan front matter, "
            "keep it concise, and add only a specific practice supported by these failures. "
            "Do not create or edit any other file. Do not mention or infer held-out tasks. "
            "The current skill and TRAINING_FAILURES.txt are in this workspace.\n\n"
            "Training failures:\n" + report
        )
        result = await self.runner.run(instruction, workspace, timeout=EFFECTIVE_TIMEOUT)
        if result.status is RunStatus.AUTH_REQUIRED:
            raise ExperimentBlocked(
                "Alan Code needs a one-time device login. Run alan --login with the Alan Pro account, then start a new run."
            )
        if result.status is not RunStatus.COMPLETED:
            self._progress(
                run_id, "evolver_failed",
                f"Round {round_number}: Alan could not propose a skill edit",
                {"status": result.status.value, "trace": _agent_trace(result)},
                round_number=round_number,
            )
            return None
        content = skill_path.read_text(encoding="utf-8") if skill_path.is_file() else ""
        if not _valid_skill(content):
            self._progress(
                run_id, "proposal_invalid",
                f"Round {round_number}: rejected a skill proposal with invalid Alan format or length",
                {"trace": _agent_trace(result)}, round_number=round_number,
            )
            return None
        return content, result

    def _accept_skill(
        self,
        run_id: str,
        skill_path: Path,
        content: str,
        parent_content: str,
        parent_version: str,
        version: str,
        training_passes: int,
        round_number: int,
        proposal: AgentResult,
        *,
        source_pr_numbers: list[int],
        current_passes: int,
    ) -> None:
        skill_path.write_text(content, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.project_root), "add", "--", SKILL_RELATIVE_PATH],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project_root), "commit", "-m", "Tune repair skill"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project_root), "push"],
            check=True, capture_output=True, text=True,
        )
        self.database.save_skill_version(
            run_id, version, content,
            parent_version=parent_version, training_passes=training_passes,
        )
        self._progress(
            run_id, "skill_accepted",
            f"Round {round_number}: accepted skill {version} with {training_passes}/6 training passes",
            {
                "version": version,
                "parent_version": parent_version,
                "training_passes": training_passes,
                "current_passes": current_passes,
                "candidate_passes": training_passes,
                "accepted": True,
                "reason": "training_improved_without_prior_pass_regression",
                "flipped_pr_numbers": [],
                "source_pr_numbers": source_pr_numbers,
                "diff": _skill_text_diff(parent_content, content, parent_version, version),
                "proposal_trace": _agent_trace(proposal),
            },
            round_number=round_number,
        )

    def _record_score(
        self,
        run_id: str,
        label: str,
        results: list[dict[str, Any]],
        tasks: list[PullRequestCandidate],
    ) -> None:
        self._progress(
            run_id, "score",
            f"{label}: {_pass_count(results)}/{len(tasks)} regression tests passed",
            {
                "label": label,
                "passed": _pass_count(results),
                "total": len(tasks),
                "rate": _pass_count(results) / len(tasks) if tasks else 0,
            },
        )

    def _record_metric(
        self,
        run_id: str,
        split: str,
        label: str,
        results: list[dict[str, Any]],
        skill_version: str,
        *,
        round_number: int | None = None,
    ) -> None:
        self._progress(
            run_id,
            "metric",
            f"{split} {label}: {_pass_count(results)}/{len(results)} passed",
            {
                "split": split,
                "label": label,
                "round": round_number,
                "skill_version": skill_version,
                "passed": _pass_count(results),
                "total": len(results),
                "rate": _pass_count(results) / len(results) if results else 0,
            },
            round_number=round_number,
        )

    @staticmethod
    def _task_record(
        candidate: PullRequestCandidate,
        split: str,
        *,
        heldout_cohort: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": f"pr-{candidate.number}",
            "pr_number": candidate.number,
            "title": candidate.title,
            "body": candidate.body,
            "merged_at": candidate.merged_at,
            "merge_sha": candidate.merge_sha,
            "base_sha": candidate.base_sha,
            "source_paths": sorted(candidate.source_paths),
            "test_paths": list(candidate.test_paths),
            "fixture_paths": list(candidate.fixture_paths),
            "split": split,
            "heldout_cohort": heldout_cohort,
        }

    @staticmethod
    def _new_trial(
        trial_id: str,
        candidate: PullRequestCandidate,
        phase: str,
        skill_version: str,
        status: str,
        passed: bool | None,
        elapsed_seconds: float,
        trace: str,
        *,
        engine_history: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        history = engine_history or {"status": "unavailable", "reason": "no_match"}
        usage = history.get("history", {}).get("usage", {})
        return {
            "id": trial_id,
            "task_id": f"pr-{candidate.number}",
            "pr_number": candidate.number,
            "phase": phase,
            "skill_version": skill_version,
            "status": status,
            "passed": passed,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "cost_usd": None,
            "cost_verified": False,
            "model": DEFAULT_MODEL,
            "actual_model": history.get("history", {}).get("model_id"),
            "tokens_input": usage.get("input_tokens"),
            "tokens_output": usage.get("output_tokens"),
            "tokens_reasoning": usage.get("reasoning_tokens"),
            "engine_history": history,
            "trace": trace,
        }

    def _transition(
        self, run_id: str, state: str, message: str, *,
        round_number: int | None = None, payload: dict[str, Any] | None = None,
    ) -> None:
        self.database.transition(run_id, state, message, round_number=round_number, payload=payload)

    def _progress(
        self, run_id: str, event_type: str, message: str, payload: dict[str, Any], *,
        round_number: int | None = None,
    ) -> None:
        self.database.record_progress(
            run_id, event_type, message, payload=payload, round_number=round_number,
        )

    def _mark_terminal(self, run_id: str, state: str, message: str) -> None:
        try:
            run = self.database.get_run(run_id)
            if run["state"] not in TERMINAL_STATES:
                self.database.transition(run_id, state, message)
        except (KeyError, ValueError):
            return

    def _publish_replay(self, run_id: str) -> None:
        replay_path = self.project_root / "web" / "public" / "data" / "demo-run.json"
        try:
            export_replay(self.database, run_id, replay_path)
            changed = subprocess.run(
                ["git", "-C", str(self.project_root), "status", "--short", "--", "web/public/data/demo-run.json"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            if not changed:
                return
            subprocess.run(
                ["git", "-C", str(self.project_root), "add", "--", "web/public/data/demo-run.json"],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-C", str(self.project_root), "commit", "-m", "Save run replay"],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-C", str(self.project_root), "push"],
                check=True, capture_output=True, text=True,
            )
            self._progress(
                run_id, "replay_published", "Saved the privacy-filtered public run replay", {},
            )
        except Exception as error:
            self._progress(
                run_id,
                "replay_publish_failed",
                "The run finished locally, but its static replay could not be published",
                {"error": f"{type(error).__name__}: {error}"},
            )


def _safe_process_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in SAFE_ENVIRONMENT_VARIABLES
    }
    environment.update({"CI": "1", "PYTHONNOUSERSITE": "1"})
    return environment


def _creation_options() -> dict[str, object]:
    return {"creationflags": 0x00000200} if os.name == "nt" else {}


async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        except (OSError, TimeoutError):
            process.kill()
    else:
        process.kill()


async def _run_process(command: list[str], cwd: Path, *, timeout: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=_safe_process_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **_creation_options(),
    )
    communication = asyncio.create_task(process.communicate())
    try:
        output_bytes, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
    except TimeoutError:
        await _terminate_tree(process)
        await communication
        return -1, "Command exceeded its time limit."
    except asyncio.CancelledError:
        await _terminate_tree(process)
        await communication
        raise
    return process.returncode or 0, output_bytes.decode("utf-8", errors="replace")


def _task_instruction(
    candidate: PullRequestCandidate, python: Path, skill: str, validation_failure: str
) -> str:
    tests = " ".join(candidate.test_paths)
    allowed = "\n".join(f"- {path}" for path in sorted(candidate.source_paths))
    description = candidate.body.strip()[:3_000] or "No description was provided."
    failure = validation_failure[-4_000:].strip() or "No failure output was available."
    skill_instruction = (
        "Follow this Alan skill:\n" + skill
        if skill else "Use no extra repair skill or repository-local skill instructions."
    )
    return (
        f"Fix this regression in the repository using only these source files:\n{allowed}\n\n"
        f"Task from merged PR #{candidate.number}: {candidate.title}\n\n{description}\n\n"
        "Make the smallest source fix and report the change briefly.\n\n"
        + skill_instruction
        + f"\n\nHarness rule: the harness will run {python.name} -m pytest -q {tests} after you finish. "
        + "You may read tests, but do not run tests or edit tests, project files, or other repository files. "
        + "This rule overrides any conflicting test instruction in the skill.\n\n"
        + "Pre-fix test failure output:\n"
        + failure
    )


def _skill_version(content: str) -> str:
    return "v-" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]


def _valid_skill(content: str) -> bool:
    if not content or len(content.encode("utf-8")) > SKILL_BYTE_LIMIT or "\x00" in content:
        return False
    lines = content.splitlines()
    if len(lines) > SKILL_LINE_LIMIT or not lines or lines[0] != "---":
        return False
    try:
        closing = lines.index("---", 1)
    except ValueError:
        return False
    header = "\n".join(lines[1:closing])
    return "name:" in header and "description:" in header and closing < len(lines) - 1


def _pass_count(results: list[dict[str, Any]]) -> int:
    return sum(result.get("passed") is True for result in results)


def _prior_pass_regressions(
    current: list[dict[str, Any]], candidate: list[dict[str, Any]],
) -> list[int]:
    """Return tasks a candidate changed from a known pass to a known failure."""
    current_by_pr = {int(result["pr_number"]): result for result in current}
    candidate_by_pr = {int(result["pr_number"]): result for result in candidate}
    return sorted(
        pr_number
        for pr_number, current_result in current_by_pr.items()
        if current_result.get("passed") is True
        and candidate_by_pr.get(pr_number, {}).get("passed") is False
    )


def _flaky_pr_numbers(
    first_attempts: list[dict[str, Any]], retries: list[dict[str, Any]],
) -> list[int]:
    """Flag a task when its retry disagrees with the candidate's first attempt."""
    first_by_pr = {int(result["pr_number"]): result for result in first_attempts}
    retry_by_pr = {int(result["pr_number"]): result for result in retries}
    return sorted(
        pr_number
        for pr_number, first in first_by_pr.items()
        if first.get("passed") in (True, False)
        and retry_by_pr.get(pr_number, {}).get("passed") in (True, False)
        and first["passed"] is not retry_by_pr[pr_number]["passed"]
    )


def _engine_history_record(result: SessionHistoryResult) -> dict[str, Any]:
    if result.status is not HistoryStatus.AVAILABLE or result.history is None:
        return {
            "status": result.status.value,
            "reason": result.reason.value if result.reason is not None else None,
        }
    return {"status": "available", "history": asdict(result.history)}


def _agent_trace(result: AgentResult) -> str:
    return json.dumps(
        {
            "status": result.status.value,
            "model": result.model,
            "returncode": result.returncode,
            "elapsed_seconds": round(result.elapsed_seconds, 3),
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
        ensure_ascii=False,
    )


def _read_workspace(workspace: Path) -> dict[str, bytes | None]:
    files: dict[str, bytes | None] = {}
    for root, directories, filenames in os.walk(workspace, followlinks=False):
        root_path = Path(root)
        for name in list(directories):
            path = root_path / name
            if path.is_symlink():
                files[path.relative_to(workspace).as_posix()] = None
                directories.remove(name)
        for name in filenames:
            path = root_path / name
            relative = path.relative_to(workspace).as_posix()
            files[relative] = None if path.is_symlink() else path.read_bytes()
    return files


def _restore_disallowed_changes(
    workspace: Path, original: dict[str, bytes | None], allowed_sources: set[str],
    allowed_skill: str,
) -> bool:
    current = _read_workspace(workspace)
    changed = False
    for relative in original.keys() | current.keys():
        if relative in allowed_sources or relative == allowed_skill:
            if relative in current and current[relative] is None:
                changed = True
                _restore_file(workspace, relative, original.get(relative))
            continue
        if original.get(relative) != current.get(relative) or (relative not in original) != (relative not in current):
            changed = True
            _restore_file(workspace, relative, original.get(relative))
    return changed


def _restore_file(workspace: Path, relative: str, content: bytes | None) -> None:
    parsed = PurePosixPath(relative)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        return
    target = workspace.joinpath(*parsed.parts)
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
    elif target.exists():
        shutil.rmtree(target)
    if content is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _source_diff(
    original: dict[str, bytes | None], workspace: Path, allowed_sources: set[str]
) -> str:
    current = _read_workspace(workspace)
    chunks: list[str] = []
    for relative in sorted(allowed_sources):
        before = (original.get(relative) or b"").decode("utf-8", errors="replace").splitlines()
        after = (current.get(relative) or b"").decode("utf-8", errors="replace").splitlines()
        chunks.extend(
            difflib.unified_diff(
                before, after, fromfile=f"a/{relative}", tofile=f"b/{relative}", lineterm=""
            )
        )
    return "\n".join(chunks)[:12_000]


def _skill_text_diff(parent_content: str, content: str, parent_version: str, version: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            parent_content.splitlines(), content.splitlines(),
            fromfile=parent_version, tofile=version, lineterm="",
        )
    )
