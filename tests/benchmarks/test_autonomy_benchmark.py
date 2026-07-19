"""Task 1 preregistration tests for the frozen autonomy 50-case corpus.

The corpus in ``benchmarks/autonomy/`` is the 90-day gate. These tests
freeze its identity: exact case IDs, strata, gates, denominators, and
per-case declarations. Changing the corpus must fail these tests.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

from agent.autonomy import ACTION_CLASSES

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
