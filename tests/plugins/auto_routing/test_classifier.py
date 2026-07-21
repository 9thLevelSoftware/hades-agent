"""Bounded, content-safe structured task-classifier contracts."""

from __future__ import annotations

import inspect
import json
import logging
import math
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.plugin_llm import (
    PluginLlmImageInput,
    PluginLlmStructuredResult,
    PluginLlmTextInput,
    PluginLlmTrustError,
    PluginLlmUsage,
    _TrustPolicy,
    make_plugin_llm_for_test,
)
from agent.reasoning_support import ReasoningSupport
from plugins.auto_routing.auto_routing.classifier import StructuredTaskClassifier
from plugins.auto_routing.auto_routing.inventory import ExecutableRuntime
from plugins.auto_routing.auto_routing.models import (
    AccessEconomics,
    ClassifierSettings,
    LocalModelRequirements,
    PolicyEnvelope,
    RoutingVocabulary,
    RuntimeKey,
    TaskAssessment,
    TaskFacts,
)
from plugins.auto_routing.auto_routing.storage import RoutingStore


def _settings(**changes: object) -> ClassifierSettings:
    values: dict[str, object] = {
        "provider": "classifier-provider",
        "model": "classifier-model",
        "reasoning_effort": "low",
        "timeout_seconds": 3.0,
        "disclosure": "full",
        "maximum_input_tokens": 8_192,
        "maximum_output_tokens": 128,
        "maximum_image_count": 2,
        "maximum_image_bytes": 1_024,
    }
    values.update(changes)
    return ClassifierSettings.model_validate(values)


def _policy(**changes: object) -> PolicyEnvelope:
    values: dict[str, object] = {
        "eligible_sources": ("configured_providers", "installed_local_models"),
        "uninstalled_local_models": "deny",
        "local_models": LocalModelRequirements(
            require_open_weights=True,
            require_compatible_hardware=True,
        ),
        "denied_providers": (),
        "denied_models": (),
        "max_estimated_task_cost_usd": 5.0,
        "max_estimated_latency_seconds": 120.0,
        "max_routing_overhead_usd_per_day": 1.0,
        "max_experiment_cost_usd_per_day": 0.0,
        "max_evaluator_calls_per_day": 0,
        "max_canary_fraction": 0.0,
        "max_reasoning_effort": "high",
        "allow_subscription": True,
        "allow_paid_access_probes": False,
        "allowed_licenses": (),
        "minimum_context_tokens": 0,
        "canary_high_risk_tasks": False,
    }
    values.update(changes)
    return PolicyEnvelope.model_validate(values)


def _vocabulary() -> RoutingVocabulary:
    return RoutingVocabulary(
        capabilities=("tools", "structured_output", "code_execution"),
        modalities=("text", "image", "audio", "document"),
    )


def _facts(**changes: object) -> TaskFacts:
    values: dict[str, object] = {
        "scope": "delegation",
        "platform": "cli",
        "domains": (),
        "required_capabilities": (),
        "required_modalities": ("text",),
        "risk_class": None,
        "complexity": None,
    }
    values.update(changes)
    return TaskFacts.model_validate(values)


def _assessment_document(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "complexity": 0.55,
        "domains": ["coding"],
        "required_capabilities": ["tools"],
        "required_modalities": ["text"],
        "expected_context_tokens": 2_000,
        "expected_output_tokens": 400,
        "quality_sensitivity": 0.7,
        "reliability_sensitivity": 0.8,
        "latency_sensitivity": 0.3,
        "cost_sensitivity": 0.2,
        "risk_class": "moderate",
        "confidence": 0.8,
    }
    values.update(changes)
    return values


def _result(
    *,
    parsed: object | None = None,
    usage: PluginLlmUsage | None = None,
    provider: str = "classifier-provider",
    model: str = "classifier-model",
    text: str | None = None,
) -> PluginLlmStructuredResult:
    document = _assessment_document() if parsed is None else parsed
    return PluginLlmStructuredResult(
        text=json.dumps(document) if text is None else text,
        provider=provider,
        model=model,
        agent_id="default",
        usage=usage or PluginLlmUsage(input_tokens=100, output_tokens=20, total_tokens=120),
        parsed=document,
        content_type="json",
    )


def _runtime(mutable_clock, **changes: object) -> ExecutableRuntime:
    observed_at = mutable_clock.now().isoformat().replace("+00:00", "Z")
    key = RuntimeKey(
        provider="classifier-provider",
        model="classifier-model",
        auth_identity="api_key:classifier",
        credential_pool_identity="pool:classifier",
        endpoint_identity="endpoint:classifier",
        api_mode="chat_completions",
        local_backend="",
        inventory_revision="inventory-1",
    )
    economics = AccessEconomics(
        billing_kind="metered",
        metered_input_usd_per_million_tokens=10.0,
        metered_output_usd_per_million_tokens=20.0,
        source_id="configured-exact-price",
        evidence_ttl_seconds=3_600,
        provenance="user-config",
        confidence=1.0,
        observed_at=observed_at,
    )
    values: dict[str, object] = {
        "key": key,
        "resolver_name": "configured-provider",
        "state": "verified",
        "reasons": (),
        "economics": economics,
        "reasoning_support": ReasoningSupport(
            efforts=("none", "low", "medium"),
            provider_aliases=(),
            provenance="configured-provider",
            exact=True,
        ),
        "verification_source": "configured-provider",
        "verified_at": observed_at,
        "verification_expires_at": (
            mutable_clock.now() + timedelta(hours=1)
        ).isoformat().replace("+00:00", "Z"),
        "provenance": ("configured-provider",),
        "observed_at": observed_at,
        "capabilities": {},
    }
    values.update(changes)
    return ExecutableRuntime(**values)


class _FakeLlm:
    def __init__(
        self,
        *,
        store: RoutingStore,
        mutable_clock,
        behavior: PluginLlmStructuredResult | BaseException,
    ) -> None:
        self.store = store
        self.mutable_clock = mutable_clock
        self.behavior = behavior
        self.calls: list[dict[str, Any]] = []

    def complete_structured(self, **kwargs: Any) -> PluginLlmStructuredResult:
        budget = self.store.daily_budget(
            "routing_overhead",
            self.mutable_clock.today(),
        )
        assert budget.reserved_usd > 0.0, "reservation must precede provider dispatch"
        self.calls.append(kwargs)
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        return self.behavior


@pytest.fixture
def store(isolated_home: Path) -> RoutingStore:
    result = RoutingStore.open()
    try:
        yield result
    finally:
        result.close()


def _classifier(
    *,
    store: RoutingStore,
    mutable_clock,
    llm: _FakeLlm,
    settings: ClassifierSettings | None = None,
    policy: PolicyEnvelope | None = None,
    runtime: ExecutableRuntime | None = None,
    vocabulary: RoutingVocabulary | None = None,
) -> StructuredTaskClassifier:
    return StructuredTaskClassifier(
        settings=settings or _settings(),
        policy=policy or _policy(),
        vocabulary=vocabulary or _vocabulary(),
        runtime=runtime or _runtime(mutable_clock),
        store=store,
        llm=llm,
        now=mutable_clock.now,
    )


def _payload_text(call: dict[str, Any]) -> str:
    inputs = call["input"]
    exposed = [
        item.text
        for item in inputs
        if isinstance(item, PluginLlmTextInput)
    ]
    return "\n".join((call["instructions"], *exposed, json.dumps(call["json_schema"])))


def test_classifier_reserves_before_dispatch_sends_closed_stable_payload_and_reconciles(
    store: RoutingStore,
    mutable_clock,
) -> None:
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())
    classifier = _classifier(store=store, mutable_clock=mutable_clock, llm=fake)

    assert "candidates" not in inspect.signature(classifier.classify).parameters
    assert "profiles" not in inspect.signature(classifier.classify).parameters
    assert all(
        parameter.kind is not inspect.Parameter.VAR_KEYWORD
        for parameter in inspect.signature(classifier.classify).parameters.values()
    )
    outcome = classifier.classify("fix this bug completely", facts=_facts())

    assert isinstance(outcome.assessment, TaskAssessment)
    assert outcome.safe_default_reason is None
    assert outcome.clarification_requested is False
    assert outcome.classifier_runtime_id == _runtime(mutable_clock).key.stable_id()
    assert outcome.classifier_input_tokens == 100
    assert outcome.classifier_output_tokens == 20
    assert outcome.classifier_cost_usd == pytest.approx(0.0014)

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["provider"] == "classifier-provider"
    assert call["model"] == "classifier-model"
    assert call["temperature"] == 0
    assert call["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert call["max_tokens"] == 128
    assert call["timeout"] == 3.0
    assert call["purpose"] == "auto_routing_task_classification"
    assert call["schema_name"] == "task_assessment"
    assert call["json_schema"]["additionalProperties"] is False
    assert set(call["json_schema"]["required"]) == set(_assessment_document())
    assert call["json_schema"]["properties"]["required_capabilities"]["items"][
        "enum"
    ] == sorted(_vocabulary().capabilities)
    assert call["json_schema"]["properties"]["required_modalities"]["items"][
        "enum"
    ] == sorted(_vocabulary().modalities)
    assert call["json_schema"]["properties"]["required_capabilities"][
        "uniqueItems"
    ] is True
    assert call["json_schema"]["properties"]["required_modalities"][
        "uniqueItems"
    ] is True
    assert any(
        isinstance(item, PluginLlmTextInput)
        and item.text == "fix this bug completely"
        for item in call["input"]
    )

    payload = _payload_text(call).casefold()
    for forbidden in (
        "candidate",
        "profile",
        "ranking",
        "gpt-5",
        "claude",
        "openrouter",
        "classifier-provider",
        "classifier-model",
    ):
        assert forbidden not in payload

    budget = store.daily_budget("routing_overhead", mutable_clock.today())
    assert budget.reserved_usd == 0.0
    assert budget.spent_usd == pytest.approx(0.0014)


@pytest.mark.parametrize(
    ("error", "reason", "expected_cost"),
    [
        (PluginLlmTrustError("RAW_SECRET trust"), "classifier_trust", 0.0),
        (TimeoutError("RAW_SECRET timeout"), "classifier_timeout", 0.08448),
        (RuntimeError("RAW_SECRET provider"), "classifier_failed", 0.08448),
        (ValueError("RAW_SECRET arbitrary"), "classifier_failed", 0.08448),
    ],
)
def test_classifier_exception_phase_controls_conservative_reconciliation(
    store: RoutingStore,
    mutable_clock,
    caplog: pytest.LogCaptureFixture,
    error: BaseException,
    reason: str,
    expected_cost: float,
) -> None:
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=error)
    classifier = _classifier(store=store, mutable_clock=mutable_clock, llm=fake)

    outcome = classifier.classify("RAW_TASK_DO_NOT_PERSIST", facts=_facts())

    assert outcome.assessment is None
    assert outcome.safe_default_reason == reason
    assert outcome.clarification_requested is False
    assert "RAW_SECRET" not in repr(outcome)
    assert "RAW_SECRET" not in caplog.text
    budget = store.daily_budget("routing_overhead", mutable_clock.today())
    assert budget.reserved_usd == 0.0
    assert budget.spent_usd == pytest.approx(expected_cost)


def test_malformed_output_with_valid_usage_reconciles_actual_cost(
    store: RoutingStore,
    mutable_clock,
) -> None:
    fake = _FakeLlm(
        store=store,
        mutable_clock=mutable_clock,
        behavior=_result(parsed={"complexity": 0.5}, usage=PluginLlmUsage(
            input_tokens=80,
            output_tokens=10,
            total_tokens=90,
        )),
    )
    classifier = _classifier(store=store, mutable_clock=mutable_clock, llm=fake)

    outcome = classifier.classify("task", facts=_facts())

    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_malformed"
    assert store.daily_budget(
        "routing_overhead", mutable_clock.today()
    ).spent_usd == pytest.approx(0.001)


def test_real_plugin_llm_schema_validation_error_is_typed_malformed_and_full_cost(
    store: RoutingStore,
    mutable_clock,
    isolated_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "RAW_REAL_FACADE_RESPONSE_7d2f"
    caplog.set_level(logging.DEBUG)
    invalid = _assessment_document(complexity=sentinel)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(invalid)))],
        usage=SimpleNamespace(
            prompt_tokens=80,
            completion_tokens=10,
            total_tokens=90,
        ),
        model="classifier-model",
    )
    calls: list[dict[str, object]] = []

    def sync_caller(**kwargs: object) -> tuple[str, str, object]:
        assert store.daily_budget(
            "routing_overhead", mutable_clock.today()
        ).reserved_usd > 0.0
        calls.append(kwargs)
        return "classifier-provider", "classifier-model", response

    llm = make_plugin_llm_for_test(
        plugin_id="auto-routing",
        policy=_TrustPolicy(
            plugin_id="auto-routing",
            allow_provider_override=True,
            allowed_providers=frozenset({"classifier-provider"}),
            allow_model_override=True,
            allowed_models=frozenset({"classifier-model"}),
        ),
        sync_caller=sync_caller,
    )
    classifier = StructuredTaskClassifier(
        settings=_settings(),
        policy=_policy(),
        vocabulary=_vocabulary(),
        runtime=_runtime(mutable_clock),
        store=store,
        llm=llm,
        now=mutable_clock.now,
    )

    outcome = classifier.classify("task", facts=_facts())

    assert len(calls) == 1
    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_malformed"
    assert outcome.classifier_cost_usd == pytest.approx(0.08448)
    assert store.daily_budget(
        "routing_overhead", mutable_clock.today()
    ).spent_usd == pytest.approx(0.08448)
    assert sentinel not in repr(outcome)
    assert sentinel not in caplog.text
    encoded = sentinel.encode()
    database = isolated_home / "auto-routing" / "state.db"
    for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if path.exists():
            assert encoded not in path.read_bytes()


@pytest.mark.parametrize(
    "document",
    [
        {**_assessment_document(), "unknown": "field"},
        _assessment_document(complexity=True),
        _assessment_document(required_capabilities=["undeclared"]),
        _assessment_document(required_capabilities=["tools", "tools"]),
        _assessment_document(domains=["x" * 65]),
        _assessment_document(quality_sensitivity=float("nan")),
    ],
)
def test_provider_claimed_schema_output_is_revalidated_strictly(
    store: RoutingStore,
    mutable_clock,
    document: dict[str, object],
) -> None:
    fake = _FakeLlm(
        store=store,
        mutable_clock=mutable_clock,
        behavior=_result(parsed=document),
    )
    classifier = _classifier(store=store, mutable_clock=mutable_clock, llm=fake)

    outcome = classifier.classify("task", facts=_facts())

    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_malformed"


@pytest.mark.parametrize(
    "task",
    [
        "x" * 10_000,
        [{"type": "image", "url": "https://example.invalid/private.png"}],
        [
            {
                "type": "image",
                "url": "https://example.invalid/forged-size.png",
                "size_bytes": 1,
            }
        ],
        [{"type": "image", "data": b"x" * 1_025}],
        [
            {"type": "image", "data": b"a"},
            {"type": "image", "data": b"b"},
            {"type": "image", "data": b"c"},
        ],
        [{"type": "audio", "data": b"must-not-be-dropped"}],
        [{"type": "document", "data": b"must-not-be-dropped"}],
    ],
)
def test_unbounded_oversized_or_unrepresentable_input_never_dispatches_partial_task(
    store: RoutingStore,
    mutable_clock,
    task: object,
) -> None:
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())
    classifier = _classifier(
        store=store,
        mutable_clock=mutable_clock,
        llm=fake,
        settings=_settings(maximum_input_tokens=1_024),
    )

    outcome = classifier.classify(task, facts=_facts())

    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_oversized"
    assert fake.calls == []
    assert store.daily_budget(
        "routing_overhead", mutable_clock.today()
    ).committed_usd == 0.0


def test_complete_transmitted_payload_not_task_text_alone_must_fit_input_bound(
    store: RoutingStore,
    mutable_clock,
) -> None:
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())
    settings = _settings(maximum_input_tokens=1_024)
    classifier = _classifier(
        store=store,
        mutable_clock=mutable_clock,
        llm=fake,
        settings=settings,
    )
    task_text = "x" * 500
    image = b"i" * 300

    outcome = classifier.classify(
        [
            {"type": "text", "text": task_text},
            {"type": "image", "data": image, "mime_type": "image/png"},
        ],
        facts=_facts(required_modalities=("image", "text")),
    )

    assert len(task_text.encode()) < settings.maximum_input_tokens
    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_oversized"
    assert fake.calls == []
    assert store.daily_budget(
        "routing_overhead", mutable_clock.today()
    ).committed_usd == 0.0


def test_remote_image_forged_size_claim_cannot_bypass_input_bounds(
    store: RoutingStore,
    mutable_clock,
) -> None:
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())
    classifier = _classifier(store=store, mutable_clock=mutable_clock, llm=fake)

    outcome = classifier.classify(
        [
            {
                "type": "image",
                "url": "https://example.invalid/unbounded-private.png",
                "size_bytes": 1,
            }
        ],
        facts=_facts(required_modalities=("image",)),
    )

    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_oversized"
    assert fake.calls == []
    assert store.daily_budget(
        "routing_overhead", mutable_clock.today()
    ).committed_usd == 0.0


def test_complete_known_size_image_is_forwarded_without_truncation(
    store: RoutingStore,
    mutable_clock,
) -> None:
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result(
        parsed=_assessment_document(required_modalities=["image", "text"])
    ))
    classifier = _classifier(
        store=store,
        mutable_clock=mutable_clock,
        llm=fake,
        settings=_settings(maximum_input_tokens=4_096),
    )
    image = b"complete-image-bytes"

    outcome = classifier.classify(
        [
            {"type": "text", "text": "inspect every pixel"},
            {"type": "image", "data": image, "mime_type": "image/png"},
        ],
        facts=_facts(required_modalities=("image", "text")),
    )

    assert outcome.assessment is not None
    assert any(
        isinstance(item, PluginLlmImageInput) and item.data == image
        for item in fake.calls[0]["input"]
    )


def test_custom_authority_vocabulary_is_bound_into_exact_dispatched_schema(
    store: RoutingStore,
    mutable_clock,
) -> None:
    vocabulary = RoutingVocabulary(
        capabilities=("z_custom", "a_custom"),
        modalities=("text", "x_custom"),
    )
    fake = _FakeLlm(
        store=store,
        mutable_clock=mutable_clock,
        behavior=_result(
            parsed=_assessment_document(
                required_capabilities=["a_custom"],
                required_modalities=["x_custom"],
            )
        ),
    )
    classifier = _classifier(
        store=store,
        mutable_clock=mutable_clock,
        llm=fake,
        vocabulary=vocabulary,
    )

    outcome = classifier.classify("task", facts=_facts())

    assert outcome.assessment is not None
    schema = fake.calls[0]["json_schema"]
    assert schema["properties"]["required_capabilities"]["items"]["enum"] == [
        "a_custom",
        "z_custom",
    ]
    assert schema["properties"]["required_modalities"]["items"]["enum"] == [
        "text",
        "x_custom",
    ]


@pytest.mark.parametrize("fault", ["naive", "raises"])
def test_invalid_injected_clock_returns_typed_failure_without_side_effects(
    store: RoutingStore,
    mutable_clock,
    caplog: pytest.LogCaptureFixture,
    fault: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())

    def invalid_now() -> datetime:
        if fault == "raises":
            raise RuntimeError("RAW_CLOCK_SECRET")
        return datetime(2026, 1, 1)

    classifier = StructuredTaskClassifier(
        settings=_settings(),
        policy=_policy(),
        vocabulary=_vocabulary(),
        runtime=_runtime(mutable_clock),
        store=store,
        llm=fake,
        now=invalid_now,
    )

    outcome = classifier.classify("task", facts=_facts())

    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_failed"
    assert fake.calls == []
    assert store.daily_budget(
        "routing_overhead", mutable_clock.today()
    ).committed_usd == 0.0
    assert "RAW_CLOCK_SECRET" not in repr(outcome)
    assert "RAW_CLOCK_SECRET" not in caplog.text


@pytest.mark.parametrize("fault", ["missing_price", "stale_price"])
def test_unknown_or_stale_exact_economics_fails_before_reservation(
    store: RoutingStore,
    mutable_clock,
    fault: str,
) -> None:
    runtime = _runtime(mutable_clock)
    if fault == "missing_price":
        economics = runtime.economics.model_copy(
            update={"metered_output_usd_per_million_tokens": None}
        )
    else:
        economics = runtime.economics.model_copy(
            update={"observed_at": "2025-12-01T00:00:00Z"}
        )
    runtime = replace(runtime, economics=economics)
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())
    classifier = _classifier(
        store=store,
        mutable_clock=mutable_clock,
        llm=fake,
        runtime=runtime,
    )

    outcome = classifier.classify("task", facts=_facts())

    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_unknown_price"
    assert fake.calls == []
    assert store.daily_budget(
        "routing_overhead", mutable_clock.today()
    ).committed_usd == 0.0


@pytest.mark.parametrize("billing_kind", ["subscription", "local"])
def test_non_metered_exact_economics_are_reserved_and_reconciled(
    store: RoutingStore,
    mutable_clock,
    billing_kind: str,
) -> None:
    runtime = _runtime(mutable_clock)
    common = {
        "source_id": "configured-exact-price",
        "evidence_ttl_seconds": 3_600,
        "provenance": "user-config",
        "confidence": 1.0,
        "observed_at": runtime.economics.observed_at,
    }
    if billing_kind == "subscription":
        economics = AccessEconomics(
            billing_kind="subscription",
            effective_marginal_cost_usd_per_task=0.02,
            effective_amortized_cost_usd_per_task=0.03,
            subscription_plan="codex",
            subscription_quota_limit=100.0,
            subscription_quota_remaining=50.0,
            subscription_quota_unit="tasks",
            subscription_state="active",
            **common,
        )
        expected = 0.03
    else:
        economics = AccessEconomics(
            billing_kind="local",
            local_energy_cost_usd_per_task=0.004,
            local_compute_cost_usd_per_task=0.006,
            **common,
        )
        expected = 0.01
    runtime = replace(runtime, economics=economics)
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())
    classifier = _classifier(
        store=store,
        mutable_clock=mutable_clock,
        llm=fake,
        runtime=runtime,
    )

    outcome = classifier.classify("task", facts=_facts())

    assert outcome.assessment is not None
    assert outcome.classifier_cost_usd == pytest.approx(expected)
    budget = store.daily_budget("routing_overhead", mutable_clock.today())
    assert budget.reserved_usd == 0.0
    assert budget.spent_usd == pytest.approx(expected)


@pytest.mark.parametrize("fault", ["unknown_cost", "inactive", "exhausted"])
def test_unknown_or_inactive_subscription_economics_never_dispatches(
    store: RoutingStore,
    mutable_clock,
    fault: str,
) -> None:
    runtime = _runtime(mutable_clock)
    values: dict[str, object] = {
        "billing_kind": "subscription",
        "effective_marginal_cost_usd_per_task": 0.02,
        "effective_amortized_cost_usd_per_task": 0.03,
        "subscription_plan": "codex",
        "subscription_quota_limit": 100.0,
        "subscription_quota_remaining": 50.0,
        "subscription_quota_unit": "tasks",
        "subscription_state": "active",
        "source_id": "configured-exact-price",
        "evidence_ttl_seconds": 3_600,
        "provenance": "user-config",
        "confidence": 1.0,
        "observed_at": runtime.economics.observed_at,
    }
    if fault == "unknown_cost":
        values["effective_marginal_cost_usd_per_task"] = None
    elif fault == "inactive":
        values["subscription_state"] = "inactive"
    else:
        values["subscription_quota_remaining"] = 0.0
    economics = AccessEconomics.model_validate(values)
    runtime = replace(runtime, economics=economics)
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())
    classifier = _classifier(
        store=store,
        mutable_clock=mutable_clock,
        llm=fake,
        runtime=runtime,
    )

    outcome = classifier.classify("task", facts=_facts())

    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_unknown_price"
    assert fake.calls == []
    assert store.daily_budget(
        "routing_overhead", mutable_clock.today()
    ).committed_usd == 0.0


def test_daily_budget_denial_never_calls_classifier(
    store: RoutingStore,
    mutable_clock,
) -> None:
    store.reserve_budget(
        "routing_overhead",
        worst_case_usd=1.0,
        daily_limit_usd=1.0,
        now=mutable_clock.now(),
    )
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())
    classifier = _classifier(store=store, mutable_clock=mutable_clock, llm=fake)

    outcome = classifier.classify("task", facts=_facts())

    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_budget"
    assert fake.calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        "provider_mismatch",
        "model_mismatch",
        "configured_unverified",
        "expired_verification",
        "moa",
        "unsupported_reasoning",
        "inexact_reasoning",
    ],
)
def test_classifier_uses_only_one_current_verified_exact_runtime(
    store: RoutingStore,
    mutable_clock,
    mutation: str,
) -> None:
    settings = _settings()
    runtime = _runtime(mutable_clock)
    if mutation == "provider_mismatch":
        settings = _settings(provider="other-provider")
    elif mutation == "model_mismatch":
        settings = _settings(model="other-model")
    elif mutation == "configured_unverified":
        runtime = replace(
            runtime,
            state="configured_unverified",
            reasons=("not-verified",),
            verification_source=None,
            verified_at=None,
        )
    elif mutation == "expired_verification":
        runtime = replace(
            runtime,
            verification_expires_at="2025-12-31T23:59:59Z",
        )
    elif mutation == "moa":
        runtime = replace(
            runtime,
            key=runtime.key.model_copy(update={"provider": "moa"}),
        )
        settings = _settings(provider="moa")
    elif mutation == "unsupported_reasoning":
        runtime = replace(
            runtime,
            reasoning_support=ReasoningSupport(
                efforts=("medium",),
                provider_aliases=(),
                provenance="configured-provider",
                exact=True,
            ),
        )
    else:
        runtime = replace(
            runtime,
            reasoning_support=ReasoningSupport(
                efforts=("low",),
                provider_aliases=(),
                provenance="heuristic",
                exact=False,
            ),
        )
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())
    classifier = _classifier(
        store=store,
        mutable_clock=mutable_clock,
        llm=fake,
        settings=settings,
        runtime=runtime,
    )

    outcome = classifier.classify("task", facts=_facts())

    assert outcome.assessment is None
    assert outcome.safe_default_reason in {"classifier_trust", "classifier_failed"}
    assert fake.calls == []
    assert store.daily_budget(
        "routing_overhead", mutable_clock.today()
    ).committed_usd == 0.0


@pytest.mark.parametrize(
    "usage",
    [
        PluginLlmUsage(),
        PluginLlmUsage(input_tokens=True, output_tokens=1, total_tokens=2),
        PluginLlmUsage(input_tokens=-1, output_tokens=1, total_tokens=0),
        PluginLlmUsage(input_tokens=100, output_tokens=20, total_tokens=120, cost_usd=math.nan),
    ],
)
def test_missing_or_unparseable_post_dispatch_usage_charges_full_reservation(
    store: RoutingStore,
    mutable_clock,
    usage: PluginLlmUsage,
) -> None:
    fake = _FakeLlm(
        store=store,
        mutable_clock=mutable_clock,
        behavior=_result(usage=usage),
    )
    classifier = _classifier(store=store, mutable_clock=mutable_clock, llm=fake)

    outcome = classifier.classify("task", facts=_facts())

    assert outcome.assessment is not None
    assert outcome.classifier_input_tokens == 0
    assert outcome.classifier_output_tokens == 0
    assert outcome.classifier_cost_usd == pytest.approx(0.08448)
    assert store.daily_budget(
        "routing_overhead", mutable_clock.today()
    ).spent_usd == pytest.approx(0.08448)


def test_result_attribution_mismatch_is_typed_trust_failure_after_dispatch(
    store: RoutingStore,
    mutable_clock,
) -> None:
    fake = _FakeLlm(
        store=store,
        mutable_clock=mutable_clock,
        behavior=_result(provider="different-provider"),
    )
    classifier = _classifier(store=store, mutable_clock=mutable_clock, llm=fake)

    outcome = classifier.classify("task", facts=_facts())

    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_trust"
    assert store.daily_budget(
        "routing_overhead", mutable_clock.today()
    ).spent_usd == pytest.approx(0.0014)


def test_reconciliation_failure_returns_redacted_typed_failure(
    store: RoutingStore,
    mutable_clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())
    classifier = _classifier(store=store, mutable_clock=mutable_clock, llm=fake)

    def fail_reconciliation(*args: object, **kwargs: object) -> object:
        raise RuntimeError("RAW_RECONCILIATION_SECRET")

    monkeypatch.setattr(store, "reconcile_budget", fail_reconciliation)
    outcome = classifier.classify("RAW_TASK_SECRET", facts=_facts())

    assert outcome.assessment is None
    assert outcome.safe_default_reason == "classifier_failed"
    assert "RAW_RECONCILIATION_SECRET" not in repr(outcome)
    assert "RAW_RECONCILIATION_SECRET" not in caplog.text


def test_task_text_never_enters_routing_store_or_control_plane_artifacts(
    store: RoutingStore,
    mutable_clock,
    isolated_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "RAW_TASK_SENTINEL_8bd9c7c24c0f"
    response_sentinel = "RAW_PROVIDER_RESPONSE_SENTINEL_5e7c"
    caplog.set_level(logging.DEBUG)
    fake = _FakeLlm(store=store, mutable_clock=mutable_clock, behavior=_result())
    classifier = _classifier(store=store, mutable_clock=mutable_clock, llm=fake)

    outcome = classifier.classify(sentinel, facts=_facts())

    assert sentinel not in repr(outcome)
    assert sentinel not in caplog.text
    encoded = sentinel.encode()
    database = isolated_home / "auto-routing" / "state.db"
    for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if path.exists():
            assert encoded not in path.read_bytes()

    malformed = _FakeLlm(
        store=store,
        mutable_clock=mutable_clock,
        behavior=_result(
            parsed={"malformed": True},
            text=response_sentinel,
        ),
    )
    classifier = _classifier(store=store, mutable_clock=mutable_clock, llm=malformed)
    failed = classifier.classify("second task", facts=_facts())

    assert failed.safe_default_reason == "classifier_malformed"
    assert response_sentinel not in repr(failed)
    assert response_sentinel not in caplog.text
    response_encoded = response_sentinel.encode()
    for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if path.exists():
            contents = path.read_bytes()
            assert encoded not in contents
            assert response_encoded not in contents
