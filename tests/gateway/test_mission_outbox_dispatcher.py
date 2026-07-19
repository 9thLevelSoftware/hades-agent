"""Durable router handoff tests for mission/workflow outbox rows."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent.operation_journal import OperationJournal
from gateway.mission_delivery import MissionOutboxDispatcher
from gateway.mission_outbox import MissionOutboxStore
from hades_cli import workflows_db as wfdb
from hades_cli import missions_db as mdb
from hades_state import SessionDB


@pytest.fixture(autouse=True)
def _bind_dispatcher_to_test_profile(tmp_path, monkeypatch):
    """Make the positive dispatcher probes carry an explicit profile binding."""
    monkeypatch.setenv("HADES_HOME", str(tmp_path))


class _ConfirmedRouter:
    def __init__(self, journal: OperationJournal) -> None:
        self.journal = journal
        self.calls: list[tuple[str, str, str, dict[str, str]]] = []

    async def deliver(self, content, targets, metadata=None):
        assert len(targets) == 1
        target = targets[0]
        assert metadata is not None
        delivery_id = metadata["delivery_id"]
        self.calls.append(
            (content, target.platform.value, target.chat_id, dict(metadata))
        )
        self.journal.create(
            operation_id=delivery_id,
            kind="outbound_delivery",
            destination=f"{target.platform.value}:{target.chat_id}",
            payload_hash="confirmed-delivery",
        )
        self.journal.transition(
            delivery_id,
            from_states={"pending"},
            to_state="running",
            effect_disposition="none",
        )
        self.journal.transition(
            delivery_id,
            from_states={"running"},
            to_state="dispatched",
            effect_disposition="unknown",
        )
        self.journal.transition(
            delivery_id,
            from_states={"dispatched"},
            to_state="confirmed",
            effect_disposition="landed",
            result={"message_id": "m-7"},
        )
        return {
            target.to_string(): {
                "success": True,
                "result": {"message_id": "m-7"},
            }
        }


class _FailedRouter:
    def __init__(self, journal: OperationJournal) -> None:
        self.journal = journal
        self.calls = 0

    async def deliver(self, content, targets, metadata=None):
        self.calls += 1
        target = targets[0]
        assert metadata is not None
        delivery_id = metadata["delivery_id"]
        self.journal.create(
            operation_id=delivery_id,
            kind="outbound_delivery",
            destination=f"{target.platform.value}:{target.chat_id}",
            payload_hash="failed-delivery",
        )
        self.journal.transition(
            delivery_id,
            from_states={"pending"},
            to_state="running",
            effect_disposition="none",
        )
        self.journal.transition(
            delivery_id,
            from_states={"running"},
            to_state="failed",
            effect_disposition="none",
            error="adapter rejected",
        )
        return {target.to_string(): {"success": False, "error": "adapter rejected"}}


class _TimeoutRouter:
    def __init__(self, journal: OperationJournal) -> None:
        self.journal = journal
        self.calls = 0

    async def deliver(self, content, targets, metadata=None):
        self.calls += 1
        target = targets[0]
        assert metadata is not None
        delivery_id = metadata["delivery_id"]
        self.journal.create(
            operation_id=delivery_id,
            kind="outbound_delivery",
            destination=f"{target.platform.value}:{target.chat_id}",
            payload_hash="timeout-delivery",
        )
        self.journal.transition(
            delivery_id,
            from_states={"pending"},
            to_state="running",
            effect_disposition="none",
        )
        self.journal.transition(
            delivery_id,
            from_states={"running"},
            to_state="dispatched",
            effect_disposition="unknown",
        )
        raise TimeoutError("adapter response timed out")


class _UnknownDedupRouter:
    def __init__(self, journal: OperationJournal) -> None:
        self.journal = journal

    async def deliver(self, content, targets, metadata=None):
        target = targets[0]
        assert metadata is not None
        delivery_id = metadata["delivery_id"]
        self.journal.create(
            operation_id=delivery_id,
            kind="outbound_delivery",
            destination=f"{target.platform.value}:{target.chat_id}",
            payload_hash="unknown-dedup-delivery",
        )
        self.journal.transition(
            delivery_id,
            from_states={"pending"},
            to_state="running",
            effect_disposition="none",
        )
        self.journal.transition(
            delivery_id,
            from_states={"running"},
            to_state="dispatched",
            effect_disposition="unknown",
        )
        self.journal.transition(
            delivery_id,
            from_states={"dispatched"},
            to_state="unknown",
            effect_disposition="unknown",
            error="prior worker crashed after dispatch",
        )
        return {
            target.to_string(): {
                "success": True,
                "result": {"deduped": True, "state": "unknown"},
            }
        }


def _materialized_row(
    store: MissionOutboxStore,
    execution_id: str,
    *,
    mission_id: str | None = None,
):
    return store.materialize(
        execution_id=execution_id,
        node_id="notify",
        mission_id=mission_id,
        platform="telegram",
        target="chat:7",
        content="hello",
    )


def _approval_for(row, *, expires_at: int) -> dict[str, int | str]:
    return {
        "outbox_id": row.outbox_id,
        "revision": row.revision,
        "content_hash": row.content_hash,
        "destination": f"{row.platform}:{row.target}",
        "authority_version": 1,
        "expires_at": expires_at,
    }


def _create_current_mission_authority(
    row, *, workflow_db_path, authority_version: int = 1
) -> None:
    authority = {
        "valid": True,
        "revoked": False,
        "expires_at": 11,
        "allowed_effects": ["delayed_message"],
        "message_targets": [row.target],
    }
    wfdb.init_db(workflow_db_path)
    with wfdb.connect(workflow_db_path) as conn:
        conn.execute(
            """
            INSERT INTO missions (
                mission_id, profile, objective, constraints_json, authority_json,
                evidence_json, authority_version, status, verdict, receipt_id,
                created_at, updated_at, terminal_at
            ) VALUES (?, 'default', 'delivery test', '[]', ?,
                '{"checks":["workflow_succeeded"]}', ?, 'running',
                NULL, NULL, 1, 1, NULL)
            """,
            (row.mission_id, json.dumps(authority), authority_version),
        )


def _mission_dispatcher(*, tmp_path, store, router, journal, row):
    workflow_db_path = tmp_path / "workflows.db"
    _create_current_mission_authority(row, workflow_db_path=workflow_db_path)
    return MissionOutboxDispatcher(
        store=store,
        router=router,
        journal=journal,
        owner_id="gateway-test",
        workflow_db_path=workflow_db_path,
    )


def test_dispatcher_delivers_confirmed_ordinary_outbox_with_stable_identity(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-confirmed")
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == report.delivered == 1
        assert router.calls == [
            ("hello", "telegram", "chat:7", {"delivery_id": row.delivery_id})
        ]
        delivered = store.get_by_id(row.outbox_id)
        assert delivered is not None
        assert delivered.status == "delivered"
        assert delivered.result == {"message_id": "m-7"}
    finally:
        db.close()


def test_dispatcher_marks_unsupported_platform_unknown_and_continues_batch(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        unsupported = store.materialize(
            execution_id="exec-unsupported-platform",
            node_id="bad-notify",
            platform="removed-plugin",
            target="chat:bad",
            content="never send",
        )
        supported = store.materialize(
            execution_id="exec-supported-platform",
            node_id="good-notify",
            platform="telegram",
            target="chat:good",
            content="send this",
        )
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=2))

        assert report.claimed == 2
        assert report.unknown == 1
        assert report.delivered == 1
        unsupported_after = store.get_by_id(unsupported.outbox_id)
        supported_after = store.get_by_id(supported.outbox_id)
        assert unsupported_after is not None
        assert supported_after is not None
        assert unsupported_after.status == "unknown"
        assert supported_after.status == "delivered"
        assert [call[0] for call in router.calls] == ["send this"]
    finally:
        db.close()


def test_dispatcher_releases_unapproved_mission_before_router_call(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-mission-approval", mission_id="mission-1")
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path,
            store=store,
            router=router,
            journal=journal,
            row=row,
        )

        first = asyncio.run(dispatcher.drain(now=10, limit=1))

        # Approval is an eligibility fence, not a post-claim cleanup step.
        assert first.claimed == first.released == 0
        assert router.calls == []
        released = store.get_by_id(row.outbox_id)
        assert released is not None
        assert released.status == "scheduled"
        assert released.claim_token is None
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )

        second = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert second.claimed == second.delivered == 1
        assert len(router.calls) == 1
        delivered = store.get_by_id(row.outbox_id)
        assert delivered is not None
        assert delivered.status == "delivered"
        assert row.transaction_id is not None
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "committed"
    finally:
        db.close()


def test_dispatcher_releases_revoked_current_mission_authority_before_router_call(tmp_path):
    workflow_db_path = tmp_path / "workflows.db"
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-revoked-mission", mission_id="mission-revoked")
        _create_current_mission_authority(row, workflow_db_path=workflow_db_path)
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )
        with wfdb.connect(workflow_db_path) as conn:
            authority = json.loads(
                conn.execute(
                    "SELECT authority_json FROM missions WHERE mission_id = ?",
                    (row.mission_id,),
                ).fetchone()["authority_json"]
            )
            authority["revoked"] = True
            conn.execute(
                """
                UPDATE missions
                   SET authority_json = ?, authority_version = authority_version + 1
                 WHERE mission_id = ?
                """,
                (json.dumps(authority), row.mission_id),
            )
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=workflow_db_path,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == report.released == 0
        assert router.calls == []
        released = store.get_by_id(row.outbox_id)
        assert released is not None
        assert released.status == "scheduled"
    finally:
        db.close()


def test_dispatcher_rechecks_mission_authority_before_each_delivery(tmp_path):
    workflow_db_path = tmp_path / "workflows.db"
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        first = _materialized_row(store, "exec-first", mission_id="mission-first")
        second = _materialized_row(store, "exec-second", mission_id="mission-second")
        _create_current_mission_authority(first, workflow_db_path=workflow_db_path)
        _create_current_mission_authority(second, workflow_db_path=workflow_db_path)
        for row in (first, second):
            assert db.set_outbox_approval(
                row.outbox_id,
                expected_revision=row.revision,
                approval=_approval_for(row, expires_at=11),
            )
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        clock = iter((10, 10, 10, 11))
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=workflow_db_path,
            clock=lambda: next(clock),
        )

        report = asyncio.run(dispatcher.drain(limit=2))

        assert report.claimed == 2
        assert report.delivered == 1
        assert report.released == 1
        assert [call[0] for call in router.calls] == ["hello"]
        second_after = store.get_by_id(second.outbox_id)
        assert second_after is not None
        assert second_after.status == "scheduled"
    finally:
        db.close()


def test_dispatcher_releases_expired_mission_approval_before_router_call(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-expired-approval", mission_id="mission-1")
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=10),
        )
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == report.released == 0
        assert router.calls == []
        released = store.get_by_id(row.outbox_id)
        assert released is not None
        assert released.status == "scheduled"
    finally:
        db.close()


def test_dispatcher_projects_known_mission_delivery_failure_to_effect_transaction(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-mission-failed", mission_id="mission-1")
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )
        journal = OperationJournal(db)
        router = _FailedRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path,
            store=store,
            router=router,
            journal=journal,
            row=row,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == report.failed == 1
        assert row.transaction_id is not None
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "failed"
    finally:
        db.close()


def test_dispatcher_projects_ambiguous_mission_delivery_to_unknown_effect(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-mission-unknown", mission_id="mission-1")
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )
        journal = OperationJournal(db)
        router = _TimeoutRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path,
            store=store,
            router=router,
            journal=journal,
            row=row,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == report.unknown == 1
        assert row.transaction_id is not None
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "unknown_effect"
    finally:
        db.close()


def test_dispatcher_fails_closed_for_stale_committing_mission_effect(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-stale-committing", mission_id="mission-1")
        approval = _approval_for(row, expires_at=11)
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=approval,
        )
        assert row.transaction_id is not None
        assert db.transition_effect_transaction(
            row.transaction_id,
            expected_phase="pending",
            next_phase="previewed",
            authority=approval,
        )
        assert db.transition_effect_transaction(
            row.transaction_id,
            expected_phase="previewed",
            next_phase="committing",
        )
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path,
            store=store,
            router=router,
            journal=journal,
            row=row,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == report.unknown == 1
        assert router.calls == []
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "unknown"
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "unknown_effect"
    finally:
        db.close()


def test_dispatcher_terminalizes_pre_router_effect_failure_as_failed(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-pre-router-effect-failure", mission_id="mission-1")
        approval = _approval_for(row, expires_at=11)
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=approval,
        )
        assert row.transaction_id is not None
        transition = db.transition_effect_transaction

        def reject_preview(transaction_id, **kwargs):
            if (
                kwargs.get("expected_phase") == "pending"
                and kwargs.get("next_phase") == "previewed"
            ):
                return False
            return transition(transaction_id, **kwargs)

        monkeypatch.setattr(db, "transition_effect_transaction", reject_preview)
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path,
            store=store,
            router=router,
            journal=journal,
            row=row,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == report.failed == 1
        assert router.calls == []
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "failed"
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "failed"
    finally:
        db.close()


def test_dispatcher_rejects_foreign_profile_before_claim_or_router_call(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hades"
    monkeypatch.setenv("HADES_HOME", str(home))
    workflow_db_path = home / "workflows.db"
    db = SessionDB(db_path=home / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-foreign-profile", mission_id="mission-foreign")
        _create_current_mission_authority(row, workflow_db_path=workflow_db_path)
        with wfdb.connect(workflow_db_path) as conn:
            conn.execute(
                "UPDATE missions SET profile = 'foreign' WHERE mission_id = ?",
                (row.mission_id,),
            )
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=workflow_db_path,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 0
        assert report.delivered == report.failed == report.unknown == report.released == 0
        assert router.calls == []
        untouched = store.get_by_id(row.outbox_id)
        assert untouched is not None
        assert untouched.status == "scheduled"
        assert untouched.claim_token is None
    finally:
        db.close()


def test_dispatcher_rejects_foreign_profile_env_for_active_home_before_claim_or_router_call(
    tmp_path, monkeypatch
):
    active_home = tmp_path / "profiles" / "active"
    monkeypatch.setenv("HADES_HOME", str(active_home))
    monkeypatch.setenv("HERMES_PROFILE", "foreign")
    workflow_db_path = active_home / "workflows.db"
    db = SessionDB(db_path=active_home / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(
            store, "exec-foreign-profile-env", mission_id="mission-foreign-env"
        )
        _create_current_mission_authority(row, workflow_db_path=workflow_db_path)
        with wfdb.connect(workflow_db_path) as conn:
            conn.execute(
                "UPDATE missions SET profile = 'foreign' WHERE mission_id = ?",
                (row.mission_id,),
            )
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=workflow_db_path,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 0
        assert report.delivered == report.failed == report.unknown == report.released == 0
        assert router.calls == []
        untouched = store.get_by_id(row.outbox_id)
        assert untouched is not None
        assert untouched.status == "scheduled"
        assert untouched.claim_token is None
    finally:
        db.close()


def test_dispatcher_fails_closed_when_active_profile_binding_is_implicit(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("HADES_HOME", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-implicit-profile")
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 0
        assert report.delivered == report.failed == report.unknown == report.released == 0
        assert router.calls == []
        untouched = store.get_by_id(row.outbox_id)
        assert untouched is not None
        assert untouched.status == "scheduled"
        assert untouched.claim_token is None
    finally:
        db.close()


def test_dispatcher_rejects_foreign_state_and_workflow_in_same_parent(
    tmp_path, monkeypatch
):
    active_home = tmp_path / "profiles" / "active"
    foreign_home = tmp_path / "profiles" / "foreign"
    monkeypatch.setenv("HADES_HOME", str(active_home))
    db = SessionDB(db_path=foreign_home / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-same-parent-foreign")
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=foreign_home / "workflows.db",
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 0
        assert report.delivered == report.failed == report.unknown == report.released == 0
        assert router.calls == []
        untouched = store.get_by_id(row.outbox_id)
        assert untouched is not None
        assert untouched.status == "scheduled"
        assert untouched.claim_token is None
    finally:
        db.close()


def test_dispatcher_rejects_foreign_state_when_workflow_path_is_implicit(
    tmp_path, monkeypatch
):
    active_home = tmp_path / "profiles" / "active"
    foreign_home = tmp_path / "profiles" / "foreign"
    monkeypatch.setenv("HADES_HOME", str(active_home))
    db = SessionDB(db_path=foreign_home / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-implicit-workflow-foreign-state")
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 0
        assert report.delivered == report.failed == report.unknown == report.released == 0
        assert router.calls == []
        untouched = store.get_by_id(row.outbox_id)
        assert untouched is not None
        assert untouched.status == "scheduled"
        assert untouched.claim_token is None
    finally:
        db.close()


def test_dispatcher_quarantines_corrupt_due_row_and_delivers_valid_batch(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        corrupt = _materialized_row(store, "exec-corrupt-dispatcher")
        valid = _materialized_row(store, "exec-valid-after-corrupt")
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE mission_outbox SET content_json = ? WHERE outbox_id = ?",
                ("{not-json", corrupt.outbox_id),
            )
        )
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=2))

        assert report.claimed == report.delivered == 1
        assert router.calls == [("hello", "telegram", "chat:7", {"delivery_id": valid.delivery_id})]
        quarantined = store.get_by_id(corrupt.outbox_id)
        assert quarantined is not None
        assert quarantined.status == "failed"
        assert quarantined.claim_token is None
        assert quarantined.content == {"corrupt_payload": True}
        delivered = store.get_by_id(valid.outbox_id)
        assert delivered is not None
        assert delivered.status == "delivered"
    finally:
        db.close()


def test_dispatcher_rolls_back_terminal_outbox_when_effect_settlement_raises(
    tmp_path, monkeypatch
):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-terminal-rollback", mission_id="mission-1")
        approval = _approval_for(row, expires_at=11)
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=approval,
        )
        assert row.transaction_id is not None
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path,
            store=store,
            router=router,
            journal=journal,
            row=row,
        )
        transition = db.transition_effect_transaction

        def fail_terminal_effect(transaction_id, **kwargs):
            if kwargs.get("next_phase") == "committed":
                raise RuntimeError("effect settlement fault")
            return transition(transaction_id, **kwargs)

        monkeypatch.setattr(db, "transition_effect_transaction", fail_terminal_effect)

        with pytest.raises(RuntimeError, match="effect settlement fault"):
            asyncio.run(dispatcher.drain(now=10, limit=1))

        assert len(router.calls) == 1
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "claimed"
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "committing"
    finally:
        db.close()


def test_dispatcher_rolls_back_terminal_outbox_when_effect_settlement_loses_cas(
    tmp_path, monkeypatch
):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-terminal-cas-loss", mission_id="mission-1")
        approval = _approval_for(row, expires_at=11)
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=approval,
        )
        assert row.transaction_id is not None
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path,
            store=store,
            router=router,
            journal=journal,
            row=row,
        )
        transition = db.transition_effect_transaction

        def lose_terminal_effect_cas(transaction_id, **kwargs):
            if kwargs.get("next_phase") == "committed":
                return False
            return transition(transaction_id, **kwargs)

        monkeypatch.setattr(db, "transition_effect_transaction", lose_terminal_effect_cas)

        with pytest.raises(RuntimeError, match="effect settlement CAS lost"):
            asyncio.run(dispatcher.drain(now=10, limit=1))

        assert len(router.calls) == 1
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "claimed"
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "committing"
    finally:
        db.close()


def test_dispatcher_marks_known_router_failure_terminal_without_retry(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-failed")
        journal = OperationJournal(db)
        router = _FailedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == report.failed == 1
        failed = store.get_by_id(row.outbox_id)
        assert failed is not None
        assert failed.status == "failed"
        assert asyncio.run(dispatcher.drain(now=11, limit=1)).claimed == 0
        assert router.calls == 1
    finally:
        db.close()


def test_dispatcher_marks_post_dispatch_timeout_unknown_without_retry(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-timeout")
        journal = OperationJournal(db)
        router = _TimeoutRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == report.unknown == 1
        unknown = store.get_by_id(row.outbox_id)
        assert unknown is not None
        assert unknown.status == "unknown"
        assert asyncio.run(dispatcher.drain(now=11, limit=1)).claimed == 0
        assert router.calls == 1
    finally:
        db.close()


def test_dispatcher_does_not_promote_unknown_journal_dedup_to_delivered(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-unknown-dedup")
        journal = OperationJournal(db)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=_UnknownDedupRouter(journal),
            journal=journal,
            owner_id="gateway-test",
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == report.unknown == 1
        unknown = store.get_by_id(row.outbox_id)
        assert unknown is not None
        assert unknown.status == "unknown"
    finally:
        db.close()


def test_dispatcher_does_not_claim_when_mission_lookup_fails(tmp_path):
    """Missing/error mission rows stay scheduled and do not churn a lease."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        missing = _materialized_row(
            store, "exec-missing-mission", mission_id="mission-missing"
        )
        valid = _materialized_row(store, "exec-valid-after-missing")
        workflow_db_path = tmp_path / "workflows.db"
        wfdb.init_db(workflow_db_path)
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=workflow_db_path,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=2))

        assert report.claimed == report.delivered == 1
        assert router.calls == [
            ("hello", "telegram", "chat:7", {"delivery_id": valid.delivery_id})
        ]
        missing_after = store.get_by_id(missing.outbox_id)
        assert missing_after is not None
        assert missing_after.status == "scheduled"
        assert missing_after.claim_token is None
    finally:
        db.close()


def test_dispatcher_bounds_mission_lookup_scan_to_limit(tmp_path, monkeypatch):
    """A large due queue must not cause an unbounded workflow DB pre-scan."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        workflow_db_path = tmp_path / "workflows.db"
        wfdb.init_db(workflow_db_path)
        rows = [
            _materialized_row(store, f"exec-bounded-{i}", mission_id=f"mission-{i}")
            for i in range(5)
        ]
        for row in rows:
            _create_current_mission_authority(row, workflow_db_path=workflow_db_path)
            assert db.set_outbox_approval(
                row.outbox_id,
                expected_revision=row.revision,
                approval=_approval_for(row, expires_at=11),
            )
        calls = 0
        original_get_mission = mdb.get_mission

        def counted_get_mission(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_get_mission(*args, **kwargs)

        monkeypatch.setattr(mdb, "get_mission", counted_get_mission)
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=workflow_db_path,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=2))

        assert report.claimed == 2
        # Pre-scan reads are bounded by `limit` (2), plus one uncached fresh
        # read per claimed row immediately before its router call (P0-1: the
        # fresh authority fence never trusts the pre-scan snapshot for the
        # actual delivery decision) — bounded by `limit` again, not by the
        # size of the due queue.
        assert calls <= 2 * 2
    finally:
        db.close()


class _HangingRouter:
    async def deliver(self, *_args, **_kwargs):
        await asyncio.Event().wait()


def test_dispatcher_bounds_router_delivery_wait(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-delivery-timeout")
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=_HangingRouter(),
            journal=OperationJournal(db),
            owner_id="gateway-test",
            delivery_timeout_seconds=0.01,
        )

        report = asyncio.run(asyncio.wait_for(dispatcher.drain(now=10, limit=1), 0.2))

        assert report.claimed == report.unknown == 1
        after = store.get_by_id(row.outbox_id)
        assert after is not None
        assert after.status == "unknown"
    finally:
        db.close()


class _ResistantRouter:
    """A router whose deliver() coroutine ignores cancellation until
    explicitly released — simulates a misbehaving adapter for P1-4
    regression coverage. Unlike ``_HangingRouter`` (hangs but responds to
    cancellation instantly), this one actively swallows CancelledError."""

    def __init__(self) -> None:
        self.allow_stop = False
        self.swallow_count = 0
        self.started = asyncio.Event()

    async def deliver(self, *_args, **_kwargs):
        self.started.set()
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if self.allow_stop:
                    raise
                # Deliberately ignore cancellation and keep running.
                self.swallow_count += 1


def test_dispatcher_bounds_router_delivery_wait_for_resistant_router(tmp_path):
    """A router whose deliver() swallows CancelledError and keeps running
    must not block drain() past the configured deadline (P1-4).

    Before the fix, drain() awaited
    ``asyncio.wait_for(router.deliver(...), timeout=...)``: wait_for
    cancels the wrapped coroutine on timeout but then still awaits it to
    completion with no further bound, so a cancellation-resistant router
    held that await open indefinitely — silently turning the configured
    ``delivery_timeout_seconds`` into no deadline at all.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-resistant-router")
        router = _ResistantRouter()
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=OperationJournal(db),
            owner_id="gateway-test",
            delivery_timeout_seconds=0.05,
        )

        async def _run():
            start = time.monotonic()
            try:
                report = await asyncio.wait_for(
                    dispatcher.drain(now=10, limit=1), timeout=2.0
                )
                elapsed = time.monotonic() - start
            finally:
                # Release the resistant coroutine unconditionally —
                # including on a failure/timeout of the assertion above —
                # so this test's own event loop can tear down cleanly.
                # asyncio.run()'s cleanup sends one more cancellation to
                # any still-pending task, and only a released router
                # honors it; a bug that regresses drain() back toward an
                # unbounded wait must show up as a clean test failure, not
                # a hung test run.
                await asyncio.wait_for(router.started.wait(), timeout=1.0)
                router.allow_stop = True
            return report, elapsed

        report, elapsed = asyncio.run(_run())

        # Bounded by the configured deadline, not by whenever the
        # resistant router eventually stops.
        assert elapsed < 1.0
        assert report.claimed == report.unknown == 1
        assert router.swallow_count >= 1
        after = store.get_by_id(row.outbox_id)
        assert after is not None
        assert after.status == "unknown"
    finally:
        db.close()


def _mutate_mission_after_first_lookup(monkeypatch, *, workflow_db_path, mission_id, mutate):
    """Simulate a mission changing between claim and the pre-router recheck.

    ``drain()``'s eligibility pre-scan (``_owned_due_outbox_ids``) reads the
    mission once, up front, and caches it. Patching ``mdb.get_mission`` to
    apply ``mutate`` right after that first real read — using a separate
    connection so it commits before any later read — reproduces "another
    process changed the mission row while this drain was in flight" without
    any real concurrency. The fresh, uncached read the pre-router check now
    performs (P0-1) must observe the mutated row, not the pre-scan snapshot.
    """
    original_get_mission = mdb.get_mission
    state = {"calls": 0}

    def patched(conn, mid):
        state["calls"] += 1
        result = original_get_mission(conn, mid)
        if state["calls"] == 1 and mid == mission_id:
            with wfdb.connect(workflow_db_path) as mutate_conn:
                mutate(mutate_conn)
        return result

    monkeypatch.setattr(mdb, "get_mission", patched)


def test_dispatcher_rejects_delivery_when_mission_revoked_after_claim(
    tmp_path, monkeypatch
):
    """A revoke landing after claim, before the pre-router recheck, must
    still block the router call — not just a revoke landing before drain()
    even starts (already covered by
    test_dispatcher_releases_revoked_current_mission_authority_before_router_call).
    """
    workflow_db_path = tmp_path / "workflows.db"
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-revoke-after-claim", mission_id="mission-1")
        _create_current_mission_authority(row, workflow_db_path=workflow_db_path)
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )

        def revoke(conn):
            authority = json.loads(
                conn.execute(
                    "SELECT authority_json FROM missions WHERE mission_id = ?",
                    (row.mission_id,),
                ).fetchone()["authority_json"]
            )
            authority["revoked"] = True
            conn.execute(
                "UPDATE missions SET authority_json = ? WHERE mission_id = ?",
                (json.dumps(authority), row.mission_id),
            )

        _mutate_mission_after_first_lookup(
            monkeypatch,
            workflow_db_path=workflow_db_path,
            mission_id=row.mission_id,
            mutate=revoke,
        )
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=workflow_db_path,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 1
        assert router.calls == []
        assert report.failed == 1
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "failed"
        assert row.transaction_id is not None
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "failed"
    finally:
        db.close()


def test_dispatcher_rejects_delivery_when_authority_version_bumped_after_claim(
    tmp_path, monkeypatch
):
    """A re-authorization (version bump) landing after claim must void the
    outstanding approval even though its own fields (revoked/valid/expiry)
    are unchanged — the approval was minted for the version that existed at
    claim time, not the version that exists at delivery time.
    """
    workflow_db_path = tmp_path / "workflows.db"
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-version-after-claim", mission_id="mission-1")
        _create_current_mission_authority(row, workflow_db_path=workflow_db_path)
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )

        def bump_version(conn):
            conn.execute(
                "UPDATE missions SET authority_version = authority_version + 1 "
                "WHERE mission_id = ?",
                (row.mission_id,),
            )

        _mutate_mission_after_first_lookup(
            monkeypatch,
            workflow_db_path=workflow_db_path,
            mission_id=row.mission_id,
            mutate=bump_version,
        )
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=workflow_db_path,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 1
        assert router.calls == []
        assert report.failed == 1
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "failed"
        assert row.transaction_id is not None
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "failed"
    finally:
        db.close()


def test_dispatcher_rejects_delivery_when_mission_reaches_terminal_state_after_claim(
    tmp_path, monkeypatch
):
    """A mission concluding (e.g. cancelled) after claim, before the
    pre-router recheck, must block the router call even though the
    authority blob itself (revoked/valid/version) never changed.
    """
    workflow_db_path = tmp_path / "workflows.db"
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-terminal-after-claim", mission_id="mission-1")
        _create_current_mission_authority(row, workflow_db_path=workflow_db_path)
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )

        def terminalize(conn):
            conn.execute(
                "UPDATE missions SET status = 'cancelled', verdict = 'cancelled' "
                "WHERE mission_id = ?",
                (row.mission_id,),
            )

        _mutate_mission_after_first_lookup(
            monkeypatch,
            workflow_db_path=workflow_db_path,
            mission_id=row.mission_id,
            mutate=terminalize,
        )
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=workflow_db_path,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 1
        assert router.calls == []
        assert report.failed == 1
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "failed"
        assert row.transaction_id is not None
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "failed"
    finally:
        db.close()


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


def test_dispatcher_rejects_delivery_when_prepared_json_diverges_before_effect_begin(
    tmp_path,
):
    """A split ledger (effect present but prepared_json diverges from the
    outbox row) caught before the effect ever transitions must block the
    router call, not just the pre-router recheck (P0-2)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-prepared-diverge", mission_id="mission-1")
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )
        assert row.transaction_id is not None
        _corrupt_prepared_target(db, row.transaction_id)
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path, store=store, router=router, journal=journal, row=row
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 1
        assert router.calls == []
        assert report.failed == 1
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "failed"
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "failed"
    finally:
        db.close()


def test_dispatcher_rejects_delivery_when_operation_identity_diverges_before_effect_begin(
    tmp_path,
):
    """A split ledger on the operation side (agent_operations diverges from
    the effect/outbox it is supposed to identify) must also block delivery."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-operation-diverge", mission_id="mission-1")
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )
        assert row.transaction_id is not None
        _diverge_operation_identity(db, f"{row.outbox_id}:operation")
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path, store=store, router=router, journal=journal, row=row
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 1
        assert router.calls == []
        assert report.failed == 1
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "failed"
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "failed"
    finally:
        db.close()


def test_dispatcher_settles_unknown_when_effect_row_missing_before_effect_begin(
    tmp_path,
):
    """A missing effect row (the durable link materialize() always creates
    atomically is simply gone) must resolve cleanly to "unknown" — not crash
    the drain batch and not silently deliver (P0-2)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-effect-missing-early", mission_id="mission-1")
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )
        assert row.transaction_id is not None
        _delete_effect_row(db, row.transaction_id)
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path, store=store, router=router, journal=journal, row=row
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 1
        assert router.calls == []
        assert report.unknown == 1
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "unknown"
        assert db.get_effect_transaction(row.transaction_id) is None
    finally:
        db.close()


def test_dispatcher_rejects_delivery_when_graph_corrupts_between_commit_and_router_call(
    tmp_path, monkeypatch
):
    """The graph can still be corrupted after _begin_mission_effect already
    advanced the row to "committing" — the fresh pre-router graph check
    (P0-2) must catch it there too, not just before the transition."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-graph-corrupt-late", mission_id="mission-1")
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )
        assert row.transaction_id is not None
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path, store=store, router=router, journal=journal, row=row
        )
        original_begin = MissionOutboxDispatcher._begin_mission_effect

        def corrupt_after_begin(self, dispatched_row):
            result = original_begin(self, dispatched_row)
            if result:
                _corrupt_prepared_target(db, dispatched_row.transaction_id)
            return result

        monkeypatch.setattr(
            MissionOutboxDispatcher, "_begin_mission_effect", corrupt_after_begin
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 1
        assert router.calls == []
        assert report.failed == 1
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "failed"
        transaction = db.get_effect_transaction(row.transaction_id)
        assert transaction is not None
        assert transaction.phase == "failed"
    finally:
        db.close()


def test_dispatcher_rejects_delivery_when_effect_disappears_between_commit_and_router_call(
    tmp_path, monkeypatch
):
    """The effect row can vanish entirely after reaching "committing". The
    fresh pre-router graph check must still block the router call, and
    settling the outbox as "failed" must not crash trying to CAS a
    transaction row that no longer exists (P0-2)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = _materialized_row(store, "exec-effect-vanish-late", mission_id="mission-1")
        assert db.set_outbox_approval(
            row.outbox_id,
            expected_revision=row.revision,
            approval=_approval_for(row, expires_at=11),
        )
        assert row.transaction_id is not None
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = _mission_dispatcher(
            tmp_path=tmp_path, store=store, router=router, journal=journal, row=row
        )
        original_begin = MissionOutboxDispatcher._begin_mission_effect

        def vanish_after_begin(self, dispatched_row):
            result = original_begin(self, dispatched_row)
            if result:
                _delete_effect_row(db, dispatched_row.transaction_id)
            return result

        monkeypatch.setattr(
            MissionOutboxDispatcher, "_begin_mission_effect", vanish_after_begin
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == 1
        assert router.calls == []
        assert report.failed == 1
        outbox = store.get_by_id(row.outbox_id)
        assert outbox is not None
        assert outbox.status == "failed"
        assert db.get_effect_transaction(row.transaction_id) is None
    finally:
        db.close()


def test_dispatcher_prescan_does_not_starve_valid_row_behind_invalid_prefix(tmp_path):
    """A single missing-mission row at the front of the due queue must not
    permanently block a valid row behind it when limit=1 (P1-3).

    Before the fix, the pre-scan's SQL LIMIT matched `limit` exactly, so at
    limit=1 it only ever inspected the single (invalid) row at the front —
    finding zero eligible ids and leaving `valid` unclaimed forever, on
    every future drain tick, since nothing about the invalid row's queue
    position or status ever changes.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        invalid = store.materialize(
            execution_id="exec-invalid-prefix",
            node_id="notify",
            mission_id="mission-does-not-exist",
            platform="telegram",
            target="chat:7",
            content="hello",
            not_before=0,
        )
        valid = store.materialize(
            execution_id="exec-valid-behind-invalid-prefix",
            node_id="notify",
            platform="telegram",
            target="chat:7",
            content="hello world",
            not_before=1,
        )
        workflow_db_path = tmp_path / "workflows.db"
        wfdb.init_db(workflow_db_path)
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=workflow_db_path,
        )

        report = asyncio.run(dispatcher.drain(now=10, limit=1))

        assert report.claimed == report.delivered == 1
        assert router.calls == [
            ("hello world", "telegram", "chat:7", {"delivery_id": valid.delivery_id})
        ]
        delivered = store.get_by_id(valid.outbox_id)
        assert delivered is not None
        assert delivered.status == "delivered"
        # The invalid row in front is left alone — never claimed, never
        # mutated — since this dispatcher has no authority to settle a
        # mission it cannot find.
        invalid_after = store.get_by_id(invalid.outbox_id)
        assert invalid_after is not None
        assert invalid_after.status == "scheduled"
        assert invalid_after.claim_token is None
    finally:
        db.close()


def test_dispatcher_prescan_bounds_inspection_past_invalid_prefix(tmp_path, monkeypatch):
    """The pre-scan looks past a bounded invalid prefix, not an unbounded
    one: inspection stays a constant multiple of `limit`, not the size of
    the due queue (P1-3)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = MissionOutboxStore(db)
        # More invalid rows than any bounded multiple of limit=1 would
        # inspect, so the valid row at the very back is provably never
        # reached by the pre-scan's SQL fetch itself.
        for i in range(50):
            store.materialize(
                execution_id=f"exec-invalid-prefix-bulk-{i}",
                node_id="notify",
                mission_id=f"mission-does-not-exist-{i}",
                platform="telegram",
                target="chat:7",
                content="hello",
                not_before=i,
            )
        valid = store.materialize(
            execution_id="exec-valid-behind-bulk-invalid-prefix",
            node_id="notify",
            platform="telegram",
            target="chat:7",
            content="hello world",
            not_before=100,
        )
        workflow_db_path = tmp_path / "workflows.db"
        wfdb.init_db(workflow_db_path)
        journal = OperationJournal(db)
        router = _ConfirmedRouter(journal)
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=router,
            journal=journal,
            owner_id="gateway-test",
            workflow_db_path=workflow_db_path,
        )
        calls = 0
        original_get_mission = mdb.get_mission

        def counted_get_mission(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_get_mission(*args, **kwargs)

        monkeypatch.setattr(mdb, "get_mission", counted_get_mission)

        report = asyncio.run(dispatcher.drain(now=100, limit=1))

        # Bounded, not exhaustive: the pre-scan does not claim the valid
        # row buried behind 50 invalid ones, and it does not pay for 50
        # mission lookups finding that out either.
        assert report.claimed == 0
        assert router.calls == []
        assert calls <= 1 * 4
        unclaimed = store.get_by_id(valid.outbox_id)
        assert unclaimed is not None
        assert unclaimed.status == "scheduled"
    finally:
        db.close()
