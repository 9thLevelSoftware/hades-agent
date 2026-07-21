"""Verified executable-runtime inventory and access-probe contracts."""

from __future__ import annotations

import dataclasses
import json
import math
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.reasoning_support import ReasoningSupport
from hermes_cli.inventory import ConfigContext, build_models_payload
from hermes_cli.models import (
    PROVIDER_MODEL_DISCOVERY_CONTRACT_VERSION,
    PROVIDER_MODEL_LIVE_ATTEMPT_STATUSES,
    PROVIDER_MODEL_PROVENANCE_VALUES,
)
from plugins.auto_routing.auto_routing.adapters.base import (
    AccessVerification,
    AdapterInventory,
    LocalInventoryRow,
    ProviderInventoryRow,
    ResolvedRuntime,
    RuntimeResolutionMismatch,
    VerificationOutcomeUncertain,
    VerificationRequest,
    ensure_runtime_match,
)
from plugins.auto_routing.auto_routing.inventory import (
    InventoryService,
    VerificationApprovalRequired,
    VerificationEconomicsUnavailable,
    VerificationError,
    VerificationFailed,
    VerificationNotAllowed,
    VerificationPreconditionChanged,
    VerificationReplay,
)
from plugins.auto_routing.auto_routing.models import (
    AccessEconomics,
    LocalModelRequirements,
    PolicyEnvelope,
    RuntimeKey,
)
from plugins.auto_routing.auto_routing.storage import RoutingStore

OBSERVED_AT = "2026-01-01T00:00:00Z"
CODEX_CONTRACT_ENDPOINT = "endpoint:9ab74da1d15bdc50a0f3fd1c"


def _economics(provider: str, observed_at: str = OBSERVED_AT) -> AccessEconomics:
    if provider == "openai-codex":
        return AccessEconomics(
            billing_kind="subscription",
            effective_marginal_cost_usd_per_task=0.001,
            effective_amortized_cost_usd_per_task=0.01,
            subscription_plan="default",
            subscription_quota_remaining=None,
            subscription_quota_unit="completion",
            subscription_state="active",
            source_id="fake-subscription",
            provenance="configured-plan",
            observed_at=observed_at,
        )
    return AccessEconomics(
        billing_kind="metered",
        metered_input_usd_per_million_tokens=1.0,
        metered_output_usd_per_million_tokens=5.0,
        source_id="fake-pricing",
        provenance="provider-pricing",
        observed_at=observed_at,
    )


def provider_row(
    provider: str,
    models: list[str],
    *,
    authenticated: bool,
    live_attempt_status: str,
    model_provenance: dict[str, str],
    provenance_details: dict[str, dict],
    auth_identity: str = "api-key:default",
    resolver_name: str | None = None,
    credential_pool_identity: str = "pool:default",
    endpoint_identity: str = "endpoint:default",
    api_mode: str | None = None,
    capabilities: dict[str, dict] | None = None,
    economics: dict[str, AccessEconomics] | None = None,
    cooldown_until: str | None = None,
) -> ProviderInventoryRow:
    """Build one complete non-secret provider/access observation."""
    if live_attempt_status not in PROVIDER_MODEL_LIVE_ATTEMPT_STATUSES:
        raise ValueError(live_attempt_status)
    if set(model_provenance) != set(models):
        raise ValueError("per-model provenance is required")
    if set(provenance_details) != set(models):
        raise ValueError("per-model provenance details are required")
    return ProviderInventoryRow(
        provider=provider,
        resolver_name=resolver_name or provider,
        models=tuple(models),
        authenticated=authenticated,
        live_attempt_status=live_attempt_status,
        model_provenance=model_provenance,
        provenance_details=provenance_details,
        auth_identity=auth_identity,
        credential_pool_identity=credential_pool_identity,
        endpoint_identity=endpoint_identity,
        credential_fingerprint=f"fingerprint:{credential_pool_identity}",
        api_mode=api_mode
        or ("codex_responses" if provider == "openai-codex" else "chat_completions"),
        capabilities=capabilities
        or {
            model: {
                "supports_tools": True,
                "reasoning_options": ["low", "medium", "high"],
            }
            for model in models
        },
        economics=economics
        or {model: _economics(provider) for model in models},
        cooldown_until=cooldown_until,
        observed_at=OBSERVED_AT,
    )


def local_row(
    *,
    model: str,
    backend_identity: str,
    reachable: bool,
    installed: bool,
    open_weights: bool,
    memory_ok: bool | None,
    license_id: str | None = "apache-2.0",
    model_size_bytes: int | None = 14 * 1024**3,
    available_ram_bytes: int | None = 8 * 1024**3,
    available_vram_bytes: int | None = 4 * 1024**3,
    loaded_healthy: bool = False,
) -> LocalInventoryRow:
    return LocalInventoryRow(
        provider="custom",
        resolver_name="custom:ollama-default",
        model=model,
        backend_identity=backend_identity,
        reachable=reachable,
        installed=installed,
        open_weights=open_weights,
        license_id=license_id,
        model_size_bytes=model_size_bytes,
        available_ram_bytes=available_ram_bytes,
        available_vram_bytes=available_vram_bytes,
        loaded_healthy=loaded_healthy,
        hardware_compatible=True if memory_ok is True else None,
        api_mode="chat_completions",
        capabilities={
            "supports_tools": True,
            "reasoning_options": ["low", "medium", "high"],
        },
        economics=AccessEconomics(
            billing_kind="local",
            local_energy_cost_usd_per_task=0.01,
            source_id="local-runtime",
            provenance="backend-inspection",
            observed_at=OBSERVED_AT,
        ),
        observed_at=OBSERVED_AT,
    )


class FakeHermesAdapter:
    """Behavioral fake: no secret inventory, exact runtime calls recorded."""

    def __init__(self) -> None:
        self.rows: list[ProviderInventoryRow] = []
        self.local: LocalInventoryRow | None = None
        self.inventory_calls: list[bool] = []
        self.resolve_calls: list[RuntimeKey] = []
        self.verify_access_calls: list[RuntimeKey] = []
        self.return_wrong_auth_identity = False
        self.verification_result: object | None = None
        self.verification_error: Exception | None = None
        self.resolved_extra: dict[str, object] = {}
        self.resolved_base_url = "https://example.invalid/v1"
        self.resolved_api_key: object = "memory-only-secret"
        self.resolved_credential_pool: object | None = None

    def inventory(self, refresh: bool = False) -> AdapterInventory:
        self.inventory_calls.append(refresh)
        return AdapterInventory(
            provider_rows=tuple(self.rows),
            local_rows=() if self.local is None else (self.local,),
        )

    def resolve(self, runtime_key: RuntimeKey) -> ResolvedRuntime:
        self.resolve_calls.append(runtime_key)
        resolved_key = runtime_key
        if self.return_wrong_auth_identity:
            resolved_key = runtime_key.model_copy(
                update={"auth_identity": "api-key:wrong"}
            )
        resolved = ResolvedRuntime(
            runtime_key=resolved_key,
            resolver_name=runtime_key.provider,
            provider=runtime_key.provider,
            api_mode=runtime_key.api_mode,
            source="fake",
            base_url=self.resolved_base_url,
            api_key=self.resolved_api_key,
            credential_pool=self.resolved_credential_pool,
            extra=self.resolved_extra,
        )
        ensure_runtime_match(runtime_key, resolved.runtime_key)
        return resolved

    def verify_access(
        self,
        resolved_runtime: ResolvedRuntime,
        request: VerificationRequest,
    ) -> AccessVerification:
        assert isinstance(resolved_runtime, ResolvedRuntime)
        runtime_key = resolved_runtime.runtime_key
        capability = resolved_runtime.probe_capability
        assert capability is not None
        assert request.prompt == "Return exactly AUTO_ROUTING_ACCESS_OK"
        assert request.maximum_input_tokens == capability.maximum_input_tokens
        assert request.maximum_output_tokens == 16
        assert request.temperature == 0
        assert request.executor_id == capability.executor_id
        assert request.executor_version == capability.executor_version
        assert (
            request.execution_shape_fingerprint
            == capability.execution_shape_fingerprint
        )
        assert request.tools == ()
        assert request.persist is False
        self.verify_access_calls.append(runtime_key)
        if self.verification_error is not None:
            raise self.verification_error
        if self.verification_result is not None:
            return self.verification_result  # type: ignore[return-value]
        return AccessVerification(
            runtime_key=runtime_key,
            sentinel="AUTO_ROUTING_ACCESS_OK",
            response_model=runtime_key.model,
            input_tokens=7,
            output_tokens=5,
            actual_cost_usd=0.000032,
            response_hash="response:fake",
        )


@pytest.fixture
def fake_adapter() -> FakeHermesAdapter:
    return FakeHermesAdapter()


@pytest.fixture
def routing_store(isolated_home: Path):
    store = RoutingStore.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def policy() -> PolicyEnvelope:
    return PolicyEnvelope(
        eligible_sources=("configured_providers", "installed_local_models"),
        uninstalled_local_models="deny",
        local_models=LocalModelRequirements(
            require_open_weights=True,
            require_compatible_hardware=True,
        ),
        denied_providers=(),
        denied_models=(),
        max_estimated_task_cost_usd=5.0,
        max_estimated_latency_seconds=600.0,
        max_routing_overhead_usd_per_day=1.0,
        max_experiment_cost_usd_per_day=1.0,
        max_evaluator_calls_per_day=10,
        max_canary_fraction=0.1,
        max_reasoning_effort="xhigh",
        allow_subscription=True,
        allow_paid_access_probes=False,
        allowed_licenses=("apache-2.0", "mit"),
        minimum_context_tokens=1,
        canary_high_risk_tasks=False,
    )


def test_only_live_proven_access_is_verified(fake_adapter: FakeHermesAdapter) -> None:
    assert PROVIDER_MODEL_DISCOVERY_CONTRACT_VERSION == 1
    assert "authenticated_live" in PROVIDER_MODEL_PROVENANCE_VALUES
    assert "succeeded" in PROVIDER_MODEL_LIVE_ATTEMPT_STATUSES
    fake_adapter.rows = [
        provider_row(
            "openai-codex",
            ["gpt-5.4"],
            authenticated=True,
            live_attempt_status="succeeded",
            model_provenance={"gpt-5.4": "authenticated_live"},
            provenance_details={
                "gpt-5.4": {
                    "endpoint_identity": "endpoint:codex",
                    "auth_identity": "subscription:default",
                    "observed_at": OBSERVED_AT,
                }
            },
            auth_identity="subscription:default",
            endpoint_identity="endpoint:codex",
        ),
        provider_row(
            "anthropic",
            ["claude-sonnet-4-6"],
            authenticated=True,
            live_attempt_status="failed",
            model_provenance={"claude-sonnet-4-6": "static_curated"},
            provenance_details={
                "claude-sonnet-4-6": {"source": "curated"}
            },
        ),
        provider_row(
            "moa",
            ["default"],
            authenticated=True,
            live_attempt_status="succeeded",
            model_provenance={"default": "authenticated_live"},
            provenance_details={
                "default": {
                    "endpoint_identity": "endpoint:moa",
                    "auth_identity": "moa:default",
                    "observed_at": OBSERVED_AT,
                }
            },
            auth_identity="moa:default",
            endpoint_identity="endpoint:moa",
        ),
    ]

    snapshot = InventoryService(fake_adapter).refresh()

    assert [(runtime.key.provider, runtime.state) for runtime in snapshot.runtimes] == [
        ("openai-codex", "verified"),
        ("anthropic", "configured_unverified"),
    ]
    assert [runtime.key.model for runtime in snapshot.eligible()] == ["gpt-5.4"]


def test_failed_live_discovery_static_fallback_never_proves_access(
    fake_adapter: FakeHermesAdapter,
) -> None:
    fake_adapter.rows = [
        provider_row(
            "custom:work",
            ["private-model"],
            authenticated=True,
            live_attempt_status="failed",
            model_provenance={"private-model": "configured_declared"},
            provenance_details={"private-model": {"source": "configured"}},
        )
    ]

    runtime = InventoryService(fake_adapter).refresh().runtimes[0]

    assert runtime.state == "configured_unverified"
    assert runtime.reasons == ["model_access_not_live_verified"]


@pytest.mark.parametrize(
    ("provenance", "details"),
    [
        ("authenticated_live", {}),
        ("validated_contract", {"contract_id": "codex-subscription"}),
    ],
)
def test_malformed_strong_provenance_fails_closed(
    fake_adapter: FakeHermesAdapter,
    provenance: str,
    details: dict,
) -> None:
    fake_adapter.rows = [
        provider_row(
            "openai-codex",
            ["gpt-5.4"],
            authenticated=True,
            live_attempt_status="succeeded",
            model_provenance={"gpt-5.4": provenance},
            provenance_details={"gpt-5.4": details},
        )
    ]

    runtime = InventoryService(fake_adapter).refresh().runtimes[0]

    assert runtime.state == "configured_unverified"
    assert runtime.reasons == ["invalid_model_provenance_details"]


def test_validated_contract_requires_a_reviewed_provider_specific_binding(
    fake_adapter: FakeHermesAdapter,
) -> None:
    valid = provider_row(
        "openai-codex",
        ["gpt-5.4"],
        authenticated=True,
        live_attempt_status="probe_disabled",
        model_provenance={"gpt-5.4": "validated_contract"},
        provenance_details={
            "gpt-5.4": {
                "contract_id": "codex-subscription",
                "contract_version": 1,
            }
        },
        resolver_name="openai-codex",
        auth_identity="subscription:default",
        credential_pool_identity="subscription:default",
        endpoint_identity=CODEX_CONTRACT_ENDPOINT,
        api_mode="codex_responses",
    )
    fake_adapter.rows = [valid]
    accepted = InventoryService(fake_adapter).refresh().runtimes[0]
    assert accepted.state == "verified"
    assert accepted.verification_source == "validated_contract"

    invalid_rows = (
        dataclasses.replace(
            valid,
            provenance_details={
                "gpt-5.4": {
                    "contract_id": "arbitrary-catalog",
                    "contract_version": 1,
                }
            },
        ),
        dataclasses.replace(
            valid,
            provenance_details={
                "gpt-5.4": {
                    "contract_id": "codex-subscription",
                    "contract_version": 2,
                }
            },
        ),
        dataclasses.replace(valid, provider="anthropic"),
        dataclasses.replace(valid, auth_identity="api-key:work"),
        dataclasses.replace(valid, api_mode="chat_completions"),
        dataclasses.replace(valid, resolver_name="openai:work"),
    )
    for invalid in invalid_rows:
        fake_adapter.rows = [invalid]
        rejected = InventoryService(fake_adapter).refresh().runtimes[0]
        assert rejected.state == "configured_unverified"
        assert rejected.reasons == ["invalid_model_provenance_details"]
        assert rejected.verification_source is None


def test_validated_contract_rejects_model_outside_reviewed_catalog(
    fake_adapter: FakeHermesAdapter,
) -> None:
    invented_model = "gpt-invented-unreviewed"
    fake_adapter.rows = [provider_row(
        "openai-codex",
        [invented_model],
        authenticated=True,
        live_attempt_status="probe_disabled",
        model_provenance={invented_model: "validated_contract"},
        provenance_details={
            invented_model: {
                "contract_id": "codex-subscription",
                "contract_version": 1,
            }
        },
        resolver_name="openai-codex",
        auth_identity="subscription:default",
        credential_pool_identity="subscription:default",
        endpoint_identity=CODEX_CONTRACT_ENDPOINT,
        api_mode="codex_responses",
    )]

    runtime = InventoryService(fake_adapter).refresh().runtimes[0]

    assert runtime.state == "configured_unverified"
    assert runtime.reasons == ["invalid_model_provenance_details"]


def test_validated_contract_rejects_unreviewed_endpoint_identity(
    fake_adapter: FakeHermesAdapter,
) -> None:
    fake_adapter.rows = [provider_row(
        "openai-codex",
        ["gpt-5.4"],
        authenticated=True,
        live_attempt_status="probe_disabled",
        model_provenance={"gpt-5.4": "validated_contract"},
        provenance_details={
            "gpt-5.4": {
                "contract_id": "codex-subscription",
                "contract_version": 1,
            }
        },
        resolver_name="openai-codex",
        auth_identity="subscription:default",
        credential_pool_identity="subscription:default",
        endpoint_identity="endpoint:unreviewed-proxy",
        api_mode="codex_responses",
    )]

    runtime = InventoryService(fake_adapter).refresh().runtimes[0]

    assert runtime.state == "configured_unverified"
    assert runtime.reasons == ["invalid_model_provenance_details"]


def test_inventory_record_retains_exact_reasoning_support(
    fake_adapter: FakeHermesAdapter,
) -> None:
    fake_adapter.rows = [
        provider_row(
            "openai-codex",
            ["gpt-5.4"],
            authenticated=True,
            live_attempt_status="succeeded",
            model_provenance={"gpt-5.4": "authenticated_live"},
            provenance_details={
                "gpt-5.4": {
                    "endpoint_identity": "endpoint:codex",
                    "auth_identity": "subscription:default",
                    "observed_at": OBSERVED_AT,
                }
            },
            auth_identity="subscription:default",
            endpoint_identity="endpoint:codex",
            capabilities={
                "gpt-5.4": {
                    "supports_tools": True,
                    "reasoning_options": ["low", "medium", "high", "xhigh"],
                }
            },
        )
    ]

    runtime = InventoryService(fake_adapter).refresh().runtimes[0]

    assert runtime.reasoning_support == ReasoningSupport(
        efforts=("low", "medium", "high", "xhigh"),
        provider_aliases=(("minimal", "low"),),
        provenance="metadata:reasoning_options",
        exact=True,
    )


def test_non_tool_model_is_not_inventory_candidate(
    fake_adapter: FakeHermesAdapter,
) -> None:
    fake_adapter.rows = [
        provider_row(
            "openai",
            ["text-embedding-4"],
            authenticated=True,
            live_attempt_status="succeeded",
            model_provenance={"text-embedding-4": "authenticated_live"},
            provenance_details={
                "text-embedding-4": {
                    "endpoint_identity": "endpoint:default",
                    "auth_identity": "api-key:default",
                    "observed_at": OBSERVED_AT,
                }
            },
            capabilities={
                "text-embedding-4": {
                    "supports_tools": False,
                    "supports_reasoning": False,
                }
            },
        )
    ]

    assert InventoryService(fake_adapter).refresh().runtimes == []


def test_local_model_requires_reachable_backend_installed_identity_and_hardware(
    fake_adapter: FakeHermesAdapter,
) -> None:
    fake_adapter.local = local_row(
        model="qwen3:14b",
        backend_identity="ollama:default",
        reachable=True,
        installed=True,
        open_weights=True,
        memory_ok=False,
    )

    runtime = InventoryService(fake_adapter).refresh().runtimes[0]

    assert runtime.key.local_backend == fake_adapter.local.backend_identity
    assert runtime.state == "ineligible"
    assert runtime.reasons == ["hardware_compatibility_unproven"]


def test_verified_local_runtime_has_bounded_backend_evidence_expiry(
    fake_adapter: FakeHermesAdapter,
    mutable_clock,
) -> None:
    fake_adapter.local = local_row(
        model="qwen3:14b",
        backend_identity="ollama:default",
        reachable=True,
        installed=True,
        open_weights=True,
        memory_ok=True,
        loaded_healthy=True,
    )

    runtime = InventoryService(fake_adapter, clock=mutable_clock).refresh().runtimes[0]

    assert runtime.state == "verified"
    assert runtime.verification_source == "installed_local"
    assert runtime.verified_at == OBSERVED_AT
    assert runtime.verification_expires_at == "2026-01-02T00:00:00Z"


@pytest.mark.parametrize("slug", ["vllm", "llama.cpp", "custom:loopback"])
def test_hermes_018_marks_local_backends_without_native_facts_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    slug: str,
) -> None:
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18

    model = "publisher/model"
    payload = {
        "providers": [
            {
                "slug": slug,
                "name": slug,
                "authenticated": True,
                "models": [model],
                "api_url": "http://127.0.0.1:8000/v1",
                "capabilities": {
                    model: {
                        "supports_tools": True,
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "context_window": 32_000,
                        "max_output_tokens": 4_000,
                    }
                },
                "discovery": {
                    "provider": slug,
                    "resolver_name": f"{slug}:default",
                    "model_provenance": {model: "authenticated_live"},
                    "provenance_details": {
                        model: {
                            "endpoint_identity": f"endpoint:{slug}",
                            "auth_identity": f"local:{slug}",
                            "observed_at": OBSERVED_AT,
                        }
                    },
                    "live_attempt_status": "succeeded",
                    "observed_at": OBSERVED_AT,
                    "credential_fingerprint": f"credential:{slug}",
                    "endpoint_identity": f"endpoint:{slug}",
                    "auth_identity": f"local:{slug}",
                    "api_mode": "chat_completions",
                },
            }
        ],
        "model": model,
        "provider": slug,
    }
    monkeypatch.setattr(hermes_0_18, "load_picker_context", object)
    monkeypatch.setattr(
        hermes_0_18,
        "build_models_payload",
        lambda *_args, **_kwargs: payload,
    )
    monkeypatch.setattr(hermes_0_18, "get_model_capabilities", lambda *_args: None)
    monkeypatch.setattr(hermes_0_18, "get_model_info", lambda *_args: None)

    runtime = InventoryService(hermes_0_18.Hermes018Adapter()).refresh().runtimes[0]

    assert runtime.state == "ineligible"
    assert runtime.reasons == ["local_evidence_backend_unsupported"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"reachable": False}, "local_backend_unreachable"),
        ({"installed": False}, "local_model_not_installed"),
        ({"open_weights": False}, "open_weights_unproven"),
        ({"license_id": None}, "license_unproven"),
        ({"license_id": "proprietary"}, "license_not_allowed"),
        (
            {
                "memory_ok": None,
                "model_size_bytes": None,
                "available_ram_bytes": None,
                "available_vram_bytes": None,
            },
            "hardware_compatibility_unproven",
        ),
    ],
)
def test_local_inventory_fails_closed_for_each_required_fact(
    fake_adapter: FakeHermesAdapter,
    overrides: dict,
    reason: str,
) -> None:
    facts = {
        "model": "qwen3:14b",
        "backend_identity": "ollama:default",
        "reachable": True,
        "installed": True,
        "open_weights": True,
        "memory_ok": True,
    }
    facts.update(overrides)
    fake_adapter.local = local_row(**facts)

    runtime = InventoryService(fake_adapter, policy=PolicyEnvelope(
        eligible_sources=("installed_local_models",),
        uninstalled_local_models="deny",
        local_models=LocalModelRequirements(
            require_open_weights=True,
            require_compatible_hardware=True,
        ),
        denied_providers=(),
        denied_models=(),
        max_estimated_task_cost_usd=5.0,
        max_estimated_latency_seconds=600.0,
        max_routing_overhead_usd_per_day=1.0,
        max_experiment_cost_usd_per_day=1.0,
        max_evaluator_calls_per_day=10,
        max_canary_fraction=0.1,
        max_reasoning_effort="xhigh",
        allow_subscription=True,
        allow_paid_access_probes=False,
        allowed_licenses=("apache-2.0", "mit"),
        minimum_context_tokens=1,
        canary_high_risk_tasks=False,
    )).refresh().runtimes[0]

    assert runtime.state == "ineligible"
    assert runtime.reasons == [reason]


def test_active_cooldown_preserves_verified_history_and_expired_cooldown_does_not(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    details = {
        "gpt-5.4": {
            "endpoint_identity": "endpoint:codex",
            "auth_identity": "subscription:default",
            "observed_at": OBSERVED_AT,
        }
    }
    fake_adapter.rows = [provider_row(
        "openai-codex",
        ["gpt-5.4"],
        authenticated=True,
        live_attempt_status="succeeded",
        model_provenance={"gpt-5.4": "authenticated_live"},
        provenance_details=details,
        auth_identity="subscription:default",
        endpoint_identity="endpoint:codex",
    )]
    service = InventoryService(
        fake_adapter,
        routing_store,
        policy,
        clock=mutable_clock,
    )
    before = service.refresh().runtimes[0]
    assert before.state == "verified"

    mutable_clock.advance(seconds=1)
    fake_adapter.rows = [dataclasses.replace(
        fake_adapter.rows[0],
        cooldown_until="2026-01-01T01:00:00Z",
    )]
    during = service.refresh().runtimes[0]
    assert during.state == "temporarily_unavailable"
    assert during.reasons == ["provider_cooldown"]
    assert routing_store.connection.execute(
        "SELECT COUNT(*) FROM inventory_snapshots WHERE complete = 1"
    ).fetchone()[0] == 2

    mutable_clock.advance(seconds=60 * 60)
    after = service.refresh().runtimes[0]
    assert after.state == "verified"
    assert after.reasons != ["provider_cooldown"]


def test_subscription_exhaustion_is_temporary_but_unknown_quota_remains_eligible(
    fake_adapter: FakeHermesAdapter,
) -> None:
    base = provider_row(
        "openai-codex",
        ["gpt-5.4"],
        authenticated=True,
        live_attempt_status="succeeded",
        model_provenance={"gpt-5.4": "authenticated_live"},
        provenance_details={
            "gpt-5.4": {
                "endpoint_identity": "endpoint:codex",
                "auth_identity": "subscription:default",
                "observed_at": OBSERVED_AT,
            }
        },
        auth_identity="subscription:default",
        endpoint_identity="endpoint:codex",
    )
    exhausted = _economics("openai-codex").model_copy(
        update={"subscription_state": "exhausted", "subscription_quota_remaining": 0}
    )
    unknown = _economics("openai-codex").model_copy(
        update={"subscription_state": "active", "subscription_quota_remaining": None}
    )
    fake_adapter.rows = [dataclasses.replace(
        base,
        economics={"gpt-5.4": exhausted},
    )]
    unavailable = InventoryService(fake_adapter).refresh().runtimes[0]
    assert unavailable.state == "temporarily_unavailable"
    assert unavailable.reasons == ["subscription_quota_exhausted"]

    fake_adapter.rows = [dataclasses.replace(
        base,
        economics={"gpt-5.4": unknown},
    )]
    uncertain = InventoryService(fake_adapter).refresh().runtimes[0]
    assert uncertain.state == "verified"
    assert uncertain.reasons == ["subscription_quota_unknown"]
    assert InventoryService(fake_adapter).refresh().eligible()


def test_unaddressable_duplicate_paths_are_ineligible(
    fake_adapter: FakeHermesAdapter,
) -> None:
    first = provider_row(
        "openai",
        ["gpt-5.4"],
        authenticated=True,
        live_attempt_status="succeeded",
        model_provenance={"gpt-5.4": "authenticated_live"},
        provenance_details={
            "gpt-5.4": {
                "endpoint_identity": "endpoint:first",
                "auth_identity": "api-key:first",
                "observed_at": OBSERVED_AT,
            }
        },
        resolver_name="openai:shared",
        auth_identity="api-key:first",
        endpoint_identity="endpoint:first",
    )
    second = dataclasses.replace(
        first,
        auth_identity="api-key:second",
        endpoint_identity="endpoint:second",
        credential_fingerprint="fingerprint:second",
        provenance_details={
            "gpt-5.4": {
                "endpoint_identity": "endpoint:second",
                "auth_identity": "api-key:second",
                "observed_at": OBSERVED_AT,
            }
        },
    )
    fake_adapter.rows = [first, second]

    snapshot = InventoryService(fake_adapter).refresh()

    assert [runtime.state for runtime in snapshot.runtimes] == [
        "ineligible",
        "ineligible",
    ]
    assert all(
        runtime.reasons == ["ambiguous_access_path"]
        for runtime in snapshot.runtimes
    )
    assert snapshot.eligible() == []


def test_distinct_resolver_names_collapsing_to_one_runtime_are_ambiguous(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
) -> None:
    first = provider_row(
        "openai",
        ["gpt-5.4"],
        authenticated=True,
        live_attempt_status="succeeded",
        model_provenance={"gpt-5.4": "authenticated_live"},
        provenance_details={
            "gpt-5.4": {
                "endpoint_identity": "endpoint:shared",
                "auth_identity": "api-key:shared",
                "observed_at": OBSERVED_AT,
            }
        },
        resolver_name="openai:path-a",
        auth_identity="api-key:shared",
        credential_pool_identity="pool:shared",
        endpoint_identity="endpoint:shared",
    )
    second = dataclasses.replace(first, resolver_name="openai:path-b")
    fake_adapter.rows = [first, second]

    snapshot = InventoryService(fake_adapter, routing_store).refresh()

    assert len(snapshot.runtimes) == 2
    assert len({runtime.key.stable_id() for runtime in snapshot.runtimes}) == 1
    assert all(runtime.state == "ineligible" for runtime in snapshot.runtimes)
    assert all(
        runtime.reasons == ["ambiguous_access_path"]
        for runtime in snapshot.runtimes
    )
    stored = routing_store.read_inventory(snapshot.runtimes[0].key)
    assert stored is not None
    assert stored.state == "ineligible"
    assert routing_store.connection.execute(
        "SELECT COUNT(*) FROM inventory_observations"
    ).fetchone()[0] == 1


def test_paid_verification_is_explicit_bounded_and_exact(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    fake_adapter.rows = [
        provider_row(
            "anthropic",
            ["claude-sonnet-4-6"],
            authenticated=True,
            live_attempt_status="failed",
            model_provenance={"claude-sonnet-4-6": "static_curated"},
            provenance_details={
                "claude-sonnet-4-6": {"source": "curated"}
            },
        )
    ]
    policy = policy.model_copy(update={"allow_paid_access_probes": True})
    service = InventoryService(
        fake_adapter,
        routing_store,
        policy,
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]

    preview = service.preview_verification(runtime.key.stable_id())
    assert preview.runtime_id == runtime.key.stable_id()
    assert preview.maximum_output_tokens == 16
    assert preview.worst_case_cost_usd > 0
    assert fake_adapter.verify_access_calls == []

    with pytest.raises(VerificationApprovalRequired):
        service.apply_verification(
            preview.precondition_hash,
            acknowledge_billable=False,
        )

    result = service.apply_verification(
        preview.precondition_hash,
        acknowledge_billable=True,
    )

    assert result.state == "verified"
    assert fake_adapter.verify_access_calls == [runtime.key]
    stored = routing_store.read_inventory(runtime.key)
    assert stored is not None
    assert stored.verification_source == "explicit_probe"

    with pytest.raises(VerificationReplay):
        service.apply_verification(
            preview.precondition_hash,
            acknowledge_billable=True,
        )
    assert fake_adapter.verify_access_calls == [runtime.key]


def test_apply_dispatches_the_single_resolved_runtime_without_reresolving(
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    class BindingAdapter(FakeHermesAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.resolved_instances: list[ResolvedRuntime] = []
            self.dispatched_runtime: object | None = None

        def resolve(self, runtime_key: RuntimeKey) -> ResolvedRuntime:
            resolved = super().resolve(runtime_key)
            self.resolved_instances.append(resolved)
            return resolved

        def verify_access(
            self,
            runtime: object,
            request: VerificationRequest,
        ) -> AccessVerification:
            self.dispatched_runtime = runtime
            runtime_key = (
                runtime.runtime_key
                if isinstance(runtime, ResolvedRuntime)
                else runtime
            )
            assert isinstance(runtime_key, RuntimeKey)
            return AccessVerification(
                runtime_key=runtime_key,
                sentinel="AUTO_ROUTING_ACCESS_OK",
                response_model=runtime_key.model,
                input_tokens=7,
                output_tokens=5,
                actual_cost_usd=0.000032,
                response_hash="response:bound-runtime",
            )

    adapter = BindingAdapter()
    adapter.rows = [provider_row(
        "anthropic",
        ["claude-sonnet-4-6"],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={"claude-sonnet-4-6": "static_curated"},
        provenance_details={
            "claude-sonnet-4-6": {"source": "curated"}
        },
    )]
    service = InventoryService(
        adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]

    preview = service.preview_verification(runtime.key.stable_id())
    assert len(adapter.resolve_calls) == 1
    service.apply_verification(
        preview.precondition_hash,
        acknowledge_billable=True,
    )

    assert len(adapter.resolve_calls) == 2
    assert adapter.dispatched_runtime is adapter.resolved_instances[-1]


def test_apply_rejects_resolved_runtime_precondition_drift_before_reservation(
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    class DriftingAdapter(FakeHermesAdapter):
        def resolve(self, runtime_key: RuntimeKey) -> ResolvedRuntime:
            self.resolve_calls.append(runtime_key)
            return ResolvedRuntime(
                runtime_key=runtime_key,
                resolver_name=runtime_key.provider,
                provider=runtime_key.provider,
                api_mode=runtime_key.api_mode,
                source=f"resolution-phase-{len(self.resolve_calls)}",
                base_url="https://example.invalid/v1",
                api_key=f"memory-only-secret-{len(self.resolve_calls)}",
            )

    adapter = DriftingAdapter()
    adapter.rows = [provider_row(
        "anthropic",
        ["claude-sonnet-4-6"],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={"claude-sonnet-4-6": "static_curated"},
        provenance_details={
            "claude-sonnet-4-6": {"source": "curated"}
        },
    )]
    service = InventoryService(
        adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]
    preview = service.preview_verification(runtime.key.stable_id())

    with pytest.raises(VerificationPreconditionChanged):
        service.apply_verification(
            preview.precondition_hash,
            acknowledge_billable=True,
        )

    assert len(adapter.resolve_calls) == 2
    assert adapter.verify_access_calls == []
    assert routing_store.verification_attempt_sequence(
        runtime.key.stable_id()
    ) == 0


def test_apply_rejects_secret_only_credential_drift_before_billing_state(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    model = "claude-sonnet-4-6"
    fake_adapter.rows = [provider_row(
        "anthropic",
        [model],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={model: "static_curated"},
        provenance_details={model: {"source": "curated"}},
    )]
    fake_adapter.resolved_api_key = "credential-selected-during-preview"
    service = InventoryService(
        fake_adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]
    preview = service.preview_verification(runtime.key.stable_id())

    fake_adapter.resolved_api_key = "credential-selected-during-apply"
    with pytest.raises(
        VerificationPreconditionChanged,
        match="resolved runtime precondition changed",
    ):
        service.apply_verification(
            preview.precondition_hash,
            acknowledge_billable=True,
        )

    assert fake_adapter.resolve_calls == [runtime.key, runtime.key]
    assert fake_adapter.verify_access_calls == []
    assert routing_store.has_verification_attempt(preview.precondition_hash) is False
    assert routing_store.verification_attempt_sequence(
        runtime.key.stable_id()
    ) == 0
    budget = routing_store.daily_budget(
        "runtime-access-verification",
        mutable_clock.today(),
    )
    assert budget.committed_usd == 0
    assert budget.reserved_usd == 0
    assert budget.spent_usd == 0


def test_explicit_probe_evidence_survives_restart_until_its_exact_ttl(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    fake_adapter.rows = [provider_row(
        "anthropic",
        ["claude-sonnet-4-6"],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={"claude-sonnet-4-6": "static_curated"},
        provenance_details={
            "claude-sonnet-4-6": {"source": "curated"}
        },
    )]
    paid_policy = policy.model_copy(update={"allow_paid_access_probes": True})
    first_service = InventoryService(
        fake_adapter,
        routing_store,
        paid_policy,
        clock=mutable_clock,
    )
    configured = first_service.refresh().runtimes[0]
    preview = first_service.preview_verification(configured.key.stable_id())
    verified = first_service.apply_verification(
        preview.precondition_hash,
        acknowledge_billable=True,
    )
    assert verified.verification_source == "explicit_probe"

    mutable_clock.advance(seconds=60)
    restarted = InventoryService(
        fake_adapter,
        routing_store,
        paid_policy,
        clock=mutable_clock,
    ).refresh().runtimes[0]

    assert restarted.state == "verified"
    assert restarted.verification_source == "explicit_probe"
    assert restarted.verified_at == verified.verified_at
    assert restarted.verification_expires_at == verified.verification_expires_at
    assert fake_adapter.verify_access_calls == [configured.key]

    mutable_clock.advance(seconds=24 * 60 * 60)
    expired = InventoryService(
        fake_adapter,
        routing_store,
        paid_policy,
        clock=mutable_clock,
    ).refresh().runtimes[0]

    assert expired.state == "configured_unverified"
    assert expired.verification_source is None
    assert expired.reasons == ["model_access_not_live_verified"]
    assert fake_adapter.verify_access_calls == [configured.key]


def test_unknown_or_unbounded_economics_blocks_access_probe(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    unknown = AccessEconomics(
        billing_kind="metered",
        source_id="pricing-unavailable",
        provenance="inventory-unavailable",
        observed_at=OBSERVED_AT,
    )
    fake_adapter.rows = [provider_row(
        "anthropic",
        ["claude-sonnet-4-6"],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={"claude-sonnet-4-6": "static_curated"},
        provenance_details={
            "claude-sonnet-4-6": {"source": "curated"}
        },
        economics={"claude-sonnet-4-6": unknown},
    )]
    service = InventoryService(
        fake_adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]

    with pytest.raises(VerificationEconomicsUnavailable):
        service.preview_verification(runtime.key.stable_id())
    assert fake_adapter.verify_access_calls == []


@pytest.mark.parametrize(
    "observed_at",
    ("2025-12-30T23:59:59Z", "not-a-timestamp"),
    ids=("stale", "invalid"),
)
def test_access_probe_rejects_stale_or_invalid_economics_observation(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
    observed_at: str,
) -> None:
    economics = _economics("anthropic", observed_at=observed_at)
    fake_adapter.rows = [provider_row(
        "anthropic",
        ["claude-sonnet-4-6"],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={"claude-sonnet-4-6": "static_curated"},
        provenance_details={
            "claude-sonnet-4-6": {"source": "curated"}
        },
        economics={"claude-sonnet-4-6": economics},
    )]
    service = InventoryService(
        fake_adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]

    with pytest.raises(
        VerificationEconomicsUnavailable,
        match="fresh economics observation",
    ):
        service.preview_verification(runtime.key.stable_id())
    assert fake_adapter.verify_access_calls == []


def test_verification_preview_binds_executor_and_finite_input_token_bound(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    fake_adapter.rows = [provider_row(
        "anthropic",
        ["claude-sonnet-4-6"],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={"claude-sonnet-4-6": "static_curated"},
        provenance_details={
            "claude-sonnet-4-6": {"source": "curated"}
        },
        api_mode="anthropic_messages",
    )]
    service = InventoryService(
        fake_adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]
    preview = service.preview_verification(runtime.key.stable_id())

    assert preview.executor_id == "agent.auxiliary_client.call_llm_exact_once"
    assert preview.executor_version
    assert preview.execution_shape_fingerprint
    assert preview.protocol_overhead_tokens > 0
    assert preview.maximum_input_tokens >= len(
        "Return exactly AUTO_ROUTING_ACCESS_OK".encode("utf-8")
    )
    assert preview.worst_case_cost_usd == pytest.approx(
        (
            preview.maximum_input_tokens
            * runtime.economics.metered_input_usd_per_million_tokens
            + preview.maximum_output_tokens
            * runtime.economics.metered_output_usd_per_million_tokens
        )
        / 1_000_000
        * 1.10
    )


@pytest.mark.parametrize(
    ("provider", "api_mode", "resolved_extra"),
    (
        ("bedrock", "bedrock_converse", {"region": "us-east-1"}),
        ("openai", "codex_app_server", {}),
        ("copilot-acp", "chat_completions", {"requested_provider": "copilot-acp"}),
        (
            "custom",
            "chat_completions",
            {"extra_headers": {"X-Required-Runtime-Header": "memory-only"}},
        ),
        (
            "custom",
            "chat_completions",
            {"default_query": {"api-version": "2026-01-01"}},
        ),
    ),
    ids=("bedrock", "codex-app-server", "acp", "custom-header", "custom-query"),
)
def test_unsupported_exact_execution_shape_fails_before_preview_or_billing(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
    provider: str,
    api_mode: str,
    resolved_extra: dict[str, object],
) -> None:
    model = "provider-model"
    fake_adapter.rows = [provider_row(
        provider,
        [model],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={model: "static_curated"},
        provenance_details={model: {"source": "curated"}},
        api_mode=api_mode,
    )]
    fake_adapter.resolved_extra = resolved_extra
    service = InventoryService(
        fake_adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]

    with pytest.raises(
        VerificationNotAllowed,
        match="verification_(?:api_mode|execution_shape)_unsupported",
    ):
        service.preview_verification(runtime.key.stable_id())

    assert fake_adapter.verify_access_calls == []
    assert routing_store.verification_attempt_sequence(
        runtime.key.stable_id()
    ) == 0
    budget = routing_store.daily_budget(
        "runtime-access-verification",
        mutable_clock.today(),
    )
    assert budget.committed_usd == 0
    assert budget.reserved_usd == 0
    assert budget.spent_usd == 0


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("empty-endpoint", "verification_endpoint_unavailable"),
        ("malformed-endpoint", "verification_endpoint_unavailable"),
        ("missing-credential", "verification_credential_unavailable"),
        ("unusable-callable", "verification_credential_unavailable"),
        ("pool-without-selection", "verification_credential_unavailable"),
    ),
)
def test_incomplete_exact_execution_fails_preflight_without_billing_state(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
    case: str,
    expected_reason: str,
) -> None:
    model = "claude-sonnet-4-6"
    fake_adapter.rows = [provider_row(
        "anthropic",
        [model],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={model: "static_curated"},
        provenance_details={model: {"source": "curated"}},
        api_mode="anthropic_messages",
    )]
    callable_invocations: list[str] = []
    if case == "empty-endpoint":
        fake_adapter.resolved_base_url = ""
    elif case == "malformed-endpoint":
        fake_adapter.resolved_base_url = "not-an-http-endpoint"
    elif case == "missing-credential":
        fake_adapter.resolved_api_key = None
    elif case == "unusable-callable":
        def unusable_credential() -> str:
            callable_invocations.append("called")
            return ""

        fake_adapter.resolved_api_key = unusable_credential
    elif case == "pool-without-selection":
        fake_adapter.resolved_api_key = None
        fake_adapter.resolved_credential_pool = SimpleNamespace(
            select=lambda: None
        )
    else:  # pragma: no cover - table is closed above
        raise AssertionError(case)

    service = InventoryService(
        fake_adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]

    with pytest.raises(VerificationNotAllowed, match=f"^{expected_reason}$"):
        service.preview_verification(runtime.key.stable_id())

    if case == "unusable-callable":
        assert callable_invocations == ["called"]
    assert fake_adapter.resolve_calls == [runtime.key]
    assert fake_adapter.verify_access_calls == []
    assert routing_store.verification_attempt_sequence(
        runtime.key.stable_id()
    ) == 0
    budget = routing_store.daily_budget(
        "runtime-access-verification",
        mutable_clock.today(),
    )
    assert budget.committed_usd == 0
    assert budget.reserved_usd == 0
    assert budget.spent_usd == 0


def test_callable_credential_is_snapshotted_once_for_fingerprint_and_execution() -> None:
    from plugins.auto_routing.auto_routing.adapters.base import (
        _resolved_runtime_execution,
    )

    key = RuntimeKey(
        provider="anthropic",
        model="claude-sonnet-4-6",
        auth_identity="entra:work",
        credential_pool_identity="pool:entra-work",
        endpoint_identity="endpoint:anthropic",
        api_mode="anthropic_messages",
        inventory_revision="inventory-1",
    )
    secret = "callable-memory-only-token"
    invocations: list[str] = []

    def credential_provider() -> str:
        invocations.append("called")
        return secret

    runtime = ResolvedRuntime(
        runtime_key=key,
        resolver_name="anthropic:entra-work",
        provider="anthropic",
        api_mode="anthropic_messages",
        source="fake",
        base_url="https://api.anthropic.com",
        api_key=credential_provider,
    )

    assert invocations == ["called"]
    frozen_provider = _resolved_runtime_execution(runtime).api_key
    assert callable(frozen_provider)
    assert frozen_provider() == secret
    assert invocations == ["called"]
    assert runtime.probe_capability is not None
    assert secret not in repr(runtime)
    assert secret not in json.dumps(runtime.public_record(), sort_keys=True)


def test_subscription_probe_requires_known_active_non_exhausted_capacity(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    base_economics = _economics("openai-codex").model_copy(
        update={
            "effective_marginal_cost_usd_per_task": 0.0,
            "subscription_quota_remaining": 1,
            "subscription_state": "active",
        }
    )
    base_row = provider_row(
        "openai-codex",
        ["gpt-5.4"],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={"gpt-5.4": "static_curated"},
        provenance_details={"gpt-5.4": {"source": "curated"}},
        resolver_name="openai-codex",
        auth_identity="subscription:default",
        credential_pool_identity="subscription:default",
        endpoint_identity="endpoint:codex",
        economics={"gpt-5.4": base_economics},
    )
    paid_policy = policy.model_copy(update={"allow_paid_access_probes": True})

    invalid_economics = (
        base_economics.model_copy(update={"subscription_state": None}),
        base_economics.model_copy(update={"subscription_state": "unknown"}),
        base_economics.model_copy(update={"subscription_quota_remaining": None}),
        base_economics.model_copy(update={"subscription_quota_remaining": 0}),
        base_economics.model_copy(update={"subscription_quota_unit": None}),
    )
    for economics in invalid_economics:
        fake_adapter.rows = [dataclasses.replace(
            base_row,
            economics={"gpt-5.4": economics},
        )]
        service = InventoryService(
            fake_adapter,
            routing_store,
            paid_policy,
            clock=mutable_clock,
        )
        runtime = service.refresh().runtimes[0]
        with pytest.raises(
            VerificationEconomicsUnavailable,
            match="known active non-exhausted subscription quota",
        ):
            service.preview_verification(runtime.key.stable_id())

    fake_adapter.rows = [base_row]
    service = InventoryService(
        fake_adapter,
        routing_store,
        paid_policy,
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]
    preview = service.preview_verification(runtime.key.stable_id())
    assert preview.quota_unit == "completion"
    assert preview.worst_case_cost_usd > 0
    assert fake_adapter.verify_access_calls == []


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(
            sentinel="WRONG",
            response_model="claude-sonnet-4-6",
            input_tokens=7,
            output_tokens=5,
            actual_cost_usd=0.000032,
            response_hash="response:wrong-sentinel",
        ),
        SimpleNamespace(
            sentinel="AUTO_ROUTING_ACCESS_OK",
            response_model="claude-opus-4-6",
            input_tokens=7,
            output_tokens=5,
            actual_cost_usd=0.000032,
            response_hash="response:wrong-model",
        ),
        SimpleNamespace(
            sentinel="AUTO_ROUTING_ACCESS_OK",
            response_model="",
            input_tokens=7,
            output_tokens=5,
            actual_cost_usd=0.000032,
            response_hash="response:missing-model",
        ),
        SimpleNamespace(
            sentinel="AUTO_ROUTING_ACCESS_OK",
            response_model="claude-sonnet-4-6",
            input_tokens=math.nan,
            output_tokens=5,
            actual_cost_usd=0.000032,
            response_hash="response:invalid-usage",
        ),
    ],
    ids=("sentinel", "response-model", "missing-response-model", "finite-usage"),
)
def test_failed_verification_reconciles_billed_usage_without_proving_access(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
    result: SimpleNamespace,
) -> None:
    fake_adapter.rows = [provider_row(
        "anthropic",
        ["claude-sonnet-4-6"],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={"claude-sonnet-4-6": "static_curated"},
        provenance_details={
            "claude-sonnet-4-6": {"source": "curated"}
        },
    )]
    service = InventoryService(
        fake_adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]
    preview = service.preview_verification(runtime.key.stable_id())
    result.runtime_key = runtime.key
    fake_adapter.verification_result = result

    with pytest.raises(VerificationFailed):
        service.apply_verification(
            preview.precondition_hash,
            acknowledge_billable=True,
        )

    attempt = routing_store.read_verification_attempt(preview.precondition_hash)
    assert attempt is not None
    assert attempt.status == "failed"
    if math.isfinite(float(result.input_tokens)):
        assert attempt.input_tokens == result.input_tokens
        assert attempt.output_tokens == result.output_tokens
        assert attempt.actual_cost_usd == result.actual_cost_usd
    else:
        assert attempt.input_tokens == 0
        assert attempt.output_tokens == 0
        assert attempt.actual_cost_usd == 0
    assert routing_store.daily_budget(
        "runtime-access-verification",
        mutable_clock.today(),
    ).reserved_usd == 0
    assert fake_adapter.verify_access_calls == [runtime.key]
    assert service._snapshot is not None
    assert service._snapshot.eligible() == []


def test_uncertain_verification_reconciles_the_full_worst_case_reservation(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    fake_adapter.rows = [provider_row(
        "anthropic",
        ["claude-sonnet-4-6"],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={"claude-sonnet-4-6": "static_curated"},
        provenance_details={
            "claude-sonnet-4-6": {"source": "curated"}
        },
    )]
    fake_adapter.verification_error = VerificationOutcomeUncertain(
        "verification_request_outcome_uncertain"
    )
    service = InventoryService(
        fake_adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]
    preview = service.preview_verification(runtime.key.stable_id())

    with pytest.raises(
        VerificationFailed,
        match="verification_request_outcome_uncertain",
    ):
        service.apply_verification(
            preview.precondition_hash,
            acknowledge_billable=True,
        )

    attempt = routing_store.read_verification_attempt(preview.precondition_hash)
    assert attempt is not None
    assert attempt.status == "failed"
    assert attempt.reason_code == "verification_request_outcome_uncertain"
    assert attempt.input_tokens == 0
    assert attempt.output_tokens == 0
    assert attempt.actual_cost_usd == pytest.approx(preview.worst_case_cost_usd)
    budget = routing_store.daily_budget(
        "runtime-access-verification",
        mutable_clock.today(),
    )
    assert budget.reserved_usd == 0
    assert budget.spent_usd == pytest.approx(preview.worst_case_cost_usd)
    assert fake_adapter.verify_access_calls == [runtime.key]
    assert service._snapshot is not None
    assert service._snapshot.eligible() == []


def test_reported_input_usage_over_reserved_bound_is_uncertain_and_worst_case(
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    model = "claude-sonnet-4-6"
    fake_adapter.rows = [provider_row(
        "anthropic",
        [model],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={model: "static_curated"},
        provenance_details={model: {"source": "curated"}},
        api_mode="anthropic_messages",
    )]
    service = InventoryService(
        fake_adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]
    preview = service.preview_verification(runtime.key.stable_id())
    fake_adapter.verification_result = AccessVerification(
        runtime_key=runtime.key,
        sentinel="AUTO_ROUTING_ACCESS_OK",
        response_model=model,
        input_tokens=10_000,
        output_tokens=1,
        actual_cost_usd=0.01,
        response_hash="response:input-overrun",
    )

    with pytest.raises(
        VerificationFailed,
        match="verification_response_usage_uncertain",
    ):
        service.apply_verification(
            preview.precondition_hash,
            acknowledge_billable=True,
        )

    attempt = routing_store.read_verification_attempt(preview.precondition_hash)
    assert attempt is not None
    assert attempt.status == "failed"
    assert attempt.reason_code == "verification_response_usage_uncertain"
    assert attempt.input_tokens == 10_000
    assert attempt.output_tokens == 1
    assert attempt.actual_cost_usd == pytest.approx(0.01)
    budget = routing_store.daily_budget(
        "runtime-access-verification",
        mutable_clock.today(),
    )
    assert budget.reserved_usd == 0
    assert budget.spent_usd == pytest.approx(0.01)
    assert service._snapshot is not None
    assert service._snapshot.eligible() == []


def test_post_dispatch_cost_translation_exception_reconciles_worst_case(
    monkeypatch: pytest.MonkeyPatch,
    fake_adapter: FakeHermesAdapter,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18

    model = "claude-sonnet-4-6"
    economics = _economics("anthropic")
    fake_adapter.rows = [provider_row(
        "anthropic",
        [model],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={model: "static_curated"},
        provenance_details={model: {"source": "curated"}},
        economics={model: economics},
        api_mode="chat_completions",
    )]
    fake_adapter.resolved_extra = {"auto_routing_economics": economics}
    dispatch_calls: list[dict] = []

    def complete(**kwargs) -> SimpleNamespace:
        dispatch_calls.append(kwargs)
        return SimpleNamespace(
            model=model,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="AUTO_ROUTING_ACCESS_OK")
                )
            ],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=1),
        )

    monkeypatch.setattr(hermes_0_18, "call_llm_exact_once", complete)
    monkeypatch.setattr(
        hermes_0_18.Hermes018Adapter,
        "_actual_cost",
        staticmethod(
            lambda *_args: (_ for _ in ()).throw(
                RuntimeError("cost translator exploded after dispatch")
            )
        ),
    )
    fake_adapter.verify_access = hermes_0_18.Hermes018Adapter().verify_access
    service = InventoryService(
        fake_adapter,
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]
    preview = service.preview_verification(runtime.key.stable_id())

    with pytest.raises(
        VerificationFailed,
        match="verification_response_usage_uncertain",
    ):
        service.apply_verification(
            preview.precondition_hash,
            acknowledge_billable=True,
        )

    assert len(dispatch_calls) == 1
    attempt = routing_store.read_verification_attempt(preview.precondition_hash)
    assert attempt is not None
    assert attempt.status == "failed"
    assert attempt.reason_code == "verification_response_usage_uncertain"
    assert attempt.actual_cost_usd == pytest.approx(preview.worst_case_cost_usd)
    budget = routing_store.daily_budget(
        "runtime-access-verification",
        mutable_clock.today(),
    )
    assert budget.reserved_usd == 0
    assert budget.spent_usd == pytest.approx(preview.worst_case_cost_usd)


def test_concurrent_apply_consumes_one_preview_and_bills_one_call(
    fake_adapter: FakeHermesAdapter,
    isolated_home: Path,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    fake_adapter.rows = [provider_row(
        "anthropic",
        ["claude-sonnet-4-6"],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={"claude-sonnet-4-6": "static_curated"},
        provenance_details={
            "claude-sonnet-4-6": {"source": "curated"}
        },
    )]
    paid_policy = policy.model_copy(update={"allow_paid_access_probes": True})
    barrier = threading.Barrier(2)

    def apply_once() -> str:
        with RoutingStore.open(home=isolated_home) as store:
            service = InventoryService(
                fake_adapter,
                store,
                paid_policy,
                clock=mutable_clock,
            )
            runtime = service.refresh().runtimes[0]
            preview = service.preview_verification(runtime.key.stable_id())
            barrier.wait(timeout=5)
            try:
                return service.apply_verification(
                    preview.precondition_hash,
                    acknowledge_billable=True,
                ).state
            except VerificationError as error:
                return type(error).__name__

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: apply_once(), range(2)))

    assert outcomes.count("verified") == 1
    assert len(fake_adapter.verify_access_calls) == 1
    with RoutingStore.open(home=isolated_home) as store:
        attempts = store.connection.execute(
            "SELECT status, COUNT(*) AS count "
            "FROM runtime_verification_attempts GROUP BY status"
        ).fetchall()
        assert [(row["status"], row["count"]) for row in attempts] == [
            ("succeeded", 1)
        ]


def test_concurrent_distinct_previews_compare_attempt_sequence_in_transaction(
    fake_adapter: FakeHermesAdapter,
    isolated_home: Path,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    fake_adapter.rows = [provider_row(
        "anthropic",
        ["claude-sonnet-4-6"],
        authenticated=True,
        live_attempt_status="failed",
        model_provenance={"claude-sonnet-4-6": "static_curated"},
        provenance_details={
            "claude-sonnet-4-6": {"source": "curated"}
        },
    )]
    paid_policy = policy.model_copy(update={"allow_paid_access_probes": True})
    preview_barrier = threading.Barrier(2)
    transaction_barrier = threading.Barrier(2)

    def apply(index: int) -> tuple[str, str, str, int]:
        with RoutingStore.open(home=isolated_home) as store:
            worker_now = mutable_clock.now() + timedelta(seconds=index)
            service = InventoryService(
                fake_adapter,
                store,
                paid_policy,
                clock=lambda: worker_now,
            )
            runtime = service.refresh().runtimes[0]
            preview = service.preview_verification(runtime.key.stable_id())
            preview_barrier.wait(timeout=5)
            real_begin = store.begin_verification_attempt

            def gated_begin(*, _real_begin=real_begin, **kwargs):
                transaction_barrier.wait(timeout=5)
                return _real_begin(**kwargs)

            store.begin_verification_attempt = gated_begin  # type: ignore[method-assign]
            try:
                outcome = service.apply_verification(
                    preview.precondition_hash,
                    acknowledge_billable=True,
                ).state
            except VerificationError as error:
                outcome = type(error).__name__
            return (
                outcome,
                preview.precondition_hash,
                preview.runtime_id,
                preview.prior_attempt_sequence,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(apply, range(2)))

    outcomes = [result[0] for result in results]
    assert results[0][1] != results[1][1]
    assert results[0][2] == results[1][2]
    assert results[0][3] == 0
    assert results[1][3] == 0
    assert outcomes.count("verified") == 1
    assert len(fake_adapter.verify_access_calls) == 1
    with RoutingStore.open(home=isolated_home) as store:
        attempts = store.connection.execute(
            "SELECT status, COUNT(*) AS count "
            "FROM runtime_verification_attempts GROUP BY status"
        ).fetchall()
        assert [(row["status"], row["count"]) for row in attempts] == [
            ("succeeded", 1)
        ]


def test_concurrent_different_runtime_previews_cas_budget_revision_in_transaction(
    fake_adapter: FakeHermesAdapter,
    isolated_home: Path,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    models = ["claude-sonnet-4-6", "claude-haiku-4-5"]
    fake_adapter.rows = [
        provider_row(
            "anthropic",
            models,
            authenticated=True,
            live_attempt_status="failed",
            model_provenance={model: "static_curated" for model in models},
            provenance_details={
                model: {"source": "curated"} for model in models
            },
        )
    ]
    paid_policy = policy.model_copy(update={"allow_paid_access_probes": True})
    preview_barrier = threading.Barrier(2)
    transaction_barrier = threading.Barrier(2)

    def apply(index: int) -> tuple[str, str, str]:
        with RoutingStore.open(home=isolated_home) as store:
            service = InventoryService(
                fake_adapter,
                store,
                paid_policy,
                clock=mutable_clock,
            )
            snapshot = service.refresh()
            runtime = next(
                item for item in snapshot.runtimes if item.key.model == models[index]
            )
            preview = service.preview_verification(runtime.key.stable_id())
            preview_barrier.wait(timeout=5)
            real_begin = store.begin_verification_attempt

            def gated_begin(*, _real_begin=real_begin, **kwargs):
                transaction_barrier.wait(timeout=5)
                return _real_begin(**kwargs)

            store.begin_verification_attempt = gated_begin  # type: ignore[method-assign]
            try:
                outcome = service.apply_verification(
                    preview.precondition_hash,
                    acknowledge_billable=True,
                ).state
            except VerificationError as error:
                outcome = type(error).__name__
            return outcome, preview.runtime_id, preview.budget_ledger_revision

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(apply, range(2)))

    outcomes = [item[0] for item in results]
    assert results[0][1] != results[1][1]
    assert results[0][2] == results[1][2]
    assert outcomes.count("verified") == 1
    assert outcomes.count("VerificationPreconditionChanged") == 1
    assert len(fake_adapter.verify_access_calls) == 1
    with RoutingStore.open(home=isolated_home) as store:
        assert store.connection.execute(
            "SELECT COUNT(*) FROM runtime_verification_attempts"
        ).fetchone()[0] == 1
        assert store.connection.execute(
            "SELECT COUNT(*) FROM budget_ledger"
        ).fetchone()[0] == 1


def test_resolve_rejects_collapsed_or_wrong_access_path(
    fake_adapter: FakeHermesAdapter,
) -> None:
    subscription = provider_row(
        "openai-codex",
        ["gpt-5.4"],
        authenticated=True,
        live_attempt_status="succeeded",
        model_provenance={"gpt-5.4": "authenticated_live"},
        provenance_details={
            "gpt-5.4": {
                "endpoint_identity": "endpoint:codex",
                "auth_identity": "subscription:default",
                "observed_at": OBSERVED_AT,
            }
        },
        auth_identity="subscription:default",
        endpoint_identity="endpoint:codex",
        resolver_name="openai-codex",
    )
    metered = provider_row(
        "openai",
        ["gpt-5.4"],
        authenticated=True,
        live_attempt_status="succeeded",
        model_provenance={"gpt-5.4": "authenticated_live"},
        provenance_details={
            "gpt-5.4": {
                "endpoint_identity": "endpoint:openai",
                "auth_identity": "api-key:work",
                "observed_at": OBSERVED_AT,
            }
        },
        auth_identity="api-key:work",
        endpoint_identity="endpoint:openai",
        resolver_name="openai",
    )
    fake_adapter.rows = [subscription, metered]
    snapshot = InventoryService(fake_adapter).refresh()
    resolved = fake_adapter.resolve(snapshot.runtimes[0].key)
    assert resolved.runtime_key == snapshot.runtimes[0].key

    fake_adapter.return_wrong_auth_identity = True
    with pytest.raises(RuntimeResolutionMismatch):
        fake_adapter.resolve(snapshot.runtimes[0].key)


def test_resolved_runtime_hides_memory_only_credentials() -> None:
    key = RuntimeKey(
        provider="openai",
        model="gpt-5.4",
        auth_identity="api-key:work",
        credential_pool_identity="pool:work",
        endpoint_identity="endpoint:openai",
        api_mode="codex_responses",
        local_backend="",
        inventory_revision="inventory-1",
    )
    runtime = ResolvedRuntime(
        runtime_key=key,
        resolver_name="openai",
        provider="openai",
        api_mode="codex_responses",
        source="fake",
        base_url="https://api.openai.com/v1",
        api_key="sk-memory-only",
    )

    assert "sk-memory-only" not in repr(runtime)
    assert "api.openai.com" not in repr(runtime)
    assert runtime.public_record() == {
        "runtime_key": dataclasses.asdict(key) if dataclasses.is_dataclass(key) else key.model_dump(),
        "resolver_name": "openai",
        "provider": "openai",
        "api_mode": "codex_responses",
        "source": "fake",
        "credential_selection_fingerprint": (
            runtime.credential_selection_fingerprint
        ),
        "probe_capability": runtime.probe_capability.public_record(),
    }


def test_resolved_credential_fingerprint_is_non_secret_and_selection_specific() -> None:
    key = RuntimeKey(
        provider="openai",
        model="gpt-5.4",
        auth_identity="api-key:work",
        credential_pool_identity="pool:work",
        endpoint_identity="endpoint:openai",
        api_mode="codex_responses",
        inventory_revision="inventory-1",
    )
    first_secret = "sk-first-memory-only-selection"
    second_secret = "sk-second-memory-only-selection"
    first = ResolvedRuntime(
        runtime_key=key,
        resolver_name="openai",
        provider="openai",
        api_mode="codex_responses",
        source="fake",
        base_url="https://api.openai.com/v1",
        api_key=first_secret,
        credential_pool=SimpleNamespace(selected=first_secret),
    )
    second = ResolvedRuntime(
        runtime_key=key,
        resolver_name="openai",
        provider="openai",
        api_mode="codex_responses",
        source="fake",
        base_url="https://api.openai.com/v1",
        api_key=second_secret,
        credential_pool=SimpleNamespace(selected=second_secret),
    )

    assert first.credential_selection_fingerprint.startswith(
        "credential-selection:v1:"
    )
    assert (
        first.credential_selection_fingerprint
        != second.credential_selection_fingerprint
    )
    assert first.public_record() != second.public_record()

    serialized_surfaces = (
        repr(first),
        json.dumps(dataclasses.asdict(first), default=repr, sort_keys=True),
        json.dumps(getattr(first, "__dict__", {}), default=repr, sort_keys=True),
        json.dumps(first.public_record(), sort_keys=True),
        pickle.dumps(first).decode("latin-1"),
    )
    for serialized in serialized_surfaces:
        assert first_secret not in serialized
        assert "api.openai.com" not in serialized


def test_hermes_018_adapter_calls_real_inventory_projection_and_exact_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18

    context = object()
    payload_calls: list[tuple[object, dict]] = []
    resolver_calls: list[dict] = []
    completion_calls: list[dict] = []

    def build_payload(received_context: object, **kwargs) -> dict:
        payload_calls.append((received_context, kwargs))
        return {
            "providers": [
                {
                    "slug": "openai",
                    "name": "OpenAI work",
                    "authenticated": True,
                    "models": ["gpt-5.4"],
                    "source": "user-config",
                    "pricing": {
                        "gpt-5.4": {
                            "input": "$1.00",
                            "output": "$5.00",
                            "cache": None,
                            "free": False,
                        }
                    },
                    "discovery": {
                        "contract_version": 1,
                        "provider": "openai",
                        "resolver_name": "openai:work",
                        "models": ["gpt-5.4"],
                        "model_provenance": {
                            "gpt-5.4": "authenticated_live"
                        },
                        "provenance_details": {
                            "gpt-5.4": {
                                "endpoint_identity": "endpoint:work",
                                "auth_identity": "api-key:work",
                                "observed_at": OBSERVED_AT,
                            }
                        },
                        "live_attempt_status": "succeeded",
                        "observed_at": OBSERVED_AT,
                        "credential_fingerprint": "credential:work",
                        "endpoint_identity": "endpoint:work",
                        "auth_identity": "api-key:work",
                        "credential_pool_identity": "pool:work",
                        "api_mode": "codex_responses",
                        "source": "resolved-runtime",
                        "pricing": {
                            "gpt-5.4": {
                                "input_usd_per_token": "0.000001",
                                "output_usd_per_token": "0.000005",
                                "observed_at": OBSERVED_AT,
                                "source_id": "openai-models-api",
                                "cache_key": "openai:models",
                                "ttl_seconds": 86400,
                                "fresh": True,
                            }
                        },
                    },
                }
            ],
            "model": "gpt-5.4",
            "provider": "openai",
        }

    def resolve_runtime(**kwargs) -> dict:
        resolver_calls.append(kwargs)
        return {
            "provider": "openai",
            "api_mode": "codex_responses",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-memory-only",
            "source": "env",
            "auth_identity": "api-key:work",
            "credential_pool_identity": "pool:work",
            "endpoint_identity": "endpoint:work",
        }

    def complete(**kwargs) -> SimpleNamespace:
        completion_calls.append(kwargs)
        return SimpleNamespace(
            model="gpt-5.4",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="AUTO_ROUTING_ACCESS_OK"
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=8, completion_tokens=2),
        )

    monkeypatch.setattr(hermes_0_18, "load_picker_context", lambda: context)
    monkeypatch.setattr(hermes_0_18, "build_models_payload", build_payload)
    monkeypatch.setattr(hermes_0_18, "resolve_runtime_provider", resolve_runtime)
    monkeypatch.setattr(
        hermes_0_18,
        "get_model_capabilities",
        lambda _provider, _model: SimpleNamespace(
            supports_tools=True,
            supports_reasoning=True,
            reasoning_options=("low", "medium", "high"),
        ),
    )
    monkeypatch.setattr(hermes_0_18, "call_llm_exact_once", complete)

    adapter = hermes_0_18.Hermes018Adapter()
    service = InventoryService(adapter)
    runtime = service.refresh(refresh=True).runtimes[0]

    assert payload_calls == [
        (
            context,
            {
                "picker_hints": True,
                "pricing": True,
                "capabilities": True,
                "discovery_provenance": True,
                "refresh": True,
            },
        )
    ]
    assert runtime.state == "verified"
    assert runtime.economics.billing_kind == "metered"
    assert runtime.economics.metered_input_usd_per_million_tokens == 1
    assert runtime.economics.metered_output_usd_per_million_tokens == 5

    resolved = adapter.resolve(runtime.key)
    assert resolver_calls == [
        {"requested": "openai:work", "target_model": "gpt-5.4"}
    ]
    assert resolved.runtime_key == runtime.key
    assert "sk-memory-only" not in repr(resolved)
    verification = adapter.verify_access(
        resolved,
        VerificationRequest(
            prompt="Return exactly AUTO_ROUTING_ACCESS_OK",
            maximum_input_tokens=resolved.probe_capability.maximum_input_tokens,
            maximum_output_tokens=16,
            temperature=0,
            executor_id=resolved.probe_capability.executor_id,
            executor_version=resolved.probe_capability.executor_version,
            execution_shape_fingerprint=(
                resolved.probe_capability.execution_shape_fingerprint
            ),
            tools=(),
            persist=False,
        ),
    )
    assert resolver_calls == [
        {"requested": "openai:work", "target_model": "gpt-5.4"},
    ]
    assert completion_calls == [
        {
            "provider": "openai",
            "model": "gpt-5.4",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-memory-only",
            "api_mode": "codex_responses",
            "messages": [
                {
                    "role": "user",
                    "content": "Return exactly AUTO_ROUTING_ACCESS_OK",
                }
            ],
            "temperature": 0,
            "max_tokens": 16,
            "tools": [],
            "main_runtime": {
                "provider": "openai",
                "model": "gpt-5.4",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-memory-only",
                "api_mode": "codex_responses",
                "credential_pool": None,
            },
            "capability": resolved.probe_capability,
        }
    ]
    assert verification.runtime_key == runtime.key
    assert verification.sentinel == "AUTO_ROUTING_ACCESS_OK"
    assert verification.response_model == "gpt-5.4"
    assert verification.input_tokens == 8
    assert verification.output_tokens == 2
    assert verification.actual_cost_usd == pytest.approx(0.000018)
    assert len(verification.response_hash) == 64


def test_hermes_018_adapter_binds_raw_pricing_observation_and_ttl(
    monkeypatch: pytest.MonkeyPatch,
    routing_store: RoutingStore,
    policy: PolicyEnvelope,
    mutable_clock,
) -> None:
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18

    pricing_observed_at = "2025-12-31T22:00:00Z"
    payload = {
        "providers": [
            {
                "slug": "openrouter",
                "name": "OpenRouter work",
                "authenticated": True,
                "models": ["vendor/tiny"],
                "source": "user-config",
                # These display values have deliberately lost all useful precision.
                "pricing": {
                    "vendor/tiny": {
                        "input": "$0.00",
                        "output": "$0.00",
                        "cache": None,
                        "free": False,
                    }
                },
                "capabilities": {
                    "vendor/tiny": {
                        "supports_tools": True,
                        "supports_reasoning": False,
                        "reasoning_options": [],
                    }
                },
                "discovery": {
                    "contract_version": 1,
                    "provider": "openrouter",
                    "resolver_name": "openrouter:work",
                    "models": ["vendor/tiny"],
                    "model_provenance": {"vendor/tiny": "static_curated"},
                    "provenance_details": {
                        "vendor/tiny": {"source": "curated"}
                    },
                    "live_attempt_status": "failed",
                    "observed_at": OBSERVED_AT,
                    "credential_fingerprint": "credential:work",
                    "endpoint_identity": "endpoint:work",
                    "auth_identity": "api-key:work",
                    "credential_pool_identity": "pool:work",
                    "api_mode": "chat_completions",
                    "source": "resolved-runtime",
                    "pricing": {
                        "vendor/tiny": {
                            "input_usd_per_token": "0.0000000001",
                            "output_usd_per_token": "0.0000000002",
                            "observed_at": pricing_observed_at,
                            "source_id": "openrouter-models-api",
                            "cache_key": "openrouter:https://openrouter.ai/api/v1/models",
                            "ttl_seconds": 3600,
                            "fresh": False,
                        }
                    },
                },
            }
        ],
        "model": "vendor/tiny",
        "provider": "openrouter",
    }
    monkeypatch.setattr(hermes_0_18, "load_picker_context", object)
    monkeypatch.setattr(
        hermes_0_18,
        "build_models_payload",
        lambda *_args, **_kwargs: payload,
    )
    monkeypatch.setattr(
        hermes_0_18,
        "get_model_capabilities",
        lambda _provider, _model: None,
    )
    monkeypatch.setattr(
        hermes_0_18,
        "resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-memory-only",
            "source": "configured",
            "auth_identity": "api-key:work",
            "credential_pool_identity": "pool:work",
            "endpoint_identity": "endpoint:work",
        },
    )

    service = InventoryService(
        hermes_0_18.Hermes018Adapter(),
        routing_store,
        policy.model_copy(update={"allow_paid_access_probes": True}),
        clock=mutable_clock,
    )
    runtime = service.refresh().runtimes[0]

    assert runtime.economics.metered_input_usd_per_million_tokens == pytest.approx(
        0.0001
    )
    assert runtime.economics.metered_output_usd_per_million_tokens == pytest.approx(
        0.0002
    )
    assert runtime.economics.observed_at == pricing_observed_at
    assert runtime.economics.source_id == "openrouter-models-api"
    assert runtime.economics.evidence_ttl_seconds == 3600
    with pytest.raises(
        VerificationEconomicsUnavailable,
        match="fresh economics observation",
    ):
        service.preview_verification(runtime.key.stable_id())


def test_hermes_018_local_resolve_rejects_resolver_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18

    payload = {
        "providers": [
            {
                "slug": "ollama",
                "name": "Ollama local",
                "authenticated": True,
                "models": ["qwen3:14b"],
                "api_url": "http://127.0.0.1:11434/v1",
                "source": "user-config",
                "capabilities": {
                    "qwen3:14b": {
                        "supports_tools": True,
                        "supports_reasoning": True,
                        "reasoning_options": ["low", "medium", "high"],
                    }
                },
                "local_runtime": {
                    "backend_identity": "ollama:default",
                    "open_weights": True,
                    "license_id": "apache-2.0",
                    "model_size_bytes": 8 * 1024**3,
                    "available_ram_bytes": 16 * 1024**3,
                    "available_vram_bytes": 0,
                    "loaded_healthy": True,
                    "hardware_compatible": True,
                },
                "discovery": {
                    "contract_version": 1,
                    "provider": "ollama",
                    "resolver_name": "ollama:default",
                    "models": ["qwen3:14b"],
                    "model_provenance": {
                        "qwen3:14b": "authenticated_live"
                    },
                    "provenance_details": {
                        "qwen3:14b": {
                            "endpoint_identity": "endpoint:ollama",
                            "auth_identity": "local:ollama",
                            "observed_at": OBSERVED_AT,
                        }
                    },
                    "live_attempt_status": "succeeded",
                    "observed_at": OBSERVED_AT,
                    "credential_fingerprint": "credential:ollama",
                    "endpoint_identity": "endpoint:ollama",
                    "auth_identity": "local:ollama",
                    "credential_pool_identity": "",
                    "api_mode": "chat_completions",
                    "source": "resolved-runtime",
                    "pricing": {
                        "gpt-5.4": {
                            "input_usd_per_token": "0.000001",
                            "output_usd_per_token": "0.000005",
                            "observed_at": OBSERVED_AT,
                            "source_id": "openai-models-api",
                            "cache_key": "openai:models",
                            "ttl_seconds": 86400,
                            "fresh": True,
                        }
                    },
                },
            }
        ],
        "model": "qwen3:14b",
        "provider": "ollama",
    }
    monkeypatch.setattr(hermes_0_18, "load_picker_context", object)
    monkeypatch.setattr(
        hermes_0_18,
        "build_models_payload",
        lambda *_args, **_kwargs: payload,
    )
    monkeypatch.setattr(
        hermes_0_18,
        "resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "ollama",
            "api_mode": "chat_completions",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key": "ollama",
            "source": "configured",
            "auth_identity": "local:wrong",
            "credential_pool_identity": "pool:wrong",
            "endpoint_identity": "endpoint:wrong",
            "local_backend": "ollama:wrong",
        },
    )
    adapter = hermes_0_18.Hermes018Adapter()
    runtime = InventoryService(adapter).refresh().runtimes[0]

    with pytest.raises(RuntimeResolutionMismatch):
        adapter.resolve(runtime.key)


def test_hermes_018_adapter_preserves_authenticated_catalog_efforts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18

    payload = {
        "providers": [
            {
                "slug": "copilot",
                "name": "GitHub Copilot",
                "authenticated": True,
                "models": ["gpt-5.4"],
                "source": "copilot-subscription",
                "capabilities": {
                    "gpt-5.4": {
                        "reasoning": True,
                        "reasoning_options": ["low", "medium", "max"],
                        "reasoning_options_authenticated": True,
                    }
                },
                "discovery": {
                    "contract_version": 1,
                    "provider": "copilot",
                    "resolver_name": "copilot:work",
                    "models": ["gpt-5.4"],
                    "model_provenance": {
                        "gpt-5.4": "authenticated_live"
                    },
                    "provenance_details": {
                        "gpt-5.4": {
                            "endpoint_identity": "endpoint:copilot",
                            "auth_identity": "subscription:copilot-work",
                            "observed_at": OBSERVED_AT,
                        }
                    },
                    "live_attempt_status": "succeeded",
                    "observed_at": OBSERVED_AT,
                    "credential_fingerprint": "credential:copilot-work",
                    "endpoint_identity": "endpoint:copilot",
                    "auth_identity": "subscription:copilot-work",
                    "credential_pool_identity": "pool:copilot-work",
                    "api_mode": "chat_completions",
                    "source": "copilot-subscription",
                },
            }
        ],
        "model": "gpt-5.4",
        "provider": "copilot",
    }
    monkeypatch.setattr(hermes_0_18, "load_picker_context", object)
    monkeypatch.setattr(
        hermes_0_18,
        "build_models_payload",
        lambda *_args, **_kwargs: payload,
    )

    runtime = InventoryService(hermes_0_18.Hermes018Adapter()).refresh().runtimes[0]

    assert runtime.reasoning_support.exact is True
    assert runtime.reasoning_support.efforts == ("low", "medium", "max")
    assert runtime.reasoning_support.provenance == "metadata:reasoning_options"


def test_real_local_payload_and_adapter_keep_model_facts_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18

    context = ConfigContext(
        current_provider="lmstudio",
        current_model="publisher/ready-model",
        current_base_url="http://127.0.0.1:1234/v1",
        user_providers={},
        custom_providers=[],
    )
    row = {
        "slug": "lmstudio",
        "name": "LM Studio",
        "authenticated": True,
        "is_current": True,
        "is_user_defined": False,
        "models": ["publisher/ready-model", "publisher/unknown-model"],
        "total_models": 2,
        "api_url": "http://127.0.0.1:1234/v1",
        "source": "configured",
        "discovery": {
            "contract_version": 1,
            "provider": "lmstudio",
            "resolver_name": "lmstudio:default",
            "models": ["publisher/ready-model", "publisher/unknown-model"],
            "model_provenance": {
                "publisher/ready-model": "authenticated_live",
                "publisher/unknown-model": "authenticated_live",
            },
            "provenance_details": {
                "publisher/ready-model": {
                    "endpoint_identity": "endpoint:lmstudio",
                    "auth_identity": "local:lmstudio",
                    "observed_at": OBSERVED_AT,
                    "local_runtime": {
                        "backend_identity": "lmstudio:default",
                        "open_weights": True,
                        "license_id": "apache-2.0",
                        "model_size_bytes": 4 * 1024**3,
                        "loaded_healthy": True,
                        "hardware_compatible": True,
                    },
                },
                "publisher/unknown-model": {
                    "endpoint_identity": "endpoint:lmstudio",
                    "auth_identity": "local:lmstudio",
                    "observed_at": OBSERVED_AT,
                    "local_runtime": {
                        "backend_identity": "lmstudio:default",
                    },
                },
            },
            "live_attempt_status": "succeeded",
            "observed_at": OBSERVED_AT,
            "credential_fingerprint": "credential:lmstudio",
            "endpoint_identity": "endpoint:lmstudio",
            "auth_identity": "local:lmstudio",
            "credential_pool_identity": "",
            "api_mode": "chat_completions",
            "source": "resolved-runtime",
        },
    }

    def rows(**_kwargs):
        return json.loads(json.dumps([row]))

    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        rows,
    )
    monkeypatch.setattr("hermes_cli.inventory._moa_provider_row", lambda *_a: None)
    monkeypatch.setattr(hermes_0_18, "load_picker_context", lambda: context)

    payload = build_models_payload(
        context,
        capabilities=True,
        discovery_provenance=True,
    )
    local_payload = payload["providers"][0]["local_runtime"]
    assert set(local_payload) == {
        "publisher/ready-model",
        "publisher/unknown-model",
    }
    assert local_payload["publisher/ready-model"]["loaded_healthy"] is True
    assert local_payload["publisher/unknown-model"]["loaded_healthy"] is False

    snapshot = InventoryService(hermes_0_18.Hermes018Adapter()).refresh()
    by_model = {runtime.key.model: runtime for runtime in snapshot.runtimes}
    assert by_model["publisher/ready-model"].state == "verified"
    assert by_model["publisher/ready-model"].key.local_backend == "lmstudio:default"
    assert by_model["publisher/unknown-model"].state == "ineligible"
    assert by_model["publisher/unknown-model"].reasons == [
        "open_weights_unproven"
    ]


def test_resolved_runtime_generic_serialization_never_contains_execution_secrets() -> None:
    runtime_key = RuntimeKey(
        provider="openai",
        model="gpt-5.4",
        auth_identity="api-key:work",
        credential_pool_identity="pool:work",
        endpoint_identity="endpoint:work",
        api_mode="chat_completions",
        inventory_revision="inventory-1",
    )
    resolved = ResolvedRuntime(
        runtime_key=runtime_key,
        resolver_name="openai:work",
        provider="openai",
        api_mode="chat_completions",
        source="test",
        base_url="https://secret-endpoint.example.invalid/v1",
        api_key="sk-reviewer-secret",
        extra={"callback": lambda: "secret-callable"},
    )

    generic_dump = dataclasses.asdict(resolved)
    instance_state = getattr(resolved, "__dict__", {})
    serialized = json.dumps(generic_dump, default=repr, sort_keys=True)
    serialized_state = json.dumps(instance_state, default=repr, sort_keys=True)

    assert "secret-endpoint" not in serialized
    assert "sk-reviewer-secret" not in serialized
    assert "secret-callable" not in serialized
    assert "secret-endpoint" not in serialized_state
    assert "sk-reviewer-secret" not in serialized_state
    assert "secret-callable" not in serialized_state


def test_hermes_018_verification_retryable_error_makes_one_transport_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import auxiliary_client
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18

    transport_calls: list[dict] = []

    class RetryableTransport:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=self)
            self.base_url = "https://api.example.invalid/v1"

        def create(self, **kwargs):
            transport_calls.append(kwargs)
            raise ConnectionError("connection reset after request send")

    transport = RetryableTransport()
    payload = {
        "providers": [
            {
                "slug": "openai",
                "name": "OpenAI work",
                "authenticated": True,
                "models": ["gpt-5.4"],
                "source": "user-config",
                "pricing": {
                    "gpt-5.4": {
                        "input": "$1.00",
                        "output": "$5.00",
                    }
                },
                "discovery": {
                    "contract_version": 1,
                    "provider": "openai",
                    "resolver_name": "openai:work",
                    "models": ["gpt-5.4"],
                    "model_provenance": {
                        "gpt-5.4": "authenticated_live"
                    },
                    "provenance_details": {
                        "gpt-5.4": {
                            "endpoint_identity": "endpoint:work",
                            "auth_identity": "api-key:work",
                            "observed_at": OBSERVED_AT,
                        }
                    },
                    "live_attempt_status": "succeeded",
                    "observed_at": OBSERVED_AT,
                    "credential_fingerprint": "credential:work",
                    "endpoint_identity": "endpoint:work",
                    "auth_identity": "api-key:work",
                    "credential_pool_identity": "pool:work",
                    "api_mode": "chat_completions",
                    "source": "resolved-runtime",
                },
            }
        ],
        "model": "gpt-5.4",
        "provider": "openai",
    }
    runtime_resolution = {
        "provider": "openai",
        "api_mode": "chat_completions",
        "base_url": "https://api.example.invalid/v1",
        "api_key": "sk-memory-only",
        "source": "env",
        "auth_identity": "api-key:work",
        "credential_pool_identity": "pool:work",
        "endpoint_identity": "endpoint:work",
    }

    monkeypatch.setattr(hermes_0_18, "load_picker_context", object)
    monkeypatch.setattr(hermes_0_18, "build_models_payload", lambda *_a, **_k: payload)
    monkeypatch.setattr(
        hermes_0_18,
        "resolve_runtime_provider",
        lambda **_kwargs: dict(runtime_resolution),
    )
    def create_exact_client(**kwargs):
        assert kwargs["api_key"] == "sk-memory-only"
        assert kwargs["base_url"] == "https://api.example.invalid/v1"
        assert kwargs["api_mode"] == "chat_completions"
        assert kwargs["provider"] == "openai"
        assert kwargs["model"] == "gpt-5.4"
        return transport

    monkeypatch.setattr(
        auxiliary_client,
        "_create_exact_runtime_client",
        create_exact_client,
    )
    monkeypatch.setattr(
        auxiliary_client,
        "resolve_provider_client",
        lambda *_a, **_k: pytest.fail("exact probe re-resolved its runtime"),
    )
    monkeypatch.setattr(auxiliary_client, "_transient_retry_count", lambda: 2)
    monkeypatch.setattr(auxiliary_client, "_TRANSIENT_RETRY_BACKOFF_BASE", 0.0)

    adapter = hermes_0_18.Hermes018Adapter()
    runtime = InventoryService(adapter).refresh().runtimes[0]
    resolved = adapter.resolve(runtime.key)
    with pytest.raises(Exception):
        adapter.verify_access(
            resolved,
            VerificationRequest(
                prompt="Return exactly AUTO_ROUTING_ACCESS_OK",
                maximum_input_tokens=(
                    resolved.probe_capability.maximum_input_tokens
                ),
                maximum_output_tokens=16,
                temperature=0,
                executor_id=resolved.probe_capability.executor_id,
                executor_version=resolved.probe_capability.executor_version,
                execution_shape_fingerprint=(
                    resolved.probe_capability.execution_shape_fingerprint
                ),
                tools=(),
                persist=False,
            ),
        )

    assert len(transport_calls) == 1
    assert transport_calls[0]["temperature"] == 0
    assert (
        transport_calls[0].get("max_tokens")
        or transport_calls[0].get("max_completion_tokens")
    ) == 16
    assert transport_calls[0].get("tools", []) == []


def test_exact_anthropic_probe_uses_one_non_streaming_physical_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import anthropic_adapter, auxiliary_client

    calls = {"stream": 0, "create": 0}

    class Messages:
        def stream(self, **_kwargs):
            calls["stream"] += 1
            raise RuntimeError("stream not supported")

        def create(self, **_kwargs):
            calls["create"] += 1
            return SimpleNamespace(
                model="claude-sonnet-4-6",
                content=[
                    SimpleNamespace(
                        type="text",
                        text="AUTO_ROUTING_ACCESS_OK",
                    )
                ],
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=8,
                    output_tokens=2,
                    total_tokens=10,
                ),
            )

    exact_client = SimpleNamespace(messages=Messages(), close=lambda: None)
    monkeypatch.setattr(
        anthropic_adapter,
        "build_anthropic_client",
        lambda *_args, **_kwargs: exact_client,
    )

    auxiliary_client.call_llm_exact_once(
        provider="anthropic",
        model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-ant-memory-only",
        api_mode="anthropic_messages",
        messages=[
            {
                "role": "user",
                "content": "Return exactly AUTO_ROUTING_ACCESS_OK",
            }
        ],
        temperature=0,
        max_tokens=16,
        tools=[],
    )

    assert calls == {"stream": 0, "create": 1}


def test_exact_anthropic_wrapper_preserves_provider_reported_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import anthropic_adapter, auxiliary_client

    provider_model = "claude-provider-drift"

    class Messages:
        def create(self, **_kwargs):
            return SimpleNamespace(
                model=provider_model,
                content=[
                    SimpleNamespace(
                        type="text",
                        text="AUTO_ROUTING_ACCESS_OK",
                    )
                ],
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=8,
                    output_tokens=2,
                    total_tokens=10,
                ),
            )

    monkeypatch.setattr(
        anthropic_adapter,
        "build_anthropic_client",
        lambda *_args, **_kwargs: SimpleNamespace(
            messages=Messages(),
            close=lambda: None,
        ),
    )

    response = auxiliary_client.call_llm_exact_once(
        provider="anthropic",
        model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-ant-memory-only",
        api_mode="anthropic_messages",
        messages=[
            {
                "role": "user",
                "content": "Return exactly AUTO_ROUTING_ACCESS_OK",
            }
        ],
        temperature=0,
        max_tokens=16,
        tools=[],
    )

    assert response.model == provider_model


def test_exact_codex_probe_keeps_output_bound_on_physical_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import auxiliary_client

    physical_calls: list[dict] = []
    message_item = SimpleNamespace(
        type="message",
        role="assistant",
        status="completed",
        content=[
            SimpleNamespace(
                type="output_text",
                text="AUTO_ROUTING_ACCESS_OK",
            )
        ],
    )
    events = [
        SimpleNamespace(type="response.created"),
        SimpleNamespace(type="response.output_item.done", item=message_item),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                status="completed",
                id="response-verification",
                usage=SimpleNamespace(
                    input_tokens=8,
                    output_tokens=2,
                    total_tokens=10,
                ),
            ),
        ),
    ]

    class ResponseStream:
        def __iter__(self):
            return iter(events)

        def close(self) -> None:
            return None

    def create_response(**kwargs):
        physical_calls.append(kwargs)
        return ResponseStream()

    exact_client = SimpleNamespace(
        api_key="sk-memory-only",
        base_url="https://chatgpt.com/backend-api/codex",
        responses=SimpleNamespace(create=create_response),
        close=lambda: None,
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_create_openai_client",
        lambda **_kwargs: exact_client,
    )

    response = auxiliary_client.call_llm_exact_once(
        provider="openai-codex",
        model="gpt-5.4",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="sk-memory-only",
        api_mode="codex_responses",
        messages=[
            {
                "role": "user",
                "content": "Return exactly AUTO_ROUTING_ACCESS_OK",
            }
        ],
        temperature=0,
        max_tokens=16,
        tools=[],
    )

    assert response.choices[0].message.content == "AUTO_ROUTING_ACCESS_OK"
    assert len(physical_calls) == 1
    assert physical_calls[0]["max_output_tokens"] == 16


def test_exact_codex_wrapper_preserves_provider_reported_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import auxiliary_client

    provider_model = "gpt-provider-drift"
    message_item = SimpleNamespace(
        type="message",
        role="assistant",
        status="completed",
        content=[
            SimpleNamespace(
                type="output_text",
                text="AUTO_ROUTING_ACCESS_OK",
            )
        ],
    )
    events = [
        SimpleNamespace(type="response.output_item.done", item=message_item),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                status="completed",
                id="response-verification",
                model=provider_model,
                usage=SimpleNamespace(
                    input_tokens=8,
                    output_tokens=2,
                    total_tokens=10,
                ),
            ),
        ),
    ]

    class ResponseStream:
        def __iter__(self):
            return iter(events)

        def close(self) -> None:
            return None

    exact_client = SimpleNamespace(
        api_key="sk-memory-only",
        base_url="https://chatgpt.com/backend-api/codex",
        responses=SimpleNamespace(create=lambda **_kwargs: ResponseStream()),
        close=lambda: None,
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_create_openai_client",
        lambda **_kwargs: exact_client,
    )

    response = auxiliary_client.call_llm_exact_once(
        provider="openai-codex",
        model="gpt-5.4",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="sk-memory-only",
        api_mode="codex_responses",
        messages=[
            {
                "role": "user",
                "content": "Return exactly AUTO_ROUTING_ACCESS_OK",
            }
        ],
        temperature=0,
        max_tokens=16,
        tools=[],
    )

    assert response.model == provider_model


def test_hermes_018_adapter_missing_discovery_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18

    monkeypatch.setattr(hermes_0_18, "load_picker_context", object)
    monkeypatch.setattr(
        hermes_0_18,
        "build_models_payload",
        lambda _context, **_kwargs: {
            "providers": [
                {
                    "slug": "anthropic",
                    "authenticated": True,
                    "models": ["claude-sonnet-4-6"],
                    "source": "built-in",
                }
            ],
            "model": "claude-sonnet-4-6",
            "provider": "anthropic",
        },
    )
    monkeypatch.setattr(
        hermes_0_18,
        "get_model_capabilities",
        lambda _provider, _model: SimpleNamespace(
            supports_tools=True,
            supports_reasoning=True,
            reasoning_options=("low", "medium", "high"),
        ),
    )

    runtime = InventoryService(hermes_0_18.Hermes018Adapter()).refresh().runtimes[0]

    assert runtime.state == "configured_unverified"
    assert runtime.reasons == ["missing_model_provenance"]
    assert InventoryService(hermes_0_18.Hermes018Adapter()).refresh().eligible() == []
