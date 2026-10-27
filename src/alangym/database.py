"""Small, transactional SQLite store for repeatable local experiment runs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TERMINAL_STATES = frozenset({"complete", "blocked", "failed", "cancelled"})
TRANSITIONS = {
    "created": frozenset({"mining", "blocked", "failed", "cancelled"}),
    "mining": frozenset({"validating", "blocked", "failed", "cancelled"}),
    "validating": frozenset({"baseline", "blocked", "failed", "cancelled"}),
    "baseline": frozenset({"training", "evaluating", "blocked", "failed", "cancelled"}),
    "training": frozenset({"training", "evolving", "evaluating", "blocked", "failed", "cancelled"}),
    "evolving": frozenset({"training", "evaluating", "blocked", "failed", "cancelled"}),
    "evaluating": frozenset({"evolving", "complete", "blocked", "failed", "cancelled"}),
    "complete": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
ANALYSIS_TRANSITIONS = {
    "queued": frozenset({"running", "blocked", "failed", "cancelled"}),
    "running": frozenset({"running", "complete", "blocked", "failed", "cancelled"}),
    "complete": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class ActiveRunError(RuntimeError):
    """Raised when a second experiment would overlap the local active run."""


class InvalidTransitionError(RuntimeError):
    """Raised when a run state change would hide an invalid lifecycle step."""


class RunDatabase:
    """Persist run progress and detailed traces outside the public source tree."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    state TEXT NOT NULL,
                    model TEXT NOT NULL,
                    seed INTEGER NOT NULL,
                    training_count INTEGER NOT NULL,
                    heldout_count INTEGER NOT NULL,
                    round_number INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    id TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    split TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, id)
                );
                CREATE TABLE IF NOT EXISTS trials (
                    run_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    skill_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    passed INTEGER,
                    elapsed_seconds REAL NOT NULL DEFAULT 0,
                    cost_usd REAL,
                    trial_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, id),
                    FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, id)
                );
                CREATE TABLE IF NOT EXISTS skills (
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    version TEXT NOT NULL,
                    parent_version TEXT,
                    content TEXT NOT NULL,
                    training_passes INTEGER,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, version)
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0 CHECK(progress >= 0 AND progress <= 1),
                    message TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, sequence);
                CREATE INDEX IF NOT EXISTS trials_by_run ON trials(run_id, phase);
                CREATE INDEX IF NOT EXISTS analyses_by_run ON analysis_jobs(run_id, created_at);
                """
            )

    def create_run(
        self,
        repo: str,
        *,
        model: str = "deepseek/deepseek-v4.1-flash",
        seed: int = 42,
        training_count: int = 6,
        heldout_count: int = 4,
    ) -> dict[str, Any]:
        if not repo or "/" not in repo or len(repo.split("/")) != 2:
            raise ValueError("Repository must use the owner/name form")
        now = _now()
        run = {
            "id": uuid.uuid4().hex,
            "repo": repo,
            "state": "created",
            "model": model,
            "seed": seed,
            "training_count": training_count,
            "heldout_count": heldout_count,
            "round_number": 0,
            "cost_usd": None,
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_run = connection.execute(
                "SELECT id FROM runs WHERE state NOT IN ('complete', 'blocked', 'failed', 'cancelled') LIMIT 1"
            ).fetchone()
            if active_run is not None:
                raise ActiveRunError(f"Run {active_run['id']} is already active")
            connection.execute(
                """INSERT INTO runs (
                    id, repo, state, model, seed, training_count, heldout_count,
                    round_number, cost_usd, created_at, updated_at
                ) VALUES (
                    :id, :repo, :state, :model, :seed, :training_count, :heldout_count,
                    :round_number, :cost_usd, :created_at, :updated_at
                )""",
                run,
            )
            self._insert_event(connection, run["id"], "created", "Run created", {})
        return run

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run {run_id}")
        return dict(row)

    def list_runs(self, *, limit: int = 25) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("Run list limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def transition(
        self,
        run_id: str,
        state: str,
        message: str,
        *,
        event_type: str | None = None,
        payload: dict[str, Any] | None = None,
        round_number: int | None = None,
        cost_usd: float | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
            if existing is None:
                raise KeyError(f"unknown run {run_id}")
            previous = existing["state"]
            if state not in TRANSITIONS.get(previous, set()):
                raise InvalidTransitionError(f"Cannot transition run from {previous!r} to {state!r}")
            connection.execute(
                """UPDATE runs
                   SET state = ?, updated_at = ?,
                       round_number = COALESCE(?, round_number),
                       cost_usd = COALESCE(?, cost_usd)
                   WHERE id = ?""",
                (state, _now(), round_number, cost_usd, run_id),
            )
            self._insert_event(connection, run_id, event_type or state, message, payload or {})
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row)

    def record_progress(
        self,
        run_id: str,
        event_type: str,
        message: str,
        *,
        payload: dict[str, Any] | None = None,
        round_number: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE runs SET updated_at = ?, round_number = COALESCE(?, round_number) WHERE id = ?",
                (_now(), round_number, run_id),
            )
            if updated.rowcount != 1:
                raise KeyError(f"unknown run {run_id}")
            self._insert_event(connection, run_id, event_type, message, payload or {})

    def save_tasks(self, run_id: str, tasks: list[dict[str, Any]]) -> None:
        with self._connect() as connection:
            connection.executemany(
                """INSERT INTO tasks (run_id, id, pr_number, title, split, task_json)
                   VALUES (:run_id, :id, :pr_number, :title, :split, :task_json)""",
                [
                    {
                        **task,
                        "run_id": run_id,
                        "task_json": json.dumps(task, sort_keys=True, ensure_ascii=False),
                    }
                    for task in tasks
                ],
            )

    def list_tasks(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY pr_number DESC", (run_id,)
            ).fetchall()
        return [json.loads(row["task_json"]) | {"split": row["split"]} for row in rows]

    def save_trial(self, run_id: str, trial: dict[str, Any]) -> None:
        passed = trial.get("passed")
        if passed not in (None, True, False):
            raise ValueError("Trial pass state must be true, false, or unknown")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO trials (
                    run_id, id, task_id, phase, skill_version, status, passed,
                    elapsed_seconds, cost_usd, trial_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    trial["id"],
                    trial["task_id"],
                    trial["phase"],
                    trial["skill_version"],
                    trial["status"],
                    None if passed is None else int(passed),
                    float(trial.get("elapsed_seconds", 0)),
                    trial.get("cost_usd"),
                    json.dumps(trial, sort_keys=True, ensure_ascii=False),
                ),
            )

    def list_trials(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT trial_json FROM trials WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [json.loads(row["trial_json"]) for row in rows]

    def save_skill_version(
        self,
        run_id: str,
        version: str,
        content: str,
        *,
        parent_version: str | None = None,
        training_passes: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO skills (
                    run_id, version, parent_version, content, training_passes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, version, parent_version, content, training_passes, _now()),
            )

    def list_skill_versions(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT version, parent_version, content, training_passes, created_at
                   FROM skills WHERE run_id = ? ORDER BY rowid""",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_events(self, run_id: str, *, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid event cursor or limit")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sequence, type, message, payload_json, created_at
                   FROM events WHERE run_id = ? AND sequence > ?
                   ORDER BY sequence LIMIT ?""",
                (run_id, after, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_analysis_job(
        self, run_id: str, kind: str, *, config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not kind or not kind.strip():
            raise ValueError("Analysis kind is required")
        job_id = uuid.uuid4().hex
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"unknown run {run_id}")
            if run["state"] != "complete":
                raise ValueError("Analysis requires a completed run")
            active = connection.execute(
                "SELECT id FROM analysis_jobs WHERE status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise ValueError(f"Analysis {active['id']} is already active")
            connection.execute(
                """INSERT INTO analysis_jobs (
                       id, run_id, kind, status, progress, message, config_json,
                       result_json, error, created_at, updated_at
                   ) VALUES (?, ?, ?, 'queued', 0, 'Queued', ?, NULL, NULL, ?, ?)""",
                (job_id, run_id, kind.strip(), json.dumps(config or {}, sort_keys=True), now, now),
            )
        return self.get_analysis_job(job_id)

    def get_analysis_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown analysis {job_id}")
        return _analysis_row(row)

    def list_analysis_jobs(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM analysis_jobs WHERE run_id = ? ORDER BY created_at DESC",
                (run_id,),
            ).fetchall()
        return [_analysis_row(row) for row in rows]

    def update_analysis_job(
        self,
        job_id: str,
        *,
        status: str,
        progress: float | None = None,
        message: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in ANALYSIS_TRANSITIONS:
            raise ValueError(f"Unknown analysis status {status!r}")
        if progress is not None and not 0 <= progress <= 1:
            raise ValueError("Analysis progress must be between zero and one")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM analysis_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown analysis {job_id}")
            previous = current["status"]
            if status not in ANALYSIS_TRANSITIONS[previous]:
                raise ValueError(f"Cannot change terminal analysis {previous!r} to {status!r}")
            connection.execute(
                """UPDATE analysis_jobs SET status = ?, progress = COALESCE(?, progress),
                       message = COALESCE(?, message), result_json = COALESCE(?, result_json),
                       error = ?, updated_at = ? WHERE id = ?""",
                (
                    status,
                    progress,
                    message,
                    json.dumps(result, sort_keys=True, ensure_ascii=False) if result is not None else None,
                    error,
                    _now(),
                    job_id,
                ),
            )
        return self.get_analysis_job(job_id)

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO events (run_id, type, message, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, event_type, message, json.dumps(payload, sort_keys=True), _now()),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _analysis_row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row) | {
        "config": json.loads(row["config_json"]),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
    }
