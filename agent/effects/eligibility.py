"""Dynamic undo eligibility and cascade planning (plan Task 9).

No optimistic labels: "exact undo" is claimed only for
``eligible_exact`` after live drift, window, authority, dependency,
unknown, and irreversible-boundary facts all check out — semantic
compensation is always displayed as its own thing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from agent.autonomy import authorize_effect
from agent.effects.authority import build_action_context
from agent.effects.graph import reverse_compensation_order
from agent.effects.models import (
    EffectContext,
    content_hash,
)

__all__ = [
    "CompensationPlan",
    "UndoEligibility",
    "eligibility_for_effect",
    "eligibility_for_transaction",
    "plan_compensation",
]

_COMMITTED_PHASES = frozenset({"committed", "verified"})


@dataclass(frozen=True)
class UndoEligibility:
    can_execute: bool
    code: str
    reason: str
    fidelity: str
    blockers: tuple[str, ...] = ()
    required_cascade_node_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompensationPlan:
    transaction_id: str
    revision: int
    target_node_id: str
    node_ids: tuple[str, ...]
    plan_hash: str


def _now_ms(clock) -> int:
    if clock is None:
        return int(time.time() * 1000)
    if hasattr(clock, "now_ms"):
        return int(clock.now_ms())
    return int(clock())


def _descendants(revision, target: str) -> set[str]:
    children: dict[str, set[str]] = {}
    for edge in revision.edges:
        children.setdefault(edge.parent_node_id, set()).add(edge.child_node_id)
    seen: set[str] = set()
    frontier = [target]
    while frontier:
        node = frontier.pop()
        for child in children.get(node, ()):
            if child not in seen:
                seen.add(child)
                frontier.append(child)
    return seen


def _graph_mapping(revision) -> dict:
    return {
        "nodes": [
            {
                "node_id": node.node_id,
                "adapter_id": node.adapter_id,
                "action": node.action,
                "args": dict(node.args),
                "resource_keys": list(node.resource_keys),
            }
            for node in revision.nodes
        ],
        "edges": [
            {"parent": edge.parent_node_id, "child": edge.child_node_id}
            for edge in revision.edges
        ],
    }


def plan_compensation(
    store, transaction_id: str, target_node_id: str, *, cascade: bool
) -> CompensationPlan:
    """Freeze the reverse-topological compensation plan for *target*."""
    transaction = store.get_transaction(transaction_id)
    if transaction is None:
        raise KeyError(f"unknown transaction {transaction_id!r}")
    revision = store.get_revision(transaction_id, transaction.current_revision)
    effects = store.latest_effects_by_node(transaction_id)
    selected = {target_node_id}
    if cascade:
        selected |= _descendants(revision, target_node_id)
    committed = {
        node_id
        for node_id in selected
        if node_id in effects and effects[node_id].phase in _COMMITTED_PHASES
    }
    order = tuple(
        reverse_compensation_order(_graph_mapping(revision), committed)
    )
    plan_hash = content_hash({
        "transaction_id": transaction_id,
        "revision": revision.revision,
        "target": target_node_id,
        "nodes": [
            [node_id, content_hash(dict(effects[node_id].verification or {}))]
            for node_id in order
        ],
    })
    return CompensationPlan(
        transaction_id=transaction_id,
        revision=revision.revision,
        target_node_id=target_node_id,
        node_ids=order,
        plan_hash=plan_hash,
    )


def _result(code: str, reason: str, fidelity: str, *, cascade_nodes=(),
            blockers=()) -> UndoEligibility:
    return UndoEligibility(
        can_execute=code in {"eligible_exact", "eligible_compensation"},
        code=code,
        reason=reason,
        fidelity=fidelity,
        blockers=tuple(blockers),
        required_cascade_node_ids=tuple(cascade_nodes),
    )


def eligibility_for_effect(
    store,
    adapters,
    transaction_id: str,
    node_id: str,
    *,
    cascade: bool = False,
    authority_provider_factory: Optional[Callable] = None,
    clock=None,
) -> UndoEligibility:
    """Evaluate, in order: terminal state, declared fidelity, unknown
    subgraph, live dependents, irreversible descendants, window,
    authority, live drift."""
    from agent.effects.coordinator import prepared_from_json

    transaction = store.get_transaction(transaction_id)
    if transaction is None:
        raise KeyError(f"unknown transaction {transaction_id!r}")
    revision = store.get_revision(transaction_id, transaction.current_revision)
    effects = store.latest_effects_by_node(transaction_id)
    effect = effects.get(node_id)
    if effect is None:
        return _result(
            "unsupported", f"node {node_id!r} has no effect attempt", "none",
        )

    # 1. Terminal state.
    if effect.phase in {"compensated", "compensating"}:
        return _result(
            "already_compensated",
            f"node {node_id!r} is already {effect.phase}",
            str((effect.semantics or {}).get("fidelity", "none")),
        )
    if effect.phase == "unknown_effect":
        return _result(
            "blocked_unknown",
            f"node {node_id!r} is unknown_effect; reconcile before undo",
            "none",
        )
    if effect.phase not in _COMMITTED_PHASES:
        return _result(
            "unsupported",
            f"node {node_id!r} phase {effect.phase} has nothing to undo",
            "none",
        )

    # 2. Declared fidelity — an adapter cannot manufacture guarantees.
    semantics = dict(effect.semantics or {})
    fidelity = str(semantics.get("fidelity", "none"))
    if fidelity == "none":
        return _result(
            "unsupported",
            f"adapter {effect.adapter_id!r} declares no compensation for "
            f"node {node_id!r}",
            "none",
        )

    descendants = _descendants(revision, node_id)

    # 3. Unknown nodes anywhere in the selected subgraph freeze undo.
    unknown = sorted(
        child for child in descendants
        if child in effects and effects[child].phase == "unknown_effect"
    )
    if unknown:
        return _result(
            "blocked_unknown",
            f"descendants {unknown} are unknown_effect; reconcile first",
            fidelity, blockers=unknown,
        )

    # 4. Live committed dependents require an explicit cascade.
    live = sorted(
        child for child in descendants
        if child in effects and effects[child].phase in _COMMITTED_PHASES
    )
    if live and not cascade:
        return _result(
            "blocked_live_dependents",
            f"committed dependents {live} remain; pass cascade to include "
            "them",
            fidelity, blockers=live,
        )

    # 5. An irreversible descendant is a boundary a cascade never crosses.
    if cascade:
        rigid = sorted(
            child for child in live
            if str(
                (effects[child].semantics or {}).get("fidelity", "none")
            ) == "none"
        )
        if rigid:
            return _result(
                "blocked_irreversible_boundary",
                f"descendants {rigid} are irreversible; cascade refuses to "
                "cross the boundary",
                fidelity, blockers=rigid,
            )

    # 6. Compensation window.
    now = _now_ms(clock)
    plan_nodes = [node_id, *(live if cascade else [])]
    for candidate in plan_nodes:
        candidate_effect = effects[candidate]
        window = (candidate_effect.semantics or {}).get(
            "compensation_window_seconds"
        )
        if window and now - candidate_effect.updated_at_ms > int(window) * 1000:
            return _result(
                "blocked_window_expired",
                f"compensation window of {window}s for node {candidate!r} "
                "has expired",
                fidelity, blockers=(candidate,),
            )

    # 7. Authority for the compensate stage: the transaction's own stored
    # contract narrows first, then the current profile-wide provider.
    prepared = prepared_from_json(effect.prepared or {})
    from agent.effects.authority import enforce_transaction_authority

    contract_ok, contract_reason = enforce_transaction_authority(
        transaction, prepared, now_ms=now,
    )
    if not contract_ok:
        return _result(
            "blocked_authority",
            f"transaction authority denies compensation of {node_id!r}: "
            f"{contract_reason}",
            fidelity,
        )
    provider = (
        authority_provider_factory()
        if authority_provider_factory is not None else None
    )
    if provider is not None:
        context = build_action_context(
            prepared,
            operation_key=f"{transaction_id}:{revision.revision}:{node_id}:compensate",
            transaction_id=transaction_id,
        )
        decision = authorize_effect(
            provider, context, stage="compensate",
            consume=False,
        )
        if not getattr(decision, "allowed", False):
            return _result(
                "blocked_authority",
                "current authority denies compensation for "
                f"node {node_id!r}",
                fidelity,
            )

    # 8. Live adapter inspection: verified-after state must still hold.
    try:
        adapter = adapters.get(effect.adapter_id)
        reconciliation = adapter.reconcile(
            effect,
            EffectContext(
                transaction_id=transaction_id,
                revision=revision.revision,
                node_id=node_id,
            ),
        )
        disposition = reconciliation.disposition
    except Exception:
        disposition = "unknown"
    if disposition != "landed":
        return _result(
            "blocked_drift",
            f"node {node_id!r} no longer matches its verified-after state "
            f"(reconcile: {disposition}); exact restoration would clobber",
            fidelity, blockers=(node_id,),
        )

    cascade_nodes = plan_compensation(
        store, transaction_id, node_id, cascade=cascade
    ).node_ids
    code = "eligible_exact" if fidelity == "exact" else "eligible_compensation"
    label = "exact undo" if fidelity == "exact" else "semantic compensation"
    return _result(
        code,
        f"node {node_id!r} is eligible for {label}",
        fidelity,
        cascade_nodes=cascade_nodes,
    )


def eligibility_for_transaction(
    store, adapters, transaction_id: str, **kwargs
) -> dict[str, UndoEligibility]:
    transaction = store.get_transaction(transaction_id)
    if transaction is None:
        raise KeyError(f"unknown transaction {transaction_id!r}")
    revision = store.get_revision(transaction_id, transaction.current_revision)
    return {
        node.node_id: eligibility_for_effect(
            store, adapters, transaction_id, node.node_id, **kwargs
        )
        for node in revision.nodes
    }
