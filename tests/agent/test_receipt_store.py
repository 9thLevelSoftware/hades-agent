"""Tests for the canonical immutable receipt store (Task 2).

Covers the frozen ``ReceiptStore.insert/append_observation/get/
find_by_source/list`` API against a real profile-local ``SessionDB``:

- ``verified`` inserts require a matching sealed scorer decision; a
  missing, mismatched, or forged seal is rejected with ``PermissionError``.
- Source ingestion is idempotent by ``(source_kind, source_id)`` and
  content hash; reusing a source identity with different content is a
  conflict, never an update.
- Receipts and observations are immutable: no public mutation methods
  exist and direct SQL ``UPDATE``/``DELETE`` is aborted by triggers.
- Recheck observations append in a CAS-protected chain and never rewrite
  the original receipt.
- Rows survive process-style restarts (fresh object graphs and a real
  second OS process) without duplicate issuance.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from hades_state import SessionDB
from agent.receipt_hashing import canonical_content_hash
from agent.receipt_models import (
    _VERIFIED_DECISION_CAPABILITY,
    _build_verified_decision,
    ReceiptQuery,
    VerifiedReceiptDecision,
    build_claim,
    build_evidence_digest,
    build_observation,
    build_receipt,
    build_requested_outcome,
)
from agent.receipt_store import (
    ReceiptIntegrityError,
    ReceiptObservationConflict,
    ReceiptSourceConflict,
    ReceiptStore,
    ReceiptStoreError,
)
from agent.receipts import ReceiptSourceKey

_REPO_ROOT = Path(__file__).resolve().parents[2]

DECIDED_AT = "2026-07-16T12:00:00Z"


def _make_receipt(
    *,
    source_kind: str = "turn",
    source_id: str = "s1:t1",
    session_id: str = "s1",
    turn_id: str = "t1",
    status: str = "completed_unverified",
    statement: str = "README contains marker",
    scorer_id: str = "hermes.receipts.default",
    decided_at: str = DECIDED_AT,
):
    evidence = build_evidence_digest(
        evidence_kind="verification_check",
        source_ref="verification_evidence.db:check-1",
        producer_id="hermes.verification",
        observed_at="2026-07-16T11:59:00Z",
        summary="pytest passed after final edit",
        payload_hash=canonical_content_hash({"check": "pytest", "result": "pass"}),
    )
    claim = build_claim(
        statement=statement,
        evidence_ids=(evidence.evidence_id,),
        verdict="satisfied",
    )
    outcome = build_requested_outcome(
        outcome_kind="code_change",
        description="add marker to README",
        producer_id="hermes.turn-ledger",
    )
    return build_receipt(
        source=ReceiptSourceKey(source_kind, source_id),
        subject_kind="turn",
        subject_id=source_id,
        session_id=session_id,
        turn_id=turn_id,
        requested_outcome=outcome,
        status=status,
        claims=(claim,),
        evidence=(evidence,),
        scorer_id=scorer_id,
        scorer_version="1.0",
        decided_at=decided_at,
    )


def _seal_for(receipt, **overrides) -> VerifiedReceiptDecision:
    kwargs = dict(
        scorer_id=receipt.scorer_id,
        scorer_version=receipt.scorer_version,
        subject_kind=receipt.subject_kind,
        subject_id=receipt.subject_id,
        snapshot_hash=canonical_content_hash({"snapshot": receipt.subject_id}),
        claim_hashes=tuple(c.content_hash for c in receipt.claims),
        decided_at=receipt.decided_at,
        fresh_until=None,
    )
    kwargs.update(overrides)
    return _build_verified_decision(_VERIFIED_DECISION_CAPABILITY, **kwargs)


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def store(db):
    return ReceiptStore(db)


@pytest.fixture()
def completed_receipt():
    return _make_receipt()


@pytest.fixture()
def verified_receipt():
    return _make_receipt(
        source_id="s1:t2",
        turn_id="t2",
        status="verified",
        scorer_id="hermes.code-turn-end-state",
    )


# =========================================================================
# Verified seal enforcement
# =========================================================================


def test_verified_insert_requires_matching_scorer_seal(store, verified_receipt):
    with pytest.raises(PermissionError, match="scorer decision"):
        store.insert(verified_receipt)


def test_verified_insert_with_matching_seal_persists(store, verified_receipt):
    stored = store.insert(verified_receipt, decision=_seal_for(verified_receipt))
    assert stored.status == "verified"
    reloaded = store.get(stored.receipt_id)
    assert reloaded == verified_receipt
    # Identical source replay returns the sealed receipt without re-minting.
    assert store.insert(verified_receipt) == stored


def test_verified_seal_with_wrong_claim_hashes_is_rejected(store, verified_receipt):
    bad = _seal_for(
        verified_receipt,
        claim_hashes=(canonical_content_hash({"other": 1}),),
    )
    with pytest.raises(PermissionError, match="scorer decision"):
        store.insert(verified_receipt, decision=bad)


def test_verified_seal_with_wrong_scorer_identity_is_rejected(store, verified_receipt):
    bad = _seal_for(verified_receipt, scorer_id="someone.else")
    with pytest.raises(PermissionError, match="scorer decision"):
        store.insert(verified_receipt, decision=bad)


def test_forged_decision_object_fails_store_validation(store, verified_receipt):
    forged = object.__new__(VerifiedReceiptDecision)
    for name, value in {
        "scorer_id": verified_receipt.scorer_id,
        "scorer_version": verified_receipt.scorer_version,
        "subject_kind": verified_receipt.subject_kind,
        "subject_id": verified_receipt.subject_id,
        "snapshot_hash": canonical_content_hash({"snapshot": "forged"}),
        "claim_hashes": tuple(c.content_hash for c in verified_receipt.claims),
        "decided_at": verified_receipt.decided_at,
        "fresh_until": None,
        "decision_hash": "sha256:" + "f" * 64,
    }.items():
        object.__setattr__(forged, name, value)
    with pytest.raises(PermissionError, match="scorer decision"):
        store.insert(verified_receipt, decision=forged)


def test_nonverified_receipt_never_receives_a_seal(store, completed_receipt):
    seal = _seal_for(completed_receipt)
    with pytest.raises(ReceiptIntegrityError, match="seal"):
        store.insert(completed_receipt, decision=seal)


# =========================================================================
# Idempotent source ingestion and conflicts
# =========================================================================


def test_source_replay_is_idempotent_but_conflict_is_rejected(store, completed_receipt):
    first = store.insert(completed_receipt)
    assert store.insert(completed_receipt) == first
    with pytest.raises(ReceiptSourceConflict):
        store.insert(replace(completed_receipt, content_hash="sha256:" + "0" * 64))


def test_insert_rejects_tampered_nested_claim_content(store, completed_receipt):
    tampered_claim = replace(completed_receipt.claims[0], statement="tampered")
    receipt = build_receipt(
        source=ReceiptSourceKey("turn", "s1:t9"),
        subject_kind="turn",
        subject_id="s1:t9",
        session_id="s1",
        turn_id="t9",
        requested_outcome=completed_receipt.requested_outcome,
        status="completed_unverified",
        claims=(tampered_claim,),
        evidence=completed_receipt.evidence,
        scorer_id="hermes.receipts.default",
        scorer_version="1.0",
        decided_at=DECIDED_AT,
    )
    with pytest.raises(ReceiptIntegrityError):
        store.insert(receipt)


def test_insert_rejects_tampered_receipt_hash_for_new_source(store):
    receipt = _make_receipt(source_id="s1:t3", turn_id="t3")
    with pytest.raises(ReceiptIntegrityError):
        store.insert(replace(receipt, content_hash="sha256:" + "1" * 64))


def test_insert_survives_reopen_in_fresh_object_graph(tmp_path, completed_receipt):
    db_path = tmp_path / "state.db"
    first_db = SessionDB(db_path=db_path)
    try:
        first = ReceiptStore(first_db).insert(completed_receipt)
    finally:
        first_db.close()

    second_db = SessionDB(db_path=db_path)
    try:
        second_store = ReceiptStore(second_db)
        assert second_store.get(first.receipt_id) == completed_receipt
        assert second_store.find_by_source(completed_receipt.source) == completed_receipt
        # Replay after restart returns the stored receipt, not a duplicate.
        assert second_store.insert(completed_receipt) == first
        count = second_db._conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        assert count == 1
    finally:
        second_db.close()


_CHILD_REPLAY_SCRIPT = '''
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])

from hades_state import SessionDB
from agent.receipt_hashing import canonical_content_hash
from agent.receipt_models import (
    build_claim,
    build_evidence_digest,
    build_receipt,
    build_requested_outcome,
)
from agent.receipt_store import ReceiptStore
from agent.receipts import ReceiptSourceKey


def make_receipt():
    evidence = build_evidence_digest(
        evidence_kind="verification_check",
        source_ref="verification_evidence.db:check-1",
        producer_id="hermes.verification",
        observed_at="2026-07-16T11:59:00Z",
        summary="pytest passed after final edit",
        payload_hash=canonical_content_hash({"check": "pytest", "result": "pass"}),
    )
    claim = build_claim(
        statement="README contains marker",
        evidence_ids=(evidence.evidence_id,),
        verdict="satisfied",
    )
    outcome = build_requested_outcome(
        outcome_kind="code_change",
        description="add marker to README",
        producer_id="hermes.turn-ledger",
    )
    return build_receipt(
        source=ReceiptSourceKey("turn", "s1:t1"),
        subject_kind="turn",
        subject_id="s1:t1",
        session_id="s1",
        turn_id="t1",
        requested_outcome=outcome,
        status="completed_unverified",
        claims=(claim,),
        evidence=(evidence,),
        scorer_id="hermes.receipts.default",
        scorer_version="1.0",
        decided_at="2026-07-16T12:00:00Z",
    )


db = SessionDB(db_path=Path(sys.argv[1]))
try:
    stored = ReceiptStore(db).insert(make_receipt())
    print(stored.receipt_id)
finally:
    db.close()
'''


def test_two_process_identical_replay_returns_existing_receipt(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        first = ReceiptStore(db).insert(_make_receipt())
    finally:
        db.close()

    script = tmp_path / "replay_child.py"
    script.write_text(_CHILD_REPLAY_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), str(db_path), str(_REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
        env=os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == first.receipt_id

    reopened = SessionDB(db_path=db_path)
    try:
        count = reopened._conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        assert count == 1
    finally:
        reopened.close()


# =========================================================================
# Immutability: no public mutation, SQL mutation aborted by triggers
# =========================================================================


def test_store_exposes_no_update_or_delete_methods(store):
    public = {name for name in dir(store) if not name.startswith("_")}
    assert not any("update" in name.lower() for name in public)
    assert not any("delete" in name.lower() for name in public)


def test_direct_sql_update_and_delete_are_aborted(store, db, completed_receipt):
    stored = store.insert(completed_receipt)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db._conn.execute(
            "UPDATE receipts SET status = 'verified' WHERE receipt_id = ?",
            (stored.receipt_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="tombstone"):
        db._conn.execute(
            "DELETE FROM receipts WHERE receipt_id = ?",
            (stored.receipt_id,),
        )
    # Row unchanged.
    assert store.get(stored.receipt_id) == completed_receipt


# =========================================================================
# Observations: append-only CAS chain
# =========================================================================


def _make_observation(receipt_id, *, previous=None, status="failed",
                      observed_at="2026-07-16T13:00:00Z"):
    return build_observation(
        receipt_id=receipt_id,
        previous_observation_id=previous,
        status=status,
        uncertainty=("artifact missing on recheck",),
        scorer_id="hermes.receipts.recheck",
        scorer_version="1.0",
        observed_at=observed_at,
    )


def test_append_observation_chains_and_rejects_forks(store, completed_receipt):
    stored = store.insert(completed_receipt)
    first = _make_observation(stored.receipt_id)
    assert store.append_observation(first) == first
    # Identical replay is idempotent.
    assert store.append_observation(first) == first

    second = _make_observation(
        stored.receipt_id,
        previous=first.observation_id,
        observed_at="2026-07-16T14:00:00Z",
    )
    store.append_observation(second)
    chain = store.observations(stored.receipt_id)
    assert [o.observation_id for o in chain] == [
        first.observation_id,
        second.observation_id,
    ]

    fork = _make_observation(
        stored.receipt_id,
        previous=None,
        status="blocked",
        observed_at="2026-07-16T15:00:00Z",
    )
    with pytest.raises(ReceiptObservationConflict):
        store.append_observation(fork)

    # The original receipt is never rewritten by rechecks.
    assert store.get(stored.receipt_id) == completed_receipt


def test_observation_requires_existing_receipt(store):
    orphan = _make_observation("rct_" + "0" * 64)
    with pytest.raises(ReceiptStoreError, match="unknown receipt"):
        store.append_observation(orphan)


def test_verified_observation_requires_seal(store, completed_receipt):
    stored = store.insert(completed_receipt)
    obs = build_observation(
        receipt_id=stored.receipt_id,
        status="verified",
        scorer_id="hermes.code-turn-end-state",
        scorer_version="1.0",
        observed_at="2026-07-16T16:00:00Z",
    )
    with pytest.raises(PermissionError, match="scorer decision"):
        store.append_observation(obs)

    seal = _build_verified_decision(
        _VERIFIED_DECISION_CAPABILITY,
        scorer_id=obs.scorer_id,
        scorer_version=obs.scorer_version,
        subject_kind=stored.subject_kind,
        subject_id=stored.subject_id,
        snapshot_hash=canonical_content_hash({"snapshot": stored.subject_id}),
        claim_hashes=(),
        decided_at=obs.observed_at,
        fresh_until=None,
    )
    appended = store.append_observation(obs, decision=seal)
    assert appended.status == "verified"


# =========================================================================
# Queries
# =========================================================================


def test_get_and_find_by_source_return_none_when_absent(store):
    assert store.get("rct_" + "0" * 64) is None
    assert store.find_by_source(ReceiptSourceKey("turn", "nope")) is None


def test_list_filters_by_status_and_session(store):
    completed = _make_receipt()
    failed = _make_receipt(
        source_id="s2:t1",
        session_id="s2",
        status="failed",
        statement="delivery landed",
    )
    store.insert(completed)
    store.insert(failed)

    by_status = store.list(ReceiptQuery(status="failed"))
    assert [s.receipt_id for s in by_status] == [failed.receipt_id]
    assert by_status[0].status == "failed"
    assert by_status[0].source == failed.source

    by_session = store.list(ReceiptQuery(session_id="s1"))
    assert [s.receipt_id for s in by_session] == [completed.receipt_id]

    everything = store.list(ReceiptQuery())
    assert {s.receipt_id for s in everything} == {
        completed.receipt_id,
        failed.receipt_id,
    }


def test_facade_forwards_receipt_store():
    import agent.receipts as receipts_facade

    assert receipts_facade.ReceiptStore is ReceiptStore
