from alangym.experiment import _prior_pass_regressions


def test_ratchet_detects_only_confirmed_prior_pass_to_fail_flips():
    current = [
        {"pr_number": 101, "passed": True},
        {"pr_number": 202, "passed": False},
        {"pr_number": 303, "passed": None},
    ]
    candidate = [
        {"pr_number": 101, "passed": False},
        {"pr_number": 202, "passed": True},
        {"pr_number": 303, "passed": False},
    ]

    assert _prior_pass_regressions(current, candidate) == [101]


def test_ratchet_marks_retry_disagreement_as_flaky():
    from alangym.experiment import _flaky_pr_numbers

    assert _flaky_pr_numbers(
        [{"pr_number": 101, "passed": False}],
        [{"pr_number": 101, "passed": True}],
    ) == [101]

