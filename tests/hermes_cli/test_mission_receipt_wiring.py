"""Mission terminalization issues the one canonical receipt.

Final integration of the canonical receipt contract with the missions
vertical slice: when ``_reconcile_unknown_mission`` terminalizes a
mission (unknown delivery outcome), the dispatcher — gated on the
profile's configured ``receipts.mode`` — inserts the receipt into the
profile-local state.db BEFORE the terminal verdict projection and links
it via the CAS ``mdb.project_receipt_verdict``. ``off`` (the shipped
default) stays byte-identical to the pre-receipt path: zero receipt
rows, plain ``set_mission_verdict``.

Real seams only: temp HADES_HOME, real SessionDB state.db, real
workflows.db through hades_cli.missions_db / hades_cli.workflows_db
APIs, the real dispatcher tick. No fixture DDL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent.receipt_ingest import build_receipt_issuer
from agent.receipt_models import ReceiptSourceKey
from agent.receipt_store import ReceiptStore
from agent.receipts import RECEIPT_STATUSES
from gateway.mission_outbox import MissionOutboxStore
from hades_state import SessionDB
from hades_cli import missions_db as mdb
from hades_cli import workflows_db as wfdb
from hades_cli import workflows_dispatcher


# ── Real-path harness (mirrors tests/hermes_cli/test_workflows_dispatcher.py) ──


def _send_message_spec():
    from hades_cli.workflows_spec import WorkflowSpec

    return WorkflowSpec.model_validate({
        "id": "send_message_demo", "name": "Send Message Demo", "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {
            "notify": {
                "type": "send_message",
                "platform": "local",
                "target": "${ input.target }",
                "message": {"text": "${ input.body }"},
                "not_before_seconds": 30,
            },
        },
    })


def _start_mission_send_execution(tmp_path, monkeypatch, *, receipts_mode):
    home = tmp_path / ".hades"
    monkeypatch.setenv("HADES_HOME", str(home))
    wfdb.init_db()
    if receipts_mode is not None:
        (home / "config.yaml").write_text(
            f"receipts:\n  mode: {receipts_mode}\n", encoding="utf-8"
        )
    with wfdb.connect() as conn:
        spec = _send_message_spec()
        wfdb.deploy_definition(conn, spec, created_by="test")
        mission, execution = mdb.create_mission_and_execution(
            conn,
            workflow_id=spec.id,
            objective="send a delayed local test notification",
            constraints=[],
            authority={
                "allowed_effects": ["delayed_message"],
                "message_targets": ["authorized-target"],
                "expires_at": 1_000,
            },
            evidence={"checks": ["workflow_succeeded"]},
            input_data={"target": "authorized-target", "body": "ready"},
            profile="default",
            now=10,
        )
    return mission.mission_id, execution.execution_id, home / "state.db"


def _node_run(exec_id: str, node_id: str) -> dict:
    with wfdb.connect() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM workflow_node_runs WHERE execution_id = ? "
            "AND node_id = ? ORDER BY id",
            (exec_id, node_id),
        )]
    assert len(rows) == 1
    return rows[0]


def _set_effect_phase(state_db: SessionDB, transaction_id: str, phase: str) -> None:
    """Walk the real CAS phase graph to ``phase`` (no direct UPDATE)."""
    paths = {
        "unknown_effect": ("previewed", "committing", "unknown_effect"),
    }
    effect = state_db.get_effect_transaction(transaction_id)
    assert effect is not None
    current = effect.phase
    for next_phase in paths[phase]:
        if current == next_phase:
            continue
        assert state_db.transition_effect_transaction(
            transaction_id, expected_phase=current, next_phase=next_phase
        )
        current = next_phase


def _terminalize_outbox_unknown(state_db_path: Path, outbox_id: str) -> None:
    state_db = SessionDB(db_path=state_db_path)
    try:
        store = MissionOutboxStore(state_db)
        claimed = store.claim(
            now=131, owner_id="receipt-test", lease_seconds=60, limit=50
        )
        target = next(row for row in claimed if row.outbox_id == outbox_id)
        assert store.mark_unknown(
            outbox_id,
            owner_id="receipt-test",
            claim_token=target.claim_token,
            result={"reason": "router timeout"},
        )
        outbox = state_db.get_outbox_by_id(outbox_id)
        assert outbox is not None and outbox.transaction_id is not None
        _set_effect_phase(state_db, outbox.transaction_id, "unknown_effect")
    finally:
        state_db.close()


def _drive_unknown_terminalization(tmp_path, monkeypatch, *, receipts_mode):
    """Full real path: materialize the outbox, make delivery unknown,
    then let the dispatcher reconcile/terminalize the mission."""
    mission_id, exec_id, state_db_path = _start_mission_send_execution(
        tmp_path, monkeypatch, receipts_mode=receipts_mode
    )
    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1
    outbox_id = _node_run(exec_id, "notify")["outbox_id"]
    assert outbox_id
    _terminalize_outbox_unknown(state_db_path, outbox_id)
    assert workflows_dispatcher.tick(
        limit=1, now=131, state_db_path=state_db_path
    ) == 0
    return mission_id, exec_id, outbox_id, state_db_path


def _mission_receipt_rows(state_db_path: Path, mission_id: str) -> list[dict]:
    conn = sqlite3.connect(state_db_path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT receipt_id, source_kind, source_id, status FROM receipts "
            "WHERE source_kind = 'mission' AND source_id = ?",
            (mission_id,),
        )]
    finally:
        conn.close()


def _all_receipt_count(state_db_path: Path) -> int:
    conn = sqlite3.connect(state_db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    finally:
        conn.close()


def _get_mission(mission_id: str) -> mdb.MissionRecord:
    with wfdb.connect() as conn:
        return mdb.get_mission(conn, mission_id)


# ── capture: receipt before verdict projection ──────────────────────────


def test_capture_mode_issues_one_receipt_and_projects_it(tmp_path, monkeypatch):
    mission_id, _exec_id, _outbox_id, state_db_path = (
        _drive_unknown_terminalization(tmp_path, monkeypatch, receipts_mode="capture")
    )

    # (a) exactly one canonical receipt with source ("mission", mission_id).
    rows = _mission_receipt_rows(state_db_path, mission_id)
    assert len(rows) == 1
    assert _all_receipt_count(state_db_path) == 1
    receipt_row = rows[0]
    assert receipt_row["source_kind"] == "mission"
    assert receipt_row["source_id"] == mission_id
    assert receipt_row["status"] in RECEIPT_STATUSES

    # (b) missions.receipt_id and verdict are projected and match the
    # receipt; the existing blocked-status behavior is preserved.
    mission = _get_mission(mission_id)
    assert mission.status == "blocked"
    assert mission.receipt_id == receipt_row["receipt_id"]
    assert mission.verdict == receipt_row["status"]

    # The projection is the CAS receipt projection, not a bare verdict:
    # the linked receipt is loadable through the canonical store.
    state_db = SessionDB(db_path=state_db_path)
    try:
        receipt = ReceiptStore(state_db).find_by_source(
            ReceiptSourceKey("mission", mission_id)
        )
    finally:
        state_db.close()
    assert receipt is not None
    assert receipt.receipt_id == mission.receipt_id
    assert receipt.mission_id == mission_id
    assert receipt.status == mission.verdict


def test_capture_mode_reconciliation_rerun_is_idempotent(tmp_path, monkeypatch):
    mission_id, _exec_id, outbox_id, state_db_path = (
        _drive_unknown_terminalization(tmp_path, monkeypatch, receipts_mode="capture")
    )
    first = _get_mission(mission_id)
    assert first.receipt_id is not None

    # (c) a dispatcher rerun and a direct reconciliation replay both leave
    # exactly one receipt and the same projection.
    assert workflows_dispatcher.tick(
        limit=1, now=132, state_db_path=state_db_path
    ) == 0

    state_db = SessionDB(db_path=state_db_path)
    try:
        outbox = MissionOutboxStore(state_db).get_by_id(outbox_id)
    finally:
        state_db.close()
    assert outbox is not None
    with wfdb.connect() as conn:
        mission = mdb.get_mission(conn, mission_id)
        workflows_dispatcher._reconcile_unknown_mission(
            conn,
            mission,
            outbox=outbox,
            error={"reason": "unknown_effect"},
            now=133,
            state_db_path=state_db_path,
            workflow_db_path=None,
        )

    rows = _mission_receipt_rows(state_db_path, mission_id)
    assert len(rows) == 1
    assert _all_receipt_count(state_db_path) == 1
    after = _get_mission(mission_id)
    assert after.receipt_id == first.receipt_id
    assert after.verdict == first.verdict
    assert after.status == "blocked"


# ── off: byte-identical pre-wiring behavior ──────────────────────────────


def test_off_mode_writes_no_receipt_and_keeps_plain_verdict(tmp_path, monkeypatch):
    mission_id, _exec_id, _outbox_id, state_db_path = (
        _drive_unknown_terminalization(tmp_path, monkeypatch, receipts_mode="off")
    )

    # (d) zero receipt rows; the pre-wiring set_mission_verdict path ran.
    assert _all_receipt_count(state_db_path) == 0
    mission = _get_mission(mission_id)
    assert mission.status == "blocked"
    assert mission.verdict == "unknown_effect"
    assert mission.receipt_id is None


def test_absent_config_defaults_to_off(tmp_path, monkeypatch):
    mission_id, _exec_id, _outbox_id, state_db_path = (
        _drive_unknown_terminalization(tmp_path, monkeypatch, receipts_mode=None)
    )
    assert _all_receipt_count(state_db_path) == 0
    mission = _get_mission(mission_id)
    assert mission.verdict == "unknown_effect"
    assert mission.receipt_id is None


# ── crash between receipt insert and projection ──────────────────────────


def test_crash_before_projection_recovers_same_receipt(tmp_path, monkeypatch):
    mission_id, exec_id, state_db_path = _start_mission_send_execution(
        tmp_path, monkeypatch, receipts_mode="capture"
    )
    assert workflows_dispatcher.tick(
        limit=1, now=100, state_db_path=state_db_path
    ) == 1
    outbox_id = _node_run(exec_id, "notify")["outbox_id"]
    _terminalize_outbox_unknown(state_db_path, outbox_id)

    source = ReceiptSourceKey("mission", mission_id)
    wf_path = wfdb.workflows_db_path()

    # (e) crash simulation: the receipt lands in state.db but the process
    # dies before any consumer projection.
    state_db = SessionDB(db_path=state_db_path)
    try:
        crashing = build_receipt_issuer(
            state_db, workflows_db_path=wf_path, profile="default"
        )
        crashing._project = lambda _source: None  # crash seam: skip projection
        issued = crashing.issue(source)
    finally:
        state_db.close()
    assert issued.status in RECEIPT_STATUSES
    assert _get_mission(mission_id).receipt_id is None
    assert _all_receipt_count(state_db_path) == 1

    # Recovery in a fresh object graph links the SAME receipt — never a
    # duplicate, never a replacement.
    state_db = SessionDB(db_path=state_db_path)
    try:
        recovering = build_receipt_issuer(
            state_db, workflows_db_path=wf_path, profile="default"
        )
        recovered = recovering.recover_projection(source)
    finally:
        state_db.close()
    assert recovered is not None
    assert recovered.receipt_id == issued.receipt_id
    assert _all_receipt_count(state_db_path) == 1
    assert _get_mission(mission_id).receipt_id == issued.receipt_id
