from alangym.mining import PullRequestCandidate
from alangym.validation import (
    TaskValidation,
    TestResult,
    _is_missing_symbol_from_changed_module,
)


def test_import_failure_from_the_changed_module_counts_as_a_real_regression():
    candidate = PullRequestCandidate(
        number=278,
        title="Adds noop function satisfying PEP 559",
        body="",
        merged_at="2026-10-01T00:00:00Z",
        merge_sha="merge",
        base_sha="base",
        source_paths=frozenset({"boltons/funcutils.py"}),
        test_paths=("tests/test_funcutils.py",),
    )
    before = TestResult(
        status="invalid",
        returncode=2,
        passed_count=0,
        failed_count=0,
        output="ImportError: cannot import name 'noop' from 'boltons.funcutils'",
        elapsed_seconds=0.1,
    )

    assert _is_missing_symbol_from_changed_module(before, candidate)
    assert TaskValidation(candidate, before, before).before_regression_count == 1


def test_import_failure_in_an_unrelated_module_is_not_a_regression():
    candidate = PullRequestCandidate(
        number=278,
        title="Adds noop function satisfying PEP 559",
        body="",
        merged_at="2026-10-01T00:00:00Z",
        merge_sha="merge",
        base_sha="base",
        source_paths=frozenset({"boltons/funcutils.py"}),
        test_paths=("tests/test_funcutils.py",),
    )
    before = TestResult(
        status="invalid",
        returncode=2,
        passed_count=0,
        failed_count=0,
        output="ImportError: cannot import name 'Path' from 'pathlib'",
        elapsed_seconds=0.1,
    )

    assert not _is_missing_symbol_from_changed_module(before, candidate)


def test_source_import_error_requires_a_collection_error_status():
    candidate = PullRequestCandidate(
        number=278,
        title="Adds noop function satisfying PEP 559",
        body="",
        merged_at="2026-10-01T00:00:00Z",
        merge_sha="merge",
        base_sha="base",
        source_paths=frozenset({"boltons/funcutils.py"}),
        test_paths=("tests/test_funcutils.py",),
    )
    before = TestResult(
        status="failed",
        returncode=1,
        passed_count=2,
        failed_count=1,
        output="ImportError: cannot import name 'noop' from 'boltons.funcutils'",
        elapsed_seconds=0.1,
    )

    assert not _is_missing_symbol_from_changed_module(before, candidate)
