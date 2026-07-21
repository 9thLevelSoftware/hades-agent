"""Catalog provenance and advisor ranking behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent.models_dev import ModelInfo
from agent.reasoning_support import ReasoningSupport
from plugins.auto_routing.auto_routing.advisor import (
    Advisor,
    ProposalRequest,
    runtime_policy_rejection_reasons,
)
from plugins.auto_routing.auto_routing.catalog import (
    CatalogRefreshError,
    CatalogRecord,
    CatalogService,
    CatalogValidationError,
    HermesCatalogSource,
    JsonCatalogSource,
    MAX_JSON_BYTES,
    MAX_JSON_RECORDS,
    ModelsDevCatalogSource,
)
from plugins.auto_routing.auto_routing.inventory import (
    ExecutableRuntime,
    InventorySnapshot,
    ReasonCodes,
)
from plugins.auto_routing.auto_routing.models import (
    AccessEconomics,
    CatalogEvidence,
    ObjectiveWeights,
    RuntimeKey,
)
from plugins.auto_routing.auto_routing.scoring import (
    conservative_metric,
    normalize_catalog_metric,
)
from plugins.auto_routing.auto_routing.storage import RoutingStore


OBSERVED_AT = "2026-01-01T00:00:00Z"


def _runtime(
    provider: str,
    model: str,
    *,
    auth_identity: str,
    endpoint_identity: str,
    state: str,
    economics: AccessEconomics,
) -> ExecutableRuntime:
    verified = state == "verified"
    return ExecutableRuntime(
        key=RuntimeKey(
            provider=provider,
            model=model,
            auth_identity=auth_identity,
            endpoint_identity=endpoint_identity,
            api_mode="chat_completions",
            inventory_revision="inventory-1",
        ),
        resolver_name=f"{provider}:{auth_identity}",
        state=state,
        reasons=ReasonCodes(()) if verified else ReasonCodes((state,)),
        economics=economics,
        reasoning_support=ReasoningSupport(
            efforts=("low", "medium", "high"),
            provider_aliases=(),
            provenance="test-catalog",
            exact=True,
        ),
        verification_source="authenticated_live" if verified else None,
        verified_at=OBSERVED_AT if verified else None,
        verification_expires_at="2026-01-02T00:00:00Z" if verified else None,
        provenance=("test-inventory",),
        observed_at=OBSERVED_AT,
    )


@dataclass(frozen=True)
class InventoryFixture:
    snapshot: InventorySnapshot

    def _find(self, provider: str, model: str, state: str) -> ExecutableRuntime:
        return next(
            runtime
            for runtime in self.snapshot.runtimes
            if runtime.key.provider == provider
            and runtime.key.model == model
            and runtime.state == state
        )

    def verified(self, provider: str, model: str) -> ExecutableRuntime:
        return self._find(provider, model, "verified")

    def configured_unverified(
        self,
        provider: str,
        model: str,
    ) -> ExecutableRuntime:
        return self._find(provider, model, "configured_unverified")

    def eligible(self) -> list[ExecutableRuntime]:
        return self.snapshot.eligible()


@pytest.fixture
def same_model_access_paths() -> tuple[ExecutableRuntime, ExecutableRuntime]:
    subscription = _runtime(
        "openai-codex",
        "gpt-5.4",
        auth_identity="subscription:codex",
        endpoint_identity="endpoint:codex",
        state="verified",
        economics=AccessEconomics(
            billing_kind="subscription",
            effective_marginal_cost_usd_per_task=0.0,
            subscription_plan="codex",
            subscription_quota_remaining=100,
            subscription_quota_unit="requests",
            subscription_state="active",
            source_id="codex-account",
            provenance="authenticated-account",
            confidence=0.9,
            observed_at=OBSERVED_AT,
        ),
    )
    metered = _runtime(
        "openai",
        "gpt-5.4",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:openai",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=5.0,
            metered_output_usd_per_million_tokens=15.0,
            source_id="openai-pricing",
            provenance="authenticated-picker-pricing",
            confidence=0.95,
            observed_at=OBSERVED_AT,
        ),
    )
    return subscription, metered


@pytest.fixture
def inventory(
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> InventoryFixture:
    subscription, metered = same_model_access_paths
    unverified = _runtime(
        "anthropic",
        "claude-sonnet-4-6",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:anthropic",
        state="configured_unverified",
        economics=AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=3.0,
            metered_output_usd_per_million_tokens=15.0,
            source_id="anthropic-pricing",
            provenance="configured-not-authenticated",
            confidence=0.8,
            observed_at=OBSERVED_AT,
        ),
    )
    return InventoryFixture(
        InventorySnapshot(
            revision="inventory-1",
            runtimes=[subscription, metered, unverified],
            observed_at=OBSERVED_AT,
        )
    )


def evidence(
    source_id: str,
    task_definition: str,
    value: float,
    *,
    source_url: str,
    confidence: float,
    model: str = "gpt-5.4",
    model_version: str | None = None,
    retrieved_at: str = OBSERVED_AT,
    published_at: str = "2025-12-15T00:00:00Z",
    domain: str = "coding",
    metric_name: str = "quality",
    metric_direction: str = "higher_is_better",
    metric_scale: str = "unit_interval",
    sample_size: int = 100,
    normalization_method: str = "identity",
) -> CatalogEvidence:
    return CatalogEvidence(
        source_id=source_id,
        source_url=source_url,
        retrieved_at=retrieved_at,
        published_at=published_at,
        model=model,
        model_version=model_version or model,
        domain=domain,
        task_definition=task_definition,
        metric_name=metric_name,
        metric_direction=metric_direction,
        metric_scale=metric_scale,
        value=value,
        sample_size=sample_size,
        confidence=confidence,
        normalization_method=normalization_method,
    )


@pytest.fixture
def catalog(mutable_clock) -> CatalogService:
    return CatalogService(clock=mutable_clock)


@pytest.fixture
def proposal_request(inventory: InventoryFixture) -> ProposalRequest:
    return ProposalRequest(
        inventory=tuple(inventory.snapshot.runtimes),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=20_000,
        expected_output_tokens=2_000,
        objectives=ObjectiveWeights(
            quality=0.55,
            reliability=0.25,
            latency=0.10,
            cost=0.10,
        ),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=30.0,
    )


@pytest.fixture
def cost_heavy_request(proposal_request: ProposalRequest) -> ProposalRequest:
    return proposal_request.model_copy(
        update={
            "objectives": ObjectiveWeights(
                quality=0.10,
                reliability=0.05,
                latency=0.05,
                cost=0.80,
            )
        }
    )


def _with_exact_capabilities(
    runtime: ExecutableRuntime,
    **updates,
) -> ExecutableRuntime:
    capabilities = {
        "supports_tools": True,
        "supports_structured_output": True,
        "input_modalities": ("text", "image"),
        "output_modalities": ("text",),
        "context_window": 32_000,
        "max_output_tokens": 4_000,
        **updates,
    }
    return replace(runtime, capabilities=capabilities)


@pytest.mark.parametrize(
    ("request_updates", "capability_updates", "reason"),
    [
        (
            {"required_modalities": ("image",)},
            {"input_modalities": ("text",)},
            "required_modality_unsupported:image",
        ),
        (
            {"required_capabilities": ("tools",)},
            {"supports_tools": False},
            "required_capability_unsupported:tools",
        ),
        (
            {"minimum_context_tokens": 32_001},
            {"context_window": 32_000},
            "context_capacity_insufficient",
        ),
        (
            {"minimum_output_tokens": 4_001},
            {"max_output_tokens": 4_000},
            "output_capacity_insufficient",
        ),
        (
            {"required_capabilities": ("structured_output",)},
            {"supports_structured_output": False},
            "required_capability_unsupported:structured_output",
        ),
    ],
)
def test_advisor_hard_rejects_incompatible_capabilities_before_ranking(
    advisor: Advisor,
    proposal_request: ProposalRequest,
    inventory: InventoryFixture,
    request_updates: dict,
    capability_updates: dict,
    reason: str,
) -> None:
    runtime = _with_exact_capabilities(
        inventory.verified("openai-codex", "gpt-5.4"),
        **capability_updates,
    )
    request = proposal_request.model_copy(
        update={"inventory": (runtime,), **request_updates},
    )

    proposal = advisor.propose(request)

    assert proposal.primary is None
    assert proposal.explanation.accepted_runtime_ids == ()
    assert proposal.explanation.rejected_candidates[runtime.key.stable_id()][
        "reasons"
    ] == (reason,)


def test_advisor_accepts_exact_capabilities_at_declared_boundaries(
    advisor: Advisor,
    proposal_request: ProposalRequest,
    inventory: InventoryFixture,
) -> None:
    runtime = _with_exact_capabilities(
        inventory.verified("openai-codex", "gpt-5.4")
    )
    request = proposal_request.model_copy(
        update={
            "inventory": (runtime,),
            "required_capabilities": ("tools", "structured_output"),
            "required_modalities": ("text", "image"),
            "minimum_context_tokens": 32_000,
            "minimum_output_tokens": 4_000,
        }
    )

    proposal = advisor.propose(request)

    assert proposal.primary is not None
    assert proposal.primary.runtime_id == runtime.key.stable_id()
    assert proposal.explanation.rejected_candidates == {}


def test_shared_runtime_policy_rejects_subscription_when_not_allowed(
    catalog: CatalogService,
    inventory: InventoryFixture,
) -> None:
    runtime = replace(
        _with_exact_capabilities(
            inventory.verified("openai-codex", "gpt-5.4")
        ),
        economics=AccessEconomics(
            billing_kind="subscription",
            effective_marginal_cost_usd_per_task=0,
            subscription_plan="test-plan",
            subscription_quota_remaining=10,
            subscription_quota_unit="request",
            subscription_state="active",
            source_id="test-subscription",
            provenance="test",
            observed_at=OBSERVED_AT,
        ),
    )
    policy = SimpleNamespace(
        denied_providers=(),
        denied_models=(),
        eligible_sources=("configured_providers",),
        allow_subscription=False,
        max_estimated_task_cost_usd=1,
        max_estimated_latency_seconds=30,
        minimum_context_tokens=0,
    )

    reasons = runtime_policy_rejection_reasons(
        runtime,
        policy=policy,
        catalog=catalog,
    )

    assert "subscription_access_disallowed" in reasons


@pytest.fixture
def advisor(
    catalog: CatalogService,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> Advisor:
    catalog.import_records(
        [
            evidence(
                "quality-lab",
                "coding",
                0.7,
                source_url="https://example.com/quality/gpt-5.4",
                confidence=0.8,
            ),
            evidence(
                "review-lab",
                "coding",
                0.99,
                model="claude-sonnet-4-6",
                source_url="https://example.com/quality/claude-sonnet-4-6",
                confidence=0.99,
            ),
        ]
    )
    catalog.import_records(
        [
            CatalogRecord(
                evidence=evidence(
                    "latency-lab",
                    "coding",
                    float(index + 1),
                    source_url="https://example.com/latency",
                    confidence=0.9,
                    metric_name="latency",
                    metric_direction="lower_is_better",
                    metric_scale="seconds",
                    normalization_method="divide_by_limit",
                ),
                canonical_provider="openai",
                canonical_model=runtime.key.model,
                canonical_version=runtime.key.model,
                runtime_id=runtime.key.stable_id(),
            )
            for index, runtime in enumerate(same_model_access_paths)
        ]
    )
    return Advisor(catalog)


def test_conflicting_evidence_remains_separate_and_cannot_grant_access(
    catalog: CatalogService,
    inventory: InventoryFixture,
) -> None:
    catalog.import_records(
        [
            evidence(
                "swe-bench",
                "coding",
                0.62,
                source_url="https://www.swebench.com/",
                confidence=0.8,
            ),
            evidence(
                "review-lab",
                "coding",
                0.48,
                source_url="https://example.com/review",
                confidence=0.4,
            ),
        ]
    )

    rows = catalog.evidence_for(inventory.verified("openai-codex", "gpt-5.4"))

    assert [row.source_id for row in rows] == ["swe-bench", "review-lab"]
    assert all(row.source_url and row.retrieved_at for row in rows)


def test_catalog_evidence_cannot_upgrade_an_unverified_runtime(
    catalog: CatalogService,
    inventory: InventoryFixture,
) -> None:
    candidate = inventory.configured_unverified(
        "anthropic",
        "claude-sonnet-4-6",
    )
    catalog.import_records(
        [
            evidence(
                "review-lab",
                "coding",
                0.99,
                model=candidate.key.model,
                source_url="https://example.com/review",
                confidence=0.99,
            )
        ]
    )

    assert catalog.evidence_for(candidate)
    assert candidate.state == "configured_unverified"
    assert candidate not in inventory.eligible()


def test_advisor_never_recommends_unverified_runtime(
    advisor: Advisor,
    proposal_request: ProposalRequest,
) -> None:
    proposal = advisor.propose(proposal_request)

    targets = [proposal.primary, *proposal.fallbacks]
    assert all(target.inventory_state == "verified" for target in targets)
    assert (
        proposal.explanation.rejected["anthropic/claude-sonnet-4-6"]
        == "configured_unverified"
    )


def test_subscription_and_metered_paths_use_separate_economics(
    advisor: Advisor,
    cost_heavy_request: ProposalRequest,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> None:
    subscription, metered = same_model_access_paths
    proposal = advisor.propose(
        cost_heavy_request.model_copy(update={"inventory": same_model_access_paths})
    )

    assert subscription.key.model == metered.key.model
    assert subscription.key.stable_id() != metered.key.stable_id()
    assert proposal.primary.runtime_id == subscription.key.stable_id()
    assert (
        proposal.explanation.candidates[metered.key.stable_id()][
            "estimated_cost_usd"
        ]
        > 0
    )
    assert (
        proposal.explanation.candidates[subscription.key.stable_id()][
            "billing_kind"
        ]
        == "subscription"
    )


def test_advisor_rejects_unbounded_token_estimates(
    advisor: Advisor,
    proposal_request: ProposalRequest,
) -> None:
    request = proposal_request.model_copy(
        update={"expected_input_tokens": 10**1_000}
    )

    with pytest.raises(ValueError, match="bounded"):
        advisor.propose(request)


def test_path_local_evidence_requires_and_honors_exact_runtime_binding(
    catalog: CatalogService,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> None:
    subscription, metered = same_model_access_paths
    generic_latency = evidence(
        "latency-lab",
        "coding",
        4.0,
        source_url="https://example.com/latency",
        confidence=0.9,
        metric_name="latency",
        metric_direction="lower_is_better",
        metric_scale="seconds",
        normalization_method="divide_by_limit",
    )
    with pytest.raises(CatalogValidationError, match="runtime binding"):
        catalog.import_records([generic_latency])

    catalog.import_records(
        [
            CatalogRecord(
                evidence=generic_latency,
                canonical_provider="openai",
                canonical_model="gpt-5.4",
                canonical_version="gpt-5.4",
                runtime_id=subscription.key.stable_id(),
            ),
            CatalogRecord(
                evidence=generic_latency.model_copy(update={"value": 1.0}),
                canonical_provider="openai",
                canonical_model="gpt-5.4",
                canonical_version="gpt-5.4",
                runtime_id=metered.key.stable_id(),
            ),
        ]
    )

    assert [
        row.value
        for row in catalog.evidence_for(subscription)
        if row.metric_name == "latency"
    ] == [4.0]
    assert [
        row.value
        for row in catalog.evidence_for(metered)
        if row.metric_name == "latency"
    ] == [1.0]


def test_json_source_loads_a_complete_provenance_record(mutable_clock) -> None:
    record = evidence(
        "swe-bench",
        "coding",
        0.62,
        source_url="https://www.swebench.com/",
        confidence=0.8,
    )
    source = JsonCatalogSource(
        json.dumps([record.model_dump(mode="json")]),
        clock=mutable_clock,
    )

    loaded = source.load()

    assert loaded == (
        CatalogRecord(
            evidence=record,
            canonical_provider="",
            canonical_model="gpt-5.4",
            canonical_version="gpt-5.4",
        ),
    )


@pytest.mark.parametrize(
    ("field", "coerced_value"),
    [
        ("value", "0.62"),
        ("value", True),
        ("sample_size", "100"),
        ("sample_size", True),
        ("sample_size", 10**1_000),
        ("confidence", "0.8"),
        ("confidence", False),
    ],
)
def test_json_source_rejects_coerced_numeric_evidence(
    mutable_clock,
    field: str,
    coerced_value: object,
) -> None:
    record = evidence(
        "swe-bench",
        "coding",
        0.62,
        source_url="https://www.swebench.com/",
        confidence=0.8,
    ).model_dump(mode="json")
    record[field] = coerced_value

    with pytest.raises(CatalogValidationError, match="schema"):
        JsonCatalogSource(json.dumps([record]), clock=mutable_clock).load()


def test_json_source_rejects_non_string_runtime_binding(mutable_clock) -> None:
    record = evidence(
        "runtime-lab",
        "coding",
        0.62,
        source_url="https://example.com/runtime",
        confidence=0.8,
    ).model_dump(mode="json")
    record["runtime_id"] = int("1" * 64)

    with pytest.raises(CatalogValidationError, match="schema"):
        JsonCatalogSource(json.dumps([record]), clock=mutable_clock).load()


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://127.0.0.1/private",
        "http://[::1]/private",
        "http://2130706433/private",
        "http://0177.0.0.1/private",
        "http://0x7f000001/private",
        "https://example.com/benchmark?access_token=not-a-real-token",
    ],
)
def test_json_source_rejects_private_or_credentialed_source_urls(
    mutable_clock,
    unsafe_url: str,
) -> None:
    record = evidence(
        "review-lab",
        "coding",
        0.6,
        source_url=unsafe_url,
        confidence=0.8,
    ).model_dump(mode="json")

    with pytest.raises(CatalogValidationError, match="source URL"):
        JsonCatalogSource(json.dumps([record]), clock=mutable_clock).load()


@pytest.mark.parametrize(
    "payload",
    [
        '[{"source_id":"first","source_id":"second"}]',
        '[{"value":NaN}]',
        '[{"value":Infinity}]',
    ],
)
def test_json_source_rejects_duplicate_fields_and_nonfinite_numbers(
    mutable_clock,
    payload: str,
) -> None:
    with pytest.raises(CatalogValidationError):
        JsonCatalogSource(payload, clock=mutable_clock).load()


def test_json_source_enforces_payload_and_record_limits(mutable_clock) -> None:
    oversized = b"[" + b" " * MAX_JSON_BYTES + b"]"
    with pytest.raises(CatalogValidationError, match="size limit"):
        JsonCatalogSource(oversized, clock=mutable_clock).load()

    too_many = json.dumps([{}] * (MAX_JSON_RECORDS + 1))
    with pytest.raises(CatalogValidationError, match="too many"):
        JsonCatalogSource(too_many, clock=mutable_clock).load()


def test_json_source_rejects_executable_fields_and_excessive_nesting(
    mutable_clock,
) -> None:
    record = evidence(
        "review-lab",
        "coding",
        0.6,
        source_url="https://example.com/benchmark",
        confidence=0.8,
    ).model_dump(mode="json")
    executable = {**record, "command": "run arbitrary code"}
    with pytest.raises(CatalogValidationError, match="schema"):
        JsonCatalogSource(json.dumps([executable]), clock=mutable_clock).load()

    nested: object = "leaf"
    for _index in range(40):
        nested = {"child": nested}
    deeply_nested = {**record, "metadata": nested}
    with pytest.raises(CatalogValidationError, match="nesting"):
        JsonCatalogSource(json.dumps([deeply_nested]), clock=mutable_clock).load()


@pytest.mark.parametrize(
    ("published_at", "retrieved_at"),
    [
        ("2026-01-01T00:00:01Z", "2026-01-01T00:00:00Z"),
        ("2025-12-15T00:00:00", "2026-01-01T00:00:00Z"),
        ("2025-12-15T00:00:00Z", "2026-01-01T01:00:00Z"),
    ],
)
def test_json_source_rejects_invalid_or_future_timestamp_evidence(
    mutable_clock,
    published_at: str,
    retrieved_at: str,
) -> None:
    record = evidence(
        "review-lab",
        "coding",
        0.6,
        source_url="https://example.com/benchmark",
        confidence=0.8,
        published_at=published_at,
        retrieved_at=retrieved_at,
    ).model_dump(mode="json")

    with pytest.raises(CatalogValidationError, match="timestamp"):
        JsonCatalogSource(json.dumps([record]), clock=mutable_clock).load()


@pytest.mark.parametrize(
    ("field", "timestamp"),
    [
        ("retrieved_at", "2026-01-01 00:00:00Z"),
        ("retrieved_at", "2026-01-01T00:00:00+00"),
        ("published_at", "2025-12-15T00:00:00+00:00"),
    ],
)
def test_json_source_rejects_noncanonical_rfc3339_timestamps(
    mutable_clock,
    field: str,
    timestamp: str,
) -> None:
    record = evidence(
        "review-lab",
        "coding",
        0.6,
        source_url="https://example.com/benchmark",
        confidence=0.8,
    ).model_dump(mode="json")
    record[field] = timestamp

    with pytest.raises(CatalogValidationError, match="timestamp"):
        JsonCatalogSource(json.dumps([record]), clock=mutable_clock).load()


@pytest.mark.parametrize(
    ("field", "unsafe_text"),
    [
        ("source_id", "   "),
        ("domain", "coding\u0000hidden"),
        ("task_definition", "coding\nexecute this"),
        ("metric_name", "quality\rhidden"),
    ],
)
def test_json_source_rejects_blank_or_control_bearing_text(
    mutable_clock,
    field: str,
    unsafe_text: str,
) -> None:
    record = evidence(
        "review-lab",
        "coding",
        0.6,
        source_url="https://example.com/benchmark",
        confidence=0.8,
    ).model_dump(mode="json")
    record[field] = unsafe_text

    with pytest.raises(CatalogValidationError, match="text field"):
        JsonCatalogSource(json.dumps([record]), clock=mutable_clock).load()


@pytest.mark.parametrize(
    ("field", "secret_assignment"),
    [
        ("source_id", "api_key=supersecretvalue"),
        ("domain", "access_token: supersecretvalue"),
        ("task_definition", "password=supersecretvalue"),
        ("metric_name", "client_secret: supersecretvalue"),
    ],
)
def test_json_source_rejects_secret_assignments_without_echo(
    mutable_clock,
    field: str,
    secret_assignment: str,
) -> None:
    record = evidence(
        "review-lab",
        "coding",
        0.6,
        source_url="https://example.com/benchmark",
        confidence=0.8,
    ).model_dump(mode="json")
    record[field] = secret_assignment

    with pytest.raises(CatalogValidationError) as caught:
        JsonCatalogSource(json.dumps([record]), clock=mutable_clock).load()

    assert secret_assignment not in str(caught.value)


def test_json_source_rejects_embedded_endpoint_content_without_echo(
    mutable_clock,
) -> None:
    endpoint = "https://private.invalid/v1"
    record = evidence(
        "review-lab",
        f"benchmark calling {endpoint}",
        0.6,
        source_url="https://example.com/benchmark",
        confidence=0.8,
    ).model_dump(mode="json")

    with pytest.raises(CatalogValidationError) as caught:
        JsonCatalogSource(json.dumps([record]), clock=mutable_clock).load()

    assert endpoint not in str(caught.value)


def test_catalog_binding_rejects_endpoint_or_credential_material(
    catalog: CatalogService,
) -> None:
    row = evidence(
        "quality-lab",
        "coding",
        0.7,
        source_url="https://example.com/quality",
        confidence=0.8,
    )
    unsafe = CatalogRecord(
        evidence=row,
        canonical_provider="https://private.invalid/v1",
        canonical_model=row.model,
        canonical_version=row.model_version,
        runtime_id="sk-not-a-real-secret",
    )

    with pytest.raises(CatalogValidationError) as caught:
        catalog.import_records([unsafe])

    assert "private.invalid" not in str(caught.value)
    assert "sk-not-a-real-secret" not in str(caught.value)


def test_catalog_rejects_evidence_rebound_to_another_model(
    catalog: CatalogService,
    mutable_clock,
) -> None:
    row = evidence(
        "quality-lab",
        "coding",
        0.7,
        source_url="https://example.com/quality",
        confidence=0.8,
    )
    rebound = CatalogRecord(
        evidence=row,
        canonical_provider="anthropic",
        canonical_model="claude-sonnet-4-6",
        canonical_version="claude-sonnet-4-6",
    )
    with pytest.raises(CatalogValidationError, match="canonical"):
        catalog.import_records([rebound])

    payload = {
        **row.model_dump(mode="json"),
        "canonical_provider": "anthropic",
        "canonical_model": "claude-sonnet-4-6",
        "canonical_version": "claude-sonnet-4-6",
    }
    with pytest.raises(CatalogValidationError, match="canonical"):
        JsonCatalogSource(json.dumps([payload]), clock=mutable_clock).load()


@pytest.mark.parametrize(
    ("metric_name", "scale", "normalization"),
    [
        ("latency", "seconds", "divide_by_limit"),
        ("metered_input_price", "usd_per_million_tokens", "path_local_only"),
    ],
)
def test_path_local_time_and_price_metrics_reject_negative_values(
    catalog: CatalogService,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
    metric_name: str,
    scale: str,
    normalization: str,
) -> None:
    runtime = same_model_access_paths[1]
    row = evidence(
        "runtime-lab",
        "coding",
        -1.0,
        source_url="https://example.com/runtime-metrics",
        confidence=0.9,
        metric_name=metric_name,
        metric_direction="lower_is_better",
        metric_scale=scale,
        normalization_method=normalization,
    )

    with pytest.raises(CatalogValidationError, match="non-negative"):
        catalog.import_records(
            [
                CatalogRecord(
                    evidence=row,
                    canonical_provider="openai",
                    canonical_model=runtime.key.model,
                    canonical_version=runtime.key.model,
                    runtime_id=runtime.key.stable_id(),
                )
            ]
        )


@pytest.mark.parametrize(
    ("metric_name", "direction", "scale", "normalization"),
    [
        ("latency", "higher_is_better", "unit_interval", "identity"),
        ("quality", "higher_is_better", "seconds", "divide_by_limit"),
        ("reliability", "lower_is_better", "percent", "divide_by_100"),
        (
            "metered_input_price",
            "higher_is_better",
            "unit_interval",
            "identity",
        ),
    ],
)
def test_catalog_rejects_metric_semantics_that_do_not_match_the_metric(
    catalog: CatalogService,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
    metric_name: str,
    direction: str,
    scale: str,
    normalization: str,
) -> None:
    runtime = same_model_access_paths[1]
    row = evidence(
        "runtime-lab",
        "coding",
        0.5,
        source_url="https://example.com/runtime-metrics",
        confidence=0.9,
        metric_name=metric_name,
        metric_direction=direction,
        metric_scale=scale,
        normalization_method=normalization,
    )

    with pytest.raises(CatalogValidationError, match="metric contract"):
        catalog.import_records(
            [
                CatalogRecord(
                    evidence=row,
                    canonical_provider="openai",
                    canonical_model=runtime.key.model,
                    canonical_version=runtime.key.model,
                    runtime_id=runtime.key.stable_id(),
                )
            ]
        )


def test_models_dev_source_requires_exact_canonical_model_and_known_price(
    mutable_clock,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> None:
    calls: list[tuple[str, str]] = []

    def get_model_info(provider: str, model: str) -> ModelInfo:
        calls.append((provider, model))
        return ModelInfo(
            id="gpt-5.4",
            name="GPT 5.4",
            family="gpt-5",
            provider_id="openai",
            reasoning=True,
            tool_call=True,
            context_window=128_000,
            cost_input=0.0,
            cost_output=0.0,
            release_date="2025-12-15",
        )

    source = ModelsDevCatalogSource(
        same_model_access_paths,
        get_model_info=get_model_info,
        clock=mutable_clock,
    )

    loaded = source.load()

    assert calls == [("openai", "gpt-5.4")]
    assert {record.evidence.metric_name for record in loaded} == {
        "capability_reasoning",
        "capability_tools",
    }
    assert all(record.canonical_provider == "openai" for record in loaded)
    assert all(record.canonical_version == "gpt-5.4" for record in loaded)
    assert all("price" not in record.evidence.metric_name for record in loaded)


def test_models_dev_price_does_not_manufacture_an_exact_runtime_binding(
    mutable_clock,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> None:
    def get_model_info(_provider: str, _model: str) -> ModelInfo:
        return ModelInfo(
            id="gpt-5.4",
            name="GPT 5.4",
            family="gpt-5",
            provider_id="openai",
            reasoning=True,
            tool_call=True,
            cost_input=3.0,
            cost_output=15.0,
            release_date="2025-12-15",
        )

    loaded = ModelsDevCatalogSource(
        same_model_access_paths,
        get_model_info=get_model_info,
        clock=mutable_clock,
    ).load()

    assert all("price" not in record.evidence.metric_name for record in loaded)


def test_hermes_source_keeps_raw_picker_price_on_the_metered_runtime(
    mutable_clock,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> None:
    metered_id = same_model_access_paths[1].key.stable_id()
    metered = same_model_access_paths[1]
    payload = {
        "providers": [
            {
                "slug": "openai",
                "models": ["gpt-5.4"],
                "capabilities": {
                    "gpt-5.4": {"reasoning": True, "fast": False}
                },
                "discovery": {
                    "provider": "openai",
                    "auth_identity": metered.key.auth_identity,
                    "credential_pool_identity": (
                        metered.key.credential_pool_identity
                    ),
                    "endpoint_identity": metered.key.endpoint_identity,
                    "api_mode": metered.key.api_mode,
                    "observed_at": OBSERVED_AT,
                    "pricing": {
                        "gpt-5.4": {
                            "input_usd_per_token": "0.000005",
                            "output_usd_per_token": "0.000015",
                            "observed_at": OBSERVED_AT,
                            "source_id": "openai-picker-pricing",
                            "ttl_seconds": 3600,
                            "fresh": True,
                        }
                    },
                },
            }
        ]
    }
    source = HermesCatalogSource(
        same_model_access_paths,
        load_payload=lambda: payload,
        clock=mutable_clock,
    )

    loaded = source.load()

    price_rows = [
        record for record in loaded if "price" in record.evidence.metric_name
    ]
    assert {record.runtime_id for record in price_rows} == {metered_id}
    assert {record.evidence.value for record in price_rows} == {5.0, 15.0}
    assert all(record.runtime_id is None for record in loaded if record not in price_rows)


def test_hermes_source_does_not_bind_generic_price_to_runtime(
    mutable_clock,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> None:
    payload = {
        "providers": [
            {
                "slug": "openai",
                "models": ["gpt-5.4"],
                "discovery": {
                    "observed_at": OBSERVED_AT,
                    "pricing": {
                        "gpt-5.4": {
                            "input_usd_per_token": "0.000005",
                            "output_usd_per_token": "0.000015",
                            "observed_at": OBSERVED_AT,
                            "source_id": "generic-picker-pricing",
                        }
                    },
                },
            }
        ]
    }

    loaded = HermesCatalogSource(
        same_model_access_paths,
        load_payload=lambda: payload,
        clock=mutable_clock,
    ).load()

    assert all("price" not in record.evidence.metric_name for record in loaded)


def test_hermes_exact_path_price_respects_freshness_and_ttl(
    mutable_clock,
) -> None:
    runtime = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:a",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            source_id="unknown-cost",
            provenance="no-current-price",
            observed_at=OBSERVED_AT,
        ),
    )
    raw_price = {
        "input_usd_per_token": "0.000005",
        "output_usd_per_token": "0.000015",
        "observed_at": OBSERVED_AT,
        "source_id": "provider-a-picker-pricing",
        "ttl_seconds": 3_600,
        "fresh": False,
    }
    payload = {
        "providers": [
            {
                "slug": "provider-a",
                "models": ["model-a"],
                "discovery": {
                    "provider": "provider-a",
                    "auth_identity": runtime.key.auth_identity,
                    "credential_pool_identity": "",
                    "endpoint_identity": runtime.key.endpoint_identity,
                    "api_mode": runtime.key.api_mode,
                    "observed_at": OBSERVED_AT,
                    "pricing": {"model-a": raw_price},
                },
            }
        ]
    }
    source = HermesCatalogSource(
        (runtime,),
        load_payload=lambda: payload,
        clock=mutable_clock,
    )

    assert all("price" not in row.evidence.metric_name for row in source.load())

    raw_price["fresh"] = True
    loaded = source.load()
    price_rows = tuple(
        row for row in loaded if "price" in row.evidence.metric_name
    )
    assert price_rows
    assert all(
        row.evidence.expires_at == "2026-01-01T01:00:00Z"
        for row in price_rows
    )
    catalog = CatalogService(clock=mutable_clock)
    catalog.import_records(
        [
            *price_rows,
            CatalogRecord(
                evidence=evidence(
                    "latency-lab",
                    "coding",
                    1.0,
                    model="model-a",
                    source_url="https://example.com/latency",
                    confidence=0.9,
                    metric_name="latency",
                    metric_direction="lower_is_better",
                    metric_scale="seconds",
                    normalization_method="divide_by_limit",
                ),
                canonical_provider="provider-a",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=runtime.key.stable_id(),
            ),
        ]
    )
    mutable_clock.advance(seconds=3_600)
    request = ProposalRequest(
        inventory=(runtime,),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=1_000,
        expected_output_tokens=100,
        objectives=ObjectiveWeights(
            quality=0.25,
            reliability=0.25,
            latency=0.25,
            cost=0.25,
        ),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=10.0,
    )

    proposal = Advisor(catalog).propose(request)

    assert proposal.primary is None
    assert proposal.explanation.rejected_candidates[runtime.key.stable_id()][
        "reasons"
    ] == ("estimated_cost_unknown",)


class MutableCatalogSource:
    def __init__(self, records: tuple[CatalogRecord, ...]) -> None:
        self.records = records
        self.error: Exception | None = None

    def load(self) -> tuple[CatalogRecord, ...]:
        if self.error is not None:
            raise self.error
        return self.records


def test_refresh_failure_reuses_persisted_snapshot_with_more_staleness(
    isolated_home,
    mutable_clock,
    inventory: InventoryFixture,
) -> None:
    del isolated_home
    runtime = inventory.verified("openai-codex", "gpt-5.4")
    row = evidence(
        "swe-bench",
        "coding",
        0.62,
        source_url="https://www.swebench.com/",
        confidence=0.8,
    )
    source = MutableCatalogSource(
        (
            CatalogRecord(
                evidence=row,
                canonical_provider="",
                canonical_model=row.model,
                canonical_version=row.model_version,
            ),
        )
    )
    with RoutingStore.open() as store:
        catalog = CatalogService(store=store, clock=mutable_clock)
        fresh = catalog.refresh([source])
        fresh_penalty = catalog.staleness_penalty(runtime)
        source.error = OSError("offline endpoint https://private.invalid")
        mutable_clock.advance(seconds=2 * 24 * 60 * 60)

        fallback = catalog.refresh([source])

        assert fallback.snapshot_id == fresh.snapshot_id
        assert fallback.stale_fallback is True
        assert catalog.evidence_for(runtime) == (row,)
        assert catalog.staleness_penalty(runtime) > fresh_penalty
        assert "private.invalid" not in " ".join(fallback.source_errors)

    with RoutingStore.open() as reopened:
        restarted = CatalogService(store=reopened, clock=mutable_clock)
        fallback_after_restart = restarted.refresh([source])

        assert fallback_after_restart.snapshot_id == fresh.snapshot_id
        assert restarted.evidence_for(runtime) == (row,)
        assert fallback_after_restart.stale_fallback is True


def test_refresh_without_a_valid_snapshot_fails_closed(mutable_clock) -> None:
    source = MutableCatalogSource(())
    source.error = OSError("offline")

    with pytest.raises(CatalogRefreshError, match="no valid snapshot"):
        CatalogService(clock=mutable_clock).refresh([source])


def test_empty_refresh_reuses_the_last_complete_snapshot(
    mutable_clock,
) -> None:
    row = evidence(
        "quality-lab",
        "coding",
        0.7,
        source_url="https://example.com/quality",
        confidence=0.8,
    )
    source = MutableCatalogSource(
        (
            CatalogRecord(
                evidence=row,
                canonical_provider="",
                canonical_model=row.model,
                canonical_version=row.model_version,
            ),
        )
    )
    catalog = CatalogService(clock=mutable_clock)
    fresh = catalog.refresh([source])
    source.records = ()

    fallback = catalog.refresh([source])

    assert fallback.snapshot_id == fresh.snapshot_id
    assert fallback.stale_fallback is True
    assert fallback.source_errors == ("MutableCatalogSource:EmptySource",)


def test_failed_refresh_never_labels_direct_imports_as_committed_snapshot(
    mutable_clock,
    inventory: InventoryFixture,
) -> None:
    committed = evidence(
        "quality-lab",
        "coding",
        0.7,
        source_url="https://example.com/quality",
        confidence=0.8,
    )
    staged = evidence(
        "review-lab",
        "coding",
        0.9,
        source_url="https://example.com/review",
        confidence=0.9,
    )
    source = MutableCatalogSource(
        (
            CatalogRecord(
                evidence=committed,
                canonical_provider="",
                canonical_model=committed.model,
                canonical_version=committed.model_version,
            ),
        )
    )
    catalog = CatalogService(clock=mutable_clock)
    fresh = catalog.refresh([source])
    catalog.import_records([staged])
    assert {
        row.source_id
        for row in catalog.evidence_for(
            inventory.verified("openai-codex", "gpt-5.4")
        )
    } == {"quality-lab", "review-lab"}
    source.error = OSError("offline")

    fallback = catalog.refresh([source])

    assert fallback.snapshot_id == fresh.snapshot_id
    assert [row.source_id for row in fallback.evidence] == ["quality-lab"]
    assert [
        row.source_id
        for row in catalog.evidence_for(
            inventory.verified("openai-codex", "gpt-5.4")
        )
    ] == ["quality-lab"]


def test_restart_offline_fallback_preserves_every_evidence_scope(
    isolated_home,
    mutable_clock,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> None:
    del isolated_home
    subscription, metered = same_model_access_paths
    other_provider = _runtime(
        "other-provider",
        "gpt-5.4",
        auth_identity="api-key:other",
        endpoint_identity="endpoint:other",
        state="verified",
        economics=metered.economics,
    )
    global_quality = evidence(
        "quality-lab",
        "coding",
        0.7,
        source_url="https://example.com/quality",
        confidence=0.8,
    )
    provider_capability = evidence(
        "provider-catalog",
        "catalog capability",
        1.0,
        source_url="https://example.com/capability",
        confidence=0.9,
        domain="general",
        metric_name="capability_tools",
    )
    subscription_latency = evidence(
        "latency-lab",
        "coding",
        1.0,
        source_url="https://example.com/latency",
        confidence=0.9,
        metric_name="latency",
        metric_direction="lower_is_better",
        metric_scale="seconds",
        normalization_method="divide_by_limit",
    )
    source = MutableCatalogSource(
        (
            CatalogRecord(
                evidence=global_quality,
                canonical_provider="",
                canonical_model="gpt-5.4",
                canonical_version="gpt-5.4",
            ),
            CatalogRecord(
                evidence=provider_capability,
                canonical_provider="openai",
                canonical_model="gpt-5.4",
                canonical_version="gpt-5.4",
            ),
            CatalogRecord(
                evidence=subscription_latency,
                canonical_provider="openai",
                canonical_model="gpt-5.4",
                canonical_version="gpt-5.4",
                runtime_id=subscription.key.stable_id(),
            ),
        )
    )
    with RoutingStore.open() as store:
        service = CatalogService(store=store, clock=mutable_clock)
        fresh = service.refresh([source])
        expected = {
            runtime.key.stable_id(): service.evidence_for(runtime)
            for runtime in (subscription, metered, other_provider)
        }

    source.error = OSError("offline")
    with RoutingStore.open() as reopened:
        restarted = CatalogService(store=reopened, clock=mutable_clock)
        fallback = restarted.refresh([source])

        assert fallback.snapshot_id == fresh.snapshot_id
        assert fallback.stale_fallback is True
        assert {
            runtime.key.stable_id(): restarted.evidence_for(runtime)
            for runtime in (subscription, metered, other_provider)
        } == expected


def test_restart_uses_the_newest_checksum_valid_complete_snapshot(
    isolated_home,
    mutable_clock,
) -> None:
    del isolated_home
    first_row = evidence(
        "quality-lab",
        "coding",
        0.7,
        source_url="https://example.com/quality",
        confidence=0.8,
    )
    second_row = first_row.model_copy(
        update={"source_id": "newer-lab", "value": 0.8}
    )
    source = MutableCatalogSource(
        (
            CatalogRecord(
                evidence=first_row,
                canonical_provider="",
                canonical_model=first_row.model,
                canonical_version=first_row.model_version,
            ),
        )
    )
    with RoutingStore.open() as store:
        service = CatalogService(store=store, clock=mutable_clock)
        older = service.refresh([source])
        mutable_clock.advance(seconds=1)
        source.records = (
            CatalogRecord(
                evidence=second_row,
                canonical_provider="",
                canonical_model=second_row.model,
                canonical_version=second_row.model_version,
            ),
        )
        newer = service.refresh([source])
        assert newer.snapshot_id != older.snapshot_id
        store.connection.execute(
            "UPDATE catalog_evidence SET document_json = ? WHERE snapshot_id = ?",
            ('{"tampered":true}', newer.snapshot_id),
        )

    with RoutingStore.open() as reopened:
        recovered = CatalogService(store=reopened, clock=mutable_clock)

        assert recovered.snapshot is not None
        assert recovered.snapshot.snapshot_id == older.snapshot_id
        assert recovered.snapshot.evidence == (first_row,)


def test_restart_skips_checksum_valid_but_semantically_invalid_snapshot(
    isolated_home,
    mutable_clock,
) -> None:
    del isolated_home
    valid = evidence(
        "quality-lab",
        "coding",
        0.7,
        source_url="https://example.com/quality",
        confidence=0.8,
    )
    source = MutableCatalogSource(
        (
            CatalogRecord(
                evidence=valid,
                canonical_provider="",
                canonical_model=valid.model,
                canonical_version=valid.model_version,
            ),
        )
    )
    invalid = valid.model_copy(
        update={
            "source_id": "invalid-newer",
            "metric_scale": "seconds",
            "normalization_method": "divide_by_limit",
        }
    )
    with RoutingStore.open() as store:
        older = CatalogService(store=store, clock=mutable_clock).refresh([source])
        store.write_catalog_snapshot(
            "invalid-newer",
            [invalid],
            created_at="2026-01-01T00:00:01Z",
        )

    with RoutingStore.open() as reopened:
        recovered = CatalogService(store=reopened, clock=mutable_clock)

        assert recovered.snapshot is not None
        assert recovered.snapshot.snapshot_id == older.snapshot_id
        assert recovered.snapshot.evidence == (valid,)


def test_persistence_failure_cannot_swap_committed_memory_or_database(
    isolated_home,
    mutable_clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_home
    first_row = evidence(
        "quality-lab",
        "coding",
        0.7,
        source_url="https://example.com/quality",
        confidence=0.8,
    )
    second_row = first_row.model_copy(
        update={"source_id": "newer-lab", "value": 0.8}
    )
    source = MutableCatalogSource(
        (
            CatalogRecord(
                evidence=first_row,
                canonical_provider="",
                canonical_model=first_row.model,
                canonical_version=first_row.model_version,
            ),
        )
    )
    with RoutingStore.open() as store:
        service = CatalogService(store=store, clock=mutable_clock)
        committed = service.refresh([source])
        source.records = (
            CatalogRecord(
                evidence=second_row,
                canonical_provider="",
                canonical_model=second_row.model,
                canonical_version=second_row.model_version,
            ),
        )

        def fail_write(*_args, **_kwargs):
            raise OSError("simulated durable write failure")

        monkeypatch.setattr(store, "write_catalog_snapshot", fail_write)
        with pytest.raises(OSError, match="durable write failure"):
            service.refresh([source])

        assert service.snapshot == committed
        assert service.snapshot.evidence == (first_row,)

    with RoutingStore.open() as reopened:
        restored = CatalogService(store=reopened, clock=mutable_clock)

        assert restored.snapshot is not None
        assert restored.snapshot.snapshot_id == committed.snapshot_id
        assert restored.snapshot.evidence == (first_row,)


def test_restart_never_turns_path_local_evidence_into_model_evidence(
    isolated_home,
    mutable_clock,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> None:
    del isolated_home
    subscription, metered = same_model_access_paths
    quality = evidence(
        "quality-lab",
        "coding",
        0.7,
        source_url="https://example.com/quality",
        confidence=0.8,
    )
    latency = evidence(
        "latency-lab",
        "coding",
        1.0,
        source_url="https://example.com/latency",
        confidence=0.9,
        metric_name="latency",
        metric_direction="lower_is_better",
        metric_scale="seconds",
        normalization_method="divide_by_limit",
    )
    source = MutableCatalogSource(
        (
            CatalogRecord(
                evidence=quality,
                canonical_provider="",
                canonical_model=quality.model,
                canonical_version=quality.model_version,
            ),
            CatalogRecord(
                evidence=latency,
                canonical_provider="openai",
                canonical_model="gpt-5.4",
                canonical_version="gpt-5.4",
                runtime_id=subscription.key.stable_id(),
            ),
        )
    )
    with RoutingStore.open() as store:
        CatalogService(store=store, clock=mutable_clock).refresh([source])

    with RoutingStore.open() as reopened:
        restarted = CatalogService(store=reopened, clock=mutable_clock)

        assert {
            row.source_id for row in restarted.evidence_for(subscription)
        } == {"quality-lab", "latency-lab"}
        assert restarted.evidence_for(metered) == (quality,)


def test_restart_never_turns_provider_bound_evidence_into_global_evidence(
    isolated_home,
    mutable_clock,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> None:
    del isolated_home
    openai_runtime = same_model_access_paths[1]
    other_runtime = _runtime(
        "other-provider",
        "gpt-5.4",
        auth_identity="api-key:other",
        endpoint_identity="endpoint:other",
        state="verified",
        economics=openai_runtime.economics,
    )
    capability = evidence(
        "provider-catalog",
        "catalog capability",
        1.0,
        source_url="https://example.com/capability",
        confidence=0.9,
        domain="general",
        metric_name="capability_tools",
    )
    source = MutableCatalogSource(
        (
            CatalogRecord(
                evidence=capability,
                canonical_provider="openai",
                canonical_model="gpt-5.4",
                canonical_version="gpt-5.4",
            ),
        )
    )
    with RoutingStore.open() as store:
        service = CatalogService(store=store, clock=mutable_clock)
        service.refresh([source])
        assert service.evidence_for(openai_runtime) == (capability,)
        assert service.evidence_for(other_runtime) == ()

    with RoutingStore.open() as reopened:
        restarted = CatalogService(store=reopened, clock=mutable_clock)

        assert restarted.evidence_for(openai_runtime) == (capability,)
        assert restarted.evidence_for(other_runtime) == ()


def test_finite_hard_gates_reject_unknown_cost_and_latency(mutable_clock) -> None:
    unknown_cost = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:a",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            source_id="unknown-cost",
            provenance="no-current-price",
            observed_at=OBSERVED_AT,
        ),
    )
    unknown_latency = _runtime(
        "provider-b",
        "model-b",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:b",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=1.0,
            metered_output_usd_per_million_tokens=1.0,
            source_id="known-cost",
            provenance="current-price",
            observed_at=OBSERVED_AT,
        ),
    )
    catalog = CatalogService(clock=mutable_clock)
    catalog.import_records(
        [
            CatalogRecord(
                evidence=evidence(
                    "latency-lab",
                    "coding",
                    1.0,
                    model="model-a",
                    source_url="https://example.com/latency",
                    confidence=0.9,
                    metric_name="latency",
                    metric_direction="lower_is_better",
                    metric_scale="seconds",
                    normalization_method="divide_by_limit",
                ),
                canonical_provider="provider-a",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=unknown_cost.key.stable_id(),
            )
        ]
    )
    request = ProposalRequest(
        inventory=(unknown_cost, unknown_latency),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=1_000,
        expected_output_tokens=100,
        objectives=ObjectiveWeights(
            quality=0.25,
            reliability=0.25,
            latency=0.25,
            cost=0.25,
        ),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=10.0,
    )

    proposal = Advisor(catalog).propose(request)

    assert proposal.primary is None
    assert proposal.fallbacks == ()
    assert proposal.explanation.rejected_candidates[
        unknown_cost.key.stable_id()
    ]["reasons"] == ("estimated_cost_unknown",)
    assert proposal.explanation.rejected_candidates[
        unknown_latency.key.stable_id()
    ]["reasons"] == ("estimated_latency_unknown",)


def test_advisor_consumes_only_exact_runtime_bound_metered_prices(
    mutable_clock,
) -> None:
    economics = AccessEconomics(
        billing_kind="metered",
        source_id="unknown-cost",
        provenance="no-current-price",
        observed_at=OBSERVED_AT,
    )
    priced = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:priced",
        endpoint_identity="endpoint:priced",
        state="verified",
        economics=economics,
    )
    unpriced = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:unpriced",
        endpoint_identity="endpoint:unpriced",
        state="verified",
        economics=economics,
    )
    catalog = CatalogService(clock=mutable_clock)
    rows: list[CatalogRecord] = []
    for source_id, metric_name, value in (
        ("price-a", "metered_input_price", 5.0),
        ("price-b", "metered_input_price", 7.0),
        ("price-a", "metered_output_price", 15.0),
    ):
        rows.append(
            CatalogRecord(
                evidence=evidence(
                    source_id,
                    "metered access-path price",
                    value,
                    model="model-a",
                    source_url="https://example.com/pricing",
                    confidence=0.9,
                    domain="economics",
                    metric_name=metric_name,
                    metric_direction="lower_is_better",
                    metric_scale="usd_per_million_tokens",
                    normalization_method="path_local_only",
                ),
                canonical_provider="provider-a",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=priced.key.stable_id(),
            )
        )
    for runtime in (priced, unpriced):
        rows.append(
            CatalogRecord(
                evidence=evidence(
                    "latency-lab",
                    "coding",
                    1.0,
                    model="model-a",
                    source_url="https://example.com/latency",
                    confidence=0.9,
                    metric_name="latency",
                    metric_direction="lower_is_better",
                    metric_scale="seconds",
                    normalization_method="divide_by_limit",
                ),
                canonical_provider="provider-a",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=runtime.key.stable_id(),
            )
        )
    catalog.import_records(rows)
    request = ProposalRequest(
        inventory=(priced, unpriced),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=1_000,
        expected_output_tokens=100,
        objectives=ObjectiveWeights(
            quality=0.25,
            reliability=0.25,
            latency=0.25,
            cost=0.25,
        ),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=10.0,
    )

    proposal = Advisor(catalog).propose(request)

    assert proposal.primary is not None
    assert proposal.primary.runtime_id == priced.key.stable_id()
    assert proposal.explanation.candidates[priced.key.stable_id()][
        "estimated_cost_usd"
    ] == pytest.approx(0.0085)
    assert proposal.explanation.rejected_candidates[unpriced.key.stable_id()][
        "reasons"
    ] == ("estimated_cost_unknown",)


def test_subscription_with_unknown_capacity_remains_eligible_with_uncertainty(
    mutable_clock,
) -> None:
    runtime = _runtime(
        "subscription-provider",
        "model-a",
        auth_identity="subscription:work",
        endpoint_identity="endpoint:subscription",
        state="verified",
        economics=AccessEconomics(
            billing_kind="subscription",
            effective_marginal_cost_usd_per_task=0.0,
            subscription_plan="work",
            subscription_state="active",
            source_id="subscription-account",
            provenance="authenticated-account",
            observed_at=OBSERVED_AT,
        ),
    )
    catalog = CatalogService(clock=mutable_clock)
    catalog.import_records(
        [
            CatalogRecord(
                evidence=evidence(
                    "latency-lab",
                    "coding",
                    1.0,
                    model="model-a",
                    source_url="https://example.com/latency",
                    confidence=0.9,
                    metric_name="latency",
                    metric_direction="lower_is_better",
                    metric_scale="seconds",
                    normalization_method="divide_by_limit",
                ),
                canonical_provider="subscription-provider",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=runtime.key.stable_id(),
            )
        ]
    )
    request = ProposalRequest(
        inventory=(runtime,),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=1_000,
        expected_output_tokens=100,
        objectives=ObjectiveWeights(
            quality=0.25,
            reliability=0.25,
            latency=0.25,
            cost=0.25,
        ),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=10.0,
    )

    proposal = Advisor(catalog).propose(request)

    assert proposal.primary is not None
    assert proposal.primary.runtime_id == runtime.key.stable_id()
    details = proposal.explanation.candidates[runtime.key.stable_id()]
    assert details["capacity_uncertainty"] == (
        "subscription_quota_unknown",
        "throttle_state_unknown",
    )
    assert details["uncertainty_penalty"] > 0


def test_future_cooldown_rejects_a_runtime_without_throttle_state(
    mutable_clock,
) -> None:
    runtime = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:a",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=1.0,
            metered_output_usd_per_million_tokens=1.0,
            cooldown_until="2026-01-01T01:00:00Z",
            source_id="runtime-state",
            provenance="authenticated-runtime",
            observed_at=OBSERVED_AT,
        ),
    )
    request = ProposalRequest(
        inventory=(runtime,),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=1_000,
        expected_output_tokens=100,
        objectives=ObjectiveWeights(
            quality=0.25,
            reliability=0.25,
            latency=0.25,
            cost=0.25,
        ),
    )

    proposal = Advisor(CatalogService(clock=mutable_clock)).propose(request)

    assert proposal.primary is None
    assert proposal.explanation.rejected_candidates[runtime.key.stable_id()][
        "reasons"
    ] == ("runtime_throttled",)


def test_subscription_cost_uses_and_discloses_conservative_path_estimates(
    mutable_clock,
) -> None:
    runtime = _runtime(
        "subscription-provider",
        "model-a",
        auth_identity="subscription:work",
        endpoint_identity="endpoint:subscription",
        state="verified",
        economics=AccessEconomics(
            billing_kind="subscription",
            effective_marginal_cost_usd_per_task=0.0,
            effective_amortized_cost_usd_per_task=5.0,
            subscription_plan="work",
            subscription_state="active",
            source_id="subscription-account",
            provenance="authenticated-account",
            observed_at=OBSERVED_AT,
        ),
    )
    catalog = CatalogService(clock=mutable_clock)
    catalog.import_records(
        [
            CatalogRecord(
                evidence=evidence(
                    "latency-lab",
                    "coding",
                    1.0,
                    model="model-a",
                    source_url="https://example.com/latency",
                    confidence=0.9,
                    metric_name="latency",
                    metric_direction="lower_is_better",
                    metric_scale="seconds",
                    normalization_method="divide_by_limit",
                ),
                canonical_provider="subscription-provider",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=runtime.key.stable_id(),
            )
        ]
    )
    request = ProposalRequest(
        inventory=(runtime,),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=1_000,
        expected_output_tokens=100,
        objectives=ObjectiveWeights(
            quality=0.25,
            reliability=0.25,
            latency=0.25,
            cost=0.25,
        ),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=10.0,
    )

    proposal = Advisor(catalog).propose(request)

    details = proposal.explanation.rejected_candidates[runtime.key.stable_id()]
    assert details["reasons"] == ("estimated_cost_exceeds_limit",)
    assert details["estimated_cost_usd"] == 5.0
    assert details["effective_marginal_cost_usd_per_task"] == 0.0
    assert details["effective_amortized_cost_usd_per_task"] == 5.0
    assert details["capacity_uncertainty"] == (
        "subscription_quota_unknown",
        "throttle_state_unknown",
    )


def test_expired_path_economics_cannot_satisfy_a_finite_cost_gate(
    mutable_clock,
) -> None:
    runtime = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:a",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=1.0,
            metered_output_usd_per_million_tokens=1.0,
            source_id="stale-price",
            evidence_ttl_seconds=3_600,
            provenance="picker-price",
            observed_at=OBSERVED_AT,
        ),
    )
    catalog = CatalogService(clock=mutable_clock)
    catalog.import_records(
        [
            CatalogRecord(
                evidence=evidence(
                    "latency-lab",
                    "coding",
                    1.0,
                    model="model-a",
                    source_url="https://example.com/latency",
                    confidence=0.9,
                    metric_name="latency",
                    metric_direction="lower_is_better",
                    metric_scale="seconds",
                    normalization_method="divide_by_limit",
                ),
                canonical_provider="provider-a",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=runtime.key.stable_id(),
            )
        ]
    )
    mutable_clock.advance(seconds=7_200)
    request = ProposalRequest(
        inventory=(runtime,),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=1_000,
        expected_output_tokens=100,
        objectives=ObjectiveWeights(
            quality=0.25,
            reliability=0.25,
            latency=0.25,
            cost=0.25,
        ),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=10.0,
    )

    proposal = Advisor(catalog).propose(request)

    assert proposal.primary is None
    assert proposal.explanation.rejected_candidates[runtime.key.stable_id()][
        "reasons"
    ] == ("estimated_cost_unknown",)


def test_explanation_partitions_every_path_and_discloses_inputs(
    advisor: Advisor,
    proposal_request: ProposalRequest,
) -> None:
    proposal = advisor.propose(proposal_request)
    input_ids = {runtime.key.stable_id() for runtime in proposal_request.inventory}
    accepted_ids = set(proposal.explanation.accepted_runtime_ids)
    rejected_ids = set(proposal.explanation.rejected_runtime_ids)

    assert proposal.explanation.request["objectives"] == {
        "quality": 0.55,
        "reliability": 0.25,
        "latency": 0.10,
        "cost": 0.10,
    }
    assert proposal.explanation.request["expected_input_tokens"] == 20_000
    assert proposal.explanation.request["expected_output_tokens"] == 2_000
    assert proposal.explanation.catalog["snapshot_id"] is None
    assert proposal.explanation.catalog["stale_fallback"] is False
    assert input_ids == accepted_ids | rejected_ids
    assert accepted_ids.isdisjoint(rejected_ids)
    assert proposal.primary is not None
    assert {
        proposal.primary.runtime_id,
        *(fallback.runtime_id for fallback in proposal.fallbacks),
    } == accepted_ids
    for runtime_id, details in proposal.explanation.candidates.items():
        assert details["runtime_id"] == runtime_id
        assert details["inventory_state"] == "verified"
        assert details["billing_kind"] in {"metered", "subscription", "local"}
        assert set(details["normalized_inputs"]) == {
            "quality",
            "reliability",
            "latency",
            "cost",
        }
        assert "reliability" in details["missing_priors"]
        assert details["uncertainty_penalty"] > 0
        assert set(details["uncertainty_components"]) == {
            "quality",
            "reliability",
            "latency",
            "cost",
            "capacity",
        }
        assert details["tie_breaker_runtime_id"] == runtime_id
        assert details["economics_source"]["source_id"]
        assert details["economics_source"]["observed_at"]
        assert "confidence" in details["economics_source"]
        assert all(
            {
                "source_id",
                "source_url",
                "retrieved_at",
                "published_at",
                "sample_size",
                "confidence",
                "normalization_method",
                "model",
                "model_version",
                "domain",
                "task_definition",
                "metric_name",
                "metric_direction",
                "metric_scale",
                "value",
                "used_for_score",
                "normalized_value",
                "conservative_value",
            }
            <= set(source)
            for source in details["sources"]
        )
    assert all(
        details["reasons"]
        for details in proposal.explanation.rejected_candidates.values()
    )
    for details in proposal.explanation.rejected_candidates.values():
        assert details["billing_kind"] in {"metered", "subscription", "local"}
        assert "subscription_quota_remaining" in details
        assert "throttle_state" in details
        assert details["economics_source"]["source_id"]
        assert details["economics_source"]["observed_at"]
        assert "confidence" in details["economics_source"]
        assert details["uncertainty"]


def test_irrelevant_evidence_age_does_not_change_candidate_staleness(
    advisor: Advisor,
    proposal_request: ProposalRequest,
) -> None:
    before = advisor.propose(proposal_request)
    before_penalties = {
        runtime_id: details["staleness_penalty"]
        for runtime_id, details in before.explanation.candidates.items()
    }
    advisor.catalog.import_records(
        [
            evidence(
                "old-capability-catalog",
                "catalog capability",
                1.0,
                source_url="https://example.com/old-capability",
                confidence=0.9,
                retrieved_at="2025-01-01T00:00:00Z",
                published_at="2024-12-01T00:00:00Z",
                domain="general",
                metric_name="capability_tools",
            )
        ]
    )

    after = advisor.propose(proposal_request)

    assert {
        runtime_id: details["staleness_penalty"]
        for runtime_id, details in after.explanation.candidates.items()
    } == before_penalties


def test_small_samples_remain_more_conservative_than_large_samples() -> None:
    small = conservative_metric(0.99, confidence=0.99, sample_size=1)
    large = conservative_metric(0.99, confidence=0.99, sample_size=10_000)

    assert 0 < small.value < large.value < 1
    assert small.uncertainty > large.uncertainty


def test_metric_direction_and_scale_are_normalized_exactly_once() -> None:
    assert normalize_catalog_metric(
        value=20.0,
        direction="higher_is_better",
        scale="percent",
        normalization_method="divide_by_100",
    ) == pytest.approx(0.2)
    assert normalize_catalog_metric(
        value=20.0,
        direction="lower_is_better",
        scale="percent",
        normalization_method="divide_by_100",
    ) == pytest.approx(0.8)

    with pytest.raises(ValueError, match="unsupported"):
        normalize_catalog_metric(
            value=20.0,
            direction="higher_is_better",
            scale="percent",
            normalization_method="identity",
        )


def test_runtime_reliability_evidence_replaces_the_missing_prior(
    mutable_clock,
    same_model_access_paths: tuple[ExecutableRuntime, ExecutableRuntime],
) -> None:
    runtime = same_model_access_paths[1]
    catalog = CatalogService(clock=mutable_clock)
    bound_rows = []
    for metric_name, value, direction, scale, normalization in (
        ("reliability", 0.9, "higher_is_better", "unit_interval", "identity"),
        ("latency", 1.0, "lower_is_better", "seconds", "divide_by_limit"),
    ):
        bound_rows.append(
            CatalogRecord(
                evidence=evidence(
                    "runtime-lab",
                    "coding",
                    value,
                    source_url="https://example.com/runtime-metrics",
                    confidence=1.0,
                    metric_name=metric_name,
                    metric_direction=direction,
                    metric_scale=scale,
                    sample_size=10_000,
                    normalization_method=normalization,
                ),
                canonical_provider="openai",
                canonical_model=runtime.key.model,
                canonical_version=runtime.key.model,
                runtime_id=runtime.key.stable_id(),
            )
        )
    catalog.import_records(bound_rows)
    request = ProposalRequest(
        inventory=(runtime,),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=1_000,
        expected_output_tokens=100,
        objectives=ObjectiveWeights(
            quality=0.25,
            reliability=0.25,
            latency=0.25,
            cost=0.25,
        ),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=10.0,
    )

    proposal = Advisor(catalog).propose(request)
    details = proposal.explanation.candidates[runtime.key.stable_id()]

    assert details["reliability"] > 0.8
    assert "reliability" not in details["missing_priors"]


def test_base_rank_then_runtime_id_are_deterministic_final_tiebreakers(
    mutable_clock,
) -> None:
    economics = AccessEconomics(
        billing_kind="metered",
        metered_input_usd_per_million_tokens=1.0,
        metered_output_usd_per_million_tokens=1.0,
        source_id="same-price",
        provenance="same-path-economics",
        observed_at=OBSERVED_AT,
    )
    first = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:first",
        endpoint_identity="endpoint:first",
        state="verified",
        economics=economics,
    )
    second = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:second",
        endpoint_identity="endpoint:second",
        state="verified",
        economics=economics,
    )
    catalog = CatalogService(clock=mutable_clock)
    latency = evidence(
        "latency-lab",
        "coding",
        1.0,
        model="model-a",
        source_url="https://example.com/latency",
        confidence=1.0,
        metric_name="latency",
        metric_direction="lower_is_better",
        metric_scale="seconds",
        normalization_method="divide_by_limit",
    )
    catalog.import_records(
        [
            CatalogRecord(
                evidence=latency,
                canonical_provider="provider-a",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=runtime.key.stable_id(),
            )
            for runtime in (first, second)
        ]
    )
    common = {
        "domain": "coding",
        "task_definition": "coding",
        "expected_input_tokens": 1_000,
        "expected_output_tokens": 100,
        "objectives": ObjectiveWeights(
            quality=0.25,
            reliability=0.25,
            latency=0.25,
            cost=0.25,
        ),
        "max_estimated_task_cost_usd": 1.0,
        "max_estimated_latency_seconds": 10.0,
    }
    ranked = Advisor(catalog).propose(
        ProposalRequest(
            inventory=(first, second),
            base_ranks={
                first.key.stable_id(): 2.0,
                second.key.stable_id(): 1.0,
            },
            **common,
        )
    )
    tied = Advisor(catalog).propose(
        ProposalRequest(
            inventory=(second, first),
            base_ranks={
                first.key.stable_id(): 1.0,
                second.key.stable_id(): 1.0,
            },
            **common,
        )
    )

    assert ranked.primary is not None
    assert ranked.primary.runtime_id == second.key.stable_id()
    assert tied.primary is not None
    assert tied.primary.runtime_id == min(
        first.key.stable_id(),
        second.key.stable_id(),
    )


def test_advisor_rejects_duplicate_runtime_identities(
    advisor: Advisor,
    proposal_request: ProposalRequest,
) -> None:
    runtime = proposal_request.inventory[0]
    duplicated = proposal_request.model_copy(
        update={"inventory": (runtime, runtime)}
    )

    with pytest.raises(ValueError, match="duplicate runtime"):
        advisor.propose(duplicated)


def test_hard_cost_gate_merges_all_fresh_exact_runtime_price_observations(
    mutable_clock,
) -> None:
    runtime_economics = AccessEconomics(
        billing_kind="metered",
        metered_input_usd_per_million_tokens=1.0,
        metered_output_usd_per_million_tokens=2.0,
        throttle_state="available",
        source_id="runtime-price",
        provenance="authenticated-runtime",
        observed_at=OBSERVED_AT,
    )
    runtime = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:target",
        endpoint_identity="endpoint:target",
        state="verified",
        economics=runtime_economics,
    )
    other_path = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:other",
        endpoint_identity="endpoint:other",
        state="verified",
        economics=runtime_economics,
    )
    catalog = CatalogService(clock=mutable_clock)
    rows: list[CatalogRecord] = []
    for target, source_id, metric_name, value in (
        (runtime, "new-price", "metered_input_price", 5.0),
        (runtime, "new-price", "metered_output_price", 15.0),
        (other_path, "other-price", "metered_input_price", 100.0),
        (other_path, "other-price", "metered_output_price", 100.0),
    ):
        price = evidence(
            source_id,
            "metered access-path price",
            value,
            model="model-a",
            source_url="https://example.com/pricing",
            confidence=0.9,
            domain="economics",
            metric_name=metric_name,
            metric_direction="lower_is_better",
            metric_scale="usd_per_million_tokens",
            normalization_method="path_local_only",
        ).model_copy(update={"expires_at": "2026-01-02T00:00:00Z"})
        rows.append(
            CatalogRecord(
                evidence=price,
                canonical_provider="provider-a",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=target.key.stable_id(),
            )
        )
    rows.append(
        CatalogRecord(
            evidence=evidence(
                "latency-lab",
                "coding",
                1.0,
                model="model-a",
                source_url="https://example.com/latency",
                confidence=0.9,
                metric_name="latency",
                metric_direction="lower_is_better",
                metric_scale="seconds",
                normalization_method="divide_by_limit",
            ),
            canonical_provider="provider-a",
            canonical_model="model-a",
            canonical_version="model-a",
            runtime_id=runtime.key.stable_id(),
        )
    )
    catalog.import_records(rows)
    request = ProposalRequest(
        inventory=(runtime,),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=100_000,
        expected_output_tokens=10_000,
        objectives=ObjectiveWeights(
            quality=0.25,
            reliability=0.25,
            latency=0.25,
            cost=0.25,
        ),
        max_estimated_task_cost_usd=0.6,
        max_estimated_latency_seconds=10.0,
    )

    proposal = Advisor(catalog).propose(request)

    assert proposal.primary is None
    details = proposal.explanation.rejected_candidates[runtime.key.stable_id()]
    assert details["reasons"] == ("estimated_cost_exceeds_limit",)
    assert details["estimated_cost_usd"] == pytest.approx(0.65)
    assert all(
        source["source_id"] != "other-price"
        for source in details["sources"]
    )


def test_fresh_exact_catalog_prices_replace_stale_runtime_prices(
    mutable_clock,
) -> None:
    runtime = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:a",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=100.0,
            metered_output_usd_per_million_tokens=100.0,
            throttle_state="available",
            source_id="stale-runtime-price",
            evidence_ttl_seconds=1,
            provenance="authenticated-runtime",
            observed_at=OBSERVED_AT,
        ),
    )
    catalog = CatalogService(clock=mutable_clock)
    rows: list[CatalogRecord] = []
    for metric_name in ("metered_input_price", "metered_output_price"):
        price = evidence(
            "fresh-exact-price",
            "metered access-path price",
            1.0,
            model="model-a",
            source_url="https://example.com/pricing",
            confidence=0.9,
            domain="economics",
            metric_name=metric_name,
            metric_direction="lower_is_better",
            metric_scale="usd_per_million_tokens",
            normalization_method="path_local_only",
        ).model_copy(update={"expires_at": "2026-01-02T00:00:00Z"})
        rows.append(
            CatalogRecord(
                evidence=price,
                canonical_provider="provider-a",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=runtime.key.stable_id(),
            )
        )
    catalog.import_records(rows)
    mutable_clock.advance(seconds=2)

    proposal = Advisor(catalog).propose(
        ProposalRequest(
            inventory=(runtime,),
            domain="coding",
            task_definition="coding",
            expected_input_tokens=100_000,
            expected_output_tokens=100_000,
            objectives=ObjectiveWeights(
                quality=0.25,
                reliability=0.25,
                latency=0.25,
                cost=0.25,
            ),
            max_estimated_task_cost_usd=1.0,
        )
    )

    assert proposal.primary is not None
    details = proposal.explanation.candidates[runtime.key.stable_id()]
    assert details["estimated_cost_usd"] == pytest.approx(0.2)


def test_catalog_model_and_version_matching_is_case_sensitive(
    mutable_clock,
) -> None:
    runtime = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:a",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            source_id="unknown-price",
            provenance="configured",
            observed_at=OBSERVED_AT,
        ),
    )
    uppercase = evidence(
        "uppercase-catalog",
        "coding",
        0.8,
        model="MODEL-A",
        source_url="https://example.com/quality",
        confidence=0.9,
    )
    catalog = CatalogService(clock=mutable_clock)
    catalog.import_records(
        [
            CatalogRecord(
                evidence=uppercase,
                canonical_provider="provider-a",
                canonical_model="MODEL-A",
                canonical_version="MODEL-A",
            )
        ]
    )

    assert catalog.evidence_for(runtime) == ()

    def lookup(_provider: str, _model: str) -> ModelInfo:
        return ModelInfo(
            id="MODEL-A",
            name="Model A",
            family="model-a",
            provider_id="provider-a",
            reasoning=True,
            tool_call=True,
            release_date="2025-12-15",
        )

    assert ModelsDevCatalogSource(
        (runtime,),
        get_model_info=lookup,
        clock=mutable_clock,
    ).load() == ()


def test_refresh_retains_complete_snapshot_for_no_sources_empty_or_failure(
    isolated_home,
    mutable_clock,
) -> None:
    del isolated_home
    first = evidence(
        "first-source",
        "coding",
        0.7,
        source_url="https://example.com/first",
        confidence=0.8,
    )
    second = evidence(
        "second-source",
        "coding",
        0.8,
        source_url="https://example.com/second",
        confidence=0.8,
    )
    changed = first.model_copy(update={"source_id": "partial-new", "value": 0.9})
    source_a = MutableCatalogSource(
        (
            CatalogRecord(
                evidence=first,
                canonical_provider="",
                canonical_model=first.model,
                canonical_version=first.model_version,
            ),
        )
    )
    source_b = MutableCatalogSource(
        (
            CatalogRecord(
                evidence=second,
                canonical_provider="",
                canonical_model=second.model,
                canonical_version=second.model_version,
            ),
        )
    )
    with RoutingStore.open() as store:
        catalog = CatalogService(store=store, clock=mutable_clock)
        complete = catalog.refresh([source_a, source_b])

        no_sources = catalog.refresh([])
        assert no_sources.snapshot_id == complete.snapshot_id
        assert no_sources.evidence == complete.evidence
        assert no_sources.stale_fallback is True
        assert no_sources.source_errors

        source_a.records = (
            CatalogRecord(
                evidence=changed,
                canonical_provider="",
                canonical_model=changed.model,
                canonical_version=changed.model_version,
            ),
        )
        source_b.records = ()
        partial = catalog.refresh([source_a, source_b])
        assert partial.snapshot_id == complete.snapshot_id
        assert partial.evidence == complete.evidence
        assert any("EmptySource" in error for error in partial.source_errors)

        source_b.error = OSError("offline")
        failed = catalog.refresh([source_a, source_b])
        assert failed.snapshot_id == complete.snapshot_id
        assert failed.evidence == complete.evidence
        assert any("OSError" in error for error in failed.source_errors)

    with RoutingStore.open() as reopened:
        restored = CatalogService(store=reopened, clock=mutable_clock)
        assert restored.snapshot is not None
        assert restored.snapshot.snapshot_id == complete.snapshot_id
        assert restored.snapshot.evidence == complete.evidence


def test_rejected_explanation_reconstructs_exact_price_hard_gate(
    mutable_clock,
) -> None:
    runtime = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:a",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            throttle_state="available",
            source_id="unknown-price",
            provenance="configured",
            observed_at=OBSERVED_AT,
        ),
    )
    catalog = CatalogService(clock=mutable_clock)
    rows: list[CatalogRecord] = []
    for metric_name, value in (
        ("metered_input_price", 5.0),
        ("metered_output_price", 15.0),
    ):
        rows.append(
            CatalogRecord(
                evidence=evidence(
                    "exact-price",
                    "metered access-path price",
                    value,
                    model="model-a",
                    source_url="https://example.com/pricing",
                    confidence=0.9,
                    domain="economics",
                    metric_name=metric_name,
                    metric_direction="lower_is_better",
                    metric_scale="usd_per_million_tokens",
                    normalization_method="path_local_only",
                ),
                canonical_provider="provider-a",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=runtime.key.stable_id(),
            )
        )
    rows.append(
        CatalogRecord(
            evidence=evidence(
                "latency-lab",
                "coding",
                1.0,
                model="model-a",
                source_url="https://example.com/latency",
                confidence=0.9,
                metric_name="latency",
                metric_direction="lower_is_better",
                metric_scale="seconds",
                normalization_method="divide_by_limit",
            ),
            canonical_provider="provider-a",
            canonical_model="model-a",
            canonical_version="model-a",
            runtime_id=runtime.key.stable_id(),
        )
    )
    catalog.import_records(rows)
    request = ProposalRequest(
        inventory=(runtime,),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=1_000,
        expected_output_tokens=100,
        objectives=ObjectiveWeights(
            quality=0.25,
            reliability=0.25,
            latency=0.25,
            cost=0.25,
        ),
        max_estimated_task_cost_usd=0.001,
        max_estimated_latency_seconds=10.0,
    )

    proposal = Advisor(catalog).propose(request)

    details = proposal.explanation.rejected_candidates[runtime.key.stable_id()]
    assert set(details["normalized_inputs"]) == {
        "quality",
        "reliability",
        "latency",
        "cost",
    }
    assert set(details["uncertainty_components"]) == {
        "quality",
        "reliability",
        "latency",
        "cost",
        "capacity",
    }
    price_sources = tuple(
        source
        for source in details["sources"]
        if "price" in source["metric_name"]
    )
    assert len(price_sources) == 2
    assert all(source["used_for_decision"] for source in price_sources)
    assert all(source["used_for_hard_gate"] for source in price_sources)
    assert all(not source["used_for_score"] for source in price_sources)


def test_equal_conservative_rows_are_order_independent(
    mutable_clock,
) -> None:
    runtime = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:a",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=1.0,
            metered_output_usd_per_million_tokens=1.0,
            throttle_state="available",
            source_id="known-price",
            provenance="authenticated-runtime",
            observed_at=OBSERVED_AT,
        ),
    )
    low_trust = evidence(
        "same-quality-source",
        "coding",
        0.0,
        model="model-a",
        source_url="https://example.com/quality",
        confidence=0.1,
        sample_size=1,
    )
    high_trust = evidence(
        "same-quality-source",
        "coding",
        0.0,
        model="model-a",
        source_url="https://example.com/quality",
        confidence=0.9,
        sample_size=10_000,
    )
    latency = CatalogRecord(
        evidence=evidence(
            "latency-lab",
            "coding",
            1.0,
            model="model-a",
            source_url="https://example.com/latency",
            confidence=0.9,
            metric_name="latency",
            metric_direction="lower_is_better",
            metric_scale="seconds",
            normalization_method="divide_by_limit",
        ),
        canonical_provider="provider-a",
        canonical_model="model-a",
        canonical_version="model-a",
        runtime_id=runtime.key.stable_id(),
    )
    request = ProposalRequest(
        inventory=(runtime,),
        domain="coding",
        task_definition="coding",
        expected_input_tokens=1_000,
        expected_output_tokens=100,
        objectives=ObjectiveWeights(
            quality=0.5,
            reliability=0.2,
            latency=0.2,
            cost=0.1,
        ),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=10.0,
    )

    proposals = []
    for quality_rows in (
        (low_trust, high_trust),
        (high_trust, low_trust),
    ):
        catalog = CatalogService(clock=mutable_clock)
        catalog.import_records([*quality_rows, latency])
        proposals.append(Advisor(catalog).propose(request))

    first, second = proposals
    assert first.primary is not None
    assert second.primary is not None
    assert first.primary.utility == second.primary.utility
    assert first.explanation == second.explanation


def test_hermes_capability_values_must_be_booleans(mutable_clock) -> None:
    runtime = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:a",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            source_id="unknown-price",
            provenance="configured",
            observed_at=OBSERVED_AT,
        ),
    )
    payload = {
        "providers": [
            {
                "slug": "provider-a",
                "models": ["model-a"],
                "capabilities": {
                    "model-a": {"reasoning": "false", "fast": False}
                },
                "discovery": {"observed_at": OBSERVED_AT},
            }
        ]
    }

    with pytest.raises(CatalogValidationError, match="capability"):
        HermesCatalogSource(
            (runtime,),
            load_payload=lambda: payload,
            clock=mutable_clock,
        ).load()


def test_dry_run_classifies_requirements_without_writes_or_content_retention(
    advisor: Advisor,
    proposal_request: ProposalRequest,
    isolated_home,
) -> None:
    proposal = advisor.propose(proposal_request)
    config_path = isolated_home / "config.yaml"
    original_config = b"model:\n  default: gpt-5.4\n"
    config_path.write_bytes(original_config)
    prompts = (
        "Debug the failing Python test using token sk-not-a-real-secret",
        "Inspect this image from https://private.invalid/endpoint",
    )
    with RoutingStore.open() as store:
        before_counts = {
            table: store.connection.execute(
                f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed table names
            ).fetchone()[0]
            for table in (
                "catalog_snapshots",
                "adaptive_revisions",
                "active_adaptive_revisions",
                "routing_decisions",
            )
        }

        result = advisor.dry_run(prompts, proposal)

        after_counts = {
            table: store.connection.execute(
                f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed table names
            ).fetchone()[0]
            for table in before_counts
        }
    rendered = repr(result)
    assert config_path.read_bytes() == original_config
    assert after_counts == before_counts
    assert len(result.assessments) == 2
    assert result.assessments[0].required_capabilities == ("coding",)
    assert result.assessments[1].required_modalities == ("image",)
    assert "sk-not-a-real-secret" not in rendered
    assert "private.invalid" not in rendered
    assert all(not hasattr(item, "prompt") for item in result.assessments)


def test_explanation_redacts_unsafe_inventory_economics_metadata(
    mutable_clock,
) -> None:
    runtime = _runtime(
        "provider-a",
        "model-a",
        auth_identity="api-key:work",
        endpoint_identity="endpoint:a",
        state="verified",
        economics=AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=1.0,
            metered_output_usd_per_million_tokens=1.0,
            throttle_state="access_token=supersecretvalue",
            source_id="api_key=supersecretvalue",
            provenance="password: supersecretvalue",
            observed_at=OBSERVED_AT,
        ),
    )
    catalog = CatalogService(clock=mutable_clock)
    latency = evidence(
        "latency-lab",
        "coding",
        1.0,
        model="model-a",
        source_url="https://example.com/latency",
        confidence=0.9,
        metric_name="latency",
        metric_direction="lower_is_better",
        metric_scale="seconds",
        normalization_method="divide_by_limit",
    )
    catalog.import_records(
        [
            CatalogRecord(
                evidence=latency,
                canonical_provider="provider-a",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=runtime.key.stable_id(),
            )
        ]
    )
    proposal = Advisor(catalog).propose(
        ProposalRequest(
            inventory=(runtime,),
            domain="coding",
            task_definition="coding",
            expected_input_tokens=1_000,
            expected_output_tokens=100,
            objectives=ObjectiveWeights(
                quality=0.25,
                reliability=0.25,
                latency=0.25,
                cost=0.25,
            ),
            max_estimated_task_cost_usd=1.0,
            max_estimated_latency_seconds=10.0,
        )
    )

    rendered = repr(proposal.explanation)
    assert "api_key=supersecretvalue" not in rendered
    assert "password: supersecretvalue" not in rendered
    assert "access_token=supersecretvalue" not in rendered


def test_request_and_explanation_nested_mappings_are_immutable(
    advisor: Advisor,
    proposal_request: ProposalRequest,
) -> None:
    runtime_id = proposal_request.inventory[0].key.stable_id()
    proposal = advisor.propose(proposal_request)

    assert proposal_request.model_dump(mode="json")["base_ranks"] == {}
    with pytest.raises(TypeError):
        proposal_request.base_ranks[runtime_id] = 99.0
    with pytest.raises(TypeError):
        proposal.explanation.candidates[runtime_id]["utility"] = 99.0
    with pytest.raises(TypeError):
        proposal.explanation.candidates[runtime_id]["normalized_inputs"][
            "quality"
        ] = 99.0
