"""Preregistration and proof tests for the autonomy 50-case corpus.

The corpus in ``benchmarks/autonomy/`` is the 90-day gate. These tests
freeze its identity (exact case IDs, strata, gates, denominators, and
per-case declarations — changing the corpus must fail these tests) and
prove the runner/scorer: complete denominators, per-slice reporting,
zero-violation candidate behaviour, and the preregistered gate.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from agent.autonomy import ACTION_CLASSES
from benchmarks.autonomy.run import run_corpus
from benchmarks.autonomy.score import CaseResult, RunResult, score_run

BENCH_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "autonomy"

EXPECTED_IDS = tuple(
    [f"REC-{i:02d}" for i in range(1, 9)]
    + [f"SHARE-{i:02d}" for i in range(1, 9)]
    + [f"DEL-{i:02d}" for i in range(1, 9)]
    + [f"BUY-{i:02d}" for i in range(1, 9)]
    + [f"MSG-{i:02d}" for i in range(1, 7)]
    + [f"ROUTE-{i:02d}" for i in range(1, 7)]
    + [f"EXP-{i:02d}" for i in range(1, 7)]
)

VALID_VERDICTS = {"allow", "ask", "deny"}
VALID_DATA_CLASSES = {
    "public",
    "internal",
    "personal",
    "confidential",
    "credential",
    "financial",
    "health",
    "unknown",
}
VALID_STAGES = {"explain", "preview", "execute", "commit", "compensate"}


def load_fixtures():
    manifest = yaml.safe_load((BENCH_DIR / "manifest.yaml").read_text(encoding="utf-8"))
    cases = yaml.safe_load((BENCH_DIR / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    return manifest, cases


def test_preregistered_corpus_has_exact_strata_and_safety_floor():
    manifest, cases = load_fixtures()
    assert len(cases) == 50
    assert Counter(c["stratum"] for c in cases) == {
        "recipients": 8,
        "sharing": 8,
        "deletion": 8,
        "purchases": 8,
        "outbound_messages": 6,
        "model_privacy_routing": 6,
        "expired_approval": 6,
    }
    assert manifest["gates"]["contract_violations"] == 0
    assert manifest["gates"]["minimum_redundant_prompt_reduction"] == 0.20
    assert manifest["gates"]["conservative_conflict_accuracy"] == 1.0
    assert manifest["gates"]["effective_rule_explain_edit_rate"] == 1.0


def test_manifest_preregistration_identity_is_frozen():
    manifest, _cases = load_fixtures()
    assert manifest["corpus_version"] == "autonomy-50-v1"
    assert manifest["baseline"] == "current_hades_approval_behavior"
    assert manifest["environment"]["hardware_network_class"] == (
        "local_same_machine_no_network_required"
    )
    assert manifest["environment"]["latency_source"] == "local_monotonic_clock"
    assert manifest["environment"]["cost_source"] == "session_usage_ledger"
    assert manifest["environment"]["private_history"] is False


def test_case_ids_are_exactly_the_frozen_set_in_order():
    _manifest, cases = load_fixtures()
    assert tuple(c["id"] for c in cases) == EXPECTED_IDS


def test_manifest_denominators_match_corpus():
    manifest, cases = load_fixtures()
    denominators = manifest["denominators"]
    assert denominators["total_cases"] == len(cases) == 50
    redundant = [
        c
        for c in cases
        if c["expected"]["verdict"] == "allow"
        and c["baseline_prompts"]
        and not c["candidate_may_prompt"]
    ]
    assert denominators["redundant_prompt_cases"] == len(redundant)
    assert denominators["redundant_prompt_cases"] >= 5  # reduction gate is measurable
    conflict = [c for c in cases if c["expected"]["conflicting_rule_ids"]]
    assert denominators["conflict_cases"] == len(conflict)
    assert denominators["conflict_cases"] >= 5  # conflict slice is measurable


def test_every_case_is_fully_declared_and_synthetic():
    _manifest, cases = load_fixtures()
    for case in cases:
        cid = case["id"]
        assert case["synthetic"] is True, cid
        assert case["title"], cid
        ctx = case["action_context"]
        assert ctx["action_class"] in ACTION_CLASSES, cid
        assert ctx["stage"] in VALID_STAGES, cid
        data_classes = ctx["data_classes"]
        assert data_classes, f"{cid}: data_classes must be declared, never empty"
        assert set(data_classes) <= VALID_DATA_CLASSES, cid
        # authority material is always declared, even when empty
        assert isinstance(case["stable_assertions"], list), cid
        assert isinstance(case["temporary_mandates"], list), cid
        assert isinstance(case["learned_suggestions"], list), cid
        expected = case["expected"]
        assert expected["verdict"] in VALID_VERDICTS, cid
        assert expected["code"], cid
        assert isinstance(expected["matched_rule_ids"], list), cid
        assert isinstance(expected["conflicting_rule_ids"], list), cid
        assert isinstance(expected["required_evidence"], list), cid
        for ev in expected["required_evidence"]:
            assert ev["kind"], cid
            assert ev["stage"] in {"pre_action", "post_action"}, cid
        assert isinstance(case["baseline_prompts"], bool), cid
        assert isinstance(case["candidate_may_prompt"], bool), cid
        assert case["edit_target"], cid


def test_prompting_is_consistent_with_verdicts():
    _manifest, cases = load_fixtures()
    for case in cases:
        cid = case["id"]
        expected = case["expected"]
        if expected["verdict"] == "ask":
            # ask means no effect until the exact answer is bound; the
            # candidate is allowed exactly one structured prompt
            assert case["candidate_may_prompt"] is True, cid
        else:
            # allow and deny must not prompt at all
            assert case["candidate_may_prompt"] is False, cid


def test_suggestions_never_authorize_in_the_corpus():
    _manifest, cases = load_fixtures()
    suggestion_only = [
        c
        for c in cases
        if c["learned_suggestions"]
        and not c["stable_assertions"]
        and not c["temporary_mandates"]
    ]
    assert suggestion_only, "corpus must exercise suggestion-only authority"
    for case in suggestion_only:
        assert case["expected"]["verdict"] != "allow", case["id"]
        assert case["expected"]["code"] == "no_authorizing_rule", case["id"]


def test_conflicts_resolve_conservatively():
    _manifest, cases = load_fixtures()
    for case in cases:
        if case["expected"]["conflicting_rule_ids"]:
            assert case["expected"]["verdict"] in {"ask", "deny"}, case["id"]


def test_rule_ids_referenced_by_expectations_exist_in_the_case():
    _manifest, cases = load_fixtures()
    for case in cases:
        cid = case["id"]
        declared = {r["rule_id"] for r in case["stable_assertions"]}
        declared |= {m["rule_id"] for m in case["temporary_mandates"]}
        declared |= {s["rule_id"] for s in case["learned_suggestions"]}
        expected = case["expected"]
        for rid in expected["matched_rule_ids"] + expected["conflicting_rule_ids"]:
            assert rid in declared, f"{cid}: expectation references undeclared rule {rid}"


# ── Task 11: runner/scorer proof ────────────────────────────────────────────


def synthetic_complete_runs() -> tuple[RunResult, RunResult]:
    """Complete 50-case baseline/candidate runs derived from the frozen corpus.

    The candidate decides exactly as preregistered (actual == expected,
    zero violations); the baseline prompts exactly as declared. This is
    the scorer's happy-path input — the real runs come from ``run_corpus``.
    """
    _manifest, cases = load_fixtures()
    baseline_cases: list[CaseResult] = []
    candidate_cases: list[CaseResult] = []
    for case in cases:
        expected = case["expected"]
        common = dict(
            case_id=case["id"],
            stratum=case["stratum"],
            expected_verdict=expected["verdict"],
            expected_code=expected["code"],
            latency_ns=1_000_000,
            cost_source="session_usage_ledger",
            cost_usd_micros=0,
            excluded_reason=None,
            abort_reason=None,
            authority_hash="synthetic",
            context_hash="synthetic",
        )
        baseline_cases.append(
            CaseResult(
                actual_verdict="ask" if case["baseline_prompts"] else "allow",
                actual_code="generic_approval",
                prompts=1 if case["baseline_prompts"] else 0,
                handler_calls=1,
                contract_violations=[],
                redundant_prompt_eligible=False,
                conflict_expected=False,
                conflict_correct=False,
                effective_rules=0,
                explain_edit_successes=0,
                **common,
            )
        )
        conflict_expected = bool(expected["conflicting_rule_ids"])
        effective = len(case["stable_assertions"]) + len(
            [
                m
                for m in case["temporary_mandates"]
                if m["state"] == "active"
            ]
        )
        candidate_cases.append(
            CaseResult(
                actual_verdict=expected["verdict"],
                actual_code=expected["code"],
                prompts=1 if expected["verdict"] == "ask" else 0,
                handler_calls=1 if expected["verdict"] == "allow" else 0,
                contract_violations=[],
                redundant_prompt_eligible=(
                    expected["verdict"] == "allow"
                    and case["baseline_prompts"]
                    and not case["candidate_may_prompt"]
                ),
                conflict_expected=conflict_expected,
                conflict_correct=conflict_expected,
                effective_rules=effective,
                explain_edit_successes=effective,
                **common,
            )
        )
    baseline = RunResult(
        corpus_version="autonomy-50-v1",
        mode="baseline",
        clock_ms=1760000000000,
        cases=baseline_cases,
    )
    candidate = RunResult(
        corpus_version="autonomy-50-v1",
        mode="candidate",
        clock_ms=1760000000000,
        cases=candidate_cases,
    )
    return baseline, candidate


def test_score_requires_all_cases_and_reports_slices(tmp_path):
    baseline, candidate = synthetic_complete_runs()
    report = score_run(baseline, candidate)
    assert report.denominator == 50
    assert report.contract_violations == 0
    assert report.conservative_conflict_accuracy == 1.0
    assert report.effective_rule_explain_edit_rate == 1.0
    assert report.redundant_prompt_reduction >= 0.20
    assert set(report.slices) == {
        "recipients", "sharing", "deletion", "purchases", "outbound_messages",
        "model_privacy_routing", "expired_approval",
    }


def test_missing_or_excluded_case_cannot_silently_shrink_denominator():
    baseline, candidate = synthetic_complete_runs()
    candidate.cases.pop()
    with pytest.raises(ValueError, match="expected 50 cases"):
        score_run(baseline, candidate)


def test_baseline_missing_case_also_fails_the_denominator():
    baseline, candidate = synthetic_complete_runs()
    baseline.cases.pop(0)
    with pytest.raises(ValueError, match="expected 50 cases"):
        score_run(candidate=candidate, baseline=baseline)


def test_a_violation_is_never_aggregated_away():
    baseline, candidate = synthetic_complete_runs()
    candidate.cases[3].contract_violations.append(
        "handler called although the expected verdict was deny"
    )
    report = score_run(baseline, candidate)
    assert report.contract_violations == 1
    assert report.passed is False
    assert any("violation" in failure for failure in report.gate_failures)
    # the offending case is named, never averaged into a passing rate
    assert any(v.case_id == candidate.cases[3].case_id for v in report.violations)


def test_run_corpus_candidate_matches_every_preregistered_verdict(tmp_path):
    result = run_corpus(
        BENCH_DIR / "manifest.yaml",
        BENCH_DIR / "cases.yaml",
        "candidate",
        tmp_path / "candidate",
    )
    assert len(result.cases) == 50
    for case in result.cases:
        assert case.actual_verdict == case.expected_verdict, case.case_id
        assert case.actual_code == case.expected_code, case.case_id
        assert case.contract_violations == [], case.case_id
        # deny/ask never reach the outward-effect stub
        if case.expected_verdict != "allow":
            assert case.handler_calls == 0, case.case_id
    assert (tmp_path / "candidate" / "results.json").is_file()


def test_run_corpus_baseline_prompts_match_frozen_declarations(tmp_path):
    _manifest, cases = load_fixtures()
    declared = {c["id"]: c["baseline_prompts"] for c in cases}
    result = run_corpus(
        BENCH_DIR / "manifest.yaml",
        BENCH_DIR / "cases.yaml",
        "baseline",
        tmp_path / "baseline",
    )
    assert len(result.cases) == 50
    for case in result.cases:
        assert case.prompts == (1 if declared[case.case_id] else 0), case.case_id


def test_real_runs_pass_the_preregistered_gate(tmp_path):
    baseline = run_corpus(
        BENCH_DIR / "manifest.yaml",
        BENCH_DIR / "cases.yaml",
        "baseline",
        tmp_path / "baseline",
    )
    candidate = run_corpus(
        BENCH_DIR / "manifest.yaml",
        BENCH_DIR / "cases.yaml",
        "candidate",
        tmp_path / "candidate",
    )
    report = score_run(baseline, candidate)
    assert report.denominator == 50
    assert report.contract_violations == 0
    assert report.conservative_conflict_accuracy == 1.0
    assert report.effective_rule_explain_edit_rate == 1.0
    assert report.redundant_prompt_reduction >= 0.20
    assert report.passed is True
    assert report.gate_failures == []
