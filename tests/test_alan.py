import asyncio
import os
from types import SimpleNamespace

import pytest

from alangym.alan import AlanRunner, RunStatus, _ensure_engine_binary_alias


def test_runner_pins_model_and_drops_inherited_secrets(monkeypatch, tmp_path):
    invocation = {}
    node_modules = tmp_path / "alan-node-modules"
    executable_paths = (node_modules / "opencode-ai" / "bin", node_modules / ".bin")
    for path in executable_paths:
        path.mkdir(parents=True)

    async def start_process(*args, **kwargs):
        invocation["args"] = args
        invocation["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            communicate=lambda: asyncio.sleep(0, result=(b"Finished the task.\n", b"")),
        )

    monkeypatch.setattr("alangym.alan.shutil.which", lambda _: "alan")
    monkeypatch.setattr("alangym.alan._alan_node_modules", lambda _: node_modules)
    monkeypatch.setattr("alangym.alan.asyncio.create_subprocess_exec", start_process)
    monkeypatch.setenv("GH_TOKEN", "must-not-reach-the-agent")

    result = asyncio.run(AlanRunner().run("Fix the described bug.", tmp_path))

    assert result.status is RunStatus.COMPLETED
    assert result.model == "deepseek/deepseek-v4.1-flash"
    assert invocation["args"] == (
        "alan",
        "--mode",
        "print",
        "--model",
        "deepseek/deepseek-v4.1-flash",
        "Fix the described bug.",
    )
    assert invocation["kwargs"]["cwd"] == str(tmp_path)
    assert invocation["kwargs"]["env"]["ALAN_HARNESS_AUTO_ALLOW_EDITS"] == "1"
    assert "GH_TOKEN" not in invocation["kwargs"]["env"]
    child_path = invocation["kwargs"]["env"]["PATH"].split(os.pathsep)
    assert child_path[:2] == [str(path) for path in executable_paths]
    assert invocation["kwargs"]["env"]["NODE_PATH"] == str(node_modules)


def test_engine_binary_is_exposed_to_alan_resolver(tmp_path):
    node_modules = tmp_path / "alan" / "node_modules"
    binary_name = "opencode.exe" if os.name == "nt" else "opencode"
    source = node_modules / "opencode-ai" / "bin" / binary_name
    source.parent.mkdir(parents=True)
    source.write_bytes(b"engine")

    workspace = tmp_path / "runs" / "run-1" / "trials" / "trial-1" / "workspace"
    workspace.mkdir(parents=True)

    _ensure_engine_binary_alias(workspace, node_modules)

    alias_dir = workspace.parent.parent / "node_modules" / "opencode-ai" / "bin"
    alias = alias_dir / binary_name
    assert alias_dir.is_dir()
    assert alias.is_file()
    assert alias.read_bytes() == b"engine"


def test_runner_reports_missing_alan_login(monkeypatch, tmp_path):
    async def start_process(*args, **kwargs):
        return SimpleNamespace(
            returncode=1,
            communicate=lambda: asyncio.sleep(
                0,
                result=(b"", b"Alan credentials are required. Run alan --login."),
            ),
        )

    monkeypatch.setattr("alangym.alan.shutil.which", lambda _: "alan")
    monkeypatch.setattr("alangym.alan.asyncio.create_subprocess_exec", start_process)

    result = asyncio.run(AlanRunner().run("test", tmp_path))

    assert result.status is RunStatus.AUTH_REQUIRED
    assert "alan --login" in result.stderr


def test_runner_terminates_a_trial_after_timeout(monkeypatch, tmp_path):
    stopped = []
    terminated = asyncio.Event()

    async def start_process(*args, **kwargs):
        async def wait_forever():
            await terminated.wait()
            return b"Stopped after timeout", b""

        return SimpleNamespace(returncode=None, communicate=wait_forever)

    async def terminate_tree(process):
        stopped.append(process)
        process.returncode = -1
        terminated.set()

    monkeypatch.setattr("alangym.alan.shutil.which", lambda _: "alan")
    monkeypatch.setattr("alangym.alan.asyncio.create_subprocess_exec", start_process)
    monkeypatch.setattr(AlanRunner, "_terminate_tree", staticmethod(terminate_tree))

    result = asyncio.run(AlanRunner().run("slow task", tmp_path, timeout=0.01))

    assert result.status is RunStatus.TIMED_OUT
    assert len(stopped) == 1


def test_runner_tolerates_process_exiting_during_tree_termination(monkeypatch):
    async def start_taskkill(*args, **kwargs):
        raise ProcessLookupError("Alan process already exited")

    def kill_exited_process():
        raise ProcessLookupError("Alan process already exited")

    process = SimpleNamespace(returncode=None, pid=123, kill=kill_exited_process)
    monkeypatch.setattr("alangym.alan.asyncio.create_subprocess_exec", start_taskkill)

    asyncio.run(AlanRunner._terminate_tree(process))


def test_runner_does_not_start_without_the_cli(monkeypatch, tmp_path):
    monkeypatch.setattr("alangym.alan.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="Alan Code CLI is not installed"):
        asyncio.run(AlanRunner().run("test", tmp_path))
