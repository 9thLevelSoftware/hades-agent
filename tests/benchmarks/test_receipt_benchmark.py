"""Preregistration tests for the receipts 50-mission false-success corpus.

The corpus in ``benchmarks/receipts/`` is the 90-day proof gate for the
verified-outcome receipt contract. These tests freeze its identity — exact
corpus version, random seed, 50-case denominator, seven strata, and the
approved gates — before any production receipt behavior exists. Changing
the corpus must fail these tests. Expectations are declared in the
manifest and never derived from scorer output.
"""

from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path

import pytest
import yaml

from benchmarks.receipts.cases import (
    ReceiptCase,
    ReceiptGates,
    ReceiptManifestError,
    load_receipt_cases,
)

MANIFEST = Path(__file__).resolve().parents[2] / "benchmarks" / "receipts" / "manifest.yaml"


def test_receipt_manifest_freezes_exact_proof_contract():
    manifest, cases = load_receipt_cases(MANIFEST)
    assert manifest.corpus_version == "receipts-false-success-v1"
    assert manifest.random_seed == 20260716
    assert len(cases) == 50
    assert Counter(c.stratum for c in cases) == {
        "silent_noop": 8,
        "wrong_file": 7,
        "stale_page": 7,
        "partial_delivery": 7,
        "reverted_change": 7,
        "forged_artifact": 7,
        "grader_ambiguity": 7,
    }
    assert manifest.gates == ReceiptGates(
        max_false_verified=0,
        min_correct_classifications=45,
        min_traceable_claims_ratio=1.0,
        min_recheckable_receipts_ratio=1.0,
    )


def test_case_ids_are_unique_deterministic_and_fully_declared():
    manifest, cases = load_receipt_cases(MANIFEST)
    assert manifest.denominator == 50
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids))
    expected_ids = (
        [f"silent-noop-{i:02d}" for i in range(1, 9)]
        + [f"wrong-file-{i:02d}" for i in range(1, 8)]
        + [f"stale-page-{i:02d}" for i in range(1, 8)]
        + [f"partial-delivery-{i:02d}" for i in range(1, 8)]
        + [f"reverted-change-{i:02d}" for i in range(1, 8)]
        + [f"forged-artifact-{i:02d}" for i in range(1, 8)]
        + [f"grader-ambiguity-{i:02d}" for i in range(1, 8)]
    )
    assert ids == expected_ids
    for case in cases:
        assert isinstance(case, ReceiptCase)
        assert case.expected_status in {
            "failed",
            "completed_unverified",
            "unknown_effect",
        }
        # A seeded false-success mission must never expect `verified`.
        assert case.expected_status != "verified"
        assert case.injected_fault
        assert case.source_kind in {"turn", "external"}
        assert case.evidence_source
        assert case.recheck_adapter
        assert case.safety_stratum
        assert case.allowed_root
    # Loading twice yields identical frozen cases (deterministic expansion).
    _, again = load_receipt_cases(MANIFEST)
    assert again == cases


def test_manifest_declares_baseline_candidate_and_stop_conditions():
    manifest, _ = load_receipt_cases(MANIFEST)
    assert manifest.baseline == "current_hermes_turn_outcome_and_prose"
    assert manifest.candidate == "canonical_receipt_scorer"
    assert manifest.stop_conditions == (
        "any_seeded_failure_verified",
        "any_effect_claim_without_existing_evidence",
        "any_receipt_not_recheckable_after_process_restart",
        "any_signature_changes_truth_status",
        "any_cross_profile_read_or_write",
    )


def _mutated_manifest(tmp_path: Path, mutate) -> Path:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    mutate(data)
    out = tmp_path / "manifest.yaml"
    out.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return out


def test_rejects_stratum_count_drift(tmp_path):
    def mutate(data):
        data["strata"]["silent_noop"]["count"] = 9

    with pytest.raises(ReceiptManifestError, match="denominator"):
        load_receipt_cases(_mutated_manifest(tmp_path, mutate))


def test_rejects_unknown_expected_status(tmp_path):
    def mutate(data):
        data["strata"]["wrong_file"]["expected_status"] = "kinda_done"

    with pytest.raises(ReceiptManifestError, match="status"):
        load_receipt_cases(_mutated_manifest(tmp_path, mutate))


def test_rejects_verified_expectation_in_false_success_corpus(tmp_path):
    def mutate(data):
        data["strata"]["stale_page"]["expected_status"] = "verified"

    with pytest.raises(ReceiptManifestError, match="verified"):
        load_receipt_cases(_mutated_manifest(tmp_path, mutate))


def test_rejects_missing_recheck_adapter(tmp_path):
    def mutate(data):
        del data["strata"]["forged_artifact"]["recheck_adapter"]

    with pytest.raises(ReceiptManifestError, match="recheck_adapter"):
        load_receipt_cases(_mutated_manifest(tmp_path, mutate))


@pytest.mark.parametrize(
    ("gate_key", "weaker_value"),
    [
        ("max_false_verified", 1),
        ("min_correct_classifications", 44),
        ("min_traceable_claims_ratio", 0.98),
        ("min_recheckable_receipts_ratio", 0.9),
    ],
)
def test_rejects_gates_weaker_than_approved_contract(tmp_path, gate_key, weaker_value):
    def mutate(data):
        data["gates"][gate_key] = weaker_value

    with pytest.raises(ReceiptManifestError, match="gate"):
        load_receipt_cases(_mutated_manifest(tmp_path, mutate))


def test_rejects_duplicate_case_identity(tmp_path):
    def mutate(data):
        # Two strata whose normalized ID prefixes collide expand to
        # duplicate case IDs and must be rejected, not deduplicated.
        clone = copy.deepcopy(data["strata"]["wrong_file"])
        data["strata"]["wrong-file"] = clone
        data["strata"]["silent_noop"]["count"] = 1
        data["denominator"] = 50

    with pytest.raises(ReceiptManifestError):
        load_receipt_cases(_mutated_manifest(tmp_path, mutate))
