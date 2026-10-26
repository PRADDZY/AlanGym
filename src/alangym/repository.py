"""Authenticated GitHub CLI access and provenance checks for benchmark PRs."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .mining import PullRequestCandidate, parse_candidate, select_candidates


REPOSITORY_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PR_FIELDS = "number,title,body,mergedAt,mergeCommit,baseRefOid,files"


class RepositoryError(RuntimeError):
    """A GitHub repository or its PR history cannot be used for this run."""


class PublicGitHubRepository:
    """Read only public GitHub repo data through the user's authenticated gh CLI."""

    def __init__(self, slug: str, cache_root: Path) -> None:
        if not REPOSITORY_SLUG.fullmatch(slug):
            raise ValueError("Repository must use a simple owner/name slug")
        self.slug = slug
        owner, name = slug.split("/", maxsplit=1)
        self.path = cache_root / f"{owner}--{name}"

    def prepare(self) -> Path:
        metadata = self._gh_json(
            "repo", "view", self.slug, "--json", "nameWithOwner,isPrivate"
        )
        if metadata.get("isPrivate") is not False:
            raise RepositoryError("AlanGym only mines public repositories")
        if metadata.get("nameWithOwner", "").casefold() != self.slug.casefold():
            raise RepositoryError("GitHub resolved a different repository than requested")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._run_gh("repo", "clone", self.slug, str(self.path), "--", "--filter=blob:none", "--no-checkout")
        else:
            remote = subprocess.run(
                ["git", "-C", str(self.path), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
            )
            if remote.returncode != 0 or not _remote_matches(remote.stdout.strip(), self.slug):
                raise RepositoryError("Cached repository path belongs to another GitHub project")
            subprocess.run(
                ["git", "-C", str(self.path), "fetch", "--filter=blob:none", "origin"],
                check=True,
                capture_output=True,
                text=True,
            )
        self._run_git("checkout", "--detach", "origin/HEAD", "--force")
        return self.path

    def candidates(self, *, limit: int = 250) -> list[PullRequestCandidate]:
        response = self._gh_json(
            "pr",
            "list",
            "--repo",
            self.slug,
            "--state",
            "merged",
            "--limit",
            str(limit),
            "--json",
            PR_FIELDS,
        )
        if not isinstance(response, list):
            raise RepositoryError("GitHub returned an invalid merged pull request list")
        result: list[PullRequestCandidate] = []
        for item in response:
            candidate = parse_candidate(item)
            if candidate is not None and self._has_exact_first_parent_diff(candidate, item):
                result.append(candidate)
        return result

    def newest_candidates(self, *, count: int = 10) -> list[PullRequestCandidate]:
        return select_candidates(self.candidates(), count=count)

    def _has_exact_first_parent_diff(
        self, candidate: PullRequestCandidate, pr: dict[str, Any]
    ) -> bool:
        graph = subprocess.run(
            ["git", "-C", str(self.path), "rev-list", "--parents", "-n", "1", candidate.merge_sha],
            capture_output=True,
            text=True,
        )
        if graph.returncode != 0:
            self._run_git("fetch", "--filter=blob:none", "origin", candidate.merge_sha)
            graph = subprocess.run(
                ["git", "-C", str(self.path), "rev-list", "--parents", "-n", "1", candidate.merge_sha],
                capture_output=True,
                text=True,
            )
        if graph.returncode != 0:
            return False
        commit_and_parents = graph.stdout.split()
        parents = commit_and_parents[1:]
        if not 1 <= len(parents) <= 2 or parents[0] != candidate.base_sha:
            return False
        changes = subprocess.run(
            [
                "git",
                "-C",
                str(self.path),
                "diff",
                "--find-renames",
                "--name-status",
                candidate.base_sha,
                candidate.merge_sha,
            ],
            capture_output=True,
            text=True,
        )
        if changes.returncode != 0:
            return False
        changed_paths: set[str] = set()
        for line in changes.stdout.splitlines():
            fields = line.split("\t")
            status = fields[0]
            if status not in {"A", "M", "D"} or len(fields) != 2:
                return False
            changed_paths.add(fields[1])
        api_paths = {
            item.get("path") for item in pr.get("files", []) if isinstance(item, dict)
        }
        return len(changed_paths) == len(pr.get("files", [])) and changed_paths == api_paths

    def _gh_json(self, *arguments: str) -> Any:
        command = ["gh", *arguments]
        for attempt in range(3):
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode == 0:
                break
            detail = result.stderr.strip() or result.stdout.strip()
            retryable = any(marker in detail for marker in ("HTTP 502", "HTTP 503", "HTTP 429"))
            if not retryable or attempt == 2:
                raise RepositoryError(f"GitHub CLI failed: {detail}")
            time.sleep(0.5 * (attempt + 1))
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RepositoryError("GitHub CLI returned malformed JSON") from error

    def _run_gh(self, *arguments: str) -> None:
        self._run_command(["gh", *arguments], "GitHub CLI")

    def _run_git(self, *arguments: str) -> None:
        self._run_command(["git", "-C", str(self.path), *arguments], "git")

    @staticmethod
    def _run_command(command: list[str], label: str) -> None:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RepositoryError(f"{label} failed: {detail}")


def _remote_matches(remote: str, slug: str) -> bool:
    normalized = remote.removesuffix(".git").lower()
    suffix = slug.lower()
    return normalized in {
        f"https://github.com/{suffix}",
        f"http://github.com/{suffix}",
        f"git@github.com:{suffix}",
        f"ssh://git@github.com/{suffix}",
    }
