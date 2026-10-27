"""Export a privacy-filtered public replay from an authentic completed run."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .analysis_report import build_analysis_report
from .database import RunDatabase


_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\(?:[^\s\"']+)")
_UNIX_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:home|users|tmp)/[^\s\"']+")
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(ALAN_API_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|GH_TOKEN|GITHUB_TOKEN)\b\s*[:=]\s*\S+"
)
_GENERIC_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")


def export_replay(database: RunDatabase, run_id: str, destination: Path) -> Path:
    """Write only public experiment facts and scrubbed grader traces as JSON."""
    run = database.get_run(run_id)
    if run["state"] != "complete":
        raise ValueError("Only a completed run can be exported as a public replay.")

    public_run = {
        key: run[key]
        for key in (
            "id",
            "repo",
            "state",
            "model",
            "seed",
            "training_count",
            "heldout_count",
            "round_number",
            "cost_usd",
            "created_at",
            "updated_at",
        )
    }
    # Run-level billing is not verified in the current data model.
    public_run["cost_usd"] = None
    tasks = [
        {
            key: task.get(key)
            for key in (
                "id",
                "pr_number",
                "title",
                "merged_at",
                "merge_sha",
                "base_sha",
                "source_paths",
                "test_paths",
                "fixture_paths",
                "split",
                "heldout_cohort",
            )
        }
        for task in database.list_tasks(run_id)
    ]
    trials = []
    for trial in database.list_trials(run_id):
        public_trial = {
            key: trial.get(key)
            for key in (
                "id", "task_id", "pr_number", "phase", "skill_version", "status",
                "passed", "elapsed_seconds", "model", "actual_model", "tokens_input",
                "tokens_output", "tokens_reasoning", "config_id", "skill_mode",
            )
        }
        public_trial["cost_verified"] = trial.get("cost_verified") is True
        public_trial["cost_usd"] = (
            trial.get("cost_usd") if public_trial["cost_verified"] else None
        )
        public_trial["trace"] = _public_trace(trial.get("trace", ""))
        public_trial["steps"] = _public_steps(trial.get("engine_history"))
        trials.append(public_trial)

    skills = []
    for skill in database.list_skill_versions(run_id):
        public_skill = {
            key: skill.get(key)
            for key in ("version", "parent_version", "training_passes", "created_at")
        }
        public_skill["content"] = _scrub(str(skill.get("content", "")))
        skills.append(public_skill)
    events = []
    for event in database.list_events(run_id, limit=1000):
        payload = json.loads(event["payload_json"])
        payload.pop("proposal_trace", None)
        payload.pop("reason", None)
        events.append(
            {
                "sequence": event["sequence"],
                "type": event["type"],
                "message": _scrub(event["message"]),
                "created_at": event["created_at"],
                "payload": _scrub_value(payload),
            }
        )

    report_detail = {
        "run": public_run,
        "tasks": tasks,
        "trials": trials,
        "skills": skills,
        "events": events,
    }
    replay = {
        "schema_version": 2,
        "run": public_run,
        "tasks": tasks,
        "trials": trials,
        "skills": skills,
        "events": events,
        "analysis_report": build_analysis_report(report_detail),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(replay, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


def _public_trace(raw_trace: str) -> dict[str, Any]:
    try:
        trace = json.loads(raw_trace)
    except (TypeError, json.JSONDecodeError):
        return {"grader_output": _scrub(raw_trace)[:8_000]}
    # The Alan transcript stays in the local database. The replay needs the
    # real grader output and source diff, not tool chatter or local paths.
    return {
        "agent_status": trace.get("agent_status"),
        "grader_output": _scrub(str(trace.get("grader_output", "")))[:8_000],
        "source_diff": _scrub(str(trace.get("source_diff", "")))[:12_000],
        "unauthorized_changes_restored": bool(trace.get("unauthorized_changes_restored")),
    }


def _public_steps(engine_history: Any) -> list[dict[str, Any]]:
    """Publish action labels and token counts while keeping private content local."""
    if not isinstance(engine_history, dict) or engine_history.get("status") != "available":
        return []
    history = engine_history.get("history")
    messages = history.get("messages") if isinstance(history, dict) else None
    if not isinstance(messages, list):
        return []
    steps: list[dict[str, Any]] = []
    safe_tool_names = {"read", "write", "edit", "bash", "grep", "glob", "terminal", "apply_patch"}
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
            continue
        for part in message["parts"]:
            if not isinstance(part, dict):
                continue
            kind = part.get("kind")
            if kind == "tool":
                raw_name = part.get("tool_name")
                name = raw_name if isinstance(raw_name, str) and raw_name in safe_tool_names else "tool"
                step = {"kind": "tool", "tool": name}
            elif kind in {"text", "reasoning"}:
                step = {"kind": "assistant_message"}
            else:
                continue
            step["role"] = message.get("role") if message.get("role") in {"assistant", "user"} else "unknown"
            usage = part.get("usage")
            if isinstance(usage, dict):
                step["tokens_input"] = usage.get("input_tokens")
                step["tokens_output"] = usage.get("output_tokens")
            steps.append(step)
    return steps


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _scrub_value(item) for key, item in value.items()}
    return value


def _scrub(value: str) -> str:
    value = _GITHUB_TOKEN.sub("[redacted token]", value)
    value = _SECRET_ASSIGNMENT.sub(r"\1=[redacted]", value)
    value = _GENERIC_SECRET.sub("[redacted key]", value)
    value = _WINDOWS_PATH.sub("[local path]", value)
    return _UNIX_PATH.sub("[local path]", value)
