"""Stable immutable records for the auto-routing domain."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)


ActivationMode: TypeAlias = Literal["off", "shadow", "active"]
InventoryState: TypeAlias = Literal[
    "verified",
    "configured_unverified",
    "temporarily_unavailable",
    "ineligible",
]
ReasoningEffort: TypeAlias = Literal[
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
]
BillingKind: TypeAlias = Literal["metered", "subscription", "local"]
EvidenceSource: TypeAlias = Literal["hermes_turn_outcome", "user_feedback"]
EvidenceSignalType: TypeAlias = Literal[
    "objective_outcome",
    "explicit_feedback",
    "operational",
]
EvidenceFeedbackValue: TypeAlias = Literal[
    "rating-1",
    "rating-2",
    "rating-3",
    "rating-4",
    "rating-5",
    "rejected",
    "corrected",
    "manual-reroute",
]
EvidenceOutcome: TypeAlias = Literal[
    "verified",
    "completed_unverified",
    "partial",
    "blocked",
    "failed",
    "interrupted",
    "unresolved",
    "cancelled",
]
MAX_CATALOG_SAMPLE_SIZE = 1_000_000_000
MAX_DECISION_CANDIDATES = 1_024
MAX_SCORE_COMPONENTS = 64
MAX_REASON_CODES = 64
MAX_TASK_INDEX = 1_000_000
MAX_PROFILE_ID_LENGTH = 256
MAX_PROFILE_FALLBACKS = 64
MAX_ROUTING_RULES = 64


def _validate_canonical_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError("timestamp must be canonical UTC ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be canonical UTC ISO-8601")
    return value


NonEmptyString: TypeAlias = Annotated[str, StringConstraints(min_length=1)]
FiniteFloat: TypeAlias = Annotated[
    float,
    Field(allow_inf_nan=False, strict=True),
]
NonNegativeFloat: TypeAlias = Annotated[
    float,
    Field(ge=0, allow_inf_nan=False, strict=True),
]
UnitFloat: TypeAlias = Annotated[
    float,
    Field(ge=0, le=1, allow_inf_nan=False, strict=True),
]
StrictUnitFloat: TypeAlias = Annotated[
    float,
    Field(ge=0, le=1, allow_inf_nan=False, strict=True),
]
NonNegativeInt: TypeAlias = Annotated[int, Field(ge=0, strict=True)]
StrictNonNegativeInt: TypeAlias = Annotated[int, Field(ge=0, strict=True)]
PositiveInt: TypeAlias = Annotated[int, Field(gt=0, strict=True)]
BoundedTokenCount: TypeAlias = Annotated[
    int,
    Field(ge=0, le=10_000_000, strict=True),
]
AuthorityLabel: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
    ),
]
ProfileIdentifier: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=MAX_PROFILE_ID_LENGTH,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]
BoundedLabels: TypeAlias = Annotated[
    tuple[AuthorityLabel, ...],
    Field(max_length=64),
]
RuntimeStableId: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
DurableIdentifier: TypeAlias = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9_.:/@+\-]+$",
    ),
]
CanonicalTimestamp: TypeAlias = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
            r"(?:\.\d{1,6})?Z$"
        ),
    ),
    AfterValidator(_validate_canonical_timestamp),
]
ScoreComponent: TypeAlias = AuthorityLabel
CandidateReasonCode: TypeAlias = Literal[
    "configured_provider_source_not_allowed",
    "context_capacity_insufficient",
    "context_capacity_unproven",
    "estimated_cost_exceeds_limit",
    "estimated_cost_unknown",
    "estimated_latency_exceeds_limit",
    "estimated_latency_unknown",
    "hardware_compatibility_unproven",
    "license_not_allowed",
    "local_source_not_allowed",
    "missing_tools",
    "moa_excluded",
    "model_denied_by_policy",
    "open_weights_unproven",
    "output_capacity_insufficient",
    "output_capacity_unproven",
    "provider_denied_by_policy",
    "reasoning_out_of_bounds",
    "reasoning_unsupported",
    "required_capability_unproven",
    "required_capability_unsupported",
    "required_modality_unproven",
    "required_modality_unsupported",
    "runtime_not_in_inventory",
    "runtime_not_verified",
    "runtime_resolution_failed",
    "runtime_throttled",
    "runtime_unavailable",
    "runtime_verification_expired",
    "safe_default_unavailable",
    "subscription_access_disallowed",
    "subscription_quota_exhausted",
]
SelectionReasonCode: TypeAlias = Literal[
    "baseline_inherit",
    "classifier_failed",
    "highest_eligible_score",
    "no_eligible_runtime",
    "pinned_profile",
    "pre_call_fallback",
    "preferred_profile",
    "recorded_replay",
    "rule",
    "safe_default",
]
SafeDefaultReasonCode: TypeAlias = Literal[
    "classifier_budget",
    "classifier_failed",
    "classifier_malformed",
    "classifier_oversized",
    "classifier_timeout",
    "classifier_trust",
    "classifier_unknown_price",
    "no_eligible_runtime",
    "rule_incomplete",
    "rule_invalid",
    "safe_default_unavailable",
]
ClassifierFailureReasonCode: TypeAlias = Literal[
    "classifier_budget",
    "classifier_failed",
    "classifier_malformed",
    "classifier_oversized",
    "classifier_timeout",
    "classifier_trust",
    "classifier_unknown_price",
]
DegradationReasonCode: TypeAlias = Literal[
    "baseline_inherit",
    "fallback_selected",
    "no_eligible_runtime",
    "plugin_state_unavailable",
    "pre_call_fallback",
    "primary_unavailable",
    "recorded_route_unavailable",
    "resolution_failed",
    "safe_default_selected",
]

MAX_CLASSIFIER_INPUT_TOKENS = 10_000_000
MAX_CLASSIFIER_OUTPUT_TOKENS = 1_000_000
MAX_CLASSIFIER_IMAGE_COUNT = 256
MAX_CLASSIFIER_IMAGE_BYTES = 1_000_000_000
DEFAULT_ROUTING_CAPABILITIES: tuple[str, ...] = (
    "tools",
    "long_context",
    "code_execution",
    "structured_output",
    "web_search",
    "agentic_reasoning",
    "batch_reasoning",
)
DEFAULT_ROUTING_MODALITIES: tuple[str, ...] = (
    "text",
    "image",
    "audio",
    "video",
)
COMPLEXITY_LABELS = frozenset({"trivial", "easy", "moderate", "hard", "extreme"})

REASONING_EFFORT_ORDER: tuple[ReasoningEffort, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)


class FrozenModel(BaseModel):
    """Shared immutable, closed-world Pydantic configuration."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
    )


class EvidenceContextBucket(FrozenModel):
    """Content-free task context used to group immutable evidence."""

    bucket_id: RuntimeStableId
    complexity_band: Literal["trivial", "easy", "moderate", "hard", "extreme"]
    domains: BoundedLabels
    required_capabilities: BoundedLabels
    required_modalities: BoundedLabels
    risk_class: Literal["low", "moderate", "high", "critical"]


class EvidenceEvent(FrozenModel):
    """One immutable routed-turn outcome or explicit feedback observation."""

    evidence_id: RuntimeStableId
    source: EvidenceSource
    signal_type: EvidenceSignalType
    parent_evidence_id: RuntimeStableId | None = None
    decision_id: DurableIdentifier
    session_id: DurableIdentifier
    turn_id: DurableIdentifier
    task_id: DurableIdentifier
    route_epoch_id: DurableIdentifier
    runtime_id: RuntimeStableId
    profile_id: ProfileIdentifier | None
    reasoning_effort: ReasoningEffort
    context_bucket: EvidenceContextBucket | None
    is_initial_routing_task: Annotated[bool, Field(strict=True)]
    outcome: EvidenceOutcome | None = None
    feedback_value: EvidenceFeedbackValue | None = None
    normalized_value: StrictUnitFloat | None
    confidence_weight: StrictUnitFloat
    attribution_confidence: StrictUnitFloat
    api_calls: StrictNonNegativeInt
    tool_iterations: StrictNonNegativeInt
    retry_count: StrictNonNegativeInt
    cost_usd: NonNegativeFloat
    input_tokens: BoundedTokenCount
    output_tokens: BoundedTokenCount
    cache_read_tokens: BoundedTokenCount
    latency_seconds: NonNegativeFloat | None = None
    observed_at: CanonicalTimestamp

    @model_validator(mode="after")
    def require_source_shape(self) -> "EvidenceEvent":
        if self.source == "hermes_turn_outcome":
            if self.outcome is None or self.feedback_value is not None:
                raise ValueError("turn evidence requires outcome only")
            if self.parent_evidence_id is not None:
                raise ValueError("turn evidence cannot have a parent")
        else:
            if self.parent_evidence_id is None or self.feedback_value is None:
                raise ValueError("feedback evidence requires parent and value")
            if self.outcome is not None:
                raise ValueError("feedback evidence cannot carry an outcome")
            if any(
                (
                    self.api_calls,
                    self.tool_iterations,
                    self.retry_count,
                    self.input_tokens,
                    self.output_tokens,
                    self.cache_read_tokens,
                )
            ):
                raise ValueError("feedback cannot duplicate operational counters")
            if self.cost_usd != 0.0 or self.latency_seconds is not None:
                raise ValueError("feedback cannot duplicate operational totals")
        if not self.is_initial_routing_task and self.context_bucket is not None:
            raise ValueError("continuation evidence cannot carry initial-task context")
        return self


class RuntimeKey(FrozenModel):
    """Non-secret identity of one exact executable access path."""

    provider: NonEmptyString
    model: NonEmptyString
    auth_identity: NonEmptyString
    credential_pool_identity: str = ""
    endpoint_identity: str = ""
    api_mode: NonEmptyString
    local_backend: str = ""
    inventory_revision: NonEmptyString

    def stable_id(self) -> str:
        """Hash stable access identity while ignoring inventory freshness."""
        payload = json.dumps(
            self.model_dump(exclude={"inventory_revision"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class AccessEconomics(FrozenModel):
    """Cost and capacity evidence for one billing/access path."""

    billing_kind: BillingKind
    metered_input_usd_per_million_tokens: NonNegativeFloat | None = None
    metered_output_usd_per_million_tokens: NonNegativeFloat | None = None
    effective_marginal_cost_usd_per_task: NonNegativeFloat | None = None
    effective_amortized_cost_usd_per_task: NonNegativeFloat | None = None
    subscription_plan: NonEmptyString | None = None
    subscription_quota_limit: NonNegativeFloat | None = None
    subscription_quota_remaining: NonNegativeFloat | None = None
    subscription_quota_unit: NonEmptyString | None = None
    subscription_reset_at: NonEmptyString | None = None
    subscription_state: NonEmptyString | None = None
    local_energy_cost_usd_per_task: NonNegativeFloat | None = None
    local_compute_cost_usd_per_task: NonNegativeFloat | None = None
    throttle_state: NonEmptyString | None = None
    cooldown_until: NonEmptyString | None = None
    source_id: NonEmptyString
    evidence_ttl_seconds: PositiveInt | None = None
    provenance: NonEmptyString
    confidence: UnitFloat | None = None
    observed_at: NonEmptyString

    @model_validator(mode="after")
    def reject_cross_path_metered_pricing(self) -> "AccessEconomics":
        """Keep public token prices exclusive to their metered path."""
        if self.billing_kind != "metered" and (
            self.metered_input_usd_per_million_tokens is not None
            or self.metered_output_usd_per_million_tokens is not None
        ):
            raise ValueError(
                "metered pricing cannot be attached to subscription or local paths"
            )
        subscription_values = (
            self.subscription_plan,
            self.subscription_quota_limit,
            self.subscription_quota_remaining,
            self.subscription_quota_unit,
            self.subscription_reset_at,
            self.subscription_state,
        )
        local_values = (
            self.local_energy_cost_usd_per_task,
            self.local_compute_cost_usd_per_task,
        )
        if self.billing_kind != "subscription" and any(
            value is not None for value in subscription_values
        ):
            raise ValueError(
                "billing-specific economics must match the subscription access kind"
            )
        if self.billing_kind != "local" and any(
            value is not None for value in local_values
        ):
            raise ValueError(
                "billing-specific economics must match the local access kind"
            )
        return self


class ReasoningBounds(FrozenModel):
    """User-approved generic reasoning default and inclusive bounds."""

    default: ReasoningEffort
    minimum: ReasoningEffort = Field(alias="min")
    maximum: ReasoningEffort = Field(alias="max")

    @model_validator(mode="after")
    def validate_canonical_order(self) -> "ReasoningBounds":
        positions = {
            effort: index for index, effort in enumerate(REASONING_EFFORT_ORDER)
        }
        if not (
            positions[self.minimum]
            <= positions[self.default]
            <= positions[self.maximum]
        ):
            raise ValueError("reasoning bounds require minimum <= default <= maximum")
        return self


class RoutingTarget(FrozenModel):
    """One requested runtime and its approved reasoning/limit policy."""

    runtime: RuntimeKey
    reasoning: ReasoningBounds
    supported_reasoning_efforts: tuple[ReasoningEffort, ...]
    max_estimated_task_cost_usd: NonNegativeFloat | None = None
    max_estimated_latency_seconds: NonNegativeFloat | None = None
    revision_status: Literal["active", "fallback", "challenger"]


class ObjectiveWeights(FrozenModel):
    """Complete normalized profile scoring priorities."""

    quality: float
    reliability: float
    latency: float
    cost: float

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        keys = ("quality", "reliability", "latency", "cost")
        missing = [key for key in keys if key not in data]
        if missing:
            raise ValueError(f"missing objective weights: {', '.join(missing)}")
        try:
            values = [float(data[key]) for key in keys]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "objective weights must be finite, non-negative, and sum above zero"
            ) from exc
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError(
                "objective weights must be finite, non-negative, and sum above zero"
            )
        total = sum(values)
        if total <= 0:
            raise ValueError(
                "objective weights must be finite, non-negative, and sum above zero"
            )
        if math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-12):
            return {**data, **dict(zip(keys, values, strict=True))}
        return {
            **data,
            **{key: float(data[key]) / total for key in keys},
        }


class ProfileMatch(FrozenModel):
    """Non-exclusive task affinities for a route profile."""

    domains: BoundedLabels
    complexity: Annotated[tuple[AuthorityLabel, ...], Field(max_length=5)]
    modalities: BoundedLabels
    capabilities: BoundedLabels

    @field_validator(
        "domains", "complexity", "modalities", "capabilities", mode="before"
    )
    @classmethod
    def normalize_match_labels(cls, value: Any) -> Any:
        return _normalize_labels(value)


class ProfileLimits(FrozenModel):
    """Optional profile restrictions that may only tighten global policy."""

    max_estimated_task_cost_usd: NonNegativeFloat | None = None
    max_estimated_latency_seconds: NonNegativeFloat | None = None
    max_reasoning_effort: ReasoningEffort | None = None
    allowed_licenses: tuple[NonEmptyString, ...] | None = None
    minimum_context_tokens: NonNegativeInt | None = None
    canary_high_risk_tasks: bool | None = None


class ProfileAdaptationSettings(FrozenModel):
    """Explicit profile-local authority for conservative adaptation."""

    enabled: bool = False
    canary_fraction: UnitFloat = 0.05
    minimum_comparable_samples: Annotated[
        int,
        Field(ge=20, le=10_000, strict=True),
    ] = 20
    observed_regression_threshold: UnitFloat = 0.10
    cooldown_base_seconds: Annotated[
        int,
        Field(ge=60, le=2_592_000, strict=True),
    ] = 3_600
    cooldown_max_seconds: Annotated[
        int,
        Field(ge=60, le=31_536_000, strict=True),
    ] = 86_400
    confidence_level: Annotated[
        float,
        Field(ge=0.80, le=0.99, allow_inf_nan=False, strict=True),
    ] = 0.90

    @model_validator(mode="after")
    def require_ordered_cooldown_bounds(self) -> "ProfileAdaptationSettings":
        if self.cooldown_base_seconds > self.cooldown_max_seconds:
            raise ValueError("cooldown_base_seconds cannot exceed cooldown_max_seconds")
        return self


class RankingPackTrust(FrozenModel):
    """Configured local ranking-pack location and verification keys."""

    ranking_pack_path: NonEmptyString
    trusted_ed25519_public_keys: Annotated[
        tuple[NonEmptyString, ...],
        Field(min_length=1, max_length=8),
    ]

    @model_validator(mode="after")
    def require_unique_public_keys(self) -> "RankingPackTrust":
        if len(self.trusted_ed25519_public_keys) != len(
            set(self.trusted_ed25519_public_keys)
        ):
            raise ValueError("ranking-pack public keys must be unique")
        return self


class AutonomousProfileManagementSettings(FrozenModel):
    """Opt-in authority for local autonomous profile management only."""

    enabled: bool = False
    ranking_pack: RankingPackTrust | None = None
    daily_change_limit: Annotated[int, Field(ge=1, le=10, strict=True)] = 1
    schedule: NonEmptyString = "17 */6 * * *"
    canary_fraction: Annotated[
        float,
        Field(gt=0.0, le=1.0, allow_inf_nan=False, strict=True),
    ] = 0.05
    minimum_comparable_samples: Annotated[
        int,
        Field(ge=20, le=10_000, strict=True),
    ] = 20
    observed_regression_threshold: UnitFloat = 0.10
    cooldown_base_seconds: Annotated[
        int,
        Field(ge=60, le=2_592_000, strict=True),
    ] = 3_600
    cooldown_max_seconds: Annotated[
        int,
        Field(ge=60, le=31_536_000, strict=True),
    ] = 86_400
    confidence_level: Annotated[
        float,
        Field(ge=0.80, le=0.99, allow_inf_nan=False, strict=True),
    ] = 0.90

    @model_validator(mode="after")
    def require_trust_when_enabled(self) -> "AutonomousProfileManagementSettings":
        if self.enabled and self.ranking_pack is None:
            raise ValueError(
                "enabled autonomous profile management requires ranking_pack trust"
            )
        return self

    @model_validator(mode="after")
    def require_ordered_cooldown_bounds(
        self,
    ) -> "AutonomousProfileManagementSettings":
        if self.cooldown_base_seconds > self.cooldown_max_seconds:
            raise ValueError("cooldown_base_seconds cannot exceed cooldown_max_seconds")
        return self


class RouteProfile(FrozenModel):
    """Named baseline route policy with ordered executable targets."""

    profile_id: ProfileIdentifier
    description: NonEmptyString
    base_rank: FiniteFloat | None = None
    match: ProfileMatch
    objectives: ObjectiveWeights
    limits: ProfileLimits | None = None
    primary: RoutingTarget
    primary_challengers: Annotated[
        tuple[RoutingTarget, ...],
        Field(max_length=MAX_PROFILE_FALLBACKS),
    ] = ()
    fallbacks: Annotated[
        tuple[RoutingTarget, ...],
        Field(max_length=MAX_PROFILE_FALLBACKS),
    ]
    adaptation: ProfileAdaptationSettings = Field(
        default_factory=ProfileAdaptationSettings
    )
    provenance: tuple[NonEmptyString, ...]

    def primary_choices(self) -> tuple[RoutingTarget, ...]:
        """Return the user-approved primary and challengers in authority order."""
        return (self.primary, *self.primary_challengers)

    @model_validator(mode="after")
    def validate_primary_choice_authority(self) -> "RouteProfile":
        primary_ids = tuple(
            target.runtime.stable_id() for target in self.primary_choices()
        )
        if len(primary_ids) != len(set(primary_ids)):
            raise ValueError("primary choices must have unique runtime identities")
        if any(
            target.revision_status != "challenger"
            for target in self.primary_challengers
        ):
            raise ValueError(
                "primary challenger revision_status must be challenger"
            )
        fallback_ids = {
            target.runtime.stable_id() for target in self.fallbacks
        }
        if fallback_ids.intersection(primary_ids):
            raise ValueError("primary choices and fallbacks must not overlap")
        if self.adaptation.enabled and not self.primary_challengers:
            raise ValueError("enabled profile adaptation requires a challenger")
        return self


class LocalModelRequirements(FrozenModel):
    """Immutable local-runtime eligibility policy."""

    require_open_weights: bool
    require_compatible_hardware: bool


class PolicyEnvelope(FrozenModel):
    """Immutable limits the adaptive learner can never relax."""

    eligible_sources: tuple[
        Literal["configured_providers", "installed_local_models"],
        ...,
    ]
    uninstalled_local_models: Literal["deny"]
    local_models: LocalModelRequirements
    denied_providers: tuple[NonEmptyString, ...]
    denied_models: tuple[NonEmptyString, ...]
    max_estimated_task_cost_usd: NonNegativeFloat
    max_estimated_latency_seconds: NonNegativeFloat
    max_routing_overhead_usd_per_day: NonNegativeFloat
    max_experiment_cost_usd_per_day: NonNegativeFloat
    max_evaluator_calls_per_day: NonNegativeInt
    max_canary_fraction: UnitFloat
    max_reasoning_effort: ReasoningEffort
    allow_subscription: bool
    allow_paid_access_probes: bool
    allowed_licenses: tuple[NonEmptyString, ...]
    minimum_context_tokens: NonNegativeInt
    canary_high_risk_tasks: bool


class PluginLlmAuthority(FrozenModel):
    """Immutable classifier/evaluator provider and model trust."""

    allow_provider_override: bool
    allowed_providers: tuple[NonEmptyString, ...]
    allow_model_override: bool
    allowed_models: tuple[NonEmptyString, ...]


class ActivationSettings(FrozenModel):
    """Requested plugin activation mode."""

    mode: ActivationMode


class RoutingScopes(FrozenModel):
    """Cache-safe boundaries that the operator has enabled."""

    fresh_sessions: bool
    delegation: bool


class RulePredicate(FrozenModel):
    """Provider-independent facts that make a deterministic rule applicable."""

    scopes: Annotated[
        tuple[Literal["fresh_session", "delegation"], ...],
        Field(max_length=2),
    ] = ()
    platforms: BoundedLabels = ()
    domains_any: BoundedLabels = ()
    required_capabilities_all: BoundedLabels = ()
    required_modalities_any: BoundedLabels = ()
    risk_classes: Annotated[
        tuple[Literal["low", "moderate", "high", "critical"], ...],
        Field(max_length=4),
    ] = ()
    minimum_complexity: StrictUnitFloat | None = None
    maximum_complexity: StrictUnitFloat | None = None

    @field_validator(
        "platforms",
        "domains_any",
        "required_capabilities_all",
        "required_modalities_any",
        mode="before",
    )
    @classmethod
    def normalize_predicate_labels(cls, value: Any) -> Any:
        return _normalize_labels(value)

    @model_validator(mode="after")
    def validate_complexity_range(self) -> "RulePredicate":
        if (
            self.minimum_complexity is not None
            and self.maximum_complexity is not None
            and self.minimum_complexity > self.maximum_complexity
        ):
            raise ValueError("minimum_complexity cannot exceed maximum_complexity")
        return self


class RuleAssessmentOverrides(FrozenModel):
    """Optional deterministic replacements for classifier assessment fields."""

    complexity: StrictUnitFloat | None = None
    domains: BoundedLabels | None = None
    required_capabilities: BoundedLabels | None = None
    required_modalities: BoundedLabels | None = None
    expected_context_tokens: BoundedTokenCount | None = None
    expected_output_tokens: BoundedTokenCount | None = None
    quality_sensitivity: StrictUnitFloat | None = None
    reliability_sensitivity: StrictUnitFloat | None = None
    latency_sensitivity: StrictUnitFloat | None = None
    cost_sensitivity: StrictUnitFloat | None = None
    risk_class: Literal["low", "moderate", "high", "critical"] | None = None
    confidence: StrictUnitFloat | None = None

    @field_validator(
        "domains",
        "required_capabilities",
        "required_modalities",
        mode="before",
    )
    @classmethod
    def normalize_override_labels(cls, value: Any) -> Any:
        return None if value is None else _normalize_labels(value)


class RoutingRule(FrozenModel):
    """One stable user-owned route rule."""

    rule_id: AuthorityLabel
    priority: Annotated[int, Field(ge=-1_000_000, le=1_000_000, strict=True)]
    profile_id: ProfileIdentifier
    effect: Literal["pin_profile", "prefer_profile"]
    when: RulePredicate
    assessment_overrides: RuleAssessmentOverrides = Field(
        default_factory=RuleAssessmentOverrides
    )

    @field_validator("rule_id", mode="before")
    @classmethod
    def normalize_rule_identity(cls, value: Any) -> Any:
        return _normalize_label(value)


class TaskFacts(FrozenModel):
    """Deterministic content-free facts available before classification."""

    scope: Literal["fresh_session", "delegation"]
    platform: AuthorityLabel
    domains: BoundedLabels
    required_capabilities: BoundedLabels
    required_modalities: BoundedLabels
    risk_class: Literal["low", "moderate", "high", "critical"] | None = None
    complexity: StrictUnitFloat | None = None

    @field_validator(
        "platform",
        "domains",
        "required_capabilities",
        "required_modalities",
        mode="before",
    )
    @classmethod
    def normalize_fact_labels(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _normalize_label(value)
        return tuple(sorted(_normalize_labels(value)))


class ComplexityBands(FrozenModel):
    """Authority-owned numeric boundaries for named complexity bands."""

    trivial_max: StrictUnitFloat = 0.2
    easy_max: StrictUnitFloat = 0.4
    moderate_max: StrictUnitFloat = 0.7
    hard_max: StrictUnitFloat = 0.9

    @model_validator(mode="after")
    def require_strict_order(self) -> "ComplexityBands":
        if not (self.trivial_max < self.easy_max < self.moderate_max < self.hard_max):
            raise ValueError(
                "complexity bands require trivial_max < easy_max < "
                "moderate_max < hard_max"
            )
        return self

    def label(self, complexity: float) -> str:
        """Return the configured name for a validated unit complexity."""
        value = float(complexity)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("complexity must be finite and between zero and one")
        if value <= self.trivial_max:
            return "trivial"
        if value <= self.easy_max:
            return "easy"
        if value <= self.moderate_max:
            return "moderate"
        if value <= self.hard_max:
            return "hard"
        return "extreme"


class RoutingVocabulary(FrozenModel):
    """Bounded authority vocabulary understood by rules and assessments."""

    capabilities: Annotated[
        tuple[AuthorityLabel, ...], Field(min_length=1, max_length=64)
    ] = DEFAULT_ROUTING_CAPABILITIES
    modalities: Annotated[
        tuple[AuthorityLabel, ...], Field(min_length=1, max_length=64)
    ] = DEFAULT_ROUTING_MODALITIES

    @field_validator("capabilities", "modalities", mode="before")
    @classmethod
    def normalize_vocabulary(cls, value: Any) -> Any:
        return _normalize_labels(value)


class ClassifierSettings(FrozenModel):
    """Structured-classifier runtime settings inside plugin LLM trust."""

    provider: NonEmptyString
    model: NonEmptyString
    reasoning_effort: ReasoningEffort
    timeout_seconds: Annotated[
        float,
        Field(gt=0, allow_inf_nan=False, strict=True),
    ]
    disclosure: Literal["full"]
    maximum_input_tokens: Annotated[
        int,
        Field(gt=0, le=MAX_CLASSIFIER_INPUT_TOKENS, strict=True),
    ] = 8_192
    maximum_output_tokens: Annotated[
        int,
        Field(gt=0, le=MAX_CLASSIFIER_OUTPUT_TOKENS, strict=True),
    ] = 1_024
    maximum_image_count: Annotated[
        int,
        Field(gt=0, le=MAX_CLASSIFIER_IMAGE_COUNT, strict=True),
    ] = 4
    maximum_image_bytes: Annotated[
        int,
        Field(gt=0, le=MAX_CLASSIFIER_IMAGE_BYTES, strict=True),
    ] = 20_000_000


class AdaptationSettings(FrozenModel):
    """User-approved adaptive controls, separate from immutable limits."""

    enabled: bool
    mode: Literal["autonomous"]
    canary_fraction: UnitFloat
    minimum_canary_samples: NonNegativeInt
    rollback_threshold: UnitFloat


def candidate_id_for(
    profile_id: str,
    target_role: str,
    target_ordinal: int,
    runtime_id: str,
) -> str:
    """Return the stable identity of one profile/target/runtime evaluation."""
    document = json.dumps(
        [profile_id, target_role, target_ordinal, runtime_id],
        separators=(",", ":"),
    )
    return hashlib.sha256(document.encode()).hexdigest()


class DecisionCandidate(FrozenModel):
    """One immutable candidate evaluation within a routing decision."""

    candidate_id: RuntimeStableId
    profile_id: ProfileIdentifier
    target_role: Literal["primary", "fallback", "safe_default"]
    target_ordinal: Annotated[
        int,
        Field(ge=0, lt=MAX_DECISION_CANDIDATES, strict=True),
    ]
    runtime_id: RuntimeStableId
    eligible: Annotated[bool, Field(strict=True)]
    reason_codes: Annotated[
        tuple[CandidateReasonCode, ...],
        Field(max_length=MAX_REASON_CODES),
    ]
    normalized_scoring_inputs: Annotated[
        tuple[tuple[ScoreComponent, FiniteFloat], ...],
        Field(max_length=MAX_SCORE_COMPONENTS),
    ]
    final_score: FiniteFloat | None = None

    @model_validator(mode="after")
    def require_stable_candidate_id(self) -> "DecisionCandidate":
        expected = candidate_id_for(
            self.profile_id,
            self.target_role,
            self.target_ordinal,
            self.runtime_id,
        )
        if self.candidate_id != expected:
            raise ValueError("candidate_id does not cover its stable target identity")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("candidate reason_codes must not contain duplicates")
        component_names = tuple(name for name, _value in self.normalized_scoring_inputs)
        if len(component_names) != len(set(component_names)):
            raise ValueError(
                "candidate scoring inputs must not contain duplicate components"
            )
        if self.eligible and self.reason_codes:
            raise ValueError("eligible candidates cannot carry rejection reason codes")
        if not self.eligible and not self.reason_codes:
            raise ValueError("ineligible candidates require rejection reason codes")
        return self


class AutoRoutingConfig(FrozenModel):
    """Complete user-owned auto-routing authority subtree."""

    llm: PluginLlmAuthority
    activation: ActivationSettings
    scopes: RoutingScopes
    classifier: ClassifierSettings
    safe_default: Literal["inherit"] | RoutingTarget
    policy: PolicyEnvelope
    adaptation: AdaptationSettings
    autonomous_profile_management: AutonomousProfileManagementSettings = Field(
        default_factory=AutonomousProfileManagementSettings
    )
    rules: Annotated[
        tuple[RoutingRule, ...],
        Field(max_length=MAX_ROUTING_RULES),
    ] = ()
    complexity_bands: ComplexityBands = Field(default_factory=ComplexityBands)
    routing_vocabulary: RoutingVocabulary = Field(default_factory=RoutingVocabulary)
    profiles: Mapping[ProfileIdentifier, RouteProfile] = Field(
        min_length=1,
        max_length=MAX_DECISION_CANDIDATES,
    )
    economics_overrides: Mapping[NonEmptyString, AccessEconomics] = Field(
        default_factory=dict
    )

    @field_validator("profiles", "economics_overrides")
    @classmethod
    def freeze_keyed_authority(cls, value: Mapping[Any, Any]) -> Mapping[Any, Any]:
        return MappingProxyType(dict(value))

    @field_serializer("profiles", "economics_overrides")
    def serialize_keyed_authority(self, value: Mapping[Any, Any]) -> dict[Any, Any]:
        return dict(value)

    @model_validator(mode="after")
    def validate_authority_invariants(self) -> "AutoRoutingConfig":
        if self.adaptation.canary_fraction > self.policy.max_canary_fraction:
            raise ValueError(
                "adaptation canary_fraction cannot exceed policy max_canary_fraction"
            )

        self._validate_classifier_trust()

        profile_ids = tuple(profile.profile_id for profile in self.profiles.values())
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("profiles contain a duplicate effective profile_id")
        mismatched_profiles = tuple(
            (mapping_key, profile.profile_id)
            for mapping_key, profile in self.profiles.items()
            if mapping_key != profile.profile_id
        )
        if mismatched_profiles:
            mapping_key, profile_id = mismatched_profiles[0]
            raise ValueError(
                "profile mapping key must match embedded profile_id: "
                f"{mapping_key!r} != {profile_id!r}"
            )
        profile_target_count = sum(
            len(profile.primary_choices()) + len(profile.fallbacks)
            for profile in self.profiles.values()
        )
        if profile_target_count > MAX_DECISION_CANDIDATES:
            raise ValueError(
                "profile targets cannot exceed the durable candidate bundle "
                f"boundary of {MAX_DECISION_CANDIDATES}"
            )

        self._validate_rules_and_vocabulary()

        for runtime_id in self.economics_overrides:
            if len(runtime_id) != 64 or any(
                character not in "0123456789abcdef" for character in runtime_id
            ):
                raise ValueError(
                    "economics_overrides keys must be a 64-character stable runtime ID"
                )

        for profile in self.profiles.values():
            self._validate_profile(profile)

        if isinstance(self.safe_default, RoutingTarget):
            self._validate_target(
                self.safe_default,
                max_cost=self.policy.max_estimated_task_cost_usd,
                max_latency=self.policy.max_estimated_latency_seconds,
                max_reasoning=self.policy.max_reasoning_effort,
                target_location="safe_default",
            )
        return self

    def _validate_rules_and_vocabulary(self) -> None:
        rule_ids = tuple(rule.rule_id for rule in self.rules)
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rules contain a duplicate rule_id")
        canonical_rules = tuple(
            sorted(self.rules, key=lambda rule: (-rule.priority, rule.rule_id))
        )
        if self.rules != canonical_rules:
            object.__setattr__(self, "rules", canonical_rules)

        capabilities = set(self.routing_vocabulary.capabilities)
        modalities = set(self.routing_vocabulary.modalities)
        profile_ids = set(self.profiles)
        identity_sources = [
            (value, identity_kind)
            for profile in self.profiles.values()
            for target in (*profile.primary_choices(), *profile.fallbacks)
            for value, identity_kind in (
                (target.runtime.provider, "provider"),
                (target.runtime.model, "model"),
            )
        ]
        if isinstance(self.safe_default, RoutingTarget):
            identity_sources.extend((
                (self.safe_default.runtime.provider, "provider"),
                (self.safe_default.runtime.model, "model"),
            ))
        identity_sources.extend((
            (self.classifier.provider, "provider"),
            (self.classifier.model, "model"),
        ))
        identity_sources.extend(
            (value, "provider") for value in self.llm.allowed_providers if value != "*"
        )
        identity_sources.extend(
            (value, "model") for value in self.llm.allowed_models if value != "*"
        )
        identity_sources.extend(
            (value, "provider")
            for value in self.policy.denied_providers
            if value != "*"
        )
        identity_sources.extend(
            (value, "model") for value in self.policy.denied_models if value != "*"
        )
        runtime_identities = {
            identity
            for value, identity_kind in identity_sources
            for identity in _identity_variants(value, identity_kind)
        }
        vocabulary = capabilities | modalities
        collisions = sorted(
            label
            for label in vocabulary
            if any(
                _contains_identity(
                    label,
                    identity,
                    minimum_characters=1 if identity_kind == "model" else 4,
                )
                for identity, identity_kind in runtime_identities
            )
        )
        if collisions:
            raise ValueError(
                "routing_vocabulary labels cannot contain a provider or model "
                f"identity: {collisions[0]}"
            )

        for profile in self.profiles.values():
            unknown_complexity = set(profile.match.complexity) - COMPLEXITY_LABELS
            if unknown_complexity:
                raise ValueError(
                    f"profile {profile.profile_id} has unknown complexity label "
                    f"{sorted(unknown_complexity)[0]}"
                )
            self._require_declared_labels(
                profile.match.capabilities,
                capabilities,
                location=f"profile {profile.profile_id} capabilities",
            )
            self._require_declared_labels(
                profile.match.modalities,
                modalities,
                location=f"profile {profile.profile_id} modalities",
            )

        for rule in self.rules:
            if rule.profile_id not in profile_ids:
                raise ValueError(
                    f"rule {rule.rule_id} references missing profile {rule.profile_id}"
                )
            self._require_declared_labels(
                rule.when.required_capabilities_all,
                capabilities,
                location=f"rule {rule.rule_id} capabilities",
            )
            self._require_declared_labels(
                rule.when.required_modalities_any,
                modalities,
                location=f"rule {rule.rule_id} modalities",
            )
            overrides = rule.assessment_overrides
            self._require_declared_labels(
                overrides.required_capabilities or (),
                capabilities,
                location=f"rule {rule.rule_id} override capabilities",
            )
            self._require_declared_labels(
                overrides.required_modalities or (),
                modalities,
                location=f"rule {rule.rule_id} override modalities",
            )

    @staticmethod
    def _require_declared_labels(
        values: tuple[str, ...],
        declared: set[str],
        *,
        location: str,
    ) -> None:
        missing = set(values) - declared
        if missing:
            raise ValueError(
                f"{location} uses label outside routing_vocabulary: "
                f"{sorted(missing)[0]}"
            )

    def _validate_classifier_trust(self) -> None:
        trust_fields = (
            (
                "provider",
                self.classifier.provider,
                self.llm.allow_provider_override,
                self.llm.allowed_providers,
            ),
            (
                "model",
                self.classifier.model,
                self.llm.allow_model_override,
                self.llm.allowed_models,
            ),
        )
        for field, requested, override_allowed, allowlist in trust_fields:
            allow_override_field = f"allow_{field}_override"
            allowed_values_field = f"allowed_{field}s"
            if not override_allowed:
                raise ValueError(
                    f"classifier {field} requires llm {allow_override_field}"
                )
            normalized_allowlist = {value.strip().lower() for value in allowlist}
            if (
                "*" not in normalized_allowlist
                and requested.strip().lower() not in normalized_allowlist
            ):
                raise ValueError(
                    f"classifier {field} is not authorized by llm "
                    f"{allowed_values_field}"
                )

    def _validate_profile(self, profile: RouteProfile) -> None:
        limits = profile.limits
        max_cost = self.policy.max_estimated_task_cost_usd
        max_latency = self.policy.max_estimated_latency_seconds
        max_reasoning = self.policy.max_reasoning_effort

        if (
            profile.adaptation.enabled
            and profile.adaptation.canary_fraction > self.policy.max_canary_fraction
        ):
            raise ValueError(
                f"profile {profile.profile_id} adaptation canary_fraction "
                "cannot exceed policy max_canary_fraction"
            )

        if limits is not None:
            if (
                limits.max_estimated_task_cost_usd is not None
                and limits.max_estimated_task_cost_usd > max_cost
            ):
                raise ValueError(
                    f"profile {profile.profile_id} limits loosen global "
                    "max_estimated_task_cost_usd"
                )
            if (
                limits.max_estimated_latency_seconds is not None
                and limits.max_estimated_latency_seconds > max_latency
            ):
                raise ValueError(
                    f"profile {profile.profile_id} limits loosen global "
                    "max_estimated_latency_seconds"
                )
            if limits.max_reasoning_effort is not None and _effort_position(
                limits.max_reasoning_effort
            ) > _effort_position(max_reasoning):
                raise ValueError(
                    f"profile {profile.profile_id} limits loosen global "
                    "max_reasoning_effort"
                )
            if (
                limits.minimum_context_tokens is not None
                and limits.minimum_context_tokens < self.policy.minimum_context_tokens
            ):
                raise ValueError(
                    f"profile {profile.profile_id} limits loosen global "
                    "minimum_context_tokens"
                )
            if (
                limits.canary_high_risk_tasks is True
                and not self.policy.canary_high_risk_tasks
            ):
                raise ValueError(
                    f"profile {profile.profile_id} limits loosen global "
                    "canary_high_risk_tasks"
                )
            if (
                limits.allowed_licenses is not None
                and self.policy.allowed_licenses
                and not set(limits.allowed_licenses).issubset(
                    self.policy.allowed_licenses
                )
            ):
                raise ValueError(
                    f"profile {profile.profile_id} allowed_licenses loosen global "
                    "allowed_licenses"
                )

            if limits.max_estimated_task_cost_usd is not None:
                max_cost = limits.max_estimated_task_cost_usd
            if limits.max_estimated_latency_seconds is not None:
                max_latency = limits.max_estimated_latency_seconds
            if limits.max_reasoning_effort is not None:
                max_reasoning = limits.max_reasoning_effort

        for index, target in enumerate(profile.primary_choices()):
            location = "primary" if index == 0 else f"primary_challenger[{index - 1}]"
            self._validate_target(
                target,
                max_cost=max_cost,
                max_latency=max_latency,
                max_reasoning=max_reasoning,
                target_location=f"profile {profile.profile_id} {location}",
            )
        for index, target in enumerate(profile.fallbacks):
            self._validate_target(
                target,
                max_cost=max_cost,
                max_latency=max_latency,
                max_reasoning=max_reasoning,
                target_location=f"profile {profile.profile_id} fallback[{index}]",
            )

    def _validate_target(
        self,
        target: RoutingTarget,
        *,
        max_cost: float,
        max_latency: float,
        max_reasoning: ReasoningEffort,
        target_location: str,
    ) -> None:
        if target.runtime.provider in self.policy.denied_providers:
            raise ValueError(
                f"{target_location} conflicts with policy denied_providers"
            )
        if target.runtime.model in self.policy.denied_models:
            raise ValueError(f"{target_location} conflicts with policy denied_models")
        auth_kind = target.runtime.auth_identity.partition(":")[0].casefold()
        if not self.policy.allow_subscription and auth_kind == "subscription":
            raise ValueError(
                f"{target_location} conflicts with policy allow_subscription"
            )
        if (
            target.max_estimated_task_cost_usd is not None
            and target.max_estimated_task_cost_usd > max_cost
        ):
            raise ValueError(
                f"{target_location} target max_estimated_task_cost_usd "
                "cannot loosen effective policy"
            )
        if (
            target.max_estimated_latency_seconds is not None
            and target.max_estimated_latency_seconds > max_latency
        ):
            raise ValueError(
                f"{target_location} target max_estimated_latency_seconds "
                "cannot loosen effective policy"
            )
        if _effort_position(target.reasoning.maximum) > _effort_position(max_reasoning):
            raise ValueError(
                f"{target_location} target exceeds policy max_reasoning_effort"
            )


class TaskAssessment(FrozenModel):
    """Validated provider-independent requirements for one task."""

    complexity: StrictUnitFloat
    domains: BoundedLabels
    required_capabilities: BoundedLabels
    required_modalities: BoundedLabels
    expected_context_tokens: BoundedTokenCount
    expected_output_tokens: BoundedTokenCount
    quality_sensitivity: StrictUnitFloat
    reliability_sensitivity: StrictUnitFloat
    latency_sensitivity: StrictUnitFloat
    cost_sensitivity: StrictUnitFloat
    risk_class: Literal["low", "moderate", "high", "critical"]
    confidence: StrictUnitFloat

    @field_validator(
        "domains",
        "required_capabilities",
        "required_modalities",
        mode="before",
    )
    @classmethod
    def normalize_assessment_labels(cls, value: Any) -> Any:
        return _normalize_labels(value)


class ClassifierOutcome(FrozenModel):
    """Content-free result of one bounded classifier attempt."""

    assessment: TaskAssessment | None
    safe_default_reason: ClassifierFailureReasonCode | None = None
    clarification_requested: Literal[False] = False
    classifier_runtime_id: RuntimeStableId | None = None
    classifier_input_tokens: BoundedTokenCount = 0
    classifier_output_tokens: BoundedTokenCount = 0
    classifier_cost_usd: NonNegativeFloat | None = None

    @model_validator(mode="after")
    def require_exactly_one_success_or_failure(self) -> "ClassifierOutcome":
        if (self.assessment is None) == (self.safe_default_reason is None):
            raise ValueError(
                "classifier outcome requires either an assessment or a failure reason"
            )
        if self.clarification_requested is not False:
            raise ValueError("classifier outcomes cannot request clarification")
        return self


class ManagementRecord(FrozenModel):
    """Closed, content-free base for every persisted management contract."""

    @model_validator(mode="before")
    @classmethod
    def reject_free_form_content(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            unknown = set(data) - set(cls.model_fields)
            if unknown:
                raise ValueError("management records must be content-free")
        return data


class RankingPackMetadata(ManagementRecord):
    """Verified, content-free identity of a local ranking pack."""

    ranking_pack_id: DurableIdentifier
    ranking_pack_sha256: RuntimeStableId
    schema_version: DurableIdentifier
    verified_at: CanonicalTimestamp


class ManagementPatch(ManagementRecord):
    """One profile's complete runtime-order replacement."""

    profile_id: ProfileIdentifier
    before_runtime_ids: Annotated[
        tuple[RuntimeStableId, ...],
        Field(min_length=1, max_length=MAX_DECISION_CANDIDATES),
    ]
    after_runtime_ids: Annotated[
        tuple[RuntimeStableId, ...],
        Field(min_length=1, max_length=MAX_DECISION_CANDIDATES),
    ]
    reason_codes: Annotated[
        tuple[AuthorityLabel, ...],
        Field(min_length=1, max_length=16),
    ]

    @model_validator(mode="after")
    def require_unique_patch_identities(self) -> "ManagementPatch":
        for name, runtime_ids in (
            ("before_runtime_ids", self.before_runtime_ids),
            ("after_runtime_ids", self.after_runtime_ids),
            ("reason_codes", self.reason_codes),
        ):
            if len(runtime_ids) != len(set(runtime_ids)):
                raise ValueError(f"{name} must contain unique identities")
        return self


class ManagementRevision(ManagementRecord):
    """Immutable, authority-bound batch of local profile-management patches."""

    revision_id: DurableIdentifier
    preceding_authority_id: RuntimeStableId
    resulting_authority_id: RuntimeStableId
    management_authority_id: RuntimeStableId
    parent_revision_id: DurableIdentifier | None = None
    ranking_pack: RankingPackMetadata
    inventory_revision: DurableIdentifier
    inventory_fingerprint: RuntimeStableId
    management_epoch: StrictNonNegativeInt
    action: Literal[
        "propose_canary",
        "fallback_reorder",
        "promote",
        "rollback",
        "recovery",
    ]
    patches: Annotated[tuple[ManagementPatch, ...], Field(min_length=1, max_length=10)]
    runtime_scores: Annotated[
        tuple[tuple[RuntimeStableId, FiniteFloat], ...],
        Field(max_length=MAX_DECISION_CANDIDATES),
    ] = ()
    created_at: CanonicalTimestamp

    @model_validator(mode="after")
    def require_unique_revision_identities(self) -> "ManagementRevision":
        if self.preceding_authority_id == self.resulting_authority_id:
            raise ValueError(
                "management preceding and resulting authority IDs must differ"
            )
        profile_ids = tuple(patch.profile_id for patch in self.patches)
        runtime_ids = tuple(runtime_id for runtime_id, _score in self.runtime_scores)
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("management revision patches must have unique profiles")
        if len(runtime_ids) != len(set(runtime_ids)):
            raise ValueError("management revision scores must have unique runtime IDs")
        return self


class ManagementProfileState(ManagementRecord):
    """Current immutable per-profile management revision pointer."""

    management_authority_id: RuntimeStableId
    profile_id: ProfileIdentifier
    authority_id: RuntimeStableId
    active_revision_id: DurableIdentifier | None = None
    management_epoch: StrictNonNegativeInt = 0
    control_revision_id: DurableIdentifier | None = None
    challenger_revision_id: DurableIdentifier | None = None
    experiment_phase: Literal[
        "eligible",
        "validated",
        "canary",
        "cooldown",
        "rolled_back",
        "recovery_required",
    ] = "eligible"
    cooldown_until: CanonicalTimestamp | None = None
    rejection_count: StrictNonNegativeInt = 0
    generation: StrictNonNegativeInt = 0
    updated_at: CanonicalTimestamp

    @model_validator(mode="after")
    def require_complete_experiment_linkage(self) -> "ManagementProfileState":
        if (self.control_revision_id is None) != (
            self.challenger_revision_id is None
        ):
            raise ValueError(
                "management experiment requires both control and challenger revisions"
            )
        if (
            self.control_revision_id is not None
            and self.control_revision_id == self.challenger_revision_id
        ):
            raise ValueError(
                "management control and challenger revisions must differ"
            )
        pair_is_clear = self.control_revision_id is None
        if self.experiment_phase in {"eligible", "rolled_back"}:
            if not pair_is_clear:
                raise ValueError(
                    f"{self.experiment_phase} management phase must clear "
                    "control and challenger revisions"
                )
        elif self.experiment_phase in {"validated", "canary", "cooldown"}:
            if pair_is_clear:
                raise ValueError(
                    "active management experiment requires control and challenger revisions"
                )
            if self.active_revision_id not in {
                self.control_revision_id,
                self.challenger_revision_id,
            }:
                raise ValueError(
                    "active management revision must match control or challenger"
                )
        elif not pair_is_clear and self.active_revision_id not in {
            self.control_revision_id,
            self.challenger_revision_id,
        }:
            raise ValueError(
                "recovery management revision must match control or challenger"
            )
        if self.experiment_phase in {"validated", "canary"} and (
            self.active_revision_id != self.control_revision_id
        ):
            raise ValueError(
                f"{self.experiment_phase} management phase must keep control active"
            )
        if self.experiment_phase == "rolled_back" and self.active_revision_id is None:
            raise ValueError("rolled_back management phase requires an active revision")
        if self.experiment_phase == "cooldown" and self.cooldown_until is None:
            raise ValueError("cooldown management phase requires cooldown_until")
        if self.experiment_phase != "cooldown" and self.cooldown_until is not None:
            raise ValueError("cooldown_until is valid only during management cooldown")
        return self


class ManagementControl(ManagementRecord):
    """Bounded global control state for autonomous profile management."""

    management_authority_id: RuntimeStableId
    frozen: Annotated[bool, Field(strict=True)] = False
    changes_today: Annotated[int, Field(ge=0, le=10, strict=True)] = 0
    cron_job_id: DurableIdentifier | None = None
    generation: StrictNonNegativeInt = 0
    updated_at: CanonicalTimestamp


class ManagementCanaryAssignment(ManagementRecord):
    """Persisted deterministic management canary arm for one profile operation."""

    assignment_id: DurableIdentifier
    management_authority_id: RuntimeStableId
    profile_id: ProfileIdentifier
    operation_identity_hash: RuntimeStableId
    control_revision_id: DurableIdentifier
    challenger_revision_id: DurableIdentifier
    arm: Literal["control", "challenger"]
    phase: Literal["reserved", "finalized", "terminal"] = "reserved"
    runtime_id: RuntimeStableId | None = None
    reasoning_effort: ReasoningEffort | None = None
    created_at: CanonicalTimestamp

    @model_validator(mode="after")
    def require_distinct_revision_arms(self) -> "ManagementCanaryAssignment":
        if self.control_revision_id == self.challenger_revision_id:
            raise ValueError("management control and challenger revisions must differ")
        resolved = self.runtime_id is not None and self.reasoning_effort is not None
        if self.phase == "reserved" and (
            self.runtime_id is not None or self.reasoning_effort is not None
        ):
            raise ValueError(
                "reserved management assignment cannot have runtime or reasoning effort"
            )
        if self.phase in {"finalized", "terminal"} and not resolved:
            raise ValueError(
                f"{self.phase} management assignment requires runtime and reasoning effort"
            )
        return self


class ManagementLifecycleEvent(ManagementRecord):
    """One immutable, content-free management lifecycle transition."""

    event_id: DurableIdentifier
    management_authority_id: RuntimeStableId
    profile_id: ProfileIdentifier
    revision_id: DurableIdentifier | None = None
    event_type: Literal[
        "proposed",
        "validated",
        "canary",
        "promoted",
        "rejected",
        "frozen",
        "unfrozen",
        "rolled_back",
        "hold",
        "cooldown",
        "recovered",
    ]
    reason_code: AuthorityLabel
    created_at: CanonicalTimestamp


class ManagementConfigReceipt(ManagementRecord):
    """Content-free recovery phase for one atomic management config apply."""

    receipt_id: DurableIdentifier
    revision_id: DurableIdentifier
    phase: Literal[
        "prepared",
        "config_replaced",
        "committed",
        "recovery_required",
    ]
    preceding_authority_id: RuntimeStableId
    resulting_authority_id: RuntimeStableId
    backup_checksum: RuntimeStableId
    created_at: CanonicalTimestamp
    updated_at: CanonicalTimestamp

    @model_validator(mode="after")
    def require_authority_transition(self) -> "ManagementConfigReceipt":
        if self.preceding_authority_id == self.resulting_authority_id:
            raise ValueError(
                "management receipt preceding and resulting authority IDs must differ"
            )
        return self


class ManagementLifecycleFinalization(ManagementRecord):
    """Separate durable journal for post-config lifecycle settlement."""

    finalization_id: DurableIdentifier
    receipt_id: DurableIdentifier
    revision_id: DurableIdentifier
    challenger_revision_id: DurableIdentifier
    management_authority_id: RuntimeStableId
    profile_id: ProfileIdentifier
    action: Literal["promote", "rollback"]
    event_type: Literal["promoted", "rejected"]
    reason_code: AuthorityLabel
    rejection_count: StrictNonNegativeInt
    expected_state_generation: StrictNonNegativeInt
    settlement_at: CanonicalTimestamp
    phase: Literal["pending", "finalized"] = "pending"
    created_at: CanonicalTimestamp
    updated_at: CanonicalTimestamp

    @model_validator(mode="after")
    def require_matching_terminal_event(self) -> "ManagementLifecycleFinalization":
        expected = "promoted" if self.action == "promote" else "rejected"
        if self.event_type != expected:
            raise ValueError("management finalization action and event must match")
        return self


class ManagementDecisionSnapshot(ManagementRecord):
    """Default-empty management lineage carried by routing decisions."""

    management_revision_id: DurableIdentifier | None = None
    management_assignment_id: DurableIdentifier | None = None
    management_profile_snapshot: Mapping[ProfileIdentifier, DurableIdentifier] = Field(
        default_factory=dict
    )

    @field_validator("management_profile_snapshot")
    @classmethod
    def freeze_management_profile_snapshot(
        cls,
        value: Mapping[str, str],
    ) -> Mapping[str, str]:
        return MappingProxyType(dict(sorted(value.items())))

    @field_serializer("management_profile_snapshot")
    def serialize_management_profile_snapshot(
        self,
        value: Mapping[str, str],
    ) -> dict[str, str]:
        return dict(value)


class RoutingDecision(FrozenModel):
    """Complete immutable explanation of one routing choice."""

    decision_id: DurableIdentifier
    scope: Literal["fresh_session", "delegation"]
    session_id: DurableIdentifier
    task_id: DurableIdentifier
    operation_id: DurableIdentifier | None
    task_index: (
        Annotated[
            int,
            Field(ge=0, le=MAX_TASK_INDEX, strict=True),
        ]
        | None
    )
    created_at: CanonicalTimestamp
    applied_rule_ids: Annotated[
        tuple[AuthorityLabel, ...],
        Field(max_length=64),
    ]
    assessment: TaskAssessment | None
    task_facts_hash: RuntimeStableId
    inventory_revision: DurableIdentifier
    catalog_revision: DurableIdentifier
    authority_revision: DurableIdentifier
    policy_revision: DurableIdentifier
    adaptive_revision: DurableIdentifier
    profile_adaptive_revision_id: DurableIdentifier | None = None
    adaptive_assignment_id: DurableIdentifier | None = None
    adaptive_profile_snapshot: Mapping[ProfileIdentifier, DurableIdentifier] = Field(
        default_factory=dict
    )
    management_revision_id: DurableIdentifier | None = None
    management_assignment_id: DurableIdentifier | None = None
    management_profile_snapshot: Mapping[ProfileIdentifier, DurableIdentifier] = Field(
        default_factory=dict
    )
    activation_receipt_id: DurableIdentifier | None = None
    activation_config_sha: RuntimeStableId | None = None
    adapter_capability_sha: RuntimeStableId | None = None
    eligible_candidates: Annotated[
        tuple[RuntimeStableId, ...],
        Field(max_length=MAX_DECISION_CANDIDATES),
    ]
    rejected_candidates: Annotated[
        tuple[
            tuple[
                RuntimeStableId,
                Annotated[
                    tuple[CandidateReasonCode, ...],
                    Field(min_length=1, max_length=MAX_REASON_CODES),
                ],
            ],
            ...,
        ],
        Field(max_length=MAX_DECISION_CANDIDATES),
    ]
    normalized_scoring_inputs: Annotated[
        tuple[tuple[ScoreComponent, FiniteFloat], ...],
        Field(max_length=MAX_SCORE_COMPONENTS),
    ]
    final_scores: Annotated[
        tuple[tuple[RuntimeStableId, FiniteFloat], ...],
        Field(max_length=MAX_DECISION_CANDIDATES),
    ]
    selected_profile_id: ProfileIdentifier | None
    selected_runtime: RuntimeKey
    selected_reasoning_effort: ReasoningEffort
    projection_mode: Literal["shadow", "active", "inherit"]
    selection_reason: SelectionReasonCode
    projected_fallback_chain: Annotated[
        tuple[RoutingTarget, ...],
        Field(max_length=64),
    ]
    safe_default_runtime: RuntimeKey
    safe_default_reasoning_effort: ReasoningEffort
    classifier_runtime_id: RuntimeStableId | None
    classifier_input_tokens: BoundedTokenCount
    classifier_output_tokens: BoundedTokenCount
    classifier_cost_usd: NonNegativeFloat | None
    routing_latency_seconds: NonNegativeFloat
    safe_default_reason: SafeDefaultReasonCode | None = None
    degradation_reason: DegradationReasonCode | None = None

    @field_validator("adaptive_profile_snapshot")
    @classmethod
    def freeze_adaptive_profile_snapshot(
        cls,
        value: Mapping[str, str],
    ) -> Mapping[str, str]:
        return MappingProxyType(dict(sorted(value.items())))

    @field_serializer("adaptive_profile_snapshot")
    def serialize_adaptive_profile_snapshot(
        self,
        value: Mapping[str, str],
    ) -> dict[str, str]:
        return dict(value)

    @field_validator("management_profile_snapshot")
    @classmethod
    def freeze_management_profile_snapshot(
        cls,
        value: Mapping[str, str],
    ) -> Mapping[str, str]:
        return MappingProxyType(dict(sorted(value.items())))

    @field_serializer("management_profile_snapshot")
    def serialize_management_profile_snapshot(
        self,
        value: Mapping[str, str],
    ) -> dict[str, str]:
        return dict(value)

    @model_validator(mode="after")
    def validate_decision_boundary(self) -> "RoutingDecision":
        if self.management_profile_snapshot:
            if self.selected_profile_id is None:
                raise ValueError(
                    "management profile snapshots require a selected profile"
                )
            selected_revision = self.management_profile_snapshot.get(
                self.selected_profile_id
            )
            if selected_revision != self.management_revision_id:
                raise ValueError(
                    "selected management revision must match the complete snapshot"
                )
        elif self.management_revision_id is not None:
            raise ValueError(
                "management revision requires a complete profile snapshot"
            )
        if (
            self.management_assignment_id is not None
            and self.management_revision_id is None
        ):
            raise ValueError(
                "management assignment requires a selected profile revision"
            )
        if self.adaptive_profile_snapshot:
            if self.selected_profile_id is None:
                raise ValueError(
                    "adaptive profile snapshots require a selected profile"
                )
            selected_revision = self.adaptive_profile_snapshot.get(
                self.selected_profile_id
            )
            if selected_revision != self.profile_adaptive_revision_id:
                raise ValueError(
                    "selected profile adaptive revision must match the complete snapshot"
                )
        elif self.profile_adaptive_revision_id is not None:
            raise ValueError(
                "profile adaptive revision requires a complete profile snapshot"
            )
        if (
            self.adaptive_assignment_id is not None
            and self.profile_adaptive_revision_id is None
        ):
            raise ValueError(
                "adaptive assignment requires a selected profile revision"
            )
        if self.scope == "delegation" and (
            self.operation_id is None or self.task_index is None
        ):
            raise ValueError("delegation decisions require operation_id and task_index")
        if self.scope == "fresh_session" and (
            self.operation_id is not None or self.task_index is not None
        ):
            raise ValueError(
                "fresh-session decisions cannot carry operation_id or task_index"
            )
        if self.assessment is None and self.safe_default_reason is None:
            raise ValueError(
                "assessment may be absent only when safe_default_reason is present"
            )
        if self.assessment is None and (
            self.selected_profile_id is not None
            or self.selected_runtime.stable_id()
            != self.safe_default_runtime.stable_id()
            or self.selected_reasoning_effort != self.safe_default_reasoning_effort
        ):
            raise ValueError(
                "assessment-free failure must select the validated safe default"
            )
        receipt_fields = (
            self.activation_receipt_id,
            self.activation_config_sha,
            self.adapter_capability_sha,
        )
        if self.projection_mode == "active" and any(
            field is None for field in receipt_fields
        ):
            raise ValueError(
                "active projection requires activation receipt/config/adapter hashes"
            )
        if self.projection_mode != "active" and any(
            field is not None for field in receipt_fields
        ):
            raise ValueError(
                "activation receipt fields are valid only for active projection"
            )
        for name, values in (
            ("applied_rule_ids", self.applied_rule_ids),
            ("eligible_candidates", self.eligible_candidates),
            (
                "rejected_candidates",
                tuple(runtime_id for runtime_id, _reasons in self.rejected_candidates),
            ),
            (
                "normalized_scoring_inputs",
                tuple(name for name, _value in self.normalized_scoring_inputs),
            ),
            (
                "final_scores",
                tuple(runtime_id for runtime_id, _score in self.final_scores),
            ),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicate identities")
        eligible = set(self.eligible_candidates)
        rejected = {runtime_id for runtime_id, _reasons in self.rejected_candidates}
        if eligible & rejected:
            raise ValueError(
                "eligible and rejected candidate identities must be disjoint"
            )
        if any(runtime_id not in eligible for runtime_id, _score in self.final_scores):
            raise ValueError("final_scores may reference only eligible candidates")
        if self.selection_reason in {
            "highest_eligible_score",
            "pinned_profile",
            "preferred_profile",
            "rule",
        }:
            selected_runtime_id = self.selected_runtime.stable_id()
            scored = {runtime_id for runtime_id, _score in self.final_scores}
            if self.selected_profile_id is None:
                raise ValueError("ranked selection requires a selected profile")
            if selected_runtime_id not in eligible:
                raise ValueError("selected runtime must be an eligible candidate")
            if selected_runtime_id not in scored:
                raise ValueError("selected runtime must have a final score")
        return self


class RuntimeObservation(FrozenModel):
    """Observed eligibility and economics for one full runtime key."""

    key: RuntimeKey
    state: InventoryState
    reasons: tuple[NonEmptyString, ...]
    economics: AccessEconomics
    verification_source: NonEmptyString | None
    verified_at: NonEmptyString | None
    verification_expires_at: NonEmptyString | None
    provenance: tuple[NonEmptyString, ...]
    observed_at: NonEmptyString
    capabilities: Mapping[NonEmptyString, Any] = Field(default_factory=dict)

    @field_validator("capabilities")
    @classmethod
    def freeze_capabilities(
        cls,
        value: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return MappingProxyType(dict(value))

    @field_serializer("capabilities")
    def serialize_capabilities(
        self,
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        return dict(value)

    @model_validator(mode="after")
    def require_state_evidence(self) -> "RuntimeObservation":
        if self.state == "verified":
            if self.verification_source is None:
                raise ValueError("verified runtime requires verification_source")
            if self.verified_at is None:
                raise ValueError("verified runtime requires verified_at")
        elif not self.reasons:
            raise ValueError(
                f"{self.state} runtime requires reasons explaining its state"
            )
        return self


class CatalogEvidence(FrozenModel):
    """One provenance-preserving external catalog metric."""

    source_id: NonEmptyString
    source_url: NonEmptyString
    retrieved_at: NonEmptyString
    published_at: NonEmptyString
    expires_at: NonEmptyString | None = None
    model: NonEmptyString
    model_version: NonEmptyString
    domain: NonEmptyString
    task_definition: NonEmptyString
    metric_name: NonEmptyString
    metric_direction: Literal["higher_is_better", "lower_is_better"]
    metric_scale: NonEmptyString
    value: FiniteFloat
    sample_size: int | None = Field(
        default=None,
        ge=0,
        le=MAX_CATALOG_SAMPLE_SIZE,
    )
    confidence: UnitFloat | None = None
    normalization_method: NonEmptyString


class CatalogApplicability(FrozenModel):
    """Exact canonical model/provider/runtime scope for catalog evidence."""

    canonical_provider: str = ""
    canonical_model: NonEmptyString
    canonical_version: NonEmptyString
    runtime_id: RuntimeStableId | None = None


class StoredCatalogRecord(FrozenModel):
    """One checksummed evidence row and its durable applicability scope."""

    evidence: CatalogEvidence
    applicability: CatalogApplicability

    @model_validator(mode="after")
    def require_exact_model_binding(self) -> "StoredCatalogRecord":
        if self.applicability.canonical_model != self.evidence.model:
            raise ValueError("catalog canonical model must match evidence model")
        if self.applicability.canonical_version != self.evidence.model_version:
            raise ValueError("catalog canonical version must match evidence version")
        return self


AdaptiveLifecyclePhase: TypeAlias = Literal[
    "eligible",
    "validated",
    "canary",
    "promoted",
    "rejected",
    "cooldown",
    "rolled_back",
]
AdaptiveLifecycleEventType: TypeAlias = Literal[
    "eligible",
    "validated",
    "canary",
    "promoted",
    "rejected",
    "cooldown",
    "frozen",
    "unfrozen",
    "rolled_back",
]


class AdaptiveOverlay(FrozenModel):
    """Complete profile-local ordering and bounded reasoning-default overlay."""

    profile_id: ProfileIdentifier
    ordered_primary_runtime_ids: Annotated[
        tuple[RuntimeStableId, ...],
        Field(min_length=1, max_length=MAX_PROFILE_FALLBACKS + 1),
    ]
    reasoning_defaults: Mapping[RuntimeStableId, ReasoningEffort] = Field(
        default_factory=dict
    )

    @field_validator("reasoning_defaults")
    @classmethod
    def freeze_reasoning_defaults(
        cls,
        value: Mapping[str, ReasoningEffort],
    ) -> Mapping[str, ReasoningEffort]:
        return MappingProxyType(dict(sorted(value.items())))

    @field_serializer("reasoning_defaults")
    def serialize_reasoning_defaults(
        self,
        value: Mapping[str, ReasoningEffort],
    ) -> dict[str, ReasoningEffort]:
        return dict(value)

    @model_validator(mode="after")
    def require_unique_primary_runtime_ids(self) -> "AdaptiveOverlay":
        if len(self.ordered_primary_runtime_ids) != len(
            set(self.ordered_primary_runtime_ids)
        ):
            raise ValueError("overlay primary runtime identities must be unique")
        unknown_defaults = set(self.reasoning_defaults) - set(
            self.ordered_primary_runtime_ids
        )
        if unknown_defaults:
            raise ValueError(
                "overlay reasoning_defaults must reference ordered primary runtime IDs"
            )
        return self


class AdaptiveExplanation(FrozenModel):
    """Bounded content-free metadata explaining one adaptive transition."""

    reason_codes: Annotated[
        tuple[AuthorityLabel, ...],
        Field(max_length=MAX_REASON_CODES),
    ] = ()
    evidence_ids: Annotated[
        tuple[RuntimeStableId, ...],
        Field(max_length=20_000),
    ] = ()
    context_bucket_id: RuntimeStableId | None = None
    operation_identity_hash: RuntimeStableId | None = None
    assignment_id: DurableIdentifier | None = None
    control_revision_id: DurableIdentifier | None = None
    challenger_revision_id: DurableIdentifier | None = None
    control_runtime_id: RuntimeStableId | None = None
    challenger_runtime_id: RuntimeStableId | None = None
    counts: Annotated[
        Mapping[AuthorityLabel, StrictNonNegativeInt],
        Field(max_length=64),
    ] = Field(default_factory=dict)
    metrics: Annotated[
        Mapping[AuthorityLabel, FiniteFloat],
        Field(max_length=64),
    ] = Field(default_factory=dict)
    labels: Annotated[
        Mapping[AuthorityLabel, AuthorityLabel],
        Field(max_length=64),
    ] = Field(default_factory=dict)

    @field_validator("reason_codes", "evidence_ids")
    @classmethod
    def canonicalize_identifier_tuples(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("adaptive explanation identities must be unique")
        return tuple(sorted(value))

    @field_validator("counts", "metrics", "labels")
    @classmethod
    def freeze_metadata_mapping(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return MappingProxyType(dict(sorted(value.items())))

    @field_serializer("counts", "metrics", "labels")
    def serialize_metadata_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class AdaptiveProfileRevision(FrozenModel):
    """One complete, immutable overlay bound to an authority and profile."""

    revision_id: DurableIdentifier
    authority_id: RuntimeStableId
    profile_id: ProfileIdentifier
    parent_revision_id: DurableIdentifier | None = None
    overlay: AdaptiveOverlay
    explanation: AdaptiveExplanation = Field(default_factory=AdaptiveExplanation)
    lifecycle: Literal[
        "eligible", "validated", "canary", "promoted", "rejected", "cooldown"
    ]
    created_at: CanonicalTimestamp
    complete: Annotated[bool, Field(strict=True)] = True

    @model_validator(mode="after")
    def validate_revision_payload(self) -> "AdaptiveProfileRevision":
        if self.overlay.profile_id != self.profile_id:
            raise ValueError("overlay profile must match revision profile")
        return self


class AdaptiveLifecycleEvent(FrozenModel):
    """One immutable profile-local adaptive lifecycle transition."""

    event_id: DurableIdentifier
    authority_id: RuntimeStableId
    profile_id: ProfileIdentifier
    revision_id: DurableIdentifier | None = None
    event_type: AdaptiveLifecycleEventType
    reason_code: AuthorityLabel
    explanation: AdaptiveExplanation = Field(default_factory=AdaptiveExplanation)
    created_at: CanonicalTimestamp


class AdaptiveCanaryAssignment(FrozenModel):
    """Persisted deterministic canary arm for one profile-local operation."""

    assignment_id: DurableIdentifier
    authority_id: RuntimeStableId
    profile_id: ProfileIdentifier
    operation_identity_hash: RuntimeStableId
    context_bucket_id: RuntimeStableId
    control_revision_id: DurableIdentifier
    challenger_revision_id: DurableIdentifier
    arm: Literal["control", "challenger"]
    created_at: CanonicalTimestamp

    @model_validator(mode="after")
    def require_distinct_revision_arms(self) -> "AdaptiveCanaryAssignment":
        if self.control_revision_id == self.challenger_revision_id:
            raise ValueError("canary control and challenger revisions must differ")
        return self


class AdaptiveProfileControl(FrozenModel):
    """Authoritative profile-local adaptive pointer and mutation generation."""

    authority_id: RuntimeStableId
    profile_id: ProfileIdentifier
    active_revision_id: DurableIdentifier | None = None
    control_revision_id: DurableIdentifier | None = None
    challenger_revision_id: DurableIdentifier | None = None
    experiment_phase: AdaptiveLifecyclePhase = "eligible"
    frozen: Annotated[bool, Field(strict=True)] = False
    cooldown_until: CanonicalTimestamp | None = None
    rejection_count: StrictNonNegativeInt = 0
    generation: StrictNonNegativeInt = 0
    updated_at: CanonicalTimestamp

    @model_validator(mode="after")
    def require_complete_experiment_linkage(self) -> "AdaptiveProfileControl":
        if (self.control_revision_id is None) != (
            self.challenger_revision_id is None
        ):
            raise ValueError(
                "adaptive experiment requires both control and challenger revisions"
            )
        if (
            self.control_revision_id is not None
            and self.control_revision_id == self.challenger_revision_id
        ):
            raise ValueError("adaptive control and challenger revisions must differ")
        pair_is_clear = self.control_revision_id is None
        if self.experiment_phase in {"eligible", "rolled_back"}:
            if not pair_is_clear:
                raise ValueError(
                    f"{self.experiment_phase} phase must clear experiment revisions"
                )
        elif pair_is_clear:
            raise ValueError(
                "active adaptive experiment requires control and challenger revisions"
            )
        if self.experiment_phase == "rolled_back" and self.active_revision_id is None:
            raise ValueError("rolled_back phase requires an active revision")
        if self.experiment_phase == "cooldown" and self.cooldown_until is None:
            raise ValueError("cooldown phase requires cooldown_until")
        if self.experiment_phase != "cooldown" and self.cooldown_until is not None:
            raise ValueError("cooldown_until is valid only during cooldown")
        return self


class OptimizerLease(FrozenModel):
    """Owner- and generation-guarded profile-local optimizer lease."""

    authority_id: RuntimeStableId
    profile_id: ProfileIdentifier
    owner_id: DurableIdentifier
    lease_expires_at: CanonicalTimestamp
    generation: StrictNonNegativeInt
    updated_at: CanonicalTimestamp


class AdaptiveRevision(FrozenModel):
    """One complete, authority-bound adaptive overlay revision."""

    revision_id: NonEmptyString
    authority_id: NonEmptyString
    parent_revision_id: NonEmptyString | None = None
    overlay: dict[str, JsonValue]
    explanation: dict[str, JsonValue]
    created_at: NonEmptyString
    is_baseline: bool

    @field_validator("overlay", "explanation")
    @classmethod
    def recursively_freeze_json(cls, value: dict[str, JsonValue]) -> Any:
        return _freeze_json(value)

    @field_serializer("overlay", "explanation")
    def serialize_frozen_json(self, value: Any) -> Any:
        return _thaw_json(value)

    @model_validator(mode="after")
    def require_json_compatible_payloads(self) -> "AdaptiveRevision":
        try:
            json.dumps(_thaw_json(self.overlay), allow_nan=False)
            json.dumps(_thaw_json(self.explanation), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "adaptive overlay and explanation must be JSON-compatible"
            ) from exc
        return self


def _effort_position(effort: ReasoningEffort) -> int:
    return REASONING_EFFORT_ORDER.index(effort)


def _normalize_label(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return value.strip().casefold()


def _normalize_labels(value: Any) -> Any:
    if not isinstance(value, (list, tuple)):
        raise ValueError("labels must be provided as a list or tuple of strings")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("every label must be a string")
    normalized = tuple(_normalize_label(item) for item in value)
    if len(normalized) != len(set(normalized)):
        raise ValueError("labels must not contain duplicates")
    return normalized


def _identity_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _identity_variants(
    value: str,
    identity_kind: Literal["provider", "model"],
) -> frozenset[tuple[str, Literal["provider", "model"]]]:
    variants = {_identity_label(value)}
    if identity_kind == "model":
        variants.add(_identity_label(re.split(r"[/\\]", value)[-1]))
    return frozenset((identity, identity_kind) for identity in variants if identity)


def _contains_identity(
    label: str,
    identity: str,
    *,
    minimum_characters: int,
) -> bool:
    identity_parts = tuple(part for part in identity.split("_") if part)
    if not identity_parts or len("".join(identity_parts)) < minimum_characters:
        return False
    label_parts = tuple(part for part in _identity_label(label).split("_") if part)
    width = len(identity_parts)
    return any(
        label_parts[index : index + width] == identity_parts
        for index in range(len(label_parts) - width + 1)
    )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            key: _freeze_json(value[key]) for key in sorted(value)
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


__all__ = [
    "AccessEconomics",
    "ActivationMode",
    "ActivationSettings",
    "AdaptationSettings",
    "AdaptiveCanaryAssignment",
    "AdaptiveExplanation",
    "AdaptiveLifecycleEvent",
    "AdaptiveLifecycleEventType",
    "AdaptiveLifecyclePhase",
    "AdaptiveOverlay",
    "AdaptiveProfileControl",
    "AdaptiveProfileRevision",
    "AdaptiveRevision",
    "AuthorityLabel",
    "AutoRoutingConfig",
    "BillingKind",
    "BoundedLabels",
    "BoundedTokenCount",
    "CandidateReasonCode",
    "candidate_id_for",
    "CatalogApplicability",
    "CatalogEvidence",
    "CanonicalTimestamp",
    "ClassifierFailureReasonCode",
    "ClassifierOutcome",
    "ClassifierSettings",
    "ComplexityBands",
    "DegradationReasonCode",
    "DecisionCandidate",
    "DurableIdentifier",
    "EvidenceContextBucket",
    "EvidenceEvent",
    "EvidenceFeedbackValue",
    "EvidenceOutcome",
    "EvidenceSignalType",
    "EvidenceSource",
    "FiniteFloat",
    "InventoryState",
    "LocalModelRequirements",
    "MAX_CATALOG_SAMPLE_SIZE",
    "MAX_CLASSIFIER_IMAGE_BYTES",
    "MAX_CLASSIFIER_IMAGE_COUNT",
    "MAX_CLASSIFIER_INPUT_TOKENS",
    "MAX_CLASSIFIER_OUTPUT_TOKENS",
    "MAX_DECISION_CANDIDATES",
    "MAX_PROFILE_FALLBACKS",
    "MAX_PROFILE_ID_LENGTH",
    "MAX_REASON_CODES",
    "MAX_ROUTING_RULES",
    "MAX_SCORE_COMPONENTS",
    "MAX_TASK_INDEX",
    "ManagementCanaryAssignment",
    "ManagementConfigReceipt",
    "ManagementControl",
    "ManagementDecisionSnapshot",
    "ManagementLifecycleEvent",
    "ManagementPatch",
    "ManagementProfileState",
    "ManagementRevision",
    "ObjectiveWeights",
    "OptimizerLease",
    "PluginLlmAuthority",
    "PolicyEnvelope",
    "ProfileIdentifier",
    "ProfileAdaptationSettings",
    "ProfileLimits",
    "ProfileMatch",
    "RankingPackMetadata",
    "RankingPackTrust",
    "ReasoningBounds",
    "ReasoningEffort",
    "RouteProfile",
    "RoutingRule",
    "RoutingDecision",
    "RoutingScopes",
    "RoutingTarget",
    "RoutingVocabulary",
    "RuleAssessmentOverrides",
    "RulePredicate",
    "RuntimeKey",
    "RuntimeObservation",
    "SafeDefaultReasonCode",
    "ScoreComponent",
    "SelectionReasonCode",
    "StoredCatalogRecord",
    "TaskAssessment",
    "TaskFacts",
    "AutonomousProfileManagementSettings",
]
