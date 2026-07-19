"""Profile-home ownership fences for the durable outbox dispatcher."""

from __future__ import annotations

from agent.operation_journal import OperationJournal
from gateway import mission_delivery
from gateway.mission_delivery import MissionOutboxDispatcher
from gateway.mission_outbox import MissionOutboxStore
from hades_state import SessionDB


def test_implicit_default_home_owns_matching_state_and_workflow_databases(
    tmp_path, monkeypatch
):
    """The default profile is authoritative even without HADES_HOME exported."""
    home = tmp_path / ".hades"
    monkeypatch.delenv("HADES_HOME", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setattr(mission_delivery, "get_hades_home", lambda: home)

    db = SessionDB(db_path=home / "state.db")
    try:
        store = MissionOutboxStore(db)
        row = store.materialize(
            execution_id="implicit-default-home",
            node_id="notify",
            platform="telegram",
            target="chat:7",
            content="hello",
        )
        dispatcher = MissionOutboxDispatcher(
            store=store,
            router=object(),
            journal=OperationJournal(db),
            owner_id="profile-test",
            workflow_db_path=home / "workflows.db",
        )

        assert dispatcher._profile_store_owned()
        assert dispatcher._owned_due_outbox_ids(now=10, limit=1) == {row.outbox_id}
    finally:
        db.close()
