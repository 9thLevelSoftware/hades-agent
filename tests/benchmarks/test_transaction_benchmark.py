"""Preregistered contract tests for the action-transaction benchmark.

These tests freeze the 100-case denominator, strata, gates, and reporting
rules BEFORE any production transaction code exists (plan Task 0). The
benchmark runner (plan Task 13) must consume this exact frozen contract.
"""

from pathlib import Path

import pytest
import yaml

from benchmarks.transactions.cases import load_cases

ROOT = Path(__file__).resolve().parents[2]


def test_transaction_benchmark_is_frozen_bounded_and_truthful():
    manifest, cases = load_cases(ROOT / "benchmarks/transactions/manifest.yaml")
    assert manifest["schema"] == "hermes.action-transactions-benchmark.v1"
    assert len(cases) == 100
    assert {case["stratum"] for case in cases} == {
        "revision", "stale_authority", "crash", "duplicate_delivery",
        "partial_failure", "compensation_boundary",
    }
    assert manifest["gates"] == {
        "unauthorized_irreversible_commits": 0,
        "duplicate_instrumented_effects": 0,
        "incorrect_compensation_order": 0,
        "unclassified_non_reversible_effects": 0,
        "false_success_receipts": 0,
        "median_eligible_overhead_ratio_max": 0.15,
    }
    assert manifest["reporting"]["rate_interval"] == "wilson_95"
    assert manifest["baseline"] == "current_hermes_without_transaction_coordinator"
    # Rollout gates: commit mode is recommended only after the 100-case
    # pass, zero safety regressions, and <15% median eligible overhead.
    assert manifest["rollout"]["require_100_case_pass"] is True
    assert manifest["rollout"]["require_zero_safety_regressions"] is True
    assert manifest["rollout"]["require_median_eligible_overhead_below"] == 0.15


def test_case_expansion_is_deterministic_and_unique():
    _, first = load_cases(ROOT / "benchmarks/transactions/manifest.yaml")
    _, second = load_cases(ROOT / "benchmarks/transactions/manifest.yaml")
    assert first == second
    assert len({case["id"] for case in first}) == 100
    crash_cases = [case for case in first if case["stratum"] == "crash"]
    assert len(crash_cases) == 25
    fault_points = {case["fault_point"] for case in crash_cases}
    assert fault_points == {
        "after_prepare", "after_preview", "after_commit_intent",
        "after_handler_return", "after_delivery_dispatch",
    }
    # Five crashes at each of the five boundaries.
    for point in fault_points:
        assert sum(1 for c in crash_cases if c["fault_point"] == point) == 5
    for case in first:
        assert case["expected"], f"case {case['id']} has no expected outcome"


def test_plan_fixture_is_the_three_family_graph():
    plan = yaml.safe_load(
        (ROOT / "benchmarks/transactions/fixtures/plan.yaml").read_text(
            encoding="utf-8"
        )
    )
    nodes = {node["node_id"]: node for node in plan["nodes"]}
    assert set(nodes) == {"workspace_write", "config_set", "delayed_message"}
    assert nodes["workspace_write"]["adapter_id"] == "workspace.v1"
    assert nodes["config_set"]["adapter_id"] == "hermes-config.v1"
    assert nodes["delayed_message"]["adapter_id"] == "message-outbox.v1"
    edges = {(edge["parent"], edge["child"]) for edge in plan["edges"]}
    assert edges == {
        ("workspace_write", "config_set"),
        ("config_set", "delayed_message"),
    }
    text = yaml.dump(plan)
    for secret_marker in ("api_key", "token", "password", "secret"):
        assert secret_marker not in text.lower()


def test_authority_fixture_is_bounded_and_expiring():
    authority = yaml.safe_load(
        (ROOT / "benchmarks/transactions/fixtures/authority.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert authority["irreversible_policy"] == "ask"
    assert authority["expires_at_ms"] > 0
    allowed_actions = set(authority["allowed_actions"])
    assert allowed_actions == {"write_file", "set", "send"}
    assert authority["allowed_resources"], "authority must enumerate resources"


def test_manifest_rejects_tampered_denominator(tmp_path):
    manifest_path = ROOT / "benchmarks/transactions/manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["strata"]["revision"] = 19
    broken = tmp_path / "manifest.yaml"
    broken.write_text(yaml.dump(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_cases(broken)


# ── Task 13: execute every frozen case through the real stack ────────────


MANIFEST, CASES = load_cases(ROOT / "benchmarks/transactions/manifest.yaml")


@pytest.fixture(scope="module")
def benchmark_base(tmp_path_factory):
    return tmp_path_factory.mktemp("tx-benchmark")


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


def test_report_math_gates_and_wilson_intervals():
    from benchmarks.transactions.runner import CaseResult, wilson_interval

    low, high = wilson_interval(95, 100)
    assert 0.88 < low < 0.95 < high <= 1.0
    assert wilson_interval(0, 0) == (0.0, 0.0)

    clean = CaseResult(
        case_id="x", stratum="revision", passed=True,
        transaction_status="committed", duplicate_effects=0,
        unauthorized_irreversible_commits=0,
        compensation_order_correct=True,
        every_non_reversible_classified=True,
        false_success_receipt=False,
        baseline_latency_ms=100.0, transaction_latency_ms=105.0,
    )
    assert clean.excluded_reason is None
