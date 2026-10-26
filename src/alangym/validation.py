"""Build and grade clean before/after snapshots for mined pull requests."""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from .alan import SAFE_ENVIRONMENT_VARIABLES
from .mining import PullRequestCandidate


DEFAULT_GRADE_TIMEOUT: Final = 60
MAX_GRADE_OUTPUT: Final = 24_000
_FAILED_SUMMARY = re.compile(r"\b(\d+) failed\b")
_PASSED_SUMMARY = re.compile(r"\b(\d+) passed\b")
_COLLECTED_SUMMARY = re.compile(r"\b(\d+) items? collected\b")


@dataclass(frozen=True, slots=True)
class TestResult:
    __test__ = False

    status: str
    returncode: int | None
    passed_count: int
    failed_count: int
    output: str
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class TaskValidation:
    candidate: PullRequestCandidate
    before: TestResult
    after: TestResult

    @property
    def before_regression_count(self) -> int:
        if self.before.failed_count > 0:
            return self.before.failed_count
        return int(_is_missing_symbol_from_changed_module(self.before, self.candidate))


class TaskValidationError(RuntimeError):
    """A PR is not a reproducible fail-before/pass-after regression task."""


def materialize_commit(repo: Path, commit: str, destination: Path) -> None:
    """Extract a commit without its Git metadata or links to the host filesystem."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    completed = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", commit],
        check=True,
        capture_output=True,
    )
    with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            _safe_archive_path(member.name)
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise TaskValidationError(
                    f"Repository snapshot contains an unsupported file type: {member.name}"
                )
        archive.extractall(destination, members=members, filter="data")


def make_task_snapshot(
    repo: Path,
    candidate: PullRequestCandidate,
    destination: Path,
    *,
    with_fix: bool,
) -> Path:
    """Use the PR parent plus its changed tests, or the complete merged commit."""
    commit = candidate.merge_sha if with_fix else candidate.base_sha
    materialize_commit(repo, commit, destination)
    if not with_fix:
        for relative_path in (*candidate.test_paths, *candidate.fixture_paths):
            target = destination.joinpath(*PurePosixPath(relative_path).parts)
            if _commit_contains(repo, candidate.merge_sha, relative_path):
                target.parent.mkdir(parents=True, exist_ok=True)
                data = subprocess.run(
                    ["git", "-C", str(repo), "show", f"{candidate.merge_sha}:{relative_path}"],
                    check=True,
                    capture_output=True,
                ).stdout
                target.write_bytes(data)
            else:
                target.unlink(missing_ok=True)
    return destination


async def run_pytest(
    workspace: Path,
    test_paths: tuple[str, ...],
    python: Path,
    *,
    timeout: float = DEFAULT_GRADE_TIMEOUT,
) -> TestResult:
    """Run only the frozen PR tests with a filtered process environment."""
    if not test_paths:
        raise ValueError("A task needs at least one regression test file")
    for relative_path in test_paths:
        _safe_archive_path(relative_path)
        if not workspace.joinpath(*PurePosixPath(relative_path).parts).is_file():
            raise TaskValidationError(f"Frozen test file is missing: {relative_path}")
    temp_root = workspace / ".alangym" / "pytest"
    temp_root.mkdir(parents=True, exist_ok=True)
    command = (
        str(python),
        "-m",
        "pytest",
        "-q",
        "--basetemp",
        str(temp_root),
        *test_paths,
    )
    environment = _safe_environment()
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    started = asyncio.get_running_loop().time()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=workspace,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **_creation_options(),
    )
    communication = asyncio.create_task(process.communicate())
    timed_out = False
    try:
        output_bytes, _ = await asyncio.wait_for(asyncio.shield(communication), timeout)
    except TimeoutError:
        timed_out = True
        await _terminate_tree(process)
        output_bytes, _ = await communication
    except asyncio.CancelledError:
        await _terminate_tree(process)
        await communication
        raise

    output = output_bytes.decode("utf-8", errors="replace")
    failed = _summary_count(_FAILED_SUMMARY, output)
    passed = _summary_count(_PASSED_SUMMARY, output)
    collected = _summary_count(_COLLECTED_SUMMARY, output)
    has_collection_error = any(
        marker in output.lower()
        for marker in ("error collecting", "errors during collection", "internal error")
    )
    if timed_out:
        status = "timed_out"
    elif has_collection_error or (process.returncode != 0 and failed == 0):
        status = "invalid"
    elif process.returncode == 0 and passed > 0 and (collected is None or collected > 0):
        status = "passed"
    elif process.returncode != 0 and failed is not None and failed > 0:
        status = "failed"
    else:
        status = "invalid"
    return TestResult(
        status=status,
        returncode=process.returncode,
        passed_count=passed or 0,
        failed_count=failed or 0,
        output=_limit_output(output),
        elapsed_seconds=asyncio.get_running_loop().time() - started,
    )


async def validate_task(
    repo: Path,
    candidate: PullRequestCandidate,
    destination: Path,
    python: Path,
    *,
    timeout: float = DEFAULT_GRADE_TIMEOUT,
) -> TaskValidation:
    """Require the regression tests to fail on the parent and pass at the merge."""
    before_workspace = make_task_snapshot(
        repo, candidate, destination / "before", with_fix=False
    )
    after_workspace = make_task_snapshot(
        repo, candidate, destination / "after", with_fix=True
    )
    before = await run_pytest(before_workspace, candidate.test_paths, python, timeout=timeout)
    source_import_failure = _is_missing_symbol_from_changed_module(before, candidate)
    if not source_import_failure and (before.status != "failed" or before.failed_count < 1):
        raise TaskValidationError(
            f"PR #{candidate.number} did not fail cleanly before its fix ({before.status}).\n"
            f"{before.output}"
        )
    after = await run_pytest(after_workspace, candidate.test_paths, python, timeout=timeout)
    if after.status != "passed":
        raise TaskValidationError(
            f"PR #{candidate.number} did not pass after its fix ({after.status}).\n"
            f"{after.output}"
        )
    return TaskValidation(candidate=candidate, before=before, after=after)


def _is_missing_symbol_from_changed_module(
    result: TestResult, candidate: PullRequestCandidate
) -> bool:
    if result.status != "invalid" or "ImportError: cannot import name" not in result.output:
        return False
    imported_module = re.search(r"ImportError: cannot import name .* from '([A-Za-z0-9_.]+)'", result.output)
    if imported_module is None:
        return False
    module_name = imported_module.group(1)
    changed_modules = {
        ".".join(PurePosixPath(path).with_suffix("").parts)
        for path in candidate.source_paths
    }
    return module_name in changed_modules


def _safe_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in SAFE_ENVIRONMENT_VARIABLES
    }
    environment.update(
        {
            "CI": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return environment


def _safe_archive_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or ":" in path
    ):
        raise TaskValidationError(f"Unsafe repository snapshot path: {path!r}")


def _commit_contains(repo: Path, commit: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}:{path}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _summary_count(pattern: re.Pattern[str], output: str) -> int | None:
    matches = pattern.findall(output)
    return int(matches[-1]) if matches else None


def _limit_output(output: str) -> str:
    if len(output) <= MAX_GRADE_OUTPUT:
        return output
    marker = "\n[grader output truncated]\n"
    half = (MAX_GRADE_OUTPUT - len(marker)) // 2
    return output[:half] + marker + output[-half:]


def _creation_options() -> dict[str, object]:
    return {"creationflags": 0x00000200} if os.name == "nt" else {}


async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        except (OSError, TimeoutError):
            process.kill()
    else:
        process.kill()
