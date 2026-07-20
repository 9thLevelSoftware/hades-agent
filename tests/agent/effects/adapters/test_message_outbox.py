"""Delayed outbox adapter tests (plan Task 8).

The node's effect is one durable delayed outbox row: revisable and
cancellable before release, truthfully irreversible after dispatch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.effects.adapters.message_outbox import MessageOutboxAdapter
from agent.effects.coordinator import (
    TransactionCoordinator,
    prepared_from_json,
)
from agent.effects.models import CompensationRequest, EffectContext
from agent.effects.registry import EffectAdapterRegistry
from agent.effects.store import TransactionStore
from agent.operation_journal import OperationJournal
from gateway.mission_outbox import MissionOutboxStore
from hades_state import SessionDB


class _AllowAll:
    def authorize(self, context, *, consume):
        return SimpleNamespace(
            allowed=True, verdict="allow", code="allow", context_hash="ctx",
        )


class OutboxHarness:
    def __init__(self, tmp_path):
        self.db = SessionDB(tmp_path / "state.db")
        self.store = TransactionStore(self.db)
        self.journal = OperationJournal(self.db)
        self.outbox = MissionOutboxStore(self.db)
        self.adapter = MessageOutboxAdapter(db_factory=lambda: self.db)
        self.adapters = EffectAdapterRegistry()
        self.adapters.register(self.adapter)
        self.coordinator = TransactionCoordinator(
            store=self.store,
            adapters=self.adapters,
            journal=self.journal,
            authority_provider_factory=_AllowAll,
        )

    def close(self):
        self.db.close()

    def create(self, transaction_id="tx-1", message="first", delay=30):
        self.store.create_transaction(
            transaction_id=transaction_id, profile="default", title="send",
            authority={"authority_version": 1, "irreversible_policy": "ask"},
            graph={
                "nodes": [{
                    "node_id": "send", "adapter_id": "message-outbox.v1",
                    "action": "send",
                    "args": {
                        "platform": "faketest",
                        "target": "faketest:chan",
                        "message": message,
                        "not_before_seconds": delay,
                    },
                }],
                "edges": [],
            },
            failure_policy="stop",
        )

    def outbox_row(self, transaction_id="tx-1"):
        effect = self.store.effect_for(transaction_id, 1, "send")
        token = (effect.prepared or {}).get("prepared_token") or {}
        return self.outbox.get_by_id(token["outbox_id"]), effect

    def compensate(self, transaction_id="tx-1"):
        effect = self.store.effect_for(transaction_id, 1, "send")
        return self.adapter.compensate(
            CompensationRequest(
                effect_id=effect.effect_id,
                prepared=prepared_from_json(effect.prepared),
                verified_result_hash="",
            ),
            EffectContext(
                transaction_id=transaction_id, revision=1, node_id="send",
            ),
        )


@pytest.fixture()
def harness(tmp_path):
    h = OutboxHarness(tmp_path)
    try:
        yield h
    finally:
        h.close()


def test_commit_enqueues_delayed_row_awaiting_approval(harness):
    harness.create(delay=3600)
    preview = harness.coordinator.preview("tx-1")
    assert preview.status == "ready"
    effect = harness.store.effect_for("tx-1", 1, "send")
    assert "irreversible after" in effect.preview["summary"].replace("\n", " ")
    # Prepare/preview created no outbox row — no outward effect.
    row, _ = harness.outbox_row()
    assert row is None

    assert harness.coordinator.commit("tx-1").status == "committed"
    row, effect = harness.outbox_row()
    assert row is not None
    assert row.status == "pending_approval"
    assert row.not_before > 0
    assert effect.phase == "verified"


def test_outbox_can_revise_before_release_but_not_after_dispatch(harness):
    harness.create(message="first", delay=3600)
    harness.coordinator.preview("tx-1")
    harness.coordinator.commit("tx-1")
    row, _ = harness.outbox_row()

    revised = harness.outbox.revise(
        row.outbox_id, content={"message": "final"},
        expected_revision=row.revision, not_before=row.not_before,
    )
    assert revised is not None
    assert revised.revision == row.revision + 1
    assert revised.content_hash != row.content_hash

    # Simulate dispatch: force the row terminal, then revision must fail.
    def _force_delivered(conn):
        conn.execute(
            "UPDATE mission_outbox SET status = 'delivered' WHERE outbox_id = ?",
            (row.outbox_id,),
        )
        return True

    harness.db._execute_write(_force_delivered)
    late = harness.outbox.revise(
        row.outbox_id, content={"message": "late"},
        expected_revision=revised.revision, not_before=row.not_before,
    )
    assert late is None
    stored = harness.outbox.get_by_id(row.outbox_id)
    assert stored.status == "delivered"
    assert stored.content_hash == revised.content_hash


def test_cancellation_before_release_is_semantic_compensation(harness):
    harness.create(delay=3600)
    harness.coordinator.preview("tx-1")
    harness.coordinator.commit("tx-1")
    result = harness.compensate()
    assert result.status == "compensated"
    assert result.fidelity == "semantic"
    row, _ = harness.outbox_row()
    assert row.status == "cancelled"
    # Idempotent: a second compensation reports the same terminal truth.
    again = harness.compensate()
    assert again.status == "compensated"


def test_dispatched_message_is_truthfully_irreversible(harness):
    harness.create(delay=3600)
    harness.coordinator.preview("tx-1")
    harness.coordinator.commit("tx-1")
    row, _ = harness.outbox_row()

    def _force_delivered(conn):
        conn.execute(
            "UPDATE mission_outbox SET status = 'delivered' WHERE outbox_id = ?",
            (row.outbox_id,),
        )
        return True

    harness.db._execute_write(_force_delivered)
    result = harness.compensate()
    assert result.status == "blocked"
    assert "irreversible" in (result.error or "")


def test_reconcile_reads_durable_row_truthfully(harness):
    harness.create(delay=3600)
    harness.coordinator.preview("tx-1")
    harness.coordinator.commit("tx-1")
    effect = harness.store.effect_for("tx-1", 1, "send")
    context = EffectContext(transaction_id="tx-1", revision=1, node_id="send")
    assert harness.adapter.reconcile(effect, context).disposition == "landed"

    row, _ = harness.outbox_row()

    def _drop_row(conn):
        conn.execute(
            "DELETE FROM mission_outbox WHERE outbox_id = ?", (row.outbox_id,),
        )
        return True

    harness.db._execute_write(_drop_row)
    assert harness.adapter.reconcile(effect, context).disposition == "not_landed"


def test_release_requires_consumed_exact_approval(harness):
    from gateway.transaction_outbox import release_transaction_outbox

    harness.create(delay=3600)
    harness.coordinator.preview("tx-1")
    harness.coordinator.commit("tx-1")
    row, _ = harness.outbox_row()

    denied = SimpleNamespace(approved=False, code="missing")
    assert release_transaction_outbox(
        harness.outbox, row.outbox_id, approval=denied,
    ) is False
    assert harness.outbox.get_by_id(row.outbox_id).status == "pending_approval"

    approved = SimpleNamespace(approved=True, code="approved")
    assert release_transaction_outbox(
        harness.outbox, row.outbox_id, approval=approved,
    ) is True
    assert harness.outbox.get_by_id(row.outbox_id).status == "scheduled"


def test_normalize_rejects_malformed_sends(harness):
    for transaction_id, args in (
        ("tx-noplat", {"target": "t", "message": "m"}),
        ("tx-blank", {"platform": "faketest", "target": "t", "message": "  "}),
        (
            "tx-delay",
            {"platform": "faketest", "target": "t", "message": "m",
             "not_before_seconds": 0},
        ),
    ):
        harness.store.create_transaction(
            transaction_id=transaction_id, profile="default", title="bad",
            authority={"authority_version": 1},
            graph={
                "nodes": [{
                    "node_id": "send", "adapter_id": "message-outbox.v1",
                    "action": "send", "args": args,
                }],
                "edges": [],
            },
            failure_policy="stop",
        )
        assert harness.coordinator.preview(transaction_id).status == "blocked"
