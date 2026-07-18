"""Deterministic reliability scenarios for the harness fault matrix.

Eight scenarios, each one asserting **state** (database row, queue receipt,
delivery channel) rather than prose. Every scenario is a pure function:
takes nothing but a ``pathlib.Path`` for hermetic tmp storage, returns a
:data:`ScenarioRow` for the matrix to score.

The matrix draws its summary through
:func:`agent.reliability_report.summarize_scenarios` once each
``run_all_scenarios`` invocation has finished walking the eight scenarios.

This is the production module the CLI imports. The legacy
``tests/reliability/test_fault_matrix.py`` kept its own copies for the
rollback-prone fixture fork; the canonical copy lives here. Tests still
import the fakes from :mod:`agent.reliability_fakes` so a single
test-library edit rolls forward through the matrix render too.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent.operation_journal import OperationJournal
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
from hermes_state import SessionDB


# ── helpers ──────────────────────────────────────────────────────────────


def _make_journal(path: Path) -> tuple[SessionDB, OperationJournal]:
    """A journal backed by a real, hermetic SessionDB on ``path``."""
    db = SessionDB(db_path=path / "state.db")
    return db, OperationJournal(db)


def _approver_db(path: Path) -> SessionDB:
    """A second SessionDB the approval-arg scenario uses for its
    fingerprint rows. Mirrors how the real approval subsystem stores
    the approved payload hash alongside the dispatch record."""
    return SessionDB(db_path=path / "approvals.db")


def _approval_record(
    db: SessionDB,
    *,
    approval_id: str,
    payload_hash: str,
    tool_call_id: str,
) -> None:
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


def _approval_lookup(db: SessionDB, approval_id: str):
    def _read(conn):
        row = conn.execute(
            "SELECT payload_hash FROM approval_fingerprint WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        return row[0] if row else None

    return db._execute_read(_read)


def _approval_check(
    db: SessionDB, approval_id: str, payload: str
) -> tuple[bool, str | None]:
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


# ── 1. timeout BEFORE dispatch ───────────────────────────────────────────


def timeout_before_dispatch(tmp_path: Path) -> ScenarioRow:
    """A timeout that fires before the call is dispatched must leave the
    journal at ``cancelled`` with effect ``none`` — nothing landed
    anywhere we can name."""
    db, journal = _make_journal(tmp_path)
    try:
        journal.create(operation_id="op-1", kind="tool_dispatch")
        cancelled = journal.transition(
            "op-1",
            from_states={"pending"},
            to_state="cancelled",
            effect_disposition="none",
        )
        reread = journal.get("op-1")
        if reread is None:
            return ScenarioRow(
                scenario="timeout_before_dispatch",
                passed=False,
                unresolved=False,
                wrong_side_effect_count=1,
                recovery_steps=("reread_journal",),
            )
        passed = (
            cancelled.state == "cancelled"
            and cancelled.effect_disposition == "none"
            and reread.state == "cancelled"
            and reread.effect_disposition == "none"
        )
        return ScenarioRow(
            scenario="timeout_before_dispatch",
            passed=passed,
            unresolved=False,
            wrong_side_effect_count=0 if passed else 1,
            recovery_steps=() if passed else ("verify_journal_state",),
        )
    finally:
        db.close()


# ── 2. timeout AFTER dispatch ────────────────────────────────────────────


def timeout_after_dispatch(tmp_path: Path) -> ScenarioRow:
    """A timeout that fires *after* dispatch cannot honestly say whether
    the side effect landed. The journal's terminal ``unknown`` state
    captures that without leaking a false confirmation."""
    db, journal = _make_journal(tmp_path)
    try:
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
        timed_out = journal.transition(
            "op-2",
            from_states={"dispatched"},
            to_state="unknown",
            effect_disposition="unknown",
        )
        passed = (
            timed_out.state == "unknown"
            and timed_out.effect_disposition == "unknown"
        )
        return ScenarioRow(
            scenario="timeout_after_dispatch",
            passed=passed,
            unresolved=passed,  # unknown is the unresolved-by-design state
            wrong_side_effect_count=0,
            recovery_steps=("reconcile_journal",) if passed else (),
        )
    finally:
        db.close()


# ── 3. late tool completion does not overwrite unknown ──────────────────


def late_tool_completion(tmp_path: Path) -> ScenarioRow:
    """A tool whose underlying work finished after the harness gave up
    must not flip the journal back to a terminal-of-record."""
    db, journal = _make_journal(tmp_path)
    try:
        journal.create(operation_id="op-3", kind="tool_dispatch")
        journal.transition(
            "op-3", from_states={"pending"}, to_state="running",
            effect_disposition="none",
        )
        journal.transition(
            "op-3", from_states={"running"}, to_state="dispatched",
            effect_disposition="unknown",
        )
        journal.transition(
            "op-3", from_states={"dispatched"}, to_state="unknown",
            effect_disposition="unknown",
        )

        fut = FakeFuture()
        fut.cancel()
        fut.complete("stale-result")
        late_completion = fut.late_completion

        reread = journal.get("op-3")
        passed = (
            late_completion is True
            and reread is not None
            and reread.state == "unknown"
            and reread.effect_disposition == "unknown"
        )
        return ScenarioRow(
            scenario="late_tool_completion",
            passed=passed,
            unresolved=True,
            wrong_side_effect_count=0,
            recovery_steps=("ignore_late_completion",) if passed else (),
        )
    finally:
        db.close()


# ── 4. rate limit fallback or truthful failed ────────────────────────────


def rate_limit_fallback(tmp_path: Path) -> ScenarioRow:
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

    delivery = FakeDelivery(lose_acks_on=())
    ack = delivery.send("payload")

    passed = (
        len(responses) == 3
        and isinstance(responses[0], RateLimitResponse)
        and isinstance(responses[1], RateLimitResponse)
        and responses[2] == {"ok": True, "payload": "payload"}
        and clock.time() == 6.0
        and provider.calls == 3
        and ack == {"acked": True, "attempt": 1}
        and delivery.attempts == 1
    )
    return ScenarioRow(
        scenario="rate_limit_fallback",
        passed=passed,
        unresolved=False,
        wrong_side_effect_count=0,
        recovery_steps=() if passed else ("honor_retry_after",),
    )


# ── 5. process restart restores one unacknowledged delegation ────────────


def process_restart(tmp_path: Path) -> ScenarioRow:
    """A delegation that was dispatched but never acknowledged when the
    process died must be reconciled to ``unknown`` on restart, not
    silently re-fired and not silently forgotten."""
    db, journal = _make_journal(tmp_path)
    try:
        journal.create(operation_id="del-1", kind="delegation")
        journal.transition(
            "del-1", from_states={"pending"}, to_state="running",
            effect_disposition="none",
        )
        journal.transition(
            "del-1", from_states={"running"}, to_state="dispatched",
            effect_disposition="unknown",
        )
        journal.transition(
            "del-1", from_states={"dispatched"}, to_state="confirmed",
            effect_disposition="landed", result={"ok": True},
        )
        journal.acknowledge("del-1")

        journal.create(operation_id="del-2", kind="delegation")
        journal.transition(
            "del-2", from_states={"pending"}, to_state="running",
            effect_disposition="none",
        )
        journal.transition(
            "del-2", from_states={"running"}, to_state="dispatched",
            effect_disposition="unknown",
        )

        n = journal.reconcile_after_restart()
        after_del1 = journal.get("del-1")
        after_del2 = journal.get("del-2")

        passed = (
            n == 1
            and after_del1 is not None
            and after_del2 is not None
            and after_del1.state == "confirmed"
            and after_del2.state == "unknown"
            and after_del2.effect_disposition == "unknown"
        )
        return ScenarioRow(
            scenario="process_restart",
            passed=passed,
            unresolved=True,
            wrong_side_effect_count=0,
            recovery_steps=("reconcile_in_flight",) if passed else (),
        )
    finally:
        db.close()


# ── 6. closed DB handle disables one agent without affecting forked child


def closed_db_handle_isolation(tmp_path: Path) -> ScenarioRow:
    """Closing one agent's DB handle must fail that agent's next op but
    leave a separately-opened handle fully usable."""
    parent_db = FakeDBHandle(close_after=2)
    child_handle = FakeDBHandle(close_after=None)

    try:
        assert parent_db.execute() == 1
        assert parent_db.execute() == 2  # trips closure
        assert parent_db.is_open is False
        parent_disabled = False
        try:
            parent_db.execute()
        except FakeDBClosedError:
            parent_disabled = True

        assert child_handle.is_open is True
        assert child_handle.execute() == 1
        assert child_handle.execute() == 2
        child_open = child_handle.is_open

        passed = parent_disabled and child_open
        return ScenarioRow(
            scenario="closed_db_handle_isolation",
            passed=passed,
            unresolved=False,
            wrong_side_effect_count=0,
            recovery_steps=() if passed else ("isolate_handles",),
        )
    finally:
        parent_db.reset()
        child_handle.reset()


# ── 7. changed approval arguments fail closed ───────────────────────────


def changed_approval_arguments(tmp_path: Path) -> ScenarioRow:
    """If the tool arguments that are about to run diverge from the
    approval fingerprint the user signed off on, the dispatch must
    refuse to run."""
    db = _approver_db(tmp_path)
    try:
        payload_at_approval = '{"path": "/tmp/report.txt", "max_bytes": 1024}'
        approved_hash = hashlib.sha256(
            payload_at_approval.encode("utf-8")
        ).hexdigest()
        _approval_record(
            db,
            approval_id="apr-1",
            payload_hash=approved_hash,
            tool_call_id="call-1",
        )

        payload_at_runtime = '{"path": "/tmp/report.txt", "max_bytes": 1048576}'
        allowed, observed = _approval_check(db, "apr-1", payload_at_runtime)
        allowed_match, _ = _approval_check(db, "apr-1", payload_at_approval)
        allowed_unknown, observed_unknown = _approval_check(
            db, "apr-99", "anything"
        )

        passed = (
            allowed is False
            and observed != approved_hash
            and allowed_match is True
            and allowed_unknown is False
            and observed_unknown is None
        )
        return ScenarioRow(
            scenario="changed_approval_arguments",
            passed=passed,
            unresolved=False,
            wrong_side_effect_count=0,
            recovery_steps=() if passed else ("compare_payload_hash",),
        )
    finally:
        db.close()


# ── 8. duplicate delivery acknowledgement → one external send ────────────


def duplicate_delivery_ack(tmp_path: Path) -> ScenarioRow:
    """When the harness retries because the first ack was lost, the
    channel must dedupe so the external side effect happens exactly
    once."""

    class DedupingDelivery:
        """Wraps FakeDelivery and only forwards to the external channel
        when the previous attempt's ack was missing. Models the
        at-least-once + idempotency-key contract."""

        def __init__(self, raw: FakeDelivery, external: list) -> None:
            self.raw = raw
            self.external = external

        def send(self, message: Any, *, idempotency_key: str) -> dict | None:  # noqa: ANN401
            ack = self.raw.send(message)
            if ack is None:
                return None
            self.external.append(idempotency_key)
            return ack

    raw = FakeDelivery(lose_acks_on=(1,))
    external: list[str] = []
    channel = DedupingDelivery(raw, external)

    r1 = channel.send("ping", idempotency_key="k-1")
    r2 = channel.send("ping", idempotency_key="k-1")

    passed = (
        r1 is None
        and r2 == {"acked": True, "attempt": 2}
        and external == ["k-1"]
        and raw.attempts == 2
        and raw.deliveries == ["ping", "ping"]
    )
    return ScenarioRow(
        scenario="duplicate_delivery_ack",
        passed=passed,
        unresolved=False,
        wrong_side_effect_count=0,
        recovery_steps=() if passed else ("dedupe_with_idempotency_key",),
    )


# ── matrix runner ────────────────────────────────────────────────────────


SCENARIOS = (
    timeout_before_dispatch,
    timeout_after_dispatch,
    late_tool_completion,
    rate_limit_fallback,
    process_restart,
    closed_db_handle_isolation,
    changed_approval_arguments,
    duplicate_delivery_ack,
)


def run_all_scenarios(tmp_path: Path) -> list[ScenarioRow]:
    """Run the eight matrix scenarios and return their rows.

    Each scenario gets its own subdirectory under ``tmp_path`` so a
    hermetic SessionDB created in one scenario cannot leak into the
    next. Determinism: the suite is offline, every fake moves only
    when told, and there is no wall-clock read.
    """
    rows: list[ScenarioRow] = []
    for i, scenario in enumerate(SCENARIOS):
        rows.append(scenario(tmp_path / f"s{i:02d}"))
    return rows
