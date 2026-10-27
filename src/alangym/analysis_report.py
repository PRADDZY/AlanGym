"""Build honest, replay-safe feature reports from saved experiment records."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


LADDER_CONFIGS = (
    ("flash_no_skill", "DeepSeek V4.1 Flash", "deepseek/deepseek-v4.1-flash", "none"),
    ("flash_current_skill", "Flash with current skill", "deepseek/deepseek-v4.1-flash", "current"),
    ("deepseek_pro_no_skill", "DeepSeek V4 Pro", "deepseek/deepseek-v4-pro", "none"),
)
PHASE_ORDER = (
    "baseline",
    "round_1_current",
    "round_1_candidate",
    "round_1_ratchet_retry",
    "round_1",
    "round_2_current",
    "round_2_candidate",
    "round_2_ratchet_retry",
    "round_2",
    "final",
    "ladder_flash_no_skill",
    "ladder_flash_current_skill",
    "ladder_deepseek_pro_no_skill",
)


def build_analysis_report(detail: dict[str, Any]) -> dict[str, Any]:
    """Return feature views based only on saved, attributable evidence."""
    run = detail.get("run", {})
    tasks = detail.get("tasks", [])
    trials = detail.get("trials", [])
    events = detail.get("events", [])
    skills = detail.get("skills", [])
    task_by_id = {str(task.get("id")): task for task in tasks}
    phase_trials: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    config_trials: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trial in trials:
        task_id = str(trial.get("task_id") or f"pr-{trial.get('pr_number')}")
        config_id = trial.get("config_id")
        phase = str(trial.get("phase", "unknown"))
        phase_key = f"ladder_{config_id}" if phase == "ladder" and isinstance(config_id, str) else phase
        phase_trials[phase_key][task_id] = trial
        if isinstance(config_id, str) and config_id:
            config_trials[config_id].append(trial)

    phase_names = sorted(
        phase_trials,
        key=lambda phase: (PHASE_ORDER.index(phase) if phase in PHASE_ORDER else len(PHASE_ORDER), phase),
    )
    training_ids = {task_id for task_id, task in task_by_id.items() if task.get("split") == "train"}
    heldout_ids = {task_id for task_id, task in task_by_id.items() if task.get("split") == "heldout"}
    all_task_ids = set(task_by_id)

    training: dict[str, Any] = {}
    heldout: dict[str, Any] = {}
    for phase in phase_names:
        training[phase] = _score(phase_trials[phase], training_ids)
        heldout[phase] = _score(phase_trials[phase], heldout_ids)

    heatmap_tasks = []
    for task_id, task in task_by_id.items():
        outcomes = {}
        for phase in phase_names:
            trial = phase_trials[phase].get(task_id)
            outcomes[phase] = trial.get("passed") if trial is not None else None
        heatmap_tasks.append(
            {
                "task_id": task_id,
                "pr_number": task.get("pr_number"),
                "split": task.get("split", "unknown"),
                "heldout_cohort": task.get("heldout_cohort"),
                "outcomes": outcomes,
            }
        )
    heatmap_tasks.sort(key=lambda item: item["pr_number"] or 0)

    ladder_configs = [
        _ladder_row(config_id, label, model, skill_mode, config_trials[config_id], heldout_ids)
        for config_id, label, model, skill_mode in LADDER_CONFIGS
    ]
    lineage = _lineage(events)
    provenance = _provenance(skills, events)

    return {
        "schema_version": 1,
        "run_id": run.get("id"),
        "benchmark": {
            "repo": run.get("repo"),
            "seed": run.get("seed"),
            "training_count": len(training_ids),
            "heldout_count": len(heldout_ids),
        },
        "training": training,
        "heldout": heldout,
        "ladder": {"configs": ladder_configs, "task_ids": sorted(heldout_ids)},
        "heatmap": {"phases": phase_names, "tasks": heatmap_tasks},
        "lineage": lineage,
        "ratchet": _ratchet_view(events, lineage),
        "provenance": provenance,
        "xray": _event_feature(events, "xray_completed") or _task_level_xray(phase_trials, trials),
        "compiler": _feature_or_unavailable(events, "compiler_completed"),
        "twins": _feature_or_unavailable(events, "twins_completed"),
        "battle": _event_feature(events, "battle_completed") or _task_battle(trials),
        "fork": _feature_or_unavailable(events, "fork_completed"),
        "coverage": {
            "task_count": len(all_task_ids),
            "trial_count": len(trials),
            "costs_verified": all(
                trial.get("cost_verified") is True
                and isinstance(trial.get("cost_usd"), (int, float))
                for trial in trials
            ) if trials else False,
            "token_counts_complete": all(
                isinstance(trial.get("tokens_input"), int)
                and isinstance(trial.get("tokens_output"), int)
                for trial in trials
            ) if trials else False,
        },
    }


def _score(
    rows_by_task: dict[str, dict[str, Any]], expected_ids: set[str],
) -> dict[str, Any]:
    if not expected_ids:
        return {"passed": 0, "total": 0, "completed": 0, "unknown": 0, "rate": None}
    rows = [rows_by_task.get(task_id) for task_id in expected_ids]
    known = [row for row in rows if row is not None and row.get("passed") in (True, False)]
    passed = sum(row["passed"] is True for row in known)
    unknown = len(expected_ids) - len(known)
    return {
        "passed": passed,
        "total": len(expected_ids),
        "completed": len(known),
        "unknown": unknown,
        "rate": passed / len(expected_ids) if unknown == 0 else None,
    }


def _ladder_row(
    config_id: str,
    label: str,
    model: str,
    skill_mode: str,
    rows: list[dict[str, Any]],
    expected_ids: set[str],
) -> dict[str, Any]:
    attributable = [
        row for row in rows
        if row.get("model") == model and row.get("skill_mode") == skill_mode
    ]
    by_task = {
        str(row.get("task_id") or f"pr-{row.get('pr_number')}"): row
        for row in attributable
    }
    attempted = [by_task[task_id] for task_id in expected_ids if task_id in by_task]
    measured = [row for row in attempted if row.get("passed") in (True, False)]
    passed = sum(row.get("passed") is True for row in measured)
    total = len(expected_ids)
    complete = bool(expected_ids) and len(measured) == total
    tokens_input_values = [row.get("tokens_input") for row in attempted]
    tokens_output_values = [row.get("tokens_output") for row in attempted]
    tokens_known = (
        len(attempted) == total
        and all(isinstance(value, int) and value >= 0 for value in tokens_input_values)
        and all(isinstance(value, int) and value >= 0 for value in tokens_output_values)
    )
    cost_verified = bool(attempted) and len(attempted) == total and all(
        row.get("cost_verified") is True
        and isinstance(row.get("cost_usd"), (int, float))
        and not isinstance(row.get("cost_usd"), bool)
        and row["cost_usd"] >= 0
        for row in attempted
    )
    cost = sum(float(row["cost_usd"]) for row in attempted) if cost_verified else None
    return {
        "id": config_id,
        "label": label,
        "model": model,
        "skill_mode": skill_mode,
        "status": "measured" if complete else "partial" if measured else "unavailable",
        "reason": (
            None if complete
            else "No valid grader outcomes for this configuration" if not measured
            else "Only some held-out tasks have valid grader outcomes"
        ),
        "passed": passed if measured else None,
        "total": total,
        "rate": passed / total if complete and total else None,
        "tokens_input": sum(tokens_input_values) if tokens_known else None,
        "tokens_output": sum(tokens_output_values) if tokens_known else None,
        "tokens_per_pass": (
            (sum(tokens_input_values) + sum(tokens_output_values)) / passed
            if tokens_known and passed > 0 else None
        ),
        "cost_usd": cost,
        "cost_per_pass": cost / passed if cost is not None and passed > 0 else None,
        "task_ids": sorted(task_id for task_id in expected_ids if task_id in by_task),
    }


def _lineage(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = []
    for event in events:
        if event.get("type") not in {"skill_accepted", "skill_rejected"}:
            continue
        payload = event.get("payload") or {}
        nodes.append(
            {
                "type": event["type"],
                "message": event.get("message", ""),
                "version": payload.get("version") or payload.get("candidate_version"),
                "parent_version": payload.get("parent_version"),
                "training_passes": (
                    payload.get("training_passes")
                    if payload.get("training_passes") is not None
                    else payload.get("candidate_passes")
                ),
                "current_passes": payload.get("current_passes"),
                "flipped_pr_numbers": payload.get("flipped_pr_numbers", []),
                "accepted": payload.get("accepted", event["type"] == "skill_accepted"),
                "reason": payload.get("reason"),
                "flaky_pr_numbers": payload.get("flaky_pr_numbers", []),
                "source_pr_numbers": payload.get("source_pr_numbers", []),
            }
        )
    return nodes


def _ratchet_view(events: list[dict[str, Any]], lineage: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = [
        node for node in lineage
        if node.get("reason") in {
            "prior_pass_regression",
            "training_improved_without_prior_pass_regression",
            "no_training_improvement_or_unknown_result",
        }
    ]
    if not decisions:
        return {"status": "unavailable", "reason": "No saved candidate decision has run through the ratchet"}
    return {
        "status": "measured",
        "policy": "Retry prior-pass regressions, reject the candidate, and mark inconsistent retries flaky",
        "decisions": decisions,
        "rejected_count": sum(not decision["accepted"] for decision in decisions),
        "flaky_pr_numbers": sorted({
            number for decision in decisions for number in decision.get("flaky_pr_numbers", [])
        }),
    }


def _provenance(skills: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    latest = skills[-1] if skills else None
    accepted = next((event for event in reversed(events) if event.get("type") == "skill_accepted"), None)
    source_prs = ((accepted or {}).get("payload") or {}).get("source_pr_numbers")
    lines = []
    for index, line in enumerate(str(latest.get("content", "") if latest else "").splitlines()):
        if not line.strip():
            continue
        lines.append(
            {
                "line": index + 1,
                "text": line,
                # We have version-level evidence only; do not claim it identifies
                # the source of an individual line.
                "origin": "initial" if accepted is None else "unattributed",
            }
        )
    return {
        "status": "initial_only" if accepted is None else "version_attributed" if source_prs else "unattributed",
        "version": latest.get("version") if latest else None,
        "source_pr_numbers": source_prs if isinstance(source_prs, list) else [],
        "lines": lines,
    }


def _event_feature(
    events: list[dict[str, Any]], completion_type: str,
) -> dict[str, Any] | None:
    event = next((row for row in reversed(events) if row.get("type") == completion_type), None)
    if event is None:
        return None
    payload = event.get("payload") or {}
    result: dict[str, Any] = {"status": "measured"}
    mode = payload.get("mode")
    if isinstance(mode, str) and len(mode) <= 40:
        result["mode"] = mode
    for key in ("task_ids", "line_impacts", "variants", "divergence_step", "skill_line"):
        value = payload.get(key)
        if key == "task_ids" and isinstance(value, list):
            result[key] = [item for item in value if isinstance(item, str)][:50]
        elif key == "line_impacts" and isinstance(value, list):
            result[key] = [
                {"line": item.get("line"), "impact": item.get("impact")}
                for item in value
                if isinstance(item, dict)
                and isinstance(item.get("line"), int)
                and isinstance(item.get("impact"), (int, float))
            ][:200]
        elif key in {"divergence_step", "skill_line"} and isinstance(value, int):
            result[key] = value
        elif key == "variants" and isinstance(value, list):
            result[key] = [
                {
                    field: item.get(field)
                    for field in ("id", "tokens", "passed", "total", "rate")
                    if isinstance(item.get(field), (str, int, float, bool))
                }
                for item in value if isinstance(item, dict)
            ][:20]
    return result


def _feature_or_unavailable(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    return _event_feature(events, event_type) or {
        "status": "unavailable",
        "reason": "No saved results for this feature",
    }


def _task_level_xray(
    phase_trials: dict[str, dict[str, dict[str, Any]]],
    trials: list[dict[str, Any]],
) -> dict[str, Any]:
    by_config: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for trial in trials:
        config_id = trial.get("config_id")
        if config_id in {"flash_no_skill", "flash_current_skill"}:
            task_id = str(trial.get("task_id") or f"pr-{trial.get('pr_number')}")
            by_config[config_id][task_id] = trial
    controlled = _paired_outcomes(
        {
            task_id: row for task_id, row in by_config.get("flash_no_skill", {}).items()
            if row.get("model") == "deepseek/deepseek-v4.1-flash"
        },
        {
            task_id: row for task_id, row in by_config.get("flash_current_skill", {}).items()
            if row.get("model") == "deepseek/deepseek-v4.1-flash"
        },
    )
    if controlled:
        return {
            "status": "measured",
            "mode": "task_level",
            "left_label": "no skill",
            "right_label": "current skill",
            "reason": "Same model and held-out task; full-task outcomes do not isolate a skill line or agent step",
            "outcomes": controlled,
            "changed_tasks": sum(item["changed"] for item in controlled),
            "total_tasks": len(controlled),
        }
    baseline = phase_trials.get("baseline", {})
    final = phase_trials.get("final", {})
    outcomes = []
    for task_id in sorted(baseline.keys() & final.keys()):
        before = baseline[task_id].get("passed")
        after = final[task_id].get("passed")
        if before not in (True, False) or after not in (True, False):
            continue
        outcomes.append(
            {
                "task_id": task_id,
                "baseline_passed": before,
                "final_passed": after,
                "changed": before is not after,
            }
        )
    if not outcomes:
        return {
            "status": "unavailable",
            "mode": "task_level",
            "reason": "No paired baseline and final task outcomes are saved",
            "outcomes": [],
        }
    return {
        "status": "measured",
        "mode": "task_level",
        "left_label": "baseline",
        "right_label": "final",
        "reason": "Full-task outcomes show association; they do not isolate a skill line or agent step",
        "outcomes": outcomes,
        "changed_tasks": sum(item["changed"] for item in outcomes),
        "total_tasks": len(outcomes),
    }


def _paired_outcomes(
    left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    outcomes = []
    for task_id in sorted(left.keys() & right.keys()):
        left_passed = left[task_id].get("passed")
        right_passed = right[task_id].get("passed")
        if left_passed not in (True, False) or right_passed not in (True, False):
            continue
        outcomes.append(
            {
                "task_id": task_id,
                "baseline_passed": left_passed,
                "final_passed": right_passed,
                "changed": left_passed is not right_passed,
            }
        )
    return outcomes


def _task_battle(trials: list[dict[str, Any]]) -> dict[str, Any]:
    by_config: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for trial in trials:
        config_id = trial.get("config_id")
        if config_id in {"flash_no_skill", "flash_current_skill"}:
            task_id = str(trial.get("task_id") or f"pr-{trial.get('pr_number')}")
            by_config[config_id][task_id] = trial
    no_skill = {
        task_id: row for task_id, row in by_config.get("flash_no_skill", {}).items()
        if row.get("model") == "deepseek/deepseek-v4.1-flash"
    }
    current_skill = {
        task_id: row for task_id, row in by_config.get("flash_current_skill", {}).items()
        if row.get("model") == "deepseek/deepseek-v4.1-flash"
    }
    task_ids = sorted(no_skill.keys() & current_skill.keys())
    if not task_ids:
        return {"status": "unavailable", "reason": "No matched Flash ladder task pair is saved"}
    return {
        "status": "measured",
        "mode": "safe_action_labels",
        "task_ids": task_ids,
        "model": "deepseek/deepseek-v4.1-flash",
        "reason": "Replay includes tool labels only; prompts, arguments, and outputs stay private",
    }
