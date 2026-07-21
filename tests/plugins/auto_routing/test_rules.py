"""Deterministic task-fact extraction and configured rule precedence."""

from __future__ import annotations

from types import SimpleNamespace
import math

import pytest

from plugins.auto_routing.auto_routing.models import (
    RoutingRule,
    RuleAssessmentOverrides,
    RulePredicate,
    TaskAssessment,
    TaskFacts,
)
from plugins.auto_routing.auto_routing.rules import (
    assess_with_rules,
    evaluate_rules,
    extract_task_facts,
    merge_facts_and_assessment,
    task_facts_hash,
)


def _facts(**changes: object) -> TaskFacts:
    values: dict[str, object] = {
        "scope": "delegation",
        "platform": "cli",
        "domains": ("coding",),
        "required_capabilities": ("tools",),
        "required_modalities": ("text",),
        "risk_class": "moderate",
        "complexity": 0.8,
    }
    values.update(changes)
    return TaskFacts.model_validate(values)


def _assessment(**changes: object) -> TaskAssessment:
    values: dict[str, object] = {
        "complexity": 0.4,
        "domains": ("general",),
        "required_capabilities": ("structured_output",),
        "required_modalities": ("text",),
        "expected_context_tokens": 2_000,
        "expected_output_tokens": 500,
        "quality_sensitivity": 0.6,
        "reliability_sensitivity": 0.7,
        "latency_sensitivity": 0.3,
        "cost_sensitivity": 0.2,
        "risk_class": "low",
        "confidence": 0.75,
    }
    values.update(changes)
    return TaskAssessment.model_validate(values)


def _complete_overrides(**changes: object) -> RuleAssessmentOverrides:
    values: dict[str, object] = {
        "expected_context_tokens": 4_000,
        "expected_output_tokens": 800,
        "quality_sensitivity": 0.8,
        "reliability_sensitivity": 0.9,
        "latency_sensitivity": 0.2,
        "cost_sensitivity": 0.1,
        "confidence": 1.0,
    }
    values.update(changes)
    return RuleAssessmentOverrides.model_validate(values)


def _rule(
    rule_id: str,
    *,
    priority: int,
    profile_id: str,
    effect: str = "prefer_profile",
    when: RulePredicate | None = None,
    overrides: RuleAssessmentOverrides | None = None,
) -> RoutingRule:
    return RoutingRule(
        rule_id=rule_id,
        priority=priority,
        profile_id=profile_id,
        effect=effect,
        when=when or RulePredicate(),
        assessment_overrides=overrides or RuleAssessmentOverrides(),
    )


class _ClassifierSpy:
    def __init__(self, assessment: TaskAssessment | None) -> None:
        self.assessment = assessment
        self.calls: list[tuple[object, TaskFacts]] = []

    def classify(self, task: object, *, facts: TaskFacts) -> SimpleNamespace:
        self.calls.append((task, facts))
        return SimpleNamespace(
            assessment=self.assessment,
            safe_default_reason=(
                None if self.assessment is not None else "classifier_failed"
            ),
            classifier_runtime_id=None,
            classifier_input_tokens=0,
            classifier_output_tokens=0,
            classifier_cost_usd=None,
            clarification_requested=False,
        )


def test_extract_task_facts_is_canonical_content_free_and_shape_only() -> None:
    sentinel = "RAW_TASK_gpt-5_openrouter_secret"
    first = extract_task_facts(
        scope="delegation",
        platform="CLI",
        task=[
            {"type": "image", "data": b"not-persisted", "file_name": "claude.png"},
            {"type": "text", "text": sentinel},
            {"type": "document", "data": b"private-document"},
            {"type": "audio", "data": b"private-audio"},
        ],
        metadata={
            "domains": ["Debugging", "Coding"],
            "required_capabilities": ["code_execution"],
            "declared_child_tools": ["terminal"],
            "risk_class": "high",
            "complexity": 0.9,
        },
    )
    second = extract_task_facts(
        scope="delegation",
        platform="cli",
        task=[
            {"type": "audio", "data": b"different"},
            {"type": "document", "data": b"different"},
            {"type": "text", "text": "semantically different raw content"},
            {"type": "image", "data": b"different", "file_name": "other.png"},
        ],
        metadata={
            "complexity": 0.9,
            "risk_class": "high",
            "declared_child_tools": ["terminal"],
            "required_capabilities": ["code_execution"],
            "domains": ["coding", "debugging"],
        },
    )

    assert first == second
    assert first.domains == ("coding", "debugging")
    assert first.required_capabilities == ("code_execution", "tools")
    assert first.required_modalities == ("audio", "document", "image", "text")
    assert task_facts_hash(first) == task_facts_hash(second)
    rendered = repr(first) + task_facts_hash(first)
    for forbidden in (sentinel, "gpt-5", "openrouter", "claude.png"):
        assert forbidden not in rendered


def test_text_or_identity_labels_do_not_infer_domain_capability_or_vendor() -> None:
    facts = extract_task_facts(
        scope="fresh_session",
        task=[
            {
                "type": "text",
                "text": "Use Claude and GPT-5 to debug Python with OpenRouter",
            },
            {
                "type": "image",
                "data": b"x",
                "file_name": "coding-openai-secret.png",
            },
        ],
    )

    assert facts.platform == "unknown"
    assert facts.domains == ()
    assert facts.required_capabilities == ()
    assert facts.required_modalities == ("image", "text")


def test_extract_task_facts_never_logs_raw_task_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "RAW_FACT_SENTINEL_2f0d843c"

    facts = extract_task_facts(
        scope="fresh_session",
        task={"type": "text", "text": sentinel},
    )

    assert facts.required_modalities == ("text",)
    assert sentinel not in caplog.text


@pytest.mark.parametrize(
    "complexity",
    [True, "0.5", math.nan, math.inf, -math.inf, -0.01, 1.01],
)
def test_extract_task_facts_rejects_non_strict_or_non_finite_complexity(
    complexity: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        extract_task_facts(
            scope="delegation",
            task="task",
            metadata={"complexity": complexity},
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {"domains": ["x" * 65]},
        {"domains": [f"domain_{index}" for index in range(65)]},
        {"domains": ["coding", 7]},
        {"required_capabilities": [True]},
        {"declared_child_tools": [f"tool-{index}" for index in range(65)]},
    ],
)
def test_extract_task_facts_enforces_label_and_child_tool_bounds(
    metadata: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        extract_task_facts(scope="delegation", task="task", metadata=metadata)


def test_extract_task_facts_rejects_excessive_block_count() -> None:
    task = [{"type": "text", "text": "x"} for _ in range(65)]

    with pytest.raises((TypeError, ValueError)):
        extract_task_facts(scope="delegation", task=task)


def test_extract_task_facts_rejects_pathological_nested_shapes_without_coercion() -> None:
    class _MustNotCoerce:
        def __str__(self) -> str:
            raise AssertionError("task facts must not coerce arbitrary objects")

        def __repr__(self) -> str:
            raise AssertionError("task facts must not repr arbitrary objects")

    with pytest.raises((TypeError, ValueError)):
        extract_task_facts(
            scope="delegation",
            task={"type": "text", "text": {"nested": _MustNotCoerce()}},
        )

    with pytest.raises((TypeError, ValueError)):
        extract_task_facts(
            scope="delegation",
            task=_MustNotCoerce(),
        )


@pytest.mark.parametrize(
    ("task", "metadata"),
    [
        ([{"type": "unknown", "text": "x"}], {}),
        ([{"type": "text", "text": 3}], {}),
        ("task", {"complexity": True}),
        ("task", {"complexity": "0.5"}),
        ("task", {"unexpected_raw_text": "secret"}),
        ("task", {"declared_child_tools": ["terminal", 7]}),
    ],
)
def test_extract_task_facts_rejects_malformed_or_non_allowlisted_shapes(
    task: object,
    metadata: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        extract_task_facts(scope="delegation", task=task, metadata=metadata)


def test_rules_use_canonical_order_pin_authority_and_unique_preferences() -> None:
    facts = _facts()
    rules = (
        _rule("z-late", priority=10, profile_id="late"),
        _rule("b-pin", priority=20, profile_id="pinned", effect="pin_profile"),
        _rule("a-first", priority=20, profile_id="preferred"),
        _rule("a-duplicate-profile", priority=15, profile_id="preferred"),
    )

    result = evaluate_rules(rules, facts=facts, assessment=None)

    assert result.profile_id == "pinned"
    assert result.preferred_profile_ids == ("preferred",)
    assert result.applied_rule_ids == ("a-first", "b-pin")
    assert result.is_complete is False


def test_rule_predicates_are_conjunctive_and_complexity_bounds_are_inclusive() -> None:
    rule = _rule(
        "all-dimensions",
        priority=1,
        profile_id="coding",
        effect="pin_profile",
        when=RulePredicate(
            scopes=("delegation",),
            platforms=("cli",),
            domains_any=("coding", "math"),
            required_capabilities_all=("tools",),
            required_modalities_any=("text", "image"),
            risk_classes=("moderate",),
            minimum_complexity=0.8,
            maximum_complexity=0.8,
        ),
    )

    assert evaluate_rules((rule,), facts=_facts(), assessment=None).profile_id == "coding"
    assert (
        evaluate_rules(
            (rule,),
            facts=_facts(required_capabilities=("structured_output",)),
            assessment=None,
        ).profile_id
        is None
    )


def test_complete_deterministic_rule_skips_classifier() -> None:
    classifier = _ClassifierSpy(_assessment())
    rule = _rule(
        "explicit",
        priority=100,
        profile_id="coding",
        effect="pin_profile",
        when=RulePredicate(domains_any=("coding",)),
        overrides=_complete_overrides(),
    )

    result = assess_with_rules(
        (rule,),
        task="repair it",
        facts=_facts(),
        classifier=classifier,
    )

    assert result.profile_id == "coding"
    assert result.is_complete is True
    assert result.assessment is not None
    assert classifier.calls == []


def test_partial_pin_survives_while_classifier_fills_unresolved_fields() -> None:
    classified = _assessment(
        domains=("classifier-domain",),
        expected_output_tokens=1_234,
    )
    classifier = _ClassifierSpy(classified)
    rule = _rule(
        "partial",
        priority=100,
        profile_id="coding",
        effect="pin_profile",
        overrides=RuleAssessmentOverrides(quality_sensitivity=0.95),
    )

    result = assess_with_rules(
        (rule,),
        task="repair it",
        facts=_facts(domains=(), required_capabilities=()),
        classifier=classifier,
    )

    assert result.profile_id == "coding"
    assert result.applied_rule_ids == ("partial",)
    assert result.assessment is not None
    assert result.assessment.domains == ("classifier-domain",)
    assert result.assessment.expected_output_tokens == 1_234
    assert result.assessment.quality_sensitivity == 0.95
    assert len(classifier.calls) == 1


def test_deterministic_facts_override_rule_and_classifier_claims() -> None:
    facts = _facts(
        complexity=0.8,
        required_modalities=("image",),
        risk_class="critical",
    )
    classified = _assessment(
        complexity=0.1,
        required_modalities=("text",),
        risk_class="low",
    )
    rule = _rule(
        "contradictory",
        priority=1,
        profile_id="coding",
        overrides=RuleAssessmentOverrides(
            complexity=0.5,
            required_modalities=("audio",),
            risk_class="high",
        ),
    )

    result = evaluate_rules((rule,), facts=facts, assessment=classified)

    assert result.assessment is not None
    assert result.assessment.complexity == 0.8
    assert result.assessment.required_modalities == ("image",)
    assert result.assessment.risk_class == "critical"


def test_empty_facts_are_unresolved_but_explicit_empty_rule_override_replaces() -> None:
    facts = _facts(domains=(), required_capabilities=())
    classified = _assessment(
        domains=("classified",),
        required_capabilities=("structured_output",),
    )

    merged = merge_facts_and_assessment(facts, classified)
    explicit = evaluate_rules(
        (
            _rule(
                "clear",
                priority=1,
                profile_id="minimal",
                overrides=RuleAssessmentOverrides(
                    domains=(),
                    required_capabilities=(),
                ),
            ),
        ),
        facts=facts,
        assessment=classified,
    )

    assert merged.domains == ("classified",)
    assert merged.required_capabilities == ("structured_output",)
    assert explicit.assessment is not None
    assert explicit.assessment.domains == ()
    assert explicit.assessment.required_capabilities == ()


def test_first_explicit_override_wins_in_canonical_rule_order() -> None:
    classified = _assessment(quality_sensitivity=0.1, cost_sensitivity=0.1)
    higher = _rule(
        "higher",
        priority=20,
        profile_id="one",
        overrides=RuleAssessmentOverrides(quality_sensitivity=0.9),
    )
    lower = _rule(
        "lower",
        priority=10,
        profile_id="two",
        overrides=RuleAssessmentOverrides(
            quality_sensitivity=0.2,
            cost_sensitivity=0.8,
        ),
    )

    result = evaluate_rules((lower, higher), facts=_facts(), assessment=classified)

    assert result.assessment is not None
    assert result.assessment.quality_sensitivity == 0.9
    assert result.assessment.cost_sensitivity == 0.8
    assert result.applied_rule_ids == ("higher", "lower")


def test_classifier_failure_is_returned_without_losing_partial_rule_state() -> None:
    classifier = _ClassifierSpy(None)
    rule = _rule(
        "pin",
        priority=1,
        profile_id="coding",
        effect="pin_profile",
    )

    result = assess_with_rules(
        (rule,),
        task="task",
        facts=_facts(domains=(), required_capabilities=(), risk_class=None),
        classifier=classifier,
    )

    assert result.profile_id == "coding"
    assert result.assessment is None
    assert result.is_complete is False
    assert result.safe_default_reason == "classifier_failed"
    assert len(classifier.calls) == 1
