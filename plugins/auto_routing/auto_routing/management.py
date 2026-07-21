"""Pure, deterministic profile-management ranking and planning.

This module intentionally works only from an already-projected inventory
snapshot and an already-verified ranking pack.  It does not load either input,
write configuration, or mutate adaptation state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .inventory import ManagementInventoryCandidate
from .models import (
    ManagementCanaryAssignment,
    ManagementPatch,
    ObjectiveWeights,
    ProfileLimits,
    REASONING_EFFORT_ORDER,
    ReasoningBounds,
    RouteProfile,
    RoutingTarget,
)
from .ranking_pack import RankingPackRow, VerifiedRankingPack


_VERIFIED_SOURCES = frozenset({
    "authenticated_live",
    "validated_contract",
    "explicit_probe",
    "installed_local",
})
_EFFORT_POSITION = {
    effort: index for index, effort in enumerate(REASONING_EFFORT_ORDER)
}


@dataclass(frozen=True, slots=True)
class RankedManagementCandidate:
    """One supplied candidate's deterministic management-rank result."""

    runtime_id: str
    eligible: bool
    reason_codes: tuple[str, ...]
    score: float | None


@dataclass(frozen=True, slots=True)
class ManagementPlan:
    """A pure proposed profile transition, never a configuration write."""

    action: Literal["hold", "no_change", "propose_canary", "fallback_reorder"]
    after_profile: RouteProfile | None
    patch: ManagementPatch | None
    before_runtime_ids: tuple[str, ...]
    after_runtime_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    planned_at: datetime | None = None


def _score(weights: ObjectiveWeights, row: RankingPackRow) -> float:
    """Apply only profile objectives to signed, normalized pack metrics."""
    return (
        weights.quality * row.quality
        + weights.reliability * row.reliability
        + weights.latency * (1.0 - row.latency)
        + weights.cost * (1.0 - row.cost)
    )


def _estimated_cost(candidate: ManagementInventoryCandidate) -> float | None:
    economics = candidate.economics
    if economics.billing_kind in {"metered", "subscription"}:
        values = tuple(
            value
            for value in (
                economics.effective_marginal_cost_usd_per_task,
                economics.effective_amortized_cost_usd_per_task,
            )
            if value is not None
        )
        return max(values) if values else None
    values = tuple(
        value
        for value in (
            economics.local_compute_cost_usd_per_task,
            economics.local_energy_cost_usd_per_task,
        )
        if value is not None
    )
    return sum(values) if values else None


def _bounded_efforts(
    candidate: ManagementInventoryCandidate,
    limits: ProfileLimits | None,
) -> tuple[str, ...]:
    if not candidate.reasoning_support.exact:
        return ()
    maximum = (
        _EFFORT_POSITION[limits.max_reasoning_effort]
        if limits is not None and limits.max_reasoning_effort is not None
        else len(REASONING_EFFORT_ORDER) - 1
    )
    return tuple(
        effort
        for effort in REASONING_EFFORT_ORDER
        if effort in candidate.reasoning_support.efforts
        and _EFFORT_POSITION[effort] <= maximum
    )


def _has_bounded_latency(
    candidate: ManagementInventoryCandidate,
    limit: float | None,
) -> bool:
    if limit is None:
        return True
    value = candidate.capabilities.get("estimated_latency_seconds")
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        and value <= limit
    )


def _eligibility_reason(
    profile: RouteProfile,
    candidate: ManagementInventoryCandidate,
    pack: VerifiedRankingPack,
) -> str | None:
    """Return the first applicable rejection in the management contract order."""
    limits = profile.limits
    if (
        candidate.verification_source not in _VERIFIED_SOURCES
        or not candidate.verification_expires_at
        or not candidate.resolver_name
    ):
        return "inventory_not_verified"
    if candidate.capabilities.get("supports_tools") is not True:
        return "local_capability_missing"
    if pack.rank_for(candidate.runtime_id) is None:
        return "ranking_missing"
    if (
        candidate.key.local_backend
        and limits is not None
        and limits.allowed_licenses is not None
        and candidate.capabilities.get("license_id") not in limits.allowed_licenses
    ):
        return "license_rejected"
    if limits is not None and limits.minimum_context_tokens is not None:
        context_window = candidate.capabilities.get("context_window")
        if (
            isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or context_window < limits.minimum_context_tokens
        ):
            return "context_limit_rejected"
    if not _bounded_efforts(candidate, limits):
        return "reasoning_limit_rejected"
    if limits is not None and limits.max_estimated_task_cost_usd is not None:
        cost = _estimated_cost(candidate)
        if cost is None or cost > limits.max_estimated_task_cost_usd:
            return "cost_limit_rejected"
    if not _has_bounded_latency(
        candidate,
        limits.max_estimated_latency_seconds if limits is not None else None,
    ):
        return "latency_limit_rejected"
    return None


def rank_management_candidates(
    profile: RouteProfile,
    candidates: tuple[ManagementInventoryCandidate, ...],
    pack: VerifiedRankingPack,
) -> tuple[RankedManagementCandidate, ...]:
    """Rank an immutable candidate snapshot with a signed local pack only."""
    ranked: list[RankedManagementCandidate] = []
    for candidate in candidates:
        rejection = _eligibility_reason(profile, candidate, pack)
        metrics = pack.rank_for(candidate.runtime_id)
        ranked.append(
            RankedManagementCandidate(
                runtime_id=candidate.runtime_id,
                eligible=rejection is None,
                reason_codes=() if rejection is None else (rejection,),
                score=None
                if rejection is not None or metrics is None
                else _score(profile.objectives, metrics),
            )
        )
    return tuple(
        sorted(
            ranked,
            key=lambda item: (not item.eligible, -(item.score or 0.0), item.runtime_id),
        )
    )


def management_hold(reason_code: str) -> ManagementPlan:
    """Return an explicit no-mutation result for an unsafe management input."""
    return ManagementPlan(
        action="hold",
        after_profile=None,
        patch=None,
        before_runtime_ids=(),
        after_runtime_ids=(),
        reason_codes=(reason_code,),
    )


def _runtime_ids(profile: RouteProfile) -> tuple[str, ...]:
    return tuple(
        target.runtime.stable_id()
        for target in (*profile.primary_choices(), *profile.fallbacks)
    )


def _candidate_target(
    profile: RouteProfile,
    candidate: ManagementInventoryCandidate,
) -> RoutingTarget | None:
    """Project only verified runtime data into a bounded challenger target."""
    allowed = _bounded_efforts(candidate, profile.limits)
    if not allowed:
        return None
    existing_default = profile.primary.reasoning.default
    if _EFFORT_POSITION[existing_default] <= _EFFORT_POSITION[allowed[0]]:
        default = allowed[0]
    elif _EFFORT_POSITION[existing_default] >= _EFFORT_POSITION[allowed[-1]]:
        default = allowed[-1]
    else:
        default = existing_default
    limits = profile.limits
    return RoutingTarget(
        runtime=candidate.key,
        reasoning=ReasoningBounds(default=default, min=allowed[0], max=allowed[-1]),
        supported_reasoning_efforts=tuple(candidate.reasoning_support.efforts),
        max_estimated_task_cost_usd=(
            limits.max_estimated_task_cost_usd if limits is not None else None
        ),
        max_estimated_latency_seconds=(
            limits.max_estimated_latency_seconds if limits is not None else None
        ),
        revision_status="challenger",
    )


def _has_duplicate_runtime_ids(
    candidates: tuple[ManagementInventoryCandidate, ...],
) -> bool:
    runtime_ids = tuple(candidate.runtime_id for candidate in candidates)
    return len(runtime_ids) != len(set(runtime_ids))


def _deduplicate_targets(
    targets: tuple[RoutingTarget, ...],
) -> tuple[RoutingTarget, ...]:
    seen: set[str] = set()
    result: list[RoutingTarget] = []
    for target in targets:
        runtime_id = target.runtime.stable_id()
        if runtime_id not in seen:
            seen.add(runtime_id)
            result.append(target)
    return tuple(result)


def _assignment_runtime_ids(
    assignments: tuple[ManagementCanaryAssignment, ...],
) -> set[str]:
    """Read optional runtime bindings without inventing lifecycle semantics.

    The frozen Stage 5 record currently carries revision identities only.  The
    optional attribute keeps this planner compatible with a later terminal
    assignment projection while treating every supplied assignment as active.
    """
    return {
        runtime_id
        for assignment in assignments
        if isinstance((runtime_id := getattr(assignment, "runtime_id", None)), str)
    }


def _fallback_target(
    profile: RouteProfile,
    candidate: ManagementInventoryCandidate,
) -> RoutingTarget | None:
    target = _candidate_target(profile, candidate)
    return (
        None
        if target is None
        else target.model_copy(update={"revision_status": "fallback"})
    )


def _managed_fallbacks(
    profile: RouteProfile,
    eligible: tuple[RankedManagementCandidate, ...],
    candidates: tuple[ManagementInventoryCandidate, ...],
    *,
    excluded_runtime_ids: set[str],
) -> tuple[RoutingTarget, ...]:
    existing = {target.runtime.stable_id(): target for target in profile.fallbacks}
    candidate_by_id = {candidate.runtime_id: candidate for candidate in candidates}
    fallbacks: list[RoutingTarget] = []
    for item in eligible:
        if item.runtime_id in excluded_runtime_ids:
            continue
        target = existing.get(item.runtime_id)
        if target is None:
            candidate = candidate_by_id.get(item.runtime_id)
            target = None if candidate is None else _fallback_target(profile, candidate)
        if target is not None and target.runtime.stable_id() in excluded_runtime_ids:
            continue
        if target is not None:
            fallbacks.append(target.model_copy(update={"revision_status": "fallback"}))
        if len(fallbacks) == 64:
            break
    return tuple(fallbacks)


def _profile_plan(
    *,
    action: Literal["no_change", "propose_canary", "fallback_reorder"],
    profile: RouteProfile,
    after_profile: RouteProfile,
    reason_code: str,
    now: datetime,
) -> ManagementPlan:
    before = _runtime_ids(profile)
    after = _runtime_ids(after_profile)
    if profile == after_profile:
        return ManagementPlan(
            action="no_change",
            after_profile=profile,
            patch=None,
            before_runtime_ids=before,
            after_runtime_ids=before,
            reason_codes=("canonical_order",),
            planned_at=now,
        )
    patch = ManagementPatch(
        profile_id=profile.profile_id,
        before_runtime_ids=before,
        after_runtime_ids=after,
        reason_codes=(reason_code,),
    )
    return ManagementPlan(
        action=action,
        after_profile=after_profile,
        patch=patch,
        before_runtime_ids=before,
        after_runtime_ids=after,
        reason_codes=(reason_code,),
        planned_at=now,
    )


def _build_safe_profile_plan(
    profile: RouteProfile,
    eligible: tuple[RankedManagementCandidate, ...],
    candidates: tuple[ManagementInventoryCandidate, ...],
    active_assignments: tuple[ManagementCanaryAssignment, ...],
    now: datetime,
) -> ManagementPlan:
    before = _runtime_ids(profile)
    candidate_by_id = {candidate.runtime_id: candidate for candidate in candidates}
    leader = eligible[0]
    leader_candidate = candidate_by_id.get(leader.runtime_id)
    if leader_candidate is None:
        return management_hold("unsafe_mutation")

    leader_target = _candidate_target(profile, leader_candidate)
    if leader_target is None:
        return management_hold("unsafe_mutation")
    leader_target_id = leader_target.runtime.stable_id()
    primary_ids = {target.runtime.stable_id() for target in profile.primary_choices()}
    leader_is_fallback = any(
        target.runtime.stable_id() == leader_target_id for target in profile.fallbacks
    )
    if leader_target_id not in primary_ids:
        # A new leader may only be introduced as a challenger.  When all slots
        # are occupied, do not evict an existing route to make room.
        if len(profile.primary_challengers) >= 64 and len(profile.fallbacks) >= 64:
            return management_hold("unsafe_mutation")
        displaced: RoutingTarget | None = None
        if leader_is_fallback:
            leader_target = next(
                target.model_copy(update={"revision_status": "challenger"})
                for target in profile.fallbacks
                if target.runtime.stable_id() == leader_target_id
            )
        challengers = _deduplicate_targets((
            leader_target,
            *profile.primary_challengers,
        ))
        if len(challengers) > 64:
            displaced = challengers[-1].model_copy(
                update={"revision_status": "fallback"}
            )
            challengers = challengers[:-1]
            fallbacks = _deduplicate_targets((displaced, *profile.fallbacks))
        else:
            fallbacks = _managed_fallbacks(
                profile,
                eligible,
                candidates,
                excluded_runtime_ids={
                    profile.primary.runtime.stable_id(),
                    *(target.runtime.stable_id() for target in challengers),
                },
            )
        if displaced is not None and all(
            item.runtime.stable_id() != displaced.runtime.stable_id()
            for item in fallbacks
        ):
            fallbacks = (*fallbacks, displaced)
        if len(fallbacks) > 64:
            return management_hold("unsafe_mutation")
        after_profile = profile.model_copy(
            update={"primary_challengers": challengers, "fallbacks": fallbacks}
        )
        after_ids = _runtime_ids(after_profile)
        required = _assignment_runtime_ids(active_assignments)
        if not required.issubset(after_ids) or not after_ids:
            return management_hold("unsafe_mutation")
        return _profile_plan(
            action="propose_canary",
            profile=profile,
            after_profile=after_profile,
            reason_code=(
                "fallback_primary_challenger"
                if leader_is_fallback
                else "new_primary_challenger"
            ),
            now=now,
        )

    fallbacks = _managed_fallbacks(
        profile,
        eligible,
        candidates,
        excluded_runtime_ids=primary_ids,
    )
    after_profile = profile.model_copy(update={"fallbacks": fallbacks})
    after_ids = _runtime_ids(after_profile)
    required = _assignment_runtime_ids(active_assignments)
    if not required.issubset(after_ids) or not after_ids:
        return management_hold("unsafe_mutation")
    return _profile_plan(
        action="fallback_reorder",
        profile=profile,
        after_profile=after_profile,
        reason_code="fallback_reordered",
        now=now,
    )


def plan_management_revision(
    *,
    profile: RouteProfile,
    candidates: tuple[ManagementInventoryCandidate, ...],
    pack: VerifiedRankingPack,
    active_assignments: tuple[ManagementCanaryAssignment, ...],
    now: datetime,
) -> ManagementPlan:
    """Create one conservative, deterministic profile-management proposal."""
    normalized_now = (
        now.replace(tzinfo=UTC)
        if now.tzinfo is None or now.utcoffset() is None
        else now.astimezone(UTC)
    )
    if _has_duplicate_runtime_ids(candidates):
        return management_hold("unsafe_mutation")
    ranked = rank_management_candidates(profile, candidates, pack)
    eligible = tuple(item for item in ranked if item.eligible)
    if not eligible:
        return management_hold("no_eligible_candidate")
    return _build_safe_profile_plan(
        profile,
        eligible,
        candidates,
        active_assignments,
        normalized_now,
    )


__all__ = [
    "ManagementPlan",
    "RankedManagementCandidate",
    "management_hold",
    "plan_management_revision",
    "rank_management_candidates",
]
