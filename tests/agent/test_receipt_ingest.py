"""Tests for deduplicated evidence snapshots from existing truth sources.

Task 4 of the Verified Outcome & Artifact Receipts plan: the three
read-only evidence source adapters (turn, mission, transaction), the
normalized immutable :class:`EvidenceSnapshot` builder, and the
idempotent :class:`ReceiptIngestor` issue/recovery seam.

Key invariants proven here:

- A turn ledger ``verified`` outcome is an untrusted source claim: the
  turn snapshot records it as ``turn_classification`` evidence and keeps
  the requested-end-state claim ``unknown``.
- Mission and transaction snapshots resolve artifacts through the one
  content-addressed catalog; identical bytes never gain a second digest.
- Snapshots are deterministic for the same durable facts regardless of
  row order and across full process-style restarts.
- "No evidence" is a durable ``absence_observed`` digest, never a
  dangling reference.
- Issue is idempotent by source; changed content for a terminal source
  is a conflict, never a replacement receipt. ``recover_projection()``
  CAS-links an already-inserted receipt after a crash.

The mission/transaction tables do not exist in this checkout (the
vertical slice lives on another machine), so the fixtures create them
from the schema preregistered in the vertical-slice plan document and
the adapters are exercised against those fixture-backed tables.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.operation_journal import OperationJournal
from agent.receipt_artifacts import ArtifactCatalog
from agent.receipt_hashing import canonical_content_hash, normalize_utc_timestamp
from agent.receipt_ingest import (
    EvidenceSourceError,
    MissionEvidenceSource,
    ReceiptIngestError,
    ReceiptIngestor,
    ReceiptIssuer,
    ReceiptSourceResolver,
    SnapshotConflictError,
    TransactionEvidenceSource,
    TurnEvidenceSource,
    build_absence_evidence,
    build_evidence_snapshot,
    build_receipt_issuer,
    build_verification_evidence_digest,
)
from agent.receipt_models import (
    EvidenceSnapshot,
    ReceiptDecision,
    build_claim,
    build_evidence_digest,
    build_observation as build_observation_record,
    build_operation_evidence,
    build_requested_outcome,
)
from agent.receipt_store import ReceiptStore
from agent.receipts import ReceiptQuery, ReceiptSourceKey
from agent.turn_ledger import TurnOutcomeRecord
from agent.verification_evidence import (
    mark_workspace_edited,
    record_terminal_result,
    session_verification_roots,
    verification_state_for_root,
)
from hades_state import SessionDB

DECIDED_AT = "2026-07-16T12:00:00Z"


# ---------------------------------------------------------------------------
# Fixture-backed vertical-slice tables (schema from the preregistered plan
# document; this clone has no missions/effects implementation).
# ---------------------------------------------------------------------------

_MISSIONS_DDL = (
    """CREATE TABLE IF NOT EXISTS missions (
        mission_id TEXT PRIMARY KEY,
        profile TEXT NOT NULL,
        objective TEXT NOT NULL,
        constraints_json TEXT NOT NULL,
        authority_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        authority_version INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL,
        verdict TEXT,
        receipt_id TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        terminal_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS mission_execution_links (
        mission_id TEXT NOT NULL,
        execution_id TEXT NOT NULL,
        relation TEXT NOT NULL DEFAULT 'primary',
        linked_at INTEGER NOT NULL,
        PRIMARY KEY (mission_id, execution_id)
    )""",
    """CREATE TABLE IF NOT EXISTS mission_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        idempotency_key TEXT,
        created_at INTEGER NOT NULL,
        UNIQUE (mission_id, idempotency_key)
    )""",
    """CREATE TABLE IF NOT EXISTS mission_review_items (
        review_id TEXT PRIMARY KEY,
        mission_id TEXT NOT NULL,
        transaction_id TEXT,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        resolved_at INTEGER
    )""",
)

_EFFECTS_DDL = (
    """CREATE TABLE IF NOT EXISTS effect_transactions (
        transaction_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE REFERENCES agent_operations(operation_id),
        mission_id TEXT NOT NULL,
        execution_id TEXT,
        step_id TEXT,
        adapter_id TEXT NOT NULL,
        sequence_no INTEGER NOT NULL,
        semantics_json TEXT NOT NULL,
        phase TEXT NOT NULL,
        depends_on_json TEXT NOT NULL,
        prepared_json TEXT,
        preview_json TEXT,
        authority_json TEXT,
        result_json TEXT,
        verification_json TEXT,
        compensation_json TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE (mission_id, sequence_no)
    )""",
    """CREATE TABLE IF NOT EXISTS mission_outbox (
        outbox_id TEXT PRIMARY KEY,
        mission_id TEXT,
        execution_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        transaction_id TEXT UNIQUE,
        delivery_id TEXT NOT NULL UNIQUE,
        platform TEXT NOT NULL,
        target TEXT NOT NULL,
        content_json TEXT NOT NULL,
        not_before INTEGER NOT NULL,
        status TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        approval_json TEXT,
        result_json TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""",
)

_T0 = 1752660000  # fixed epoch for durable fixture timestamps


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def artifact_catalog(db):
    return ArtifactCatalog(db)


@pytest.fixture()
def shared_artifact(artifact_catalog):
    return artifact_catalog.register_bytes(
        b"final deliverable bytes",
        source_kind="mission",
        source_ref="m1:artifact",
        display_name="deliverable.txt",
    )


@pytest.fixture()
def workflows_db_path(tmp_path):
    path = tmp_path / "workflows.db"
    conn = sqlite3.connect(path)
    try:
        for statement in _MISSIONS_DDL:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def effect_tables(db):
    def _create(conn):
        for statement in _EFFECTS_DDL:
            conn.execute(statement)

    db._execute_write(_create)
    return db


def record(**overrides) -> TurnOutcomeRecord:
    fields = dict(
        session_id="s1",
        turn_id="t1",
        created_at=float(_T0),
        outcome="completed_unverified",
        outcome_reason="response completed without verification",
        turn_exit_reason="text_response(finish_reason=stop)",
        api_calls=1,
        tool_iterations=1,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=10,
        output_tokens_delta=5,
        cache_read_tokens_delta=0,
        skills_loaded=(),
        model="test-model",
    )
    fields.update(overrides)
    return TurnOutcomeRecord(**fields)


@pytest.fixture()
def turn_source(db):
    return TurnEvidenceSource(db)


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    return ws


@pytest.fixture()
def stale_evidence(workspace):
    """Passed verification followed by a later edit → stale evidence."""
    event = record_terminal_result(
        command="python -m pytest -q",
        cwd=workspace,
        session_id="s1",
        exit_code=0,
        output="all green",
    )
    assert event is not None
    marked = mark_workspace_edited(
        session_id="s1", cwd=workspace, paths=[str(workspace / "calc.py")]
    )
    assert marked is not None
    roots = session_verification_roots("s1")
    assert len(roots) == 1
    state = verification_state_for_root(session_id="s1", root=roots[0])
    assert state["status"] == "stale"
    return build_verification_evidence_digest(state)


def _seed_mission(
    workflows_db_path: Path,
    *,
    mission_id: str = "m1",
    profile: str = "default",
    status: str = "completed",
    evidence: dict | None = None,
    constraints: tuple[str, ...] = ("no purchases",),
) -> None:
    conn = sqlite3.connect(workflows_db_path)
    try:
        conn.execute(
            "INSERT INTO missions (mission_id, profile, objective, "
            "constraints_json, authority_json, evidence_json, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mission_id,
                profile,
                "publish the release notes page",
                json.dumps(list(constraints)),
                json.dumps({"scopes": ["web"]}),
                json.dumps(evidence or {}),
                status,
                _T0,
                _T0 + 60,
            ),
        )
        conn.execute(
            "INSERT INTO mission_execution_links (mission_id, execution_id, "
            "relation, linked_at) VALUES (?, ?, 'primary', ?)",
            (mission_id, "exec-1", _T0),
        )
        conn.execute(
            "INSERT INTO mission_events (mission_id, kind, payload_json, "
            "idempotency_key, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                mission_id,
                "step_completed",
                json.dumps({"step_id": "step-1"}),
                f"{mission_id}:step-1",
                _T0 + 30,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_transaction(
    db,
    *,
    transaction_id: str = "tx1",
    mission_id: str = "m1",
    operation_id: str = "op1",
    phase: str = "committed",
    op_terminal: str = "confirmed",
    result: dict | None = None,
) -> None:
    journal = OperationJournal(db)
    journal.create(
        operation_id=operation_id,
        kind="effect",
        session_id="s1",
        turn_id="t1",
        tool_call_id=f"call-{operation_id}",
        destination="workspace",
        payload_hash="p1",
    )
    journal.transition(
        operation_id,
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    if op_terminal == "confirmed":
        journal.transition(
            operation_id,
            from_states={"running"},
            to_state="confirmed",
            effect_disposition="landed",
        )
    elif op_terminal == "unknown":
        journal.transition(
            operation_id,
            from_states={"running"},
            to_state="unknown",
            effect_disposition="unknown",
        )

    def _insert(conn):
        conn.execute(
            "INSERT INTO effect_transactions (transaction_id, operation_id, "
            "mission_id, execution_id, step_id, adapter_id, sequence_no, "
            "semantics_json, phase, depends_on_json, preview_json, "
            "authority_json, result_json, verification_json, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                transaction_id,
                operation_id,
                mission_id,
                "exec-1",
                "step-1",
                "workspace.file",
                1,
                json.dumps({"reversible": True}),
                phase,
                json.dumps([]),
                json.dumps({"summary": "write deliverable.txt"}),
                json.dumps({"authority_version": 1}),
                json.dumps(result or {}),
                json.dumps({"postcondition": "file exists"}),
                _T0,
                _T0 + 45,
            ),
        )

    db._execute_write(_insert)


@pytest.fixture()
def mission_source(db, workflows_db_path, shared_artifact, artifact_catalog):
    _seed_mission(
        workflows_db_path,
        evidence={
            "artifact_ids": [shared_artifact.artifact_id],
            "before": {"page": "absent"},
            "after": {"page": "published"},
        },
    )
    return MissionEvidenceSource(
        db, workflows_db_path=workflows_db_path, profile="default"
    )


@pytest.fixture()
def transaction_source(db, effect_tables, shared_artifact):
    _seed_transaction(
        db, result={"artifact_ids": [shared_artifact.artifact_id]}
    )
    return TransactionEvidenceSource(db)


def _fake_decide(snapshot: EvidenceSnapshot) -> ReceiptDecision:
    body = {
        "scorer_id": "test.independent-scorer",
        "scorer_version": "1.0",
        "subject_kind": snapshot.subject_kind,
        "subject_id": snapshot.subject_id,
        "snapshot_hash": snapshot.content_hash,
        "claim_hashes": tuple(c.content_hash for c in snapshot.claims),
        "decided_at": DECIDED_AT,
        "fresh_until": None,
    }
    return ReceiptDecision(
        status="completed_unverified",
        scorer_id="test.independent-scorer",
        scorer_version="1.0",
        subject_kind=snapshot.subject_kind,
        subject_id=snapshot.subject_id,
        snapshot_hash=snapshot.content_hash,
        claim_hashes=tuple(c.content_hash for c in snapshot.claims),
        uncertainty=(),
        decided_at=DECIDED_AT,
        fresh_until=None,
        decision_hash=canonical_content_hash(body),
    )


# ---------------------------------------------------------------------------
# Turn source: the ledger label is an untrusted source claim.
# ---------------------------------------------------------------------------


def test_turn_verified_label_is_not_receipt_verification(turn_source, stale_evidence):
    turn_source.db.record_turn_outcome(record(outcome="verified"))
    snapshot = turn_source.snapshot("s1", "t1")
    assert snapshot.source == ReceiptSourceKey("turn", "s1:t1")
    assert snapshot.producer_id == "hermes.turn-ledger"
    assert snapshot.claim("turn-completed").verdict == "satisfied"
    assert snapshot.claim("requested-end-state").verdict == "unknown"
    assert stale_evidence.evidence_id in snapshot.claim("requested-end-state").evidence_ids


def test_turn_snapshot_records_ledger_as_turn_classification_evidence(turn_source):
    turn_source.db.record_turn_outcome(record(outcome="verified"))
    snapshot = turn_source.snapshot("s1", "t1")
    kinds = {e.evidence_kind for e in snapshot.evidence}
    assert "turn_classification" in kinds
    # The untrusted label surfaces as explicit uncertainty, never as truth.
    assert any("untrusted" in u for u in snapshot.uncertainty)


def test_turn_snapshot_missing_turn_row_raises(turn_source):
    with pytest.raises(EvidenceSourceError):
        turn_source.snapshot("s1", "no-such-turn")


def test_turn_snapshot_absent_verification_db_yields_absence_evidence(turn_source):
    turn_source.db.record_turn_outcome(record())
    snapshot = turn_source.snapshot("s1", "t1")
    absent = [e for e in snapshot.evidence if e.evidence_kind == "absence_observed"]
    assert len(absent) == 1
    claim = snapshot.claim("requested-end-state")
    assert absent[0].evidence_id in claim.evidence_ids
    assert claim.verdict == "unknown"


def test_turn_snapshot_stale_verification_is_uncertainty(turn_source, stale_evidence):
    turn_source.db.record_turn_outcome(record())
    snapshot = turn_source.snapshot("s1", "t1")
    assert stale_evidence.evidence_id in {e.evidence_id for e in snapshot.evidence}
    assert any("stale" in u for u in snapshot.uncertainty)


def test_turn_snapshot_unknown_operation_disposition_is_uncertainty(turn_source):
    turn_source.db.record_turn_outcome(record())
    journal = OperationJournal(turn_source.db)
    journal.create(
        operation_id="op-unknown",
        kind="message_send",
        session_id="s1",
        turn_id="t1",
        tool_call_id="call-1",
    )
    journal.transition(
        "op-unknown",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "op-unknown",
        from_states={"running"},
        to_state="unknown",
        effect_disposition="unknown",
    )
    snapshot = turn_source.snapshot("s1", "t1")
    states = {op.operation_id: op for op in snapshot.operation_states}
    assert states["op-unknown"].effect_disposition == "unknown"
    assert any("op-unknown" in u for u in snapshot.uncertainty)


def test_turn_snapshot_failed_and_blocked_map_to_failure_and_blocked_reasons(
    turn_source,
):
    turn_source.db.record_turn_outcome(
        record(turn_id="t-failed", outcome="failed", outcome_reason="turn failed")
    )
    turn_source.db.record_turn_outcome(
        record(turn_id="t-blocked", outcome="blocked", outcome_reason="approval blocked")
    )
    failed = turn_source.snapshot("s1", "t-failed")
    blocked = turn_source.snapshot("s1", "t-blocked")
    assert failed.known_failures
    assert failed.claim("turn-completed").verdict == "unsatisfied"
    assert blocked.blocked_reasons
    assert blocked.claim("turn-completed").verdict == "unsatisfied"


def test_turn_snapshot_is_deterministic_across_restart(tmp_path, workspace):
    record_terminal_result(
        command="python -m pytest -q",
        cwd=workspace,
        session_id="s1",
        exit_code=0,
        output="ok",
    )
    db_path = tmp_path / "state.db"
    first_db = SessionDB(db_path=db_path)
    try:
        first_db.record_turn_outcome(record())
        first = TurnEvidenceSource(first_db).snapshot("s1", "t1")
    finally:
        first_db.close()
    second_db = SessionDB(db_path=db_path)
    try:
        second = TurnEvidenceSource(second_db).snapshot("s1", "t1")
    finally:
        second_db.close()
    assert first.content_hash == second.content_hash
    assert first.claims == second.claims
    assert first.evidence == second.evidence


# ---------------------------------------------------------------------------
# Mission and transaction sources.
# ---------------------------------------------------------------------------


def test_mission_and_transaction_share_artifact_digest_without_duplicate(
    mission_source, transaction_source, artifact_catalog,
):
    mission = mission_source.snapshot("m1")
    transaction = transaction_source.snapshot("tx1")
    assert mission.artifacts[0].artifact_id == transaction.artifacts[0].artifact_id
    assert artifact_catalog.digest_count() == 1


def test_mission_snapshot_missing_row_raises(mission_source):
    with pytest.raises(EvidenceSourceError):
        mission_source.snapshot("no-such-mission")


def test_mission_snapshot_rejects_cross_profile_mission(
    db, workflows_db_path, artifact_catalog
):
    _seed_mission(workflows_db_path, mission_id="m-other", profile="other-profile")
    source = MissionEvidenceSource(
        db, workflows_db_path=workflows_db_path, profile="default"
    )
    with pytest.raises(EvidenceSourceError):
        source.snapshot("m-other")


def test_mission_snapshot_unavailable_when_tables_absent(db, tmp_path):
    source = MissionEvidenceSource(
        db, workflows_db_path=tmp_path / "missing-workflows.db", profile="default"
    )
    with pytest.raises(EvidenceSourceError):
        source.snapshot("m1")


def test_mission_snapshot_carries_constraints_before_after_and_step_ids(
    mission_source,
):
    snapshot = mission_source.snapshot("m1")
    assert snapshot.requested_outcome.constraints == ("no purchases",)
    kinds = {e.evidence_kind for e in snapshot.evidence}
    assert {"mission_record", "before_observation", "after_observation"} <= kinds
    assert any(
        e.evidence_kind == "mission_event" and "step-1" in e.summary
        for e in snapshot.evidence
    )
    assert any(e.evidence_kind == "mission_execution_link" for e in snapshot.evidence)
    assert snapshot.claim("requested-end-state").verdict == "unknown"


def test_mission_snapshot_outbox_ambiguity_is_uncertainty(
    db, workflows_db_path, effect_tables, shared_artifact
):
    _seed_mission(workflows_db_path)
    _seed_transaction(db, result={})

    def _outbox(conn):
        conn.execute(
            "INSERT INTO mission_outbox (outbox_id, mission_id, execution_id, "
            "node_id, transaction_id, delivery_id, platform, target, "
            "content_json, not_before, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ob1", "m1", "exec-1", "node-1", "tx1", "d1", "email",
                "user@example.com", json.dumps({"body": "hi"}), _T0,
                "dispatched", _T0, _T0,
            ),
        )

    db._execute_write(_outbox)
    source = MissionEvidenceSource(
        db, workflows_db_path=workflows_db_path, profile="default"
    )
    snapshot = source.snapshot("m1")
    assert any("outbox" in u for u in snapshot.uncertainty)


def test_mission_snapshot_review_items_become_blocked_reasons(
    db, workflows_db_path, artifact_catalog
):
    _seed_mission(workflows_db_path, mission_id="m-rev", status="review")
    conn = sqlite3.connect(workflows_db_path)
    try:
        conn.execute(
            "INSERT INTO mission_review_items (review_id, mission_id, kind, "
            "status, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("rev-1", "m-rev", "approval", "pending", json.dumps({}), _T0),
        )
        conn.commit()
    finally:
        conn.close()
    source = MissionEvidenceSource(
        db, workflows_db_path=workflows_db_path, profile="default"
    )
    snapshot = source.snapshot("m-rev")
    assert any("rev-1" in reason for reason in snapshot.blocked_reasons)


def test_transaction_snapshot_missing_row_and_absent_tables(db, effect_tables, tmp_path):
    source = TransactionEvidenceSource(db)
    with pytest.raises(EvidenceSourceError):
        source.snapshot("no-such-tx")
    bare = SessionDB(db_path=tmp_path / "bare" / "state.db")
    try:
        with pytest.raises(EvidenceSourceError):
            TransactionEvidenceSource(bare).snapshot("tx1")
    finally:
        bare.close()


def test_transaction_snapshot_carries_lineage_hashes_and_operation_state(
    transaction_source,
):
    snapshot = transaction_source.snapshot("tx1")
    kinds = {e.evidence_kind for e in snapshot.evidence}
    assert {"transaction_record", "transaction_lineage"} <= kinds
    assert "adapter_postcondition" in kinds
    ops = {op.operation_id for op in snapshot.operation_states}
    assert "op1" in ops
    assert snapshot.claim("requested-end-state").verdict == "unknown"


def test_transaction_snapshot_unknown_journal_state_is_uncertainty(
    db, effect_tables, shared_artifact
):
    _seed_transaction(
        db,
        transaction_id="tx-unknown",
        operation_id="op-unknown",
        phase="dispatched",
        op_terminal="unknown",
    )
    snapshot = TransactionEvidenceSource(db).snapshot("tx-unknown")
    assert any("unknown" in u for u in snapshot.uncertainty)
    states = {op.operation_id: op for op in snapshot.operation_states}
    assert states["op-unknown"].effect_disposition == "unknown"


def test_mission_snapshot_deterministic_across_restart(
    tmp_path, workflows_db_path, shared_artifact
):
    db_path = tmp_path / "state.db"
    _seed_mission(workflows_db_path, mission_id="m-det")
    first_db = SessionDB(db_path=db_path)
    try:
        first = MissionEvidenceSource(
            first_db, workflows_db_path=workflows_db_path, profile="default"
        ).snapshot("m-det")
    finally:
        first_db.close()
    second_db = SessionDB(db_path=db_path)
    try:
        second = MissionEvidenceSource(
            second_db, workflows_db_path=workflows_db_path, profile="default"
        ).snapshot("m-det")
    finally:
        second_db.close()
    assert first.content_hash == second.content_hash


# ---------------------------------------------------------------------------
# Snapshot builder: dedupe, ordering, absence, traceability.
# ---------------------------------------------------------------------------


def _builder_parts():
    outcome = build_requested_outcome(
        outcome_kind="code_change",
        description="demo",
        producer_id="hermes.turn-ledger",
    )
    evidence_a = build_evidence_digest(
        evidence_kind="verification_check",
        source_ref="verification_evidence.db:s1:root",
        producer_id="hermes.verification",
        observed_at=DECIDED_AT,
        summary="pytest passed",
        payload_hash=canonical_content_hash({"check": 1}),
    )
    evidence_b = build_evidence_digest(
        evidence_kind="turn_classification",
        source_ref="state.db:turn_outcomes:s1:t1",
        producer_id="hermes.turn-ledger",
        observed_at=DECIDED_AT,
        summary="ledger outcome",
        payload_hash=canonical_content_hash({"outcome": "completed_unverified"}),
    )
    claim = build_claim(
        claim_kind="requested-end-state",
        statement="the requested end state independently holds",
        evidence_ids=(evidence_a.evidence_id,),
        verdict="unknown",
    )
    op = build_operation_evidence(
        operation_id="op1",
        operation_kind="effect",
        state="confirmed",
        effect_disposition="landed",
        source_ref="state.db:agent_operations:op1",
        observed_at=DECIDED_AT,
    )
    return outcome, evidence_a, evidence_b, claim, op


def test_builder_collapses_duplicates_and_ignores_row_order():
    outcome, ev_a, ev_b, claim, op = _builder_parts()
    kwargs = dict(
        source=ReceiptSourceKey("turn", "s1:t1"),
        subject_kind="turn",
        subject_id="s1:t1",
        producer_id="hermes.turn-ledger",
        requested_outcome=outcome,
        captured_at=DECIDED_AT,
    )
    first = build_evidence_snapshot(
        claims=(claim, claim),
        evidence=(ev_a, ev_b, ev_a),
        artifacts=(),
        operation_states=(op, op),
        uncertainty=("b", "a", "a"),
        **kwargs,
    )
    second = build_evidence_snapshot(
        claims=(claim,),
        evidence=(ev_b, ev_a),
        artifacts=(),
        operation_states=(op,),
        uncertainty=("a", "b"),
        **kwargs,
    )
    assert first.content_hash == second.content_hash
    assert len(first.evidence) == 2
    assert len(first.claims) == 1
    assert len(first.operation_states) == 1
    assert [e.evidence_id for e in first.evidence] == sorted(
        e.evidence_id for e in first.evidence
    )


def test_builder_rejects_conflicting_operation_duplicates():
    outcome, ev_a, _, claim, op = _builder_parts()
    conflicting = build_operation_evidence(
        operation_id="op1",
        operation_kind="effect",
        state="failed",
        effect_disposition="none",
        source_ref="state.db:agent_operations:op1",
        observed_at=DECIDED_AT,
    )
    with pytest.raises(ReceiptIngestError):
        build_evidence_snapshot(
            source=ReceiptSourceKey("turn", "s1:t1"),
            subject_kind="turn",
            subject_id="s1:t1",
            producer_id="hermes.turn-ledger",
            requested_outcome=outcome,
            claims=(claim,),
            evidence=(ev_a,),
            artifacts=(),
            operation_states=(op, conflicting),
            captured_at=DECIDED_AT,
        )


def test_builder_requires_every_claim_to_cite_existing_evidence():
    outcome, ev_a, _, _, _ = _builder_parts()
    orphan = build_claim(
        claim_kind="effect",
        statement="an effect with no evidence at all",
        evidence_ids=(),
        verdict="satisfied",
    )
    with pytest.raises(ReceiptIngestError):
        build_evidence_snapshot(
            source=ReceiptSourceKey("turn", "s1:t1"),
            subject_kind="turn",
            subject_id="s1:t1",
            producer_id="hermes.turn-ledger",
            requested_outcome=outcome,
            claims=(orphan,),
            evidence=(ev_a,),
            artifacts=(),
            captured_at=DECIDED_AT,
        )


def test_absence_evidence_is_durable_not_dangling():
    absence = build_absence_evidence(
        scope="verification:s1:t1",
        source_ref="verification_evidence.db",
        producer_id="hermes.turn-ledger",
        observed_at=DECIDED_AT,
    )
    assert absence.evidence_kind == "absence_observed"
    assert absence.evidence_id.startswith("evd_")
    again = build_absence_evidence(
        scope="verification:s1:t1",
        source_ref="verification_evidence.db",
        producer_id="hermes.turn-ledger",
        observed_at=DECIDED_AT,
    )
    assert absence == again


# ---------------------------------------------------------------------------
# ReceiptIngestor: idempotent issue and projection recovery.
# ---------------------------------------------------------------------------


def test_issue_is_idempotent_for_identical_source(turn_source, db):
    turn_source.db.record_turn_outcome(record())
    store = ReceiptStore(db)
    ingestor = ReceiptIngestor(store, decide=_fake_decide)
    first = ingestor.issue(turn_source.snapshot("s1", "t1"))
    second = ingestor.issue(turn_source.snapshot("s1", "t1"))
    assert first.receipt_id == second.receipt_id
    assert first == second
    assert len(store.list(ReceiptQuery())) == 1
    assert first.status == "completed_unverified"
    assert first.session_id == "s1"
    assert first.turn_id == "t1"


def test_issue_accepts_bound_source(turn_source, db):
    turn_source.db.record_turn_outcome(record())
    store = ReceiptStore(db)
    ingestor = ReceiptIngestor(store, decide=_fake_decide)
    receipt = ingestor.issue(turn_source.bind("s1", "t1"))
    assert store.find_by_source(ReceiptSourceKey("turn", "s1:t1")) == receipt


def test_issue_changed_terminal_source_is_conflict_not_replacement(turn_source, db):
    turn_source.db.record_turn_outcome(record())
    store = ReceiptStore(db)
    ingestor = ReceiptIngestor(store, decide=_fake_decide)
    original = ingestor.issue(turn_source.snapshot("s1", "t1"))
    # The source identity is reused with different durable content.
    turn_source.db.record_turn_outcome(
        record(outcome="failed", outcome_reason="turn failed")
    )
    with pytest.raises(SnapshotConflictError):
        ingestor.issue(turn_source.snapshot("s1", "t1"))
    assert store.get(original.receipt_id) == original
    assert len(store.list(ReceiptQuery())) == 1


def test_recover_projection_links_mission_receipt_after_crash(
    db, mission_source, workflows_db_path
):
    store = ReceiptStore(db)
    ingestor = ReceiptIngestor(
        store, decide=_fake_decide, workflows_db_path=workflows_db_path
    )
    receipt = ingestor.issue(mission_source.snapshot("m1"))
    assert receipt.mission_id == "m1"
    # Crash happened between receipt insert and mission projection: the
    # missions row still has no receipt_id.
    conn = sqlite3.connect(workflows_db_path)
    try:
        row = conn.execute(
            "SELECT receipt_id FROM missions WHERE mission_id = 'm1'"
        ).fetchone()
        assert row[0] is None
    finally:
        conn.close()
    recovered = ingestor.recover_projection(ReceiptSourceKey("mission", "m1"))
    assert recovered == receipt
    conn = sqlite3.connect(workflows_db_path)
    try:
        row = conn.execute(
            "SELECT receipt_id FROM missions WHERE mission_id = 'm1'"
        ).fetchone()
        assert row[0] == receipt.receipt_id
    finally:
        conn.close()
    # Idempotent: recovery never duplicates the receipt or relinks.
    assert ingestor.recover_projection(ReceiptSourceKey("mission", "m1")) == receipt
    assert len(store.list(ReceiptQuery())) == 1


def test_recover_projection_without_projection_tables_is_safe(turn_source, db):
    turn_source.db.record_turn_outcome(record())
    store = ReceiptStore(db)
    ingestor = ReceiptIngestor(store, decide=_fake_decide)
    receipt = ingestor.issue(turn_source.snapshot("s1", "t1"))
    assert ingestor.recover_projection(ReceiptSourceKey("turn", "s1:t1")) == receipt
    assert ingestor.recover_projection(ReceiptSourceKey("turn", "s9:t9")) is None


# ---------------------------------------------------------------------------
# Task 6: ReceiptIssuer — deterministic issue, observation-only recheck,
# CAS-chained observations, and crash-safe mission projection recovery.
# ---------------------------------------------------------------------------


class InjectedCrash(RuntimeError):
    """Test-injected crash between receipt insert and projection."""


def _ticking_now():
    """Deterministic strictly-increasing decision clock for scoring."""
    base = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    state = {"i": 0}

    def _now() -> str:
        state["i"] += 1
        return normalize_utc_timestamp(base + timedelta(seconds=state["i"]))

    return _now


@pytest.fixture()
def receipt_issuer(db, workflows_db_path, tmp_path):
    """A full ReceiptIssuer over a mission with a file-backed artifact."""
    artifact_root = tmp_path / "deliverables"
    artifact_root.mkdir()
    artifact_path = artifact_root / "deliverable.txt"
    artifact_path.write_text("published release notes v2\n")
    catalog = ArtifactCatalog(db)
    digest = catalog.register_path(
        artifact_path,
        source_kind="mission",
        source_ref="m1:artifact",
        allowed_roots=(artifact_root,),
    )
    _seed_mission(
        workflows_db_path,
        evidence={
            "artifact_ids": [digest.artifact_id],
            "before": {"page": "absent"},
            "after": {"page": "published"},
        },
    )
    issuer = build_receipt_issuer(
        db,
        workflows_db_path=workflows_db_path,
        profile="default",
        allowed_roots=(artifact_root,),
        now=_ticking_now(),
    )
    issuer.fixture = SimpleNamespace(
        artifact_id=digest.artifact_id,
        artifact_path=artifact_path,
        revert_artifact=lambda: artifact_path.write_text(
            "old draft content (reverted)\n"
        ),
        delete_artifact=lambda: artifact_path.unlink(),
    )
    return issuer


def test_recheck_appends_and_never_rewrites_original(receipt_issuer):
    original = receipt_issuer.issue(ReceiptSourceKey("mission", "m1"))
    receipt_issuer.fixture.revert_artifact()
    observation = receipt_issuer.recheck(original.receipt_id)
    assert observation.receipt_id == original.receipt_id
    assert observation.previous_observation_id is None
    assert observation.status == "failed"
    assert receipt_issuer.store.get(original.receipt_id) == original


def test_issue_by_source_key_is_idempotent_and_projects_mission_id(
    receipt_issuer, workflows_db_path
):
    first = receipt_issuer.issue(ReceiptSourceKey("mission", "m1"))
    second = receipt_issuer.issue(ReceiptSourceKey("mission", "m1"))
    assert first == second
    assert first.mission_id == "m1"
    assert len(receipt_issuer.store.list(ReceiptQuery())) == 1
    conn = sqlite3.connect(workflows_db_path)
    try:
        row = conn.execute(
            "SELECT receipt_id FROM missions WHERE mission_id = 'm1'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == first.receipt_id


def test_second_and_third_rechecks_chain_predecessor_ids(receipt_issuer):
    original = receipt_issuer.issue(ReceiptSourceKey("mission", "m1"))
    first = receipt_issuer.recheck(original.receipt_id)
    second = receipt_issuer.recheck(original.receipt_id)
    third = receipt_issuer.recheck(original.receipt_id)
    assert first.previous_observation_id is None
    assert second.previous_observation_id == first.observation_id
    assert third.previous_observation_id == second.observation_id
    chain = receipt_issuer.store.observations(original.receipt_id)
    assert [o.observation_id for o in chain] == [
        first.observation_id,
        second.observation_id,
        third.observation_id,
    ]
    # Earlier observations are immutable — the chain grows, never rewrites.
    assert chain[0] == first
    assert chain[1] == second
    assert receipt_issuer.store.get(original.receipt_id) == original


def test_concurrent_recheck_cas_conflict_retries(receipt_issuer):
    original = receipt_issuer.issue(ReceiptSourceKey("mission", "m1"))
    store = receipt_issuer.store
    real_append = store.append_observation
    state = {"competitor": None}

    def racing_append(observation, *, decision=None):
        if state["competitor"] is None:
            # A concurrent process appends its own observation first.
            competitor = build_observation_record(
                receipt_id=original.receipt_id,
                previous_observation_id=None,
                status="completed_unverified",
                uncertainty=("appended by a concurrent recheck",),
                scorer_id="test.competitor",
                scorer_version="1.0",
                observed_at="2026-07-16T11:00:00Z",
            )
            state["competitor"] = real_append(competitor)
        return real_append(observation, decision=decision)

    store.append_observation = racing_append
    observation = receipt_issuer.recheck(original.receipt_id)
    assert state["competitor"] is not None
    # The CAS conflict was retried and chained after the competitor.
    assert observation.previous_observation_id == (
        state["competitor"].observation_id
    )
    chain = store.observations(original.receipt_id)
    assert [o.observation_id for o in chain] == [
        state["competitor"].observation_id,
        observation.observation_id,
    ]


def test_recheck_missing_artifact_file_is_failed(receipt_issuer):
    original = receipt_issuer.issue(ReceiptSourceKey("mission", "m1"))
    receipt_issuer.fixture.delete_artifact()
    observation = receipt_issuer.recheck(original.receipt_id)
    assert observation.status == "failed"
    assert any("missing" in u for u in observation.uncertainty)
    assert receipt_issuer.store.get(original.receipt_id) == original


def test_recheck_pruned_artifact_locator_is_uncertainty_not_failure(
    receipt_issuer, db
):
    original = receipt_issuer.issue(ReceiptSourceKey("mission", "m1"))
    artifact_id = receipt_issuer.fixture.artifact_id

    def _prune(conn):
        conn.execute(
            "DELETE FROM artifact_locations WHERE artifact_id = ?",
            (artifact_id,),
        )

    db._execute_write(_prune)
    observation = receipt_issuer.recheck(original.receipt_id)
    assert observation.status == "completed_unverified"
    assert any("no recheckable location" in u for u in observation.uncertainty)


def test_recheck_turn_receipt_operation_ambiguity_after_issue(db):
    db.record_turn_outcome(record())
    issuer = build_receipt_issuer(db, now=_ticking_now())
    original = issuer.issue(ReceiptSourceKey("turn", "s1:t1"))
    assert original.status == "completed_unverified"
    journal = OperationJournal(db)
    journal.create(
        operation_id="op-late",
        kind="message_send",
        session_id="s1",
        turn_id="t1",
        tool_call_id="call-late",
    )
    journal.transition(
        "op-late",
        from_states={"pending"},
        to_state="running",
        effect_disposition="none",
    )
    journal.transition(
        "op-late",
        from_states={"running"},
        to_state="unknown",
        effect_disposition="unknown",
    )
    observation = issuer.recheck(original.receipt_id)
    assert observation.status == "unknown_effect"
    assert issuer.store.get(original.receipt_id) == original


def _external_snapshot(subject_id: str = "x1") -> EvidenceSnapshot:
    outcome = build_requested_outcome(
        outcome_kind="external_state",
        description="external subject",
        producer_id="test.external",
    )
    evidence = build_evidence_digest(
        evidence_kind="external_observation",
        source_ref=f"external:{subject_id}",
        producer_id="test.external",
        observed_at=DECIDED_AT,
        summary="external state observed",
        payload_hash=canonical_content_hash({"external": subject_id}),
    )
    claim = build_claim(
        claim_kind="requested-end-state",
        statement="the external end state independently holds",
        evidence_ids=(evidence.evidence_id,),
        verdict="unknown",
    )
    return build_evidence_snapshot(
        source=ReceiptSourceKey("external", subject_id),
        subject_kind="external",
        subject_id=subject_id,
        producer_id="test.external",
        requested_outcome=outcome,
        claims=(claim,),
        evidence=(evidence,),
        captured_at=DECIDED_AT,
    )


class _MutatingAdapter:
    """A domain adapter that wrongly exposes a mutating method."""

    def __init__(self, subject_id: str) -> None:
        self._subject_id = subject_id

    def snapshot(self) -> EvidenceSnapshot:
        return _external_snapshot(self._subject_id)

    def revert_artifact(self):  # pragma: no cover - must never be reached
        raise AssertionError("a mutating adapter method must never run")


def test_recheck_source_view_refuses_mutating_adapter_methods(db):
    resolver = ReceiptSourceResolver(db)
    resolver.register_adapter(
        "external", lambda source_id: _MutatingAdapter(source_id)
    )
    view = resolver.for_key(ReceiptSourceKey("external", "x1"))
    # Read-only snapshot access passes through.
    assert view.snapshot().subject_id == "x1"
    # A mutating adapter method is refused at the recheck seam.
    with pytest.raises(ReceiptIngestError):
        view.revert_artifact


def test_resolver_rejects_sources_without_live_adapter(db):
    resolver = ReceiptSourceResolver(db)
    with pytest.raises(EvidenceSourceError):
        resolver.for_key(ReceiptSourceKey("legacy", "legacy-1"))
    with pytest.raises(EvidenceSourceError):
        resolver.for_key(ReceiptSourceKey("turn", "missing-turn-separator"))


class _MissionCrashHarness:
    """Simulates the mission terminalizer's insert-then-project sequence.

    The vertical-slice mission implementation does not exist in this
    clone; the harness drives the same ReceiptIssuer seam that
    implementation will call, against the fixture-backed tables.
    """

    def __init__(
        self, state_dir: Path, workflows_db_path: Path, artifact_root: Path
    ) -> None:
        self._state_dir = state_dir
        self._workflows_db_path = workflows_db_path
        self._artifact_root = artifact_root
        self.db = SessionDB(db_path=state_dir / "state.db")

    def _issuer(self) -> ReceiptIssuer:
        return build_receipt_issuer(
            self.db,
            workflows_db_path=self._workflows_db_path,
            profile="default",
            allowed_roots=(self._artifact_root,),
            now=_ticking_now(),
        )

    def issue_mission(self, mission_id: str = "m1", crash_at: str | None = None):
        issuer = self._issuer()
        if crash_at == "after_receipt_insert":
            def _crash(source):
                raise InjectedCrash(
                    "crash between receipt insert and mission projection"
                )

            issuer._project = _crash
        return issuer.issue(ReceiptSourceKey("mission", mission_id))

    def reopen(self) -> "_MissionCrashHarness":
        self.db.close()
        self.db = SessionDB(db_path=self._state_dir / "state.db")
        return self

    def recover_mission(self, mission_id: str):
        issuer = self._issuer()
        receipt = issuer.recover_projection(
            ReceiptSourceKey("mission", mission_id)
        )
        conn = sqlite3.connect(self._workflows_db_path)
        try:
            row = conn.execute(
                "SELECT receipt_id FROM missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
        finally:
            conn.close()
        return SimpleNamespace(
            receipt=receipt,
            receipt_count=len(issuer.store.list(ReceiptQuery())),
            mission=SimpleNamespace(receipt_id=row[0] if row else None),
        )


@pytest.fixture()
def harness(tmp_path, workflows_db_path):
    artifact_root = tmp_path / "harness-deliverables"
    artifact_root.mkdir()
    artifact_path = artifact_root / "notes.txt"
    artifact_path.write_text("published notes\n")
    state_dir = tmp_path / "harness-state"
    state_dir.mkdir()
    crash_harness = _MissionCrashHarness(
        state_dir, workflows_db_path, artifact_root
    )
    catalog = ArtifactCatalog(crash_harness.db)
    digest = catalog.register_path(
        artifact_path,
        source_kind="mission",
        source_ref="m1:artifact",
        allowed_roots=(artifact_root,),
    )
    _seed_mission(
        workflows_db_path,
        evidence={"artifact_ids": [digest.artifact_id]},
    )
    yield crash_harness
    crash_harness.db.close()


def test_crash_after_insert_before_mission_projection_reuses_receipt(harness):
    with pytest.raises(InjectedCrash):
        harness.issue_mission(crash_at="after_receipt_insert")
    recovered = harness.reopen().recover_mission("m1")
    assert recovered.receipt_count == 1
    assert recovered.mission.receipt_id == recovered.receipt.receipt_id
