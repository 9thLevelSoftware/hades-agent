"""Tests for the durable mission outbox storage on SessionDB.

The outbox holds delivery work for completed mission steps. Each row is
identified by a stable ``delivery_id`` so retries are idempotent.
``claim_due_outbox`` atomically picks up due entries in
``(not_before, created_at)`` order with a configurable lease; an expired
claim is recoverable.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent.operation_journal import OperationJournal
from gateway import mission_outbox as mission_outbox_module
from gateway.mission_outbox import (
    MissionOutboxStore,
    OUTBOX_STATUSES,
)
from hades_state import OutboxMigrationError, SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _outbox_kwargs(**overrides):
    # Legacy tests historically supplied arbitrary IDs. Public SessionDB now
    # derives them from execution/node, so retain only the legacy label as the
    # execution seed and never pass caller-controlled ID fields to storage.
    legacy_outbox_id = overrides.pop("outbox_id", "ob-1")
    overrides.pop("delivery_id", None)
    legacy_stem = (
        legacy_outbox_id[3:]
        if legacy_outbox_id.startswith("ob-")
        else legacy_outbox_id
    )
    base = {
        "mission_id": "m-1",
        "execution_id": (
            f"ex-{legacy_stem}"
            if legacy_outbox_id != "ob-1"
            else "ex-1"
        ),
        "node_id": "node-a",
        "transaction_id": "tx-1",
        "platform": "telegram",
        "target": "chat:1",
        "content": {"text": "hello"},
        "not_before": 0,
        "status": "pending",
        "revision": 1,
        "approval": None,
        "result": None,
    }
    base.update(overrides)
    return base


def test_mission_outbox_module_is_storage_only_and_has_no_delivery_dispatcher():
    source = Path(mission_outbox_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert not hasattr(mission_outbox_module, "MissionOutboxDispatcher")
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and (
            (isinstance(node, ast.ImportFrom) and node.module in {
                "gateway.config",
                "gateway.delivery",
                "gateway.router",
            })
            or (
                isinstance(node, ast.Import)
                and any(
                    alias.name.startswith(("gateway.delivery", "gateway.router"))
                    for alias in node.names
                )
            )
        )
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.ClassDef) and node.name == "MissionOutboxDispatcher"
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "deliver"
        for node in ast.walk(tree)
    )


def test_direct_sessiondb_create_outbox_rejects_mission_context_mismatch(db):
    db.create_outbox(**_outbox_kwargs(mission_id="mission-a"))

    with pytest.raises(ValueError, match="mission_id"):
        db.create_outbox(**_outbox_kwargs(mission_id=None))
    with pytest.raises(ValueError, match="mission_id"):
        db.create_outbox(**_outbox_kwargs(mission_id="mission-b"))

    db.create_outbox(
        **_outbox_kwargs(
            execution_id="ex-ordinary",
            mission_id=None,
            transaction_id=None,
        )
    )
    with pytest.raises(ValueError, match="mission_id"):
        db.create_outbox(
            **_outbox_kwargs(execution_id="ex-ordinary", mission_id="mission-a")
        )


def test_direct_create_outbox_rejects_transaction_context_mismatch(db):
    db.create_outbox(**_outbox_kwargs(mission_id="mission-a", transaction_id="tx-a"))

    with pytest.raises(ValueError, match="transaction_id"):
        db.create_outbox(
            **_outbox_kwargs(
                mission_id="mission-a",
                transaction_id="tx-b",
            )
        )

    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox"
    ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("platform", "discord"), ("target", "chat:other")],
)
def test_direct_create_outbox_rejects_existing_platform_or_target_mismatch(
    db, field, value
):
    """Direct SessionDB retries must fence transport identity too."""
    created = db.create_outbox(
        **_outbox_kwargs(platform=" telegram ", target=" chat:1 ")
    )

    with pytest.raises(ValueError, match="platform|target|identity"):
        db.create_outbox(**_outbox_kwargs(**{field: value}))

    persisted = db.get_outbox_by_id(created.outbox_id)
    assert persisted is not None
    assert persisted.platform == "telegram"
    assert persisted.target == "chat:1"


@pytest.mark.parametrize("platform", ["telegram!", "../x", "1telegram", "telegram/x"])
def test_materialize_rejects_unsafe_dynamic_platform_tokens(db, platform):
    with pytest.raises(ValueError, match="platform"):
        MissionOutboxStore(db).materialize(
            execution_id=f"exec-platform-{platform}",
            node_id="notify",
            platform=platform,
            target="chat:1",
            content={"text": "hello"},
        )


@pytest.mark.parametrize("platform", ["telegram!", "../x"])
def test_direct_create_outbox_rejects_unsafe_dynamic_platform_tokens(db, platform):
    with pytest.raises(ValueError, match="platform"):
        db.create_outbox(
            **_outbox_kwargs(
                execution_id=f"exec-direct-platform-{platform}",
                platform=platform,
                mission_id=None,
                transaction_id=None,
            )
        )


def test_direct_create_outbox_accepts_safe_dynamic_platform_token(db):
    row = db.create_outbox(
        **_outbox_kwargs(
            execution_id="exec-direct-platform-irc",
            platform="irc",
            mission_id=None,
            transaction_id=None,
        )
    )
    assert row.platform == "irc"


def test_materialize_normalizes_dynamic_platform_token(db):
    row = MissionOutboxStore(db).materialize(
        execution_id="exec-platform-irc",
        node_id="notify",
        platform=" IRC ",
        target="42",
        content={"text": "hello"},
    )
    assert row.platform == "irc"


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_requeue_terminal_mission_outbox_resets_effect_and_operation_for_fresh_claim(
    db, terminal_status
):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id=f"exec-requeue-{terminal_status}",
        node_id="notify",
        mission_id=f"mission-requeue-{terminal_status}",
        platform="irc",
        target="42",
        content={"text": "hello"},
        not_before=10,
    )
    assert row.transaction_id is not None
    if terminal_status == "failed":
        claimed = store.claim(now=10, owner_id="requeue-test", limit=1)
        assert store.mark_failed(
            row.outbox_id,
            owner_id="requeue-test",
            claim_token=claimed[0].claim_token,
            result=0,
            error="adapter rejected",
        )
        assert db.transition_effect_transaction(
            row.transaction_id, expected_phase="pending", next_phase="failed"
        )
    else:
        assert store.cancel(row.outbox_id, expected_revision=row.revision)
        cancelled_effect = db.get_effect_transaction(row.transaction_id)
        assert cancelled_effect is not None
        assert cancelled_effect.phase == "cancelled"

    retried = store.requeue_terminal(
        execution_id=row.execution_id,
        node_id=row.node_id,
        mission_id=row.mission_id,
        platform=row.platform,
        target=row.target,
        content=row.content,
        not_before=20,
    )

    assert retried.outbox_id == row.outbox_id
    assert retried.status == "scheduled"
    assert retried.not_before == 10
    assert retried.revision == row.revision + 1
    assert retried.approval is None
    assert retried.result is None
    effect = db.get_effect_transaction(row.transaction_id)
    assert effect is not None
    assert effect.phase == "pending"
    operation = OperationJournal(db).get(f"{row.outbox_id}:operation")
    assert operation is not None
    assert operation.state == "pending"
    assert operation.effect_disposition == "none"
    assert db._execute_read(
        lambda conn: conn.execute(
            "SELECT count(*) FROM mission_outbox WHERE execution_id = ? AND node_id = ?",
            (row.execution_id, row.node_id),
        ).fetchone()[0]
    ) == 1
    claimable = store.claim(now=20, owner_id="fresh-dispatch", limit=1)
    assert [claimed.outbox_id for claimed in claimable] == [row.outbox_id]


def test_requeue_terminal_mission_outbox_clears_prior_acknowledgement(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-requeue-acknowledgement",
        node_id="notify",
        mission_id="mission-requeue-acknowledgement",
        platform="irc",
        target="42",
        content={"text": "hello"},
    )
    assert store.cancel(row.outbox_id, expected_revision=row.revision)
    assert store.acknowledge(row.outbox_id) is True

    requeued = store.requeue_terminal(
        execution_id=row.execution_id,
        node_id=row.node_id,
        mission_id=row.mission_id,
        platform=row.platform,
        target=row.target,
        content=row.content,
    )
    assert requeued.acknowledged_at is None

    claimed = store.claim(now=10, owner_id="requeue-ack", limit=1)
    assert len(claimed) == 1
    assert store.mark_delivered(
        row.outbox_id,
        owner_id="requeue-ack",
        claim_token=claimed[0].claim_token,
        result={"message_id": "fresh"},
    )
    assert store.acknowledge(row.outbox_id) is True


@pytest.mark.parametrize("path", ["quarantine", "compensate"])
def test_same_mission_wrong_effect_identity_is_never_settled(db, path):
    store = MissionOutboxStore(db)
    first = store.materialize(
        execution_id="exec-effect-identity",
        node_id="first",
        mission_id="mission-effect-identity",
        platform="irc",
        target="42",
        content={"text": "first"},
    )
    unrelated_transaction_id = "tx-unrelated-effect"
    OperationJournal(db).create(
        operation_id="operation-unrelated-effect",
        kind="other_effect",
        destination="internal:other",
        payload_hash="unrelated",
    )
    db.create_effect_transaction(
        transaction_id=unrelated_transaction_id,
        operation_id="operation-unrelated-effect",
        mission_id="mission-effect-identity",
        execution_id="exec-effect-identity",
        step_id="unrelated",
        adapter_id="internal.other",
        sequence_no=2,
        semantics={"kind": "other"},
        depends_on=[],
        prepared={"kind": "other"},
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET transaction_id = ? WHERE outbox_id = ?",
            (unrelated_transaction_id, first.outbox_id),
        )
    )

    if path == "quarantine":
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE mission_outbox SET content_json = '{bad' WHERE outbox_id = ?",
                (first.outbox_id,),
            )
        )
        with pytest.raises(RuntimeError, match="effect identity"):
            store.claim(now=10, limit=1)
    else:
        with pytest.raises(RuntimeError, match="effect identity"):
            store.compensate(first.outbox_id)

    persisted_status = db._execute_read(
        lambda conn: conn.execute(
            "SELECT status FROM mission_outbox WHERE outbox_id = ?",
            (first.outbox_id,),
        ).fetchone()[0]
    )
    assert persisted_status == "scheduled"
    effect = db.get_effect_transaction(unrelated_transaction_id)
    assert effect is not None
    assert effect.phase == "pending"


def _materialized_mission_row(store, **overrides):
    kwargs = {
        "execution_id": "exec-graph-integrity",
        "node_id": "notify",
        "mission_id": "mission-graph-integrity",
        "platform": "irc",
        "target": "42",
        "content": {"text": "hello"},
    }
    kwargs.update(overrides)
    return store.materialize(**kwargs)


def _corrupt_prepared_target(db, transaction_id):
    """Split the ledger: effect row survives but its prepared_json diverges
    from the outbox row it is supposed to describe."""
    def _write(conn):
        prepared = json.loads(
            conn.execute(
                "SELECT prepared_json FROM effect_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()[0]
        )
        prepared["target"] = "tampered"
        conn.execute(
            "UPDATE effect_transactions SET prepared_json = ? WHERE transaction_id = ?",
            (json.dumps(prepared), transaction_id),
        )

    db._execute_write(_write)


def _diverge_operation_identity(db, operation_id):
    """Split the ledger: the linked agent_operations row no longer agrees
    with the effect/outbox it is supposed to identify."""
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE agent_operations SET destination = 'outbox:tampered' "
            "WHERE operation_id = ?",
            (operation_id,),
        )
    )


def _delete_effect_row(db, transaction_id):
    db._execute_write(
        lambda conn: conn.execute(
            "DELETE FROM effect_transactions WHERE transaction_id = ?",
            (transaction_id,),
        )
    )


def test_mission_outbox_graph_matches_true_for_ordinary_row(db):
    row = db.create_outbox(**_outbox_kwargs(mission_id=None, transaction_id=None))
    assert db.mission_outbox_graph_matches(row.outbox_id) is True


def test_mission_outbox_graph_matches_true_for_consistent_mission_row(db):
    store = MissionOutboxStore(db)
    row = _materialized_mission_row(store)
    assert db.mission_outbox_graph_matches(row.outbox_id) is True


def test_mission_outbox_graph_matches_false_for_missing_outbox_row(db):
    assert db.mission_outbox_graph_matches("outbox-missing") is False


def test_mission_outbox_graph_matches_false_for_missing_effect_row(db):
    store = MissionOutboxStore(db)
    row = _materialized_mission_row(store)
    assert row.transaction_id is not None
    _delete_effect_row(db, row.transaction_id)
    assert db.mission_outbox_graph_matches(row.outbox_id) is False


def test_mission_outbox_graph_matches_false_for_corrupted_prepared_json(db):
    store = MissionOutboxStore(db)
    row = _materialized_mission_row(store)
    assert row.transaction_id is not None
    _corrupt_prepared_target(db, row.transaction_id)
    assert db.mission_outbox_graph_matches(row.outbox_id) is False


def test_mission_outbox_graph_matches_false_for_diverged_operation_identity(db):
    store = MissionOutboxStore(db)
    row = _materialized_mission_row(store)
    operation_id = f"{row.outbox_id}:operation"
    _diverge_operation_identity(db, operation_id)
    assert db.mission_outbox_graph_matches(row.outbox_id) is False


def test_set_delivery_capabilities_refuses_when_effect_missing(db):
    store = MissionOutboxStore(db)
    row = _materialized_mission_row(store)
    assert row.transaction_id is not None
    before = db.get_effect_transaction(row.transaction_id)
    assert before is not None
    _delete_effect_row(db, row.transaction_id)

    assert store.set_delivery_capabilities(row.outbox_id, idempotent=True) is False


def test_set_delivery_capabilities_refuses_when_prepared_json_diverges(db):
    store = MissionOutboxStore(db)
    row = _materialized_mission_row(store)
    assert row.transaction_id is not None
    _corrupt_prepared_target(db, row.transaction_id)
    before = db.get_effect_transaction(row.transaction_id)
    assert before is not None

    assert store.set_delivery_capabilities(row.outbox_id, idempotent=True) is False

    after = db.get_effect_transaction(row.transaction_id)
    assert after is not None
    assert after.semantics == before.semantics


def test_cancel_outbox_fails_closed_when_mission_effect_missing_and_not_claimed(db):
    store = MissionOutboxStore(db)
    row = _materialized_mission_row(store)
    assert row.transaction_id is not None
    _delete_effect_row(db, row.transaction_id)

    with pytest.raises(RuntimeError, match="missing effect"):
        db.cancel_outbox(row.outbox_id, expected_revision=row.revision)

    persisted = db.get_outbox_by_id(row.outbox_id)
    assert persisted is not None
    assert persisted.status == "scheduled"


def test_cancel_outbox_settles_unknown_when_claimed_row_effect_missing(db):
    store = MissionOutboxStore(db)
    row = _materialized_mission_row(store)
    assert row.transaction_id is not None
    claimed = store.claim(now=10, owner_id="graph-integrity-test", limit=1)
    assert [c.outbox_id for c in claimed] == [row.outbox_id]
    claim_token = claimed[0].claim_token
    _delete_effect_row(db, row.transaction_id)

    result = db.cancel_outbox(
        row.outbox_id,
        expected_revision=claimed[0].revision,
        claim_token=claim_token,
    )

    assert result is True
    persisted = db.get_outbox_by_id(row.outbox_id)
    assert persisted is not None
    assert persisted.status == "unknown"
    assert persisted.status != "cancelled"


@pytest.mark.parametrize("result", [0, False, [], ""])
def test_mark_failed_preserves_falsey_result_when_adding_error(db, result):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id=f"exec-falsey-failed-{type(result).__name__}",
        node_id="notify",
        platform="irc",
        target="42",
        content="hello",
    )
    claimed = store.claim(now=10, owner_id="falsey-test", limit=1)
    assert store.mark_failed(
        row.outbox_id,
        owner_id="falsey-test",
        claim_token=claimed[0].claim_token,
        result=result,
        error="adapter rejected",
    )
    failed = store.get_by_id(row.outbox_id)
    assert failed is not None
    assert failed.result == {"result": result, "error": "[REDACTED]"}


def test_compensate_unclaimed_mission_cancels_outbox_and_effect_atomically(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-compensate-unclaimed",
        node_id="notify",
        mission_id="mission-compensate-unclaimed",
        platform="irc",
        target="42",
        content="hello",
    )
    assert store.compensate(row.outbox_id) == "cancelled"
    cancelled = store.get_by_id(row.outbox_id)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert row.transaction_id is not None
    effect = db.get_effect_transaction(row.transaction_id)
    assert effect is not None and effect.phase == "cancelled"


def test_compensate_claimed_mission_marks_unknown_and_effect_unknown(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-compensate-claimed",
        node_id="notify",
        mission_id="mission-compensate-claimed",
        platform="irc",
        target="42",
        content="hello",
    )
    claimed = store.claim(now=10, owner_id="compensation-race", limit=1)
    assert claimed and claimed[0].claim_token
    assert store.compensate(row.outbox_id) == "unknown"
    unknown = store.get_by_id(row.outbox_id)
    assert unknown is not None
    assert unknown.status == "unknown"
    assert unknown.result == {
        "error": "[REDACTED]",
        "reconciliation_required": True,
    }
    assert row.transaction_id is not None
    effect = db.get_effect_transaction(row.transaction_id)
    assert effect is not None and effect.phase == "unknown_effect"
    assert effect.compensation == {
        "error": "[REDACTED]",
        "reconciliation_required": True,
    }


def test_compensate_surfaces_missing_outbox_instead_of_ignoring_cas_failure(db):
    with pytest.raises(RuntimeError, match="disappeared"):
        MissionOutboxStore(db).compensate("missing-outbox")


def test_revise_rejects_existing_unsafe_platform_token_at_storage_boundary(db):
    created = db.create_outbox(
        **_outbox_kwargs(
            execution_id="exec-unsafe-revise",
            platform="telegram",
            mission_id=None,
            transaction_id=None,
        )
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET platform = 'telegram!' WHERE outbox_id = ?",
            (created.outbox_id,),
        )
    )
    with pytest.raises(ValueError, match="platform"):
        MissionOutboxStore(db).revise(
            created.outbox_id,
            expected_revision=created.revision,
            content={"text": "revised"},
        )


def test_materialize_rejects_caller_effect_id_collision_without_outbox(db):
    """Caller IDs must not attach a new mission to an old effect transaction."""
    content = {"text": "hello"}
    OperationJournal(db).create(
        operation_id="op-old",
        kind="mission_outbox",
        destination="outbox:telegram",
        payload_hash=mission_outbox_module._content_hash(content),
    )
    db.create_effect_transaction(
        transaction_id="tx-old",
        operation_id="op-old",
        mission_id="mission-old",
        execution_id="exec-old",
        step_id="notify",
        adapter_id="outbox.telegram",
        sequence_no=1,
        semantics={"kind": "outbound_delivery"},
        depends_on=[],
    )

    store = MissionOutboxStore(db)
    with pytest.raises(ValueError, match="derived"):
        store.materialize(
            execution_id="exec-new",
            node_id="notify",
            mission_id="mission-new",
            platform="telegram",
            target="chat:1",
            content=content,
            transaction_id="tx-old",
            operation_id="op-old",
        )

    assert db.get_outbox_by_identity("exec-new", "notify") is None
    old_effect = db.get_effect_transaction("tx-old")
    assert old_effect is not None
    assert old_effect.mission_id == "mission-old"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mission_id", 0),
        ("mission_id", False),
        ("mission_id", {}),
        ("mission_id", ""),
        ("transaction_id", 0),
        ("transaction_id", False),
        ("transaction_id", {}),
        ("transaction_id", ""),
    ],
)
def test_direct_create_outbox_rejects_invalid_mission_context(db, field, value):
    with pytest.raises(ValueError, match=field):
        db.create_outbox(**_outbox_kwargs(**{field: value}))

    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("mission_id", "transaction_id"),
    [(None, "tx-orphan"), ("mission-orphan", None)],
)
def test_direct_create_outbox_rejects_partial_mission_context(
    db, mission_id, transaction_id
):
    with pytest.raises(
        ValueError, match="mission_id.*transaction_id|transaction_id.*mission_id"
    ):
        db.create_outbox(
            **_outbox_kwargs(
                mission_id=mission_id,
                transaction_id=transaction_id,
            )
        )

    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox"
    ).fetchone()[0] == 0


def test_direct_create_outbox_rejects_forged_content_hash(db):
    with pytest.raises(ValueError, match="content_hash"):
        db.create_outbox(
            **_outbox_kwargs(
                content={"text": "canonical"},
                content_hash="forged-hash",
            )
        )

    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox"
    ).fetchone()[0] == 0


def test_direct_existing_outbox_rejects_forged_canonical_length_hash(db):
    content = {"text": "existing"}
    created = db.create_outbox(**_outbox_kwargs(content=content))

    with pytest.raises(ValueError, match="content_hash"):
        db.create_outbox(
            **_outbox_kwargs(
                content=content,
                content_hash="f" * 64,
            )
        )

    persisted = db.get_outbox_by_id(created.outbox_id)
    assert persisted is not None
    assert persisted.content == content


def test_direct_existing_outbox_accepts_canonical_hash_when_backfilling_blank(db):
    content = {"text": "legacy"}
    created = db.create_outbox(**_outbox_kwargs(content=content))
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_hash='' WHERE outbox_id=?",
            (created.outbox_id,),
        )
    )
    canonical_json = json.dumps(content, sort_keys=True, separators=(",", ":"))
    canonical_hash = hashlib.sha256(canonical_json.encode()).hexdigest()

    retried = db.create_outbox(
        **_outbox_kwargs(content=content, content_hash=canonical_hash)
    )

    assert retried.outbox_id == created.outbox_id
    assert retried.content_hash == canonical_hash


@pytest.mark.parametrize(
    "invalid_initial_status",
    ["claimed", "delivered", "cancelled", "failed", "unknown", "sent"],
)
def test_direct_create_outbox_rejects_claimed_or_terminal_initial_status(
    db, invalid_initial_status
):
    with pytest.raises(ValueError, match="initial outbox status"):
        db.create_outbox(
            **_outbox_kwargs(status=invalid_initial_status)
        )

    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox"
    ).fetchone()[0] == 0


def test_direct_existing_outbox_backfills_hash_from_persisted_content(db):
    created = db.create_outbox(
        **_outbox_kwargs(content={"persisted": "content"})
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_hash='' WHERE outbox_id=?",
            (created.outbox_id,),
        )
    )

    caller_content = {"caller": "must not win"}
    caller_hash = hashlib.sha256(
        json.dumps(caller_content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="prepared semantics"):
        db.create_outbox(
            **_outbox_kwargs(
                content=caller_content,
                content_hash=caller_hash,
            )
        )

    persisted = db.get_outbox_by_id(created.outbox_id)
    assert persisted is not None
    assert persisted.content == {"persisted": "content"}
    assert persisted.content_hash == ""


def test_materialize_rejects_changed_target_before_reusing_effect(db):
    store = MissionOutboxStore(db)
    first = store.materialize(
        execution_id="exec-effect-reuse",
        node_id="notify",
        mission_id="mission-effect-reuse",
        platform="telegram",
        target="chat:old",
        content={"text": "same"},
    )

    with pytest.raises(ValueError, match="effect|target|prepared"):
        store.materialize(
            execution_id="exec-effect-reuse",
            node_id="notify",
            mission_id="mission-effect-reuse",
            platform="telegram",
            target="chat:new",
            content={"text": "same"},
        )

    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox WHERE execution_id=? AND node_id=?",
        ("exec-effect-reuse", "notify"),
    ).fetchone()[0] == 1
    existing = db.get_outbox_by_id(first.outbox_id)
    assert existing is not None
    assert existing.target == "chat:old"
    effect = db.get_effect_transaction(f"{first.outbox_id}:transaction")
    assert effect is not None
    assert effect.prepared["target"] == "chat:old"


def test_create_outbox_returns_frozen_record_with_canonical_json(db):
    row = db.create_outbox(**_outbox_kwargs())

    assert row.outbox_id == "ob-1"
    assert row.mission_id == "m-1"
    assert row.execution_id == "ex-1"
    assert row.node_id == "node-a"
    assert row.transaction_id == "tx-1"
    assert row.delivery_id == "dl-1"
    assert row.platform == "telegram"
    assert row.target == "chat:1"
    assert row.content == {"text": "hello"}
    assert row.not_before == 0
    assert row.status == "pending"
    assert row.revision == 1
    assert row.approval is None
    assert row.result is None

    with pytest.raises(FrozenInstanceError):
        row.status = "sent"  # type: ignore[misc]


def test_create_outbox_validates_required_fields_before_sql(db):
    with pytest.raises(TypeError):
        db.create_outbox(**_outbox_kwargs(), delivery_id="")
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(execution_id=""))
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(node_id=""))
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(platform=""))
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(target=""))
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(status="wat"))

    count = db._conn.execute("SELECT COUNT(*) FROM mission_outbox").fetchone()[0]
    assert count == 0


def test_create_outbox_is_idempotent_per_delivery_id(db):
    first = db.create_outbox(**_outbox_kwargs())
    with pytest.raises(ValueError, match="prepared semantics"):
        db.create_outbox(
            **_outbox_kwargs(
                outbox_id="ob-2",  # different row id, same stable delivery_id
                execution_id="ex-1",
                content={"text": "ignored"},
                revision=7,
            )
        )

    assert first.delivery_id == "dl-1"
    assert first.outbox_id == "ob-1"
    current = db.get_outbox_by_id(first.outbox_id)
    assert current is not None
    assert current.content == {"text": "hello"}
    assert current.revision == 1
    rows = db._conn.execute(
        "SELECT outbox_id FROM mission_outbox WHERE delivery_id='dl-1'"
    ).fetchall()
    assert [r[0] for r in rows] == ["ob-1"]


def test_get_outbox_returns_deep_copied_content(db):
    db.create_outbox(**_outbox_kwargs())
    row = db.get_outbox("dl-1")
    assert row is not None

    # Caller-side mutation of the returned structured fields must not
    # bleed into the next read. (Reassigning a frozen dataclass field
    # itself raises FrozenInstanceError — only the deep-copied *contents*
    # are mutable.)
    row.content["text"] = "tampered"
    if row.approval is not None:
        row.approval["actor"] = "intruder"

    fresh = db.get_outbox("dl-1")
    assert fresh is not None
    assert fresh.content == {"text": "hello"}
    assert fresh.approval is None


def test_get_outbox_returns_none_for_unknown_delivery_id(db):
    assert db.get_outbox("missing") is None


def test_claim_due_outbox_returns_rows_in_not_before_created_order(db):
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-a",
            delivery_id="dl-a",
            transaction_id="tx-a",
            not_before=2,
            content={"text": "third"},
        )
    )
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-b",
            delivery_id="dl-b",
            transaction_id="tx-b",
            not_before=0,
            content={"text": "first"},
        )
    )
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-c",
            delivery_id="dl-c",
            transaction_id="tx-c",
            not_before=1,
            content={"text": "second"},
        )
    )

    claimed = db.claim_due_outbox(now=10, lease_seconds=60)
    assert [r.delivery_id for r in claimed] == ["dl-b", "dl-c", "dl-a"]
    assert all(r.status == "claimed" for r in claimed)


def test_claim_due_outbox_quarantines_valid_json_hash_substitution(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-valid-json-substitution",
        node_id="notify",
        mission_id="mission-valid-json-substitution",
        platform="irc",
        target="42",
        content={"text": "approved"},
    )
    assert row.transaction_id is not None
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_json = ? WHERE outbox_id = ?",
            (json.dumps({"text": "substituted"}), row.outbox_id),
        )
    )

    assert store.claim(now=10, limit=1) == []

    quarantined = store.get_by_id(row.outbox_id)
    assert quarantined is not None
    assert quarantined.status == "failed"
    effect = db.get_effect_transaction(row.transaction_id)
    assert effect is not None
    assert effect.phase == "failed"


def test_claim_due_outbox_quarantine_settles_pending_mission_effect(db):
    store = MissionOutboxStore(db)
    corrupt = store.materialize(
        execution_id="exec-corrupt-effect",
        node_id="notify",
        mission_id="mission-corrupt-effect",
        platform="telegram",
        target="chat:1",
        content={"text": "corrupt"},
    )
    assert corrupt.transaction_id is not None
    before = db.get_effect_transaction(corrupt.transaction_id)
    assert before is not None
    assert before.phase == "pending"
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_json = ? WHERE outbox_id = ?",
            ("{not-json", corrupt.outbox_id),
        )
    )

    assert db.claim_due_outbox(now=10, lease_seconds=60) == []

    quarantined = db.get_outbox_by_id(corrupt.outbox_id)
    assert quarantined is not None
    assert quarantined.status == "failed"
    after = db.get_effect_transaction(corrupt.transaction_id)
    assert after is not None
    assert after.phase == "failed"


def test_claim_due_outbox_rolls_back_quarantine_when_effect_settlement_fails(
    db, monkeypatch
):
    store = MissionOutboxStore(db)
    corrupt = store.materialize(
        execution_id="exec-corrupt-effect-rollback",
        node_id="notify",
        mission_id="mission-corrupt-effect-rollback",
        platform="telegram",
        target="chat:1",
        content={"text": "corrupt"},
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_json = ? WHERE outbox_id = ?",
            ("{not-json", corrupt.outbox_id),
        )
    )
    status_before = db._conn.execute(
        "SELECT status FROM mission_outbox WHERE outbox_id = ?",
        (corrupt.outbox_id,),
    ).fetchone()[0]

    def fail_settlement(*_args, **_kwargs):
        raise RuntimeError("effect settlement fault")

    monkeypatch.setattr(db, "_settle_quarantined_outbox_effect", fail_settlement)

    with pytest.raises(RuntimeError, match="effect settlement fault"):
        db.claim_due_outbox(now=10, lease_seconds=60)

    assert db._conn.execute(
        "SELECT status FROM mission_outbox WHERE outbox_id = ?",
        (corrupt.outbox_id,),
    ).fetchone()[0] == status_before
    assert corrupt.transaction_id is not None
    effect = db.get_effect_transaction(corrupt.transaction_id)
    assert effect is not None
    assert effect.phase == "pending"


def test_claim_due_outbox_quarantine_marks_committing_effect_unknown(db):
    store = MissionOutboxStore(db)
    corrupt = store.materialize(
        execution_id="exec-corrupt-committing-effect",
        node_id="notify",
        mission_id="mission-corrupt-committing-effect",
        platform="telegram",
        target="chat:1",
        content={"text": "corrupt"},
    )
    assert corrupt.transaction_id is not None
    assert db.claim_due_outbox(now=10, lease_seconds=60) == [
        db.get_outbox_by_id(corrupt.outbox_id)
    ]
    assert db.transition_effect_transaction(
        corrupt.transaction_id,
        expected_phase="pending",
        next_phase="previewed",
    )
    assert db.transition_effect_transaction(
        corrupt.transaction_id,
        expected_phase="previewed",
        next_phase="committing",
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_json = ? WHERE outbox_id = ?",
            ("{not-json", corrupt.outbox_id),
        )
    )

    assert db.claim_due_outbox(now=70, lease_seconds=60) == []

    quarantined = db.get_outbox_by_id(corrupt.outbox_id)
    assert quarantined is not None
    assert quarantined.status == "failed"
    after = db.get_effect_transaction(corrupt.transaction_id)
    assert after is not None
    assert after.phase == "unknown_effect"


def test_claim_due_outbox_quarantines_corrupt_payload_without_stranding_batch(db):
    corrupt = db.create_outbox(**_outbox_kwargs(outbox_id="ob-corrupt"))
    valid = db.create_outbox(
        **_outbox_kwargs(outbox_id="ob-valid", transaction_id="tx-valid")
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_json = ? WHERE outbox_id = ?",
            ("{not-json", corrupt.outbox_id),
        )
    )

    claimed = db.claim_due_outbox(now=10, lease_seconds=60)

    assert [row.outbox_id for row in claimed] == [valid.outbox_id]
    quarantined = db.get_outbox_by_id(corrupt.outbox_id)
    assert quarantined is not None
    assert quarantined.status == "failed"
    assert quarantined.claim_token is None
    assert quarantined.lease_expires_at is None
    assert quarantined.content == {"corrupt_payload": True}
    assert quarantined.result == {"error": "[REDACTED]"}


def test_claim_due_outbox_quarantines_corrupt_row_before_applying_limit(db):
    corrupt = db.create_outbox(**_outbox_kwargs(outbox_id="ob-corrupt-limit"))
    valid = db.create_outbox(
        **_outbox_kwargs(outbox_id="ob-valid-limit", transaction_id="tx-valid-limit")
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_json = ? WHERE outbox_id = ?",
            ("{not-json", corrupt.outbox_id),
        )
    )

    claimed = db.claim_due_outbox(now=10, lease_seconds=60, limit=1)

    assert [row.outbox_id for row in claimed] == [valid.outbox_id]
    quarantined = db.get_outbox_by_id(corrupt.outbox_id)
    assert quarantined is not None
    assert quarantined.status == "failed"
    assert quarantined.claim_token is None
    assert quarantined.lease_expires_at is None


def test_claim_due_outbox_bounds_corrupt_prefix_inspection_by_limit(db):
    for index in range(5):
        corrupt = db.create_outbox(
            **_outbox_kwargs(
                outbox_id=f"ob-corrupt-prefix-{index}",
                transaction_id=f"tx-corrupt-prefix-{index}",
            )
        )
        db._execute_write(
            lambda conn, outbox_id=corrupt.outbox_id: conn.execute(
                "UPDATE mission_outbox SET content_json = ? WHERE outbox_id = ?",
                ("{not-json", outbox_id),
            )
        )
    valid = db.create_outbox(
        **_outbox_kwargs(outbox_id="ob-valid-after-prefix", transaction_id="tx-valid-after-prefix")
    )

    first = db.claim_due_outbox(now=10, lease_seconds=60, limit=1)

    assert first == []
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox WHERE status = 'failed'"
    ).fetchone()[0] == 4
    second = db.claim_due_outbox(now=10, lease_seconds=60, limit=1)
    assert [row.outbox_id for row in second] == [valid.outbox_id]


def test_claim_due_outbox_skips_rows_not_yet_due(db):
    db.create_outbox(**_outbox_kwargs())
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-future",
            delivery_id="dl-future",
            transaction_id="tx-future",
            not_before=100,
        )
    )
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)
    assert [r.delivery_id for r in claimed] == ["dl-1"]
    # Future row is still pending.
    fresh = db.get_outbox("dl-future")
    assert fresh is not None
    assert fresh.status == "pending"


def test_claim_due_outbox_skips_already_claimed_rows_no_double_claim(db):
    db.create_outbox(**_outbox_kwargs())
    first = db.claim_due_outbox(now=10, lease_seconds=60)
    assert [r.delivery_id for r in first] == ["dl-1"]

    # A second claim inside the lease window finds nothing.
    second = db.claim_due_outbox(now=11, lease_seconds=60)
    assert second == []


def test_claim_due_outbox_recovers_expired_claim(db):
    db.create_outbox(**_outbox_kwargs())
    db.claim_due_outbox(now=10, lease_seconds=30)

    # Move the wall clock past the lease; the row must be re-claimable.
    recovered = db.claim_due_outbox(now=41, lease_seconds=30)
    assert [r.delivery_id for r in recovered] == ["dl-1"]

    # A subsequent claim inside the new lease sees nothing.
    again = db.claim_due_outbox(now=42, lease_seconds=30)
    assert again == []


def test_claim_due_outbox_skips_terminal_rows(db):
    db.create_outbox(**_outbox_kwargs())
    db._execute_write(
        lambda c: c.execute(
            "UPDATE mission_outbox SET status='sent', updated_at=? "
            "WHERE delivery_id='dl-1'",
            (int(time.time()),),
        )
    )
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)
    assert claimed == []


def test_claim_due_outbox_returns_deep_copied_records(db):
    db.create_outbox(**_outbox_kwargs())
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)
    assert len(claimed) == 1
    claimed[0].content["text"] = "tampered"

    # The stored row was not mutated.
    fresh = db.get_outbox("dl-1")
    assert fresh is not None
    assert fresh.content == {"text": "hello"}


def test_reopen_preserves_outbox(tmp_path):
    db_path = tmp_path / "state.db"
    first = SessionDB(db_path=db_path)
    first.create_outbox(**_outbox_kwargs(content={"text": "hello"}))
    first.close()

    reopened = SessionDB(db_path=db_path)
    try:
        row = reopened.get_outbox("dl-1")
        assert row is not None
        assert row.content == {"text": "hello"}
        assert row.status == "scheduled"
    finally:
        reopened.close()


def test_outbox_timestamps_and_integers_stored_as_sqlite_integer(db):
    """not_before, revision, created_at, updated_at, and the lease
    timestamp set by ``claim_due_outbox`` are all integer at the
    storage boundary."""
    db.create_outbox(
        **_outbox_kwargs(not_before=5, revision=1)
    )
    row = db._conn.execute(
        "SELECT typeof(not_before), typeof(revision), typeof(created_at), "
        "typeof(updated_at) FROM mission_outbox WHERE delivery_id='dl-1'"
    ).fetchone()
    assert tuple(row) == ("integer", "integer", "integer", "integer")

    # Claim sets status='claimed' and updated_at = now (int). The
    # lease-window check uses the same integer for arithmetic.
    db.claim_due_outbox(now=10, lease_seconds=60)
    row = db._conn.execute(
        "SELECT typeof(updated_at), status FROM mission_outbox "
        "WHERE delivery_id='dl-1'"
    ).fetchone()
    assert tuple(row) == ("integer", "claimed")


def test_create_outbox_coerces_floats_at_boundary(db):
    """Floats are no longer silently coerced at the write boundary —
    storage columns are INTEGER, so a caller-supplied float for
    ``not_before`` / ``revision`` must raise before SQL."""
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(not_before=3.7))
    count = db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox"
    ).fetchone()[0]
    assert count == 0


def test_create_outbox_rejects_non_int_not_before_and_revision(db):
    """Integer boundary: ``not_before`` and ``revision`` are INTEGER
    storage columns; reject any non-int (incl. bool) before SQL."""
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(not_before=5.0))
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(not_before="5"))
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(not_before=True))
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(revision=1.0))
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(revision="1"))
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(revision=True))
    count = db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox"
    ).fetchone()[0]
    assert count == 0


def test_create_outbox_rejects_nonpositive_revision(db):
    """``revision`` starts at 1 by API contract — a zero or negative
    value is rejected before SQL rather than silently stored."""
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(revision=0))
    with pytest.raises(ValueError):
        db.create_outbox(**_outbox_kwargs(revision=-1))
    count = db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox"
    ).fetchone()[0]
    assert count == 0


def test_create_outbox_accepts_int_not_before_and_revision(db):
    """Legitimate integer inputs are accepted and stored as INTEGER."""
    db.create_outbox(**_outbox_kwargs(not_before=5, revision=2))
    row = db._conn.execute(
        "SELECT not_before, revision, typeof(not_before), typeof(revision) "
        "FROM mission_outbox WHERE delivery_id='dl-1'"
    ).fetchone()
    assert (row[0], row[1]) == (5, 2)
    assert (row[2], row[3]) == ("integer", "integer")


def test_claim_due_outbox_rejects_non_int_now(db):
    """``now`` is compared against INTEGER columns — reject floats
    and bools before SQL, do not silently truncate."""
    db.create_outbox(**_outbox_kwargs())
    with pytest.raises(ValueError):
        db.claim_due_outbox(now=10.0)
    with pytest.raises(ValueError):
        db.claim_due_outbox(now="10")
    with pytest.raises(ValueError):
        db.claim_due_outbox(now=True)


def test_claim_due_outbox_rejects_non_int_or_nonpositive_lease(db):
    """``lease_seconds`` is an INTEGER boundary too — reject floats,
    bools, and any nonpositive value (a zero/negative lease would
    immediately relock every claimed row)."""
    db.create_outbox(**_outbox_kwargs())
    with pytest.raises(ValueError):
        db.claim_due_outbox(now=10, lease_seconds=60.0)
    with pytest.raises(ValueError):
        db.claim_due_outbox(now=10, lease_seconds="60")
    with pytest.raises(ValueError):
        db.claim_due_outbox(now=10, lease_seconds=True)
    with pytest.raises(ValueError):
        db.claim_due_outbox(now=10, lease_seconds=0)
    with pytest.raises(ValueError):
        db.claim_due_outbox(now=10, lease_seconds=-1)


def test_claim_due_outbox_accepts_int_now_and_lease(db):
    """Integer ``now`` and ``lease_seconds`` are accepted and stored as INTEGER."""
    db.create_outbox(**_outbox_kwargs(not_before=0))
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)
    assert [r.delivery_id for r in claimed] == ["dl-1"]
    row = db._conn.execute(
        "SELECT typeof(updated_at) FROM mission_outbox WHERE delivery_id='dl-1'"
    ).fetchone()
    assert row[0] == "integer"


@pytest.mark.parametrize("invalid_limit", [True, False, 1.0, "1", 0, -1])
def test_claim_due_outbox_rejects_invalid_limit(db, invalid_limit):
    """``limit`` is a strict positive built-in ``int`` boundary."""
    db.create_outbox(**_outbox_kwargs())
    with pytest.raises(ValueError, match="limit"):
        db.claim_due_outbox(now=10, limit=invalid_limit)


def test_claim_due_outbox_accepts_positive_builtin_limit(db):
    db.create_outbox(**_outbox_kwargs())
    claimed = db.claim_due_outbox(now=10, limit=1)
    assert [row.delivery_id for row in claimed] == ["dl-1"]


def test_create_outbox_payload_strings_round_trip_as_strings(db):
    """Plain Python strings are valid JSON values, not pre-serialized
    JSON; ``_canonicalize_payload`` stores them via ``json.dumps`` so
    they round-trip back as the original string."""
    db.create_outbox(
        **_outbox_kwargs(
            content="hello world",
            approval="approved-by-user",
            result="delivered-ok",
        )
    )
    row = db.get_outbox("dl-1")
    assert row is not None
    assert row.content == "hello world"
    assert row.approval == "approved-by-user"
    assert row.result == "delivered-ok"

    # And the stored JSON is the canonical JSON-encoded string form.
    stored = db._conn.execute(
        "SELECT content_json, approval_json, result_json "
        "FROM mission_outbox WHERE delivery_id='dl-1'"
    ).fetchone()
    assert stored[0] == '"hello world"'
    assert stored[1] == '"approved-by-user"'
    assert stored[2] == '"delivered-ok"'


# ─────────────────────────────────────────────────────────────────────
# Lifecycle transitions (Task 7 storage seam)
# ─────────────────────────────────────────────────────────────────────
#
# These tests cover the durable outbox lifecycle storage seam required
# before the Task 7 dispatcher/runtime integration. They are deliberately
# separated from the create/claim tests above and exercise the API the
# dispatcher will rely on:
#
#   * terminal ``unknown`` status vocabulary
#   * explicit pending/claimed transition map
#   * ``get_outbox_by_id`` (stable row-key reads)
#   * ``transition_outbox`` (status CAS)
#   * ``revise_outbox`` (pending-only content bump with revision bump)
#   * ``set_outbox_approval`` (pending-only approval JSON update)
#   * ``cancel_outbox`` (pending-only cancel CAS)
#
# Terminal statuses (``sent`` / ``failed`` / ``unknown`` / ``cancelled``)
# MUST be non-claimable — the existing ``claim_due_outbox`` only returns
# pending rows (or expired-claim rows), so the transition is enforced
# by the dispatcher path that asks ``get_outbox`` / ``transition_outbox``
# before retrying.


def test_get_outbox_by_id_returns_record_for_known_outbox_id(db):
    db.create_outbox(**_outbox_kwargs(outbox_id="ob-known"))
    row = db.get_outbox_by_id("ob-known")
    assert row is not None
    assert row.outbox_id == "ob-known"
    assert row.delivery_id == "dl-known"


def test_get_outbox_by_id_returns_none_for_unknown_outbox_id(db):
    assert db.get_outbox_by_id("ob-missing") is None


def test_get_outbox_by_id_returns_deep_copied_content(db):
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-dc",
            delivery_id="dl-dc",
            transaction_id="tx-dc",
            content={"text": "hello"},
            approval={"actor": "u1"},
        )
    )
    row = db.get_outbox_by_id("ob-dc")
    assert row is not None
    row.content["text"] = "tampered"
    row.approval["actor"] = "intruder"

    fresh = db.get_outbox_by_id("ob-dc")
    assert fresh is not None
    assert fresh.content == {"text": "hello"}
    assert fresh.approval == {"actor": "u1"}


def test_get_outbox_by_id_preserves_existing_get_outbox_by_delivery_id(db):
    """``get_outbox(delivery_id)`` keeps its prior behavior after
    ``get_outbox_by_id`` is added — the two are independent lookups."""
    db.create_outbox(**_outbox_kwargs(outbox_id="ob-both"))

    by_id = db.get_outbox_by_id("ob-both")
    by_dl = db.get_outbox("dl-both")
    assert by_id is not None and by_dl is not None
    assert by_id.outbox_id == by_dl.outbox_id == "ob-both"
    assert by_id.delivery_id == by_dl.delivery_id == "dl-both"


# ── Transition map (status vocabulary + illegal-jump rejection) ──────


def test_unknown_status_is_in_vocabulary_and_not_claimable(db):
    """``unknown`` is a terminal status (ambiguous post-dispatch ack).
    It must be in the status vocabulary and must not be re-claimable
    by ``claim_due_outbox``.
    """
    db.create_outbox(**_outbox_kwargs())

    # Move to ``unknown`` from ``claimed`` via the explicit CAS.
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)[0]
    assert db.transition_outbox(
        "ob-1",
        expected_status="claimed",
        next_status="unknown",
        claim_token=claimed.claim_token,
    ) is True

    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "unknown"

    # Terminal: a claim call returns nothing.
    assert db.claim_due_outbox(now=20, lease_seconds=60) == []


def test_pending_to_claimed_is_allowed_pending_to_cancelled_is_allowed(db):
    """The transition map allows both ``pending -> claimed`` and
    ``pending -> cancelled``; nothing else is reachable directly from
    ``pending``."""
    # pending -> claimed is done by claim_due_outbox (already covered
    # elsewhere); we re-verify the explicit CAS path here.
    a = db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-tc-a",
            delivery_id="dl-tc-a",
            transaction_id="tx-tc-a",
        )
    )
    assert a.status == "pending"
    assert (
        db.transition_outbox(
            "ob-tc-a", expected_status="pending", next_status="claimed"
        )
        is True
    )

    # pending -> cancelled is the cancel path. Not mission-linked — this
    # test exercises plain CAS mechanics, not the mission/effect graph.
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-tc-b",
            delivery_id="dl-tc-b",
            mission_id=None,
            transaction_id=None,
        )
    )
    assert (
        db.cancel_outbox("ob-tc-b", expected_revision=1) is True
    )
    row = db.get_outbox_by_id("ob-tc-b")
    assert row is not None
    assert row.status == "cancelled"


def test_pending_to_terminal_sent_or_failed_or_unknown_is_rejected(db):
    """``pending`` may only move to ``claimed`` or ``cancelled`` —
    any other status (including ``sent`` / ``failed`` / ``unknown``)
    is an illegal jump and must raise before SQL."""
    db.create_outbox(**_outbox_kwargs())

    for illegal in ("sent", "failed", "unknown"):
        with pytest.raises(ValueError):
            db.transition_outbox(
                "ob-1",
                expected_status="pending",
                next_status=illegal,
            )
    # No row mutation occurred.
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "pending"


def test_claimed_to_sent_failed_unknown_are_allowed_via_transition_outbox(db):
    """``claimed`` admits exactly ``sent``, ``failed``, ``unknown`` —
    not pending and not cancelled."""
    for nxt in ("sent", "failed", "unknown"):
        db.create_outbox(
            **_outbox_kwargs(
                outbox_id=f"ob-claimed-{nxt}",
                delivery_id=f"dl-claimed-{nxt}",
                transaction_id=f"tx-claimed-{nxt}",
            )
        )
        claimed = db.claim_due_outbox(now=10, lease_seconds=60)[0]
        assert (
            db.transition_outbox(
                f"ob-claimed-{nxt}",
                expected_status="claimed",
                next_status=nxt,
                claim_token=claimed.claim_token,
            )
            is True
        )
        row = db.get_outbox_by_id(f"ob-claimed-{nxt}")
        assert row is not None
        assert row.status == nxt


def test_claimed_to_pending_or_cancelled_is_rejected(db):
    """``claimed`` cannot regress to ``pending`` or be cancelled —
    the cancel path is pending-only by contract."""
    db.create_outbox(**_outbox_kwargs())
    assert (
        db.transition_outbox(
            "ob-1", expected_status="pending", next_status="claimed"
        )
        is True
    )

    for illegal in ("pending", "cancelled"):
        with pytest.raises(ValueError):
            db.transition_outbox(
                "ob-1",
                expected_status="claimed",
                next_status=illegal,
            )
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "claimed"


def test_terminal_statuses_have_no_outgoing_transitions(db):
    """``sent`` / ``failed`` / ``unknown`` / ``cancelled`` are terminal —
    no status transition may leave them, and CAS on them fails because
    the expected status won't match the current row."""
    targets = ("sent", "failed", "unknown", "cancelled")
    for nxt in targets:
        # Not mission-linked for the "cancelled" case: the cancel path here
        # exercises the plain CAS/status contract, not the mission/effect
        # graph — a mission-linked row must have a matching effect row
        # (P0-2) to be cancellable at all.
        db.create_outbox(
            **_outbox_kwargs(
                outbox_id=f"ob-term-{nxt}",
                delivery_id=f"dl-term-{nxt}",
                mission_id=None if nxt == "cancelled" else "m-1",
                transaction_id=None if nxt == "cancelled" else f"tx-term-{nxt}",
            )
        )
        # Drive each row to its target state through the legal path.
        if nxt == "cancelled":
            assert db.cancel_outbox(
                f"ob-term-{nxt}", expected_revision=1
            ) is True
        else:
            claimed = db.claim_due_outbox(now=10, lease_seconds=60)[0]
            assert (
                db.transition_outbox(
                    f"ob-term-{nxt}",
                    expected_status="claimed",
                    next_status=nxt,
                    claim_token=claimed.claim_token,
                )
                is True
            )

    # No terminal status admits any further transition — including
    # self-loops, which are illegal by contract (validate-before-SQL).
    for term in targets:
        outbox_id = f"ob-term-{term}"
        for nxt in ("pending", "claimed", "sent", "failed", "unknown", "cancelled"):
            with pytest.raises(ValueError):
                db.transition_outbox(
                    outbox_id,
                    expected_status=term,
                    next_status=nxt,
                )

    # And CAS on a terminal row at its own status never mutates the
    # row (self-transition is illegal, so validation raises).
    for term in targets:
        outbox_id = f"ob-term-{term}"
        with pytest.raises(ValueError):
            db.transition_outbox(
                outbox_id,
                expected_status=term,
                next_status=term,
            )
        # Row still untouched: same updated_at as before any terminal
        # transition (post-claim, the clock has moved on, so just
        # assert the status is still the terminal value).
        row = db.get_outbox_by_id(outbox_id)
        assert row is not None
        assert row.status == term


def test_lease_recovery_remains_the_only_claimed_to_claimed_refresh(db):
    """The CAS path can never move ``claimed -> claimed``; lease
    recovery stays the sole controlled refresh route (via
    ``claim_due_outbox``'s UPDATE inside the same method)."""
    db.create_outbox(**_outbox_kwargs())
    assert (
        db.transition_outbox(
            "ob-1", expected_status="pending", next_status="claimed"
        )
        is True
    )
    with pytest.raises(ValueError):
        db.transition_outbox(
            "ob-1", expected_status="claimed", next_status="claimed"
        )


# ── transition_outbox ────────────────────────────────────────────────


def test_transition_outbox_returns_false_for_missing_row(db):
    """No row, no transition — returns False (not raising)."""
    assert (
        db.transition_outbox(
            "ob-missing",
            expected_status="pending",
            next_status="claimed",
        )
        is False
    )


def test_transition_outbox_returns_false_for_stale_expected_status(db):
    """CAS: a row in the wrong current status must NOT be mutated."""
    db.create_outbox(**_outbox_kwargs())
    # Row is ``pending`` but the caller thinks it's ``claimed``.
    assert (
        db.transition_outbox(
            "ob-1",
            expected_status="claimed",
            next_status="sent",
        )
        is False
    )
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "pending"


def test_transition_outbox_validates_before_sql(db):
    """An invalid ``next_status`` raises before SQL — the row is
    not touched."""
    db.create_outbox(**_outbox_kwargs())
    with pytest.raises(ValueError):
        db.transition_outbox(
            "ob-1", expected_status="pending", next_status="wat"
        )
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "pending"


def test_transition_outbox_omitted_result_preserves_pre_existing_value(db):
    """Omitting ``result`` keeps the existing ``result_json`` unchanged.
    The legal path to mutate the column is ``claimed -> failed`` (the
    only claim-exit transition we can drive from a fresh row here
    without bumping into terminal ``sent``, which has no outgoing
    transitions by contract)."""
    db.create_outbox(
        **_outbox_kwargs(
            result={"code": 200, "ok": True},
        )
    )
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)[0]
    # Result column is not touched because ``result`` was omitted.
    assert (
        db.transition_outbox(
            "ob-1",
            expected_status="claimed",
            next_status="failed",
            claim_token=claimed.claim_token,
        )
        is True
    )
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.result == {"code": 200, "ok": True}
    stored = db._conn.execute(
        "SELECT result_json FROM mission_outbox WHERE outbox_id='ob-1'"
    ).fetchone()[0]
    assert stored == json.dumps(
        {"code": 200, "ok": True}, sort_keys=True, separators=(",", ":")
    )


def test_transition_outbox_explicit_none_persists_canonical_json_null(db):
    """Passing ``result=None`` explicitly persists the canonical JSON
    ``null`` literal (``result_json == "null"`` as stored TEXT) and the
    round-trip reads back as Python ``None`` — distinct from omitting
    the argument (which preserves the pre-existing value) and distinct
    from the literal Python string ``"null"`` (which round-trips as
    ``'"null"'`` with quotes)."""
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-clr",
            delivery_id="dl-clr",
            transaction_id="tx-clr",
            result={"code": 200, "ok": True},
        )
    )
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)[0]
    assert (
        db.transition_outbox(
            "ob-clr",
            expected_status="claimed",
            next_status="failed",
            result=None,
            claim_token=claimed.claim_token,
        )
        is True
    )
    row = db.get_outbox_by_id("ob-clr")
    assert row is not None
    # Read API surfaces JSON null as Python None.
    assert row.result is None
    stored = db._conn.execute(
        "SELECT result_json FROM mission_outbox "
        "WHERE outbox_id='ob-clr'"
    ).fetchone()[0]
    # Stored as canonical JSON null TEXT, NOT SQL NULL — the two are
    # distinct on disk: an explicit ``None`` payload is a terminal
    # result the caller supplied, while a SQL NULL would mean the
    # column was never set (or was cleared via set_outbox_approval).
    assert stored == "null"
    # And the literal string "null" still round-trips as a string,
    # proving the two are not conflated.


def test_transition_outbox_string_null_round_trips_as_string(db):
    """The Python string ``"null"`` is just a normal payload under the
    new contract — canonical JSON encodes it as ``"null"`` (with
    quotes) so it round-trips back to the same string instead of being
    silently destroyed as a JSON null. The legal path is
    ``claimed -> failed`` (terminal ``sent`` has no outgoing
    transitions)."""
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-str-null",
            delivery_id="dl-str-null",
            transaction_id="tx-str-null",
        )
    )
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)[0]
    assert (
        db.transition_outbox(
            "ob-str-null",
            expected_status="claimed",
            next_status="failed",
            result="null",
            claim_token=claimed.claim_token,
        )
        is True
    )
    row = db.get_outbox_by_id("ob-str-null")
    assert row is not None
    assert row.result == "null"
    stored = db._conn.execute(
        "SELECT result_json FROM mission_outbox "
        "WHERE outbox_id='ob-str-null'"
    ).fetchone()[0]
    # JSON-encoded string with quotes — what ``json.dumps("null", ...)``
    # produces verbatim.
    assert stored == '"null"'


def test_transition_outbox_supplied_dict_payload_canonicalizes(db):
    """Sanity check: a caller-supplied dict still canonicalizes through
    the same code path that the new contract leaves intact. The
    supplied-payload path is exercised through the only legal claim-exit
    transitions (``claimed -> sent`` / ``claimed -> failed`` /
    ``claimed -> unknown``) since terminal states have no outgoing
    transitions."""
    db.create_outbox(**_outbox_kwargs())
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)[0]
    # Omitted: result remains None.
    assert (
        db.transition_outbox(
            "ob-1",
            expected_status="claimed",
            next_status="sent",
            claim_token=claimed.claim_token,
        )
        is True
    )
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.result is None

    # Supplied canonical dict on the legal claim-exit path: persists
    # as canonical JSON. Use a fresh row so we can chain another
    # claimed-exit transition (terminal ``sent`` has no outgoing
    # transitions by contract).
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-rs",
            delivery_id="dl-rs",
            transaction_id="tx-rs",
        )
    )
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)[0]
    assert (
        db.transition_outbox(
            "ob-rs",
            expected_status="claimed",
            next_status="failed",
            result={"code": 500, "reason": "boom"},
            claim_token=claimed.claim_token,
        )
        is True
    )
    row = db.get_outbox_by_id("ob-rs")
    assert row is not None
    assert row.result == {"code": 500, "reason": "boom"}


def test_transition_outbox_bumps_updated_at_on_success(db):
    """A successful transition refreshes ``updated_at`` — the same
    field the lease-recovery path reads as the lease timestamp."""
    db.create_outbox(**_outbox_kwargs())
    before = db._conn.execute(
        "SELECT updated_at FROM mission_outbox WHERE outbox_id='ob-1'"
    ).fetchone()[0]

    # Wait one second so the integer clock moves. Tests use UTC and
    # this is a coarse-grained freshness check.
    time.sleep(1.05)

    assert (
        db.transition_outbox(
            "ob-1", expected_status="pending", next_status="claimed"
        )
        is True
    )
    after = db._conn.execute(
        "SELECT updated_at FROM mission_outbox WHERE outbox_id='ob-1'"
    ).fetchone()[0]
    assert after > before


def test_transition_outbox_never_mutates_a_terminal_row(db):
    """Even if expected_status accidentally matches a terminal row,
    no transition is allowed out of a terminal status — validation
    raises before SQL, so the row's ``updated_at`` and ``status``
    are not touched."""
    db.create_outbox(**_outbox_kwargs())
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)[0]
    assert (
        db.transition_outbox(
            "ob-1",
            expected_status="claimed",
            next_status="sent",
            claim_token=claimed.claim_token,
        )
        is True
    )
    # Snapshot the post-transition state. ``sent`` is terminal — no
    # transition may leave it.
    before = db._conn.execute(
        "SELECT updated_at, status FROM mission_outbox WHERE outbox_id='ob-1'"
    ).fetchone()
    # Even a self-loop (expected_status matches the current terminal
    # status) must raise, not silently false — validation runs before
    # SQL and self-transitions are illegal by contract.
    with pytest.raises(ValueError):
        db.transition_outbox(
            "ob-1", expected_status="sent", next_status="sent"
        )
    with pytest.raises(ValueError):
        db.transition_outbox(
            "ob-1", expected_status="sent", next_status="failed"
        )
    after = db._conn.execute(
        "SELECT updated_at, status FROM mission_outbox WHERE outbox_id='ob-1'"
    ).fetchone()
    assert after == before


# ── revise_outbox ─────────────────────────────────────────────────────


def test_revise_outbox_bumps_content_and_revision_atomically(db):
    """Successful revise updates an ordinary outbox row atomically."""
    db.create_outbox(
        **_outbox_kwargs(
            content={"text": "v1"},
            revision=1,
            mission_id=None,
            transaction_id=None,
        )
    )
    revised = db.revise_outbox(
        "ob-1",
        expected_revision=1,
        content={"text": "v2"},
        not_before=10,
    )
    assert revised is not None
    assert revised.content == {"text": "v2"}
    assert revised.revision == 2
    assert revised.not_before == 10
    # Identity preserved.
    assert revised.outbox_id == "ob-1"
    assert revised.delivery_id == "dl-1"
    assert revised.platform == "telegram"
    assert revised.target == "chat:1"
    assert revised.mission_id is None
    assert revised.transaction_id is None

    # And the row in SQLite matches.
    stored = db._conn.execute(
        "SELECT content_json, revision, not_before FROM mission_outbox "
        "WHERE outbox_id='ob-1'"
    ).fetchone()
    assert stored[0] == json.dumps({"text": "v2"}, sort_keys=True, separators=(",", ":"))
    assert stored[1] == 2
    assert stored[2] == 10


def test_revise_outbox_returns_none_for_missing_row(db):
    assert (
        db.revise_outbox(
            "ob-missing",
            expected_revision=1,
            content={"text": "x"},
            not_before=0,
        )
        is None
    )


def test_revise_outbox_rejects_missing_linked_effect_atomically(db):
    created = db.create_outbox(
        **_outbox_kwargs(
            mission_id="mission-missing-effect",
            transaction_id="tx-missing-effect",
            execution_id="exec-missing-effect",
            node_id="notify",
            content={"text": "v1"},
            preview={"summary": "preview-v1"},
        )
    )
    before = tuple(
        db._conn.execute(
            "SELECT * FROM mission_outbox WHERE outbox_id=?",
            (created.outbox_id,),
        ).fetchone()
    )
    assert db.get_effect_transaction("tx-missing-effect") is None
    assert db._conn.execute("SELECT COUNT(*) FROM agent_operations").fetchone()[0] == 0

    with pytest.raises(ValueError, match="linked mission effect is missing"):
        db.revise_outbox(
            created.outbox_id,
            expected_revision=1,
            content={"text": "v2"},
            not_before=10,
            preview={"summary": "preview-v2"},
        )

    after = tuple(
        db._conn.execute(
            "SELECT * FROM mission_outbox WHERE outbox_id=?",
            (created.outbox_id,),
        ).fetchone()
    )
    assert after == before
    assert db.get_effect_transaction("tx-missing-effect") is None
    assert db._conn.execute("SELECT COUNT(*) FROM agent_operations").fetchone()[0] == 0


def test_revise_mission_rejects_corrupt_linked_effect_identity_atomically(db):
    store = MissionOutboxStore(db)
    original = store.materialize(
        execution_id="exec-revision-corrupt-effect",
        node_id="notify",
        mission_id="mission-revision-corrupt-effect",
        platform="telegram",
        target="chat:corrupt",
        content={"text": "v1"},
        preview={"summary": "preview-v1"},
    )
    before = tuple(
        db._conn.execute(
            "SELECT * FROM mission_outbox WHERE outbox_id=?", (original.outbox_id,)
        ).fetchone()
    )
    assert original.transaction_id is not None
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE effect_transactions SET prepared_json=? WHERE transaction_id=?",
            (
                json.dumps(
                    {
                        "delivery_kind": "outbox",
                        "platform": "telegram",
                        "target": "chat:WRONG",
                        "content_hash": original.content_hash,
                        "execution_id": original.execution_id,
                        "node_id": original.node_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                original.transaction_id,
            ),
        )
    )

    with pytest.raises(ValueError, match="linked mission effect|prepared identity"):
        store.revise(
            original.outbox_id,
            expected_revision=original.revision,
            content={"text": "v2"},
            preview={"summary": "preview-v2"},
        )

    after = tuple(
        db._conn.execute(
            "SELECT * FROM mission_outbox WHERE outbox_id=?", (original.outbox_id,)
        ).fetchone()
    )
    assert after == before
    effect = db.get_effect_transaction(original.transaction_id)
    assert effect is not None
    assert effect.prepared["target"] == "chat:WRONG"


def test_revise_outbox_returns_none_for_stale_revision(db):
    """CAS on revision: a stale ``expected_revision`` returns None
    and does not mutate the row."""
    db.create_outbox(**_outbox_kwargs(revision=1))
    result = db.revise_outbox(
        "ob-1",
        expected_revision=2,  # wrong — actual is 1
        content={"text": "v2"},
        not_before=0,
    )
    assert result is None
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.revision == 1
    assert row.content == {"text": "hello"}


def test_revise_outbox_returns_none_for_non_pending_row(db):
    """Only ``pending`` rows can be revised — once claimed/sent/etc.,
    content is immutable."""
    db.create_outbox(**_outbox_kwargs(revision=1))
    assert (
        db.transition_outbox(
            "ob-1", expected_status="pending", next_status="claimed"
        )
        is True
    )
    result = db.revise_outbox(
        "ob-1",
        expected_revision=1,
        content={"text": "v2"},
        not_before=0,
    )
    assert result is None
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.revision == 1
    assert row.content == {"text": "hello"}
    assert row.status == "claimed"


def test_revise_outbox_validates_boundary_types(db):
    """Integer-boundary validation runs before SQL — non-int
    ``not_before`` / ``revision`` (incl. bool / float / str) is
    rejected with no row mutation."""
    db.create_outbox(**_outbox_kwargs(revision=1))

    for bad_nb in (1.5, "5", True):
        with pytest.raises(ValueError):
            db.revise_outbox(
                "ob-1",
                expected_revision=1,
                content={"text": "v2"},
                not_before=bad_nb,
            )
    for bad_rev in (1.0, "1", True, 0, -1):
        with pytest.raises(ValueError):
            db.revise_outbox(
                "ob-1",
                expected_revision=bad_rev,
                content={"text": "v2"},
                not_before=0,
            )
    # No mutation occurred from any of those attempts.
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.revision == 1
    assert row.content == {"text": "hello"}


def test_revise_outbox_preserves_identity_fields(db):
    """``revise_outbox`` MUST NOT change delivery identity fields."""
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-id",
            delivery_id="dl-id",
            transaction_id=None,
            mission_id=None,
            platform="telegram",
            target="chat:42",
            content={"text": "v1"},
            revision=1,
        )
    )
    revised = db.revise_outbox(
        "ob-id",
        expected_revision=1,
        content={"text": "v2"},
        not_before=5,
    )
    assert revised is not None
    assert revised.delivery_id == "dl-id"
    assert revised.platform == "telegram"
    assert revised.target == "chat:42"
    assert revised.mission_id is None
    assert revised.transaction_id is None

    # And the storage row matches.
    row = db._conn.execute(
        "SELECT delivery_id, platform, target, mission_id, transaction_id "
        "FROM mission_outbox WHERE outbox_id='ob-id'"
    ).fetchone()
    assert tuple(row) == ("dl-id", "telegram", "chat:42", None, None)


def test_revise_outbox_returns_deep_copied_frozen_record(db):
    """The returned record is a frozen dataclass with deep-copied
    structured fields."""
    db.create_outbox(
        **_outbox_kwargs(
            content={"text": "v1"},
            revision=1,
            mission_id=None,
            transaction_id=None,
        )
    )
    revised = db.revise_outbox(
        "ob-1",
        expected_revision=1,
        content={"text": "v2"},
        not_before=0,
    )
    assert revised is not None
    with pytest.raises(FrozenInstanceError):
        revised.content = {"text": "tampered"}  # type: ignore[misc]
    revised.content["text"] = "tampered"
    fresh = db.get_outbox_by_id("ob-1")
    assert fresh is not None
    assert fresh.content == {"text": "v2"}


def test_revise_outbox_canonicalizes_content(db):
    """Content is canonical JSON — key-sorted, tight separators — so
    an equivalent dict with different key order round-trips identically."""
    db.create_outbox(
        **_outbox_kwargs(
            content={"text": "v1"},
            revision=1,
            mission_id=None,
            transaction_id=None,
        )
    )
    db.revise_outbox(
        "ob-1",
        expected_revision=1,
        content={"b": 2, "a": 1},  # unsorted input
        not_before=0,
    )
    stored = db._conn.execute(
        "SELECT content_json FROM mission_outbox WHERE outbox_id='ob-1'"
    ).fetchone()[0]
    assert stored == '{"a":1,"b":2}'


def test_revise_outbox_preserves_approval_and_result(db):
    """Revise must not touch approval or result — those are managed
    by their own dedicated methods."""
    db.create_outbox(
        **_outbox_kwargs(
            revision=1,
            approval={"actor": "u1"},
            result={"ok": True},
            mission_id=None,
            transaction_id=None,
        )
    )
    revised = db.revise_outbox(
        "ob-1",
        expected_revision=1,
        content={"text": "v2"},
        not_before=0,
    )
    assert revised is not None
    assert revised.approval == {"actor": "u1"}
    assert revised.result == {"ok": True}


def test_revise_outbox_serial_revision_chain(db):
    """Sequential revises chain correctly: 1 -> 2 -> 3, each CAS'd
    on the prior revision."""
    db.create_outbox(
        **_outbox_kwargs(
            revision=1,
            mission_id=None,
            transaction_id=None,
        )
    )
    r2 = db.revise_outbox(
        "ob-1", expected_revision=1, content={"v": 2}, not_before=0
    )
    assert r2 is not None and r2.revision == 2
    r3 = db.revise_outbox(
        "ob-1", expected_revision=2, content={"v": 3}, not_before=0
    )
    assert r3 is not None and r3.revision == 3
    # Stale attempt fails.
    stale = db.revise_outbox(
        "ob-1", expected_revision=1, content={"v": 99}, not_before=0
    )
    assert stale is None
    final = db.get_outbox_by_id("ob-1")
    assert final is not None
    assert final.revision == 3
    assert final.content == {"v": 3}


# ── set_outbox_approval ──────────────────────────────────────────────


def test_set_outbox_approval_persists_canonical_json(db):
    """Approval is canonical JSON; passing a dict writes it
    key-sorted with tight separators."""
    db.create_outbox(**_outbox_kwargs())
    assert (
        db.set_outbox_approval(
            "ob-1", expected_revision=1, approval={"b": 2, "a": 1}
        )
        is True
    )
    stored = db._conn.execute(
        "SELECT approval_json FROM mission_outbox WHERE outbox_id='ob-1'"
    ).fetchone()[0]
    assert stored == '{"a":1,"b":2}'
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.approval == {"a": 1, "b": 2}


def test_set_outbox_approval_returns_false_for_missing_row(db):
    assert (
        db.set_outbox_approval(
            "ob-missing", expected_revision=1, approval={"ok": True}
        )
        is False
    )


def test_set_outbox_approval_returns_false_for_stale_revision(db):
    """CAS on revision: stale ``expected_revision`` is a no-op."""
    db.create_outbox(**_outbox_kwargs(revision=3))
    assert (
        db.set_outbox_approval(
            "ob-1", expected_revision=1, approval={"ok": True}
        )
        is False
    )
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.approval is None


def test_set_outbox_approval_returns_false_for_non_pending_row(db):
    """Only ``pending`` rows may have their approval mutated."""
    db.create_outbox(**_outbox_kwargs())
    assert (
        db.transition_outbox(
            "ob-1", expected_status="pending", next_status="claimed"
        )
        is True
    )
    assert (
        db.set_outbox_approval(
            "ob-1", expected_revision=1, approval={"ok": True}
        )
        is False
    )
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.approval is None


def test_set_outbox_approval_can_clear_with_python_none(db):
    """Approval can be cleared to SQL NULL by passing ``approval=None``
    — the round-trip reads back as Python ``None``."""
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-clr",
            delivery_id="dl-clr",
            transaction_id="tx-clr",
            approval={"actor": "u1"},
        )
    )
    assert (
        db.set_outbox_approval(
            "ob-clr", expected_revision=1, approval=None
        )
        is True
    )
    row = db.get_outbox_by_id("ob-clr")
    assert row is not None
    assert row.approval is None
    # SQLite NULL surfaces via sqlite3 as Python ``None`` on read,
    # distinct from the JSON string ``"null"``.
    stored = db._conn.execute(
        "SELECT approval_json FROM mission_outbox WHERE outbox_id='ob-clr'"
    ).fetchone()[0]
    assert stored is None


def test_set_outbox_approval_string_null_round_trips_as_string(db):
    """The Python string ``"null"`` is just a normal approval payload —
    canonical JSON encodes it as ``"null"`` (with quotes) so it
    round-trips back to the same string instead of being silently
    collapsed to a SQL clear."""
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-sn",
            delivery_id="dl-sn",
            transaction_id="tx-sn",
        )
    )
    assert (
        db.set_outbox_approval(
            "ob-sn", expected_revision=1, approval="null"
        )
        is True
    )
    row = db.get_outbox_by_id("ob-sn")
    assert row is not None
    assert row.approval == "null"
    stored = db._conn.execute(
        "SELECT approval_json FROM mission_outbox "
        "WHERE outbox_id='ob-sn'"
    ).fetchone()[0]
    assert stored == '"null"'


def test_set_outbox_approval_does_not_mutate_content_or_identity(db):
    """``set_outbox_approval`` must not touch content, status, or
    delivery identity fields."""
    db.create_outbox(
        **_outbox_kwargs(
            outbox_id="ob-imm",
            delivery_id="dl-imm",
            transaction_id="tx-imm",
            content={"text": "frozen"},
        )
    )
    assert (
        db.set_outbox_approval(
            "ob-imm", expected_revision=1, approval={"ok": True}
        )
        is True
    )
    row = db._conn.execute(
        "SELECT status, content_json, delivery_id, platform, target "
        "FROM mission_outbox WHERE outbox_id='ob-imm'"
    ).fetchone()
    assert row[0] == "pending"
    assert row[1] == json.dumps({"text": "frozen"}, sort_keys=True, separators=(",", ":"))
    assert row[2] == "dl-imm"
    assert row[3] == "telegram"
    assert row[4] == "chat:1"


def test_set_outbox_approval_validates_revision_boundary(db):
    """Integer validation runs before SQL — non-int / non-positive
    ``expected_revision`` is rejected."""
    db.create_outbox(**_outbox_kwargs())
    for bad in (1.0, "1", True, 0, -1):
        with pytest.raises(ValueError):
            db.set_outbox_approval(
                "ob-1", expected_revision=bad, approval={"ok": True}
            )
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.approval is None


# ── cancel_outbox ─────────────────────────────────────────────────────


def test_cancel_outbox_transitions_pending_to_cancelled(db):
    """``cancel_outbox`` is the pending-only CAS path to ``cancelled``."""
    db.create_outbox(**_outbox_kwargs(mission_id=None, transaction_id=None))
    assert db.cancel_outbox("ob-1", expected_revision=1) is True
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "cancelled"


def test_cancel_outbox_returns_false_for_missing_row(db):
    assert db.cancel_outbox("ob-missing", expected_revision=1) is False


def test_cancel_outbox_returns_false_for_stale_revision(db):
    db.create_outbox(**_outbox_kwargs(revision=2))
    assert db.cancel_outbox("ob-1", expected_revision=1) is False
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "pending"


def test_cancel_outbox_returns_false_for_non_pending_row(db):
    """Only ``pending`` rows can be cancelled — claimed/sent/etc. are
    reachable via ``transition_outbox`` only."""
    db.create_outbox(**_outbox_kwargs())
    assert (
        db.transition_outbox(
            "ob-1", expected_status="pending", next_status="claimed"
        )
        is True
    )
    assert db.cancel_outbox("ob-1", expected_revision=1) is False
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "claimed"


def test_cancel_outbox_preserves_content_approval_result(db):
    """Cancel does not delete the structured payload — content,
    approval, and result all survive."""
    db.create_outbox(
        **_outbox_kwargs(
            content={"text": "preserved"},
            approval={"actor": "u1"},
            result=None,
            mission_id=None,
            transaction_id=None,
        )
    )
    assert db.cancel_outbox("ob-1", expected_revision=1) is True
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.content == {"text": "preserved"}
    assert row.approval == {"actor": "u1"}
    assert row.result is None


def test_cancel_outbox_makes_row_non_claimable(db):
    """After cancel, ``claim_due_outbox`` never returns the row."""
    db.create_outbox(**_outbox_kwargs(mission_id=None, transaction_id=None))
    assert db.cancel_outbox("ob-1", expected_revision=1) is True
    assert db.claim_due_outbox(now=10, lease_seconds=60) == []
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "cancelled"


def test_cancel_outbox_validates_revision_boundary(db):
    """Integer validation runs before SQL — non-int / non-positive
    ``expected_revision`` is rejected."""
    db.create_outbox(**_outbox_kwargs())
    for bad in (1.0, "1", True, 0, -1):
        with pytest.raises(ValueError):
            db.cancel_outbox("ob-1", expected_revision=bad)
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "pending"


# ── Composite scenarios ───────────────────────────────────────────────


def test_unknown_terminal_status_blocks_subsequent_claim_attempt(db):
    """A row moved to ``unknown`` via ``transition_outbox`` is
    permanently non-claimable — no automatic retry is possible."""
    db.create_outbox(**_outbox_kwargs())
    claimed = db.claim_due_outbox(now=10, lease_seconds=60)[0]
    assert (
        db.transition_outbox(
            "ob-1",
            expected_status="claimed",
            next_status="unknown",
            result={"reason": "ambiguous ack"},
            claim_token=claimed.claim_token,
        )
        is True
    )
    # Even after a long time, claim never returns the row.
    assert db.claim_due_outbox(now=10_000, lease_seconds=60) == []
    # And revise / approval / cancel are all rejected.
    assert (
        db.revise_outbox(
            "ob-1",
            expected_revision=1,
            content={"text": "v2"},
            not_before=0,
        )
        is None
    )
    assert (
        db.set_outbox_approval(
            "ob-1", expected_revision=1, approval={"ok": True}
        )
        is False
    )
    assert db.cancel_outbox("ob-1", expected_revision=1) is False


def test_pending_revision_chain_then_cancel(db):
    """Revising a pending row bumps revision; cancel must CAS on the
    current revision to succeed."""
    db.create_outbox(
        **_outbox_kwargs(
            revision=1,
            mission_id=None,
            transaction_id=None,
        )
    )
    r2 = db.revise_outbox(
        "ob-1", expected_revision=1, content={"v": 2}, not_before=0
    )
    assert r2 is not None and r2.revision == 2

    # Stale cancel fails.
    assert db.cancel_outbox("ob-1", expected_revision=1) is False
    # Current-revision cancel succeeds.
    assert db.cancel_outbox("ob-1", expected_revision=2) is True
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "cancelled"
    assert row.revision == 2
    assert row.content == {"v": 2}


# ─────────────────────────────────────────────────────────────────────
# Task 2: one durable materialization API for mission + ordinary workflows
# ─────────────────────────────────────────────────────────────────────


def test_materialize_mission_is_idempotent_and_links_effect_transaction(db):
    store = MissionOutboxStore(db)
    first = store.materialize(
        execution_id="exec-mission",
        node_id="notify",
        mission_id="mission-1",
        platform="telegram",
        target="chat:1",
        content={"text": "hello"},
        requires_approval=True,
        preview={"target": "chat:1"},
    )
    again = store.materialize(
        execution_id="exec-mission",
        node_id="notify",
        mission_id="mission-1",
        platform="telegram",
        target="chat:1",
        content={"text": "hello"},
        requires_approval=True,
        preview={"target": "chat:1"},
    )

    assert first.outbox_id == again.outbox_id
    assert first.delivery_id == again.delivery_id
    assert first.status == "pending_approval"
    assert first.content_hash == again.content_hash
    assert first.preview == {"target": "chat:1"}
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox WHERE execution_id=? AND node_id=?",
        ("exec-mission", "notify"),
    ).fetchone()[0] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM effect_transactions WHERE mission_id=?",
        ("mission-1",),
    ).fetchone()[0] == 1


def test_materialize_mission_binds_preview_and_fails_closed_on_mismatch(db):
    store = MissionOutboxStore(db)
    first = store.materialize(
        execution_id="exec-preview-binding",
        node_id="notify",
        mission_id="mission-preview-binding",
        platform="telegram",
        target="chat:preview-binding",
        content={"text": "hello"},
        preview={"rendered": "old-preview", "target": "chat:preview-binding"},
    )
    outbox_before = tuple(
        db._conn.execute(
            "SELECT * FROM mission_outbox WHERE outbox_id=?", (first.outbox_id,)
        ).fetchone()
    )
    effect_before = tuple(
        db._conn.execute(
            "SELECT * FROM effect_transactions WHERE transaction_id=?",
            (first.transaction_id,),
        ).fetchone()
    )

    with pytest.raises(ValueError, match="preview"):
        store.materialize(
            execution_id="exec-preview-binding",
            node_id="notify",
            mission_id="mission-preview-binding",
            platform="telegram",
            target="chat:preview-binding",
            content={"text": "hello"},
            preview={"rendered": "new-preview", "target": "chat:preview-binding"},
        )

    assert tuple(
        db._conn.execute(
            "SELECT * FROM mission_outbox WHERE outbox_id=?", (first.outbox_id,)
        ).fetchone()
    ) == outbox_before
    assert tuple(
        db._conn.execute(
            "SELECT * FROM effect_transactions WHERE transaction_id=?",
            (first.transaction_id,),
        ).fetchone()
    ) == effect_before
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox WHERE execution_id=? AND node_id=?",
        ("exec-preview-binding", "notify"),
    ).fetchone()[0] == 1
def test_parallel_mission_materialization_allocates_unique_effect_sequences(db, monkeypatch):
    original_create = SessionDB.create_effect_transaction

    def delayed_create(self, **kwargs):
        time.sleep(0.05)
        return original_create(self, **kwargs)

    monkeypatch.setattr(SessionDB, "create_effect_transaction", delayed_create)

    def materialize(index: int):
        connection = SessionDB(db_path=db.db_path)
        try:
            return MissionOutboxStore(connection).materialize(
                execution_id=f"exec-parallel-{index}",
                node_id="notify",
                mission_id="mission-parallel",
                platform="local",
                target="benchmark-channel",
                content={"text": f"message {index}"},
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=6) as executor:
        records = list(executor.map(materialize, range(6)))

    assert len({record.outbox_id for record in records}) == 6
    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox WHERE mission_id = ?",
        ("mission-parallel",),
    ).fetchone()[0] == 6
    sequences = db._conn.execute(
        "SELECT sequence_no FROM effect_transactions WHERE mission_id = ? ORDER BY sequence_no",
        ("mission-parallel",),
    ).fetchall()
    assert [row["sequence_no"] for row in sequences] == [1, 2, 3, 4, 5, 6]


def test_materialize_ordinary_is_idempotent_without_effect_transaction(db):
    store = MissionOutboxStore(db)
    first = store.materialize(
        execution_id="exec-workflow",
        node_id="notify",
        platform="discord",
        target="channel:2",
        content={"text": "ordinary"},
    )
    again = store.materialize(
        execution_id="exec-workflow",
        node_id="notify",
        platform="discord",
        target="channel:2",
        content={"text": "ordinary"},
    )

    assert first.outbox_id == again.outbox_id
    assert first.delivery_id == again.delivery_id
    assert first.status == "scheduled"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM effect_transactions"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("invalid_status", ["", 0, False, "invalid"])
def test_materialize_rejects_explicit_invalid_status_without_defaulting(
    db, invalid_status
):
    store = MissionOutboxStore(db)
    with pytest.raises(ValueError, match="materialization status"):
        store.materialize(
            execution_id="exec-invalid-status",
            node_id="notify",
            platform="discord",
            target="channel:2",
            content={"text": "invalid"},
            requires_approval=True,
            status=invalid_status,
        )

    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox"
    ).fetchone()[0] == 0


def test_materialize_supports_storage_preview_revision_and_cancel(db):
    store = MissionOutboxStore(db)
    row = store.preview(
        execution_id="exec-preview",
        node_id="notify",
        platform="telegram",
        target="chat:3",
        content={"text": "before"},
        preview={"would_send": True},
        requires_approval=True,
    )
    assert row.status == "pending_approval"
    revised = store.revise(
        row.outbox_id,
        expected_revision=row.revision,
        content={"text": "after"},
        preview={"would_send": True, "revision": 2},
        not_before=20,
    )
    assert revised is not None
    assert revised.revision == 2
    assert revised.content == {"text": "after"}
    assert revised.not_before == 20
    assert store.cancel(revised.outbox_id, expected_revision=2) is True
    assert store.get_by_id(revised.outbox_id).status == "cancelled"


def test_terminal_result_is_redacted_and_acknowledgement_persists(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-terminal",
        node_id="notify",
        platform="telegram",
        target="chat:4",
        content={"text": "do not log"},
    )
    claimed = store.claim(now=10, owner_id="worker-1", lease_seconds=30)
    assert [item.outbox_id for item in claimed] == [row.outbox_id]
    assert store.mark_delivered(
        row.outbox_id,
        claim_token=claimed[0].claim_token,
        owner_id="worker-1",
        result={
            "message_id": "msg-1",
            "token": "super-secret",
            "content": "do not log",
        },
    ) is True
    delivered = store.get_by_id(row.outbox_id)
    assert delivered.status == "delivered"
    assert delivered.result["message_id"] == "msg-1"
    assert delivered.result["token"] == "[REDACTED]"
    assert delivered.result["content"] == "[REDACTED]"
    assert store.acknowledge(row.outbox_id) is True
    assert store.get_by_id(row.outbox_id).acknowledged_at is not None

    db_path = db.db_path
    db.close()
    reopened = SessionDB(db_path=db_path)
    try:
        recovered = MissionOutboxStore(reopened).get_by_id(row.outbox_id)
        assert recovered is not None
        assert recovered.status == "delivered"
        assert recovered.acknowledged_at is not None
        assert recovered.result["token"] == "[REDACTED]"
    finally:
        reopened.close()


def test_claim_lease_recovery_uses_owner_and_expiry_fields(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-lease",
        node_id="notify",
        platform="telegram",
        target="chat:5",
        content={"text": "lease"},
    )
    first = store.claim(now=10, owner_id="worker-1", lease_seconds=30)
    assert first[0].outbox_id == row.outbox_id
    assert first[0].lease_owner == "worker-1"
    assert first[0].lease_expires_at == 40
    assert store.claim(now=20, owner_id="worker-2", lease_seconds=30) == []
    recovered = store.claim(now=41, owner_id="worker-2", lease_seconds=30)
    assert recovered[0].lease_owner == "worker-2"
    assert recovered[0].lease_expires_at == 71


def test_task2_status_vocabulary_is_explicit(db):
    assert OUTBOX_STATUSES == frozenset(
        {"pending_approval", "scheduled", "claimed", "delivered", "cancelled", "failed", "unknown"}
    )


def test_terminal_transition_is_fenced_to_current_claim_token(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-fenced",
        node_id="notify",
        platform="telegram",
        target="chat:6",
        content={"text": "fenced"},
    )

    first = store.claim(now=10, owner_id="worker-a", lease_seconds=30)[0]
    recovered = store.claim(now=41, owner_id="worker-b", lease_seconds=30)[0]
    assert first.claim_token
    assert recovered.claim_token
    assert first.claim_token != recovered.claim_token

    assert store.mark_delivered(
        row.outbox_id,
        owner_id="worker-a",
        claim_token=first.claim_token,
        result={"worker": "a"},
    ) is False
    assert store.get_by_id(row.outbox_id).status == "claimed"
    assert store.mark_delivered(
        row.outbox_id,
        owner_id="worker-b",
        claim_token=recovered.claim_token,
        result={"worker": "b"},
    ) is True


def test_terminal_transition_requires_a_claim_token(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-token-required",
        node_id="notify",
        platform="telegram",
        target="chat:7",
        content={"text": "token"},
    )
    store.claim(now=10, owner_id="worker-a", lease_seconds=30)

    assert store.mark_failed(row.outbox_id, error="missing token") is False
    assert store.get_by_id(row.outbox_id).status == "claimed"


def test_direct_terminal_transition_is_fenced_by_current_claim_token(db):
    """The low-level SessionDB CAS must enforce fencing, not just the store."""
    db.create_outbox(**_outbox_kwargs(outbox_id="ob-direct-fence"))
    first = db.claim_due_outbox(now=10, owner_id="worker-a", lease_seconds=30)[0]
    recovered = db.claim_due_outbox(now=41, owner_id="worker-b", lease_seconds=30)[0]
    assert first.claim_token and recovered.claim_token
    assert first.claim_token != recovered.claim_token

    # A stale worker cannot finish the row, even when it calls SessionDB
    # directly and omits the fencing token entirely.
    assert db.transition_outbox(
        "ob-direct-fence",
        expected_status="claimed",
        next_status="failed",
    ) is False
    assert db.transition_outbox(
        "ob-direct-fence",
        expected_status="claimed",
        next_status="failed",
        owner_id="worker-a",
        claim_token=first.claim_token,
    ) is False

    assert db.transition_outbox(
        "ob-direct-fence",
        expected_status="claimed",
        next_status="failed",
        owner_id="worker-b",
        claim_token=recovered.claim_token,
    ) is True


def test_release_and_claimed_cancel_are_fenced(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-release-fenced",
        node_id="notify",
        platform="telegram",
        target="chat:7b",
        content={"text": "release"},
    )
    claimed = store.claim(now=10, owner_id="worker-a", lease_seconds=30)[0]
    assert store.release(
        row.outbox_id,
        owner_id="worker-b",
        claim_token=claimed.claim_token,
    ) is False
    assert store.release(
        row.outbox_id,
        owner_id="worker-a",
        claim_token=claimed.claim_token,
    ) is True
    assert store.get_by_id(row.outbox_id).status == "scheduled"

    claimed_again = store.claim(now=20, owner_id="worker-a", lease_seconds=30)[0]
    assert store.cancel(
        row.outbox_id,
        expected_revision=claimed_again.revision,
        owner_id="worker-a",
        claim_token=claimed_again.claim_token,
    ) is True
    assert store.get_by_id(row.outbox_id).status == "cancelled"


def test_mission_materialization_rolls_back_all_rows_on_outbox_failure(db, monkeypatch):
    store = MissionOutboxStore(db)

    def fail_create_outbox(**_kwargs):
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(db, "create_outbox", fail_create_outbox)
    with pytest.raises(RuntimeError, match="injected outbox failure"):
        store.materialize(
            execution_id="exec-atomic",
            node_id="notify",
            mission_id="mission-atomic",
            platform="telegram",
            target="chat:8",
            content={"text": "atomic"},
        )

    assert db._conn.execute(
        "SELECT COUNT(*) FROM mission_outbox WHERE execution_id='exec-atomic'"
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM effect_transactions WHERE mission_id='mission-atomic'"
    ).fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM agent_operations WHERE kind='mission_outbox'"
    ).fetchone()[0] == 0


def test_materialize_rejects_caller_identity_overrides(db):
    store = MissionOutboxStore(db)
    kwargs = {
        "execution_id": "exec-identity",
        "node_id": "notify",
        "platform": "telegram",
        "target": "chat:9",
        "content": {"text": "identity"},
    }
    with pytest.raises(ValueError, match="outbox_id"):
        store.materialize(**kwargs, outbox_id="caller-controlled")
    with pytest.raises(ValueError, match="delivery_id"):
        store.materialize(**kwargs, delivery_id="caller-controlled")


def test_materialize_concurrent_same_identity_returns_one_stable_row(tmp_path):
    db_path = tmp_path / "state.db"
    db_a = SessionDB(db_path=db_path)
    db_b = SessionDB(db_path=db_path)
    try:
        stores = [MissionOutboxStore(db_a), MissionOutboxStore(db_b)]

        def materialize(index):
            return stores[index % 2].materialize(
                execution_id="exec-concurrent-identity",
                node_id="notify",
                platform="telegram",
                target="chat:10",
                content={"text": "same"},
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            rows = list(executor.map(materialize, range(8)))
        assert len({row.outbox_id for row in rows}) == 1
        assert len({row.delivery_id for row in rows}) == 1
        assert db_a._conn.execute(
            "SELECT COUNT(*) FROM mission_outbox "
            "WHERE execution_id='exec-concurrent-identity' AND node_id='notify'"
        ).fetchone()[0] == 1
    finally:
        db_a.close()
        db_b.close()


def test_terminal_result_redacts_nested_credential_keys(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-nested-redaction",
        node_id="notify",
        platform="telegram",
        target="chat:11",
        content={"text": "body"},
        preview={
            "metadata": {
                "access_token": "preview-token",
                "client_secret": "preview-secret",
            }
        },
    )
    claimed = store.claim(now=10, owner_id="worker-a", lease_seconds=30)[0]
    assert store.mark_delivered(
        row.outbox_id,
        owner_id="worker-a",
        claim_token=claimed.claim_token,
        result={
            "receipt": {
                "access_token": "result-token",
                "client_secret": "result-secret",
                "message_id": "msg-11",
            },
            "items": [{"api_key": "nested-api-key"}],
        },
    ) is True
    stored = store.get_by_id(row.outbox_id)
    assert stored.result["receipt"]["access_token"] == "[REDACTED]"
    assert stored.result["receipt"]["client_secret"] == "[REDACTED]"
    assert stored.result["items"][0]["api_key"] == "[REDACTED]"
    assert stored.preview["metadata"]["access_token"] == "[REDACTED]"
    raw = db._conn.execute(
        "SELECT preview_json, result_json FROM mission_outbox WHERE outbox_id=?",
        (row.outbox_id,),
    ).fetchone()
    assert "preview-token" not in raw[0]
    assert "preview-secret" not in raw[0]
    assert "result-token" not in raw[1]
    assert "result-secret" not in raw[1]
    assert "nested-api-key" not in raw[1]


def test_terminal_result_redacts_nested_raw_output_metadata(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-raw-output-redaction",
        node_id="notify",
        platform="telegram",
        target="chat:13",
        content={"text": "body"},
    )
    claimed = store.claim(now=10, owner_id="worker-a", lease_seconds=30)[0]
    result = {
        "outer": {
            "stdout": "raw stdout value",
            "nested": [
                {"stderr": "raw stderr value"},
                {"details": {"error": "raw error value"}},
            ],
        }
    }
    assert store.mark_delivered(
        row.outbox_id,
        owner_id="worker-a",
        claim_token=claimed.claim_token,
        result=result,
    ) is True

    stored = store.get_by_id(row.outbox_id)
    assert stored is not None
    assert stored.result["outer"]["stdout"] == "[REDACTED]"
    assert stored.result["outer"]["nested"][0]["stderr"] == "[REDACTED]"
    assert (
        stored.result["outer"]["nested"][1]["details"]["error"]
        == "[REDACTED]"
    )
    serialized = json.dumps(stored.result, sort_keys=True)
    for raw_value in (
        "raw stdout value",
        "raw stderr value",
        "raw error value",
    ):
        assert raw_value not in serialized

    persisted = db._conn.execute(
        "SELECT result_json FROM mission_outbox WHERE outbox_id=?",
        (row.outbox_id,),
    ).fetchone()[0]
    for raw_value in (
        "raw stdout value",
        "raw stderr value",
        "raw error value",
    ):
        assert raw_value not in persisted


def test_concurrent_mission_materialization_allocates_contiguous_sequences(tmp_path):
    db_path = tmp_path / "state.db"
    db_a = SessionDB(db_path=db_path)
    db_b = SessionDB(db_path=db_path)
    try:
        stores = [MissionOutboxStore(db_a), MissionOutboxStore(db_b)]

        def materialize(index):
            return stores[index % 2].materialize(
                execution_id=f"exec-sequence-{index}",
                node_id="notify",
                mission_id="mission-sequence",
                platform="telegram",
                target=f"chat:{index}",
                content={"text": str(index)},
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(materialize, range(8)))
        sequences = [
            row[0]
            for row in db_a._conn.execute(
                "SELECT sequence_no FROM effect_transactions "
                "WHERE mission_id='mission-sequence' ORDER BY sequence_no"
            ).fetchall()
        ]
        assert sequences == list(range(1, 9))
    finally:
        db_a.close()
        db_b.close()


def test_materialize_backfills_empty_legacy_content_hash_without_overwriting(db):
    store = MissionOutboxStore(db)
    row = store.materialize(
        execution_id="exec-legacy-hash",
        node_id="notify",
        platform="telegram",
        target="chat:12",
        content={"text": "legacy"},
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_hash='' WHERE outbox_id=?",
            (row.outbox_id,),
        )
    )
    backfilled = store.materialize(
        execution_id="exec-legacy-hash",
        node_id="notify",
        platform="telegram",
        target="chat:12",
        content={"text": "legacy"},
    )
    assert backfilled.outbox_id == row.outbox_id
    assert backfilled.content_hash
    assert backfilled.content_hash == row.content_hash

    sentinel = "legacy-nonempty-hash"
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_hash=? WHERE outbox_id=?",
            (sentinel, row.outbox_id),
        )
    )
    with pytest.raises(ValueError, match="prepared semantics"):
        store.materialize(
            execution_id="exec-legacy-hash",
            node_id="notify",
            platform="telegram",
            target="chat:12",
            content={"text": "legacy"},
        )
    preserved = db.get_outbox_by_id(row.outbox_id)
    assert preserved is not None
    assert preserved.content_hash == sentinel


def test_reopen_backfills_legacy_empty_content_hash_without_materialize(tmp_path):
    db_path = tmp_path / "legacy-reopen.db"
    first = SessionDB(db_path=db_path)
    created = first.create_outbox(
        execution_id="exec-reopen-legacy",
        node_id="notify",
        transaction_id=None,
        platform="telegram",
        target="chat:14",
        content={"text": "legacy reopen"},
        preview={"content_preview": "safe preview"},
    )
    first._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_hash='' "
            "WHERE outbox_id=?",
            (created.outbox_id,),
        )
    )
    first.close()

    reopened = SessionDB(db_path=db_path)
    try:
        row = reopened.get_outbox_by_id(created.outbox_id)
        assert row is not None
        assert row.content_hash
        persisted = reopened._conn.execute(
            "SELECT content_hash FROM mission_outbox "
            "WHERE outbox_id=?",
            (created.outbox_id,),
        ).fetchone()[0]
        assert persisted == row.content_hash
    finally:
        reopened.close()


def test_noncanonical_content_hash_matches_materialize_first_and_reopen_first(
    tmp_path,
):
    canonical_json = '{"alpha":1,"beta":2}'
    noncanonical_json = '{ "beta": 2, "alpha": 1 }'
    expected_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def seed(path, execution_id):
        seeded = SessionDB(db_path=path)
        outbox_id, _delivery_id = seeded.derive_outbox_ids(execution_id, "notify")
        seeded.create_outbox(
            execution_id=execution_id,
            node_id="notify",
            mission_id="mission-canonical-content",
            transaction_id=f"{outbox_id}:transaction",
            platform="telegram",
            target="chat:canonical",
            content={"alpha": 1, "beta": 2},
        )
        seeded._execute_write(
            lambda conn: conn.execute(
                "UPDATE mission_outbox SET content_json=?, content_hash='' "
                "WHERE outbox_id=?",
                (noncanonical_json, outbox_id),
            )
        )
        return seeded, outbox_id

    materialize_first, first_id = seed(
        tmp_path / "materialize-first.db", "exec-materialize-first"
    )
    try:
        materialized = MissionOutboxStore(materialize_first).materialize(
            execution_id="exec-materialize-first",
            node_id="notify",
            mission_id="mission-canonical-content",
            platform="telegram",
            target="chat:canonical",
            content={"beta": 2, "alpha": 1},
        )
        assert materialized.content_hash == expected_hash
        assert materialized.content == {"alpha": 1, "beta": 2}
        assert materialize_first._conn.execute(
            "SELECT content_json FROM mission_outbox WHERE outbox_id=?",
            (first_id,),
        ).fetchone()[0] == canonical_json
        effect = materialize_first.get_effect_transaction(materialized.transaction_id)
        assert effect is not None
        assert effect.prepared["content_hash"] == expected_hash
    finally:
        materialize_first.close()

    reopen_first, second_id = seed(
        tmp_path / "reopen-first.db", "exec-reopen-first"
    )
    reopen_first.close()
    reopened = SessionDB(db_path=tmp_path / "reopen-first.db")
    try:
        assert tuple(
            reopened._conn.execute(
                "SELECT content_json, content_hash FROM mission_outbox WHERE outbox_id=?",
                (second_id,),
            ).fetchone()
        ) == (canonical_json, expected_hash)
        materialized = MissionOutboxStore(reopened).materialize(
            execution_id="exec-reopen-first",
            node_id="notify",
            mission_id="mission-canonical-content",
            platform="telegram",
            target="chat:canonical",
            content={"alpha": 1, "beta": 2},
        )
        assert materialized.content_hash == expected_hash
        effect = reopened.get_effect_transaction(materialized.transaction_id)
        assert effect is not None
        assert effect.prepared["content_hash"] == expected_hash
    finally:
        reopened.close()


def test_reopen_preserves_nonempty_legacy_content_hash_sentinel(tmp_path):
    db_path = tmp_path / "legacy-sentinel-reopen.db"
    first = SessionDB(db_path=db_path)
    created = first.create_outbox(
        execution_id="exec-sentinel-reopen",
        node_id="notify",
        platform="telegram",
        target="chat:15",
        content={"text": "legacy sentinel"},
    )
    sentinel = "legacy-nonempty-sentinel"
    first._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_hash=? WHERE outbox_id=?",
            (sentinel, created.outbox_id),
        )
    )
    first.close()

    reopened = SessionDB(db_path=db_path)
    try:
        assert reopened._conn is not None
        row = reopened.get_outbox_by_id(created.outbox_id)
        assert row is not None
        assert row.content_hash == sentinel
        assert reopened._conn.execute(
            "SELECT content_hash FROM mission_outbox WHERE outbox_id=?",
            (created.outbox_id,),
        ).fetchone()[0] == sentinel
    finally:
        reopened.close()


def test_materialize_rejects_caller_sequence_number_even_when_none(db):
    store = MissionOutboxStore(db)
    kwargs = {
        "execution_id": "exec-sequence-boundary",
        "node_id": "notify",
        "mission_id": "mission-sequence-boundary",
        "platform": "telegram",
        "target": "chat:15",
        "content": {"text": "allocator"},
    }
    with pytest.raises(TypeError, match="sequence_no"):
        store.materialize(**kwargs, sequence_no=9)
    with pytest.raises(TypeError, match="sequence_no"):
        store.materialize(**kwargs, sequence_no=None)


def _legacy_insert_outbox_row(conn, *, outbox_id, delivery_id, execution_id, node_id,
                               status, revision, transaction_id=None,
                               mission_id: str | None = None,
                               acknowledged_at=None,
                               created_at=1, updated_at=1, content=None):
    payload = json.dumps(content or {"text": outbox_id}, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """INSERT INTO mission_outbox (
               outbox_id, mission_id, execution_id, node_id, transaction_id,
               delivery_id, platform, target, content_json, content_hash,
               preview_json, not_before, status, revision, approval_json,
               result_json, lease_owner, lease_expires_at, claim_token,
               created_at, updated_at, acknowledged_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (outbox_id, mission_id, execution_id, node_id, transaction_id,
         delivery_id, "telegram", "chat:legacy", payload, "", None, 0,
         status, revision, None, None, None, None, None, created_at,
         updated_at, acknowledged_at),
    )


def _legacy_insert_operation_and_effect(conn, *, operation_id, transaction_id,
                                         mission_id, execution_id, node_id,
                                         sequence_no, phase):
    conn.execute(
        """INSERT INTO agent_operations (
               operation_id, kind, session_id, turn_id, tool_call_id,
               destination, payload_hash, state, effect_disposition,
               result_json, error, created_at, updated_at, acknowledged_at
           ) VALUES (?, 'mission_outbox', '', '', '', 'outbox:telegram', '',
                     'pending', 'none', NULL, NULL, 1, 1, NULL)""",
        (operation_id,),
    )
    conn.execute(
        """INSERT INTO effect_transactions (
               transaction_id, operation_id, mission_id, execution_id, step_id,
               adapter_id, sequence_no, semantics_json, phase, depends_on_json,
               prepared_json, preview_json, authority_json, result_json,
               verification_json, compensation_json, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, 'outbox.telegram', ?, ?, ?, '[]',
                     NULL, NULL, NULL, NULL, NULL, NULL, 1, 1)""",
        (transaction_id, operation_id, mission_id, execution_id, node_id,
         sequence_no, '{"kind":"outbound_delivery"}', phase),
    )


def test_reopen_fails_closed_for_duplicate_identity_with_conflicting_links(tmp_path):
    db_path = tmp_path / "legacy-duplicate.db"
    first = SessionDB(db_path=db_path)
    first.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_mission_outbox_identity")
        _legacy_insert_operation_and_effect(
            conn, operation_id="op-old", transaction_id="tx-old",
            mission_id="mission-legacy", execution_id="exec-legacy",
            node_id="notify", sequence_no=1, phase="pending",
        )
        _legacy_insert_operation_and_effect(
            conn, operation_id="op-new", transaction_id="tx-new",
            mission_id="mission-legacy", execution_id="exec-legacy",
            node_id="notify", sequence_no=2, phase="committed",
        )
        _legacy_insert_outbox_row(
            conn, outbox_id="ob-old", delivery_id="dl-old",
            execution_id="exec-legacy", node_id="notify", status="pending",
            revision=1, transaction_id="tx-old", mission_id="mission-legacy",
            created_at=10, updated_at=10,
        )
        _legacy_insert_outbox_row(
            conn, outbox_id="ob-new", delivery_id="dl-new",
            execution_id="exec-legacy", node_id="notify", status="sent",
            revision=2, transaction_id="tx-new", mission_id="mission-legacy",
            acknowledged_at=20,
            created_at=20, updated_at=20,
        )
        conn.commit()
    finally:
        conn.close()

    before = sqlite3.connect(db_path)
    try:
        outbox_before = before.execute(
            "SELECT outbox_id, mission_id, transaction_id, status, acknowledged_at "
            "FROM mission_outbox WHERE execution_id='exec-legacy' ORDER BY outbox_id"
        ).fetchall()
        effects_before = before.execute(
            "SELECT transaction_id, operation_id, phase FROM effect_transactions "
            "WHERE transaction_id IN ('tx-old', 'tx-new') ORDER BY transaction_id"
        ).fetchall()
    finally:
        before.close()

    with pytest.raises(OutboxMigrationError, match="reconciliation required"):
        SessionDB(db_path=db_path)

    check = sqlite3.connect(db_path)
    try:
        assert check.execute(
            "SELECT outbox_id, mission_id, transaction_id, status, acknowledged_at "
            "FROM mission_outbox WHERE execution_id='exec-legacy' ORDER BY outbox_id"
        ).fetchall() == outbox_before
        assert check.execute(
            "SELECT transaction_id, operation_id, phase FROM effect_transactions "
            "WHERE transaction_id IN ('tx-old', 'tx-new') ORDER BY transaction_id"
        ).fetchall() == effects_before
    finally:
        check.close()


@pytest.mark.parametrize(
    "conflict",
    [
        "transaction",
        "platform",
        "target",
        "content",
        "preview",
        "approval",
        "result",
        "status",
        "ack",
    ],
)
def test_reopen_fails_closed_for_semantic_duplicate_conflict_and_preserves_rows(
    tmp_path, conflict
):
    """Duplicate logical identities are safe to merge only when semantically equal."""
    db_path = tmp_path / f"legacy-duplicate-{conflict}.db"
    first = SessionDB(db_path=db_path)
    first.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_mission_outbox_identity")
        if conflict == "transaction":
            first_context = ("mission-conflict", "tx-a")
            second_context = ("mission-conflict", "tx-b")
        else:
            first_context = (None, None)
            second_context = (None, None)
        _legacy_insert_outbox_row(
            conn,
            outbox_id="ob-conflict-a",
            delivery_id="dl-conflict-a",
            execution_id="exec-conflict",
            node_id="notify",
            status="pending",
            revision=1,
            mission_id=first_context[0],
            transaction_id=first_context[1],
            created_at=10,
            updated_at=10,
            content={"text": "same"},
        )
        _legacy_insert_outbox_row(
            conn,
            outbox_id="ob-conflict-b",
            delivery_id="dl-conflict-b",
            execution_id="exec-conflict",
            node_id="notify",
            status="pending",
            revision=1,
            mission_id=second_context[0],
            transaction_id=second_context[1],
            created_at=20,
            updated_at=20,
            content={"text": "same"},
        )
        updates = {
            "platform": "UPDATE mission_outbox SET platform='discord' WHERE outbox_id='ob-conflict-b'",
            "target": "UPDATE mission_outbox SET target='chat:different' WHERE outbox_id='ob-conflict-b'",
            "content": "UPDATE mission_outbox SET content_json='\"different\"' WHERE outbox_id='ob-conflict-b'",
            "preview": "UPDATE mission_outbox SET preview_json='{\"summary\":\"different\"}' WHERE outbox_id='ob-conflict-b'",
            "approval": "UPDATE mission_outbox SET approval_json='{\"actor\":\"different\"}' WHERE outbox_id='ob-conflict-b'",
            "result": "UPDATE mission_outbox SET result_json='{\"status\":\"different\"}' WHERE outbox_id='ob-conflict-b'",
            "status": "UPDATE mission_outbox SET status='sent' WHERE outbox_id='ob-conflict-b'",
            "ack": "UPDATE mission_outbox SET acknowledged_at=99 WHERE outbox_id='ob-conflict-b'",
        }
        if conflict != "transaction":
            conn.execute(updates[conflict])
        conn.commit()
    finally:
        conn.close()

    before_conn = sqlite3.connect(db_path)
    try:
        before = before_conn.execute(
            "SELECT outbox_id, mission_id, transaction_id, platform, target, "
            "content_json, preview_json, status, acknowledged_at "
            "FROM mission_outbox WHERE execution_id='exec-conflict' ORDER BY outbox_id"
        ).fetchall()
    finally:
        before_conn.close()

    with pytest.raises(OutboxMigrationError, match="reconciliation required"):
        SessionDB(db_path=db_path)

    check = sqlite3.connect(db_path)
    try:
        after = check.execute(
            "SELECT outbox_id, mission_id, transaction_id, platform, target, "
            "content_json, preview_json, status, acknowledged_at "
            "FROM mission_outbox WHERE execution_id='exec-conflict' ORDER BY outbox_id"
        ).fetchall()
        assert after == before
    finally:
        check.close()


def test_reopen_dedupes_only_semantically_equal_legacy_rows(tmp_path):
    db_path = tmp_path / "legacy-semantic-equal.db"
    first = SessionDB(db_path=db_path)
    first.close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_mission_outbox_identity")
        for suffix in ("a", "b"):
            _legacy_insert_outbox_row(
                conn,
                outbox_id=f"ob-equal-{suffix}",
                delivery_id=f"dl-equal-{suffix}",
                execution_id="exec-equal",
                node_id="notify",
                status="pending",
                revision=1,
                mission_id=None,
                transaction_id=None,
                created_at=10,
                updated_at=10,
                content={"text": "same"},
            )
        conn.commit()
    finally:
        conn.close()

    reopened = SessionDB(db_path=db_path)
    try:
        rows = reopened._conn.execute(
            "SELECT outbox_id FROM mission_outbox "
            "WHERE execution_id='exec-equal' AND node_id='notify'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("payload_column", "record_attribute"),
    [
        ("preview_json", "preview"),
        ("approval_json", "approval"),
        ("result_json", "result"),
    ],
)
def test_reopen_dedupes_legacy_sql_null_and_json_null_payloads(
    tmp_path, payload_column, record_attribute
):
    db_path = tmp_path / f"legacy-null-equivalence-{record_attribute}.db"
    first = SessionDB(db_path=db_path)
    first.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_mission_outbox_identity")
        for suffix in ("a", "b"):
            _legacy_insert_outbox_row(
                conn,
                outbox_id=f"ob-null-{suffix}",
                delivery_id=f"dl-null-{suffix}",
                execution_id="exec-null-equivalence",
                node_id="notify",
                status="pending",
                revision=1,
                mission_id=None,
                transaction_id=None,
                created_at=10,
                updated_at=10,
                content={"text": "same"},
            )
        conn.execute(
            f"UPDATE mission_outbox SET {payload_column}='null' "
            "WHERE outbox_id='ob-null-b'"
        )
        conn.commit()
    finally:
        conn.close()

    reopened = SessionDB(db_path=db_path)
    try:
        rows = reopened._conn.execute(
            "SELECT outbox_id FROM mission_outbox "
            "WHERE execution_id='exec-null-equivalence' AND node_id='notify'"
        ).fetchall()
        assert len(rows) == 1
        record = reopened.get_outbox_by_identity("exec-null-equivalence", "notify")
        assert record is not None
        assert getattr(record, record_attribute) is None
    finally:
        reopened.close()


def test_reopen_fails_closed_for_mixed_context_duplicate_and_preserves_rows(
    tmp_path, monkeypatch
):
    """Ordinary and mission rows sharing an identity need manual reconciliation."""
    db_path = tmp_path / "legacy-mixed-context.db"
    first = SessionDB(db_path=db_path)
    first.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_mission_outbox_identity")
        _legacy_insert_operation_and_effect(
            conn,
            operation_id="op-mixed-mission",
            transaction_id="tx-mixed-mission",
            mission_id="mission-mixed",
            execution_id="exec-mixed",
            node_id="notify",
            sequence_no=1,
            phase="pending",
        )
        _legacy_insert_outbox_row(
            conn,
            outbox_id="ob-mixed-ordinary",
            delivery_id="dl-mixed-ordinary",
            execution_id="exec-mixed",
            node_id="notify",
            status="pending",
            revision=1,
            mission_id=None,
            transaction_id=None,
            created_at=10,
            updated_at=10,
        )
        _legacy_insert_outbox_row(
            conn,
            outbox_id="ob-mixed-mission",
            delivery_id="dl-mixed-mission",
            execution_id="exec-mixed",
            node_id="notify",
            status="sent",
            revision=2,
            mission_id="mission-mixed",
            transaction_id="tx-mixed-mission",
            acknowledged_at=20,
            created_at=20,
            updated_at=20,
        )
        conn.commit()
    finally:
        conn.close()

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("legacy mutation ran before migration preflight")

    monkeypatch.setattr(
        SessionDB,
        "_sanitize_legacy_outbox_metadata",
        classmethod(fail_if_called),
    )
    monkeypatch.setattr(
        SessionDB,
        "_backfill_legacy_outbox_content_hashes",
        classmethod(fail_if_called),
    )

    with pytest.raises(sqlite3.IntegrityError, match="reconciliation required"):
        SessionDB(db_path=db_path)

    check = sqlite3.connect(db_path)
    try:
        rows = check.execute(
            "SELECT outbox_id, mission_id, transaction_id, status "
            "FROM mission_outbox WHERE execution_id=? AND node_id=? "
            "ORDER BY outbox_id",
            ("exec-mixed", "notify"),
        ).fetchall()
        assert rows == [
            ("ob-mixed-mission", "mission-mixed", "tx-mixed-mission", "sent"),
            ("ob-mixed-ordinary", None, None, "pending"),
        ]
        assert check.execute(
            "SELECT COUNT(*) FROM effect_transactions "
            "WHERE transaction_id='tx-mixed-mission'"
        ).fetchone()[0] == 1
        assert check.execute(
            "SELECT COUNT(*) FROM agent_operations "
            "WHERE operation_id='op-mixed-mission'"
        ).fetchone()[0] == 1
    finally:
        check.close()


@pytest.mark.parametrize(
    ("mission_id", "transaction_id"),
    [("mission-partial", None), (None, "transaction-partial")],
)
def test_reopen_rejects_partial_legacy_mission_context_with_rollback(
    tmp_path, mission_id, transaction_id
):
    db_path = tmp_path / f"partial-context-{mission_id or transaction_id}.db"
    first = SessionDB(db_path=db_path)
    first.create_outbox(
        execution_id="exec-partial-context",
        node_id="notify",
        platform="telegram",
        target="chat:partial",
        content={"text": "partial"},
    )
    first._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET mission_id=?, transaction_id=? "
            "WHERE execution_id=? AND node_id=?",
            (mission_id, transaction_id, "exec-partial-context", "notify"),
        )
    )
    first.close()

    with pytest.raises(OutboxMigrationError, match="reconciliation required"):
        SessionDB(db_path=db_path)

    check = sqlite3.connect(db_path)
    try:
        row = check.execute(
            "SELECT mission_id, transaction_id, status FROM mission_outbox "
            "WHERE execution_id=? AND node_id=?",
            ("exec-partial-context", "notify"),
        ).fetchone()
        assert row == (mission_id, transaction_id, "pending")
    finally:
        check.close()


def test_reopen_preflights_invalid_context_before_any_legacy_mutation(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-invalid-context-preflight.db"
    first = SessionDB(db_path=db_path)
    first.close()

    conn = sqlite3.connect(db_path)
    try:
        _legacy_insert_outbox_row(
            conn,
            outbox_id="ob-preflight",
            delivery_id="dl-preflight",
            execution_id="exec-preflight",
            node_id="notify",
            status="pending",
            revision=1,
            mission_id="mission-preflight",
            transaction_id=None,
        )
        raw_preview = json.dumps({"stdout_data": "legacy-raw"})
        conn.execute(
            "UPDATE mission_outbox SET preview_json=?, content_hash='' "
            "WHERE outbox_id='ob-preflight'",
            (raw_preview,),
        )
        conn.commit()
    finally:
        conn.close()

    before_conn = sqlite3.connect(db_path)
    try:
        before = tuple(
            before_conn.execute(
                "SELECT * FROM mission_outbox WHERE outbox_id='ob-preflight'"
            ).fetchone()
        )
    finally:
        before_conn.close()

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("legacy mutation ran before migration preflight")

    monkeypatch.setattr(
        SessionDB,
        "_sanitize_legacy_outbox_metadata",
        classmethod(fail_if_called),
    )
    monkeypatch.setattr(
        SessionDB,
        "_backfill_legacy_outbox_content_hashes",
        classmethod(fail_if_called),
    )

    with pytest.raises(OutboxMigrationError, match="reconciliation required"):
        SessionDB(db_path=db_path)

    check = sqlite3.connect(db_path)
    try:
        after = tuple(
            check.execute(
                "SELECT * FROM mission_outbox WHERE outbox_id='ob-preflight'"
            ).fetchone()
        )
        assert after == before
    finally:
        check.close()


def test_reopen_rolls_back_legacy_repairs_when_migration_reconcile_fails(
    tmp_path, monkeypatch
):
    """A failed migration leaves every legacy row byte-for-byte unchanged."""
    db_path = tmp_path / "legacy-migration-rollback.db"
    first = SessionDB(db_path=db_path)
    assert first._conn is not None
    conn = first._conn
    _legacy_insert_outbox_row(
        conn,
        outbox_id="ob-rollback",
        delivery_id="dl-rollback",
        execution_id="exec-rollback",
        node_id="notify",
        status="pending",
        revision=1,
        mission_id=None,
        transaction_id=None,
    )
    conn.execute(
        "UPDATE mission_outbox SET preview_json=?, content_hash='' "
        "WHERE outbox_id='ob-rollback'",
        (json.dumps({"stdout_data": "legacy-raw"}),),
    )
    conn.commit()
    first.close()

    def fail_reconcile(cls, cursor):
        cursor.execute(
            "UPDATE mission_outbox SET status='delivered' "
            "WHERE outbox_id='ob-rollback'"
        )
        raise sqlite3.IntegrityError("injected reconcile failure")

    monkeypatch.setattr(
        SessionDB,
        "_migrate_legacy_outbox_rows",
        classmethod(fail_reconcile),
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected reconcile failure"):
        SessionDB(db_path=db_path)

    check = sqlite3.connect(db_path)
    try:
        row = check.execute(
            "SELECT status, content_hash, preview_json FROM mission_outbox "
            "WHERE outbox_id='ob-rollback'"
        ).fetchone()
        assert row == ("pending", "", json.dumps({"stdout_data": "legacy-raw"}))
    finally:
        check.close()


def test_reopen_maps_legacy_outbox_statuses_to_supported_vocabulary(tmp_path):
    db_path = tmp_path / "legacy-status.db"
    first = SessionDB(db_path=db_path)
    first.close()
    conn = sqlite3.connect(db_path)
    try:
        _legacy_insert_outbox_row(
            conn, outbox_id="ob-pending", delivery_id="dl-pending",
            execution_id="exec-pending", node_id="notify", status="pending",
            revision=1,
        )
        _legacy_insert_outbox_row(
            conn, outbox_id="ob-sent", delivery_id="dl-sent",
            execution_id="exec-sent", node_id="notify", status="sent",
            revision=1,
        )
        conn.commit()
    finally:
        conn.close()

    reopened = SessionDB(db_path=db_path)
    try:
        statuses = [row[0] for row in reopened._conn.execute(
            "SELECT status FROM mission_outbox ORDER BY outbox_id"
        ).fetchall()]
        assert statuses == ["scheduled", "delivered"]
        assert set(statuses) <= OUTBOX_STATUSES
    finally:
        reopened.close()


def test_direct_sessiondb_outbox_redacts_preview_approval_and_result_before_write(db):
    raw_values = {
        "access": "access-secret",
        "client": "client-secret",
        "stdout": "stdout-secret",
        "stderr": "stderr-secret",
        "error": "error-secret",
    }
    db.create_outbox(
        **_outbox_kwargs(
            preview={"nested": {"access_token": raw_values["access"]}},
            approval={"client_secret": raw_values["client"]},
            result={
                "items": [{"stdout_text": raw_values["stdout"]}],
                "stderr": raw_values["stderr"],
                "error_message": raw_values["error"],
            },
        )
    )
    stored = db._conn.execute(
        "SELECT preview_json, approval_json, result_json FROM mission_outbox "
        "WHERE outbox_id='ob-1'"
    ).fetchone()
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    serialized = json.dumps([dict(stored), row.preview, row.approval, row.result])
    for value in raw_values.values():
        assert value not in serialized
    assert row.preview["nested"]["access_token"] == "[REDACTED]"
    assert row.approval["client_secret"] == "[REDACTED]"
    assert row.result["items"][0]["stdout_text"] == "[REDACTED]"
    assert row.result["stderr"] == "[REDACTED]"
    assert row.result["error_message"] == "[REDACTED]"


def test_direct_sessiondb_redacts_variant_raw_output_keys_at_write(db):
    raw_values = {
        "stdout": "variant-stdout-secret",
        "stderr": "variant-stderr-secret",
        "error": "variant-error-secret",
        "traceback": "variant-traceback-secret",
        "exception": "variant-exception-secret",
    }
    db.create_outbox(
        **_outbox_kwargs(
            preview={"items": [{"stdout_data": raw_values["stdout"]}]},
            approval={"stderr_output": raw_values["stderr"]},
            result={
                "nested": [
                    {
                        "error_details": raw_values["error"],
                        "traceback_text": raw_values["traceback"],
                    },
                    {"exception_info": raw_values["exception"]},
                ]
            },
        )
    )
    persisted = db._conn.execute(
        "SELECT preview_json, approval_json, result_json FROM mission_outbox "
        "WHERE outbox_id='ob-1'"
    ).fetchone()
    serialized = json.dumps(tuple(persisted))
    for raw_value in raw_values.values():
        assert raw_value not in serialized

    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.preview["items"][0]["stdout_data"] == "[REDACTED]"
    assert row.approval["stderr_output"] == "[REDACTED]"
    assert row.result["nested"][0]["error_details"] == "[REDACTED]"
    assert row.result["nested"][0]["traceback_text"] == "[REDACTED]"
    assert row.result["nested"][1]["exception_info"] == "[REDACTED]"


def test_reopen_sanitizes_legacy_outbox_metadata_at_rest(tmp_path):
    db_path = tmp_path / "legacy-raw-metadata.db"
    first = SessionDB(db_path=db_path)
    row = first.create_outbox(
        **_outbox_kwargs(
            preview={"safe": "preview"},
            approval={"safe": "approval"},
            result={"safe": "result"},
        )
    )
    raw_values = {
        "preview": "legacy-preview-token",
        "approval": "legacy-approval-secret",
        "result": "legacy-traceback-output",
    }
    first._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET preview_json=?, approval_json=?, result_json=? "
            "WHERE outbox_id=?",
            (
                json.dumps({"stdout_data": raw_values["preview"]}),
                json.dumps({"client_secret": raw_values["approval"]}),
                json.dumps({"traceback_text": raw_values["result"]}),
                row.outbox_id,
            ),
        )
    )
    first.close()

    reopened = SessionDB(db_path=db_path)
    try:
        persisted = reopened._conn.execute(
            "SELECT preview_json, approval_json, result_json FROM mission_outbox "
            "WHERE outbox_id=?",
            (row.outbox_id,),
        ).fetchone()
        serialized = json.dumps(tuple(persisted))
        for raw_value in raw_values.values():
            assert raw_value not in serialized
        assert json.loads(persisted[0]) == {"stdout_data": "[REDACTED]"}
        assert json.loads(persisted[1]) == {"client_secret": "[REDACTED]"}
        assert json.loads(persisted[2]) == {"traceback_text": "[REDACTED]"}
    finally:
        reopened.close()


def test_direct_sessiondb_transition_and_revision_redact_raw_output(db):
    db.create_outbox(**_outbox_kwargs())
    claimed = db.claim_due_outbox(now=10, lease_seconds=30)[0]
    assert db.transition_outbox(
        "ob-1", expected_status="claimed", next_status="failed",
        claim_token=claimed.claim_token,
        result={"nested": {"stdout": "raw-out", "error": "raw-error"}},
    ) is True
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.result == {"nested": {"stdout": "[REDACTED]", "error": "[REDACTED]"}}

    db.create_outbox(**_outbox_kwargs(
        outbox_id="ob-revise", delivery_id="dl-revise",
        execution_id="ex-revise", mission_id=None, transaction_id=None,
    ))
    revised = db.revise_outbox(
        "ob-revise", expected_revision=1, content={"text": "v2"},
        not_before=0, preview={"stderr_text": "raw-preview"},
    )
    assert revised is not None
    assert revised.preview == {"stderr_text": "[REDACTED]"}
    assert "raw-preview" not in db._conn.execute(
        "SELECT preview_json FROM mission_outbox WHERE outbox_id='ob-revise'"
    ).fetchone()[0]


def test_direct_sessiondb_acknowledgement_redacts_legacy_result_before_write(db):
    db.create_outbox(**_outbox_kwargs())
    claimed = db.claim_due_outbox(now=10, lease_seconds=30)[0]
    assert db.transition_outbox(
        "ob-1", expected_status="claimed", next_status="failed",
        claim_token=claimed.claim_token, result={"ok": False},
    ) is True
    # Simulate a pre-boundary row that reached acknowledgement with raw
    # terminal metadata; acknowledgement is itself a storage write boundary.
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET result_json=? WHERE outbox_id='ob-1'",
            (json.dumps({"error_message": "legacy-secret", "stdout": "raw"}),),
        )
    )
    assert db.acknowledge_outbox("ob-1") is True
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.result == {
        "error_message": "[REDACTED]",
        "stdout": "[REDACTED]",
    }
    persisted = db._conn.execute(
        "SELECT result_json FROM mission_outbox WHERE outbox_id='ob-1'"
    ).fetchone()[0]
    assert "legacy-secret" not in persisted
    assert "raw" not in persisted


def test_direct_sessiondb_generic_claim_transition_allocates_fencing_fields(db):
    db.create_outbox(**_outbox_kwargs())
    assert db.transition_outbox(
        "ob-1", expected_status="pending", next_status="claimed"
    ) is True
    row = db.get_outbox_by_id("ob-1")
    assert row is not None
    assert row.status == "claimed"
    assert row.claim_token
    assert row.lease_expires_at is not None


def test_direct_sessiondb_outbox_ids_are_derived_not_caller_controlled(db):
    kwargs = {
        "execution_id": "direct-id-probe",
        "node_id": "notify",
        "platform": "telegram",
        "target": "chat:1",
        "content": {"text": "identity"},
    }
    with pytest.raises(TypeError):
        db.create_outbox(**kwargs, outbox_id="arbitrary-outbox")
    with pytest.raises(TypeError):
        db.create_outbox(**kwargs, delivery_id="arbitrary-delivery")
    assert db._conn.execute("SELECT COUNT(*) FROM mission_outbox").fetchone()[0] == 0


def test_materialize_rejects_mission_identity_mismatch_on_existing_row(db):
    store = MissionOutboxStore(db)
    kwargs = {
        "execution_id": "exec-mission-context",
        "node_id": "notify",
        "platform": "telegram",
        "target": "chat:99",
        "content": {"text": "context"},
    }
    store.materialize(**kwargs, mission_id="mission-a")
    with pytest.raises(ValueError, match="mission_id"):
        store.materialize(**kwargs)
    with pytest.raises(ValueError, match="mission_id"):
        store.materialize(**kwargs, mission_id="mission-b")


def test_revise_mission_atomically_rebinds_effect_and_operation_payload(db):
    store = MissionOutboxStore(db)
    original = store.materialize(
        execution_id="exec-revision-binding",
        node_id="notify",
        mission_id="mission-revision-binding",
        platform="telegram",
        target="chat:revision",
        content={"text": "v1"},
        preview={"summary": "preview-v1"},
    )
    assert original.transaction_id is not None
    effect_before = db.get_effect_transaction(original.transaction_id)
    operation_before = OperationJournal(db).get(f"{original.outbox_id}:operation")
    assert effect_before is not None
    assert operation_before is not None

    revised = store.revise(
        original.outbox_id,
        expected_revision=original.revision,
        content={"text": "v2"},
        preview={"summary": "preview-v2"},
    )

    assert revised is not None
    effect_after = db.get_effect_transaction(original.transaction_id)
    operation_after = OperationJournal(db).get(operation_before.operation_id)
    assert effect_after is not None
    assert operation_after is not None
    assert effect_after.prepared["content_hash"] == revised.content_hash
    assert effect_after.preview == revised.preview == {"summary": "preview-v2"}
    assert operation_after.payload_hash == revised.content_hash
    assert effect_after.operation_id == operation_after.operation_id
    assert effect_before.prepared["content_hash"] != revised.content_hash


@pytest.mark.parametrize(
    ("explicit_preview", "outbox_preview", "effect_preview"),
    [
        # Omitted preview must canonicalize the outbox's legacy JSON-null
        # spelling while accepting a linked effect stored as SQL NULL.
        (False, "null", None),
        # Explicit None must canonicalize the opposite legacy spelling pair.
        (True, None, "null"),
    ],
)
def test_revise_mission_normalizes_legacy_null_preview_after_reopen(
    tmp_path, explicit_preview, outbox_preview, effect_preview
):
    """Revision treats SQL NULL and JSON ``null`` preview storage as equal.

    The two representations are deliberately installed on opposite sides of
    the linked rows after a real close/reopen cycle, matching legacy durable
    databases.  Both the omitted-preview and explicit-``None`` API paths must
    still atomically rebind the content hash and preview identity.
    """
    db_path = tmp_path / f"revision-null-preview-{explicit_preview}.db"
    first = SessionDB(db_path=db_path)
    try:
        store = MissionOutboxStore(first)
        materialize_kwargs = {
            "execution_id": "exec-revision-null-preview",
            "node_id": "notify",
            "mission_id": "mission-revision-null-preview",
            "platform": "telegram",
            "target": "chat:null-preview",
            "content": {"text": "v1"},
        }
        if explicit_preview:
            materialize_kwargs["preview"] = None
        original = store.materialize(**materialize_kwargs)
    finally:
        first.close()

    reopened = SessionDB(db_path=db_path)
    try:
        assert original.transaction_id is not None
        # Exercise both legacy spellings: outbox SQL NULL/effect JSON null,
        # then outbox JSON null/effect SQL NULL.
        reopened._execute_write(
            lambda conn: (
                conn.execute(
                    "UPDATE mission_outbox SET preview_json=? WHERE outbox_id=?",
                    (outbox_preview, original.outbox_id),
                ),
                conn.execute(
                    "UPDATE effect_transactions SET preview_json=? WHERE transaction_id=?",
                    (effect_preview, original.transaction_id),
                ),
            )
        )

        revised_kwargs = {
            "content": {"text": "v2"},
            "expected_revision": original.revision,
        }
        if explicit_preview:
            revised_kwargs["preview"] = None
        revised = MissionOutboxStore(reopened).revise(
            original.outbox_id, **revised_kwargs
        )

        assert revised is not None
        assert revised.revision == 2
        assert revised.content == {"text": "v2"}
        assert revised.preview is None
        effect = reopened.get_effect_transaction(original.transaction_id)
        operation = OperationJournal(reopened).get(f"{original.outbox_id}:operation")
        assert effect is not None
        assert operation is not None
        assert effect.preview is None
        assert effect.prepared["content_hash"] == revised.content_hash
        assert effect.prepared["execution_id"] == revised.execution_id
        assert effect.prepared["node_id"] == revised.node_id
        assert operation.payload_hash == revised.content_hash
        assert operation.operation_id == effect.operation_id
        raw_previews = reopened._conn.execute(
            """SELECT outbox.preview_json, effect.preview_json
                 FROM mission_outbox AS outbox
                 JOIN effect_transactions AS effect
                   ON effect.transaction_id = outbox.transaction_id
                WHERE outbox.outbox_id=?""",
            (original.outbox_id,),
        ).fetchone()
        assert tuple(raw_previews) == (None, None)
    finally:
        reopened.close()


def test_revise_mission_normalizes_key_ordered_preview_after_reopen(tmp_path):
    """Equivalent durable preview mappings use one canonical CAS spelling."""
    db_path = tmp_path / "revision-preview-key-order.db"
    first = SessionDB(db_path=db_path)
    try:
        original = MissionOutboxStore(first).materialize(
            execution_id="exec-revision-preview-key-order",
            node_id="notify",
            mission_id="mission-revision-preview-key-order",
            platform="telegram",
            target="chat:key-order",
            content={"text": "v1"},
            preview={"a": 1, "b": 2},
        )
    finally:
        first.close()

    reopened = SessionDB(db_path=db_path)
    try:
        assert original.transaction_id is not None
        reopened._execute_write(
            lambda conn: (
                conn.execute(
                    "UPDATE mission_outbox SET preview_json=? WHERE outbox_id=?",
                    ('{"a":1,"b":2}', original.outbox_id),
                ),
                conn.execute(
                    "UPDATE effect_transactions SET preview_json=? WHERE transaction_id=?",
                    ('{ "b": 2, "a": 1 }', original.transaction_id),
                ),
            )
        )

        revised = MissionOutboxStore(reopened).revise(
            original.outbox_id,
            expected_revision=original.revision,
            content={"text": "v2"},
        )

        assert revised is not None
        assert revised.revision == 2
        assert revised.preview == {"a": 1, "b": 2}
        raw_previews = reopened._conn.execute(
            """SELECT outbox.preview_json, effect.preview_json
                 FROM mission_outbox AS outbox
                 JOIN effect_transactions AS effect
                   ON effect.transaction_id = outbox.transaction_id
                WHERE outbox.outbox_id=?""",
            (original.outbox_id,),
        ).fetchone()
        assert tuple(raw_previews) == ('{"a":1,"b":2}', '{"a":1,"b":2}')
    finally:
        reopened.close()


@pytest.mark.parametrize("guarded_table", ["mission_outbox", "effect_transactions"])
def test_revise_mission_preview_normalization_guard_rejects_raw_change(
    tmp_path, guarded_table
):
    """Raw changes during normalization cannot be overwritten or committed."""
    db_path = tmp_path / f"revision-preview-guard-{guarded_table}.db"
    first = SessionDB(db_path=db_path)
    try:
        original = MissionOutboxStore(first).materialize(
            execution_id=f"exec-revision-preview-guard-{guarded_table}",
            node_id="notify",
            mission_id=f"mission-revision-preview-guard-{guarded_table}",
            platform="telegram",
            target="chat:preview-guard",
            content={"text": "v1"},
            preview={"a": 1, "b": 2},
        )
    finally:
        first.close()

    reopened = SessionDB(db_path=db_path)
    try:
        assert original.transaction_id is not None
        raw_preview = '{ "b": 2, "a": 1 }'
        canonical_preview = '{"a":1,"b":2}'
        reopened._execute_write(
            lambda conn: (
                conn.execute(
                    "UPDATE mission_outbox SET preview_json=? WHERE outbox_id=?",
                    (
                        raw_preview
                        if guarded_table == "mission_outbox"
                        else canonical_preview,
                        original.outbox_id,
                    ),
                ),
                conn.execute(
                    "UPDATE effect_transactions SET preview_json=? WHERE transaction_id=?",
                    (
                        raw_preview
                        if guarded_table == "effect_transactions"
                        else canonical_preview,
                        original.transaction_id,
                    ),
                ),
                conn.execute(
                    f"""CREATE TRIGGER mutate_{guarded_table}_preview
                        BEFORE UPDATE OF preview_json ON {guarded_table}
                        WHEN OLD.preview_json = '{raw_preview}'
                         AND NEW.preview_json = '{canonical_preview}'
                        BEGIN
                            UPDATE {guarded_table}
                               SET preview_json = '{{"a":9}}'
                             WHERE rowid = OLD.rowid;
                            SELECT RAISE(IGNORE);
                        END"""
                ),
            )
        )

        operation_id = f"{original.outbox_id}:operation"

        def snapshot():
            return (
                tuple(
                    reopened._conn.execute(
                        "SELECT revision, content_json, preview_json FROM mission_outbox "
                        "WHERE outbox_id=?",
                        (original.outbox_id,),
                    ).fetchone()
                ),
                tuple(
                    reopened._conn.execute(
                        "SELECT prepared_json, preview_json FROM effect_transactions "
                        "WHERE transaction_id=?",
                        (original.transaction_id,),
                    ).fetchone()
                ),
                reopened._conn.execute(
                    "SELECT payload_hash FROM agent_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()[0],
            )

        before = snapshot()
        with pytest.raises(ValueError, match="preview CAS conflict"):
            MissionOutboxStore(reopened).revise(
                original.outbox_id,
                expected_revision=original.revision,
                content={"text": "v2"},
            )
        assert snapshot() == before
    finally:
        reopened.close()


def test_revise_mission_rejects_true_preview_mismatch_after_reopen_atomically(
    tmp_path,
):
    """A non-null preview mismatch remains a linked CAS conflict."""
    db_path = tmp_path / "revision-preview-mismatch.db"
    first = SessionDB(db_path=db_path)
    store = MissionOutboxStore(first)
    original = store.materialize(
        execution_id="exec-revision-preview-mismatch",
        node_id="notify",
        mission_id="mission-revision-preview-mismatch",
        platform="telegram",
        target="chat:preview-mismatch",
        content={"text": "v1"},
    )
    assert original.transaction_id is not None
    first.close()

    reopened = SessionDB(db_path=db_path)
    try:
        operation_id = f"{original.outbox_id}:operation"
        reopened._execute_write(
            lambda conn: conn.execute(
                "UPDATE effect_transactions SET preview_json=? WHERE transaction_id=?",
                (json.dumps({"summary": "different"}), original.transaction_id),
            )
        )
        before = (
            tuple(
                reopened._conn.execute(
                    "SELECT revision, content_json, preview_json FROM mission_outbox "
                    "WHERE outbox_id=?",
                    (original.outbox_id,),
                ).fetchone()
            ),
            tuple(
                reopened._conn.execute(
                    "SELECT prepared_json, preview_json FROM effect_transactions "
                    "WHERE transaction_id=?",
                    (original.transaction_id,),
                ).fetchone()
            ),
            reopened._conn.execute(
                "SELECT payload_hash FROM agent_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()[0],
        )

        with pytest.raises(ValueError, match="preview identity conflict"):
            MissionOutboxStore(reopened).revise(
                original.outbox_id,
                expected_revision=original.revision,
                content={"text": "v2"},
            )

        after = (
            tuple(
                reopened._conn.execute(
                    "SELECT revision, content_json, preview_json FROM mission_outbox "
                    "WHERE outbox_id=?",
                    (original.outbox_id,),
                ).fetchone()
            ),
            tuple(
                reopened._conn.execute(
                    "SELECT prepared_json, preview_json FROM effect_transactions "
                    "WHERE transaction_id=?",
                    (original.transaction_id,),
                ).fetchone()
            ),
            reopened._conn.execute(
                "SELECT payload_hash FROM agent_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()[0],
        )
        assert after == before
    finally:
        reopened.close()


def test_revise_mission_rolls_back_outbox_when_linked_effect_update_fails(
    db,
):
    store = MissionOutboxStore(db)
    original = store.materialize(
        execution_id="exec-revision-rollback",
        node_id="notify",
        mission_id="mission-revision-rollback",
        platform="telegram",
        target="chat:rollback",
        content={"text": "v1"},
        preview={"summary": "preview-v1"},
    )
    assert original.transaction_id is not None
    operation_id = f"{original.outbox_id}:operation"
    db._execute_write(
        lambda conn: conn.execute(
            """CREATE TRIGGER fail_mission_revision_effect
               BEFORE UPDATE OF prepared_json ON effect_transactions
               BEGIN SELECT RAISE(ABORT, 'injected linked update failure'); END"""
        )
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected linked update failure"):
        store.revise(
            original.outbox_id,
            expected_revision=original.revision,
            content={"text": "v2"},
            preview={"summary": "preview-v2"},
        )

    persisted = db.get_outbox_by_id(original.outbox_id)
    effect = db.get_effect_transaction(original.transaction_id)
    operation = OperationJournal(db).get(operation_id)
    assert persisted is not None
    assert effect is not None
    assert operation is not None
    assert persisted.revision == original.revision
    assert persisted.content == {"text": "v1"}
    assert persisted.preview == {"summary": "preview-v1"}
    assert effect.prepared["content_hash"] == original.content_hash
    assert effect.preview == {"summary": "preview-v1"}
    assert operation.payload_hash == original.content_hash


@pytest.mark.parametrize("mission_id", [None, "mission-recovery-identity"])
def test_materialize_existing_identity_rejects_changed_content_and_preview(
    db, mission_id
):
    store = MissionOutboxStore(db)
    kwargs = {
        "execution_id": "exec-recovery-identity",
        "node_id": "notify",
        "platform": "telegram",
        "target": "chat:recovery",
        "content": {"text": "persisted"},
        "preview": {"summary": "persisted-preview"},
    }
    created = store.materialize(**kwargs, mission_id=mission_id)

    with pytest.raises(ValueError, match="prepared semantics|preview"):
        store.materialize(**{**kwargs, "content": {"text": "caller-content"}}, mission_id=mission_id)
    with pytest.raises(ValueError, match="prepared semantics|preview"):
        store.materialize(**{**kwargs, "preview": {"summary": "caller-preview"}}, mission_id=mission_id)

    persisted = db.get_outbox_by_id(created.outbox_id)
    assert persisted is not None
    assert persisted.content == {"text": "persisted"}
    assert persisted.preview == {"summary": "persisted-preview"}


def test_materialize_missing_effect_recovers_only_from_persisted_preview(db):
    store = MissionOutboxStore(db)
    original = store.materialize(
        execution_id="exec-recovery-missing-effect",
        node_id="notify",
        mission_id="mission-recovery-missing-effect",
        platform="telegram",
        target="chat:recovery",
        content={"text": "persisted"},
        preview={"summary": "persisted-preview"},
    )
    assert original.transaction_id is not None
    db._execute_write(
        lambda conn: conn.execute(
            "DELETE FROM effect_transactions WHERE transaction_id=?",
            (original.transaction_id,),
        )
    )

    with pytest.raises(ValueError, match="prepared semantics|preview"):
        store.materialize(
            execution_id="exec-recovery-missing-effect",
            node_id="notify",
            mission_id="mission-recovery-missing-effect",
            platform="telegram",
            target="chat:recovery",
            content={"text": "persisted"},
            preview={"summary": "caller-preview"},
        )
    assert db.get_effect_transaction(original.transaction_id) is None


@pytest.mark.parametrize("terminal_state", ["confirmed", "failed", "cancelled", "unknown"])
def test_materialize_missing_effect_fails_closed_for_terminal_operation(
    db, terminal_state
):
    """A terminal journal row is evidence, never permission to recreate an effect."""
    store = MissionOutboxStore(db)
    original = store.materialize(
        execution_id=f"exec-terminal-missing-effect-{terminal_state}",
        node_id="notify",
        mission_id="mission-terminal-missing-effect",
        platform="telegram",
        target="chat:terminal",
        content={"text": "persisted"},
        preview={"summary": "persisted-preview"},
    )
    assert original.transaction_id is not None
    operation_id = f"{original.outbox_id}:operation"
    journal = OperationJournal(db)
    if terminal_state == "failed":
        journal.transition(
            operation_id,
            from_states={"pending"},
            to_state="failed",
            effect_disposition="none",
        )
    elif terminal_state == "cancelled":
        journal.transition(
            operation_id,
            from_states={"pending"},
            to_state="cancelled",
            effect_disposition="none",
        )
    elif terminal_state == "unknown":
        journal.transition(
            operation_id,
            from_states={"pending"},
            to_state="running",
            effect_disposition="none",
        )
        journal.transition(
            operation_id,
            from_states={"running"},
            to_state="unknown",
            effect_disposition="unknown",
        )
    else:
        journal.transition(
            operation_id,
            from_states={"pending"},
            to_state="running",
            effect_disposition="none",
        )
        journal.transition(
            operation_id,
            from_states={"running"},
            to_state="confirmed",
            effect_disposition="none",
        )
    db._execute_write(
        lambda conn: conn.execute(
            "DELETE FROM effect_transactions WHERE transaction_id=?",
            (original.transaction_id,),
        )
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE mission_outbox SET content_hash='' WHERE outbox_id=?",
            (original.outbox_id,),
        )
    )
    before = tuple(
        db._conn.execute(
            "SELECT * FROM mission_outbox WHERE outbox_id=?", (original.outbox_id,)
        ).fetchone()
    )

    with pytest.raises(ValueError, match="reconciliation required"):
        store.materialize(
            execution_id=original.execution_id,
            node_id=original.node_id,
            mission_id=original.mission_id,
            platform=original.platform,
            target=original.target,
            content=original.content,
            preview=original.preview,
        )

    assert db.get_effect_transaction(original.transaction_id) is None
    assert tuple(
        db._conn.execute(
            "SELECT * FROM mission_outbox WHERE outbox_id=?", (original.outbox_id,)
        ).fetchone()
    ) == before
    assert OperationJournal(db).get(operation_id).state == terminal_state


def test_reopen_replaces_malformed_legacy_metadata_with_typed_sentinels(tmp_path):
    db_path = tmp_path / "malformed-legacy-metadata.db"
    first = SessionDB(db_path=db_path)
    row = first.create_outbox(
        execution_id="exec-malformed-legacy",
        node_id="notify",
        platform="telegram",
        target="chat:malformed",
        content={"text": "payload"},
    )
    first._execute_write(
        lambda conn: conn.execute(
            """UPDATE mission_outbox
                  SET preview_json=?, approval_json=?, result_json=?
                WHERE outbox_id=?""",
            (
                "{stdout_data: raw-preview-secret}",
                "{approval_data: raw-approval-secret}",
                "{result_data: raw-result-secret}",
                row.outbox_id,
            ),
        )
    )
    first.close()

    reopened = SessionDB(db_path=db_path)
    try:
        raw = reopened._conn.execute(
            "SELECT preview_json, approval_json, result_json FROM mission_outbox "
            "WHERE outbox_id=?",
            (row.outbox_id,),
        ).fetchone()
        sentinel = {"reason": "malformed_legacy_metadata", "redacted": True}
        assert [json.loads(value) for value in raw] == [sentinel, sentinel, sentinel]
        serialized = json.dumps(tuple(raw))
        assert "raw-preview-secret" not in serialized
        assert "raw-approval-secret" not in serialized
        assert "raw-result-secret" not in serialized
    finally:
        reopened.close()


def test_reopen_holds_write_lock_through_legacy_preflight_and_repair(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-preflight-lock.db"
    first = SessionDB(db_path=db_path)
    first.close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_mission_outbox_identity")
        _legacy_insert_outbox_row(
            conn,
            outbox_id="ob-lock-original",
            delivery_id="dl-lock-original",
            execution_id="exec-lock",
            node_id="notify",
            status="pending",
            revision=1,
            mission_id=None,
            transaction_id=None,
        )
        conn.commit()
    finally:
        conn.close()

    injected_error: list[str] = []
    original_preflight = SessionDB._preflight_legacy_outbox_rows.__func__

    def preflight_then_inject(cls, cursor):
        original_preflight(cls, cursor)
        writer = sqlite3.connect(db_path, timeout=0)
        try:
            _legacy_insert_outbox_row(
                writer,
                outbox_id="ob-lock-injected",
                delivery_id="dl-lock-injected",
                execution_id="exec-lock",
                node_id="notify",
                status="sent",
                revision=2,
                mission_id="injected-mission",
                transaction_id="tx-injected",
            )
            writer.commit()
        except sqlite3.OperationalError as exc:
            injected_error.append(str(exc))
            writer.rollback()
        finally:
            writer.close()

    monkeypatch.setattr(
        SessionDB,
        "_preflight_legacy_outbox_rows",
        classmethod(preflight_then_inject),
    )
    reopened = SessionDB(db_path=db_path)
    try:
        assert injected_error and "locked" in injected_error[0].lower()
        rows = reopened._conn.execute(
            "SELECT outbox_id, mission_id FROM mission_outbox "
            "WHERE execution_id=? AND node_id=?",
            ("exec-lock", "notify"),
        ).fetchall()
        assert [tuple(row) for row in rows] == [("ob-lock-original", None)]
    finally:
        reopened.close()
