"""Tests for the frozen canonical receipt contract (Task 1).

Covers three surfaces:

1. ``agent.receipts`` — the one public facade: exact five-value status
   vocabulary, immutable value objects, and ``canonical_content_hash``.
2. ``agent.receipt_hashing`` — strict canonical JSON: NFC strings, UTC
   RFC 3339 timestamps, tuple-to-array conversion, finite numbers only,
   and loud rejection of everything non-canonical.
3. ``agent.receipt_models`` builders — deterministic ``clm_/evd_/art_/
   rct_/obs_`` IDs derived from content hashes, traceability validation,
   and the scorer-only sealed ``VerifiedReceiptDecision``.

Protocol note on the ``{"answer": 42}`` vector: the digest asserted below
is the SHA-256 of the exact canonical bytes ``{"answer":42}`` (UTF-8,
sorted keys, compact separators). It is an interoperability invariant —
recomputable by any independent implementation — not a snapshot of this
codebase's behavior.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from agent.receipts import (
    RECEIPT_STATUSES,
    ArtifactDigest,
    EvidenceDigest,
    Receipt,
    ReceiptClaim,
    ReceiptObservation,
    ReceiptSourceKey,
    RequestedOutcome,
    VerifiedReceiptDecision,
    canonical_content_hash,
)
from agent.receipt_models import (
    _VERIFIED_DECISION_CAPABILITY,
    _build_verified_decision,
    EvidenceSnapshot,
    ReceiptDecision,
    ReceiptEnvelope,
    ReceiptQuery,
    ReceiptSummary,
    build_artifact_digest,
    build_claim,
    build_evidence_digest,
    build_observation,
    build_receipt,
    build_requested_outcome,
)

# The plan-frozen SHA-256 of the exact canonical bytes {"answer":42}.
ANSWER_VECTOR = (
    "sha256:ecf59a2696ca44a417e20e2a7eabb1b2"
    "6e82c779f8546bea354a2cc80e8e1eed"
)

UTC_TS = "2026-07-16T12:00:00Z"


def make_claim(**overrides: object) -> ReceiptClaim:
    """Build a claim with canonical defaults; overrides win."""
    fields: dict[str, object] = {
        "claim_kind": "effect",
        "statement": "README contains marker",
        "expected_json": "null",
        "observed_json": "null",
        "evidence_ids": (),
        "artifact_ids": (),
        "required": True,
        "verdict": "unknown",
        "uncertainty": (),
    }
    fields.update(overrides)
    return build_claim(**fields)


def make_evidence(**overrides: object) -> EvidenceDigest:
    fields: dict[str, object] = {
        "evidence_kind": "turn_classification",
        "source_ref": "turn_outcomes:s1:t1",
        "producer_id": "hermes.turn-ledger",
        "observed_at": UTC_TS,
        "fresh_until": None,
        "summary": "turn ledger row",
        "payload_hash": canonical_content_hash({"row": 1}),
        "artifact_ids": (),
    }
    fields.update(overrides)
    return build_evidence_digest(**fields)


def make_artifact(**overrides: object) -> ArtifactDigest:
    fields: dict[str, object] = {
        "source_kind": "execute_code",
        "source_ref": "s1:t1:call-1",
        "display_name": "report.txt",
        "media_type": "text/plain",
        "size_bytes": 5,
        "sha256": "a" * 64,
        "mtime_ns": None,
        "captured_at": UTC_TS,
    }
    fields.update(overrides)
    return build_artifact_digest(**fields)


def make_outcome(**overrides: object) -> RequestedOutcome:
    fields: dict[str, object] = {
        "outcome_kind": "code_change",
        "description": "add marker to README",
        "constraints": ("no unrelated edits",),
        "producer_id": "hermes.turn-ledger",
    }
    fields.update(overrides)
    return build_requested_outcome(**fields)


def make_receipt(**overrides: object) -> Receipt:
    evidence = make_evidence()
    claim = make_claim(evidence_ids=(evidence.evidence_id,))
    fields: dict[str, object] = {
        "source": ReceiptSourceKey("turn", "s1:t1"),
        "subject_kind": "turn",
        "subject_id": "s1:t1",
        "session_id": "s1",
        "turn_id": "t1",
        "mission_id": None,
        "transaction_id": None,
        "requested_outcome": make_outcome(),
        "status": "completed_unverified",
        "claims": (claim,),
        "evidence": (evidence,),
        "artifacts": (),
        "uncertainty": ("verification missing",),
        "scorer_id": "hermes.code-end-state",
        "scorer_version": "1.0.0",
        "decided_at": UTC_TS,
    }
    fields.update(overrides)
    return build_receipt(**fields)


# ---------------------------------------------------------------------------
# Public contract — statuses, immutability, sealed decision.
# ---------------------------------------------------------------------------


def test_public_status_contract_and_immutable_claim():
    assert RECEIPT_STATUSES == frozenset({
        "verified", "completed_unverified", "failed", "blocked", "unknown_effect",
    })
    claim = make_claim(statement="README contains marker", evidence_ids=("evd_a",))
    with pytest.raises(FrozenInstanceError):
        claim.statement = "changed"


def test_hash_is_stable_across_key_order_and_rejects_non_finite_float():
    assert canonical_content_hash({"b": [2], "a": "é"}) == canonical_content_hash(
        {"a": "é", "b": (2,)}
    )
    assert canonical_content_hash({"answer": 42}) == ANSWER_VECTOR
    with pytest.raises(ValueError, match="finite"):
        canonical_content_hash({"bad": float("nan")})


def test_verified_decision_has_no_public_constructor():
    with pytest.raises(TypeError):
        VerifiedReceiptDecision(scorer_id="self")


def test_all_public_value_objects_are_frozen():
    receipt = make_receipt()
    with pytest.raises(FrozenInstanceError):
        receipt.status = "verified"
    with pytest.raises(FrozenInstanceError):
        receipt.requested_outcome.description = "changed"
    with pytest.raises(FrozenInstanceError):
        receipt.evidence[0].summary = "changed"
    artifact = make_artifact()
    with pytest.raises(FrozenInstanceError):
        artifact.sha256 = "b" * 64
    with pytest.raises(FrozenInstanceError):
        ReceiptSourceKey("turn", "s1:t1").source_id = "other"


def test_receipt_fields_match_frozen_interface():
    names = [f.name for f in dataclasses.fields(Receipt)]
    assert names == [
        "receipt_id", "source", "subject_kind", "subject_id", "session_id",
        "turn_id", "mission_id", "transaction_id", "requested_outcome",
        "status", "claims", "evidence", "artifacts", "uncertainty",
        "scorer_id", "scorer_version", "decided_at", "content_hash",
    ]
    obs_names = [f.name for f in dataclasses.fields(ReceiptObservation)]
    assert obs_names == [
        "observation_id", "receipt_id", "previous_observation_id", "status",
        "claims", "evidence", "artifacts", "uncertainty", "scorer_id",
        "scorer_version", "observed_at", "content_hash",
    ]
    decision_names = [f.name for f in dataclasses.fields(VerifiedReceiptDecision)]
    assert decision_names == [
        "scorer_id", "scorer_version", "subject_kind", "subject_id",
        "snapshot_hash", "claim_hashes", "decided_at", "fresh_until",
        "decision_hash",
    ]


def test_facade_declares_explicit_all():
    import agent.receipts as receipts

    required = {
        "ReceiptStatus", "RECEIPT_STATUSES", "RequestedOutcome", "ReceiptClaim",
        "EvidenceDigest", "ArtifactDigest", "ReceiptSourceKey", "Receipt",
        "ReceiptObservation", "VerifiedReceiptDecision", "ReceiptStore",
        "canonical_content_hash", "digest_artifact",
    }
    assert required <= set(receipts.__all__)


# ---------------------------------------------------------------------------
# Canonical hashing — normalization and rejection rules.
# ---------------------------------------------------------------------------


def test_hash_distinguishes_bool_from_int_and_normalizes_none():
    assert canonical_content_hash({"a": True}) != canonical_content_hash({"a": 1})
    assert canonical_content_hash({"a": None}) == canonical_content_hash({"a": None})


def test_hash_normalizes_aware_datetimes_to_utc_and_rejects_naive():
    plus_two = timezone(timedelta(hours=2))
    assert canonical_content_hash(
        {"t": datetime(2026, 7, 16, 14, 0, tzinfo=plus_two)}
    ) == canonical_content_hash({"t": datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)})
    with pytest.raises(ValueError, match="aware"):
        canonical_content_hash({"t": datetime(2026, 7, 16, 12, 0)})


def test_hash_accepts_finite_decimal_and_rejects_infinite():
    assert canonical_content_hash({"n": Decimal("42")}) == canonical_content_hash(
        {"n": 42}
    )
    with pytest.raises(ValueError, match="finite"):
        canonical_content_hash({"n": Decimal("Infinity")})
    with pytest.raises(ValueError, match="finite"):
        canonical_content_hash({"n": float("inf")})


@pytest.mark.parametrize(
    "value",
    [
        {"raw": b"bytes"},
        {"path": Path("secret.txt")},
        {"items": {1, 2}},
        {1: "non-string-key"},
        {"obj": object()},
    ],
    ids=["bytes", "path", "set", "non-string-key", "unknown-object"],
)
def test_hash_rejects_non_canonical_values(value):
    with pytest.raises(TypeError):
        canonical_content_hash(value)


def test_hash_normalizes_dataclasses_like_mappings():
    claim = make_claim()
    body = {f.name: getattr(claim, f.name) for f in dataclasses.fields(claim)}
    assert canonical_content_hash(claim) == canonical_content_hash(body)


# ---------------------------------------------------------------------------
# Builders — deterministic IDs, validation, traceability.
# ---------------------------------------------------------------------------


def test_builder_ids_are_deterministic_hash_prefixed_values():
    claim = make_claim()
    again = make_claim()
    assert claim == again
    assert claim.claim_id == "clm_" + claim.content_hash.removeprefix("sha256:")
    assert len(claim.claim_id) == 4 + 64

    evidence = make_evidence()
    assert evidence.evidence_id == "evd_" + evidence.content_hash.removeprefix("sha256:")

    artifact = make_artifact()
    assert artifact.artifact_id == "art_" + artifact.content_hash.removeprefix("sha256:")

    receipt = make_receipt()
    assert receipt.receipt_id == "rct_" + receipt.content_hash.removeprefix("sha256:")


def test_claim_content_hash_excludes_id_and_changes_with_body():
    base = make_claim()
    other = make_claim(statement="different statement")
    assert base.content_hash != other.content_hash
    assert base.claim_id != other.claim_id


def test_claim_rejects_unknown_verdict_and_invalid_status():
    with pytest.raises(ValueError, match="verdict"):
        make_claim(verdict="probably")
    with pytest.raises(ValueError, match="status"):
        make_receipt(status="done")


def test_receipt_rejects_dangling_claim_references():
    with pytest.raises(ValueError, match="dangling"):
        make_receipt(
            claims=(make_claim(evidence_ids=("evd_" + "0" * 64,)),),
            evidence=(),
        )


def test_receipt_rejects_duplicate_ids_and_duplicate_content_hashes():
    evidence = make_evidence()
    claim = make_claim(evidence_ids=(evidence.evidence_id,))
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        make_receipt(claims=(claim, claim), evidence=(evidence,))
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        make_receipt(claims=(claim,), evidence=(evidence, evidence))


def test_receipt_rejects_unknown_source_and_subject_kinds():
    with pytest.raises(ValueError, match="source_kind"):
        make_receipt(source=ReceiptSourceKey("workflow", "w1"))
    with pytest.raises(ValueError, match="subject_kind"):
        make_receipt(subject_kind="workflow")


def test_observation_hash_binds_parent_receipt_lineage():
    kwargs: dict[str, object] = {
        "previous_observation_id": None,
        "status": "completed_unverified",
        "claims": (),
        "evidence": (),
        "artifacts": (),
        "uncertainty": (),
        "scorer_id": "hermes.code-end-state",
        "scorer_version": "1.0.0",
        "observed_at": UTC_TS,
    }
    first = build_observation(receipt_id="rct_" + "a" * 64, **kwargs)
    second = build_observation(receipt_id="rct_" + "b" * 64, **kwargs)
    assert first.observation_id != second.observation_id
    assert first.observation_id == "obs_" + first.content_hash.removeprefix("sha256:")
    assert first.status == "completed_unverified"


# ---------------------------------------------------------------------------
# Sealed VerifiedReceiptDecision — module-private capability only.
# ---------------------------------------------------------------------------


def test_seal_capability_builds_decision_and_wrong_capability_fails():
    kwargs: dict[str, object] = {
        "scorer_id": "hermes.code-end-state",
        "scorer_version": "1.0.0",
        "subject_kind": "turn",
        "subject_id": "s1:t1",
        "snapshot_hash": canonical_content_hash({"snapshot": 1}),
        "claim_hashes": (canonical_content_hash({"claim": 1}),),
        "decided_at": UTC_TS,
        "fresh_until": None,
    }
    decision = _build_verified_decision(_VERIFIED_DECISION_CAPABILITY, **kwargs)
    assert isinstance(decision, VerifiedReceiptDecision)
    assert decision.decision_hash.startswith("sha256:")
    assert decision.status == "verified"
    again = _build_verified_decision(_VERIFIED_DECISION_CAPABILITY, **kwargs)
    assert decision == again
    with pytest.raises(FrozenInstanceError):
        decision.scorer_id = "self"
    with pytest.raises(PermissionError):
        _build_verified_decision(object(), **kwargs)


# ---------------------------------------------------------------------------
# Private support types exist for later tasks and stay frozen.
# ---------------------------------------------------------------------------


def test_private_support_types_are_frozen_dataclasses():
    for cls in (ReceiptDecision, ReceiptEnvelope, EvidenceSnapshot, ReceiptSummary):
        assert dataclasses.is_dataclass(cls)
        assert cls.__dataclass_params__.frozen

    query = ReceiptQuery()
    assert query.limit > 0
    with pytest.raises(FrozenInstanceError):
        query.limit = 5


def test_nonverified_decision_rejects_verified_status():
    with pytest.raises(ValueError, match="verified"):
        ReceiptDecision(
            status="verified",
            scorer_id="hermes.code-end-state",
            scorer_version="1.0.0",
            subject_kind="turn",
            subject_id="s1:t1",
            snapshot_hash=canonical_content_hash({"snapshot": 1}),
            claim_hashes=(),
            uncertainty=(),
            decided_at=UTC_TS,
            fresh_until=None,
            decision_hash=canonical_content_hash({"d": 1}),
        )
