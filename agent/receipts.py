"""Deterministic evidence scoring and immutable receipt canonicalization.

This module deliberately separates the end-state decision from persistence.  The
only path that can return ``verified`` is ``WorkflowEndStateScorer.score`` over a
complete, independently observed ``MissionEvidenceSnapshot``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


SUPPORTED_EVIDENCE_CHECKS = frozenset({
    "workflow_succeeded",
    "all_effects_settled",
    "fresh_verification",
    "artifacts_exist",
    "outbox_confirmed",
})
_SETTLED_EFFECT_PHASES = frozenset({"committed", "compensated", "cancelled"})
_UNKNOWN_STATES = frozenset({"unknown", "unknown_effect"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class EvidenceManifest:
    """The V1 evidence declared before a mission starts."""

    checks: tuple[str, ...]
    artifact_paths: tuple[str, ...] = ()
    outbox_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        checks = tuple(str(check) for check in self.checks)
        unknown = sorted(set(checks) - SUPPORTED_EVIDENCE_CHECKS)
        if unknown:
            raise ValueError(f"unsupported evidence check(s): {unknown!r}")
        if len(set(checks)) != len(checks):
            raise ValueError("evidence checks must not contain duplicates")
        paths = tuple(str(path) for path in self.artifact_paths)
        if any(not path or path.startswith("/") for path in paths):
            raise ValueError("artifact paths must be non-empty relative paths")
        outbox_ids = tuple(str(outbox_id) for outbox_id in self.outbox_ids)
        if any(not outbox_id for outbox_id in outbox_ids):
            raise ValueError("outbox ids must be non-empty")
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "artifact_paths", paths)
        object.__setattr__(self, "outbox_ids", outbox_ids)


def validate_evidence_manifest(value: Mapping[str, Any]) -> EvidenceManifest:
    """Parse and reject unsupported evidence before any mission starts."""
    if not isinstance(value, Mapping):
        raise ValueError("evidence manifest must be a mapping")
    checks = value.get("checks", ())
    if not isinstance(checks, (list, tuple)):
        raise ValueError("evidence checks must be a list")
    artifact_paths = value.get("artifact_paths", ())
    if not isinstance(artifact_paths, (list, tuple)):
        raise ValueError("artifact_paths must be a list")
    outbox_ids = value.get("outbox_ids", ())
    if not isinstance(outbox_ids, (list, tuple)):
        raise ValueError("outbox_ids must be a list")
    return EvidenceManifest(
        checks=tuple(checks),
        artifact_paths=tuple(artifact_paths),
        outbox_ids=tuple(outbox_ids),
    )


@dataclass(frozen=True)
class MissionEvidenceSnapshot:
    """All state the V1 scorer may observe; no model assertion is sufficient."""

    mission_id: str
    objective: str
    constraints: tuple[Any, ...]
    execution_ids: tuple[str, ...]
    transaction_ids: tuple[str, ...]
    before_after: Mapping[str, Any]
    claims: Mapping[str, Any]
    manifest: EvidenceManifest
    execution_statuses: tuple[str, ...]
    authority_blocked: bool
    review_blocked: bool
    operation_phases: tuple[str, ...]
    transaction_phases: tuple[str, ...]
    outbox_statuses: Mapping[str, str]
    verification: Mapping[str, Any]
    artifacts: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if not self.mission_id:
            raise ValueError("mission_id is required")
        if not self.objective:
            raise ValueError("objective is required")
        object.__setattr__(self, "constraints", tuple(copy.deepcopy(self.constraints)))
        object.__setattr__(self, "execution_ids", tuple(self.execution_ids))
        object.__setattr__(self, "transaction_ids", tuple(self.transaction_ids))
        object.__setattr__(self, "before_after", copy.deepcopy(dict(self.before_after)))
        object.__setattr__(self, "claims", copy.deepcopy(dict(self.claims)))
        object.__setattr__(self, "execution_statuses", tuple(self.execution_statuses))
        object.__setattr__(self, "operation_phases", tuple(self.operation_phases))
        object.__setattr__(self, "transaction_phases", tuple(self.transaction_phases))
        object.__setattr__(self, "outbox_statuses", copy.deepcopy(dict(self.outbox_statuses)))
        object.__setattr__(self, "verification", copy.deepcopy(dict(self.verification)))
        object.__setattr__(
            self, "artifacts", tuple(copy.deepcopy(dict(artifact)) for artifact in self.artifacts)
        )


@dataclass(frozen=True)
class ReceiptDecision:
    status: str
    checks: Mapping[str, bool]
    evidence: Mapping[str, Any]
    uncertainty: tuple[str, ...]
    freshness: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", copy.deepcopy(dict(self.checks)))
        object.__setattr__(self, "evidence", copy.deepcopy(dict(self.evidence)))
        object.__setattr__(self, "uncertainty", tuple(self.uncertainty))
        object.__setattr__(self, "freshness", copy.deepcopy(dict(self.freshness)))


class EndStateScorer(Protocol):
    scorer_id: str
    scorer_version: str

    def score(self, snapshot: MissionEvidenceSnapshot) -> ReceiptDecision: ...


class WorkflowEndStateScorer:
    """The sole V1 scorer allowed to produce a ``verified`` receipt."""

    scorer_id = "hermes.workflow-end-state"
    scorer_version = "1"

    @staticmethod
    def _workflow_succeeded(snapshot: MissionEvidenceSnapshot) -> bool:
        return bool(snapshot.execution_statuses) and all(
            status == "succeeded" for status in snapshot.execution_statuses
        )

    @staticmethod
    def _effects_settled(snapshot: MissionEvidenceSnapshot) -> bool:
        phases = (*snapshot.operation_phases, *snapshot.transaction_phases)
        return all(phase in _SETTLED_EFFECT_PHASES for phase in phases)

    @staticmethod
    def _fresh_verification(snapshot: MissionEvidenceSnapshot) -> bool:
        verification = snapshot.verification
        return (
            verification.get("status") == "passed"
            and bool(verification.get("timestamp"))
            and bool(verification.get("source"))
        )

    @staticmethod
    def _artifacts_exist(snapshot: MissionEvidenceSnapshot) -> bool:
        required = set(snapshot.manifest.artifact_paths)
        if not required:
            return True
        observed: dict[str, Mapping[str, Any]] = {}
        for artifact in snapshot.artifacts:
            required_path = artifact.get("required_path")
            if isinstance(required_path, str):
                observed[required_path] = artifact
        if set(observed) != required:
            return False
        return all(
            artifact.get("exists") is True
            and artifact.get("within_allowed_root") is True
            and isinstance(artifact.get("size"), int)
            and artifact["size"] > 0
            and isinstance(artifact.get("sha256"), str)
            and _SHA256_RE.fullmatch(artifact["sha256"]) is not None
            and artifact.get("mtime") is not None
            for artifact in observed.values()
        )

    @staticmethod
    def _outbox_confirmed(snapshot: MissionEvidenceSnapshot) -> bool:
        return all(
            snapshot.outbox_statuses.get(outbox_id) in {"confirmed", "delivered"}
            for outbox_id in snapshot.manifest.outbox_ids
        )

    @staticmethod
    def _has_unknown_effect(snapshot: MissionEvidenceSnapshot) -> bool:
        return any(
            phase in _UNKNOWN_STATES
            for phase in (
                *snapshot.operation_phases,
                *snapshot.transaction_phases,
                *snapshot.outbox_statuses.values(),
            )
        )

    def score(self, snapshot: MissionEvidenceSnapshot) -> ReceiptDecision:
        checks = {
            "workflow_succeeded": self._workflow_succeeded(snapshot),
            "all_effects_settled": self._effects_settled(snapshot),
            "fresh_verification": self._fresh_verification(snapshot),
            "artifacts_exist": self._artifacts_exist(snapshot),
            "outbox_confirmed": self._outbox_confirmed(snapshot),
        }
        freshness = {
            "verification_timestamp": snapshot.verification.get("timestamp"),
            "verification_source": snapshot.verification.get("source"),
            "verification_status": snapshot.verification.get("status"),
        }
        evidence = {
            "execution_statuses": snapshot.execution_statuses,
            "operation_phases": snapshot.operation_phases,
            "transaction_phases": snapshot.transaction_phases,
            "outbox_statuses": snapshot.outbox_statuses,
            "verification": snapshot.verification,
            "artifacts": snapshot.artifacts,
        }
        if any(status == "failed" for status in snapshot.execution_statuses):
            return ReceiptDecision("failed", checks, evidence, (), freshness)
        if snapshot.authority_blocked or snapshot.review_blocked:
            return ReceiptDecision("blocked", checks, evidence, (), freshness)
        if self._has_unknown_effect(snapshot):
            return ReceiptDecision("unknown_effect", checks, evidence, ("unknown_effect",), freshness)
        missing = tuple(check for check in snapshot.manifest.checks if not checks[check])
        if missing:
            return ReceiptDecision("completed_unverified", checks, evidence, missing, freshness)
        return ReceiptDecision("verified", checks, evidence, (), freshness)


def canonical_receipt_json(receipt: Mapping[str, Any]) -> str:
    """Canonical receipt content, excluding an optional non-content signature."""
    if not isinstance(receipt, Mapping):
        raise ValueError("receipt must be a mapping")
    content = copy.deepcopy(dict(receipt))
    content.pop("signature", None)
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def receipt_content_hash(receipt: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_receipt_json(receipt).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _receipt_content(
    snapshot: MissionEvidenceSnapshot,
    decision: ReceiptDecision,
    scorer: EndStateScorer,
    signature: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mission_id": snapshot.mission_id,
        "status": decision.status,
        "objective": snapshot.objective,
        "constraints": snapshot.constraints,
        "execution_ids": snapshot.execution_ids,
        "transaction_ids": snapshot.transaction_ids,
        "before_after": snapshot.before_after,
        "claims": snapshot.claims,
        "verifier": {
            "scorer_id": scorer.scorer_id,
            "scorer_version": scorer.scorer_version,
        },
        "evidence": {"checks": decision.checks, **decision.evidence},
        "artifacts": snapshot.artifacts,
        "uncertainty": decision.uncertainty,
        "freshness": decision.freshness,
        "signature": copy.deepcopy(dict(signature)) if signature is not None else None,
    }


def issue_receipt(
    snapshot: MissionEvidenceSnapshot,
    *,
    session_db: Any,
    project_receipt: Any | None = None,
    scorer: EndStateScorer | None = None,
    signature: Mapping[str, Any] | None = None,
) -> Any:
    """Score, persist once, then optionally project the immutable verdict.

    ``project_receipt`` is injected because MissionDB lives in a separate
    profile-local database.  It runs strictly after durable SessionDB insert or
    deterministic replay lookup, making a crash between the two writes safe to
    retry without a second receipt.
    """
    active_scorer = scorer or WorkflowEndStateScorer()
    decision = active_scorer.score(snapshot)
    content = _receipt_content(snapshot, decision, active_scorer, signature)
    content_hash = receipt_content_hash(content)
    receipt_id = f"receipt_{content_hash}"
    existing = session_db.get_receipt(receipt_id)
    if existing is None:
        receipt = session_db.insert_receipt(
            receipt_id=receipt_id,
            mission_id=snapshot.mission_id,
            status=decision.status,
            objective=snapshot.objective,
            constraints_json=_json(snapshot.constraints),
            execution_ids_json=_json(snapshot.execution_ids),
            transaction_ids_json=_json(snapshot.transaction_ids),
            before_after_json=_json(snapshot.before_after),
            claims_json=_json(snapshot.claims),
            verifier_json=_json(content["verifier"]),
            evidence_json=_json(content["evidence"]),
            artifacts_json=_json(snapshot.artifacts),
            uncertainty_json=_json(decision.uncertainty),
            freshness_json=_json(decision.freshness),
            content_hash=content_hash,
            signature_json=_json(signature) if signature is not None else None,
        )
    else:
        if existing.content_hash != content_hash:
            raise RuntimeError("deterministic receipt id resolved to different content")
        receipt = existing
    if project_receipt is not None:
        project_receipt(receipt_id=receipt.receipt_id, verdict=decision.status)
    return receipt


def recheck_receipt(
    receipt_id: str,
    snapshot: MissionEvidenceSnapshot,
    *,
    session_db: Any,
    scorer: EndStateScorer | None = None,
) -> Any:
    """Append current observations without editing receipt or mission verdict."""
    receipt = session_db.get_receipt(receipt_id)
    if receipt is None:
        raise KeyError(f"receipt not found: {receipt_id}")
    if receipt.mission_id != snapshot.mission_id:
        raise ValueError("recheck snapshot mission_id does not match receipt")
    active_scorer = scorer or WorkflowEndStateScorer()
    decision = active_scorer.score(snapshot)
    observation = {
        "receipt_id": receipt_id,
        "status": decision.status,
        "checks": decision.checks,
        "evidence": decision.evidence,
        "uncertainty": decision.uncertainty,
        "freshness": decision.freshness,
        # SessionDB makes observations append-only and unique by content hash;
        # the collection instant distinguishes two identical rechecks.
        "observed_at_ns": time.time_ns(),
    }
    return session_db.append_receipt_observation(
        receipt_id=receipt_id,
        status=decision.status,
        evidence=observation,
        content_hash=receipt_content_hash(observation),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def collect_artifact_evidence(
    manifest: EvidenceManifest,
    *,
    allowed_roots: tuple[str | Path, ...],
) -> tuple[dict[str, Any], ...]:
    """Resolve and hash declared artifacts without crossing an allowed root."""
    roots = tuple(Path(root).expanduser().resolve(strict=True) for root in allowed_roots)
    if not roots:
        raise ValueError("artifact observation requires allowed roots")
    evidence: list[dict[str, Any]] = []
    for required_path in manifest.artifact_paths:
        candidates = tuple(root / required_path for root in roots)
        candidate = next((path for path in candidates if path.exists() or path.is_symlink()), None)
        if candidate is None:
            evidence.append({
                "required_path": required_path,
                "path": None,
                "exists": False,
                "within_allowed_root": False,
                "size": None,
                "sha256": None,
                "mtime": None,
            })
            continue
        resolved = candidate.resolve(strict=True)
        if not any(_is_within(resolved, root) for root in roots):
            raise ValueError(f"artifact {required_path!r} escapes allowed roots")
        if not resolved.is_file():
            raise ValueError(f"artifact {required_path!r} is not a regular file")
        stat = resolved.stat()
        evidence.append({
            "required_path": required_path,
            "path": str(resolved),
            "exists": True,
            "within_allowed_root": True,
            "size": stat.st_size,
            "sha256": _sha256_file(resolved),
            "mtime": stat.st_mtime_ns,
        })
    return tuple(evidence)
