"""Frozen value objects and canonical vocabulary for action transactions.

This module owns the plan-canonical public contract names —
``ActionTransaction``, ``TransactionRevision``, ``RevisionNode``,
``RevisionEdge`` — plus effect attempts, events, and the storage error
types. Everything here is immutable; mutation happens only through
``TransactionStore`` CAS methods.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

__all__ = [
    "COMPENSATION_FIDELITIES",
    "EFFECT_PHASES",
    "ELIGIBILITY_CODES",
    "FAILURE_POLICIES",
    "RECONCILE_DISPOSITIONS",
    "TRANSACTION_STATUSES",
    "ActionTransaction",
    "CommitOutcome",
    "CommitRequest",
    "CompensationRequest",
    "CompensationResult",
    "EffectContext",
    "EffectPreview",
    "EffectSemantics",
    "EffectTransaction",
    "ImmutableRecordError",
    "NormalizedEffect",
    "PreparedEffect",
    "ReconciliationResult",
    "RevisionConflict",
    "RevisionEdge",
    "RevisionNode",
    "TransactionEvent",
    "TransactionRevision",
    "TransactionSnapshot",
    "TransactionStoreError",
    "VerificationResult",
    "canonical_json",
    "content_hash",
]

# ── Canonical vocabulary (user-visible contracts; reject anything else) ──

TRANSACTION_STATUSES: frozenset[str] = frozenset({
    "draft", "previewing", "ready", "committing", "committed",
    "revising", "compensating", "compensated", "partially_compensated",
    "blocked", "failed", "unknown_effect", "cancelled",
})

EFFECT_PHASES: frozenset[str] = frozenset({
    "planned", "prepared", "previewed", "committing", "committed", "verified",
    "superseded", "compensating", "compensated", "blocked", "failed",
    "unknown_effect",
})

COMPENSATION_FIDELITIES: frozenset[str] = frozenset({"exact", "semantic", "none"})

RECONCILE_DISPOSITIONS: frozenset[str] = frozenset({"landed", "not_landed", "unknown"})

ELIGIBILITY_CODES: frozenset[str] = frozenset({
    "eligible_exact", "eligible_compensation", "already_compensated",
    "blocked_live_dependents", "blocked_irreversible_boundary", "blocked_unknown",
    "blocked_drift", "blocked_window_expired", "blocked_authority",
    "unsupported",
})

FAILURE_POLICIES: frozenset[str] = frozenset({"stop", "compensate_prefix"})


class TransactionStoreError(RuntimeError):
    """Base class for transaction storage contract violations."""


class ImmutableRecordError(TransactionStoreError):
    """An attempt was made to rewrite an immutable persisted record."""


class RevisionConflict(TransactionStoreError):
    """Optimistic revision CAS failed or a frozen node was altered."""


# ── Canonical serialization ──────────────────────────────────────────────

def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _validate_member(value: str, vocabulary: frozenset[str], label: str) -> str:
    if value not in vocabulary:
        raise ValueError(
            f"invalid {label} {value!r}; expected one of {sorted(vocabulary)}"
        )
    return value


def validate_status(status: str) -> str:
    return _validate_member(status, TRANSACTION_STATUSES, "status")


def validate_phase(phase: str) -> str:
    return _validate_member(phase, EFFECT_PHASES, "phase")


def validate_failure_policy(policy: str) -> str:
    return _validate_member(policy, FAILURE_POLICIES, "failure_policy")


# ── Frozen records ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ActionTransaction:
    transaction_id: str
    profile: str
    title: str
    status: str
    current_revision: int
    authority_version: int
    authority: Mapping[str, Any]
    failure_policy: str
    receipt_id: Optional[str]
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class RevisionNode:
    node_id: str
    adapter_id: str
    action: str
    args: Mapping[str, Any]
    resource_keys: tuple[str, ...] = ()

    def canonical(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "adapter_id": self.adapter_id,
            "action": self.action,
            "args": self.args,
            "resource_keys": sorted(self.resource_keys),
        }


@dataclass(frozen=True)
class RevisionEdge:
    parent_node_id: str
    child_node_id: str


@dataclass(frozen=True)
class TransactionRevision:
    transaction_id: str
    revision: int
    base_revision: Optional[int]
    reason: str
    graph_hash: str
    preview_hash: Optional[str]
    created_at_ms: int
    nodes: tuple[RevisionNode, ...] = ()
    edges: tuple[RevisionEdge, ...] = ()

    @property
    def content_hash(self) -> str:
        return self.graph_hash


@dataclass(frozen=True)
class EffectTransaction:
    effect_id: str
    transaction_id: str
    revision: int
    node_id: str
    operation_id: str
    adapter_id: str
    phase: str
    semantics: Mapping[str, Any]
    prepared: Optional[Mapping[str, Any]]
    preview: Optional[Mapping[str, Any]]
    preview_hash: Optional[str]
    authority: Optional[Mapping[str, Any]]
    result: Optional[Mapping[str, Any]]
    verification: Optional[Mapping[str, Any]]
    reconciliation: Optional[Mapping[str, Any]]
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class TransactionEvent:
    event_id: str
    transaction_id: str
    kind: str
    effect_id: Optional[str]
    payload: Mapping[str, Any]
    idempotency_key: str
    created_at_ms: int


@dataclass(frozen=True)
class TransactionSnapshot:
    """One consistent read of a transaction and its durable history."""

    transaction: ActionTransaction
    revisions: tuple[TransactionRevision, ...] = ()
    effects: tuple[EffectTransaction, ...] = ()
    events: tuple[TransactionEvent, ...] = field(default=())


# ── Adapter SDK value objects (plan Task 2) ─────────────────────────────
# Frozen requests/results exchanged between the coordinator and effect
# adapters. Adapters never mutate transaction status directly; they only
# return these values.

@dataclass(frozen=True)
class EffectSemantics:
    """Declared truth about one effect's reversibility and certainty."""

    fidelity: str  # exact | semantic | none
    reconciliation: str  # none | query
    idempotency: str  # none | keyed | native
    irreversible_after: str  # never | dispatch | commit
    compensation_window_seconds: Optional[int] = None

    def __post_init__(self):
        _validate_member(self.fidelity, COMPENSATION_FIDELITIES, "fidelity")
        _validate_member(
            self.reconciliation, frozenset({"none", "query"}), "reconciliation"
        )
        _validate_member(
            self.idempotency, frozenset({"none", "keyed", "native"}), "idempotency"
        )
        _validate_member(
            self.irreversible_after,
            frozenset({"never", "dispatch", "commit"}),
            "irreversible_after",
        )


@dataclass(frozen=True)
class EffectContext:
    """Trusted execution facts the coordinator hands every adapter call."""

    transaction_id: str
    revision: int
    node_id: str
    profile: str = "default"
    workspace_root: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedEffect:
    """Adapter-normalized node spec: canonical args and resource keys."""

    node_id: str
    adapter_id: str
    action: str
    args: Mapping[str, Any]
    resource_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedEffect:
    """Durable before-state and declared facts captured by prepare()."""

    node_id: str
    adapter_id: str
    action: str
    args: Mapping[str, Any]
    resources: tuple[str, ...]
    semantics: EffectSemantics
    # Canonical dotted autonomy action class (e.g. "workspace.write").
    # "unknown.mutation" is the conservative fail-closed default.
    action_class: str = "unknown.mutation"
    before: Mapping[str, Any] = field(default_factory=dict)
    expected_after: Optional[Mapping[str, Any]] = None
    prepared_token: Mapping[str, Any] = field(default_factory=dict)
    recipient_classes: tuple[str, ...] = ()
    recipient_hashes: tuple[str, ...] = ()
    data_classes: tuple[str, ...] = ()
    cost_usd_micros: int = 0
    uncertainty_ppm: int = 0
    required_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class EffectPreview:
    """Redacted human-facing preview; never carries credentials."""

    node_id: str
    summary: str
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    resources: tuple[str, ...]
    semantics: EffectSemantics
    requires_approval: bool
    uncertainty_ppm: int = 0


@dataclass(frozen=True)
class CommitRequest:
    """Single-use commit instruction for one prepared effect.

    ``invoke`` optionally carries the existing terminal tool handler so
    adapters can delegate the actual mutation; it must be called at most
    once.
    """

    prepared: PreparedEffect
    operation_id: str
    idempotency_key: str
    invoke: Optional[Any] = None


@dataclass(frozen=True)
class CommitOutcome:
    """Raw adapter commit result persisted before any success is reported."""

    status: str  # committed | failed
    result: Mapping[str, Any]
    evidence: Mapping[str, Any]
    error: Optional[str] = None

    def __post_init__(self):
        _validate_member(
            self.status, frozenset({"committed", "failed"}), "commit status"
        )


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    evidence: Mapping[str, Any]
    reason: str = ""


@dataclass(frozen=True)
class ReconciliationResult:
    disposition: str  # landed | not_landed | unknown
    evidence: Mapping[str, Any]

    def __post_init__(self):
        _validate_member(
            self.disposition, RECONCILE_DISPOSITIONS, "reconcile disposition"
        )


@dataclass(frozen=True)
class CompensationRequest:
    effect_id: str
    prepared: PreparedEffect
    verified_result_hash: str
    cascade_plan_hash: Optional[str] = None


@dataclass(frozen=True)
class CompensationResult:
    fidelity: str  # exact | semantic | none
    status: str  # compensated | failed | blocked
    evidence: Mapping[str, Any]
    error: Optional[str] = None

    def __post_init__(self):
        _validate_member(self.fidelity, COMPENSATION_FIDELITIES, "fidelity")
        _validate_member(
            self.status,
            frozenset({"compensated", "failed", "blocked"}),
            "compensation status",
        )


# ── Graph input normalization (storage-shape only; DAG rules live in
#    agent/effects/graph.py) ─────────────────────────────────────────────

def normalize_graph_input(graph: Mapping[str, Any]) -> tuple[
    tuple[RevisionNode, ...], tuple[RevisionEdge, ...]
]:
    """Validate the minimal storage shape of a graph mapping.

    Accepts ``{"nodes": [...], "edges": [...]}`` and returns frozen node and
    edge tuples. Full DAG semantics (cycles, endpoints, adapter existence)
    are owned by ``agent.effects.graph``; storage only refuses records it
    cannot persist faithfully.
    """
    if not isinstance(graph, Mapping):
        raise ValueError("graph must be a mapping with 'nodes' and 'edges'")
    raw_nodes = graph.get("nodes")
    if not raw_nodes:
        raise ValueError("graph must contain at least one node")
    nodes: list[RevisionNode] = []
    seen: set[str] = set()
    for raw in raw_nodes:
        node_id = raw.get("node_id")
        adapter_id = raw.get("adapter_id")
        action = raw.get("action")
        if not node_id or not adapter_id or not action:
            raise ValueError("every node needs node_id, adapter_id, and action")
        if node_id in seen:
            raise ValueError(f"duplicate node_id {node_id!r}")
        seen.add(node_id)
        args = raw.get("args") or {}
        if not isinstance(args, Mapping):
            raise ValueError(f"node {node_id!r} args must be a mapping")
        resource_keys = tuple(str(key) for key in raw.get("resource_keys") or ())
        nodes.append(
            RevisionNode(
                node_id=str(node_id),
                adapter_id=str(adapter_id),
                action=str(action),
                args=json.loads(canonical_json(args)),
                resource_keys=resource_keys,
            )
        )
    edges: list[RevisionEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    for raw in graph.get("edges") or ():
        parent = raw.get("parent") or raw.get("parent_node_id")
        child = raw.get("child") or raw.get("child_node_id")
        if not parent or not child:
            raise ValueError("every edge needs parent and child node ids")
        if parent not in seen or child not in seen:
            raise ValueError(f"edge {parent!r}->{child!r} references unknown node")
        key = (str(parent), str(child))
        if key in seen_edges:
            raise ValueError(f"duplicate edge {parent!r}->{child!r}")
        seen_edges.add(key)
        edges.append(RevisionEdge(parent_node_id=str(parent), child_node_id=str(child)))
    nodes.sort(key=lambda node: node.node_id)
    edges.sort(key=lambda edge: (edge.parent_node_id, edge.child_node_id))
    return tuple(nodes), tuple(edges)


def graph_content_hash(
    nodes: tuple[RevisionNode, ...], edges: tuple[RevisionEdge, ...]
) -> str:
    return content_hash({
        "nodes": [node.canonical() for node in nodes],
        "edges": [
            {"parent": edge.parent_node_id, "child": edge.child_node_id}
            for edge in edges
        ],
    })
