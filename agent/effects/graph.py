"""DAG validation, deterministic ordering, and immutable plan revisions.

Committed work is history: a later revision may add pending nodes that
depend on committed ones, but can never remove a frozen node, change its
spec, or rewrite its incoming causality. All ordering is deterministic —
Kahn topological sort with lexical node-id tie breaking — so previews,
commits, and compensations replay identically across processes.
"""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from agent.effects.models import (
    RevisionConflict,
    RevisionEdge,
    RevisionNode,
    TransactionRevision,
    canonical_json,
    normalize_graph_input,
)

__all__ = [
    "FROZEN_PHASES",
    "GraphCycleError",
    "GraphValidationError",
    "RevisionDiff",
    "create_revision",
    "reverse_compensation_order",
    "topological_order",
    "validate_graph",
    "validate_revision",
]

_NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# An effect in any of these phases freezes its node in every later
# revision. `committing` and `unknown_effect` freeze the frontier too:
# ambiguity is never revised away.
FROZEN_PHASES: frozenset[str] = frozenset({
    "committing", "committed", "verified",
    "compensating", "compensated", "unknown_effect",
})


class GraphValidationError(ValueError):
    """The proposed graph violates structural DAG rules."""


class GraphCycleError(GraphValidationError):
    """The proposed graph contains a dependency cycle."""


def validate_graph(
    graph: Mapping[str, Any],
    *,
    adapter_registry=None,
) -> tuple[tuple[RevisionNode, ...], tuple[RevisionEdge, ...]]:
    """Validate structure and return normalized frozen nodes/edges.

    When *adapter_registry* is provided, every ``(adapter_id, action)``
    pair must be registered; without one, adapter existence is deferred to
    preview time.
    """
    nodes, edges = normalize_graph_input(graph)
    for node in nodes:
        if not _NODE_ID_RE.match(node.node_id):
            raise GraphValidationError(
                f"node id {node.node_id!r} must match {_NODE_ID_RE.pattern}"
            )
        # Canonical-JSON round trip is guaranteed by normalize_graph_input;
        # re-assert cheaply so a future refactor cannot drop it silently.
        canonical_json(node.args)
    for edge in edges:
        if edge.parent_node_id == edge.child_node_id:
            raise GraphValidationError(
                f"self-edge on node {edge.parent_node_id!r} is not allowed"
            )
    if adapter_registry is not None:
        for node in nodes:
            if not adapter_registry.supports(node.adapter_id, node.action):
                raise GraphValidationError(
                    f"node {node.node_id!r} references unregistered "
                    f"adapter/action {node.adapter_id!r}/{node.action!r}"
                )
    _topological_order_or_raise(nodes, edges)
    return nodes, edges


def _as_nodes_edges(
    graph: Mapping[str, Any] | tuple,
) -> tuple[tuple[RevisionNode, ...], tuple[RevisionEdge, ...]]:
    if isinstance(graph, tuple):
        return graph
    return normalize_graph_input(graph)


def _topological_order_or_raise(
    nodes: tuple[RevisionNode, ...], edges: tuple[RevisionEdge, ...]
) -> list[str]:
    children: dict[str, list[str]] = {node.node_id: [] for node in nodes}
    indegree: dict[str, int] = {node.node_id: 0 for node in nodes}
    for edge in edges:
        children[edge.parent_node_id].append(edge.child_node_id)
        indegree[edge.child_node_id] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        node_id = heapq.heappop(ready)
        order.append(node_id)
        for child in children[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(nodes):
        cyclic = sorted(
            node_id for node_id, degree in indegree.items() if degree > 0
        )
        raise GraphCycleError(f"graph contains a cycle through {cyclic}")
    return order


def topological_order(graph: Mapping[str, Any] | tuple) -> list[str]:
    """Stable topological order with lexical node-id tie breaking."""
    nodes, edges = _as_nodes_edges(graph)
    return _topological_order_or_raise(nodes, edges)


def reverse_compensation_order(
    graph: Mapping[str, Any] | tuple, selected: Iterable[str]
) -> list[str]:
    """Reverse of the stable topological order, restricted to *selected*."""
    selected_set = set(selected)
    return [
        node_id
        for node_id in reversed(topological_order(graph))
        if node_id in selected_set
    ]


@dataclass(frozen=True)
class RevisionDiff:
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]
    frozen: tuple[str, ...]


def _node_map(nodes: tuple[RevisionNode, ...]) -> dict[str, RevisionNode]:
    return {node.node_id: node for node in nodes}


def _incoming(edges: tuple[RevisionEdge, ...]) -> dict[str, frozenset[str]]:
    incoming: dict[str, set[str]] = {}
    for edge in edges:
        incoming.setdefault(edge.child_node_id, set()).add(edge.parent_node_id)
    return {child: frozenset(parents) for child, parents in incoming.items()}


def validate_revision(
    old_nodes: tuple[RevisionNode, ...],
    old_edges: tuple[RevisionEdge, ...],
    new_nodes: tuple[RevisionNode, ...],
    new_edges: tuple[RevisionEdge, ...],
    phases: Mapping[str, str],
) -> RevisionDiff:
    """Enforce the revision truth table against latest effect phases.

    *phases* maps node id -> latest effect phase; nodes without an entry
    count as pending (``planned``).
    """
    old_map = _node_map(old_nodes)
    new_map = _node_map(new_nodes)
    frozen_ids = {
        node_id
        for node_id in old_map
        if phases.get(node_id) in FROZEN_PHASES
    }

    for node_id in sorted(frozen_ids):
        if node_id not in new_map:
            raise RevisionConflict(
                f"frozen node {node_id} cannot be removed by a revision"
            )
        if new_map[node_id] != old_map[node_id]:
            raise RevisionConflict(
                f"frozen node {node_id} cannot be changed by a revision"
            )

    old_incoming = _incoming(old_edges)
    new_incoming = _incoming(new_edges)
    for node_id in sorted(frozen_ids):
        before = old_incoming.get(node_id, frozenset())
        after = new_incoming.get(node_id, frozenset())
        if before != after:
            raise RevisionConflict(
                f"incoming edges of committed node {node_id} are frozen "
                "and cannot be rewritten"
            )

    added = tuple(sorted(set(new_map) - set(old_map)))
    removed = tuple(sorted(set(old_map) - set(new_map)))
    changed = tuple(sorted(
        node_id
        for node_id in set(old_map) & set(new_map)
        if old_map[node_id] != new_map[node_id]
    ))
    return RevisionDiff(
        added=added, changed=changed, removed=removed,
        frozen=tuple(sorted(frozen_ids)),
    )


def create_revision(
    store,
    transaction_id: str,
    expected_revision: int,
    graph: Mapping[str, Any],
    reason: str,
    *,
    adapter_registry=None,
) -> TransactionRevision:
    """Validate and atomically persist revision ``expected_revision + 1``.

    Validation order matters for honest errors: structural graph problems
    (cycles, bad ids) surface first, then the optimistic-CAS check, then
    the frozen-node truth table. The storage CAS re-checks the revision
    inside the write transaction, so a concurrent revision loses cleanly
    with ``RevisionConflict`` and no partial writes.
    """
    new_nodes, new_edges = validate_graph(graph, adapter_registry=adapter_registry)

    transaction = store.get_transaction(transaction_id)
    if transaction is None:
        raise KeyError(f"unknown transaction {transaction_id!r}")
    if transaction.current_revision != expected_revision:
        raise RevisionConflict(
            f"expected revision {expected_revision}, "
            f"current {transaction.current_revision}"
        )

    old_revision = store.get_revision(transaction_id, expected_revision)
    phases = store.latest_effect_phases(transaction_id)
    validate_revision(
        old_revision.nodes, old_revision.edges, new_nodes, new_edges, phases
    )

    # Every pending prepared/previewed attempt is superseded: a revision
    # always requires a fresh preview, even for unchanged pending nodes.
    superseded = [
        effect.effect_id
        for effect in store.list_effects(transaction_id)
        if effect.phase in {"prepared", "previewed"}
    ]

    return store.create_revision(
        transaction_id=transaction_id,
        expected_revision=expected_revision,
        nodes=new_nodes,
        edges=new_edges,
        reason=reason,
        superseded_effect_ids=superseded,
    )
