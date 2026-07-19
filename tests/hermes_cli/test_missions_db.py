"""TDD: durable Mission aggregate + atomic workflow execution linking.

The mission aggregate lives in the profile-local workflows.db (never
state.db). `create_mission_and_execution` performs all of: read
definition (with cross-profile agent_task rejection), insert mission row,
start workflow execution, insert the primary execution link, and append
the two initial mission events — inside one outer transaction so a
`start_execution` failure rolls back the whole mission aggregate.
"""

from __future__ import annotations

import pytest

from hades_cli import missions_db as mdb
from hades_cli import workflows_db as wfdb
from hades_cli.missions_db import MissionStateError
from hades_cli.workflows_spec import WorkflowSpec


def _three_effects_spec(*, version: int = 1) -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": "three_effects",
        "name": "Three Effects",
        "version": version,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "start": {
                "type": "agent_task",
                "profile": "default",
                "prompt": "do the work",
            },
        },
    })


def _cross_profile_spec() -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": "cross_profile",
        "name": "Cross Profile",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "start": {
                "type": "agent_task",
                "profile": "other",
                "prompt": "do the work",
            },
        },
    })


def _pass_spec(*, workflow_id: str = "pass_only") -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "id": workflow_id,
        "name": "Pass Only",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {"start": {"type": "pass"}},
    })


def _deploy(conn, spec: WorkflowSpec) -> None:
    wfdb.deploy_definition(conn, spec, created_by="test")


def _counts(conn) -> dict[str, int]:
    return {
        "missions": conn.execute("SELECT count(*) FROM missions").fetchone()[0],
        "execution_links": conn.execute(
            "SELECT count(*) FROM mission_execution_links"
        ).fetchone()[0],
        "events": conn.execute("SELECT count(*) FROM mission_events").fetchone()[0],
        "executions": conn.execute(
            "SELECT count(*) FROM workflow_executions"
        ).fetchone()[0],
    }


def test_init_db_creates_mission_tables(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    wfdb.init_db()
    with wfdb.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "missions" in tables
    assert "mission_execution_links" in tables
    assert "mission_events" in tables
    assert "mission_review_items" in tables


def test_create_mission_and_execution_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, execution = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="Patch, verify, record state, then notify me",
            constraints=["never push", "only modify the worktree"],
            authority={
                "allowed_effects": ["workspace", "hades_state", "delayed_message"],
                "workspace_roots": [str(worktree)],
                "message_targets": ["test:mission-user"],
                "expires_at": 1_800_000_000,
                "irreversible": "ask",
            },
            evidence={
                "checks": ["workflow_succeeded", "all_effects_settled", "fresh_verification"],
                "artifacts": ["report.json"],
            },
            input_data={"issue": "real fixture"},
            profile="default",
        )

        # Execution links to the workflow it claims to.
        assert wfdb.get_execution(conn, execution.execution_id).workflow_id == "three_effects"
        # Link exists, is the only one, and is primary.
        links = mdb.list_execution_links(conn, mission.mission_id)
        assert links == [execution.execution_id]
        # Mission persisted with the requested objective.
        assert mission.objective == "Patch, verify, record state, then notify me"

        # Two initial events recorded (mission_created + execution_started).
        events = mdb.list_mission_events(conn, mission.mission_id)
        kinds = [e.kind for e in events]
        assert "mission_created" in kinds
        assert "execution_started" in kinds


def test_returned_mission_values_are_deep_copies(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="deep copy me",
            constraints=["c1"],
            authority={"allowed_effects": ["workspace"], "workspace_roots": [str(worktree)]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={"k": "v"},
            profile="default",
        )
        # Mutate returned structured values.
        mission.constraints.append("MUTATED")
        mission.authority["allowed_effects"].append("MUTATED")
        mission.evidence["checks"].append("MUTATED")

        fetched = mdb.get_mission(conn, mission.mission_id)
        assert "MUTATED" not in fetched.constraints
        assert "MUTATED" not in fetched.authority["allowed_effects"]
        assert "MUTATED" not in fetched.evidence["checks"]


def test_create_mission_and_execution_rolls_back_when_start_execution_fails(
    tmp_path, monkeypatch
):
    """A failing start_execution must leave no mission/link/event rows."""
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        # Disable the workflow so start_execution raises "is disabled".
        spec = _pass_spec(workflow_id="disabled_wf")
        _deploy(conn, spec)
        conn.execute(
            "UPDATE workflow_definitions SET enabled = 0 WHERE workflow_id = 'disabled_wf'"
        )

        before = _counts(conn)
        with pytest.raises(ValueError, match="disabled"):
            mdb.create_mission_and_execution(
                conn,
                workflow_id="disabled_wf",
                objective="should rollback",
                constraints=[],
                authority={"allowed_effects": ["workspace"]},
                evidence={"checks": ["workflow_succeeded"]},
                input_data={},
                profile="default",
            )
        after = _counts(conn)

    assert before == after


def test_create_mission_rejects_cross_profile_agent_task(tmp_path, monkeypatch):
    """An agent_task naming another profile is rejected before any row is written."""
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _cross_profile_spec())
        before = _counts(conn)
        with pytest.raises(ValueError, match="profile"):
            mdb.create_mission_and_execution(
                conn,
                workflow_id="cross_profile",
                objective="wrong profile",
                constraints=[],
                authority={"allowed_effects": ["workspace"]},
                evidence={"checks": ["workflow_succeeded"]},
                input_data={},
                profile="default",
            )
        after = _counts(conn)
    assert before == after


def test_terminal_verdict_cannot_transition_back_to_running(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="terminalize",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        mdb.set_mission_verdict(conn, mission.mission_id, "succeeded")
        with pytest.raises(ValueError, match="terminal"):
            mdb.set_mission_status(conn, mission.mission_id, "running")


def test_invalid_status_and_verdict_rejected_before_write(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="validate",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        with pytest.raises(ValueError):
            mdb.set_mission_status(conn, mission.mission_id, "not-a-real-status")
        with pytest.raises(ValueError):
            mdb.set_mission_verdict(conn, mission.mission_id, "vibes")


def test_duplicate_event_idempotency_key_produces_single_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="idempotent event",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        a = mdb.append_mission_event(
            conn,
            mission.mission_id,
            kind="audit",
            payload={"x": 1},
            idempotency_key="audit-key-1",
        )
        b = mdb.append_mission_event(
            conn,
            mission.mission_id,
            kind="audit",
            payload={"x": 1},
            idempotency_key="audit-key-1",
        )
        assert a.event_id == b.event_id
        count = conn.execute(
            "SELECT count(*) FROM mission_events WHERE mission_id = ? AND idempotency_key = ?",
            (mission.mission_id, "audit-key-1"),
        ).fetchone()[0]
        assert count == 1


def test_duplicate_review_idempotency_key_produces_single_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="idempotent review",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        a = mdb.upsert_review_item(
            conn,
            mission.mission_id,
            review_id="rev-1",
            kind="approve",
            detail={"why": "looks good"},
        )
        b = mdb.upsert_review_item(
            conn,
            mission.mission_id,
            review_id="rev-1",
            kind="approve",
            detail={"why": "looks good"},
        )
        assert a.review_id == b.review_id
        count = conn.execute(
            "SELECT count(*) FROM mission_review_items WHERE mission_id = ? AND review_id = ?",
            (mission.mission_id, "rev-1"),
        ).fetchone()[0]
        assert count == 1


def test_terminal_verdict_blocks_running_for_every_admitted_verdict(
    tmp_path, monkeypatch
):
    """Every verdict admitted by ``_MISSION_VERDICTS`` (including ``abandoned``)
    must block a transition back to ``running``. The root-cause fix lives in
    shared ``set_mission_status`` so callers don't need to know the verdict
    set.
    """
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        for verdict in mdb._MISSION_VERDICTS:
            mission, _ = mdb.create_mission_and_execution(
                conn,
                workflow_id="three_effects",
                objective=f"verdict-{verdict}",
                constraints=[],
                authority={"allowed_effects": ["workspace"]},
                evidence={"checks": ["workflow_succeeded"]},
                input_data={},
                profile="default",
            )
            mdb.set_mission_verdict(conn, mission.mission_id, verdict)
            with pytest.raises(MissionStateError, match="terminal"):
                mdb.set_mission_status(conn, mission.mission_id, "running")


def test_upsert_review_item_rejects_unknown_status(tmp_path, monkeypatch):
    """``upsert_review_item`` must validate ``status`` against a small
    explicit vocabulary before any SQL runs. ``pending`` remains the
    default; an obviously bogus value must raise and write nothing.
    """
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="bogus review status",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        with pytest.raises(MissionStateError):
            mdb.upsert_review_item(
                conn,
                mission.mission_id,
                review_id="rev-bogus",
                kind="approve",
                status="vibes",
            )
        # And it must NOT have persisted anything for that review_id.
        rows = conn.execute(
            "SELECT count(*) FROM mission_review_items WHERE review_id = ?",
            ("rev-bogus",),
        ).fetchone()[0]
        assert rows == 0


def test_upsert_review_item_accepts_default_pending(tmp_path, monkeypatch):
    """The default ``pending`` status still works — vocabulary change is
    additive only.
    """
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="default pending",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        item = mdb.upsert_review_item(
            conn,
            mission.mission_id,
            review_id="rev-default",
            kind="approve",
        )
        assert item.status == "pending"


def test_workflow_only_writer_still_works(tmp_path, monkeypatch):
    """Existing workflow-only behavior must remain backward compatible."""
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _pass_spec())
        exec_id = wfdb.start_execution(
            conn, "pass_only", input_data={"x": 1}, trigger_type="manual"
        )
        execution = wfdb.get_execution(conn, exec_id)
    assert execution.workflow_id == "pass_only"
    assert execution.status == "queued"
    assert execution.input == {"x": 1}


# ---------------------------------------------------------------------------
# Quality-remediation tests (Task 1 quality/state-integrity findings)
# ---------------------------------------------------------------------------


def test_null_idempotency_key_appends_new_row_each_call(tmp_path, monkeypatch):
    """No-key append is append-only — every call creates a new event row.

    SQLite's UNIQUE constraint treats NULLs as distinct, so multiple
    NULL idempotency_keys coexist. The IntegrityError recovery branch
    for ``idempotency_key=None`` is therefore dead.
    """
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="append no-key",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        a = mdb.append_mission_event(
            conn, mission.mission_id, kind="audit", payload={"x": 1}
        )
        b = mdb.append_mission_event(
            conn, mission.mission_id, kind="audit", payload={"x": 2}
        )
    assert a.event_id != b.event_id
    # The 2 initial events plus 2 no-key appends = 4 rows total.
    events = mdb.list_mission_events(conn, mission.mission_id)
    assert len(events) == 4


def test_returned_event_payload_is_deep_copy(tmp_path, monkeypatch):
    """Mutations to a returned event's payload must not leak into the row."""
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="event deep copy",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        event = mdb.append_mission_event(
            conn,
            mission.mission_id,
            kind="audit",
            payload={"items": [1, 2, 3]},
        )
        event.payload["items"].append(99)
        event.payload["new_key"] = "MUTATED"

    fetched = mdb.list_mission_events(conn, mission.mission_id)
    audit = [e for e in fetched if e.kind == "audit"][0]
    assert audit.payload == {"items": [1, 2, 3]}
    assert "new_key" not in audit.payload


def test_returned_event_payload_is_deep_copy_on_idempotent_path(tmp_path, monkeypatch):
    """The duplicate-key read path must also deep-copy the payload."""
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="event deep copy idempotent",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        first = mdb.append_mission_event(
            conn,
            mission.mission_id,
            kind="audit",
            payload={"items": [1, 2, 3]},
            idempotency_key="k1",
        )
        # Second call returns the same row through the duplicate-key path.
        second = mdb.append_mission_event(
            conn,
            mission.mission_id,
            kind="audit",
            payload={"items": [9, 9, 9]},
            idempotency_key="k1",
        )
        assert first.event_id == second.event_id
        second.payload["items"].append(99)
        second.payload["new_key"] = "MUTATED"

    fetched = mdb.list_mission_events(conn, mission.mission_id)
    audit = [e for e in fetched if e.kind == "audit"][0]
    assert audit.payload == {"items": [1, 2, 3]}
    assert "new_key" not in audit.payload


def test_terminal_verdict_cannot_be_overwritten_by_different_verdict(
    tmp_path, monkeypatch
):
    """Every admitted terminal verdict is final — no overwrite, even
    failed→failed, cancelled→abandoned, abandoned→succeeded.
    """
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        cases = [
            ("failed", "succeeded"),
            ("cancelled", "succeeded"),
            ("abandoned", "succeeded"),
            ("failed", "cancelled"),
            ("cancelled", "abandoned"),
            ("abandoned", "failed"),
        ]
        for original, attempted in cases:
            mission, _ = mdb.create_mission_and_execution(
                conn,
                workflow_id="three_effects",
                objective=f"{original}-then-{attempted}",
                constraints=[],
                authority={"allowed_effects": ["workspace"]},
                evidence={"checks": ["workflow_succeeded"]},
                input_data={},
                profile="default",
            )
            mdb.set_mission_verdict(conn, mission.mission_id, original)
            with pytest.raises(MissionStateError):
                mdb.set_mission_verdict(conn, mission.mission_id, attempted)


def test_terminal_status_cannot_be_overwritten_by_different_status(
    tmp_path, monkeypatch
):
    """``succeeded``/``failed``/``cancelled`` cannot be rewritten by a
    different admitted status (e.g. terminal→blocked, terminal→running,
    or terminal→another terminal).
    """
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        for terminal in ("succeeded", "failed", "cancelled"):
            mission, _ = mdb.create_mission_and_execution(
                conn,
                workflow_id="three_effects",
                objective=f"terminal-{terminal}",
                constraints=[],
                authority={"allowed_effects": ["workspace"]},
                evidence={"checks": ["workflow_succeeded"]},
                input_data={},
                profile="default",
            )
            mdb.set_mission_status(conn, mission.mission_id, terminal)
            for attempted in ("running", "blocked", "pending_authorization"):
                with pytest.raises(MissionStateError):
                    mdb.set_mission_status(
                        conn, mission.mission_id, attempted
                    )


def test_first_terminal_at_preserved_across_idempotent_updates(
    tmp_path, monkeypatch
):
    """The first terminalization timestamp is sticky — neither an
    idempotent terminal status update nor a later verdict update can
    overwrite ``terminal_at``.
    """
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="preserve terminal_at",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        mdb.set_mission_status(conn, mission.mission_id, "failed", now=1_000)
        first = mdb.get_mission(conn, mission.mission_id)
        assert first.terminal_at == 1_000

        # Idempotent same-value status update must not advance terminal_at.
        mdb.set_mission_status(conn, mission.mission_id, "failed", now=2_000)
        again = mdb.get_mission(conn, mission.mission_id)
        assert again.terminal_at == 1_000

        # Setting the verdict AFTER terminalization also must not overwrite.
        mdb.set_mission_verdict(conn, mission.mission_id, "failed", now=3_000)
        final = mdb.get_mission(conn, mission.mission_id)
        assert final.terminal_at == 1_000


def test_profile_check_runs_inside_outer_transaction(
    tmp_path, monkeypatch
):
    """``create_mission_and_execution`` must perform the cross-profile
    check inside the same ``write_txn`` that writes the mission and
    execution rows. Hook ``wfdb.write_txn`` to assert the
    ``_check_agent_task_profile`` call landed inside its body.
    """
    from hades_cli import missions_db as mdb_mod

    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _cross_profile_spec())

        captured: dict[str, bool] = {"saw_in_txn": False}
        real_write_txn = wfdb.write_txn

        from contextlib import contextmanager

        @contextmanager
        def spy_write_txn(c):
            with real_write_txn(c) as inner:
                # While the txn is open, the cross-profile check must
                # have already happened (it's done just before mission
                # insert, at the start of our txn body).
                captured["saw_in_txn"] = conn.in_transaction
                yield inner

        monkeypatch.setattr(mdb_mod.wfdb, "write_txn", spy_write_txn)

        with pytest.raises(MissionStateError, match="profile"):
            mdb.create_mission_and_execution(
                conn,
                workflow_id="cross_profile",
                objective="txn-check",
                constraints=[],
                authority={"allowed_effects": ["workspace"]},
                evidence={"checks": ["workflow_succeeded"]},
                input_data={},
                profile="default",
            )

    assert captured["saw_in_txn"] is True, (
        "profile check must run inside the same write_txn that performs "
        "the mission inserts (so the read and the writes share one txn "
        "snapshot)."
    )


def test_unknown_evidence_check_rejects_mission_before_execution_insert(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        with pytest.raises(ValueError, match="unsupported evidence check"):
            mdb.create_mission_and_execution(
                conn,
                workflow_id="three_effects",
                objective="no unknown evidence",
                constraints=[],
                authority={"allowed_effects": ["workspace"]},
                evidence={"checks": ["workflow_succeeded", "model_confidence"]},
                input_data={},
                profile="default",
            )
        assert conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0] == 0


def test_receipt_projection_links_once_and_records_evidence_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, _ = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="receipt projection",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        projected = mdb.project_receipt_verdict(
            conn,
            mission.mission_id,
            receipt_id="receipt_deadbeef",
            verdict="verified",
            now=1_000,
        )
        assert projected.receipt_id == "receipt_deadbeef"
        assert projected.verdict == "verified"
        assert projected.terminal_at == 1_000
        assert mdb.project_receipt_verdict(
            conn,
            mission.mission_id,
            receipt_id="receipt_deadbeef",
            verdict="verified",
            now=2_000,
        ).terminal_at == 1_000
        with pytest.raises(MissionStateError, match="receipt"):
            mdb.project_receipt_verdict(
                conn,
                mission.mission_id,
                receipt_id="receipt_other",
                verdict="verified",
            )


# ---------------------------------------------------------------------------
# mission_for_execution tests
# ---------------------------------------------------------------------------


def test_mission_for_execution_returns_none_when_no_link(tmp_path, monkeypatch):
    """No mission_execution_links row for this execution → None."""
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _pass_spec())
        exec_id = wfdb.start_execution(
            conn, "pass_only", input_data={}, trigger_type="manual"
        )
        result = mdb.mission_for_execution(conn, exec_id)
    assert result is None


def test_mission_for_execution_returns_linked_mission(tmp_path, monkeypatch):
    """Exactly one link → the linked MissionRecord."""
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _three_effects_spec())
        mission, execution = mdb.create_mission_and_execution(
            conn,
            workflow_id="three_effects",
            objective="find me",
            constraints=[],
            authority={"allowed_effects": ["workspace"]},
            evidence={"checks": ["workflow_succeeded"]},
            input_data={},
            profile="default",
        )
        result = mdb.mission_for_execution(conn, execution.execution_id)
    assert result is not None
    assert result.mission_id == mission.mission_id
    assert result.objective == "find me"


def test_mission_for_execution_raises_on_multiple_links(tmp_path, monkeypatch):
    """More than one mission link for one execution → MissionStateError.

    Direct SQL setup is permitted for this invariant test.
    """
    monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
    wfdb.init_db()
    with wfdb.connect() as conn:
        _deploy(conn, _pass_spec())
        exec_id = wfdb.start_execution(
            conn, "pass_only", input_data={}, trigger_type="manual"
        )
        # Insert two distinct missions and link both to the same execution.
        ts = 1_700_000_000
        for i in range(2):
            mid = f"mission_dup_{i}"
            conn.execute(
                """
                INSERT INTO missions (
                    mission_id, profile, objective,
                    constraints_json, authority_json, evidence_json,
                    authority_version, status, verdict, receipt_id,
                    created_at, updated_at, terminal_at
                ) VALUES (?, 'default', 'dup', '[]', '{}',
                    '{"checks":["workflow_succeeded"]}', 1, 'running',
                    NULL, NULL, ?, ?, NULL)
                """,
                (mid, ts + i, ts + i),
            )
            conn.execute(
                """
                INSERT INTO mission_execution_links (
                    mission_id, execution_id, relation, linked_at
                ) VALUES (?, ?, 'primary', ?)
                """,
                (mid, exec_id, ts + i),
            )

        with pytest.raises(MissionStateError, match="multiple"):
            mdb.mission_for_execution(conn, exec_id)
