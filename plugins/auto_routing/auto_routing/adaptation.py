"""Pure profile-local overlay validation and materialization."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from types import MappingProxyType

from .models import (
    AdaptiveOverlay,
    AutoRoutingConfig,
    REASONING_EFFORT_ORDER,
    RouteProfile,
    RoutingTarget,
)


_EFFORT_POSITION = {
    effort: index for index, effort in enumerate(REASONING_EFFORT_ORDER)
}


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def operation_identity_hash(
    *,
    scope: str,
    session_id: str,
    task_id: str,
    operation_id: str | None,
    task_index: int | None,
) -> str:
    """Hash only the stable identity fields for one routing operation."""
    if scope == "fresh_session":
        value: dict[str, object] = {
            "scope": scope,
            "session_id": session_id,
            "task_id": task_id,
        }
    elif scope == "delegation":
        value = {
            "scope": scope,
            "operation_id": operation_id,
            "task_index": task_index,
            "task_id": task_id,
        }
    else:
        raise ValueError("operation identity scope must be fresh_session or delegation")
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def deterministic_canary_arm(
    profile_key: bytes,
    profile_id: str,
    operation_hash: str,
    fraction: float,
) -> str:
    """Return a stable profile-local arm without storing a raw operation identity."""
    if not isinstance(profile_key, bytes) or len(profile_key) != 32:
        raise ValueError("deterministic canary assignment requires a 32-byte key")
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not math.isfinite(fraction)
        or not 0.0 <= fraction <= 1.0
    ):
        raise ValueError("canary fraction must be finite and between zero and one")
    digest = hmac.new(
        profile_key,
        f"{profile_id}:{operation_hash}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    sample = int.from_bytes(digest[:8], "big") / 2**64
    return "challenger" if sample < fraction else "control"


def canary_eligible(
    *,
    scope: str,
    is_resume: bool,
    is_compression: bool,
    manual_override: bool,
    fixed_runtime: bool,
    risk_class: str,
    canary_high_risk_tasks: bool,
    policy_compliant: bool,
    frozen: bool,
    adaptation_enabled: bool,
    challenger_available: bool,
    canary_fraction: float,
) -> bool:
    """Apply the closed, fail-safe policy gate for deterministic canaries."""
    boolean_gates = (
        is_resume,
        is_compression,
        manual_override,
        fixed_runtime,
        canary_high_risk_tasks,
        policy_compliant,
        frozen,
        adaptation_enabled,
        challenger_available,
    )
    if any(type(value) is not bool for value in boolean_gates):
        return False
    if type(scope) is not str or scope not in ("fresh_session", "delegation"):
        return False
    if type(risk_class) is not str or risk_class not in (
        "low",
        "moderate",
        "high",
        "critical",
    ):
        return False
    if (
        isinstance(canary_fraction, bool)
        or not isinstance(canary_fraction, (int, float))
        or not math.isfinite(canary_fraction)
        or not 0.0 < canary_fraction <= 1.0
    ):
        return False
    return bool(
        not is_resume
        and not is_compression
        and not manual_override
        and not fixed_runtime
        and (
            risk_class not in ("high", "critical")
            or canary_high_risk_tasks
        )
        and policy_compliant
        and not frozen
        and adaptation_enabled
        and challenger_available
    )


def validate_overlay(
    config: AutoRoutingConfig,
    overlay: AdaptiveOverlay,
) -> AdaptiveOverlay:
    """Require an overlay to stay exactly within a profile's primary authority."""
    profile = config.profiles.get(overlay.profile_id)
    if profile is None:
        raise ValueError("overlay profile is not in authority")
    _validate_profile_overlay(profile, overlay)
    return overlay


def materialize_profile(
    profile: RouteProfile,
    overlay: AdaptiveOverlay,
) -> RouteProfile:
    """Reorder approved primaries and apply bounded default-effort overrides."""
    targets = _validate_profile_overlay(profile, overlay)
    ordered = tuple(
        _with_overlay_effort(targets[runtime_id], overlay).model_copy(
            update={"revision_status": "active" if index == 0 else "challenger"}
        )
        for index, runtime_id in enumerate(overlay.ordered_primary_runtime_ids)
    )
    return profile.model_copy(
        update={"primary": ordered[0], "primary_challengers": ordered[1:]}
    )


def materialize_profiles(
    config: AutoRoutingConfig,
    overlays: Mapping[str, AdaptiveOverlay],
) -> AutoRoutingConfig:
    """Return a config copy with each validated profile-local overlay applied."""
    validated: dict[str, AdaptiveOverlay] = {}
    for profile_id, overlay in overlays.items():
        if profile_id != overlay.profile_id:
            raise ValueError("overlay mapping key must match overlay profile")
        validated[profile_id] = validate_overlay(config, overlay)
    profiles = {
        profile_id: (
            materialize_profile(profile, validated[profile_id])
            if profile_id in validated
            else profile
        )
        for profile_id, profile in config.profiles.items()
    }
    # ``model_copy`` intentionally bypasses Pydantic validation. Preserve the
    # immutable authority-map contract even for this transient materialization.
    return config.model_copy(update={"profiles": MappingProxyType(profiles)})


def static_adaptive_revision_id(authority_id: str, profile_id: str) -> str:
    """Return the deterministic static revision identity for one profile."""
    payload = f"static-adaptive-revision:{authority_id}:{profile_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _effort_is_within_target_bounds(target: RoutingTarget, effort: str) -> bool:
    position = _EFFORT_POSITION.get(effort)
    return position is not None and (
        _EFFORT_POSITION[target.reasoning.minimum]
        <= position
        <= _EFFORT_POSITION[target.reasoning.maximum]
    )


def _validate_profile_overlay(
    profile: RouteProfile,
    overlay: AdaptiveOverlay,
) -> dict[str, RoutingTarget]:
    if overlay.profile_id != profile.profile_id:
        raise ValueError("overlay profile does not match materialized profile")
    targets = {
        target.runtime.stable_id(): target for target in profile.primary_choices()
    }
    if set(overlay.ordered_primary_runtime_ids) != set(targets):
        raise ValueError("overlay must name the exact primary choices")
    if len(overlay.ordered_primary_runtime_ids) != len(targets):
        raise ValueError("overlay primary choices must be unique")
    for runtime_id, effort in overlay.reasoning_defaults.items():
        if (
            runtime_id not in targets
            or not _effort_is_within_target_bounds(targets[runtime_id], effort)
        ):
            raise ValueError("overlay reasoning effort outside target bounds")
    return targets


def _with_overlay_effort(
    target: RoutingTarget,
    overlay: AdaptiveOverlay,
) -> RoutingTarget:
    effort = overlay.reasoning_defaults.get(target.runtime.stable_id())
    if effort is None:
        return target
    reasoning = target.reasoning.model_copy(update={"default": effort})
    return target.model_copy(update={"reasoning": reasoning})


__all__ = [
    "canary_eligible",
    "deterministic_canary_arm",
    "materialize_profile",
    "materialize_profiles",
    "operation_identity_hash",
    "static_adaptive_revision_id",
    "validate_overlay",
]
