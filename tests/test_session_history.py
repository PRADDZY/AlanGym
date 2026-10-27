import json
import sqlite3

from alangym.session_history import (
    HistoryReason,
    HistoryStatus,
    read_session_history,
)


def _create_engine_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            directory TEXT NOT NULL,
            model TEXT,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER,
            tokens_reasoning INTEGER,
            tokens_cache_read INTEGER,
            tokens_cache_write INTEGER,
            time_created INTEGER,
            time_updated INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER,
            data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER,
            data TEXT NOT NULL
        );
        """
    )
    return connection


def _insert_session(connection, session_id, directory):
    connection.execute(
        """INSERT INTO session
           (id, directory, model, cost, tokens_input, tokens_output, tokens_reasoning,
            tokens_cache_read, tokens_cache_write, time_created, time_updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            str(directory),
            json.dumps({"id": "deepseek-v4.1-flash", "providerID": "deepseek"}),
            0.25,
            120,
            45,
            9,
            4,
            2,
            1,
            2,
        ),
    )


def test_unique_workspace_match_returns_ordered_private_history(tmp_path):
    workspace = tmp_path / "workspace"
    child = workspace / "child"
    child.mkdir(parents=True)
    database_path = tmp_path / "engine.sqlite"
    connection = _create_engine_database(database_path)
    _insert_session(connection, "session-1", child / "..")

    connection.execute(
        "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
        (
            "assistant-message",
            "session-1",
            20,
            json.dumps(
                {
                    "role": "assistant",
                    "modelID": "deepseek-v4.1-flash",
                    "cost": 0.2,
                    "tokens": {
                        "input": 80,
                        "output": 40,
                        "reasoning": 8,
                        "total": 120,
                        "cache": {"read": 3, "write": 2},
                    },
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
        ("user-message", "session-1", 10, json.dumps({"role": "user"})),
    )
    ordered_parts = (
        ("part-start", 10, {"type": "step-start"}),
        ("part-text", 20, {"type": "text", "text": "private answer"}),
        (
            "part-tool",
            30,
            {
                "type": "tool",
                "callID": "call-1",
                "tool": "read_file",
                "state": {
                    "status": "completed",
                    "input": {"path": "src/example.py"},
                    "output": "private tool output",
                    "time": {"start": 30, "end": 31},
                },
            },
        ),
        (
            "part-finish",
            40,
            {
                "type": "step-finish",
                "cost": 0.2,
                "tokens": {"input": 80, "output": 40, "total": 120},
            },
        ),
    )
    for part_id, created_at, data in ordered_parts:
        connection.execute(
            """INSERT INTO part (id, message_id, session_id, time_created, data)
               VALUES (?, ?, ?, ?, ?)""",
            (part_id, "assistant-message", "session-1", created_at, json.dumps(data)),
        )
    connection.commit()
    connection.close()

    result = read_session_history(workspace, database_path=database_path)

    assert result.status is HistoryStatus.AVAILABLE
    assert result.reason is None
    assert result.history is not None
    history = result.history
    assert history.session_id == "session-1"
    assert history.model_id == "deepseek-v4.1-flash"
    assert history.cost == 0.25
    assert (history.usage.input_tokens, history.usage.output_tokens) == (120, 45)
    assert (history.usage.reasoning_tokens, history.usage.cache_read_tokens) == (9, 4)
    assert [message.message_id for message in history.messages] == [
        "user-message",
        "assistant-message",
    ]
    assistant = history.messages[1]
    assert assistant.role == "assistant"
    assert assistant.model_id == "deepseek-v4.1-flash"
    assert assistant.usage.cache_read_tokens == 3
    assert [part.kind for part in assistant.parts] == [
        "step-start",
        "text",
        "tool",
        "step-finish",
    ]
    assert [part.sequence for part in assistant.parts] == [0, 1, 2, 3]
    assert assistant.parts[1].text == "private answer"
    tool_part = assistant.parts[2]
    assert tool_part.tool_name == "read_file"
    assert tool_part.tool_call_id == "call-1"
    assert tool_part.arguments == {"path": "src/example.py"}
    assert tool_part.output == "private tool output"
    assert assistant.parts[3].cost == 0.2
    assert assistant.parts[3].usage.total_tokens == 120


def test_missing_database_returns_unavailable(tmp_path):
    result = read_session_history(
        tmp_path / "workspace",
        database_path=tmp_path / "missing.sqlite",
    )

    assert result.status is HistoryStatus.UNAVAILABLE
    assert result.reason is HistoryReason.DATABASE_MISSING
    assert result.history is None


def test_schema_mismatch_returns_unavailable(tmp_path):
    database_path = tmp_path / "wrong-schema.sqlite"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE session (id TEXT, directory TEXT)")
    connection.commit()
    connection.close()

    result = read_session_history(tmp_path, database_path=database_path)

    assert result.status is HistoryStatus.UNAVAILABLE
    assert result.reason is HistoryReason.SCHEMA_MISMATCH


def test_no_workspace_match_returns_unavailable(tmp_path):
    database_path = tmp_path / "engine.sqlite"
    connection = _create_engine_database(database_path)
    connection.commit()
    connection.close()

    result = read_session_history(tmp_path / "workspace", database_path=database_path)

    assert result.status is HistoryStatus.UNAVAILABLE
    assert result.reason is HistoryReason.NO_MATCH


def test_multiple_workspace_matches_return_ambiguous(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database_path = tmp_path / "engine.sqlite"
    connection = _create_engine_database(database_path)
    _insert_session(connection, "session-1", workspace)
    _insert_session(connection, "session-2", workspace)
    connection.commit()
    connection.close()

    result = read_session_history(workspace, database_path=database_path)

    assert result.status is HistoryStatus.AMBIGUOUS
    assert result.reason is HistoryReason.AMBIGUOUS_MATCH
    assert result.history is None
