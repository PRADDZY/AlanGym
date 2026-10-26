import json

from alangym.database import RunDatabase
from alangym.replay import export_replay


def test_replay_exports_completed_results_and_scrubs_local_traces(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")
    database.transition(run["id"], "mining", "Mining")
    database.transition(run["id"], "validating", "Validating")
    database.transition(run["id"], "baseline", "Baseline")
    database.save_tasks(
        run["id"],
        [
            {
                "id": "pr-513",
                "pr_number": 513,
                "title": "Fix a bug",
                "merged_at": "2026-10-01T00:00:00Z",
                "merge_sha": "merge",
                "base_sha": "base",
                "source_paths": ["boltons/ioutils.py"],
                "test_paths": ["tests/test_ioutils.py"],
                "fixture_paths": [],
                "split": "train",
            }
        ],
    )
    database.save_trial(
        run["id"],
        {
            "id": "baseline:pr-513",
            "task_id": "pr-513",
            "pr_number": 513,
            "phase": "baseline",
            "skill_version": "v-one",
            "status": "failed",
            "passed": False,
            "elapsed_seconds": 1.0,
            "cost_usd": None,
            "model": "deepseek/deepseek-v4.1-flash",
            "trace": json.dumps(
                {
                    "agent_status": "completed",
                    "agent_stdout": "private agent chatter ghp_abcdefghijklmnopqrstuvwxyz123456",
                    "grader_output": "Failed in C:\\Users\\Pratik\\Desktop\\private\\test.py",
                    "source_diff": "- old\\n+ new",
                }
            ),
        },
    )
    database.transition(run["id"], "evaluating", "Final evaluation")
    database.transition(run["id"], "complete", "Done")

    output = export_replay(database, run["id"], tmp_path / "public" / "demo-run.json")
    raw = output.read_text(encoding="utf-8")
    replay = json.loads(raw)

    assert replay["run"]["state"] == "complete"
    assert replay["tasks"][0]["pr_number"] == 513
    assert replay["trials"][0]["trace"]["agent_status"] == "completed"
    assert replay["trials"][0]["trace"]["source_diff"] == "- old\\n+ new"
    assert "private agent chatter" not in raw
    assert "ghp_" not in raw
    assert "C:\\\\Users" not in raw


def test_replay_refuses_incomplete_runs(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")

    try:
        export_replay(database, run["id"], tmp_path / "demo-run.json")
    except ValueError as error:
        assert "completed run" in str(error)
    else:
        raise AssertionError("incomplete runs must not be published")
