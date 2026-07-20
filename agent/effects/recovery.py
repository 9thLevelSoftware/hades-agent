"""Bounded owner-fenced startup reconciliation for action transactions.

Shared by CLI, TUI gateway, and messaging gateway startup: after the
operation journal's owner-fenced pass, classify in-flight effects through
their adapters — reconcile only, never re-commit. ``not_landed`` nodes
resume only via an explicit later ``commit``; ``unknown`` freezes the
transaction for review, exactly once.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

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
    except KeyError:
        # No adapter registered in THIS process: leave the effect
        # untouched so a later pass with the adapter available can still
        # classify it. Freezing it as a projected unknown here would
        # permanently skip it.
        return "skipped"
    try:
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
    effects = store.latest_effects_by_node(transaction_id)
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


def _startup_recovery_settings() -> tuple[bool, int]:
    """Read ``transactions.auto_reconcile_on_start`` / ``recovery_batch_size``.

    Unreadable config falls back to the safe documented defaults
    (enabled, batch 100); values outside validation bounds are clamped.
    """
    enabled, batch = True, 100
    try:
        from hades_cli.config import load_config_readonly

        section = (load_config_readonly() or {}).get("transactions") or {}
        raw_enabled = section.get("auto_reconcile_on_start")
        if isinstance(raw_enabled, bool):
            enabled = raw_enabled
        raw_batch = section.get("recovery_batch_size")
        if isinstance(raw_batch, int) and not isinstance(raw_batch, bool):
            batch = min(1000, max(1, raw_batch))
    except Exception:
        pass
    return enabled, batch


def recover_transactions_at_startup(db, *, limit: Optional[int] = None) -> dict:
    """Convenience seam for CLI/TUI/gateway startup over one SessionDB.

    Honors the user's configuration: ``auto_reconcile_on_start: false``
    disables the startup pass entirely, and ``recovery_batch_size``
    bounds it (an explicit *limit* argument overrides). Constructs the
    built-in adapter families the same way the CLI service does — the
    process-global registry starts empty in production, and an empty
    registry must never cause in-flight effects to be frozen as unknown.
    Effects owned by unregistered (e.g. plugin) adapters are skipped
    untouched for a later pass.
    """
    from pathlib import Path

    from agent.effects.adapters import register_builtin_adapters
    from agent.effects.adapters.message_outbox import MessageOutboxAdapter
    from agent.effects.registry import EffectAdapterRegistry
    from agent.effects.store import TransactionStore
    from agent.operation_journal import OperationJournal

    enabled, configured_batch = _startup_recovery_settings()
    if not enabled:
        return {"landed": 0, "not_landed": 0, "unknown": 0, "skipped": 0,
                "disabled": True}
    if limit is None:
        limit = configured_batch

    store = TransactionStore(db)
    journal = OperationJournal(db)
    adapters = EffectAdapterRegistry()
    try:
        register_builtin_adapters(
            adapters,
            workspace_root=Path.cwd(),
            transaction_lookup=store.get_effect_by_operation_id,
        )
        adapters.register(MessageOutboxAdapter(db_factory=lambda: db))
    except Exception:
        # A partially populated registry is still safer than an empty
        # one; unregistered adapters lead to skips, never freezes.
        pass
    return recover_transactions(store, journal, adapters, limit=limit)
