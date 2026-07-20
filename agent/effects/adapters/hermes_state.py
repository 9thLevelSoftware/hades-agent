"""Versioned workflow, cron, and config adapters (plan Task 7).

Bridges the ``agent.effects`` SDK onto the mission-era Hermes-state
adapters, whose owner-module services already implement revision-hashed
prepare/apply/verify/restore under the stores' own locks and atomic
writers. Compensation is semantic: workflow compensation selects the
prior immutable version/enabled state, cron compensation restores the
exact prior normalized job (or disables a created one), and config
compensation restores the single user leaf while a whole-document
revision mismatch blocks instead of overwriting concurrent edits.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional

from agent.effect_adapters import (
    HermesConfigStateAdapter,
    HermesCronStateAdapter,
    HermesWorkflowStateAdapter,
)
from agent.effect_transactions import OperationRequest as LegacyOperationRequest
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
)
from agent.effects.registry import AdapterDescriptor, EffectAdapter

__all__ = [
    "HermesConfigAdapter",
    "HermesCronAdapter",
    "HermesWorkflowAdapter",
]


def _state_semantics() -> EffectSemantics:
    return EffectSemantics(
        fidelity="semantic", reconciliation="query", idempotency="none",
        irreversible_after="never",
    )


def _compensation_record(prepared: PreparedEffect) -> SimpleNamespace:
    token = dict(prepared.prepared_token or {})
    return SimpleNamespace(compensation=dict(token.get("compensation") or {}))


class _StateBridge(EffectAdapter):
    """Shared SDK mapping over one legacy Hermes-state adapter."""

    action_class_prefix: str = "state"

    def _legacy(self):  # pragma: no cover — overridden per family
        raise NotImplementedError

    def normalize(
        self, node: RevisionNode, context: EffectContext
    ) -> NormalizedEffect:
        if node.action not in self.descriptor.actions:
            raise EffectBlocked(
                f"unsupported action {node.action!r} for "
                f"{self.descriptor.adapter_id}"
            )
        return NormalizedEffect(
            node_id=node.node_id,
            adapter_id=self.descriptor.adapter_id,
            action=node.action,
            args=dict(node.args),
            resource_keys=tuple(node.resource_keys),
        )

    def prepare(
        self, effect: NormalizedEffect, context: EffectContext
    ) -> PreparedEffect:
        from agent.effects.coordinator import deterministic_operation_id

        args = {"action": effect.action, **dict(effect.args)}
        request = LegacyOperationRequest(
            tool_name=self.descriptor.adapter_id,
            args=args,
            mission_id=None,
            operation_key=deterministic_operation_id(
                context.transaction_id, context.revision, context.node_id
            ),
        )
        try:
            legacy = self._legacy().prepare(request)
        except (PermissionError, ValueError, KeyError) as exc:
            raise EffectBlocked(str(exc)) from exc
        payload = dict(legacy.compensation or {})
        preview = dict(legacy.preview or {})
        return PreparedEffect(
            node_id=effect.node_id,
            adapter_id=self.descriptor.adapter_id,
            action=effect.action,
            action_class=f"{self.action_class_prefix}.{effect.action}",
            args=dict(effect.args),
            resources=(str(preview.get("resource") or payload.get("resource") or ""),),
            semantics=_state_semantics(),
            before={"state": copy.deepcopy(preview.get("before"))},
            expected_after={"state": copy.deepcopy(preview.get("after"))},
            prepared_token={"compensation": payload, "preview": preview},
            data_classes=("internal",),
        )

    def preview(
        self, prepared: PreparedEffect, context: EffectContext
    ) -> EffectPreview:
        token = dict(prepared.prepared_token or {})
        preview = dict(token.get("preview") or {})
        return EffectPreview(
            node_id=prepared.node_id,
            summary=(
                f"{preview.get('resource', '')}: {preview.get('action', '')} "
                f"(expected revision {preview.get('expected_revision')})"
            ),
            before=(
                {"exists": False}
                if preview.get("before") is None
                else {"exists": True, **dict(preview["before"])}
            ),
            after=dict(preview.get("after") or {}),
            resources=prepared.resources,
            semantics=prepared.semantics,
            requires_approval=False,
        )

    def commit(
        self, request: CommitRequest, context: EffectContext
    ) -> CommitOutcome:
        record = _compensation_record(request.prepared)
        raw = self._legacy().commit(record, lambda _args: None)
        return CommitOutcome(
            status="committed",
            result=dict(raw),
            evidence={"compensation": dict(record.compensation)},
        )

    def verify(
        self, outcome: CommitOutcome, context: EffectContext
    ) -> VerificationResult:
        record = SimpleNamespace(
            compensation=dict(outcome.evidence.get("compensation") or {}),
        )
        result = self._legacy().verify(record, dict(outcome.result))
        return VerificationResult(
            verified=bool(result.get("landed")),
            evidence=dict(result),
        )

    def reconcile(
        self, effect: EffectTransaction, context: EffectContext
    ) -> ReconciliationResult:
        prepared = dict(effect.prepared or {})
        token = dict(prepared.get("prepared_token") or {})
        compensation = dict(token.get("compensation") or {})
        if not compensation:
            return ReconciliationResult(disposition="unknown", evidence={})
        record = SimpleNamespace(compensation=compensation)
        try:
            result = self._legacy().reconcile(record)
        except Exception:
            return ReconciliationResult(disposition="unknown", evidence={})
        disposition = result.get("disposition", "unknown")
        if disposition not in {"landed", "not_landed", "unknown"}:
            disposition = "unknown"
        evidence = {k: v for k, v in result.items() if k != "disposition"}
        return ReconciliationResult(disposition=disposition, evidence=evidence)

    def compensate(
        self, request: CompensationRequest, context: EffectContext
    ) -> CompensationResult:
        record = _compensation_record(request.prepared)
        try:
            result = self._legacy().compensate(record)
        except Exception as exc:
            return CompensationResult(
                fidelity="semantic", status="blocked", evidence={},
                error=f"{type(exc).__name__}: {exc}",
            )
        return CompensationResult(
            fidelity="semantic", status="compensated",
            evidence=dict(result or {}),
        )


class HermesWorkflowAdapter(_StateBridge):
    """``hermes-workflow.v1``: immutable workflow version state."""

    descriptor = AdapterDescriptor(
        adapter_id="hermes-workflow.v1",
        actions=frozenset({"deploy", "enable", "disable"}),
        idempotency="none",
        reconciliation="query",
        compensation="semantic",
        irreversible_after="never",
    )
    action_class_prefix = "workflow"

    def __init__(self, *, conn_factory: Callable[[], Any]):
        self._conn_factory = conn_factory

    def _legacy(self) -> HermesWorkflowStateAdapter:
        return HermesWorkflowStateAdapter(self._conn_factory())


class HermesCronAdapter(_StateBridge):
    """``hermes-cron.v1``: create/update/disable — never hard-delete."""

    descriptor = AdapterDescriptor(
        adapter_id="hermes-cron.v1",
        actions=frozenset({"create", "update", "disable"}),
        idempotency="none",
        reconciliation="query",
        compensation="semantic",
        irreversible_after="never",
    )
    action_class_prefix = "cron"

    def __init__(self):
        self._adapter = HermesCronStateAdapter()

    def _legacy(self) -> HermesCronStateAdapter:
        return self._adapter


class HermesConfigAdapter(_StateBridge):
    """``hermes-config.v1``: one-key config mutation, secrets rejected."""

    descriptor = AdapterDescriptor(
        adapter_id="hermes-config.v1",
        actions=frozenset({"set"}),
        idempotency="none",
        reconciliation="query",
        compensation="semantic",
        irreversible_after="never",
    )
    action_class_prefix = "config"

    def __init__(self):
        self._adapter = HermesConfigStateAdapter()

    def _legacy(self) -> HermesConfigStateAdapter:
        return self._adapter
