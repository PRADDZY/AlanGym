import json
import subprocess
from types import SimpleNamespace

import pytest

from alangym.database import RunDatabase
from alangym.alan import AgentResult, RunStatus
from alangym.experiment import ExperimentBlocked, ExperimentManager, SKILL_RELATIVE_PATH


def test_completed_replay_is_saved_for_review_without_publication(tmp_path, monkeypatch):
    data_root = tmp_path / ".alangym"
    database = RunDatabase(data_root / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")
    for state in ("mining", "validating", "baseline", "evaluating", "complete"):
        database.transition(run["id"], state, state)
    public_replay = tmp_path / "web" / "public" / "data" / "demo-run.json"
    public_replay.parent.mkdir(parents=True)
    public_replay.write_text("existing replay", encoding="utf-8")

    def unexpected_git(*args, **kwargs):
        raise AssertionError("saving a replay must not invoke git")

    monkeypatch.setattr("alangym.experiment.subprocess.run", unexpected_git)
    manager = ExperimentManager(database, project_root=tmp_path, data_root=data_root)
    manager._save_replay_for_review(run["id"])

    saved = data_root / "exports" / f"{run['id']}.json"
    assert json.loads(saved.read_text(encoding="utf-8"))["run"]["id"] == run["id"]
    assert public_replay.read_text(encoding="utf-8") == "existing replay"
    assert any(event["type"] == "replay_saved" for event in database.list_events(run["id"]))


def test_accepted_skill_is_committed_locally_without_pushing_other_changes(tmp_path, monkeypatch):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")
    manager = ExperimentManager(database, project_root=tmp_path, data_root=tmp_path / "data")
    skill_path = tmp_path / SKILL_RELATIVE_PATH
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("old skill", encoding="utf-8")
    calls = []
    def capture(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="")
    monkeypatch.setattr("alangym.experiment.subprocess.run", capture)
    proposal = AgentResult(RunStatus.COMPLETED, "model", 0, "", "", 1.0)

    manager._accept_skill(
        run["id"], skill_path, "new skill", "old skill", "old", "new", 5, 1, proposal,
        source_pr_numbers=[123], current_passes=4,
    )

    assert skill_path.read_text(encoding="utf-8") == "new skill"
    assert len(calls) == 2
    assert all("push" not in call for call in calls)
    assert calls[1][-2:] == ["--", SKILL_RELATIVE_PATH]
    assert "--only" in calls[1]


@pytest.mark.parametrize("staged", [False, True])
def test_accepting_skill_preserves_user_changes(tmp_path, staged):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    skill_path = tmp_path / SKILL_RELATIVE_PATH
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("old skill", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", SKILL_RELATIVE_PATH], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "commit.gpgsign=false", "commit", "-qm", "Starter"], check=True)
    skill_path.write_text("user edit", encoding="utf-8")
    if staged:
        subprocess.run(["git", "-C", str(tmp_path), "add", "--", SKILL_RELATIVE_PATH], check=True)
        skill_path.write_text("old skill", encoding="utf-8")
    database = RunDatabase(tmp_path / ".alangym" / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")
    manager = ExperimentManager(database, project_root=tmp_path, data_root=tmp_path / ".alangym")
    proposal = AgentResult(RunStatus.COMPLETED, "model", 0, "", "", 1.0)

    with pytest.raises(ExperimentBlocked, match="local changes"):
        manager._accept_skill(
            run["id"], skill_path, "new skill", "old skill", "old", "new", 5, 1, proposal,
            source_pr_numbers=[123], current_passes=4,
        )

    assert skill_path.read_text(encoding="utf-8") == ("old skill" if staged else "user edit")
    assert not database.list_skill_versions(run["id"])


@pytest.mark.parametrize("concurrent_edit", [False, True])
def test_failed_skill_commit_preserves_original_or_concurrent_edit(tmp_path, monkeypatch, concurrent_edit):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")
    manager = ExperimentManager(database, project_root=tmp_path, data_root=tmp_path / "data")
    skill_path = tmp_path / SKILL_RELATIVE_PATH
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("old skill", encoding="utf-8")

    def fail_commit(command, **kwargs):
        if "status" in command:
            return SimpleNamespace(stdout="")
        if concurrent_edit:
            skill_path.write_text("concurrent edit", encoding="utf-8")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("alangym.experiment.subprocess.run", fail_commit)
    proposal = AgentResult(RunStatus.COMPLETED, "model", 0, "", "", 1.0)
    with pytest.raises(subprocess.CalledProcessError):
        manager._accept_skill(
            run["id"], skill_path, "new skill", "old skill", "old", "new", 5, 1, proposal,
            source_pr_numbers=[123], current_passes=4,
        )
    assert skill_path.read_text(encoding="utf-8") == ("concurrent edit" if concurrent_edit else "old skill")
    assert not database.list_skill_versions(run["id"])


def test_skill_commit_keeps_unrelated_staged_changes(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(tmp_path), "config", key, value], check=True)
    skill_path = tmp_path / SKILL_RELATIVE_PATH
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("old skill", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("original", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", SKILL_RELATIVE_PATH, "other.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "Starter"], check=True)
    other.write_text("staged edit", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", "other.txt"], check=True)
    database = RunDatabase(tmp_path / ".alangym" / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")
    manager = ExperimentManager(database, project_root=tmp_path, data_root=tmp_path / ".alangym")
    proposal = AgentResult(RunStatus.COMPLETED, "model", 0, "", "", 1.0)

    manager._accept_skill(
        run["id"], skill_path, "new skill", "old skill", "old", "new", 5, 1, proposal,
        source_pr_numbers=[123], current_passes=4,
    )

    committed = subprocess.run(["git", "-C", str(tmp_path), "show", f"HEAD:{SKILL_RELATIVE_PATH}"], check=True, capture_output=True, text=True)
    staged = subprocess.run(["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"], check=True, capture_output=True, text=True)
    assert committed.stdout == "new skill"
    assert staged.stdout.strip() == "other.txt"
    assert database.list_skill_versions(run["id"])[0]["version"] == "new"
