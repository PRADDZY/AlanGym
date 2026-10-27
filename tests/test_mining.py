import pytest

from alangym.mining import (
    parse_candidate,
    InsufficientTasksError,
    PullRequestCandidate,
    build_benchmark_split,
    select_candidates,
    split_tasks,
)


def candidate(number, source_paths, *, merged_at=None):
    return PullRequestCandidate(
        number=number,
        title=f"Fix {number}",
        body="Add a regression test.",
        merged_at=merged_at or f"2026-10-{number:02d}T00:00:00Z",
        merge_sha=f"merge-{number}",
        base_sha=f"base-{number}",
        source_paths=frozenset(source_paths),
        test_paths=(f"tests/test_{number}.py",),
    )


def test_select_tasks_rejects_overlapping_sources_and_sorts_newest_first():
    candidates = [
        candidate(1, ["boltons/listutils.py"], merged_at="2026-01-01T00:00:00Z"),
        candidate(3, ["boltons/urlutils.py"], merged_at="2026-03-01T00:00:00Z"),
        candidate(2, ["boltons/listutils.py"], merged_at="2026-02-01T00:00:00Z"),
    ]

    selected = select_candidates(candidates, count=2)

    assert [item.number for item in selected] == [3, 2]
    assert selected[0].source_paths.isdisjoint(selected[1].source_paths)


def test_task_split_is_fixed_six_four_and_reproducible():
    tasks = [candidate(number, [f"boltons/module_{number}.py"]) for number in range(1, 11)]

    training, heldout = split_tasks(tasks, train_count=6, seed=42)

    assert len(training) == 6
    assert len(heldout) == 4
    assert (training, heldout) == split_tasks(tasks, train_count=6, seed=42)
    assert {item.number for item in training}.isdisjoint(item.number for item in heldout)


def test_augmented_benchmark_keeps_four_validation_tasks_and_mines_four_fresh_holdouts():
    validation = [candidate(number, [f"boltons/old_{number}.py"]) for number in range(90, 94)]
    fresh = [candidate(number, [f"boltons/new_{number}.py"]) for number in range(1, 11)]

    training, reused, new_heldout = build_benchmark_split(
        fresh, validation, seed=42,
    )

    assert len(training) == 6
    assert len(reused) == len(new_heldout) == 4
    assert {item.number for item in reused} == {90, 91, 92, 93}
    assert len({item.number for item in training + reused + new_heldout}) == 14
    assert {item.number for item in training}.isdisjoint(
        item.number for item in reused + new_heldout
    )


def test_augmented_benchmark_rejects_fresh_tasks_that_overlap_validation_sources():
    validation = [
        candidate(number, [f"boltons/module_{number - 89}.py"])
        for number in range(90, 94)
    ]
    fresh = [candidate(number, [f"boltons/module_{number}.py"]) for number in range(1, 5)]
    fresh += [candidate(number, [f"boltons/new_{number}.py"]) for number in range(10, 16)]

    with pytest.raises(InsufficientTasksError):
        build_benchmark_split(fresh, validation, seed=42)


def test_split_refuses_to_shrink_when_the_requested_count_is_missing():
    tasks = [candidate(number, [f"boltons/module_{number}.py"]) for number in range(1, 8)]

    with pytest.raises(InsufficientTasksError, match="Need 10 eligible tasks; found 7"):
        select_candidates(tasks, count=10)


def test_split_does_not_mutate_the_source_candidate_list():
    tasks = [candidate(number, [f"boltons/module_{number}.py"]) for number in range(1, 11)]
    original = list(tasks)

    split_tasks(tasks, train_count=6, seed=42)

    assert tasks == original


def test_parser_admits_small_source_and_regression_test_changes():
    parsed = parse_candidate(
        {
            "number": 23,
            "title": "Fix empty input",
            "body": "Handle empty input in the parser.",
            "mergedAt": "2026-06-23T10:00:00Z",
            "mergeCommit": {"oid": "merge-sha"},
            "baseRefOid": "base-sha",
            "files": [
                {"path": "boltons/urlutils.py", "additions": 3, "deletions": 2},
                {"path": "tests/test_urlutils.py", "additions": 4, "deletions": 0},
                {"path": "tests/data/input.txt", "additions": 1, "deletions": 0},
            ],
        }
    )

    assert parsed is not None
    assert parsed.number == 23
    assert parsed.source_paths == {"boltons/urlutils.py"}
    assert parsed.test_paths == ("tests/test_urlutils.py",)
    assert parsed.fixture_paths == ("tests/data/input.txt",)


@pytest.mark.parametrize(
    "extra_file",
    [
        {"path": "pyproject.toml", "additions": 1, "deletions": 0},
        {"path": "docs/changelog.rst", "additions": 1, "deletions": 0},
        {"path": "boltons/gone.py", "changeType": "RENAMED"},
        {"path": "tests/fixture.exe", "additions": 20, "deletions": 0},
    ],
)
def test_parser_rejects_non_source_changes_and_renames(extra_file):
    assert parse_candidate(
        {
            "number": 23,
            "title": "Fix empty input",
            "mergedAt": "2026-06-23T10:00:00Z",
            "mergeCommit": {"oid": "merge-sha"},
            "baseRefOid": "base-sha",
            "files": [
                {"path": "boltons/urlutils.py", "additions": 3, "deletions": 2},
                {"path": "tests/test_urlutils.py", "additions": 4, "deletions": 0},
                extra_file,
            ],
        }
    ) is None


def test_parser_rejects_large_fixes_without_lowering_the_experiment_size():
    large_pr = {
        "number": 23,
        "title": "Big change",
        "mergedAt": "2026-06-23T10:00:00Z",
        "mergeCommit": {"oid": "merge-sha"},
        "baseRefOid": "base-sha",
        "files": [
            {"path": "boltons/urlutils.py", "additions": 120, "deletions": 0},
            {"path": "tests/test_urlutils.py", "additions": 60, "deletions": 0},
        ],
    }

    assert parse_candidate(large_pr) is None
