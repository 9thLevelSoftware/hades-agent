"""Transaction evidence snapshots over the shared receipt contract.

Item #12 (``agent.receipts``) owns the receipt schema, the five-status
vocabulary, canonical hashing, immutable insertion, and the sealed
scorer path. This module adds ONLY transaction claim construction: it
projects the durable transaction aggregate — graph/preview hashes,
authority decisions, effect phases, operation states, verification,
reconciliation, compensation — into one immutable evidence snapshot and
lets the shared scoring service decide. Model output, handler success,
workflow success, or a journal row alone are never sufficient for
``verified``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from agent.receipt_ingest import (
    ReceiptIngestor,
    build_evidence_snapshot,
    build_observation,
)
from agent.receipt_models import (
    build_claim,
    build_evidence_digest,
    build_operation_evidence,
    build_requested_outcome,
)
from agent.receipt_scoring import ReceiptScoringService, ScorerEvaluation
from agent.receipts import ReceiptSourceKey, canonical_content_hash
from agent.effects.models import EffectContext, EffectTransaction

__all__ = ["ActionTransactionScorer", "TransactionReceiptBuilder"]

_PRODUCER_ID = "hermes.action-transactions"
_OUTCOME_KIND = "action_transaction"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ActionTransactionScorer:
    """The only verified path for transaction receipts.

    Passes only when every required claim is ``satisfied`` — anything
    committed without sufficient proof stays ``completed_unverified``.
    """

    scorer_id = "hermes.action-transaction-scorer"
    scorer_version = "1"
    supported_outcome_kinds = frozenset({_OUTCOME_KIND})

    def evaluate(self, snapshot) -> ScorerEvaluation:
        reasons: list[str] = []
        for claim in snapshot.claims:
            if not claim.required:
                continue
            if claim.verdict != "satisfied":
                reasons.append(
                    f"required claim {claim.claim_kind!r} is "
                    f"{claim.verdict}: {claim.statement}"
                )
        if reasons:
            return ScorerEvaluation(passed=False, reasons=tuple(reasons))
        return ScorerEvaluation(passed=True)


class TransactionReceiptBuilder:
    """Issue immutable transaction receipts; append recheck observations."""

    def __init__(
        self,
        store,
        *,
        receipt_store,
        adapters=None,
        journal=None,
        scoring: Optional[ReceiptScoringService] = None,
        now=None,
    ):
        self._store = store
        self._receipt_store = receipt_store
        self._adapters = adapters
        self._journal = journal
        self._now = now or _now_iso
        self._scoring = scoring or ReceiptScoringService()
        try:
            self._scoring.register(ActionTransactionScorer())
        except Exception:
            # An equivalent scorer is already registered on a shared
            # service instance — fine, the path exists.
            pass
        self._ingestor = ReceiptIngestor(
            receipt_store, decide=self._scoring.decide,
        )

    # ── Snapshot construction ────────────────────────────────────────

    def _live_disposition(self, effect: EffectTransaction) -> Optional[str]:
        """Observation-only adapter inspection; never commits/compensates."""
        if self._adapters is None:
            return None
        try:
            adapter = self._adapters.get(effect.adapter_id)
            result = adapter.reconcile(
                effect,
                EffectContext(
                    transaction_id=effect.transaction_id,
                    revision=effect.revision,
                    node_id=effect.node_id,
                ),
            )
            return result.disposition
        except Exception:
            return "unknown"

    def snapshot(self, transaction_id: str, *, done_text: Optional[str] = None):
        store = self._store
        transaction = store.get_transaction(transaction_id)
        if transaction is None:
            raise KeyError(f"unknown transaction {transaction_id!r}")
        revision = store.get_revision(
            transaction_id, transaction.current_revision
        )
        effects = {
            effect.node_id: effect
            for effect in store.list_effects(transaction_id)
            if effect.revision == revision.revision
        }
        compensations = {
            attempt.effect_id: attempt
            for attempt in store.list_compensations(transaction_id)
        }
        # Evidence timestamps derive from DURABLE facts so an identical
        # aggregate re-issues an identical snapshot content hash — the
        # volatile capture time lives only in captured_at (excluded from
        # the hash).
        observed_at = datetime.fromtimestamp(
            transaction.updated_at_ms / 1000, timezone.utc
        ).isoformat()

        evidence = [
            build_evidence_digest(
                evidence_kind="transaction_state",
                source_ref=f"state.db:action_transactions:{transaction_id}",
                producer_id=_PRODUCER_ID,
                observed_at=observed_at,
                summary=(
                    f"transaction {transaction_id} status "
                    f"{transaction.status} revision {revision.revision}"
                ),
                payload_hash=canonical_content_hash({
                    "status": transaction.status,
                    "revision": revision.revision,
                    "graph_hash": revision.graph_hash,
                    "preview_hash": revision.preview_hash,
                    "authority_version": transaction.authority_version,
                }),
            )
        ]
        claims = []
        operation_states = []
        blocked_reasons: list[str] = []
        known_failures: list[str] = []
        uncertainty: list[str] = []

        for event in store.load_snapshot(transaction_id).events:
            if event.kind in {"authority_blocked", "approval_blocked"}:
                blocked_reasons.append(
                    f"{event.kind}: {dict(event.payload)}"
                )
        if transaction.status == "blocked" and not blocked_reasons:
            blocked_reasons.append(
                f"transaction {transaction_id} is blocked"
            )

        for node in revision.nodes:
            effect = effects.get(node.node_id)
            phase = effect.phase if effect is not None else "planned"
            payload: Mapping[str, Any] = {
                "node_id": node.node_id,
                "adapter_id": node.adapter_id,
                "action": node.action,
                "phase": phase,
                "preview_hash": (
                    effect.preview_hash if effect is not None else None
                ),
                "semantics": dict(effect.semantics or {}) if effect else {},
                "verification": (
                    dict(effect.verification or {}) if effect else {}
                ),
                "reconciliation": (
                    dict(effect.reconciliation or {}) if effect else {}
                ),
            }
            digest = build_evidence_digest(
                evidence_kind="effect_state",
                source_ref=(
                    f"state.db:transaction_effects:"
                    f"{effect.effect_id if effect else node.node_id}"
                ),
                producer_id=_PRODUCER_ID,
                observed_at=observed_at,
                summary=f"node {node.node_id} phase {phase}",
                payload_hash=canonical_content_hash(dict(payload)),
            )
            evidence.append(digest)

            compensation = (
                compensations.get(effect.effect_id) if effect else None
            )
            terminal_mode = None
            if phase == "compensated":
                terminal_mode = "compensated"
            verdict = "unknown"
            statement = f"node {node.node_id} reached a proven terminal state"
            if phase == "verified":
                verdict = "satisfied"
            elif phase == "compensated" and compensation is not None and (
                compensation.status == "compensated"
            ):
                # Live drift is expected after restoration; the proof is
                # the journaled compensation itself.
                verdict = "satisfied"
            elif phase == "failed":
                verdict = "unsatisfied"
                known_failures.append(
                    f"node {node.node_id} effect failed"
                )
            elif phase == "unknown_effect":
                uncertainty.append(
                    f"node {node.node_id} effect landing is ambiguous"
                )
            # Live inspection for still-committed work: the durable
            # verified-after state must still hold right now.
            if phase in {"committed", "verified"}:
                disposition = self._live_disposition(effect)
                if disposition is not None and disposition != "landed":
                    uncertainty.append(
                        f"node {node.node_id} no longer matches its "
                        f"verified-after state (reconcile: {disposition})"
                    )
                    verdict = "unknown"
            claims.append(build_claim(
                claim_kind=f"effect:{node.node_id}",
                statement=statement,
                expected_json='{"phase": ["verified", "compensated"]}',
                observed_json=(
                    '{"phase": "%s"%s}' % (
                        phase,
                        ', "terminal_mode": "compensated"'
                        if terminal_mode else "",
                    )
                ),
                evidence_ids=(digest.evidence_id,),
                artifact_ids=(),
                required=True,
                verdict=verdict,
                uncertainty=(),
            ))

            if effect is not None and self._journal is not None:
                record = self._journal.get(effect.operation_id)
                if record is not None:
                    operation_states.append(build_operation_evidence(
                        operation_id=record.operation_id,
                        operation_kind=record.kind,
                        state=record.state,
                        effect_disposition=record.effect_disposition,
                        source_ref=(
                            f"state.db:agent_operations:{record.operation_id}"
                        ),
                        observed_at=observed_at,
                    ))
            if compensation is not None and self._journal is not None:
                comp_record = self._journal.get(compensation.operation_id)
                if comp_record is not None:
                    operation_states.append(build_operation_evidence(
                        operation_id=comp_record.operation_id,
                        operation_kind=comp_record.kind,
                        state=comp_record.state,
                        effect_disposition=comp_record.effect_disposition,
                        source_ref=(
                            f"state.db:agent_operations:"
                            f"{comp_record.operation_id}"
                        ),
                        observed_at=observed_at,
                    ))

        if done_text:
            # Model/user-facing "done" text is recorded but proves
            # nothing: an optional, never-satisfied claim.
            text_digest = build_evidence_digest(
                evidence_kind="reported_text",
                source_ref=f"transaction:{transaction_id}:report",
                producer_id=_PRODUCER_ID,
                observed_at=observed_at,
                summary="unverified completion text",
                payload_hash=canonical_content_hash({"text": done_text}),
            )
            evidence.append(text_digest)
            claims.append(build_claim(
                claim_kind="reported_done_text",
                statement="a completion report exists; text is not proof",
                expected_json='{"proof": "independent"}',
                observed_json='{"proof": "text_only"}',
                evidence_ids=(text_digest.evidence_id,),
                artifact_ids=(),
                required=False,
                verdict="unknown",
                uncertainty=("completion text is not evidence",),
            ))

        requested = build_requested_outcome(
            outcome_kind=_OUTCOME_KIND,
            description=(
                f"complete action transaction {transaction_id} "
                f"({transaction.title}) under current authority"
            ),
            constraints=(
                f"graph_hash:{revision.graph_hash}",
                f"authority_version:{transaction.authority_version}",
            ),
            producer_id=_PRODUCER_ID,
        )
        return build_evidence_snapshot(
            source=ReceiptSourceKey("transaction", transaction_id),
            subject_kind="transaction",
            subject_id=transaction_id,
            producer_id=_PRODUCER_ID,
            requested_outcome=requested,
            claims=tuple(claims),
            evidence=tuple(evidence),
            operation_states=tuple(operation_states),
            blocked_reasons=tuple(blocked_reasons),
            known_failures=tuple(known_failures),
            uncertainty=tuple(uncertainty),
            captured_at=observed_at,
        )

    # ── Issue and recheck ────────────────────────────────────────────

    def issue(self, transaction_id: str, *, done_text: Optional[str] = None):
        snapshot = self.snapshot(transaction_id, done_text=done_text)
        receipt = self._ingestor.issue(snapshot)
        self._project_receipt_id(transaction_id, receipt.receipt_id)
        return receipt

    def _project_receipt_id(self, transaction_id: str, receipt_id: str) -> None:
        try:
            self._store.set_receipt_id(transaction_id, receipt_id)
        except Exception:
            # Projection is repaired on the next issue()/startup link via
            # the source key; the immutable receipt itself is the truth.
            pass

    def recheck(self, receipt_id: str):
        receipt = self._receipt_store.get(receipt_id)
        if receipt is None:
            raise KeyError(f"unknown receipt {receipt_id!r}")
        transaction_id = receipt.transaction_id or receipt.subject_id
        snapshot = self.snapshot(transaction_id)
        decision = self._scoring.decide(snapshot)
        observations = self._receipt_store.observations(receipt_id)
        previous = observations[-1] if observations else None
        observation = build_observation(receipt, previous, snapshot, decision)
        from agent.receipt_models import VerifiedReceiptDecision

        seal = (
            decision if isinstance(decision, VerifiedReceiptDecision) else None
        )
        return self._receipt_store.append_observation(
            observation, decision=seal,
        )
