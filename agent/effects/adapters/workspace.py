"""Workspace and disposable-worktree Git adapters (plan Task 6).

These bridge the new ``agent.effects`` adapter SDK onto the proven
mission-era implementations in ``agent.effect_adapters`` — the path
authorization, V4A header rewriting, forced checkpoints, drift guards,
and bounded local-git semantics are shared, not reimplemented. What this
module owns is the SDK mapping: frozen requests in, frozen results out.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional

from agent.effect_adapters import (
    WorkspaceAuthority,
    WorkspaceCommitEffectAdapter,
    WorkspaceEffectAdapter,
)
from agent.effect_transactions import (
    EffectSemantics as LegacyEffectSemantics,
    OperationRequest as LegacyOperationRequest,
    PreparedEffect as LegacyPreparedEffect,
)
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

__all__ = ["WorkspaceAdapter", "WorkspaceGitAdapter"]


def _sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _wrap_lookup(raw_lookup: Optional[Callable]) -> Optional[Callable]:
    """Adapt new-SDK durable effect rows to the legacy evidence shape.

    Legacy adapters expect ``record.prepared['before']`` and a flat
    ``record.verification`` mapping carrying ``after_hashes`` /
    ``changed_paths`` / ``created_commit``. The new store persists
    ``VerificationResult`` as ``{verified, evidence, reason}`` — unwrap.
    """
    if raw_lookup is None:
        return None

    def _lookup(operation_id: str):
        effect = raw_lookup(operation_id)
        if effect is None:
            return None
        prepared = dict(effect.prepared or {})
        verification_raw = dict(effect.verification or {})
        evidence = verification_raw.get("evidence") or {}
        if not isinstance(evidence, Mapping):
            evidence = {}
        return SimpleNamespace(
            prepared={"before": prepared.get("before") or {}},
            verification={
                "after_hashes": dict(evidence.get("after_hashes") or {}),
                "changed_paths": list(evidence.get("changed_paths") or []),
                "created_commit": evidence.get("created_commit", ""),
            },
        )

    return _lookup


def _legacy_prepared(prepared: PreparedEffect, adapter_id: str) -> LegacyPreparedEffect:
    token = dict(prepared.prepared_token or {})
    return LegacyPreparedEffect(
        adapter_id=adapter_id,
        normalized_args=dict(prepared.args),
        before=dict(prepared.before),
        preview={},
        semantics=LegacyEffectSemantics(
            kind="reversible" if prepared.semantics.fidelity == "exact"
            else "compensatable",
            idempotent=False,
            reconcilable=True,
        ),
        compensation=dict(token.get("compensation") or {}),
    )


class WorkspaceAdapter(EffectAdapter):
    """``workspace.v1``: reversible ``write_file`` / ``patch`` effects."""

    descriptor = AdapterDescriptor(
        adapter_id="workspace.v1",
        actions=frozenset({"write_file", "patch"}),
        idempotency="none",
        reconciliation="query",
        compensation="exact",
        irreversible_after="never",
    )

    def __init__(
        self,
        *,
        workspace_root,
        transaction_lookup: Optional[Callable] = None,
        checkpoint_base: Optional[Path] = None,
    ):
        root = Path(workspace_root)
        self._authority = WorkspaceAuthority(
            mission_id="action-transaction",
            workspace_roots=(str(root),),
            workspace_root=root,
            actor_id="transaction-coordinator",
        )
        self._legacy = WorkspaceEffectAdapter(
            authority=self._authority,
            checkpoint_base=checkpoint_base,
            transaction_lookup=_wrap_lookup(transaction_lookup),
        )

    # ── SDK protocol ─────────────────────────────────────────────────

    def normalize(
        self, node: RevisionNode, context: EffectContext
    ) -> NormalizedEffect:
        if node.action not in self.descriptor.actions:
            raise EffectBlocked(
                f"unsupported action {node.action!r} for workspace adapter"
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

        request = LegacyOperationRequest(
            tool_name=effect.action,
            args=dict(effect.args),
            mission_id=None,
            operation_key=deterministic_operation_id(
                context.transaction_id, context.revision, context.node_id
            ),
        )
        try:
            legacy = self._legacy.prepare(request)
        except (PermissionError, ValueError) as exc:
            raise EffectBlocked(str(exc)) from exc

        targets = list(legacy.before.get("targets") or [])
        expected_after: Optional[dict] = None
        if effect.action == "write_file" and targets:
            expected_after = {
                targets[0]: hashlib.sha256(
                    str(effect.args.get("content", "")).encode("utf-8")
                ).hexdigest()
            }
        return PreparedEffect(
            node_id=effect.node_id,
            adapter_id=self.descriptor.adapter_id,
            action=effect.action,
            action_class="workspace.write",
            args=dict(legacy.normalized_args),
            resources=tuple(f"file:{target}" for target in targets),
            semantics=EffectSemantics(
                fidelity="exact", reconciliation="query", idempotency="none",
                irreversible_after="never",
            ),
            before=dict(legacy.before),
            expected_after=expected_after,
            prepared_token={
                "compensation": dict(legacy.compensation or {}),
                "unified_diff": legacy.preview.get("unified_diff", ""),
            },
            data_classes=("internal",),
        )

    def preview(
        self, prepared: PreparedEffect, context: EffectContext
    ) -> EffectPreview:
        states = list(prepared.before.get("targets_with_state") or [])
        first = states[0] if states else {}
        return EffectPreview(
            node_id=prepared.node_id,
            summary=prepared.prepared_token.get("unified_diff", ""),
            before={
                "sha256": first.get("sha256"),
                "existed": first.get("existed"),
                "targets": states,
            },
            after=dict(prepared.expected_after or {}),
            resources=prepared.resources,
            semantics=prepared.semantics,
            requires_approval=False,
        )

    def commit(
        self, request: CommitRequest, context: EffectContext
    ) -> CommitOutcome:
        if request.invoke is None:
            raise EffectBlocked(
                "workspace commit requires the terminal tool handler; "
                "run the commit through the tool boundary or supply an "
                "invoke callback"
            )
        raw = request.invoke(dict(request.prepared.args))
        targets = list(request.prepared.before.get("targets") or [])
        return CommitOutcome(
            status="committed",
            result={"result": raw},
            evidence={"targets": targets},
        )

    def verify(
        self, outcome: CommitOutcome, context: EffectContext
    ) -> VerificationResult:
        targets = list(outcome.evidence.get("targets") or [])
        after_hashes: dict[str, Optional[str]] = {}
        for target in targets:
            path = Path(target)
            after_hashes[target] = (
                _sha256_file(path) if path.exists() or path.is_symlink() else None
            )
        return VerificationResult(
            verified=bool(targets),
            evidence={
                "changed_paths": targets,
                "after_hashes": after_hashes,
            },
        )

    def reconcile(
        self, effect: EffectTransaction, context: EffectContext
    ) -> ReconciliationResult:
        verification = dict(effect.verification or {})
        evidence = verification.get("evidence") or {}
        after_hashes = dict(evidence.get("after_hashes") or {})
        if after_hashes:
            for target, expected in after_hashes.items():
                path = Path(target)
                current = (
                    _sha256_file(path)
                    if path.exists() or path.is_symlink() else None
                )
                if current != expected:
                    return ReconciliationResult(
                        disposition="unknown",
                        evidence={"drifted": target},
                    )
            return ReconciliationResult(
                disposition="landed", evidence={"after_hashes": after_hashes},
            )
        prepared = dict(effect.prepared or {})
        expected_after = prepared.get("expected_after") or {}
        before_states = {
            state.get("path"): state.get("sha256")
            for state in (prepared.get("before") or {}).get(
                "targets_with_state"
            ) or []
        }
        if expected_after:
            observed = {
                target: _sha256_file(Path(target))
                for target in expected_after
            }
            if observed == dict(expected_after):
                return ReconciliationResult(
                    disposition="landed", evidence={"after_hashes": observed},
                )
            if all(
                observed.get(target) == before_states.get(target)
                for target in expected_after
            ):
                return ReconciliationResult(
                    disposition="not_landed", evidence={"before_match": True},
                )
        return ReconciliationResult(disposition="unknown", evidence={})

    def compensate(
        self, request: CompensationRequest, context: EffectContext
    ) -> CompensationResult:
        legacy = _legacy_prepared(request.prepared, self.descriptor.adapter_id)
        try:
            result = self._legacy.compensate(legacy)
        except RuntimeError as exc:
            return CompensationResult(
                fidelity="exact", status="blocked", evidence={},
                error=str(exc),
            )
        return CompensationResult(
            fidelity="exact", status="compensated", evidence=dict(result),
        )


class WorkspaceGitAdapter(EffectAdapter):
    """``workspace-git.v1``: bounded local commit in a disposable worktree."""

    descriptor = AdapterDescriptor(
        adapter_id="workspace-git.v1",
        actions=frozenset({"commit_local"}),
        idempotency="none",
        reconciliation="query",
        compensation="exact",
        irreversible_after="never",
    )

    def __init__(
        self,
        *,
        transaction_lookup: Optional[Callable] = None,
        checkpoint_base: Optional[Path] = None,
    ):
        self._legacy = WorkspaceCommitEffectAdapter(
            checkpoint_base=checkpoint_base,
            transaction_lookup=_wrap_lookup(transaction_lookup),
        )

    def normalize(
        self, node: RevisionNode, context: EffectContext
    ) -> NormalizedEffect:
        if node.action not in self.descriptor.actions:
            raise EffectBlocked(
                f"unsupported action {node.action!r} for workspace-git adapter"
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

        request = LegacyOperationRequest(
            tool_name="commit_local",
            args=dict(effect.args),
            mission_id=None,
            operation_key=deterministic_operation_id(
                context.transaction_id, context.revision, context.node_id
            ),
        )
        try:
            legacy = self._legacy.prepare(request)
        except (PermissionError, ValueError) as exc:
            raise EffectBlocked(str(exc)) from exc
        worktree = legacy.normalized_args.get("worktree", "")
        return PreparedEffect(
            node_id=effect.node_id,
            adapter_id=self.descriptor.adapter_id,
            action=effect.action,
            action_class="workspace.commit_local",
            args=dict(legacy.normalized_args),
            resources=(f"git:{worktree}",),
            semantics=EffectSemantics(
                fidelity="exact", reconciliation="query", idempotency="none",
                irreversible_after="never",
            ),
            before=dict(legacy.before),
            prepared_token={"compensation": dict(legacy.compensation or {})},
            data_classes=("internal",),
        )

    def preview(
        self, prepared: PreparedEffect, context: EffectContext
    ) -> EffectPreview:
        paths = list(prepared.args.get("paths") or [])
        return EffectPreview(
            node_id=prepared.node_id,
            summary=(
                f"git commit {len(paths)} path(s) in "
                f"{prepared.args.get('worktree', '')}: "
                f"{prepared.args.get('message', '')}"
            ),
            before=dict(prepared.before),
            after={"paths": paths},
            resources=prepared.resources,
            semantics=prepared.semantics,
            requires_approval=False,
        )

    def commit(
        self, request: CommitRequest, context: EffectContext
    ) -> CommitOutcome:
        legacy = _legacy_prepared(request.prepared, self.descriptor.adapter_id)
        raw = self._legacy.commit(legacy, invoke=None)
        return CommitOutcome(
            status="committed" if raw.get("success") else "failed",
            result=dict(raw),
            evidence={
                "created_commit": raw.get("created_commit", ""),
                "parent_head": request.prepared.before.get("parent_head", ""),
                "worktree": request.prepared.before.get("worktree", ""),
            },
        )

    def verify(
        self, outcome: CommitOutcome, context: EffectContext
    ) -> VerificationResult:
        worktree = outcome.evidence.get("worktree", "")
        legacy = LegacyPreparedEffect(
            adapter_id=self.descriptor.adapter_id,
            normalized_args={"worktree": worktree},
            before={
                "parent_head": outcome.evidence.get("parent_head", ""),
                "worktree": worktree,
            },
            preview={},
            semantics=LegacyEffectSemantics(
                kind="compensatable", idempotent=False, reconcilable=True,
            ),
            compensation=None,
        )
        result = self._legacy.verify(legacy, dict(outcome.result))
        return VerificationResult(
            verified=bool(result.get("success")),
            evidence=dict(result),
        )

    def reconcile(
        self, effect: EffectTransaction, context: EffectContext
    ) -> ReconciliationResult:
        record = SimpleNamespace(operation_id=effect.operation_id)
        result = self._legacy.reconcile(record)
        disposition = result.get("disposition", "unknown")
        if disposition not in {"landed", "not_landed", "unknown"}:
            disposition = "unknown"
        evidence = {k: v for k, v in result.items() if k != "disposition"}
        return ReconciliationResult(disposition=disposition, evidence=evidence)

    def compensate(
        self, request: CompensationRequest, context: EffectContext
    ) -> CompensationResult:
        legacy = _legacy_prepared(request.prepared, self.descriptor.adapter_id)
        try:
            result = self._legacy.compensate(legacy)
        except RuntimeError as exc:
            return CompensationResult(
                fidelity="exact", status="blocked", evidence={},
                error=str(exc),
            )
        return CompensationResult(
            fidelity="exact", status="compensated", evidence=dict(result),
        )
