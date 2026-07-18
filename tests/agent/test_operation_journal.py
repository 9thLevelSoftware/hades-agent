"""Tests for the shared durable operation journal."""

import json
import sqlite3
from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from agent.operation_journal import OperationJournal, OperationRecord
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _journal(db):
    return OperationJournal(db)


def test_schema_and_profile_isolation(tmp_path):
    first = SessionDB(db_path=tmp_path / "first" / "state.db")
    second = SessionDB(db_path=tmp_path / "second" / "state.db")
    try:
        first_journal = _journal(first)
        second_journal = _journal(second)

        record = first_journal.create(operation_id="shared-id", kind="tool")

        assert isinstance(record, OperationRecord)
        assert is_dataclass(record)
        assert second_journal.get("shared-id") is None
        assert first_journal.list_unacknowledged() == [record]

        columns = [
            row[1]
            for row in first._conn.execute("PRAGMA table_info(agent_operations)")
        ]
        assert columns == [
            "operation_id",
            "kind",
            "session_id",
            "turn_id",
            "tool_call_id",
            "destination",
            "payload_hash",
            "state",
            "effect_disposition",
            "result_json",
            "error",
            "created_at",
            "updated_at",
            "acknowledged_at",
        ]
        indexes = {
            tuple(row[2] for row in first._conn.execute(
                f'PRAGMA index_info("{index_name}")'
            ))
            for index_name in (
                "idx_agent_operations_kind_state_updated",
                "idx_agent_operations_session_updated",
            )
        }
        assert indexes == {
            ("kind", "state", "updated_at"),
            ("session_id", "updated_at"),
        }
    finally:
        first.close()
        second.close()


def test_create_defaults_and_idempotent_identity(db):
    journal = _journal(db)

    created = journal.create(
        operation_id="op-1",
        kind="tool",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        destination="telegram:chat-1",
        payload_hash="hash-1",
    )
    same = journal.create(
        operation_id="op-1",
        kind="tool",
        session_id="session-1",
        turn_id="turn-1",
        tool_call_id="call-1",
        destination="telegram:chat-1",
        payload_hash="hash-1",
    )

    assert created == same
    assert created.state == "pending"
    assert created.effect_disposition == "none"
    assert created.result_json is None
    assert created.error is None
    assert created.acknowledged_at is None
    with pytest.raises(FrozenInstanceError):
        created.state = "running"


def test_create_rejects_conflicting_identity(db):
    journal = _journal(db)
    journal.create(operation_id="op-1", kind="tool", destination="one")

    with pytest.raises(ValueError):
        journal.create(operation_id="op-1", kind="tool", destination="two")

    assert journal.get("op-1").destination == "one"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", None),
        ("operation_id", ""),
        ("operation_id", 123),
        ("kind", None),
        ("kind", ""),
        ("kind", 123),
    ],
)
def test_create_rejects_invalid_identity_before_db_write(db, field, value):
    journal = _journal(db)
    identity = {"operation_id": "op-1", "kind": "tool"}
    identity[field] = value

    with pytest.raises(ValueError):
        journal.create(**identity)

    assert db._conn.execute(
        "SELECT COUNT(*) FROM agent_operations"
    ).fetchone()[0] == 0


def test_valid_transition_serializes_result_and_invalid_or_stale_leaves_row_unchanged(db):
    journal = _journal(db)
    journal.create(operation_id="op-1", kind="tool")

    with pytest.raises(ValueError):
        journal.transition(
            "op-1",
            from_states={"pending"},
            to_state="confirmed",
            effect_disposition="landed",
        )
    original = journal.get("op-1")
    assert original.state == "pending"

    running = journal.transition(
        "op-1",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    assert running.state == "running"

    with pytest.raises(ValueError):
        journal.transition(
            "op-1",
            from_states={"pending"},
            to_state="failed",
            effect_disposition="unknown",
        )
    assert journal.get("op-1") == running

    result = journal.transition(
        "op-1",
        from_states={"running"},
        to_state="confirmed",
        effect_disposition="landed",
        result={"z": 1, "a": "value"},
    )
    assert result.result_json == json.dumps(
        {"z": 1, "a": "value"}, sort_keys=True, default=str
    )

    with pytest.raises(ValueError):
        journal.transition(
            "op-1",
            from_states={"confirmed"},
            to_state="running",
            effect_disposition="none",
        )
    assert journal.get("op-1") == result


def test_transition_rejects_invalid_state_and_effect(db):
    journal = _journal(db)
    journal.create(operation_id="op-1", kind="tool")

    with pytest.raises(ValueError):
        journal.transition(
            "op-1",
            from_states={"pending"},
            to_state="not-a-state",
            effect_disposition="none",
        )
    with pytest.raises(ValueError):
        journal.transition(
            "op-1",
            from_states={"pending"},
            to_state="running",
            effect_disposition="not-an-effect",
        )
    assert journal.get("op-1").state == "pending"


def test_transition_rejects_contradictory_state_effect_pairs(db):
    journal = _journal(db)
    operation_ids = ("cancelled", "running", "dispatched", "confirmed", "unknown")
    for operation_id in operation_ids:
        journal.create(operation_id=operation_id, kind="tool")
    for operation_id in ("dispatched", "confirmed", "unknown"):
        journal.transition(
            operation_id,
            from_states={"pending"},
            to_state="running",
            effect_disposition="none",
        )

    invalid = [
        ("cancelled", {"pending"}, "cancelled", "landed"),
        ("running", {"pending"}, "running", "unknown"),
        ("dispatched", {"running"}, "dispatched", "none"),
        ("confirmed", {"running"}, "confirmed", "unknown"),
        ("unknown", {"running"}, "unknown", "none"),
    ]
    for operation_id, from_states, to_state, effect_disposition in invalid:
        before = journal.get(operation_id)
        with pytest.raises(ValueError):
            journal.transition(
                operation_id,
                from_states=from_states,
                to_state=to_state,
                effect_disposition=effect_disposition,
            )
        assert journal.get(operation_id) == before


def test_transition_accepts_representative_state_effect_pairs(db):
    journal = _journal(db)
    for operation_id in ("running", "dispatched", "confirmed", "failed", "unknown", "cancelled"):
        journal.create(operation_id=operation_id, kind="tool")
    journal.transition(
        "running",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    for operation_id in ("dispatched", "confirmed", "failed", "unknown"):
        journal.transition(
            operation_id,
            from_states={"pending"},
            to_state="running",
            effect_disposition="none",
        )

    for operation_id, to_state, effect_disposition in (
        ("dispatched", "dispatched", "unknown"),
        ("confirmed", "confirmed", "none"),
        ("failed", "failed", "none"),
        ("unknown", "unknown", "unknown"),
    ):
        record = journal.transition(
            operation_id,
            from_states={"running"},
            to_state=to_state,
            effect_disposition=effect_disposition,
        )
        assert (record.state, record.effect_disposition) == (
            to_state,
            effect_disposition,
        )

    cancelled = journal.transition(
        "cancelled",
        from_states={"pending"},
        to_state="cancelled",
        effect_disposition="none",
    )
    assert (cancelled.state, cancelled.effect_disposition) == ("cancelled", "none")


def test_reconcile_marks_inflight_unknown_without_retrying(db):
    journal = _journal(db)
    journal.create(operation_id="pending", kind="tool")
    journal.create(operation_id="running", kind="tool")
    journal.create(operation_id="dispatched", kind="tool")
    journal.create(operation_id="confirmed", kind="tool")
    journal.create(operation_id="failed", kind="tool")
    journal.create(operation_id="unknown", kind="tool")
    journal.create(operation_id="cancelled", kind="tool")

    journal.transition(
        "running",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "dispatched",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "dispatched",
        from_states={"running"},
        to_state="dispatched",
        effect_disposition="unknown",
    )
    journal.transition(
        "confirmed",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "confirmed",
        from_states={"running"},
        to_state="confirmed",
        effect_disposition="landed",
    )
    journal.transition(
        "failed",
        from_states={"pending"},
        to_state="failed",
        effect_disposition="unknown",
    )
    journal.transition(
        "unknown",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "unknown",
        from_states={"running"},
        to_state="unknown",
        effect_disposition="unknown",
    )
    journal.transition(
        "cancelled",
        from_states={"pending"},
        to_state="cancelled",
        effect_disposition="none",
    )

    assert journal.reconcile_after_restart() == 2
    assert journal.get("pending").state == "pending"
    assert journal.get("running").state == "unknown"
    assert journal.get("running").effect_disposition == "unknown"
    assert journal.get("dispatched").state == "unknown"
    assert journal.get("dispatched").effect_disposition == "unknown"
    assert journal.get("confirmed").state == "confirmed"
    assert journal.get("failed").state == "failed"
    assert journal.get("unknown").state == "unknown"
    assert journal.get("cancelled").state == "cancelled"

    with pytest.raises(ValueError):
        journal.transition(
            "running",
            from_states={"unknown"},
            to_state="running",
            effect_disposition="none",
        )


def test_acknowledge_only_terminal_and_is_idempotent(db):
    journal = _journal(db)
    journal.create(operation_id="op-1", kind="tool")
    assert journal.acknowledge("op-1") is False

    journal.transition(
        "op-1",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    assert journal.acknowledge("op-1") is False
    journal.transition(
        "op-1",
        from_states={"running"},
        to_state="dispatched",
        effect_disposition="unknown",
    )
    assert journal.acknowledge("op-1") is False
    journal.transition(
        "op-1",
        from_states={"dispatched"},
        to_state="failed",
        effect_disposition="unknown",
        error="send failed",
    )

    assert journal.acknowledge("op-1") is True
    assert journal.acknowledge("op-1") is False
    assert journal.get("op-1").acknowledged_at is not None


def test_list_unacknowledged_filters_kind_and_orders_created_at(db, monkeypatch):
    journal = _journal(db)
    monkeypatch.setattr("agent.operation_journal.time.time", lambda: 1.0)
    journal.create(operation_id="first", kind="tool")
    journal.create(operation_id="second", kind="delivery")
    journal.create(operation_id="third", kind="tool")
    journal.transition(
        "third",
        from_states={"pending"},
        to_state="cancelled",
        effect_disposition="none",
    )
    assert journal.acknowledge("third") is True

    assert [r.operation_id for r in journal.list_unacknowledged()] == [
        "first",
        "second",
    ]
    assert [r.operation_id for r in journal.list_unacknowledged(kind="tool")] == [
        "first",
    ]


def test_prune_terminal_only_deletes_acknowledged_terminal_older_than_cutoff(db):
    """Bounded retention: only acknowledged terminal (confirmed/failed/cancelled)
    rows older than the cutoff may be deleted. Everything else must survive,
    including unacknowledged terminal, unknown (never prune), and any
    pending/running/dispatched state regardless of age.
    """
    journal = _journal(db)

    now = 1_000_000.0
    old = now - 31 * 86400
    fresh = now - 29 * 86400

    def _seed(op_id, path, ts, ack):
        journal.create(operation_id=op_id, kind="tool")
        for frm, to, eff in path:
            journal.transition(op_id, from_states={frm}, to_state=to, effect_disposition=eff)
        db._conn.execute(
            "UPDATE agent_operations SET created_at = ?, updated_at = ? WHERE operation_id = ?",
            (ts, ts, op_id),
        )
        if ack:
            journal.acknowledge(op_id)

    cases = [
        # Prunable: acknowledged + terminal + older than cutoff
        ("ack_confirmed_old", [("pending", "running", "none"), ("running", "confirmed", "landed")], old, True),
        ("ack_failed_old", [("pending", "failed", "unknown")], old, True),
        ("ack_cancelled_old", [("pending", "cancelled", "none")], old, True),

        # Survives: same shape but newer than cutoff
        ("ack_confirmed_fresh", [("pending", "running", "none"), ("running", "confirmed", "landed")], fresh, True),
        ("ack_failed_fresh", [("pending", "failed", "unknown")], fresh, True),
        ("ack_cancelled_fresh", [("pending", "cancelled", "none")], fresh, True),

        # Survives: terminal but unacknowledged, even when old
        ("unack_confirmed_old", [("pending", "running", "none"), ("running", "confirmed", "landed")], old, False),
        ("unack_failed_old", [("pending", "failed", "unknown")], old, False),
        ("unack_cancelled_old", [("pending", "cancelled", "none")], old, False),

        # Survives: 'unknown' state is never pruned, even when acked and old
        ("ack_unknown_old", [("pending", "running", "none"), ("running", "unknown", "unknown")], old, True),

        # Survives: non-terminal states, even when old
        ("running_old", [("pending", "running", "none")], old, False),
    ]

    for op_id, path, ts, ack in cases:
        _seed(op_id, path, ts, ack)
    db._conn.commit()

    seeded = db._conn.execute("SELECT COUNT(*) FROM agent_operations").fetchone()[0]
    assert seeded == len(cases)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr("agent.operation_journal.time.time", lambda: now)
        deleted = journal.prune_terminal(older_than_days=30)
    finally:
        monkey.undo()

    assert deleted == 3

    survivors = {
        row["operation_id"]
        for row in db._conn.execute("SELECT operation_id FROM agent_operations").fetchall()
    }
    expected_remaining = {
        "ack_confirmed_fresh",
        "ack_failed_fresh",
        "ack_cancelled_fresh",
        "unack_confirmed_old",
        "unack_failed_old",
        "unack_cancelled_old",
        "ack_unknown_old",
        "running_old",
    }
    assert survivors == expected_remaining

    # Pruning twice in a row is a no-op the second time.
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr("agent.operation_journal.time.time", lambda: now)
        assert journal.prune_terminal(older_than_days=30) == 0
    finally:
        monkey.undo()


def test_prune_terminal_default_cutoff_is_thirty_days(db, monkeypatch):
    journal = _journal(db)
    # Default cutoff = 30 days, exclusive (<). A row that's 30d old to the
    # second is on the boundary and must survive; a row that's 30d + 1s old
    # falls inside and must be deleted.
    now = 5_000_000.0
    boundary = now - 30 * 86400
    inside = now - (30 * 86400 + 1)

    for op_id, ts in (("boundary", boundary), ("inside", inside)):
        journal.create(operation_id=op_id, kind="tool")
        journal.transition(op_id, from_states={"pending"}, to_state="cancelled", effect_disposition="none")
        db._conn.execute(
            "UPDATE agent_operations SET created_at = ?, updated_at = ? WHERE operation_id = ?",
            (ts, ts, op_id),
        )
        journal.acknowledge(op_id)
    db._conn.commit()

    monkeypatch.setattr("agent.operation_journal.time.time", lambda: now)
    # Default (30d) deletes only the row strictly older than the cutoff;
    # the boundary row (exactly 30d old) survives.
    assert journal.prune_terminal() == 1
    # Idempotent re-run does nothing.
    assert journal.prune_terminal() == 0


def test_database_checks_states_and_effects(db):
    with pytest.raises(sqlite3.IntegrityError):
        db._execute_write(
            lambda conn: conn.execute(
                "INSERT INTO agent_operations "
                "(operation_id, kind, state, effect_disposition, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (None, "tool", "pending", "none", 1.0, 1.0),
            )
        )
    with pytest.raises(sqlite3.IntegrityError):
        db._execute_write(
            lambda conn: conn.execute(
                "INSERT INTO agent_operations "
                "(operation_id, kind, state, effect_disposition, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("bad-state", "tool", "invalid", "none", 1.0, 1.0),
            )
        )
    with pytest.raises(sqlite3.IntegrityError):
        db._execute_write(
            lambda conn: conn.execute(
                "INSERT INTO agent_operations "
                "(operation_id, kind, state, effect_disposition, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("bad-effect", "tool", "pending", "invalid", 1.0, 1.0),
            )
        )


def test_reads_use_closed_database_guard(db):
    journal = _journal(db)
    db.close()
    from hermes_state import SessionDBClosedError

    with pytest.raises(SessionDBClosedError):
        journal.get("missing")
    with pytest.raises(SessionDBClosedError):
        journal.list_unacknowledged()


def test_reconcile_owner_fence_preserves_live_operations(db):
    journal = OperationJournal(db)
    journal.create(operation_id="live", kind="test")
    journal.transition("live", from_states={"pending"}, to_state="running", effect_disposition="none")
    journal.create(operation_id="dead", kind="test")
    journal.transition("dead", from_states={"pending"}, to_state="running", effect_disposition="none")
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE agent_operation_owners SET owner_pid = 0, owner_started_at = NULL WHERE operation_id = ?",
            ("dead",),
        )
    )

    assert journal.reconcile_after_restart(owner_fenced=True) == 1
    assert journal.get("live").state == "running"
    assert journal.get("dead").state == "unknown"


def test_fenced_reconcile_skips_live_owner(db):
    journal = _journal(db)
    journal.create(operation_id="live", kind="async_delegation")
    journal.transition(
        "live",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )

    assert journal.reconcile_after_restart(owner_fenced=True) == 0
    assert journal.get("live").state == "running"
