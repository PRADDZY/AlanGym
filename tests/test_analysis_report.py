from alangym.analysis_report import build_analysis_report


def test_report_marks_missing_ladder_configs_unavailable_and_builds_task_heatmap():
    report = build_analysis_report(_run_detail())

    assert [item["id"] for item in report["ladder"]["configs"]] == [
        "flash_no_skill", "flash_current_skill", "deepseek_pro_no_skill",
    ]
    assert all(item["status"] == "unavailable" for item in report["ladder"]["configs"])
    assert report["heatmap"]["tasks"] == [
        {"task_id": "pr-101", "pr_number": 101, "split": "train", "heldout_cohort": None, "outcomes": {"baseline": True}},
        {"task_id": "pr-202", "pr_number": 202, "split": "heldout", "heldout_cohort": None, "outcomes": {"baseline": False}},
        {"task_id": "pr-303", "pr_number": 303, "split": "heldout", "heldout_cohort": None, "outcomes": {"baseline": True}},
    ]
    assert report["training"]["baseline"]["passed"] == 1
    assert report["training"]["baseline"]["rate"] == 1.0
    assert report["heldout"]["baseline"]["passed"] == 1
    assert report["heldout"]["baseline"]["rate"] == 0.5


def test_report_only_calculates_cost_when_billing_is_verified():
    detail = _run_detail()
    detail["trials"].append(
        {
            "id": "ladder:1", "task_id": "pr-202", "pr_number": 202,
            "phase": "ladder", "config_id": "flash_no_skill", "passed": True,
            "model": "deepseek/deepseek-v4.1-flash", "skill_mode": "none",
            "tokens_input": 100, "tokens_output": 10, "cost_usd": 0.0,
            "cost_verified": False,
        }
    )
    detail["trials"].append(
        {
            "id": "ladder:1b", "task_id": "pr-303", "pr_number": 303,
            "phase": "ladder", "config_id": "flash_no_skill", "passed": True,
            "model": "deepseek/deepseek-v4.1-flash", "skill_mode": "none",
            "tokens_input": 200, "tokens_output": 20, "cost_usd": 0.5,
            "cost_verified": True,
        }
    )

    row = build_analysis_report(detail)["ladder"]["configs"][0]

    assert row["status"] == "measured"
    assert row["passed"] == 2
    assert row["total"] == 2
    assert row["tokens_input"] == 300
    assert row["tokens_output"] == 30
    assert row["cost_usd"] is None
    assert row["cost_per_pass"] is None


def test_report_preserves_zero_pass_as_unavailable_cost_per_pass():
    detail = _run_detail()
    detail["trials"].append(
        {
            "id": "ladder:2", "task_id": "pr-202", "pr_number": 202,
            "phase": "ladder", "config_id": "flash_no_skill", "passed": False,
            "model": "deepseek/deepseek-v4.1-flash", "skill_mode": "none",
            "tokens_input": 100, "tokens_output": 10, "cost_usd": 1.0,
            "cost_verified": True,
        }
    )
    detail["trials"].append(
        {
            "id": "ladder:2b", "task_id": "pr-303", "pr_number": 303,
            "phase": "ladder", "config_id": "flash_no_skill", "passed": False,
            "model": "deepseek/deepseek-v4.1-flash", "skill_mode": "none",
            "tokens_input": 100, "tokens_output": 10, "cost_usd": 0.5,
            "cost_verified": True,
        }
    )

    row = build_analysis_report(detail)["ladder"]["configs"][0]

    assert row["status"] == "measured"
    assert row["passed"] == 0
    assert row["cost_usd"] == 1.5
    assert row["cost_per_pass"] is None


def test_xray_fallback_reports_task_outcomes_without_claiming_line_causality():
    detail = _run_detail()
    detail["trials"].append(
        {
            "id": "final:202", "task_id": "pr-202", "pr_number": 202,
            "phase": "final", "passed": True,
        }
    )

    xray = build_analysis_report(detail)["xray"]

    assert xray["status"] == "measured"
    assert xray["mode"] == "task_level"
    assert xray["changed_tasks"] == 1
    assert xray["outcomes"] == [
        {"task_id": "pr-202", "baseline_passed": False, "final_passed": True, "changed": True}
    ]
    assert "do not isolate" in xray["reason"]


def test_line_provenance_does_not_assign_version_sources_to_every_line():
    detail = _run_detail()
    detail["events"].append(
        {
            "type": "skill_accepted",
            "payload": {"version": "v2", "source_pr_numbers": [101, 102]},
        }
    )
    detail["skills"].append({"version": "v2", "content": "# Repair\n- Run tests"})

    provenance = build_analysis_report(detail)["provenance"]

    assert provenance["status"] == "version_attributed"
    assert provenance["source_pr_numbers"] == [101, 102]
    assert {line["origin"] for line in provenance["lines"]} == {"unattributed"}


def test_controlled_ladder_pair_powers_task_level_xray_and_battle_status():
    detail = _run_detail()
    detail["trials"].extend(
        [
            {
                "id": "ladder:none", "task_id": "pr-202", "pr_number": 202,
                "phase": "ladder", "config_id": "flash_no_skill", "skill_mode": "none",
                "model": "deepseek/deepseek-v4.1-flash", "passed": False,
            },
            {
                "id": "ladder:skill", "task_id": "pr-202", "pr_number": 202,
                "phase": "ladder", "config_id": "flash_current_skill", "skill_mode": "current",
                "model": "deepseek/deepseek-v4.1-flash", "passed": True,
            },
        ]
    )

    report = build_analysis_report(detail)

    assert report["xray"]["mode"] == "task_level"
    assert report["xray"]["left_label"] == "no skill"
    assert report["xray"]["right_label"] == "current skill"
    assert report["xray"]["outcomes"] == [
        {"task_id": "pr-202", "baseline_passed": False, "final_passed": True, "changed": True}
    ]
    assert report["battle"]["status"] == "measured"
    assert report["battle"]["task_ids"] == ["pr-202"]


def _run_detail():
    return {
        "run": {"id": "run-1", "model": "deepseek/deepseek-v4.1-flash", "heldout_count": 2},
        "tasks": [
            {"id": "pr-101", "pr_number": 101, "split": "train"},
            {"id": "pr-202", "pr_number": 202, "split": "heldout"},
            {"id": "pr-303", "pr_number": 303, "split": "heldout"},
        ],
        "trials": [
            {
                "id": "baseline:101", "task_id": "pr-101", "pr_number": 101,
                "phase": "baseline", "passed": True,
                "model": "deepseek/deepseek-v4.1-flash", "skill_version": "v1",
            },
            {
                "id": "baseline:202", "task_id": "pr-202", "pr_number": 202,
                "phase": "baseline", "passed": False,
                "model": "deepseek/deepseek-v4.1-flash", "skill_version": "v1",
            },
            {
                "id": "baseline:303", "task_id": "pr-303", "pr_number": 303,
                "phase": "baseline", "passed": True,
                "model": "deepseek/deepseek-v4.1-flash", "skill_version": "v1",
            },
        ],
        "skills": [{"version": "v1", "content": "# Repair\n- Run tests"}],
        "events": [],
    }
