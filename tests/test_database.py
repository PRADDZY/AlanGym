import json

import pytest

from alangym.database import ActiveRunError, InvalidTransitionError, RunDatabase


def test_database_persists_run_events_and_trial_outcomes_atomically(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")

    database.transition(run["id"], "mining", "Mining merged pull requests")
    database.record_progress(run["id"], "mined", "Found one task")
    database.save_tasks(
        run["id"],
        [
            {
                "id": "513",
                "pr_number": 513,
                "title": "Fix spooled writes",
                "split": "train",
            }
        ],
    )
    database.save_trial(
        run["id"],
        {
            "id": "baseline:513",
            "task_id": "513",
            "phase": "baseline",
            "skill_version": "starter",
            "status": "passed",
            "passed": True,
            "trace": "tests passed",
        },
    )

    reopened = RunDatabase(tmp_path / "runs.sqlite3")
    assert reopened.get_run(run["id"])["state"] == "mining"
    assert reopened.list_tasks(run["id"])[0]["split"] == "train"
    assert reopened.list_trials(run["id"])[0]["passed"] is True
    assert [event["sequence"] for event in reopened.list_events(run["id"])] == [1, 2, 3]


def test_only_one_run_can_be_active(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    database.create_run("mahmoud/boltons")

    with pytest.raises(ActiveRunError, match="already active"):
        database.create_run("another/repository")


def test_terminal_runs_cannot_be_resumed_or_relabelled(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")
    database.transition(run["id"], "mining", "Start")
    database.transition(run["id"], "blocked", "Alan login required")

    with pytest.raises(InvalidTransitionError, match="blocked.*mining"):
        database.transition(run["id"], "mining", "Resume")


def test_event_data_is_stored_as_json_and_unknown_runs_fail(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")
    database.transition(run["id"], "mining", "Inspecting PRs", payload={"count": 100})

    event = database.list_events(run["id"])[-1]
    assert json.loads(event["payload_json"]) == {"count": 100}
    with pytest.raises(KeyError, match="unknown run"):
        database.transition("missing", "mining", "Start")
