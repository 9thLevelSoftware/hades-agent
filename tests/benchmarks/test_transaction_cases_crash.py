"""Preregistered crash cases through the real transaction stack.

One stratum per file so the 100-case corpus parallelizes across the
per-file test runner instead of hitting its serial-file time cap. The
frozen denominator and gates live in test_transaction_benchmark.py.
"""

from pathlib import Path

import pytest

from benchmarks.transactions.cases import load_cases

ROOT = Path(__file__).resolve().parents[2]
STRATUM = "crash"
_, _ALL_CASES = load_cases(ROOT / "benchmarks/transactions/manifest.yaml")
CASES = [case for case in _ALL_CASES if case["stratum"] == STRATUM]


@pytest.fixture(scope="module")
def benchmark_base(tmp_path_factory):
    return tmp_path_factory.mktemp(f"tx-crash")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_preregistered_transaction_case(case, benchmark_base):
    from benchmarks.transactions.runner import run_case

    result = run_case(case, benchmark_base)
    assert result.passed, result
    assert result.unauthorized_irreversible_commits == 0
    assert result.duplicate_effects == 0
    assert result.compensation_order_correct
    assert result.every_non_reversible_classified
    assert not result.false_success_receipt
