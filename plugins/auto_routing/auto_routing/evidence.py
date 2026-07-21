"""Closed, content-free evidence identities and normalization helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .models import (
    BoundedTokenCount,
    ComplexityBands,
    DurableIdentifier,
    EvidenceContextBucket,
    EvidenceEvent,
    EvidenceFeedbackValue,
    EvidenceOutcome,
    EvidenceSignalType,
    NonNegativeFloat,
    ReasoningEffort,
    StrictNonNegativeInt,
    TaskAssessment,
)


@dataclass(frozen=True, slots=True)
class NormalizedEvidence:
    """Canonical signal interpretation for one finite evidence value."""

    signal_type: EvidenceSignalType
    normalized_value: float | None
    confidence_weight: float


_OUTCOMES: dict[EvidenceOutcome, NormalizedEvidence] = {
    "verified": NormalizedEvidence("objective_outcome", 1.0, 1.0),
    "failed": NormalizedEvidence("operational", None, 0.0),
    "partial": NormalizedEvidence("operational", None, 0.0),
    "completed_unverified": NormalizedEvidence("operational", None, 0.0),
    "blocked": NormalizedEvidence("operational", None, 0.0),
    "interrupted": NormalizedEvidence("operational", None, 0.0),
    "unresolved": NormalizedEvidence("operational", None, 0.0),
    "cancelled": NormalizedEvidence("operational", None, 0.0),
}

_FEEDBACK: dict[EvidenceFeedbackValue, tuple[float | None, float]] = {
    "rating-1": (0.0, 1.0),
    "rating-2": (0.25, 1.0),
    "rating-3": (0.5, 1.0),
    "rating-4": (0.75, 1.0),
    "rating-5": (1.0, 1.0),
    # Only ratings have an inherent ordinal mapping. These explicit actions
    # remain categorical so later scoring policy cannot leak into evidence.
    "rejected": (None, 1.0),
    "corrected": (None, 1.0),
    "manual-reroute": (None, 1.0),
}


class ObserverRuntimeBinding(BaseModel):
    """Sealed public route binding accepted from the generic core observer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Literal["fresh_session", "delegation"]
    session_id: DurableIdentifier
    task_id: DurableIdentifier
    action: Literal["inherit", "shadow", "project"]
    model: DurableIdentifier
    provider: DurableIdentifier
    decision_id: DurableIdentifier | None


class TurnOutcomeObserverPayload(BaseModel):
    """Strict generic observer payload consumed by the routing plugin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    telemetry_schema_version: Literal["hermes.observer.v1"]
    session_id: DurableIdentifier
    turn_id: DurableIdentifier
    task_id: DurableIdentifier
    observed_at_unix: NonNegativeFloat
    outcome: EvidenceOutcome
    api_calls: StrictNonNegativeInt
    tool_iterations: StrictNonNegativeInt
    retry_count: StrictNonNegativeInt
    cost_usd: NonNegativeFloat
    input_tokens: BoundedTokenCount
    output_tokens: BoundedTokenCount
    cache_read_tokens: BoundedTokenCount
    reasoning_effort: ReasoningEffort | None
    runtime_binding: ObserverRuntimeBinding | None


def _sha(value: object) -> str:
    document = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def turn_evidence_id(session_id: str, turn_id: str) -> str:
    """Return the deterministic identity of one routed-turn observation."""
    return _sha(["hermes_turn_outcome", session_id, turn_id])


def feedback_evidence_id(
    parent_evidence_id: str,
    value: EvidenceFeedbackValue,
) -> str:
    """Return the deterministic identity of one finite feedback action."""
    return _sha(["user_feedback", parent_evidence_id, value])


def normalize_turn_outcome(outcome: str) -> NormalizedEvidence:
    """Map the closed outcome vocabulary without inferring unverified success."""
    try:
        return _OUTCOMES[outcome]  # type: ignore[index]
    except KeyError as error:
        raise ValueError(f"unsupported turn outcome: {outcome}") from error


def build_context_bucket(
    assessment: TaskAssessment,
    bands: ComplexityBands,
) -> EvidenceContextBucket:
    """Build a stable, content-free bucket using the recorded band authority."""
    body = {
        "complexity_band": bands.label(assessment.complexity),
        "domains": sorted(assessment.domains),
        "required_capabilities": sorted(assessment.required_capabilities),
        "required_modalities": sorted(assessment.required_modalities),
        "risk_class": assessment.risk_class,
    }
    return EvidenceContextBucket(bucket_id=_sha(body), **body)


def build_feedback_event(
    parent: EvidenceEvent,
    value: EvidenceFeedbackValue,
    *,
    observed_at: str,
) -> EvidenceEvent:
    """Create feedback with attribution copied exactly from its routed parent."""
    if parent.source != "hermes_turn_outcome":
        raise ValueError("feedback parent must be routed turn evidence")
    normalized_value, confidence = _FEEDBACK[value]
    return EvidenceEvent(
        evidence_id=feedback_evidence_id(parent.evidence_id, value),
        source="user_feedback",
        signal_type="explicit_feedback",
        parent_evidence_id=parent.evidence_id,
        decision_id=parent.decision_id,
        session_id=parent.session_id,
        turn_id=parent.turn_id,
        task_id=parent.task_id,
        route_epoch_id=parent.route_epoch_id,
        runtime_id=parent.runtime_id,
        profile_id=parent.profile_id,
        reasoning_effort=parent.reasoning_effort,
        context_bucket=parent.context_bucket,
        is_initial_routing_task=parent.is_initial_routing_task,
        feedback_value=value,
        normalized_value=normalized_value,
        confidence_weight=confidence,
        attribution_confidence=parent.attribution_confidence,
        api_calls=0,
        tool_iterations=0,
        retry_count=0,
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        latency_seconds=None,
        observed_at=observed_at,
    )


def validate_evidence_semantics(event: EvidenceEvent) -> None:
    """Recompute immutable identities and normalization at trust boundaries."""
    if event.context_bucket is not None:
        bucket = event.context_bucket
        expected_bucket_id = _sha(
            {
                "complexity_band": bucket.complexity_band,
                "domains": sorted(bucket.domains),
                "required_capabilities": sorted(bucket.required_capabilities),
                "required_modalities": sorted(bucket.required_modalities),
                "risk_class": bucket.risk_class,
            }
        )
        if bucket.bucket_id != expected_bucket_id:
            raise ValueError("evidence context bucket identity changed")
    if event.source == "hermes_turn_outcome":
        expected_id = turn_evidence_id(event.session_id, event.turn_id)
        normalized = normalize_turn_outcome(event.outcome or "")
        expected = (
            normalized.signal_type,
            normalized.normalized_value,
            normalized.confidence_weight,
        )
    else:
        assert event.parent_evidence_id is not None
        assert event.feedback_value is not None
        expected_id = feedback_evidence_id(
            event.parent_evidence_id,
            event.feedback_value,
        )
        value, confidence = _FEEDBACK[event.feedback_value]
        expected = ("explicit_feedback", value, confidence)
    actual = (
        event.signal_type,
        event.normalized_value,
        event.confidence_weight,
    )
    if event.evidence_id != expected_id or actual != expected:
        raise ValueError("evidence identity or normalization changed")


__all__ = [
    "NormalizedEvidence",
    "ObserverRuntimeBinding",
    "TurnOutcomeObserverPayload",
    "build_context_bucket",
    "build_feedback_event",
    "feedback_evidence_id",
    "normalize_turn_outcome",
    "turn_evidence_id",
    "validate_evidence_semantics",
]
