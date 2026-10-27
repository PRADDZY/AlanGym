"""Read private Alan engine session history without modifying its database.

The returned payload contains transcript text and tool input/output. Keep it local;
callers must not publish it directly through the public replay exporter.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias


DEFAULT_DATABASE_PATH = (
    Path.home() / ".alan-code" / "pi-agent" / "engine" / "share" / "opencode" / "opencode.db"
)

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


class HistoryStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"


class HistoryReason(StrEnum):
    DATABASE_MISSING = "database_missing"
    SCHEMA_MISMATCH = "schema_mismatch"
    NO_MATCH = "no_match"
    AMBIGUOUS_MATCH = "ambiguous_match"
    READ_ERROR = "read_error"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class HistoryPart:
    part_id: str
    kind: str
    sequence: int
    created_at: int | str | None = None
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    arguments: dict[str, JSONValue] | None = None
    output: JSONValue = None
    error: str | None = None
    status: str | None = None
    cost: int | float | None = None
    usage: TokenUsage = TokenUsage()


@dataclass(frozen=True, slots=True)
class MessageHistory:
    message_id: str
    role: str
    model_id: str | None
    cost: int | float | None
    usage: TokenUsage
    parts: tuple[HistoryPart, ...]
    created_at: int | str | None = None


@dataclass(frozen=True, slots=True)
class SessionHistory:
    session_id: str
    model_id: str
    cost: int | float | None
    usage: TokenUsage
    messages: tuple[MessageHistory, ...]


@dataclass(frozen=True, slots=True)
class SessionHistoryResult:
    status: HistoryStatus
    history: SessionHistory | None = None
    reason: HistoryReason | None = None


class _SchemaMismatch(Exception):
    """The local database no longer matches the engine schema this adapter supports."""


_REQUIRED_COLUMNS = {
    "session": {
        "id",
        "directory",
        "model",
        "cost",
        "tokens_input",
        "tokens_output",
        "tokens_reasoning",
        "tokens_cache_read",
        "tokens_cache_write",
    },
    "message": {"id", "session_id", "time_created", "data"},
    "part": {"id", "message_id", "session_id", "time_created", "data"},
}


def read_session_history(
    workspace: Path,
    *,
    database_path: Path | None = None,
) -> SessionHistoryResult:
    """Return private history only when exactly one session matches ``workspace``.

    The database is opened in SQLite read-only mode. Missing, incompatible, or
    unmatched databases produce an explicit unavailable result; duplicate matches
    produce an ambiguous result instead of choosing one arbitrarily.
    """
    try:
        path = Path(database_path or DEFAULT_DATABASE_PATH).expanduser().resolve(strict=False)
        if not path.is_file():
            return _unavailable(HistoryReason.DATABASE_MISSING)
        target_directory = _canonical_directory(workspace)
    except (OSError, RuntimeError, ValueError):
        return _unavailable(HistoryReason.READ_ERROR)

    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            _check_schema(connection)
            matches = [
                row
                for row in connection.execute(
                    """SELECT id, directory, model, cost, tokens_input, tokens_output,
                              tokens_reasoning, tokens_cache_read, tokens_cache_write
                       FROM session"""
                )
                if _canonical_directory(row["directory"]) == target_directory
            ]
            if not matches:
                return _unavailable(HistoryReason.NO_MATCH)
            if len(matches) > 1:
                return SessionHistoryResult(
                    status=HistoryStatus.AMBIGUOUS,
                    reason=HistoryReason.AMBIGUOUS_MATCH,
                )
            history = _load_history(connection, matches[0])
        finally:
            connection.close()
    except _SchemaMismatch:
        return _unavailable(HistoryReason.SCHEMA_MISMATCH)
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return _unavailable(HistoryReason.READ_ERROR)

    return SessionHistoryResult(status=HistoryStatus.AVAILABLE, history=history)


def _unavailable(reason: HistoryReason) -> SessionHistoryResult:
    return SessionHistoryResult(status=HistoryStatus.UNAVAILABLE, reason=reason)


def _canonical_directory(directory: str | Path) -> str:
    if not isinstance(directory, (str, Path)) or not str(directory).strip():
        raise _SchemaMismatch("session directory is missing")
    return os.path.normcase(str(Path(directory).expanduser().resolve(strict=False)))


def _check_schema(connection: sqlite3.Connection) -> None:
    for table, required_columns in _REQUIRED_COLUMNS.items():
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not required_columns.issubset(columns):
            raise _SchemaMismatch(f"unexpected {table} table columns")


def _load_history(connection: sqlite3.Connection, session: sqlite3.Row) -> SessionHistory:
    session_id = _required_string(session["id"])
    model_id = _session_model_id(session["model"])
    message_rows = list(
        connection.execute(
            """SELECT id, time_created, data FROM message
               WHERE session_id = ? ORDER BY time_created, rowid""",
            (session_id,),
        )
    )

    messages: list[MessageHistory] = []
    parts_by_message: dict[str, list[HistoryPart]] = {}
    message_ids: set[str] = set()
    for message in message_rows:
        message_id = _required_string(message["id"])
        message_ids.add(message_id)
        data = _json_object(message["data"])
        role = _required_string(data.get("role"))
        messages.append(
            MessageHistory(
                message_id=message_id,
                role=role,
                model_id=_optional_string(data.get("modelID")),
                cost=_optional_number(data.get("cost")),
                usage=_usage_from_message(data.get("tokens")),
                parts=(),
                created_at=_optional_timestamp(message["time_created"]),
            )
        )
        parts_by_message[message_id] = []

    part_rows = connection.execute(
        """SELECT id, message_id, time_created, data FROM part
           WHERE session_id = ? ORDER BY time_created, rowid""",
        (session_id,),
    )
    for part_row in part_rows:
        message_id = _required_string(part_row["message_id"])
        if message_id not in message_ids:
            raise _SchemaMismatch("part references a message outside its session")
        parts = parts_by_message[message_id]
        parts.append(
            _parse_part(
                part_row,
                sequence=len(parts),
            )
        )

    return SessionHistory(
        session_id=session_id,
        model_id=model_id,
        cost=_optional_number(session["cost"]),
        usage=TokenUsage(
            input_tokens=_optional_integer(session["tokens_input"]),
            output_tokens=_optional_integer(session["tokens_output"]),
            reasoning_tokens=_optional_integer(session["tokens_reasoning"]),
            cache_read_tokens=_optional_integer(session["tokens_cache_read"]),
            cache_write_tokens=_optional_integer(session["tokens_cache_write"]),
        ),
        messages=tuple(
            MessageHistory(
                message_id=message.message_id,
                role=message.role,
                model_id=message.model_id,
                cost=message.cost,
                usage=message.usage,
                parts=tuple(parts_by_message[message.message_id]),
                created_at=message.created_at,
            )
            for message in messages
        ),
    )


def _parse_part(row: sqlite3.Row, *, sequence: int) -> HistoryPart:
    data = _json_object(row["data"])
    kind = _required_string(data.get("type"))
    part_id = _required_string(row["id"])
    state = data.get("state")
    if state is not None and not isinstance(state, dict):
        raise _SchemaMismatch("part state is not an object")

    text = None
    tool_name = None
    tool_call_id = None
    arguments = None
    output: JSONValue = None
    error = None
    status = None
    if kind in {"text", "reasoning"}:
        text = _optional_string(data.get("text"))
    elif kind == "tool":
        if state is None:
            raise _SchemaMismatch("tool part has no state")
        tool_name = _required_string(data.get("tool"))
        tool_call_id = _optional_string(data.get("callID"))
        raw_arguments = state.get("input")
        if raw_arguments is not None and not isinstance(raw_arguments, dict):
            raise _SchemaMismatch("tool arguments are not an object")
        arguments = raw_arguments
        output = state.get("output")
        error = _optional_string(state.get("error"))
        status = _optional_string(state.get("status"))

    tokens = data.get("tokens")
    return HistoryPart(
        part_id=part_id,
        kind=kind,
        sequence=sequence,
        created_at=_optional_timestamp(row["time_created"]),
        text=text,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        arguments=arguments,
        output=output,
        error=error,
        status=status,
        cost=_optional_number(data.get("cost")),
        usage=_usage_from_message(tokens),
    )


def _session_model_id(value: object) -> str:
    if not isinstance(value, str):
        raise _SchemaMismatch("session model is not JSON text")
    try:
        model = json.loads(value)
    except json.JSONDecodeError as error:
        raise _SchemaMismatch("session model is not valid JSON") from error
    if not isinstance(model, dict):
        raise _SchemaMismatch("session model is not an object")
    return _required_string(model.get("id"))


def _json_object(value: object) -> dict[str, JSONValue]:
    if not isinstance(value, str):
        raise _SchemaMismatch("JSON payload is not text")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise _SchemaMismatch("JSON payload is invalid") from error
    if not isinstance(decoded, dict):
        raise _SchemaMismatch("JSON payload is not an object")
    return decoded


def _usage_from_message(value: object) -> TokenUsage:
    if value is None:
        return TokenUsage()
    if not isinstance(value, dict):
        raise _SchemaMismatch("token usage is not an object")
    cache = value.get("cache")
    if cache is not None and not isinstance(cache, dict):
        raise _SchemaMismatch("cache token usage is not an object")
    return TokenUsage(
        input_tokens=_optional_integer(value.get("input")),
        output_tokens=_optional_integer(value.get("output")),
        reasoning_tokens=_optional_integer(value.get("reasoning")),
        cache_read_tokens=_optional_integer(cache.get("read") if cache else None),
        cache_write_tokens=_optional_integer(cache.get("write") if cache else None),
        total_tokens=_optional_integer(value.get("total")),
    )


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _SchemaMismatch("required string field is missing")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _SchemaMismatch("string field has an unexpected type")
    return value


def _optional_number(value: object) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _SchemaMismatch("numeric field has an unexpected type")
    return value


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _SchemaMismatch("integer field has an unexpected type")
    return value


def _optional_timestamp(value: object) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _SchemaMismatch("timestamp field has an unexpected type")
    return value
