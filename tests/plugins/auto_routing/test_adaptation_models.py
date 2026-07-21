"""Profile-local conservative-adaptation authority contracts."""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest
from pydantic import ValidationError

from plugins.auto_routing.auto_routing import models as models_module
from plugins.auto_routing.auto_routing.models import (
    AdaptiveExplanation,
    AdaptiveCanaryAssignment,
    AdaptiveLifecycleEvent,
    AdaptiveOverlay,
    AdaptiveProfileControl,
    AdaptiveProfileRevision,
    OptimizerLease,
    ProfileAdaptationSettings,
    RouteProfile,
)


def _target(model: str, *, status: str) -> dict[str, Any]:
    return {
        "runtime": {
            "provider": "openai-codex",
            "model": model,
            "auth_identity": "subscription:default",
            "credential_pool_identity": "pool:codex",
            "endpoint_identity": "endpoint:codex",
            "api_mode": "codex_responses",
            "local_backend": "",
            "inventory_revision": "inventory-1",
        },
        "reasoning": {"default": "medium", "min": "low", "max": "high"},
        "supported_reasoning_efforts": [],
        "revision_status": status,
    }


@pytest.fixture
def valid_profile() -> dict[str, Any]:
    return {
        "profile_id": "coding",
        "description": "Software development tasks",
        "base_rank": 70.0,
        "match": {
            "domains": ["coding"],
            "complexity": ["moderate", "hard"],
            "modalities": ["text"],
            "capabilities": ["tools"],
        },
        "objectives": {
            "quality": 0.55,
            "reliability": 0.25,
            "latency": 0.10,
            "cost": 0.10,
        },
        "primary": _target("gpt-5.4", status="active"),
        "fallbacks": [_target("gpt-5.4-mini", status="fallback")],
        "provenance": [],
    }


@pytest.fixture
def challenger() -> dict[str, Any]:
    return _target("gpt-5.4-challenger", status="challenger")


def test_existing_profile_is_static(valid_profile: dict[str, Any]) -> None:
    profile = RouteProfile.model_validate(valid_profile)

    assert profile.adaptation.enabled is False
    assert profile.primary_choices() == (profile.primary,)


def test_primary_challenger_is_not_fallback(
    valid_profile: dict[str, Any],
    challenger: dict[str, Any],
) -> None:
    profile = RouteProfile.model_validate({
        **valid_profile,
        "primary_challengers": [challenger],
    })

    assert profile.primary_choices()[1].revision_status == "challenger"
    assert profile.fallbacks == RouteProfile.model_validate(valid_profile).fallbacks


def test_enabled_profile_defaults() -> None:
    value = ProfileAdaptationSettings(enabled=True)

    assert (
        value.canary_fraction,
        value.minimum_comparable_samples,
        value.observed_regression_threshold,
    ) == (0.05, 20, 0.10)
    assert (value.cooldown_base_seconds, value.cooldown_max_seconds) == (3_600, 86_400)
    assert value.confidence_level == 0.90


def test_duplicate_primary_runtime_identity_is_rejected(
    valid_profile: dict[str, Any],
) -> None:
    duplicate = copy.deepcopy(valid_profile["primary"])
    duplicate["revision_status"] = "challenger"

    with pytest.raises(ValidationError, match="primary.*duplicate|unique"):
        RouteProfile.model_validate({
            **valid_profile,
            "primary_challengers": [duplicate],
        })


def test_primary_fallback_runtime_overlap_is_rejected(
    valid_profile: dict[str, Any],
    challenger: dict[str, Any],
) -> None:
    fallback = copy.deepcopy(challenger)
    fallback["revision_status"] = "fallback"

    with pytest.raises(ValidationError, match="primary.*fallback|overlap"):
        RouteProfile.model_validate({
            **valid_profile,
            "primary_challengers": [challenger],
            "fallbacks": [fallback],
        })


def test_primary_challenger_requires_challenger_revision_status(
    valid_profile: dict[str, Any],
    challenger: dict[str, Any],
) -> None:
    challenger["revision_status"] = "active"

    with pytest.raises(ValidationError, match="challenger.*revision_status"):
        RouteProfile.model_validate({
            **valid_profile,
            "primary_challengers": [challenger],
        })


def test_enabled_profile_requires_primary_challenger(
    valid_profile: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="enabled.*challenger"):
        RouteProfile.model_validate({
            **valid_profile,
            "adaptation": {"enabled": True},
        })


def test_challenger_reasoning_bounds_are_validated(
    valid_profile: dict[str, Any],
    challenger: dict[str, Any],
) -> None:
    challenger["reasoning"] = {"default": "low", "min": "medium", "max": "high"}

    with pytest.raises(ValidationError, match="minimum.*default.*maximum"):
        RouteProfile.model_validate({
            **valid_profile,
            "primary_challengers": [challenger],
        })


def test_adaptation_records_are_public_and_immutable() -> None:
    required = {
        "AdaptiveCanaryAssignment",
        "AdaptiveLifecycleEvent",
        "AdaptiveOverlay",
        "AdaptiveProfileControl",
        "AdaptiveProfileRevision",
        "OptimizerLease",
        "ProfileAdaptationSettings",
    }
    assert required <= set(models_module.__all__)

    overlay = AdaptiveOverlay(
        profile_id="coding",
        ordered_primary_runtime_ids=("a" * 64,),
        reasoning_defaults={"a" * 64: "medium"},
    )
    with pytest.raises(ValidationError, match="frozen"):
        overlay.profile_id = "research"  # type: ignore[misc]


def test_adaptive_revision_rejects_non_finite_explanation() -> None:
    overlay = AdaptiveOverlay(
        profile_id="coding",
        ordered_primary_runtime_ids=("a" * 64,),
        reasoning_defaults={},
    )

    with pytest.raises(ValidationError, match="JSON-compatible|finite"):
        AdaptiveProfileRevision(
            revision_id="revision-a",
            authority_id="b" * 64,
            profile_id="coding",
            parent_revision_id=None,
            overlay=overlay,
            explanation={"metrics": {"score": math.nan}},
            lifecycle="validated",
            created_at="2026-07-18T12:00:00Z",
            complete=True,
        )


def test_overlay_rejects_duplicate_ordered_primary_runtime_ids() -> None:
    with pytest.raises(ValidationError, match="unique"):
        AdaptiveOverlay(
            profile_id="coding",
            ordered_primary_runtime_ids=("a" * 64, "a" * 64),
            reasoning_defaults={},
        )


def test_overlay_reasoning_defaults_must_reference_ordered_primary() -> None:
    with pytest.raises(ValidationError, match="reasoning_defaults.*ordered"):
        AdaptiveOverlay(
            profile_id="coding",
            ordered_primary_runtime_ids=("a" * 64,),
            reasoning_defaults={"b" * 64: "medium"},
        )


@pytest.mark.parametrize(
    "unsafe_explanation",
    [
        {"prompt": "PROMPT_SENTINEL user text"},
        {"response": "RESPONSE_SENTINEL assistant text"},
        {"endpoint": "https://private.invalid/v1"},
        {"credential": "sk-secret-sentinel"},
    ],
)
def test_adaptive_explanation_rejects_free_form_content(
    unsafe_explanation: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs|content-free|pattern"):
        AdaptiveExplanation.model_validate(unsafe_explanation)


def test_revision_lifecycle_and_assignment_reject_content_sentinels() -> None:
    overlay = AdaptiveOverlay(
        profile_id="coding",
        ordered_primary_runtime_ids=("a" * 64,),
        reasoning_defaults={},
    )
    revision = {
        "revision_id": "revision-a",
        "authority_id": "b" * 64,
        "profile_id": "coding",
        "parent_revision_id": None,
        "overlay": overlay,
        "explanation": {"prompt": "PROMPT_SENTINEL"},
        "lifecycle": "validated",
        "created_at": "2026-07-18T12:00:00Z",
        "complete": True,
    }
    event = {
        "event_id": "event-a",
        "authority_id": "b" * 64,
        "profile_id": "coding",
        "revision_id": "revision-a",
        "event_type": "validated",
        "reason_code": "enough_evidence",
        "explanation": {"response": "RESPONSE_SENTINEL"},
        "created_at": "2026-07-18T12:00:00Z",
    }
    assignment = {
        "assignment_id": "assignment-a",
        "authority_id": "b" * 64,
        "profile_id": "coding",
        "operation_identity_hash": "c" * 64,
        "context_bucket_id": "d" * 64,
        "control_revision_id": "revision-control",
        "challenger_revision_id": "revision-challenger",
        "arm": "challenger",
        "created_at": "2026-07-18T12:00:00Z",
        "credential": "sk-secret-sentinel",
    }

    for model, document in (
        (AdaptiveProfileRevision, revision),
        (AdaptiveLifecycleEvent, event),
        (AdaptiveCanaryAssignment, assignment),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(document)


def test_adaptive_revision_requires_overlay_profile_linkage() -> None:
    overlay = AdaptiveOverlay(
        profile_id="research",
        ordered_primary_runtime_ids=("a" * 64,),
        reasoning_defaults={},
    )

    with pytest.raises(ValidationError, match="overlay.*profile"):
        AdaptiveProfileRevision(
            revision_id="revision-a",
            authority_id="b" * 64,
            profile_id="coding",
            parent_revision_id=None,
            overlay=overlay,
            explanation={},
            lifecycle="validated",
            created_at="2026-07-18T12:00:00Z",
            complete=True,
        )


def test_profile_local_records_require_authority_and_profile_linkage() -> None:
    common = {"authority_id": "b" * 64, "profile_id": "coding"}
    event = AdaptiveLifecycleEvent(
        event_id="event-a",
        **common,
        revision_id="revision-a",
        event_type="validated",
        reason_code="enough_evidence",
        explanation={},
        created_at="2026-07-18T12:00:00Z",
    )
    assignment = AdaptiveCanaryAssignment(
        assignment_id="assignment-a",
        **common,
        operation_identity_hash="c" * 64,
        context_bucket_id="d" * 64,
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        arm="challenger",
        created_at="2026-07-18T12:00:00Z",
    )
    control = AdaptiveProfileControl(
        **common,
        active_revision_id="revision-a",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="canary",
        frozen=False,
        cooldown_until=None,
        rejection_count=0,
        generation=1,
        updated_at="2026-07-18T12:00:00Z",
    )
    lease = OptimizerLease(
        **common,
        owner_id="owner-a",
        lease_expires_at="2026-07-18T12:00:10Z",
        generation=1,
        updated_at="2026-07-18T12:00:00Z",
    )

    assert {
        event.profile_id,
        assignment.profile_id,
        control.profile_id,
        lease.profile_id,
    } == {"coding"}
    assert {
        event.authority_id,
        assignment.authority_id,
        control.authority_id,
        lease.authority_id,
    } == {"b" * 64}
