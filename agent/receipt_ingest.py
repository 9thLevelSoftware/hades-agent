"""Deduplicated evidence snapshots from existing truth sources.

Task 4 of the Verified Outcome & Artifact Receipts plan. This module
owns:

- :func:`build_evidence_snapshot` — the one normalized, read-only,
  immutable :class:`~agent.receipt_models.EvidenceSnapshot` builder. It
  sorts by stable IDs, collapses identical evidence/artifact/claim
  content hashes, rejects conflicting duplicates, and validates that
  every claim cites at least one existing evidence digest. "No
  evidence" is itself a durable ``absence_observed`` digest — never a
  dangling reference.
- Three exact source adapters. :class:`TurnEvidenceSource` reads the
  ``turn_outcomes`` ledger, matching tool-call messages,
  ``agent_operations``, and each recorded root's verification state; the
  existing ledger result is recorded as ``turn_classification`` evidence
  and NEVER satisfies the requested end state on its own.
  :class:`MissionEvidenceSource` and :class:`TransactionEvidenceSource`
  read the vertical-slice mission/effect tables through
  existence-guarded lookups so they activate when that slice lands and
  fail truthfully when it has not; they never create a second missions
  or effects implementation.
- :class:`ReceiptIngestor` — idempotent-by-source issuance plus
  crash-safe :meth:`ReceiptIngestor.recover_projection`. Identical
  source content returns the existing receipt; changed content for a
  terminal source is a conflict (Task 6 turns it into a recheck
  observation), never a replacement receipt.

Every adapter is read-only, profile-bound (all databases resolve inside
one ``HADES_HOME``), and returns the same snapshot hash for the same
durable facts regardless of row order: the snapshot content hash
deliberately excludes the volatile ``captured_at`` capture time.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from agent.operation_journal import OperationJournal
from agent.receipt_artifacts import ArtifactCatalog, _redact_source_ref
from agent.receipt_hashing import canonical_content_hash, normalize_utc_timestamp
from agent.receipt_models import (
    EvidenceSnapshot,
    OperationEvidence,
    ReceiptDecision,
    ReceiptSourceKey,
    VerifiedReceiptDecision,
    _validate_traceability,
    build_claim,
    build_evidence_digest,
    build_operation_evidence,
    build_receipt,
    build_requested_outcome,
)
from agent.receipt_store import ReceiptSourceConflict, ReceiptStore
from agent.turn_ledger import fetch_turn_outcome
from agent.verification_evidence import (
    session_verification_roots,
    verification_state_for_root,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.receipt_models import (
        ArtifactDigest,
        EvidenceDigest,
        Receipt,
        ReceiptClaim,
        RequestedOutcome,
    )
    from hades_state import SessionDB

__all__ = [
    "EvidenceSourceError",
    "MissionEvidenceSource",
    "ReceiptIngestError",
    "ReceiptIngestor",
    "SnapshotConflictError",
    "TransactionEvidenceSource",
    "TurnEvidenceSource",
    "build_absence_evidence",
    "build_evidence_snapshot",
    "build_verification_evidence_digest",
]

_SNAPSHOT_MARKER_KIND = "evidence_snapshot"
_SNAPSHOT_MARKER_PRODUCER = "hermes.receipt-ingest"

# Ledger outcome vocabulary → "did the turn complete?" claim verdict.
_COMPLETED_OUTCOMES = frozenset({"verified", "completed_unverified"})
_UNSETTLED_OUTCOMES = frozenset({"partial", "unresolved", "interrupted"})


class ReceiptIngestError(RuntimeError):
    """Base error for receipt evidence ingestion failures."""


class EvidenceSourceError(ReceiptIngestError):
    """A source row/table/profile is missing, foreign, or unavailable."""


class SnapshotConflictError(ReceiptIngestError):
    """A terminal source's durable content changed after issuance."""


# ---------------------------------------------------------------------------
# Small deterministic helpers.
# ---------------------------------------------------------------------------


def _epoch_rfc3339(value: object) -> str:
    """Convert a durable epoch timestamp to canonical UTC RFC 3339."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReceiptIngestError(
            f"expected an epoch timestamp, got {value!r}"
        )
    return normalize_utc_timestamp(
        datetime.fromtimestamp(float(value), timezone.utc)
    )


def _now() -> str:
    return normalize_utc_timestamp(datetime.now(timezone.utc))


def _json_or(text: object, default: object) -> object:
    if not isinstance(text, str) or not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _dump_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row[1] == column for row in conn.execute(f'PRAGMA table_info("{table}")')
    )


def _read_only_connect(path: Path) -> sqlite3.Connection:
    """Open a foreign profile-local database strictly read-only."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_artifacts(
    catalog: ArtifactCatalog,
    artifact_ids: object,
    uncertainty: list[str],
) -> tuple:
    """Resolve referenced artifact IDs through the one shared catalog.

    Never re-registers bytes: identical bytes referenced by several
    sources keep one digest row. An unknown ID becomes uncertainty, not
    a dangling reference.
    """
    resolved = []
    ids = artifact_ids if isinstance(artifact_ids, (list, tuple)) else []
    for artifact_id in sorted({str(item) for item in ids if item}):
        digest = catalog.get(artifact_id)
        if digest is None:
            uncertainty.append(
                f"artifact {artifact_id} is referenced by the source but is "
                "not present in the artifact catalog"
            )
        else:
            resolved.append(digest)
    return tuple(resolved)


# ---------------------------------------------------------------------------
# Durable evidence digests shared by adapters and their tests.
# ---------------------------------------------------------------------------


def build_absence_evidence(
    *,
    scope: str,
    source_ref: str,
    producer_id: str,
    observed_at: str,
) -> "EvidenceDigest":
    """Record "no evidence exists" as a durable evidence digest.

    Absence is a fact with a source, scope, and timestamp — it is never
    represented by a dangling reference. ``observed_at`` must be a
    durable fact timestamp (for example the subject row's creation time)
    so re-reads of the same durable state stay hash-identical.
    """
    return build_evidence_digest(
        evidence_kind="absence_observed",
        source_ref=_redact_source_ref(source_ref),
        producer_id=producer_id,
        observed_at=observed_at,
        summary=f"no evidence observed for {scope}",
        payload_hash=canonical_content_hash({"absence_scope": scope}),
    )


def build_verification_evidence_digest(state: Mapping[str, Any]) -> "EvidenceDigest":
    """Project one recorded verification root state into evidence.

    ``state`` is the mapping returned by
    :func:`agent.verification_evidence.verification_state_for_root`. The
    digest is deterministic for the same durable rows and redacts
    sensitive absolute path prefixes before anything is hashed.
    """
    root = _redact_source_ref(str(state.get("root") or ""))
    if not root:
        raise ReceiptIngestError("verification state has no root")
    session_id = str(state.get("session_id") or "default")
    status = str(state.get("status") or "unverified")
    event = state.get("evidence")
    event_facts = None
    if isinstance(event, Mapping) and event:
        event_facts = {
            key: event.get(key)
            for key in (
                "canonical_command",
                "kind",
                "scope",
                "status",
                "exit_code",
                "created_at",
            )
        }
    observed_at = None
    if event_facts and event_facts.get("created_at"):
        observed_at = str(event_facts["created_at"])
    elif state.get("last_edit_at"):
        observed_at = str(state["last_edit_at"])
    if observed_at is None:
        raise ReceiptIngestError(
            f"verification state for root {root} has no durable timestamp"
        )
    changed_paths = [
        _redact_source_ref(str(path))
        for path in (state.get("changed_paths") or [])
    ]
    payload = {
        "root": root,
        "session_id": session_id,
        "status": status,
        "changed_paths": changed_paths,
        "event": event_facts,
        "last_edit_at": state.get("last_edit_at"),
    }
    summary = f"verification {status} for {root}"
    if event_facts and event_facts.get("canonical_command"):
        summary += f" ({event_facts['canonical_command']})"
    return build_evidence_digest(
        evidence_kind="verification_check",
        source_ref=f"verification_evidence.db:{session_id}:{root}",
        producer_id="hermes.verification",
        observed_at=normalize_utc_timestamp(observed_at),
        summary=summary,
        payload_hash=canonical_content_hash(payload),
    )


# ---------------------------------------------------------------------------
# The normalized read-only evidence envelope.
# ---------------------------------------------------------------------------


def _dedupe_by_hash(items: tuple, id_attr: str, what: str) -> list:
    """Collapse identical content hashes; reject conflicting duplicates."""
    by_id: dict[str, object] = {}
    for item in items:
        key = getattr(item, id_attr)
        existing = by_id.get(key)
        if existing is None:
            by_id[key] = item
        elif existing.content_hash != item.content_hash:  # type: ignore[attr-defined]
            raise ReceiptIngestError(
                f"conflicting duplicate {what} {key!r}: identical identity "
                "with different content is a conflict, not a merge"
            )
    return sorted(by_id.values(), key=lambda item: getattr(item, id_attr))


def _sorted_unique(values: object, name: str) -> tuple[str, ...]:
    items = tuple(values or ())  # type: ignore[arg-type]
    for item in items:
        if not isinstance(item, str):
            raise ReceiptIngestError(f"{name} entries must be strings: {item!r}")
    return tuple(sorted(dict.fromkeys(items)))


def build_evidence_snapshot(
    *,
    source: ReceiptSourceKey,
    subject_kind: str,
    subject_id: str,
    producer_id: str,
    requested_outcome: "RequestedOutcome",
    claims: tuple = (),
    evidence: tuple = (),
    artifacts: tuple = (),
    operation_states: tuple = (),
    blocked_reasons: tuple = (),
    known_failures: tuple = (),
    uncertainty: tuple = (),
    captured_at: str | None = None,
) -> EvidenceSnapshot:
    """Build the one normalized immutable evidence envelope.

    Deterministic for the same durable facts regardless of input row
    order: collections sort by stable IDs, identical content hashes
    collapse, conflicting duplicates are rejected, and the content hash
    excludes the volatile ``captured_at``. Every claim must cite at
    least one existing evidence digest — "no evidence" must be recorded
    as an ``absence_observed`` digest first.
    """
    if not isinstance(source, ReceiptSourceKey):
        raise ReceiptIngestError(f"source must be a ReceiptSourceKey: {source!r}")
    sorted_claims = tuple(_dedupe_by_hash(tuple(claims), "claim_id", "claim"))
    sorted_evidence = tuple(
        _dedupe_by_hash(tuple(evidence), "evidence_id", "evidence")
    )
    sorted_artifacts = tuple(
        _dedupe_by_hash(tuple(artifacts), "artifact_id", "artifact")
    )
    sorted_operations = tuple(
        _dedupe_by_hash(tuple(operation_states), "operation_id", "operation")
    )
    for operation in sorted_operations:
        if not isinstance(operation, OperationEvidence):
            raise ReceiptIngestError(
                f"operation_states entries must be OperationEvidence: "
                f"{operation!r}"
            )
    for claim in sorted_claims:
        if not claim.evidence_ids:
            raise ReceiptIngestError(
                f"claim {claim.claim_id!r} cites no evidence; every claimed "
                "effect needs at least one existing evidence digest "
                "(absence itself is durable absence_observed evidence)"
            )
    try:
        _validate_traceability(sorted_claims, sorted_evidence, sorted_artifacts)
    except ValueError as exc:
        raise ReceiptIngestError(str(exc)) from exc
    blocked = _sorted_unique(blocked_reasons, "blocked_reasons")
    failures = _sorted_unique(known_failures, "known_failures")
    uncertain = _sorted_unique(uncertainty, "uncertainty")
    hash_body = {
        "source": {
            "source_kind": source.source_kind,
            "source_id": source.source_id,
        },
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "producer_id": producer_id,
        "requested_outcome": requested_outcome.content_hash,
        "claims": [c.content_hash for c in sorted_claims],
        "evidence": [e.content_hash for e in sorted_evidence],
        "artifacts": [a.content_hash for a in sorted_artifacts],
        "operation_states": [o.content_hash for o in sorted_operations],
        "blocked_reasons": list(blocked),
        "known_failures": list(failures),
        "uncertainty": list(uncertain),
    }
    return EvidenceSnapshot(
        source=source,
        subject_kind=subject_kind,
        subject_id=subject_id,
        producer_id=producer_id,
        requested_outcome=requested_outcome,
        claims=sorted_claims,
        evidence=sorted_evidence,
        artifacts=sorted_artifacts,
        operation_states=sorted_operations,
        blocked_reasons=blocked,
        known_failures=failures,
        uncertainty=uncertain,
        captured_at=normalize_utc_timestamp(captured_at) if captured_at else _now(),
        content_hash=canonical_content_hash(hash_body),
    )


# ---------------------------------------------------------------------------
# Bound source: lets ReceiptIngestor.issue(source) take a subject-bound
# adapter without re-threading subject identifiers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BoundEvidenceSource:
    _snapshot: Callable[..., EvidenceSnapshot]
    _args: tuple

    def snapshot(self) -> EvidenceSnapshot:
        return self._snapshot(*self._args)


# ---------------------------------------------------------------------------
# Turn evidence source.
# ---------------------------------------------------------------------------


class TurnEvidenceSource:
    """Read-only projection of one recorded turn into evidence.

    The turn ledger's ``verified`` outcome is preserved for
    compatibility but treated only as an untrusted source claim: it is
    recorded as ``turn_classification`` evidence and the
    ``requested-end-state`` claim stays ``unknown`` until an independent
    scorer decides.
    """

    producer_id = "hermes.turn-ledger"

    def __init__(
        self,
        db: "SessionDB",
        *,
        catalog: ArtifactCatalog | None = None,
    ) -> None:
        self.db = db
        self._catalog = catalog if catalog is not None else ArtifactCatalog(db)

    def bind(self, session_id: str, turn_id: str) -> _BoundEvidenceSource:
        return _BoundEvidenceSource(self.snapshot, (session_id, turn_id))

    def snapshot(self, session_id: str, turn_id: str) -> EvidenceSnapshot:
        record = fetch_turn_outcome(self.db, session_id, turn_id)
        if record is None:
            raise EvidenceSourceError(
                f"unknown turn {session_id}:{turn_id}: no turn_outcomes row"
            )
        observed_at = _epoch_rfc3339(record.created_at)
        evidence: list = []
        uncertainty: list[str] = []
        blocked_reasons: list[str] = []
        known_failures: list[str] = []

        turn_evidence = build_evidence_digest(
            evidence_kind="turn_classification",
            source_ref=f"state.db:turn_outcomes:{session_id}:{turn_id}",
            producer_id=self.producer_id,
            observed_at=observed_at,
            summary=(
                f"turn ledger outcome {record.outcome!r} "
                "(untrusted source claim)"
            ),
            payload_hash=canonical_content_hash(dataclasses.asdict(record)),
        )
        evidence.append(turn_evidence)

        if record.outcome == "verified":
            uncertainty.append(
                "turn ledger 'verified' outcome is an untrusted source claim; "
                "only an independent scorer can verify the requested end state"
            )
        elif record.outcome == "failed":
            known_failures.append(
                f"turn ledger reports failure: "
                f"{record.outcome_reason or 'turn failed'}"
            )
        elif record.outcome == "blocked":
            blocked_reasons.append(
                f"turn ledger reports blocked: "
                f"{record.outcome_reason or 'approval blocked'}"
            )
        elif record.outcome in _UNSETTLED_OUTCOMES:
            uncertainty.append(
                f"turn outcome {record.outcome!r} left the turn unsettled"
            )

        operations = OperationJournal(self.db).list_for_turn(session_id, turn_id)
        operation_states = tuple(
            build_operation_evidence(
                operation_id=op.operation_id,
                operation_kind=op.kind,
                state=op.state,
                effect_disposition=op.effect_disposition,
                source_ref=f"state.db:agent_operations:{op.operation_id}",
                observed_at=_epoch_rfc3339(op.updated_at),
            )
            for op in operations
        )
        for op in operations:
            if op.state == "unknown" or op.effect_disposition == "unknown":
                uncertainty.append(
                    f"operation {op.operation_id} has an unknown effect "
                    "disposition; its landing is ambiguous"
                )
        evidence.extend(
            self._tool_result_evidence(session_id, operations)
        )

        verification_ids: list[str] = []
        for root in session_verification_roots(session_id):
            state = verification_state_for_root(session_id=session_id, root=root)
            digest = build_verification_evidence_digest(state)
            evidence.append(digest)
            verification_ids.append(digest.evidence_id)
            status = state["status"]
            redacted_root = _redact_source_ref(root)
            if status == "stale":
                uncertainty.append(
                    "verification evidence is stale after a later edit for "
                    f"root {redacted_root}"
                )
            elif status == "failed":
                known_failures.append(
                    f"verification failed for root {redacted_root}"
                )
            elif status not in ("passed",):
                uncertainty.append(
                    f"verification is {status} for root {redacted_root}"
                )
        outcome_kind = "code_change" if verification_ids else "turn_outcome"
        if not verification_ids:
            absence = build_absence_evidence(
                scope=f"verification:{session_id}:{turn_id}",
                source_ref="verification_evidence.db",
                producer_id=self.producer_id,
                observed_at=observed_at,
            )
            evidence.append(absence)
            verification_ids.append(absence.evidence_id)
            uncertainty.append(
                "no verification evidence exists for this turn"
            )

        artifacts = self._turn_artifacts(session_id, turn_id)

        if record.outcome in _COMPLETED_OUTCOMES:
            completed_verdict = "satisfied"
        elif record.outcome in _UNSETTLED_OUTCOMES:
            completed_verdict = "unknown"
        else:
            completed_verdict = "unsatisfied"
        claims = (
            build_claim(
                claim_kind="turn-completed",
                statement="the turn reached a terminal ledger outcome",
                observed_json=_dump_json(
                    {"outcome": record.outcome, "reason": record.outcome_reason}
                ),
                evidence_ids=(turn_evidence.evidence_id,),
                required=True,
                verdict=completed_verdict,
            ),
            build_claim(
                claim_kind="requested-end-state",
                statement="the requested end state independently holds",
                evidence_ids=tuple(verification_ids),
                artifact_ids=tuple(a.artifact_id for a in artifacts),
                required=True,
                # Never satisfied at ingest: only the independent scorer
                # may decide this, and never from the ledger label alone.
                verdict="unknown",
            ),
        )
        requested = build_requested_outcome(
            outcome_kind=outcome_kind,
            description=f"requested end state for turn {session_id}:{turn_id}",
            producer_id=self.producer_id,
        )
        return build_evidence_snapshot(
            source=ReceiptSourceKey("turn", f"{session_id}:{turn_id}"),
            subject_kind="turn",
            subject_id=f"{session_id}:{turn_id}",
            producer_id=self.producer_id,
            requested_outcome=requested,
            claims=claims,
            evidence=tuple(evidence),
            artifacts=artifacts,
            operation_states=operation_states,
            blocked_reasons=tuple(blocked_reasons),
            known_failures=tuple(known_failures),
            uncertainty=tuple(uncertainty),
        )

    def _tool_result_evidence(self, session_id: str, operations: list) -> list:
        """Match recorded tool-call IDs back to their persisted messages."""
        tool_call_ids = sorted(
            {op.tool_call_id for op in operations if op.tool_call_id}
        )
        if not tool_call_ids:
            return []

        def _read(conn: sqlite3.Connection):
            rows = []
            for tool_call_id in tool_call_ids:
                row = conn.execute(
                    "SELECT id, tool_call_id, tool_name, content, timestamp "
                    "FROM messages WHERE session_id = ? AND tool_call_id = ? "
                    "AND role = 'tool' ORDER BY id LIMIT 1",
                    (session_id, tool_call_id),
                ).fetchone()
                if row is not None:
                    rows.append(dict(row))
            return rows

        digests = []
        for row in self.db._execute_read(_read):
            digests.append(
                build_evidence_digest(
                    evidence_kind="tool_result",
                    source_ref=f"state.db:messages:{row['id']}",
                    producer_id=self.producer_id,
                    observed_at=_epoch_rfc3339(row["timestamp"]),
                    summary=(
                        f"tool result for call {row['tool_call_id']}"
                        + (f" ({row['tool_name']})" if row["tool_name"] else "")
                    ),
                    payload_hash=canonical_content_hash(
                        {
                            "tool_call_id": row["tool_call_id"],
                            "tool_name": row["tool_name"],
                            "content": row["content"],
                        }
                    ),
                )
            )
        return digests

    def _turn_artifacts(self, session_id: str, turn_id: str) -> tuple:
        """Resolve artifacts the turn registered through the catalog."""
        prefix = f"{session_id}:{turn_id}:"

        def _read(conn: sqlite3.Connection) -> list[str]:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT artifact_id FROM artifact_locations "
                    "WHERE source_kind = 'execute_code' AND source_ref LIKE ? "
                    "ORDER BY artifact_id",
                    (prefix + "%",),
                )
            ]

        uncertainty: list[str] = []
        return _resolve_artifacts(
            self._catalog, self.db._execute_read(_read), uncertainty
        )


# ---------------------------------------------------------------------------
# Mission evidence source (vertical-slice tables, existence-guarded).
# ---------------------------------------------------------------------------


class MissionEvidenceSource:
    """Read-only projection of one mission aggregate into evidence.

    Reads the vertical-slice ``missions``/``mission_events``/
    ``mission_execution_links``/``mission_review_items`` tables through
    existence-guarded lookups; the adapter activates when that slice
    lands and fails truthfully (:class:`EvidenceSourceError`) when it
    has not. It never copies workflow retry/node state into receipt
    tables and never crosses a profile boundary.
    """

    producer_id = "hermes.missions"

    def __init__(
        self,
        db: "SessionDB",
        *,
        workflows_db_path: Path | None = None,
        profile: str | None = None,
        catalog: ArtifactCatalog | None = None,
    ) -> None:
        self.db = db
        self._workflows_db_path = (
            Path(workflows_db_path) if workflows_db_path is not None else None
        )
        self._profile = profile
        self._catalog = catalog if catalog is not None else ArtifactCatalog(db)

    def bind(self, mission_id: str) -> _BoundEvidenceSource:
        return _BoundEvidenceSource(self.snapshot, (mission_id,))

    def _resolve_workflows_path(self) -> Path:
        if self._workflows_db_path is not None:
            return self._workflows_db_path
        from hades_cli.workflows_db import workflows_db_path

        return workflows_db_path()

    def snapshot(self, mission_id: str) -> EvidenceSnapshot:
        path = self._resolve_workflows_path()
        if not path.exists():
            raise EvidenceSourceError(
                "mission source unavailable: no workflows database exists in "
                "this profile"
            )
        conn = _read_only_connect(path)
        try:
            if not _table_exists(conn, "missions"):
                raise EvidenceSourceError(
                    "mission source unavailable: the missions table does not "
                    "exist yet (vertical slice not installed)"
                )
            row = conn.execute(
                "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            if row is None:
                raise EvidenceSourceError(f"unknown mission {mission_id!r}")
            mission = dict(row)
            links = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM mission_execution_links "
                    "WHERE mission_id = ? ORDER BY execution_id",
                    (mission_id,),
                )
            ]
            events = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM mission_events WHERE mission_id = ? "
                    "ORDER BY id",
                    (mission_id,),
                )
            ] if _table_exists(conn, "mission_events") else []
            reviews = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM mission_review_items WHERE mission_id = ? "
                    "ORDER BY review_id",
                    (mission_id,),
                )
            ] if _table_exists(conn, "mission_review_items") else []
        finally:
            conn.close()

        profile = mission.get("profile")
        if self._profile is not None and profile != self._profile:
            raise EvidenceSourceError(
                f"mission {mission_id!r} belongs to profile {profile!r}, not "
                f"{self._profile!r}; evidence sources never cross profiles"
            )

        created_at = _epoch_rfc3339(mission["created_at"])
        updated_at = _epoch_rfc3339(
            mission.get("updated_at") or mission["created_at"]
        )
        status = str(mission.get("status") or "")
        evidence: list = []
        uncertainty: list[str] = []
        blocked_reasons: list[str] = []
        known_failures: list[str] = []

        mission_record = build_evidence_digest(
            evidence_kind="mission_record",
            source_ref=f"workflows.db:missions:{mission_id}",
            producer_id=self.producer_id,
            observed_at=updated_at,
            summary=f"mission status {status!r} (untrusted source claim)",
            payload_hash=canonical_content_hash(mission),
        )
        evidence.append(mission_record)

        end_state_ids = [mission_record.evidence_id]
        mission_evidence = _json_or(mission.get("evidence_json"), {})
        if not isinstance(mission_evidence, dict):
            mission_evidence = {}
        for key, kind, observed in (
            ("before", "before_observation", created_at),
            ("after", "after_observation", updated_at),
        ):
            payload = mission_evidence.get(key)
            if payload is None:
                continue
            digest = build_evidence_digest(
                evidence_kind=kind,
                source_ref=f"workflows.db:missions:{mission_id}:evidence:{key}",
                producer_id=self.producer_id,
                observed_at=observed,
                summary=f"mission {key} observation",
                payload_hash=canonical_content_hash({key: payload}),
            )
            evidence.append(digest)
            end_state_ids.append(digest.evidence_id)

        for link in links:
            evidence.append(
                build_evidence_digest(
                    evidence_kind="mission_execution_link",
                    source_ref=(
                        f"workflows.db:mission_execution_links:{mission_id}:"
                        f"{link['execution_id']}"
                    ),
                    producer_id=self.producer_id,
                    observed_at=_epoch_rfc3339(link["linked_at"]),
                    summary=(
                        f"{link.get('relation') or 'primary'} execution "
                        f"{link['execution_id']}"
                    ),
                    payload_hash=canonical_content_hash(link),
                )
            )

        for event in events:
            payload = _json_or(event.get("payload_json"), {})
            step_id = (
                payload.get("step_id") if isinstance(payload, dict) else None
            )
            summary = f"mission event {event['kind']!r}"
            if step_id:
                summary += f" for step {step_id}"
            evidence.append(
                build_evidence_digest(
                    evidence_kind="mission_event",
                    source_ref=f"workflows.db:mission_events:{event['id']}",
                    producer_id=self.producer_id,
                    observed_at=_epoch_rfc3339(event["created_at"]),
                    summary=summary,
                    payload_hash=canonical_content_hash(event),
                )
            )

        for review in reviews:
            evidence.append(
                build_evidence_digest(
                    evidence_kind="mission_review_item",
                    source_ref=(
                        f"workflows.db:mission_review_items:{review['review_id']}"
                    ),
                    producer_id=self.producer_id,
                    observed_at=_epoch_rfc3339(review["created_at"]),
                    summary=(
                        f"review {review['review_id']} {review['status']}: "
                        f"{review['kind']}"
                    ),
                    payload_hash=canonical_content_hash(review),
                )
            )
            if review.get("resolved_at") is None and review.get("status") in (
                "pending",
                "open",
                "blocked",
            ):
                blocked_reasons.append(
                    f"review {review['review_id']} is {review['status']}: "
                    f"{review['kind']}"
                )

        if status == "blocked":
            blocked_reasons.append("mission status is blocked")
        elif status == "failed":
            known_failures.append("mission status is failed")

        transaction_claims, operation_states = self._transaction_facts(
            mission_id, evidence, uncertainty
        )
        self._outbox_facts(mission_id, evidence, uncertainty)

        artifacts = _resolve_artifacts(
            self._catalog, mission_evidence.get("artifact_ids"), uncertainty
        )

        if status in ("completed", "succeeded"):
            recorded_verdict = "satisfied"
        elif status in ("failed", "cancelled"):
            recorded_verdict = "unsatisfied"
        else:
            recorded_verdict = "unknown"
        claims = (
            build_claim(
                claim_kind="mission-recorded",
                statement="the mission recorded a terminal status",
                observed_json=_dump_json({"status": status}),
                evidence_ids=(mission_record.evidence_id,),
                required=True,
                verdict=recorded_verdict,
            ),
            build_claim(
                claim_kind="requested-end-state",
                statement="the requested mission end state independently holds",
                evidence_ids=tuple(end_state_ids),
                artifact_ids=tuple(a.artifact_id for a in artifacts),
                required=True,
                verdict="unknown",
            ),
        ) + transaction_claims

        constraints = tuple(
            str(item)
            for item in (_json_or(mission.get("constraints_json"), []) or [])
            if isinstance(item, str)
        )
        requested = build_requested_outcome(
            outcome_kind="mission_outcome",
            description=str(mission.get("objective") or f"mission {mission_id}"),
            constraints=constraints,
            producer_id=self.producer_id,
        )
        return build_evidence_snapshot(
            source=ReceiptSourceKey("mission", mission_id),
            subject_kind="mission",
            subject_id=mission_id,
            producer_id=self.producer_id,
            requested_outcome=requested,
            claims=claims,
            evidence=tuple(evidence),
            artifacts=artifacts,
            operation_states=operation_states,
            blocked_reasons=tuple(blocked_reasons),
            known_failures=tuple(known_failures),
            uncertainty=tuple(uncertainty),
        )

    def _transaction_facts(
        self,
        mission_id: str,
        evidence: list,
        uncertainty: list[str],
    ) -> tuple[tuple, tuple]:
        """Existence-guarded read of the mission's effect transactions."""

        def _read(conn: sqlite3.Connection):
            if not _table_exists(conn, "effect_transactions"):
                return []
            rows = []
            for row in conn.execute(
                "SELECT * FROM effect_transactions WHERE mission_id = ? "
                "ORDER BY sequence_no, transaction_id",
                (mission_id,),
            ):
                tx = dict(row)
                op_row = conn.execute(
                    "SELECT * FROM agent_operations WHERE operation_id = ?",
                    (tx["operation_id"],),
                ).fetchone()
                rows.append((tx, dict(op_row) if op_row else None))
            return rows

        claims: list = []
        operation_states: list = []
        for tx, op in self.db._execute_read(_read):
            digest = build_evidence_digest(
                evidence_kind="effect_transaction",
                source_ref=(
                    f"state.db:effect_transactions:{tx['transaction_id']}"
                ),
                producer_id=self.producer_id,
                observed_at=_epoch_rfc3339(tx["updated_at"]),
                summary=(
                    f"effect transaction {tx['transaction_id']} phase "
                    f"{tx['phase']!r}"
                ),
                payload_hash=canonical_content_hash(tx),
            )
            evidence.append(digest)
            verdict = "unknown"
            if op is None:
                uncertainty.append(
                    f"transaction {tx['transaction_id']} has no operation "
                    "journal row; its landing is unknown"
                )
            else:
                operation_states.append(
                    build_operation_evidence(
                        operation_id=op["operation_id"],
                        operation_kind=op["kind"],
                        state=op["state"],
                        effect_disposition=op["effect_disposition"],
                        source_ref=(
                            f"state.db:agent_operations:{op['operation_id']}"
                        ),
                        observed_at=_epoch_rfc3339(op["updated_at"]),
                    )
                )
                if (
                    op["state"] == "unknown"
                    or op["effect_disposition"] == "unknown"
                ):
                    uncertainty.append(
                        f"operation {op['operation_id']} for transaction "
                        f"{tx['transaction_id']} has an unknown effect "
                        "disposition"
                    )
                elif (
                    op["state"] == "confirmed"
                    and tx["phase"] in ("committed", "compensated")
                ):
                    verdict = "satisfied"
                elif op["state"] == "failed" or tx["phase"] == "failed":
                    verdict = "unsatisfied"
            claims.append(
                build_claim(
                    claim_kind="effect",
                    statement=(
                        f"effect transaction {tx['transaction_id']} landed "
                        "its declared effect"
                    ),
                    observed_json=_dump_json(
                        {
                            "phase": tx["phase"],
                            "operation_state": op["state"] if op else None,
                        }
                    ),
                    evidence_ids=(digest.evidence_id,),
                    required=True,
                    verdict=verdict,
                )
            )
        return tuple(claims), tuple(operation_states)

    def _outbox_facts(
        self, mission_id: str, evidence: list, uncertainty: list[str]
    ) -> None:
        """Existence-guarded read of the mission's delayed outbox rows."""

        def _read(conn: sqlite3.Connection):
            if not _table_exists(conn, "mission_outbox"):
                return []
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM mission_outbox WHERE mission_id = ? "
                    "ORDER BY outbox_id",
                    (mission_id,),
                )
            ]

        for row in self.db._execute_read(_read):
            evidence.append(
                build_evidence_digest(
                    evidence_kind="outbox_record",
                    source_ref=f"state.db:mission_outbox:{row['outbox_id']}",
                    producer_id=self.producer_id,
                    observed_at=_epoch_rfc3339(row["updated_at"]),
                    summary=(
                        f"outbox delivery {row['outbox_id']} status "
                        f"{row['status']!r}"
                    ),
                    payload_hash=canonical_content_hash(row),
                )
            )
            if row["status"] in ("dispatched", "unknown"):
                uncertainty.append(
                    f"outbox delivery {row['outbox_id']} is {row['status']}; "
                    "its landing is unconfirmed"
                )


# ---------------------------------------------------------------------------
# Transaction evidence source (vertical-slice fallback, existence-guarded).
# ---------------------------------------------------------------------------

# Ordered probe: richer portfolio-item-#2 transaction tables are preferred
# when a later slice adds them; the vertical-slice table is the fallback.
_TRANSACTION_TABLES = ("effect_transactions",)


class TransactionEvidenceSource:
    """Read-only projection of one effect transaction into evidence.

    Reads transaction/revision/preview/authority lineage from the first
    existing transaction table (item #2 tables when present, else the
    vertical-slice ``effect_transactions``); it never creates a second
    effect journal and treats any unknown journal/dispatch state as
    uncertainty.
    """

    producer_id = "hermes.effect-transactions"

    def __init__(
        self,
        db: "SessionDB",
        *,
        catalog: ArtifactCatalog | None = None,
    ) -> None:
        self.db = db
        self._catalog = catalog if catalog is not None else ArtifactCatalog(db)

    def bind(self, transaction_id: str) -> _BoundEvidenceSource:
        return _BoundEvidenceSource(self.snapshot, (transaction_id,))

    def snapshot(self, transaction_id: str) -> EvidenceSnapshot:
        def _read(conn: sqlite3.Connection):
            table = next(
                (name for name in _TRANSACTION_TABLES if _table_exists(conn, name)),
                None,
            )
            if table is None:
                return None
            row = conn.execute(
                f"SELECT * FROM {table} WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if row is None:
                return (table, None, None, [])
            tx = dict(row)
            op_row = conn.execute(
                "SELECT * FROM agent_operations WHERE operation_id = ?",
                (tx["operation_id"],),
            ).fetchone()
            outbox = []
            if _table_exists(conn, "mission_outbox"):
                outbox = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM mission_outbox WHERE transaction_id = ? "
                        "ORDER BY outbox_id",
                        (transaction_id,),
                    )
                ]
            return (table, tx, dict(op_row) if op_row else None, outbox)

        result = self.db._execute_read(_read)
        if result is None:
            raise EvidenceSourceError(
                "transaction source unavailable: no effect transaction tables "
                "exist in this profile (vertical slice not installed)"
            )
        table, tx, op, outbox_rows = result
        if tx is None:
            raise EvidenceSourceError(
                f"unknown transaction {transaction_id!r} in {table}"
            )

        updated_at = _epoch_rfc3339(tx["updated_at"])
        evidence: list = []
        uncertainty: list[str] = []
        known_failures: list[str] = []
        operation_states: list = []

        tx_record = build_evidence_digest(
            evidence_kind="transaction_record",
            source_ref=f"state.db:{table}:{transaction_id}",
            producer_id=self.producer_id,
            observed_at=updated_at,
            summary=(
                f"effect transaction {transaction_id} phase {tx['phase']!r} "
                "(untrusted source claim)"
            ),
            payload_hash=canonical_content_hash(tx),
        )
        evidence.append(tx_record)

        lineage_payload = {
            "transaction_id": transaction_id,
            "mission_id": tx.get("mission_id"),
            "execution_id": tx.get("execution_id"),
            "step_id": tx.get("step_id"),
            "adapter_id": tx.get("adapter_id"),
            "sequence_no": tx.get("sequence_no"),
            "depends_on": _json_or(tx.get("depends_on_json"), []),
            "semantics_hash": canonical_content_hash(
                _json_or(tx.get("semantics_json"), None)
            ),
            "preview_hash": canonical_content_hash(
                _json_or(tx.get("preview_json"), None)
            ),
            "authority_hash": canonical_content_hash(
                _json_or(tx.get("authority_json"), None)
            ),
            "revision_hash": canonical_content_hash(
                {
                    "transaction_id": transaction_id,
                    "sequence_no": tx.get("sequence_no"),
                    "phase": tx.get("phase"),
                }
            ),
        }
        lineage = build_evidence_digest(
            evidence_kind="transaction_lineage",
            source_ref=f"state.db:{table}:{transaction_id}:lineage",
            producer_id=self.producer_id,
            observed_at=updated_at,
            summary=(
                f"transaction {transaction_id} revision/graph/preview/"
                "authority lineage hashes"
            ),
            payload_hash=canonical_content_hash(lineage_payload),
        )
        evidence.append(lineage)

        end_state_ids = [tx_record.evidence_id, lineage.evidence_id]
        for column, kind, summary in (
            (
                "verification_json",
                "adapter_postcondition",
                "adapter postcondition evidence",
            ),
            (
                "compensation_json",
                "compensation_record",
                "compensation record",
            ),
        ):
            payload = _json_or(tx.get(column), None)
            if payload is None:
                continue
            digest = build_evidence_digest(
                evidence_kind=kind,
                source_ref=f"state.db:{table}:{transaction_id}:{column}",
                producer_id=self.producer_id,
                observed_at=updated_at,
                summary=f"{summary} for transaction {transaction_id}",
                payload_hash=canonical_content_hash({column: payload}),
            )
            evidence.append(digest)
            end_state_ids.append(digest.evidence_id)

        committed_verdict = "unknown"
        if op is None:
            uncertainty.append(
                f"transaction {transaction_id} has no operation journal row; "
                "its landing is unknown"
            )
        else:
            operation_states.append(
                build_operation_evidence(
                    operation_id=op["operation_id"],
                    operation_kind=op["kind"],
                    state=op["state"],
                    effect_disposition=op["effect_disposition"],
                    source_ref=f"state.db:agent_operations:{op['operation_id']}",
                    observed_at=_epoch_rfc3339(op["updated_at"]),
                )
            )
            if op["state"] == "unknown" or op["effect_disposition"] == "unknown":
                uncertainty.append(
                    f"operation {op['operation_id']} for transaction "
                    f"{transaction_id} has an unknown effect disposition; "
                    "its landing is ambiguous"
                )
            elif op["state"] == "confirmed" and tx["phase"] in (
                "committed",
                "compensated",
            ):
                committed_verdict = "satisfied"
            elif op["state"] == "failed" or tx["phase"] == "failed":
                committed_verdict = "unsatisfied"
                known_failures.append(
                    f"transaction {transaction_id} failed "
                    f"(phase {tx['phase']!r}, operation {op['state']!r})"
                )

        for row in outbox_rows:
            evidence.append(
                build_evidence_digest(
                    evidence_kind="outbox_record",
                    source_ref=f"state.db:mission_outbox:{row['outbox_id']}",
                    producer_id=self.producer_id,
                    observed_at=_epoch_rfc3339(row["updated_at"]),
                    summary=(
                        f"outbox delivery {row['outbox_id']} status "
                        f"{row['status']!r}"
                    ),
                    payload_hash=canonical_content_hash(row),
                )
            )
            if row["status"] in ("dispatched", "unknown"):
                uncertainty.append(
                    f"outbox delivery {row['outbox_id']} is {row['status']}; "
                    "its landing is unconfirmed"
                )

        result_payload = _json_or(tx.get("result_json"), {})
        artifacts = _resolve_artifacts(
            self._catalog,
            result_payload.get("artifact_ids")
            if isinstance(result_payload, dict)
            else [],
            uncertainty,
        )

        claims = (
            build_claim(
                claim_kind="transaction-committed",
                statement=(
                    f"effect transaction {transaction_id} committed its "
                    "declared effect"
                ),
                observed_json=_dump_json(
                    {
                        "phase": tx["phase"],
                        "operation_state": op["state"] if op else None,
                        "effect_disposition": (
                            op["effect_disposition"] if op else None
                        ),
                    }
                ),
                evidence_ids=(tx_record.evidence_id,),
                required=True,
                verdict=committed_verdict,
            ),
            build_claim(
                claim_kind="requested-end-state",
                statement=(
                    "the requested transaction end state independently holds"
                ),
                evidence_ids=tuple(end_state_ids),
                artifact_ids=tuple(a.artifact_id for a in artifacts),
                required=True,
                verdict="unknown",
            ),
        )
        requested = build_requested_outcome(
            outcome_kind="transaction_commit",
            description=f"commit effect transaction {transaction_id}",
            producer_id=self.producer_id,
        )
        return build_evidence_snapshot(
            source=ReceiptSourceKey("transaction", transaction_id),
            subject_kind="transaction",
            subject_id=transaction_id,
            producer_id=self.producer_id,
            requested_outcome=requested,
            claims=claims,
            evidence=tuple(evidence),
            artifacts=artifacts,
            operation_states=tuple(operation_states),
            known_failures=tuple(known_failures),
            uncertainty=tuple(uncertainty),
        )


# ---------------------------------------------------------------------------
# Idempotent issuance and crash-safe projection recovery.
# ---------------------------------------------------------------------------


def _stored_snapshot_hash(receipt: "Receipt") -> str | None:
    """Recover the snapshot hash a receipt was issued from, if recorded."""
    for item in receipt.evidence:
        if (
            item.evidence_kind == _SNAPSHOT_MARKER_KIND
            and item.producer_id == _SNAPSHOT_MARKER_PRODUCER
        ):
            return item.payload_hash
    return None


class ReceiptIngestor:
    """Issue receipts from evidence snapshots, idempotently by source.

    ``decide`` is the scoring seam: Task 5's ``ReceiptScoringService``
    plugs in here (Task 6 wires it); it receives the immutable snapshot
    and returns an ordinary ``ReceiptDecision`` or the sealed
    ``VerifiedReceiptDecision``. The ingestor itself never invents a
    status and never mutates a source.
    """

    def __init__(
        self,
        store: ReceiptStore,
        *,
        decide: Callable[[EvidenceSnapshot], object],
        workflows_db_path: Path | None = None,
    ) -> None:
        self._store = store
        self._decide = decide
        self._workflows_db_path = (
            Path(workflows_db_path) if workflows_db_path is not None else None
        )

    # ── Issue ──

    def issue(self, source: object) -> "Receipt":
        """Compute/accept a snapshot and return the one receipt for it.

        An identical source returns the existing receipt; changed
        content for a terminal source raises
        :class:`SnapshotConflictError` — it must become a recheck
        observation, never a replacement receipt.
        """
        if isinstance(source, EvidenceSnapshot):
            snapshot = source
        elif hasattr(source, "snapshot") and callable(source.snapshot):
            snapshot = source.snapshot()
        else:
            raise ReceiptIngestError(
                "issue() takes an EvidenceSnapshot or a bound evidence source"
            )
        existing = self._store.find_by_source(snapshot.source)
        if existing is not None:
            return self._match_existing(existing, snapshot)
        decision = self._decide(snapshot)
        receipt = self._build_receipt(snapshot, decision)
        seal = decision if isinstance(decision, VerifiedReceiptDecision) else None
        try:
            return self._store.insert(receipt, decision=seal)
        except ReceiptSourceConflict:
            # A concurrent process inserted first (crash/recovery race):
            # the source key + content hash repair the projection.
            racing = self._store.find_by_source(snapshot.source)
            if racing is not None:
                return self._match_existing(racing, snapshot)
            raise

    def _match_existing(
        self, existing: "Receipt", snapshot: EvidenceSnapshot
    ) -> "Receipt":
        if _stored_snapshot_hash(existing) == snapshot.content_hash:
            return existing
        raise SnapshotConflictError(
            f"source {snapshot.source.source_kind}:"
            f"{snapshot.source.source_id} already has receipt "
            f"{existing.receipt_id} for different durable content; a changed "
            "terminal source must become a recheck observation, never a "
            "replacement receipt"
        )

    def _build_receipt(
        self, snapshot: EvidenceSnapshot, decision: object
    ) -> "Receipt":
        if not isinstance(decision, (ReceiptDecision, VerifiedReceiptDecision)):
            raise ReceiptIngestError(
                "decide() must return a ReceiptDecision or a sealed "
                f"VerifiedReceiptDecision, got {type(decision).__name__}"
            )
        marker = build_evidence_digest(
            evidence_kind=_SNAPSHOT_MARKER_KIND,
            source_ref=(
                f"{snapshot.source.source_kind}:{snapshot.source.source_id}"
            ),
            producer_id=_SNAPSHOT_MARKER_PRODUCER,
            observed_at=snapshot.captured_at,
            summary="normalized evidence snapshot content hash",
            payload_hash=snapshot.content_hash,
        )
        session_id = turn_id = mission_id = transaction_id = None
        if snapshot.subject_kind == "turn":
            head, _, tail = snapshot.subject_id.partition(":")
            session_id = head or None
            turn_id = tail or None
        elif snapshot.subject_kind == "mission":
            mission_id = snapshot.subject_id
        elif snapshot.subject_kind == "transaction":
            transaction_id = snapshot.subject_id
        uncertainty = tuple(
            dict.fromkeys(
                tuple(snapshot.uncertainty)
                + tuple(getattr(decision, "uncertainty", ()) or ())
            )
        )
        return build_receipt(
            source=snapshot.source,
            subject_kind=snapshot.subject_kind,
            subject_id=snapshot.subject_id,
            session_id=session_id,
            turn_id=turn_id,
            mission_id=mission_id,
            transaction_id=transaction_id,
            requested_outcome=snapshot.requested_outcome,
            status=decision.status,
            claims=snapshot.claims,
            evidence=snapshot.evidence + (marker,),
            artifacts=snapshot.artifacts,
            uncertainty=uncertainty,
            scorer_id=decision.scorer_id,
            scorer_version=decision.scorer_version,
            decided_at=decision.decided_at,
        )

    # ── Crash-safe consumer projection recovery ──

    def recover_projection(self, source: ReceiptSourceKey) -> "Receipt | None":
        """Re-link an already-inserted receipt into consumer projections.

        A crash between receipt insertion and a consumer's projection is
        repaired here: the receipt is found by its source key and its ID
        is CAS-linked into the mission/transaction projection when those
        tables and columns exist. Never inserts a duplicate receipt and
        never overwrites a different receipt ID already projected.
        """
        receipt = self._store.find_by_source(source)
        if receipt is None:
            return None
        if receipt.mission_id:
            self._cas_link_mission(receipt)
        if receipt.transaction_id:
            self._cas_link_transaction(receipt)
        return receipt

    def _resolve_workflows_path(self) -> Path | None:
        if self._workflows_db_path is not None:
            return self._workflows_db_path
        try:
            from hades_cli.workflows_db import workflows_db_path

            return workflows_db_path()
        except Exception:
            return None

    def _cas_link_mission(self, receipt: "Receipt") -> None:
        path = self._resolve_workflows_path()
        if path is None or not path.exists():
            return
        conn = sqlite3.connect(path)
        try:
            conn.row_factory = sqlite3.Row
            if not _table_exists(conn, "missions") or not _column_exists(
                conn, "missions", "receipt_id"
            ):
                return
            conn.execute(
                "UPDATE missions SET receipt_id = ? "
                "WHERE mission_id = ? AND (receipt_id IS NULL "
                "OR receipt_id = ?)",
                (receipt.receipt_id, receipt.mission_id, receipt.receipt_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _cas_link_transaction(self, receipt: "Receipt") -> None:
        db = self._store._db

        def _link(conn: sqlite3.Connection) -> None:
            for table in _TRANSACTION_TABLES:
                if not _table_exists(conn, table) or not _column_exists(
                    conn, table, "receipt_id"
                ):
                    continue
                conn.execute(
                    f"UPDATE {table} SET receipt_id = ? "
                    "WHERE transaction_id = ? AND (receipt_id IS NULL "
                    "OR receipt_id = ?)",
                    (
                        receipt.receipt_id,
                        receipt.transaction_id,
                        receipt.receipt_id,
                    ),
                )

        db._execute_write(_link)
