import pytest

from alangym.database import RunDatabase
from alangym.experiment import ExperimentBlocked, ExperimentManager
from alangym.mining import PullRequestCandidate


def _complete_run(database, *, changed_sha=False):
    run = database.create_run("mahmoud/boltons", training_count=6, heldout_count=4)
    for state in ("mining", "validating", "baseline", "evaluating", "complete"):
        database.transition(run["id"], state, state)
    candidates = []
    rows = []
    for number in range(100, 104):
        source = f"boltons/module_{number}.py"
        test = f"tests/test_{number}.py"
        merged_sha = f"merge-{number}"
        candidate = PullRequestCandidate(
            number=number, title=f"Fix {number}", body="Regression fix",
            merged_at="2026-10-01T00:00:00Z", merge_sha=merged_sha,
            base_sha=f"base-{number}", source_paths=frozenset({source}), test_paths=(test,),
        )
        candidates.append(candidate)
        rows.append(
            {
                "id": f"pr-{number}", "pr_number": number, "title": candidate.title,
                "body": candidate.body, "merged_at": candidate.merged_at,
                "merge_sha": "wrong-sha" if changed_sha and number == 100 else merged_sha,
                "base_sha": candidate.base_sha, "source_paths": [source], "test_paths": [test],
                "fixture_paths": [], "split": "heldout",
            }
        )
    database.save_tasks(run["id"], rows)
    return run, candidates


def test_retained_validation_tasks_are_recovered_by_exact_pr_identity(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    old_run, candidates = _complete_run(database)
    current_run = database.create_run("mahmoud/boltons")
    manager = ExperimentManager(database, project_root=tmp_path, data_root=tmp_path)

    selected = manager._load_retained_validation_candidates(
        current_run["id"], "mahmoud/boltons", candidates
    )

    assert {item.number for item in selected} == {100, 101, 102, 103}
    assert database.get_run(old_run["id"])["state"] == "complete"


def test_retained_validation_tasks_are_not_replaced_when_identity_changed(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    _old_run, candidates = _complete_run(database, changed_sha=True)
    current_run = database.create_run("mahmoud/boltons")
    manager = ExperimentManager(database, project_root=tmp_path, data_root=tmp_path)

    with pytest.raises(ExperimentBlocked, match="four exact held-out validation tasks"):
        manager._load_retained_validation_candidates(
            current_run["id"], "mahmoud/boltons", candidates
        )
