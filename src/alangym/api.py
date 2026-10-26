"""Local FastAPI control surface for AlanGym runs."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .database import ActiveRunError, RunDatabase
from .experiment import ExperimentManager


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class CreateRunRequest(BaseModel):
    repo: str = Field(min_length=3, max_length=100)
    seed: int = Field(default=42, ge=0, le=2_147_483_647)

    @field_validator("repo")
    @classmethod
    def validate_repo_slug(cls, value: str) -> str:
        if value != value.strip() or value.count("/") != 1:
            raise ValueError("Use the GitHub owner/name form.")
        owner, name = value.split("/")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-/")
        if not owner or not name or any(character not in allowed for character in value):
            raise ValueError("Use a simple GitHub owner/name slug.")
        return value


def create_app(
    *,
    database_path: Path | None = None,
    project_root: Path = PROJECT_ROOT,
    data_root: Path | None = None,
    manager: ExperimentManager | None = None,
) -> FastAPI:
    root = project_root.resolve()
    resolved_data_root = (data_root or root / ".alangym").resolve()
    database = RunDatabase(database_path or resolved_data_root / "runs.sqlite3")
    run_manager = manager or ExperimentManager(
        database, project_root=root, data_root=resolved_data_root
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        run_manager.recover_interrupted_runs()
        yield
        await run_manager.shutdown()

    app = FastAPI(title="AlanGym", version="0.1.0", lifespan=lifespan)
    app.state.database = database
    app.state.manager = run_manager
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/runs")
    async def list_runs() -> dict[str, Any]:
        return {"runs": database.list_runs()}

    @app.post("/api/runs", status_code=202)
    async def create_run(request: CreateRunRequest) -> dict[str, Any]:
        try:
            run = run_manager.start(request.repo, seed=request.seed)
        except ActiveRunError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"run": run}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return _run_detail(database, run_id)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        try:
            database.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Run not found.") from error
        cancelled = await run_manager.cancel(run_id)
        return {"cancelled": cancelled}

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        try:
            database.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Run not found.") from error

        async def event_stream():
            cursor = after
            while True:
                events = database.list_events(run_id, after=cursor, limit=200)
                for event in events:
                    cursor = event["sequence"]
                    payload = {
                        **event,
                        "payload": json.loads(event.pop("payload_json")),
                    }
                    yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                run = database.get_run(run_id)
                if run["state"] in {"complete", "blocked", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.6)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _run_detail(database: RunDatabase, run_id: str) -> dict[str, Any]:
    try:
        run = database.get_run(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Run not found.") from error
    return {
        "run": run,
        "tasks": database.list_tasks(run_id),
        "trials": database.list_trials(run_id),
        "skills": database.list_skill_versions(run_id),
        "events": [
            {
                **event,
                "payload": json.loads(event.pop("payload_json")),
            }
            for event in database.list_events(run_id, limit=1_000)
        ],
    }
