"""Delayed message outbox adapter (plan Task 8).

``message-outbox.v1/send`` commits an ENQUEUE, never a network send: the
node's effect is one durable, delayed, revisable outbox row. Until
release+dispatch the row can be revised or cancelled (semantic
compensation); after dispatch the message is truthfully irreversible
unless the concrete platform adapter proves edit/delete support.
Dispatch itself is owned by the leased outbox dispatcher over
``DeliveryRouter`` and the delivery operation journal — never by this
adapter.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from agent.effects.models import (
    CommitOutcome,
    CommitRequest,
    CompensationRequest,
    CompensationResult,
    EffectBlocked,
    EffectContext,
    EffectPreview,
    EffectSemantics,
    EffectTransaction,
    NormalizedEffect,
    PreparedEffect,
    ReconciliationResult,
    RevisionNode,
    VerificationResult,
    content_hash,
)
from agent.effects.registry import AdapterDescriptor, EffectAdapter

__all__ = ["MessageOutboxAdapter"]

# Outbox rows created before release wait for explicit approval; these
# are the only statuses a revision or cancellation may touch.
_REVISABLE_STATUSES = frozenset({"pending_approval", "scheduled"})


class MessageOutboxAdapter(EffectAdapter):
    descriptor = AdapterDescriptor(
        adapter_id="message-outbox.v1",
        actions=frozenset({"send"}),
        idempotency="keyed",
        reconciliation="query",
        compensation="semantic",
        irreversible_after="dispatch",
    )

    def __init__(self, *, db_factory: Callable[[], object]):
        # A factory (not a handle) so fresh-process recovery constructs
        # its own SessionDB and the adapter never caches a closed handle.
        self._db_factory = db_factory

    def _store(self):
        from gateway.mission_outbox import MissionOutboxStore

        return MissionOutboxStore(self._db_factory())

    @staticmethod
    def _identity(context: EffectContext) -> tuple[str, str]:
        return (f"tx:{context.transaction_id}", context.node_id)

    def normalize(
        self, node: RevisionNode, context: EffectContext
    ) -> NormalizedEffect:
        if node.action not in self.descriptor.actions:
            raise EffectBlocked(
                f"unsupported action {node.action!r} for message outbox"
            )
        args = dict(node.args)
        platform = str(args.get("platform") or "")
        target = str(args.get("target") or "")
        message = args.get("message")
        if not platform or not target:
            raise EffectBlocked("message send requires platform and target")
        if not isinstance(message, str) or not message.strip():
            raise EffectBlocked("message send requires a non-blank message")
        delay = args.get("not_before_seconds", 30)
        if not isinstance(delay, int) or isinstance(delay, bool) or delay < 1:
            raise EffectBlocked("not_before_seconds must be an integer >= 1")
        return NormalizedEffect(
            node_id=node.node_id,
            adapter_id=self.descriptor.adapter_id,
            action=node.action,
            args={
                "platform": platform,
                "target": target,
                "message": message,
                "not_before_seconds": delay,
            },
            resource_keys=(f"message:{platform}:{target}",),
        )

    def prepare(
        self, effect: NormalizedEffect, context: EffectContext
    ) -> PreparedEffect:
        from hades_state import SessionDB

        execution_id, node_id = self._identity(context)
        outbox_id, delivery_id = SessionDB.derive_outbox_ids(
            execution_id, node_id
        )
        payload = {"message": effect.args["message"]}
        return PreparedEffect(
            node_id=effect.node_id,
            adapter_id=self.descriptor.adapter_id,
            action=effect.action,
            action_class="message.send",
            args=dict(effect.args),
            resources=tuple(effect.resource_keys),
            semantics=EffectSemantics(
                fidelity="semantic", reconciliation="query",
                idempotency="keyed", irreversible_after="dispatch",
            ),
            before={"outbox_id": outbox_id, "delivery_id": delivery_id},
            expected_after={"content_hash": content_hash(payload)},
            prepared_token={
                "execution_id": execution_id,
                "outbox_id": outbox_id,
                "delivery_id": delivery_id,
                "content_hash": content_hash(payload),
            },
            recipient_classes=("configured_target",),
            data_classes=("internal",),
        )

    def preview(
        self, prepared: PreparedEffect, context: EffectContext
    ) -> EffectPreview:
        args = prepared.args
        return EffectPreview(
            node_id=prepared.node_id,
            summary=(
                f"enqueue delayed message to {args['platform']}:"
                f"{args['target']} after {args['not_before_seconds']}s "
                "(release requires exact approval; irreversible after "
                "dispatch)"
            ),
            before=dict(prepared.before),
            after=dict(prepared.expected_after or {}),
            resources=prepared.resources,
            semantics=prepared.semantics,
            requires_approval=False,
        )

    def commit(
        self, request: CommitRequest, context: EffectContext
    ) -> CommitOutcome:
        args = request.prepared.args
        token = dict(request.prepared.prepared_token)
        store = self._store()
        # Ordinary (non-mission) rows derive their ids from identity and
        # never carry a caller operation id — enqueue certainty lives on
        # the transaction effect row; dispatch certainty on the delivery
        # journal keyed by the derived delivery_id.
        record = store.materialize(
            execution_id=token["execution_id"],
            node_id=context.node_id,
            platform=args["platform"],
            target=args["target"],
            content={"message": args["message"]},
            requires_approval=True,
            not_before=int(time.time()) + int(args["not_before_seconds"]),
        )
        return CommitOutcome(
            status="committed",
            result={
                "outbox_id": record.outbox_id,
                "delivery_id": record.delivery_id,
                "status": record.status,
                "revision": record.revision,
                "not_before": record.not_before,
            },
            evidence={
                "outbox_id": record.outbox_id,
                "content_hash": record.content_hash,
            },
        )

    def verify(
        self, outcome: CommitOutcome, context: EffectContext
    ) -> VerificationResult:
        outbox_id = outcome.evidence.get("outbox_id", "")
        record = self._store().get_by_id(outbox_id)
        if record is None:
            return VerificationResult(
                verified=False, evidence={}, reason="outbox row missing",
            )
        return VerificationResult(
            verified=record.status in _REVISABLE_STATUSES,
            evidence={
                "outbox_id": record.outbox_id,
                "status": record.status,
                "content_hash": record.content_hash,
                "revision": record.revision,
            },
        )

    def reconcile(
        self, effect: EffectTransaction, context: EffectContext
    ) -> ReconciliationResult:
        token = dict((effect.prepared or {}).get("prepared_token") or {})
        outbox_id = token.get("outbox_id", "")
        if not outbox_id:
            return ReconciliationResult(disposition="unknown", evidence={})
        record = self._store().get_by_id(outbox_id)
        if record is None:
            return ReconciliationResult(
                disposition="not_landed", evidence={"outbox_id": outbox_id},
            )
        expected_hash = token.get("content_hash")
        if expected_hash and record.content_hash != expected_hash:
            return ReconciliationResult(
                disposition="unknown",
                evidence={"outbox_id": outbox_id, "status": record.status},
            )
        return ReconciliationResult(
            disposition="landed",
            evidence={"outbox_id": outbox_id, "status": record.status},
        )

    def compensate(
        self, request: CompensationRequest, context: EffectContext
    ) -> CompensationResult:
        token = dict(request.prepared.prepared_token)
        outbox_id = token.get("outbox_id", "")
        store = self._store()
        record = store.get_by_id(outbox_id)
        if record is None:
            return CompensationResult(
                fidelity="semantic", status="blocked", evidence={},
                error="outbox row missing; cannot certify cancellation",
            )
        if record.status == "cancelled":
            return CompensationResult(
                fidelity="semantic", status="compensated",
                evidence={"outbox_id": outbox_id, "already": True},
            )
        if record.status not in _REVISABLE_STATUSES:
            # Dispatched (claimed/delivered/failed/unknown): the message
            # is past the irreversible boundary. Platform edit/delete
            # compensation would need a concrete adapter proof — never
            # claimed here.
            return CompensationResult(
                fidelity="semantic", status="blocked",
                evidence={"outbox_id": outbox_id, "status": record.status},
                error=(
                    f"message is {record.status}; irreversible after "
                    "dispatch without proven edit/delete support"
                ),
            )
        cancelled = store.cancel(outbox_id, expected_revision=record.revision)
        if not cancelled:
            return CompensationResult(
                fidelity="semantic", status="blocked",
                evidence={"outbox_id": outbox_id},
                error="cancellation lost a concurrent race; re-evaluate",
            )
        return CompensationResult(
            fidelity="semantic", status="compensated",
            evidence={"outbox_id": outbox_id, "cancelled": True},
        )
