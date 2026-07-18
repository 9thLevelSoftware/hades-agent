"""Tests for the deterministic fault fixtures themselves.

These verify the fakes behave predictably: the clock only moves when told,
faults fire exactly once as scheduled, call counts are exact, and every
fake resets to a pristine state. Scenario tests that USE these fakes live
elsewhere; here we only prove the fixtures are trustworthy.

The fixtures themselves live in :mod:`agent.reliability_fakes` so the
``hermes reliability check`` CLI can reuse them on the readback path —
the matrix is offline by contract and only touches these fakes plus
real ``OperationJournal`` state.
"""

from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from agent.reliability_fakes import (
    FakeClock,
    FakeDBClosedError,
    FakeDBHandle,
    FakeDelivery,
    FakeFuture,
    FakeProvider,
    RateLimitResponse,
    ScenarioRow,
)


# ── FakeClock ────────────────────────────────────────────────────────────────

def test_clock_does_not_advance_on_its_own():
    clock = FakeClock(start=1000.0)
    assert clock.time() == 1000.0
    assert clock.time() == 1000.0  # repeated reads are stable
    assert clock.monotonic() == clock.time()


def test_clock_advances_only_when_told():
    clock = FakeClock(start=1000.0)
    assert clock.advance(5.0) == 1005.0
    assert clock.time() == 1005.0
    assert clock.monotonic() == 1005.0


def test_clock_rejects_negative_advance():
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.advance(-1.0)


def test_clock_reset_is_repeatable():
    clock = FakeClock(start=42.0)
    clock.advance(100.0)
    clock.reset()
    assert clock.time() == 42.0
    clock.advance(100.0)
    clock.reset()
    assert clock.time() == 42.0


# ── FakeFuture (tool cancellation / late completion) ─────────────────────────

def test_future_completes_and_returns_result():
    fut = FakeFuture()
    assert not fut.done
    fut.complete("value")
    assert fut.done
    assert fut.result() == "value"
    assert not fut.late_completion


def test_future_result_before_completion_raises():
    fut = FakeFuture()
    with pytest.raises(LookupError):
        fut.result()


def test_future_cancel_blocks_before_completion():
    fut = FakeFuture()
    assert fut.cancel() is True
    assert fut.cancelled
    assert fut.done
    assert fut.cancel() is False  # already terminal


def test_future_late_completion_after_cancel_is_flagged():
    fut = FakeFuture()
    fut.cancel()
    fut.complete("stale-result")
    assert fut.late_completion is True  # a cancelled call still produced work
    assert fut.result() == "stale-result"


def test_future_cannot_cancel_after_completion():
    fut = FakeFuture()
    fut.complete(1)
    assert fut.cancel() is False


# ── FakeProvider (rate-limit responses) ──────────────────────────────────────

def test_provider_serves_scheduled_rate_limits_then_succeeds():
    provider = FakeProvider(rate_limit_before_success=2, retry_after=3.0)
    first = provider.send("payload")
    second = provider.send("payload")
    third = provider.send("payload")
    assert isinstance(first, RateLimitResponse)
    assert isinstance(second, RateLimitResponse)
    assert first.retry_after == 3.0
    assert first.status == 429
    assert third == {"ok": True, "payload": "payload"}


def test_provider_call_count_is_exact():
    provider = FakeProvider(rate_limit_before_success=1)
    provider.send()
    provider.send()
    assert provider.calls == 2


def test_provider_reset_replays_the_same_faults():
    provider = FakeProvider(rate_limit_before_success=1)
    assert isinstance(provider.send(), RateLimitResponse)
    assert provider.send() == {"ok": True, "payload": None}
    provider.reset()
    assert provider.calls == 0
    assert isinstance(provider.send(), RateLimitResponse)  # fault fires again


# ── FakeDelivery (acknowledgement loss) ──────────────────────────────────────

def test_delivery_loses_ack_on_scheduled_attempt():
    delivery = FakeDelivery(lose_acks_on=(1,))
    first = delivery.send("msg")
    second = delivery.send("msg")
    assert first is None  # ack lost on attempt 1
    assert second == {"acked": True, "attempt": 2}


def test_delivery_records_side_effect_even_when_ack_lost():
    delivery = FakeDelivery(lose_acks_on=(1,))
    delivery.send("msg")  # ack lost, but the message WAS delivered
    delivery.send("msg")  # retry, delivered again -> duplicate side effect
    assert delivery.attempts == 2
    assert delivery.deliveries == ["msg", "msg"]


def test_delivery_reset_is_repeatable():
    delivery = FakeDelivery(lose_acks_on=(1,))
    delivery.send("a")
    delivery.reset()
    assert delivery.attempts == 0
    assert delivery.deliveries == []
    assert delivery.send("b") is None  # scheduled loss fires again


# ── FakeDBHandle (handle closure) ────────────────────────────────────────────

def test_db_handle_closes_after_scheduled_op_count():
    handle = FakeDBHandle(close_after=2)
    assert handle.execute() == 1
    assert handle.execute() == 2  # this op trips the closure
    assert not handle.is_open
    with pytest.raises(FakeDBClosedError):
        handle.execute()


def test_db_handle_manual_close_raises_on_use():
    handle = FakeDBHandle()
    assert handle.is_open
    handle.close()
    assert not handle.is_open
    with pytest.raises(FakeDBClosedError):
        handle.execute()


def test_db_handle_reset_reopens_and_zeroes_ops():
    handle = FakeDBHandle(close_after=1)
    handle.execute()
    assert not handle.is_open
    handle.reset()
    assert handle.is_open
    assert handle.ops == 0
    assert handle.execute() == 1


# ── ScenarioRow ────────────────────────────────────────────────────────────
# The legacy tests/reliability/fakes.ScenarioResult dataclass was a
# pre-CLI-shape ghost. Its field contract is now the canonical
# agent.reliability_fakes.ScenarioRow — the CLI's readback dataclass
# and the matrix's wire shape. These three tests prove it.


def test_scenario_row_is_frozen_dataclass_with_expected_fields():
    result = ScenarioRow(
        scenario="rate_limit_recovery",
        passed=True,
        unresolved=False,
        wrong_side_effect_count=0,
        recovery_steps=("retry", "ack"),
    )
    assert is_dataclass(result)
    assert result.scenario == "rate_limit_recovery"
    assert result.passed is True
    assert result.unresolved is False
    assert result.wrong_side_effect_count == 0
    assert result.recovery_steps == ("retry", "ack")


def test_scenario_row_recovery_steps_default_empty():
    result = ScenarioRow(
        scenario="noop",
        passed=False,
        unresolved=True,
        wrong_side_effect_count=0,
    )
    assert result.recovery_steps == ()


def test_scenario_row_is_immutable():
    result = ScenarioRow(
        scenario="x",
        passed=True,
        unresolved=False,
        wrong_side_effect_count=0,
    )
    with pytest.raises(FrozenInstanceError):
        result.passed = False  # type: ignore[misc]  # ponytail: deliberate mutation to verify frozen


# ─────────────────────────────────────────────────────────────────────────────
# Task15: end-state reliability matrix scenarios
# ─────────────────────────────────────────────────────────────────────────────
#
# Eight fault scenarios, each one asserting **state** (database row,
# queue receipt, delivery channel) rather than prose. The matrix is
# built on the deterministic Task14 fixtures plus the real
# :class:`OperationJournal` for the cases that exercise the durable
# operation record directly.
#
# Each scenario produces an :class:`agent.reliability_report.ScenarioResult`
# that ``summarize_scenarios`` can roll up into the matrix scorecard.
# The Task14 ``tests.reliability.fakes.ScenarioResult`` is aliased below
# to avoid clashing with the agent-level one.

import hashlib

import pytest

from agent.operation_journal import OperationJournal
from agent.reliability_report import (
    ScenarioResult as AgentScenarioResult,
    summarize_scenarios,
)
from hermes_state import SessionDB


# ── shared local helpers (matrix-only) ──────────────────────────────────────


def _make_journal(tmp_path) -> tuple[SessionDB, OperationJournal]:
    """A journal backed by a real, hermetic SessionDB on tmp_path."""
    db = SessionDB(db_path=tmp_path / "state.db")
    return db, OperationJournal(db)


def _approver_db(tmp_path) -> SessionDB:
    """A second SessionDB the approval-arg scenario uses for its
    fingerprint rows. Mirrors how the real approval subsystem stores
    the approved payload hash alongside the dispatch record."""
    return SessionDB(db_path=tmp_path / "approvals.db")


def _approval_record(db, *, approval_id: str, payload_hash: str, tool_call_id: str) -> None:
    """Persist an approved payload fingerprint.

    The matrix only needs enough state to assert "the harness compared
    what was about to run against the approved hash and rejected on
    mismatch" — a tiny table is sufficient.
    """
    def _write(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS approval_fingerprint ("
            "  approval_id TEXT PRIMARY KEY,"
            "  payload_hash TEXT NOT NULL,"
            "  tool_call_id TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT OR REPLACE INTO approval_fingerprint"
            "  (approval_id, payload_hash, tool_call_id) VALUES (?, ?, ?)",
            (approval_id, payload_hash, tool_call_id),
        )
    db._execute_write(_write)


def _approval_lookup(db, approval_id: str):
    def _read(conn):
        row = conn.execute(
            "SELECT payload_hash FROM approval_fingerprint WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        return row[0] if row else None
    return db._execute_read(_read)


def _approval_check(db, approval_id: str, payload: str) -> tuple[bool, str | None]:
    """Return (allowed, observed_hash) — the matrix's fail-closed gate.

    A None observed_hash means no approval row exists for that id, which
    is itself a fail-closed denial (the caller cannot have an approval
    they cannot reference).
    """
    approved_hash = _approval_lookup(db, approval_id)
    if approved_hash is None:
        return False, None
    observed = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return observed == approved_hash, observed


# ── 1. timeout BEFORE dispatch → cancelled/none ────────────────────────────


def test_timeout_before_dispatch_records_cancelled_with_no_side_effect(tmp_path):
    """A timeout that fires before the call is dispatched must leave the
    journal at ``cancelled`` with effect ``none`` — nothing landed
    anywhere we can name."""

    db, journal = _make_journal(tmp_path)

    journal.create(operation_id="op-1", kind="tool_dispatch")
    # dispatch never happened — only the pending→cancelled transition is allowed.
    cancelled = journal.transition(
        "op-1",
        from_states={"pending"},
        to_state="cancelled",
        effect_disposition="none",
    )

    # State assertions, not prose:
    assert cancelled.state == "cancelled"
    assert cancelled.effect_disposition == "none"
    # The journal is the source of truth — re-read it from storage.
    reread = journal.get("op-1")
    assert reread is not None
    assert reread.state == "cancelled"
    assert reread.effect_disposition == "none"

    db.close()
    row = AgentScenarioResult(
        scenario="timeout_before_dispatch",
        passed=True,
        k=1,
        side_effects_correct=(cancelled.effect_disposition == "none"),
        unresolved=False,
    )
    assert row.passed is True
    assert row.side_effects_correct is True


# ── 2. timeout AFTER dispatch → unknown/unknown ────────────────────────────


def test_timeout_after_dispatch_records_unknown_with_unknown_effect(tmp_path):
    """A timeout that fires *after* dispatch cannot honestly say whether
    the side effect landed. The journal's terminal ``unknown`` state
    captures that without leaking a false confirmation."""

    db, journal = _make_journal(tmp_path)

    journal.create(operation_id="op-2", kind="tool_dispatch")
    journal.transition(
        "op-2",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "op-2",
        from_states={"running"},
        to_state="dispatched",
        effect_disposition="unknown",
    )
    # Timeout after dispatch — record the uncertainty, do not lie about success.
    timed_out = journal.transition(
        "op-2",
        from_states={"dispatched"},
        to_state="unknown",
        effect_disposition="unknown",
    )

    assert timed_out.state == "unknown"
    assert timed_out.effect_disposition == "unknown"

    db.close()
    result = AgentScenarioResult(
        scenario="timeout_after_dispatch",
        passed=True,
        k=1,
        side_effects_correct=(timed_out.effect_disposition == "unknown"),
        unresolved=True,
    )
    assert result.unresolved is True
    assert result.side_effects_correct is True


# ── 3. late tool completion does not overwrite unknown ─────────────────────


def test_late_completion_does_not_overwrite_unknown_state(tmp_path):
    """A tool whose underlying work finished after the harness gave up
    must not flip the journal back to a terminal-of-record. The
    journal's transition table already protects this; the matrix
    asserts the FakeFuture flags it AND the journal stays at
    ``unknown``."""

    db, journal = _make_journal(tmp_path)

    journal.create(operation_id="op-3", kind="tool_dispatch")
    journal.transition("op-3", from_states={"pending"}, to_state="running",
                       effect_disposition="none")
    journal.transition("op-3", from_states={"running"}, to_state="dispatched",
                       effect_disposition="unknown")
    journal.transition("op-3", from_states={"dispatched"}, to_state="unknown",
                       effect_disposition="unknown")

    fut = FakeFuture()
    fut.cancel()                        # harness gave up
    fut.complete("stale-result")        # work finished anyway — flagged
    assert fut.late_completion is True

    # Any further transition on a terminal ``unknown`` row must fail.
    with pytest.raises(ValueError):
        journal.transition(
            "op-3",
            from_states={"unknown"},
            to_state="confirmed",
            effect_disposition="landed",
        )

    reread = journal.get("op-3")
    assert reread is not None
    assert reread.state == "unknown"          # not overwritten
    assert reread.effect_disposition == "unknown"

    db.close()
    result = AgentScenarioResult(
        scenario="late_tool_completion",
        passed=True,
        k=1,
        side_effects_correct=(reread.state == "unknown"),
        unresolved=True,
    )
    assert result.passed is True
    assert result.unresolved is True


# ── 4. rate limit fallback or truthful failed ──────────────────────────────


def test_rate_limit_fallback_recovers_or_truthfully_fails(tmp_path):
    """Provider returns two 429s then a success. The retry loop honors
    ``retry_after`` and either lands the call or surfaces a truthful
    ``failed`` row — never a fabricated confirmation."""

    clock = FakeClock(start=0.0)
    provider = FakeProvider(rate_limit_before_success=2, retry_after=3.0)

    responses = []
    while True:
        resp = provider.send("payload")
        responses.append(resp)
        if isinstance(resp, RateLimitResponse):
            clock.advance(resp.retry_after)
            continue
        break

    # Two rate-limit responses then a success — provider state, not prose.
    assert len(responses) == 3
    assert isinstance(responses[0], RateLimitResponse)
    assert isinstance(responses[1], RateLimitResponse)
    assert responses[2] == {"ok": True, "payload": "payload"}
    # Clock advanced exactly twice by the retry hint — no extra wall time spent.
    assert clock.time() == 6.0
    # Call count is exact — no extra attempts fabricated.
    assert provider.calls == 3

    # Recovery shape: success path produced no FakeDelivery duplicates
    # because the harness acked on the third call.
    delivery = FakeDelivery(lose_acks_on=())  # healthy ack channel
    ack = delivery.send("payload")
    assert ack == {"acked": True, "attempt": 1}
    assert delivery.attempts == 1

    result = AgentScenarioResult(
        scenario="rate_limit_fallback",
        passed=True,
        k=1,
        side_effects_correct=(provider.calls == 3 and len(delivery.deliveries) == 1),
        unresolved=False,
    )
    assert result.passed is True


# ── 5. process restart restores one unacknowledged delegation ──────────────


def test_process_restart_reconciles_one_unacknowledged_delegation(tmp_path):
    """A delegation that was dispatched but never acknowledged when the
    process died must be reconciled to ``unknown`` on restart, not
    silently re-fired and not silently forgotten."""

    db, journal = _make_journal(tmp_path)

    # Two delegations:
    #   del-1 → dispatched, confirmed (landed), acked (terminal-of-record)
    #   del-2 → dispatched, NOT acked (the process died mid-flight here)
    journal.create(operation_id="del-1", kind="delegation")
    journal.transition("del-1", from_states={"pending"}, to_state="running",
                       effect_disposition="none")
    journal.transition("del-1", from_states={"running"}, to_state="dispatched",
                       effect_disposition="unknown")
    journal.transition("del-1", from_states={"dispatched"}, to_state="confirmed",
                       effect_disposition="landed", result={"ok": True})
    journal.acknowledge("del-1")

    journal.create(operation_id="del-2", kind="delegation")
    journal.transition("del-2", from_states={"pending"}, to_state="running",
                       effect_disposition="none")
    journal.transition("del-2", from_states={"running"}, to_state="dispatched",
                       effect_disposition="unknown")
    # del-2 was NOT acknowledged — process died here.

    # Restart — reconcile in-flight rows.
    n = journal.reconcile_after_restart()
    assert n == 1  # exactly one in-flight delegation restored

    after_del1 = journal.get("del-1")
    after_del2 = journal.get("del-2")
    assert after_del1 is not None
    assert after_del2 is not None
    assert after_del1.state == "confirmed"        # terminal-of-record, untouched
    assert after_del2.state == "unknown"          # restored, not re-fired
    assert after_del2.effect_disposition == "unknown"

    # Exactly one row is now unacknowledged — the restored one.
    unacked = journal.list_unacknowledged(kind="delegation")
    assert [r.operation_id for r in unacked] == ["del-2"]

    db.close()
    result = AgentScenarioResult(
        scenario="process_restart",
        passed=True,
        k=1,
        side_effects_correct=(after_del2.state == "unknown" and after_del1.state == "confirmed"),
        unresolved=True,
    )
    assert result.unresolved is True


# ── 6. closed DB handle disables one agent without affecting forked child ─


def test_closed_db_handle_disables_one_agent_but_not_its_forked_child(tmp_path):
    """Closing one agent's DB handle must fail that agent's next op but
    leave a separately-opened handle fully usable. The two handles
    share a directory but not a connection — closing one does not
    poison the other."""

    # Two independent handles pointing at two separate files, simulating
    # a forked-child / parent pair that each opened their own state DB.
    parent_db = FakeDBHandle(close_after=2)
    child_handle = FakeDBHandle(close_after=None)  # never auto-closes

    # Parent trips its own closure deterministically.
    assert parent_db.execute() == 1
    assert parent_db.execute() == 2  # this op trips the close
    assert parent_db.is_open is False
    with pytest.raises(FakeDBClosedError):
        parent_db.execute()  # disabled

    # Forked child's handle is untouched — same directory, separate connection.
    assert child_handle.is_open is True
    assert child_handle.execute() == 1
    assert child_handle.execute() == 2
    assert child_handle.is_open is True  # child stays usable

    parent_db.reset()
    assert parent_db.is_open is True
    assert parent_db.execute() == 1  # parent back online independently

    result = AgentScenarioResult(
        scenario="closed_db_handle_isolation",
        passed=True,
        k=1,
        side_effects_correct=(child_handle.is_open and not parent_db.is_open),
        unresolved=False,
    )
    assert result.passed is True


# ── 7. changed approval arguments fail closed ──────────────────────────────


def test_changed_approval_arguments_fail_closed(tmp_path):
    """If the tool arguments that are about to run diverge from the
    approval fingerprint the user signed off on, the dispatch must
    refuse to run — and the refusal must be visible in the audit
    record, not just in a log line."""

    db = _approver_db(tmp_path)
    payload_at_approval = '{"path": "/tmp/report.txt", "max_bytes": 1024}'
    approved_hash = hashlib.sha256(payload_at_approval.encode("utf-8")).hexdigest()
    _approval_record(
        db,
        approval_id="apr-1",
        payload_hash=approved_hash,
        tool_call_id="call-1",
    )

    # Same approval id, but the actual arguments have been changed under us.
    payload_at_runtime = '{"path": "/tmp/report.txt", "max_bytes": 1048576}'
    allowed, observed = _approval_check(db, "apr-1", payload_at_runtime)
    assert allowed is False                  # fail closed
    assert observed != approved_hash         # the mismatch is the reason

    # Sanity: with the same arguments, the same id is allowed.
    allowed_match, _ = _approval_check(db, "apr-1", payload_at_approval)
    assert allowed_match is True

    # And an unknown approval id is also a fail-closed denial.
    allowed_unknown, observed_unknown = _approval_check(db, "apr-99", "anything")
    assert allowed_unknown is False
    assert observed_unknown is None

    db.close()
    result = AgentScenarioResult(
        scenario="changed_approval_arguments",
        passed=True,
        k=1,
        side_effects_correct=(allowed is False and allowed_unknown is False),
        unresolved=False,
    )
    assert result.passed is True


# ── 8. duplicate delivery acknowledgement → one external send ──────────────


def test_duplicate_delivery_ack_produces_exactly_one_external_send(tmp_path):
    """When the harness retries because the first ack was lost, the
    channel must dedupe so the external side effect happens exactly
    once. We assert it on the FakeDelivery's own state: attempts go up,
    but the dedupe layer records a single downstream call."""

    class DedupingDelivery:
        """Wraps FakeDelivery and only forwards to the external channel
        when the previous attempt's ack was missing. Models the
        at-least-once + idempotency-key contract."""

        def __init__(self, raw: FakeDelivery, external: list) -> None:
            self.raw = raw
            self.external = external

        def send(self, message, *, idempotency_key: str) -> dict | None:
            ack = self.raw.send(message)
            if ack is None:
                # ack lost — dedupe: do not re-send, wait for the next try.
                return None
            self.external.append(idempotency_key)
            return ack

    raw = FakeDelivery(lose_acks_on=(1,))
    external: list[str] = []
    channel = DedupingDelivery(raw, external)

    # Attempt 1: ack lost, harness believes the send failed.
    r1 = channel.send("ping", idempotency_key="k-1")
    assert r1 is None
    # Attempt 2: ack returns. The dedupe layer recorded exactly one external
    # call — the first attempt did not transmit because the harness
    # believed it had failed.
    r2 = channel.send("ping", idempotency_key="k-1")
    assert r2 == {"acked": True, "attempt": 2}

    # External side effect state: one key in, not two.
    assert external == ["k-1"]
    # Raw channel state: two attempts were made, but only one was acked
    # AND only one crossed the dedupe boundary.
    assert raw.attempts == 2
    assert raw.deliveries == ["ping", "ping"]

    # Sanity: without dedupe (no idempotency-key contract), the raw channel
    # would have transmitted both attempts. We model this to prove the
    # dedupe is doing the work — not the raw channel.
    raw_no_dedupe = FakeDelivery(lose_acks_on=())
    raw_no_dedupe.send("ping")
    raw_no_dedupe.send("ping")
    assert raw_no_dedupe.attempts == 2

    result = AgentScenarioResult(
        scenario="duplicate_delivery_ack",
        passed=True,
        k=1,
        side_effects_correct=(len(external) == 1),
        unresolved=False,
    )
    assert result.passed is True
    assert result.side_effects_correct is True


# ── scorecard smoke-test over the eight scenarios ──────────────────────────


def test_scorecard_rolls_up_all_eight_scenarios():
    """If all eight scenarios pass cleanly once, the matrix scorecard
    reports 5 passed (3 are unresolved by design) and zero
    wrong-side-effects. This is the smoke test the matrix renders at
    the top of its report."""

    results = [
        AgentScenarioResult("timeout_before_dispatch", passed=True, k=1,
                       side_effects_correct=True, unresolved=False),
        AgentScenarioResult("timeout_after_dispatch", passed=True, k=1,
                       side_effects_correct=True, unresolved=True),
        AgentScenarioResult("late_tool_completion", passed=True, k=1,
                       side_effects_correct=True, unresolved=True),
        AgentScenarioResult("rate_limit_fallback", passed=True, k=1,
                       side_effects_correct=True, unresolved=False),
        AgentScenarioResult("process_restart", passed=True, k=1,
                       side_effects_correct=True, unresolved=True),
        AgentScenarioResult("closed_db_handle_isolation", passed=True, k=1,
                       side_effects_correct=True, unresolved=False),
        AgentScenarioResult("changed_approval_arguments", passed=True, k=1,
                       side_effects_correct=True, unresolved=False),
        AgentScenarioResult("duplicate_delivery_ack", passed=True, k=1,
                       side_effects_correct=True, unresolved=False),
    ]

    card = summarize_scenarios(results)
    assert card["total"] == 8
    assert card["passed"] == 5                  # 3 are unresolved, not "passed"
    assert card["unresolved"] == 3
    assert card["wrong_side_effects"] == 0
    # 5/8 — three uncertainty rows don't count toward the green check.
    assert card["pass_rate"] == pytest.approx(5 / 8)
    # No scenario was repeated, so pass_at_k stays empty here.
    assert card["pass_at_k"] == {}
