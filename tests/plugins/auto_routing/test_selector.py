"""Deterministic static-routing selector and decision contracts."""

from __future__ import annotations

import inspect
import json
import random
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from agent.reasoning_support import ReasoningSupport
from plugins.auto_routing.auto_routing.inventory import (
    ExecutableRuntime,
    InventorySnapshot,
    ReasonCodes,
)
from plugins.auto_routing.auto_routing.models import (
    AccessEconomics,
    CatalogEvidence,
    ComplexityBands,
    LocalModelRequirements,
    ObjectiveWeights,
    PolicyEnvelope,
    ProfileLimits,
    ProfileMatch,
    ReasoningBounds,
    RouteProfile,
    RoutingTarget,
    RuntimeKey,
    TaskAssessment,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
OBSERVED_AT = "2026-07-16T11:00:00Z"
EXPIRES_AT = "2026-07-17T11:00:00Z"


class _Catalog:
    def __init__(self, metrics: dict[str, dict[str, float]] | None = None) -> None:
        self.metrics = metrics or {}

    def evidence_for(self, runtime: ExecutableRuntime) -> tuple[CatalogEvidence, ...]:
        values = self.metrics.get(runtime.key.stable_id(), {})
        return tuple(
            CatalogEvidence(
                source_id="test-catalog",
                source_url="https://example.invalid/catalog",
                retrieved_at=OBSERVED_AT,
                published_at=OBSERVED_AT,
                expires_at=EXPIRES_AT,
                model=runtime.key.model,
                model_version="test-version",
                domain="coding",
                task_definition="default",
                metric_name=name,
                metric_direction=(
                    "lower_is_better"
                    if name in {"latency", "cost"}
                    else "higher_is_better"
                ),
                metric_scale=(
                    "unit_interval"
                    if name in {"quality", "reliability"}
                    else "absolute"
                ),
                value=value,
                sample_size=100,
                confidence=1.0,
                normalization_method=(
                    "identity"
                    if name in {"quality", "reliability"}
                    else "policy_limit"
                ),
            )
            for name, value in sorted(values.items())
        )

    def evidence_is_expired(self, _row: CatalogEvidence) -> bool:
        return False

    def current_time(self) -> datetime:
        return NOW

    def staleness_penalty(self, *_args: Any, **_kwargs: Any) -> float:
        return 0.0

    def economics_is_stale(self, _runtime: ExecutableRuntime) -> bool:
        return False

    def economics_staleness_penalty(self, _runtime: ExecutableRuntime) -> float:
        return 0.0


def _economics(*, cost: float = 0.01, throttle: str = "available") -> AccessEconomics:
    return AccessEconomics(
        billing_kind="metered",
        metered_input_usd_per_million_tokens=1.0,
        metered_output_usd_per_million_tokens=2.0,
        effective_marginal_cost_usd_per_task=cost,
        throttle_state=throttle,
        source_id="test-economics",
        evidence_ttl_seconds=3600,
        provenance="test",
        confidence=1.0,
        observed_at=OBSERVED_AT,
    )


def _runtime(
    name: str,
    *,
    provider: str = "test-provider",
    state: str = "verified",
    expires_at: str | None = EXPIRES_AT,
    efforts: tuple[str, ...] = ("low", "medium", "high"),
    exact_reasoning: bool = True,
    capabilities: dict[str, Any] | None = None,
    cost: float = 0.01,
    local: bool = False,
) -> ExecutableRuntime:
    facts = {
        "supports_tools": True,
        "input_modalities": ("text", "image", "document"),
        "context_window": 32_768,
        "max_output_tokens": 8_192,
        "license_id": "apache-2.0",
        "open_weights": True,
        "hardware_compatible": True,
    }
    facts.update(capabilities or {})
    key = RuntimeKey(
        provider="ollama" if local else provider,
        model=name,
        auth_identity="local:ollama" if local else "api_key:test",
        api_mode="chat_completions",
        local_backend="ollama" if local else "",
        inventory_revision="inventory-1",
    )
    economics = (
        AccessEconomics(
            billing_kind="local",
            local_energy_cost_usd_per_task=cost,
            local_compute_cost_usd_per_task=0.0,
            throttle_state="available",
            source_id="test-local-economics",
            evidence_ttl_seconds=3600,
            provenance="test",
            confidence=1.0,
            observed_at=OBSERVED_AT,
        )
        if local
        else _economics(cost=cost)
    )
    return ExecutableRuntime(
        key=key,
        resolver_name=f"{key.provider}:test",
        state=state,
        reasons=ReasonCodes(()) if state == "verified" else ReasonCodes((state,)),
        economics=economics,
        reasoning_support=ReasoningSupport(
            efforts=efforts,
            provider_aliases=(),
            provenance="test",
            exact=exact_reasoning,
        ),
        verification_source="authenticated_live" if state == "verified" else None,
        verified_at=OBSERVED_AT if state == "verified" else None,
        verification_expires_at=expires_at,
        provenance=("test-inventory",),
        observed_at=OBSERVED_AT,
        capabilities=facts,
    )


def _target(
    runtime: ExecutableRuntime,
    *,
    default: str = "high",
    minimum: str = "low",
    maximum: str = "high",
    status: str = "active",
) -> RoutingTarget:
    return RoutingTarget(
        runtime=runtime.key,
        reasoning=ReasoningBounds(
            default=default,
            min=minimum,
            max=maximum,
        ),
        supported_reasoning_efforts=tuple(runtime.reasoning_support.efforts),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=30.0,
        revision_status=status,
    )


def _profile(
    profile_id: str,
    primary: ExecutableRuntime,
    *,
    primary_challengers: tuple[ExecutableRuntime, ...] = (),
    fallbacks: tuple[ExecutableRuntime, ...] = (),
    base_rank: float = 0.0,
    match: ProfileMatch | None = None,
    limits: ProfileLimits | None = None,
) -> RouteProfile:
    return RouteProfile(
        profile_id=profile_id,
        description=f"{profile_id} profile",
        base_rank=base_rank,
        match=match
        or ProfileMatch(
            domains=("coding",),
            complexity=("hard", "extreme"),
            modalities=("text",),
            capabilities=("tools",),
        ),
        objectives=ObjectiveWeights(
            quality=0.5,
            reliability=0.3,
            latency=0.1,
            cost=0.1,
        ),
        limits=limits,
        primary=_target(primary),
        primary_challengers=tuple(
            _target(runtime, status="challenger")
            for runtime in primary_challengers
        ),
        fallbacks=tuple(
            _target(runtime, status="fallback") for runtime in fallbacks
        ),
        provenance=("user",),
    )


def _policy(**updates: Any) -> PolicyEnvelope:
    values: dict[str, Any] = {
        "eligible_sources": ("configured_providers", "installed_local_models"),
        "uninstalled_local_models": "deny",
        "local_models": LocalModelRequirements(
            require_open_weights=True,
            require_compatible_hardware=True,
        ),
        "denied_providers": (),
        "denied_models": (),
        "max_estimated_task_cost_usd": 2.0,
        "max_estimated_latency_seconds": 60.0,
        "max_routing_overhead_usd_per_day": 1.0,
        "max_experiment_cost_usd_per_day": 1.0,
        "max_evaluator_calls_per_day": 0,
        "max_canary_fraction": 0.0,
        "max_reasoning_effort": "high",
        "allow_subscription": True,
        "allow_paid_access_probes": False,
        "allowed_licenses": ("apache-2.0",),
        "minimum_context_tokens": 1_024,
        "canary_high_risk_tasks": False,
    }
    values.update(updates)
    return PolicyEnvelope(**values)


def _assessment(**updates: Any) -> TaskAssessment:
    values: dict[str, Any] = {
        "complexity": 0.8,
        "domains": ("coding",),
        "required_capabilities": ("tools",),
        "required_modalities": ("text",),
        "expected_context_tokens": 4_096,
        "expected_output_tokens": 1_024,
        "quality_sensitivity": 0.8,
        "reliability_sensitivity": 0.7,
        "latency_sensitivity": 0.2,
        "cost_sensitivity": 0.3,
        "risk_class": "moderate",
        "confidence": 0.9,
    }
    values.update(updates)
    return TaskAssessment(**values)


def _snapshot(*runtimes: ExecutableRuntime) -> InventorySnapshot:
    return InventorySnapshot(
        revision="inventory-1",
        runtimes=list(runtimes),
        observed_at=OBSERVED_AT,
    )


def _selector(catalog: _Catalog | None = None):
    from plugins.auto_routing.auto_routing.selector import StaticSelector

    return StaticSelector(catalog=catalog or _Catalog(), now=lambda: NOW)


def _select(
    *,
    profiles: tuple[RouteProfile, ...],
    inventory: InventorySnapshot,
    safe_default: RoutingTarget,
    catalog: _Catalog | None = None,
    assessment: TaskAssessment | None = None,
    policy: PolicyEnvelope | None = None,
    **kwargs: Any,
):
    return _selector(catalog).select(
        profiles=profiles,
        assessment=assessment or _assessment(),
        inventory=inventory,
        policy=policy or _policy(),
        complexity_bands=ComplexityBands(),
        safe_default=safe_default,
        **kwargs,
    )


def test_selector_has_no_storage_evidence_or_learner_imports() -> None:
    from plugins.auto_routing.auto_routing import selector

    source = inspect.getsource(selector)

    assert "from .storage import" not in source
    assert "from .evidence import" not in source
    assert "from .learner import" not in source


def test_profile_affinity_averages_only_nonempty_match_dimensions() -> None:
    from plugins.auto_routing.auto_routing.selector import (
        PROFILE_AFFINITY_COEFFICIENT,
        profile_affinity,
    )

    affinity = profile_affinity(
        assessment=_assessment(),
        match=ProfileMatch(
            domains=("coding",),
            complexity=("hard",),
            modalities=(),
            capabilities=("web_search",),
        ),
        complexity_bands=ComplexityBands(),
    )

    assert PROFILE_AFFINITY_COEFFICIENT == pytest.approx(0.15)
    assert affinity == pytest.approx(2 / 3)


def test_hard_filters_run_before_scoring_and_explain_every_target() -> None:
    from plugins.auto_routing.auto_routing.scoring import utility_score

    primary = _runtime("primary")
    unverified = _runtime("unverified", state="configured_unverified")
    missing_tools = _runtime("missing-tools", capabilities={"supports_tools": False})
    safe = _runtime("safe")
    result = _select(
        profiles=(_profile("coding", primary, fallbacks=(unverified, missing_tools)),),
        inventory=_snapshot(primary, unverified, missing_tools, safe),
        safe_default=_target(safe),
    )

    assert result.selected_runtime.key.stable_id() == primary.key.stable_id()
    assert result.score_calls == (primary.key.stable_id(),)
    assert result.rejections[unverified.key.stable_id()] == ("runtime_not_verified",)
    assert result.rejections[missing_tools.key.stable_id()] == (
        "required_capability_unsupported",
    )
    assert len(result.candidates) == 4  # profile targets plus safe default
    selected_candidate = next(
        candidate
        for candidate in result.candidates
        if candidate.profile_id == "coding" and candidate.target_role == "primary"
    )
    assert tuple(name for name, _value in selected_candidate.normalized_scoring_inputs) == (
        "quality",
        "reliability",
        "normalized_latency",
        "normalized_cost",
        "uncertainty_penalty",
        "staleness_penalty",
        "profile_affinity",
        "profile_affinity_adjustment",
        "normalized_base_rank",
        "base_rank_adjustment",
    )
    components = dict(selected_candidate.normalized_scoring_inputs)
    base_utility = utility_score(
        objectives=_profile("coding", primary).objectives,
        quality=components["quality"],
        reliability=components["reliability"],
        normalized_latency=components["normalized_latency"],
        normalized_cost=components["normalized_cost"],
        uncertainty_penalty=components["uncertainty_penalty"],
        staleness_penalty=components["staleness_penalty"],
    )
    assert components["profile_affinity_adjustment"] == pytest.approx(
        components["profile_affinity"] * 0.15
    )
    assert selected_candidate.final_score == pytest.approx(
        base_utility
        + components["profile_affinity_adjustment"]
        + components["base_rank_adjustment"]
    )


@pytest.mark.parametrize(
    ("runtime", "policy", "reason"),
    [
        (
            _runtime("missing", state="temporarily_unavailable"),
            _policy(),
            "runtime_unavailable",
        ),
        (
            _runtime("expired", expires_at="2026-07-15T00:00:00Z"),
            _policy(),
            "runtime_verification_expired",
        ),
        (
            _runtime("moa", provider="moa"),
            _policy(),
            "moa_excluded",
        ),
        (
            _runtime("denied", provider="forbidden"),
            _policy(denied_providers=("forbidden",)),
            "provider_denied_by_policy",
        ),
        (
            _runtime("small", capabilities={"context_window": 512}),
            _policy(),
            "context_capacity_insufficient",
        ),
        (
            _runtime("inexact", exact_reasoning=False),
            _policy(),
            "reasoning_unsupported",
        ),
    ],
)
def test_current_inventory_policy_capacity_and_reasoning_are_hard_gates(
    runtime: ExecutableRuntime,
    policy: PolicyEnvelope,
    reason: str,
) -> None:
    good = _runtime("good")
    safe = _runtime("safe")
    result = _select(
        profiles=(
            _profile("bad", runtime),
            _profile("good", good),
        ),
        inventory=_snapshot(runtime, good, safe),
        safe_default=_target(safe),
        policy=policy,
    )

    assert result.selected_profile_id == "good"
    assert reason in result.rejections[runtime.key.stable_id()]
    assert runtime.key.stable_id() not in result.score_calls


def test_target_absent_from_current_inventory_is_rejected_before_scoring() -> None:
    absent = _runtime("absent")
    good = _runtime("good")
    safe = _runtime("safe")
    result = _select(
        profiles=(_profile("absent", absent), _profile("good", good)),
        inventory=_snapshot(good, safe),
        safe_default=_target(safe),
    )
    assert result.rejections[absent.key.stable_id()] == ("runtime_not_in_inventory",)


def test_duplicate_current_inventory_runtime_ids_fail_closed() -> None:
    primary = _runtime("primary")
    safe = _runtime("safe")
    duplicate = replace(primary, observed_at="2026-07-16T11:30:00Z")
    with pytest.raises(ValueError, match="duplicate runtime"):
        _select(
            profiles=(_profile("coding", primary),),
            inventory=_snapshot(primary, duplicate, safe),
            safe_default=_target(safe),
        )


def test_no_supported_reasoning_effort_inside_all_bounds_is_rejected() -> None:
    unsupported = _runtime("unsupported", efforts=("minimal",))
    good = _runtime("good")
    safe = _runtime("safe")
    result = _select(
        profiles=(_profile("unsupported", unsupported), _profile("good", good)),
        inventory=_snapshot(unsupported, good, safe),
        safe_default=_target(safe),
    )
    assert result.rejections[unsupported.key.stable_id()] == (
        "reasoning_out_of_bounds",
    )


def test_profile_limits_tighten_policy_before_scoring() -> None:
    too_costly = _runtime("too-costly", cost=0.5)
    good = _runtime("good", cost=0.01)
    safe = _runtime("safe")
    strict = _profile(
        "strict",
        too_costly,
        limits=ProfileLimits(max_estimated_task_cost_usd=0.1),
    )
    result = _select(
        profiles=(strict, _profile("good", good)),
        inventory=_snapshot(too_costly, good, safe),
        safe_default=_target(safe),
    )
    assert result.selected_profile_id == "good"
    assert result.rejections[too_costly.key.stable_id()] == (
        "estimated_cost_exceeds_limit",
    )


def test_profile_latency_license_and_context_limits_are_hard_gates() -> None:
    safe = _runtime("safe")
    for label, bad, limits, metrics, expected_reason in (
        (
            "latency",
            _runtime("slow"),
            ProfileLimits(max_estimated_latency_seconds=5.0),
            {"latency": 10.0},
            "estimated_latency_exceeds_limit",
        ),
        (
            "license",
            _runtime(
                "licensed",
                capabilities={"license_id": "proprietary"},
                local=True,
            ),
            ProfileLimits(allowed_licenses=("apache-2.0",)),
            {},
            "license_not_allowed",
        ),
        (
            "context",
            _runtime("small-context", capabilities={"context_window": 2_048}),
            ProfileLimits(minimum_context_tokens=4_096),
            {},
            "context_capacity_insufficient",
        ),
    ):
        good = _runtime(f"good-{label}")
        catalog = _Catalog({bad.key.stable_id(): metrics})
        result = _select(
            profiles=(
                _profile("strict", bad, limits=limits),
                _profile("good", good),
            ),
            inventory=_snapshot(bad, good, safe),
            safe_default=_target(safe),
            catalog=catalog,
        )
        rejected = next(
            candidate
            for candidate in result.candidates
            if candidate.profile_id == "strict"
        )
        assert rejected.reason_codes == (expected_reason,)
        assert rejected.normalized_scoring_inputs == ()
        assert rejected.final_score is None


def test_same_runtime_in_two_profiles_has_distinct_target_level_outcomes() -> None:
    shared = _runtime("shared", cost=0.5)
    safe = _runtime("safe")
    result = _select(
        profiles=(
            _profile(
                "strict",
                shared,
                limits=ProfileLimits(max_estimated_task_cost_usd=0.1),
            ),
            _profile(
                "loose",
                shared,
                limits=ProfileLimits(max_estimated_task_cost_usd=0.6),
            ),
        ),
        inventory=_snapshot(shared, safe),
        safe_default=_target(safe),
    )
    shared_candidates = tuple(
        candidate
        for candidate in result.candidates
        if candidate.runtime_id == shared.key.stable_id()
    )
    assert len(shared_candidates) == 2
    assert len({candidate.candidate_id for candidate in shared_candidates}) == 2
    loose_candidate, strict_candidate = sorted(
        shared_candidates, key=lambda item: item.profile_id
    )
    assert loose_candidate.profile_id == "loose" and loose_candidate.eligible is True
    assert strict_candidate.profile_id == "strict" and strict_candidate.eligible is False
    assert strict_candidate.reason_codes == ("estimated_cost_exceeds_limit",)
    assert strict_candidate.normalized_scoring_inputs == ()
    assert strict_candidate.final_score is None
    assert result.eligible_runtime_ids == (shared.key.stable_id(),)
    assert shared.key.stable_id() not in result.rejections


def test_reasoning_resolution_honors_model_override_before_plugin_request() -> None:
    primary = _runtime("primary", efforts=("low", "medium", "high"))
    safe = _runtime("safe")
    result = _select(
        profiles=(_profile("coding", primary),),
        inventory=_snapshot(primary, safe),
        safe_default=_target(safe),
        requested_reasoning_effort="high",
        hermes_config={
            "agent": {
                "reasoning_effort": "high",
                "reasoning_overrides": {"primary": "low"},
            }
        },
    )
    assert result.selected_reasoning_effort == "low"


def test_reasoning_resolution_uses_global_when_plugin_request_is_omitted() -> None:
    primary = _runtime("primary", efforts=("low", "medium", "high"))
    safe = _runtime("safe")
    result = _select(
        profiles=(_profile("coding", primary),),
        inventory=_snapshot(primary, safe),
        safe_default=_target(safe),
        hermes_config={"agent": {"reasoning_effort": "low"}},
    )
    assert result.selected_reasoning_effort == "low"


def test_reasoning_is_clamped_to_target_profile_global_and_exact_support() -> None:
    primary = _runtime("primary", efforts=("low", "medium"))
    safe = _runtime("safe")
    profile = _profile(
        "coding",
        primary,
        limits=ProfileLimits(max_reasoning_effort="medium"),
    )
    result = _select(
        profiles=(profile,),
        inventory=_snapshot(primary, safe),
        safe_default=_target(safe),
        requested_reasoning_effort="high",
        policy=_policy(max_reasoning_effort="high"),
    )
    assert result.selected_reasoning_effort == "medium"


def test_unavailable_first_primary_allows_second_primary_selection() -> None:
    unavailable = _runtime("unavailable-primary", state="temporarily_unavailable")
    second_primary = _runtime("second-primary")
    safe = _runtime("safe")

    result = _select(
        profiles=(
            _profile(
                "coding",
                unavailable,
                primary_challengers=(second_primary,),
            ),
        ),
        inventory=_snapshot(unavailable, second_primary, safe),
        safe_default=_target(safe),
    )

    primary_candidates = [
        candidate
        for candidate in result.candidates
        if candidate.profile_id == "coding" and candidate.target_role == "primary"
    ]
    assert [candidate.target_ordinal for candidate in primary_candidates] == [0, 1]
    assert primary_candidates[0].reason_codes == ("runtime_unavailable",)
    assert result.selected_runtime.key.stable_id() == second_primary.key.stable_id()
    assert result.selection_reason == "highest_eligible_score"


def test_second_primary_honors_manual_generic_reasoning_override() -> None:
    unavailable = _runtime("unavailable-primary", state="temporarily_unavailable")
    second_primary = _runtime("second-primary", efforts=("low", "medium", "high"))
    safe = _runtime("safe")

    result = _select(
        profiles=(
            _profile(
                "coding",
                unavailable,
                primary_challengers=(second_primary,),
            ),
        ),
        inventory=_snapshot(unavailable, second_primary, safe),
        safe_default=_target(safe),
        requested_reasoning_effort="high",
        hermes_config={
            "agent": {
                "reasoning_effort": "high",
                "reasoning_overrides": {"second-primary": "low"},
            }
        },
    )

    assert result.selected_runtime.key.stable_id() == second_primary.key.stable_id()
    assert result.selected_reasoning_effort == "low"


def test_profile_fallback_chain_is_exact_ordered_and_never_appends_global() -> None:
    from plugins.auto_routing.auto_routing.selector import StaticSelector

    assert "global_fallbacks" not in inspect.signature(StaticSelector.select).parameters
    primary = _runtime("primary")
    first = _runtime("first")
    rejected = _runtime("rejected", state="temporarily_unavailable")
    second = _runtime("second")
    forbidden_global = _runtime("forbidden-global")
    safe = _runtime("safe")
    result = _select(
        profiles=(
            _profile("coding", primary, fallbacks=(first, rejected, second)),
        ),
        inventory=_snapshot(primary, first, rejected, second, forbidden_global, safe),
        safe_default=_target(safe),
    )
    assert [item.runtime.model for item in result.fallbacks] == ["first", "second"]


def test_fallback_chain_records_each_clamped_effective_reasoning_effort() -> None:
    primary = _runtime("primary")
    fallback = _runtime("fallback", efforts=("low", "medium"))
    safe = _runtime("safe")
    result = _select(
        profiles=(_profile("coding", primary, fallbacks=(fallback,)),),
        inventory=_snapshot(primary, fallback, safe),
        safe_default=_target(safe),
        requested_reasoning_effort="high",
    )
    assert result.fallbacks[0].reasoning.default == "medium"


def test_every_eligible_profile_target_is_scored_after_hard_filters() -> None:
    primary = _runtime("primary")
    fallback = _runtime("fallback")
    safe = _runtime("safe")
    result = _select(
        profiles=(_profile("coding", primary, fallbacks=(fallback,)),),
        inventory=_snapshot(primary, fallback, safe),
        safe_default=_target(safe),
    )
    assert result.score_calls == result.eligible_runtime_ids
    fallback_candidate = next(
        candidate
        for candidate in result.candidates
        if candidate.target_role == "fallback"
    )
    assert fallback_candidate.normalized_scoring_inputs
    assert fallback_candidate.final_score is not None


def test_first_eligible_fallback_is_promoted_before_safe_default() -> None:
    primary = _runtime("primary", state="temporarily_unavailable")
    promoted = _runtime("promoted")
    remaining = _runtime("remaining")
    safe = _runtime("safe")
    result = _select(
        profiles=(_profile("coding", primary, fallbacks=(promoted, remaining)),),
        inventory=_snapshot(primary, promoted, remaining, safe),
        safe_default=_target(safe),
    )
    assert result.selected_profile_id == "coding"
    assert result.selected_runtime.key.stable_id() == promoted.key.stable_id()
    assert result.selection_reason == "pre_call_fallback"
    assert [target.runtime.model for target in result.fallbacks] == ["remaining"]


def test_unrelated_catalog_domains_cannot_change_task_score() -> None:
    better = _runtime("better")
    worse = _runtime("worse")
    safe = _runtime("safe")

    class MixedDomainCatalog(_Catalog):
        def evidence_for(self, runtime: ExecutableRuntime) -> tuple[CatalogEvidence, ...]:
            relevant = 0.9 if runtime.key.model == "better" else 0.5
            values = (
                ("coding", "quality", relevant),
                ("unrelated", "quality", 0.0),
                ("unrelated", "latency", 999.0),
            )
            return tuple(
                CatalogEvidence(
                    source_id=f"{runtime.key.model}-{domain}-{metric}",
                    source_url="https://example.invalid/catalog",
                    retrieved_at=OBSERVED_AT,
                    published_at=OBSERVED_AT,
                    expires_at=EXPIRES_AT,
                    model=runtime.key.model,
                    model_version="test-version",
                    domain=domain,
                    task_definition="default",
                    metric_name=metric,
                    metric_direction=(
                        "lower_is_better" if metric == "latency" else "higher_is_better"
                    ),
                    metric_scale=(
                        "absolute" if metric == "latency" else "unit_interval"
                    ),
                    value=value,
                    sample_size=100,
                    confidence=1.0,
                    normalization_method=(
                        "policy_limit" if metric == "latency" else "identity"
                    ),
                )
                for domain, metric, value in values
            )

    result = _select(
        profiles=(_profile("better", better), _profile("worse", worse)),
        inventory=_snapshot(better, worse, safe),
        safe_default=_target(safe),
        catalog=MixedDomainCatalog(),
        task_definition="default",
    )
    assert result.selected_profile_id == "better"


def test_metered_token_estimate_cannot_be_understated_by_effective_cost() -> None:
    runtime = _runtime("expensive")
    runtime = replace(
        runtime,
        economics=runtime.economics.model_copy(
            update={
                "metered_input_usd_per_million_tokens": 100_000.0,
                "metered_output_usd_per_million_tokens": 100_000.0,
                "effective_marginal_cost_usd_per_task": 0.01,
            }
        ),
    )
    good = _runtime("good")
    safe = _runtime("safe")
    result = _select(
        profiles=(
            _profile(
                "expensive",
                runtime,
                limits=ProfileLimits(max_estimated_task_cost_usd=0.1),
            ),
            _profile("good", good),
        ),
        inventory=_snapshot(runtime, good, safe),
        safe_default=_target(safe),
    )
    assert result.rejections[runtime.key.stable_id()] == (
        "estimated_cost_exceeds_limit",
    )


def test_metered_token_prices_are_sufficient_when_effective_cost_is_missing() -> None:
    runtime = _runtime("metered")
    runtime = replace(
        runtime,
        economics=runtime.economics.model_copy(
            update={"effective_marginal_cost_usd_per_task": None}
        ),
    )
    safe = _runtime("safe")
    result = _select(
        profiles=(_profile("metered", runtime),),
        inventory=_snapshot(runtime, safe),
        safe_default=_target(safe),
    )
    assert result.selected_runtime.key.stable_id() == runtime.key.stable_id()


def test_base_rank_is_only_a_late_prior_and_cannot_override_clear_utility() -> None:
    better = _runtime("better")
    worse = _runtime("worse")
    safe = _runtime("safe")
    catalog = _Catalog(
        {
            better.key.stable_id(): {"quality": 1.0, "reliability": 1.0},
            worse.key.stable_id(): {"quality": 0.0, "reliability": 0.0},
        }
    )
    result = _select(
        profiles=(
            # Lower rank numbers are the stronger prior. Deliberately put the
            # stronger prior on the worse-utility profile.
            _profile("better", better, base_rank=1_000_000),
            _profile("worse", worse, base_rank=-1_000_000),
        ),
        inventory=_snapshot(better, worse, safe),
        safe_default=_target(safe),
        catalog=catalog,
    )
    assert result.selected_profile_id == "better"


def test_equal_utility_uses_lower_base_rank_then_lexical_profile_id() -> None:
    from plugins.auto_routing.auto_routing.selector import (
        BASE_RANK_PRIOR_COEFFICIENT,
    )

    assert BASE_RANK_PRIOR_COEFFICIENT == pytest.approx(0.01)
    first = _runtime("first")
    second = _runtime("second")
    safe = _runtime("safe")
    result = _select(
        profiles=(
            _profile("z-profile", first, base_rank=10.0),
            _profile("a-profile", second, base_rank=1.0),
        ),
        inventory=_snapshot(first, second, safe),
        safe_default=_target(safe),
    )
    assert result.selected_profile_id == "a-profile"
    by_profile = {
        candidate.profile_id: candidate for candidate in result.candidates
    }
    preferred_inputs = dict(by_profile["a-profile"].normalized_scoring_inputs)
    other_inputs = dict(by_profile["z-profile"].normalized_scoring_inputs)
    assert preferred_inputs["normalized_base_rank"] == pytest.approx(1.0)
    assert preferred_inputs["base_rank_adjustment"] == pytest.approx(
        BASE_RANK_PRIOR_COEFFICIENT
    )
    assert other_inputs["normalized_base_rank"] == pytest.approx(0.0)
    assert other_inputs["base_rank_adjustment"] == pytest.approx(0.0)
    assert by_profile["a-profile"].final_score == pytest.approx(
        by_profile["z-profile"].final_score + BASE_RANK_PRIOR_COEFFICIENT
    )

    lexical = _select(
        profiles=(
            _profile("z-profile", first, base_rank=1.0),
            _profile("a-profile", second, base_rank=1.0),
        ),
        inventory=_snapshot(first, second, safe),
        safe_default=_target(safe),
    )
    assert lexical.selected_profile_id == "a-profile"


def test_omitted_base_rank_is_neutral_not_stronger_than_explicit_prior() -> None:
    omitted = _runtime("omitted")
    explicit = _runtime("explicit")
    safe = _runtime("safe")
    omitted_profile = _profile("a-omitted", omitted).model_copy(
        update={"base_rank": None}
    )
    result = _select(
        profiles=(omitted_profile, _profile("z-explicit", explicit, base_rank=70.0)),
        inventory=_snapshot(omitted, explicit, safe),
        safe_default=_target(safe),
    )
    assert result.selected_profile_id == "z-explicit"


def test_explicit_pin_selects_only_an_eligible_profile() -> None:
    higher = _runtime("higher")
    pinned = _runtime("pinned")
    safe = _runtime("safe")
    catalog = _Catalog(
        {
            higher.key.stable_id(): {"quality": 1.0, "reliability": 1.0},
            pinned.key.stable_id(): {"quality": 0.2, "reliability": 0.2},
        }
    )
    result = _select(
        profiles=(_profile("higher", higher), _profile("pinned", pinned)),
        inventory=_snapshot(higher, pinned, safe),
        safe_default=_target(safe),
        catalog=catalog,
        pinned_profile_id="pinned",
    )
    assert result.selected_profile_id == "pinned"
    assert result.selection_reason == "pinned_profile"


def test_ineligible_pin_cannot_bypass_hard_gates() -> None:
    pinned = _runtime("pinned", state="temporarily_unavailable")
    good = _runtime("good")
    safe = _runtime("safe")
    result = _select(
        profiles=(_profile("pinned", pinned), _profile("good", good)),
        inventory=_snapshot(pinned, good, safe),
        safe_default=_target(safe),
        pinned_profile_id="pinned",
    )
    assert result.selected_profile_id == "good"
    assert result.selected_runtime.key.stable_id() == good.key.stable_id()


def test_fixed_inputs_produce_identical_semantic_record() -> None:
    primary = _runtime("primary")
    safe = _runtime("safe")
    kwargs = {
        "profiles": (_profile("coding", primary),),
        "inventory": _snapshot(primary, safe),
        "safe_default": _target(safe),
    }
    assert _select(**kwargs).semantic_record() == _select(**kwargs).semantic_record()


def test_safe_default_must_be_a_complete_current_eligible_runtime() -> None:
    primary = _runtime("primary")
    missing_safe = _runtime("missing-safe")
    with pytest.raises(ValueError, match="safe default"):
        _select(
            profiles=(_profile("coding", primary),),
            inventory=_snapshot(primary),
            safe_default=_target(missing_safe),
        )


@pytest.mark.parametrize(
    ("safe", "policy"),
    [
        (
            _runtime("safe-unverified", state="temporarily_unavailable"),
            _policy(),
        ),
        (
            _runtime("safe-expired", expires_at="2026-07-15T00:00:00Z"),
            _policy(),
        ),
        (
            _runtime("safe-denied", provider="forbidden"),
            _policy(denied_providers=("forbidden",)),
        ),
        (_runtime("safe-inexact", exact_reasoning=False), _policy()),
    ],
)
def test_safe_default_rejects_unverified_expired_policy_or_reasoning_failures(
    safe: ExecutableRuntime,
    policy: PolicyEnvelope,
) -> None:
    primary = _runtime("primary")
    with pytest.raises(ValueError, match="safe default"):
        _select(
            profiles=(_profile("coding", primary),),
            inventory=_snapshot(primary, safe),
            safe_default=_target(safe),
            policy=policy,
        )


def test_resolved_inherited_safe_default_uses_current_full_runtime_snapshot() -> None:
    primary = _runtime("primary", state="temporarily_unavailable")
    current_safe = _runtime("safe")
    inherited_key = RuntimeKey(
        **{
            **current_safe.key.model_dump(),
            "inventory_revision": "baseline-inherited-revision",
        }
    )
    inherited_target = _target(current_safe).model_copy(
        update={"runtime": inherited_key}
    )
    result = _select(
        profiles=(_profile("coding", primary),),
        inventory=_snapshot(primary, current_safe),
        safe_default=inherited_target,
    )
    assert result.selected_profile_id is None
    assert result.selection_reason == "safe_default"
    assert result.safe_default_reason == "no_eligible_runtime"
    assert result.selected_runtime == current_safe
    assert result.safe_default_runtime == current_safe


def test_shared_eligibility_helpers_are_the_advisor_helpers() -> None:
    from plugins.auto_routing.auto_routing import advisor, eligibility

    assert (
        advisor.runtime_capability_rejection_reasons
        is eligibility.runtime_capability_rejection_reasons
    )
    assert (
        advisor.runtime_policy_rejection_reasons
        is eligibility.runtime_policy_rejection_reasons
    )


def test_seeded_candidate_fuzz_never_selects_ineligible() -> None:
    rng = random.Random(240716)
    safe = _runtime("safe")
    for case in range(500):
        candidates: list[ExecutableRuntime] = []
        profiles: list[RouteProfile] = []
        inventory_rows: list[ExecutableRuntime] = []
        independently_eligible: set[str] = set()
        for index in range(3):
            present = rng.random() > 0.15
            verified = rng.random() > 0.25
            expiry = rng.choice((EXPIRES_AT, "2026-07-15T00:00:00Z", "invalid"))
            provider = "moa" if rng.random() < 0.1 else "test-provider"
            supports_tools = rng.random() > 0.2
            supports_text = rng.random() > 0.1
            context_window = 32_768 if rng.random() > 0.15 else 512
            max_output = 8_192 if rng.random() > 0.15 else 128
            exact_reasoning = rng.random() > 0.1
            cost = 0.01 if rng.random() > 0.2 else 0.5
            profile_cost_limit = 0.1 if rng.random() > 0.2 else 1.0
            candidate = _runtime(
                f"model-{case}-{index}",
                provider=provider,
                state="verified" if verified else "temporarily_unavailable",
                expires_at=expiry,
                exact_reasoning=exact_reasoning,
                capabilities={
                    "supports_tools": supports_tools,
                    "input_modalities": ("text",) if supports_text else ("image",),
                    "context_window": context_window,
                    "max_output_tokens": max_output,
                },
                cost=cost,
            )
            candidates.append(candidate)
            profiles.append(
                _profile(
                    f"profile-{index}",
                    candidate,
                    limits=ProfileLimits(
                        max_estimated_task_cost_usd=profile_cost_limit
                    ),
                )
            )
            if present:
                inventory_rows.append(candidate)
            if (
                present
                and verified
                and expiry == EXPIRES_AT
                and provider != "moa"
                and supports_tools
                and supports_text
                and context_window >= 4_096
                and max_output >= 1_024
                and exact_reasoning
                and cost <= profile_cost_limit
            ):
                independently_eligible.add(candidate.key.stable_id())
        result = _select(
            profiles=tuple(profiles),
            inventory=_snapshot(*inventory_rows, safe),
            safe_default=_target(safe),
        )
        selected_id = result.selected_runtime.key.stable_id()
        if independently_eligible:
            assert result.selected_profile_id is not None
            assert selected_id in independently_eligible
        else:
            assert result.selected_profile_id is None
            assert selected_id == safe.key.stable_id()


def test_decision_builder_separates_semantic_checksum_from_event_identity() -> None:
    from plugins.auto_routing.auto_routing.decisions import DecisionBuilder

    primary = _runtime(
        "primary",
        capabilities={"debug_note": "RAW_TASK_AND_SECRET_SENTINEL"},
    )
    safe = _runtime("safe")
    selection = _select(
        profiles=(_profile("coding", primary),),
        inventory=_snapshot(primary, safe),
        safe_default=_target(safe),
    )
    ids = iter(("decision-a", "decision-b"))
    times = iter(("2026-07-16T12:00:00Z", "2026-07-16T12:01:00Z"))
    builder = DecisionBuilder(id_factory=lambda: next(ids), clock=lambda: next(times))
    common = {
        "scope": "fresh_session",
        "session_id": "session-1",
        "task_id": "task-1",
        "operation_id": None,
        "task_index": None,
        "selection": selection,
        "task_facts_hash": "a" * 64,
        "inventory_revision": "inventory-1",
        "catalog_revision": "catalog-1",
        "authority_revision": "authority-1",
        "policy_revision": "policy-1",
        "adaptive_revision": "adaptive-1",
        "projection_mode": "shadow",
        "routing_latency_seconds": 0.01,
    }
    first = builder.build(**common)
    second = builder.build(**common)
    assert first.semantic_checksum == second.semantic_checksum
    assert first.decision.decision_id != second.decision.decision_id
    assert first.decision.created_at != second.decision.created_at

    changed_revision = DecisionBuilder(
        id_factory=lambda: "decision-c",
        clock=lambda: "2026-07-16T12:02:00Z",
    ).build(**{**common, "authority_revision": "authority-2"})
    assert changed_revision.semantic_checksum != first.semantic_checksum

    changed_facts = DecisionBuilder(
        id_factory=lambda: "decision-d",
        clock=lambda: "2026-07-16T12:03:00Z",
    ).build(**{**common, "task_facts_hash": "b" * 64})
    assert changed_facts.semantic_checksum != first.semantic_checksum

    changed_assessment = replace(
        selection,
        assessment=_assessment(complexity=0.2),
    )
    changed_assessment_build = DecisionBuilder(
        id_factory=lambda: "decision-e",
        clock=lambda: "2026-07-16T12:04:00Z",
    ).build(**{**common, "selection": changed_assessment})
    assert changed_assessment_build.semantic_checksum != first.semantic_checksum

    other = _runtime("other")
    changed_selection = _select(
        profiles=(_profile("coding", other),),
        inventory=_snapshot(other, safe),
        safe_default=_target(safe),
    )
    changed_candidate_build = DecisionBuilder(
        id_factory=lambda: "decision-f",
        clock=lambda: "2026-07-16T12:05:00Z",
    ).build(**{**common, "selection": changed_selection})
    assert changed_candidate_build.semantic_checksum != first.semantic_checksum

    changed_event_only = DecisionBuilder(
        id_factory=lambda: "decision-g",
        clock=lambda: "2026-07-16T12:06:00Z",
    ).build(
        **{
            **common,
            "session_id": "session-2",
            "task_id": "task-2",
            "routing_latency_seconds": 99.0,
        }
    )
    assert changed_event_only.semantic_checksum == first.semantic_checksum

    serialized = json.dumps(
        {
            "decision": first.decision.model_dump(mode="json"),
            "candidates": [
                candidate.model_dump(mode="json") for candidate in first.candidates
            ],
            "semantic": selection.semantic_record(),
        },
        sort_keys=True,
    )
    assert "RAW_TASK_AND_SECRET_SENTINEL" not in serialized
    assert "api_key_value" not in serialized


def test_decision_builder_api_and_json_cannot_carry_task_or_secrets() -> None:
    from plugins.auto_routing.auto_routing.decisions import DecisionBuilder

    signature = inspect.signature(DecisionBuilder.build)
    assert "task" not in signature.parameters
    assert "prompt" not in signature.parameters
    assert "api_key" not in signature.parameters


def test_decision_builder_keeps_selected_profile_score_for_shared_runtime() -> None:
    from plugins.auto_routing.auto_routing.decisions import DecisionBuilder
    from plugins.auto_routing.auto_routing.storage import (
        _validate_decision_candidate_coherence,
    )

    shared = _runtime("shared")
    safe = _runtime("safe")
    preferred = _profile("preferred", shared)
    lower_affinity = _profile(
        "lower-affinity",
        shared,
        match=ProfileMatch(
            domains=("unrelated",),
            complexity=(),
            modalities=(),
            capabilities=(),
        ),
    )
    selection = _select(
        profiles=(preferred, lower_affinity),
        inventory=_snapshot(shared, safe),
        safe_default=_target(safe),
        pinned_profile_id="lower-affinity",
    )
    built = DecisionBuilder(
        id_factory=lambda: "decision-shared",
        clock=lambda: "2026-07-16T12:00:00Z",
    ).build(
        scope="fresh_session",
        session_id="session-1",
        task_id="task-1",
        operation_id=None,
        task_index=None,
        selection=selection,
        task_facts_hash="a" * 64,
        inventory_revision="inventory-1",
        catalog_revision="catalog-1",
        authority_revision="authority-1",
        policy_revision="policy-1",
        adaptive_revision="adaptive-1",
        projection_mode="shadow",
        routing_latency_seconds=0.01,
    )

    _validate_decision_candidate_coherence(built.decision, built.candidates)


def test_decision_builder_chooses_one_exact_target_rejection_per_runtime() -> None:
    from plugins.auto_routing.auto_routing.decisions import DecisionBuilder
    from plugins.auto_routing.auto_routing.storage import (
        _validate_decision_candidate_coherence,
    )

    rejected = _runtime("rejected", cost=0.5)
    good = _runtime("good")
    safe = _runtime("safe")
    selection = _select(
        profiles=(
            _profile(
                "cost-strict",
                rejected,
                limits=ProfileLimits(max_estimated_task_cost_usd=0.1),
            ),
            _profile(
                "context-strict",
                rejected,
                limits=ProfileLimits(minimum_context_tokens=65_536),
            ),
            _profile("good", good),
        ),
        inventory=_snapshot(rejected, good, safe),
        safe_default=_target(safe),
    )
    built = DecisionBuilder(
        id_factory=lambda: "decision-rejected",
        clock=lambda: "2026-07-16T12:00:00Z",
    ).build(
        scope="fresh_session",
        session_id="session-1",
        task_id="task-1",
        operation_id=None,
        task_index=None,
        selection=selection,
        task_facts_hash="a" * 64,
        inventory_revision="inventory-1",
        catalog_revision="catalog-1",
        authority_revision="authority-1",
        policy_revision="policy-1",
        adaptive_revision="adaptive-1",
        projection_mode="shadow",
        routing_latency_seconds=0.01,
    )

    _validate_decision_candidate_coherence(built.decision, built.candidates)


def test_semantic_checksum_changes_with_effective_fallback_reasoning() -> None:
    from plugins.auto_routing.auto_routing.decisions import DecisionBuilder

    primary = _runtime("primary", efforts=("medium",))
    fallback = _runtime("fallback", efforts=("low", "high"))
    safe = _runtime("safe", efforts=("medium",))
    common_selection = {
        "profiles": (_profile("coding", primary, fallbacks=(fallback,)),),
        "inventory": _snapshot(primary, fallback, safe),
        "safe_default": _target(safe),
    }
    low = _select(**common_selection, requested_reasoning_effort="low")
    high = _select(**common_selection, requested_reasoning_effort="high")
    assert low.selected_reasoning_effort == high.selected_reasoning_effort == "medium"
    assert low.safe_default_reasoning_effort == high.safe_default_reasoning_effort
    assert low.fallbacks[0].reasoning.default == "low"
    assert high.fallbacks[0].reasoning.default == "high"

    def build(selection, decision_id: str):
        return DecisionBuilder(
            id_factory=lambda: decision_id,
            clock=lambda: "2026-07-16T12:00:00Z",
        ).build(
            scope="fresh_session",
            session_id="session-1",
            task_id="task-1",
            operation_id=None,
            task_index=None,
            selection=selection,
            task_facts_hash="a" * 64,
            inventory_revision="inventory-1",
            catalog_revision="catalog-1",
            authority_revision="authority-1",
            policy_revision="policy-1",
            adaptive_revision="adaptive-1",
            projection_mode="shadow",
            routing_latency_seconds=0.01,
        )

    assert build(low, "decision-low").semantic_checksum != build(
        high,
        "decision-high",
    ).semantic_checksum
