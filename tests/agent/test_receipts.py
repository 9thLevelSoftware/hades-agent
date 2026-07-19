"""Tests for SessionDB immutable receipts and append-only observations.

A receipt is the durable, content-hash-addressed record of a completed
mission step. Once inserted it is immutable — the accessors must reject
re-inserting an existing receipt_id even with different fields and must
not update receipt rows. Observations extend a receipt over time
without touching the receipt itself.
"""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError
from itertools import product
from typing import Any

import pytest

from agent.receipts import (
    EvidenceManifest,
    MissionEvidenceSnapshot,
    WorkflowEndStateScorer,
    canonical_receipt_json,
    receipt_content_hash,
    validate_evidence_manifest,
)
from hades_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _receipt_kwargs(**overrides):
    base = {
        "receipt_id": "rc-1",
        "mission_id": "m-1",
        "status": "succeeded",
        "objective": "Write README intro",
        "constraints_json": '{"budget":100}',
        "execution_ids_json": '["ex-1"]',
        "transaction_ids_json": '["tx-1"]',
        "before_after_json": json.dumps(
            {"before": {"size": 0}, "after": {"size": 42}}
        ),
        "claims_json": json.dumps([{"claim_id": "c1", "text": "Added intro"}]),
        "verifier_json": json.dumps({"kind": "self_report", "passed": True}),
        "evidence_json": json.dumps({"diff_lines": 3}),
        "artifacts_json": json.dumps(["README.md"]),
        "uncertainty_json": json.dumps({"score": 0.1}),
        "freshness_json": json.dumps({"captured_at": 1.0}),
        "content_hash": "h" * 64,
        "signature_json": json.dumps({"alg": "ed25519", "value": "x"}),
    }
    base.update(overrides)
    return base


def test_insert_receipt_returns_frozen_record_with_canonical_json(db):
    receipt = db.insert_receipt(**_receipt_kwargs())

    assert receipt.receipt_id == "rc-1"
    assert receipt.mission_id == "m-1"
    assert receipt.status == "succeeded"
    assert receipt.objective == "Write README intro"
    assert receipt.before_after == {"before": {"size": 0}, "after": {"size": 42}}
    assert receipt.claims == [{"claim_id": "c1", "text": "Added intro"}]
    assert receipt.evidence == {"diff_lines": 3}
    assert receipt.artifacts == ["README.md"]
    assert receipt.uncertainty == {"score": 0.1}
    assert receipt.freshness == {"captured_at": 1.0}
    assert receipt.content_hash == "h" * 64

    with pytest.raises(FrozenInstanceError):
        receipt.status = "failed"  # type: ignore[misc]


def test_insert_receipt_validates_required_strings_before_sql(db):
    with pytest.raises(ValueError):
        db.insert_receipt(**_receipt_kwargs(receipt_id=""))
    with pytest.raises(ValueError):
        db.insert_receipt(**_receipt_kwargs(content_hash=""))

    count = db._conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    assert count == 0


def test_insert_receipt_rejects_duplicate_receipt_id_even_if_fields_differ(db):
    db.insert_receipt(**_receipt_kwargs())

    with pytest.raises(ValueError):
        db.insert_receipt(
            **_receipt_kwargs(
                receipt_id="rc-1",
                status="failed",
                objective="something else",
                content_hash="z" * 64,
            )
        )

    # The original receipt was not mutated by the failed insert attempt.
    fetched = db.get_receipt("rc-1")
    assert fetched.status == "succeeded"
    assert fetched.objective == "Write README intro"


def test_insert_receipt_rejects_duplicate_content_hash(db):
    db.insert_receipt(**_receipt_kwargs())
    with pytest.raises(ValueError):
        db.insert_receipt(**_receipt_kwargs(receipt_id="rc-2"))

    # Only the original row exists.
    rows = db._conn.execute("SELECT receipt_id FROM receipts").fetchall()
    assert [row[0] for row in rows] == ["rc-1"]


def test_get_receipt_returns_deep_copied_structured_fields(db):
    db.insert_receipt(**_receipt_kwargs())
    r1 = db.get_receipt("rc-1")
    assert r1 is not None

    # Caller-side mutation of the returned structured fields must not
    # bleed into the next read. (Reassigning a frozen dataclass field
    # itself raises FrozenInstanceError — only the deep-copied *contents*
    # are mutable.)
    r1.claims.append({"claim_id": "evil"})
    r1.evidence["diff_lines"] = 999
    r1.before_after["after"]["size"] = 0

    r2 = db.get_receipt("rc-1")
    assert r2 is not None
    assert r2.claims == [{"claim_id": "c1", "text": "Added intro"}]
    assert r2.evidence == {"diff_lines": 3}
    assert r2.before_after == {"before": {"size": 0}, "after": {"size": 42}}


def test_get_receipt_returns_none_for_unknown_id(db):
    assert db.get_receipt("missing") is None


def test_insert_receipt_does_not_update_existing_receipt(db):
    db.insert_receipt(**_receipt_kwargs())
    # Second call with the same id but different content_hash attempts an
    # update and must raise instead of mutating the row in place.
    original_row = db._conn.execute(
        "SELECT * FROM receipts WHERE receipt_id='rc-1'"
    ).fetchone()
    with pytest.raises(ValueError):
        db.insert_receipt(**_receipt_kwargs(content_hash="z" * 64))
    after_row = db._conn.execute(
        "SELECT * FROM receipts WHERE receipt_id='rc-1'"
    ).fetchone()
    assert dict(original_row) == dict(after_row)


def test_append_receipt_observation_is_append_only_and_does_not_mutate_receipt(db):
    db.insert_receipt(**_receipt_kwargs())
    before = db.get_receipt("rc-1")
    receipt_snapshot = copy.deepcopy(before)

    obs1 = db.append_receipt_observation(
        receipt_id="rc-1",
        status="pending",
        evidence={"step": 1},
        content_hash="a" * 64,
    )
    obs2 = db.append_receipt_observation(
        receipt_id="rc-1",
        status="verified",
        evidence={"step": 2},
        content_hash="b" * 64,
    )

    after = db.get_receipt("rc-1")
    assert after == receipt_snapshot

    rows = db.list_receipt_observations("rc-1")
    assert [r.observation_id for r in rows] == [obs1.observation_id, obs2.observation_id]
    assert rows[0].status == "pending"
    assert rows[1].status == "verified"

    # Mutating returned structured fields doesn't affect later reads.
    rows[0].evidence["step"] = 999
    fresh = db.list_receipt_observations("rc-1")
    assert fresh[0].evidence == {"step": 1}


def test_append_receipt_observation_rejects_duplicate_content_hash(db):
    db.insert_receipt(**_receipt_kwargs())
    db.append_receipt_observation(
        receipt_id="rc-1",
        status="pending",
        evidence={"step": 1},
        content_hash="a" * 64,
    )
    with pytest.raises(ValueError):
        db.append_receipt_observation(
            receipt_id="rc-1",
            status="different",
            evidence={"step": 99},
            content_hash="a" * 64,
        )


def test_append_receipt_observation_validates_inputs_before_sql(db):
    with pytest.raises(ValueError):
        db.append_receipt_observation(
            receipt_id="",
            status="pending",
            evidence={"x": 1},
            content_hash="a" * 64,
        )
    with pytest.raises(ValueError):
        db.append_receipt_observation(
            receipt_id="rc-1",
            status="",
            evidence={"x": 1},
            content_hash="a" * 64,
        )
    with pytest.raises(ValueError):
        db.append_receipt_observation(
            receipt_id="rc-1",
            status="pending",
            evidence={"x": 1},
            content_hash="",
        )
    count = db._conn.execute(
        "SELECT COUNT(*) FROM receipt_observations"
    ).fetchone()[0]
    assert count == 0


def test_list_receipt_observations_is_deterministic_by_creation_order(db):
    db.insert_receipt(**_receipt_kwargs())
    ids = []
    for i in range(5):
        ids.append(
            db.append_receipt_observation(
                receipt_id="rc-1",
                status="pending",
                evidence={"i": i},
                content_hash=f"{i:064x}",
            ).observation_id
        )
    rows = db.list_receipt_observations("rc-1")
    assert [r.observation_id for r in rows] == ids
    assert [r.evidence for r in rows] == [{"i": i} for i in range(5)]


def test_reopen_preserves_receipts_and_observations(tmp_path):
    db_path = tmp_path / "state.db"
    first = SessionDB(db_path=db_path)
    first.insert_receipt(**_receipt_kwargs())
    first.append_receipt_observation(
        receipt_id="rc-1",
        status="verified",
        evidence={"step": 1},
        content_hash="a" * 64,
    )
    first.close()

    reopened = SessionDB(db_path=db_path)
    try:
        r = reopened.get_receipt("rc-1")
        assert r is not None
        assert r.status == "succeeded"
        obs = reopened.list_receipt_observations("rc-1")
        assert len(obs) == 1
        assert obs[0].evidence == {"step": 1}
    finally:
        reopened.close()


def test_receipt_timestamps_stored_as_sqlite_integer(db):
    """created_at on receipts and receipt_observations must be integer."""
    db.insert_receipt(**_receipt_kwargs())
    row = db._conn.execute(
        "SELECT typeof(created_at) FROM receipts WHERE receipt_id='rc-1'"
    ).fetchone()
    assert row[0] == "integer"

    db.append_receipt_observation(
        receipt_id="rc-1",
        status="pending",
        evidence={"x": 1},
        content_hash="a" * 64,
    )
    row = db._conn.execute(
        "SELECT typeof(created_at) FROM receipt_observations "
        "WHERE receipt_id='rc-1'"
    ).fetchone()
    assert row[0] == "integer"


def test_insert_receipt_validates_mission_status_objective_before_sql(db):
    """mission_id, status, and objective are non-blank string contracts."""
    with pytest.raises(ValueError):
        db.insert_receipt(**_receipt_kwargs(mission_id=""))
    with pytest.raises(ValueError):
        db.insert_receipt(**_receipt_kwargs(status=""))
    with pytest.raises(ValueError):
        db.insert_receipt(**_receipt_kwargs(objective=""))
    count = db._conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    assert count == 0


def test_insert_receipt_parses_and_reserializes_required_json_strings(db):
    """Pretty-printed caller JSON is canonicalized on the way to storage;
    every required JSON column stored in the row uses the canonical form."""
    db.insert_receipt(**_receipt_kwargs(
        # Pretty-printed / non-canonical caller's JSON.
        constraints_json='{  "budget" : 100 }',
        execution_ids_json='["ex-1","ex-2"]',
        claims_json=json.dumps(
            [{"claim_id": "c1", "text": "Added intro"}],
            indent=2,
        ),
    ))
    rows = {
        col: db._conn.execute(
            f"SELECT {col} FROM receipts WHERE receipt_id='rc-1'"
        ).fetchone()[0]
        for col in (
            "constraints_json",
            "execution_ids_json",
            "claims_json",
        )
    }
    assert rows["constraints_json"] == '{"budget":100}'
    assert rows["execution_ids_json"] == '["ex-1","ex-2"]'
    assert rows["claims_json"] == '[{"claim_id":"c1","text":"Added intro"}]'

    # Read path returns the parsed canonical object — never the raw
    # caller-formatted string and never ``None``.
    r = db.get_receipt("rc-1")
    assert r is not None
    assert r.constraints == {"budget": 100}
    assert r.execution_ids == ["ex-1", "ex-2"]


def test_insert_receipt_rejects_malformed_required_json_before_sql(db):
    """Required JSON columns are validated before SQL — the storage
    layer never persists a value that the read path would later see
    as ``None``."""
    bad = "{not valid json"
    for col in (
        "constraints_json",
        "execution_ids_json",
        "transaction_ids_json",
        "before_after_json",
        "claims_json",
        "verifier_json",
        "evidence_json",
        "artifacts_json",
        "uncertainty_json",
        "freshness_json",
    ):
        with pytest.raises(ValueError):
            db.insert_receipt(**_receipt_kwargs(**{col: bad}))
    count = db._conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    assert count == 0


def test_insert_receipt_rejects_malformed_optional_signature_before_sql(db):
    """The optional signature string is canonicalized too — a malformed
    value raises before SQL, never silently becoming ``None`` on read."""
    with pytest.raises(ValueError):
        db.insert_receipt(**_receipt_kwargs(signature_json="{nope"))
    count = db._conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    assert count == 0

    # A canonical signature round-trips through the storage layer.
    db.insert_receipt(**_receipt_kwargs(signature_json='{"alg":"ed25519","value":"x"}'))
    r = db.get_receipt("rc-1")
    assert r is not None
    assert r.signature == {"alg": "ed25519", "value": "x"}
    stored = db._conn.execute(
        "SELECT signature_json FROM receipts WHERE receipt_id='rc-1'"
    ).fetchone()[0]
    assert stored == '{"alg":"ed25519","value":"x"}'


def test_list_receipt_observations_breaks_timestamp_ties_by_row_identity(db):
    """When two observations share the same integer-second created_at,
    the SQLite rowid (the order rows were inserted) — not the random
    observation_id — is the deterministic tiebreaker."""
    db.insert_receipt(**_receipt_kwargs())

    # Insert three observations in the same integer second. The
    # observation_id is derived from ``int(time.time()*1_000_000)`` so
    # forcing identical seconds is straightforward here; we instead
    # post-update created_at to a single value via direct SQL to make
    # the tiebreaker property observable regardless of microsecond skew.
    ids = []
    for i in range(3):
        ids.append(
            db.append_receipt_observation(
                receipt_id="rc-1",
                status="pending",
                evidence={"i": i},
                content_hash=f"{i:064x}",
            ).observation_id
        )

    # Force identical created_at for all three rows.
    db._execute_write(
        lambda c: c.execute(
            "UPDATE receipt_observations SET created_at = 1000 "
            "WHERE receipt_id = ?",
            ("rc-1",),
        )
    )

    rows = db.list_receipt_observations("rc-1")
    # Insertion order is preserved across reads (rowid tiebreaker).
    assert [r.observation_id for r in rows] == ids


# ---------------------------------------------------------------------------
# Task 6 receipt scorer contract
# ---------------------------------------------------------------------------


_ALL_CHECKS = (
    "workflow_succeeded",
    "all_effects_settled",
    "fresh_verification",
    "artifacts_exist",
    "outbox_confirmed",
)


def _manifest(**overrides: Any) -> EvidenceManifest:
    values: dict[str, Any] = {
        "checks": _ALL_CHECKS,
        "artifact_paths": ("build/output.txt",),
        "outbox_ids": ("outbox-1",),
    }
    values.update(overrides)
    return EvidenceManifest(**values)


def _snapshot(**overrides: Any) -> MissionEvidenceSnapshot:
    values: dict[str, Any] = {
        "mission_id": "mission-receipt-test",
        "objective": "Prove the requested change",
        "constraints": ("stay in scope",),
        "execution_ids": ("execution-1",),
        "transaction_ids": ("transaction-1",),
        "before_after": {"claimed": "after"},
        "claims": {"model": "done"},
        "manifest": _manifest(),
        "execution_statuses": ("succeeded",),
        "authority_blocked": False,
        "review_blocked": False,
        "operation_phases": ("committed",),
        "transaction_phases": ("committed",),
        "outbox_statuses": {"outbox-1": "confirmed"},
        "verification": {
            "status": "passed",
            "timestamp": "2026-07-16T12:00:00+00:00",
            "source": "verification_evidence",
        },
        "artifacts": ({
            "path": "/workspace/build/output.txt",
            "required_path": "build/output.txt",
            "exists": True,
            "within_allowed_root": True,
            "size": 12,
            "sha256": "a" * 64,
            "mtime": 1,
        },),
    }
    values.update(overrides)
    return MissionEvidenceSnapshot(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"execution_statuses": ("failed",)}, "failed"),
        ({"authority_blocked": True}, "blocked"),
        ({"review_blocked": True}, "blocked"),
        ({"operation_phases": ("unknown_effect",)}, "unknown_effect"),
        ({"transaction_phases": ("unknown_effect",)}, "unknown_effect"),
        ({"outbox_statuses": {"outbox-1": "unknown"}}, "unknown_effect"),
        ({"verification": {"status": "unverified"}}, "completed_unverified"),
        ({"verification": {"status": "stale"}}, "completed_unverified"),
        ({"artifacts": ()}, "completed_unverified"),
        ({"outbox_statuses": {"outbox-1": "pending"}}, "completed_unverified"),
        (
            {
                "claims": {"model": "done"},
                "verification": {"status": "unverified"},
            },
            "completed_unverified",
        ),
        ({}, "verified"),
    ],
)
def test_end_state_scorer_truth_table(overrides: dict[str, Any], expected: str) -> None:
    assert WorkflowEndStateScorer().score(_snapshot(**overrides)).status == expected


_FALSE_SUCCESS_CASES: list[dict[str, Any]] = []
for verification_status, artifact_mode, outbox_status, effect_phase in product(
    ("unverified", "stale", "failed", "not_applicable", "missing"),
    ("missing", "outside", "zero_size"),
    ("pending", "failed", "cancelled", "missing"),
    ("pending", "failed", "cancelled"),
):
    if artifact_mode == "missing":
        artifact: tuple[dict[str, Any], ...] = ()
    elif artifact_mode == "outside":
        artifact = ({
            "path": "/outside/output.txt", "required_path": "build/output.txt",
            "exists": True, "within_allowed_root": False, "size": 12,
            "sha256": "b" * 64, "mtime": 1,
        },)
    else:
        artifact = ({
            "path": "/workspace/build/output.txt", "required_path": "build/output.txt",
            "exists": True, "within_allowed_root": True, "size": 0,
            "sha256": "c" * 64, "mtime": 1,
        },)
    _FALSE_SUCCESS_CASES.append({
        "verification": {"status": verification_status},
        "artifacts": artifact,
        "outbox_statuses": {"outbox-1": outbox_status},
        "transaction_phases": (effect_phase,),
    })


@pytest.mark.parametrize("overrides", _FALSE_SUCCESS_CASES[:50])
def test_false_success_corpus_never_returns_verified(overrides: dict[str, Any]) -> None:
    assert WorkflowEndStateScorer().score(_snapshot(**overrides)).status != "verified"


def test_unknown_manifest_check_blocks_mission_start() -> None:
    with pytest.raises(ValueError, match="unsupported evidence check"):
        validate_evidence_manifest({"checks": ["workflow_succeeded", "vibes"]})


def test_canonical_receipt_hash_ignores_signature_and_is_key_order_independent() -> None:
    first = {
        "status": "verified", "evidence": {"b": 2, "a": 1},
        "signature": {"algorithm": "test", "value": "one"},
    }
    second = {
        "evidence": {"a": 1, "b": 2},
        "signature": {"algorithm": "test", "value": "two"}, "status": "verified",
    }
    assert canonical_receipt_json(first) == canonical_receipt_json(second)
    assert receipt_content_hash(first) == receipt_content_hash(second)


def test_issue_receipt_is_deterministic_and_persists_before_projection(tmp_path) -> None:
    from agent.receipts import issue_receipt

    issued = SessionDB(db_path=tmp_path / "issued.db")
    projected: list[tuple[str, str]] = []

    def project(*, receipt_id: str, verdict: str) -> None:
        assert issued.get_receipt(receipt_id) is not None
        projected.append((receipt_id, verdict))

    try:
        first = issue_receipt(_snapshot(), session_db=issued, project_receipt=project)
        second = issue_receipt(_snapshot(), session_db=issued, project_receipt=project)
    finally:
        issued.close()

    assert first.receipt_id == second.receipt_id
    assert first.content_hash == second.content_hash
    assert first.status == "verified"
    assert projected == [(first.receipt_id, "verified")] * 2


def test_recheck_appends_observation_without_mutating_receipt(tmp_path) -> None:
    from agent.receipts import issue_receipt, recheck_receipt

    issued = SessionDB(db_path=tmp_path / "recheck.db")
    try:
        receipt = issue_receipt(_snapshot(), session_db=issued)
        observation = recheck_receipt(
            receipt.receipt_id, _snapshot(verification={"status": "stale"}), session_db=issued,
        )
        persisted = issued.get_receipt(receipt.receipt_id)
        observations = issued.list_receipt_observations(receipt.receipt_id)
    finally:
        issued.close()

    assert persisted is not None
    assert persisted.content_hash == receipt.content_hash
    assert persisted.status == "verified"
    assert observation.status == "completed_unverified"
    assert [item.observation_id for item in observations] == [observation.observation_id]


def test_artifact_observation_hashes_only_allowed_regular_files(tmp_path) -> None:
    from agent.receipts import collect_artifact_evidence

    root = tmp_path / "workspace"
    root.mkdir()
    artifact = root / "build" / "output.txt"
    artifact.parent.mkdir()
    artifact.write_text("proof\n", encoding="utf-8")
    evidence = collect_artifact_evidence(_manifest(), allowed_roots=(root,))

    assert evidence == ({
        "required_path": "build/output.txt", "path": str(artifact.resolve()),
        "exists": True, "within_allowed_root": True, "size": len("proof\n"),
        "sha256": "f6ed42a9d765eeb230a069bbc3d5dc346b2669594bb0b83cc6d14d5d967b8961",
        "mtime": artifact.stat().st_mtime_ns,
    },)


def test_artifact_observation_rejects_symlink_escape(tmp_path) -> None:
    from agent.receipts import collect_artifact_evidence

    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / "build" / "output.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="allowed roots"):
        collect_artifact_evidence(_manifest(), allowed_roots=(root,))
