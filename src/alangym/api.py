"""Local FastAPI control surface for AlanGym runs."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .analysis_report import build_analysis_report
from .database import ActiveRunError, RunDatabase
from .experiment import ExperimentManager
from .replay import _public_steps, _public_trace, _scrub, _scrub_value


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
        if owner in {".", ".."} or name in {".", ".."} or owner.startswith("-") or name.startswith("-"):
            raise ValueError("Repository names cannot be path segments or command flags.")
        if value.casefold() != "mahmoud/boltons":
            raise ValueError("This benchmark currently supports mahmoud/boltons.")
        return value


class CreateAnalysisRequest(BaseModel):
    kind: Literal["suite", "ladder", "ratchet", "xray", "compiler", "twins", "battle", "fork"] = "suite"
    config: dict[str, Any] = Field(default_factory=dict)


def create_app(
    *,
    database_path: Path | None = None,
    project_root: Path = PROJECT_ROOT,
    data_root: Path | None = None,
    manager: ExperimentManager | None = None,
    analysis_manager: Any | None = None,
) -> FastAPI:
    root = project_root.resolve()
    resolved_data_root = (data_root or root / ".alangym").resolve()
    database = RunDatabase(database_path or resolved_data_root / "runs.sqlite3")
    run_manager = manager or ExperimentManager(
        database, project_root=root, data_root=resolved_data_root
    )
    if analysis_manager is None:
        from .analysis_service import AnalysisManager

        analysis_runner = AnalysisManager(database, project_root=root, data_root=resolved_data_root)
    else:
        analysis_runner = analysis_manager

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        run_manager.recover_interrupted_runs()
        analysis_runner.recover_interrupted_analyses()
        yield
        await analysis_runner.shutdown()
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

    @app.post("/api/runs/{run_id}/analyses", status_code=202)
    async def create_analysis(run_id: str, request: CreateAnalysisRequest) -> dict[str, Any]:
        try:
            analysis = analysis_runner.start(run_id, request.kind, config=request.config)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Run not found.") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"analysis": analysis}

    @app.get("/api/runs/{run_id}/analyses")
    async def list_analyses(run_id: str) -> dict[str, Any]:
        try:
            database.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Run not found.") from error
        return {"analyses": database.list_analysis_jobs(run_id)}

    @app.get("/api/analyses/{analysis_id}")
    async def get_analysis(analysis_id: str) -> dict[str, Any]:
        try:
            return {"analysis": database.get_analysis_job(analysis_id)}
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Analysis not found.") from error

    @app.post("/api/analyses/{analysis_id}/cancel")
    async def cancel_analysis(analysis_id: str) -> dict[str, Any]:
        try:
            database.get_analysis_job(analysis_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Analysis not found.") from error
        return {"cancelled": await analysis_runner.cancel(analysis_id)}

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
    events = []
    for event in database.list_events(run_id, limit=1_000):
        payload = json.loads(event.pop("payload_json"))
        payload.pop("proposal_trace", None)
        events.append(
            {
                **event,
                "message": _scrub(event["message"]),
                "payload": _scrub_value(payload),
            }
        )
    skills = [
        {**skill, "content": _scrub(str(skill.get("content", "")))}
        for skill in database.list_skill_versions(run_id)
    ]
    detail = {
        "run": run,
        "tasks": database.list_tasks(run_id),
        "trials": [_api_trial(trial) for trial in database.list_trials(run_id)],
        "skills": skills,
        "events": events,
    }
    detail["analysis_report"] = build_analysis_report(detail)
    return detail


def _api_trial(trial: dict[str, Any]) -> dict[str, Any]:
    safe = {
        key: trial.get(key)
        for key in (
            "id", "task_id", "pr_number", "phase", "skill_version", "status", "passed",
            "elapsed_seconds", "model", "actual_model", "tokens_input", "tokens_output",
            "tokens_reasoning", "config_id", "skill_mode",
        )
    }
    safe["cost_verified"] = trial.get("cost_verified") is True
    safe["cost_usd"] = trial.get("cost_usd") if safe["cost_verified"] else None
    safe["trace"] = _public_trace(trial.get("trace", ""))
    safe["steps"] = _public_steps(trial.get("engine_history"))
    return safe
