"""Content-free task facts and deterministic configured-rule evaluation."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from agent.plugin_llm import PluginLlmImageInput, PluginLlmTextInput

from .models import (
    RoutingRule,
    RoutingVocabulary,
    SafeDefaultReasonCode,
    TaskAssessment,
    TaskFacts,
)


MAX_TASK_FACT_BLOCKS = 64
MAX_DECLARED_CHILD_TOOLS = 64
MAX_DECLARED_CHILD_TOOL_LENGTH = 256

_FACT_METADATA_FIELDS = frozenset({
    "complexity",
    "declared_child_tools",
    "domains",
    "required_capabilities",
    "required_modalities",
    "risk_class",
})
_ASSESSMENT_FIELDS = tuple(TaskAssessment.model_fields)
_FACT_FIELDS = (
    "complexity",
    "domains",
    "required_capabilities",
    "required_modalities",
    "risk_class",
)


class _TaskClassifier(Protocol):
    def classify(self, task: object, *, facts: TaskFacts) -> Any: ...


@dataclass(frozen=True)
class RuleEvaluation:
    """Deterministic partial or complete assessment plus route constraints."""

    profile_id: str | None
    preferred_profile_ids: tuple[str, ...]
    applied_rule_ids: tuple[str, ...]
    assessment: TaskAssessment | None
    is_complete: bool
    safe_default_reason: SafeDefaultReasonCode | None = None
    classifier_runtime_id: str | None = None
    classifier_input_tokens: int = 0
    classifier_output_tokens: int = 0
    classifier_cost_usd: float | None = None


def extract_task_facts(
    *,
    scope: str,
    task: object,
    metadata: Mapping[str, object] | None = None,
    platform: str = "unknown",
) -> TaskFacts:
    """Extract only host-declared metadata and deterministic message shape.

    Task text, file names, URLs, bytes, and provider/model words are never
    inspected for semantic meaning and never enter the returned record.
    """

    document = _validate_metadata(metadata)
    detected_modalities = _task_modalities(task)
    domains = _bounded_labels(document.get("domains", ()), field="domains")
    capabilities = set(_bounded_labels(
        document.get("required_capabilities", ()),
        field="required_capabilities",
    ))
    declared_modalities = _bounded_labels(
        document.get("required_modalities", ()),
        field="required_modalities",
    )
    child_tools = _declared_child_tools(document.get("declared_child_tools", ()))
    if child_tools:
        capabilities.add("tools")

    complexity = document.get("complexity")
    if complexity is not None and (
        type(complexity) is not float
        or not math.isfinite(complexity)
        or not 0.0 <= complexity <= 1.0
    ):
        raise ValueError("task metadata complexity must be a finite unit float")

    risk_class = document.get("risk_class")
    if risk_class is not None and not isinstance(risk_class, str):
        raise ValueError("task metadata risk_class must be a string")

    return TaskFacts(
        scope=scope,
        platform=platform,
        domains=tuple(sorted(domains)),
        required_capabilities=tuple(sorted(capabilities)),
        required_modalities=tuple(sorted(
            set(detected_modalities) | set(declared_modalities)
        )),
        risk_class=(None if risk_class is None else risk_class.strip().casefold()),
        complexity=complexity,
    )


def task_facts_hash(facts: TaskFacts) -> str:
    """Return a canonical hash containing no task content."""

    payload = facts.model_dump(mode="json")
    for field in ("domains", "required_capabilities", "required_modalities"):
        payload[field] = sorted(payload[field])
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_rules(
    rules: Sequence[RoutingRule],
    *,
    facts: TaskFacts,
    assessment: TaskAssessment | None,
    vocabulary: RoutingVocabulary | None = None,
) -> RuleEvaluation:
    """Apply matching rules in canonical ``(-priority, rule_id)`` order."""

    canonical = tuple(sorted(rules, key=lambda rule: (-rule.priority, rule.rule_id)))
    rule_ids = tuple(rule.rule_id for rule in canonical)
    if len(rule_ids) != len(set(rule_ids)):
        return _invalid_rule_evaluation()

    profile_id: str | None = None
    preferences: list[str] = []
    applied_rule_ids: list[str] = []
    overrides: dict[str, object] = {}

    for rule in canonical:
        if not _matches(rule, facts):
            continue
        applied_rule_ids.append(rule.rule_id)
        for field in _ASSESSMENT_FIELDS:
            value = getattr(rule.assessment_overrides, field)
            if value is not None and field not in overrides:
                overrides[field] = value
        if rule.effect == "prefer_profile":
            if rule.profile_id not in preferences:
                preferences.append(rule.profile_id)
            continue
        profile_id = rule.profile_id
        break

    merged, invalid = _build_assessment(
        facts=facts,
        overrides=overrides,
        classified=assessment,
        vocabulary=vocabulary,
    )
    return RuleEvaluation(
        profile_id=profile_id,
        preferred_profile_ids=tuple(preferences),
        applied_rule_ids=tuple(applied_rule_ids),
        assessment=merged,
        is_complete=merged is not None,
        safe_default_reason="rule_invalid" if invalid else None,
    )


def merge_facts_and_assessment(
    facts: TaskFacts,
    classified: TaskAssessment,
) -> TaskAssessment:
    """Overlay resolved deterministic facts on one complete classification."""

    merged, invalid = _build_assessment(
        facts=facts,
        overrides={},
        classified=classified,
        vocabulary=None,
    )
    if invalid or merged is None:  # pragma: no cover - validated inputs invariant
        raise ValueError("deterministic task facts produced an invalid assessment")
    return merged


def assess_with_rules(
    rules: Sequence[RoutingRule],
    *,
    task: object,
    facts: TaskFacts,
    classifier: _TaskClassifier,
    vocabulary: RoutingVocabulary | None = None,
) -> RuleEvaluation:
    """Classify only when facts and explicit rule overrides remain incomplete."""

    deterministic = evaluate_rules(
        rules,
        facts=facts,
        assessment=None,
        vocabulary=vocabulary,
    )
    if deterministic.is_complete or deterministic.safe_default_reason is not None:
        return deterministic

    try:
        outcome = classifier.classify(task, facts=facts)
    except Exception:
        return replace(
            deterministic,
            safe_default_reason="classifier_failed",
        )

    classified = getattr(outcome, "assessment", None)
    if not isinstance(classified, TaskAssessment):
        reason = getattr(outcome, "safe_default_reason", None)
        if reason not in {
            "classifier_budget",
            "classifier_failed",
            "classifier_malformed",
            "classifier_oversized",
            "classifier_timeout",
            "classifier_trust",
            "classifier_unknown_price",
        }:
            reason = "classifier_failed"
        return _with_classifier_evidence(
            deterministic,
            outcome,
            safe_default_reason=reason,
        )

    completed = evaluate_rules(
        rules,
        facts=facts,
        assessment=classified,
        vocabulary=vocabulary,
    )
    return _with_classifier_evidence(completed, outcome)


def _validate_metadata(
    metadata: Mapping[str, object] | None,
) -> Mapping[str, object]:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise TypeError("task metadata must be a mapping")
    if len(metadata) > len(_FACT_METADATA_FIELDS):
        raise ValueError("task metadata contains unsupported fields")
    for key in metadata:
        if not isinstance(key, str) or key not in _FACT_METADATA_FIELDS:
            raise ValueError("task metadata contains an unsupported field")
    return metadata


def _bounded_labels(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"task metadata {field} must be a list or tuple")
    if len(value) > 64:
        raise ValueError(f"task metadata {field} exceeds its count bound")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"task metadata {field} must contain only strings")
    normalized = tuple(item.strip().casefold() for item in value)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"task metadata {field} contains duplicate labels")
    return normalized


def _declared_child_tools(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("declared_child_tools must be a list or tuple")
    if len(value) > MAX_DECLARED_CHILD_TOOLS:
        raise ValueError("declared_child_tools exceeds its count bound")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("declared_child_tools must contain only strings")
        name = item.strip().casefold()
        if not name or len(name) > MAX_DECLARED_CHILD_TOOL_LENGTH:
            raise ValueError("declared_child_tools contains an invalid name")
        normalized.append(name)
    if len(normalized) != len(set(normalized)):
        raise ValueError("declared_child_tools contains duplicate names")
    return tuple(sorted(normalized))


def _task_modalities(task: object) -> tuple[str, ...]:
    if isinstance(task, str):
        return ("text",)
    if isinstance(task, (PluginLlmTextInput, PluginLlmImageInput, Mapping)):
        blocks = (task,)
    elif isinstance(task, (list, tuple)):
        blocks = tuple(task)
    else:
        raise TypeError("task must be text or a bounded sequence of input blocks")
    if not blocks or len(blocks) > MAX_TASK_FACT_BLOCKS:
        raise ValueError("task input block count is outside the supported bound")

    modalities: set[str] = set()
    for block in blocks:
        if isinstance(block, PluginLlmTextInput):
            if not isinstance(block.text, str):
                raise ValueError("text input must contain a string")
            modalities.add("text")
            continue
        if isinstance(block, PluginLlmImageInput):
            if block.data is None and not block.url:
                raise ValueError("image input requires bytes or a URL")
            if block.data is not None and not isinstance(block.data, (bytes, bytearray)):
                raise ValueError("image input data must be bytes")
            if block.url is not None and not isinstance(block.url, str):
                raise ValueError("image input URL must be a string")
            modalities.add("image")
            continue
        if not isinstance(block, Mapping) or len(block) > 16:
            raise TypeError("task input blocks must be bounded mappings")
        kind = block.get("type")
        if not isinstance(kind, str):
            raise ValueError("task input block type must be a string")
        normalized_kind = kind.strip().casefold()
        if normalized_kind in {"text", "input_text"}:
            if not isinstance(block.get("text"), str):
                raise ValueError("text input must contain a string")
            modalities.add("text")
        elif normalized_kind in {"image", "image_url", "input_image"}:
            _require_binary_or_reference(block, kind="image")
            modalities.add("image")
        elif normalized_kind in {"audio", "input_audio"}:
            _require_binary_or_reference(block, kind="audio")
            modalities.add("audio")
        elif normalized_kind in {"document", "file", "input_file"}:
            _require_binary_or_reference(block, kind="document")
            modalities.add("document")
        else:
            raise ValueError("task input block has an unsupported type")
    return tuple(sorted(modalities))


def _require_binary_or_reference(block: Mapping[object, object], *, kind: str) -> None:
    data = block.get("data")
    url = block.get("url")
    if kind == "image" and url is None:
        image_url = block.get("image_url")
        if isinstance(image_url, Mapping):
            url = image_url.get("url")
    reference = block.get("file_id")
    if data is None and url is None and reference is None:
        raise ValueError(f"{kind} input requires bytes or a reference")
    if data is not None and not isinstance(data, (bytes, bytearray)):
        raise ValueError(f"{kind} input data must be bytes")
    if url is not None and not isinstance(url, str):
        raise ValueError(f"{kind} input URL must be a string")
    if reference is not None and not isinstance(reference, str):
        raise ValueError(f"{kind} input reference must be a string")


def _matches(rule: RoutingRule, facts: TaskFacts) -> bool:
    predicate = rule.when
    if predicate.scopes and facts.scope not in predicate.scopes:
        return False
    if predicate.platforms and facts.platform not in predicate.platforms:
        return False
    if predicate.domains_any and not set(predicate.domains_any) & set(facts.domains):
        return False
    if predicate.required_capabilities_all and not set(
        predicate.required_capabilities_all
    ).issubset(facts.required_capabilities):
        return False
    if predicate.required_modalities_any and not set(
        predicate.required_modalities_any
    ) & set(facts.required_modalities):
        return False
    if predicate.risk_classes and facts.risk_class not in predicate.risk_classes:
        return False
    if predicate.minimum_complexity is not None and (
        facts.complexity is None or facts.complexity < predicate.minimum_complexity
    ):
        return False
    if predicate.maximum_complexity is not None and (
        facts.complexity is None or facts.complexity > predicate.maximum_complexity
    ):
        return False
    return True


def _build_assessment(
    *,
    facts: TaskFacts,
    overrides: Mapping[str, object],
    classified: TaskAssessment | None,
    vocabulary: RoutingVocabulary | None,
) -> tuple[TaskAssessment | None, bool]:
    values = {} if classified is None else classified.model_dump(mode="python")
    values.update(overrides)
    for field in _FACT_FIELDS:
        value = getattr(facts, field)
        if value is not None and (not isinstance(value, tuple) or value):
            values[field] = value

    if vocabulary is not None and not _vocabulary_allows(values, vocabulary):
        return None, True
    if any(field not in values for field in _ASSESSMENT_FIELDS):
        return None, False
    try:
        return TaskAssessment.model_validate(values), False
    except Exception:
        return None, True


def _vocabulary_allows(
    values: Mapping[str, object],
    vocabulary: RoutingVocabulary,
) -> bool:
    capabilities = values.get("required_capabilities")
    modalities = values.get("required_modalities")
    return (
        not isinstance(capabilities, (list, tuple))
        or set(capabilities).issubset(vocabulary.capabilities)
    ) and (
        not isinstance(modalities, (list, tuple))
        or set(modalities).issubset(vocabulary.modalities)
    )


def _invalid_rule_evaluation() -> RuleEvaluation:
    return RuleEvaluation(
        profile_id=None,
        preferred_profile_ids=(),
        applied_rule_ids=(),
        assessment=None,
        is_complete=False,
        safe_default_reason="rule_invalid",
    )


def _with_classifier_evidence(
    evaluation: RuleEvaluation,
    outcome: object,
    *,
    safe_default_reason: SafeDefaultReasonCode | None = None,
) -> RuleEvaluation:
    return replace(
        evaluation,
        safe_default_reason=safe_default_reason or evaluation.safe_default_reason,
        classifier_runtime_id=getattr(outcome, "classifier_runtime_id", None),
        classifier_input_tokens=getattr(outcome, "classifier_input_tokens", 0),
        classifier_output_tokens=getattr(outcome, "classifier_output_tokens", 0),
        classifier_cost_usd=getattr(outcome, "classifier_cost_usd", None),
    )


__all__ = [
    "MAX_DECLARED_CHILD_TOOLS",
    "MAX_TASK_FACT_BLOCKS",
    "RuleEvaluation",
    "assess_with_rules",
    "evaluate_rules",
    "extract_task_facts",
    "merge_facts_and_assessment",
    "task_facts_hash",
]
