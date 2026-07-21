"""Read-only proposal models for provenance-aware runtime advice."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

from .inventory import ExecutableRuntime
from .eligibility import (
    runtime_capability_rejection_reasons,
    runtime_policy_rejection_reasons,
)
from .models import (
    MAX_DECISION_CANDIDATES,
    MAX_PROFILE_FALLBACKS,
    MAX_ROUTING_RULES,
    COMPLEXITY_LABELS,
    ComplexityBands,
    FiniteFloat,
    FrozenModel,
    NonEmptyString,
    ObjectiveWeights,
    PolicyEnvelope,
    ProfileIdentifier,
    ProfileLimits,
    ProfileMatch,
    REASONING_EFFORT_ORDER,
    ReasoningBounds,
    RoutingRule,
    RoutingVocabulary,
    RuntimeKey,
    RuntimeStableId,
)
from .scoring import (
    ConservativeMetric,
    MISSING_METRIC_UNCERTAINTY,
    conservative_metric,
    normalize_catalog_metric,
    normalize_against_limit,
    utility_score,
)


_UNSAFE_METADATA = re.compile(
    r"(?i)(?:://|sk-(?:proj-)?[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}|bearer\s+\S{8,}|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password)\s*[:=]\s*\S+)"
)
MAX_EXPECTED_TASK_TOKENS = 100_000_000
# Evidence age changes continuously, but sub-second wall-clock jitter must not
# perturb conversational comparisons. Quantize before utility and sorting;
# four decimals retains meaningful freshness changes (~14.4-minute age steps).
STALENESS_PENALTY_DECIMALS = 4

ADVISOR_REQUIRED_FACTS: tuple[str, ...] = (
    "workloads",
    "modalities",
    "required_capabilities",
    "risk_and_tool_use",
    "hard_limits",
    "classifier_evaluator_disclosure",
    "profiles",
    "rules",
    "complexity_bands",
    "routing_vocabulary",
    "representative_prompts",
    "explicit_approval",
)


class WorkloadRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    domains: tuple[str, ...] = Field(min_length=1)
    examples: tuple[str, ...] = Field(min_length=1)
    expected_input_tokens: int = Field(ge=0, le=MAX_EXPECTED_TASK_TOKENS)
    expected_output_tokens: int = Field(ge=0, le=MAX_EXPECTED_TASK_TOKENS)


class RiskAndToolUseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_classes: tuple[str, ...] = Field(min_length=1)
    requires_tools: bool


class HardLimitsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    denied_providers: tuple[str, ...]
    denied_models: tuple[str, ...]
    allowed_licenses: tuple[str, ...]
    allow_local: bool
    allow_subscription: bool
    max_cost_usd: float = Field(ge=0, allow_inf_nan=False)
    max_latency_seconds: float = Field(ge=0, allow_inf_nan=False)


class DisclosureRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    classifier_provider: str = Field(min_length=1)
    classifier_model: str = Field(min_length=1)
    evaluator_provider: str = Field(min_length=1)
    evaluator_model: str = Field(min_length=1)
    full_disclosure_approved: bool


class AdvisorAccessPaths(FrozenModel):
    """One profile's explicit primary and ordered fallback runtime IDs."""

    primary_runtime_id: RuntimeStableId
    fallback_runtime_ids: tuple[RuntimeStableId, ...] = Field(
        max_length=MAX_PROFILE_FALLBACKS
    )

    @model_validator(mode="after")
    def require_unique_targets(self) -> "AdvisorAccessPaths":
        target_ids = (self.primary_runtime_id, *self.fallback_runtime_ids)
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("primary and fallback runtime IDs must be unique")
        return self


class AdvisorProfileIntent(FrozenModel):
    """Complete profile policy used for ranking before target selection."""

    profile_id: ProfileIdentifier
    description: NonEmptyString
    base_rank: FiniteFloat | None = None
    match: ProfileMatch
    objectives: ObjectiveWeights
    # Deliberately required even though nullable: authoring must distinguish
    # an explicit inheritance choice from an omitted interview answer.
    limits: ProfileLimits | None


class AdvisorProfileRequest(AdvisorProfileIntent):
    """One independently authored profile and its exact executable paths."""

    access_paths: AdvisorAccessPaths
    reasoning_bounds: Mapping[RuntimeStableId, ReasoningBounds]

    @field_validator("reasoning_bounds")
    @classmethod
    def freeze_reasoning_bounds(
        cls,
        value: Mapping[str, ReasoningBounds],
    ) -> Mapping[str, ReasoningBounds]:
        return MappingProxyType(dict(sorted(value.items())))

    @field_serializer("reasoning_bounds")
    def serialize_reasoning_bounds(
        self,
        value: Mapping[str, ReasoningBounds],
    ) -> dict[str, Any]:
        return dict(value)

    @model_validator(mode="after")
    def require_exact_reasoning_coverage(self) -> "AdvisorProfileRequest":
        target_ids = {
            self.access_paths.primary_runtime_id,
            *self.access_paths.fallback_runtime_ids,
        }
        if set(self.reasoning_bounds) != target_ids:
            raise ValueError("reasoning bounds must cover every exact access path")
        if self.limits is not None and self.limits.max_reasoning_effort is not None:
            limit_index = REASONING_EFFORT_ORDER.index(
                self.limits.max_reasoning_effort
            )
            if any(
                REASONING_EFFORT_ORDER.index(bounds.maximum) > limit_index
                for bounds in self.reasoning_bounds.values()
            ):
                raise ValueError(
                    f"profile {self.profile_id} reasoning bounds loosen "
                    "max_reasoning_effort"
                )
        return self


class AdvisorReadiness(BaseModel):
    """Progressive interview state with one stable missing-fact ordering."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ready: bool
    missing_facts: tuple[str, ...]


class AdvisorRankingRequest(FrozenModel):
    """Profile policy sufficient for evidence ranking, without target choices."""

    workloads: WorkloadRequest
    modalities: tuple[str, ...] = Field(min_length=1)
    required_capabilities: tuple[str, ...]
    risk_and_tool_use: RiskAndToolUseRequest
    hard_limits: HardLimitsRequest
    classifier_evaluator_disclosure: DisclosureRequest
    profiles: tuple[AdvisorProfileIntent, ...] = Field(
        min_length=1,
        max_length=MAX_DECISION_CANDIDATES,
    )
    rules: tuple[RoutingRule, ...] = Field(max_length=MAX_ROUTING_RULES)
    complexity_bands: ComplexityBands
    routing_vocabulary: RoutingVocabulary

    @field_validator("required_capabilities")
    @classmethod
    def validate_required_capabilities(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized = tuple(item.strip().casefold() for item in value)
        if any(not item for item in normalized):
            raise ValueError("required capabilities must not contain empty values")
        if len(normalized) != len(set(normalized)):
            raise ValueError("required capabilities must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_ranking_contract(self) -> "AdvisorRankingRequest":
        if not self.classifier_evaluator_disclosure.full_disclosure_approved:
            raise ValueError("full classifier/evaluator disclosure approval is required")
        profile_ids = tuple(profile.profile_id for profile in self.profiles)
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("profile IDs must be unique")

        rule_ids = tuple(rule.rule_id for rule in self.rules)
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rules contain a duplicate rule_id")
        canonical_rules = tuple(
            sorted(self.rules, key=lambda rule: (-rule.priority, rule.rule_id))
        )
        if self.rules != canonical_rules:
            object.__setattr__(self, "rules", canonical_rules)

        known_profiles = set(profile_ids)
        capabilities = set(self.routing_vocabulary.capabilities)
        modalities = set(self.routing_vocabulary.modalities)
        required_capabilities = set(self.required_capabilities)
        if self.risk_and_tool_use.requires_tools:
            required_capabilities.add("tools")
        undeclared_global_capabilities = required_capabilities - capabilities
        if undeclared_global_capabilities:
            raise ValueError(
                "global capabilities use label outside routing_vocabulary: "
                f"{sorted(undeclared_global_capabilities)[0]}"
            )
        undeclared_global_modalities = set(self.modalities) - modalities
        if undeclared_global_modalities:
            raise ValueError(
                "global modalities use label outside routing_vocabulary: "
                f"{sorted(undeclared_global_modalities)[0]}"
            )

        for profile in self.profiles:
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
            self._validate_profile_limits(profile)

        for rule in self.rules:
            if rule.profile_id not in known_profiles:
                raise ValueError(
                    f"rule {rule.rule_id} references missing profile "
                    f"{rule.profile_id}"
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
            self._require_declared_labels(
                rule.assessment_overrides.required_capabilities or (),
                capabilities,
                location=f"rule {rule.rule_id} override capabilities",
            )
            self._require_declared_labels(
                rule.assessment_overrides.required_modalities or (),
                modalities,
                location=f"rule {rule.rule_id} override modalities",
            )
        return self

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

    def _validate_profile_limits(self, profile: AdvisorProfileIntent) -> None:
        limits = profile.limits
        if limits is None:
            return
        if (
            limits.max_estimated_task_cost_usd is not None
            and limits.max_estimated_task_cost_usd
            > self.hard_limits.max_cost_usd
        ):
            raise ValueError(
                f"profile {profile.profile_id} limits loosen "
                "max_estimated_task_cost_usd"
            )
        if (
            limits.max_estimated_latency_seconds is not None
            and limits.max_estimated_latency_seconds
            > self.hard_limits.max_latency_seconds
        ):
            raise ValueError(
                f"profile {profile.profile_id} limits loosen "
                "max_estimated_latency_seconds"
            )
        if (
            limits.allowed_licenses is not None
            and self.hard_limits.allowed_licenses
            and not set(limits.allowed_licenses).issubset(
                self.hard_limits.allowed_licenses
            )
        ):
            raise ValueError(
                f"profile {profile.profile_id} limits loosen allowed_licenses"
            )
        if limits.canary_high_risk_tasks is True:
            raise ValueError(
                f"profile {profile.profile_id} limits loosen "
                "canary_high_risk_tasks"
            )


class AdvisorRequest(AdvisorRankingRequest):
    """Complete, explicit advisor interview contract."""

    profiles: tuple[AdvisorProfileRequest, ...] = Field(
        min_length=1,
        max_length=MAX_DECISION_CANDIDATES,
    )
    representative_prompts: tuple[str, ...] = Field(min_length=1)
    explicit_approval: bool

    @model_validator(mode="before")
    @classmethod
    def upgrade_exact_legacy_request(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        return cls._upgrade_legacy_mapping(value)

    @model_validator(mode="after")
    def validate_complete_contract(self) -> "AdvisorRequest":
        if not self.explicit_approval:
            raise ValueError("explicit approval is required")
        target_count = sum(
            1 + len(profile.access_paths.fallback_runtime_ids)
            for profile in self.profiles
        )
        if target_count > MAX_DECISION_CANDIDATES:
            raise ValueError(
                "profile targets cannot exceed the durable candidate bundle "
                f"boundary of {MAX_DECISION_CANDIDATES}"
            )
        return self

    @classmethod
    def _upgrade_legacy_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(value)
        legacy_fields = {"objectives", "access_paths", "reasoning_bounds"}
        present_legacy = legacy_fields.intersection(raw)
        if not present_legacy:
            return raw

        profiles = raw.get("profiles")
        new_profile_fields = {
            "match",
            "objectives",
            "limits",
            "access_paths",
            "reasoning_bounds",
        }
        new_top_fields = {"rules", "complexity_bands", "routing_vocabulary"}
        if new_top_fields.intersection(raw) or (
            isinstance(profiles, (list, tuple))
            and any(
                isinstance(item, Mapping)
                and new_profile_fields.intersection(item)
                for item in profiles
            )
        ):
            raise ValueError("ambiguous legacy/new advisor request")
        if not isinstance(profiles, (list, tuple)) or len(profiles) != 1:
            raise ValueError("single-profile legacy requests require exactly one profile")
        profile = profiles[0]
        if not isinstance(profile, Mapping):
            raise ValueError("single-profile legacy profile must be a mapping")
        if set(profile) - {"profile_id", "description", "base_rank"}:
            raise ValueError("ambiguous legacy/new advisor request")

        capabilities = [
            item.strip().casefold() if isinstance(item, str) else item
            for item in (raw.get("required_capabilities") or ())
        ]
        risk = raw.get("risk_and_tool_use")
        if isinstance(risk, Mapping) and risk.get("requires_tools"):
            capabilities.append("tools")
        profile_data = {
            **dict(profile),
            "match": {
                "domains": list(
                    (raw.get("workloads") or {}).get("domains", ())
                    if isinstance(raw.get("workloads"), Mapping)
                    else ()
                ),
                "complexity": [],
                "modalities": list(raw.get("modalities") or ()),
                "capabilities": list(dict.fromkeys(capabilities)),
            },
            "objectives": raw.get("objectives"),
            "limits": None,
        }
        if "access_paths" in raw:
            profile_data["access_paths"] = raw.get("access_paths")
        if "reasoning_bounds" in raw:
            profile_data["reasoning_bounds"] = raw.get("reasoning_bounds")
        raw["profiles"] = [profile_data]
        for field in legacy_fields:
            raw.pop(field, None)
        raw["rules"] = []
        raw["complexity_bands"] = ComplexityBands().model_dump(mode="json")
        raw["routing_vocabulary"] = RoutingVocabulary().model_dump(mode="json")
        return raw

    @classmethod
    def ranking_request(cls, value: Any) -> AdvisorRankingRequest:
        """Validate the authority needed to rank, before selecting runtimes."""
        if not isinstance(value, Mapping):
            raise ValueError("advisor request must contain a mapping")
        raw = cls._upgrade_legacy_mapping(value)
        allowed_top_fields = {
            "workloads",
            "modalities",
            "required_capabilities",
            "risk_and_tool_use",
            "hard_limits",
            "classifier_evaluator_disclosure",
            "profiles",
            "rules",
            "complexity_bands",
            "routing_vocabulary",
            "representative_prompts",
            "explicit_approval",
        }
        unknown_top_fields = set(raw) - allowed_top_fields
        if unknown_top_fields:
            raise ValueError(
                "advisor request contains unknown field: "
                f"{sorted(unknown_top_fields)[0]}"
            )
        profile_fields = {
            "profile_id",
            "description",
            "base_rank",
            "match",
            "objectives",
            "limits",
        }
        ranking_data = {
            field: raw.get(field)
            for field in (
                "workloads",
                "modalities",
                "required_capabilities",
                "risk_and_tool_use",
                "hard_limits",
                "classifier_evaluator_disclosure",
                "rules",
                "complexity_bands",
                "routing_vocabulary",
            )
        }
        profiles = raw.get("profiles")
        allowed_profile_fields = profile_fields | {
            "access_paths",
            "reasoning_bounds",
        }
        if isinstance(profiles, (list, tuple)):
            for profile in profiles:
                if not isinstance(profile, Mapping):
                    raise ValueError("advisor profile must be a mapping")
                unknown_profile_fields = set(profile) - allowed_profile_fields
                if unknown_profile_fields:
                    raise ValueError(
                        "advisor profile contains unknown field: "
                        f"{sorted(unknown_profile_fields)[0]}"
                    )
        ranking_data["profiles"] = [
            {field: profile.get(field) for field in profile_fields if field in profile}
            for profile in profiles
        ] if isinstance(profiles, (list, tuple)) else profiles
        return AdvisorRankingRequest.model_validate(ranking_data)

    @classmethod
    def validate_readiness(cls, value: Any) -> AdvisorReadiness:
        """Validate cumulative interview facts without inventing defaults."""
        raw: Mapping[str, Any]
        if isinstance(value, Mapping):
            try:
                raw = cls._upgrade_legacy_mapping(value)
            except ValueError:
                raw = value
        else:
            raw = {}
        missing: list[str] = []

        def valid(field: str, model: type[BaseModel]) -> bool:
            try:
                model.model_validate(raw.get(field))
            except (TypeError, ValueError):
                return False
            return True

        if not valid("workloads", WorkloadRequest):
            missing.append("workloads")
        modalities = raw.get("modalities")
        if not (
            isinstance(modalities, (list, tuple))
            and modalities
            and all(isinstance(item, str) and item.strip() for item in modalities)
        ):
            missing.append("modalities")
        capabilities = raw.get("required_capabilities")
        if not (
            isinstance(capabilities, (list, tuple))
            and all(
                isinstance(item, str) and item.strip()
                for item in capabilities
            )
            and len({item.strip().casefold() for item in capabilities})
            == len(capabilities)
        ):
            missing.append("required_capabilities")
        if not valid("risk_and_tool_use", RiskAndToolUseRequest):
            missing.append("risk_and_tool_use")
        if not valid("hard_limits", HardLimitsRequest):
            missing.append("hard_limits")
        try:
            disclosure = DisclosureRequest.model_validate(
                raw.get("classifier_evaluator_disclosure")
            )
            if not disclosure.full_disclosure_approved:
                raise ValueError
        except (TypeError, ValueError):
            missing.append("classifier_evaluator_disclosure")
        profiles = raw.get("profiles")
        profile_missing: list[str] = []
        if not isinstance(profiles, (list, tuple)) or not profiles:
            missing.append("profiles")
        else:
            parsed_ids: list[str] = []
            identity_adapter = TypeAdapter(ProfileIdentifier)
            for index, profile in enumerate(profiles):
                if not isinstance(profile, Mapping):
                    profile_missing = []
                    missing.append("profiles")
                    break
                try:
                    profile_id = identity_adapter.validate_python(
                        profile.get("profile_id")
                    )
                except (TypeError, ValueError):
                    profile_missing = []
                    missing.append("profiles")
                    break
                parsed_ids.append(profile_id)
                prefix = f"profiles.{profile_id}"
                description = profile.get("description")
                if not isinstance(description, str) or not description:
                    profile_missing.append(f"{prefix}.description")
                if "base_rank" in profile:
                    try:
                        TypeAdapter(FiniteFloat | None).validate_python(
                            profile.get("base_rank")
                        )
                    except (TypeError, ValueError):
                        profile_missing.append(f"{prefix}.base_rank")
                for field, model in (
                    ("match", ProfileMatch),
                    ("objectives", ObjectiveWeights),
                ):
                    try:
                        model.model_validate(profile.get(field))
                    except (TypeError, ValueError):
                        profile_missing.append(f"{prefix}.{field}")
                if "limits" not in profile:
                    profile_missing.append(f"{prefix}.limits")
                elif profile.get("limits") is not None:
                    try:
                        ProfileLimits.model_validate(profile.get("limits"))
                    except (TypeError, ValueError):
                        profile_missing.append(f"{prefix}.limits")
                try:
                    access = AdvisorAccessPaths.model_validate(
                        profile.get("access_paths")
                    )
                except (TypeError, ValueError):
                    access = None
                    profile_missing.append(f"{prefix}.access_paths")
                try:
                    bounds = profile.get("reasoning_bounds")
                    if not isinstance(bounds, Mapping) or access is None:
                        raise ValueError
                    parsed_bounds = {
                        str(runtime_id): ReasoningBounds.model_validate(item)
                        for runtime_id, item in bounds.items()
                    }
                    target_ids = {
                        access.primary_runtime_id,
                        *access.fallback_runtime_ids,
                    }
                    if set(parsed_bounds) != target_ids:
                        raise ValueError
                except (TypeError, ValueError):
                    profile_missing.append(f"{prefix}.reasoning_bounds")
            if "profiles" not in missing and len(parsed_ids) != len(set(parsed_ids)):
                missing.append("profiles")
                profile_missing = []
            missing.extend(profile_missing)

        for field, model in (
            ("rules", TypeAdapter(tuple[RoutingRule, ...])),
            ("complexity_bands", ComplexityBands),
            ("routing_vocabulary", RoutingVocabulary),
        ):
            if field not in raw:
                missing.append(field)
                continue
            try:
                if isinstance(model, TypeAdapter):
                    model.validate_python(raw.get(field))
                else:
                    model.model_validate(raw.get(field))
            except (TypeError, ValueError):
                missing.append(field)
        prompts = raw.get("representative_prompts")
        if not (
            isinstance(prompts, (list, tuple))
            and prompts
            and all(isinstance(item, str) and item.strip() for item in prompts)
        ):
            missing.append("representative_prompts")
        if raw.get("explicit_approval") is not True:
            missing.append("explicit_approval")

        if not missing:
            cls.model_validate(raw)
        ordered: list[str] = []
        for field in ADVISOR_REQUIRED_FACTS:
            if field == "profiles":
                if "profiles" in missing:
                    ordered.append("profiles")
                ordered.extend(
                    item for item in missing if item.startswith("profiles.")
                )
            elif field in missing:
                ordered.append(field)
        return AdvisorReadiness(ready=not ordered, missing_facts=tuple(ordered))


class ProposalRequest(BaseModel):
    """Complete bounded inputs needed to rank an inventory snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    inventory: tuple[ExecutableRuntime, ...]
    domain: str = Field(min_length=1)
    evidence_domains: tuple[str, ...] = ()
    task_definition: str = Field(min_length=1)
    expected_input_tokens: int = Field(ge=0, le=MAX_EXPECTED_TASK_TOKENS)
    expected_output_tokens: int = Field(ge=0, le=MAX_EXPECTED_TASK_TOKENS)
    required_capabilities: tuple[str, ...] = ()
    required_modalities: tuple[str, ...] = ()
    minimum_context_tokens: int = Field(
        default=0,
        ge=0,
        le=MAX_EXPECTED_TASK_TOKENS,
    )
    minimum_output_tokens: int = Field(
        default=0,
        ge=0,
        le=MAX_EXPECTED_TASK_TOKENS,
    )
    objectives: ObjectiveWeights
    max_estimated_task_cost_usd: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    max_estimated_latency_seconds: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    base_ranks: Mapping[str, float] = Field(default_factory=dict)

    @field_validator("base_ranks")
    @classmethod
    def validate_base_ranks(
        cls,
        value: Mapping[str, float],
    ) -> Mapping[str, float]:
        if any(not math.isfinite(rank) for rank in value.values()):
            raise ValueError("base ranks must be finite")
        return dict(value)

    @field_validator("evidence_domains")
    @classmethod
    def normalize_evidence_domains(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized = tuple(
            sorted({item.strip().casefold() for item in value if item.strip()})
        )
        if len(normalized) != len(value):
            raise ValueError("evidence domains must be non-empty and unique")
        return normalized

    @model_validator(mode="after")
    def freeze_base_ranks(self) -> "ProposalRequest":
        object.__setattr__(
            self,
            "base_ranks",
            MappingProxyType(dict(self.base_ranks)),
        )
        return self

    @field_serializer("base_ranks")
    def serialize_base_ranks(self, value: Mapping[str, float]) -> dict[str, float]:
        return dict(value)


@dataclass(frozen=True)
class ProposedCandidate:
    """One ranked exact runtime access path."""

    runtime_id: str
    key: RuntimeKey
    inventory_state: str
    utility: float


@dataclass(frozen=True)
class ProposalExplanation:
    """Deterministic accepted and rejected candidate accounting."""

    request: Any
    catalog: Any
    candidates: Any
    rejected: Any
    rejected_candidates: Any
    accepted_runtime_ids: tuple[str, ...]
    rejected_runtime_ids: tuple[str, ...]


@dataclass(frozen=True)
class AdvisorProposal:
    """Read-only primary/fallback recommendation and complete explanation."""

    primary: ProposedCandidate | None
    fallbacks: tuple[ProposedCandidate, ...]
    explanation: ProposalExplanation


@dataclass(frozen=True)
class DryRunAssessment:
    """Content-free requirements inferred for one representative prompt."""

    prompt_index: int
    domains: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    required_modalities: tuple[str, ...]
    risk_class: str


@dataclass(frozen=True)
class DryRunResult:
    """Read-only representative assessments tied to a proposal."""

    proposed_runtime_ids: tuple[str, ...]
    assessments: tuple[DryRunAssessment, ...]


@dataclass(frozen=True)
class _CandidateScoreInputs:
    quality: ConservativeMetric
    reliability: ConservativeMetric
    quality_rows: tuple[Any, ...]
    reliability_rows: tuple[Any, ...]
    normalized_cost: float
    normalized_latency: float
    missing_priors: tuple[str, ...]
    uncertainty_components: Mapping[str, float]
    uncertainty_penalty: float


class Advisor:
    """Rank hard-eligible runtime candidates without applying a route."""

    def __init__(self, catalog: Any) -> None:
        self.catalog = catalog

    def propose(self, request: ProposalRequest) -> AdvisorProposal:
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= MAX_EXPECTED_TASK_TOKENS
            for value in (
                request.expected_input_tokens,
                request.expected_output_tokens,
            )
        ):
            raise ValueError("task token estimates must be bounded integers")
        inventory = tuple(request.inventory)
        runtime_ids = tuple(runtime.key.stable_id() for runtime in inventory)
        if len(runtime_ids) != len(set(runtime_ids)):
            raise ValueError("proposal inventory contains a duplicate runtime")
        inventory = tuple(
            sorted(inventory, key=lambda runtime: runtime.key.stable_id())
        )
        rejected = {
            f"{runtime.key.provider}/{runtime.key.model}": runtime.state
            for runtime in inventory
            if runtime.state != "verified"
        }
        rejected_candidates: dict[str, dict[str, Any]] = {}
        for runtime in inventory:
            if runtime.state == "verified":
                continue
            rejected_candidates[runtime.key.stable_id()] = _rejection_details(
                runtime,
                reasons=tuple(runtime.reasons) or (runtime.state,),
                rows=tuple(self.catalog.evidence_for(runtime)),
                request=request,
            )
        ranked: list[tuple[float, str, ProposedCandidate, dict[str, Any]]] = []
        for runtime in inventory:
            if runtime.state != "verified":
                continue
            runtime_id = runtime.key.stable_id()
            if (
                runtime.key.provider.strip().casefold() == "moa"
                or runtime.capabilities.get("is_moa") is True
            ):
                rejected[
                    f"{runtime.key.provider}/{runtime.key.model}"
                ] = "moa_excluded"
                rejected_candidates[runtime_id] = _rejection_details(
                    runtime,
                    reasons=("moa_excluded",),
                    rows=tuple(self.catalog.evidence_for(runtime)),
                    request=request,
                    pre_scoring=True,
                )
                continue
            capability_reasons = runtime_capability_rejection_reasons(
                runtime,
                required_capabilities=request.required_capabilities,
                required_modalities=request.required_modalities,
                minimum_context_tokens=request.minimum_context_tokens,
                minimum_output_tokens=request.minimum_output_tokens,
            )
            if capability_reasons:
                rejected[
                    f"{runtime.key.provider}/{runtime.key.model}"
                ] = capability_reasons[0]
                rejected_candidates[runtime_id] = _rejection_details(
                    runtime,
                    reasons=capability_reasons,
                    rows=(),
                    request=request,
                    pre_scoring=True,
                )
                continue
            rows = tuple(self.catalog.evidence_for(runtime))
            evidence_domains = set(request.evidence_domains or (request.domain,))
            latency_rows = tuple(
                row
                for row in rows
                if row.metric_name == "latency"
                and row.domain in evidence_domains
                and row.task_definition == request.task_definition
            )
            estimated_latency = (
                max(row.value for row in latency_rows) if latency_rows else None
            )
            cost_rows = tuple(
                row
                for row in rows
                if not (
                    row.metric_name
                    in {"metered_input_price", "metered_output_price"}
                    and self.catalog.evidence_is_expired(row)
                )
            )
            economics_stale = bool(self.catalog.economics_is_stale(runtime))
            estimated_cost = (
                None
                if economics_stale
                and runtime.economics.billing_kind != "metered"
                else _estimate_task_cost(
                    runtime,
                    request,
                    rows=cost_rows,
                    include_runtime_prices=not economics_stale,
                )
            )
            price_rows = tuple(
                row
                for row in cost_rows
                if row.metric_name
                in {"metered_input_price", "metered_output_price"}
            )
            used_price_rows = (
                price_rows
                if runtime.economics.billing_kind == "metered"
                else ()
            )
            hard_reasons: list[str] = []
            capacity_uncertainty: list[str] = []
            economics = runtime.economics
            throttle = str(economics.throttle_state or "").strip().casefold()
            if throttle in {"cooldown", "exhausted", "depleted", "rate_limited"}:
                hard_reasons.append("runtime_throttled")
            elif not throttle:
                capacity_uncertainty.append("throttle_state_unknown")
            if _cooldown_is_active(
                economics.cooldown_until,
                now=self.catalog.current_time(),
            ):
                hard_reasons.append("runtime_throttled")
            if economics.billing_kind == "subscription":
                subscription_state = str(
                    economics.subscription_state or ""
                ).strip().casefold()
                remaining = economics.subscription_quota_remaining
                if subscription_state in {"exhausted", "depleted"} or (
                    remaining is not None and remaining <= 0
                ):
                    hard_reasons.append("subscription_quota_exhausted")
                elif remaining is None:
                    capacity_uncertainty.insert(0, "subscription_quota_unknown")
            if request.max_estimated_task_cost_usd is not None:
                if estimated_cost is None:
                    hard_reasons.append("estimated_cost_unknown")
                elif estimated_cost > request.max_estimated_task_cost_usd:
                    hard_reasons.append("estimated_cost_exceeds_limit")
            if request.max_estimated_latency_seconds is not None:
                if estimated_latency is None:
                    hard_reasons.append("estimated_latency_unknown")
                elif estimated_latency > request.max_estimated_latency_seconds:
                    hard_reasons.append("estimated_latency_exceeds_limit")
            score_inputs = _candidate_score_inputs(
                rows=rows,
                request=request,
                estimated_cost=estimated_cost,
                estimated_latency=estimated_latency,
                capacity_uncertainty=tuple(capacity_uncertainty),
            )
            consumed_rows = (
                *score_inputs.quality_rows,
                *score_inputs.reliability_rows,
                *latency_rows,
                *used_price_rows,
            )
            hard_gate_rows = (
                *(
                    used_price_rows
                    if request.max_estimated_task_cost_usd is not None
                    else ()
                ),
                *(
                    latency_rows
                    if request.max_estimated_latency_seconds is not None
                    else ()
                ),
            )
            staleness_penalty = round(
                float(
                    self.catalog.staleness_penalty(
                        runtime,
                        evidence=consumed_rows,
                    )
                    + self.catalog.economics_staleness_penalty(runtime)
                ),
                STALENESS_PENALTY_DECIMALS,
            )
            if hard_reasons:
                rejected[f"{runtime.key.provider}/{runtime.key.model}"] = hard_reasons[0]
                rejected_candidates[runtime_id] = _rejection_details(
                    runtime,
                    reasons=tuple(hard_reasons),
                    rows=rows,
                    request=request,
                    estimated_cost=estimated_cost,
                    estimated_latency=estimated_latency,
                    capacity_uncertainty=tuple(capacity_uncertainty),
                    hard_gate_rows=hard_gate_rows,
                    score_inputs=score_inputs,
                    staleness_penalty=staleness_penalty,
                )
                continue
            quality = score_inputs.quality
            reliability = score_inputs.reliability
            normalized_cost = score_inputs.normalized_cost
            normalized_latency = score_inputs.normalized_latency
            uncertainty_components = score_inputs.uncertainty_components
            uncertainty = score_inputs.uncertainty_penalty
            utility = utility_score(
                objectives=request.objectives,
                quality=quality.value,
                reliability=reliability.value,
                normalized_latency=normalized_latency,
                normalized_cost=normalized_cost,
                uncertainty_penalty=uncertainty,
                staleness_penalty=staleness_penalty,
            )
            candidate = ProposedCandidate(
                runtime_id=runtime_id,
                key=runtime.key,
                inventory_state=runtime.state,
                utility=utility,
            )
            details = {
                "runtime_id": runtime_id,
                "provider": runtime.key.provider,
                "model": runtime.key.model,
                "inventory_state": runtime.state,
                "inventory_reasons": tuple(runtime.reasons),
                "billing_kind": runtime.economics.billing_kind,
                "subscription_plan": _safe_optional_metadata(
                    runtime.economics.subscription_plan
                ),
                "effective_marginal_cost_usd_per_task": (
                    runtime.economics.effective_marginal_cost_usd_per_task
                ),
                "effective_amortized_cost_usd_per_task": (
                    runtime.economics.effective_amortized_cost_usd_per_task
                ),
                "subscription_quota_remaining": (
                    runtime.economics.subscription_quota_remaining
                ),
                "subscription_quota_unit": _safe_optional_metadata(
                    runtime.economics.subscription_quota_unit
                ),
                "subscription_reset_at": _safe_optional_metadata(
                    runtime.economics.subscription_reset_at
                ),
                "subscription_state": _safe_optional_metadata(
                    runtime.economics.subscription_state
                ),
                "throttle_state": _safe_optional_metadata(
                    runtime.economics.throttle_state
                ),
                "cooldown_until": _safe_optional_metadata(
                    runtime.economics.cooldown_until
                ),
                "economics_source": {
                    "source_id": _safe_metadata(runtime.economics.source_id),
                    "provenance": _safe_metadata(runtime.economics.provenance),
                    "observed_at": _safe_metadata(runtime.economics.observed_at),
                    "confidence": runtime.economics.confidence,
                },
                "estimated_cost_usd": estimated_cost,
                "estimated_latency_seconds": estimated_latency,
                "normalized_cost": normalized_cost,
                "normalized_latency": normalized_latency,
                "quality": quality.value,
                "reliability": reliability.value,
                "normalized_inputs": {
                    "quality": quality.value,
                    "reliability": reliability.value,
                    "latency": normalized_latency,
                    "cost": normalized_cost,
                },
                "missing_priors": score_inputs.missing_priors,
                "capacity_uncertainty": tuple(capacity_uncertainty),
                "sources": tuple(
                    _source_details(
                        row,
                        request=request,
                        used_for_score=row in consumed_rows,
                        used_for_hard_gate=row in hard_gate_rows,
                    )
                    for row in sorted(
                        rows,
                        key=_source_sort_key,
                    )
                ),
                "uncertainty_penalty": uncertainty,
                "uncertainty_components": uncertainty_components,
                "staleness_penalty": staleness_penalty,
                "utility": utility,
                "base_rank": request.base_ranks.get(runtime_id, 0.0),
                "tie_breaker_runtime_id": runtime_id,
            }
            ranked.append((utility, runtime_id, candidate, details))

        ranked.sort(
            key=lambda item: (
                -item[0],
                request.base_ranks.get(item[1], 0.0),
                item[1],
            )
        )
        candidates = MappingProxyType(
            {
                runtime_id: _freeze_value(details)
                for _score, runtime_id, _candidate, details in ranked
            }
        )
        proposals = tuple(item[2] for item in ranked)
        return AdvisorProposal(
            primary=proposals[0] if proposals else None,
            fallbacks=proposals[1:],
            explanation=ProposalExplanation(
                request=_freeze_value(
                    {
                        "domain": request.domain,
                        "evidence_domains": (
                            request.evidence_domains or (request.domain,)
                        ),
                        "task_definition": request.task_definition,
                        "expected_input_tokens": request.expected_input_tokens,
                        "expected_output_tokens": request.expected_output_tokens,
                        "required_capabilities": request.required_capabilities,
                        "required_modalities": request.required_modalities,
                        "minimum_context_tokens": request.minimum_context_tokens,
                        "minimum_output_tokens": request.minimum_output_tokens,
                        "objectives": request.objectives.model_dump(mode="json"),
                        "max_estimated_task_cost_usd": (
                            request.max_estimated_task_cost_usd
                        ),
                        "max_estimated_latency_seconds": (
                            request.max_estimated_latency_seconds
                        ),
                    }
                ),
                catalog=_freeze_value(
                    {
                        "snapshot_id": (
                            None
                            if self.catalog.snapshot is None
                            else self.catalog.snapshot.snapshot_id
                        ),
                        "stale_fallback": bool(
                            self.catalog.snapshot is not None
                            and self.catalog.snapshot.stale_fallback
                        ),
                    }
                ),
                candidates=candidates,
                rejected=MappingProxyType(rejected),
                rejected_candidates=MappingProxyType(
                    {
                        runtime_id: _freeze_value(details)
                        for runtime_id, details in rejected_candidates.items()
                    }
                ),
                accepted_runtime_ids=tuple(item[1] for item in ranked),
                rejected_runtime_ids=tuple(sorted(rejected_candidates)),
            ),
        )

    def dry_run(
        self,
        prompts: tuple[str, ...] | list[str],
        proposal: AdvisorProposal,
    ) -> DryRunResult:
        """Classify representative requirements without executing or persisting."""
        assessments: list[DryRunAssessment] = []
        for index, prompt in enumerate(tuple(prompts)):
            if not isinstance(prompt, str):
                raise TypeError("dry-run prompts must be strings")
            lowered = prompt.casefold()
            coding = any(
                token in lowered
                for token in ("bug", "code", "debug", "python", "test")
            )
            image = any(token in lowered for token in ("image", "photo", "vision"))
            sensitive = any(
                token in lowered
                for token in ("credential", "password", "secret", "token", "private")
            )
            assessments.append(
                DryRunAssessment(
                    prompt_index=index,
                    domains=("coding",) if coding else ("general",),
                    required_capabilities=("coding",) if coding else (),
                    required_modalities=("image",) if image else ("text",),
                    risk_class="sensitive" if sensitive else "standard",
                )
            )
        runtime_ids = tuple(
            candidate.runtime_id
            for candidate in (
                *((proposal.primary,) if proposal.primary is not None else ()),
                *proposal.fallbacks,
            )
        )
        return DryRunResult(
            proposed_runtime_ids=runtime_ids,
            assessments=tuple(assessments),
        )


def _estimate_task_cost(
    runtime: ExecutableRuntime,
    request: ProposalRequest,
    *,
    rows: tuple[Any, ...] = (),
    include_runtime_prices: bool = True,
) -> float | None:
    economics = runtime.economics
    if economics.billing_kind == "metered":
        input_prices = [
            row.value
            for row in rows
            if row.metric_name == "metered_input_price"
        ]
        output_prices = [
            row.value
            for row in rows
            if row.metric_name == "metered_output_price"
        ]
        if (
            include_runtime_prices
            and economics.metered_input_usd_per_million_tokens is not None
        ):
            input_prices.append(
                economics.metered_input_usd_per_million_tokens
            )
        if (
            include_runtime_prices
            and economics.metered_output_usd_per_million_tokens is not None
        ):
            output_prices.append(
                economics.metered_output_usd_per_million_tokens
            )
        if not input_prices or not output_prices:
            return None
        input_price = max(input_prices)
        output_price = max(output_prices)
        return (
            request.expected_input_tokens * input_price
            + request.expected_output_tokens * output_price
        ) / 1_000_000
    if economics.billing_kind == "subscription":
        estimates = tuple(
            estimate
            for estimate in (
                economics.effective_marginal_cost_usd_per_task,
                economics.effective_amortized_cost_usd_per_task,
            )
            if estimate is not None
        )
        return None if not estimates else max(estimates)
    values = (
        economics.local_compute_cost_usd_per_task,
        economics.local_energy_cost_usd_per_task,
    )
    known = tuple(value for value in values if value is not None)
    return None if not known else sum(known)


def _most_conservative_metric(rows: tuple[Any, ...]) -> ConservativeMetric:
    if not rows:
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
        for row in rows
    )
    return min(
        estimates,
        key=lambda item: (item.value, -item.uncertainty),
    )


def _candidate_score_inputs(
    *,
    rows: tuple[Any, ...],
    request: ProposalRequest,
    estimated_cost: float | None,
    estimated_latency: float | None,
    capacity_uncertainty: tuple[str, ...],
) -> _CandidateScoreInputs:
    evidence_domains = set(request.evidence_domains or (request.domain,))
    quality_rows = tuple(
        row
        for row in rows
        if row.metric_name == "quality"
        and row.domain in evidence_domains
        and row.task_definition == request.task_definition
    )
    reliability_rows = tuple(
        row
        for row in rows
        if row.metric_name == "reliability"
        and row.domain in evidence_domains
        and row.task_definition == request.task_definition
    )
    quality = _most_conservative_metric(quality_rows)
    reliability = _most_conservative_metric(reliability_rows)
    if estimated_cost is None:
        normalized_cost = 1.0
        cost_uncertainty = MISSING_METRIC_UNCERTAINTY
    elif request.max_estimated_task_cost_usd is None:
        normalized_cost = min(1.0, estimated_cost)
        cost_uncertainty = 0.0
    else:
        normalized_cost = normalize_against_limit(
            estimated_cost,
            request.max_estimated_task_cost_usd,
        )
        cost_uncertainty = 0.0
    if estimated_latency is None:
        normalized_latency = 0.5
        latency_uncertainty = MISSING_METRIC_UNCERTAINTY
    elif request.max_estimated_latency_seconds is None:
        normalized_latency = min(1.0, estimated_latency / 30.0)
        latency_uncertainty = 0.0
    else:
        normalized_latency = normalize_against_limit(
            estimated_latency,
            request.max_estimated_latency_seconds,
        )
        latency_uncertainty = 0.0
    uncertainty_components = {
        "quality": quality.uncertainty,
        "reliability": reliability.uncertainty,
        "latency": latency_uncertainty,
        "cost": cost_uncertainty,
        "capacity": len(capacity_uncertainty)
        * (MISSING_METRIC_UNCERTAINTY / 2.0),
    }
    return _CandidateScoreInputs(
        quality=quality,
        reliability=reliability,
        quality_rows=quality_rows,
        reliability_rows=reliability_rows,
        normalized_cost=normalized_cost,
        normalized_latency=normalized_latency,
        missing_priors=tuple(
            name
            for name, used_prior in (
                ("quality", quality.used_prior),
                ("reliability", reliability.used_prior),
                ("latency", estimated_latency is None),
                ("cost", estimated_cost is None),
            )
            if used_prior
        ),
        uncertainty_components=MappingProxyType(uncertainty_components),
        uncertainty_penalty=sum(uncertainty_components.values()),
    )


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _rejection_details(
    runtime: ExecutableRuntime,
    *,
    reasons: tuple[str, ...],
    rows: tuple[Any, ...],
    request: ProposalRequest,
    estimated_cost: float | None = None,
    estimated_latency: float | None = None,
    capacity_uncertainty: tuple[str, ...] = (),
    hard_gate_rows: tuple[Any, ...] = (),
    score_inputs: _CandidateScoreInputs | None = None,
    staleness_penalty: float = 0.0,
    pre_scoring: bool = False,
) -> dict[str, Any]:
    economics = runtime.economics
    if pre_scoring:
        return {
            "runtime_id": runtime.key.stable_id(),
            "provider": runtime.key.provider,
            "model": runtime.key.model,
            "inventory_state": runtime.state,
            "inventory_reasons": tuple(runtime.reasons),
            "reasons": reasons,
            "billing_kind": economics.billing_kind,
            "capabilities": _freeze_value(dict(runtime.capabilities)),
            "sources": (),
            "normalized_inputs": None,
            "utility": None,
            "hard_gate_passed": False,
        }
    if score_inputs is None:
        score_inputs = _candidate_score_inputs(
            rows=rows,
            request=request,
            estimated_cost=estimated_cost,
            estimated_latency=estimated_latency,
            capacity_uncertainty=capacity_uncertainty,
        )
    runtime_id = runtime.key.stable_id()
    pre_gate_utility = utility_score(
        objectives=request.objectives,
        quality=score_inputs.quality.value,
        reliability=score_inputs.reliability.value,
        normalized_latency=score_inputs.normalized_latency,
        normalized_cost=score_inputs.normalized_cost,
        uncertainty_penalty=score_inputs.uncertainty_penalty,
        staleness_penalty=staleness_penalty,
    )
    return {
        "runtime_id": runtime_id,
        "provider": runtime.key.provider,
        "model": runtime.key.model,
        "inventory_state": runtime.state,
        "inventory_reasons": tuple(runtime.reasons),
        "reasons": reasons,
        "billing_kind": economics.billing_kind,
        "estimated_cost_usd": estimated_cost,
        "estimated_latency_seconds": estimated_latency,
        "normalized_cost": score_inputs.normalized_cost,
        "normalized_latency": score_inputs.normalized_latency,
        "quality": score_inputs.quality.value,
        "reliability": score_inputs.reliability.value,
        "normalized_inputs": {
            "quality": score_inputs.quality.value,
            "reliability": score_inputs.reliability.value,
            "latency": score_inputs.normalized_latency,
            "cost": score_inputs.normalized_cost,
        },
        "missing_priors": score_inputs.missing_priors,
        "subscription_plan": _safe_optional_metadata(economics.subscription_plan),
        "effective_marginal_cost_usd_per_task": (
            economics.effective_marginal_cost_usd_per_task
        ),
        "effective_amortized_cost_usd_per_task": (
            economics.effective_amortized_cost_usd_per_task
        ),
        "subscription_quota_remaining": economics.subscription_quota_remaining,
        "subscription_quota_unit": _safe_optional_metadata(
            economics.subscription_quota_unit
        ),
        "subscription_reset_at": _safe_optional_metadata(
            economics.subscription_reset_at
        ),
        "subscription_state": _safe_optional_metadata(economics.subscription_state),
        "throttle_state": _safe_optional_metadata(economics.throttle_state),
        "cooldown_until": _safe_optional_metadata(economics.cooldown_until),
        "capacity_uncertainty": capacity_uncertainty,
        "economics_source": {
            "source_id": _safe_metadata(economics.source_id),
            "provenance": _safe_metadata(economics.provenance),
            "observed_at": _safe_metadata(economics.observed_at),
            "confidence": economics.confidence,
        },
        "sources": tuple(
            _source_details(
                row,
                request=request,
                used_for_score=False,
                used_for_hard_gate=row in hard_gate_rows,
            )
            for row in sorted(
                rows,
                key=_source_sort_key,
            )
        ),
        "uncertainty": tuple(
            dict.fromkeys(
                (
                    *capacity_uncertainty,
                    *(
                        reason
                        for reason in reasons
                        if "unknown" in reason or runtime.state != "verified"
                    ),
                )
            )
        )
        or ("hard_gate_rejected",),
        "uncertainty_penalty": score_inputs.uncertainty_penalty,
        "uncertainty_components": score_inputs.uncertainty_components,
        "staleness_penalty": staleness_penalty,
        "utility": pre_gate_utility,
        "base_rank": request.base_ranks.get(runtime_id, 0.0),
        "tie_breaker_runtime_id": runtime_id,
        "hard_gate_passed": False,
    }


def _source_details(
    row: Any,
    *,
    request: ProposalRequest,
    used_for_score: bool,
    used_for_hard_gate: bool = False,
) -> dict[str, Any]:
    normalized_value: float | None = None
    conservative_value: float | None = None
    if row.metric_name in {"quality", "reliability"} or row.metric_name.startswith(
        "capability_"
    ):
        normalized_value = normalize_catalog_metric(
            value=row.value,
            direction=row.metric_direction,
            scale=row.metric_scale,
            normalization_method=row.normalization_method,
        )
        conservative_value = conservative_metric(
            normalized_value,
            confidence=row.confidence,
            sample_size=row.sample_size,
        ).value
    elif row.metric_name == "latency":
        if request.max_estimated_latency_seconds is None:
            normalized_value = min(1.0, row.value / 30.0)
        else:
            normalized_value = normalize_against_limit(
                row.value,
                request.max_estimated_latency_seconds,
            )
        conservative_value = normalized_value
    elif row.metric_name in {
        "metered_input_price",
        "metered_output_price",
    }:
        conservative_value = row.value
    return {
        "source_id": row.source_id,
        "source_url": row.source_url,
        "retrieved_at": row.retrieved_at,
        "published_at": row.published_at,
        "expires_at": row.expires_at,
        "model": row.model,
        "model_version": row.model_version,
        "domain": row.domain,
        "task_definition": row.task_definition,
        "metric_name": row.metric_name,
        "metric_direction": row.metric_direction,
        "metric_scale": row.metric_scale,
        "value": row.value,
        "sample_size": row.sample_size,
        "confidence": row.confidence,
        "normalization_method": row.normalization_method,
        "used_for_score": used_for_score,
        "used_for_hard_gate": used_for_hard_gate,
        "used_for_decision": used_for_score or used_for_hard_gate,
        "normalized_value": normalized_value,
        "conservative_value": conservative_value,
    }


def _source_sort_key(row: Any) -> tuple[Any, ...]:
    return (
        row.source_id,
        row.metric_name,
        row.retrieved_at,
        row.source_url,
        row.published_at,
        row.expires_at or "",
        row.model,
        row.model_version,
        row.domain,
        row.task_definition,
        row.metric_direction,
        row.metric_scale,
        row.normalization_method,
        row.value,
        -1 if row.sample_size is None else row.sample_size,
        -1.0 if row.confidence is None else row.confidence,
    )


def _safe_metadata(value: str) -> str:
    return "unsafe-metadata-redacted" if _UNSAFE_METADATA.search(value) else value


def _safe_optional_metadata(value: str | None) -> str | None:
    return None if value is None else _safe_metadata(value)


def _cooldown_is_active(value: str | None, *, now: datetime) -> bool:
    if value is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None:
        return False
    return parsed > now


__all__ = [
    "ADVISOR_REQUIRED_FACTS",
    "Advisor",
    "AdvisorAccessPaths",
    "AdvisorProfileIntent",
    "AdvisorProfileRequest",
    "AdvisorProposal",
    "AdvisorRankingRequest",
    "AdvisorReadiness",
    "AdvisorRequest",
    "DryRunAssessment",
    "DryRunResult",
    "MAX_EXPECTED_TASK_TOKENS",
    "ProposalExplanation",
    "ProposalRequest",
    "ProposedCandidate",
    "STALENESS_PENALTY_DECIMALS",
]
