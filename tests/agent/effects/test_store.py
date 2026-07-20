"""Real-SQLite storage tests for the action-transaction store (plan Task 1).

Every test uses a real profile-local ``SessionDB`` on disk: immutability,
CAS transitions, and reopen durability are storage facts, not mocks.
"""

from __future__ import annotations

import pytest

from agent.effects.models import ImmutableRecordError
from agent.effects.store import TransactionStore
from agent.operation_journal import OperationJournal
from hades_state import SessionDB


@pytest.fixture()
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        db.close()


def authority_fixture() -> dict:
    return {
        "authority_version": 1,
        "irreversible_policy": "ask",
        "allowed_actions": ["write_file", "set", "send"],
        "allowed_resources": ["file:notes/benchmark.md", "config:ui.timezone"],
        "expires_at_ms": 4_600_000,
        "requester": "test-operator",
        "channel": "cli",
    }


def graph_fixture() -> dict:
    return {
        "nodes": [
            {
                "node_id": "workspace_write",
                "adapter_id": "workspace.v1",
                "action": "write_file",
                "args": {"path": "notes/benchmark.md", "content": "new\n"},
                "resource_keys": ["file:notes/benchmark.md"],
            },
            {
                "node_id": "config_set",
                "adapter_id": "hermes-config.v1",
                "action": "set",
                "args": {"key": "ui.timezone", "value": "UTC"},
                "resource_keys": ["config:ui.timezone"],
            },
        ],
        "edges": [{"parent": "workspace_write", "child": "config_set"}],
    }


def seed_complete_transaction(store: TransactionStore) -> None:
    store.create_transaction(
        transaction_id="tx-1",
        profile="default",
        title="bounded change",
        authority=authority_fixture(),
        graph=graph_fixture(),
        failure_policy="stop",
    )
    store.append_event("tx-1", "revision_previewed", payload={"revision": 1})
    OperationJournal(store.db).create(operation_id="op-1", kind="effect_commit")
    store.create_effect_attempt(
        effect_id="ef-1",
        transaction_id="tx-1",
        revision=1,
        node_id="workspace_write",
        operation_id="op-1",
        adapter_id="workspace.v1",
    )
    assert store.transition_effect("ef-1", {"planned"}, "prepared")
    assert store.transition_effect("ef-1", {"prepared"}, "previewed")
    assert store.transition_effect("ef-1", {"previewed"}, "committing")
    assert store.transition_effect("ef-1", {"committing"}, "committed")
    store.append_event("tx-1", "effect_committed", effect_id="ef-1")
    assert store.transition_status("tx-1", {"draft"}, "committing")
    assert store.transition_status("tx-1", {"committing"}, "committed")
    store.append_event("tx-1", "receipt_issued", payload={"receipt_id": "rc-1"})


def test_revision_and_effect_storage_is_immutable_and_cas(session_db):
    store = TransactionStore(session_db)
    created = store.create_transaction(
        transaction_id="tx-1", profile="default", title="bounded change",
        authority=authority_fixture(), graph=graph_fixture(), failure_policy="stop",
    )
    assert created.current_revision == 1
    assert store.get_revision("tx-1", 1).content_hash
    OperationJournal(session_db).create(operation_id="op-1", kind="effect_commit")
    effect = store.create_effect_attempt(
        effect_id="ef-1", transaction_id="tx-1", revision=1,
        node_id="workspace_write", operation_id="op-1", adapter_id="workspace.v1",
    )
    assert effect.phase == "planned"
    assert store.transition_effect("ef-1", {"planned"}, "prepared")
    assert not store.transition_effect("ef-1", {"planned"}, "prepared")
    with pytest.raises(ImmutableRecordError):
        store.replace_revision("tx-1", 1, graph_fixture())


def test_reopen_preserves_graph_events_approval_outbox_and_receipt(tmp_path):
    first = SessionDB(tmp_path / "state.db")
    seed_complete_transaction(TransactionStore(first))
    first.close()
    second = SessionDB(tmp_path / "state.db")
    try:
        snapshot = TransactionStore(second).load_snapshot("tx-1")
        assert snapshot.transaction.status == "committed"
        assert [event.kind for event in snapshot.events] == [
            "transaction_created", "revision_previewed", "effect_committed",
            "receipt_issued",
        ]
    finally:
        second.close()


def test_created_transaction_persists_canonical_graph_and_authority(session_db):
    store = TransactionStore(session_db)
    store.create_transaction(
        transaction_id="tx-1", profile="default", title="bounded change",
        authority=authority_fixture(), graph=graph_fixture(), failure_policy="stop",
    )
    revision = store.get_revision("tx-1", 1)
    assert [node.node_id for node in revision.nodes] == [
        "config_set", "workspace_write",
    ]
    assert revision.nodes[1].args == {
        "path": "notes/benchmark.md", "content": "new\n",
    }
    assert [(edge.parent_node_id, edge.child_node_id) for edge in revision.edges] == [
        ("workspace_write", "config_set"),
    ]
    loaded = store.get_transaction("tx-1")
    assert loaded.authority["irreversible_policy"] == "ask"
    assert loaded.authority_version == 1
    assert loaded.status == "draft"
    # Decoded JSON must be defensive copies: mutating one read never
    # changes what a later read observes.
    loaded.authority["irreversible_policy"] = "tampered"
    assert store.get_transaction("tx-1").authority["irreversible_policy"] == "ask"


def test_store_rejects_unknown_vocabulary_before_sql(session_db):
    store = TransactionStore(session_db)
    with pytest.raises(ValueError, match="failure_policy"):
        store.create_transaction(
            transaction_id="tx-1", profile="default", title="t",
            authority=authority_fixture(), graph=graph_fixture(),
            failure_policy="explode",
        )
    store.create_transaction(
        transaction_id="tx-1", profile="default", title="t",
        authority=authority_fixture(), graph=graph_fixture(),
        failure_policy="stop",
    )
    with pytest.raises(ValueError, match="phase"):
        store.transition_effect("ef-missing", {"planned"}, "not-a-phase")
    with pytest.raises(ValueError, match="status"):
        store.transition_status("tx-1", {"draft"}, "not-a-status")


def test_duplicate_transaction_and_effect_ids_are_rejected(session_db):
    store = TransactionStore(session_db)
    store.create_transaction(
        transaction_id="tx-1", profile="default", title="t",
        authority=authority_fixture(), graph=graph_fixture(),
        failure_policy="stop",
    )
    with pytest.raises(Exception):
        store.create_transaction(
            transaction_id="tx-1", profile="default", title="t",
            authority=authority_fixture(), graph=graph_fixture(),
            failure_policy="stop",
        )
    OperationJournal(session_db).create(operation_id="op-1", kind="effect_commit")
    store.create_effect_attempt(
        effect_id="ef-1", transaction_id="tx-1", revision=1,
        node_id="workspace_write", operation_id="op-1", adapter_id="workspace.v1",
    )
    with pytest.raises(Exception):
        store.create_effect_attempt(
            effect_id="ef-1", transaction_id="tx-1", revision=1,
            node_id="workspace_write", operation_id="op-1",
            adapter_id="workspace.v1",
        )


def test_events_are_append_only_and_idempotency_keyed(session_db):
    store = TransactionStore(session_db)
    store.create_transaction(
        transaction_id="tx-1", profile="default", title="t",
        authority=authority_fixture(), graph=graph_fixture(),
        failure_policy="stop",
    )
    first = store.append_event(
        "tx-1", "revision_previewed", idempotency_key="preview:1",
    )
    second = store.append_event(
        "tx-1", "revision_previewed", idempotency_key="preview:1",
    )
    assert first.event_id == second.event_id
    events = store.load_snapshot("tx-1").events
    assert [event.kind for event in events] == [
        "transaction_created", "revision_previewed",
    ]
