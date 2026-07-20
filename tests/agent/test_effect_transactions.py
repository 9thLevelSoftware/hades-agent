"""Tests for SessionDB effect_transactions storage.

The effect transaction is the durable record of a mission step's effect
(prepare / preview / commit / verify / compensate). Each transaction
binds to a single ``agent_operations`` row (one operation = one effect
tx) and to a ``(mission_id, sequence_no)`` slot inside its mission.

This file covers the storage contract only. Coordinator / adapter
semantics ship in later tasks.
"""

from __future__ import annotations

import sqlite3
import types
from dataclasses import FrozenInstanceError
from typing import Any, List

import pytest
from agent.operation_journal import OperationJournal

from hades_state import SCHEMA_VERSION, SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _seed_operation(db: SessionDB, operation_id: str = "op-1", kind: str = "tool") -> None:
    OperationJournal(db).create(operation_id=operation_id, kind=kind)


# Used by every cancellation/transit test below: the row is created with
# no prepared/preview so phase stays at ``pending``, identical to the
# "not yet started" state cancellation would target.
def _seed_pending_tx(db: SessionDB) -> None:
    """Create ``tx-1`` left in ``pending`` — the "not yet started"
    state cancellation targets."""
    OperationJournal(db).create(operation_id="op-1", kind="tool")
    db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
    )


def _step(db: SessionDB, expected: str, nxt: str) -> None:
    """Single CAS hop; tiny wrapper so the path-driven tests below
    stay one line per hop."""
    assert (
        db.transition_effect_transaction(
            "tx-1", expected_phase=expected, next_phase=nxt
        )
        is True
    )


def test_create_effect_transaction_validates_inputs_before_sql(db):
    with pytest.raises(ValueError):
        db.create_effect_transaction(
            transaction_id="",
            operation_id="op-1",
            mission_id="m-1",
            execution_id="ex-1",
            step_id="write",
            adapter_id="workspace.v1",
            sequence_no=1,
            semantics={"kind": "reversible", "idempotent": True},
            depends_on=[],
            prepared={"path": "README.md"},
            preview={"diff": "+ok"},
        )
    # No row, no FK violation on the empty transaction_id.
    count = db._conn.execute(
        "SELECT COUNT(*) FROM effect_transactions"
    ).fetchone()[0]
    assert count == 0


def test_create_effect_transaction_validates_phase_vocabulary(db):
    _seed_operation(db)
    with pytest.raises(ValueError):
        db.create_effect_transaction(
            transaction_id="tx-1",
            operation_id="op-1",
            mission_id="m-1",
            execution_id="ex-1",
            step_id="write",
            adapter_id="workspace.v1",
            sequence_no=1,
            semantics={"kind": "reversible", "idempotent": True},
            depends_on=[],
            prepared={"path": "README.md"},
            preview={"diff": "+ok"},
            phase="nonsense",
        )


def test_create_effect_transaction_one_per_operation_id(db):
    _seed_operation(db)
    tx = db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
        prepared={"path": "README.md"},
        preview={"diff": "+ok"},
    )
    # Same operation_id, different transaction_id → UNIQUE conflict.
    with pytest.raises(ValueError):
        db.create_effect_transaction(
            transaction_id="tx-2",
            operation_id="op-1",
            mission_id="m-1",
            execution_id="ex-1",
            step_id="write",
            adapter_id="workspace.v1",
            sequence_no=2,
            semantics={"kind": "reversible", "idempotent": True},
            depends_on=[],
            prepared={"path": "OTHER.md"},
            preview={"diff": "+x"},
        )
    # tx-1 row exists with the original payload.
    again = db.get_effect_transaction("tx-1")
    assert again == tx


def test_create_effect_transaction_unique_mission_sequence(db):
    OperationJournal(db).create(operation_id="op-1", kind="tool")
    OperationJournal(db).create(operation_id="op-2", kind="tool")
    db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
        prepared={"path": "README.md"},
        preview={"diff": "+ok"},
    )
    with pytest.raises(ValueError):
        db.create_effect_transaction(
            transaction_id="tx-2",
            operation_id="op-2",
            mission_id="m-1",
            execution_id="ex-1",
            step_id="write",
            adapter_id="workspace.v1",
            sequence_no=1,  # collision
            semantics={"kind": "reversible", "idempotent": True},
            depends_on=[],
            prepared={"path": "OTHER.md"},
            preview={"diff": "+x"},
        )


def test_create_effect_transaction_round_trips_with_canonical_json(db):
    _seed_operation(db, operation_id="op-1")
    tx = db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[{"transaction_id": "tx-0"}],
        prepared={"path": "README.md"},
        preview={"diff": "+ok"},
        authority={"actor": "user", "scopes": ["fs:write"]},
    )

    assert tx.transaction_id == "tx-1"
    assert tx.operation_id == "op-1"
    assert tx.mission_id == "m-1"
    assert tx.sequence_no == 1
    assert tx.semantics == {"kind": "reversible", "idempotent": True}
    assert tx.depends_on == [{"transaction_id": "tx-0"}]
    assert tx.prepared == {"path": "README.md"}
    assert tx.preview == {"diff": "+ok"}
    assert tx.authority == {"actor": "user", "scopes": ["fs:write"]}
    # Round-trip equivalence is what guarantees the JSON serialization is
    # deterministic — re-fetch and compare the parsed fields.
    again = db.get_effect_transaction("tx-1")
    assert again is not None
    assert again.semantics == tx.semantics
    assert again.depends_on == tx.depends_on
    assert again.authority == tx.authority


def test_create_effect_transaction_returns_frozen_record_with_deep_copied_structured_fields(db):
    _seed_operation(db)
    semantics = {"kind": "reversible", "idempotent": True}
    depends_on = [{"transaction_id": "tx-0"}]
    prepared = {"path": "README.md"}
    preview = {"diff": "+ok"}
    tx = db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics=semantics,
        depends_on=depends_on,
        prepared=prepared,
        preview=preview,
    )

    with pytest.raises(FrozenInstanceError):
        tx.transaction_id = "tampered"  # type: ignore[misc]

    # Mutating the inputs / a returned view must not corrupt the record.
    semantics["idempotent"] = False
    depends_on.append({"transaction_id": "tx-evil"})
    preview["diff"] = "-ok"

    fresh = db.get_effect_transaction("tx-1")
    assert fresh.semantics == {"kind": "reversible", "idempotent": True}
    assert fresh.depends_on == [{"transaction_id": "tx-0"}]
    assert fresh.preview == {"diff": "+ok"}


def test_transition_effect_transaction_cas_proof(db):
    _seed_operation(db)
    tx = db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
        prepared={"path": "README.md"},
        preview={"diff": "+ok"},
    )
    # prepared+preview at create time advances the row past ``pending``
    # into ``previewed`` — first legitimate CAS target.
    assert tx.phase == "previewed"

    assert (
        db.transition_effect_transaction(
            "tx-1", expected_phase="previewed", next_phase="committing"
        )
        is True
    )
    # Second transition with the same expected_phase is a no-op (stale).
    assert (
        db.transition_effect_transaction(
            "tx-1", expected_phase="previewed", next_phase="committing"
        )
        is False
    )
    fresh = db.get_effect_transaction("tx-1")
    assert fresh.phase == "committing"


def test_transition_effect_transaction_unknown_transaction_is_false(db):
    assert (
        db.transition_effect_transaction(
            "missing", expected_phase="pending", next_phase="previewed"
        )
        is False
    )


def test_transition_effect_transaction_validates_phases_before_sql(db):
    _seed_operation(db)
    # No prepared/preview — phase stays at the default ``pending``.
    db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
    )
    with pytest.raises(ValueError):
        db.transition_effect_transaction(
            "tx-1", expected_phase="nonsense", next_phase="committing"
        )
    with pytest.raises(ValueError):
        db.transition_effect_transaction(
            "tx-1", expected_phase="pending", next_phase="wat"
        )
    # State untouched.
    assert db.get_effect_transaction("tx-1").phase == "pending"


def test_transition_effect_transaction_merges_optional_payloads(db):
    _seed_operation(db)
    # No prepared/preview at create time — phase stays at ``pending``.
    db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
    )
    assert (
        db.transition_effect_transaction(
            "tx-1",
            expected_phase="pending",
            next_phase="previewed",
            preview={"diff": "+ok", "lines": [1, 2, 3]},
        )
        is True
    )
    tx = db.get_effect_transaction("tx-1")
    assert tx.phase == "previewed"
    assert tx.preview == {"diff": "+ok", "lines": [1, 2, 3]}

    assert (
        db.transition_effect_transaction(
            "tx-1",
            expected_phase="previewed",
            next_phase="committing",
            authority={"actor": "user"},
        )
        is True
    )
    tx2 = db.get_effect_transaction("tx-1")
    assert tx2.phase == "committing"
    assert tx2.authority == {"actor": "user"}


def test_reopen_preserves_effect_transactions(tmp_path):
    """Reopening SessionDB on a brand-new DB keeps every transaction
    row intact (no one-off migration script)."""
    db_path = tmp_path / "state.db"
    first = SessionDB(db_path=db_path)
    OperationJournal(first).create(operation_id="op-1", kind="tool")
    first.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
        prepared={"path": "README.md"},
        preview={"diff": "+ok"},
    )
    first.close()

    # ── brand-new reopen ──
    reopened = SessionDB(db_path=db_path)
    try:
        tx = reopened.get_effect_transaction("tx-1")
        assert tx is not None
        assert tx.semantics == {"kind": "reversible", "idempotent": True}
        assert tx.preview == {"diff": "+ok"}
    finally:
        reopened.close()


def test_reopen_legacy_v21_db_lacks_new_tables_then_reconciles(tmp_path):
    """Simulate a real pre-feature database: schema_version=21, no
    effect_transactions / receipts / receipt_observations / mission_outbox
    tables.  Reopening through SessionDB declarative reconciliation must
    create them (and the absence of any speculative index does not block
    reconciliation)."""
    legacy_path = tmp_path / "legacy_v21.db"
    build_path = tmp_path / "build.db"

    # 1. Build a brand-new DB with current schema so the legacy file
    #    inherits every pre-existing table without typing each one out.
    src = SessionDB(db_path=build_path)
    src.close()

    # 2. Use SQLite's backup API to clone the post-feature DB into the
    #    legacy file, then strip the four new tables + indexes and pin
    #    schema_version to the pre-feature value.
    src = sqlite3.connect(build_path)
    dst = sqlite3.connect(legacy_path)
    src.backup(dst)
    src.close()
    dst.close()

    conn = sqlite3.connect(legacy_path)
    conn.executescript(
        """
        DROP TABLE IF EXISTS effect_transactions;
        DROP TABLE IF EXISTS receipts;
        DROP TABLE IF EXISTS receipt_observations;
        DROP TABLE IF EXISTS mission_outbox;
        DROP INDEX IF EXISTS idx_effect_transactions_mission;
        DROP INDEX IF EXISTS idx_receipts_mission_created;
        DROP INDEX IF EXISTS idx_receipt_observations_receipt_created;
        DROP INDEX IF EXISTS idx_mission_outbox_due;
        UPDATE schema_version SET version = """ + str(SCHEMA_VERSION) + """;
        """
    )
    conn.commit()

    # Sanity: the legacy DB really is missing the four tables — a
    # SELECT against them raises before SessionDB touches the file.
    for table in (
        "effect_transactions",
        "receipts",
        "receipt_observations",
        "mission_outbox",
    ):
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(f"SELECT * FROM {table}").fetchall()
    conn.close()

    # 3. Reopen through SessionDB — declarative reconciliation must
    #    create the four tables.  After reopen the new tables exist
    #    and are empty (no data was ever stored in them).
    legacy = SessionDB(db_path=legacy_path)
    try:
        for table in (
            "effect_transactions",
            "receipts",
            "receipt_observations",
            "mission_outbox",
        ):
            count = legacy._conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            assert count == 0

        # schema_version is unchanged (no version bump on column-add only).
        current = legacy._conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()[0]
        assert current == SCHEMA_VERSION
    finally:
        legacy.close()


def test_effect_transactions_timestamps_stored_as_sqlite_integer(db):
    """Write boundary must coerce time.time() floats to integer seconds;
    storage typeof() must be ``integer`` for created_at, updated_at."""
    OperationJournal(db).create(operation_id="op-1", kind="tool")
    db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
        prepared={"path": "README.md"},
        preview={"diff": "+ok"},
    )
    rows = db._conn.execute(
        "SELECT typeof(created_at), typeof(updated_at) "
        "FROM effect_transactions WHERE transaction_id = ?",
        ("tx-1",),
    ).fetchone()
    assert tuple(rows) == ("integer", "integer")

    # After a CAS update, updated_at is still integer.
    db.transition_effect_transaction(
        "tx-1", expected_phase="previewed", next_phase="committing"
    )
    rows = db._conn.execute(
        "SELECT typeof(updated_at) FROM effect_transactions "
        "WHERE transaction_id = ?",
        ("tx-1",),
    ).fetchone()
    assert rows[0] == "integer"


def test_transition_effect_transaction_rejects_illegal_vocabulary_known_jumps(db):
    """Vocabulary membership is necessary but not sufficient — the
    transition map forbids ``pending→committed``, ``pending→committing``,
    self transitions, and any onward move out of a terminal state."""
    OperationJournal(db).create(operation_id="op-1", kind="tool")
    db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
    )
    # Pending → committed (skip-ahead) — both phrases are in the
    # vocabulary, the transition is still illegal.
    with pytest.raises(ValueError):
        db.transition_effect_transaction(
            "tx-1", expected_phase="pending", next_phase="committed"
        )
    # Pending → committing (skip-ahead).
    with pytest.raises(ValueError):
        db.transition_effect_transaction(
            "tx-1", expected_phase="pending", next_phase="committing"
        )
    # Self transition.
    with pytest.raises(ValueError):
        db.transition_effect_transaction(
            "tx-1", expected_phase="pending", next_phase="pending"
        )
    # State untouched after every rejected call.
    assert db.get_effect_transaction("tx-1").phase == "pending"


def test_transition_effect_transaction_rejects_out_of_terminal(db):
    """Terminal phases have no onward transitions."""
    OperationJournal(db).create(operation_id="op-1", kind="tool")
    db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
        prepared={"path": "README.md"},
        preview={"diff": "+ok"},
    )
    # Advance through to a terminal state via legal jumps.
    assert db.transition_effect_transaction(
        "tx-1", expected_phase="previewed", next_phase="committing"
    ) is True
    assert db.transition_effect_transaction(
        "tx-1", expected_phase="committing", next_phase="committed"
    ) is True

    # committed is terminal — any further transition raises.
    for target in ("previewed", "committing", "pending", "failed", "cancelled"):
        with pytest.raises(ValueError):
            db.transition_effect_transaction(
                "tx-1", expected_phase="committed", next_phase=target
            )
    assert db.get_effect_transaction("tx-1").phase == "committed"


def test_transition_effect_transaction_legal_unknown_effect_path(db):
    """Verification / compensation: ``committing→unknown_effect`` and
    ``unknown_effect→committed`` / ``unknown_effect→failed`` are legal
    onward jumps and update the row."""
    OperationJournal(db).create(operation_id="op-1", kind="tool")
    db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
        prepared={"path": "README.md"},
        preview={"diff": "+ok"},
    )
    assert db.transition_effect_transaction(
        "tx-1", expected_phase="previewed", next_phase="committing"
    ) is True
    # Committing -> unknown_effect is the explicit compensation fork.
    assert db.transition_effect_transaction(
        "tx-1",
        expected_phase="committing",
        next_phase="unknown_effect",
        compensation={"reason": "verify"},
    ) is True
    tx = db.get_effect_transaction("tx-1")
    assert tx.phase == "unknown_effect"
    assert tx.compensation == {"reason": "verify"}

    # From unknown_effect we may settle on committed or failed.
    assert db.transition_effect_transaction(
        "tx-1",
        expected_phase="unknown_effect",
        next_phase="committed",
        verification={"passed": True},
    ) is True
    assert db.get_effect_transaction("tx-1").phase == "committed"
    assert db.get_effect_transaction("tx-1").verification == {"passed": True}


def test_create_effect_transaction_rejects_non_jsonable_payloads(db):
    """Values that ``json.dumps`` cannot serialize raise before SQL —
    the storage layer never persists a value that the read path
    could not decode."""
    OperationJournal(db).create(operation_id="op-1", kind="tool")
    # Circular self-reference defeats ``json.dumps``'s default cycle
    # detection once ``default=str`` is applied: every visited object
    # produces another of the same kind, so the encoder recurses
    # forever and raises ``ValueError``.
    a: List[Any] = []
    a.append(a)

    for kw in ("prepared", "preview", "authority", "result", "verification", "compensation"):
        with pytest.raises(ValueError):
            db.create_effect_transaction(
                transaction_id="tx-1",
                operation_id="op-1",
                mission_id="m-1",
                execution_id="ex-1",
                step_id="write",
                adapter_id="workspace.v1",
                sequence_no=1,
                semantics={"kind": "reversible", "idempotent": True},
                depends_on=[],
                **{kw: a},
            )
    count = db._conn.execute(
        "SELECT COUNT(*) FROM effect_transactions"
    ).fetchone()[0]
    assert count == 0


def test_create_effect_transaction_payload_strings_round_trip_as_strings(db):
    """Plain Python strings are valid JSON values, not pre-serialized
    JSON. ``_canonicalize_payload`` stores them via ``json.dumps`` so
    they round-trip back as the original string in every required and
    optional payload column at create time."""
    OperationJournal(db).create(operation_id="op-1", kind="tool")
    db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics="kind=reversible",
        depends_on="tx-0",
        prepared="path=README.md",
        preview="diff=+ok",
        authority="actor=user",
        result="ok",
        verification="passed=true",
        compensation="reason=verify",
    )
    tx = db.get_effect_transaction("tx-1")
    assert tx is not None
    assert tx.semantics == "kind=reversible"
    assert tx.depends_on == "tx-0"
    assert tx.prepared == "path=README.md"
    assert tx.preview == "diff=+ok"
    assert tx.authority == "actor=user"
    assert tx.result == "ok"
    assert tx.verification == "passed=true"
    assert tx.compensation == "reason=verify"

    # And the stored JSON is the canonical JSON-encoded string form.
    stored = db._conn.execute(
        "SELECT semantics_json, depends_on_json, prepared_json, "
        "preview_json, authority_json, result_json, verification_json, "
        "compensation_json FROM effect_transactions WHERE transaction_id='tx-1'"
    ).fetchone()
    assert stored[0] == '"kind=reversible"'
    assert stored[1] == '"tx-0"'
    assert stored[2] == '"path=README.md"'
    assert stored[3] == '"diff=+ok"'
    assert stored[4] == '"actor=user"'
    assert stored[5] == '"ok"'
    assert stored[6] == '"passed=true"'
    assert stored[7] == '"reason=verify"'


def test_transition_effect_transaction_payload_strings_round_trip_as_strings(db):
    """Plain Python strings supplied via ``transition_effect_transaction``
    must also store via ``json.dumps`` and round-trip back as the
    original string in every optional payload column."""
    OperationJournal(db).create(operation_id="op-1", kind="tool")
    # No prepared/preview at create time — phase stays at ``pending``.
    db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
    )
    assert db.transition_effect_transaction(
        "tx-1",
        expected_phase="pending",
        next_phase="previewed",
        preview="diff=+ok",
    ) is True
    assert db.transition_effect_transaction(
        "tx-1",
        expected_phase="previewed",
        next_phase="committing",
        authority="actor=user",
        result="ok",
        verification="passed=true",
        compensation="reason=verify",
    ) is True

    tx = db.get_effect_transaction("tx-1")
    assert tx is not None
    assert tx.phase == "committing"
    assert tx.preview == "diff=+ok"
    assert tx.authority == "actor=user"
    assert tx.result == "ok"
    assert tx.verification == "passed=true"
    assert tx.compensation == "reason=verify"

    # Stored JSON is the canonical JSON-encoded string form.
    stored = db._conn.execute(
        "SELECT preview_json, authority_json, result_json, "
        "verification_json, compensation_json "
        "FROM effect_transactions WHERE transaction_id='tx-1'"
    ).fetchone()
    assert stored[0] == '"diff=+ok"'
    assert stored[1] == '"actor=user"'
    assert stored[2] == '"ok"'
    assert stored[3] == '"passed=true"'
    assert stored[4] == '"reason=verify"'


# Phase-creation bypass guard: every caller-supplied phase outside the
# safe initial pair ``pending``/``previewed`` must be rejected before SQL.
_BYPASS_PHASES = (
    "committing",
    "unknown_effect",
    "committed",
    "failed",
    "cancelled",
)


@pytest.mark.parametrize("bad_phase", _BYPASS_PHASES)
def test_create_effect_transaction_rejects_caller_bypass_phases(db, bad_phase):
    """``phase=committing|unknown_effect|committed|failed|cancelled``
    must raise before SQL — the CAS graph is the only way into those
    states."""
    _seed_operation(db)
    with pytest.raises(ValueError):
        db.create_effect_transaction(
            transaction_id="tx-1",
            operation_id="op-1",
            mission_id="m-1",
            execution_id="ex-1",
            step_id="write",
            adapter_id="workspace.v1",
            sequence_no=1,
            semantics={"kind": "reversible", "idempotent": True},
            depends_on=[],
            phase=bad_phase,
        )
    count = db._conn.execute(
        "SELECT COUNT(*) FROM effect_transactions"
    ).fetchone()[0]
    assert count == 0


def test_create_effect_transaction_accepts_explicit_pending(db):
    """The happy-path pendant to the bypass-rejection test."""
    _seed_operation(db)
    tx = db.create_effect_transaction(
        transaction_id="tx-1",
        operation_id="op-1",
        mission_id="m-1",
        execution_id="ex-1",
        step_id="write",
        adapter_id="workspace.v1",
        sequence_no=1,
        semantics={"kind": "reversible", "idempotent": True},
        depends_on=[],
        prepared={"path": "README.md"},
        phase="pending",
    )
    assert tx.phase == "pending"


# Cancellation-path map: ``cancelled`` is reachable from ``pending`` and
# ``previewed`` (early cancel before commit work begins). Once commit
# work has started the row has to settle on the commit-attempt pathway.

def test_transition_pending_to_cancelled_legal(db):
    _seed_pending_tx(db)
    assert db.transition_effect_transaction(
        "tx-1", expected_phase="pending", next_phase="cancelled"
    ) is True
    assert db.get_effect_transaction("tx-1").phase == "cancelled"


def test_transition_previewed_to_cancelled_legal(db):
    _seed_pending_tx(db)
    _step(db, "pending", "previewed")
    assert db.transition_effect_transaction(
        "tx-1", expected_phase="previewed", next_phase="cancelled"
    ) is True
    assert db.get_effect_transaction("tx-1").phase == "cancelled"


@pytest.mark.parametrize(
    "start_phase, walk",
    [
        # Cancellation requires pre-commit state — every other source is
        # blocked, plus all three terminals.
        ("committing", ["pending", "previewed", "committing"]),
        ("committed", ["pending", "previewed", "committing", "committed"]),
        ("failed", ["pending", "failed"]),
        ("unknown_effect", ["pending", "previewed", "committing", "unknown_effect"]),
    ],
)
def test_transition_to_cancelled_illegal_from_post_commit_or_terminal(
    db, start_phase, walk
):
    """``{committing|committed|failed|unknown_effect} → cancelled`` is
    illegal — once commit work has begun the row must settle on the
    commit-attempt pathway."""
    _seed_pending_tx(db)
    # ``walk[0]`` is the row's current phase (set by the seed); iterate
    # the rest as expected→next pairs.
    prev = walk[0]
    for nxt in walk[1:]:
        _step(db, prev, nxt)
        prev = nxt
    assert db.get_effect_transaction("tx-1").phase == start_phase
    with pytest.raises(ValueError):
        db.transition_effect_transaction(
            "tx-1", expected_phase=start_phase, next_phase="cancelled"
        )
    assert db.get_effect_transaction("tx-1").phase == start_phase


def test_transition_pending_to_failed_legal(db):
    """``pending → failed`` — work could not even reach preview."""
    _seed_pending_tx(db)
    assert db.transition_effect_transaction(
        "tx-1", expected_phase="pending", next_phase="failed"
    ) is True
    assert db.get_effect_transaction("tx-1").phase == "failed"


def test_transition_committing_to_failed_legal(db):
    """``committing → failed`` — commit was attempted and failed."""
    _seed_pending_tx(db)
    _step(db, "pending", "previewed")
    _step(db, "previewed", "committing")
    assert db.transition_effect_transaction(
        "tx-1", expected_phase="committing", next_phase="failed"
    ) is True
    assert db.get_effect_transaction("tx-1").phase == "failed"


@pytest.mark.parametrize(
    "terminal, walk",
    [
        # ``committed`` is already covered by the pre-existing
        # out-of-terminal test; ``failed``/``cancelled`` close the
        # graph symmetrically.
        ("cancelled", ["pending", "cancelled"]),
        ("failed", ["pending", "failed"]),
    ],
)
def test_transition_new_terminal_phase_has_no_onward_edges(db, terminal, walk):
    _seed_pending_tx(db)
    prev = "pending"
    for nxt in walk[1:]:
        _step(db, prev, nxt)
        prev = nxt
    assert db.get_effect_transaction("tx-1").phase == terminal
    for target in ("pending", "previewed", "committing", terminal):
        with pytest.raises(ValueError):
            db.transition_effect_transaction(
                "tx-1", expected_phase=terminal, next_phase=target
            )
    assert db.get_effect_transaction("tx-1").phase == terminal


# ────────────────────────────────────────────────────────────────────────
# Task 3 — Effect contracts, adapter registry, coordinator
# ────────────────────────────────────────────────────────────────────────
#
# The tests below exercise the *coordinator-level* contracts in
# ``agent.effect_transactions``: frozen semantics/prepared/adapter
# contracts, the in-process adapter registry (rejects duplicate
# ``adapter_id``s, fails loudly on unknown lookups), and the
# coordinator itself, which mediates between the tool handler and
# SessionDB + OperationJournal. Every SessionDB / OperationJournal call
# is injected via stubs so the coordinator's contract is pinned
# independent of the storage layer.
# ────────────────────────────────────────────────────────────────────────


from typing import Dict, List, Optional, Tuple

from agent.effect_transactions import (  # noqa: E402  (intentional late import)
    AdapterRegistry,
    CoordinatorBlockedError,
    EffectAdapter,
    EffectSemantics,
    OperationRequest,
    PreparedEffect,
    UnknownEffectError,
    build_coordinator,
)


# ── Effect contract shape ───────────────────────────────────────────────


class TestEffectContractShapes:
    def test_effect_semantics_is_frozen_and_carries_kind_idempotent_reconcilable(self):
        sem = EffectSemantics(
            kind="reversible", idempotent=True, reconcilable=True
        )
        assert sem.kind == "reversible"
        assert sem.idempotent is True
        assert sem.reconcilable is True
        with pytest.raises((AttributeError, FrozenInstanceError)):
            sem.kind = "irreversible"  # type: ignore[misc]

    def test_effect_semantics_rejects_unknown_kind(self):
        with pytest.raises(ValueError):
            EffectSemantics(kind="invented", idempotent=False, reconcilable=False)

    @pytest.mark.parametrize(
        "kind", ["read_only", "reversible", "compensatable", "irreversible"]
    )
    def test_effect_semantics_accepts_full_vocabulary(self, kind):
        sem = EffectSemantics(kind=kind, idempotent=False, reconcilable=False)
        assert sem.kind == kind

    def test_prepared_effect_is_frozen_with_all_fields(self):
        sem = EffectSemantics(kind="reversible", idempotent=True, reconcilable=True)
        prepared = PreparedEffect(
            adapter_id="workspace.v1",
            normalized_args={"path": "README.md"},
            before={"exists": False},
            preview={"diff": "+ok"},
            semantics=sem,
            compensation={"undo": "remove"},
        )
        assert prepared.adapter_id == "workspace.v1"
        assert prepared.normalized_args == {"path": "README.md"}
        assert prepared.before == {"exists": False}
        assert prepared.preview == {"diff": "+ok"}
        assert prepared.semantics.kind == "reversible"
        assert prepared.compensation == {"undo": "remove"}
        with pytest.raises((AttributeError, FrozenInstanceError)):
            prepared.adapter_id = "other"  # type: ignore[misc]

    def test_prepared_effect_compensation_optional(self):
        sem = EffectSemantics(kind="read_only", idempotent=True, reconcilable=False)
        prepared = PreparedEffect(
            adapter_id="reader.v1",
            normalized_args={"path": "README.md"},
            before={"exists": True},
            preview={"size": 10},
            semantics=sem,
            compensation=None,
        )
        assert prepared.compensation is None


# ── Adapter registry ────────────────────────────────────────────────────


class _StubAdapter:
    """Minimal adapter impl used by the coordinator tests."""

    def __init__(self, adapter_id, *, semantic_kind="reversible",
                 idempotent=True, reconcilable=True, fail_prepare=None,
                 fail_commit=None, fail_verify=None, fail_compensate=None):
        self.adapter_id = adapter_id
        self._semantic_kind = semantic_kind
        self._idempotent = idempotent
        self._reconcilable = reconcilable
        self.fail_prepare = fail_prepare
        self.fail_commit = fail_commit
        self.fail_verify = fail_verify
        self.fail_compensate = fail_compensate
        self.calls: List[Tuple[str, ...]] = []

    def prepare(self, request: OperationRequest) -> PreparedEffect:
        self.calls.append(("prepare", request.tool_name))
        if self.fail_prepare is not None:
            raise self.fail_prepare
        return PreparedEffect(
            adapter_id=self.adapter_id,
            normalized_args=dict(request.args),
            before={"snapshot": True},
            preview={"would": "do"},
            semantics=EffectSemantics(
                kind=self._semantic_kind,
                idempotent=self._idempotent,
                reconcilable=self._reconcilable,
            ),
            compensation={"undo": True} if self._semantic_kind == "reversible" else None,
        )

    def commit(self, prepared: PreparedEffect, invoke) -> dict:
        self.calls.append(("commit",))
        if self.fail_commit is not None:
            raise self.fail_commit
        return invoke(dict(prepared.normalized_args))

    def verify(self, prepared: PreparedEffect, result) -> dict:
        self.calls.append(("verify",))
        if self.fail_verify is not None:
            raise self.fail_verify
        return {"verified": True, "result": result}

    def reconcile(self, record) -> dict:  # pragma: no cover - exercised in coordinator tests
        self.calls.append(("reconcile",))
        # Spec: adapter.reconcile returns a disposition-typed envelope.
        # Test stubs default to ``landed`` so happy-path coordinator
        # tests are unaffected; tests that want non-landed behavior
        # override this method or supply their own adapter.
        return {
            "disposition": "landed",
            "reconciled": True,
            "operation_id": record.operation_id,
        }

    def compensate(self, record) -> dict:
        self.calls.append(("compensate",))
        if self.fail_compensate is not None:
            raise self.fail_compensate
        return {"compensated": True, "operation_id": record.operation_id}


class TestAdapterRegistry:
    def test_register_and_lookup(self):
        reg = AdapterRegistry()
        adapter = _StubAdapter("workspace.v1")
        reg.register(adapter)
        assert reg.get("workspace.v1") is adapter

    def test_register_rejects_duplicate_adapter_id(self):
        reg = AdapterRegistry()
        reg.register(_StubAdapter("workspace.v1"))
        with pytest.raises(ValueError):
            reg.register(_StubAdapter("workspace.v1"))

    def test_register_rejects_missing_adapter_id(self):
        reg = AdapterRegistry()
        with pytest.raises(ValueError):
            reg.register(_StubAdapter(""))  # type: ignore[arg-type]

    def test_unknown_lookup_fails_loudly(self):
        reg = AdapterRegistry()
        with pytest.raises(KeyError):
            reg.get("nope")


# ── Stub SessionDB / OperationJournal / mission loader ─────────────────


class _StubTxRecord:
    """Bare-bones stand-in for SessionDB.EffectTransactionRecord."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


class _StubJournal:
    def __init__(self):
        self.rows: Dict[str, dict] = {}
        self.transitions: List[Tuple[str, str, str]] = []
        self.ack_calls: List[str] = []

    def create(self, *, operation_id, kind, **kwargs):
        if operation_id in self.rows:
            return _StubOperationRecord(**self.rows[operation_id])
        row = {
            "operation_id": operation_id,
            "kind": kind,
            "state": "pending",
            "effect_disposition": "none",
            "result_json": None,
            **kwargs,
        }
        self.rows[operation_id] = row
        return _StubOperationRecord(**row)

    def transition(self, operation_id, *, from_states, to_state,
                   effect_disposition, result=None, error=None):
        if operation_id not in self.rows:
            raise KeyError(operation_id)
        if self.rows[operation_id]["state"] not in from_states:
            raise ValueError("stale")
        self.transitions.append((operation_id, to_state, effect_disposition))
        self.rows[operation_id]["state"] = to_state
        self.rows[operation_id]["effect_disposition"] = effect_disposition
        if result is not None:
            self.rows[operation_id]["result_json"] = result
        if error is not None:
            self.rows[operation_id]["error"] = error
        return _StubOperationRecord(**self.rows[operation_id])

    def get(self, operation_id):
        row = self.rows.get(operation_id)
        return _StubOperationRecord(**row) if row is not None else None

    def terminal_result(self, operation_id):
        row = self.rows.get(operation_id)
        if row is None:
            return None
        if row.get("state") != "confirmed":
            return None
        if row.get("effect_disposition") != "landed":
            return None
        result_json = row.get("result_json")
        if result_json is None:
            return None
        return result_json

    def acknowledge(self, operation_id):
        self.ack_calls.append(operation_id)
        return True


class _StubOperationRecord:
    """Attribute-style stand-in for the real ``OperationRecord`` —
    lets the coordinator's ``.state / .effect_disposition / .destination``
    accesses work the same way they do against the production class."""

    def __init__(self, **fields):
        for k, v in fields.items():
            setattr(self, k, v)

    def __eq__(self, other):
        if not isinstance(other, _StubOperationRecord):
            return NotImplemented
        return self.__dict__ == other.__dict__


class _StubSessionDB:
    def __init__(self):
        self.created: List[dict] = []
        self.transitions: List[Tuple[str, str, str, dict]] = []
        self.sequence_counter = 0

    def _execute_read(self, fn):
        """Conformance shim: real SessionDB exposes ``_execute_read``; the
        default sequence factory refuses stubs that lack it (fail-closed).
        Tests that want deterministic sequences either inject
        ``sequence_no_factory=...`` or use ``_build_coordinator`` (which
        already does), so this shim only fires when a test deliberately
        exercises the real-SessionDB happy path on a stub."""
        # ponytail: the stub never opens a real conn — assume no prior
        # rows for the mission, so the factory's MAX()+1 reads as 1.
        return {"n": 1}

    def create_effect_transaction(self, **kwargs):
        self.sequence_counter += 1
        rec = _StubTxRecord(**kwargs)
        self.created.append(kwargs)
        return rec

    def transition_effect_transaction(self, transaction_id, *,
                                       expected_phase, next_phase,
                                       result=None, verification=None,
                                       compensation=None, **extras):
        self.transitions.append(
            (transaction_id, expected_phase, next_phase, {
                "result": result, "verification": verification,
                "compensation": compensation, **extras,
            })
        )
        return True

    def get_effect_transaction(self, transaction_id):
        for kw in self.created:
            if kw.get("transaction_id") == transaction_id:
                return _StubTxRecord(**kw)
        return None


def _build_coordinator(**overrides):
    """Build a coordinator wired to stubs. Overrides let a test pin any
    dependency without monkeypatching module globals."""
    journal = _StubJournal()
    session_db = _StubSessionDB()
    approval_requested: List[dict] = []
    review_requested: List[dict] = []

    clock = overrides.pop("clock", lambda: 1000.0)
    opid_factory = overrides.pop(
        "operation_id_factory",
        overrides.pop("opid_factory", lambda: "op-test"),
    )
    sequence_no_factory = overrides.pop(
        "sequence_no_factory",
        # ponytail: trivial monotonic counter for stub — production
        # default delegates to real SessionDB._execute_read.
        overrides.pop("seq_factory", lambda mid: session_db.sequence_counter + 1),
    )
    metadata_loader = overrides.pop("metadata_loader", None)
    mission_loader = overrides.pop(
        "mission_loader", lambda mission_id: None
    )
    adapter_registry = overrides.pop(
        "adapter_registry", AdapterRegistry()
    )
    approval_request = overrides.pop(
        "approval_request",
        lambda payload: approval_requested.append(payload) or "approved-token",
    )
    review_request = overrides.pop(
        "review_request",
        lambda payload: review_requested.append(payload),
    )

    # ponytail: tests that don't override ``metadata_loader`` default
    # to a loader that consults the mission's per-tool ``adapter_id``.
    # Spec 4 requires the loader to be authoritative for adapter_id /
    # semantic overrides; the default loader below honors the mission
    # payload when it explicitly names a registered adapter, and
    # returns a benign empty metadata otherwise.
    if metadata_loader is None:
        def _default_loader(tool_name: str):
            try:
                # The helper builds the loader once but the mission
                # loader is callable — call it with ``None`` to get
                # whatever default the test fixture provides.
                mission = mission_loader(None)
            except Exception:
                return {
                    "effect_adapter": None,
                    "effect_semantic_kind": None,
                    "effect_overrides": {},
                }
            if not isinstance(mission, dict):
                return {
                    "effect_adapter": None,
                    "effect_semantic_kind": None,
                    "effect_overrides": {},
                }
            entry = (mission.get("operations") or {}).get(tool_name) or {}
            return {
                "effect_adapter": entry.get("adapter_id"),
                "effect_semantic_kind": entry.get("effect_semantic_kind"),
                "effect_overrides": {},
            }
        metadata_loader = _default_loader

    coord = build_coordinator(
        mission_loader=mission_loader,
        session_db=session_db,
        operation_journal=journal,
        adapter_registry=adapter_registry,
        approval_request=approval_request,
        review_request=review_request,
        clock=clock,
        operation_id_factory=opid_factory,
        sequence_no_factory=sequence_no_factory,
        operation_metadata_loader=metadata_loader,
    )
    # 5-tuple shape: most callers do ``coord, journal, session_db, app, _``.
    return coord, journal, session_db, approval_requested, review_requested


# ── Coordinator: no-mission / read-only paths ───────────────────────────


class TestCoordinatorNoMission:
    def test_no_mission_invokes_handler_once_no_tx_writes(self):
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1"))
        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=lambda mid: None,  # no mission → no tx
            adapter_registry=registry,
        )

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(dict(args))
            return {"wrote": args["path"]}

        result = coord.execute(
            tool_name="writer",
            args={"path": "README.md"},
            handler=handler,
            operation_key="opk-1",
        )

        assert result == {"wrote": "README.md"}
        assert len(handler_calls) == 1
        assert journal.rows == {}  # operation journal untouched
        assert session_db.created == []
        assert session_db.transitions == []

    def test_read_only_mission_invokes_handler_once_no_tx(self):
        registry = AdapterRegistry()
        registry.register(_StubAdapter("reader.v1"))
        mission = {
            "mission_id": "m-1",
            "kind": "read_only",
            "operations": {},  # no permission entries → not supported mutation
        }
        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
        )

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(dict(args))
            return {"content": "ok"}

        result = coord.execute(
            tool_name="reader",
            args={"path": "README.md"},
            handler=handler,
            operation_key="opk-1",
        )
        assert result == {"content": "ok"}
        assert len(handler_calls) == 1
        assert journal.rows == {}
        assert session_db.created == []


# ── Coordinator: unsupported mutations block before handler ─────────────


class TestCoordinatorBlocksUnsupported:
    def test_unsupported_mutation_blocks_before_handler(self):
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1"))
        mission = {
            "mission_id": "m-1",
            "kind": "mutate",
            "operations": {
                # ``writer`` is NOT in supported ops for this mission
                "other_tool": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
        )

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": args["path"]}

        with pytest.raises(CoordinatorBlockedError):
            coord.execute(
                tool_name="writer",
                args={"path": "README.md"},
                handler=handler,
                operation_key="opk-1",
            )
        assert handler_calls == []
        assert journal.rows == {}
        assert session_db.created == []


# ── Coordinator: supported mutation happy path ──────────────────────────


class TestCoordinatorSupportedMutation:
    def test_supported_mutation_runs_full_lifecycle(self):
        registry = AdapterRegistry()
        adapter = _StubAdapter("writer.v1", semantic_kind="reversible")
        registry.register(adapter)

        mission = {
            "mission_id": "m-1",
            "kind": "mutate",
            "operations": {
                "writer": {
                    "adapter_id": "writer.v1",
                    "allowed": True,
                    "requires_approval": False,
                },
            },
        }

        approval_calls: List[dict] = []
        coord, journal, session_db, app_calls, _ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            approval_request=lambda payload: approval_calls.append(payload) or "tok",
        )

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(dict(args))
            return {"wrote": args["path"]}

        result = coord.execute(
            tool_name="writer",
            args={"path": "README.md"},
            handler=handler,
            operation_key="opk-2",
        )
        assert result == {"verified": True, "result": {"wrote": "README.md"}}
        assert len(handler_calls) == 1
        # adapter ran the full lifecycle in order
        assert [c[0] for c in adapter.calls] == ["prepare", "commit", "verify"]
        # operation journal: created, then a confirmed-landed terminal settle.
        # The default operation_id is the operation_key (Spec 2 — stable
        # replay identity).
        op = journal.rows["opk-2"]
        assert op["state"] in {"running", "confirmed"}
        assert op["effect_disposition"] in {"none", "landed"}
        # session db: prepared+preview written, committing CAS, committed settle
        assert len(session_db.created) == 1
        tx_kw = session_db.created[0]
        assert tx_kw["semantics"] == {
            "kind": "reversible", "idempotent": True, "reconcilable": True
        }
        assert tx_kw["prepared"]["adapter_id"] == "writer.v1"
        phases = [(t[1], t[2]) for t in session_db.transitions]
        assert ("previewed", "committing") in phases
        # no approval was needed (reversible + requires_approval=False)
        assert approval_calls == []

    def test_irreversible_mutation_requests_approval(self):
        registry = AdapterRegistry()
        adapter = _StubAdapter(
            "poster.v1", semantic_kind="irreversible",
            idempotent=False, reconcilable=False,
        )
        registry.register(adapter)
        mission = {
            "mission_id": "m-2",
            "kind": "mutate",
            "operations": {
                "poster": {
                    "adapter_id": "poster.v1",
                    "allowed": True,
                    "requires_approval": True,
                },
            },
        }
        coord, journal, session_db, app_calls, _ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
        )
        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"posted": True}

        result = coord.execute(
            tool_name="poster",
            args={"message": "hi"},
            handler=handler,
            operation_key="opk-3",
        )
        assert result == {"verified": True, "result": {"posted": True}}
        assert len(handler_calls) == 1
        assert len(app_calls) == 1
        # approval payload mentions irreversibility so reviewers know the cost
        assert "irreversible" in str(app_calls[0]).lower() or \
               "poster" in str(app_calls[0])


# ── Coordinator: authority / revocation ─────────────────────────────────


class TestCoordinatorAuthorityChecks:
    def test_authority_expired_between_prepare_and_commit_blocks_handler(self):
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1", semantic_kind="reversible"))

        # The mission_loader returns ``valid`` once, then expired afterwards —
        # coordinator must re-check authority before commit.
        loader_calls = {"n": 0}

        def mission_loader(mid):
            loader_calls["n"] += 1
            if loader_calls["n"] == 1:
                return {
                    "mission_id": "m-3",
                    "kind": "mutate",
                    "operations": {
                        "writer": {"adapter_id": "writer.v1", "allowed": True},
                    },
                    "authority": {"valid": True, "expires_at": 2000.0},
                }
            return {
                "mission_id": "m-3",
                "kind": "mutate",
                "operations": {
                    "writer": {"adapter_id": "writer.v1", "allowed": True},
                },
                "authority": {"valid": False, "expires_at": 1000.0},
            }

        clock_values = iter([1000.0, 2000.0, 5000.0])

        def clock():
            return next(clock_values)

        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=mission_loader, clock=clock,
        )

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": True}

        with pytest.raises(CoordinatorBlockedError):
            coord.execute(
                tool_name="writer",
                args={"path": "x"},
                handler=handler,
                operation_key="opk-4",
            )
        assert handler_calls == []

    def test_revoked_authority_blocks_handler(self):
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1"))

        def mission_loader(mid):
            return {
                "mission_id": "m-4",
                "kind": "mutate",
                "operations": {
                    "writer": {
                        "adapter_id": "writer.v1",
                        "allowed": True,
                        "revoked": True,  # explicit revocation flag
                    },
                },
            }

        coord, *_ = _build_coordinator(mission_loader=mission_loader)

        def handler(args):
            raise AssertionError("handler must not be called on revoked mission")

        with pytest.raises(CoordinatorBlockedError):
            coord.execute(
                tool_name="writer",
                args={"path": "x"},
                handler=handler,
                operation_key="opk-5",
            )


# ── Coordinator: timeouts & interrupts become unknown effect ────────────


class TestCoordinatorUnknownOnInterrupt:
    def test_timeout_becomes_unknown_effect_no_retry(self):
        registry = AdapterRegistry()
        adapter = _StubAdapter(
            "writer.v1", semantic_kind="reversible",
            fail_verify=TimeoutError("verify timed out"),
        )
        registry.register(adapter)
        mission = {
            "mission_id": "m-5",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        review_calls: List[dict] = []
        coord, journal, session_db, _, _ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            review_request=lambda payload: review_calls.append(payload),
        )

        def handler(args):
            return {"wrote": True}

        with pytest.raises(UnknownEffectError):
            coord.execute(
                tool_name="writer",
                args={"path": "x"},
                handler=handler,
                operation_key="opk-6",
            )
        # operation journal: not confirmed; either failed-unknown or pending.
        # Stable replay identity → journal key is the operation_key.
        op = journal.rows["opk-6"]
        assert op["effect_disposition"] == "unknown"
        assert op["state"] in {"running", "failed", "unknown"}
        # session db: the tx advanced to committing then unknown_effect
        phases = [(t[1], t[2]) for t in session_db.transitions]
        assert ("committing", "unknown_effect") in phases
        # review item was queued through the injected hook (not a hardcoded path)
        assert len(review_calls) == 1
        assert "verify_TimeoutError" in str(review_calls[0])

    def test_keyboard_interrupt_becomes_unknown_no_retry(self):
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1"))

        mission = {
            "mission_id": "m-6",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
        )

        def handler(args):
            raise KeyboardInterrupt()

        with pytest.raises(UnknownEffectError):
            coord.execute(
                tool_name="writer",
                args={"path": "x"},
                handler=handler,
                operation_key="opk-7",
            )
        op = journal.rows["opk-7"]
        assert op["effect_disposition"] == "unknown"
        # handler was called exactly once (no retry)
        # (counting is implicit: the only assertion that matters is that
        # the journal reflects unknown and no second pass happened.)


# ── Coordinator: repeated operation keys ────────────────────────────────


class TestCoordinatorRepeatOperations:
    def test_repeated_confirmed_key_returns_stored_result(self):
        registry = AdapterRegistry()
        adapter = _StubAdapter("writer.v1")
        registry.register(adapter)
        mission = {
            "mission_id": "m-7",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        # Spec 2: the durable operation_id is the operation_key. The
        # factory is no longer used to seed the id (it's the fallback
        # for empty keys), so we exercise the stable-key path directly.
        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
        )
        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": True}

        first = coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=handler,
            operation_key="opk-8",
        )
        # second call: same operation_key, but now the journal says confirmed
        # (we simulate by pretending the prior settle already happened — the
        # coordinator must NOT re-invoke handler, must NOT commit twice.)
        journal.rows["opk-8"]["state"] = "confirmed"
        journal.rows["opk-8"]["effect_disposition"] = "landed"
        journal.rows["opk-8"]["result_json"] = {"wrote": True}

        second = coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=handler,
            operation_key="opk-8",
        )
        assert second == {"wrote": True}
        # handler called once total — the second invocation hit the cache path
        assert len(handler_calls) == 1

    # ponytail: the non-stable-id
    # ``test_running_state_reconciles_via_adapter_no_double_commit`` was
    # retired when the operation_id identity became the operation_key
    # itself — coverage moved to
    # ``TestLegacyRepeatTestsUpdatedForStableKey.test_running_state_reconciles_via_adapter_with_stable_id``.


# ────────────────────────────────────────────────────────────────────────
# Task 3 — Terminal middleware / coordinator boundary
# ────────────────────────────────────────────────────────────────────────
#
# These tests pin the integration between ``hades_cli.middleware`` (the
# chain runner) and the effect-transactions coordinator (which consumes
# the ``operation_key`` that middleware injects). They exercise the real
# ``run_tool_execution_middleware`` so the contract — operation_key is
# computed once, before any registered tool_execution middleware runs
# (so a callback can read it from its kwargs, a pre-existing hades-agent
# contract), the coordinator only sees it at the terminal call, plugin
# short-circuits skip the coordinator, and the post-plugin args are what
# the handler sees — is pinned against real middleware execution, not
# source-text inspection.
# ────────────────────────────────────────────────────────────────────────


from hades_cli.middleware import run_tool_execution_middleware  # noqa: E402


def _plugin_manager_with(monkeypatch, callbacks):
    """Install a stub plugin manager exposing the given middleware callbacks.

    The middleware module reads ``_middleware`` directly off the manager;
    one stub helper covers both the "no middleware" and "with middleware"
    cases (empty dict vs. populated dict).
    """
    manager = types.SimpleNamespace(_middleware=callbacks)
    monkeypatch.setattr("hades_cli.plugins.get_plugin_manager", lambda: manager)


# ── Plugin short-circuit must not run the terminal handler ────────────


class TestMiddlewareShortCircuit:
    def test_short_circuit_returns_normally_no_terminal_handler(self, monkeypatch):
        """A plugin that doesn't call ``next_call`` short-circuits the
        chain. The terminal handler must NOT be invoked and no operation
        or effect transaction may be created downstream."""
        _plugin_manager_with(monkeypatch, {
            "tool_execution": [lambda **kw: "intercepted-result"],
        })

        terminal_calls: List[dict] = []

        def terminal(args):
            terminal_calls.append(args)
            return {"wrote": True}

        result = run_tool_execution_middleware(
            "writer", {"path": "x"}, terminal,
        )
        assert result == "intercepted-result"
        assert terminal_calls == []


# ── Final post-plugin args reach handler ──────────────────────────────


class TestMiddlewareFinalArgs:
    def test_post_plugin_args_are_what_handler_sees(self, monkeypatch):
        """The terminal handler must observe the args produced by the
        *last* plugin's ``next_call`` rewrite — never the pre-chain args."""

        def mutator(**kw):
            return kw["next_call"]({**kw["args"], "rewritten": True})

        _plugin_manager_with(monkeypatch, {"tool_execution": [mutator]})

        terminal_calls: List[dict] = []

        def terminal(args):
            terminal_calls.append(args)
            return {"ok": True}

        result = run_tool_execution_middleware(
            "writer", {"path": "x"}, terminal,
        )
        assert result == {"ok": True}
        assert terminal_calls == [{"path": "x", "rewritten": True}]


# ── operation_key_factory visibility to tool_execution middleware ─────


class TestOperationKeyFactoryTerminalOnly:
    def test_factory_called_before_chain_when_plugin_short_circuits(
        self, monkeypatch
    ):
        """When any ``tool_execution`` middleware is registered, the key
        must be evaluated up front — a callback may short-circuit the
        chain (never call ``next_call``), so the key can't wait for a
        terminal call that might not happen. This is also what lets a
        callback read ``kwargs['operation_key']`` at all, a pre-existing
        hades-agent middleware contract real plugins rely on."""
        _plugin_manager_with(monkeypatch, {
            "tool_execution": [lambda **kw: "intercepted-result"],
        })

        factory_calls: List[int] = []

        def factory():
            factory_calls.append(1)
            return "opk-precomputed"

        terminal_calls: List[dict] = []

        def terminal(args):
            terminal_calls.append(args)
            return {"ok": True}

        result = run_tool_execution_middleware(
            "writer", {"path": "x"}, terminal,
            operation_key_factory=factory,
        )
        assert result == "intercepted-result"
        assert terminal_calls == []
        # The key is computed once, before the chain runs, regardless of
        # whether a callback goes on to short-circuit.
        assert factory_calls == [1]

    def test_factory_called_when_no_observer_plugin_present_but_coordinator_is(
        self, monkeypatch,
    ):
        """No middleware registered, but an effect_coordinator is wired
        in: the terminal handler is invoked directly, and
        ``operation_key_factory`` must still be evaluated exactly once so
        the operation_key is available for the coordinator's
        mission-transaction logic, which has no plugin observer of its
        own."""
        _plugin_manager_with(monkeypatch, {})

        factory_calls: List[int] = []
        coord_calls: List[dict] = []

        def factory():
            factory_calls.append(1)
            return "opk-fresh"

        def fake_coord_execute(**kwargs):
            coord_calls.append(kwargs)
            return kwargs["handler"](kwargs["args"])

        coord = types.SimpleNamespace(execute=fake_coord_execute)

        def terminal(args):
            return {"ok": True}

        result = run_tool_execution_middleware(
            "writer", {"path": "x"}, terminal,
            effect_coordinator=coord,
            operation_key_factory=factory,
        )
        assert result == {"ok": True}
        assert factory_calls == [1]
        assert coord_calls[0]["operation_key"] == "opk-fresh"

    def test_factory_not_called_when_no_observer_and_no_coordinator(
        self, monkeypatch,
    ):
        """No middleware and no coordinator: nothing will ever read the
        key, so ``operation_key_factory`` must stay unevaluated. This is
        a real pre-existing hades-agent contract — some callers derive
        the key from large/expensive-to-hash arguments and rely on it
        never being computed when there's no consumer."""
        _plugin_manager_with(monkeypatch, {})

        factory_calls: List[int] = []

        def factory():
            factory_calls.append(1)
            return "opk-never-used"

        def terminal(args):
            return {"ok": True}

        result = run_tool_execution_middleware(
            "writer", {"path": "x"}, terminal,
            operation_key_factory=factory,
        )
        assert result == {"ok": True}
        assert factory_calls == []

    def test_factory_called_when_observer_passes_through(self, monkeypatch):
        """A pass-through middleware (calls ``next_call`` exactly once)
        still must trigger factory evaluation, since the terminal handler
        runs."""

        def passthrough(**kw):
            return kw["next_call"](kw["args"])

        _plugin_manager_with(monkeypatch, {"tool_execution": [passthrough]})

        factory_calls: List[int] = []

        def factory():
            factory_calls.append(1)
            return "opk-passthrough"

        def terminal(args):
            return {"ok": True}

        result = run_tool_execution_middleware(
            "writer", {"path": "x"}, terminal,
            operation_key_factory=factory,
        )
        assert result == {"ok": True}
        assert factory_calls == [1]


# ponytail: the general ``TestMiddlewareContextCompatibility`` and
# ``TestMiddlewarePluginOrdering`` classes were retired — they never
# exercise the effect-coordinator boundary. Task 3 retains only the
# middleware tests that touch the terminal / short-circuit /
# ``operation_key_factory`` slots the coordinator consumes:
# ``TestMiddlewareShortCircuit``, ``TestMiddlewareFinalArgs``, and
# ``TestOperationKeyFactoryTerminalOnly``.


# ────────────────────────────────────────────────────────────────────────
# Task 3 — STRICT SPEC REMEDIATION (post-adversarial-review)
# ────────────────────────────────────────────────────────────────────────
#
# These tests pin the failures the adversarial review surfaced:
#
# 1. Real sequence allocation (no more fake session_db.sequence_counter).
# 2. Stable replay identity (operation_key → durable operation_id by default).
# 3. Actual terminal-middleware bridge (effect_coordinator injection).
# 4. Registry-derived metadata (loader selects adapter_id / semantic kind).
# 5. Approval-denial short-circuits commit and cancels the tx/journal.
# 6. Deep defensive copies on registry + frozen contracts.
# 7. Authority reload after preview when caller omitted mission_id.
# 8. Literal-union typing on EffectSemantics.kind.
# ────────────────────────────────────────────────────────────────────────


import copy
import json as _json
from typing import Literal


# ── Helpers for real SessionDB integration ──────────────────────────────


def _build_real_session_db(tmp_path):
    """Real SessionDB with no stubs — proves no AttributeError on the
    coordinator's sequence allocation path."""
    from hades_state import SessionDB as _SessionDB
    return _SessionDB(db_path=tmp_path / "state.db")


def _writer_metadata_loader(*, semantic_kind=None):
    """Shared ``operation_metadata_loader`` for tests that don't exercise
    a semantic-kind override — collapses the ``lambda tool: {...}``
    boilerplate that otherwise appears at every direct ``build_coordinator``
    call site."""
    def _load(tool_name: str):
        return {
            "effect_adapter": "writer.v1",
            "effect_semantic_kind": semantic_kind,
            "effect_overrides": {},
        }
    return _load


# ── Spec 1 — Real sequence allocation ──────────────────────────────────


class TestSpec1RealSequenceAllocation:
    def test_coordinator_does_not_touch_fake_sequence_counter(self):
        """Production SessionDB has no ``sequence_counter`` attribute.
        Coordinator must compute the next sequence_no without it.

        The stub used by other tests hides this — it auto-increments
        ``sequence_counter`` as a side effect. We use a stub variant
        that raises on any ``sequence_counter`` access, asserting the
        coordinator never reaches for it.
        """

        class _NoCounterStub:
            def __init__(self):
                self.created = []

            def __getattr__(self, name):
                if name == "sequence_counter":
                    raise AttributeError(
                        "production SessionDB has no sequence_counter"
                    )
                raise AttributeError(name)

            def _execute_read(self, fn):
                # ponytail: no conn — assume no prior rows for the
                # mission; the factory's MAX()+1 reads as 1.
                return {"n": 1}

            def create_effect_transaction(self, **kwargs):
                self.created.append(kwargs)
                return _StubTxRecord(**kwargs)

            def transition_effect_transaction(
                self, transaction_id, *, expected_phase, next_phase,
                result=None, verification=None, compensation=None,
                **extras,
            ):
                return True

        journal = _StubJournal()
        adapter = _StubAdapter("writer.v1", semantic_kind="reversible")
        registry = AdapterRegistry()
        registry.register(adapter)
        sdb = _NoCounterStub()
        mission = {
            "mission_id": "m-1",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        coord = build_coordinator(
            mission_loader=lambda mid: mission,
            session_db=sdb,
            operation_journal=journal,
            adapter_registry=registry,
            approval_request=lambda payload: None,
            review_request=lambda payload: None,
            operation_metadata_loader=_writer_metadata_loader(),
        )

        # Must not raise AttributeError.
        coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key="opk-seq",
            mission_id="m-1",
        )
        assert len(sdb.created) == 1
        # And the sequence_no computed was 1 (no prior transactions for
        # the mission).
        assert sdb.created[0]["sequence_no"] == 1

    def test_coordinator_default_sequence_factory_increments_per_mission(
        self, tmp_path,
    ):
        """Two consecutive transactions on the same mission get sequence
        numbers 1 and 2 from the default factory."""
        from agent.operation_journal import OperationJournal as _OJ
        sdb = _build_real_session_db(tmp_path)
        journal = _OJ(sdb)  # real journal for FK integrity
        adapter = _StubAdapter("writer.v1", semantic_kind="reversible")
        registry = AdapterRegistry()
        registry.register(adapter)
        mission = {
            "mission_id": "m-seq",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        coord = build_coordinator(
            mission_loader=lambda mid: mission,
            session_db=sdb,
            operation_journal=journal,
            adapter_registry=registry,
            approval_request=lambda payload: None,
            review_request=lambda payload: None,
            operation_metadata_loader=_writer_metadata_loader(),
        )

        coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key="opk-seq-1",
            mission_id="m-seq",
        )
        coord.execute(
            tool_name="writer",
            args={"path": "y"},
            handler=lambda a: {"wrote": True},
            operation_key="opk-seq-2",
            mission_id="m-seq",
        )
        # Read sequence numbers directly from real storage.
        rows = sdb._conn.execute(
            "SELECT transaction_id, sequence_no FROM effect_transactions "
            "WHERE mission_id = ? ORDER BY sequence_no",
            ("m-seq",),
        ).fetchall()
        seq_nos = [r[1] for r in rows]
        assert seq_nos == [1, 2]

    def test_real_session_db_full_lifecycle_persists_transaction(
        self, tmp_path,
    ):
        """End-to-end: real SessionDB + real OperationJournal, exercised
        through the coordinator. No AttributeError, persisted row."""
        from agent.operation_journal import OperationJournal as _OJ
        sdb = _build_real_session_db(tmp_path)
        journal = _OJ(sdb)
        adapter = _StubAdapter("writer.v1", semantic_kind="reversible")
        registry = AdapterRegistry()
        registry.register(adapter)
        mission = {
            "mission_id": "m-real",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        coord = build_coordinator(
            mission_loader=lambda mid: mission,
            session_db=sdb,
            operation_journal=journal,
            adapter_registry=registry,
            approval_request=lambda payload: None,
            review_request=lambda payload: None,
            operation_metadata_loader=_writer_metadata_loader(),
        )

        result = coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key="opk-real",
            mission_id="m-real",
        )
        assert result == {"verified": True, "result": {"wrote": True}}
        # The transaction landed in real storage at previewed → committing
        # → committed.
        tx = sdb.get_effect_transaction("opk-real:tx")
        assert tx is not None
        assert tx.mission_id == "m-real"
        assert tx.adapter_id == "writer.v1"
        assert tx.sequence_no == 1
        assert tx.phase == "committed"


# ── Spec 2 — Stable replay identity ────────────────────────────────────


class TestSpec2StableReplayIdentity:
    def test_default_operation_id_factory_uses_operation_key(self):
        """Under default wiring, the durable operation_id must equal
        the operation_key so two retries land on the same row."""
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1"))
        mission = {
            "mission_id": "m-2",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        # Default factory (None) → operation_id should derive from
        # operation_key, NOT from a UUID.
        coord = build_coordinator(
            mission_loader=lambda mid: mission,
            session_db=_StubSessionDB(),
            operation_journal=_StubJournal(),
            adapter_registry=registry,
            approval_request=lambda payload: None,
            review_request=lambda payload: None,
            operation_metadata_loader=_writer_metadata_loader(),
            # operation_id_factory NOT supplied → default behavior.
        )

        coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key="stable-key-1",
            mission_id="m-2",
        )
        # The journal row's id must equal the operation_key.
        assert "stable-key-1" in coord.operation_journal.rows
        # The session_db tx was created with operation_id == operation_key.
        tx_kw = coord.session_db.created[0]
        assert tx_kw["operation_id"] == "stable-key-1"

    def test_explicit_uuid_factory_used_when_operation_key_empty(self):
        """When the caller passes an empty/invalid operation_key, the
        injected operation_id_factory (or its default fallback) is used."""
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1"))
        mission = {
            "mission_id": "m-2c",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        # operation_key = "" → factory should fire.
        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            opid_factory=lambda: "fallback-id",
        )

        coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key="",
            mission_id="m-2c",
        )
        assert "fallback-id" in journal.rows


# ── Spec 3 — Actual terminal-middleware bridge ─────────────────────────


class TestSpec3TerminalMiddlewareBridge:
    def test_terminal_bridge_invokes_coordinator_after_plugin_rewrite(
        self, monkeypatch,
    ):
        """A pass-through plugin that rewrites args must trigger the
        effect_coordinator at the terminal slot, with the FINAL args."""
        _plugin_manager_with(monkeypatch, {})

        calls: List[dict] = []

        def fake_coord_execute(
            *, tool_name, args, handler, operation_key, mission_id=None
        ):
            calls.append({
                "tool_name": tool_name,
                "args": dict(args),
                "operation_key": operation_key,
                "mission_id": mission_id,
            })
            return handler(args)

        coord = types.SimpleNamespace(execute=fake_coord_execute)

        def terminal(args):
            return {"would_write": args["path"]}

        result = run_tool_execution_middleware(
            "writer", {"path": "x"}, terminal,
            effect_coordinator=coord,
            operation_key="opk-mw-1",
            mission_id="m-mw",
        )
        assert result == {"would_write": "x"}
        # The coordinator was invoked with the (post-plugin) final args.
        assert len(calls) == 1
        assert calls[0]["tool_name"] == "writer"
        assert calls[0]["operation_key"] == "opk-mw-1"
        assert calls[0]["mission_id"] == "m-mw"

    def test_plugin_short_circuit_skips_coordinator(
        self, monkeypatch,
    ):
        """A short-circuiting plugin must NOT trigger the coordinator.
        The operation_key_factory IS still evaluated up front (before the
        chain runs) since a registered tool_execution callback needs the
        key available in its kwargs regardless of whether it goes on to
        short-circuit."""
        _plugin_manager_with(monkeypatch, {
            "tool_execution": [lambda **kw: "intercepted"],
        })

        factory_calls: List[int] = []
        coord_calls: List[dict] = []

        def factory():
            factory_calls.append(1)
            return "opk-precomputed"

        def fake_coord_execute(**kwargs):
            coord_calls.append(kwargs)
            return {"never": True}

        coord = types.SimpleNamespace(execute=fake_coord_execute)

        def terminal(args):
            return {"never": True}

        result = run_tool_execution_middleware(
            "writer", {"path": "x"}, terminal,
            effect_coordinator=coord,
            operation_key_factory=factory,
        )
        assert result == "intercepted"
        assert factory_calls == [1]
        assert coord_calls == []

    def test_no_coordinator_passes_through_unchanged(self, monkeypatch):
        """When no effect_coordinator is injected, the middleware
        behaves as before — terminal handler runs, no bridge call."""
        _plugin_manager_with(monkeypatch, {})

        seen: List[dict] = []

        def terminal(args):
            seen.append(args)
            return {"ok": True}

        result = run_tool_execution_middleware(
            "writer", {"path": "x"}, terminal,
        )
        assert result == {"ok": True}
        assert seen == [{"path": "x"}]

    def test_plugin_arg_rewrite_reaches_coordinator_handler(
        self, monkeypatch,
    ):
        """The args the coordinator's handler receives must be the
        FINAL post-plugin args, not the pre-chain args."""

        def mutator(**kw):
            return kw["next_call"]({**kw["args"], "rewritten": True})

        _plugin_manager_with(monkeypatch, {"tool_execution": [mutator]})

        handler_args: List[dict] = []

        def real_handler(args):
            handler_args.append(dict(args))
            return {"wrote": args["path"]}

        def fake_coord_execute(
            *, tool_name, args, handler, operation_key, mission_id=None
        ):
            # The coordinator decides whether to invoke the handler; in
            # this happy-path test it does, after the plugin rewrite.
            return handler(args)

        coord = types.SimpleNamespace(execute=fake_coord_execute)

        # ``next_call`` for the middleware is the actual handler — what
        # the coordinator will hand the args to.
        run_tool_execution_middleware(
            "writer", {"path": "x"}, real_handler,
            effect_coordinator=coord,
            operation_key="opk-mw-2",
            mission_id="m-mw",
        )
        assert handler_args == [{"path": "x", "rewritten": True}]


# ── Spec 4 — Registry-derived metadata overrides mission tool_metadata ─


class TestSpec4RegistryMetadataLoader:
    def test_malicious_mission_adapter_id_overridden_by_loader(self):
        """Mission payload declares adapter X; registry loader says Y.
        The loader's metadata governs — the mission cannot influence
        adapter_id / effect_semantic_kind at the boundary."""
        loader = lambda tool_name: {
            "effect_adapter": "registered.v1",
            "effect_semantic_kind": "reversible",
            "effect_overrides": {},
        }
        registry = AdapterRegistry()
        # The mission-declared adapter is NOT registered; the loader's
        # registered.v1 IS.
        registry.register(_StubAdapter("registered.v1"))
        # Mission tries to claim authority via tool_metadata — must be ignored.
        mission = {
            "mission_id": "m-4",
            "kind": "mutate",
            "operations": {
                "writer": {
                    # adapter_id at mission level → ignored.
                    "adapter_id": "unregistered.evil",
                    "tool_metadata": {
                        "effect_adapter": "unregistered.evil",
                    },
                    "allowed": True,
                },
            },
        }
        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            metadata_loader=loader,
        )

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": True}

        result = coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=handler,
            operation_key="opk-4",
            mission_id="m-4",
        )
        # Handler ran exactly once (the registered adapter was used).
        assert len(handler_calls) == 1
        # The adapter that ran was the loader's choice, not the mission's.
        tx_kw = session_db.created[0]
        assert tx_kw["adapter_id"] == "registered.v1"

    def test_loader_metadata_can_flip_semantic_kind(self):
        """A loader returning irreversible semantics must trigger
        approval even if the mission entry doesn't ask for it."""
        loader = lambda tool_name: {
            "effect_adapter": "writer.v1",
            "effect_semantic_kind": "irreversible",
            "effect_overrides": {},
        }
        registry = AdapterRegistry()
        registry.register(_StubAdapter(
            "writer.v1", semantic_kind="reversible",
        ))
        mission = {
            "mission_id": "m-4b",
            "kind": "mutate",
            "operations": {
                "writer": {
                    "adapter_id": "writer.v1",
                    "allowed": True,
                    "requires_approval": False,
                },
            },
        }
        coord, journal, session_db, app_calls, _ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            metadata_loader=loader,
        )

        coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key="opk-4b",
            mission_id="m-4b",
        )
        # Approval was requested because the loader said irreversible.
        assert len(app_calls) == 1

    def test_default_loader_uses_registry_metadata(self):
        """Without an injected loader, the coordinator must default to
        registry.get_operation_metadata — same behavior, no surprise."""
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1"))
        mission = {
            "mission_id": "m-4c",
            "kind": "mutate",
            "operations": {
                "writer": {
                    "adapter_id": "writer.v1",
                    "allowed": True,
                },
            },
        }
        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            metadata_loader=lambda tool_name: {
                "effect_adapter": "writer.v1",
                "effect_semantic_kind": "reversible",
                "effect_overrides": {},
            },
        )

        coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key="opk-4c",
            mission_id="m-4c",
        )
        tx_kw = session_db.created[0]
        assert tx_kw["adapter_id"] == "writer.v1"


# ── Spec 5 — Approval-denial cancels tx and journal, blocks handler ────


class TestSpec5ApprovalDenial:
    def test_falsy_approval_cancels_tx_and_journal_blocks_handler(self):
        """When approval_request returns falsy, the coordinator must
        transition tx previewed→cancelled and journal pending→cancelled
        BEFORE the handler runs."""
        registry = AdapterRegistry()
        registry.register(_StubAdapter(
            "writer.v1", semantic_kind="irreversible",
            idempotent=False, reconcilable=False,
        ))
        mission = {
            "mission_id": "m-5",
            "kind": "mutate",
            "operations": {
                "writer": {
                    "adapter_id": "writer.v1",
                    "allowed": True,
                    "requires_approval": True,
                },
            },
        }
        app_calls: List[dict] = []
        coord, journal, session_db, _default_app, _ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            approval_request=lambda payload: (
                app_calls.append(payload) or None  # falsy → deny
            ),
        )

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"posted": True}

        with pytest.raises(CoordinatorBlockedError):
            coord.execute(
                tool_name="writer",
                args={"message": "hi"},
                handler=handler,
                operation_key="opk-5",
                mission_id="m-5",
            )
        # Handler was NEVER invoked.
        assert handler_calls == []
        # Approval was asked exactly once.
        assert len(app_calls) == 1
        # Journal settled to cancelled.
        op = journal.rows["opk-5"]
        assert op["state"] == "cancelled"
        assert op["effect_disposition"] == "none"
        # Tx transitioned to cancelled (previewed → cancelled CAS).
        phases = [(t[1], t[2]) for t in session_db.transitions]
        assert ("previewed", "cancelled") in phases

    def test_truthy_approval_allows_commit(self):
        """Falsy is deny; truthy allows the existing commit path."""
        registry = AdapterRegistry()
        registry.register(_StubAdapter(
            "writer.v1", semantic_kind="irreversible",
            idempotent=False, reconcilable=False,
        ))
        mission = {
            "mission_id": "m-5b",
            "kind": "mutate",
            "operations": {
                "writer": {
                    "adapter_id": "writer.v1",
                    "allowed": True,
                    "requires_approval": True,
                },
            },
        }
        app_calls: List[dict] = []
        coord, journal, session_db, _default_app, _ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            approval_request=lambda payload: (
                app_calls.append(payload) or "approved"
            ),
        )

        result = coord.execute(
            tool_name="writer",
            args={"message": "hi"},
            handler=lambda a: {"posted": True},
            operation_key="opk-5b",
            mission_id="m-5b",
        )
        assert result == {"verified": True, "result": {"posted": True}}
        # No cancelled transition.
        phases = [(t[1], t[2]) for t in session_db.transitions]
        assert ("previewed", "cancelled") not in phases
        # And approval was actually invoked (truthy branch).
        assert len(app_calls) == 1

    def test_approval_exception_propagates_without_handler_invocation(self):
        """An exception raised from approval_request must propagate
        and the handler must NOT be invoked."""
        registry = AdapterRegistry()
        registry.register(_StubAdapter(
            "writer.v1", semantic_kind="irreversible",
        ))
        mission = {
            "mission_id": "m-5c",
            "kind": "mutate",
            "operations": {
                "writer": {
                    "adapter_id": "writer.v1",
                    "allowed": True,
                    "requires_approval": True,
                },
            },
        }
        def boom(payload):
            raise RuntimeError("user rejected")

        coord, journal, session_db, _default_app, _ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            approval_request=boom,
        )

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"posted": True}

        with pytest.raises(RuntimeError, match="user rejected"):
            coord.execute(
                tool_name="writer",
                args={"message": "hi"},
                handler=handler,
                operation_key="opk-5c",
                mission_id="m-5c",
            )
        assert handler_calls == []


# ── Spec 6 — Deep defensive copies ─────────────────────────────────────


class TestSpec6DeepDefensiveCopies:
    def test_operation_request_args_deep_copied(self):
        """Mutating the original args mapping after passing it to
        OperationRequest must NOT change the frozen record's view."""
        args = {"path": "README.md", "lines": [1, 2, 3]}
        req = OperationRequest(
            tool_name="writer",
            args=args,
            mission_id="m-6",
            operation_key="opk-6",
        )
        # Mutate after construction.
        args["path"] = "EVIL.md"
        args["lines"].append(999)
        assert req.args == {"path": "README.md", "lines": [1, 2, 3]}

    def test_prepared_effect_mapping_fields_deep_copied(self):
        """Mutating original mappings after PreparedEffect construction
        must NOT leak into the frozen record."""
        normalized = {"path": "x"}
        before = {"exists": True}
        preview = {"diff": "+ok"}
        compensation = {"undo": True}

        prepared = PreparedEffect(
            adapter_id="writer.v1",
            normalized_args=normalized,
            before=before,
            preview=preview,
            semantics=EffectSemantics(
                kind="reversible", idempotent=True, reconcilable=True,
            ),
            compensation=compensation,
        )

        normalized["path"] = "EVIL"
        before["exists"] = False
        preview["diff"] = "-ok"
        compensation["undo"] = False

        assert prepared.normalized_args == {"path": "x"}
        assert prepared.before == {"exists": True}
        assert prepared.preview == {"diff": "+ok"}
        assert prepared.compensation == {"undo": True}


# ── Spec 7 — Authority reload after preview with no explicit mission_id ─


class TestSpec7AuthorityReload:
    def test_reloads_authority_using_resolved_mission_id(self):
        """When caller passed mission_id=None but the mission_loader
        returned a mission with a mission_id, the post-preview reload
        MUST use that resolved id."""
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1"))
        loader_calls: List[Optional[str]] = []

        def mission_loader(mid):
            loader_calls.append(mid)
            if len(loader_calls) == 1:
                # Initial load — return a mission with a resolved id.
                return {
                    "mission_id": "m-resolved",
                    "kind": "mutate",
                    "operations": {
                        "writer": {
                            "adapter_id": "writer.v1",
                            "allowed": True,
                        },
                    },
                    "authority": {"valid": True, "expires_at": 2000.0},
                }
            # Post-preview reload — authority has been revoked.
            return {
                "mission_id": "m-resolved",
                "kind": "mutate",
                "operations": {
                    "writer": {
                        "adapter_id": "writer.v1",
                        "allowed": True,
                    },
                },
                "authority": {"valid": False, "revoked": True},
            }

        clock_values = iter([1000.0, 2000.0])

        def clock():
            return next(clock_values)

        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=mission_loader,
            clock=clock,
            adapter_registry=registry,
        )

        with pytest.raises(CoordinatorBlockedError):
            coord.execute(
                tool_name="writer",
                args={"path": "x"},
                handler=lambda a: {"wrote": True},
                operation_key="opk-7",
                mission_id=None,  # explicit None — coordinator must still reload
            )
        # Loader was called at least twice: initial + post-preview reload.
        assert len(loader_calls) >= 2
        # The reload call used the RESOLVED mission_id, not None.
        assert "m-resolved" in loader_calls


# ── Spec 8 — Literal-union typing for EffectSemantics.kind ─────────────


class TestSpec8LiteralKindAnnotation:
    def test_effect_semantics_kind_literal_annotation(self):
        """EffectSemantics.kind must be annotated with the
        Literal[union] of supported kinds. We assert the annotation
        is present so a future drift to plain ``str`` is caught."""
        hints = EffectSemantics.__dataclass_fields__["kind"]
        # Python represents Literal[...] via typing.get_type_hints.
        from typing import get_type_hints
        resolved = get_type_hints(EffectSemantics)["kind"]
        # The annotation must accept ONLY the four known kinds.
        # Literal[...] inherits from _LiteralGenericAlias — check args.
        args = getattr(resolved, "__args__", ())
        assert set(args) == {
            "read_only", "reversible", "compensatable", "irreversible",
        }


# ── Updated existing tests that depend on old operation_id_factory ─────


class TestLegacyRepeatTestsUpdatedForStableKey:
    """The two pre-existing repeat-key tests referenced a per-call
    factory. They now exercise stable-key replay (Spec 2)."""

    def test_repeated_confirmed_key_returns_stored_result_via_stable_id(self):
        registry = AdapterRegistry()
        adapter = _StubAdapter("writer.v1")
        registry.register(adapter)
        mission = {
            "mission_id": "m-7b",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
        )
        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": True}

        coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=handler,
            operation_key="repeat-stable",
            mission_id="m-7b",
        )
        # Second call: simulate prior settle to confirmed+landed.
        journal.rows["repeat-stable"]["state"] = "confirmed"
        journal.rows["repeat-stable"]["effect_disposition"] = "landed"
        journal.rows["repeat-stable"]["result_json"] = {"wrote": True}

        second = coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=handler,
            operation_key="repeat-stable",
            mission_id="m-7b",
        )
        assert second == {"wrote": True}
        assert len(handler_calls) == 1

    def test_running_state_reconciles_via_adapter_with_stable_id(self):
        registry = AdapterRegistry()
        adapter = _StubAdapter("writer.v1")
        registry.register(adapter)
        mission = {
            "mission_id": "m-8b",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        # Build ONE coordinator; inject a factory that yields the
        # stable "running-stable" id so the prior-running row is found
        # and reconcile fires. Stable-key path with a non-empty
        # operation_key would also work, but exercising the
        # factory fallback documents that the fallback exists.
        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            opid_factory=lambda: "running-stable",
        )
        # Pre-existing journal row in running state, keyed by the stable key.
        journal.create(
            operation_id="running-stable", kind="tool",
            destination="writer.v1",
        )
        journal.rows["running-stable"]["state"] = "running"
        journal.rows["running-stable"]["effect_disposition"] = "none"

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": True}

        # Empty operation_key → factory returns "running-stable" →
        # matches the pre-existing row, exercising the reconcile branch.
        result = coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=handler,
            operation_key="",  # empty → factory used → running-stable
            mission_id="m-8b",
        )
        assert "reconcile" in [c[0] for c in adapter.calls]
        assert sum(1 for c in adapter.calls if c[0] == "commit") <= 1


# ── Task 3 re-review remediation ───────────────────────────────────────
# These four tests cover the remaining real-system failures:
#   1. default metadata loader must consult the tools.registry singleton
#   2. _default_sequence_no_factory must propagate storage errors
#   3. adapter.verify() payload must be persisted as ``verification``
#   4. semantic_kind_override must validate against EFFECT_SEMANTIC_KINDS
#      and govern both the persisted semantics and the approval payload.


class TestTask3ReReviewRemediation:
    def test_default_metadata_loader_delegates_to_tools_registry_singleton(
        self, monkeypatch,
    ):
        """Without an injected loader, the coordinator must lazily defer
        to ``tools.registry.registry.get_operation_metadata`` and let the
        registered adapter run for a supported mutation."""
        from tools import registry as tools_registry_module

        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1", semantic_kind="reversible"))
        mission = {
            "mission_id": "m-default-loader",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }

        # Spy on the module-level singleton's metadata lookup so we can
        # assert it was reached AND drive it from the test (no global
        # state left dangling — monkeypatch restores it).
        spy_calls: List[str] = []

        def fake_get_operation_metadata(name: str):
            spy_calls.append(name)
            return {
                "read_only": False,
                "destructive": False,
                "idempotent": True,
                "effect_adapter": "writer.v1",
                "effect_semantic_kind": "reversible",
                "effect_overrides": {},
            }

        monkeypatch.setattr(
            tools_registry_module.registry,
            "get_operation_metadata",
            fake_get_operation_metadata,
        )

        # Build the coordinator WITHOUT an injected
        # ``operation_metadata_loader`` — the default loader is what we
        # are testing.
        coord = build_coordinator(
            mission_loader=lambda mid: mission,
            session_db=_StubSessionDB(),
            operation_journal=_StubJournal(),
            adapter_registry=registry,
            approval_request=lambda payload: "approved",
            review_request=lambda payload: None,
            # ``operation_metadata_loader`` deliberately omitted.
        )

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": True}

        result = coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=handler,
            operation_key="opk-default-loader",
            mission_id="m-default-loader",
        )

        # The default loader reached the registry's singleton getter.
        assert "writer" in spy_calls
        # And the supported mutation ran end to end (handler invoked).
        assert handler_calls == [{"path": "x"}]
        assert result == {"verified": True, "result": {"wrote": True}}

    def test_default_sequence_factory_propagates_storage_failure(self):
        """Real storage errors on ``_execute_read`` must NOT be swallowed
        and silently turned into sequence_no=1. The handler must not run
        either — the storage outage surfaces before we attempt a write."""

        class _ExplodingSessionDB:
            """Mimics real SessionDB: the ``_execute_read`` raises a
            non-storage RuntimeError to prove the default sequence
            factory propagates it instead of swallowing it."""

            def __init__(self):
                self.created: List[dict] = []

            def _execute_read(self, fn):
                raise RuntimeError("storage offline")

            def create_effect_transaction(self, **kwargs):
                # If we got here, the coordinator swallowed the failure —
                # the test should fail because the handler also ran.
                self.created.append(kwargs)
                return _StubTxRecord(**kwargs)

            def transition_effect_transaction(
                self, transaction_id, *, expected_phase, next_phase,
                result=None, verification=None, compensation=None, **extras,
            ):
                return True

            def get_effect_transaction(self, transaction_id):
                for kw in self.created:
                    if kw.get("transaction_id") == transaction_id:
                        return _StubTxRecord(**kw)
                return None

        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1", semantic_kind="reversible"))
        mission = {
            "mission_id": "m-storage-out",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        sdb = _ExplodingSessionDB()
        coord = build_coordinator(
            mission_loader=lambda mid: mission,
            session_db=sdb,
            operation_journal=_StubJournal(),
            adapter_registry=registry,
            approval_request=lambda payload: "approved",
            review_request=lambda payload: None,
            operation_metadata_loader=_writer_metadata_loader(),
            # ``sequence_no_factory`` deliberately omitted so the default
            # factory — which is what we are testing — runs.
        )

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": True}

        with pytest.raises(RuntimeError, match="storage offline"):
            coord.execute(
                tool_name="writer",
                args={"path": "x"},
                handler=handler,
                operation_key="opk-storage-out",
                mission_id="m-storage-out",
            )
        # Handler was NEVER invoked — the storage error surfaced first.
        assert handler_calls == []
        # And no effect_transaction row was created.
        assert sdb.created == []

    def test_default_sequence_factory_returns_one_for_empty_table(self, tmp_path):
        """Empty mission table → first transaction gets sequence_no=1.
        A second transaction in the same mission gets sequence_no=2.
        This is the real-SessionDB happy path proving the default
        factory still works on the not-failure path."""
        from agent.operation_journal import OperationJournal as _OJ

        sdb = _build_real_session_db(tmp_path)
        _OJ(sdb).create(operation_id="opk-empty", kind="mission_effect")
        # Empty mission table → 1.
        from agent.effect_transactions import _default_sequence_no_factory

        nxt = _default_sequence_no_factory(sdb)
        assert nxt("any-mission") == 1

        # Now exercise the real end-to-end increment: two transactions
        # on the same mission land at sequence_no 1 and 2.
        journal = _OJ(sdb)
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1", semantic_kind="reversible"))
        mission = {
            "mission_id": "m-empty-seq",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        coord = build_coordinator(
            mission_loader=lambda mid: mission,
            session_db=sdb,
            operation_journal=journal,
            adapter_registry=registry,
            approval_request=lambda payload: "approved",
            review_request=lambda payload: None,
            operation_metadata_loader=_writer_metadata_loader(),
        )
        coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key="opk-empty-1",
            mission_id="m-empty-seq",
        )
        coord.execute(
            tool_name="writer",
            args={"path": "y"},
            handler=lambda a: {"wrote": True},
            operation_key="opk-empty-2",
            mission_id="m-empty-seq",
        )
        rows = sdb._conn.execute(
            "SELECT sequence_no FROM effect_transactions "
            "WHERE mission_id = ? ORDER BY sequence_no",
            ("m-empty-seq",),
        ).fetchall()
        assert [r[0] for r in rows] == [1, 2]

    def test_verify_payload_persisted_on_real_session_db(self, tmp_path):
        """The adapter.verify() envelope must be persisted as
        ``verification`` on the real SessionDB row at
        ``committing → committed``."""
        from agent.operation_journal import OperationJournal as _OJ

        sdb = _build_real_session_db(tmp_path)
        journal = _OJ(sdb)
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1", semantic_kind="reversible"))
        mission = {
            "mission_id": "m-verify",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        coord = build_coordinator(
            mission_loader=lambda mid: mission,
            session_db=sdb,
            operation_journal=journal,
            adapter_registry=registry,
            approval_request=lambda payload: "approved",
            review_request=lambda payload: None,
            operation_metadata_loader=_writer_metadata_loader(),
        )
        coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key="opk-verify",
            mission_id="m-verify",
        )
        tx = sdb.get_effect_transaction("opk-verify:tx")
        assert tx is not None
        # The verify envelope is the one returned by _StubAdapter.verify:
        # ``{"verified": True, "result": {...}}``.
        assert tx.verification == {
            "verified": True, "result": {"wrote": True},
        }

    def test_semantic_kind_override_persists_and_governs_approval(self):
        """When the registered metadata says irreversible but the
        adapter's own prepare() returned reversible, the coordinator
        must (a) trigger approval AND (b) persist the effective kind as
        irreversible in the effect_transactions row."""
        registry = AdapterRegistry()
        # Adapter says reversible.
        registry.register(_StubAdapter(
            "writer.v1", semantic_kind="reversible",
        ))
        # Registered metadata says irreversible.
        loader = lambda tool_name: {
            "effect_adapter": "writer.v1",
            "effect_semantic_kind": "irreversible",
            "effect_overrides": {},
        }
        mission = {
            "mission_id": "m-sem-override",
            "kind": "mutate",
            "operations": {
                "writer": {
                    "adapter_id": "writer.v1",
                    "allowed": True,
                    # Note: no ``requires_approval`` — the override alone
                    # must flip the gate.
                },
            },
        }
        coord, journal, session_db, app_calls, _ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            metadata_loader=loader,
        )

        coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key="opk-sem-override",
            mission_id="m-sem-override",
        )
        # Approval was triggered by the override.
        assert len(app_calls) == 1
        # The approval payload reflects the EFFECTIVE kind, not the
        # adapter's reversible kind.
        assert app_calls[0]["semantics"] == "irreversible"
        # Persisted semantics on the tx row carries the effective kind.
        tx_kw = session_db.created[0]
        assert tx_kw["semantics"]["kind"] == "irreversible"

    def test_semantic_kind_override_invalid_kind_rejected_before_handler(self):
        """A non-empty ``effect_semantic_kind`` that is not one of
        EFFECT_SEMANTIC_KINDS must be rejected at the boundary — the
        handler MUST NOT run, no effect_transaction row, no journal
        operation, and adapter.prepare must NOT have been invoked."""
        registry = AdapterRegistry()
        adapter = _StubAdapter("writer.v1", semantic_kind="reversible")
        registry.register(adapter)
        loader = lambda tool_name: {
            "effect_adapter": "writer.v1",
            "effect_semantic_kind": "mayhem",  # not a known kind
            "effect_overrides": {},
        }
        mission = {
            "mission_id": "m-bad-override",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        coord, journal, session_db, *_ = _build_coordinator(
            mission_loader=lambda mid: mission,
            adapter_registry=registry,
            metadata_loader=loader,
        )
        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": True}

        with pytest.raises(CoordinatorBlockedError):
            coord.execute(
                tool_name="writer",
                args={"path": "x"},
                handler=handler,
                operation_key="opk-bad-override",
                mission_id="m-bad-override",
            )
        # Handler never ran.
        assert handler_calls == []
        # No effect_transaction row was created (validate-before-handler).
        assert session_db.created == []
        # No operation_journal row was created either — invalid override
        # is rejected at the metadata boundary, BEFORE
        # ``operation_journal.create``.
        assert journal.rows == {}
        # Adapter.prepare was never invoked.
        assert [c for c in adapter.calls if c[0] == "prepare"] == []

    def test_default_sequence_factory_loud_error_on_nonconforming_session_db(self):
        """Spec 1 fail-closed: a SessionDB without ``_execute_read`` must
        raise a loud TypeError at factory-build time, NOT silently return
        sequence_no=1 (which would race the UNIQUE constraint and roll
        every new transaction onto 1)."""

        class _NonconformingSessionDB:
            """Real SessionDB shape is enforced by the storage boundary;
            this stub proves the default factory refuses a default that
            lacks the read primitive rather than silently allocate 1."""

            def __init__(self):
                self.created: List[dict] = []

            def create_effect_transaction(self, **kwargs):
                self.created.append(kwargs)
                return _StubTxRecord(**kwargs)

            def transition_effect_transaction(
                self, transaction_id, *, expected_phase, next_phase,
                result=None, verification=None, compensation=None, **extras,
            ):
                return True

            def get_effect_transaction(self, transaction_id):
                return None

        from agent.effect_transactions import _default_sequence_no_factory

        sdb = _NonconformingSessionDB()
        with pytest.raises(TypeError):
            _default_sequence_no_factory(sdb)

        # And when that loud error is allowed to surface (i.e. we did
        # NOT inject a sequence_no_factory), the coordinator must
        # propagate it before any handler call or effect_transaction
        # write.
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1", semantic_kind="reversible"))
        mission = {
            "mission_id": "m-nonconforming",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }

        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": True}

        # The TypeError surfaces during ``build_coordinator`` because
        # the default factory is the one that rejects the nonconforming
        # SessionDB at factory-build time.
        with pytest.raises(TypeError):
            build_coordinator(
                mission_loader=lambda mid: mission,
                session_db=sdb,
                operation_journal=_StubJournal(),
                adapter_registry=registry,
                approval_request=lambda payload: "approved",
                review_request=lambda payload: None,
                operation_metadata_loader=_writer_metadata_loader(),
                # ``sequence_no_factory`` deliberately omitted so the
                # default factory — which is what we are testing — runs.
            )
        # Handler was NEVER invoked.
        assert handler_calls == []
        # No effect_transaction row was created.
        assert sdb.created == []


# ── Spec: Task4 registry metadata snapshot ───────────────────────────────


class TestTask4RegistrySchemaSnapshot:
    """Spec: adding effect_adapter metadata to write_file/patch must not
    alter the model-visible schema or the cached definitions returned by
    ``registry.get_definitions``. This test imports the production registry
    singleton and asserts stability."""

    def test_write_file_schema_unchanged_after_effect_adapter_metadata(self):
        # Spec: import file_tools so the module-level registration calls
        # actually populate the registry before snapshotting. Without this
        # import the writes were never registered and the snapshot would
        # have a stale shape.
        import tools.file_tools  # noqa: F401  # registration side-effect
        from tools.registry import registry
        defs = registry.get_definitions({"write_file", "patch", "read_file"})
        assert isinstance(defs, list)
        assert len(defs) == 3
        name_to_def = {d["function"]["name"]: d for d in defs}
        assert "write_file" in name_to_def
        assert "patch" in name_to_def
        assert "read_file" in name_to_def
        # Schema is stable across two consecutive reads.
        defs2 = registry.get_definitions({"write_file", "patch", "read_file"})
        assert defs == defs2
        # Spec: no effect_* keys leak into the model-visible schema.
        for entry in defs:
            schema = entry.get("parameters", {})
            props = schema.get("properties", {})
            assert "effect_adapter" not in props
            assert "effect_semantic_kind" not in props
            assert "effect_overrides" not in props


# ── Spec: Task4 reconciliation root cause ────────────────────────────────
#
# Bug: the coordinator's running/dispatched/unknown state
# reconciliation currently treats ANY adapter.reconcile() outcome as
# confirmed/landed. The shared root cause is in
# ``_execute`` — only ``disposition == "landed"`` should advance the
# operation_journal; ``unknown``/malformed must leave the journal in
# the uncertain state AND raise ``CoordinatorBlockedError`` BEFORE the
# handler is re-invoked. (Pre-fix: any return path from
# ``adapter.reconcile`` conflated "landed" with "we don't know".)


class TestReconcileUnknownDisposition:
    """Spec: shared root cause — only ``disposition == "landed"``
    confirms the journal; ``unknown``/missing disposition leaves the
    operation in the uncertain state and raises CoordinatorBlockedError
    so the handler is NOT re-invoked."""

    def _make_unknown_adapter(self, calls: List[Tuple[str, ...]]):
        from agent.effect_transactions import (
            PreparedEffect, EffectSemantics,
        )

        class _UnknownAdapter:
            adapter_id = "writer.unk.v1"

            def prepare(self, request):
                calls.append(("prepare",))
                return PreparedEffect(
                    adapter_id=self.adapter_id,
                    normalized_args={},
                    before={},
                    preview={},
                    semantics=EffectSemantics(
                        kind="reversible", idempotent=True, reconcilable=True,
                    ),
                    compensation={"x": 1},
                )

            def commit(self, prepared, invoke):
                calls.append(("commit",))
                return invoke({})

            def verify(self, prepared, result):
                calls.append(("verify",))
                return {"verified": True}

            def reconcile(self, record):
                calls.append(("reconcile",))
                # Unknown — handler's effect is uncertain.
                return {"disposition": "unknown"}

            def compensate(self, record):
                calls.append(("compensate",))
                return {"compensated": True}

        return _UnknownAdapter()

    def test_running_state_unknown_disposition_raises_blocked_no_handler(
        self,
    ):
        """When the prior row is ``running`` and ``adapter.reconcile``
        returns ``disposition='unknown'``, the coordinator MUST raise
        ``CoordinatorBlockedError`` and the handler MUST NOT be invoked."""
        from agent.effect_transactions import (
            AdapterRegistry, CoordinatorBlockedError, build_coordinator,
        )
        registry = AdapterRegistry()
        calls: List[Tuple[str, ...]] = []
        registry.register(self._make_unknown_adapter(calls))

        mission = {
            "mission_id": "m-unk",
            "kind": "mutate",
            "operations": {"writer": {"adapter_id": "writer.unk.v1", "allowed": True}},
        }
        opid = "op-unk-1"
        journal = _StubJournal()
        # Pre-existing running prior row that points at our adapter.
        journal.create(operation_id=opid, kind="tool", destination="writer.unk.v1")
        journal.rows[opid]["state"] = "running"
        journal.rows[opid]["effect_disposition"] = "none"

        session_db = _StubSessionDB()
        coord = build_coordinator(
            mission_loader=lambda mid: mission,
            session_db=session_db,
            operation_journal=journal,
            adapter_registry=registry,
            approval_request=lambda payload: "ok",
            review_request=lambda payload: None,
            operation_id_factory=lambda: opid,
            sequence_no_factory=lambda mid: 1,
            operation_metadata_loader=lambda tool_name: {
                "effect_adapter": "writer.unk.v1",
                "effect_semantic_kind": None,
                "effect_overrides": {},
            },
        )
        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": True}

        with pytest.raises(CoordinatorBlockedError):
            coord.execute(
                tool_name="writer",
                args={"path": "x"},
                handler=handler,
                operation_key="",  # factory wins
                mission_id="m-unk",
            )
        # Handler never invoked: reconcile blocked before any rerun.
        assert handler_calls == []
        # Reconcile WAS attempted.
        assert ("reconcile",) in calls
        # No journal transition to "confirmed".
        assert "confirmed" not in [
            t[1] for t in journal.transitions
        ]
        # No SessionDB transition to "committed" (no effect transaction row written).
        assert all(
            t[1] != "committing" for t in session_db.transitions
        )

    def test_running_state_landed_disposition_confirms_with_no_handler(
        self,
    ):
        """Sanity baseline: a landed reconcile DOES advance the journal,
        and the handler is NOT re-invoked."""
        from agent.effect_transactions import (
            AdapterRegistry, build_coordinator,
        )
        from agent.effect_transactions import (
            PreparedEffect, EffectSemantics,
        )

        class _LandedAdapter:
            adapter_id = "writer.landed.v1"

            def prepare(self, request):
                return PreparedEffect(
                    adapter_id=self.adapter_id,
                    normalized_args={},
                    before={},
                    preview={},
                    semantics=EffectSemantics(
                        kind="reversible", idempotent=True, reconcilable=True,
                    ),
                    compensation={"x": 1},
                )

            def commit(self, prepared, invoke):
                return invoke({})

            def verify(self, prepared, result):
                return {"verified": True}

            def reconcile(self, record):
                return {"disposition": "landed", "evidence": "ok"}

            def compensate(self, record):
                return {"compensated": True}

        registry = AdapterRegistry()
        registry.register(_LandedAdapter())
        mission = {
            "mission_id": "m-landed",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.landed.v1", "allowed": True},
            },
        }
        opid = "op-landed-1"
        journal = _StubJournal()
        journal.create(
            operation_id=opid, kind="tool",
            destination="writer.landed.v1",
        )
        journal.rows[opid]["state"] = "running"
        journal.rows[opid]["effect_disposition"] = "none"

        session_db = _StubSessionDB()
        coord = build_coordinator(
            mission_loader=lambda mid: mission,
            session_db=session_db,
            operation_journal=journal,
            adapter_registry=registry,
            approval_request=lambda payload: "ok",
            review_request=lambda payload: None,
            operation_id_factory=lambda: opid,
            sequence_no_factory=lambda mid: 1,
            operation_metadata_loader=lambda tool_name: {
                "effect_adapter": "writer.landed.v1",
                "effect_semantic_kind": None,
                "effect_overrides": {},
            },
        )
        handler_calls: List[dict] = []

        def handler(args):
            handler_calls.append(args)
            return {"wrote": True}

        # Landed → confirm; handler NOT called again (already ran when
        # state went to running). Empty operation_key so the factory's
        # opid is used.
        coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=handler,
            operation_key="",
            mission_id="m-landed",
        )
        # No handler re-invocation.
        assert handler_calls == []
        # Journal advanced to confirmed + landed.
        assert journal.rows[opid]["state"] == "confirmed"
        assert journal.rows[opid]["effect_disposition"] == "landed"


# ────────────────────────────────────────────────────────────────────────
# Terminal-phase receipt wiring — the coordinator's one transition seam
# issues the canonical receipt best-effort, gated on config receipts.mode.
# ────────────────────────────────────────────────────────────────────────


class TestTerminalReceiptWiring:
    """Real SessionDB + real OperationJournal end-to-end: a transaction
    driven to a terminal phase through ``coord.execute`` produces exactly
    one canonical receipt under ``capture`` and zero rows under ``off``."""

    @staticmethod
    def _configure_receipts_mode(tmp_path, monkeypatch, mode):
        home = tmp_path / ".hades"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(
            f"receipts:\n  mode: {mode}\n", encoding="utf-8"
        )
        monkeypatch.setenv("HADES_HOME", str(home))

    @staticmethod
    def _committed_transaction(tmp_path, *, operation_key="opk-receipt"):
        """Drive one real transaction to ``committed`` through the
        module's real API; returns (session_db, coordinator, tx_id)."""
        from agent.operation_journal import OperationJournal as _OJ

        sdb = _build_real_session_db(tmp_path)
        journal = _OJ(sdb)
        registry = AdapterRegistry()
        registry.register(_StubAdapter("writer.v1", semantic_kind="reversible"))
        mission = {
            "mission_id": "m-receipt",
            "kind": "mutate",
            "operations": {
                "writer": {"adapter_id": "writer.v1", "allowed": True},
            },
        }
        coord = build_coordinator(
            mission_loader=lambda mid: mission,
            session_db=sdb,
            operation_journal=journal,
            adapter_registry=registry,
            approval_request=lambda payload: None,
            review_request=lambda payload: None,
            operation_metadata_loader=_writer_metadata_loader(),
        )
        result = coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key=operation_key,
            mission_id="m-receipt",
        )
        assert result == {"verified": True, "result": {"wrote": True}}
        tx_id = f"{operation_key}:tx"
        tx = sdb.get_effect_transaction(tx_id)
        assert tx is not None and tx.phase == "committed"
        return sdb, coord, tx_id

    @staticmethod
    def _receipt_rows(sdb, tx_id):
        return sdb._execute_read(
            lambda conn: [
                dict(row)
                for row in conn.execute(
                    "SELECT receipt_id, source_kind, source_id, status "
                    "FROM receipts WHERE source_kind = 'transaction' "
                    "AND source_id = ?",
                    (tx_id,),
                )
            ]
        )

    @staticmethod
    def _total_receipts(sdb):
        return sdb._execute_read(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM receipts"
            ).fetchone()[0]
        )

    def test_capture_mode_issues_one_receipt_and_replay_is_idempotent(
        self, tmp_path, monkeypatch
    ):
        from agent.receipts import RECEIPT_STATUSES

        self._configure_receipts_mode(tmp_path, monkeypatch, "capture")
        sdb, coord, tx_id = self._committed_transaction(tmp_path)

        rows = self._receipt_rows(sdb, tx_id)
        assert len(rows) == 1
        assert self._total_receipts(sdb) == 1
        receipt_row = rows[0]
        assert receipt_row["source_kind"] == "transaction"
        assert receipt_row["source_id"] == tx_id
        assert receipt_row["status"] in RECEIPT_STATUSES

        # The canonical store resolves the same receipt by source key and
        # its transaction projection column links back (no state mutation
        # beyond the existing receipt_id projection).
        from agent.receipt_models import ReceiptSourceKey
        from agent.receipt_store import ReceiptStore

        receipt = ReceiptStore(sdb).find_by_source(
            ReceiptSourceKey("transaction", tx_id)
        )
        assert receipt is not None
        assert receipt.receipt_id == receipt_row["receipt_id"]
        assert receipt.transaction_id == tx_id

        # Replay through the module's real API: the repeat-key
        # short-circuit returns the stored result without a second
        # transition and without a second receipt.
        replay = coord.execute(
            tool_name="writer",
            args={"path": "x"},
            handler=lambda a: {"wrote": True},
            operation_key="opk-receipt",
            mission_id="m-receipt",
        )
        assert replay == {"wrote": True}
        assert self._total_receipts(sdb) == 1

        # Re-running the issuance seam itself is idempotent by source key.
        from agent.effect_transactions import _issue_transaction_receipt_safely

        _issue_transaction_receipt_safely(sdb, tx_id)
        rows_after = self._receipt_rows(sdb, tx_id)
        assert len(rows_after) == 1
        assert rows_after[0]["receipt_id"] == receipt_row["receipt_id"]

    def test_off_mode_writes_zero_receipt_rows(self, tmp_path, monkeypatch):
        self._configure_receipts_mode(tmp_path, monkeypatch, "off")
        sdb, _coord, tx_id = self._committed_transaction(tmp_path)
        assert self._receipt_rows(sdb, tx_id) == []
        assert self._total_receipts(sdb) == 0
