"""Closed-world evidence contract and deterministic normalization tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plugins.auto_routing.auto_routing.evidence import (
    build_context_bucket,
    build_feedback_event,
    feedback_evidence_id,
    normalize_turn_outcome,
    turn_evidence_id,
    validate_evidence_semantics,
)
from plugins.auto_routing.auto_routing.models import (
    ComplexityBands,
    EvidenceEvent,
    TaskAssessment,
)


@pytest.mark.parametrize(
    ("outcome", "signal_type", "value", "confidence"),
    [
        ("verified", "objective_outcome", 1.0, 1.0),
        ("failed", "operational", None, 0.0),
        ("partial", "operational", None, 0.0),
        ("completed_unverified", "operational", None, 0.0),
        ("blocked", "operational", None, 0.0),
        ("interrupted", "operational", None, 0.0),
        ("unresolved", "operational", None, 0.0),
        ("cancelled", "operational", None, 0.0),
    ],
)
def test_turn_outcome_normalization_is_closed_and_conservative(
    outcome, signal_type, value, confidence
):
    normalized = normalize_turn_outcome(outcome)
    assert normalized.signal_type == signal_type
    assert normalized.normalized_value == value
    assert normalized.confidence_weight == confidence


def test_completed_without_verification_is_not_success():
    normalized = normalize_turn_outcome("completed_unverified")
    assert normalized.normalized_value is None
    assert normalized.confidence_weight == 0.0


def test_unknown_outcome_is_rejected_instead_of_coerced():
    with pytest.raises(ValueError, match="unsupported turn outcome"):
        normalize_turn_outcome("looked-good")


def test_context_bucket_uses_recorded_bands_and_sorted_content_free_labels():
    assessment = TaskAssessment(
        complexity=0.71,
        domains=("debugging", "coding"),
        required_capabilities=("tools",),
        required_modalities=("text",),
        expected_context_tokens=4096,
        expected_output_tokens=1024,
        quality_sensitivity=0.9,
        reliability_sensitivity=0.8,
        latency_sensitivity=0.2,
        cost_sensitivity=0.1,
        risk_class="moderate",
        confidence=0.8,
    )
    bucket = build_context_bucket(assessment, ComplexityBands())
    assert bucket.complexity_band == "hard"
    assert bucket.domains == ("coding", "debugging")
    assert len(bucket.bucket_id) == 64


def test_event_rejects_feedback_fields_on_turn_outcome(valid_turn_event):
    payload = valid_turn_event.model_dump(mode="json")
    payload["feedback_value"] = "rating-5"
    with pytest.raises(ValidationError):
        EvidenceEvent.model_validate(payload)


def test_evidence_ids_are_deterministic_and_source_separated():
    first = turn_evidence_id("session-a", "turn-a")
    assert first == turn_evidence_id("session-a", "turn-a")
    assert first != feedback_evidence_id(first, "rating-5")
    assert feedback_evidence_id(first, "rating-4") != feedback_evidence_id(
        first, "rating-5"
    )


def _feedback_payload(valid_turn_event) -> dict[str, object]:
    payload = valid_turn_event.model_dump(mode="json")
    payload.update(
        {
            "evidence_id": feedback_evidence_id(
                valid_turn_event.evidence_id, "rating-5"
            ),
            "source": "user_feedback",
            "signal_type": "explicit_feedback",
            "parent_evidence_id": valid_turn_event.evidence_id,
            "outcome": None,
            "feedback_value": "rating-5",
            "api_calls": 0,
            "tool_iterations": 0,
            "retry_count": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "latency_seconds": None,
        }
    )
    return payload


def test_event_rejects_out_of_range_normalized_value(valid_turn_event):
    payload = valid_turn_event.model_dump(mode="json")
    payload["normalized_value"] = 1.1
    with pytest.raises(ValidationError):
        EvidenceEvent.model_validate(payload)


def test_feedback_event_requires_parent(valid_turn_event):
    payload = _feedback_payload(valid_turn_event)
    payload["parent_evidence_id"] = None
    with pytest.raises(ValidationError, match="feedback evidence requires parent"):
        EvidenceEvent.model_validate(payload)


def test_turn_outcome_event_rejects_parent(valid_turn_event):
    payload = valid_turn_event.model_dump(mode="json")
    payload["parent_evidence_id"] = "c" * 64
    with pytest.raises(ValidationError, match="turn evidence cannot have a parent"):
        EvidenceEvent.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_calls", 1),
        ("tool_iterations", 1),
        ("retry_count", 1),
        ("input_tokens", 1),
        ("output_tokens", 1),
        ("cache_read_tokens", 1),
        ("cost_usd", 0.01),
        ("latency_seconds", 0.1),
    ],
)
def test_feedback_event_rejects_operational_values(
    valid_turn_event, field, value
):
    payload = _feedback_payload(valid_turn_event)
    payload[field] = value
    with pytest.raises(ValidationError, match="feedback cannot duplicate operational"):
        EvidenceEvent.model_validate(payload)


def test_semantics_reject_context_bucket_with_changed_identity(valid_turn_event):
    payload = valid_turn_event.model_dump(mode="json")
    payload["context_bucket"]["bucket_id"] = "c" * 64
    event = EvidenceEvent.model_validate(payload)
    with pytest.raises(ValueError, match="context bucket identity changed"):
        validate_evidence_semantics(event)


def test_continuation_rejects_initial_task_context(valid_turn_event):
    payload = valid_turn_event.model_dump(mode="json")
    payload["is_initial_routing_task"] = False
    with pytest.raises(
        ValidationError, match="continuation evidence cannot carry initial-task context"
    ):
        EvidenceEvent.model_validate(payload)


@pytest.mark.parametrize(
    ("value", "normalized_value"),
    [
        ("rating-1", 0.0),
        ("rating-2", 0.25),
        ("rating-3", 0.5),
        ("rating-4", 0.75),
        ("rating-5", 1.0),
        ("rejected", None),
        ("corrected", None),
        ("manual-reroute", None),
    ],
)
def test_feedback_inherits_parent_attribution_and_uses_finite_mapping(
    valid_turn_event, value, normalized_value
):
    feedback = build_feedback_event(
        valid_turn_event,
        value,
        observed_at="2026-07-17T12:05:00Z",
    )
    assert feedback.is_initial_routing_task is valid_turn_event.is_initial_routing_task
    assert feedback.context_bucket == valid_turn_event.context_bucket
    assert feedback.decision_id == valid_turn_event.decision_id
    assert feedback.route_epoch_id == valid_turn_event.route_epoch_id
    assert feedback.runtime_id == valid_turn_event.runtime_id
    assert feedback.attribution_confidence == valid_turn_event.attribution_confidence
    assert feedback.normalized_value == normalized_value
    assert feedback.confidence_weight == 1.0
    validate_evidence_semantics(feedback)


def test_feedback_parent_must_be_a_turn_outcome(valid_turn_event):
    feedback = build_feedback_event(
        valid_turn_event,
        "rating-5",
        observed_at="2026-07-17T12:05:00Z",
    )
    with pytest.raises(ValueError, match="feedback parent must be routed turn evidence"):
        build_feedback_event(
            feedback,
            "rating-4",
            observed_at="2026-07-17T12:06:00Z",
        )


def test_semantics_reject_changed_identity_or_normalization(valid_turn_event):
    payload = valid_turn_event.model_dump(mode="json")
    payload["normalized_value"] = 0.5
    event = EvidenceEvent.model_validate(payload)
    with pytest.raises(ValueError, match="identity or normalization changed"):
        validate_evidence_semantics(event)
