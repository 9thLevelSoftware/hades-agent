"""Bounded owner-fenced startup reconciliation for action transactions.

Shared by CLI, TUI gateway, and messaging gateway startup: after the
operation journal's owner-fenced pass, classify in-flight effects through
their adapters — reconcile only, never re-commit. ``not_landed`` nodes
resume only via an explicit later ``commit``; ``unknown`` freezes the
transaction for review, exactly once.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.effects.models import EffectContext, EffectTransaction

__all__ = [
    "project_transaction_status",
    "reconcile_effect",
    "recover_transactions",
    "recover_transactions_at_startup",
]

# Statuses recovery must never edit — the transaction already reached a
# terminal truth.
_TERMINAL_STATUSES = frozenset({
    "committed", "compensated", "partially_compensated", "failed",
    "cancelled",
})


def reconcile_effect(
    store, journal, adapters, effect: EffectTransaction,
    *,
    profile: str = "default",
    workspace_root: str | None = None,
) -> str:
    """Classify one in-flight effect via its adapter. Returns the
    disposition applied: landed | not_landed | unknown | skipped."""
    if effect.phase not in {"committing", "unknown_effect"}:
        return "skipped"
    if (
        effect.phase == "unknown_effect"
        and effect.reconciliation is not None
        and effect.reconciliation.get("projected")
    ):
        # Unknown review state is projected exactly once.
        return "skipped"
    context = EffectContext(
        transaction_id=effect.transaction_id,
        revision=effect.revision,
        node_id=effect.node_id,
        profile=profile,
        workspace_root=workspace_root,
    )
    try:
        adapter = adapters.get(effect.adapter_id)
        result = adapter.reconcile(effect, context)
        disposition = result.disposition
        evidence: Mapping[str, Any] = dict(result.evidence)
    except Exception:
        disposition = "unknown"
        evidence = {}
    payload = {
        "disposition": disposition, "evidence": dict(evidence),
        "projected": True,
    }
    if disposition == "landed":
        journal.transition_if_current(
            effect.operation_id, from_states={"running", "dispatched"},
            to_state="confirmed", effect_disposition="landed",
        )
        store.transition_effect(
            effect.effect_id, {"committing", "unknown_effect"}, "committed",
            updates={"reconciliation_json": payload},
        )
        store.append_event(
            effect.transaction_id, "effect_reconciled",
            effect_id=effect.effect_id, payload=payload,
            idempotency_key=f"effect_reconciled:{effect.effect_id}",
        )
        return "landed"
    if disposition == "not_landed":
        journal.transition_if_current(
            effect.operation_id, from_states={"running"},
            to_state="failed", effect_disposition="none",
        )
        store.transition_effect(
            effect.effect_id, {"committing", "unknown_effect"}, "failed",
            updates={"reconciliation_json": payload},
        )
        return "not_landed"
    journal.transition_if_current(
        effect.operation_id, from_states={"running", "dispatched"},
        to_state="unknown", effect_disposition="unknown",
    )
    store.transition_effect(
        effect.effect_id, {"committing", "unknown_effect"}, "unknown_effect",
        updates={"reconciliation_json": payload},
    )
    store.append_event(
        effect.transaction_id, "unknown_effect_review",
        effect_id=effect.effect_id, payload=payload,
        idempotency_key=f"unknown_effect_review:{effect.effect_id}",
    )
    return "unknown"


def project_transaction_status(store, transaction_id: str) -> str:
    """Recompute the aggregate status from durable effect truth."""
    transaction = store.get_transaction(transaction_id)
    if transaction is None:
        raise KeyError(f"unknown transaction {transaction_id!r}")
    revision = store.get_revision(transaction_id, transaction.current_revision)
    effects = {
        effect.node_id: effect
        for effect in store.list_effects(transaction_id)
        if effect.revision == transaction.current_revision
    }
    phases = [
        effects[node.node_id].phase if node.node_id in effects else "planned"
        for node in revision.nodes
    ]
    if any(phase == "unknown_effect" for phase in phases):
        store.transition_status(
            transaction_id,
            {"committing", "ready", "blocked", "previewing", "draft"},
            "unknown_effect",
        )
        return "unknown_effect"
    if phases and all(phase in {"committed", "verified"} for phase in phases):
        store.transition_status(
            transaction_id, {"committing", "ready", "blocked"}, "committed"
        )
        return store.get_transaction(transaction_id).status
    if any(phase == "failed" for phase in phases):
        store.transition_status(
            transaction_id, {"committing", "ready"}, "blocked"
        )
        return store.get_transaction(transaction_id).status
    current = store.get_transaction(transaction_id).status
    if current == "committing":
        store.transition_status(transaction_id, {"committing"}, "blocked")
        return "blocked"
    return current


def recover_transactions(store, journal, adapters, limit: int = 100) -> dict:
    """Bounded recovery pass over the oldest in-flight effects.

    Runs AFTER the journal's owner-fenced restart reconciliation. Only
    adapter ``reconcile`` is invoked; recovery never re-commits, never
    compensates, and never edits a terminal transaction. Returns
    ``{landed, not_landed, unknown, skipped}``.
    """
    counts = {"landed": 0, "not_landed": 0, "unknown": 0, "skipped": 0}
    inflight: list[EffectTransaction] = []
    seen_transactions: dict[str, Any] = {}
    for transaction in store.list_transactions_by_status(
        {"previewing", "committing", "ready", "blocked", "unknown_effect"}
    ):
        # A crash mid-preview leaves the aggregate stuck in previewing:
        # surface it as blocked so a later preview can run again.
        store.transition_status(
            transaction.transaction_id, {"previewing"}, "blocked"
        )
        seen_transactions[transaction.transaction_id] = transaction
        for effect in store.list_effects(transaction.transaction_id):
            if effect.phase not in {"committing", "unknown_effect"}:
                continue
            if (
                effect.phase == "unknown_effect"
                and effect.reconciliation is not None
                and effect.reconciliation.get("projected")
            ):
                continue
            inflight.append(effect)
    inflight.sort(key=lambda effect: (effect.created_at_ms, effect.effect_id))

    touched: set[str] = set()
    for index, effect in enumerate(inflight):
        if index >= max(0, int(limit)):
            counts["skipped"] += 1
            continue
        disposition = reconcile_effect(store, journal, adapters, effect)
        if disposition in counts:
            counts[disposition] += 1
        touched.add(effect.transaction_id)
    for transaction_id in sorted(touched):
        project_transaction_status(store, transaction_id)
    return counts


def recover_transactions_at_startup(db, *, limit: int = 100) -> dict:
    """Convenience seam for CLI/TUI/gateway startup over one SessionDB."""
    from agent.effects.registry import default_effect_adapter_registry
    from agent.effects.store import TransactionStore
    from agent.operation_journal import OperationJournal

    store = TransactionStore(db)
    journal = OperationJournal(db)
    adapters = default_effect_adapter_registry()
    return recover_transactions(store, journal, adapters, limit=limit)
