import asyncio

from httpx import ASGITransport, AsyncClient

from alangym.api import create_app
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
