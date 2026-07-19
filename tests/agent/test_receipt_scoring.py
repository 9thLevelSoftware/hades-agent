"""Tests for independent end-state scoring and status precedence.

Task 5 of the Verified Outcome & Artifact Receipts plan. Proven here:

- The fixed non-verified terminal precedence: ambiguous effect landing
  dominates everything (``unknown_effect``), then known failure
  (``failed``), then prevention (``blocked``), then completed work with
  missing/stale/inconclusive verification (``completed_unverified``).
- Only an independent, domain-appropriate scorer can mint the sealed
  ``VerifiedReceiptDecision``. A self-authored scorer raises
  ``ScorerIndependenceError``; an explicitly requested wrong-domain
  scorer raises ``InappropriateScorerError``.
- Expired ``fresh_until``, a missing required claim, empty evidence
  refs, an artifact mismatch, an ambiguous grader, an unknown operation,
  and a forged attestation can never yield ``verified``.
- A sealed decision cannot be constructed publicly, and a pickled/
  rebuilt seal reused for different content fails store validation.
"""

from __future__ import annotations

import pickle

import pytest

from agent.receipt_artifacts import ArtifactCatalog
from agent.receipt_hashing import canonical_content_hash
from agent.receipt_ingest import ReceiptIngestor, build_evidence_snapshot
from agent.receipt_models import (
    EvidenceSnapshot,
    ReceiptDecision,
    VerifiedReceiptDecision,
    build_claim,
    build_evidence_digest,
    build_operation_evidence,
    build_receipt,
    build_requested_outcome,
)
from agent.receipt_scoring import (
    CodeTurnEndStateScorer,
    InappropriateScorerError,
    MissionEndStateScorer,
    ReceiptScoringError,
    ReceiptScoringService,
    ScorerEvaluation,
    ScorerIndependenceError,
    ScorerRegistry,
    ScorerRegistryError,
    TransactionEndStateScorer,
    build_default_scoring_service,
)
from agent.receipt_store import ReceiptStore
from agent.receipts import ReceiptSourceKey
from hades_state import SessionDB

OBSERVED_AT = "2026-07-16T10:00:00Z"
NOW = "2026-07-16T12:00:00Z"


class FakePassingScorer:
    """Independent always-passing scorer used to probe the seal path."""

    def __init__(
        self,
        *,
        scorer_id: str = "test.independent-end-state",
        supported_outcomes: tuple[str, ...] = ("code_change",),
        passed: bool = True,
        ambiguous: bool = False,
        fresh_until: str | None = None,
    ) -> None:
        self.scorer_id = scorer_id
        self.scorer_version = "1.0"
        self.supported_outcome_kinds = frozenset(supported_outcomes)
        self._evaluation = ScorerEvaluation(
            passed=passed,
            ambiguous=ambiguous,
            fresh_until=fresh_until,
        )

    def evaluate(self, snapshot: EvidenceSnapshot) -> ScorerEvaluation:
        return self._evaluation


def _make_snapshot(
    *,
    completed: bool = True,
    unknown_effect: bool = False,
    known_failure: bool = False,
    blocked: bool = False,
    verification: str = "missing",
    evidence_fresh_until: str | None = None,
    required: bool = True,
    attestation: bool = False,
    outcome_kind: str = "code_change",
    producer_id: str = "hermes.turn-ledger",
    constraints: tuple[str, ...] = (),
) -> EvidenceSnapshot:
    evidence = []
    turn_evidence = build_evidence_digest(
        evidence_kind="turn_classification",
        source_ref="state.db:turn_outcomes:s1:t1",
        producer_id=producer_id,
        observed_at=OBSERVED_AT,
        summary="turn ledger outcome (untrusted source claim)",
        payload_hash=canonical_content_hash({"outcome": "completed_unverified"}),
    )
    evidence.append(turn_evidence)
    end_state_evidence = build_evidence_digest(
        evidence_kind=(
            "verification_check"
            if verification == "passed"
            else "absence_observed"
        ),
        source_ref="verification_evidence.db:s1:workspace",
        producer_id=producer_id,
        observed_at=OBSERVED_AT,
        fresh_until=evidence_fresh_until,
        summary=(
            "verification passed for workspace"
            if verification == "passed"
            else "no evidence observed for verification:s1:t1"
        ),
        payload_hash=canonical_content_hash({"verification": verification}),
    )
    evidence.append(end_state_evidence)
    if attestation:
        evidence.append(
            build_evidence_digest(
                evidence_kind="provenance_attestation",
                source_ref="state.db:receipt_attestations:att-1",
                producer_id="external.signer",
                observed_at=OBSERVED_AT,
                summary="signature claims the outcome is verified",
                payload_hash=canonical_content_hash({"signed": "verified"}),
            )
        )
    if unknown_effect:
        operations = (
            build_operation_evidence(
                operation_id="op-1",
                operation_kind="send_message",
                state="unknown",
                effect_disposition="unknown",
                source_ref="state.db:agent_operations:op-1",
                observed_at=OBSERVED_AT,
            ),
        )
    else:
        operations = (
            build_operation_evidence(
                operation_id="op-1",
                operation_kind="write_file",
                state="confirmed",
                effect_disposition="landed",
                source_ref="state.db:agent_operations:op-1",
                observed_at=OBSERVED_AT,
            ),
        )
    claims = (
        build_claim(
            claim_kind="turn-completed",
            statement="the turn reached a terminal ledger outcome",
            evidence_ids=(turn_evidence.evidence_id,),
            required=required,
            verdict="satisfied" if completed else "unknown",
        ),
        build_claim(
            claim_kind="requested-end-state",
            statement="the requested end state independently holds",
            evidence_ids=(end_state_evidence.evidence_id,),
            required=required,
            verdict="unknown",
        ),
    )
    return build_evidence_snapshot(
        source=ReceiptSourceKey("turn", "s1:t1"),
        subject_kind="turn",
        subject_id="s1:t1",
        producer_id=producer_id,
        requested_outcome=build_requested_outcome(
            outcome_kind=outcome_kind,
            description="requested end state for turn s1:t1",
            constraints=constraints,
            producer_id=producer_id,
        ),
        claims=claims,
        evidence=tuple(evidence),
        operation_states=operations,
        blocked_reasons=(
            ("approval is required before the side effect may land",)
            if blocked
            else ()
        ),
        known_failures=(
            ("the claimed edit does not exist on disk (artifact hash mismatch)",)
            if known_failure
            else ()
        ),
        uncertainty=(
            ("no verification evidence exists for this turn",)
            if verification == "missing"
            else ()
        ),
        captured_at=OBSERVED_AT,
    )


@pytest.fixture()
def snapshot_factory():
    return _make_snapshot


@pytest.fixture()
def scoring():
    return build_default_scoring_service(now=lambda: NOW)


@pytest.fixture()
def registry():
    return ReceiptScoringService(ScorerRegistry(), now=lambda: NOW)


@pytest.fixture()
def code_snapshot(snapshot_factory):
    return snapshot_factory(completed=True)


# ---------------------------------------------------------------------------
# Fixed non-verified terminal precedence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("facts", "status"), [
    ({"unknown_effect": True, "known_failure": True}, "unknown_effect"),
    ({"known_failure": True, "blocked": True}, "failed"),
    ({"blocked": True}, "blocked"),
    ({"completed": True, "verification": "missing"}, "completed_unverified"),
])
def test_nonverified_precedence(scoring, snapshot_factory, facts, status):
    assert scoring.decide(snapshot_factory(**facts)).status == status


def test_precedence_dominates_a_passing_scorer(registry, snapshot_factory):
    registry.register(FakePassingScorer())
    ambiguous = snapshot_factory(unknown_effect=True)
    assert registry.decide(ambiguous).status == "unknown_effect"
    failed = snapshot_factory(known_failure=True)
    assert registry.decide(failed).status == "failed"
    blocked = snapshot_factory(blocked=True)
    assert registry.decide(blocked).status == "blocked"


def test_precedence_decisions_are_ordinary_not_sealed(scoring, snapshot_factory):
    decision = scoring.decide(snapshot_factory(known_failure=True))
    assert isinstance(decision, ReceiptDecision)
    assert not isinstance(decision, VerifiedReceiptDecision)


# ---------------------------------------------------------------------------
# Independence and appropriateness.
# ---------------------------------------------------------------------------


def test_self_scorer_and_wrong_domain_cannot_verify(registry, code_snapshot):
    registry.register(FakePassingScorer(
        scorer_id=code_snapshot.producer_id, supported_outcomes=("code_change",)
    ))
    with pytest.raises(ScorerIndependenceError):
        registry.decide(code_snapshot)
    with pytest.raises(InappropriateScorerError):
        registry.decide(code_snapshot, scorer_id="hermes.delivery-end-state")


def test_no_appropriate_scorer_yields_completed_unverified(registry, code_snapshot):
    decision = registry.decide(code_snapshot)
    assert decision.status == "completed_unverified"
    assert any("scorer" in item for item in decision.uncertainty)


def test_explicit_wrong_domain_scorer_is_inappropriate(registry, code_snapshot):
    registry.register(FakePassingScorer(
        scorer_id="hermes.delivery-end-state",
        supported_outcomes=("delivery_confirmation",),
    ))
    with pytest.raises(InappropriateScorerError):
        registry.decide(code_snapshot, scorer_id="hermes.delivery-end-state")


def test_registry_rejects_empty_domain_duplicate_and_mutating_scorers():
    registry = ScorerRegistry()
    with pytest.raises(ScorerRegistryError, match="supported"):
        registry.register(FakePassingScorer(supported_outcomes=()))
    registry.register(FakePassingScorer(scorer_id="a", supported_outcomes=("x",)))
    with pytest.raises(ScorerRegistryError, match="duplicate"):
        registry.register(
            FakePassingScorer(scorer_id="a", supported_outcomes=("y",))
        )

    class MutatingScorer:
        scorer_id = "test.mutating"
        scorer_version = "1.0"
        supported_outcome_kinds = frozenset({"code_change"})

        def evaluate(self, snapshot):
            return ScorerEvaluation(passed=True)

        def delete_workspace(self):  # pragma: no cover - never called
            raise AssertionError("mutation")

    with pytest.raises(ScorerRegistryError, match="mutat"):
        registry.register(MutatingScorer())


def test_scorer_must_return_a_scorer_evaluation(registry, code_snapshot):
    class LeakyScorer:
        scorer_id = "test.leaky"
        scorer_version = "1.0"
        supported_outcome_kinds = frozenset({"code_change"})

        def evaluate(self, snapshot):
            return {"passed": True}

    registry.register(LeakyScorer())
    with pytest.raises(ReceiptScoringError, match="ScorerEvaluation"):
        registry.decide(code_snapshot)


# ---------------------------------------------------------------------------
# The sealed verified path and everything that must refuse it.
# ---------------------------------------------------------------------------


def test_fresh_independent_passing_scorer_can_verify(registry, snapshot_factory):
    registry.register(FakePassingScorer())
    snapshot = snapshot_factory(completed=True, verification="passed")
    decision = registry.decide(snapshot)
    assert isinstance(decision, VerifiedReceiptDecision)
    assert decision.status == "verified"
    assert decision.subject_kind == "turn"
    assert decision.subject_id == "s1:t1"
    assert decision.snapshot_hash == snapshot.content_hash
    assert decision.claim_hashes == tuple(
        c.content_hash for c in snapshot.claims
    )
    assert decision.decided_at == NOW


def test_sealed_decision_round_trips_through_the_store(
    registry, snapshot_factory, tmp_path
):
    registry.register(FakePassingScorer())
    snapshot = snapshot_factory(completed=True, verification="passed")
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = ReceiptStore(db)
        ingestor = ReceiptIngestor(store, decide=registry.decide)
        receipt = ingestor.issue(snapshot)
        assert receipt.status == "verified"
        assert store.get(receipt.receipt_id) == receipt
    finally:
        db.close()


def test_expired_evidence_fresh_until_cannot_verify(registry, snapshot_factory):
    registry.register(FakePassingScorer())
    stale = snapshot_factory(
        completed=True,
        verification="passed",
        evidence_fresh_until="2026-07-16T11:00:00Z",  # before NOW
    )
    decision = registry.decide(stale)
    assert decision.status == "completed_unverified"
    assert any("fresh" in item or "expire" in item for item in decision.uncertainty)


def test_expired_scorer_fresh_until_cannot_verify(registry, snapshot_factory):
    registry.register(
        FakePassingScorer(fresh_until="2026-07-16T11:00:00Z")  # before NOW
    )
    decision = registry.decide(
        snapshot_factory(completed=True, verification="passed")
    )
    assert decision.status == "completed_unverified"


def test_missing_required_claim_cannot_verify(registry, snapshot_factory):
    registry.register(FakePassingScorer())
    decision = registry.decide(
        snapshot_factory(completed=True, verification="passed", required=False)
    )
    assert decision.status == "completed_unverified"


def test_empty_evidence_refs_cannot_verify(registry):
    registry.register(FakePassingScorer())
    dangling_claim = build_claim(
        claim_kind="effect",
        statement="something changed with no evidence at all",
        evidence_ids=(),
        required=True,
        verdict="satisfied",
    )
    forged = EvidenceSnapshot(
        source=ReceiptSourceKey("turn", "s1:t1"),
        subject_kind="turn",
        subject_id="s1:t1",
        producer_id="hermes.turn-ledger",
        requested_outcome=build_requested_outcome(
            outcome_kind="code_change",
            description="requested end state for turn s1:t1",
            producer_id="hermes.turn-ledger",
        ),
        claims=(dangling_claim,),
        evidence=(),
        artifacts=(),
        operation_states=(),
        blocked_reasons=(),
        known_failures=(),
        uncertainty=(),
        captured_at=OBSERVED_AT,
        content_hash=canonical_content_hash({"forged": "snapshot"}),
    )
    decision = registry.decide(forged)
    assert decision.status == "completed_unverified"


def test_artifact_mismatch_is_failed_not_verified(registry, snapshot_factory):
    registry.register(FakePassingScorer())
    decision = registry.decide(snapshot_factory(known_failure=True))
    assert decision.status == "failed"


def test_ambiguous_grader_cannot_verify(registry, snapshot_factory):
    registry.register(FakePassingScorer(ambiguous=True))
    decision = registry.decide(
        snapshot_factory(completed=True, verification="passed")
    )
    assert decision.status == "completed_unverified"


def test_unknown_operation_cannot_verify(registry, snapshot_factory):
    registry.register(FakePassingScorer())
    decision = registry.decide(
        snapshot_factory(completed=True, verification="passed", unknown_effect=True)
    )
    assert decision.status == "unknown_effect"


def test_forged_attestation_cannot_verify(registry, snapshot_factory):
    # A signature proves provenance over bytes, never truth: with no
    # independent scorer the attested snapshot stays completed_unverified.
    decision = registry.decide(
        snapshot_factory(completed=True, verification="passed", attestation=True)
    )
    assert decision.status == "completed_unverified"


def test_ledger_verified_label_never_verifies_by_itself(scoring, snapshot_factory):
    # The default service trusts no turn outcome label; without fresh
    # independent verification the built-in code scorer refuses.
    decision = scoring.decide(
        snapshot_factory(completed=True, verification="missing")
    )
    assert decision.status == "completed_unverified"


# ---------------------------------------------------------------------------
# Seal forgery must fail store validation.
# ---------------------------------------------------------------------------


def _verified_shaped_receipt(*, decided_at: str):
    outcome = build_requested_outcome(
        outcome_kind="code_change",
        description="requested end state for turn s1:t1",
        producer_id="hermes.turn-ledger",
    )
    return build_receipt(
        source=ReceiptSourceKey("turn", "s1:t1"),
        subject_kind="turn",
        subject_id="s1:t1",
        session_id="s1",
        turn_id="t1",
        requested_outcome=outcome,
        status="verified",
        scorer_id="test.independent-end-state",
        scorer_version="1.0",
        decided_at=decided_at,
    )


def test_verified_decision_has_no_public_constructor():
    with pytest.raises(TypeError):
        VerifiedReceiptDecision(scorer_id="self")


def test_pickled_seal_reused_for_different_content_fails_store_validation(
    registry, snapshot_factory, tmp_path
):
    registry.register(FakePassingScorer())
    snapshot = snapshot_factory(completed=True, verification="passed")
    decision = registry.decide(snapshot)
    assert isinstance(decision, VerifiedReceiptDecision)
    clone = pickle.loads(pickle.dumps(decision))
    # The rebuilt seal is bound to the original snapshot's exact claim
    # hashes; a receipt with different claims must be rejected.
    receipt = _verified_shaped_receipt(decided_at=clone.decided_at)
    assert tuple(c.content_hash for c in receipt.claims) != clone.claim_hashes
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = ReceiptStore(db)
        with pytest.raises(PermissionError, match="scorer decision"):
            store.insert(receipt, decision=clone)
    finally:
        db.close()


def test_hand_built_decision_object_fails_store_validation(tmp_path):
    receipt = _verified_shaped_receipt(decided_at=NOW)
    forged = object.__new__(VerifiedReceiptDecision)
    for name, value in {
        "scorer_id": receipt.scorer_id,
        "scorer_version": receipt.scorer_version,
        "subject_kind": receipt.subject_kind,
        "subject_id": receipt.subject_id,
        "snapshot_hash": "sha256:" + "a" * 64,
        "claim_hashes": (),
        "decided_at": NOW,
        "fresh_until": None,
        "decision_hash": "sha256:" + "0" * 64,
    }.items():
        object.__setattr__(forged, name, value)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = ReceiptStore(db)
        with pytest.raises(PermissionError, match="scorer decision"):
            store.insert(receipt, decision=forged)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Built-in scorers stay narrow and re-derive truth themselves.
# ---------------------------------------------------------------------------


def test_code_turn_scorer_rechecks_cited_artifacts(tmp_path, snapshot_factory):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        catalog = ArtifactCatalog(db)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "output.txt"
        target.write_text("genuine bytes")
        digest = catalog.register_path(
            target,
            source_kind="execute_code",
            source_ref="s1:t1:artifact",
            allowed_roots=(workspace,),
        )
        target.write_text("tampered bytes after registration")
        scorer = CodeTurnEndStateScorer(
            catalog=catalog,
            allowed_roots=(workspace,),
            verification_loader=lambda session_id: (
                {"status": "passed", "root": str(workspace)},
            ),
        )
        base = snapshot_factory(completed=True, verification="passed")
        snapshot = build_evidence_snapshot(
            source=base.source,
            subject_kind=base.subject_kind,
            subject_id=base.subject_id,
            producer_id=base.producer_id,
            requested_outcome=base.requested_outcome,
            claims=base.claims,
            evidence=base.evidence,
            artifacts=(digest,),
            operation_states=base.operation_states,
            captured_at=OBSERVED_AT,
        )
        evaluation = scorer.evaluate(snapshot)
        assert not evaluation.passed
        assert evaluation.failures
        service = ReceiptScoringService(ScorerRegistry(), now=lambda: NOW)
        service.register(scorer)
        assert service.decide(snapshot).status == "failed"
    finally:
        db.close()


def test_code_turn_scorer_supports_only_code_change():
    scorer = CodeTurnEndStateScorer()
    assert scorer.supported_outcome_kinds == frozenset({"code_change"})


def test_mission_scorer_refuses_unknown_check_and_passes_declared_check(
    snapshot_factory,
):
    service = ReceiptScoringService(ScorerRegistry(), now=lambda: NOW)
    service.register(
        MissionEndStateScorer(checks={"page_live": lambda snapshot: True})
    )
    verified = service.decide(snapshot_factory(
        completed=True,
        verification="passed",
        outcome_kind="mission_outcome",
        producer_id="hermes.missions",
        constraints=("check:page_live",),
    ))
    assert isinstance(verified, VerifiedReceiptDecision)
    unknown = service.decide(snapshot_factory(
        completed=True,
        verification="passed",
        outcome_kind="mission_outcome",
        producer_id="hermes.missions",
        constraints=("check:not-a-registered-check",),
    ))
    assert unknown.status == "completed_unverified"


def test_transaction_scorer_requires_lineage_and_postconditions(snapshot_factory):
    service = ReceiptScoringService(ScorerRegistry(), now=lambda: NOW)
    service.register(TransactionEndStateScorer())
    missing = service.decide(snapshot_factory(
        completed=True,
        verification="passed",
        outcome_kind="transaction_commit",
        producer_id="hermes.effect-transactions",
    ))
    assert missing.status == "completed_unverified"

    lineage = build_evidence_digest(
        evidence_kind="transaction_lineage",
        source_ref="state.db:effect_transactions:tx1:lineage",
        producer_id="hermes.effect-transactions",
        observed_at=OBSERVED_AT,
        summary="transaction tx1 lineage hashes",
        payload_hash=canonical_content_hash({"revision": 1}),
    )
    postcondition = build_evidence_digest(
        evidence_kind="adapter_postcondition",
        source_ref="state.db:effect_transactions:tx1:verification_json",
        producer_id="hermes.effect-transactions",
        observed_at=OBSERVED_AT,
        summary="adapter postcondition evidence for transaction tx1",
        payload_hash=canonical_content_hash({"postcondition": "held"}),
    )
    committed = build_claim(
        claim_kind="transaction-committed",
        statement="effect transaction tx1 committed its declared effect",
        evidence_ids=(lineage.evidence_id,),
        required=True,
        verdict="satisfied",
    )
    end_state = build_claim(
        claim_kind="requested-end-state",
        statement="the requested transaction end state independently holds",
        evidence_ids=(lineage.evidence_id, postcondition.evidence_id),
        required=True,
        verdict="unknown",
    )
    operation = build_operation_evidence(
        operation_id="op-tx1",
        operation_kind="effect_commit",
        state="confirmed",
        effect_disposition="landed",
        source_ref="state.db:agent_operations:op-tx1",
        observed_at=OBSERVED_AT,
    )
    snapshot = build_evidence_snapshot(
        source=ReceiptSourceKey("transaction", "tx1"),
        subject_kind="transaction",
        subject_id="tx1",
        producer_id="hermes.effect-transactions",
        requested_outcome=build_requested_outcome(
            outcome_kind="transaction_commit",
            description="commit effect transaction tx1",
            producer_id="hermes.effect-transactions",
        ),
        claims=(committed, end_state),
        evidence=(lineage, postcondition),
        operation_states=(operation,),
        captured_at=OBSERVED_AT,
    )
    verified = service.decide(snapshot)
    assert isinstance(verified, VerifiedReceiptDecision)
