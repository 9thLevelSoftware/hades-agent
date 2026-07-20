"""Typed immutable SQLite store and v1 migration for canonical receipts.

Owns the exact frozen ``ReceiptStore.insert/append_observation/get/
find_by_source/list`` API from the Verified Outcome & Artifact Receipts
plan, the attestation/tombstone primitives later tasks build on, and the
one-shot atomic migration of the provisional vertical-slice
``receipts``/``receipt_observations`` tables.

Storage invariants enforced here (schema triggers in ``hades_state``
back them at the SQL layer):

- Receipts, observations, and attestations are immutable — there are no
  update methods, and every persisted value is revalidated against its
  recomputed canonical content hash before insert.
- ``status == "verified"`` requires a matching sealed
  :class:`VerifiedReceiptDecision`; any source label, signature, or
  ordinary decision yields at most ``completed_unverified``.
- Source ingestion is idempotent by ``(source_kind, source_id)`` and
  content hash; reusing a source identity with different content raises
  :class:`ReceiptSourceConflict`, never an update.
- Observations append in a CAS-protected chain: the newcomer must name
  the current latest observation (or ``None`` for the first); forks
  raise :class:`ReceiptObservationConflict`.

Consumes only Task 1 models/hashing plus ``SessionDB._execute_read`` /
``_execute_write``; no scorer, mission, transaction, or UI imports.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from agent.receipt_hashing import canonical_content_hash, normalize_utc_timestamp
from agent.receipt_models import (
    CLAIM_VERDICTS,
    RECEIPT_STATUSES,
    ArtifactDigest,
    EvidenceDigest,
    Receipt,
    ReceiptClaim,
    ReceiptObservation,
    ReceiptQuery,
    ReceiptSourceKey,
    ReceiptSummary,
    RequestedOutcome,
    VerifiedReceiptDecision,
    build_claim,
    build_evidence_digest,
    build_observation,
    build_receipt,
    build_requested_outcome,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hades_state import SessionDB

__all__ = [
    "ReceiptAttestation",
    "ReceiptIntegrityError",
    "ReceiptObservationConflict",
    "ReceiptSourceConflict",
    "ReceiptStore",
    "ReceiptStoreError",
    "ReceiptTombstone",
    "migrate_v1_receipt_tables",
]


class ReceiptStoreError(RuntimeError):
    """Base error for canonical receipt storage failures."""


class ReceiptSourceConflict(ReceiptStoreError):
    """A source identity was reused with different receipt content."""


class ReceiptObservationConflict(ReceiptStoreError):
    """An observation does not chain from the current latest observation."""


class ReceiptIntegrityError(ReceiptStoreError):
    """Persisted or submitted receipt content fails hash revalidation."""


@dataclass(frozen=True)
class ReceiptAttestation:
    """Untrusted provenance attestation over a receipt/observation hash.

    A signature proves who or what produced bytes. It never changes a
    status, claim verdict, uncertainty, freshness, or scorer result.
    """

    attestation_id: str
    target_kind: str
    target_id: str
    target_content_hash: str
    provider_id: str
    key_id: str
    algorithm: str
    signature_b64: str
    signed_at: str
    verification_state: str
    content_hash: str


@dataclass(frozen=True)
class ReceiptTombstone:
    """Immutable record that the retention service deleted one receipt.

    Carries the source identity and old canonical content hash so a
    replayed source ingest or audit can prove what was deleted and why
    without resurrecting the deleted content.
    """

    receipt_id: str
    receipt_content_hash: str
    source_kind: str
    source_id: str
    deleted_at: str
    reason: str
    content_hash: str


# ---------------------------------------------------------------------------
# JSON projection helpers (canonical, key-sorted, compact).
# ---------------------------------------------------------------------------


def _dump_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _items_to_json(items: tuple) -> str:
    return _dump_json([dataclasses.asdict(item) for item in items])


def _outcome_from_json(text: str) -> RequestedOutcome:
    data = json.loads(text)
    return RequestedOutcome(
        outcome_kind=data["outcome_kind"],
        description=data["description"],
        constraints=tuple(data["constraints"]),
        producer_id=data["producer_id"],
        content_hash=data["content_hash"],
    )


def _claims_from_json(text: str) -> tuple[ReceiptClaim, ...]:
    return tuple(
        ReceiptClaim(
            claim_id=d["claim_id"],
            claim_kind=d["claim_kind"],
            statement=d["statement"],
            expected_json=d["expected_json"],
            observed_json=d["observed_json"],
            evidence_ids=tuple(d["evidence_ids"]),
            artifact_ids=tuple(d["artifact_ids"]),
            required=d["required"],
            verdict=d["verdict"],
            uncertainty=tuple(d["uncertainty"]),
            content_hash=d["content_hash"],
        )
        for d in json.loads(text)
    )


def _evidence_from_json(text: str) -> tuple[EvidenceDigest, ...]:
    return tuple(
        EvidenceDigest(
            evidence_id=d["evidence_id"],
            evidence_kind=d["evidence_kind"],
            source_ref=d["source_ref"],
            producer_id=d["producer_id"],
            observed_at=d["observed_at"],
            fresh_until=d["fresh_until"],
            summary=d["summary"],
            payload_hash=d["payload_hash"],
            artifact_ids=tuple(d["artifact_ids"]),
            content_hash=d["content_hash"],
        )
        for d in json.loads(text)
    )


def _artifacts_from_json(text: str) -> tuple[ArtifactDigest, ...]:
    return tuple(
        ArtifactDigest(
            artifact_id=d["artifact_id"],
            source_kind=d["source_kind"],
            source_ref=d["source_ref"],
            display_name=d["display_name"],
            media_type=d["media_type"],
            size_bytes=d["size_bytes"],
            sha256=d["sha256"],
            mtime_ns=d["mtime_ns"],
            captured_at=d["captured_at"],
            content_hash=d["content_hash"],
        )
        for d in json.loads(text)
    )


def _decode_receipt_row(row: sqlite3.Row) -> Receipt:
    return Receipt(
        receipt_id=row["receipt_id"],
        source=ReceiptSourceKey(row["source_kind"], row["source_id"]),
        subject_kind=row["subject_kind"],
        subject_id=row["subject_id"],
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        mission_id=row["mission_id"],
        transaction_id=row["transaction_id"],
        requested_outcome=_outcome_from_json(row["requested_outcome_json"]),
        status=row["status"],
        claims=_claims_from_json(row["claims_json"]),
        evidence=_evidence_from_json(row["evidence_json"]),
        artifacts=_artifacts_from_json(row["artifacts_json"]),
        uncertainty=tuple(json.loads(row["uncertainty_json"])),
        scorer_id=row["scorer_id"],
        scorer_version=row["scorer_version"],
        decided_at=row["decided_at"],
        content_hash=row["content_hash"],
    )


def _decode_observation_row(row: sqlite3.Row) -> ReceiptObservation:
    return ReceiptObservation(
        observation_id=row["observation_id"],
        receipt_id=row["receipt_id"],
        previous_observation_id=row["previous_observation_id"],
        status=row["status"],
        claims=_claims_from_json(row["claims_json"]),
        evidence=_evidence_from_json(row["evidence_json"]),
        artifacts=_artifacts_from_json(row["artifacts_json"]),
        uncertainty=tuple(json.loads(row["uncertainty_json"])),
        scorer_id=row["scorer_id"],
        scorer_version=row["scorer_version"],
        observed_at=row["observed_at"],
        content_hash=row["content_hash"],
    )


def _decode_attestation_row(row: sqlite3.Row) -> ReceiptAttestation:
    return ReceiptAttestation(
        attestation_id=row["attestation_id"],
        target_kind=row["target_kind"],
        target_id=row["target_id"],
        target_content_hash=row["target_content_hash"],
        provider_id=row["provider_id"],
        key_id=row["key_id"],
        algorithm=row["algorithm"],
        signature_b64=row["signature_b64"],
        signed_at=row["signed_at"],
        verification_state=row["verification_state"],
        content_hash=row["content_hash"],
    )


# ---------------------------------------------------------------------------
# Content-hash revalidation: every nested hash is recomputed on insert.
# ---------------------------------------------------------------------------


def _rebuild_claim(claim: ReceiptClaim) -> ReceiptClaim:
    return build_claim(
        claim_kind=claim.claim_kind,
        statement=claim.statement,
        expected_json=claim.expected_json,
        observed_json=claim.observed_json,
        evidence_ids=claim.evidence_ids,
        artifact_ids=claim.artifact_ids,
        required=claim.required,
        verdict=claim.verdict,
        uncertainty=claim.uncertainty,
    )


def _rebuild_evidence(evidence: EvidenceDigest) -> EvidenceDigest:
    return build_evidence_digest(
        evidence_kind=evidence.evidence_kind,
        source_ref=evidence.source_ref,
        producer_id=evidence.producer_id,
        observed_at=evidence.observed_at,
        fresh_until=evidence.fresh_until,
        summary=evidence.summary,
        payload_hash=evidence.payload_hash,
        artifact_ids=evidence.artifact_ids,
    )


def _rebuild_artifact(artifact: ArtifactDigest) -> ArtifactDigest:
    # build_artifact_digest revalidates sha256/size/mtime shapes; the
    # artifact_id/content_hash it derives must match what was submitted.
    from agent.receipt_models import build_artifact_digest

    return build_artifact_digest(
        source_kind=artifact.source_kind,
        source_ref=artifact.source_ref,
        display_name=artifact.display_name,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        mtime_ns=artifact.mtime_ns,
        captured_at=artifact.captured_at,
    )


def _validate_receipt(receipt: Receipt) -> None:
    outcome = receipt.requested_outcome
    rebuilt_outcome = build_requested_outcome(
        outcome_kind=outcome.outcome_kind,
        description=outcome.description,
        constraints=outcome.constraints,
        producer_id=outcome.producer_id,
    )
    if rebuilt_outcome != outcome:
        raise ReceiptIntegrityError(
            "requested_outcome content hash does not match its fields"
        )
    for claim in receipt.claims:
        if _rebuild_claim(claim) != claim:
            raise ReceiptIntegrityError(
                f"claim {claim.claim_id!r} does not match its recomputed "
                "content hash"
            )
    for evidence in receipt.evidence:
        if _rebuild_evidence(evidence) != evidence:
            raise ReceiptIntegrityError(
                f"evidence {evidence.evidence_id!r} does not match its "
                "recomputed content hash"
            )
    for artifact in receipt.artifacts:
        if _rebuild_artifact(artifact) != artifact:
            raise ReceiptIntegrityError(
                f"artifact {artifact.artifact_id!r} does not match its "
                "recomputed content hash"
            )
    rebuilt = build_receipt(
        source=receipt.source,
        subject_kind=receipt.subject_kind,
        subject_id=receipt.subject_id,
        session_id=receipt.session_id,
        turn_id=receipt.turn_id,
        mission_id=receipt.mission_id,
        transaction_id=receipt.transaction_id,
        requested_outcome=receipt.requested_outcome,
        status=receipt.status,
        claims=receipt.claims,
        evidence=receipt.evidence,
        artifacts=receipt.artifacts,
        uncertainty=receipt.uncertainty,
        scorer_id=receipt.scorer_id,
        scorer_version=receipt.scorer_version,
        decided_at=receipt.decided_at,
    )
    if receipt.source.source_kind == "legacy":
        # Migrated legacy receipt IDs are the explicit compatibility
        # exception: they keep their original ID mapped to the recomputed
        # canonical hash.
        rebuilt = dataclasses.replace(rebuilt, receipt_id=receipt.receipt_id)
    if rebuilt != receipt:
        raise ReceiptIntegrityError(
            "receipt content hash does not match its recomputed canonical "
            "content"
        )


def _validate_observation(observation: ReceiptObservation) -> None:
    for claim in observation.claims:
        if _rebuild_claim(claim) != claim:
            raise ReceiptIntegrityError(
                f"claim {claim.claim_id!r} does not match its recomputed "
                "content hash"
            )
    for evidence in observation.evidence:
        if _rebuild_evidence(evidence) != evidence:
            raise ReceiptIntegrityError(
                f"evidence {evidence.evidence_id!r} does not match its "
                "recomputed content hash"
            )
    for artifact in observation.artifacts:
        if _rebuild_artifact(artifact) != artifact:
            raise ReceiptIntegrityError(
                f"artifact {artifact.artifact_id!r} does not match its "
                "recomputed content hash"
            )
    rebuilt = build_observation(
        receipt_id=observation.receipt_id,
        previous_observation_id=observation.previous_observation_id,
        status=observation.status,
        claims=observation.claims,
        evidence=observation.evidence,
        artifacts=observation.artifacts,
        uncertainty=observation.uncertainty,
        scorer_id=observation.scorer_id,
        scorer_version=observation.scorer_version,
        observed_at=observation.observed_at,
    )
    if not observation.observation_id.startswith("obs_"):
        # Migrated legacy observation IDs bypass the store insert path;
        # store-appended observations must carry their derived ID.
        raise ReceiptIntegrityError(
            f"observation ID {observation.observation_id!r} is not derived "
            "from its canonical content hash"
        )
    if rebuilt != observation:
        raise ReceiptIntegrityError(
            "observation content hash does not match its recomputed "
            "canonical content"
        )


_DECISION_HASH_FIELDS = (
    "scorer_id",
    "scorer_version",
    "subject_kind",
    "subject_id",
    "snapshot_hash",
    "claim_hashes",
    "decided_at",
    "fresh_until",
)


def _validate_decision_seal(
    *,
    status: str,
    decision: object,
    subject_kind: str,
    subject_id: str,
    claims: tuple[ReceiptClaim, ...],
    scorer_id: str,
    scorer_version: str,
    timestamp: str,
) -> None:
    """Enforce the verified-status seal contract at the storage boundary.

    Every rejection message deliberately contains "scorer decision": the
    only path to a persisted ``verified`` status is a sealed decision
    from the independent scoring service that matches this exact
    subject, claim set, scorer identity, and freshness fact.
    """
    if status != "verified":
        if isinstance(decision, VerifiedReceiptDecision):
            raise ReceiptIntegrityError(
                "non-verified receipts never receive a verified seal"
            )
        return
    if not isinstance(decision, VerifiedReceiptDecision):
        raise PermissionError(
            "verified status requires a sealed independent scorer decision"
        )
    body = {name: getattr(decision, name) for name in _DECISION_HASH_FIELDS}
    if canonical_content_hash(body) != decision.decision_hash:
        raise PermissionError("sealed scorer decision hash is invalid")
    if (
        decision.subject_kind != subject_kind
        or decision.subject_id != subject_id
    ):
        raise PermissionError(
            "sealed scorer decision subject does not match the receipt"
        )
    if (
        decision.scorer_id != scorer_id
        or decision.scorer_version != scorer_version
    ):
        raise PermissionError(
            "sealed scorer decision scorer identity does not match"
        )
    if decision.decided_at != timestamp:
        raise PermissionError(
            "sealed scorer decision freshness fact does not match"
        )
    if tuple(decision.claim_hashes) != tuple(c.content_hash for c in claims):
        raise PermissionError(
            "sealed scorer decision claim hashes do not match the receipt"
        )


# ---------------------------------------------------------------------------
# Low-level row writers shared by the store and the v1 migration.
# ---------------------------------------------------------------------------

_INSERT_RECEIPT_SQL = (
    "INSERT INTO receipts (receipt_id, source_kind, source_id, subject_kind, "
    "subject_id, session_id, turn_id, mission_id, transaction_id, "
    "requested_outcome_json, status, claims_json, evidence_json, "
    "artifacts_json, uncertainty_json, scorer_id, scorer_version, "
    "decided_at, content_hash, inserted_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_OBSERVATION_SQL = (
    "INSERT INTO receipt_observations (observation_id, receipt_id, "
    "previous_observation_id, status, claims_json, evidence_json, "
    "artifacts_json, uncertainty_json, scorer_id, scorer_version, "
    "observed_at, content_hash, inserted_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_ATTESTATION_SQL = (
    "INSERT INTO receipt_attestations (attestation_id, target_kind, "
    "target_id, target_content_hash, provider_id, key_id, algorithm, "
    "signature_b64, signed_at, verification_state, content_hash) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _insert_receipt_row(
    conn: sqlite3.Connection, receipt: Receipt, inserted_at: float
) -> None:
    conn.execute(
        _INSERT_RECEIPT_SQL,
        (
            receipt.receipt_id,
            receipt.source.source_kind,
            receipt.source.source_id,
            receipt.subject_kind,
            receipt.subject_id,
            receipt.session_id,
            receipt.turn_id,
            receipt.mission_id,
            receipt.transaction_id,
            _dump_json(dataclasses.asdict(receipt.requested_outcome)),
            receipt.status,
            _items_to_json(receipt.claims),
            _items_to_json(receipt.evidence),
            _items_to_json(receipt.artifacts),
            _dump_json(list(receipt.uncertainty)),
            receipt.scorer_id,
            receipt.scorer_version,
            receipt.decided_at,
            receipt.content_hash,
            inserted_at,
        ),
    )


def _insert_observation_row(
    conn: sqlite3.Connection,
    observation: ReceiptObservation,
    inserted_at: float,
) -> None:
    conn.execute(
        _INSERT_OBSERVATION_SQL,
        (
            observation.observation_id,
            observation.receipt_id,
            observation.previous_observation_id,
            observation.status,
            _items_to_json(observation.claims),
            _items_to_json(observation.evidence),
            _items_to_json(observation.artifacts),
            _dump_json(list(observation.uncertainty)),
            observation.scorer_id,
            observation.scorer_version,
            observation.observed_at,
            observation.content_hash,
            inserted_at,
        ),
    )


def _insert_attestation_row(
    conn: sqlite3.Connection, attestation: ReceiptAttestation
) -> None:
    conn.execute(
        _INSERT_ATTESTATION_SQL,
        (
            attestation.attestation_id,
            attestation.target_kind,
            attestation.target_id,
            attestation.target_content_hash,
            attestation.provider_id,
            attestation.key_id,
            attestation.algorithm,
            attestation.signature_b64,
            attestation.signed_at,
            attestation.verification_state,
            attestation.content_hash,
        ),
    )


# ---------------------------------------------------------------------------
# The canonical typed store.
# ---------------------------------------------------------------------------


class ReceiptStore:
    """Typed immutable receipt storage over a profile-local ``SessionDB``."""

    def __init__(self, db: "SessionDB") -> None:
        self._db = db

    # ── Writes ──

    def insert(
        self,
        receipt: Receipt,
        *,
        decision: VerifiedReceiptDecision | None = None,
    ) -> Receipt:
        if not isinstance(receipt, Receipt):
            raise TypeError(f"expected a Receipt, got {type(receipt).__name__}")

        def _do(conn: sqlite3.Connection) -> Receipt:
            row = conn.execute(
                "SELECT * FROM receipts WHERE source_kind = ? AND source_id = ?",
                (receipt.source.source_kind, receipt.source.source_id),
            ).fetchone()
            if row is not None:
                existing = _decode_receipt_row(row)
                if existing.content_hash == receipt.content_hash:
                    # Idempotent replay of an identical source.
                    return existing
                raise ReceiptSourceConflict(
                    f"source {receipt.source.source_kind}:"
                    f"{receipt.source.source_id} already has receipt "
                    f"{existing.receipt_id} with different content; "
                    "changed content must become a recheck observation, "
                    "never a replacement receipt"
                )
            _validate_receipt(receipt)
            _validate_decision_seal(
                status=receipt.status,
                decision=decision,
                subject_kind=receipt.subject_kind,
                subject_id=receipt.subject_id,
                claims=receipt.claims,
                scorer_id=receipt.scorer_id,
                scorer_version=receipt.scorer_version,
                timestamp=receipt.decided_at,
            )
            _insert_receipt_row(conn, receipt, time.time())
            return receipt

        return self._db._execute_write(_do)

    def append_observation(
        self,
        observation: ReceiptObservation,
        *,
        decision: VerifiedReceiptDecision | None = None,
    ) -> ReceiptObservation:
        if not isinstance(observation, ReceiptObservation):
            raise TypeError(
                f"expected a ReceiptObservation, got {type(observation).__name__}"
            )

        def _do(conn: sqlite3.Connection) -> ReceiptObservation:
            receipt_row = conn.execute(
                "SELECT * FROM receipts WHERE receipt_id = ?",
                (observation.receipt_id,),
            ).fetchone()
            if receipt_row is None:
                raise ReceiptStoreError(
                    f"unknown receipt {observation.receipt_id!r}: observations "
                    "attach only to an existing immutable receipt"
                )
            duplicate = conn.execute(
                "SELECT * FROM receipt_observations "
                "WHERE receipt_id = ? AND content_hash = ?",
                (observation.receipt_id, observation.content_hash),
            ).fetchone()
            if duplicate is not None:
                existing = _decode_observation_row(duplicate)
                if existing.observation_id == observation.observation_id:
                    # Idempotent replay of an identical observation.
                    return existing
                raise ReceiptObservationConflict(
                    "identical observation content already appended as "
                    f"{existing.observation_id!r}"
                )
            latest_row = conn.execute(
                "SELECT observation_id FROM receipt_observations "
                "WHERE receipt_id = ? ORDER BY inserted_at DESC, rowid DESC "
                "LIMIT 1",
                (observation.receipt_id,),
            ).fetchone()
            latest_id = latest_row["observation_id"] if latest_row else None
            if observation.previous_observation_id != latest_id:
                raise ReceiptObservationConflict(
                    f"observation must chain from latest {latest_id!r}, got "
                    f"{observation.previous_observation_id!r}; the chain "
                    "never forks and earlier observations are never updated"
                )
            _validate_observation(observation)
            _validate_decision_seal(
                status=observation.status,
                decision=decision,
                subject_kind=receipt_row["subject_kind"],
                subject_id=receipt_row["subject_id"],
                claims=observation.claims,
                scorer_id=observation.scorer_id,
                scorer_version=observation.scorer_version,
                timestamp=observation.observed_at,
            )
            _insert_observation_row(conn, observation, time.time())
            return observation

        return self._db._execute_write(_do)

    # ── Reads ──

    def get(self, receipt_id: str) -> Receipt | None:
        def _do(conn: sqlite3.Connection) -> Receipt | None:
            row = conn.execute(
                "SELECT * FROM receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            return None if row is None else _decode_receipt_row(row)

        return self._db._execute_read(_do)

    def find_by_source(self, source: ReceiptSourceKey) -> Receipt | None:
        if not isinstance(source, ReceiptSourceKey):
            raise TypeError(
                f"expected a ReceiptSourceKey, got {type(source).__name__}"
            )

        def _do(conn: sqlite3.Connection) -> Receipt | None:
            row = conn.execute(
                "SELECT * FROM receipts WHERE source_kind = ? AND source_id = ?",
                (source.source_kind, source.source_id),
            ).fetchone()
            return None if row is None else _decode_receipt_row(row)

        return self._db._execute_read(_do)

    def list(self, query: ReceiptQuery) -> list[ReceiptSummary]:
        if not isinstance(query, ReceiptQuery):
            raise TypeError(
                f"expected a ReceiptQuery, got {type(query).__name__}"
            )
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (
            ("status", query.status),
            ("source_kind", query.source_kind),
            ("subject_kind", query.subject_kind),
            ("session_id", query.session_id),
            ("turn_id", query.turn_id),
            ("mission_id", query.mission_id),
            ("transaction_id", query.transaction_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if query.decided_after is not None:
            clauses.append("decided_at > ?")
            params.append(normalize_utc_timestamp(query.decided_after))
        if query.decided_before is not None:
            clauses.append("decided_at < ?")
            params.append(normalize_utc_timestamp(query.decided_before))
        sql = (
            "SELECT receipt_id, source_kind, source_id, subject_kind, "
            "subject_id, session_id, status, scorer_id, scorer_version, "
            "decided_at, content_hash FROM receipts"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY decided_at DESC, receipt_id LIMIT ? OFFSET ?"
        params.extend((query.limit, query.offset))

        def _do(conn: sqlite3.Connection) -> list[ReceiptSummary]:
            return [
                ReceiptSummary(
                    receipt_id=row["receipt_id"],
                    source=ReceiptSourceKey(row["source_kind"], row["source_id"]),
                    subject_kind=row["subject_kind"],
                    subject_id=row["subject_id"],
                    session_id=row["session_id"],
                    status=row["status"],
                    scorer_id=row["scorer_id"],
                    scorer_version=row["scorer_version"],
                    decided_at=row["decided_at"],
                    content_hash=row["content_hash"],
                )
                for row in conn.execute(sql, params)
            ]

        return self._db._execute_read(_do)

    def observations(self, receipt_id: str) -> tuple[ReceiptObservation, ...]:
        """Return the append-only observation chain, oldest first."""

        def _do(conn: sqlite3.Connection) -> tuple[ReceiptObservation, ...]:
            return tuple(
                _decode_observation_row(row)
                for row in conn.execute(
                    "SELECT * FROM receipt_observations WHERE receipt_id = ? "
                    "ORDER BY inserted_at, rowid",
                    (receipt_id,),
                )
            )

        return self._db._execute_read(_do)

    def latest_observation(self, receipt_id: str) -> ReceiptObservation | None:
        chain = self.observations(receipt_id)
        return chain[-1] if chain else None

    def list_attestations(self, target_id: str) -> tuple[ReceiptAttestation, ...]:
        """Untrusted provenance attestations for a receipt/observation ID."""

        def _do(conn: sqlite3.Connection) -> tuple[ReceiptAttestation, ...]:
            return tuple(
                _decode_attestation_row(row)
                for row in conn.execute(
                    "SELECT * FROM receipt_attestations WHERE target_id = ? "
                    "ORDER BY attestation_id",
                    (target_id,),
                )
            )

        return self._db._execute_read(_do)

    def get_observation(self, observation_id: str) -> ReceiptObservation | None:
        def _do(conn: sqlite3.Connection) -> ReceiptObservation | None:
            row = conn.execute(
                "SELECT * FROM receipt_observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
            return None if row is None else _decode_observation_row(row)

        return self._db._execute_read(_do)

    # ── Provenance attestations (append-only, never truth) ──

    def append_attestation(
        self, attestation: ReceiptAttestation
    ) -> ReceiptAttestation:
        """Append one immutable provenance attestation over a stored hash.

        The attestation must be self-consistent (its ``content_hash`` and
        ``attestation_id`` derive from its own fields) and must target an
        existing receipt or observation whose stored canonical content
        hash equals ``target_content_hash``. A signature proves who or
        what produced bytes — appending one never changes a status,
        claim verdict, uncertainty, freshness, or scorer result.
        """
        if not isinstance(attestation, ReceiptAttestation):
            raise TypeError(
                "expected a ReceiptAttestation, got "
                f"{type(attestation).__name__}"
            )
        body = {
            "target_kind": attestation.target_kind,
            "target_id": attestation.target_id,
            "target_content_hash": attestation.target_content_hash,
            "provider_id": attestation.provider_id,
            "key_id": attestation.key_id,
            "algorithm": attestation.algorithm,
            "signature_b64": attestation.signature_b64,
            "signed_at": attestation.signed_at,
            "verification_state": attestation.verification_state,
        }
        recomputed = canonical_content_hash(body)
        if recomputed != attestation.content_hash:
            raise ReceiptIntegrityError(
                "attestation content hash does not match its fields"
            )
        expected_id = f"att_{recomputed.removeprefix('sha256:')}"
        if attestation.attestation_id != expected_id:
            raise ReceiptIntegrityError(
                "attestation ID is not derived from its canonical content hash"
            )
        if attestation.target_kind not in ("receipt", "observation"):
            raise ReceiptIntegrityError(
                f"unknown attestation target kind {attestation.target_kind!r}"
            )

        def _do(conn: sqlite3.Connection) -> ReceiptAttestation:
            existing = conn.execute(
                "SELECT * FROM receipt_attestations WHERE content_hash = ?",
                (attestation.content_hash,),
            ).fetchone()
            if existing is not None:
                # Idempotent replay of an identical attestation.
                return _decode_attestation_row(existing)
            table = (
                "receipts"
                if attestation.target_kind == "receipt"
                else "receipt_observations"
            )
            id_column = (
                "receipt_id"
                if attestation.target_kind == "receipt"
                else "observation_id"
            )
            target = conn.execute(
                f"SELECT content_hash FROM {table} WHERE {id_column} = ?",
                (attestation.target_id,),
            ).fetchone()
            if target is None:
                raise ReceiptStoreError(
                    f"unknown attestation target {attestation.target_kind}:"
                    f"{attestation.target_id}"
                )
            if target["content_hash"] != attestation.target_content_hash:
                raise ReceiptIntegrityError(
                    "attestation target hash does not match the stored "
                    f"{attestation.target_kind} content hash"
                )
            _insert_attestation_row(conn, attestation)
            return attestation

        return self._db._execute_write(_do)

    # ── Retention (the only deletion path; tombstone-first) ──

    def list_tombstones(self) -> tuple[ReceiptTombstone, ...]:
        def _do(conn: sqlite3.Connection) -> tuple[ReceiptTombstone, ...]:
            return tuple(
                ReceiptTombstone(
                    receipt_id=row["receipt_id"],
                    receipt_content_hash=row["receipt_content_hash"],
                    source_kind=row["source_kind"],
                    source_id=row["source_id"],
                    deleted_at=row["deleted_at"],
                    reason=row["reason"],
                    content_hash=row["content_hash"],
                )
                for row in conn.execute(
                    "SELECT * FROM receipt_deletion_tombstones "
                    "ORDER BY deleted_at, receipt_id"
                )
            )

        return self._db._execute_read(_do)

    def _retention_delete(
        self,
        *,
        receipt_ids: tuple[str, ...],
        artifact_location_ids: tuple[str, ...] = (),
        deleted_at: str,
        reason: str,
    ) -> dict:
        """Delete expired rows for the retention service — nothing else.

        Deliberately private: deletion is available only to
        ``agent.receipt_security.ReceiptRetentionService``, so the
        store's public surface stays free of update/delete methods.

        One transaction: expired raw artifact locators (and any digest
        row left with zero locations) are deleted before receipt rows;
        each receipt deletion first appends an immutable tombstone
        carrying the source identity and old content hash, then removes
        its attestations, observations, and the receipt itself. An
        already-tombstoned receipt is skipped, making replay safe. The
        returned dict includes the raw file locator paths of deleted
        locations so the caller can remove bytes only inside its
        configured receipt artifact directory — this method never
        touches the filesystem.
        """
        deleted_at_norm = normalize_utc_timestamp(deleted_at)

        def _do(conn: sqlite3.Connection) -> dict:
            counts = {
                "deleted_receipts": 0,
                "deleted_observations": 0,
                "deleted_attestations": 0,
                "deleted_artifact_locations": 0,
                "tombstones": 0,
                "already_deleted": 0,
            }
            locator_paths: list[str] = []
            touched_artifacts: set[str] = set()
            for location_id in artifact_location_ids:
                row = conn.execute(
                    "SELECT artifact_id, locator_json FROM artifact_locations "
                    "WHERE location_id = ?",
                    (location_id,),
                ).fetchone()
                if row is None:
                    continue
                try:
                    locator = json.loads(row["locator_json"])
                except ValueError:
                    locator = {}
                if isinstance(locator, dict) and locator.get("kind") == "file":
                    path = str(locator.get("path") or "")
                    if path:
                        locator_paths.append(path)
                conn.execute(
                    "DELETE FROM artifact_locations WHERE location_id = ?",
                    (location_id,),
                )
                counts["deleted_artifact_locations"] += 1
                touched_artifacts.add(row["artifact_id"])
            for artifact_id in sorted(touched_artifacts):
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM artifact_locations "
                    "WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()[0]
                if remaining == 0:
                    conn.execute(
                        "DELETE FROM artifact_digests WHERE artifact_id = ?",
                        (artifact_id,),
                    )
            for receipt_id in receipt_ids:
                row = conn.execute(
                    "SELECT receipt_id, source_kind, source_id, content_hash "
                    "FROM receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if row is None:
                    tombstoned = conn.execute(
                        "SELECT 1 FROM receipt_deletion_tombstones "
                        "WHERE receipt_id = ?",
                        (receipt_id,),
                    ).fetchone()
                    if tombstoned is None:
                        raise ReceiptStoreError(
                            f"retention plan names unknown receipt "
                            f"{receipt_id!r} with no deletion tombstone"
                        )
                    counts["already_deleted"] += 1
                    continue
                tombstone_body = {
                    "receipt_id": row["receipt_id"],
                    "receipt_content_hash": row["content_hash"],
                    "source_kind": row["source_kind"],
                    "source_id": row["source_id"],
                    "deleted_at": deleted_at_norm,
                    "reason": reason,
                }
                conn.execute(
                    "INSERT INTO receipt_deletion_tombstones (receipt_id, "
                    "receipt_content_hash, source_kind, source_id, "
                    "deleted_at, reason, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["receipt_id"],
                        row["content_hash"],
                        row["source_kind"],
                        row["source_id"],
                        deleted_at_norm,
                        reason,
                        canonical_content_hash(tombstone_body),
                    ),
                )
                counts["tombstones"] += 1
                observation_ids = [
                    obs_row["observation_id"]
                    for obs_row in conn.execute(
                        "SELECT observation_id FROM receipt_observations "
                        "WHERE receipt_id = ?",
                        (receipt_id,),
                    )
                ]
                for target_id in [receipt_id, *observation_ids]:
                    cursor = conn.execute(
                        "DELETE FROM receipt_attestations WHERE target_id = ?",
                        (target_id,),
                    )
                    counts["deleted_attestations"] += cursor.rowcount
                cursor = conn.execute(
                    "DELETE FROM receipt_observations WHERE receipt_id = ?",
                    (receipt_id,),
                )
                counts["deleted_observations"] += cursor.rowcount
                conn.execute(
                    "DELETE FROM receipts WHERE receipt_id = ?", (receipt_id,)
                )
                counts["deleted_receipts"] += 1
            counts["deleted_locator_paths"] = locator_paths
            return counts

        return self._db._execute_write(_do)


# ---------------------------------------------------------------------------
# Atomic migration of the provisional vertical-slice v1 tables.
# ---------------------------------------------------------------------------

_LEGACY_VERIFIED_UNCERTAINTY = (
    "legacy verified status requires independent recheck"
)
_LEGACY_PRODUCER_ID = "legacy.vertical_slice"
_MIGRATION_META_KEY = "receipt_migration_v1"

# Signature columns of the provisional vertical-slice `receipts` table.
_V1_MARKER_COLUMNS = frozenset(
    {"objective", "before_after_json", "freshness_json", "signature_json"}
)


def _epoch_to_rfc3339(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReceiptIntegrityError(
            f"legacy created_at must be an epoch number, got {value!r}"
        )
    return normalize_utc_timestamp(
        datetime.fromtimestamp(float(value), timezone.utc)
    )


def _parse_json(text: object, default: object) -> object:
    if not isinstance(text, str) or not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _convert_v1_claims(
    claims_json: object, evidence_id: str
) -> tuple[ReceiptClaim, ...]:
    parsed = _parse_json(claims_json, [])
    claims: list[ReceiptClaim] = []
    seen: set[str] = set()

    def _add(claim: ReceiptClaim) -> None:
        if claim.content_hash not in seen:
            seen.add(claim.content_hash)
            claims.append(claim)

    if isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry, dict):
                statement = str(
                    entry.get("statement")
                    or entry.get("claim")
                    or _dump_json(entry)
                )
                verdict = entry.get("verdict")
                if verdict not in CLAIM_VERDICTS:
                    verdict = "unknown"
                _add(
                    build_claim(
                        claim_kind=str(
                            entry.get("claim_kind")
                            or entry.get("kind")
                            or "legacy_effect"
                        ),
                        statement=statement,
                        observed_json=_dump_json(entry),
                        evidence_ids=(evidence_id,),
                        required=bool(entry.get("required", True)),
                        verdict=verdict,
                    )
                )
            else:
                _add(
                    build_claim(
                        claim_kind="legacy_effect",
                        statement=str(entry),
                        evidence_ids=(evidence_id,),
                    )
                )
    elif parsed:
        _add(
            build_claim(
                claim_kind="legacy_effect",
                statement="legacy claims payload",
                observed_json=_dump_json(parsed),
                evidence_ids=(evidence_id,),
            )
        )
    return tuple(claims)


def _downgrade_legacy_status(
    status: object, uncertainty: tuple[str, ...], *, what: str
) -> tuple[str, tuple[str, ...]]:
    if status == "verified":
        # A legacy verified label is an untrusted source claim: only a
        # current independent scorer recheck can restore `verified`.
        if _LEGACY_VERIFIED_UNCERTAINTY not in uncertainty:
            uncertainty = uncertainty + (_LEGACY_VERIFIED_UNCERTAINTY,)
        return "completed_unverified", uncertainty
    if status not in RECEIPT_STATUSES:
        raise ReceiptIntegrityError(
            f"legacy {what} has unknown status {status!r}; migration refuses "
            "to guess and rolls back"
        )
    return status, uncertainty


def _convert_v1_receipt(
    row: dict,
) -> tuple[Receipt, ReceiptAttestation | None]:
    receipt_id = str(row["receipt_id"])
    decided_at = _epoch_to_rfc3339(row.get("created_at"))
    objective = str(row.get("objective") or f"legacy receipt {receipt_id}")
    constraints = tuple(_str_list(_parse_json(row.get("constraints_json"), [])))
    outcome = build_requested_outcome(
        outcome_kind="mission_outcome",
        description=objective,
        constraints=constraints,
        producer_id=_LEGACY_PRODUCER_ID,
    )
    row_evidence = build_evidence_digest(
        evidence_kind="legacy_receipt_row",
        source_ref=f"state.db:receipts:{receipt_id}",
        producer_id=_LEGACY_PRODUCER_ID,
        observed_at=decided_at,
        summary=(
            "original provisional vertical-slice receipt row preserved "
            "verbatim during migration"
        ),
        payload_hash=canonical_content_hash(
            {key: row[key] for key in sorted(row)}
        ),
    )
    legacy_hash_evidence = build_evidence_digest(
        evidence_kind="legacy_content_hash",
        source_ref=f"state.db:receipts:{receipt_id}",
        producer_id=_LEGACY_PRODUCER_ID,
        observed_at=decided_at,
        summary=f"legacy content hash {row.get('content_hash')!s}",
        payload_hash=canonical_content_hash(
            {"legacy_content_hash": str(row.get("content_hash"))}
        ),
    )
    evidence = (row_evidence, legacy_hash_evidence)
    claims = _convert_v1_claims(row.get("claims_json"), row_evidence.evidence_id)
    uncertainty = tuple(_str_list(_parse_json(row.get("uncertainty_json"), [])))
    status, uncertainty = _downgrade_legacy_status(
        row.get("status"), uncertainty, what=f"receipt {receipt_id!r}"
    )
    verifier = _parse_json(row.get("verifier_json"), {})
    if not isinstance(verifier, dict):
        verifier = {}
    scorer_id = str(verifier.get("verifier_id") or "legacy.unknown-verifier")
    scorer_version = str(verifier.get("verifier_version") or "v1")
    transaction_ids = _str_list(_parse_json(row.get("transaction_ids_json"), []))
    transaction_id = transaction_ids[0] if len(transaction_ids) == 1 else None
    receipt = build_receipt(
        source=ReceiptSourceKey("legacy", receipt_id),
        subject_kind="mission",
        subject_id=str(row["mission_id"]),
        mission_id=str(row["mission_id"]),
        transaction_id=transaction_id,
        requested_outcome=outcome,
        status=status,
        claims=claims,
        evidence=evidence,
        uncertainty=uncertainty,
        scorer_id=scorer_id,
        scorer_version=scorer_version,
        decided_at=decided_at,
    )
    # Preserve the legacy receipt ID (compatibility exception) mapped to
    # the recomputed canonical content hash.
    receipt = dataclasses.replace(receipt, receipt_id=receipt_id)
    attestation = _convert_v1_signature(row, receipt)
    return receipt, attestation


def _convert_v1_signature(
    row: dict, receipt: Receipt
) -> ReceiptAttestation | None:
    raw = row.get("signature_json")
    if not raw:
        return None
    signature = _parse_json(raw, None)
    if not isinstance(signature, dict):
        signature = {"raw": str(raw)}
    signed_at_raw = signature.get("signed_at")
    if isinstance(signed_at_raw, (int, float)) and not isinstance(
        signed_at_raw, bool
    ):
        signed_at = _epoch_to_rfc3339(signed_at_raw)
    elif isinstance(signed_at_raw, str):
        try:
            signed_at = normalize_utc_timestamp(signed_at_raw)
        except ValueError:
            signed_at = receipt.decided_at
    else:
        signed_at = receipt.decided_at
    body = {
        "target_kind": "receipt",
        "target_id": receipt.receipt_id,
        "target_content_hash": receipt.content_hash,
        "provider_id": str(
            signature.get("provider") or signature.get("provider_id") or "legacy"
        ),
        "key_id": str(signature.get("key_id") or "unknown"),
        "algorithm": str(signature.get("algorithm") or "unknown"),
        "signature_b64": str(
            signature.get("signature") or signature.get("signature_b64") or ""
        ),
        "signed_at": signed_at,
        # Imported signatures are provenance-only and untrusted: they
        # never change a status or claim verdict.
        "verification_state": "unverified_import",
    }
    content_hash = canonical_content_hash(body)
    return ReceiptAttestation(
        attestation_id=f"att_{content_hash.removeprefix('sha256:')}",
        content_hash=content_hash,
        **body,
    )


def _convert_v1_observation(
    row: dict, *, previous_observation_id: str | None
) -> ReceiptObservation:
    observation_id = str(row["observation_id"])
    observed_at = _epoch_to_rfc3339(row.get("created_at"))
    row_evidence = build_evidence_digest(
        evidence_kind="legacy_observation_row",
        source_ref=f"state.db:receipt_observations:{observation_id}",
        producer_id=_LEGACY_PRODUCER_ID,
        observed_at=observed_at,
        summary=(
            "original provisional vertical-slice observation row preserved "
            "verbatim during migration"
        ),
        payload_hash=canonical_content_hash(
            {key: row[key] for key in sorted(row)}
        ),
    )
    status, uncertainty = _downgrade_legacy_status(
        row.get("status"), (), what=f"observation {observation_id!r}"
    )
    observation = build_observation(
        receipt_id=str(row["receipt_id"]),
        previous_observation_id=previous_observation_id,
        status=status,
        evidence=(row_evidence,),
        uncertainty=uncertainty,
        scorer_id="legacy.unknown-verifier",
        scorer_version="v1",
        observed_at=observed_at,
    )
    return dataclasses.replace(observation, observation_id=observation_id)


def _validate_migrated_rows(
    conn: sqlite3.Connection,
    *,
    expected_receipts: int,
    expected_observations: int,
) -> None:
    receipt_count = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    observation_count = conn.execute(
        "SELECT COUNT(*) FROM receipt_observations"
    ).fetchone()[0]
    if receipt_count != expected_receipts:
        raise ReceiptIntegrityError(
            f"migration row-count mismatch: {receipt_count} receipts, "
            f"expected {expected_receipts}"
        )
    if observation_count != expected_observations:
        raise ReceiptIntegrityError(
            f"migration row-count mismatch: {observation_count} observations, "
            f"expected {expected_observations}"
        )
    for table in ("receipts", "receipt_observations", "receipt_attestations"):
        violations = conn.execute(
            f'PRAGMA foreign_key_check("{table}")'
        ).fetchall()
        if violations:
            raise ReceiptIntegrityError(
                f"migration foreign-key violations in {table}: {violations!r}"
            )
    # Recompute every canonical hash from the persisted projections.
    for row in conn.execute("SELECT * FROM receipts"):
        decoded = _decode_receipt_row(row)
        _validate_receipt(decoded)
    for row in conn.execute("SELECT * FROM receipt_observations"):
        decoded = _decode_observation_row(row)
        rebuilt = build_observation(
            receipt_id=decoded.receipt_id,
            previous_observation_id=decoded.previous_observation_id,
            status=decoded.status,
            claims=decoded.claims,
            evidence=decoded.evidence,
            artifacts=decoded.artifacts,
            uncertainty=decoded.uncertainty,
            scorer_id=decoded.scorer_id,
            scorer_version=decoded.scorer_version,
            observed_at=decoded.observed_at,
        )
        if rebuilt.content_hash != decoded.content_hash:
            raise ReceiptIntegrityError(
                f"migrated observation {decoded.observation_id!r} fails "
                "canonical hash recomputation"
            )


def migrate_v1_receipt_tables(conn: sqlite3.Connection) -> dict:
    """Atomically convert v1 vertical-slice receipt tables to canonical.

    One ``BEGIN IMMEDIATE`` transaction: rename the old tables to
    ``_receipt_v1_*``, create the canonical tables, convert every row to
    immutable canonical values (preserving IDs and lineage, recomputing
    hashes, downgrading legacy ``verified``, importing signatures as
    untrusted attestations, chaining observations by old
    ``(receipt_id, created_at, observation_id)`` order), validate counts,
    foreign keys, source uniqueness, and recomputed hashes, then drop the
    renamed tables and record the result in ``state_meta``. Any exception
    rolls back to the original untouched v1 tables.
    """
    import hades_state

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-check the shape inside the transaction: a concurrent process
        # may have completed the migration while we waited for the lock.
        columns = {
            r[1] for r in conn.execute('PRAGMA table_info("receipts")')
        }
        if not (_V1_MARKER_COLUMNS <= columns):
            conn.execute("ROLLBACK")
            return {"version": 1, "skipped": True}

        receipt_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM receipts ORDER BY created_at, receipt_id"
            )
        ]
        observation_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM receipt_observations "
                "ORDER BY receipt_id, created_at, observation_id"
            )
        ]

        conn.execute("ALTER TABLE receipts RENAME TO _receipt_v1_receipts")
        conn.execute(
            "ALTER TABLE receipt_observations "
            "RENAME TO _receipt_v1_receipt_observations"
        )
        for statement in hades_state.RECEIPT_SCHEMA_STATEMENTS:
            conn.execute(statement)

        inserted_at = time.time()
        attestation_count = 0
        for row in receipt_rows:
            receipt, attestation = _convert_v1_receipt(row)
            _insert_receipt_row(conn, receipt, inserted_at)
            if attestation is not None:
                _insert_attestation_row(conn, attestation)
                attestation_count += 1

        previous_by_receipt: dict[str, str] = {}
        for row in observation_rows:
            observation = _convert_v1_observation(
                row,
                previous_observation_id=previous_by_receipt.get(
                    str(row["receipt_id"])
                ),
            )
            _insert_observation_row(conn, observation, inserted_at)
            previous_by_receipt[observation.receipt_id] = (
                observation.observation_id
            )

        _validate_migrated_rows(
            conn,
            expected_receipts=len(receipt_rows),
            expected_observations=len(observation_rows),
        )

        # Drop child before parent so foreign keys stay satisfied.
        conn.execute("DROP TABLE _receipt_v1_receipt_observations")
        conn.execute("DROP TABLE _receipt_v1_receipts")

        report = {
            "version": 1,
            "receipts": len(receipt_rows),
            "observations": len(observation_rows),
            "attestations": attestation_count,
            "completed_at": normalize_utc_timestamp(
                datetime.now(timezone.utc)
            ),
        }
        conn.execute(
            "INSERT INTO state_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_MIGRATION_META_KEY, _dump_json(report)),
        )
        conn.execute("COMMIT")
        return report
    except BaseException:
        conn.execute("ROLLBACK")
        raise
