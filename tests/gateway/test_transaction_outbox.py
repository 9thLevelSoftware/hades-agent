"""Transaction outbox dispatch-certainty tests (plan Task 8).

Proves the delivery boundary is truthful: a send whose durable
confirmation cannot be persisted surfaces ``DeliveryEffectUnknown``
instead of success, and platform edit/delete capabilities are detected
from concrete method overrides, never optimistic platform names.
"""

from __future__ import annotations

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.delivery import (
    DeliveryEffectUnknown,
    DeliveryRouter,
    DeliveryTarget,
)
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.transaction_outbox import TransactionOutboxDispatcher
from hades_state import SessionDB
from agent.operation_journal import OperationJournal
from tests.gateway.test_delivery_operation_journal import RecordingAdapter


class _ConfirmFailJournal(OperationJournal):
    """Real journal that fails ONLY the confirmed/landed persist."""

    def transition(self, operation_id, *, from_states, to_state,
                   effect_disposition, result=None, error=None):
        if to_state == "confirmed" and effect_disposition == "landed":
            raise OSError("disk full while persisting confirmation")
        return super().transition(
            operation_id, from_states=from_states, to_state=to_state,
            effect_disposition=effect_disposition, result=result, error=error,
        )


@pytest.mark.asyncio
async def test_unpersistable_confirmation_raises_delivery_effect_unknown(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("gateway.delivery.get_hades_home", lambda: tmp_path)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        journal = _ConfirmFailJournal(db)
        adapter = RecordingAdapter(message_id="msg-9")
        router = DeliveryRouter(
            GatewayConfig(),
            adapters={Platform.TELEGRAM: adapter},
            journal=journal,
        )
        target = DeliveryTarget.parse("telegram:123")
        with pytest.raises(DeliveryEffectUnknown) as excinfo:
            await router._deliver_to_platform(
                target, "hello", metadata={"delivery_id": "delivery-9"},
            )
        # The bounded acknowledgement travels with the ambiguity; the
        # send DID happen exactly once.
        assert excinfo.value.acknowledgement.get("message_id") == "msg-9"
        assert len(adapter.calls) == 1
        # The journal row is quarantined as unknown — a restart can never
        # blind-resend it.
        record = journal.get("delivery-9")
        assert record.state == "unknown"
        assert record.effect_disposition == "unknown"
    finally:
        db.close()


def test_edit_delete_capabilities_come_from_concrete_overrides():
    class _Bare(BasePlatformAdapter):
        async def connect(self, *, is_reconnect: bool = False) -> bool:
            return True

        async def disconnect(self) -> None:
            return None

        async def send(self, chat_id, content, metadata=None):
            return SendResult(success=True)

        async def get_chat_info(self, chat_id):
            return {}

    class _Editing(_Bare):
        async def edit_message(self, chat_id, message_id, content, *,
                               finalize=False):
            return SendResult(success=True)

    bare = _Bare.__new__(_Bare)
    editing = _Editing.__new__(_Editing)
    assert bare.supports_message_edit is False
    assert bare.supports_message_delete is False
    assert editing.supports_message_edit is True
    assert editing.supports_message_delete is False


def test_transaction_dispatcher_reuses_leased_claim_semantics(tmp_path):
    from gateway.mission_outbox import MissionOutboxStore

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        journal = OperationJournal(db)
        dispatcher = TransactionOutboxDispatcher(
            store=store,
            router=object(),
            journal=journal,
            owner_id="test-owner",
            workflow_db_path=tmp_path / "workflows.db",
        )
        assert dispatcher.DEFAULT_DRAIN_LIMIT == 20
        assert hasattr(dispatcher, "drain_transactions")
    finally:
        db.close()
