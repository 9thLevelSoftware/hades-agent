"""Transaction coordinator: preview, commit, revise, reconcile.

The coordinator owns ordering and certainty; adapters own effects. The
non-negotiable commit ordering per node is:

    journal.create(pending)
    journal.transition(pending -> running/none)
    effect CAS previewed -> committing
    adapter.commit exactly once
    persist raw outcome
    adapter.verify and persist evidence
    journal transition running|dispatched -> confirmed/(landed|none)
    effect CAS committing -> verified|committed
    append event

Success is never reported before durable result/evidence writes. If the
handler may have run but durable confirmation failed, adapter
reconciliation runs once; anything unclassifiable becomes
``unknown_effect`` and freezes the frontier — no blind retries, ever.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from agent.effects.authority import build_action_context
from agent.effects.context import TransactionExecutionContext
from agent.effects.graph import create_revision as graph_create_revision
from agent.effects.graph import topological_order
from agent.effects.models import (
    CommitRequest,
    CompensationRequest,
    EffectBlocked,
    EffectContext,
    EffectSemantics,
    PreparedEffect,
    content_hash,
)
from agent.autonomy import authorize_effect

__all__ = [
    "CommitResult",
    "CompensationOutcome",
    "PreviewResult",
    "ReconcileResult",
    "TransactionCoordinator",
]


def _noop_fault_hook(point: str, context: Mapping[str, Any]) -> None:
    return None


def _noop_trace(event: str) -> None:
    return None


@dataclass(frozen=True)
class PreviewResult:
    status: str  # ready | blocked
    preview_hash: Optional[str] = None
    nodes: tuple = ()
    error: Optional[str] = None


@dataclass(frozen=True)
class CommitResult:
    status: str  # committed | ready | blocked | failed | unknown_effect
    committed_nodes: tuple[str, ...] = ()
    blocked_node: Optional[str] = None
    error: Optional[str] = None
    # Nodes compensated by the compensate_prefix failure policy after a
    # failed node stopped the commit; empty under the default policy.
    compensated_prefix: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompensationOutcome:
    status: str  # compensated | partially_compensated
    compensated_nodes: tuple[str, ...] = ()
    error: Optional[str] = None


@dataclass(frozen=True)
class ReconcileResult:
    status: str
    counts: Mapping[str, int] = dataclasses.field(
        default_factory=lambda: {"landed": 0, "not_landed": 0, "unknown": 0,
                                 "skipped": 0}
    )


def deterministic_operation_id(
    transaction_id: str, revision: int, node_id: str
) -> str:
    digest = hashlib.sha256(
        f"effect\0{transaction_id}\0{revision}\0{node_id}".encode("utf-8")
    ).hexdigest()
    return f"txop-{digest[:40]}"


def _semantics_from_json(raw: Mapping[str, Any]) -> EffectSemantics:
    return EffectSemantics(
        fidelity=raw.get("fidelity", "none"),
        reconciliation=raw.get("reconciliation", "none"),
        idempotency=raw.get("idempotency", "none"),
        irreversible_after=raw.get("irreversible_after", "commit"),
        compensation_window_seconds=raw.get("compensation_window_seconds"),
    )


def prepared_from_json(raw: Mapping[str, Any]) -> PreparedEffect:
    """Rebuild the frozen PreparedEffect from its persisted JSON form."""
    return PreparedEffect(
        node_id=raw["node_id"],
        adapter_id=raw["adapter_id"],
        action=raw["action"],
        action_class=raw.get("action_class", "unknown.mutation"),
        args=raw.get("args") or {},
        resources=tuple(raw.get("resources") or ()),
        semantics=_semantics_from_json(raw.get("semantics") or {}),
        before=raw.get("before") or {},
        expected_after=raw.get("expected_after"),
        prepared_token=raw.get("prepared_token") or {},
        recipient_classes=tuple(raw.get("recipient_classes") or ()),
        recipient_hashes=tuple(raw.get("recipient_hashes") or ()),
        data_classes=tuple(raw.get("data_classes") or ()),
        cost_usd_micros=raw.get("cost_usd_micros") or 0,
        uncertainty_ppm=raw.get("uncertainty_ppm") or 0,
        required_evidence=tuple(raw.get("required_evidence") or ()),
    )


class TransactionCoordinator:
    """Generic orchestration around the store, journal, and adapter SDK."""

    def __init__(
        self,
        *,
        store,
        adapters,
        journal,
        authority_provider_factory: Callable[[], Any],
        approval_consumer: Optional[Callable[..., Any]] = None,
        receipt_builder: Optional[Any] = None,
        fault_hook: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
        trace: Optional[Callable[[str], None]] = None,
        profile: str = "default",
        workspace_root: Optional[str] = None,
    ):
        self._store = store
        self._adapters = adapters
        self._journal = journal
        self._authority_provider_factory = authority_provider_factory
        self._approval_consumer = approval_consumer
        self._receipt_builder = receipt_builder
        self._fault = fault_hook or _noop_fault_hook
        self._trace = trace or _noop_trace
        self._profile = profile
        self._workspace_root = workspace_root

    # ── Shared helpers ───────────────────────────────────────────────

    @property
    def store(self):
        return self._store

    def _effect_context(
        self, transaction_id: str, revision: int, node_id: str
    ) -> EffectContext:
        return EffectContext(
            transaction_id=transaction_id,
            revision=revision,
            node_id=node_id,
            profile=self._profile,
            workspace_root=self._workspace_root,
        )

    def _block_transaction(
        self, transaction_id: str, from_statuses: set[str], kind: str,
        payload: Mapping[str, Any],
    ) -> None:
        self._store.transition_status(transaction_id, from_statuses, "blocked")
        self._store.append_event(transaction_id, kind, payload=dict(payload))

    # ── Preview ──────────────────────────────────────────────────────

    def preview(self, transaction_id: str) -> PreviewResult:
        transaction = self._store.get_transaction(transaction_id)
        if transaction is None:
            raise KeyError(f"unknown transaction {transaction_id!r}")
        if not self._store.transition_status(
            transaction_id, {"draft", "blocked"}, "previewing"
        ):
            raise EffectBlocked(
                f"transaction {transaction_id!r} is {transaction.status}; "
                "preview requires draft or blocked"
            )
        revision_number = transaction.current_revision
        revision = self._store.get_revision(transaction_id, revision_number)
        node_map = {node.node_id: node for node in revision.nodes}
        order = topological_order((revision.nodes, revision.edges))
        previews: list[tuple[str, dict]] = []
        latest_effects = self._store.latest_effects_by_node(transaction_id)
        try:
            for node_id in order:
                node = node_map[node_id]
                # Cross-revision truth: a node frozen at an EARLIER
                # revision (committed/verified/…) is never re-prepared —
                # its stored preview is carried forward as-is. Without
                # this, a post-partial-commit revision would mint a fresh
                # attempt and commit would execute the effect twice.
                existing = latest_effects.get(node_id)
                if existing is not None and existing.phase in {
                    "committed", "verified", "compensating", "compensated",
                    "committing", "unknown_effect",
                }:
                    previews.append((node_id, dict(existing.preview or {})))
                    continue
                try:
                    adapter = self._adapters.get(node.adapter_id)
                except KeyError as exc:
                    raise EffectBlocked(str(exc)) from exc
                context = self._effect_context(
                    transaction_id, revision_number, node_id
                )
                normalized = adapter.normalize(node, context)
                prepared = adapter.prepare(normalized, context)
                self._fault("after_prepare", {"node_id": node_id})
                preview = adapter.preview(prepared, context)
                operation_id = deterministic_operation_id(
                    transaction_id, revision_number, node_id
                )
                self._journal.create(
                    operation_id=operation_id,
                    kind="effect_commit",
                    destination=node.adapter_id,
                    payload_hash=content_hash(node.args),
                )
                effect = self._store.effect_for(
                    transaction_id, revision_number, node_id
                )
                if effect is None:
                    effect = self._store.create_effect_attempt(
                        effect_id=f"ef-{transaction_id}-{revision_number}-{node_id}",
                        transaction_id=transaction_id,
                        revision=revision_number,
                        node_id=node_id,
                        operation_id=operation_id,
                        adapter_id=node.adapter_id,
                    )
                prepared_json = dataclasses.asdict(prepared)
                preview_json = dataclasses.asdict(preview)
                self._store.transition_effect(
                    effect.effect_id,
                    {"planned", "prepared", "previewed", "failed", "blocked"},
                    "prepared",
                    updates={
                        "semantics_json": dataclasses.asdict(prepared.semantics),
                        "prepared_json": prepared_json,
                    },
                )
                self._fault("after_preview", {"node_id": node_id})
                previews.append((node_id, preview_json))
        except EffectBlocked as exc:
            self._block_transaction(
                transaction_id, {"previewing"}, "preview_failed",
                {"error": str(exc)},
            )
            return PreviewResult(status="blocked", error=str(exc))
        except Exception as exc:  # adapter bug or validation failure
            self._block_transaction(
                transaction_id, {"previewing"}, "preview_failed",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            return PreviewResult(status="blocked", error=str(exc))

        preview_hash = content_hash(
            [[node_id, payload] for node_id, payload in previews]
        )
        for node_id, payload in previews:
            effect = self._store.effect_for(
                transaction_id, revision_number, node_id
            )
            if effect is None:
                # Frozen node carried forward from an earlier revision —
                # no attempt exists at THIS revision, and none may.
                continue
            if effect.phase == "prepared":
                self._store.transition_effect(
                    effect.effect_id, {"prepared"}, "previewed",
                    updates={
                        "preview_json": payload,
                        "preview_hash": preview_hash,
                    },
                )
        self._store.set_revision_preview_hash(
            transaction_id, revision_number, preview_hash
        )
        self._store.transition_status(transaction_id, {"previewing"}, "ready")
        self._store.append_event(
            transaction_id, "revision_previewed",
            payload={"revision": revision_number, "preview_hash": preview_hash},
            idempotency_key=f"revision_previewed:{revision_number}:{preview_hash}",
        )
        return PreviewResult(
            status="ready", preview_hash=preview_hash,
            nodes=tuple(node_id for node_id, _ in previews),
        )

    # ── Commit ───────────────────────────────────────────────────────

    def commit(
        self,
        transaction_id: str,
        *,
        through_node: Optional[str] = None,
        requester: str = "system",
        channel: str = "cli",
        invoke_map: Optional[Mapping[str, Callable]] = None,
    ) -> CommitResult:
        transaction = self._store.get_transaction(transaction_id)
        if transaction is None:
            raise KeyError(f"unknown transaction {transaction_id!r}")
        if not self._store.transition_status(
            transaction_id, {"ready"}, "committing"
        ):
            raise EffectBlocked(
                f"transaction {transaction_id!r} is {transaction.status}; "
                "commit requires ready"
            )
        revision_number = transaction.current_revision
        revision = self._store.get_revision(transaction_id, revision_number)
        if not revision.preview_hash:
            self._block_transaction(
                transaction_id, {"committing"}, "commit_blocked",
                {"reason": "missing preview"},
            )
            return CommitResult(status="blocked", error="missing preview")
        order = topological_order((revision.nodes, revision.edges))
        parents: dict[str, set[str]] = {node.node_id: set() for node in revision.nodes}
        for edge in revision.edges:
            parents[edge.child_node_id].add(edge.parent_node_id)

        committed: list[str] = []
        for node_id in order:
            outcome = self._commit_node(
                transaction_id, revision_number, node_id,
                parents=parents[node_id],
                expected_preview_hash=revision.preview_hash,
                requester=requester, channel=channel,
                invoke=(invoke_map or {}).get(node_id),
            )
            if outcome == "skipped":
                continue
            if outcome != "committed":
                compensated_prefix: tuple[str, ...] = ()
                if (
                    outcome == "failed"
                    and transaction.failure_policy == "compensate_prefix"
                ):
                    # The accepted policy: after a known failure, the
                    # eligible committed prefix compensates in reverse
                    # order under its own separately authorized
                    # eligibility checks. Never across unknown effects —
                    # only a clean `failed` triggers it.
                    compensated_prefix = self._compensate_prefix(
                        transaction_id, committed,
                    )
                return CommitResult(
                    status=outcome, committed_nodes=tuple(committed),
                    blocked_node=node_id,
                    compensated_prefix=compensated_prefix,
                )
            committed.append(node_id)
            if through_node is not None and node_id == through_node:
                break

        effects = self._store.latest_effects_by_node(transaction_id)
        all_done = all(
            effects.get(node.node_id) is not None
            and effects[node.node_id].phase in {"committed", "verified"}
            for node in revision.nodes
        )
        if all_done:
            self._store.transition_status(
                transaction_id, {"committing"}, "committed"
            )
            self._store.append_event(
                transaction_id, "transaction_committed",
                payload={"revision": revision_number},
                idempotency_key=f"transaction_committed:{revision_number}",
            )
            return CommitResult(status="committed", committed_nodes=tuple(committed))
        # Partial (through_node) commit: return to ready for the rest.
        self._store.transition_status(transaction_id, {"committing"}, "ready")
        return CommitResult(status="ready", committed_nodes=tuple(committed))

    def _commit_node(
        self,
        transaction_id: str,
        revision_number: int,
        node_id: str,
        *,
        parents: set[str],
        expected_preview_hash: str,
        requester: str,
        channel: str,
        invoke: Optional[Callable] = None,
    ) -> str:
        """Commit one node. Returns committed|skipped|blocked|failed|unknown_effect."""
        store = self._store
        # Reload the transaction before every node: revisions or authority
        # may have changed mid-commit.
        transaction = store.get_transaction(transaction_id)
        if transaction.current_revision != revision_number:
            self._block_transaction(
                transaction_id, {"committing"}, "commit_blocked",
                {"reason": "revision changed mid-commit"},
            )
            return "blocked"
        # Cross-revision lookup: a node committed at an earlier revision
        # is this node's truth and must be skipped, never re-executed.
        effect = store.latest_effects_by_node(transaction_id).get(node_id)
        if effect is None:
            self._block_transaction(
                transaction_id, {"committing"}, "commit_blocked",
                {"reason": f"node {node_id} has no prepared effect"},
            )
            return "blocked"
        if effect.phase in {"committed", "verified"}:
            return "skipped"
        if effect.phase == "unknown_effect":
            self._block_transaction(
                transaction_id, {"committing"}, "commit_blocked",
                {"reason": f"node {node_id} is unknown_effect; reconcile first"},
            )
            raise EffectBlocked(
                f"node {node_id} is unknown_effect; reconcile before commit"
            )
        if effect.phase != "previewed":
            self._block_transaction(
                transaction_id, {"committing"}, "commit_blocked",
                {"reason": f"node {node_id} phase {effect.phase} is not previewed"},
            )
            return "blocked"
        if effect.preview_hash != expected_preview_hash:
            self._block_transaction(
                transaction_id, {"committing"}, "commit_blocked",
                {"reason": f"node {node_id} preview is stale"},
            )
            return "blocked"
        latest_effects = store.latest_effects_by_node(transaction_id)
        for parent in sorted(parents):
            parent_effect = latest_effects.get(parent)
            if parent_effect is None or parent_effect.phase not in {
                "committed", "verified",
            }:
                self._block_transaction(
                    transaction_id, {"committing"}, "commit_blocked",
                    {"reason": f"parent {parent} of {node_id} is not committed"},
                )
                return "blocked"

        adapter = self._adapters.get(effect.adapter_id)
        context = self._effect_context(transaction_id, revision_number, node_id)
        prepared = prepared_from_json(effect.prepared or {})

        # The transaction's own stored authority contract narrows first:
        # actions/resources/expiry declared at creation bind every commit.
        from agent.effects.authority import enforce_transaction_authority

        contract_ok, contract_reason = enforce_transaction_authority(
            transaction, prepared, now_ms=int(time.time() * 1000),
        )
        if not contract_ok:
            store.transition_effect(effect.effect_id, {"previewed"}, "blocked")
            self._block_transaction(
                transaction_id, {"committing"}, "authority_blocked",
                {"node_id": node_id, "code": "transaction_authority",
                 "reason": contract_reason},
            )
            return "blocked"

        # Profile-wide authority is reloaded immediately before the
        # effect. A factory returning None means no profile evaluator is
        # configured (autonomy mode off) — mirroring the runtime
        # authority_gate — and the transaction contract plus exact
        # approvals still bind.
        provider = self._authority_provider_factory()
        decision = None
        if provider is not None:
            action_context = build_action_context(
                prepared,
                operation_key=f"{transaction_id}:{revision_number}:{node_id}",
                transaction_id=transaction_id,
                profile_id=self._profile,
            )
            decision = authorize_effect(
                provider, action_context, stage="commit", consume=True
            )
            if not getattr(decision, "allowed", False):
                store.transition_effect(
                    effect.effect_id, {"previewed"}, "blocked"
                )
                self._block_transaction(
                    transaction_id, {"committing"}, "authority_blocked",
                    {"node_id": node_id,
                     "code": getattr(decision, "code", "")},
                )
                return "blocked"
        self._trace("authority_rechecked")

        preview_payload = effect.preview or {}
        if preview_payload.get("requires_approval"):
            if self._approval_consumer is None:
                store.transition_effect(effect.effect_id, {"previewed"}, "blocked")
                self._block_transaction(
                    transaction_id, {"committing"}, "approval_blocked",
                    {"node_id": node_id, "reason": "no approval channel"},
                )
                return "blocked"
            consumption = self._approval_consumer(
                transaction_id=transaction_id,
                revision=revision_number,
                node_id=node_id,
                args_hash=content_hash(prepared.args),
                preview_hash=effect.preview_hash,
                resources=tuple(prepared.resources),
                authority_version=transaction.authority_version,
                requester=requester,
                channel=channel,
            )
            if not getattr(consumption, "approved", False):
                store.transition_effect(effect.effect_id, {"previewed"}, "blocked")
                self._block_transaction(
                    transaction_id, {"committing"}, "approval_blocked",
                    {
                        "node_id": node_id,
                        "code": getattr(consumption, "code", "missing"),
                    },
                )
                return "blocked"

        operation_id = effect.operation_id
        record = self._journal.get(operation_id)
        if record is None:
            self._journal.create(
                operation_id=operation_id, kind="effect_commit",
                destination=effect.adapter_id,
                payload_hash=content_hash(prepared.args),
            )
            record = self._journal.get(operation_id)
        if record.state in {"running", "dispatched", "unknown"}:
            # Ambiguous prior attempt: never blind retry.
            self._block_transaction(
                transaction_id, {"committing"}, "commit_blocked",
                {"reason": f"operation {operation_id} is {record.state}"},
            )
            raise EffectBlocked(
                f"operation {operation_id} is {record.state}; reconcile first"
            )
        if record.state == "confirmed":
            # Already durably confirmed: finalize bookkeeping only.
            store.transition_effect(
                effect.effect_id, {"previewed"}, "committed",
            )
            return "committed"
        from_state = record.state  # pending or failed
        self._journal.transition(
            operation_id, from_states={from_state}, to_state="running",
            effect_disposition="none",
        )
        self._trace("journal_running")
        store.transition_effect(
            effect.effect_id, {"previewed"}, "committing",
            updates={"authority_json": {
                "verdict": getattr(decision, "verdict", ""),
                "code": getattr(decision, "code", ""),
                "context_hash": getattr(decision, "context_hash", ""),
            }},
        )
        self._trace("effect_committing")
        self._fault("after_commit_intent", {"node_id": node_id})

        request = CommitRequest(
            prepared=prepared,
            operation_id=operation_id,
            idempotency_key=operation_id,
            invoke=invoke,
        )
        try:
            outcome = adapter.commit(request, context)
        except Exception as exc:
            return self._classify_ambiguous(
                transaction_id, effect, adapter, context,
                error=f"{type(exc).__name__}: {exc}",
            )
        self._trace("handler_called")
        self._fault("after_handler_return", {"node_id": node_id})

        store.transition_effect(
            effect.effect_id, {"committing"}, "committing",
            updates={"result_json": dataclasses.asdict(outcome)},
        )
        self._trace("raw_result_persisted")

        if outcome.status != "committed":
            self._journal.transition_if_current(
                operation_id, from_states={"running"}, to_state="failed",
                effect_disposition="none", error=outcome.error,
            )
            store.transition_effect(effect.effect_id, {"committing"}, "failed")
            self._block_transaction(
                transaction_id, {"committing"}, "effect_failed",
                {"node_id": node_id, "error": outcome.error or ""},
            )
            return "failed"

        verification = adapter.verify(outcome, context)
        store.transition_effect(
            effect.effect_id, {"committing"}, "committing",
            updates={"verification_json": dataclasses.asdict(verification)},
        )
        self._trace("verified")
        self._fault("after_delivery_dispatch", {"node_id": node_id})

        self._journal.transition(
            operation_id, from_states={"running", "dispatched"},
            to_state="confirmed",
            effect_disposition="landed" if verification.verified else "none",
            result=dict(outcome.result),
        )
        self._trace("journal_confirmed")
        final_phase = "verified" if verification.verified else "committed"
        store.transition_effect(effect.effect_id, {"committing"}, final_phase)
        store.append_event(
            transaction_id, "effect_committed", effect_id=effect.effect_id,
            payload={"node_id": node_id, "verified": verification.verified},
            idempotency_key=f"effect_committed:{effect.effect_id}",
        )
        if self._receipt_builder is not None:
            try:
                self._receipt_builder.record_effect(
                    transaction_id=transaction_id, effect_id=effect.effect_id,
                )
            except Exception:
                pass
        self._trace("receipt_persisted")
        return "committed"

    def _classify_ambiguous(
        self, transaction_id: str, effect, adapter, context, *, error: str
    ) -> str:
        """Handler raised or durable confirmation failed: reconcile once."""
        try:
            current = self._store.get_effect(effect.effect_id)
            reconciliation = adapter.reconcile(current, context)
            disposition = reconciliation.disposition
            evidence = dict(reconciliation.evidence)
        except Exception:
            disposition = "unknown"
            evidence = {}
        payload = {
            "disposition": disposition, "evidence": evidence, "error": error,
        }
        if disposition == "landed":
            self._journal.transition_if_current(
                effect.operation_id, from_states={"running", "dispatched"},
                to_state="confirmed", effect_disposition="landed",
            )
            self._store.transition_effect(
                effect.effect_id, {"committing"}, "committed",
                updates={"reconciliation_json": payload},
            )
            self._store.append_event(
                transaction_id, "effect_reconciled",
                effect_id=effect.effect_id, payload=payload,
                idempotency_key=f"effect_reconciled:{effect.effect_id}",
            )
            return "committed"
        if disposition == "not_landed":
            self._journal.transition_if_current(
                effect.operation_id, from_states={"running"},
                to_state="failed", effect_disposition="none", error=error,
            )
            self._store.transition_effect(
                effect.effect_id, {"committing"}, "failed",
                updates={"reconciliation_json": payload},
            )
            self._block_transaction(
                transaction_id, {"committing"}, "effect_failed",
                {"effect_id": effect.effect_id, "error": error},
            )
            return "failed"
        self._journal.transition_if_current(
            effect.operation_id, from_states={"running", "dispatched"},
            to_state="unknown", effect_disposition="unknown", error=error,
        )
        self._store.transition_effect(
            effect.effect_id, {"committing"}, "unknown_effect",
            updates={"reconciliation_json": payload},
        )
        self._store.transition_status(
            transaction_id,
            {"committing", "ready", "blocked"},
            "unknown_effect",
        )
        self._store.append_event(
            transaction_id, "unknown_effect_review",
            effect_id=effect.effect_id, payload=payload,
            idempotency_key=f"unknown_effect_review:{effect.effect_id}",
        )
        return "unknown_effect"

    # ── Revise ───────────────────────────────────────────────────────

    def revise(
        self,
        transaction_id: str,
        *,
        expected_revision: int,
        graph: Mapping[str, Any],
        reason: str,
    ):
        return graph_create_revision(
            self._store, transaction_id, expected_revision, graph, reason,
            adapter_registry=self._adapters,
        )

    # ── Reconcile ────────────────────────────────────────────────────

    def reconcile(self, transaction_id: str) -> ReconcileResult:
        from agent.effects.recovery import (
            project_transaction_status,
            reconcile_effect,
        )

        store = self._store
        transaction = store.get_transaction(transaction_id)
        if transaction is None:
            raise KeyError(f"unknown transaction {transaction_id!r}")
        counts = {"landed": 0, "not_landed": 0, "unknown": 0, "skipped": 0}
        # A crash mid-preview leaves the aggregate stuck in previewing:
        # surface it as blocked so a later preview can run again.
        store.transition_status(transaction_id, {"previewing"}, "blocked")
        for effect in store.list_effects(transaction_id):
            if effect.phase not in {"committing", "unknown_effect"}:
                continue
            disposition = reconcile_effect(
                store, self._journal, self._adapters, effect,
                profile=self._profile, workspace_root=self._workspace_root,
            )
            counts[disposition] += 1
        return ReconcileResult(
            status=project_transaction_status(store, transaction_id),
            counts=counts,
        )

    def _compensate_prefix(
        self, transaction_id: str, committed: list[str]
    ) -> tuple[str, ...]:
        """Compensate the just-committed prefix in reverse commit order.

        Each node passes through the full eligibility + authority path of
        :meth:`compensate`; the pass stops at the first node that cannot
        compensate safely rather than forcing through danger.
        """
        compensated: list[str] = []
        for node_id in reversed(committed):
            try:
                outcome = self.compensate(transaction_id, node_id)
            except (EffectBlocked, KeyError):
                break
            if outcome.status != "compensated":
                break
            compensated.extend(outcome.compensated_nodes)
        if compensated:
            self._store.append_event(
                transaction_id, "prefix_compensated",
                payload={"nodes": list(compensated)},
                idempotency_key=(
                    f"prefix_compensated:{transaction_id}:"
                    f"{'-'.join(compensated)}"
                ),
            )
        return tuple(compensated)

    # ── Compensation ─────────────────────────────────────────────────

    def compensate(
        self,
        transaction_id: str,
        node_id: str,
        *,
        cascade: bool = False,
        requester: str = "system",
        channel: str = "cli",
    ) -> "CompensationOutcome":
        """Compensate *node_id* (and, with cascade, its committed
        descendants) in reverse topological order, re-evaluating
        eligibility immediately before every node. Stops at the first
        changed/unsafe node and reports ``partially_compensated``; it
        never continues across an unknown or irreversible boundary."""
        from agent.effects.eligibility import (
            eligibility_for_effect,
            plan_compensation,
        )

        store = self._store
        transaction = store.get_transaction(transaction_id)
        if transaction is None:
            raise KeyError(f"unknown transaction {transaction_id!r}")

        initial = eligibility_for_effect(
            store, self._adapters, transaction_id, node_id,
            cascade=cascade,
            authority_provider_factory=self._authority_provider_factory,
        )
        if initial.code == "already_compensated":
            return CompensationOutcome(status="compensated")
        if initial.code == "blocked_live_dependents":
            raise EffectBlocked(
                f"committed dependents {list(initial.blockers)} remain for "
                f"node {node_id!r}; pass cascade to include them"
            )
        if not initial.can_execute:
            raise EffectBlocked(initial.reason)

        plan = plan_compensation(store, transaction_id, node_id, cascade=cascade)
        compensated: list[str] = []
        stopped_reason: Optional[str] = None
        for plan_node in plan.node_ids:
            eligibility = eligibility_for_effect(
                store, self._adapters, transaction_id, plan_node,
                cascade=cascade,
                authority_provider_factory=self._authority_provider_factory,
            )
            if eligibility.code == "already_compensated":
                continue
            if not eligibility.can_execute:
                stopped_reason = eligibility.reason
                break
            effect = store.latest_effects_by_node(transaction_id).get(
                plan_node
            )
            prepared = prepared_from_json(effect.prepared or {})
            from agent.effects.authority import enforce_transaction_authority

            contract_ok, contract_reason = enforce_transaction_authority(
                transaction, prepared, now_ms=int(time.time() * 1000),
            )
            if not contract_ok:
                stopped_reason = (
                    f"transaction authority denies compensation of "
                    f"{plan_node!r}: {contract_reason}"
                )
                break
            provider = self._authority_provider_factory()
            decision = None
            if provider is not None:
                decision = authorize_effect(
                    provider,
                    build_action_context(
                        prepared,
                        operation_key=(
                            f"{transaction_id}:{plan.revision}:{plan_node}"
                            ":compensate"
                        ),
                        transaction_id=transaction_id,
                    ),
                    stage="compensate", consume=True,
                )
                if not getattr(decision, "allowed", False):
                    stopped_reason = (
                        f"authority denied compensation for node {plan_node!r}"
                    )
                    break

            verified_hash = content_hash(dict(effect.verification or {}))
            digest = hashlib.sha256(
                f"compensate\0{effect.effect_id}\0{verified_hash}".encode(
                    "utf-8"
                )
            ).hexdigest()
            operation_id = f"txcomp-{digest[:40]}"
            existing = store.get_compensation_by_operation_id(operation_id)
            if existing is not None and existing.status == "compensated":
                # Idempotent: the terminal attempt already answered this
                # exact compensation; never execute it twice.
                compensated.append(plan_node)
                continue
            self._journal.create(
                operation_id=operation_id, kind="effect_compensation",
                destination=effect.adapter_id, payload_hash=verified_hash,
            )
            record = self._journal.get(operation_id)
            if record.state in {"pending", "failed"}:
                self._journal.transition(
                    operation_id, from_states={record.state},
                    to_state="running", effect_disposition="none",
                )
            elif record.state in {"running", "dispatched", "unknown"}:
                stopped_reason = (
                    f"compensation operation {operation_id} is "
                    f"{record.state}; reconcile before retrying"
                )
                break
            if existing is None:
                store.insert_compensation(
                    compensation_id=f"cm-{digest[:40]}",
                    effect_id=effect.effect_id,
                    operation_id=operation_id,
                    fidelity=eligibility.fidelity,
                    authority={
                        "verdict": getattr(decision, "verdict", ""),
                        "code": getattr(decision, "code", ""),
                    },
                    before=dict(effect.verification or {}),
                )
            store.transition_effect(
                effect.effect_id, {"committed", "verified"}, "compensating",
            )
            adapter = self._adapters.get(effect.adapter_id)
            context = self._effect_context(
                transaction_id, plan.revision, plan_node
            )
            request = CompensationRequest(
                effect_id=effect.effect_id,
                prepared=prepared,
                verified_result_hash=verified_hash,
                cascade_plan_hash=plan.plan_hash,
            )
            try:
                result = adapter.compensate(request, context)
            except Exception as exc:
                result = None
                error = f"{type(exc).__name__}: {exc}"
            else:
                error = result.error
            if result is not None and result.status == "compensated":
                self._journal.transition(
                    operation_id, from_states={"running"},
                    to_state="confirmed", effect_disposition="landed",
                    result=dict(result.evidence),
                )
                store.finish_compensation(
                    f"cm-{digest[:40]}", status="compensated",
                    result=dict(result.evidence),
                    verification={"fidelity": result.fidelity},
                )
                store.transition_effect(
                    effect.effect_id, {"compensating"}, "compensated",
                )
                store.append_event(
                    transaction_id, "effect_compensated",
                    effect_id=effect.effect_id,
                    payload={"node_id": plan_node, "fidelity": result.fidelity},
                    idempotency_key=f"effect_compensated:{effect.effect_id}",
                )
                compensated.append(plan_node)
                continue
            # Blocked or failed: roll the phase back and stop the cascade.
            self._journal.transition_if_current(
                operation_id, from_states={"running"}, to_state="failed",
                effect_disposition="none", error=error,
            )
            store.finish_compensation(
                f"cm-{digest[:40]}",
                status="blocked" if result is not None else "failed",
                error=error,
            )
            store.transition_effect(
                effect.effect_id, {"compensating"}, "committed",
            )
            stopped_reason = error or f"compensation of {plan_node!r} failed"
            break

        revision = store.get_revision(transaction_id, plan.revision)
        effects = store.latest_effects_by_node(transaction_id)
        all_compensated = all(
            node.node_id in effects
            and effects[node.node_id].phase == "compensated"
            for node in revision.nodes
        )
        if stopped_reason is not None:
            store.transition_status(
                transaction_id,
                {"committed", "compensating", "blocked"},
                "partially_compensated",
            )
            return CompensationOutcome(
                status="partially_compensated",
                compensated_nodes=tuple(compensated),
                error=stopped_reason,
            )
        target_status = "compensated" if all_compensated else (
            "partially_compensated" if compensated else transaction.status
        )
        if compensated:
            store.transition_status(
                transaction_id,
                {"committed", "compensating", "blocked",
                 "partially_compensated"},
                target_status,
            )
        return CompensationOutcome(
            status="compensated" if compensated or not plan.node_ids
            else "partially_compensated",
            compensated_nodes=tuple(compensated),
        )

    # ── In-transaction tool calls ────────────────────────────────────

    def commit_tool_effect(
        self,
        *,
        tool_name: str,
        effective_args: Mapping[str, Any],
        operation_key: str,
        invoke: Callable,
        execution: TransactionExecutionContext,
    ) -> Any:
        """Commit exactly one planned node via a live tool call.

        Fails closed when the node is not part of the planned graph — a
        transaction context never appends hidden work.
        """
        transaction_id = execution.transaction_id
        revision_number = execution.revision
        node_id = execution.node_id
        node = self._store.get_node(transaction_id, revision_number, node_id)
        if node is None:
            raise EffectBlocked(
                f"node {node_id!r} is not planned in transaction "
                f"{transaction_id!r} revision {revision_number}; refusing "
                "to append hidden work"
            )
        transaction = self._store.get_transaction(transaction_id)
        if transaction is None or transaction.status not in {"ready", "committing"}:
            raise EffectBlocked(
                f"transaction {transaction_id!r} is not committable"
            )
        revision = self._store.get_revision(transaction_id, revision_number)
        parents = {
            edge.parent_node_id
            for edge in revision.edges
            if edge.child_node_id == node_id
        }
        outcome = self._commit_node(
            transaction_id, revision_number, node_id,
            parents=parents,
            expected_preview_hash=revision.preview_hash or "",
            requester="system", channel="tool",
            invoke=invoke,
        )
        if outcome not in {"committed", "skipped"}:
            raise EffectBlocked(
                f"in-transaction tool call for node {node_id!r} was {outcome}"
            )
        effect = self._store.latest_effects_by_node(transaction_id).get(node_id)
        result = dict(effect.result or {})
        raw = result.get("result")
        return raw if raw is not None else result

    @staticmethod
    def operation_key_for(tool_name: str, effective_args: Mapping[str, Any]) -> str:
        return content_hash({"tool": tool_name, "args": dict(effective_args)})
