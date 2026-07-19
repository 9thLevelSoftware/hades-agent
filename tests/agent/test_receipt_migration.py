"""Tests for the atomic v1 vertical-slice receipt migration (Task 2).

The provisional vertical-slice ``receipts``/``receipt_observations``
tables (copied verbatim from the approved missions/transactions plan)
are migration INPUT, never a second schema. On first open of a state.db
carrying that shape, ``SessionDB._init_schema()`` must atomically:

- preserve receipt IDs and mission/transaction lineage;
- recompute canonical content hashes;
- keep the original row and old content hash as evidence;
- downgrade legacy ``verified`` to ``completed_unverified`` with explicit
  uncertainty until a current independent scorer rechecks it;
- import ``signature_json`` as an untrusted ``unverified_import``
  provenance attestation that never changes status;
- chain observations by old ``(receipt_id, created_at, observation_id)``
  order;
- roll back to the untouched v1 tables on any failure — there is no
  half-migrated state.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import agent.receipt_store as receipt_store_module
from hades_state import SessionDB
from agent.receipt_models import build_receipt
from agent.receipt_store import ReceiptStore
from agent.receipts import ReceiptSourceKey

# Exact provisional tables from the approved vertical-slice plan
# (docs/superpowers/plans/in progress/
#  2026-07-15-missions-transactions-receipts-vertical-slice.md).
V1_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS effect_transactions (
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
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    status TEXT NOT NULL,
    objective TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    execution_ids_json TEXT NOT NULL,
    transaction_ids_json TEXT NOT NULL,
    before_after_json TEXT NOT NULL,
    claims_json TEXT NOT NULL,
    verifier_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    uncertainty_json TEXT NOT NULL,
    freshness_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    signature_json TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS receipt_observations (
    observation_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL REFERENCES receipts(receipt_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mission_outbox (
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
);
"""

_V1_RECEIPT_SQL = (
    "INSERT INTO receipts (receipt_id, mission_id, status, objective, "
    "constraints_json, execution_ids_json, transaction_ids_json, "
    "before_after_json, claims_json, verifier_json, evidence_json, "
    "artifacts_json, uncertainty_json, freshness_json, content_hash, "
    "signature_json, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_V1_OBSERVATION_SQL = (
    "INSERT INTO receipt_observations (observation_id, receipt_id, status, "
    "evidence_json, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)"
)


def _v1_receipt_row(
    receipt_id,
    *,
    mission_id="m1",
    status="verified",
    objective="Deliver the weekly report",
    transaction_ids=("tx1",),
    content_hash=None,
    signature_json=None,
    created_at=1721000000,
):
    return (
        receipt_id,
        mission_id,
        status,
        objective,
        json.dumps(["no external send"]),
        json.dumps(["ex1"]),
        json.dumps(list(transaction_ids)),
        json.dumps({"before": {"report": "absent"}, "after": {"report": "present"}}),
        json.dumps(
            [
                {
                    "statement": "weekly report delivered to ops channel",
                    "verdict": "satisfied",
                    "required": True,
                }
            ]
        ),
        json.dumps(
            {
                "verifier_id": "workflow.end-state",
                "verifier_version": "0.1",
                "passed": True,
            }
        ),
        json.dumps([{"kind": "file", "path": "report.md"}]),
        json.dumps([{"path": "report.md", "sha256": "ab" * 32}]),
        json.dumps([]),
        json.dumps({"fresh_until": None}),
        content_hash or f"v1hash-{receipt_id}",
        signature_json,
        created_at,
    )


def _seed_v1_db(db_path, *, extra_receipt_rows=(), observation_rows=None):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(V1_TABLES_SQL)
        signature = json.dumps(
            {
                "provider": "legacy-signer",
                "key_id": "k1",
                "algorithm": "ed25519",
                "signature": "c2lnbmF0dXJl",
                "signed_at": 1721000000,
            }
        )
        conn.execute(
            _V1_RECEIPT_SQL,
            _v1_receipt_row("legacy-r1", signature_json=signature),
        )
        conn.execute(
            _V1_RECEIPT_SQL,
            _v1_receipt_row(
                "legacy-r2",
                mission_id="m2",
                status="failed",
                objective="Rotate the API key",
                transaction_ids=("tx2", "tx3"),
                created_at=1721000500,
            ),
        )
        for row in extra_receipt_rows:
            conn.execute(_V1_RECEIPT_SQL, row)
        if observation_rows is None:
            observation_rows = [
                (
                    "legacy-o1",
                    "legacy-r1",
                    "verified",
                    json.dumps([{"kind": "recheck", "result": "pass"}]),
                    "v1hash-o1",
                    1721000100,
                ),
                (
                    "legacy-o2",
                    "legacy-r1",
                    "failed",
                    json.dumps([{"kind": "recheck", "result": "artifact missing"}]),
                    "v1hash-o2",
                    1721000200,
                ),
            ]
        for row in observation_rows:
            conn.execute(_V1_OBSERVATION_SQL, row)
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture()
def v1_state_db(tmp_path):
    return _seed_v1_db(tmp_path / "state.db")


def _table_names(conn):
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _column_names(conn, table):
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


# =========================================================================
# Core migration behavior
# =========================================================================


def test_migrates_v1_verified_as_unverified_until_recheck(v1_state_db):
    db = SessionDB(v1_state_db)
    try:
        migrated = ReceiptStore(db).get("legacy-r1")
        assert migrated.status == "completed_unverified"
        assert "legacy verified status requires independent recheck" in migrated.uncertainty
        assert migrated.receipt_id == "legacy-r1"
        assert ReceiptStore(db).find_by_source(ReceiptSourceKey("legacy", "legacy-r1"))
    finally:
        db.close()


def test_migration_preserves_other_statuses_and_lineage(v1_state_db):
    db = SessionDB(v1_state_db)
    try:
        store = ReceiptStore(db)
        r1 = store.get("legacy-r1")
        r2 = store.get("legacy-r2")

        assert r1.mission_id == "m1"
        assert r1.subject_kind == "mission"
        assert r1.subject_id == "m1"
        # A single legacy transaction ID survives as direct lineage.
        assert r1.transaction_id == "tx1"
        assert r1.source == ReceiptSourceKey("legacy", "legacy-r1")

        assert r2.status == "failed"
        assert "legacy verified status requires independent recheck" not in r2.uncertainty
        assert r2.mission_id == "m2"
        # Multiple transaction IDs cannot be flattened into one column.
        assert r2.transaction_id is None
    finally:
        db.close()


def test_original_fields_and_old_hash_survive_as_evidence(v1_state_db):
    db = SessionDB(v1_state_db)
    try:
        migrated = ReceiptStore(db).get("legacy-r1")
        kinds = {e.evidence_kind for e in migrated.evidence}
        assert "legacy_receipt_row" in kinds
        assert "legacy_content_hash" in kinds
        legacy_hash = next(
            e for e in migrated.evidence if e.evidence_kind == "legacy_content_hash"
        )
        assert "v1hash-legacy-r1" in legacy_hash.summary
        # Every migrated claim is traceable to existing evidence.
        evidence_ids = {e.evidence_id for e in migrated.evidence}
        assert migrated.claims
        for claim in migrated.claims:
            assert claim.evidence_ids
            assert set(claim.evidence_ids) <= evidence_ids
    finally:
        db.close()


def test_migration_recomputes_canonical_hashes(v1_state_db):
    db = SessionDB(v1_state_db)
    try:
        migrated = ReceiptStore(db).get("legacy-r1")
        assert migrated.content_hash.startswith("sha256:")
        assert migrated.content_hash != "v1hash-legacy-r1"
        rebuilt = build_receipt(
            source=migrated.source,
            subject_kind=migrated.subject_kind,
            subject_id=migrated.subject_id,
            session_id=migrated.session_id,
            turn_id=migrated.turn_id,
            mission_id=migrated.mission_id,
            transaction_id=migrated.transaction_id,
            requested_outcome=migrated.requested_outcome,
            status=migrated.status,
            claims=migrated.claims,
            evidence=migrated.evidence,
            artifacts=migrated.artifacts,
            uncertainty=migrated.uncertainty,
            scorer_id=migrated.scorer_id,
            scorer_version=migrated.scorer_version,
            decided_at=migrated.decided_at,
        )
        assert rebuilt.content_hash == migrated.content_hash
    finally:
        db.close()


def test_legacy_signature_imported_as_untrusted_attestation(v1_state_db):
    db = SessionDB(v1_state_db)
    try:
        store = ReceiptStore(db)
        migrated = store.get("legacy-r1")
        attestations = store.list_attestations("legacy-r1")
        assert len(attestations) == 1
        attestation = attestations[0]
        assert attestation.verification_state == "unverified_import"
        assert attestation.provider_id == "legacy-signer"
        assert attestation.target_kind == "receipt"
        assert attestation.target_content_hash == migrated.content_hash
        # A signature never changes truth status: still downgraded.
        assert migrated.status == "completed_unverified"
        # Unsigned rows import no attestation.
        assert store.list_attestations("legacy-r2") == ()
    finally:
        db.close()


def test_migration_chains_observations_in_created_order(v1_state_db):
    db = SessionDB(v1_state_db)
    try:
        store = ReceiptStore(db)
        chain = store.observations("legacy-r1")
        assert [o.observation_id for o in chain] == ["legacy-o1", "legacy-o2"]
        assert chain[0].previous_observation_id is None
        assert chain[1].previous_observation_id == "legacy-o1"
        # Legacy verified observation is downgraded exactly like receipts.
        assert chain[0].status == "completed_unverified"
        assert (
            "legacy verified status requires independent recheck"
            in chain[0].uncertainty
        )
        assert chain[1].status == "failed"
    finally:
        db.close()


def test_migration_is_recorded_and_reopen_is_idempotent(v1_state_db):
    db = SessionDB(v1_state_db)
    try:
        report = json.loads(db.get_meta("receipt_migration_v1"))
        assert report["receipts"] == 2
        assert report["observations"] == 2
    finally:
        db.close()

    reopened = SessionDB(v1_state_db)
    try:
        conn = reopened._conn
        assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM receipt_observations").fetchone()[0]
            == 2
        )
        assert "_receipt_v1_receipts" not in _table_names(conn)
        assert "_receipt_v1_receipt_observations" not in _table_names(conn)
        # Fresh object graph can still decode and revalidate every row.
        assert ReceiptStore(reopened).get("legacy-r1") is not None
    finally:
        reopened.close()


def test_duplicate_identical_v1_bodies_migrate_to_distinct_receipts(tmp_path):
    extra = (
        _v1_receipt_row("legacy-r3", mission_id="m3", status="blocked"),
        _v1_receipt_row("legacy-r4", mission_id="m3", status="blocked"),
    )
    db_path = _seed_v1_db(tmp_path / "state.db", extra_receipt_rows=extra)
    db = SessionDB(db_path)
    try:
        store = ReceiptStore(db)
        r3 = store.get("legacy-r3")
        r4 = store.get("legacy-r4")
        assert r3 is not None and r4 is not None
        # Identical legacy bodies stay distinct receipts: the legacy source
        # identity is part of the canonical hash input.
        assert r3.content_hash != r4.content_hash
        assert r3.status == "blocked" and r4.status == "blocked"
    finally:
        db.close()


def test_migrated_rows_are_immutable(v1_state_db):
    db = SessionDB(v1_state_db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db._conn.execute(
                "UPDATE receipts SET status = 'verified' "
                "WHERE receipt_id = 'legacy-r1'"
            )
    finally:
        db.close()


# =========================================================================
# Interrupted migration: atomic rollback to the untouched v1 shape
# =========================================================================


def test_interrupted_migration_rolls_back_to_v1(v1_state_db, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash mid-migration")

    monkeypatch.setattr(receipt_store_module, "_convert_v1_observation", _boom)

    db = SessionDB(v1_state_db)  # must not raise; receipts stay disabled
    try:
        conn = db._conn
        # v1 shape untouched.
        assert "objective" in _column_names(conn, "receipts")
        names = _table_names(conn)
        assert "_receipt_v1_receipts" not in names
        assert "_receipt_v1_receipt_observations" not in names
        assert "receipt_attestations" not in names
        assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2
        assert db.get_meta("receipt_migration_v1") is None
    finally:
        db.close()

    monkeypatch.undo()

    # A restarted process with working code completes the migration.
    retried = SessionDB(v1_state_db)
    try:
        migrated = ReceiptStore(retried).get("legacy-r1")
        assert migrated is not None
        assert migrated.status == "completed_unverified"
        assert "source_kind" in _column_names(retried._conn, "receipts")
    finally:
        retried.close()


def test_clean_db_gets_canonical_schema_without_migration(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        conn = db._conn
        cols = _column_names(conn, "receipts")
        assert {"source_kind", "source_id", "requested_outcome_json"} <= cols
        assert "objective" not in cols
        assert db.get_meta("receipt_migration_v1") is None
        names = _table_names(conn)
        assert {
            "receipts",
            "receipt_observations",
            "receipt_attestations",
            "receipt_deletion_tombstones",
        } <= names
    finally:
        db.close()
