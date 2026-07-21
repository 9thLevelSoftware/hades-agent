"""Stage 2 profile-aware runtime resolver contracts."""

from __future__ import annotations

import argparse
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent.runtime_routing import (
    RUNTIME_ROUTING_CONTRACT_VERSION,
    AgentRuntimeContext,
    AgentRuntimePlan,
    AgentRuntimeRequest,
    AgentRuntimeSpec,
    ManualRuntimePinRequest,
    RuntimeSessionContinuation,
)
from plugins.auto_routing.auto_routing import cli as auto_routing_cli
from plugins.auto_routing.auto_routing.adapters.base import (
    PERSISTED_RUNTIME_PROJECTION_CONTRACT,
    AdapterInventory,
    PersistedRuntimeProjection,
    ProviderInventoryRow,
    ResolvedRuntime,
)
from plugins.auto_routing.auto_routing.catalog import CatalogRecord, CatalogService
from plugins.auto_routing.auto_routing.classifier import StructuredTaskClassifier
from plugins.auto_routing.auto_routing.models import (
    AccessEconomics,
    CatalogEvidence,
    RuntimeObservation,
)
from plugins.auto_routing.auto_routing.service import AutoRoutingService
from plugins.auto_routing.auto_routing.storage import ActivationReceipt, RoutingStore


def _baseline(model: str = "baseline") -> AgentRuntimeSpec:
    return AgentRuntimeSpec(
        model=model,
        provider="custom:test",
        base_url="https://baseline.invalid/v1",
        api_key="BASELINE_SECRET",
        resolution_state="requested",
        api_mode="chat_completions",
        credential_pool=object(),
        reasoning_config={"enabled": True, "effort": "low"},
        fallback_model=({"model": "global-forbidden"},),
    )


def _request(
    *,
    scope: str = "fresh_session",
    session_id: str = "session-a",
    is_resume: bool = False,
    manual_runtime_pin: bool = False,
    metadata: dict | None = None,
) -> AgentRuntimeRequest:
    return AgentRuntimeRequest(
        contract_version=RUNTIME_ROUTING_CONTRACT_VERSION,
        context=AgentRuntimeContext(
            scope=scope,
            task="RAW TASK MUST REMAIN EPHEMERAL",
            session_id=session_id,
            task_id="task-a",
            operation_id="operation-a" if scope == "delegation" else None,
            task_index=0 if scope == "delegation" else None,
            is_resume=is_resume,
            manual_runtime_pin=manual_runtime_pin,
            manual_pin_source="test" if manual_runtime_pin else None,
            metadata=metadata or {},
        ),
        baseline=_baseline(),
    )


def _config(
    *,
    mode: str = "shadow",
    fresh_sessions: bool = True,
    delegation: bool = True,
):
    return SimpleNamespace(
        activation=SimpleNamespace(mode=mode),
        scopes=SimpleNamespace(
            fresh_sessions=fresh_sessions,
            delegation=delegation,
        ),
    )


@dataclass
class _Binding:
    binding_kind: str = "routed"
    decision_id: str | None = "decision-a"


@dataclass
class _Backend:
    config: object = field(default_factory=_config)
    binding: object | None = None
    receipt: object | None = None
    replay_plan: AgentRuntimePlan | None = None
    decide_plan: AgentRuntimePlan | None = None
    config_error: Exception | None = None
    decide_error: Exception | None = None
    events: list[str] = field(default_factory=list)

    def load_config(self):
        self.events.append("config")
        if self.config_error is not None:
            raise self.config_error
        return self.config

    def read_binding(self, request):
        del request
        self.events.append("binding")
        return self.binding

    def matching_activation_receipt(self, config):
        del config
        self.events.append("receipt")
        return self.receipt

    def replay(self, request, binding):
        del request, binding
        self.events.append("replay")
        assert self.replay_plan is not None
        return self.replay_plan

    def decide(self, request, config, receipt):
        del request, config, receipt
        self.events.append("decide")
        if self.decide_error is not None:
            raise self.decide_error
        assert self.decide_plan is not None
        return self.decide_plan

    def record_manual_pin(self, request):
        del request
        self.events.append("manual")

    def record_session_continuation(self, request):
        del request
        self.events.append("continuation")

    def mark_provider_started(self, **event):
        assert "request_messages" not in event
        assert "conversation_history" not in event
        assert "user_message" not in event
        self.events.append("provider_started")


@dataclass
class _Service:
    backend: _Backend
    closed: bool = False

    def close(self):
        self.closed = True


def _projected_plan(model: str = "selected") -> AgentRuntimePlan:
    return AgentRuntimePlan(
        action="project",
        runtime=AgentRuntimeSpec(
            model=model,
            provider="custom:selected",
            base_url="https://selected.invalid/v1",
            api_key="SELECTED_SECRET",
            resolution_state="resolved",
            api_mode="chat_completions",
            credential_pool=object(),
            reasoning_config={"enabled": True, "effort": "medium"},
            fallback_model=(),
        ),
        decision_id="decision-a",
        bound_route_identity="route-a",
        owns_fallbacks=True,
        reason_code="active_projected",
    )


def _shadow_plan(request: AgentRuntimeRequest) -> AgentRuntimePlan:
    return AgentRuntimePlan(
        action="shadow",
        runtime=request.baseline,
        decision_id="decision-a",
        bound_route_identity="route-a",
        owns_fallbacks=False,
        reason_code="shadow_recorded",
    )


@pytest.fixture
def resolver_factory(tmp_path: Path):
    from plugins.auto_routing.auto_routing.runtime_resolver import (
        AutoRoutingRuntimeResolver,
    )

    home = tmp_path / "profile-a"
    home.mkdir()

    def build(backend: _Backend):
        service = _Service(backend)
        resolver = AutoRoutingRuntimeResolver(
            plugin_context=object(),
            home_resolver=lambda: home,
            service_factory=lambda: service,
            backend_factory=lambda _service: backend,
        )
        return resolver, service

    return build


@pytest.mark.parametrize(
    ("config", "runtime_request", "reason"),
    [
        (_config(mode="off"), _request(), "routing_off"),
        (_config(fresh_sessions=False), _request(), "scope_disabled"),
        (_config(delegation=False), _request(scope="delegation"), "scope_disabled"),
        (_config(), _request(manual_runtime_pin=True), "manual_runtime_pin"),
        (
            _config(),
            _request(
                scope="delegation",
                metadata={"fixed_delegation_provider": True},
            ),
            "fixed_delegation_runtime",
        ),
    ],
)
def test_new_decision_bypass_modes_inherit_without_deciding(
    resolver_factory,
    config,
    runtime_request,
    reason,
):
    backend = _Backend(config=config)
    resolver, _service = resolver_factory(backend)

    plan = resolver.resolve(runtime_request)

    assert plan.action == "inherit"
    assert plan.runtime is runtime_request.baseline
    assert plan.reason_code == reason
    assert "decide" not in backend.events


def test_existing_binding_replays_before_new_scope_and_fixed_runtime_checks(
    resolver_factory,
):
    request = _request(
        scope="delegation",
        is_resume=True,
        metadata={"fixed_delegation_model": True},
    )
    backend = _Backend(
        config=_config(delegation=False),
        binding=_Binding(),
        replay_plan=_projected_plan(),
    )
    resolver, _service = resolver_factory(backend)

    plan = resolver.resolve(request)

    assert plan.action == "project"
    assert backend.events.index("replay") > backend.events.index("binding")
    assert "decide" not in backend.events


@pytest.mark.parametrize("current_override", ["off", "manual"])
def test_current_off_or_manual_pin_supersedes_recorded_binding(
    resolver_factory,
    current_override,
):
    request = _request(
        is_resume=True,
        manual_runtime_pin=current_override == "manual",
    )
    backend = _Backend(
        config=_config(mode="off" if current_override == "off" else "active"),
        binding=_Binding(),
        replay_plan=_projected_plan(),
    )
    resolver, _service = resolver_factory(backend)

    plan = resolver.resolve(request)

    assert plan.action == "inherit"
    assert plan.runtime is request.baseline
    assert "replay" not in backend.events


def test_active_without_matching_receipt_inherits_before_deciding(resolver_factory):
    backend = _Backend(config=_config(mode="active"), receipt=None)
    resolver, _service = resolver_factory(backend)
    request = _request()

    plan = resolver.resolve(request)

    assert plan.action == "inherit"
    assert plan.runtime is request.baseline
    assert plan.reason_code == "activation_receipt_missing"
    assert backend.events[-1] == "receipt"
    assert "decide" not in backend.events


def test_shadow_records_once_but_preserves_exact_baseline(resolver_factory):
    request = _request()
    backend = _Backend(config=_config(mode="shadow"))
    backend.decide_plan = _shadow_plan(request)
    resolver, _service = resolver_factory(backend)

    plan = resolver.resolve(request)

    assert plan.action == "shadow"
    assert plan.runtime is request.baseline
    assert backend.events.count("decide") == 1


def test_active_projects_only_with_matching_receipt(resolver_factory):
    receipt = object()
    backend = _Backend(
        config=_config(mode="active"),
        receipt=receipt,
        decide_plan=_projected_plan(),
    )
    resolver, _service = resolver_factory(backend)

    plan = resolver.resolve(_request())

    assert plan.action == "project"
    assert plan.runtime.model == "selected"
    assert plan.runtime.fallback_model == ()
    assert plan.owns_fallbacks is True


def test_recorded_binding_replays_when_new_authority_is_invalid(resolver_factory):
    backend = _Backend(
        binding=_Binding(),
        config_error=ValueError("new authority is corrupt"),
        replay_plan=_projected_plan(),
    )
    resolver, _service = resolver_factory(backend)

    plan = resolver.resolve(_request(is_resume=True))

    assert plan.action == "project"
    assert backend.events == ["binding", "config", "replay"]


def test_invalid_authority_without_binding_fails_open_with_typed_reason(
    resolver_factory,
):
    backend = _Backend(config_error=ValueError("authority is corrupt"))
    resolver, _service = resolver_factory(backend)

    plan = resolver.resolve(_request())

    assert plan.action == "inherit"
    assert plan.reason_code == "authority_invalid"
    assert plan.runtime.model == "baseline"
    assert backend.events == ["binding", "config"]


def test_resume_without_binding_never_creates_or_classifies_a_new_decision(
    resolver_factory,
):
    request = _request(is_resume=True)
    backend = _Backend(decide_plan=_shadow_plan(request))
    resolver, _service = resolver_factory(backend)

    plan = resolver.resolve(request)

    assert plan.action == "inherit"
    assert plan.reason_code == "resume_binding_missing"
    assert plan.runtime.model == "baseline"
    assert backend.events == ["binding", "config"]


def test_live_operation_owner_returns_typed_defer(resolver_factory):
    from plugins.auto_routing.auto_routing.storage import RuntimeRoutingPending

    backend = _Backend(
        decide_error=RuntimeRoutingPending("owned by another process")
    )
    resolver, _service = resolver_factory(backend)

    plan = resolver.resolve(_request())

    assert plan.action == "defer"
    assert plan.runtime.model == "baseline"
    assert plan.reason_code == "operation_pending"
    assert plan.retry_after_seconds == pytest.approx(0.25)


def test_profile_service_lookup_follows_current_home_and_close_is_exactly_once(
    tmp_path: Path,
):
    from plugins.auto_routing.auto_routing.runtime_resolver import (
        AutoRoutingRuntimeResolver,
    )

    current = [tmp_path / "profile-a"]
    current[0].mkdir()
    services: list[_Service] = []

    def factory():
        service = _Service(_Backend())
        services.append(service)
        return service

    resolver = AutoRoutingRuntimeResolver(
        plugin_context=object(),
        home_resolver=lambda: current[0],
        service_factory=factory,
        backend_factory=lambda service: service.backend,
    )
    first = resolver.service_for_current_profile()
    assert first is resolver.service_for_current_profile()
    current[0] = tmp_path / "profile-b"
    current[0].mkdir()
    second = resolver.service_for_current_profile()

    assert first is not second
    resolver.close()
    resolver.close()
    assert [service.closed for service in services] == [True, True]


def test_profile_cached_resolver_owns_one_real_store_per_thread(
    isolated_home: Path,
    plugin_context,
):
    from plugins.auto_routing.auto_routing.runtime_resolver import (
        AutoRoutingRuntimeResolver,
    )

    backends_by_service: dict[int, object] = {}

    class StoreBackend:
        def __init__(self, service):
            self.service = service
            self.provider_started = 0
            backends_by_service[id(service)] = self

        def read_binding(self, _request):
            self.service.store.count_decisions()
            return None

        @staticmethod
        def load_config():
            return _config(mode="off")

        def mark_provider_started(self, **_event):
            self.service.store.count_decisions()
            self.provider_started += 1

    resolver = AutoRoutingRuntimeResolver(
        plugin_context=plugin_context,
        home_resolver=lambda: isolated_home,
        backend_factory=StoreBackend,
    )
    main_service = resolver.service_for_current_profile()
    assert resolver.service_for_current_profile() is main_service
    assert resolver.resolve(_request(session_id="session-main")).reason_code == (
        "routing_off"
    )

    def use_resolver_from_worker() -> dict[str, object]:
        service = resolver.service_for_current_profile()
        plan = resolver.resolve(_request(session_id="session-worker"))
        resolver.on_pre_api_request(
            session_id="session-worker",
            task_id="task-worker",
            api_request_id="request-worker",
            decision_id="decision-worker",
            runtime_id="runtime-worker",
            model="worker-model",
            provider="worker-provider",
        )
        backend = backends_by_service[id(service)]
        try:
            store_count = service.store.count_decisions()
            store_error = None
        except Exception as error:  # expose the underlying store error in failures
            store_count = None
            store_error = f"{type(error).__name__}: {error}"
        return {
            "same_service_within_worker": (
                resolver.service_for_current_profile() is service
            ),
            "distinct_service_from_main": service is not main_service,
            "resolve_reason": plan.reason_code,
            "provider_started": backend.provider_started,
            "store_count": store_count,
            "store_error": store_error,
        }

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(use_resolver_from_worker).result()
        result["service_count"] = len(backends_by_service)
        assert result == {
            "same_service_within_worker": True,
            "distinct_service_from_main": True,
            "resolve_reason": "routing_off",
            "provider_started": 1,
            "store_count": 0,
            "store_error": None,
            "service_count": 2,
        }
    finally:
        resolver.close()

    for backend in backends_by_service.values():
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            backend.service.store.count_decisions()
    resolver.close()


def test_transient_thread_services_return_to_profile_bounded_cache(tmp_path: Path):
    from plugins.auto_routing.auto_routing.runtime_resolver import (
        AutoRoutingRuntimeResolver,
    )

    home = tmp_path / "profile"
    home.mkdir()
    services: list[_Service] = []

    def factory():
        service = _Service(_Backend(config=_config(mode="off")))
        services.append(service)
        return service

    resolver = AutoRoutingRuntimeResolver(
        plugin_context=object(),
        home_resolver=lambda: home,
        service_factory=factory,
        backend_factory=lambda service: service.backend,
    )
    def use_resolver(index: int) -> None:
        resolver.resolve(_request(session_id=f"transient-{index}"))
        resolver.on_pre_api_request(
            session_id=f"transient-{index}",
            task_id=f"task-{index}",
            api_request_id=f"request-{index}",
        )

    workers = []
    for index in range(20):
        worker = Thread(target=use_resolver, args=(index,))
        worker.start()
        worker.join()
        workers.append(worker)

    # Retain every joined Thread object to mirror TUI session bookkeeping.
    # Each subsequent resolver call must reap dead owners even when GC cannot.
    assert len(workers) == 20
    assert len(resolver._services) == 1
    assert len(services) == 20
    assert sum(not service.closed for service in services) == 1
    resolver.resolve(_request(session_id="main-thread"))

    assert len(resolver._services) == 1
    assert len(services) == 21
    assert sum(not service.closed for service in services) == 1
    resolver.close()
    assert all(service.closed for service in services)


def test_manual_pin_continuation_and_provider_marker_are_content_free(
    resolver_factory,
):
    backend = _Backend()
    resolver, _service = resolver_factory(backend)
    resolver.record_manual_pin(
        ManualRuntimePinRequest(
            session_id="session-a",
            source="cli_model_command",
            runtime=_baseline("manual"),
        )
    )
    resolver.record_session_continuation(
        RuntimeSessionContinuation(
            parent_session_id="session-a",
            child_session_id="session-b",
        )
    )
    resolver.on_pre_api_request(
        session_id="session-a",
        task_id="task-a",
        api_request_id="request-a",
        model="selected",
        provider="custom:selected",
        request_messages=[{"role": "user", "content": "RAW TASK"}],
        conversation_history=[{"role": "user", "content": "RAW TASK"}],
        user_message="RAW TASK",
    )

    assert backend.events == ["manual", "continuation", "provider_started"]


def test_post_turn_evidence_observer_is_fail_open(resolver_factory):
    backend = _Backend()
    resolver, service = resolver_factory(backend)
    service.ingest_turn_outcome = lambda _payload: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        RuntimeError("evidence store unavailable")
    )

    assert resolver.on_post_turn_outcome(session_id="session-a") is None


def test_post_turn_management_advance_runs_only_after_exact_evidence_commit(
    resolver_factory,
):
    backend = _Backend()
    resolver, service = resolver_factory(backend)
    committed = object()
    calls: list[object] = []
    service.ingest_turn_outcome = lambda _payload: committed  # type: ignore[attr-defined]
    service.record_management_outcome = calls.append  # type: ignore[attr-defined]

    resolver.on_post_turn_outcome(session_id="session-a")
    service.ingest_turn_outcome = lambda _payload: None  # type: ignore[attr-defined]
    resolver.on_post_turn_outcome(session_id="session-b")

    assert calls == [committed]


def test_resolver_requires_task_for_both_supported_scopes(resolver_factory):
    resolver, _service = resolver_factory(_Backend())
    assert resolver.requires_initial_task("fresh_session") is True
    assert resolver.requires_initial_task("delegation") is True


def test_adapter_projects_exact_opaque_runtime_without_global_fallback(monkeypatch):
    from plugins.auto_routing.auto_routing.adapters import base
    from plugins.auto_routing.auto_routing.adapters.hermes_0_18 import (
        Hermes018Adapter,
    )
    from plugins.auto_routing.auto_routing.models import RuntimeKey

    key = RuntimeKey(
        provider="custom:selected",
        model="selected",
        auth_identity="auth:test",
        credential_pool_identity="pool:test",
        endpoint_identity="endpoint:test",
        api_mode="chat_completions",
        local_backend="",
        inventory_revision="inventory-a",
    )
    runtime = base.ResolvedRuntime(
        runtime_key=key,
        resolver_name="custom:selected",
        provider="custom:selected",
        api_mode="chat_completions",
        source="test",
        base_url="https://selected.invalid/v1",
        api_key="SELECTED_SECRET",
        credential_pool=object(),
        extra={"acp_command": None, "acp_args": ()},
    )
    adapter = Hermes018Adapter()

    spec = adapter.to_agent_runtime_spec(
        runtime,
        reasoning_effort="medium",
        hermes_config={"reasoning_effort": "low"},
    )

    assert spec.model == "selected"
    assert spec.provider == "custom:selected"
    assert spec.resolution_state == "resolved"
    assert spec.api_key == "SELECTED_SECRET"
    assert spec.credential_pool is not None
    assert spec.reasoning_config == {"enabled": True, "effort": "medium"}
    assert spec.fallback_model == ()
    assert "SELECTED_SECRET" not in repr(spec)
    report = adapter.capability_report()
    assert report["fresh_session"] is True
    assert report["delegation"] is True
    assert report["pre_call_fallback"] is True
    assert report["exact_credential_pool"] is True
    assert report["reasoning_projection"] is True
    assert report["post_call_model_failover"] is False


def test_adapter_inspects_vertex_projection_without_live_resolution(monkeypatch):
    from agent import vertex_adapter
    from hermes_cli import runtime_provider
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18
    from plugins.auto_routing.auto_routing.adapters.hermes_0_18 import (
        Hermes018Adapter,
    )
    from plugins.auto_routing.auto_routing.models import RuntimeKey

    def forbidden_live_path(*_args, **_kwargs):
        raise AssertionError("persisted projection inspection must be local")

    monkeypatch.setattr(
        hermes_0_18,
        "resolve_runtime_provider",
        forbidden_live_path,
    )
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        forbidden_live_path,
    )
    monkeypatch.setattr(
        runtime_provider,
        "resolve_xai_oauth_runtime_credentials",
        forbidden_live_path,
    )
    monkeypatch.setattr(vertex_adapter, "get_vertex_config", forbidden_live_path)
    key = RuntimeKey(
        provider="vertex",
        model="gemini-2.5-pro",
        auth_identity="vertex:configured",
        credential_pool_identity="pool:vertex",
        endpoint_identity="endpoint:vertex",
        api_mode="chat_completions",
        local_backend="",
        inventory_revision="inventory-vertex",
    )
    signing_key = b"v" * 32
    writer = Hermes018Adapter(credential_fingerprint_key=signing_key)
    descriptor = writer._persisted_projection_descriptor(
        runtime_key=key,
        resolver_name="vertex",
    )
    observation = RuntimeObservation(
        key=key,
        state="verified",
        reasons=(),
        economics=AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=1.0,
            metered_output_usd_per_million_tokens=2.0,
            source_id="vertex-test",
            provenance="test",
            observed_at="2026-01-01T00:00:00Z",
        ),
        verification_source="authenticated_live",
        verified_at="2026-01-01T00:00:00Z",
        verification_expires_at="2999-01-01T00:00:00Z",
        provenance=("hermes", "authenticated_live"),
        observed_at="2026-01-01T00:00:00Z",
        capabilities={"auto_routing_projection": descriptor},
    )
    restarted = Hermes018Adapter(credential_fingerprint_key=signing_key)
    assert restarted._projections == {}

    projection = restarted.inspect_persisted_projection(
        observation,
        reasoning_effort="high",
        hermes_config={"agent": {"reasoning_effort": "low"}},
    )

    assert projection.contract == PERSISTED_RUNTIME_PROJECTION_CONTRACT
    assert projection.runtime_key == key
    assert projection.model == key.model
    assert projection.provider == key.provider
    assert projection.api_mode == key.api_mode
    assert projection.credential_pool_identity == "pool:vertex"
    assert projection.reasoning_effort == "high"
    assert projection.fallback_owner == "auto-routing-pre-call"
    assert projection.fallback_count == 0


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing", "metadata is missing"),
        ("mismatch", "descriptor does not match"),
        ("unsupported", "contract is unsupported"),
        ("unresolvable", "resolver is unsupported"),
    ],
)
def test_adapter_rejects_untrusted_persisted_projection_metadata(
    mutation: str,
    expected: str,
):
    from plugins.auto_routing.auto_routing.adapters.hermes_0_18 import (
        Hermes018Adapter,
    )
    from plugins.auto_routing.auto_routing.models import RuntimeKey

    signing_key = b"p" * 32
    writer = Hermes018Adapter(credential_fingerprint_key=signing_key)
    key = RuntimeKey(
        provider="openai-codex",
        model="gpt-5.4",
        auth_identity=(
            "unconfigured:openai-codex"
            if mutation == "unresolvable"
            else "subscription:default"
        ),
        credential_pool_identity="pool:codex",
        endpoint_identity="endpoint:codex",
        api_mode="codex_responses",
        local_backend="",
        inventory_revision="inventory-projection-negative",
    )
    descriptor = dict(
        writer._persisted_projection_descriptor(
            runtime_key=key,
            resolver_name=("moa" if mutation == "unresolvable" else "openai-codex"),
        )
    )
    if mutation == "missing":
        capabilities = {}
    else:
        if mutation == "mismatch":
            descriptor["provider"] = "forged-provider"
        elif mutation == "unsupported":
            descriptor["contract"] = "unsupported-projection-v99"
        capabilities = {"auto_routing_projection": descriptor}
    observation = RuntimeObservation(
        key=key,
        state="verified",
        reasons=(),
        economics=AccessEconomics(
            billing_kind="subscription",
            effective_marginal_cost_usd_per_task=0.0,
            effective_amortized_cost_usd_per_task=0.01,
            subscription_plan="test",
            subscription_state="active",
            subscription_quota_remaining=1.0,
            subscription_quota_unit="request",
            source_id="projection-test",
            provenance="test",
            observed_at="2026-01-01T00:00:00Z",
        ),
        verification_source="authenticated_live",
        verified_at="2026-01-01T00:00:00Z",
        verification_expires_at="2999-01-01T00:00:00Z",
        provenance=("hermes", "authenticated_live"),
        observed_at="2026-01-01T00:00:00Z",
        capabilities=capabilities,
    )

    with pytest.raises(ValueError, match=expected):
        Hermes018Adapter(
            credential_fingerprint_key=signing_key
        ).inspect_persisted_projection(
            observation,
            reasoning_effort="medium",
            hermes_config={},
        )


class _CatalogSource:
    def __init__(self, model: str, now: str, runtime_id: str) -> None:
        self.model = model
        self.now = now
        self.runtime_id = runtime_id

    def load(self):
        common = {
            "source_id": "task5-test-catalog",
            "source_url": "https://catalog.invalid/task5",
            "retrieved_at": self.now,
            "published_at": self.now,
            "model": self.model,
            "model_version": self.model,
            "domain": "coding",
            "task_definition": "default",
            "sample_size": 100,
            "confidence": 0.9,
        }
        return tuple(
            CatalogRecord(
                evidence=CatalogEvidence(
                    **common,
                    metric_name=name,
                    metric_direction=direction,
                    metric_scale=(
                        "unit_interval" if name != "latency" else "seconds"
                    ),
                    value=value,
                    normalization_method=(
                        "identity" if name != "latency" else "divide_by_limit"
                    ),
                ),
                canonical_provider="openai",
                canonical_model=self.model,
                canonical_version=self.model,
                runtime_id=self.runtime_id,
            )
            for name, direction, value in (
                ("quality", "higher_is_better", 0.9),
                ("reliability", "higher_is_better", 0.95),
                ("latency", "lower_is_better", 1.0),
            )
        )


class _RuntimeAdapter:
    def __init__(self, now: str) -> None:
        self.compatible = True
        self.inventory_forbidden = False
        self.failed_resolution_models: set[str] = set()
        self.credential_pool = object()
        self.projected_credential_pool = self.credential_pool
        self.projected_model: str | None = None
        self.projected_provider: str | None = None
        self.projected_api_mode: str | None = None
        self.projected_reasoning_effort: str | None = None
        self.projected_fallback_model: tuple[dict[str, str], ...] = ()
        economics = AccessEconomics(
            billing_kind="subscription",
            effective_marginal_cost_usd_per_task=0.0,
            effective_amortized_cost_usd_per_task=0.01,
            source_id="task5-subscription",
            provenance="configured-subscription",
            observed_at=now,
            evidence_ttl_seconds=3600,
            subscription_plan="task5-plan",
            subscription_state="active",
            subscription_quota_remaining=100.0,
            subscription_quota_unit="request",
        )
        models = ("gpt-5.4", "gpt-5.4-mini")
        self.row = ProviderInventoryRow(
            provider="openai-codex",
            resolver_name="openai-codex",
            models=models,
            authenticated=True,
            live_attempt_status="succeeded",
            model_provenance={model: "authenticated_live" for model in models},
            provenance_details={
                model: {
                    "endpoint_identity": "endpoint:codex",
                    "auth_identity": "subscription:default",
                    "observed_at": now,
                }
                for model in models
            },
            auth_identity="subscription:default",
            credential_pool_identity="pool:codex",
            endpoint_identity="endpoint:codex",
            credential_fingerprint="fingerprint:codex",
            api_mode="codex_responses",
            capabilities={
                model: {
                    "supports_tools": True,
                    "input_modalities": ["text"],
                    "context_window": 128_000,
                    "max_output_tokens": 16_384,
                    "reasoning_options": ["low", "medium", "high"],
                }
                for model in models
            },
            economics={model: economics for model in models},
            observed_at=now,
            source="task5-test",
        )
        self.resolve_calls: list[str] = []

    def inventory(self, refresh: bool = False) -> AdapterInventory:
        del refresh
        if self.inventory_forbidden:
            raise AssertionError("doctor must use persisted inventory")
        return AdapterInventory(provider_rows=(self.row,), local_rows=())

    def retain_models(self, *models: str) -> None:
        wanted = tuple(models)
        self.row = replace(
            self.row,
            models=wanted,
            model_provenance={model: self.row.model_provenance[model] for model in wanted},
            provenance_details={model: self.row.provenance_details[model] for model in wanted},
            capabilities={model: self.row.capabilities[model] for model in wanted},
            economics={model: self.row.economics[model] for model in wanted},
        )

    def resolve(self, runtime_key):
        self.resolve_calls.append(runtime_key.model)
        if runtime_key.model in self.failed_resolution_models:
            raise RuntimeError("injected runtime resolution failure")
        return ResolvedRuntime(
            runtime_key=runtime_key,
            resolver_name="openai-codex",
            provider="openai-codex",
            api_mode="codex_responses",
            source="task5-test",
            base_url="https://selected.invalid/v1",
            api_key="SELECTED_SECRET",
            credential_pool=self.credential_pool,
            extra={},
        )

    def resolve_inherited_baseline(self, inventory_revision):
        del inventory_revision
        return None

    def capability_report(self):
        return {
            "contract": "task5-test-v1",
            "fresh_session": self.compatible,
            "delegation": self.compatible,
            "pre_call_fallback": self.compatible,
            "exact_credential_pool": self.compatible,
            "reasoning_projection": self.compatible,
            "post_call_model_failover": False,
        }

    def to_agent_runtime_spec(
        self,
        resolved_runtime,
        *,
        reasoning_effort,
        hermes_config=None,
    ):
        del hermes_config
        projected_effort = self.projected_reasoning_effort or reasoning_effort
        return AgentRuntimeSpec(
            model=self.projected_model or resolved_runtime.runtime_key.model,
            provider=self.projected_provider or resolved_runtime.provider,
            base_url="https://selected.invalid/v1",
            api_key="SELECTED_SECRET",
            resolution_state="resolved",
            api_mode=self.projected_api_mode or resolved_runtime.api_mode,
            credential_pool=self.projected_credential_pool,
            reasoning_config={
                "enabled": projected_effort != "none",
                "effort": projected_effort,
            },
            fallback_model=self.projected_fallback_model,
        )

    def inspect_persisted_projection(
        self,
        observation,
        *,
        reasoning_effort,
        hermes_config=None,
    ):
        del hermes_config
        runtime_key = observation.key
        projected_effort = self.projected_reasoning_effort or reasoning_effort
        pool_identity = runtime_key.credential_pool_identity
        if self.projected_credential_pool is not self.credential_pool:
            pool_identity = "pool:mismatch"
        return PersistedRuntimeProjection(
            contract=PERSISTED_RUNTIME_PROJECTION_CONTRACT,
            runtime_key=runtime_key,
            resolution_state="resolved",
            model=self.projected_model or runtime_key.model,
            provider=self.projected_provider or runtime_key.provider,
            api_mode=self.projected_api_mode or runtime_key.api_mode,
            credential_pool_identity=pool_identity,
            resolver_name=runtime_key.provider,
            access_kind=runtime_key.auth_identity.partition(":")[0],
            reasoning_effort=projected_effort,
            fallback_owner="auto-routing-pre-call",
            fallback_count=len(self.projected_fallback_model),
        )

    def to_recorded_agent_runtime_spec(
        self,
        resolved_runtime,
        *,
        reasoning_effort,
    ):
        return self.to_agent_runtime_spec(
            resolved_runtime,
            reasoning_effort=reasoning_effort,
            hermes_config=None,
        )


def _degrade_runtime_models(
    adapter: _RuntimeAdapter,
    *models: str,
    degradation: str,
) -> None:
    """Make selected current inventory rows non-verified without removing them."""
    degraded = set(models)
    row = adapter.row
    if degradation == "configured_unverified":
        adapter.row = replace(
            row,
            model_provenance={
                model: (
                    "configured"
                    if model in degraded
                    else provenance
                )
                for model, provenance in row.model_provenance.items()
            },
        )
        return
    economics = dict(row.economics)
    for model in degraded:
        update = (
            {
                "subscription_state": "exhausted",
                "subscription_quota_remaining": 0.0,
            }
            if degradation == "quota"
            else {"cooldown_until": "2999-01-01T00:00:00Z"}
        )
        economics[model] = economics[model].model_copy(update=update)
    adapter.row = replace(row, economics=economics)


def _install_real_hermes_inventory(
    service,
    monkeypatch,
    *,
    signing_key: bytes,
):
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18
    from plugins.auto_routing.auto_routing.adapters.hermes_0_18 import (
        Hermes018Adapter,
    )

    source = service.adapter
    row = source.row
    payload = {
        "model": "gpt-5.4",
        "providers": [
            {
                "slug": row.resolver_name,
                "models": list(row.models),
                "authenticated": True,
                "is_current": True,
                "capabilities": {
                    model: {
                        **dict(row.capabilities[model]),
                        "supports_reasoning": True,
                        "reasoning_options_authenticated": True,
                    }
                    for model in row.models
                },
                "access_economics": {
                    model: row.economics[model].model_dump(mode="json")
                    for model in row.models
                },
                "discovery": {
                    "provider": row.provider,
                    "resolver_name": row.resolver_name,
                    "endpoint_identity": row.endpoint_identity,
                    "auth_identity": row.auth_identity,
                    "credential_pool_identity": row.credential_pool_identity,
                    "api_mode": row.api_mode,
                    "observed_at": row.observed_at,
                    "credential_fingerprint": row.credential_fingerprint,
                    "live_attempt_status": row.live_attempt_status,
                    "model_provenance": dict(row.model_provenance),
                    "provenance_details": {
                        model: dict(row.provenance_details[model])
                        for model in row.models
                    },
                    "source": row.source,
                },
            }
        ],
    }
    monkeypatch.setattr(hermes_0_18, "load_picker_context", lambda: object())
    monkeypatch.setattr(
        hermes_0_18,
        "build_models_payload",
        lambda *_args, **_kwargs: payload,
    )
    adapter = Hermes018Adapter(credential_fingerprint_key=signing_key)
    service.adapter = adapter
    current = service._new_inventory_service().refresh(
        refresh=False,
        persist=True,
    )
    persisted = service.store.read_inventory_snapshot(current.revision)
    assert persisted is not None
    return adapter, persisted


def _inherited_only_proposal(service):
    proposal = service._activation_proposal(
        service._configured_authority(),
        "active",
    )
    coding = proposal.profiles["coding"].model_copy(update={"fallbacks": ()})
    return proposal.model_copy(
        update={
            "safe_default": "inherit",
            "profiles": {**proposal.profiles, "coding": coding},
        }
    )


def _write_rebased_signed_inventory(
    service,
    writer,
    persisted,
    *,
    revision: str,
    inherited_descriptor_mutation: str | None = None,
):
    observations = []
    for observation in persisted.observations:
        runtime_key = observation.key.model_copy(
            update={"inventory_revision": revision}
        )
        capabilities = dict(observation.capabilities)
        source_descriptor = dict(capabilities["auto_routing_projection"])
        descriptor = writer._persisted_projection_descriptor(
            runtime_key=runtime_key,
            resolver_name=source_descriptor["resolver_name"],
        )
        capabilities["auto_routing_projection"] = descriptor
        if observation.key.model == "gpt-5.4-mini":
            if inherited_descriptor_mutation == "missing":
                capabilities.pop("auto_routing_projection")
            elif inherited_descriptor_mutation == "forged":
                forged = dict(descriptor)
                forged["provider"] = "forged-provider"
                capabilities["auto_routing_projection"] = forged
        observations.append(
            observation.model_copy(
                update={
                    "key": runtime_key,
                    "capabilities": capabilities,
                }
            )
        )
    service.store.write_inventory_snapshot(
        revision,
        observations,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    snapshot = service.store.read_inventory_snapshot(revision)
    assert snapshot is not None
    return snapshot


def _forbid_real_hermes_doctor_live_paths(
    service,
    adapter,
    monkeypatch,
) -> None:
    from agent import vertex_adapter
    from hermes_cli import runtime_provider
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18

    def forbidden_live_path(*_args, **_kwargs):
        raise AssertionError(
            "activation doctor must not resolve providers, credentials, or picker state"
        )

    monkeypatch.setattr(adapter, "resolve", forbidden_live_path)
    monkeypatch.setattr(
        adapter,
        "resolve_inherited_baseline",
        forbidden_live_path,
    )
    monkeypatch.setattr(adapter, "inventory", forbidden_live_path)
    monkeypatch.setattr(adapter, "verify_access", forbidden_live_path)
    monkeypatch.setattr(adapter, "to_agent_runtime_spec", forbidden_live_path)
    monkeypatch.setattr(
        adapter,
        "prepare_persisted_inventory",
        forbidden_live_path,
    )
    monkeypatch.setattr(service, "_new_inventory_service", forbidden_live_path)
    monkeypatch.setattr(
        hermes_0_18,
        "resolve_runtime_provider",
        forbidden_live_path,
    )
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        forbidden_live_path,
    )
    monkeypatch.setattr(
        runtime_provider,
        "resolve_xai_oauth_runtime_credentials",
        forbidden_live_path,
    )
    monkeypatch.setattr(vertex_adapter, "get_vertex_config", forbidden_live_path)
    monkeypatch.setattr(hermes_0_18, "load_picker_context", forbidden_live_path)
    monkeypatch.setattr(hermes_0_18, "build_models_payload", forbidden_live_path)


def _runtime_authority(mode: str = "shadow") -> dict:
    fixture = Path(__file__).with_name("fixtures") / "approved_proposal.json"
    authority = json.loads(fixture.read_text(encoding="utf-8"))
    authority["activation"]["mode"] = mode
    authority["safe_default"] = authority["profiles"]["coding"]["primary"]
    fallback = json.loads(
        json.dumps(authority["profiles"]["coding"]["primary"])
    )
    fallback["runtime"]["model"] = "gpt-5.4-mini"
    fallback["revision_status"] = "fallback"
    authority["profiles"]["coding"]["fallbacks"] = [fallback]
    authority["rules"] = [
        {
            "rule_id": "complete-coding",
            "priority": 1000,
            "profile_id": "coding",
            "effect": "pin_profile",
            "when": {"scopes": ["fresh_session"]},
            "assessment_overrides": {
                "complexity": 0.6,
                "domains": ["coding"],
                "required_capabilities": ["tools"],
                "required_modalities": ["text"],
                "expected_context_tokens": 2048,
                "expected_output_tokens": 512,
                "quality_sensitivity": 0.9,
                "reliability_sensitivity": 0.8,
                "latency_sensitivity": 0.3,
                "cost_sensitivity": 0.2,
                "risk_class": "moderate",
                "confidence": 0.95,
            },
        }
    ]
    return authority


def _real_runtime_resolver(
    *,
    isolated_home: Path,
    service,
    mode: str,
):
    from plugins.auto_routing.auto_routing.config import (
        authority_document,
        authority_revision,
        parse_config,
    )
    from plugins.auto_routing.auto_routing.runtime_resolver import (
        AutoRoutingRuntimeResolver,
    )

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    service.adapter = _RuntimeAdapter(now)
    service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": _runtime_authority(mode)}}}
        ),
        encoding="utf-8",
    )
    config = parse_config(
        {"plugins": {"entries": {"auto-routing": _runtime_authority(mode)}}}
    )
    authority_id = authority_revision(config)
    service.store.publish_authority_and_baseline(
        authority_id=authority_id,
        document=authority_document(config),
        baseline=service._baseline_revision(config, authority_id=authority_id),
    )
    inventory = service._new_inventory_service().refresh(
        refresh=False,
        persist=True,
    )
    runtime_ids = {
        runtime.key.model: runtime.key.stable_id()
        for runtime in inventory.runtimes
    }
    CatalogService(store=service.store).refresh(
        [
            _CatalogSource("gpt-5.4", now, runtime_ids["gpt-5.4"]),
            _CatalogSource(
                "gpt-5.4-mini",
                now,
                runtime_ids["gpt-5.4-mini"],
            ),
        ]
    )
    resolver = AutoRoutingRuntimeResolver(
        plugin_context=service.plugin_context,
        home_resolver=lambda: isolated_home,
        service_factory=lambda: service,
    )
    if service.plugin_context._manager.agent_runtime_resolver is None:
        service.plugin_context.register_agent_runtime_resolver(resolver)
    return resolver


@pytest.fixture
def runtime_service(isolated_home: Path, plugin_context):
    service = AutoRoutingService(
        plugin_context=plugin_context,
        hermes_home=isolated_home,
        store=RoutingStore.open(home=isolated_home),
    )
    try:
        yield service
    finally:
        service.store.close()


def _seed_activation_receipt(
    service,
    *,
    receipt_id: str,
    inventory_contract_sha: str,
):
    from plugins.auto_routing.auto_routing.config import (
        authority_revision,
        config_document,
        parse_config,
    )
    from plugins.auto_routing.auto_routing.runtime_resolver import (
        _adapter_capability_sha,
    )
    from utils import fast_safe_load

    config = parse_config(fast_safe_load(service.config_path.read_bytes()))
    config_sha = __import__("hashlib").sha256(
        json.dumps(
            config_document(config),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return service.store.write_activation_receipt(
        ActivationReceipt(
            receipt_id=receipt_id,
            authority_id=authority_revision(config),
            config_sha=config_sha,
            inventory_contract_sha=inventory_contract_sha,
            inventory_revision=f"inventory-{receipt_id}",
            adapter_capability_sha=_adapter_capability_sha(service.adapter),
            created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
    )


def test_real_shadow_decision_is_committed_once_and_resume_never_reclassifies(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    first_request = _request()

    first = resolver.resolve(first_request)
    replay = resolver.resolve(_request(is_resume=True))

    assert first.action == "shadow", (first.reason_code, first.event)
    assert first.runtime is first_request.baseline
    assert first.reason_code == "shadow_recorded"
    assert replay.action == "shadow"
    assert replay.decision_id == first.decision_id
    assert service.store.count_decisions() == 1
    decision = service.store.read_session_decision("session-a")
    assert decision is not None
    assert decision.selected_runtime.model == "gpt-5.4"
    assert decision.applied_rule_ids == ("complete-coding",)


def test_incompatible_adapter_fails_open_before_decision(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    service.adapter.compatible = False

    plan = resolver.resolve(_request())

    assert plan.action == "inherit"
    assert plan.reason_code == "adapter_incompatible"
    assert plan.runtime.model == "baseline"
    assert service.store.count_decisions() == 0


def test_real_active_decision_projects_exact_runtime_and_replays_recorded_chain(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    from utils import fast_safe_load

    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="active",
    )
    _seed_activation_receipt(
        service,
        receipt_id="receipt-task5",
        inventory_contract_sha="a" * 64,
    )

    first = resolver.resolve(_request())
    assert first.action == "project", (first.reason_code, first.event)
    assert first.runtime.model == "gpt-5.4"
    assert first.runtime.fallback_model == ()
    assert service.store.count_decisions() == 1

    # New-decision scope edits cannot invalidate an existing immutable binding.
    root = fast_safe_load(service.config_path.read_bytes())
    root["plugins"]["entries"]["auto-routing"]["scopes"]["fresh_sessions"] = False
    service.config_path.write_text(json.dumps(root), encoding="utf-8")
    service.adapter.compatible = False
    replay = resolver.resolve(_request(is_resume=True))
    assert replay.action == "project"
    assert replay.decision_id == first.decision_id
    assert service.store.count_decisions() == 1
    service.adapter.compatible = True

    # Recorded projection and reasoning are independent of a newer corrupt
    # authority/config document.
    service.config_path.write_text("{", encoding="utf-8")
    corrupt_current_replay = resolver.resolve(_request(is_resume=True))
    assert corrupt_current_replay.action == "project"
    assert corrupt_current_replay.runtime.reasoning_config == {
        "enabled": True,
        "effort": "medium",
    }
    assert corrupt_current_replay.decision_id == first.decision_id
    assert service.store.count_decisions() == 1

    resolver.record_session_continuation(
        RuntimeSessionContinuation(
            parent_session_id="session-a",
            child_session_id="session-b",
        )
    )
    child_replay = resolver.resolve(
        _request(session_id="session-b", is_resume=True)
    )
    assert child_replay.action == "project"
    assert child_replay.decision_id == first.decision_id
    assert child_replay.runtime.model == "gpt-5.4"
    assert service.store.count_decisions() == 1


def test_real_resume_repairs_missing_compression_alias_with_older_child_rows(
    isolated_home: Path,
    runtime_service,
    plugin_context,
    monkeypatch,
):
    from agent.runtime_routing import prepare_agent_runtime_for_construction
    from hermes_state import SessionDB

    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="active",
    )
    monkeypatch.setattr(
        "hermes_cli.plugins._plugin_manager",
        plugin_context._manager,
    )
    _seed_activation_receipt(
        service,
        receipt_id="receipt-compression-repair",
        inventory_contract_sha="a" * 64,
    )
    first = resolver.resolve(_request(session_id="session-a"))
    assert first.action == "project"
    parent_binding = service.store.read_session_binding("session-a")
    assert parent_binding is not None

    session_db = SessionDB(db_path=isolated_home / "session-state.db")
    try:
        session_db.create_session("session-a", "cli")
        session_db.create_session(
            "delegate",
            "cli",
            model_config={"_delegate_from": "session-a"},
            parent_session_id="session-a",
        )
        session_db.create_session(
            "tool-child",
            "tool",
            parent_session_id="session-a",
        )
        session_db.create_session(
            "branch",
            "cli",
            model_config={"_branched_from": "session-a"},
            parent_session_id="session-a",
        )
        session_db.end_session("session-a", "compression")
        session_db.create_session(
            "session-child",
            "cli",
            parent_session_id="session-a",
        )

        assert service.store.read_session_binding("session-child") is None
        resume = _request(session_id="session-child", is_resume=True)

        prepared = prepare_agent_runtime_for_construction(
            resume,
            session_store=session_db,
        )
    finally:
        session_db.close()

    child_binding = service.store.read_session_binding("session-child")
    assert child_binding is not None
    assert child_binding.binding_kind == parent_binding.binding_kind
    assert child_binding.projection_mode == parent_binding.projection_mode
    assert child_binding.decision_id == parent_binding.decision_id
    assert child_binding.runtime_id == parent_binding.runtime_id
    assert child_binding.current_epoch == parent_binding.current_epoch
    assert child_binding.continuation_root == "session-a"
    assert child_binding.parent_session_id == "session-a"
    assert child_binding.continuation_reason == "compression"
    assert prepared.plan.action == "project"
    assert prepared.plan.decision_id == first.decision_id
    assert prepared.plan.bound_route_identity == first.decision_id
    assert prepared.plan.runtime.model == first.runtime.model
    assert prepared.plan.runtime.provider == first.runtime.provider
    assert prepared.plan.runtime.api_mode == first.runtime.api_mode
    assert prepared.plan.runtime.reasoning_config == first.runtime.reasoning_config
    assert prepared.plan.runtime.fallback_model == first.runtime.fallback_model
    assert prepared.plan.event["recorded_replay"] is True
    assert service.store.count_decisions() == 1


def test_real_hermes_replay_survives_current_capability_drift(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
):
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18

    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="active",
    )
    _seed_activation_receipt(
        service,
        receipt_id="receipt-capability-drift",
        inventory_contract_sha="7" * 64,
    )
    first = resolver.resolve(_request())
    assert first.action == "project"
    adapter, _persisted = _install_real_hermes_inventory(
        service,
        monkeypatch,
        signing_key=b"z" * 32,
    )
    credential_pool = object()

    def resolve_recorded_runtime(*, requested, target_model):
        del requested
        return {
            "provider": "openai-codex",
            "model": target_model,
            "api_mode": "codex_responses",
            "source": "capability-drift-test",
            "base_url": "https://selected.invalid/v1",
            "api_key": "SELECTED_SECRET",
            "credential_pool": credential_pool,
            "auth_identity": "subscription:default",
            "credential_pool_identity": "pool:codex",
            "endpoint_identity": "endpoint:codex",
        }

    monkeypatch.setattr(
        hermes_0_18,
        "resolve_runtime_provider",
        resolve_recorded_runtime,
    )
    drifted_report = adapter.capability_report()
    drifted_report["reasoning_projection"] = False
    monkeypatch.setattr(adapter, "capability_report", lambda: drifted_report)
    replay = resolver.resolve(_request(is_resume=True))
    new_decision = resolver.resolve(_request(session_id="new-session"))

    assert replay.action == "project", (replay.reason_code, replay.event)
    assert replay.decision_id == first.decision_id
    assert replay.runtime.reasoning_config == {
        "enabled": True,
        "effort": "medium",
    }
    assert replay.runtime.credential_pool is credential_pool
    assert new_decision.action == "inherit"
    assert new_decision.reason_code == "adapter_incompatible"
    assert service.store.count_decisions() == 1


def test_real_active_primary_resolution_failure_selects_profile_fallback_pre_call(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="active",
    )
    _seed_activation_receipt(
        service,
        receipt_id="receipt-pre-call-fallback",
        inventory_contract_sha="d" * 64,
    )
    service.adapter.failed_resolution_models.add("gpt-5.4")

    plan = resolver.resolve(_request())

    assert plan.action == "project", (plan.reason_code, plan.event)
    assert plan.runtime.model == "gpt-5.4-mini"
    assert service.adapter.resolve_calls == ["gpt-5.4", "gpt-5.4-mini"]
    decision = service.store.read_session_decision("session-a")
    assert decision is not None
    assert decision.selected_runtime.model == "gpt-5.4-mini"
    assert decision.selection_reason == "pre_call_fallback"
    assert decision.degradation_reason == "pre_call_fallback"
    epochs = service.store.read_route_epochs("session-a")
    assert [epoch.epoch_number for epoch in epochs] == [0]
    assert epochs[0].runtime_id == decision.selected_runtime.stable_id()


def test_real_active_exhausted_auto_chain_commits_inherit_before_host_resolution(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="active",
    )
    _seed_activation_receipt(
        service,
        receipt_id="receipt-pre-call-inherit",
        inventory_contract_sha="e" * 64,
    )
    service.adapter.failed_resolution_models.update({"gpt-5.4", "gpt-5.4-mini"})
    request = _request()

    plan = resolver.resolve(request)

    assert plan.action == "inherit"
    assert plan.runtime is request.baseline
    assert plan.reason_code == "baseline_inherit"
    assert service.adapter.resolve_calls == [
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4",
    ]
    decision = service.store.read_session_decision("session-a")
    assert decision is not None
    assert decision.projection_mode == "inherit"
    assert decision.degradation_reason == "baseline_inherit"
    assert decision.assessment is None
    assert decision.selected_profile_id is None
    assert decision.eligible_candidates == ()
    assert decision.rejected_candidates == ()
    assert decision.final_scores == ()
    assert decision.projected_fallback_chain == ()
    assert decision.selected_runtime == decision.safe_default_runtime
    assert decision.classifier_runtime_id is None
    assert decision.classifier_input_tokens == 0
    assert decision.classifier_output_tokens == 0
    assert decision.classifier_cost_usd is None
    assert service.store.connection.execute(
        "SELECT COUNT(*) FROM decision_candidates WHERE decision_id = ?",
        (decision.decision_id,),
    ).fetchone()[0] == 0
    assert service.store.read_route_epochs("session-a") == ()


def test_guarded_activation_preview_apply_writes_matching_receipt(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )

    preview = service.preview_activation("active")

    assert preview["applied"] is False
    assert preview["doctor"]["healthy"] is True, preview["doctor"]
    assert preview["proposed_config_sha256"] == preview["fingerprints"][
        "config_sha"
    ]
    applied = service.apply_activation(
        "active",
        expected_config_sha256=preview["expected_config_sha256"],
    )

    assert applied["applied"] is True
    assert service.status()["activation_mode"] == "active"
    active_doctor = service.doctor()
    assert active_doctor["healthy"] is True, active_doctor
    assert next(
        item
        for item in active_doctor["checks"]
        if item["name"] == "post_call_model_failover"
    )["status"] == "warning"
    receipt = service.store.read_matching_activation_receipt(
        authority_id=applied["authority_id"],
        config_sha=applied["proposed_config_sha256"],
        adapter_capability_sha=applied["fingerprints"]["adapter_capability_sha"],
    )
    assert receipt is not None
    assert receipt.receipt_id == applied["activation_receipt_id"]
    assert receipt.inventory_contract_sha == applied["fingerprints"][
        "inventory_contract_sha"
    ]


def test_reactivation_records_new_inventory_receipt_and_old_session_replays(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    first_preview = service.preview_activation("active")
    first_apply = service.apply_activation(
        "active",
        expected_config_sha256=first_preview["expected_config_sha256"],
    )
    first_receipt_id = first_apply["activation_receipt_id"]
    old_plan = resolver.resolve(_request(session_id="old-session"))
    assert old_plan.action == "project"
    old_decision = service.store.read_decision(old_plan.decision_id)
    assert old_decision is not None
    assert old_decision.activation_receipt_id == first_receipt_id

    shadow_preview = service.preview_activation("shadow")
    service.apply_activation(
        "shadow",
        expected_config_sha256=shadow_preview["expected_config_sha256"],
    )
    previous = service.store.read_inventory_snapshot(
        first_apply["fingerprints"]["inventory_revision"]
    )
    assert previous is not None
    next_revision = "inventory-reactivation-new-approval"
    next_observations = tuple(
        observation.model_copy(
            update={
                "key": observation.key.model_copy(
                    update={"inventory_revision": next_revision}
                ),
                "capabilities": {
                    **dict(observation.capabilities),
                    "reactivation_audit_generation": 2,
                },
            }
        )
        for observation in previous.observations
    )
    service.store.write_inventory_snapshot(
        next_revision,
        next_observations,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )

    second_preview = service.preview_activation("active")
    assert second_preview["fingerprints"]["inventory_revision"] == next_revision
    assert (
        second_preview["fingerprints"]["inventory_contract_sha"]
        != first_apply["fingerprints"]["inventory_contract_sha"]
    )
    second_apply = service.apply_activation(
        "active",
        expected_config_sha256=second_preview["expected_config_sha256"],
    )
    second_receipt_id = second_apply["activation_receipt_id"]

    assert second_receipt_id != first_receipt_id
    second_receipt = service.store.read_activation_receipt(second_receipt_id)
    assert second_receipt is not None
    assert second_receipt.inventory_revision == next_revision
    assert second_receipt.inventory_contract_sha == second_apply["fingerprints"][
        "inventory_contract_sha"
    ]
    assert service.store.read_activation_receipt(first_receipt_id) is not None
    assert service.store.connection.execute(
        "SELECT COUNT(*) FROM activation_receipts"
    ).fetchone()[0] == 2

    replay = resolver.resolve(_request(session_id="old-session", is_resume=True))

    assert replay.action == "project", (replay.reason_code, replay.event)
    assert replay.decision_id == old_plan.decision_id


def test_activate_cli_is_preview_by_default_and_requires_exact_apply_hash(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    parser = argparse.ArgumentParser()
    auto_routing_cli.build_parser(parser)

    preview = auto_routing_cli.execute(
        parser.parse_args(["activate", "--mode", "active", "--json"]),
        service=service,
    )
    missing_hash = auto_routing_cli.execute(
        parser.parse_args(["activate", "--mode", "active", "--apply"]),
        service=service,
    )

    assert preview.exit_code == 0, preview.payload
    assert preview.payload["applied"] is False
    assert preview.payload["write_class"] == "guarded_control_plane"
    assert missing_hash.exit_code == 2
    assert "must be supplied together" in missing_hash.payload["error"]


def test_explain_cli_emits_redacted_concise_and_detailed_json(
    isolated_home: Path,
    runtime_service,
    capsys,
):
    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    plan = resolver.resolve(_request())
    assert plan.decision_id is not None
    parser = argparse.ArgumentParser()
    auto_routing_cli.build_parser(parser)
    before_changes = service.store.connection.total_changes
    before_config = service.config_path.read_bytes()

    concise_args = parser.parse_args(
        ["explain", "--session-id", "session-a", "--json"]
    )
    assert auto_routing_cli.auto_routing_command(
        concise_args,
        service=service,
    ) == 0
    concise_text = capsys.readouterr().out
    concise = json.loads(concise_text)

    assert concise["command"] == "explain"
    assert concise["write_class"] == "read_only"
    assert concise["schema"] == "auto-routing-decision-explanation-v1"
    assert concise["detail"] == "concise"
    assert concise["decision"]["decision_id"] == plan.decision_id
    assert concise["decision"]["selected_runtime"]["model"] == "gpt-5.4"
    assert concise["evidence"] == {
        "event_ids": [],
        "turn_outcomes": 0,
        "explicit_feedback": 0,
        "quality_unknown": 0,
    }
    assert "candidates" not in concise
    assert "RAW TASK MUST REMAIN EPHEMERAL" not in concise_text
    assert "BASELINE_SECRET" not in concise_text

    detailed_args = parser.parse_args(
        [
            "explain",
            "--decision-id",
            plan.decision_id,
            "--detailed",
            "--json",
        ]
    )
    assert auto_routing_cli.auto_routing_command(
        detailed_args,
        service=service,
    ) == 0
    detailed_text = capsys.readouterr().out
    detailed = json.loads(detailed_text)
    stored = service.store.read_decision(plan.decision_id)
    assert stored is not None

    assert detailed["detail"] == "detailed"
    assert set(detailed["decision"]) == set(type(stored).model_fields)
    assert detailed["candidates"]
    assert detailed["evidence_events"] == []
    assert all("candidate_id" in item for item in detailed["candidates"])
    assert "RAW TASK MUST REMAIN EPHEMERAL" not in detailed_text
    assert "BASELINE_SECRET" not in detailed_text
    assert service.store.connection.total_changes == before_changes
    assert service.config_path.read_bytes() == before_config


def test_explain_supports_delegation_operation_lookup(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    plan = resolver.resolve(
        _request(scope="delegation", session_id="delegation-session")
    )
    assert plan.decision_id is not None
    parser = argparse.ArgumentParser()
    auto_routing_cli.build_parser(parser)

    result = auto_routing_cli.execute(
        parser.parse_args(
            [
                "explain",
                "--operation-id",
                "operation-a",
                "--task-index",
                "0",
                "--json",
            ]
        ),
        service=service,
    )

    assert result.exit_code == 0, result.payload
    assert result.payload["decision"]["decision_id"] == plan.decision_id
    assert result.payload["decision"]["scope"] == "delegation"
    assert result.payload["lookup"] == {
        "kind": "operation",
        "operation_id": "operation-a",
        "task_index": 0,
    }


def test_explain_service_rejects_ambiguous_or_unsafe_lookups(
    runtime_service,
):
    service = runtime_service

    with pytest.raises(ValueError, match="exactly one"):
        service.explain(decision_id="decision-a", session_id="session-a")
    with pytest.raises(ValueError, match="task index"):
        service.explain(operation_id="operation-a")
    with pytest.raises(ValueError, match="bounded content-free"):
        service.explain(session_id="' OR 1=1 --")


def test_activation_doctor_uses_only_persisted_inventory(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    service.adapter.inventory_forbidden = True

    def forbidden_live_path(*_args, **_kwargs):
        raise AssertionError("doctor must not execute a live discovery path")

    monkeypatch.setattr(service, "_new_inventory_service", forbidden_live_path)
    monkeypatch.setattr(
        service.adapter,
        "verify_access",
        forbidden_live_path,
        raising=False,
    )
    monkeypatch.setattr(
        StructuredTaskClassifier,
        "classify",
        forbidden_live_path,
    )

    preview = service.preview_activation("active")

    assert preview["doctor"]["healthy"] is True, preview["doctor"]


def test_activation_doctor_never_resolves_provider_credentials_or_vertex(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
):
    from agent import vertex_adapter
    from hermes_cli import runtime_provider
    from plugins.auto_routing.auto_routing.adapters import hermes_0_18
    from plugins.auto_routing.auto_routing.adapters.hermes_0_18 import (
        Hermes018Adapter,
    )

    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    _writer, persisted = _install_real_hermes_inventory(
        service,
        monkeypatch,
        signing_key=b"x" * 32,
    )
    assert all(
        "auto_routing_projection" in observation.capabilities
        for observation in persisted.observations
    )
    adapter = Hermes018Adapter(credential_fingerprint_key=b"x" * 32)
    service.adapter = adapter
    assert adapter._projections == {}

    def forbidden_live_path(*_args, **_kwargs):
        raise AssertionError(
            "activation doctor must not resolve providers or credentials"
        )

    monkeypatch.setattr(adapter, "resolve", forbidden_live_path)
    monkeypatch.setattr(adapter, "inventory", forbidden_live_path)
    monkeypatch.setattr(adapter, "verify_access", forbidden_live_path)
    monkeypatch.setattr(adapter, "to_agent_runtime_spec", forbidden_live_path)
    monkeypatch.setattr(
        adapter,
        "prepare_persisted_inventory",
        forbidden_live_path,
    )
    monkeypatch.setattr(service, "_new_inventory_service", forbidden_live_path)
    monkeypatch.setattr(
        hermes_0_18,
        "resolve_runtime_provider",
        forbidden_live_path,
    )
    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        forbidden_live_path,
    )
    monkeypatch.setattr(
        runtime_provider,
        "resolve_xai_oauth_runtime_credentials",
        forbidden_live_path,
    )
    monkeypatch.setattr(vertex_adapter, "get_vertex_config", forbidden_live_path)
    monkeypatch.setattr(hermes_0_18, "load_picker_context", forbidden_live_path)
    monkeypatch.setattr(hermes_0_18, "build_models_payload", forbidden_live_path)
    before_config = service.config_path.read_bytes()
    before_changes = service.store.connection.total_changes
    before_projections = dict(adapter._projections)

    preview = service.preview_activation("active")

    assert preview["doctor"]["healthy"] is True, preview["doctor"]
    assert service.config_path.read_bytes() == before_config
    assert service.store.connection.total_changes == before_changes
    assert adapter._projections == before_projections


@pytest.mark.parametrize("mutation", ["missing", "mismatch", "unsupported"])
def test_activation_doctor_rejects_untrusted_persisted_projection_descriptor(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
    mutation: str,
):
    from plugins.auto_routing.auto_routing.adapters.hermes_0_18 import (
        Hermes018Adapter,
    )

    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    _writer, persisted = _install_real_hermes_inventory(
        service,
        monkeypatch,
        signing_key=b"d" * 32,
    )
    drift_id = f"inventory-projection-{mutation}"
    observations = []
    for observation in persisted.observations:
        capabilities = dict(observation.capabilities)
        if observation.key.model == "gpt-5.4":
            descriptor = dict(capabilities["auto_routing_projection"])
            if mutation == "missing":
                capabilities.pop("auto_routing_projection")
            elif mutation == "mismatch":
                descriptor["provider"] = "forged-provider"
                capabilities["auto_routing_projection"] = descriptor
            else:
                descriptor["contract"] = "unsupported-projection-v99"
                capabilities["auto_routing_projection"] = descriptor
        observations.append(
            observation.model_copy(
                update={
                    "key": observation.key.model_copy(
                        update={"inventory_revision": drift_id}
                    ),
                    "capabilities": capabilities,
                }
            )
        )
    service.store.write_inventory_snapshot(
        drift_id,
        observations,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    service.adapter = Hermes018Adapter(credential_fingerprint_key=b"d" * 32)
    proposal = service._activation_proposal(
        service._configured_authority(),
        "active",
    )

    doctor = service.doctor(
        _proposal=proposal,
        _activation_transition=True,
    )

    exact = next(
        item for item in doctor["checks"] if item["name"] == "exact_targets"
    )
    assert exact["status"] == "error"
    assert "persisted_projection_inspection_failed" in exact["detail"][
        "coding:primary"
    ]["reasons"]


@pytest.mark.parametrize("model_field", ["model", "default"])
def test_real_hermes_doctor_accepts_inherited_model_with_provider_auto(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
    model_field: str,
):
    from plugins.auto_routing.auto_routing.adapters.hermes_0_18 import (
        Hermes018Adapter,
    )

    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    proposal = service._configured_authority().model_copy(
        update={"safe_default": "inherit"}
    )
    inventory, _checksum = service._activation_inventory_fingerprint(proposal)
    service.adapter = Hermes018Adapter(credential_fingerprint_key=b"i" * 32)
    monkeypatch.setattr(
        service,
        "_runtime_root_config",
        lambda: {
            "model": {
                model_field: "gpt-5.4",
                "provider": "auto",
            }
        },
    )

    status, detail, runtime_id = service._doctor_inherited_safe_default(
        proposal,
        inventory,
    )

    assert status == "ok", detail
    assert runtime_id is not None
    assert detail["runtime_id"] == runtime_id
    assert detail["policy_compliant"] is True


def test_activation_doctor_validates_distinct_inherited_signed_projection_without_live_io(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
):
    from plugins.auto_routing.auto_routing.adapters.hermes_0_18 import (
        Hermes018Adapter,
    )

    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    _writer, persisted = _install_real_hermes_inventory(
        service,
        monkeypatch,
        signing_key=b"h" * 32,
    )
    adapter = Hermes018Adapter(credential_fingerprint_key=b"h" * 32)
    service.adapter = adapter
    proposal = _inherited_only_proposal(service)
    root_config = {
        "model": {"model": "gpt-5.4-mini", "provider": "auto"},
        "agent": {"reasoning_effort": "high"},
    }
    monkeypatch.setattr(service, "_runtime_root_config", lambda: root_config)
    inspections: list[tuple[str, str]] = []
    inspect_projection = adapter.inspect_persisted_projection

    def record_inspection(observation, *, reasoning_effort, hermes_config=None):
        inspections.append((observation.key.model, reasoning_effort))
        return inspect_projection(
            observation,
            reasoning_effort=reasoning_effort,
            hermes_config=hermes_config,
        )

    monkeypatch.setattr(
        adapter,
        "inspect_persisted_projection",
        record_inspection,
    )
    _forbid_real_hermes_doctor_live_paths(service, adapter, monkeypatch)
    before_config = service.config_path.read_bytes()
    before_changes = service.store.connection.total_changes
    before_projections = dict(adapter._projections)

    doctor = service.doctor(
        _proposal=proposal,
        _activation_transition=True,
    )

    exact = next(
        item for item in doctor["checks"] if item["name"] == "exact_targets"
    )
    assert exact["status"] == "ok", exact
    assert exact["detail"]["safe-default"] == {
        "runtime_id": next(
            observation.key.stable_id()
            for observation in persisted.observations
            if observation.key.model == "gpt-5.4-mini"
        ),
        "status": "ok",
        "reasoning_effort": "high",
        "fallback_count": 0,
    }
    assert ("gpt-5.4-mini", "high") in inspections
    assert service.config_path.read_bytes() == before_config
    assert service.store.connection.total_changes == before_changes
    assert adapter._projections == before_projections == {}


@pytest.mark.parametrize("mutation", ["missing", "forged"])
def test_activation_doctor_rejects_untrusted_distinct_inherited_projection_without_live_io(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
    mutation: str,
):
    from plugins.auto_routing.auto_routing.adapters.hermes_0_18 import (
        Hermes018Adapter,
    )

    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    writer, persisted = _install_real_hermes_inventory(
        service,
        monkeypatch,
        signing_key=b"j" * 32,
    )
    _write_rebased_signed_inventory(
        service,
        writer,
        persisted,
        revision=f"inventory-inherited-{mutation}",
        inherited_descriptor_mutation=mutation,
    )
    adapter = Hermes018Adapter(credential_fingerprint_key=b"j" * 32)
    service.adapter = adapter
    proposal = _inherited_only_proposal(service)
    monkeypatch.setattr(
        service,
        "_runtime_root_config",
        lambda: {
            "model": {"default": "gpt-5.4-mini", "provider": "auto"},
            "agent": {"reasoning_effort": "high"},
        },
    )
    _forbid_real_hermes_doctor_live_paths(service, adapter, monkeypatch)

    doctor = service.doctor(
        _proposal=proposal,
        _activation_transition=True,
    )

    exact = next(
        item for item in doctor["checks"] if item["name"] == "exact_targets"
    )
    assert exact["status"] == "error"
    assert exact["detail"]["safe-default"]["status"] == "projection_error"
    assert "persisted_projection_inspection_failed" in exact["detail"][
        "safe-default"
    ]["reasons"]
    assert adapter._projections == {}


def test_activation_doctor_does_not_reinspect_inherited_runtime_already_targeted(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
):
    from plugins.auto_routing.auto_routing.adapters.hermes_0_18 import (
        Hermes018Adapter,
    )

    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    _writer, _persisted = _install_real_hermes_inventory(
        service,
        monkeypatch,
        signing_key=b"k" * 32,
    )
    adapter = Hermes018Adapter(credential_fingerprint_key=b"k" * 32)
    service.adapter = adapter
    proposal = _inherited_only_proposal(service)
    monkeypatch.setattr(
        service,
        "_runtime_root_config",
        lambda: {
            "model": {"default": "gpt-5.4", "provider": "auto"},
            "agent": {"reasoning_effort": "medium"},
        },
    )
    inspected_models: list[str] = []
    inspect_projection = adapter.inspect_persisted_projection

    def record_inspection(observation, *, reasoning_effort, hermes_config=None):
        inspected_models.append(observation.key.model)
        return inspect_projection(
            observation,
            reasoning_effort=reasoning_effort,
            hermes_config=hermes_config,
        )

    monkeypatch.setattr(
        adapter,
        "inspect_persisted_projection",
        record_inspection,
    )
    _forbid_real_hermes_doctor_live_paths(service, adapter, monkeypatch)

    doctor = service.doctor(
        _proposal=proposal,
        _activation_transition=True,
    )

    exact = next(
        item for item in doctor["checks"] if item["name"] == "exact_targets"
    )
    assert exact["status"] == "ok", exact
    assert inspected_models == ["gpt-5.4"]


def test_activation_doctor_reinspects_targeted_inherited_runtime_for_distinct_effort(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
):
    from plugins.auto_routing.auto_routing.adapters.hermes_0_18 import (
        Hermes018Adapter,
    )

    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    _writer, _persisted = _install_real_hermes_inventory(
        service,
        monkeypatch,
        signing_key=b"l" * 32,
    )
    adapter = Hermes018Adapter(credential_fingerprint_key=b"l" * 32)
    service.adapter = adapter
    proposal = _inherited_only_proposal(service)
    monkeypatch.setattr(
        service,
        "_runtime_root_config",
        lambda: {
            "model": {"default": "gpt-5.4", "provider": "auto"},
            "agent": {"reasoning_effort": "high"},
        },
    )
    inspections: list[tuple[str, str]] = []
    inspect_projection = adapter.inspect_persisted_projection

    def record_inspection(observation, *, reasoning_effort, hermes_config=None):
        inspections.append((observation.key.model, reasoning_effort))
        return inspect_projection(
            observation,
            reasoning_effort=reasoning_effort,
            hermes_config=hermes_config,
        )

    monkeypatch.setattr(
        adapter,
        "inspect_persisted_projection",
        record_inspection,
    )
    _forbid_real_hermes_doctor_live_paths(service, adapter, monkeypatch)

    doctor = service.doctor(
        _proposal=proposal,
        _activation_transition=True,
    )

    exact = next(
        item for item in doctor["checks"] if item["name"] == "exact_targets"
    )
    assert exact["status"] == "ok", exact
    assert inspections == [
        ("gpt-5.4", "medium"),
        ("gpt-5.4", "high"),
    ]
    assert exact["detail"]["safe-default"]["reasoning_effort"] == "high"
    assert "projection_reused_from" not in exact["detail"]["safe-default"]


def test_activation_doctor_rejects_expired_persisted_target_verification(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    preview = service.preview_activation("active")
    prior = service.store.read_inventory_snapshot(
        preview["fingerprints"]["inventory_revision"]
    )
    assert prior is not None
    drift_id = "inventory-expired-activation-targets"
    observations = tuple(
        observation.model_copy(
            update={
                "key": observation.key.model_copy(
                    update={"inventory_revision": drift_id}
                ),
                "verification_expires_at": "2020-01-01T00:00:00Z",
            }
        )
        for observation in prior.observations
    )
    service.store.write_inventory_snapshot(
        drift_id,
        observations,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    proposal = service._activation_proposal(
        service._configured_authority(),
        "active",
    )

    doctor = service.doctor(
        _proposal=proposal,
        _activation_transition=True,
    )

    exact = next(
        item for item in doctor["checks"] if item["name"] == "exact_targets"
    )
    assert exact["status"] == "error"
    assert exact["detail"]["coding:primary"]["status"] == "verification_expired"


def test_activation_doctor_rejects_profile_capability_mismatch(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    preview = service.preview_activation("active")
    prior = service.store.read_inventory_snapshot(
        preview["fingerprints"]["inventory_revision"]
    )
    assert prior is not None
    drift_id = "inventory-capability-mismatch"
    observations = []
    for observation in prior.observations:
        capabilities = dict(observation.capabilities)
        if observation.key.model == "gpt-5.4":
            capabilities["supports_tools"] = False
        observations.append(
            observation.model_copy(
                update={
                    "key": observation.key.model_copy(
                        update={"inventory_revision": drift_id}
                    ),
                    "capabilities": capabilities,
                }
            )
        )
    service.store.write_inventory_snapshot(
        drift_id,
        observations,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    proposal = service._activation_proposal(
        service._configured_authority(),
        "active",
    )

    doctor = service.doctor(
        _proposal=proposal,
        _activation_transition=True,
    )

    exact = next(
        item for item in doctor["checks"] if item["name"] == "exact_targets"
    )
    assert exact["status"] == "error"
    assert "required_capability_unsupported:tools" in exact["detail"][
        "coding:primary"
    ]["reasons"]


@pytest.mark.parametrize(
    ("attribute", "value", "expected_reason"),
    [
        (
            "projected_credential_pool",
            object(),
            "projected_credential_pool_mismatch",
        ),
        ("projected_model", "wrong-model", "projected_model_mismatch"),
        (
            "projected_provider",
            "wrong-provider",
            "projected_provider_mismatch",
        ),
        (
            "projected_api_mode",
            "wrong-mode",
            "projected_api_mode_mismatch",
        ),
        (
            "projected_reasoning_effort",
            "low",
            "projected_reasoning_mismatch",
        ),
        (
            "projected_fallback_model",
            ({"model": "global-fallback"},),
            "projected_fallback_not_empty",
        ),
    ],
)
def test_activation_doctor_rejects_inexact_runtime_projection(
    isolated_home: Path,
    runtime_service,
    attribute: str,
    value,
    expected_reason: str,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    setattr(service.adapter, attribute, value)
    proposal = service._activation_proposal(
        service._configured_authority(),
        "active",
    )

    doctor = service.doctor(
        _proposal=proposal,
        _activation_transition=True,
    )

    exact = next(
        item for item in doctor["checks"] if item["name"] == "exact_targets"
    )
    assert exact["status"] == "error"
    assert expected_reason in exact["detail"]["coding:primary"]["reasons"]


def test_activation_doctor_applies_target_cost_limit_to_explicit_safe_default(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    proposal = service._activation_proposal(
        service._configured_authority(),
        "active",
    )
    assert not isinstance(proposal.safe_default, str)
    proposal = proposal.model_copy(
        update={
            "safe_default": proposal.safe_default.model_copy(
                update={"max_estimated_task_cost_usd": 0.0}
            )
        }
    )

    doctor = service.doctor(
        _proposal=proposal,
        _activation_transition=True,
    )

    exact = next(
        item for item in doctor["checks"] if item["name"] == "exact_targets"
    )
    assert exact["status"] == "error"
    assert "estimated_cost_exceeds_limit" in exact["detail"]["safe-default"][
        "reasons"
    ]


def test_activation_precondition_binds_inventory_contract_drift(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    preview = service.preview_activation("active")
    prior_id = preview["fingerprints"]["inventory_revision"]
    prior = service.store.read_inventory_snapshot(prior_id)
    assert prior is not None
    drift_id = "inventory-activation-drift"
    observations = [
        observation.model_copy(
            update={
                "key": observation.key.model_copy(
                    update={"inventory_revision": drift_id}
                )
            }
        )
        for observation in prior.observations
    ]
    service.store.write_inventory_snapshot(
        drift_id,
        observations,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )

    with pytest.raises(Exception, match="activation precondition changed"):
        service.apply_activation(
            "active",
            expected_config_sha256=preview["expected_config_sha256"],
        )

    assert service.status()["activation_mode"] == "shadow"
    assert service.store.connection.execute(
        "SELECT COUNT(*) FROM activation_receipts"
    ).fetchone()[0] == 0


def test_activation_serializes_inventory_publication_through_receipt_commit(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    preview = service.preview_activation("active")
    approved_id = preview["fingerprints"]["inventory_revision"]
    approved = service.store.read_inventory_snapshot(approved_id)
    assert approved is not None
    concurrent_id = "inventory-concurrent-activation"
    concurrent_observations = tuple(
        observation.model_copy(
            update={
                "key": observation.key.model_copy(
                    update={"inventory_revision": concurrent_id}
                )
            }
        )
        for observation in approved.observations
    )
    ready = Event()
    publish = Event()
    finished = Event()
    writer_errors: list[BaseException] = []

    def publish_inventory() -> None:
        try:
            with RoutingStore.open(path=service.store.path) as writer:
                ready.set()
                if not publish.wait(5):
                    raise AssertionError("activation never released inventory writer")
                writer.write_inventory_snapshot(
                    concurrent_id,
                    concurrent_observations,
                    created_at=datetime.now(UTC).isoformat().replace(
                        "+00:00", "Z"
                    ),
                )
        except BaseException as error:
            writer_errors.append(error)
        finally:
            finished.set()

    writer = Thread(target=publish_inventory, daemon=True)
    writer.start()
    assert ready.wait(2)

    class ConcurrentWriter:
        def after_activation_prepared(self) -> None:
            publish.set()
            assert not finished.wait(0.1), (
                "inventory publication escaped the activation write transaction"
            )

    service._fault_injector = ConcurrentWriter()
    applied = service.apply_activation(
        "active",
        expected_config_sha256=preview["expected_config_sha256"],
    )
    service._fault_injector = None
    writer.join(10)

    assert not writer.is_alive()
    assert writer_errors == []
    assert service.store.read_inventory_snapshot(concurrent_id) is not None
    assert applied["fingerprints"]["inventory_revision"] == approved_id
    receipt = service.store.read_matching_activation_receipt(
        authority_id=applied["authority_id"],
        config_sha=applied["proposed_config_sha256"],
        adapter_capability_sha=applied["fingerprints"]["adapter_capability_sha"],
    )
    assert receipt is not None
    assert receipt.inventory_revision == approved_id


def test_activation_rejects_adapter_capability_drift_after_doctor(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    preview = service.preview_activation("active")

    class CapabilityDrift:
        def after_activation_prepared(self) -> None:
            service.adapter.compatible = False

    service._fault_injector = CapabilityDrift()

    with pytest.raises(Exception, match="adapter capability"):
        service.apply_activation(
            "active",
            expected_config_sha256=preview["expected_config_sha256"],
        )

    service._fault_injector = None
    assert service.status()["activation_mode"] == "shadow"
    assert service._pending_apply_journals() == []
    assert service.store.connection.execute(
        "SELECT COUNT(*) FROM activation_receipts"
    ).fetchone()[0] == 0


def test_activation_apply_rejects_changed_config_precondition_without_decision(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    preview = service.preview_activation("active")
    service.config_path.write_bytes(
        service.config_path.read_bytes() + b"\n# concurrent edit\n"
    )

    with pytest.raises(Exception, match="precondition changed"):
        service.apply_activation(
            "active",
            expected_config_sha256=preview["expected_config_sha256"],
        )

    assert service.store.count_decisions() == 0
    assert service.status()["activation_mode"] == "shadow"


def test_unhealthy_adapter_doctor_rejects_active_without_writes(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    service.adapter.compatible = False
    before = service.config_path.read_bytes()
    changes = service.store.connection.total_changes

    with pytest.raises(Exception, match="healthy doctor"):
        service.preview_activation("active")

    assert service.config_path.read_bytes() == before
    assert service.store.count_decisions() == 0
    assert service.store.connection.total_changes == changes


def test_resolver_signature_drift_rejects_active_preview(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    manager = service.plugin_context._manager
    manager._agent_runtime_resolver = SimpleNamespace(
        requires_initial_task=lambda: True,
        resolve=lambda: None,
        record_manual_pin=lambda: None,
        record_session_continuation=lambda: None,
    )
    before = service.config_path.read_bytes()

    with pytest.raises(Exception, match="resolver_registration"):
        service.preview_activation("active")

    assert service.config_path.read_bytes() == before
    assert service.store.count_decisions() == 0


def test_classifier_economics_drift_rejects_active_preview(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    preview = service.preview_activation("active")
    prior = service.store.read_inventory_snapshot(
        preview["fingerprints"]["inventory_revision"]
    )
    assert prior is not None
    drift_id = "inventory-classifier-economics-drift"
    observations = []
    for observation in prior.observations:
        update = {
            "key": observation.key.model_copy(
                update={"inventory_revision": drift_id}
            )
        }
        if observation.key.model == "gpt-5.4-mini":
            update["economics"] = observation.economics.model_copy(
                update={"observed_at": "2020-01-01T00:00:00Z"}
            )
        observations.append(observation.model_copy(update=update))
    service.store.write_inventory_snapshot(
        drift_id,
        observations,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    proposal = service._activation_proposal(
        service._configured_authority(),
        "active",
    )

    doctor = service.doctor(
        _proposal=proposal,
        _activation_transition=True,
    )

    classifier = next(
        item
        for item in doctor["checks"]
        if item["name"] == "classifier_evaluator_trust"
    )
    assert classifier["status"] == "error"
    assert classifier["detail"]["reason"] == "classifier_economics_stale"
    assert service.store.count_decisions() == 0


class _ActivationCrash(BaseException):
    pass


class _ActivationFault:
    def __init__(self, crash_point: str) -> None:
        self.crash_point = crash_point

    def after_receipt_before_yaml(self) -> None:
        if self.crash_point == "after_receipt_before_yaml":
            raise _ActivationCrash

    def after_yaml_before_saga_commit(self) -> None:
        if self.crash_point == "after_yaml_before_saga_commit":
            raise _ActivationCrash

    def after_activation_complete(self) -> None:
        if self.crash_point == "after_activation_complete":
            raise _ActivationCrash

    def after_activation_db_commit_before_journal_remove(self) -> None:
        if self.crash_point == "after_activation_db_commit_before_journal_remove":
            raise _ActivationCrash


@pytest.mark.parametrize(
    "crash_point, expected_mode",
    [
        ("after_receipt_before_yaml", "shadow"),
        ("after_yaml_before_saga_commit", "active"),
        ("after_activation_complete", "active"),
        ("after_activation_db_commit_before_journal_remove", "active"),
    ],
)
def test_activation_saga_recovers_to_shadow_or_matching_active_pair(
    isolated_home: Path,
    runtime_service,
    crash_point: str,
    expected_mode: str,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    preview = service.preview_activation("active")
    service._fault_injector = _ActivationFault(crash_point)

    with pytest.raises(_ActivationCrash):
        service.apply_activation(
            "active",
            expected_config_sha256=preview["expected_config_sha256"],
        )

    assert service._pending_apply_journals()
    service._fault_injector = None
    service._recover_pending_applies()
    assert service._pending_apply_journals() == []
    status = service.status()
    assert status["activation_mode"] == expected_mode
    if expected_mode == "active":
        assert status["activation_receipt_id"] is not None


def test_orphan_receipt_cannot_legitimize_hand_edited_active_noop(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    preview = service.preview_activation("active")
    service._fault_injector = _ActivationFault("after_receipt_before_yaml")
    with pytest.raises(_ActivationCrash):
        service.apply_activation(
            "active",
            expected_config_sha256=preview["expected_config_sha256"],
        )
    service._fault_injector = None
    service._recover_pending_applies()
    assert service.status()["activation_mode"] == "shadow"
    assert service.store.connection.execute(
        "SELECT COUNT(*) FROM activation_receipts"
    ).fetchone()[0] == 0
    service.store.write_activation_receipt(
        ActivationReceipt(
            receipt_id="orphan-active-receipt",
            authority_id=preview["authority_id"],
            config_sha=preview["proposed_config_sha256"],
            inventory_contract_sha=preview["fingerprints"][
                "inventory_contract_sha"
            ],
            inventory_revision=preview["fingerprints"]["inventory_revision"],
            adapter_capability_sha=preview["fingerprints"][
                "adapter_capability_sha"
            ],
            created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
    )

    root = json.loads(service.config_path.read_text(encoding="utf-8"))
    root["plugins"]["entries"]["auto-routing"]["activation"]["mode"] = "active"
    service.config_path.write_text(json.dumps(root), encoding="utf-8")

    with pytest.raises(Exception, match="healthy doctor"):
        service.preview_activation("active")
    assert service.status()["activation_mode"] == "off"


def test_real_active_replay_requires_recorded_receipt_integrity(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="active",
    )
    receipt = _seed_activation_receipt(
        service,
        receipt_id="receipt-integrity",
        inventory_contract_sha="c" * 64,
    )
    first = resolver.resolve(_request())
    assert first.action == "project"

    service.store.connection.execute(
        "DELETE FROM activation_receipts WHERE receipt_id = ?",
        (receipt.receipt_id,),
    )
    replay = resolver.resolve(_request(is_resume=True))

    assert replay.action == "inherit"
    assert replay.reason_code == "recorded_state_invalid"
    assert replay.runtime.model == "baseline"
    assert service.store.count_decisions() == 1


@pytest.mark.parametrize("observer_fails", [False, True])
def test_real_replay_uses_only_recorded_fallback_and_starts_cache_epoch(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
    observer_fails,
):
    from plugins.auto_routing.auto_routing.config import parse_config
    from utils import fast_safe_load

    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="active",
    )
    config = parse_config(fast_safe_load(service.config_path.read_bytes()))
    _seed_activation_receipt(
        service,
        receipt_id="receipt-fallback",
        inventory_contract_sha="b" * 64,
    )
    first = resolver.resolve(_request())
    assert first.action == "project"
    marker = service.store.mark_route_epoch_provider_started
    if observer_fails:
        def fail_marker(*_args, **_kwargs):
            raise RuntimeError("injected provider marker failure")

        monkeypatch.setattr(
            service.store,
            "mark_route_epoch_provider_started",
            fail_marker,
        )
    resolver.on_pre_api_request(
        session_id="session-a",
        task_id="task-a",
        api_request_id="request-first",
        decision_id=first.decision_id,
        runtime_id=first.event["runtime_id"],
        model="gpt-5.4",
        provider="openai-codex",
    )
    if observer_fails:
        monkeypatch.setattr(
            service.store,
            "mark_route_epoch_provider_started",
            marker,
        )
        assert service.store.read_route_epochs("session-a")[0].provider_started is False

    service.adapter.retain_models("gpt-5.4-mini")
    replay = resolver.resolve(_request(is_resume=True))

    assert replay.action == "project"
    assert replay.runtime.model == "gpt-5.4-mini"
    assert replay.event["cache_degraded"] is True
    assert service.store.count_decisions() == 1
    epochs = service.store.read_route_epochs("session-a")
    assert [epoch.epoch_number for epoch in epochs] == [0, 1]
    assert [epoch.runtime_id for epoch in epochs] == [
        config.profiles["coding"].primary.runtime.stable_id(),
        config.profiles["coding"].fallbacks[0].runtime.stable_id(),
    ]

    # A delayed hook from the primary must not mark the newer fallback epoch.
    resolver.on_pre_api_request(
        session_id="session-a",
        task_id="task-a",
        api_request_id="stale-primary-request",
        decision_id=first.decision_id,
        runtime_id=first.event["runtime_id"],
        model="gpt-5.4",
        provider="openai-codex",
    )
    assert service.store.read_route_epochs("session-a")[-1].provider_started is False


@pytest.mark.parametrize(
    "degradation",
    ["configured_unverified", "quota", "cooldown"],
)
def test_recorded_replay_skips_degraded_primary_for_recorded_fallback_without_reclassification(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
    degradation: str,
):
    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="active",
    )
    _seed_activation_receipt(
        service,
        receipt_id=f"receipt-replay-primary-{degradation}",
        inventory_contract_sha="8" * 64,
    )
    first = resolver.resolve(_request())
    assert first.action == "project"
    adapter = service.adapter
    adapter.resolve_calls.clear()
    _degrade_runtime_models(
        adapter,
        "gpt-5.4",
        degradation=degradation,
    )

    def forbid_reclassification(*_args, **_kwargs):
        pytest.fail("recorded replay must not reclassify")

    monkeypatch.setattr(
        service,
        "_runtime_rule_evaluation",
        forbid_reclassification,
    )

    replay = resolver.resolve(_request(is_resume=True))

    assert replay.action == "project", (replay.reason_code, replay.event)
    assert replay.runtime.model == "gpt-5.4-mini"
    assert replay.decision_id == first.decision_id
    assert replay.event["recorded_replay"] is True
    assert adapter.resolve_calls == ["gpt-5.4-mini"]
    assert service.store.count_decisions() == 1


@pytest.mark.parametrize("degradation", ["configured_unverified", "quota"])
def test_recorded_replay_inherits_after_recorded_chain_is_degraded_without_reclassification(
    isolated_home: Path,
    runtime_service,
    monkeypatch,
    degradation: str,
):
    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="active",
    )
    _seed_activation_receipt(
        service,
        receipt_id=f"receipt-replay-exhausted-{degradation}",
        inventory_contract_sha="9" * 64,
    )
    first = resolver.resolve(_request())
    assert first.action == "project"
    adapter = service.adapter
    adapter.resolve_calls.clear()
    _degrade_runtime_models(
        adapter,
        "gpt-5.4",
        "gpt-5.4-mini",
        degradation=degradation,
    )

    def forbid_reclassification(*_args, **_kwargs):
        pytest.fail("recorded replay must not reclassify")

    monkeypatch.setattr(
        service,
        "_runtime_rule_evaluation",
        forbid_reclassification,
    )

    replay = resolver.resolve(_request(is_resume=True))

    assert replay.action == "inherit"
    assert replay.runtime.model == "baseline"
    assert replay.decision_id is None
    assert adapter.resolve_calls == []
    assert service.store.count_decisions() == 1


def test_classifier_failure_commits_typed_safe_default_without_fabricated_assessment(
    isolated_home: Path,
    runtime_service,
):
    service = runtime_service
    resolver = _real_runtime_resolver(
        isolated_home=isolated_home,
        service=service,
        mode="shadow",
    )
    root = json.loads(service.config_path.read_text(encoding="utf-8"))
    root["plugins"]["entries"]["auto-routing"]["rules"] = []
    service.config_path.write_text(json.dumps(root), encoding="utf-8")
    service.adapter.retain_models("gpt-5.4")

    plan = resolver.resolve(_request())

    assert plan.action == "shadow", (plan.reason_code, plan.event)
    decision = service.store.read_session_decision("session-a")
    assert decision is not None
    assert decision.assessment is None
    assert decision.safe_default_reason == "classifier_failed"
    assert decision.selected_profile_id is None
    assert decision.selected_runtime.stable_id() == (
        decision.safe_default_runtime.stable_id()
    )
