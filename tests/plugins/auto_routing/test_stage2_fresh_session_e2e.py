"""Real fresh-session Auto Routing construction and provider-dispatch E2Es."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.runtime_routing import AgentRuntimeContext, AgentRuntimeSpec
from hermes_cli.plugins import PluginContext, PluginManager
from plugins.auto_routing.auto_routing.adapters.base import (
    AdapterInventory,
    PERSISTED_RUNTIME_PROJECTION_CONTRACT,
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
from plugins.auto_routing.auto_routing.models import AccessEconomics, CatalogEvidence
from plugins.auto_routing.auto_routing.runtime_resolver import (
    AutoRoutingRuntimeResolver,
)
from plugins.auto_routing.auto_routing.service import AutoRoutingService
from plugins.auto_routing.auto_routing.storage import RoutingStore
from run_agent import AIAgent

from _stage2_test_support import (
    LoopbackProvider,
    install_runtime_resolver,
    plugin_manifest,
)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _LoopbackRuntimeAdapter:
    def __init__(self, base_url: str, *, api_key: str = "SELECTED_KEY") -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.now = _timestamp()
        economics = AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=0.10,
            metered_output_usd_per_million_tokens=0.20,
            effective_marginal_cost_usd_per_task=0.001,
            effective_amortized_cost_usd_per_task=0.001,
            evidence_ttl_seconds=3_600,
            source_id="stage2-loopback-economics",
            provenance="test-contract",
            observed_at=self.now,
        )
        self.row = ProviderInventoryRow(
            provider="openrouter",
            resolver_name="openrouter",
            models=("selected-model",),
            authenticated=True,
            live_attempt_status="succeeded",
            model_provenance={"selected-model": "authenticated_live"},
            provenance_details={
                "selected-model": {
                    "endpoint_identity": "endpoint:selected",
                    "auth_identity": "api-key:selected",
                    "observed_at": self.now,
                }
            },
            auth_identity="api-key:selected",
            credential_pool_identity="pool:selected",
            endpoint_identity="endpoint:selected",
            credential_fingerprint="fingerprint:selected",
            api_mode="chat_completions",
            capabilities={
                "selected-model": {
                    "supports_tools": True,
                    "input_modalities": ["text"],
                    "context_window": 128_000,
                    "max_output_tokens": 16_384,
                    "reasoning_options": ["low", "medium", "high"],
                }
            },
            economics={"selected-model": economics},
            observed_at=self.now,
            source="stage2-loopback",
        )

    def inventory(self, refresh: bool = False) -> AdapterInventory:
        del refresh
        return AdapterInventory(provider_rows=(self.row,), local_rows=())

    def resolve(self, runtime_key) -> ResolvedRuntime:
        return ResolvedRuntime(
            runtime_key=runtime_key,
            resolver_name="openrouter",
            provider="openrouter",
            api_mode="chat_completions",
            source="stage2-loopback",
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
            "contract": "stage2-loopback-v1",
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
            credential_pool=None,
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
    def __init__(self, now: str, runtime_id: str) -> None:
        self.now = now
        self.runtime_id = runtime_id

    def load(self):
        common = {
            "source_id": "stage2-loopback-catalog",
            "source_url": "https://catalog.invalid/stage2-loopback",
            "retrieved_at": self.now,
            "published_at": self.now,
            "model": "selected-model",
            "model_version": "selected-model",
            "domain": "coding",
            "task_definition": "default",
            "sample_size": 100,
            "confidence": 0.95,
        }
        return tuple(
            CatalogRecord(
                evidence=CatalogEvidence(
                    **common,
                    metric_name=name,
                    metric_direction=direction,
                    metric_scale="seconds" if name == "latency" else "unit_interval",
                    value=value,
                    normalization_method=(
                        "divide_by_limit" if name == "latency" else "identity"
                    ),
                ),
                canonical_provider="openrouter",
                canonical_model="selected-model",
                canonical_version="selected-model",
                runtime_id=self.runtime_id,
            )
            for name, direction, value in (
                ("quality", "higher_is_better", 0.9),
                ("reliability", "higher_is_better", 0.95),
                ("latency", "lower_is_better", 1.0),
            )
        )


def _authority(mode: str) -> dict[str, Any]:
    authority = json.loads(
        (Path(__file__).with_name("fixtures") / "approved_proposal.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = authority["profiles"]["coding"]["primary"]["runtime"]
    runtime.update(
        {
            "provider": "openrouter",
            "model": "selected-model",
            "auth_identity": "api-key:selected",
            "credential_pool_identity": "pool:selected",
            "endpoint_identity": "endpoint:selected",
            "api_mode": "chat_completions",
            "inventory_revision": "inventory-stage2",
        }
    )
    authority["activation"]["mode"] = mode
    authority["safe_default"] = json.loads(
        json.dumps(authority["profiles"]["coding"]["primary"])
    )
    authority["profiles"]["coding"]["fallbacks"] = []
    authority["llm"]["allowed_providers"] = ["openrouter"]
    authority["llm"]["allowed_models"] = ["selected-model"]
    authority["classifier"]["provider"] = "openrouter"
    authority["classifier"]["model"] = "selected-model"
    authority["rules"] = [
        {
            "rule_id": "stage2-complete-coding",
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


def _seed_service(
    *,
    root: Path,
    home: Path,
    adapter: _LoopbackRuntimeAdapter,
    mode: str,
) -> tuple[AutoRoutingService, AutoRoutingRuntimeResolver]:
    manager = PluginManager()
    context = PluginContext(plugin_manifest(root), manager)
    service = AutoRoutingService(
        plugin_context=context,
        hermes_home=home,
        store=RoutingStore.open(home=home),
        adapter=adapter,
        _pinned_config_path=home / "config.yaml",
    )
    authority = _authority("shadow" if mode == "active" else mode)
    service.config_path.write_text(
        json.dumps({"plugins": {"entries": {"auto-routing": authority}}}),
        encoding="utf-8",
    )
    config = parse_config(
        {"plugins": {"entries": {"auto-routing": authority}}}
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
    selected = next(
        runtime for runtime in inventory.runtimes if runtime.key.model == "selected-model"
    )
    CatalogService(store=service.store).refresh(
        [_CatalogSource(adapter.now, selected.key.stable_id())]
    )
    resolver = AutoRoutingRuntimeResolver(
        plugin_context=context,
        home_resolver=lambda: home,
        service_factory=lambda: service,
    )
    context.register_agent_runtime_resolver(resolver)
    if mode == "active":
        preview = service.preview_activation("active")
        assert preview["doctor"]["healthy"] is True, preview["doctor"]
        applied = service.apply_activation(
            "active",
            expected_config_sha256=preview["expected_config_sha256"],
        )
        assert applied["applied"] is True
        assert applied["activation_receipt_id"]
    return service, resolver


def _context(session_id: str, task: str | None, *, is_resume: bool = False):
    return AgentRuntimeContext(
        scope="fresh_session",
        task=task,
        session_id=session_id,
        task_id=f"task-{session_id}",
        is_resume=is_resume,
        metadata={"platform": "cli"},
    )


def _run_agent(
    *,
    baseline_url: str,
    context: AgentRuntimeContext,
    prompt: str,
    history: list[dict[str, Any]] | None = None,
    credential_pool: Any = None,
    service_tier: str | None = None,
    request_overrides: dict[str, Any] | None = None,
    enabled_toolsets: list[str] | None = None,
    baseline_api_key: str = "BASELINE_KEY",
) -> tuple[AIAgent, dict[str, Any]]:
    agent = AIAgent(
        api_key=baseline_api_key,
        base_url=baseline_url,
        provider="openrouter",
        model="baseline-model",
        max_iterations=4,
        enabled_toolsets=[] if enabled_toolsets is None else enabled_toolsets,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        save_trajectories=False,
        platform="cli",
        session_id=context.session_id,
        runtime_routing_context=context,
        credential_pool=credential_pool,
        service_tier=service_tier,
        request_overrides=request_overrides,
        fallback_model={
            "provider": "openrouter",
            "model": "global-fallback-must-not-run",
        },
    )
    try:
        result = agent.run_conversation(
            prompt,
            conversation_history=history or [],
            task_id=context.task_id,
        )
    except BaseException:
        agent.close()
        raise
    return agent, result


def test_active_decision_is_committed_before_selected_handler_and_baseline_is_idle(
    isolated_home: Path,
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[3]
    session_id = "stage2-fresh-loopback"
    observed: list[str] = []

    def assert_committed_at_handler_entry() -> None:
        with RoutingStore.open(home=isolated_home) as store:
            decision = store.read_session_decision(session_id)
            assert decision is not None
            assert decision.projection_mode == "active"
            assert decision.selected_runtime.model == "selected-model"
            assert decision.profile_adaptive_revision_id is not None
            assert decision.adaptive_profile_snapshot == {
                "coding": decision.profile_adaptive_revision_id
            }
            assert decision.adaptive_assignment_id is None
            observed.append(decision.decision_id)

    with (
        LoopbackProvider(response_text="baseline") as baseline,
        LoopbackProvider(
            response_text="selected",
            on_request_entry=assert_committed_at_handler_entry,
        ) as selected,
    ):
        service, resolver = _seed_service(
            root=root,
            home=isolated_home,
            adapter=_LoopbackRuntimeAdapter(selected.base_url),
            mode="active",
        )
        install_runtime_resolver(monkeypatch, resolver)
        try:
            agent, result = _run_agent(
                baseline_url=baseline.base_url,
                context=_context(session_id, "route this fresh task"),
                prompt="route this fresh task",
            )
            try:
                assert result["final_response"].strip() == "selected"
                assert agent.model == "selected-model"
                assert agent.base_url == selected.base_url
                assert agent._runtime_fallback_authority == "plugin"
                assert agent._fallback_chain == []
            finally:
                agent.close()
        finally:
            resolver.close()
            service.store.close()

    assert selected.entry_errors == []
    assert len(observed) == len(selected.requests) >= 1
    chat_requests = [request for request in selected.requests if "messages" in request]
    assert len(chat_requests) == 1
    assert chat_requests[0]["model"] == "selected-model"
    assert baseline.requests == []
