"""Frozen immutable value objects for the canonical receipt contract.

Owns every public value type in the "Canonical Public Interface" frozen by
the Verified Outcome & Artifact Receipts plan, the private support types
used by the store/scorer/ingest layers, and the deterministic builders
that derive ``clm_/evd_/art_/rct_/obs_`` IDs from canonical content
hashes.

Consumes only the Python standard library plus the sibling
``agent.receipt_hashing`` module. No store, scorer, mission, transaction,
or UI imports belong here.

Hash inputs exclude ``receipt_id``/``observation_id`` (each derives from
its own hash), database ``inserted_at``, ``content_hash`` itself, local
artifact locators, and provenance attestations. They include subject and
source keys, the requested outcome, status, all claim/evidence/artifact
content hashes, uncertainty, scorer identity/version, and the
``decided_at``/``observed_at`` freshness facts. An observation's hash
additionally binds its parent ``receipt_id`` and predecessor link so
identical facts on different receipts never collide.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, get_args

from agent.receipt_hashing import (
    canonical_content_hash,
    hash_hex,
    normalize_utc_timestamp,
)

__all__ = [
    "CLAIM_VERDICTS",
    "RECEIPT_SOURCE_KINDS",
    "RECEIPT_STATUSES",
    "RECEIPT_SUBJECT_KINDS",
    "ArtifactDigest",
    "ClaimVerdict",
    "EvidenceDigest",
    "EvidenceSnapshot",
    "OperationEvidence",
    "Receipt",
    "ReceiptClaim",
    "ReceiptDecision",
    "ReceiptEnvelope",
    "ReceiptObservation",
    "ReceiptQuery",
    "ReceiptSourceKey",
    "ReceiptStatus",
    "ReceiptSummary",
    "RequestedOutcome",
    "VerifiedReceiptDecision",
    "build_artifact_digest",
    "build_claim",
    "build_evidence_digest",
    "build_observation",
    "build_operation_evidence",
    "build_receipt",
    "build_requested_outcome",
]

# ---------------------------------------------------------------------------
# Frozen vocabularies.
# ---------------------------------------------------------------------------

ReceiptStatus = Literal[
    "verified", "completed_unverified", "failed", "blocked", "unknown_effect"
]
RECEIPT_STATUSES: frozenset[ReceiptStatus] = frozenset({
    "verified", "completed_unverified", "failed", "blocked", "unknown_effect",
})

ClaimVerdict = Literal["satisfied", "unsatisfied", "unknown", "not_applicable"]
CLAIM_VERDICTS: frozenset[ClaimVerdict] = frozenset(get_args(ClaimVerdict))

ReceiptSourceKind = Literal["turn", "mission", "transaction", "legacy", "external"]
RECEIPT_SOURCE_KINDS: frozenset[ReceiptSourceKind] = frozenset(
    get_args(ReceiptSourceKind)
)

ReceiptSubjectKind = Literal["turn", "mission", "transaction", "external"]
RECEIPT_SUBJECT_KINDS: frozenset[ReceiptSubjectKind] = frozenset(
    get_args(ReceiptSubjectKind)
)


# ---------------------------------------------------------------------------
# Public frozen value objects — field names and order are contract.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestedOutcome:
    outcome_kind: str
    description: str
    constraints: tuple[str, ...]
    producer_id: str
    content_hash: str


@dataclass(frozen=True)
class ReceiptClaim:
    claim_id: str
    claim_kind: str
    statement: str
    expected_json: str
    observed_json: str
    evidence_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    required: bool
    verdict: Literal["satisfied", "unsatisfied", "unknown", "not_applicable"]
    uncertainty: tuple[str, ...]
    content_hash: str


@dataclass(frozen=True)
class EvidenceDigest:
    evidence_id: str
    evidence_kind: str
    source_ref: str
    producer_id: str
    observed_at: str
    fresh_until: str | None
    summary: str
    payload_hash: str
    artifact_ids: tuple[str, ...]
    content_hash: str


@dataclass(frozen=True)
class ArtifactDigest:
    artifact_id: str
    source_kind: str
    source_ref: str
    display_name: str
    media_type: str | None
    size_bytes: int
    sha256: str
    mtime_ns: int | None
    captured_at: str
    content_hash: str


@dataclass(frozen=True)
class ReceiptSourceKey:
    source_kind: Literal["turn", "mission", "transaction", "legacy", "external"]
    source_id: str


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    source: ReceiptSourceKey
    subject_kind: Literal["turn", "mission", "transaction", "external"]
    subject_id: str
    session_id: str | None
    turn_id: str | None
    mission_id: str | None
    transaction_id: str | None
    requested_outcome: RequestedOutcome
    status: ReceiptStatus
    claims: tuple[ReceiptClaim, ...]
    evidence: tuple[EvidenceDigest, ...]
    artifacts: tuple[ArtifactDigest, ...]
    uncertainty: tuple[str, ...]
    scorer_id: str
    scorer_version: str
    decided_at: str
    content_hash: str


@dataclass(frozen=True)
class ReceiptObservation:
    observation_id: str
    receipt_id: str
    previous_observation_id: str | None
    status: ReceiptStatus
    claims: tuple[ReceiptClaim, ...]
    evidence: tuple[EvidenceDigest, ...]
    artifacts: tuple[ArtifactDigest, ...]
    uncertainty: tuple[str, ...]
    scorer_id: str
    scorer_version: str
    observed_at: str
    content_hash: str


@dataclass(frozen=True, init=False)
class VerifiedReceiptDecision:
    """Sealed proof that an independent scorer verified the end state.

    ``init=False``: there is no public constructor. Only
    ``ReceiptScoringService`` holds the module-private capability passed
    to :func:`_build_verified_decision`. A signature, source label, or
    consumer can never mint one.
    """

    scorer_id: str
    scorer_version: str
    subject_kind: str
    subject_id: str
    snapshot_hash: str
    claim_hashes: tuple[str, ...]
    decided_at: str
    fresh_until: str | None
    decision_hash: str

    @property
    def status(self) -> ReceiptStatus:
        return "verified"


# Module-private capability. ``agent.receipt_scoring`` imports these two
# names; nothing else may construct a VerifiedReceiptDecision.
_VERIFIED_DECISION_CAPABILITY: object = object()


def _build_verified_decision(
    capability: object,
    *,
    scorer_id: str,
    scorer_version: str,
    subject_kind: str,
    subject_id: str,
    snapshot_hash: str,
    claim_hashes: tuple[str, ...],
    decided_at: str,
    fresh_until: str | None,
) -> VerifiedReceiptDecision:
    """Construct the sealed verified decision. Capability-gated."""
    if capability is not _VERIFIED_DECISION_CAPABILITY:
        raise PermissionError(
            "VerifiedReceiptDecision may only be constructed by the "
            "receipt scoring service's sealed capability"
        )
    body = {
        "scorer_id": _text(scorer_id, "scorer_id"),
        "scorer_version": _text(scorer_version, "scorer_version"),
        "subject_kind": _text(subject_kind, "subject_kind"),
        "subject_id": _text(subject_id, "subject_id"),
        "snapshot_hash": _content_hash_text(snapshot_hash, "snapshot_hash"),
        "claim_hashes": tuple(
            _content_hash_text(item, "claim_hashes") for item in claim_hashes
        ),
        "decided_at": normalize_utc_timestamp(decided_at),
        "fresh_until": (
            None if fresh_until is None else normalize_utc_timestamp(fresh_until)
        ),
    }
    decision_hash = canonical_content_hash(body)
    decision = object.__new__(VerifiedReceiptDecision)
    for name, value in {**body, "decision_hash": decision_hash}.items():
        object.__setattr__(decision, name, value)
    return decision


# ---------------------------------------------------------------------------
# Private support types consumed by later store/scorer/ingest tasks.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptDecision:
    """Ordinary non-verified scoring decision. Never sealed."""

    status: ReceiptStatus
    scorer_id: str
    scorer_version: str
    subject_kind: str
    subject_id: str
    snapshot_hash: str
    claim_hashes: tuple[str, ...]
    uncertainty: tuple[str, ...]
    decided_at: str
    fresh_until: str | None
    decision_hash: str

    def __post_init__(self) -> None:
        if self.status not in RECEIPT_STATUSES:
            raise ValueError(f"unknown receipt status: {self.status!r}")
        if self.status == "verified":
            raise ValueError(
                "verified status requires the sealed VerifiedReceiptDecision, "
                "not an ordinary ReceiptDecision"
            )


@dataclass(frozen=True)
class OperationEvidence:
    """Immutable projection of one operation-journal row for scoring."""

    operation_id: str
    operation_kind: str
    state: str
    effect_disposition: str
    source_ref: str
    observed_at: str
    content_hash: str


@dataclass(frozen=True)
class ReceiptEnvelope:
    """Pre-scoring bundle of requested outcome, claims, and evidence."""

    source: ReceiptSourceKey
    subject_kind: str
    subject_id: str
    session_id: str | None
    turn_id: str | None
    mission_id: str | None
    transaction_id: str | None
    requested_outcome: RequestedOutcome
    claims: tuple[ReceiptClaim, ...]
    evidence: tuple[EvidenceDigest, ...]
    artifacts: tuple[ArtifactDigest, ...]
    uncertainty: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceSnapshot:
    """Normalized read-only evidence envelope handed to scorers."""

    source: ReceiptSourceKey
    subject_kind: str
    subject_id: str
    producer_id: str
    requested_outcome: RequestedOutcome
    claims: tuple[ReceiptClaim, ...]
    evidence: tuple[EvidenceDigest, ...]
    artifacts: tuple[ArtifactDigest, ...]
    operation_states: tuple[OperationEvidence, ...]
    blocked_reasons: tuple[str, ...]
    known_failures: tuple[str, ...]
    uncertainty: tuple[str, ...]
    captured_at: str
    content_hash: str

    def claim(self, claim_ref: str) -> ReceiptClaim:
        """Look up a claim by ``claim_kind`` (first match) or ``claim_id``."""
        for candidate in self.claims:
            if candidate.claim_kind == claim_ref or candidate.claim_id == claim_ref:
                return candidate
        raise KeyError(f"snapshot has no claim {claim_ref!r}")


@dataclass(frozen=True)
class ReceiptQuery:
    """Filter set for ``ReceiptStore.list()``."""

    status: ReceiptStatus | None = None
    source_kind: str | None = None
    subject_kind: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    mission_id: str | None = None
    transaction_id: str | None = None
    decided_after: str | None = None
    decided_before: str | None = None
    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("limit must be positive")
        if self.offset < 0:
            raise ValueError("offset must be non-negative")
        if self.status is not None and self.status not in RECEIPT_STATUSES:
            raise ValueError(f"unknown receipt status: {self.status!r}")


@dataclass(frozen=True)
class ReceiptSummary:
    """List-view projection of a stored receipt."""

    receipt_id: str
    source: ReceiptSourceKey
    subject_kind: str
    subject_id: str
    session_id: str | None
    status: ReceiptStatus
    scorer_id: str
    scorer_version: str
    decided_at: str
    content_hash: str


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _str_tuple(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a sequence of strings, got {value!r}")
    items = tuple(value)  # type: ignore[arg-type]
    for item in items:
        if not isinstance(item, str):
            raise ValueError(f"{name} entries must be strings, got {item!r}")
    return items


def _content_hash_text(value: object, name: str) -> str:
    text = _text(value, name)
    hash_hex(text)  # validates the sha256:<64 hex> shape
    return text


def _json_text(value: object, name: str) -> str:
    text = value if isinstance(value, str) else None
    if text is None:
        raise ValueError(f"{name} must be a JSON string, got {value!r}")
    try:
        json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be valid JSON text: {text!r}") from exc
    return text


def _unique(ids: list[str], what: str) -> None:
    seen: set[str] = set()
    for item in ids:
        if item in seen:
            raise ValueError(f"duplicate {what}: {item!r}")
        seen.add(item)


# ---------------------------------------------------------------------------
# Deterministic builders — IDs derive from canonical content hashes.
# ---------------------------------------------------------------------------


def build_requested_outcome(
    *,
    outcome_kind: str,
    description: str,
    constraints: tuple[str, ...] = (),
    producer_id: str,
) -> RequestedOutcome:
    body = {
        "outcome_kind": _text(outcome_kind, "outcome_kind"),
        "description": _text(description, "description"),
        "constraints": _str_tuple(constraints, "constraints"),
        "producer_id": _text(producer_id, "producer_id"),
    }
    digest = canonical_content_hash(body)
    return RequestedOutcome(content_hash=digest, **body)


def _claim_body(
    *,
    claim_kind: str = "effect",
    statement: str,
    expected_json: str = "null",
    observed_json: str = "null",
    evidence_ids: tuple[str, ...] = (),
    artifact_ids: tuple[str, ...] = (),
    required: bool = True,
    verdict: str = "unknown",
    uncertainty: tuple[str, ...] = (),
) -> dict[str, object]:
    if verdict not in CLAIM_VERDICTS:
        raise ValueError(
            f"unknown claim verdict: {verdict!r}; expected one of "
            f"{sorted(CLAIM_VERDICTS)}"
        )
    return {
        "claim_kind": _text(claim_kind, "claim_kind"),
        "statement": _text(statement, "statement"),
        "expected_json": _json_text(expected_json, "expected_json"),
        "observed_json": _json_text(observed_json, "observed_json"),
        "evidence_ids": _str_tuple(evidence_ids, "evidence_ids"),
        "artifact_ids": _str_tuple(artifact_ids, "artifact_ids"),
        "required": bool(required),
        "verdict": verdict,
        "uncertainty": _str_tuple(uncertainty, "uncertainty"),
    }


def build_claim(**fields: object) -> ReceiptClaim:
    body = _claim_body(**fields)  # type: ignore[arg-type]
    digest = canonical_content_hash(body)
    return ReceiptClaim(
        claim_id=f"clm_{digest.removeprefix('sha256:')}",
        content_hash=digest,
        **body,  # type: ignore[arg-type]
    )


def build_evidence_digest(
    *,
    evidence_kind: str,
    source_ref: str,
    producer_id: str,
    observed_at: str,
    fresh_until: str | None = None,
    summary: str,
    payload_hash: str,
    artifact_ids: tuple[str, ...] = (),
) -> EvidenceDigest:
    body = {
        "evidence_kind": _text(evidence_kind, "evidence_kind"),
        "source_ref": _text(source_ref, "source_ref"),
        "producer_id": _text(producer_id, "producer_id"),
        "observed_at": normalize_utc_timestamp(observed_at),
        "fresh_until": (
            None if fresh_until is None else normalize_utc_timestamp(fresh_until)
        ),
        "summary": _text(summary, "summary"),
        "payload_hash": _content_hash_text(payload_hash, "payload_hash"),
        "artifact_ids": _str_tuple(artifact_ids, "artifact_ids"),
    }
    digest = canonical_content_hash(body)
    return EvidenceDigest(
        evidence_id=f"evd_{digest.removeprefix('sha256:')}",
        content_hash=digest,
        **body,  # type: ignore[arg-type]
    )


def build_artifact_digest(
    *,
    source_kind: str,
    source_ref: str,
    display_name: str,
    media_type: str | None = None,
    size_bytes: int,
    sha256: str,
    mtime_ns: int | None = None,
    captured_at: str,
) -> ArtifactDigest:
    sha = _text(sha256, "sha256")
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError(f"sha256 must be 64 lowercase hex characters, got {sha!r}")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
        raise ValueError(f"size_bytes must be a non-negative int, got {size_bytes!r}")
    if mtime_ns is not None and (
        not isinstance(mtime_ns, int) or isinstance(mtime_ns, bool)
    ):
        raise ValueError(f"mtime_ns must be an int or None, got {mtime_ns!r}")
    body = {
        "source_kind": _text(source_kind, "source_kind"),
        "source_ref": _text(source_ref, "source_ref"),
        "display_name": _text(display_name, "display_name"),
        "media_type": _optional_text(media_type, "media_type"),
        "size_bytes": size_bytes,
        "sha256": sha,
        "mtime_ns": mtime_ns,
        "captured_at": normalize_utc_timestamp(captured_at),
    }
    digest = canonical_content_hash(body)
    return ArtifactDigest(
        artifact_id=f"art_{digest.removeprefix('sha256:')}",
        content_hash=digest,
        **body,  # type: ignore[arg-type]
    )


def build_operation_evidence(
    *,
    operation_id: str,
    operation_kind: str,
    state: str,
    effect_disposition: str,
    source_ref: str,
    observed_at: str,
) -> OperationEvidence:
    body = {
        "operation_id": _text(operation_id, "operation_id"),
        "operation_kind": _text(operation_kind, "operation_kind"),
        "state": _text(state, "state"),
        "effect_disposition": _text(effect_disposition, "effect_disposition"),
        "source_ref": _text(source_ref, "source_ref"),
        "observed_at": normalize_utc_timestamp(observed_at),
    }
    digest = canonical_content_hash(body)
    return OperationEvidence(content_hash=digest, **body)  # type: ignore[arg-type]


def _validate_traceability(
    claims: tuple[ReceiptClaim, ...],
    evidence: tuple[EvidenceDigest, ...],
    artifacts: tuple[ArtifactDigest, ...],
) -> None:
    _unique([c.claim_id for c in claims], "claim_id")
    _unique([e.evidence_id for e in evidence], "evidence_id")
    _unique([a.artifact_id for a in artifacts], "artifact_id")
    _unique([c.content_hash for c in claims], "claim content_hash")
    _unique([e.content_hash for e in evidence], "evidence content_hash")
    _unique([a.content_hash for a in artifacts], "artifact content_hash")
    evidence_ids = {e.evidence_id for e in evidence}
    artifact_ids = {a.artifact_id for a in artifacts}
    for claim in claims:
        for evidence_id in claim.evidence_ids:
            if evidence_id not in evidence_ids:
                raise ValueError(
                    f"dangling evidence reference {evidence_id!r} in claim "
                    f"{claim.claim_id!r}"
                )
        for artifact_id in claim.artifact_ids:
            if artifact_id not in artifact_ids:
                raise ValueError(
                    f"dangling artifact reference {artifact_id!r} in claim "
                    f"{claim.claim_id!r}"
                )
    for item in evidence:
        for artifact_id in item.artifact_ids:
            if artifact_id not in artifact_ids:
                raise ValueError(
                    f"dangling artifact reference {artifact_id!r} in evidence "
                    f"{item.evidence_id!r}"
                )


def build_receipt(
    *,
    source: ReceiptSourceKey,
    subject_kind: str,
    subject_id: str,
    session_id: str | None = None,
    turn_id: str | None = None,
    mission_id: str | None = None,
    transaction_id: str | None = None,
    requested_outcome: RequestedOutcome,
    status: str,
    claims: tuple[ReceiptClaim, ...] = (),
    evidence: tuple[EvidenceDigest, ...] = (),
    artifacts: tuple[ArtifactDigest, ...] = (),
    uncertainty: tuple[str, ...] = (),
    scorer_id: str,
    scorer_version: str,
    decided_at: str,
) -> Receipt:
    if not isinstance(source, ReceiptSourceKey):
        raise ValueError(f"source must be a ReceiptSourceKey, got {source!r}")
    if source.source_kind not in RECEIPT_SOURCE_KINDS:
        raise ValueError(f"unknown source_kind: {source.source_kind!r}")
    _text(source.source_id, "source.source_id")
    if subject_kind not in RECEIPT_SUBJECT_KINDS:
        raise ValueError(f"unknown subject_kind: {subject_kind!r}")
    if status not in RECEIPT_STATUSES:
        raise ValueError(f"unknown receipt status: {status!r}")
    if not isinstance(requested_outcome, RequestedOutcome):
        raise ValueError(
            f"requested_outcome must be a RequestedOutcome, got {requested_outcome!r}"
        )
    claims = tuple(claims)
    evidence = tuple(evidence)
    artifacts = tuple(artifacts)
    _validate_traceability(claims, evidence, artifacts)
    decided = normalize_utc_timestamp(decided_at)
    hash_body = {
        "source": {"source_kind": source.source_kind, "source_id": source.source_id},
        "subject_kind": subject_kind,
        "subject_id": _text(subject_id, "subject_id"),
        "session_id": _optional_text(session_id, "session_id"),
        "turn_id": _optional_text(turn_id, "turn_id"),
        "mission_id": _optional_text(mission_id, "mission_id"),
        "transaction_id": _optional_text(transaction_id, "transaction_id"),
        "requested_outcome": requested_outcome.content_hash,
        "status": status,
        "claims": [c.content_hash for c in claims],
        "evidence": [e.content_hash for e in evidence],
        "artifacts": [a.content_hash for a in artifacts],
        "uncertainty": _str_tuple(uncertainty, "uncertainty"),
        "scorer_id": _text(scorer_id, "scorer_id"),
        "scorer_version": _text(scorer_version, "scorer_version"),
        "decided_at": decided,
    }
    digest = canonical_content_hash(hash_body)
    return Receipt(
        receipt_id=f"rct_{digest.removeprefix('sha256:')}",
        source=source,
        subject_kind=subject_kind,  # type: ignore[arg-type]
        subject_id=subject_id,
        session_id=session_id,
        turn_id=turn_id,
        mission_id=mission_id,
        transaction_id=transaction_id,
        requested_outcome=requested_outcome,
        status=status,  # type: ignore[arg-type]
        claims=claims,
        evidence=evidence,
        artifacts=artifacts,
        uncertainty=tuple(uncertainty),
        scorer_id=scorer_id,
        scorer_version=scorer_version,
        decided_at=decided,
        content_hash=digest,
    )


def build_observation(
    *,
    receipt_id: str,
    previous_observation_id: str | None = None,
    status: str,
    claims: tuple[ReceiptClaim, ...] = (),
    evidence: tuple[EvidenceDigest, ...] = (),
    artifacts: tuple[ArtifactDigest, ...] = (),
    uncertainty: tuple[str, ...] = (),
    scorer_id: str,
    scorer_version: str,
    observed_at: str,
) -> ReceiptObservation:
    if status not in RECEIPT_STATUSES:
        raise ValueError(f"unknown receipt status: {status!r}")
    claims = tuple(claims)
    evidence = tuple(evidence)
    artifacts = tuple(artifacts)
    _validate_traceability(claims, evidence, artifacts)
    observed = normalize_utc_timestamp(observed_at)
    hash_body = {
        # The parent receipt and predecessor link are lineage facts: the
        # observation's own ID is excluded (it derives from this hash),
        # but identical facts observed on different receipts or after
        # different predecessors are different observations.
        "receipt_id": _text(receipt_id, "receipt_id"),
        "previous_observation_id": _optional_text(
            previous_observation_id, "previous_observation_id"
        ),
        "status": status,
        "claims": [c.content_hash for c in claims],
        "evidence": [e.content_hash for e in evidence],
        "artifacts": [a.content_hash for a in artifacts],
        "uncertainty": _str_tuple(uncertainty, "uncertainty"),
        "scorer_id": _text(scorer_id, "scorer_id"),
        "scorer_version": _text(scorer_version, "scorer_version"),
        "observed_at": observed,
    }
    digest = canonical_content_hash(hash_body)
    return ReceiptObservation(
        observation_id=f"obs_{digest.removeprefix('sha256:')}",
        receipt_id=receipt_id,
        previous_observation_id=previous_observation_id,
        status=status,  # type: ignore[arg-type]
        claims=claims,
        evidence=evidence,
        artifacts=artifacts,
        uncertainty=tuple(uncertainty),
        scorer_id=scorer_id,
        scorer_version=scorer_version,
        observed_at=observed,
        content_hash=digest,
    )
