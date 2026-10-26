"""Admission and deterministic split rules for verified regression tasks."""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Iterable


MAX_CHANGED_FILES: Final = 12
MAX_CHANGED_LINES: Final = 160
FIXTURE_SUFFIXES: Final = frozenset({".csv", ".json", ".jsonl", ".txt", ".xml"})


class InsufficientTasksError(ValueError):
    """Raised when the frozen experiment cannot be filled without guessing."""


@dataclass(frozen=True, slots=True)
class PullRequestCandidate:
    """Only the user-facing facts needed to form one verified task."""

    number: int
    title: str
    body: str
    merged_at: str
    merge_sha: str
    base_sha: str
    source_paths: frozenset[str]
    test_paths: tuple[str, ...]
    fixture_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("PR number must be positive")
        if not self.title.strip() or not self.merge_sha or not self.base_sha:
            raise ValueError("Task needs a title and both Git commit hashes")
        for path in (*self.source_paths, *self.test_paths, *self.fixture_paths):
            _validate_relative_repo_path(path)


def parse_candidate(
    pr: dict[str, object],
    *,
    source_prefixes: tuple[str, ...] = ("boltons/",),
    test_prefixes: tuple[str, ...] = ("tests/",),
    max_changed_files: int = MAX_CHANGED_FILES,
    max_changed_lines: int = MAX_CHANGED_LINES,
) -> PullRequestCandidate | None:
    """Keep small, test-backed fixes without manifest or dependency changes."""
    files = pr.get("files")
    commit = pr.get("mergeCommit")
    number = pr.get("number")
    merged_at = pr.get("mergedAt")
    title = pr.get("title")
    if not isinstance(files, list) or not isinstance(commit, dict):
        return None
    if (
        not isinstance(number, int)
        or not isinstance(merged_at, str)
        or not isinstance(title, str)
        or len(files) > max_changed_files
    ):
        return None

    total_changes = 0
    source_paths: set[str] = set()
    test_paths: set[str] = set()
    fixture_paths: set[str] = set()

    for file_entry in files:
        if not isinstance(file_entry, dict):
            return None
        path = file_entry.get("path")
        raw_change_type = file_entry.get("changeType")
        change_type = (
            raw_change_type.upper()
            if isinstance(raw_change_type, str) and raw_change_type
            else "MODIFIED"
        )
        additions = file_entry.get("additions", 0)
        deletions = file_entry.get("deletions", 0)
        if not isinstance(path, str) or change_type not in {"ADDED", "MODIFIED", "DELETED"}:
            return None
        try:
            _validate_relative_repo_path(path)
        except ValueError:
            return None
        if (
            not isinstance(additions, int)
            or not isinstance(deletions, int)
            or additions < 0
            or deletions < 0
        ):
            return None
        total_changes += additions + deletions
        if _has_prefix(path, source_prefixes) and path.lower().endswith(".py"):
            source_paths.add(path)
        elif _has_prefix(path, test_prefixes):
            if path.lower().endswith(".py"):
                test_paths.add(path)
            elif PurePosixPath(path).suffix.lower() in FIXTURE_SUFFIXES:
                fixture_paths.add(path)
            else:
                return None
        else:
            # Includes packaging and build files, so the validation env cannot drift.
            return None

    merge_sha = commit.get("oid")
    base_sha = pr.get("baseRefOid")
    if not isinstance(merge_sha, str) or not isinstance(base_sha, str):
        return None
    if total_changes > max_changed_lines or not source_paths or not test_paths:
        return None

    body = pr.get("body")
    return PullRequestCandidate(
        number=number,
        title=title,
        body=body if isinstance(body, str) else "",
        merged_at=merged_at,
        merge_sha=merge_sha,
        base_sha=base_sha,
        source_paths=frozenset(source_paths),
        test_paths=tuple(sorted(test_paths)),
        fixture_paths=tuple(sorted(fixture_paths)),
    )


def select_candidates(
    candidates: Iterable[PullRequestCandidate], *, count: int = 10
) -> list[PullRequestCandidate]:
    """Take the newest count fixes with no shared source module."""
    if count < 1:
        raise ValueError("Candidate count must be positive")
    ordered = sorted(candidates, key=lambda item: (item.merged_at, item.number), reverse=True)
    selected: list[PullRequestCandidate] = []
    used_sources: set[str] = set()
    for candidate in ordered:
        if candidate.source_paths & used_sources:
            continue
        selected.append(candidate)
        used_sources.update(candidate.source_paths)
        if len(selected) == count:
            return selected

    raise InsufficientTasksError(
        f"Need {count} eligible tasks; found {len(selected)} with distinct source files"
    )


def split_tasks(
    tasks: Iterable[PullRequestCandidate], *, train_count: int = 6, seed: int = 42
) -> tuple[list[PullRequestCandidate], list[PullRequestCandidate]]:
    """Assign an exact deterministic training/held-out split on a copy."""
    ordered = list(tasks)
    if train_count < 1 or train_count >= len(ordered):
        raise ValueError("Training split must leave at least one held-out task")
    shuffled = list(ordered)
    random.Random(seed).shuffle(shuffled)
    return shuffled[:train_count], shuffled[train_count:]


def _has_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def _validate_relative_repo_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or ":" in path
    ):
        raise ValueError(f"Not a safe relative repository path: {path!r}")
