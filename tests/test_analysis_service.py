import asyncio
import alangym.analysis_service as analysis_service
from alangym.analysis_service import AnalysisManager
from alangym.alan import AgentResult, RunStatus
from alangym.database import RunDatabase
from alangym.session_history import HistoryReason, HistoryStatus, SessionHistoryResult
from alangym.validation import TestResult


def _completed_run(database):
    run = database.create_run("mahmoud/boltons", heldout_count=2)
    for state in ("mining", "validating", "baseline", "evaluating", "complete"):
        database.transition(run["id"], state, state.title())
    database.save_tasks(
        run["id"],
        [
            {
                "id": "pr-101", "pr_number": 101, "title": "Training task", "body": "Fix it",
                "split": "train", "merge_sha": "merge-101", "base_sha": "base-101",
                "source_paths": ["boltons/mod_101.py"], "test_paths": ["tests/test_101.py"],
                "fixture_paths": [],
            },
            {
                "id": "pr-202", "pr_number": 202, "title": "Held-out task", "body": "Fix it",
                "split": "heldout", "merge_sha": "merge-202", "base_sha": "base-202",
                "source_paths": ["boltons/mod_202.py"], "test_paths": ["tests/test_202.py"],
                "fixture_paths": [],
            },
            {
                "id": "pr-303", "pr_number": 303, "title": "Another held-out task", "body": "Fix it",
                "split": "heldout", "merge_sha": "merge-303", "base_sha": "base-303",
                "source_paths": ["boltons/mod_303.py"], "test_paths": ["tests/test_303.py"],
                "fixture_paths": [],
            },
        ],
    )
    database.save_skill_version(run["id"], "v1", "# Repair\n- Run tests")
    for task_id, pr_number, passed in (
        ("pr-101", 101, True), ("pr-202", 202, False), ("pr-303", 303, False),
    ):
        database.save_trial(
            run["id"],
            {
                "id": f"baseline:{pr_number}", "task_id": task_id, "pr_number": pr_number,
                "phase": "baseline", "skill_version": "v1", "status": "complete",
                "passed": passed, "elapsed_seconds": 1.0,
            },
        )
    return run


def test_suite_analysis_builds_report_from_persisted_data_only(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    run = _completed_run(database)
    manager = AnalysisManager(database, project_root=tmp_path, data_root=tmp_path)

    async def execute():
        job = manager.start(run["id"], "suite", config={"include_steps": True})
        assert job["status"] == "queued"
        for _ in range(100):
            current = database.get_analysis_job(job["id"])
            if current["status"] in {"complete", "failed", "blocked"}:
                break
            await asyncio.sleep(0.01)
        assert current["status"] == "complete", current["message"]
        assert current["progress"] == 1
        assert current["result"]["heldout"]["baseline"]["passed"] == 0
        assert current["result"]["ladder"]["configs"][0]["status"] == "unavailable"
        await manager.shutdown()

    asyncio.run(execute())


def test_feature_analysis_reports_missing_evidence_as_unavailable(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    run = _completed_run(database)
    manager = AnalysisManager(database, project_root=tmp_path, data_root=tmp_path)

    async def execute():
        job = manager.start(run["id"], "xray")
        for _ in range(100):
            current = database.get_analysis_job(job["id"])
            if current["status"] in {"complete", "failed", "blocked"}:
                break
            await asyncio.sleep(0.01)
        assert current["status"] == "complete", current
        assert current["result"]["status"] == "unavailable"
        assert current["result"]["mode"] == "task_level"
        await manager.shutdown()

    asyncio.run(execute())


def test_ladder_runs_three_verified_configurations_without_topups(tmp_path, monkeypatch):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    run = _completed_run(database)
    repository_path = tmp_path / "repos" / "mahmoud--boltons"
    python = repository_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    runner_models = []

    class StubRunner:
        def __init__(self, model):
            self.model = model

        async def run(self, instruction, workspace, **kwargs):
            return AgentResult(
                RunStatus.COMPLETED, self.model, 0, "", "", 0.1,
            )

    def make_snapshot(_repo, task, destination, *, with_fix):
        assert with_fix is False
        (destination / "boltons").mkdir(parents=True, exist_ok=True)
        (destination / "tests").mkdir(parents=True, exist_ok=True)
        source = next(iter(task.source_paths))
        (destination / source).write_text("original", encoding="utf-8")
        (destination / task.test_paths[0]).write_text("def test_ok(): pass", encoding="utf-8")
        return destination

    async def grade(*_args, **_kwargs):
        return TestResult("passed", 0, 1, 0, "1 passed", 0.1)

    def runner_factory(model):
        runner_models.append(model)
        return StubRunner(model)

    monkeypatch.setattr(analysis_service, "make_task_snapshot", make_snapshot)
    monkeypatch.setattr(analysis_service, "run_pytest", grade)
    monkeypatch.setattr(
        analysis_service,
        "read_session_history",
        lambda _workspace: SessionHistoryResult(
            HistoryStatus.UNAVAILABLE, reason=HistoryReason.NO_MATCH
        ),
    )
    manager = AnalysisManager(
        database, project_root=tmp_path, data_root=tmp_path, runner_factory=runner_factory
    )

    async def execute():
        job = manager.start(run["id"], "ladder")
        for _ in range(200):
            current = database.get_analysis_job(job["id"])
            if current["status"] in {"complete", "failed", "blocked"}:
                break
            await asyncio.sleep(0.01)
        assert current["status"] == "complete", current["message"]
        trials = database.list_trials(run["id"])
        ladder_trials = [trial for trial in trials if trial["phase"] == "ladder"]
        assert len(ladder_trials) == 6
        assert {trial["config_id"] for trial in ladder_trials} == {
            "flash_no_skill", "flash_current_skill", "deepseek_pro_no_skill",
        }
        assert {trial["model"] for trial in ladder_trials} == {
            "deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-pro",
        }
        assert all(trial["cost_usd"] is None and trial["cost_verified"] is False for trial in ladder_trials)
        assert len(runner_models) == 6
        await manager.shutdown()

    asyncio.run(execute())
