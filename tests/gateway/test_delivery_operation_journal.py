"""Task 9: durable outbound delivery via OperationJournal.

The DeliveryRouter records each outbound delivery as an ``agent_operations``
row (kind=outbound_delivery) so a process restart does not cause duplicate
sends: a confirmed row is acknowledged, an in-flight row is reconciled to
``unknown`` (no auto-retry).

All journal access is opt-in: callers that don't supply a journal behave
exactly as before — and tests can use an in-memory SessionDB.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

from agent.operation_journal import OperationJournal
from gateway.config import GatewayConfig, Platform
from gateway.delivery import DeliveryRouter, DeliveryTarget
from gateway.platforms.base import SendResult
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# Recording adapter with deterministic ids
# ---------------------------------------------------------------------------


class RecordingAdapter:
    """Adapter that records every send() and can be steered from the test."""

    def __init__(self, success: bool = True, message_id: str = "msg-1"):
        self.calls: List[Dict[str, Any]] = []
        self._success = success
        self._message_id = message_id

    async def send(self, chat_id, content, metadata=None):
        self.calls.append(
            {"chat_id": chat_id, "content": content, "metadata": dict(metadata or {})}
        )
        if not self._success:
            return SendResult(success=False, error="nope", retryable=False)
        return SendResult(success=True, message_id=self._message_id)

    async def ensure_dm_topic(self, chat_id, topic_name, force_create=False):
        return "99999"


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _journal_for(tmp_path) -> OperationJournal:
    db = SessionDB(db_path=tmp_path / "state.db")
    return OperationJournal(db), db


# ---------------------------------------------------------------------------
# 1. confirmed dedup: a confirmed record short-circuits re-send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_record_short_circuits_resend(tmp_path, monkeypatch):
    """If the journal already has a confirmed row for this delivery_id +
    payload, do NOT call the adapter again — return the prior receipt."""
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)

    # Pre-seed a confirmed row (this is what a restart with a previous
    # successful send would look like — already terminal, not yet acknowledged).
    journal.create(
        operation_id="delivery-1",
        kind="outbound_delivery",
        destination="telegram:123",
        payload_hash=_hash("hello"),
    )
    journal.transition(
        "delivery-1",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "delivery-1",
        from_states={"running"},
        to_state="dispatched",
        effect_disposition="unknown",
    )
    journal.transition(
        "delivery-1",
        from_states={"dispatched"},
        to_state="confirmed",
        effect_disposition="landed",
        result={"message_id": "msg-1", "platform": "telegram"},
    )

    adapter = RecordingAdapter(message_id="msg-1")
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: adapter},
        journal=journal,
    )
    target = DeliveryTarget.parse("telegram:123")
    result = await router._deliver_to_platform(
        target,
        "hello",
        metadata={"delivery_id": "delivery-1"},
    )

    # Adapter was NOT called.
    assert adapter.calls == []
    # Receipt was returned from the journal, not from a fresh send.
    assert result["message_id"] == "msg-1"
    assert result["deduped"] is True
    db.close()


# ---------------------------------------------------------------------------
# 2. identity conflict (caller reused delivery_id with different content)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_conflict_raises_value_error(tmp_path, monkeypatch):
    """Same delivery_id but different payload_hash → ValueError, no send."""
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)

    journal.create(
        operation_id="delivery-2",
        kind="outbound_delivery",
        destination="telegram:123",
        payload_hash=_hash("hello"),
    )

    adapter = RecordingAdapter()
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: adapter},
        journal=journal,
    )
    target = DeliveryTarget.parse("telegram:123")

    with pytest.raises(ValueError, match="already identifies"):
        await router._deliver_to_platform(
            target,
            "DIFFERENT CONTENT",  # hash mismatch
            metadata={"delivery_id": "delivery-2"},
        )

    assert adapter.calls == []
    db.close()


# ---------------------------------------------------------------------------
# 3. pre-dispatch failure → failed/none (NOT unknown — we know it didn't go)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_dispatch_failure_records_failed_none(tmp_path, monkeypatch):
    """A sync exception before ``adapter.send()`` records failed/none so the
    retry path can deterministically know it didn't fly."""
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)

    adapter = RecordingAdapter()
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: adapter},
        journal=journal,
    )
    target = DeliveryTarget(platform=Platform.TELEGRAM, chat_id="123")

    # Force a pre-dispatch raise by passing an obviously broken target.
    target.chat_id = None  # DeliveryRouter raises ValueError before send()

    with pytest.raises(ValueError):
        await router._deliver_to_platform(
            target,
            "hello",
            metadata={"delivery_id": "delivery-pre"},
        )

    record = journal.get("delivery-pre")
    assert record is not None
    assert record.state == "failed"
    assert record.effect_disposition == "none"
    assert record.error is not None
    db.close()


# ---------------------------------------------------------------------------
# 4. cancel/timeout/process death after dispatch → unknown/unknown, no auto-retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_records_unknown_and_does_not_retry(
    tmp_path, monkeypatch
):
    """A caller-cancelled dispatch (e.g. task.cancel()) must end as
    ``unknown/unknown`` — the durable record says "I do not know if it flew".

    The next attempt must NOT be auto-retried from the router: the journal
    row blocks re-send, and the caller decides what to do.
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)

    adapter = RecordingAdapter()
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: adapter},
        journal=journal,
    )
    target = DeliveryTarget.parse("telegram:123")

    async def slow_send(*_a, **_kw):
        await asyncio.sleep(5)
        return SendResult(success=True, message_id="late")

    adapter.send = slow_send

    coro = router._deliver_to_platform(
        target,
        "hello",
        metadata={"delivery_id": "delivery-cancel"},
    )
    task = asyncio.create_task(coro)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = journal.get("delivery-cancel")
    assert record is not None
    assert record.state == "unknown"
    assert record.effect_disposition == "unknown"

    # And re-attempting with the same identity must NOT call the adapter again.
    adapter2 = RecordingAdapter()
    router2 = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: adapter2},
        journal=journal,
    )
    target2 = DeliveryTarget.parse("telegram:123")
    result = await router2._deliver_to_platform(
        target2,
        "hello",
        metadata={"delivery_id": "delivery-cancel"},
    )
    assert adapter2.calls == []
    assert result["deduped"] is True
    db.close()


# ---------------------------------------------------------------------------
# 5. restart keeps unknown as unknown — NO adapter call on retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_keeps_unknown_and_blocks_resend(tmp_path, monkeypatch):
    """After a process restart, ``reconcile_after_restart()`` leaves an
    in-flight row as ``unknown/unknown``. The new process must NOT
    auto-retry it — that would risk a duplicate send."""
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)

    # Simulate the previous process: created + dispatched, then died.
    journal.create(
        operation_id="delivery-restart",
        kind="outbound_delivery",
        destination="telegram:123",
        payload_hash=_hash("hello"),
    )
    journal.transition(
        "delivery-restart",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "delivery-restart",
        from_states={"running"},
        to_state="dispatched",
        effect_disposition="unknown",
    )

    # Restart reconciliation.
    n = journal.reconcile_after_restart()
    assert n == 1
    assert journal.get("delivery-restart").state == "unknown"

    # New process — adapter must not be called for the unknown row.
    adapter = RecordingAdapter()
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: adapter},
        journal=journal,
    )
    target = DeliveryTarget.parse("telegram:123")
    result = await router._deliver_to_platform(
        target,
        "hello",
        metadata={"delivery_id": "delivery-restart"},
    )
    assert adapter.calls == []
    assert result["deduped"] is True
    db.close()


# ---------------------------------------------------------------------------
# 6. silence-filtered send records confirmed/none (it never reaches the wire,
#    so there is nothing "landed", but the outcome is known and durable).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silence_filtered_send_records_confirmed_none(tmp_path, monkeypatch):
    """A silence-narration filter drop is a deterministic, known outcome
    (the message never flies). Record it as confirmed/none so the caller
    doesn't redeliver."""
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)

    adapter = RecordingAdapter()
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: adapter},
        journal=journal,
    )
    target = DeliveryTarget.parse("telegram:123")

    result = await router._deliver_to_platform(
        target,
        "*(silent)*",
        metadata={"delivery_id": "delivery-silence"},
    )

    # Adapter never saw it.
    assert adapter.calls == []
    assert result["filtered"] == "silence_narration"

    record = journal.get("delivery-silence")
    assert record is not None
    assert record.state == "confirmed"
    assert record.effect_disposition == "none"
    assert record.result_json is not None
    db.close()


# ---------------------------------------------------------------------------
# 7. happy path records pending → dispatched → confirmed/landed with receipt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_records_landed_receipt(tmp_path, monkeypatch):
    """A successful send persists a bounded receipt (no full content, no
    credentials) so a recovery can show "this fired and returned msg-42"
    without leaking the body."""
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)

    adapter = RecordingAdapter(message_id="msg-42")
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: adapter},
        journal=journal,
    )
    target = DeliveryTarget.parse("telegram:123")

    result = await router._deliver_to_platform(
        target,
        "hello",
        metadata={"delivery_id": "delivery-happy"},
    )

    # Adapter was called once.
    assert len(adapter.calls) == 1
    assert result.message_id == "msg-42"

    record = journal.get("delivery-happy")
    assert record is not None
    assert record.state == "confirmed"
    assert record.effect_disposition == "landed"
    assert record.result_json is not None
    # The receipt metadata is bounded — message_id, platform, success — and
    # does NOT include the full content or any credentials.
    import json as _json

    receipt = _json.loads(record.result_json)
    assert receipt["message_id"] == "msg-42"
    assert receipt["platform"] == "telegram"
    assert "hello" not in receipt  # full content NOT stored
    assert "password" not in _json.dumps(receipt).lower()
    db.close()


# ---------------------------------------------------------------------------
# 8. cron supplies stable delivery_id via metadata; router keys on it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_delivery_id_is_required_when_journal_set(
    tmp_path, monkeypatch
):
    """A journal is wired but the caller forgot to provide ``delivery_id``:
    we fail loudly rather than invent an identity (the journal row needs a
    stable id to dedup across restarts)."""
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)

    adapter = RecordingAdapter()
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: adapter},
        journal=journal,
    )
    target = DeliveryTarget.parse("telegram:123")

    with pytest.raises(ValueError, match="delivery_id"):
        await router._deliver_to_platform(target, "hello", metadata=None)

    assert adapter.calls == []
    db.close()


# ---------------------------------------------------------------------------
# 9. no journal → back-compat (no record, no error, send still happens)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_journal_falls_back_to_legacy_send(tmp_path, monkeypatch):
    """When the router has no journal, behavior must match the previous
    ship-it-every-time shape: no record is written and no error is raised."""
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)

    adapter = RecordingAdapter(message_id="msg-legacy")
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: adapter},
        # journal=None — the default
    )
    target = DeliveryTarget.parse("telegram:123")

    result = await router._deliver_to_platform(target, "hello", metadata=None)
    # Adapter WAS called — no journal means the legacy send-every-time shape.
    assert len(adapter.calls) == 1
    assert result.message_id == "msg-legacy"


# ---------------------------------------------------------------------------
# 10. LOCAL deliveries are not journaled (no platform round-trip).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_delivery_is_not_journaled(tmp_path, monkeypatch):
    """LOCAL delivery is a file save, not a transport round-trip — no journal
    row should be created even when a journal is wired and a delivery_id is
    passed."""
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)

    router = DeliveryRouter(
        GatewayConfig(),
        adapters={},
        journal=journal,
    )
    target = DeliveryTarget.parse("local")

    # Use the public deliver() entry point — _deliver_to_platform is the
    # platform-only chokepoint; LOCAL goes through deliver()'s
    # _deliver_local branch and never reaches the journal code.
    await router.deliver(
        "hello", [target],
        job_id="job-local", job_name="local-job",
        metadata={"delivery_id": "delivery-local"},
    )

    assert journal.get("delivery-local") is None
    db.close()


# ---------------------------------------------------------------------------
# 11. payload_hash uses sha256 (not any weaker scheme).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_hash_uses_sha256(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)

    adapter = RecordingAdapter()
    router = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: adapter},
        journal=journal,
    )
    target = DeliveryTarget.parse("telegram:123")

    content = "the-quick-brown-fox"
    expected = hashlib.sha256(content.encode("utf-8")).hexdigest()

    await router._deliver_to_platform(
        target,
        content,
        metadata={"delivery_id": "delivery-hash"},
    )

    record = journal.get("delivery-hash")
    assert record.payload_hash == expected


@pytest.mark.asyncio
async def test_failed_delivery_is_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)
    journal.create(
        operation_id="delivery-retry",
        kind="outbound_delivery",
        destination="telegram:123",
        payload_hash=_hash("hello"),
    )
    journal.transition(
        "delivery-retry",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "delivery-retry",
        from_states={"running"},
        to_state="dispatched",
        effect_disposition="unknown",
    )
    journal.transition(
        "delivery-retry",
        from_states={"dispatched"},
        to_state="failed",
        effect_disposition="none",
        error="temporary failure",
    )

    adapter = RecordingAdapter()
    router = DeliveryRouter(
        GatewayConfig(), adapters={Platform.TELEGRAM: adapter}, journal=journal
    )
    result = await router._deliver_to_platform(
        DeliveryTarget.parse("telegram:123"),
        "hello",
        metadata={"delivery_id": "delivery-retry"},
    )

    assert len(adapter.calls) == 1
    assert getattr(result, "success", None) is True
    assert journal.get("delivery-retry").state == "confirmed"
    db.close()


@pytest.mark.asyncio
async def test_dispatch_transition_failure_still_records_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    journal, db = _journal_for(tmp_path)
    original_transition = journal.transition

    def fail_dispatched(operation_id, **kwargs):
        if kwargs.get("to_state") == "dispatched":
            raise RuntimeError("journal unavailable")
        return original_transition(operation_id, **kwargs)

    journal.transition = fail_dispatched
    adapter = RecordingAdapter()

    async def fail_send(*_args, **_kwargs):
        raise RuntimeError("adapter failed")

    adapter.send = fail_send
    router = DeliveryRouter(
        GatewayConfig(), adapters={Platform.TELEGRAM: adapter}, journal=journal
    )

    with pytest.raises(RuntimeError, match="adapter failed"):
        await router._deliver_to_platform(
            DeliveryTarget.parse("telegram:123"),
            "hello",
            metadata={"delivery_id": "delivery-dispatch-transition"},
        )

    record = journal.get("delivery-dispatch-transition")
    assert record.state == "unknown"
    assert record.effect_disposition == "unknown"
    db.close()
    db.close()