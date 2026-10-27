import asyncio

from httpx import ASGITransport, AsyncClient

from alangym.api import _api_trial, create_app
from alangym.database import RunDatabase


class StubManager:
    def __init__(self, database):
        self.database = database

    def recover_interrupted_runs(self):
        pass

    def start(self, repo, *, seed):
        return self.database.create_run(repo, seed=seed)

    async def cancel(self, run_id):
        return False

    async def shutdown(self):
        pass


class StubAnalysisManager:
    def __init__(self, database):
        self.database = database

    def start(self, run_id, kind, *, config):
        return self.database.create_analysis_job(run_id, kind, config=config)

    async def cancel(self, analysis_id):
        job = self.database.get_analysis_job(analysis_id)
        if job["status"] not in {"queued", "running"}:
            return False
        self.database.update_analysis_job(
            analysis_id, status="cancelled", progress=job["progress"], message="Cancelled"
        )
        return True

    async def shutdown(self):
        pass


def test_api_creates_and_returns_a_local_run(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    app = create_app(
        database_path=tmp_path / "runs.sqlite3",
        project_root=tmp_path,
        data_root=tmp_path,
        manager=StubManager(database),
    )

    async def request_endpoints():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            created = await client.post("/api/runs", json={"repo": "mahmoud/boltons", "seed": 17})
            assert created.status_code == 202
            run = created.json()["run"]

            detail = await client.get("/api/runs/" + run["id"])
            assert detail.status_code == 200
            assert detail.json()["run"]["repo"] == "mahmoud/boltons"
            assert (await client.get("/api/runs")).json()["runs"][0]["id"] == run["id"]

    asyncio.run(request_endpoints())


def test_api_rejects_invalid_slugs_and_missing_run_ids(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    app = create_app(
        database_path=tmp_path / "runs.sqlite3",
        project_root=tmp_path,
        data_root=tmp_path,
        manager=StubManager(database),
    )

    async def request_endpoints():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            invalid = await client.post("/api/runs", json={"repo": "../private/repo"})
            assert invalid.status_code == 422
            unsupported = await client.post("/api/runs", json={"repo": "owner/other"})
            assert unsupported.status_code == 422
            assert (await client.get("/api/runs/not-a-run")).status_code == 404

    asyncio.run(request_endpoints())


def test_api_queues_and_cancels_an_analysis_job(tmp_path):
    database = RunDatabase(tmp_path / "runs.sqlite3")
    run = database.create_run("mahmoud/boltons")
    for state in ("mining", "validating", "baseline", "evaluating", "complete"):
        database.transition(run["id"], state, state.title())
    app = create_app(
        database_path=tmp_path / "runs.sqlite3",
        project_root=tmp_path,
        data_root=tmp_path,
        manager=StubManager(database),
        analysis_manager=StubAnalysisManager(database),
    )

    async def request_endpoints():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                f"/api/runs/{run['id']}/analyses",
                json={"kind": "suite", "config": {"include_steps": True}},
            )
            assert created.status_code == 202
            job = created.json()["analysis"]
            assert job["status"] == "queued"
            assert job["config"] == {"include_steps": True}

            listed = await client.get(f"/api/runs/{run['id']}/analyses")
            assert listed.json()["analyses"][0]["id"] == job["id"]
            detail = await client.get(f"/api/analyses/{job['id']}")
            assert detail.json()["analysis"]["kind"] == "suite"

            cancelled = await client.post(f"/api/analyses/{job['id']}/cancel")
            assert cancelled.json() == {"cancelled": True}
            assert (await client.get(f"/api/analyses/{job['id']}")).json()["analysis"]["status"] == "cancelled"

    asyncio.run(request_endpoints())


def test_api_trial_keeps_private_session_content_out_of_browser_payload():
    result = _api_trial(
        {
            "id": "ladder:one", "task_id": "pr-1", "pr_number": 1,
            "phase": "ladder", "skill_version": "v1", "status": "passed",
            "passed": True, "elapsed_seconds": 1, "model": "deepseek/deepseek-v4.1-flash",
            "cost_usd": 2.5, "cost_verified": False,
            "engine_history": {"status": "available", "history": {"messages": [{
                "role": "assistant", "parts": [
                    {"kind": "text", "text": "private transcript"},
                    {"kind": "tool", "tool_name": "bash", "arguments": {"cmd": "private command"}},
                ],
            }]}},
        }
    )

    assert "engine_history" not in result
    assert "private transcript" not in str(result)
    assert "private command" not in str(result)
    assert result["cost_usd"] is None
    assert result["steps"] == [
        {"kind": "assistant_message", "role": "assistant"},
        {"kind": "tool", "tool": "bash", "role": "assistant"},
    ]
