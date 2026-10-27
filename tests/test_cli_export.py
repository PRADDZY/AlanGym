import json
import sys

import pytest

from alangym.cli import main
from alangym.database import RunDatabase


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["alangym", "export", *args])
    main()


def test_export_includes_saved_ladder_and_supports_output_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("alangym.cli.PROJECT_ROOT", tmp_path)
    database = RunDatabase(tmp_path / ".alangym" / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")
    database.save_tasks(run["id"], [{
        "id": "pr-1", "pr_number": 1, "title": "Fix", "merged_at": "2026-01-01",
        "merge_sha": "merge", "base_sha": "base", "source_paths": ["a.py"],
        "test_paths": ["test_a.py"], "fixture_paths": [], "split": "heldout",
    }])
    for state in ("mining", "validating", "baseline", "evaluating", "complete"):
        database.transition(run["id"], state, state)
    database.save_trial(run["id"], {
        "id": "ladder:1", "task_id": "pr-1", "pr_number": 1, "phase": "ladder",
        "skill_version": "v1", "status": "passed", "passed": True,
        "config_id": "flash_current_skill", "model": "deepseek/deepseek-v4.1-flash",
        "skill_mode": "current", "elapsed_seconds": 1,
    })

    invoke(monkeypatch, run["id"])
    default = tmp_path / ".alangym" / "exports" / f"{run['id']}.json"
    exported = json.loads(default.read_text(encoding="utf-8"))
    assert exported["trials"][0]["phase"] == "ladder"
    ladder = exported["analysis_report"]["ladder"]["configs"]
    current_skill = next(item for item in ladder if item["id"] == "flash_current_skill")
    assert current_skill["passed"] == 1
    assert current_skill["total"] == 1

    custom = tmp_path / "review" / "replay.json"
    invoke(monkeypatch, run["id"], "--output", str(custom))
    assert json.loads(custom.read_text(encoding="utf-8"))["run"]["id"] == run["id"]


def test_export_rejects_missing_database_without_creating_it(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("alangym.cli.PROJECT_ROOT", tmp_path)
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "missing")
    assert error.value.code != 0
    assert "database" in capsys.readouterr().err.lower()
    assert not (tmp_path / ".alangym" / "runs.sqlite3").exists()


def test_export_rejects_incomplete_and_unknown_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("alangym.cli.PROJECT_ROOT", tmp_path)
    database = RunDatabase(tmp_path / ".alangym" / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")
    for run_id, message in ((run["id"], "completed"), ("unknown", "unknown run")):
        with pytest.raises(SystemExit) as error:
            invoke(monkeypatch, run_id)
        assert error.value.code != 0
        assert message in capsys.readouterr().err.lower()
    assert not (tmp_path / ".alangym" / "exports").exists()
