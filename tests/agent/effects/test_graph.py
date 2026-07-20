"""Revision truth-table and DAG tests for action transactions (plan Task 3).

Frozen nodes are immutable facts: no later revision may remove them,
change their spec, or rewrite their incoming causality. Pending work is
freely revisable under optimistic CAS.
"""

from __future__ import annotations

import pytest

from agent.effects.graph import (
    GraphCycleError,
    GraphValidationError,
    create_revision,
    reverse_compensation_order,
    topological_order,
    validate_graph,
)
from agent.effects.models import RevisionConflict
from agent.effects.store import TransactionStore
from agent.operation_journal import OperationJournal
from hades_state import SessionDB


@pytest.fixture()
def store(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield TransactionStore(db)
    finally:
        db.close()


def _node(node_id, action="write_file", adapter="workspace.v1", **args):
    return {
        "node_id": node_id,
        "adapter_id": adapter,
        "action": action,
        "args": args or {"path": f"{node_id}.txt"},
        "resource_keys": [f"file:{node_id}"],
    }


def _authority():
    return {"authority_version": 1, "irreversible_policy": "ask"}


def _create(store, graph, transaction_id="tx-1"):
    return store.create_transaction(
        transaction_id=transaction_id, profile="default", title="t",
        authority=_authority(), graph=graph, failure_policy="stop",
    )


_PHASE_CHAINS = {
    "planned": (),
    "prepared": ("prepared",),
    "previewed": ("prepared", "previewed"),
    "committing": ("prepared", "previewed", "committing"),
    "committed": ("prepared", "previewed", "committing", "committed"),
    "verified": ("prepared", "previewed", "committing", "committed", "verified"),
    "compensating": (
        "prepared", "previewed", "committing", "committed", "compensating",
    ),
    "compensated": (
        "prepared", "previewed", "committing", "committed", "compensating",
        "compensated",
    ),
    "unknown_effect": ("prepared", "previewed", "committing", "unknown_effect"),
    "blocked": ("blocked",),
    "failed": ("prepared", "previewed", "committing", "failed"),
}


def _drive_effect(store, transaction_id, revision, node_id, phase, seq=(0,)):
    seq = (seq[0] + 1,)
    operation_id = f"op-{transaction_id}-{revision}-{node_id}"
    effect_id = f"ef-{transaction_id}-{revision}-{node_id}"
    OperationJournal(store.db).create(operation_id=operation_id, kind="effect_commit")
    store.create_effect_attempt(
        effect_id=effect_id, transaction_id=transaction_id, revision=revision,
        node_id=node_id, operation_id=operation_id, adapter_id="workspace.v1",
    )
    current = "planned"
    for step in _PHASE_CHAINS[phase]:
        assert store.transition_effect(effect_id, {current}, step)
        current = step


def seed_revision_with_phase(store, transaction_id, node_id, phase):
    graph = {
        "nodes": [_node(node_id), _node("other")],
        "edges": [{"parent": node_id, "child": "other"}],
    }
    _create(store, graph, transaction_id)
    _drive_effect(store, transaction_id, 1, node_id, phase)
    return graph


def graph_without_node(node_id):
    return {"nodes": [_node("other")], "edges": []}


def seed_mixed_graph(store):
    graph = {
        "nodes": [
            _node("write"),
            _node("message", action="send", adapter="message-outbox.v1",
                  target="chat:1", message="first"),
        ],
        "edges": [{"parent": "write", "child": "message"}],
    }
    _create(store, graph)
    _drive_effect(store, "tx-1", 1, "write", "committed")
    _drive_effect(store, "tx-1", 1, "message", "prepared")
    return graph


def graph_with_changed_pending_message_and_new_audit_node():
    return {
        "nodes": [
            _node("write"),
            _node("message", action="send", adapter="message-outbox.v1",
                  target="chat:2", message="second recipient"),
            _node("audit"),
        ],
        "edges": [
            {"parent": "write", "child": "message"},
            {"parent": "message", "child": "audit"},
        ],
    }


def seed_committed_parent(store):
    graph = {
        "nodes": [_node("parent"), _node("child")],
        "edges": [{"parent": "parent", "child": "child"}],
    }
    _create(store, graph)
    _drive_effect(store, "tx-1", 1, "parent", "committed")
    return graph


def cyclic_graph():
    return {
        "nodes": [_node("parent"), _node("child")],
        "edges": [
            {"parent": "parent", "child": "child"},
            {"parent": "child", "child": "parent"},
        ],
    }


def valid_graph():
    return {
        "nodes": [_node("parent"), _node("child"), _node("extra")],
        "edges": [{"parent": "parent", "child": "child"}],
    }


def graph_adding_parent_to_committed():
    return {
        "nodes": [_node("parent"), _node("child"), _node("newparent")],
        "edges": [
            {"parent": "parent", "child": "child"},
            {"parent": "newparent", "child": "parent"},
        ],
    }


# ── Pure graph algorithms ───────────────────────────────────────────────


def test_topological_order_is_deterministic_across_input_order():
    graph = {
        "nodes": [_node("b"), _node("a"), _node("c"), _node("root")],
        "edges": [
            {"parent": "root", "child": "a"},
            {"parent": "root", "child": "b"},
            {"parent": "root", "child": "c"},
        ],
    }
    shuffled = {
        "nodes": list(reversed(graph["nodes"])),
        "edges": list(reversed(graph["edges"])),
    }
    assert topological_order(graph) == ["root", "a", "b", "c"]
    assert topological_order(shuffled) == ["root", "a", "b", "c"]


def test_reverse_compensation_order_restricts_to_selection():
    graph = {
        "nodes": [_node("a"), _node("b"), _node("c"), _node("d")],
        "edges": [
            {"parent": "a", "child": "b"},
            {"parent": "b", "child": "c"},
            {"parent": "c", "child": "d"},
        ],
    }
    assert reverse_compensation_order(graph, {"a", "b", "c", "d"}) == [
        "d", "c", "b", "a",
    ]
    assert reverse_compensation_order(graph, {"b", "d"}) == ["d", "b"]


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (
            lambda g: (g["nodes"][0].update(node_id="Bad_Upper"),
                       g["edges"].clear()),
            "node id",
        ),
        (lambda g: g["edges"].append({"parent": "a", "child": "a"}), "self-edge"),
        (
            lambda g: g["edges"].append({"parent": "ghost", "child": "a"}),
            "unknown node",
        ),
        (lambda g: g["nodes"].clear(), "at least one node"),
    ],
)
def test_validate_graph_rejects_malformed_shapes(mutation, match):
    graph = {
        "nodes": [_node("a"), _node("b")],
        "edges": [{"parent": "a", "child": "b"}],
    }
    mutation(graph)
    with pytest.raises((GraphValidationError, ValueError), match=match):
        validate_graph(graph)


def test_validate_graph_rejects_cycles():
    with pytest.raises(GraphCycleError):
        validate_graph(cyclic_graph())


# ── Revision truth table ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phase",
    ["committed", "verified", "compensating", "compensated", "unknown_effect"],
)
def test_revision_cannot_remove_or_change_frozen_node(store, phase):
    seed_revision_with_phase(store, "tx-1", "write", phase)
    changed = graph_without_node("write")
    with pytest.raises(RevisionConflict, match="frozen node write"):
        create_revision(store, "tx-1", expected_revision=1, graph=changed,
                        reason="change")


def test_revision_supersedes_prepared_attempt_and_preserves_committed_causality(store):
    seed_mixed_graph(store)
    revised = graph_with_changed_pending_message_and_new_audit_node()
    record = create_revision(store, "tx-1", expected_revision=1, graph=revised,
                             reason="new recipient")
    assert record.revision == 2
    assert store.effect_for("tx-1", 1, "message").phase == "superseded"
    assert store.get_node("tx-1", 2, "write") == store.get_node("tx-1", 1, "write")
    assert topological_order(revised) == ["write", "message", "audit"]


def test_revision_rejects_cycle_stale_cas_and_new_parent_for_committed_node(store):
    seed_committed_parent(store)
    with pytest.raises(GraphCycleError):
        create_revision(store, "tx-1", 1, cyclic_graph(), "cycle")
    create_revision(store, "tx-1", 1, valid_graph(), "advance")
    with pytest.raises(RevisionConflict, match="expected revision 1, current 2"):
        create_revision(store, "tx-1", 1, valid_graph(), "stale")
    with pytest.raises(RevisionConflict, match="incoming edges of committed node"):
        create_revision(store, "tx-1", 2, graph_adding_parent_to_committed(),
                        "rewrite history")


def test_revision_cas_appends_event_and_resets_status_atomically(store):
    seed_mixed_graph(store)
    assert store.transition_status("tx-1", {"draft"}, "ready")
    record = create_revision(
        store, "tx-1", expected_revision=1,
        graph=graph_with_changed_pending_message_and_new_audit_node(),
        reason="edit",
    )
    assert record.revision == 2
    transaction = store.get_transaction("tx-1")
    assert transaction.current_revision == 2
    assert transaction.status == "draft"
    events = [event.kind for event in store.load_snapshot("tx-1").events]
    assert "revision_created" in events


def test_frozen_node_edge_between_frozen_nodes_cannot_be_removed(store):
    graph = {
        "nodes": [_node("first"), _node("second")],
        "edges": [{"parent": "first", "child": "second"}],
    }
    _create(store, graph)
    _drive_effect(store, "tx-1", 1, "first", "committed")
    _drive_effect(store, "tx-1", 1, "second", "committed")
    stripped = {
        "nodes": [_node("first"), _node("second")],
        "edges": [],
    }
    with pytest.raises(RevisionConflict, match="frozen"):
        create_revision(store, "tx-1", 1, stripped, "strip edge")


# ── Review round 2: revise/commit race is atomic at the store ────────────


def test_revision_conflicts_when_phases_drift_after_validation(store):
    seed_mixed_graph(store)
    phases_snapshot = store.latest_effect_phases("tx-1")
    # A racing commit moves the pending node to committing AFTER the
    # caller validated but BEFORE the install.
    assert store.transition_effect("ef-tx-1-1-message", {"prepared"}, "previewed")
    assert store.transition_effect("ef-tx-1-1-message", {"previewed"}, "committing")
    from agent.effects.models import normalize_graph_input

    nodes, edges = normalize_graph_input(
        graph_with_changed_pending_message_and_new_audit_node()
    )
    with pytest.raises(RevisionConflict, match="phases changed"):
        store.create_revision(
            transaction_id="tx-1", expected_revision=1,
            nodes=nodes, edges=edges, reason="stale phases",
            expected_phases=phases_snapshot,
        )
    # No partial writes: still at revision 1.
    assert store.get_transaction("tx-1").current_revision == 1


def test_revision_refuses_mid_commit_transaction(store):
    seed_mixed_graph(store)
    assert store.transition_status("tx-1", {"draft"}, "previewing")
    assert store.transition_status("tx-1", {"previewing"}, "ready")
    assert store.transition_status("tx-1", {"ready"}, "committing")
    with pytest.raises(RevisionConflict, match="committing"):
        create_revision(
            store, "tx-1", 1,
            graph_with_changed_pending_message_and_new_audit_node(),
            "mid-commit revise",
        )
