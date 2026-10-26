"""The fixed-model Alan Code process boundary used by task trials."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final


DEFAULT_MODEL: Final = "deepseek/deepseek-v4.1-flash"
DEFAULT_TIMEOUT: Final = 180
MAX_TRACE_CHARACTERS: Final = 24_000
SAFE_ENVIRONMENT_VARIABLES: Final = (
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)


def _alan_node_modules(executable: str) -> Path | None:
    """Find the installed Alan package's dependencies for Bun module resolution."""
    executable_path = Path(executable)
    try:
        resolved_path = executable_path.resolve()
    except OSError:
        resolved_path = executable_path

    roots = [executable_path.parent, resolved_path.parent, *resolved_path.parents]
    checked: set[Path] = set()
    for root in roots:
        candidates = (
            root / "node_modules" / "alan-code" / "node_modules",
            root / "alan-code" / "node_modules",
            root / "node_modules",
        )
        for candidate in candidates:
            normalized = candidate.resolve()
            if normalized in checked:
                continue
            checked.add(normalized)
            if (normalized / "opencode-ai" / "package.json").is_file():
                return normalized
    return None


class RunStatus(StrEnum):
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    AUTH_REQUIRED = "auth_required"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Output and process status from one fresh Alan Code session."""

    status: RunStatus
    model: str
    returncode: int | None
    stdout: str
    stderr: str
    elapsed_seconds: float


class AlanRunner:
    """Launch one non-resumed Alan Code process with a pinned model."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        executable: str = "alan",
        trace_limit: int = MAX_TRACE_CHARACTERS,
    ) -> None:
        self.model = model
        self.executable = executable
        self.trace_limit = trace_limit

    async def run(
        self,
        instruction: str,
        workspace: Path,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        tool_path: Path | None = None,
    ) -> AgentResult:
        """Run a single bounded task without exposing unrelated environment secrets."""
        resolved_executable = shutil.which(self.executable)
        if resolved_executable is None:
            raise RuntimeError("Alan Code CLI is not installed or is not available on PATH")
        if not workspace.is_dir():
            raise ValueError(f"Alan task workspace does not exist: {workspace}")
        if not instruction.strip():
            raise ValueError("Alan task instruction must not be empty")
        if timeout <= 0:
            raise ValueError("Alan task timeout must be greater than zero")

        command = (
            resolved_executable,
            "--mode",
            "print",
            "--model",
            self.model,
            instruction,
        )
        child_environment = {
            key: value for key, value in os.environ.items() if key.upper() in SAFE_ENVIRONMENT_VARIABLES
        }
        node_modules = _alan_node_modules(resolved_executable)
        if node_modules is not None:
            child_environment["NODE_PATH"] = str(node_modules)
        if tool_path is not None:
            child_environment["PATH"] = os.pathsep.join(
                (str(tool_path), child_environment.get("PATH", ""))
            )
        child_environment.update(
            {
                "ALAN_HARNESS_AUTO_ALLOW_EDITS": "1",
                "ALAN_HARNESS_AUTO_UPDATE": "0",
                "ALAN_HARNESS_MCP": "0",
                "ALAN_HARNESS_WEBSEARCH": "0",
                "CI": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )

        creation_options: dict[str, object] = {}
        if os.name == "nt":
            creation_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        started_at = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace),
                env=child_environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **creation_options,
            )
        except OSError as error:
            raise RuntimeError(f"Could not start Alan Code: {error}") from error

        communicate = asyncio.create_task(process.communicate())
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                asyncio.shield(communicate), timeout=timeout
            )
        except TimeoutError:
            timed_out = True
            await self._terminate_tree(process)
            stdout_bytes, stderr_bytes = await communicate
        except asyncio.CancelledError:
            await self._terminate_tree(process)
            await communicate
            raise

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        status = self._classify(process.returncode, stdout, stderr, timed_out)
        return AgentResult(
            status=status,
            model=self.model,
            returncode=process.returncode,
            stdout=self._limit_trace(stdout),
            stderr=self._limit_trace(stderr),
            elapsed_seconds=time.monotonic() - started_at,
        )

    @staticmethod
    async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
        """Stop only the process tree started for this task."""
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

    def _limit_trace(self, value: str) -> str:
        if len(value) <= self.trace_limit:
            return value
        marker = "\n[trace truncated]\n"
        half_limit = max(0, (self.trace_limit - len(marker)) // 2)
        return value[:half_limit] + marker + value[-half_limit:]

    @staticmethod
    def _classify(
        returncode: int | None, stdout: str, stderr: str, timed_out: bool
    ) -> RunStatus:
        if timed_out:
            return RunStatus.TIMED_OUT
        combined_output = f"{stdout}\n{stderr}".lower()
        if any(
            phrase in combined_output
            for phrase in (
                "alan credentials are required",
                "run `alan --login`",
                "run alan --login",
                "unauthorized: missing credential",
            )
        ):
            return RunStatus.AUTH_REQUIRED
        if returncode == 0:
            return RunStatus.COMPLETED
        if (
            stdout.strip()
            and "AbortError: The operation was aborted." in stderr
            and "runPrintSession" in stderr
        ):
            # Alan's print-mode engine can emit a complete response, then fail
            # while aborting its stream during shutdown. The workspace grader
            # remains the authority on whether the response accomplished work.
            return RunStatus.COMPLETED
        return RunStatus.FAILED
