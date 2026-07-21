"""Immutable domain and authority-boundary contracts for auto routing."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from plugins.auto_routing.auto_routing import models as models_module
from plugins.auto_routing.auto_routing.config import (
    ConfigError,
    authority_document,
    authority_revision,
    config_document,
    config_revision,
    parse_config,
)
from plugins.auto_routing.auto_routing.models import (
    AccessEconomics,
    AdaptiveRevision,
    AutoRoutingConfig,
    CatalogEvidence,
    ComplexityBands,
    PolicyEnvelope,
    ProfileAdaptationSettings,
    RouteProfile,
    RoutingDecision,
    RoutingRule,
    RoutingTarget,
    RoutingVocabulary,
    RuntimeKey,
    RuntimeObservation,
    TaskFacts,
    TaskAssessment,
)
from plugins.auto_routing.auto_routing.storage import ActivationReceipt, RoutingStore


_VALID_ROOT: dict[str, Any] = {
    "plugins": {
        "entries": {
            "auto-routing": {
                "llm": {
                    "allow_provider_override": True,
                    "allowed_providers": ["openai-codex"],
                    "allow_model_override": True,
                    "allowed_models": ["gpt-5.4-mini"],
                },
                "activation": {"mode": "shadow"},
                "scopes": {
                    "fresh_sessions": True,
                    "delegation": True,
                },
                "classifier": {
                    "provider": "openai-codex",
                    "model": "gpt-5.4-mini",
                    "reasoning_effort": "low",
                    "timeout_seconds": 15,
                    "disclosure": "full",
                },
                "safe_default": "inherit",
                "policy": {
                    "eligible_sources": [
                        "configured_providers",
                        "installed_local_models",
                    ],
                    "uninstalled_local_models": "deny",
                    "local_models": {
                        "require_open_weights": True,
                        "require_compatible_hardware": True,
                    },
                    "denied_providers": [],
                    "denied_models": [],
                    "max_estimated_task_cost_usd": 2.0,
                    "max_estimated_latency_seconds": 120.0,
                    "max_routing_overhead_usd_per_day": 1.0,
                    "max_experiment_cost_usd_per_day": 2.0,
                    "max_evaluator_calls_per_day": 20,
                    "max_canary_fraction": 0.05,
                    "max_reasoning_effort": "high",
                    "allow_subscription": True,
                    "allow_paid_access_probes": False,
                    "allowed_licenses": [],
                    "minimum_context_tokens": 0,
                    "canary_high_risk_tasks": False,
                },
                "adaptation": {
                    "enabled": True,
                    "mode": "autonomous",
                    "canary_fraction": 0.05,
                    "minimum_canary_samples": 20,
                    "rollback_threshold": 0.10,
                },
                "profiles": {
                    "coding": {
                        "profile_id": "coding",
                        "description": "Tool-using software development tasks",
                        "base_rank": 70,
                        "match": {
                            "domains": ["coding", "debugging"],
                            "complexity": ["moderate", "hard", "extreme"],
                            "modalities": ["text"],
                            "capabilities": ["tools"],
                        },
                        "objectives": {
                            "quality": 0.55,
                            "reliability": 0.25,
                            "latency": 0.10,
                            "cost": 0.10,
                        },
                        "primary": {
                            "runtime": {
                                "provider": "openai-codex",
                                "model": "gpt-5.4",
                                "auth_identity": "subscription:default",
                                "credential_pool_identity": "pool:codex",
                                "endpoint_identity": "endpoint:codex",
                                "api_mode": "codex_responses",
                                "local_backend": "",
                                "inventory_revision": "inventory-1",
                            },
                            "reasoning": {
                                "default": "medium",
                                "min": "low",
                                "max": "high",
                            },
                            "supported_reasoning_efforts": [],
                            "revision_status": "active",
                        },
                        "fallbacks": [],
                        "provenance": [],
                    }
                },
                "economics_overrides": {},
            }
        }
    }
}


@pytest.fixture
def valid_root() -> dict[str, Any]:
    """Return an independent full minimal authority mapping for each test."""
    return copy.deepcopy(_VALID_ROOT)


def _plugin(root: dict[str, Any]) -> dict[str, Any]:
    return root["plugins"]["entries"]["auto-routing"]


def _distinct_target(
    template: dict[str, Any],
    identity: str,
    *,
    revision_status: str,
) -> dict[str, Any]:
    target = copy.deepcopy(template)
    target["runtime"]["model"] = f"model-{identity}"
    target["revision_status"] = revision_status
    return target


def test_task2_public_domain_types_are_exported() -> None:
    required = {
        "AuthorityLabel",
        "BoundedLabels",
        "BoundedTokenCount",
        "CandidateReasonCode",
        "CanonicalTimestamp",
        "ComplexityBands",
        "DegradationReasonCode",
        "DurableIdentifier",
        "FiniteFloat",
        "MAX_CLASSIFIER_IMAGE_BYTES",
        "MAX_CLASSIFIER_IMAGE_COUNT",
        "MAX_CLASSIFIER_INPUT_TOKENS",
        "MAX_CLASSIFIER_OUTPUT_TOKENS",
        "MAX_DECISION_CANDIDATES",
        "MAX_REASON_CODES",
        "MAX_SCORE_COMPONENTS",
        "MAX_TASK_INDEX",
        "RoutingDecision",
        "RoutingRule",
        "RoutingVocabulary",
        "RuleAssessmentOverrides",
        "RulePredicate",
        "SafeDefaultReasonCode",
        "ScoreComponent",
        "SelectionReasonCode",
        "TaskFacts",
    }

    assert required <= set(models_module.__all__)


def test_profile_mapping_key_must_match_embedded_profile_id(valid_root) -> None:
    profiles = _plugin(valid_root)["profiles"]
    profiles["renamed-coding"] = profiles.pop("coding")

    with pytest.raises(ConfigError, match="profile mapping key.*profile_id"):
        parse_config(valid_root)


def test_profile_mapping_rejects_duplicate_effective_profile_ids(valid_root) -> None:
    profiles = _plugin(valid_root)["profiles"]
    profiles["coding-copy"] = copy.deepcopy(profiles["coding"])

    with pytest.raises(ConfigError, match="duplicate effective profile_id"):
        parse_config(valid_root)


def test_profile_identity_is_exact_across_config_rules_and_decisions(
    valid_root,
) -> None:
    plugin = _plugin(valid_root)
    profile = plugin["profiles"].pop("coding")
    profile["profile_id"] = "Coding Profile"
    plugin["profiles"]["Coding Profile"] = profile
    plugin["rules"] = [
        {
            "rule_id": "coding-profile",
            "priority": 1,
            "profile_id": "Coding Profile",
            "effect": "pin_profile",
            "when": {"domains_any": ["coding"]},
        }
    ]

    parsed = parse_config(valid_root)

    assert tuple(parsed.profiles) == ("Coding Profile",)
    assert parsed.rules[0].profile_id == "Coding Profile"

    decision_document = _decision_document(copy.deepcopy(_VALID_ROOT))
    decision_document["selected_profile_id"] = "Coding Profile"
    decision_document["final_scores"] = [
        [decision_document["eligible_candidates"][0], 0.5]
    ]
    decision = RoutingDecision.model_validate(decision_document)
    assert decision.selected_profile_id == "Coding Profile"


@pytest.mark.parametrize(("length", "accepted"), [(256, True), (257, False)])
def test_profile_identity_has_one_durable_boundary(
    valid_root,
    length,
    accepted,
) -> None:
    plugin = _plugin(valid_root)
    profile = plugin["profiles"].pop("coding")
    profile_id = "p" * length
    profile["profile_id"] = profile_id
    plugin["profiles"][profile_id] = profile

    if accepted:
        assert tuple(parse_config(valid_root).profiles) == (profile_id,)
    else:
        with pytest.raises(ConfigError, match="profile"):
            parse_config(valid_root)


def test_route_profile_fallback_count_matches_decision_projection_boundary(
    valid_root,
) -> None:
    profile = _plugin(valid_root)["profiles"]["coding"]
    profile["fallbacks"] = [
        _distinct_target(
            profile["primary"],
            f"fallback-{index}",
            revision_status="fallback",
        )
        for index in range(64)
    ]

    assert len(parse_config(valid_root).profiles["coding"].fallbacks) == 64

    profile["fallbacks"].append(
        _distinct_target(
            profile["primary"],
            "fallback-over-limit",
            revision_status="fallback",
        )
    )
    with pytest.raises(ConfigError, match="fallback"):
        parse_config(valid_root)


def test_total_profile_targets_match_candidate_bundle_boundary(valid_root) -> None:
    plugin = _plugin(valid_root)
    base_profile = plugin["profiles"]["coding"]
    profiles: dict[str, Any] = {}
    for index in range(16):
        profile = copy.deepcopy(base_profile)
        profile_id = f"profile-{index}"
        profile["profile_id"] = profile_id
        fallback_count = 64 if index < 15 else 48
        profile["fallbacks"] = [
            _distinct_target(
                profile["primary"],
                f"{index}-fallback-{fallback_index}",
                revision_status="fallback",
            )
            for fallback_index in range(fallback_count)
        ]
        profiles[profile_id] = profile
    plugin["profiles"] = profiles

    parsed = parse_config(valid_root)
    assert (
        sum(1 + len(profile.fallbacks) for profile in parsed.profiles.values()) == 1024
    )

    profiles["profile-15"]["fallbacks"].append(
        _distinct_target(
            profiles["profile-15"]["primary"],
            "15-fallback-over-limit",
            revision_status="fallback",
        )
    )
    with pytest.raises(ConfigError, match="profile targets|candidate"):
        parse_config(valid_root)


def test_subscription_permission_is_closed_durable_authority(valid_root) -> None:
    plugin = _plugin(valid_root)
    plugin["profiles"]["coding"]["primary"]["runtime"]["auth_identity"] = (
        "api-key:default"
    )
    policy = plugin["policy"]
    policy["allow_subscription"] = False

    denied = parse_config(valid_root)
    denied_document = denied.model_dump(mode="json", by_alias=True)
    denied_revision = authority_revision(denied)

    assert denied.policy.allow_subscription is False
    assert denied_document["policy"]["allow_subscription"] is False

    policy["allow_subscription"] = True
    allowed = parse_config(valid_root)
    assert allowed.policy.allow_subscription is True
    assert authority_revision(allowed) != denied_revision


def test_subscription_policy_rejects_explicit_subscription_access_target(
    valid_root,
) -> None:
    _plugin(valid_root)["policy"]["allow_subscription"] = False

    with pytest.raises(ConfigError, match="allow_subscription"):
        parse_config(valid_root)


def _access_economics(
    billing_kind: str = "subscription",
    **updates: Any,
) -> AccessEconomics:
    values: dict[str, Any] = {
        "billing_kind": billing_kind,
        "source_id": "user-configured",
        "provenance": "config_override",
        "observed_at": "2026-07-15T12:00:00Z",
    }
    values.update(updates)
    return AccessEconomics(**values)


def _task_assessment() -> TaskAssessment:
    return TaskAssessment(
        complexity=0.78,
        domains=("coding", "debugging"),
        required_capabilities=("tools", "long_context"),
        required_modalities=("text",),
        expected_context_tokens=32_000,
        expected_output_tokens=4_000,
        quality_sensitivity=0.9,
        reliability_sensitivity=0.8,
        latency_sensitivity=0.3,
        cost_sensitivity=0.2,
        risk_class="moderate",
        confidence=0.74,
    )


def test_profile_requires_complete_objectives_and_verified_target(valid_root):
    parsed = parse_config(valid_root)

    assert parsed.profiles["coding"].objectives.model_dump() == {
        "quality": 0.55,
        "reliability": 0.25,
        "latency": 0.10,
        "cost": 0.10,
    }
    assert parsed.profiles["coding"].primary.reasoning.minimum == "low"
    assert parsed.profiles["coding"].primary.runtime.auth_identity == (
        "subscription:default"
    )
    assert parsed.profiles["coding"].primary.revision_status == "active"


@pytest.mark.parametrize("missing", ["quality", "reliability", "latency", "cost"])
def test_missing_objective_is_invalid(valid_root, missing):
    del _plugin(valid_root)["profiles"]["coding"]["objectives"][missing]

    with pytest.raises(ConfigError, match=missing):
        parse_config(valid_root)


def test_objective_weights_are_complete_finite_non_negative_and_normalized(valid_root):
    _plugin(valid_root)["profiles"]["coding"]["objectives"] = {
        "quality": 55,
        "reliability": 25,
        "latency": 10,
        "cost": 10,
    }

    objectives = parse_config(valid_root).profiles["coding"].objectives

    assert objectives.model_dump() == {
        "quality": 0.55,
        "reliability": 0.25,
        "latency": 0.10,
        "cost": 0.10,
    }


@pytest.mark.parametrize(
    "objectives",
    [
        {"quality": -1, "reliability": 1, "latency": 1, "cost": 1},
        {"quality": float("inf"), "reliability": 1, "latency": 1, "cost": 1},
        {"quality": 0, "reliability": 0, "latency": 0, "cost": 0},
    ],
)
def test_invalid_objective_values_are_rejected(valid_root, objectives):
    _plugin(valid_root)["profiles"]["coding"]["objectives"] = objectives

    with pytest.raises(
        ConfigError,
        match="finite, non-negative, and sum above zero",
    ):
        parse_config(valid_root)


def test_non_numeric_objective_is_reported_as_config_error(valid_root):
    _plugin(valid_root)["profiles"]["coding"]["objectives"]["quality"] = None

    with pytest.raises(ConfigError, match="objective weights"):
        parse_config(valid_root)


def test_authority_hash_ignores_inventory_derived_target_state(valid_root):
    parsed = parse_config(valid_root)
    coding = parsed.profiles["coding"]
    observed_primary = coding.primary.model_copy(
        update={"supported_reasoning_efforts": ("low", "medium", "high")}
    )
    with_observation = parsed.model_copy(
        update={
            "profiles": {
                **parsed.profiles,
                "coding": coding.model_copy(update={"primary": observed_primary}),
            }
        }
    )

    first = authority_revision(parsed)
    second = authority_revision(with_observation)

    assert first == second
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def test_authority_hash_ignores_other_observational_profile_state(valid_root):
    parsed = parse_config(valid_root)
    coding = parsed.profiles["coding"]
    observed_runtime = coding.primary.runtime.model_copy(
        update={"inventory_revision": "inventory-2"}
    )
    observed_primary = coding.primary.model_copy(update={"runtime": observed_runtime})
    observed_profile = coding.model_copy(
        update={
            "primary": observed_primary,
            "provenance": ("catalog-revision-2",),
        }
    )
    with_observations = parsed.model_copy(
        update={"profiles": {"coding": observed_profile}}
    )

    assert authority_revision(parsed) == authority_revision(with_observations)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plugin: plugin["policy"].update({"denied_models": ["vendor/forbidden"]}),
        lambda plugin: plugin["llm"].update({
            "allowed_models": ["gpt-5.4-mini", "gpt-5.4"]
        }),
        lambda plugin: plugin["profiles"]["coding"].update({
            "description": "A different user-approved profile description"
        }),
        lambda plugin: plugin["profiles"]["coding"]["primary"]["runtime"].update({
            "auth_identity": "api-key:work"
        }),
        lambda plugin: plugin["profiles"]["coding"]["primary"]["reasoning"].update({
            "default": "high"
        }),
    ],
)
def test_authority_hash_includes_every_user_owned_authority_field(
    valid_root,
    mutate: Callable[[dict[str, Any]], None],
):
    baseline = authority_revision(parse_config(valid_root))
    mutate(_plugin(valid_root))

    assert authority_revision(parse_config(valid_root)) != baseline


def test_authority_hash_includes_user_economics_overrides(valid_root):
    parsed = parse_config(valid_root)
    runtime_id = parsed.profiles["coding"].primary.runtime.stable_id()
    _plugin(valid_root)["economics_overrides"][runtime_id] = {
        "billing_kind": "subscription",
        "effective_marginal_cost_usd_per_task": 0.02,
        "source_id": "user-configured",
        "provenance": "config_override",
        "observed_at": "2026-07-15T12:00:00Z",
    }

    assert authority_revision(parse_config(valid_root)) != authority_revision(parsed)


def test_runtime_key_distinguishes_pool_and_local_backend_paths() -> None:
    base = RuntimeKey(
        provider="custom",
        model="qwen3:14b",
        auth_identity="configured:work",
        credential_pool_identity="pool:work",
        endpoint_identity="endpoint:local",
        api_mode="chat_completions",
        local_backend="ollama",
        inventory_revision="inv-1",
    )

    assert (
        base.stable_id()
        != base.model_copy(
            update={"credential_pool_identity": "pool:personal"}
        ).stable_id()
    )
    assert (
        base.stable_id()
        != base.model_copy(update={"local_backend": "lmstudio"}).stable_id()
    )
    assert (
        base.stable_id()
        == base.model_copy(update={"inventory_revision": "inv-2"}).stable_id()
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("auth_identity", "subscription:personal"),
        ("credential_pool_identity", "pool:personal"),
        ("endpoint_identity", "endpoint:remote"),
        ("api_mode", "responses"),
        ("local_backend", "lmstudio"),
    ],
)
def test_runtime_stable_id_includes_every_access_path_dimension(field, replacement):
    base = RuntimeKey(
        provider="custom",
        model="qwen3:14b",
        auth_identity="configured:work",
        credential_pool_identity="pool:work",
        endpoint_identity="endpoint:local",
        api_mode="chat_completions",
        local_backend="ollama",
        inventory_revision="inv-1",
    )

    assert base.stable_id() != base.model_copy(update={field: replacement}).stable_id()


def test_access_economics_preserves_unknown_numbers_as_none_and_explicit_zero():
    unknown = _access_economics()
    free_marginal = _access_economics(
        effective_marginal_cost_usd_per_task=0.0,
    )

    assert unknown.metered_input_usd_per_million_tokens is None
    assert unknown.metered_output_usd_per_million_tokens is None
    assert unknown.effective_marginal_cost_usd_per_task is None
    assert unknown.effective_amortized_cost_usd_per_task is None
    assert unknown.subscription_quota_remaining is None
    assert unknown.local_energy_cost_usd_per_task is None
    assert unknown.local_compute_cost_usd_per_task is None
    assert free_marginal.effective_marginal_cost_usd_per_task == 0.0


@pytest.mark.parametrize("billing_kind", ["subscription", "local"])
def test_public_metered_prices_cannot_be_attached_to_non_metered_paths(billing_kind):
    with pytest.raises(ValidationError, match="metered pricing"):
        _access_economics(
            billing_kind,
            metered_input_usd_per_million_tokens=2.5,
            effective_marginal_cost_usd_per_task=0.01,
        )


@pytest.mark.parametrize(
    ("billing_kind", "field", "value"),
    [
        ("metered", "subscription_plan", "pro"),
        ("local", "subscription_quota_remaining", 10.0),
        ("metered", "local_compute_cost_usd_per_task", 0.01),
        ("subscription", "local_energy_cost_usd_per_task", 0.01),
    ],
)
def test_billing_specific_economics_cannot_cross_access_kinds(
    billing_kind,
    field,
    value,
):
    with pytest.raises(ValidationError, match="billing-specific economics"):
        _access_economics(billing_kind, **{field: value})


@pytest.mark.parametrize("secret_field", ["plan_credential", "account_id", "api_key"])
def test_access_economics_rejects_credential_and_account_identifiers(secret_field):
    with pytest.raises(ValidationError, match=secret_field):
        AccessEconomics(
            billing_kind="subscription",
            source_id="user-configured",
            provenance="config_override",
            observed_at="2026-07-15T12:00:00Z",
            **{secret_field: "secret-or-account"},
        )


def test_runtime_observation_binds_one_economics_record_to_full_access_path():
    key = RuntimeKey(
        provider="openai-codex",
        model="gpt-5.4",
        auth_identity="subscription:default",
        credential_pool_identity="pool:codex",
        endpoint_identity="endpoint:codex",
        api_mode="codex_responses",
        local_backend="",
        inventory_revision="inventory-1",
    )
    economics = _access_economics(
        subscription_plan="pro",
        subscription_quota_remaining=None,
        throttle_state="healthy",
    )

    observation = RuntimeObservation(
        key=key,
        state="verified",
        reasons=("authenticated_live",),
        economics=economics,
        verification_source="authenticated_live",
        verified_at="2026-07-15T11:59:00Z",
        verification_expires_at="2026-07-16T11:59:00Z",
        provenance=("provider-model-list",),
        observed_at="2026-07-15T12:00:00Z",
    )

    assert observation.key == key
    assert observation.economics is economics
    assert observation.reasons == ("authenticated_live",)


@pytest.mark.parametrize(
    ("state", "reasons", "verification_source", "verified_at", "requirement"),
    [
        ("configured_unverified", (), None, None, "reasons"),
        ("temporarily_unavailable", (), "authenticated_live", "earlier", "reasons"),
        ("ineligible", (), None, None, "reasons"),
        ("verified", (), None, "2026-07-15T11:59:00Z", "verification_source"),
        ("verified", (), "authenticated_live", None, "verified_at"),
    ],
)
def test_runtime_observation_enforces_state_evidence_truth_table(
    valid_root,
    state,
    reasons,
    verification_source,
    verified_at,
    requirement,
):
    key = parse_config(valid_root).profiles["coding"].primary.runtime

    with pytest.raises(
        ValidationError,
        match=rf"{state}.*{requirement}",
    ):
        RuntimeObservation(
            key=key,
            state=state,
            reasons=reasons,
            economics=_access_economics(),
            verification_source=verification_source,
            verified_at=verified_at,
            verification_expires_at=None,
            provenance=("inventory",),
            observed_at="2026-07-15T12:00:00Z",
        )


@pytest.mark.parametrize(
    ("state", "reasons", "verification_source", "verified_at"),
    [
        ("verified", (), "authenticated_live", "2026-07-15T11:59:00Z"),
        ("configured_unverified", ("access_not_established",), None, None),
        (
            "temporarily_unavailable",
            ("provider_cooldown_until_2026-07-15T12:05:00Z",),
            "authenticated_live",
            "2026-07-15T11:59:00Z",
        ),
        ("ineligible", ("license_not_allowed",), None, None),
    ],
)
def test_runtime_observation_accepts_complete_state_evidence(
    valid_root,
    state,
    reasons,
    verification_source,
    verified_at,
):
    key = parse_config(valid_root).profiles["coding"].primary.runtime

    observation = RuntimeObservation(
        key=key,
        state=state,
        reasons=reasons,
        economics=_access_economics(),
        verification_source=verification_source,
        verified_at=verified_at,
        verification_expires_at=None,
        provenance=("inventory",),
        observed_at="2026-07-15T12:00:00Z",
    )

    assert observation.state == state
    assert observation.reasons == reasons


def test_policy_envelope_contains_exact_immutable_policy_fields(valid_root):
    policy = parse_config(valid_root).policy

    assert set(policy.model_dump()) == {
        "eligible_sources",
        "uninstalled_local_models",
        "local_models",
        "denied_providers",
        "denied_models",
        "max_estimated_task_cost_usd",
        "max_estimated_latency_seconds",
        "max_routing_overhead_usd_per_day",
        "max_experiment_cost_usd_per_day",
        "max_evaluator_calls_per_day",
        "max_canary_fraction",
        "max_reasoning_effort",
        "allow_subscription",
        "allow_paid_access_probes",
        "allowed_licenses",
        "minimum_context_tokens",
        "canary_high_risk_tasks",
    }
    assert policy.local_models.require_open_weights is True
    assert policy.local_models.require_compatible_hardware is True


def test_empty_license_list_means_no_additional_allowlist(valid_root):
    parsed = parse_config(valid_root)

    assert parsed.policy.allowed_licenses == ()
    assert parsed.profiles["coding"].limits is None


def test_stage_two_parses_active_activation_without_projecting_it(valid_root):
    _plugin(valid_root)["activation"]["mode"] = "active"

    assert parse_config(valid_root).activation.mode == "active"


def test_stage_one_authority_loads_bounded_routing_defaults(valid_root):
    parsed = parse_config(valid_root)

    assert parsed.rules == ()
    assert parsed.complexity_bands.trivial_max < parsed.complexity_bands.easy_max
    assert parsed.complexity_bands.easy_max < parsed.complexity_bands.moderate_max
    assert parsed.complexity_bands.moderate_max < parsed.complexity_bands.hard_max
    assert {"tools", "long_context"} <= set(parsed.routing_vocabulary.capabilities)
    assert "text" in parsed.routing_vocabulary.modalities
    assert parsed.classifier.maximum_input_tokens > 0
    assert parsed.classifier.maximum_output_tokens > 0
    assert parsed.classifier.maximum_image_count > 0
    assert parsed.classifier.maximum_image_bytes > 0


def test_explicit_compatibility_defaults_hash_like_implicit_defaults(valid_root):
    implicit = parse_config(valid_root)
    explicit_root = copy.deepcopy(valid_root)
    plugin = _plugin(explicit_root)
    plugin["rules"] = [rule.model_dump(mode="json") for rule in implicit.rules]
    plugin["complexity_bands"] = implicit.complexity_bands.model_dump(mode="json")
    plugin["routing_vocabulary"] = implicit.routing_vocabulary.model_dump(mode="json")
    plugin["classifier"].update(
        maximum_input_tokens=implicit.classifier.maximum_input_tokens,
        maximum_output_tokens=implicit.classifier.maximum_output_tokens,
        maximum_image_count=implicit.classifier.maximum_image_count,
        maximum_image_bytes=implicit.classifier.maximum_image_bytes,
    )

    explicit = parse_config(explicit_root)
    assert explicit == implicit
    assert authority_revision(explicit) == authority_revision(implicit)


def test_pre_stage4_active_receipt_survives_default_profile_fields(
    valid_root,
    isolated_home,
) -> None:
    _plugin(valid_root)["activation"]["mode"] = "active"
    parsed = parse_config(valid_root)

    legacy_authority = authority_document(parsed)
    legacy_config = config_document(parsed)
    for document in (legacy_authority, legacy_config):
        profile = document["profiles"]["coding"]
        profile.pop("primary_challengers", None)
        profile.pop("adaptation", None)
    expected_authority_id = hashlib.sha256(
        json.dumps(
            legacy_authority,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    expected_config_sha = hashlib.sha256(
        json.dumps(
            legacy_config,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    receipt = ActivationReceipt(
        receipt_id="receipt-pre-stage4",
        authority_id=expected_authority_id,
        config_sha=expected_config_sha,
        inventory_contract_sha="c" * 64,
        inventory_revision="inventory-pre-stage4",
        adapter_capability_sha="d" * 64,
        created_at="2026-07-18T12:00:00Z",
    )

    with RoutingStore.open(home=isolated_home) as store:
        store.write_activation_receipt(receipt)
        reparsed = parse_config(valid_root)
        restored = store.read_matching_activation_receipt(
            authority_id=authority_revision(reparsed),
            config_sha=config_revision(reparsed),
            adapter_capability_sha=receipt.adapter_capability_sha,
        )

    assert authority_revision(parsed) == expected_authority_id
    assert config_revision(parsed) == expected_config_sha
    assert restored == receipt


def test_nondefault_disabled_profile_adaptation_round_trips_and_hashes(
    valid_root,
) -> None:
    baseline = parse_config(valid_root)
    _plugin(valid_root)["profiles"]["coding"]["adaptation"] = {
        "enabled": False,
        "canary_fraction": 0.01,
    }

    staged = parse_config(valid_root)
    document = config_document(staged)
    reparsed = parse_config({
        "plugins": {"entries": {"auto-routing": document}}
    })

    assert document["profiles"]["coding"]["adaptation"] == {
        "enabled": False,
        "canary_fraction": 0.01,
        "minimum_comparable_samples": 20,
        "observed_regression_threshold": 0.10,
        "cooldown_base_seconds": 3_600,
        "cooldown_max_seconds": 86_400,
        "confidence_level": 0.90,
    }
    assert reparsed == staged
    assert config_revision(reparsed) == config_revision(staged)
    assert config_revision(staged) != config_revision(baseline)
    assert authority_revision(staged) != authority_revision(baseline)


def test_rule_profile_must_exist(valid_root):
    _plugin(valid_root)["rules"] = [
        {
            "rule_id": "coding",
            "priority": 100,
            "profile_id": "missing",
            "effect": "pin_profile",
            "when": {"domains_any": ["coding"]},
        }
    ]

    with pytest.raises(ConfigError, match="missing profile"):
        parse_config(valid_root)


def test_rules_have_unique_ids_and_canonical_precedence(valid_root):
    plugin = _plugin(valid_root)
    plugin["rules"] = [
        {
            "rule_id": "z-last",
            "priority": 10,
            "profile_id": "coding",
            "effect": "prefer_profile",
            "when": {"domains_any": ["coding"]},
        },
        {
            "rule_id": "high",
            "priority": 20,
            "profile_id": "coding",
            "effect": "pin_profile",
            "when": {"domains_any": ["coding"]},
        },
        {
            "rule_id": "a-first",
            "priority": 10,
            "profile_id": "coding",
            "effect": "prefer_profile",
            "when": {"domains_any": ["debugging"]},
        },
    ]

    parsed = parse_config(valid_root)
    assert tuple(rule.rule_id for rule in parsed.rules) == (
        "high",
        "a-first",
        "z-last",
    )

    plugin["rules"].append(copy.deepcopy(plugin["rules"][0]))
    with pytest.raises(ConfigError, match="duplicate rule_id"):
        parse_config(valid_root)


def test_rule_count_matches_decision_applied_rule_boundary(valid_root) -> None:
    plugin = _plugin(valid_root)
    plugin["rules"] = [
        {
            "rule_id": f"rule-{index}",
            "priority": index,
            "profile_id": "coding",
            "effect": "prefer_profile",
            "when": {},
        }
        for index in range(64)
    ]

    assert len(parse_config(valid_root).rules) == 64

    plugin["rules"].append({
        "rule_id": "rule-overflow",
        "priority": 65,
        "profile_id": "coding",
        "effect": "prefer_profile",
        "when": {},
    })
    with pytest.raises(ConfigError, match="rules"):
        parse_config(valid_root)


def test_rule_ranges_and_vocabulary_are_closed_authority(valid_root):
    plugin = _plugin(valid_root)
    plugin["routing_vocabulary"] = {
        "capabilities": ["tools", "long_context", "batch_reasoning"],
        "modalities": ["text", "image"],
    }
    plugin["rules"] = [
        {
            "rule_id": "batch",
            "priority": 1,
            "profile_id": "coding",
            "effect": "prefer_profile",
            "when": {
                "required_capabilities_all": ["batch_reasoning"],
                "required_modalities_any": ["text"],
                "minimum_complexity": 0.8,
                "maximum_complexity": 0.2,
            },
        }
    ]

    with pytest.raises(ConfigError, match="minimum_complexity.*maximum_complexity"):
        parse_config(valid_root)

    plugin["rules"][0]["when"].pop("maximum_complexity")
    plugin["rules"][0]["when"]["required_capabilities_all"] = ["undeclared"]
    with pytest.raises(ConfigError, match="routing_vocabulary"):
        parse_config(valid_root)


@pytest.mark.parametrize(
    "bands",
    [
        {"trivial_max": 0.2, "easy_max": 0.2, "moderate_max": 0.7, "hard_max": 0.9},
        {"trivial_max": 0.3, "easy_max": 0.2, "moderate_max": 0.7, "hard_max": 0.9},
        {"trivial_max": 0.1, "easy_max": 0.4, "moderate_max": 0.9, "hard_max": 0.8},
    ],
)
def test_complexity_bands_are_strictly_increasing(valid_root, bands):
    _plugin(valid_root)["complexity_bands"] = bands

    with pytest.raises(
        ConfigError,
        match="trivial_max.*easy_max.*moderate_max.*hard_max",
    ):
        parse_config(valid_root)


def test_profile_complexity_and_matches_use_declared_vocabulary(valid_root):
    plugin = _plugin(valid_root)
    plugin["profiles"]["coding"]["match"]["complexity"] = ["commercial-pro"]
    with pytest.raises(ConfigError, match="unknown complexity"):
        parse_config(valid_root)

    plugin["profiles"]["coding"]["match"]["complexity"] = ["hard"]
    plugin["profiles"]["coding"]["match"]["capabilities"] = ["undeclared"]
    with pytest.raises(ConfigError, match="routing_vocabulary"):
        parse_config(valid_root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("maximum_input_tokens", 0),
        ("maximum_input_tokens", 10_000_001),
        ("maximum_output_tokens", 0),
        ("maximum_output_tokens", 1_000_001),
        ("maximum_image_count", 0),
        ("maximum_image_count", 257),
        ("maximum_image_bytes", 0),
        ("maximum_image_bytes", 1_000_000_001),
    ],
)
def test_classifier_resource_bounds_have_hard_ceilings(valid_root, field, value):
    _plugin(valid_root)["classifier"][field] = value

    with pytest.raises(ConfigError, match=field):
        parse_config(valid_root)


def test_vocabulary_labels_are_bounded_and_cannot_name_runtimes(valid_root):
    plugin = _plugin(valid_root)
    plugin["routing_vocabulary"] = {
        "capabilities": ["tools", "long_context", "openai_codex"],
        "modalities": ["text"],
    }

    with pytest.raises(ConfigError, match="provider or model identity"):
        parse_config(valid_root)

    plugin["routing_vocabulary"]["capabilities"] = [
        "tools",
        "gpt_5_4_coding",
    ]
    with pytest.raises(ConfigError, match="provider or model identity"):
        parse_config(valid_root)

    runtime = plugin["profiles"]["coding"]["primary"]["runtime"]
    runtime["model"] = "o3"
    plugin["routing_vocabulary"]["capabilities"] = ["tools", "o3_reasoning"]
    with pytest.raises(ConfigError, match="provider or model identity"):
        parse_config(valid_root)

    runtime["provider"] = "ai"
    runtime["model"] = "m"
    plugin["routing_vocabulary"]["capabilities"] = ["tools", "ai_reasoning"]
    assert "ai_reasoning" in parse_config(valid_root).routing_vocabulary.capabilities

    plugin["routing_vocabulary"]["capabilities"] = ["tools", "a" * 65]
    with pytest.raises(ConfigError, match="64"):
        parse_config(valid_root)


@pytest.mark.parametrize(
    "label",
    ["openai_codex_reasoning", "gpt_5_4_mini_reasoning"],
)
def test_vocabulary_cannot_name_classifier_runtime_when_routes_differ(
    valid_root,
    label,
):
    plugin = _plugin(valid_root)
    route_runtime = plugin["profiles"]["coding"]["primary"]["runtime"]
    route_runtime["provider"] = "unrelated-provider"
    route_runtime["model"] = "unrelated-model"
    plugin["routing_vocabulary"] = {
        "capabilities": ["tools", label],
        "modalities": ["text"],
    }

    with pytest.raises(ConfigError, match="provider or model identity"):
        parse_config(valid_root)


@pytest.mark.parametrize(
    ("authority_field", "authority_identity", "label"),
    [
        ("allowed_providers", "anthropic", "anthropic_reasoning"),
        ("allowed_models", "claude-4", "claude_4_reasoning"),
    ],
)
def test_vocabulary_cannot_name_explicit_llm_allowlist_identity(
    valid_root,
    authority_field,
    authority_identity,
    label,
):
    plugin = _plugin(valid_root)
    plugin["llm"][authority_field].append(authority_identity)
    plugin["routing_vocabulary"] = {
        "capabilities": ["tools", label],
        "modalities": ["text"],
    }

    with pytest.raises(ConfigError, match="provider or model identity"):
        parse_config(valid_root)


def test_vocabulary_cannot_name_namespaced_model_basename(valid_root):
    plugin = _plugin(valid_root)
    route_runtime = plugin["profiles"]["coding"]["primary"]["runtime"]
    route_runtime["provider"] = "unrelated-provider"
    route_runtime["model"] = "openai/gpt-5.4"
    plugin["routing_vocabulary"] = {
        "capabilities": ["tools", "gpt_5_4_coding"],
        "modalities": ["text"],
    }

    with pytest.raises(ConfigError, match="provider or model identity"):
        parse_config(valid_root)


@pytest.mark.parametrize(
    ("policy_field", "identity", "label"),
    [
        (
            "denied_providers",
            "forbidden-provider",
            "forbidden_provider_reasoning",
        ),
        ("denied_models", "forbidden/model-x", "model_x_reasoning"),
    ],
)
def test_vocabulary_cannot_name_denied_runtime_identity(
    valid_root,
    policy_field,
    identity,
    label,
):
    plugin = _plugin(valid_root)
    plugin["policy"][policy_field] = [identity]
    plugin["routing_vocabulary"] = {
        "capabilities": ["tools", label],
        "modalities": ["text"],
    }

    with pytest.raises(ConfigError, match="provider or model identity"):
        parse_config(valid_root)


@pytest.mark.parametrize(
    ("authority_field", "classifier_field", "authority_value"),
    [
        ("allow_provider_override", "provider", False),
        ("allow_model_override", "model", False),
        ("allowed_providers", "provider", ["another-provider"]),
        ("allowed_models", "model", ["another-model"]),
    ],
)
def test_classifier_runtime_must_be_authorized_by_plugin_llm_trust(
    valid_root,
    authority_field,
    classifier_field,
    authority_value,
):
    plugin = _plugin(valid_root)
    plugin["llm"][authority_field] = authority_value

    with pytest.raises(
        ConfigError,
        match=rf"classifier {classifier_field}.*{authority_field}",
    ):
        parse_config(valid_root)


@pytest.mark.parametrize(
    ("classifier_field", "allowed_field", "classifier_value", "allowed_values"),
    [
        ("provider", "allowed_providers", "OpenAI-Codex", ["openai-codex"]),
        ("model", "allowed_models", "GPT-5.4-MINI", ["gpt-5.4-mini"]),
        ("provider", "allowed_providers", "any-provider", ["*"]),
        ("model", "allowed_models", "any-model", ["*"]),
    ],
)
def test_classifier_trust_allowlists_match_plugin_llm_normalization(
    valid_root,
    classifier_field,
    allowed_field,
    classifier_value,
    allowed_values,
):
    plugin = _plugin(valid_root)
    plugin["classifier"][classifier_field] = classifier_value
    plugin["llm"][allowed_field] = allowed_values

    assert parse_config(valid_root).classifier.model_dump()[classifier_field] == (
        classifier_value
    )


def test_canary_fraction_cannot_exceed_policy_maximum(valid_root):
    _plugin(valid_root)["adaptation"]["canary_fraction"] = 0.051

    with pytest.raises(ConfigError, match="canary_fraction"):
        parse_config(valid_root)


def test_legacy_top_level_adaptation_does_not_enable_profile(valid_root) -> None:
    plugin = _plugin(valid_root)
    assert plugin["adaptation"]["enabled"] is True

    parsed = parse_config(valid_root)

    assert parsed.adaptation.enabled is True
    assert parsed.profiles["coding"].adaptation == ProfileAdaptationSettings()


def test_profile_canary_fraction_cannot_exceed_policy_maximum(valid_root) -> None:
    profile = _plugin(valid_root)["profiles"]["coding"]
    profile["primary_challengers"] = [
        _distinct_target(
            profile["primary"],
            "challenger",
            revision_status="challenger",
        )
    ]
    profile["adaptation"] = {"enabled": True, "canary_fraction": 0.051}

    with pytest.raises(ConfigError, match="profile.*canary_fraction"):
        parse_config(valid_root)


def test_disabled_profile_default_ignores_zero_policy_canary_ceiling(
    valid_root,
) -> None:
    plugin = _plugin(valid_root)
    plugin["policy"]["max_canary_fraction"] = 0.0
    plugin["adaptation"]["canary_fraction"] = 0.0

    parsed = parse_config(valid_root)

    assert parsed.profiles["coding"].adaptation.enabled is False
    assert parsed.profiles["coding"].adaptation.canary_fraction == 0.05


def test_profile_adaptation_authority_changes_both_revisions(valid_root) -> None:
    baseline = parse_config(valid_root)
    profile = _plugin(valid_root)["profiles"]["coding"]
    profile["primary_challengers"] = [
        _distinct_target(
            profile["primary"],
            "challenger",
            revision_status="challenger",
        )
    ]
    profile["adaptation"] = {"enabled": True}

    adaptive = parse_config(valid_root)

    assert authority_revision(adaptive) != authority_revision(baseline)
    assert config_revision(adaptive) != config_revision(baseline)


def test_nonempty_challengers_change_hashes_before_profile_opt_in(valid_root) -> None:
    baseline = parse_config(valid_root)
    profile = _plugin(valid_root)["profiles"]["coding"]
    profile["primary_challengers"] = [
        _distinct_target(
            profile["primary"],
            "challenger",
            revision_status="challenger",
        )
    ]

    challenger_only = parse_config(valid_root)

    assert challenger_only.profiles["coding"].adaptation.enabled is False
    assert authority_revision(challenger_only) != authority_revision(baseline)
    assert config_revision(challenger_only) != config_revision(baseline)


def test_enabling_adaptation_changes_hashes_for_same_primary_choices(
    valid_root,
) -> None:
    profile = _plugin(valid_root)["profiles"]["coding"]
    profile["primary_challengers"] = [
        _distinct_target(
            profile["primary"],
            "challenger",
            revision_status="challenger",
        )
    ]
    disabled = parse_config(valid_root)
    profile["adaptation"] = {"enabled": True}

    enabled = parse_config(valid_root)

    assert authority_revision(enabled) != authority_revision(disabled)
    assert config_revision(enabled) != config_revision(disabled)


def test_profile_candidate_limit_counts_primary_choices_and_fallbacks(
    valid_root,
) -> None:
    plugin = _plugin(valid_root)
    base_profile = plugin["profiles"]["coding"]
    profiles: dict[str, Any] = {}
    for index in range(16):
        profile = copy.deepcopy(base_profile)
        profile_id = f"profile-{index}"
        profile["profile_id"] = profile_id
        profile["primary_challengers"] = [
            _distinct_target(
                profile["primary"],
                f"{index}-challenger",
                revision_status="challenger",
            )
        ]
        fallback_count = 62
        profile["fallbacks"] = [
            _distinct_target(
                profile["primary"],
                f"{index}-fallback-{fallback_index}",
                revision_status="fallback",
            )
            for fallback_index in range(fallback_count)
        ]
        profiles[profile_id] = profile
    plugin["profiles"] = profiles

    parsed = parse_config(valid_root)
    assert (
        sum(
            len(profile.primary_choices()) + len(profile.fallbacks)
            for profile in parsed.profiles.values()
        )
        == 1024
    )

    profiles["profile-15"]["fallbacks"].append(
        _distinct_target(
            profiles["profile-15"]["primary"],
            "15-candidate-over-limit",
            revision_status="fallback",
        )
    )
    with pytest.raises(ConfigError, match="profile targets|candidate"):
        parse_config(valid_root)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_estimated_task_cost_usd": 2.01},
        {"max_estimated_latency_seconds": 120.01},
        {"max_reasoning_effort": "xhigh"},
        {"minimum_context_tokens": -1},
        {"canary_high_risk_tasks": True},
    ],
)
def test_profile_limits_cannot_loosen_global_policy(valid_root, limits):
    _plugin(valid_root)["profiles"]["coding"]["limits"] = limits

    with pytest.raises(ConfigError, match="profile.*loosen|minimum_context_tokens"):
        parse_config(valid_root)


def test_profile_license_allowlist_cannot_loosen_global_allowlist(valid_root):
    plugin = _plugin(valid_root)
    plugin["policy"]["allowed_licenses"] = ["apache-2.0"]
    plugin["profiles"]["coding"]["limits"] = {"allowed_licenses": ["apache-2.0", "mit"]}

    with pytest.raises(ConfigError, match="allowed_licenses.*loosen"):
        parse_config(valid_root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_estimated_task_cost_usd", 2.01),
        ("max_estimated_latency_seconds", 120.01),
    ],
)
def test_target_limits_cannot_loosen_effective_policy(valid_root, field, value):
    _plugin(valid_root)["profiles"]["coding"]["primary"][field] = value

    with pytest.raises(ConfigError, match=field):
        parse_config(valid_root)


@pytest.mark.parametrize(
    "reasoning",
    [
        {"default": "low", "min": "medium", "max": "high"},
        {"default": "high", "min": "low", "max": "medium"},
    ],
)
def test_reasoning_bounds_follow_canonical_effort_order(valid_root, reasoning):
    _plugin(valid_root)["profiles"]["coding"]["primary"]["reasoning"] = reasoning

    with pytest.raises(ConfigError, match="reasoning.*minimum.*default.*maximum"):
        parse_config(valid_root)


def test_target_reasoning_cannot_exceed_global_policy(valid_root):
    target = _plugin(valid_root)["profiles"]["coding"]["primary"]
    target["reasoning"] = {"default": "high", "min": "low", "max": "xhigh"}

    with pytest.raises(ConfigError, match="max_reasoning_effort"):
        parse_config(valid_root)


def test_challenger_reasoning_cannot_exceed_global_policy(valid_root) -> None:
    profile = _plugin(valid_root)["profiles"]["coding"]
    challenger = _distinct_target(
        profile["primary"],
        "challenger",
        revision_status="challenger",
    )
    challenger["reasoning"] = {"default": "high", "min": "low", "max": "xhigh"}
    profile["primary_challengers"] = [challenger]

    with pytest.raises(ConfigError, match="primary_challenger.*max_reasoning_effort"):
        parse_config(valid_root)


@pytest.mark.parametrize("location", ["primary", "fallback", "safe_default"])
@pytest.mark.parametrize("denied_field", ["denied_providers", "denied_models"])
def test_global_denials_reject_every_configured_target_location(
    valid_root,
    location,
    denied_field,
):
    plugin = _plugin(valid_root)
    profile = plugin["profiles"]["coding"]
    if location == "primary":
        target = profile["primary"]
    else:
        target = copy.deepcopy(profile["primary"])
        target["runtime"].update({
            "provider": f"{location}-provider",
            "model": f"{location}-model",
            "auth_identity": f"configured:{location}",
            "credential_pool_identity": "",
            "endpoint_identity": f"endpoint:{location}",
            "api_mode": "chat_completions",
        })
        if location == "fallback":
            target["revision_status"] = "fallback"
            profile["fallbacks"] = [target]
        else:
            plugin["safe_default"] = target

    runtime_field = "provider" if denied_field == "denied_providers" else "model"
    plugin["policy"][denied_field] = [target["runtime"][runtime_field]]

    with pytest.raises(
        ConfigError,
        match=rf"({location}.*{denied_field}|{denied_field}.*{location})",
    ):
        parse_config(valid_root)


def test_economics_overrides_are_keyed_by_runtime_stable_id(valid_root):
    _plugin(valid_root)["economics_overrides"]["not-a-runtime-id"] = {
        "billing_kind": "subscription",
        "source_id": "user-configured",
        "provenance": "config_override",
        "observed_at": "2026-07-15T12:00:00Z",
    }

    with pytest.raises(ConfigError, match="stable runtime ID"):
        parse_config(valid_root)


def test_task_assessment_carries_complete_provider_independent_requirements():
    assessment = _task_assessment()

    assert assessment.complexity == 0.78
    assert assessment.required_capabilities == ("tools", "long_context")
    assert assessment.required_modalities == ("text",)
    assert assessment.expected_context_tokens == 32_000
    assert assessment.expected_output_tokens == 4_000
    assert assessment.risk_class == "moderate"
    assert assessment.confidence == 0.74


@pytest.mark.parametrize("risk_class", ["routine", "normal", "dangerous"])
def test_task_assessment_rejects_unknown_risk_class(risk_class):
    document = _task_assessment().model_dump(mode="json")
    document["risk_class"] = risk_class

    with pytest.raises(ValidationError, match="risk_class"):
        TaskAssessment.model_validate(document)


def test_task_assessment_and_task_facts_have_bounded_normalized_labels():
    assessment = _task_assessment()
    assert assessment.domains == ("coding", "debugging")

    with pytest.raises(ValidationError):
        TaskAssessment.model_validate({
            **assessment.model_dump(mode="json"),
            "domains": [f"domain_{index}" for index in range(65)],
        })
    with pytest.raises(ValidationError):
        TaskFacts(
            scope="fresh_session",
            platform="cli",
            domains=("coding",),
            required_capabilities=("has spaces",),
            required_modalities=("text",),
        )


@pytest.mark.parametrize(
    "invalid_labels",
    [
        [{"nested": "label"}],
        {"tools": True},
        {"tools", "text"},
    ],
)
def test_label_collections_reject_nested_mapping_and_set_inputs(
    valid_root,
    invalid_labels,
):
    _plugin(valid_root)["routing_vocabulary"] = {
        "capabilities": invalid_labels,
        "modalities": ["text", "image", "audio", "video"],
    }
    with pytest.raises(ConfigError, match="list or tuple|every label"):
        parse_config(valid_root)

    document = _task_assessment().model_dump(mode="json")
    document["required_capabilities"] = invalid_labels
    with pytest.raises(ValidationError, match="list or tuple|every label"):
        TaskAssessment.model_validate(document)


def test_routing_decision_carries_complete_explainable_decision(valid_root):
    parsed = parse_config(valid_root)
    primary = parsed.profiles["coding"].primary
    decision = RoutingDecision(
        decision_id="decision-1",
        scope="fresh_session",
        session_id="session-1",
        task_id="task-1",
        operation_id=None,
        task_index=None,
        created_at="2026-07-15T12:00:00Z",
        applied_rule_ids=("rule-tools",),
        assessment=_task_assessment(),
        task_facts_hash="1" * 64,
        inventory_revision="inventory-1",
        catalog_revision="catalog-1",
        authority_revision="authority-1",
        policy_revision="policy-1",
        adaptive_revision="adaptive-1",
        activation_receipt_id=None,
        activation_config_sha=None,
        adapter_capability_sha=None,
        eligible_candidates=(primary.runtime.stable_id(),),
        rejected_candidates=(("2" * 64, ("missing_tools",)),),
        normalized_scoring_inputs=(("quality", 0.8), ("cost", 0.6)),
        final_scores=((primary.runtime.stable_id(), 0.77),),
        selected_profile_id="coding",
        selected_runtime=primary.runtime,
        selected_reasoning_effort="medium",
        projection_mode="shadow",
        selection_reason="highest_eligible_score",
        projected_fallback_chain=(),
        safe_default_runtime=primary.runtime,
        safe_default_reasoning_effort="medium",
        classifier_runtime_id=primary.runtime.stable_id(),
        classifier_input_tokens=120,
        classifier_output_tokens=30,
        classifier_cost_usd=0.001,
        routing_latency_seconds=0.25,
    )

    assert decision.selected_runtime == primary.runtime
    assert decision.assessment.required_capabilities == ("tools", "long_context")
    assert decision.rejected_candidates[0][1] == ("missing_tools",)
    assert decision.final_scores[0][1] == 0.77
    assert decision.safe_default_reason is None
    assert decision.degradation_reason is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_at", "the user asked for a private coding task"),
        ("eligible_candidates", ["not-a-runtime-hash"]),
        ("selection_reason", "user_wants_a_private_summary"),
        ("safe_default_reason", "user_wants_a_private_summary"),
        ("degradation_reason", "user_wants_a_private_summary"),
        ("normalized_scoring_inputs", [["raw task prose", 0.5]]),
        (
            "rejected_candidates",
            [["3" * 64, ["user_wants_a_private_summary"]]],
        ),
    ],
)
def test_routing_decision_rejects_prose_and_unbounded_identity_fields(
    valid_root,
    field,
    value,
):
    document = _decision_document(valid_root)
    document[field] = value
    with pytest.raises(ValidationError):
        RoutingDecision.model_validate(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("normalized_scoring_inputs", [["quality", True]]),
        ("normalized_scoring_inputs", [["quality", float("nan")]]),
        ("final_scores", [["1" * 64, True]]),
        ("final_scores", [["1" * 64, float("inf")]]),
        ("classifier_cost_usd", True),
        ("routing_latency_seconds", float("nan")),
    ],
)
def test_routing_decision_scoring_numbers_are_strict_and_finite(
    valid_root,
    field,
    value,
):
    document = _decision_document(valid_root)
    document[field] = value
    with pytest.raises(ValidationError):
        RoutingDecision.model_validate(document)


def _decision_document(valid_root, *, scope: str = "fresh_session") -> dict[str, Any]:
    parsed = parse_config(valid_root)
    primary = parsed.profiles["coding"].primary
    return {
        "decision_id": f"decision-{scope}",
        "scope": scope,
        "session_id": "session-1",
        "task_id": "task-1",
        "operation_id": None,
        "task_index": None,
        "created_at": "2026-07-15T12:00:00Z",
        "applied_rule_ids": [],
        "assessment": _task_assessment().model_dump(mode="json"),
        "task_facts_hash": "2" * 64,
        "inventory_revision": "inventory-1",
        "catalog_revision": "catalog-1",
        "authority_revision": "authority-1",
        "policy_revision": "policy-1",
        "adaptive_revision": "adaptive-1",
        "activation_receipt_id": None,
        "activation_config_sha": None,
        "adapter_capability_sha": None,
        "eligible_candidates": [primary.runtime.stable_id()],
        "rejected_candidates": [],
        "normalized_scoring_inputs": [],
        "final_scores": [],
        "selected_profile_id": "coding",
        "selected_runtime": primary.runtime.model_dump(mode="json"),
        "selected_reasoning_effort": "medium",
        "projection_mode": "shadow",
        "selection_reason": "rule",
        "projected_fallback_chain": [],
        "safe_default_runtime": primary.runtime.model_dump(mode="json"),
        "safe_default_reasoning_effort": "medium",
        "classifier_runtime_id": primary.runtime.stable_id(),
        "classifier_input_tokens": 1,
        "classifier_output_tokens": 1,
        "classifier_cost_usd": 0,
        "routing_latency_seconds": 0,
    }


def test_routing_decision_requires_operation_identity_for_delegation(valid_root):
    with pytest.raises(ValidationError, match="operation_id.*task_index"):
        RoutingDecision.model_validate(
            _decision_document(valid_root, scope="delegation")
        )


def test_assessment_may_be_absent_only_for_typed_safe_default(valid_root):
    document = _decision_document(valid_root)
    document.update(
        assessment=None,
        selected_profile_id=None,
        projection_mode="inherit",
        selection_reason="classifier_failed",
        classifier_runtime_id=None,
        classifier_input_tokens=0,
        classifier_output_tokens=0,
    )
    with pytest.raises(ValidationError, match="safe_default_reason"):
        RoutingDecision.model_validate(document)

    decision = RoutingDecision.model_validate({
        **document,
        "safe_default_reason": "classifier_malformed",
    })
    assert decision.assessment is None


def test_catalog_evidence_retains_mandated_provenance_fields():
    evidence = CatalogEvidence(
        source_id="swe-bench",
        source_url="https://www.swebench.com/",
        retrieved_at="2026-07-15T12:00:00Z",
        published_at="2026-06-01T00:00:00Z",
        model="gpt-5.4",
        model_version="2026-06-30",
        domain="coding",
        task_definition="SWE-bench Verified",
        metric_name="resolved_rate",
        metric_direction="higher_is_better",
        metric_scale="fraction",
        value=0.62,
        sample_size=500,
        confidence=0.8,
        normalization_method="identity_fraction",
    )

    assert evidence.source_url == "https://www.swebench.com/"
    assert evidence.retrieved_at == "2026-07-15T12:00:00Z"
    assert evidence.published_at == "2026-06-01T00:00:00Z"
    assert evidence.metric_direction == "higher_is_better"
    assert evidence.sample_size == 500
    assert evidence.normalization_method == "identity_fraction"


def test_catalog_evidence_rejects_endpoint_and_credential_fields():
    base = {
        "source_id": "review-lab",
        "source_url": "https://example.test/review",
        "retrieved_at": "2026-07-15T12:00:00Z",
        "published_at": "2026-06-01T00:00:00Z",
        "model": "gpt-5.4",
        "model_version": "2026-06-30",
        "domain": "coding",
        "task_definition": "review rubric",
        "metric_name": "quality",
        "metric_direction": "higher_is_better",
        "metric_scale": "fraction",
        "value": 0.8,
        "sample_size": None,
        "confidence": None,
        "normalization_method": "identity_fraction",
    }

    with pytest.raises(ValidationError, match="endpoint_url"):
        CatalogEvidence(**base, endpoint_url="https://private.example/v1")
    with pytest.raises(ValidationError, match="api_key"):
        CatalogEvidence(**base, api_key="secret")


def test_catalog_sample_and_confidence_are_optional_when_unavailable():
    evidence = CatalogEvidence(
        source_id="review-lab",
        source_url="https://example.test/review",
        retrieved_at="2026-07-15T12:00:00Z",
        published_at="2026-06-01T00:00:00Z",
        model="gpt-5.4",
        model_version="2026-06-30",
        domain="coding",
        task_definition="review rubric",
        metric_name="quality",
        metric_direction="higher_is_better",
        metric_scale="fraction",
        value=0.8,
        normalization_method="identity_fraction",
    )

    assert evidence.sample_size is None
    assert evidence.confidence is None


def test_adaptive_revision_is_complete_lineage_linked_and_authority_bound():
    revision = AdaptiveRevision(
        revision_id="revision-1",
        authority_id="authority-1",
        overlay={"profiles": {"coding": {"primary": "runtime-1"}}},
        explanation={
            "summary": "Initial approved baseline",
            "reason_codes": ["baseline"],
        },
        created_at="2026-07-15T12:00:00Z",
        is_baseline=True,
    )

    assert revision.parent_revision_id is None
    assert revision.overlay["profiles"]["coding"]["primary"] == "runtime-1"
    assert revision.explanation["reason_codes"] == ("baseline",)


def test_adaptive_revision_nested_mapping_is_frozen_and_json_serializable():
    revision = AdaptiveRevision(
        revision_id="revision-1",
        authority_id="authority-1",
        overlay={
            "profiles": {
                "coding": {
                    "primary": "runtime-1",
                    "fallbacks": ["runtime-2"],
                }
            }
        },
        explanation={"summary": "baseline", "reason_codes": ["baseline"]},
        created_at="2026-07-15T12:00:00Z",
        is_baseline=True,
    )

    with pytest.raises(TypeError):
        revision.overlay["profiles"]["coding"]["primary"] = "runtime-2"

    dumped = revision.model_dump(mode="json")
    assert dumped["overlay"] == {
        "profiles": {
            "coding": {
                "fallbacks": ["runtime-2"],
                "primary": "runtime-1",
            }
        }
    }
    reordered = AdaptiveRevision(
        revision_id="revision-1",
        authority_id="authority-1",
        overlay={
            "profiles": {
                "coding": {
                    "fallbacks": ["runtime-2"],
                    "primary": "runtime-1",
                }
            }
        },
        explanation={"reason_codes": ["baseline"], "summary": "baseline"},
        created_at="2026-07-15T12:00:00Z",
        is_baseline=True,
    )
    assert json.loads(revision.model_dump_json()) == dumped
    assert revision.model_dump_json() == reordered.model_dump_json()


def test_adaptive_revision_nested_sequence_is_frozen():
    revision = AdaptiveRevision(
        revision_id="revision-1",
        authority_id="authority-1",
        overlay={},
        explanation={"summary": "baseline", "reason_codes": ["baseline"]},
        created_at="2026-07-15T12:00:00Z",
        is_baseline=True,
    )

    with pytest.raises(AttributeError):
        revision.explanation["reason_codes"].append("mutated")


@pytest.mark.parametrize(
    "model_type",
    [
        AutoRoutingConfig,
        PolicyEnvelope,
        RuntimeKey,
        AccessEconomics,
        RoutingTarget,
        RouteProfile,
        ComplexityBands,
        RoutingVocabulary,
        RoutingRule,
        TaskFacts,
        TaskAssessment,
        RoutingDecision,
        RuntimeObservation,
        CatalogEvidence,
        AdaptiveRevision,
    ],
)
def test_every_stable_record_is_frozen_and_forbids_extra_fields(model_type):
    assert model_type.model_config["frozen"] is True
    assert model_type.model_config["extra"] == "forbid"


def test_frozen_records_cannot_be_reassigned(valid_root):
    parsed = parse_config(valid_root)

    with pytest.raises(ValidationError, match="frozen"):
        parsed.activation = parsed.activation
    with pytest.raises(ValidationError, match="frozen"):
        parsed.profiles["coding"].primary.runtime.model = "different"


def test_frozen_config_mappings_cannot_be_mutated(valid_root):
    parsed = parse_config(valid_root)

    with pytest.raises(TypeError):
        parsed.profiles["other"] = parsed.profiles["coding"]
    with pytest.raises(TypeError):
        parsed.economics_overrides["0" * 64] = _access_economics()


def test_frozen_config_mappings_remain_json_serializable(valid_root):
    dumped = parse_config(valid_root).model_dump(mode="json", by_alias=True)

    assert dumped["profiles"]["coding"]["primary"]["reasoning"] == {
        "default": "medium",
        "min": "low",
        "max": "high",
    }
    assert dumped["economics_overrides"] == {}


def test_identifiers_and_canonical_timestamps_must_be_non_empty():
    with pytest.raises(ValidationError, match="provider"):
        RuntimeKey(
            provider="",
            model="gpt-5.4",
            auth_identity="subscription:default",
            api_mode="codex_responses",
            inventory_revision="inventory-1",
        )

    with pytest.raises(ValidationError, match="created_at"):
        AdaptiveRevision(
            revision_id="revision-1",
            authority_id="authority-1",
            parent_revision_id=None,
            overlay={},
            explanation={},
            created_at="",
            is_baseline=True,
        )


def test_parse_config_requires_the_exact_plugin_subtree():
    with pytest.raises(ConfigError, match=r"plugins\.entries\.auto-routing"):
        parse_config({"plugins": {"entries": {}}})


def test_parse_config_rejects_unknown_authority_fields(valid_root):
    _plugin(valid_root)["policy"]["learner_can_raise_spend"] = True

    with pytest.raises(ConfigError, match="learner_can_raise_spend"):
        parse_config(valid_root)


def test_config_errors_name_secret_fields_without_echoing_secret_values(valid_root):
    runtime_id = parse_config(valid_root).profiles["coding"].primary.runtime.stable_id()
    _plugin(valid_root)["economics_overrides"][runtime_id] = {
        "billing_kind": "subscription",
        "source_id": "user-configured",
        "provenance": "config_override",
        "observed_at": "2026-07-15T12:00:00Z",
        "api_key": "must-not-appear-in-errors",
    }

    with pytest.raises(ConfigError) as raised:
        parse_config(valid_root)

    assert "api_key" in str(raised.value)
    assert "must-not-appear-in-errors" not in str(raised.value)
    assert raised.value.__cause__ is None
