"""Shared real-path harness for coordinator/recovery tests (plan Task 5).

The fake adapter derives ALL state from the filesystem — commit log,
target files — so a "process restart" (new SessionDB/store/journal/
adapter/coordinator objects) observes only durable truth, exactly like a
real crash recovery would.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

from agent.effects.coordinator import TransactionCoordinator
from agent.effects.models import (
    CommitOutcome,
    EffectPreview,
    EffectSemantics,
    NormalizedEffect,
    PreparedEffect,
    ReconciliationResult,
    VerificationResult,
    content_hash,
)
from agent.effects.registry import (
    AdapterDescriptor,
    EffectAdapter,
    EffectAdapterRegistry,
)
from agent.effects.store import TransactionStore
from agent.operation_journal import OperationJournal
from hades_state import SessionDB

FAULT_POINTS = (
    "after_prepare", "after_preview", "after_commit_intent",
    "after_handler_return", "after_delivery_dispatch",
)


class SimulatedCrash(BaseException):
    """Raised by the fault hook; BaseException so nothing 'handles' it."""


class AllowAllProvider:
    def authorize(self, context, *, consume):
        return SimpleNamespace(
            allowed=True, verdict="allow", code="allow",
            context_hash="ctx", authority_version=1,
        )


class DenyAllProvider:
    def authorize(self, context, *, consume):
        return SimpleNamespace(
            allowed=False, verdict="deny", code="no_authorizing_rule",
            context_hash="ctx", authority_version=1,
        )


class FileBackedAdapter(EffectAdapter):
    """Reversible file-write adapter whose evidence is purely durable."""

    descriptor = AdapterDescriptor(
        adapter_id="faketest.v1",
        actions=frozenset({"write"}),
        idempotency="keyed",
        reconciliation="query",
        compensation="exact",
        irreversible_after="never",
    )

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._log = self.base_dir / "commits.log"

    @property
    def commit_calls(self) -> int:
        if not self._log.exists():
            return 0
        return len(self._log.read_text(encoding="utf-8").splitlines())

    def _target(self, args) -> Path:
        return self.base_dir / str(args["path"])

    def normalize(self, node, context):
        return NormalizedEffect(
            node_id=node.node_id, adapter_id=self.descriptor.adapter_id,
            action=node.action, args=dict(node.args),
            resource_keys=tuple(node.resource_keys),
        )

    def prepare(self, effect, context):
        target = self._target(effect.args)
        before = {
            "exists": target.exists(),
            "content": target.read_text(encoding="utf-8") if target.exists() else None,
        }
        descriptor = self.descriptor
        return PreparedEffect(
            node_id=effect.node_id, adapter_id=effect.adapter_id,
            action=effect.action, action_class="workspace.write",
            args=dict(effect.args),
            resources=(f"file:{effect.args['path']}",),
            # Semantics mirror the descriptor so subclasses can vary
            # fidelity/window without re-implementing prepare.
            semantics=EffectSemantics(
                fidelity=descriptor.compensation,
                reconciliation=descriptor.reconciliation,
                idempotency=descriptor.idempotency,
                irreversible_after=descriptor.irreversible_after,
                compensation_window_seconds=descriptor.compensation_window_seconds,
            ),
            before=before,
            expected_after={"content_hash": content_hash(effect.args["content"])},
            data_classes=("internal",),
        )

    def preview(self, prepared, context):
        return EffectPreview(
            node_id=prepared.node_id,
            summary=f"write {prepared.args['path']}",
            before=dict(prepared.before),
            after={"content_hash": prepared.expected_after["content_hash"]},
            resources=prepared.resources,
            semantics=prepared.semantics,
            requires_approval=False,
        )

    def commit(self, request, context):
        args = request.prepared.args
        target = self._target(args)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args["content"], encoding="utf-8")
        with self._log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "operation_id": request.operation_id, "path": args["path"],
            }) + "\n")
        return CommitOutcome(
            status="committed",
            result={"path": args["path"], "bytes": len(args["content"])},
            evidence={"content_hash": content_hash(args["content"])},
        )

    def verify(self, outcome, context):
        return VerificationResult(verified=True, evidence=dict(outcome.evidence))

    def reconcile(self, effect, context):
        prepared = effect.prepared or {}
        args = prepared.get("args") or {}
        if "path" not in args:
            return ReconciliationResult(disposition="unknown", evidence={})
        target = self._target(args)
        if not target.exists():
            return ReconciliationResult(
                disposition="not_landed", evidence={"exists": False},
            )
        observed = content_hash(target.read_text(encoding="utf-8"))
        expected = (prepared.get("expected_after") or {}).get("content_hash")
        if observed == expected:
            return ReconciliationResult(
                disposition="landed", evidence={"content_hash": observed},
            )
        return ReconciliationResult(
            disposition="unknown", evidence={"content_hash": observed},
        )

    def compensate(self, request, context):
        args = request.prepared.args
        target = self._target(args)
        before = request.prepared.before
        if before.get("exists"):
            target.write_text(before["content"], encoding="utf-8")
        elif target.exists():
            target.unlink()
        from agent.effects.models import CompensationResult

        return CompensationResult(
            fidelity="exact", status="compensated",
            evidence={"restored": True},
        )


class AmnesiacAdapter(FileBackedAdapter):
    """Same effect, but reconciliation can only answer 'unknown'."""

    descriptor = dataclasses.replace(
        FileBackedAdapter.descriptor, adapter_id="amnesiac.v1",
    )

    def reconcile(self, effect, context):
        return ReconciliationResult(disposition="unknown", evidence={})


class TxHarness:
    """Builds a real store/journal/coordinator over one durable state.db."""

    def __init__(self, tmp_path: Path, *, adapter_cls=FileBackedAdapter,
                 provider=None):
        self.tmp_path = Path(tmp_path)
        self.workspace = self.tmp_path / "workspace"
        self.adapter_cls = adapter_cls
        self.provider = provider or AllowAllProvider()
        self.trace: list[str] = []
        self.fault_point: str | None = None
        self.db: SessionDB | None = None
        self._build()

    def _build(self):
        self.db = SessionDB(self.tmp_path / "state.db")
        self.store = TransactionStore(self.db)
        self.journal = OperationJournal(self.db)
        self.adapter = self.adapter_cls(self.workspace)
        self.adapters = EffectAdapterRegistry()
        self.adapters.register(self.adapter)
        self.coordinator = TransactionCoordinator(
            store=self.store,
            adapters=self.adapters,
            journal=self.journal,
            authority_provider_factory=lambda: self.provider,
            fault_hook=self._fault_hook,
            trace=self.trace.append,
        )

    def _fault_hook(self, point: str, context) -> None:
        if point == self.fault_point:
            raise SimulatedCrash(point)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def restart(self):
        """Simulate process death: all in-memory objects are rebuilt."""
        self.close()
        self.trace = []
        self.fault_point = None
        self._build()
        # First owner-fence pass, exactly like the production startup seams.
        self.journal.reconcile_after_restart(owner_fenced=True)

    # ── Transaction helpers ─────────────────────────────────────────

    def graph(self, node_ids=("workspace_write",), *, edges=()):
        adapter_id = self.adapter.descriptor.adapter_id
        return {
            "nodes": [
                {
                    "node_id": node_id,
                    "adapter_id": adapter_id,
                    "action": "write",
                    "args": {
                        "path": f"{node_id}.txt",
                        "content": f"content of {node_id}\n",
                    },
                    "resource_keys": [f"file:{node_id}.txt"],
                }
                for node_id in node_ids
            ],
            "edges": [
                {"parent": parent, "child": child} for parent, child in edges
            ],
        }

    def create(self, transaction_id="tx-1", node_ids=("workspace_write",),
               edges=()):
        return self.store.create_transaction(
            transaction_id=transaction_id, profile="default",
            title="harness transaction",
            authority={"authority_version": 1, "irreversible_policy": "ask"},
            graph=self.graph(node_ids, edges=edges),
            failure_policy="stop",
        )

    def preview(self, transaction_id="tx-1"):
        return self.coordinator.preview(transaction_id)

    def commit(self, transaction_id="tx-1", **kwargs):
        return self.coordinator.commit(transaction_id, **kwargs)

    def crash_at(self, fault_point: str, transaction_id="tx-1"):
        """Preview + commit with a crash injected at *fault_point*."""
        self.fault_point = (
            fault_point if fault_point in ("after_prepare", "after_preview")
            else None
        )
        try:
            self.preview(transaction_id)
        except SimulatedCrash:
            return
        self.fault_point = fault_point
        try:
            self.commit(transaction_id)
        except SimulatedCrash:
            return
        raise AssertionError(f"fault {fault_point} did not fire")
