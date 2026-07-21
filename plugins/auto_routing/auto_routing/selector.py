"""Deterministic static-route selection over current verified runtimes."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Mapping, get_args

from hermes_constants import (
    effective_generic_reasoning_effort,
    resolve_reasoning_config,
)

from .eligibility import (
    runtime_capability_rejection_reasons,
    runtime_policy_rejection_reasons,
)
from .inventory import ExecutableRuntime, InventorySnapshot
from .models import (
    CandidateReasonCode,
    ComplexityBands,
    DecisionCandidate,
    PolicyEnvelope,
    ProfileLimits,
    ProfileMatch,
    REASONING_EFFORT_ORDER,
    RouteProfile,
    RoutingTarget,
    TaskAssessment,
    candidate_id_for,
)
from .scoring import (
    SELECTOR_SCORE_COMPONENTS,
    conservative_metric,
    normalize_against_limit,
    normalize_catalog_metric,
    utility_score,
)
PROFILE_AFFINITY_COEFFICIENT = 0.15
BASE_RANK_PRIOR_COEFFICIENT = 0.01
_SAFE_DEFAULT_PROFILE_ID = "safe-default"
_FINITE_REASONS = frozenset(get_args(CandidateReasonCode))
_EFFORT_POSITION = {
    effort: index for index, effort in enumerate(REASONING_EFFORT_ORDER)
}


@dataclass(frozen=True)
class SelectionResult:
    """Complete content-free result of one deterministic selection pass."""

    assessment: TaskAssessment | None
    candidates: tuple[DecisionCandidate, ...]
    eligible_runtime_ids: tuple[str, ...]
    rejections: Mapping[str, tuple[CandidateReasonCode, ...]]
    score_calls: tuple[str, ...]
    selected_profile_id: str | None
    selected_runtime: ExecutableRuntime
    selected_reasoning_effort: str
    fallbacks: tuple[RoutingTarget, ...]
    safe_default_runtime: ExecutableRuntime
    safe_default_reasoning_effort: str
    selection_reason: str
    safe_default_reason: str | None = None

    def semantic_record(self) -> dict[str, object]:
        """Return stable route semantics without task content or execution data."""
        return {
            "assessment": (
                None
                if self.assessment is None
                else self.assessment.model_dump(mode="json")
            ),
            "candidates": [
                candidate.model_dump(mode="json") for candidate in self.candidates
            ],
            "eligible_runtime_ids": list(self.eligible_runtime_ids),
            "selected_profile_id": self.selected_profile_id,
            "selected_runtime_id": self.selected_runtime.key.stable_id(),
            "selected_reasoning_effort": self.selected_reasoning_effort,
            "projected_fallbacks": [
                target.model_dump(mode="json") for target in self.fallbacks
            ],
            "safe_default_runtime_id": self.safe_default_runtime.key.stable_id(),
            "safe_default_reasoning_effort": self.safe_default_reasoning_effort,
            "selection_reason": self.selection_reason,
            "safe_default_reason": self.safe_default_reason,
        }


@dataclass(frozen=True)
class _Evaluation:
    profile: RouteProfile
    target: RoutingTarget
    role: str
    ordinal: int
    runtime: ExecutableRuntime | None
    reasoning_effort: str | None
    reasons: tuple[CandidateReasonCode, ...]
    candidate: DecisionCandidate


def profile_affinity(
    *,
    assessment: TaskAssessment,
    match: ProfileMatch,
    complexity_bands: ComplexityBands,
) -> float:
    """Mean intersection across configured profile-match dimensions.

    A non-empty dimension contributes one for any intersection and zero for
    none. Empty dimensions are neutral and excluded from the denominator.
    """
    dimensions: list[bool] = []
    pairs = (
        (match.domains, assessment.domains),
        (match.modalities, assessment.required_modalities),
        (match.capabilities, assessment.required_capabilities),
    )
    for wanted, actual in pairs:
        if wanted:
            dimensions.append(bool(set(wanted) & set(actual)))
    if match.complexity:
        dimensions.append(
            complexity_bands.label(assessment.complexity) in match.complexity
        )
    if not dimensions:
        return 0.0
    return sum(dimensions) / len(dimensions)


class StaticSelector:
    """Select one profile primary and its exact eligible fallback chain."""

    def __init__(self, *, catalog: object, now) -> None:
        self.catalog = catalog
        self._now = now

    def select(
        self,
        *,
        profiles: tuple[RouteProfile, ...],
        assessment: TaskAssessment,
        inventory: InventorySnapshot,
        policy: PolicyEnvelope,
        complexity_bands: ComplexityBands,
        safe_default: RoutingTarget,
        requested_reasoning_effort: str | None = None,
        pinned_profile_id: str | None = None,
        preferred_profile_id: str | None = None,
        hermes_config: dict | None = None,
        task_definition: str = "default",
    ) -> SelectionResult:
        """Run inventory join, hard gates, scoring, and projection in order."""
        now = self._current_time()
        current = self._current_inventory(inventory)
        ordered_profiles = tuple(sorted(profiles, key=lambda item: item.profile_id))
        if len({profile.profile_id for profile in ordered_profiles}) != len(
            ordered_profiles
        ):
            raise ValueError("selector profiles contain duplicate profile_id")

        safe_runtime, safe_effort = self._validated_safe_default(
            safe_default,
            assessment=assessment,
            current=current,
            policy=policy,
            now=now,
            requested_effort=requested_reasoning_effort,
            hermes_config=hermes_config,
            task_definition=task_definition,
        )
        normalized_ranks = _normalized_base_ranks(ordered_profiles)
        evaluations: list[_Evaluation] = []
        score_calls: list[str] = []

        for profile in ordered_profiles:
            primary_targets = tuple(
                ("primary", index, target)
                for index, target in enumerate(profile.primary_choices())
            )
            targets = primary_targets + tuple(
                ("fallback", index, target)
                for index, target in enumerate(profile.fallbacks)
            )
            for role, ordinal, target in targets:
                runtime = current.get(target.runtime.stable_id())
                reasons, effort = self._target_eligibility(
                    runtime,
                    target=target,
                    profile=profile,
                    assessment=assessment,
                    policy=policy,
                    now=now,
                    requested_effort=requested_reasoning_effort,
                    hermes_config=hermes_config,
                    task_definition=task_definition,
                )
                scoring_inputs: tuple[tuple[str, float], ...] = ()
                final_score: float | None = None
                if not reasons and runtime is not None:
                    scoring_inputs, final_score = self._score_target(
                        runtime,
                        target=target,
                        profile=profile,
                        assessment=assessment,
                        policy=policy,
                        complexity_bands=complexity_bands,
                        normalized_base_rank=normalized_ranks[profile.profile_id],
                        task_definition=task_definition,
                    )
                    runtime_id = runtime.key.stable_id()
                    if runtime_id not in score_calls:
                        score_calls.append(runtime_id)
                runtime_id = target.runtime.stable_id()
                candidate = DecisionCandidate(
                    candidate_id=candidate_id_for(
                        profile.profile_id,
                        role,
                        ordinal,
                        runtime_id,
                    ),
                    profile_id=profile.profile_id,
                    target_role=role,
                    target_ordinal=ordinal,
                    runtime_id=runtime_id,
                    eligible=not reasons,
                    reason_codes=reasons,
                    normalized_scoring_inputs=scoring_inputs,
                    final_score=final_score,
                )
                evaluations.append(
                    _Evaluation(
                        profile=profile,
                        target=target,
                        role=role,
                        ordinal=ordinal,
                        runtime=runtime,
                        reasoning_effort=effort,
                        reasons=reasons,
                        candidate=candidate,
                    )
                )

        safe_id = safe_runtime.key.stable_id()
        safe_candidate = DecisionCandidate(
            candidate_id=candidate_id_for(
                _SAFE_DEFAULT_PROFILE_ID,
                "safe_default",
                0,
                safe_id,
            ),
            profile_id=_SAFE_DEFAULT_PROFILE_ID,
            target_role="safe_default",
            target_ordinal=0,
            runtime_id=safe_id,
            eligible=True,
            reason_codes=(),
            normalized_scoring_inputs=(),
            final_score=None,
        )
        candidates = tuple(item.candidate for item in evaluations) + (safe_candidate,)
        eligible_runtime_ids = _unique(
            item.candidate.runtime_id
            for item in evaluations
            if item.candidate.eligible
        )
        rejections = _rejection_summary(evaluations)
        active_targets = _active_profile_targets(evaluations)
        selected = _select_target(
            active_targets,
            pinned_profile_id=pinned_profile_id,
            preferred_profile_id=preferred_profile_id,
        )
        if selected is None:
            return SelectionResult(
                assessment=assessment,
                candidates=candidates,
                eligible_runtime_ids=eligible_runtime_ids,
                rejections=MappingProxyType(rejections),
                score_calls=tuple(score_calls),
                selected_profile_id=None,
                selected_runtime=safe_runtime,
                selected_reasoning_effort=safe_effort,
                fallbacks=(),
                safe_default_runtime=safe_runtime,
                safe_default_reasoning_effort=safe_effort,
                selection_reason="safe_default",
                safe_default_reason="no_eligible_runtime",
            )

        fallbacks = tuple(
            _current_target(item.target, item.runtime, item.reasoning_effort)
            for item in evaluations
            if item.profile.profile_id == selected.profile.profile_id
            and item.role == "fallback"
            and (
                selected.role == "primary"
                or item.ordinal > selected.ordinal
            )
            and item.candidate.eligible
            and item.runtime is not None
        )
        selection_reason = "highest_eligible_score"
        if selected.role == "fallback":
            selection_reason = "pre_call_fallback"
        elif pinned_profile_id == selected.profile.profile_id:
            selection_reason = "pinned_profile"
        elif preferred_profile_id == selected.profile.profile_id:
            selection_reason = "preferred_profile"
        return SelectionResult(
            assessment=assessment,
            candidates=candidates,
            eligible_runtime_ids=eligible_runtime_ids,
            rejections=MappingProxyType(rejections),
            score_calls=tuple(score_calls),
            selected_profile_id=selected.profile.profile_id,
            selected_runtime=selected.runtime,
            selected_reasoning_effort=str(selected.reasoning_effort),
            fallbacks=fallbacks,
            safe_default_runtime=safe_runtime,
            safe_default_reasoning_effort=safe_effort,
            selection_reason=selection_reason,
        )

    def _current_time(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("selector clock must return an aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _current_inventory(
        inventory: InventorySnapshot,
    ) -> dict[str, ExecutableRuntime]:
        current: dict[str, ExecutableRuntime] = {}
        for runtime in inventory.runtimes:
            runtime_id = runtime.key.stable_id()
            if runtime_id in current:
                raise ValueError("current inventory contains duplicate runtime")
            current[runtime_id] = runtime
        return current

    def _validated_safe_default(
        self,
        target: RoutingTarget,
        *,
        assessment: TaskAssessment,
        current: Mapping[str, ExecutableRuntime],
        policy: PolicyEnvelope,
        now: datetime,
        requested_effort: str | None,
        hermes_config: dict | None,
        task_definition: str,
    ) -> tuple[ExecutableRuntime, str]:
        runtime = current.get(target.runtime.stable_id())
        reasons, effort = self._target_eligibility(
            runtime,
            target=target,
            profile=None,
            assessment=assessment,
            policy=policy,
            now=now,
            requested_effort=requested_effort,
            hermes_config=hermes_config,
            task_definition=task_definition,
        )
        if reasons or runtime is None or effort is None:
            detail = ", ".join(reasons) or "runtime_not_in_inventory"
            raise ValueError(f"safe default is unavailable: {detail}")
        return runtime, effort

    def _target_eligibility(
        self,
        runtime: ExecutableRuntime | None,
        *,
        target: RoutingTarget,
        profile: RouteProfile | None,
        assessment: TaskAssessment,
        policy: PolicyEnvelope,
        now: datetime,
        requested_effort: str | None,
        hermes_config: dict | None,
        task_definition: str,
    ) -> tuple[tuple[CandidateReasonCode, ...], str | None]:
        if runtime is None:
            return ("runtime_not_in_inventory",), None
        state_reason = _runtime_state_reason(runtime)
        if state_reason is not None:
            return (state_reason,), None
        if not _verification_is_current(runtime, now):
            return ("runtime_verification_expired",), None
        if (
            runtime.key.provider.strip().casefold() == "moa"
            or runtime.capabilities.get("is_moa") is True
        ):
            return ("moa_excluded",), None

        raw_reasons = list(
            runtime_policy_rejection_reasons(
                runtime,
                policy=policy,
                catalog=self.catalog,
            )
        )
        # Missing catalog evidence is a conservative scoring prior, not proof
        # that a hard latency ceiling was crossed.
        raw_reasons = [
            reason
            for reason in raw_reasons
            if reason
            not in {
                "estimated_cost_unknown",
                "estimated_cost_exceeds_limit",
                "estimated_latency_unknown",
                "estimated_latency_exceeds_limit",
            }
        ]
        minimum_context = max(
            policy.minimum_context_tokens,
            assessment.expected_context_tokens,
            profile.limits.minimum_context_tokens
            if profile is not None
            and profile.limits is not None
            and profile.limits.minimum_context_tokens is not None
            else 0,
        )
        raw_reasons.extend(
            runtime_capability_rejection_reasons(
                runtime,
                required_capabilities=assessment.required_capabilities,
                required_modalities=assessment.required_modalities,
                minimum_context_tokens=minimum_context,
                minimum_output_tokens=assessment.expected_output_tokens,
            )
        )
        raw_reasons.extend(
            self._effective_limit_reasons(
                runtime,
                target=target,
                profile_limits=profile.limits if profile is not None else None,
                assessment=assessment,
                policy=policy,
                task_definition=task_definition,
            )
        )
        if _cooldown_is_active(runtime, now):
            raw_reasons.append("runtime_throttled")

        effort_reason, effort = _resolve_reasoning_effort(
            runtime,
            target=target,
            profile_limits=profile.limits if profile is not None else None,
            policy=policy,
            requested_effort=requested_effort,
            hermes_config=hermes_config,
        )
        if effort_reason is not None:
            raw_reasons.append(effort_reason)
        reasons = _finite_reason_codes(raw_reasons)
        return reasons, None if reasons else effort

    def _effective_limit_reasons(
        self,
        runtime: ExecutableRuntime,
        *,
        target: RoutingTarget,
        profile_limits: ProfileLimits | None,
        assessment: TaskAssessment,
        policy: PolicyEnvelope,
        task_definition: str,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        cost_limit = min(
            value
            for value in (
                policy.max_estimated_task_cost_usd,
                target.max_estimated_task_cost_usd,
                profile_limits.max_estimated_task_cost_usd
                if profile_limits is not None
                else None,
            )
            if value is not None
        )
        estimated_cost = _estimated_cost(runtime, assessment)
        if estimated_cost is None:
            reasons.append("estimated_cost_unknown")
        elif estimated_cost > cost_limit:
            reasons.append("estimated_cost_exceeds_limit")

        latency_limit = min(
            value
            for value in (
                policy.max_estimated_latency_seconds,
                target.max_estimated_latency_seconds,
                profile_limits.max_estimated_latency_seconds
                if profile_limits is not None
                else None,
            )
            if value is not None
        )
        latency_rows = _current_metric_rows(
            self.catalog,
            runtime,
            "latency",
            domains=assessment.domains,
            task_definition=task_definition,
        )
        if latency_rows and max(row.value for row in latency_rows) > latency_limit:
            reasons.append("estimated_latency_exceeds_limit")

        allowed_licenses = (
            profile_limits.allowed_licenses
            if profile_limits is not None
            and profile_limits.allowed_licenses is not None
            else None
        )
        if runtime.key.local_backend and allowed_licenses is not None and (
            runtime.capabilities.get("license_id") not in allowed_licenses
        ):
            reasons.append("license_not_allowed")
        return tuple(reasons)

    def _score_target(
        self,
        runtime: ExecutableRuntime,
        *,
        target: RoutingTarget,
        profile: RouteProfile,
        assessment: TaskAssessment,
        policy: PolicyEnvelope,
        complexity_bands: ComplexityBands,
        normalized_base_rank: float,
        task_definition: str,
    ) -> tuple[tuple[tuple[str, float], ...], float]:
        rows = _current_rows(
            self.catalog,
            runtime,
            domains=assessment.domains,
            task_definition=task_definition,
        )
        quality = _conservative_catalog_component(rows, "quality")
        reliability = _conservative_catalog_component(rows, "reliability")
        latency_rows = tuple(row for row in rows if row.metric_name == "latency")
        estimated_latency = (
            max(row.value for row in latency_rows) if latency_rows else None
        )
        latency_limit = min(
            value
            for value in (
                policy.max_estimated_latency_seconds,
                target.max_estimated_latency_seconds,
                profile.limits.max_estimated_latency_seconds
                if profile.limits is not None
                else None,
            )
            if value is not None
        )
        normalized_latency = (
            1.0
            if estimated_latency is None
            else normalize_against_limit(estimated_latency, latency_limit)
        )
        estimated_cost = _estimated_cost(runtime, assessment)
        cost_limit = min(
            value
            for value in (
                policy.max_estimated_task_cost_usd,
                target.max_estimated_task_cost_usd,
                profile.limits.max_estimated_task_cost_usd
                if profile.limits is not None
                else None,
            )
            if value is not None
        )
        normalized_cost = (
            1.0
            if estimated_cost is None
            else normalize_against_limit(estimated_cost, cost_limit)
        )
        uncertainty_penalty = (
            quality.uncertainty
            + reliability.uncertainty
            + (1.0 - assessment.confidence) * 0.1
        )
        staleness_penalty = float(
            self.catalog.staleness_penalty(runtime, evidence=rows)
        ) + float(self.catalog.economics_staleness_penalty(runtime))
        affinity = profile_affinity(
            assessment=assessment,
            match=profile.match,
            complexity_bands=complexity_bands,
        )
        affinity_adjustment = affinity * PROFILE_AFFINITY_COEFFICIENT
        base_rank_adjustment = (
            normalized_base_rank * BASE_RANK_PRIOR_COEFFICIENT
        )
        values = (
            quality.value,
            reliability.value,
            normalized_latency,
            normalized_cost,
            uncertainty_penalty,
            staleness_penalty,
            affinity,
            affinity_adjustment,
            normalized_base_rank,
            base_rank_adjustment,
        )
        components = tuple(zip(SELECTOR_SCORE_COMPONENTS, values, strict=True))
        score = utility_score(
            objectives=profile.objectives,
            quality=quality.value,
            reliability=reliability.value,
            normalized_latency=normalized_latency,
            normalized_cost=normalized_cost,
            uncertainty_penalty=uncertainty_penalty,
            staleness_penalty=staleness_penalty,
        ) + affinity_adjustment + base_rank_adjustment
        if not math.isfinite(score):  # pragma: no cover - guarded by components
            raise ValueError("selector score must be finite")
        return components, score


def _runtime_state_reason(
    runtime: ExecutableRuntime,
) -> CandidateReasonCode | None:
    if runtime.state == "verified":
        return None
    if runtime.state == "configured_unverified":
        return "runtime_not_verified"
    return "runtime_unavailable"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _verification_is_current(runtime: ExecutableRuntime, now: datetime) -> bool:
    verified_at = _parse_time(runtime.verified_at)
    expires_at = _parse_time(runtime.verification_expires_at)
    return (
        runtime.verification_source is not None
        and verified_at is not None
        and expires_at is not None
        and verified_at <= now < expires_at
    )


def _cooldown_is_active(runtime: ExecutableRuntime, now: datetime) -> bool:
    until = _parse_time(runtime.economics.cooldown_until)
    return until is not None and now < until


def _finite_reason_codes(
    reasons: list[str] | tuple[str, ...],
) -> tuple[CandidateReasonCode, ...]:
    finite: list[CandidateReasonCode] = []
    for reason in reasons:
        canonical = str(reason).partition(":")[0]
        if canonical not in _FINITE_REASONS:
            raise ValueError(f"unsupported selector rejection reason: {canonical}")
        if canonical not in finite:
            finite.append(canonical)  # type: ignore[arg-type]
    return tuple(finite)


def _resolve_reasoning_effort(
    runtime: ExecutableRuntime,
    *,
    target: RoutingTarget,
    profile_limits: ProfileLimits | None,
    policy: PolicyEnvelope,
    requested_effort: str | None,
    hermes_config: dict | None,
) -> tuple[str | None, str | None]:
    support = runtime.reasoning_support
    if not support.exact:
        return "reasoning_unsupported", None
    supported = set(support.efforts) & set(target.supported_reasoning_efforts)
    minimum = _EFFORT_POSITION[target.reasoning.minimum]
    maximum = min(
        _EFFORT_POSITION[target.reasoning.maximum],
        _EFFORT_POSITION[policy.max_reasoning_effort],
        _EFFORT_POSITION[
            profile_limits.max_reasoning_effort
            if profile_limits is not None
            and profile_limits.max_reasoning_effort is not None
            else policy.max_reasoning_effort
        ],
    )
    allowed = tuple(
        effort
        for effort in REASONING_EFFORT_ORDER
        if effort in supported and minimum <= _EFFORT_POSITION[effort] <= maximum
    )
    if not allowed:
        return "reasoning_out_of_bounds", None
    generic_config = resolve_reasoning_config(
        hermes_config,
        runtime.key.model,
        requested_effort=requested_effort,
    )
    desired = effective_generic_reasoning_effort(generic_config)
    if desired not in _EFFORT_POSITION:
        desired = target.reasoning.default
    desired_position = _EFFORT_POSITION[desired]
    if desired_position <= _EFFORT_POSITION[allowed[0]]:
        return None, allowed[0]
    if desired_position >= _EFFORT_POSITION[allowed[-1]]:
        return None, allowed[-1]
    return None, min(
        allowed,
        key=lambda effort: (
            abs(_EFFORT_POSITION[effort] - desired_position),
            _EFFORT_POSITION[effort],
        ),
    )


def _estimated_cost(
    runtime: ExecutableRuntime,
    assessment: TaskAssessment,
) -> float | None:
    economics = runtime.economics
    estimates: list[float] = []
    if economics.billing_kind in {"metered", "subscription"}:
        estimates.extend(
            value
            for value in (
                economics.effective_marginal_cost_usd_per_task,
                economics.effective_amortized_cost_usd_per_task,
            )
            if value is not None
        )
        if (
            economics.billing_kind == "metered"
            and economics.metered_input_usd_per_million_tokens is not None
            and economics.metered_output_usd_per_million_tokens is not None
        ):
            estimates.append(
                assessment.expected_context_tokens
                * economics.metered_input_usd_per_million_tokens
                + assessment.expected_output_tokens
                * economics.metered_output_usd_per_million_tokens
            )
            estimates[-1] /= 1_000_000
        return max(estimates) if estimates else None
    values = (
        economics.local_compute_cost_usd_per_task,
        economics.local_energy_cost_usd_per_task,
    )
    known = tuple(value for value in values if value is not None)
    return sum(known) if known else None


def _current_rows(
    catalog: object,
    runtime: ExecutableRuntime,
    *,
    domains: tuple[str, ...],
    task_definition: str,
) -> tuple[object, ...]:
    allowed_domains = set(domains)
    return tuple(
        row
        for row in catalog.evidence_for(runtime)
        if not catalog.evidence_is_expired(row)
        and row.domain in allowed_domains
        and row.task_definition == task_definition
    )


def _current_metric_rows(
    catalog: object,
    runtime: ExecutableRuntime,
    metric: str,
    *,
    domains: tuple[str, ...],
    task_definition: str,
) -> tuple[object, ...]:
    return tuple(
        row
        for row in _current_rows(
            catalog,
            runtime,
            domains=domains,
            task_definition=task_definition,
        )
        if row.metric_name == metric
    )


def _conservative_catalog_component(rows: tuple[object, ...], metric: str):
    matching = tuple(row for row in rows if row.metric_name == metric)
    if not matching:
        return conservative_metric(None)
    estimates = tuple(
        conservative_metric(
            normalize_catalog_metric(
                value=row.value,
                direction=row.metric_direction,
                scale=row.metric_scale,
                normalization_method=row.normalization_method,
            ),
            confidence=row.confidence,
            sample_size=row.sample_size,
        )
        for row in matching
    )
    return min(estimates, key=lambda item: (item.value, -item.uncertainty))


def _normalized_base_ranks(
    profiles: tuple[RouteProfile, ...],
) -> dict[str, float]:
    explicit = {
        profile.profile_id: float(profile.base_rank)
        for profile in profiles
        if profile.base_rank is not None
    }
    normalized = {profile.profile_id: 0.0 for profile in profiles}
    if not explicit:
        return normalized
    if len(explicit) == 1:
        normalized[next(iter(explicit))] = 1.0
        return normalized
    minimum = min(explicit.values())
    maximum = max(explicit.values())
    if math.isclose(minimum, maximum, rel_tol=0.0, abs_tol=0.0):
        return normalized
    normalized.update(
        {
            profile_id: (maximum - rank) / (maximum - minimum)
            for profile_id, rank in explicit.items()
        }
    )
    return normalized


def _active_profile_targets(
    evaluations: list[_Evaluation],
) -> tuple[_Evaluation, ...]:
    active: dict[str, _Evaluation] = {}
    for item in evaluations:
        if (
            item.candidate.eligible
            and item.candidate.final_score is not None
            and item.runtime is not None
            and item.profile.profile_id not in active
        ):
            active[item.profile.profile_id] = item
    return tuple(active.values())


def _select_target(
    targets: tuple[_Evaluation, ...],
    *,
    pinned_profile_id: str | None,
    preferred_profile_id: str | None,
) -> _Evaluation | None:
    if not targets:
        return None
    for requested in (pinned_profile_id, preferred_profile_id):
        if requested is None:
            continue
        selected = next(
            (item for item in targets if item.profile.profile_id == requested),
            None,
        )
        if selected is not None:
            return selected
    return min(
        targets,
        key=lambda item: (
            -float(item.candidate.final_score),
            -dict(item.candidate.normalized_scoring_inputs)["normalized_base_rank"],
            item.profile.profile_id,
            item.candidate.runtime_id,
        ),
    )


def _current_target(
    target: RoutingTarget,
    runtime: ExecutableRuntime | None,
    effort: str | None,
) -> RoutingTarget:
    if runtime is None:  # pragma: no cover - caller requires eligibility
        return target
    reasoning = target.reasoning
    if effort is not None:
        reasoning = reasoning.model_copy(update={"default": effort})
    return target.model_copy(update={"runtime": runtime.key, "reasoning": reasoning})


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _rejection_summary(
    evaluations: list[_Evaluation],
) -> dict[str, tuple[CandidateReasonCode, ...]]:
    eligible_runtime_ids = {
        item.candidate.runtime_id
        for item in evaluations
        if item.candidate.eligible
    }
    collected: dict[str, list[CandidateReasonCode]] = {}
    for item in evaluations:
        if not item.reasons or item.candidate.runtime_id in eligible_runtime_ids:
            continue
        values = collected.setdefault(item.candidate.runtime_id, [])
        for reason in item.reasons:
            if reason not in values:
                values.append(reason)
    return {runtime_id: tuple(reasons) for runtime_id, reasons in collected.items()}


__all__ = [
    "BASE_RANK_PRIOR_COEFFICIENT",
    "PROFILE_AFFINITY_COEFFICIENT",
    "SelectionResult",
    "StaticSelector",
    "profile_affinity",
]
