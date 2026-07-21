"""Production-backed routed-turn evidence harness for Stage 3 tests."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import patch

from agent.runtime_routing import (
    RUNTIME_ROUTING_CONTRACT_VERSION,
    AgentRuntimeContext,
    AgentRuntimeRequest,
    AgentRuntimeSpec,
    RuntimeSessionContinuation,
)
from hermes_cli.plugins import PluginContext, PluginManager
from plugins.auto_routing.auto_routing.adapters.base import (
    PERSISTED_RUNTIME_PROJECTION_CONTRACT,
    AdapterInventory,
    PersistedRuntimeProjection,
    ProviderInventoryRow,
    ResolvedRuntime,
)
from plugins.auto_routing.auto_routing.catalog import CatalogRecord, CatalogService
from plugins.auto_routing.auto_routing.config import (
    authority_document,
    authority_revision,
    parse_config,
)
from plugins.auto_routing.auto_routing.models import (
    AccessEconomics,
    CatalogEvidence,
    EvidenceOutcome,
    ReasoningEffort,
    RoutingDecision,
    RuntimeKey,
)
from plugins.auto_routing.auto_routing.runtime_resolver import (
    AutoRoutingRuntimeResolver,
)
from plugins.auto_routing.auto_routing.service import AutoRoutingService
from plugins.auto_routing.auto_routing.storage import (
    RouteEpoch,
    RoutingStore,
    SessionRouteBinding,
)
from run_agent import AIAgent

from _stage2_test_support import LoopbackProvider, plugin_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _clear_profile_caches() -> None:
    from agent import skill_utils
    from hermes_cli import config

    with config._CONFIG_LOCK:
        config._LOAD_CONFIG_CACHE.clear()
        config._RAW_CONFIG_CACHE.clear()
        config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    config.invalidate_env_cache()
    skill_utils._ENV_DETECT_CACHE.clear()
    skill_utils._external_dirs_cache_clear()


class _Stage3Adapter:
    """Two-target adapter whose private endpoint can change without identity drift."""

    def __init__(self, *, api_key: str) -> None:
        self.base_url = "http://127.0.0.1:1/v1"
        self.api_key = api_key
        self.primary_available = True
        self.now = _timestamp()
        economics = AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=0.10,
            metered_output_usd_per_million_tokens=0.20,
            effective_marginal_cost_usd_per_task=0.001,
            effective_amortized_cost_usd_per_task=0.001,
            evidence_ttl_seconds=3_600,
            source_id="stage3-loopback-economics",
            provenance="test-contract",
            observed_at=self.now,
        )
        self._economics = economics

    def inventory(self, refresh: bool = False) -> AdapterInventory:
        del refresh
        models = (
            ("primary-model", "fallback-model")
            if self.primary_available
            else ("fallback-model",)
        )
        row = ProviderInventoryRow(
            provider="openrouter",
            resolver_name="openrouter",
            models=models,
            authenticated=True,
            live_attempt_status="succeeded",
            model_provenance={model: "authenticated_live" for model in models},
            provenance_details={
                model: {
                    "endpoint_identity": "endpoint:stage3-loopback",
                    "auth_identity": "api-key:stage3",
                    "observed_at": self.now,
                }
                for model in models
            },
            auth_identity="api-key:stage3",
            credential_pool_identity="pool:stage3",
            endpoint_identity="endpoint:stage3-loopback",
            credential_fingerprint="fingerprint:stage3",
            api_mode="chat_completions",
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
            economics={model: self._economics for model in models},
            observed_at=self.now,
            source="stage3-loopback",
        )
        return AdapterInventory(provider_rows=(row,), local_rows=())

    def resolve(self, runtime_key: RuntimeKey) -> ResolvedRuntime:
        if runtime_key.model == "primary-model" and not self.primary_available:
            raise RuntimeError("primary unavailable before dispatch")
        return ResolvedRuntime(
            runtime_key=runtime_key,
            resolver_name="openrouter",
            provider="openrouter",
            api_mode="chat_completions",
            source="stage3-loopback",
            base_url=self.base_url,
            api_key=self.api_key,
            credential_pool=None,
            extra={},
        )

    def resolve_inherited_baseline(self, _inventory_revision: str):
        return None

    @staticmethod
    def capability_report() -> dict[str, Any]:
        return {
            "contract": "stage3-loopback-v1",
            "fresh_session": True,
            "delegation": True,
            "pre_call_fallback": True,
            "exact_credential_pool": True,
            "reasoning_projection": True,
            "post_call_model_failover": False,
        }

    def to_agent_runtime_spec(
        self,
        resolved_runtime: ResolvedRuntime,
        *,
        reasoning_effort: str,
        hermes_config=None,
    ) -> AgentRuntimeSpec:
        del hermes_config
        return AgentRuntimeSpec(
            model=resolved_runtime.runtime_key.model,
            provider="openrouter",
            base_url=self.base_url,
            api_key=self.api_key,
            resolution_state="resolved",
            api_mode="chat_completions",
            reasoning_config={
                "enabled": reasoning_effort != "none",
                "effort": reasoning_effort,
            },
            fallback_model=(),
        )

    def to_recorded_agent_runtime_spec(
        self,
        resolved_runtime: ResolvedRuntime,
        *,
        reasoning_effort: str,
    ) -> AgentRuntimeSpec:
        return self.to_agent_runtime_spec(
            resolved_runtime,
            reasoning_effort=reasoning_effort,
        )

    @staticmethod
    def inspect_persisted_projection(
        observation,
        *,
        reasoning_effort: str,
        hermes_config=None,
    ) -> PersistedRuntimeProjection:
        del hermes_config
        runtime_key = observation.key
        return PersistedRuntimeProjection(
            contract=PERSISTED_RUNTIME_PROJECTION_CONTRACT,
            runtime_key=runtime_key,
            resolution_state="resolved",
            model=runtime_key.model,
            provider=runtime_key.provider,
            api_mode=runtime_key.api_mode,
            credential_pool_identity=runtime_key.credential_pool_identity,
            resolver_name="openrouter",
            access_kind="api-key",
            reasoning_effort=reasoning_effort,
            fallback_owner="auto-routing-pre-call",
            fallback_count=0,
        )


class _CatalogSource:
    def __init__(self, now: str, runtime_ids: dict[str, str]) -> None:
        self.now = now
        self.runtime_ids = runtime_ids

    def load(self):
        records = []
        for model, runtime_id in self.runtime_ids.items():
            quality = 0.95 if model == "primary-model" else 0.75
            for name, direction, value in (
                ("quality", "higher_is_better", quality),
                ("reliability", "higher_is_better", quality),
                ("latency", "lower_is_better", 1.0),
            ):
                evidence = CatalogEvidence(
                    source_id=f"stage3-{model}-{name}",
                    source_url=f"https://catalog.invalid/stage3/{model}/{name}",
                    retrieved_at=self.now,
                    published_at=self.now,
                    model=model,
                    model_version=model,
                    domain="coding",
                    task_definition="default",
                    metric_name=name,
                    metric_direction=direction,
                    metric_scale="seconds" if name == "latency" else "unit_interval",
                    value=value,
                    normalization_method=(
                        "divide_by_limit" if name == "latency" else "identity"
                    ),
                    sample_size=100,
                    confidence=0.95,
                )
                records.append(
                    CatalogRecord(
                        evidence=evidence,
                        canonical_provider="openrouter",
                        canonical_model=model,
                        canonical_version=model,
                        runtime_id=runtime_id,
                    )
                )
        return tuple(records)


def _authority() -> dict[str, Any]:
    value = json.loads(
        (Path(__file__).with_name("fixtures") / "approved_proposal.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = value["profiles"]["coding"]["primary"]["runtime"]
    runtime.update(
        {
            "provider": "openrouter",
            "model": "primary-model",
            "auth_identity": "api-key:stage3",
            "credential_pool_identity": "pool:stage3",
            "endpoint_identity": "endpoint:stage3-loopback",
            "api_mode": "chat_completions",
            "inventory_revision": "inventory-stage3",
        }
    )
    value["profiles"]["coding"]["primary"]["reasoning"] = {
        "default": "high",
        "min": "low",
        "max": "high",
    }
    fallback = json.loads(json.dumps(value["profiles"]["coding"]["primary"]))
    fallback["runtime"]["model"] = "fallback-model"
    fallback["reasoning"] = {"default": "high", "min": "low", "max": "high"}
    value["profiles"]["coding"]["fallbacks"] = [fallback]
    value["safe_default"] = json.loads(json.dumps(fallback))
    value["activation"]["mode"] = "shadow"
    value["llm"]["allowed_providers"] = ["openrouter"]
    value["llm"]["allowed_models"] = ["primary-model", "fallback-model"]
    value["classifier"]["provider"] = "openrouter"
    value["classifier"]["model"] = "primary-model"
    value["rules"] = [
        {
            "rule_id": "stage3-coding",
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
    return value


def _request(
    *,
    session_id: str,
    task_id: str,
    is_resume: bool = False,
) -> AgentRuntimeRequest:
    return AgentRuntimeRequest(
        contract_version=RUNTIME_ROUTING_CONTRACT_VERSION,
        context=AgentRuntimeContext(
            scope="fresh_session",
            task="stage3 production-backed route",
            session_id=session_id,
            task_id=task_id,
            is_resume=is_resume,
            metadata={"platform": "cli"},
        ),
        baseline=AgentRuntimeSpec(
            model="baseline-model",
            provider="openrouter",
            base_url="http://127.0.0.1:1/v1",
            api_key="BASELINE_STAGE3_KEY",
            resolution_state="requested",
            api_mode="chat_completions",
            reasoning_config={"enabled": True, "effort": "low"},
        ),
    )


@dataclass(slots=True)
class Stage3RouteHarness:
    home: Path
    service: AutoRoutingService
    resolver: AutoRoutingRuntimeResolver
    session_id: str
    task_id: str
    decision: RoutingDecision
    binding: SessionRouteBinding
    epoch: RouteEpoch
    runtime: RuntimeKey
    reasoning_effort: ReasoningEffort
    _manager: PluginManager = field(repr=False)
    _adapter: _Stage3Adapter = field(repr=False)
    _response_text: str = field(repr=False)
    _selected_api_key: str = field(repr=False)
    _endpoint_suffix: str = field(repr=False)
    _turn_id: str = field(repr=False)
    _owned_services: list[AutoRoutingService] = field(default_factory=list, repr=False)

    def payload(
        self,
        *,
        outcome: EvidenceOutcome = "verified",
        api_calls: int = 1,
        task_id: str | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        return {
            "telemetry_schema_version": "hermes.observer.v1",
            "session_id": self.session_id,
            "turn_id": self._turn_id,
            "task_id": self.task_id if task_id is None else task_id,
            "observed_at_unix": datetime.now(UTC).timestamp(),
            "outcome": outcome,
            "api_calls": api_calls,
            "tool_iterations": 0,
            "retry_count": 0,
            "cost_usd": 0.001,
            "input_tokens": 5,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "reasoning_effort": self.reasoning_effort,
            "runtime_binding": {
                "scope": self.decision.scope,
                "session_id": self.session_id,
                "task_id": self.task_id,
                "action": "project",
                "model": self.runtime.model if model is None else model,
                "provider": self.runtime.provider if provider is None else provider,
                "decision_id": self.decision.decision_id,
            },
        }

    def fresh_service(
        self,
        *,
        allow_cross_thread_close: bool = False,
    ) -> AutoRoutingService:
        context = PluginContext(plugin_manifest(PROJECT_ROOT), PluginManager())
        service = AutoRoutingService(
            plugin_context=context,
            hermes_home=self.home,
            store=RoutingStore.open(
                home=self.home,
                allow_cross_thread_close=allow_cross_thread_close,
            ),
            adapter=self._adapter,
            _pinned_config_path=self.home / "config.yaml",
        )
        self._owned_services.append(service)
        return service

    @contextmanager
    def activate_profile(self) -> Iterator[None]:
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        token = set_hermes_home_override(self.home)
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(self.home)}),
            patch("hermes_cli.plugins._plugin_manager", self._manager),
        ):
            _clear_profile_caches()
            try:
                yield
            finally:
                _clear_profile_caches()
                reset_hermes_home_override(token)

    def compression_child(
        self,
        *,
        child_session_id: str,
        child_task_id: str,
    ) -> "Stage3RouteHarness":
        with self.activate_profile():
            self.resolver.record_session_continuation(
                RuntimeSessionContinuation(
                    parent_session_id=self.session_id,
                    child_session_id=child_session_id,
                )
            )
            plan = self.resolver.resolve(
                _request(
                    session_id=child_session_id,
                    task_id=child_task_id,
                    is_resume=True,
                )
            )
            assert plan.action == "project"
            binding = self.service.store.read_session_binding(child_session_id)
            assert binding is not None
            epochs = self.service.store.read_route_epochs(child_session_id)
            epoch = next(
                item for item in epochs if item.epoch_number == binding.current_epoch
            )
            target = AutoRoutingService._recorded_target_for_runtime(
                self.decision,
                epoch.runtime_id,
            )
            assert target is not None
            runtime, effort = target
            self.service.mark_runtime_provider_started(
                session_id=child_session_id,
                task_id=child_task_id,
                api_request_id=f"request-{child_session_id}"[:256],
                decision_id=self.decision.decision_id,
                runtime_id=epoch.runtime_id,
                model=runtime.model,
                provider=runtime.provider,
            )
            epoch = next(
                item
                for item in self.service.store.read_route_epochs(child_session_id)
                if item.epoch_number == binding.current_epoch
            )
            assert epoch.provider_started is True
        return Stage3RouteHarness(
            home=self.home,
            service=self.service,
            resolver=self.resolver,
            session_id=child_session_id,
            task_id=child_task_id,
            decision=self.decision,
            binding=binding,
            epoch=epoch,
            runtime=runtime,
            reasoning_effort=effort,
            _manager=self._manager,
            _adapter=self._adapter,
            _response_text=self._response_text,
            _selected_api_key=self._selected_api_key,
            _endpoint_suffix=self._endpoint_suffix,
            _turn_id=self._turn_id,
            _owned_services=self._owned_services,
        )

    def run_real_turn(
        self,
        *,
        prompt: str,
        on_request_entry: Callable[[], None] | None = None,
        on_chat_request_entry: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        from plugins.auto_routing.auto_routing.evidence import turn_evidence_id

        with LoopbackProvider(
            response_text=self._response_text,
            on_request_entry=on_request_entry,
            on_chat_request_entry=on_chat_request_entry,
        ) as provider:
            suffix = self._endpoint_suffix.strip("/")
            self._adapter.base_url = (
                f"{provider.base_url}/{suffix}" if suffix else provider.base_url
            )
            with self.activate_profile():
                agent = AIAgent(
                    api_key=self._selected_api_key,
                    base_url=provider.base_url,
                    provider="openrouter",
                    model="baseline-model",
                    max_iterations=4,
                    enabled_toolsets=[],
                    quiet_mode=True,
                    skip_context_files=True,
                    skip_memory=True,
                    save_trajectories=False,
                    platform="cli",
                    session_id=self.session_id,
                    runtime_routing_context=AgentRuntimeContext(
                        scope="fresh_session",
                        task=prompt,
                        session_id=self.session_id,
                        task_id=self.task_id,
                        is_resume=True,
                        metadata={"platform": "cli"},
                    ),
                    fallback_model=(),
                )
                try:
                    result = agent.run_conversation(
                        prompt,
                        conversation_history=[],
                        task_id=self.task_id,
                    )
                    public_turn_id = __import__("hashlib").sha256(
                        f"turn\0{agent._current_turn_id}".encode()
                    ).hexdigest()
                finally:
                    agent.close()
            assert provider.entry_errors == []
            assert provider.chat_authorization_headers == [
                f"Bearer {self._selected_api_key}"
            ]
            if self._endpoint_suffix:
                assert self._adapter.base_url.endswith(
                    f"/{self._endpoint_suffix.strip('/')}"
                )
        evidence_id = turn_evidence_id(self.session_id, public_turn_id)
        try:
            event = self.service.store.read_evidence_event(evidence_id)
        except sqlite3.DatabaseError:
            # Fail-open tests deliberately damage or lock the evidence leaf.
            # The real turn result must remain observable without requiring a
            # successful test-only evidence lookup after the agent returns.
            event = None
        return {
            **result,
            "decision_id": self.decision.decision_id,
            "evidence_id": evidence_id,
            "evidence": event,
        }

    def auto_routing_artifacts(self) -> tuple[Path, ...]:
        candidates = [
            self.service.store.path,
            Path(f"{self.service.store.path}-wal"),
            Path(f"{self.service.store.path}-shm"),
        ]
        candidates.extend(self.home.glob("auto-routing-apply-*.pending.json"))
        return tuple(path for path in candidates if path.exists())

    @contextmanager
    def lock_store(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.service.store.path, timeout=0.05)
        connection.execute("PRAGMA busy_timeout = 50")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()

    def close(self) -> None:
        for service in reversed(self._owned_services):
            service.close()
        self._owned_services.clear()
        self.resolver.close()


def build_stage3_route_harness(
    tmp_path,
    monkeypatch,
    *,
    execution="primary",
    profile_name="default",
    session_id=None,
    task_id=None,
    turn_id=None,
    response_text="ok",
    selected_api_key="STAGE3_TEST_KEY",
    endpoint_suffix="",
):
    del monkeypatch
    home = Path(tmp_path) / f"profile-{profile_name}"
    home.mkdir(parents=True, exist_ok=True)
    session_id = session_id or f"stage3-{profile_name}-session"
    task_id = task_id or f"stage3-{profile_name}-task"
    turn_id = turn_id or __import__("hashlib").sha256(
        f"stage3-turn:{profile_name}:{session_id}:{task_id}".encode()
    ).hexdigest()

    manager = PluginManager()
    context = PluginContext(plugin_manifest(PROJECT_ROOT), manager)
    adapter = _Stage3Adapter(api_key=selected_api_key)
    service = AutoRoutingService(
        plugin_context=context,
        hermes_home=home,
        store=RoutingStore.open(home=home),
        adapter=adapter,
        _pinned_config_path=home / "config.yaml",
    )
    resolver = AutoRoutingRuntimeResolver(
        plugin_context=context,
        home_resolver=lambda: home,
        service_factory=lambda: service,
    )
    context.register_agent_runtime_resolver(resolver)
    context.register_hook("pre_api_request", resolver.on_pre_api_request)
    context.register_hook("post_turn_outcome", resolver.on_post_turn_outcome)

    authority = _authority()
    service.config_path.write_text(
        json.dumps({"plugins": {"entries": {"auto-routing": authority}}}),
        encoding="utf-8",
    )
    config = parse_config({"plugins": {"entries": {"auto-routing": authority}}})
    authority_id = authority_revision(config)
    service.store.publish_authority_and_baseline(
        authority_id=authority_id,
        document=authority_document(config),
        baseline=service._baseline_revision(config, authority_id=authority_id),
    )
    inventory = service._new_inventory_service().refresh(refresh=False, persist=True)
    runtime_ids = {
        runtime.key.model: runtime.key.stable_id()
        for runtime in inventory.runtimes
        if runtime.key.model in {"primary-model", "fallback-model"}
    }
    assert set(runtime_ids) == {"primary-model", "fallback-model"}
    CatalogService(store=service.store).refresh(
        [_CatalogSource(adapter.now, runtime_ids)]
    )
    preview = service.preview_activation("active")
    assert preview["doctor"]["healthy"] is True, preview["doctor"]
    applied = service.apply_activation(
        "active",
        expected_config_sha256=preview["expected_config_sha256"],
    )
    assert applied["applied"] is True

    token = None
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(home)
        _clear_profile_caches()
        plan = resolver.resolve(
            _request(session_id=session_id, task_id=task_id)
        )
        assert plan.action == "project"
        if execution == "fallback":
            adapter.primary_available = False
            plan = resolver.resolve(
                _request(session_id=session_id, task_id=task_id, is_resume=True)
            )
            assert plan.action == "project"
            assert plan.runtime.model == "fallback-model"
        elif execution != "primary":
            raise ValueError("execution must be primary or fallback")
    finally:
        _clear_profile_caches()
        if token is not None:
            reset_hermes_home_override(token)

    binding = service.store.read_session_binding(session_id)
    assert binding is not None
    decision = service.store.read_decision(binding.decision_id)
    assert decision is not None
    epoch = next(
        item
        for item in service.store.read_route_epochs(session_id)
        if item.epoch_number == binding.current_epoch
    )
    target = AutoRoutingService._recorded_target_for_runtime(decision, epoch.runtime_id)
    assert target is not None
    runtime, effort = target
    service.mark_runtime_provider_started(
        session_id=session_id,
        task_id=task_id,
        api_request_id=f"request-{profile_name}-{execution}"[:256],
        decision_id=decision.decision_id,
        runtime_id=runtime.stable_id(),
        model=runtime.model,
        provider=runtime.provider,
    )
    epoch = next(
        item
        for item in service.store.read_route_epochs(session_id)
        if item.epoch_number == binding.current_epoch
    )
    assert epoch.provider_started is True
    return Stage3RouteHarness(
        home=home,
        service=service,
        resolver=resolver,
        session_id=session_id,
        task_id=task_id,
        decision=decision,
        binding=binding,
        epoch=epoch,
        runtime=runtime,
        reasoning_effort=effort,
        _manager=manager,
        _adapter=adapter,
        _response_text=response_text,
        _selected_api_key=selected_api_key,
        _endpoint_suffix=endpoint_suffix,
        _turn_id=turn_id,
    )


__all__ = ["Stage3RouteHarness", "build_stage3_route_harness"]
