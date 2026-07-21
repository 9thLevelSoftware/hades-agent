"""Stage 1 advisor CLI, write-class, and guarded apply contracts."""

from __future__ import annotations

import argparse
import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from plugins.auto_routing.auto_routing import advisor as advisor_module
from plugins.auto_routing.auto_routing import cli as cli_module
from plugins.auto_routing.auto_routing.adapters.base import (
    AccessVerification,
    AdapterInventory,
    LocalInventoryRow,
    ProviderInventoryRow,
    ResolvedRuntime,
)
from plugins.auto_routing.auto_routing.config import parse_config
from plugins.auto_routing.auto_routing.config_io import preview_update
from plugins.auto_routing.auto_routing.models import (
    AccessEconomics,
    AdaptiveRevision,
    RuntimeKey,
    RuntimeObservation,
)
from plugins.auto_routing.auto_routing.service import AutoRoutingService
from plugins.auto_routing.auto_routing.storage import RoutingStore
from utils import fast_safe_load


class SimulatedProcessDeath(BaseException):
    pass


class FaultInjector:
    def __init__(self) -> None:
        self.fail_publish = False
        self.fail_after_publish = False
        self.crash_after_replace = False
        self.crash_phase: str | None = None

    def fail_next_baseline_publish(self) -> None:
        self.fail_publish = True

    def crash_after_yaml_replace(self) -> None:
        self.crash_after_replace = True

    def fail_after_baseline_publish(self) -> None:
        self.fail_after_publish = True

    def crash_after_phase(self, phase: str) -> None:
        self.crash_phase = phase

    def after_apply_prepared(self) -> None:
        if self.crash_phase == "prepared":
            self.crash_phase = None
            raise SimulatedProcessDeath

    def before_baseline_publish(self) -> None:
        if self.fail_publish:
            self.fail_publish = False
            raise RuntimeError("simulated baseline publication failure")

    def after_yaml_replace(self) -> None:
        if self.crash_after_replace or self.crash_phase == "yaml_replaced":
            self.crash_after_replace = False
            self.crash_phase = None
            raise SimulatedProcessDeath

    def after_baseline_publish(self) -> None:
        if self.crash_phase == "db_published":
            self.crash_phase = None
            raise SimulatedProcessDeath
        if self.fail_after_publish:
            self.fail_after_publish = False
            raise RuntimeError("simulated post-publication verification failure")

    def after_apply_complete(self) -> None:
        if self.crash_phase == "complete":
            self.crash_phase = None
            raise SimulatedProcessDeath


class ProbeAdapter:
    def __init__(self) -> None:
        self.request_count = 0
        self._key = None
        self.observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def inventory(self, refresh: bool = False) -> AdapterInventory:
        del refresh
        observed_at = self.observed_at
        economics = AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=1.0,
            metered_output_usd_per_million_tokens=2.0,
            source_id="test-metered-prices",
            evidence_ttl_seconds=3600,
            provenance="local-test-provider",
            confidence=1.0,
            observed_at=observed_at,
        )
        return AdapterInventory(
            provider_rows=(
                ProviderInventoryRow(
                    provider="test-provider",
                    resolver_name="test-provider",
                    models=("test-model",),
                    authenticated=True,
                    live_attempt_status="not_attempted",
                    model_provenance={"test-model": None},
                    provenance_details={"test-model": {}},
                    auth_identity="api-key:test",
                    credential_pool_identity="pool:test",
                    endpoint_identity="endpoint:test",
                    credential_fingerprint="credential:test",
                    api_mode="chat_completions",
                    capabilities={"test-model": {"supports_tools": True}},
                    economics={"test-model": economics},
                    observed_at=observed_at,
                ),
            ),
            local_rows=(),
        )

    def resolve(self, runtime_key):
        self._key = runtime_key
        return ResolvedRuntime(
            runtime_key=runtime_key,
            resolver_name="test-provider",
            provider="test-provider",
            api_mode="chat_completions",
            source="test",
            base_url="http://127.0.0.1:9/v1",
            api_key="test-key",
        )

    def verify_access(self, resolved_runtime, request):
        self.request_count += 1
        return AccessVerification(
            runtime_key=resolved_runtime.runtime_key,
            sentinel="AUTO_ROUTING_ACCESS_OK",
            response_model=resolved_runtime.runtime_key.model,
            input_tokens=min(4, request.maximum_input_tokens),
            output_tokens=1,
            actual_cost_usd=0.00001,
            response_hash="response:test",
        )


class PlanningAdapter(ProbeAdapter):
    def inventory(self, refresh: bool = False) -> AdapterInventory:
        del refresh
        economics = AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=1.0,
            metered_output_usd_per_million_tokens=2.0,
            source_id="planning-metered-prices",
            evidence_ttl_seconds=3600,
            provenance="planning-provider",
            confidence=1.0,
            observed_at=self.observed_at,
        )
        models = ("test-model-a", "test-model-b")
        details = {
            model: {
                "endpoint_identity": "endpoint:test",
                "auth_identity": "api-key:test",
                "observed_at": self.observed_at,
            }
            for model in models
        }
        return AdapterInventory(
            provider_rows=(
                ProviderInventoryRow(
                    provider="test-provider",
                    resolver_name="test-provider",
                    models=models,
                    authenticated=True,
                    live_attempt_status="succeeded",
                    model_provenance={model: "authenticated_live" for model in models},
                    provenance_details=details,
                    auth_identity="api-key:test",
                    credential_pool_identity="pool:test",
                    endpoint_identity="endpoint:test",
                    credential_fingerprint="credential:test",
                    api_mode="chat_completions",
                    capabilities={
                        model: {
                            "supports_tools": True,
                            "supports_structured_output": True,
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                            "context_window": 1_000_000,
                            "max_output_tokens": 100_000,
                            "supports_reasoning": True,
                            "reasoning_options": [
                                "none",
                                "low",
                                "medium",
                                "high",
                            ],
                        }
                        for model in models
                    },
                    economics={model: economics for model in models},
                    observed_at=self.observed_at,
                ),
            ),
            local_rows=(),
        )


class MixedVerificationPlanningAdapter(PlanningAdapter):
    unverified_model = "configured-unverified-model"
    unavailable_model = "temporarily-unavailable-model"
    moa_model = "moa-ensemble-model"

    def __init__(self) -> None:
        super().__init__()
        self._local_row = LocalPlanningAdapter().inventory().local_rows[0]

    def inventory(self, refresh: bool = False) -> AdapterInventory:
        base = super().inventory(refresh=refresh).provider_rows[0]
        models = (
            *base.models,
            self.unverified_model,
            self.unavailable_model,
            self.moa_model,
        )
        unavailable_economics = base.economics[base.models[0]].model_copy(
            update={
                "throttle_state": "cooldown",
                "cooldown_until": "2999-01-01T00:00:00Z",
            }
        )
        verified_details = {
            "endpoint_identity": base.endpoint_identity,
            "auth_identity": base.auth_identity,
            "observed_at": self.observed_at,
        }
        return AdapterInventory(
            provider_rows=(
                replace(
                    base,
                    models=models,
                    model_provenance={
                        **dict(base.model_provenance),
                        self.unverified_model: None,
                        self.unavailable_model: "authenticated_live",
                        self.moa_model: "authenticated_live",
                    },
                    provenance_details={
                        **dict(base.provenance_details),
                        self.unverified_model: {},
                        self.unavailable_model: verified_details,
                        self.moa_model: verified_details,
                    },
                    capabilities={
                        **dict(base.capabilities),
                        self.unverified_model: dict(
                            base.capabilities[base.models[0]]
                        ),
                        self.unavailable_model: dict(
                            base.capabilities[base.models[0]]
                        ),
                        self.moa_model: {
                            **dict(base.capabilities[base.models[0]]),
                            "is_moa": True,
                        },
                    },
                    economics={
                        **dict(base.economics),
                        self.unverified_model: base.economics[base.models[0]],
                        self.unavailable_model: unavailable_economics,
                        self.moa_model: base.economics[base.models[0]],
                    },
                ),
            ),
            local_rows=(
                replace(
                    self._local_row,
                    model_size_bytes=3,
                    hardware_compatible=False,
                    loaded_healthy=False,
                ),
            ),
        )


class SubscriptionBaselinePlanningAdapter(PlanningAdapter):
    baseline_model = "subscription-baseline"

    def inventory(self, refresh: bool = False) -> AdapterInventory:
        base = super().inventory(refresh=refresh).provider_rows[0]
        models = (*base.models, self.baseline_model)
        subscription = AccessEconomics(
            billing_kind="subscription",
            effective_marginal_cost_usd_per_task=0,
            subscription_plan="test-plan",
            subscription_quota_remaining=10,
            subscription_quota_unit="request",
            subscription_state="active",
            source_id="current-subscription",
            provenance="planning-provider",
            observed_at=self.observed_at,
        )
        return AdapterInventory(
            provider_rows=(
                replace(
                    base,
                    models=models,
                    model_provenance={
                        **dict(base.model_provenance),
                        self.baseline_model: "authenticated_live",
                    },
                    provenance_details={
                        **dict(base.provenance_details),
                        self.baseline_model: {
                            "endpoint_identity": base.endpoint_identity,
                            "auth_identity": base.auth_identity,
                            "observed_at": self.observed_at,
                        },
                    },
                    capabilities={
                        **dict(base.capabilities),
                        self.baseline_model: dict(base.capabilities[base.models[0]]),
                    },
                    economics={
                        **dict(base.economics),
                        self.baseline_model: subscription,
                    },
                ),
            ),
            local_rows=(),
        )

    def resolve_inherited_baseline(self, inventory_revision: str):
        row = self.inventory().provider_rows[0]
        return self.resolve(
            RuntimeKey(
                provider=row.provider,
                model=self.baseline_model,
                auth_identity=row.auth_identity,
                credential_pool_identity=row.credential_pool_identity,
                endpoint_identity=row.endpoint_identity,
                api_mode=row.api_mode,
                inventory_revision=inventory_revision,
            )
        )

    def identify_persisted_inherited_runtime(self, observations, hermes_config):
        del hermes_config
        matches = [
            item
            for item in observations
            if item.key.model == self.baseline_model
        ]
        return matches[0].key if len(matches) == 1 else None


class CapabilityPlanningAdapter(PlanningAdapter):
    def __init__(self, capability_updates: dict[str, Any]) -> None:
        super().__init__()
        self.capability_updates = capability_updates

    def inventory(self, refresh: bool = False) -> AdapterInventory:
        base = super().inventory(refresh=refresh).provider_rows[0]
        return AdapterInventory(
            provider_rows=(
                replace(
                    base,
                    capabilities={
                        model: {
                            **dict(base.capabilities[model]),
                            **self.capability_updates,
                        }
                        for model in base.models
                    },
                ),
            ),
            local_rows=(),
        )


class LocalPlanningAdapter(ProbeAdapter):
    def inventory(self, refresh: bool = False) -> AdapterInventory:
        del refresh
        economics = AccessEconomics(
            billing_kind="local",
            local_energy_cost_usd_per_task=0.01,
            local_compute_cost_usd_per_task=0.0,
            source_id="local-runtime",
            provenance="backend-inspection",
            observed_at=self.observed_at,
            evidence_ttl_seconds=3600,
        )
        return AdapterInventory(
            provider_rows=(),
            local_rows=(
                LocalInventoryRow(
                    provider="local-test",
                    resolver_name="local-test",
                    model="local-model",
                    backend_identity="ollama:test",
                    reachable=True,
                    installed=True,
                    open_weights=True,
                    license_id="mit",
                    model_size_bytes=1,
                    available_ram_bytes=2,
                    available_vram_bytes=0,
                    loaded_healthy=True,
                    hardware_compatible=True,
                    api_mode="chat_completions",
                    capabilities={
                        "supports_tools": True,
                        "supports_structured_output": True,
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "context_window": 1_000_000,
                        "max_output_tokens": 100_000,
                        "reasoning_options": ["low", "medium", "high"],
                    },
                    economics=economics,
                    observed_at=self.observed_at,
                ),
            ),
        )

    def resolve_inherited_baseline(self, inventory_revision: str):
        if getattr(self, "inherited_available", True) is False:
            return None
        row = self.inventory().local_rows[0]
        return self.resolve(
            RuntimeKey(
                provider=row.provider,
                model=row.model,
                auth_identity=f"local:{row.backend_identity}",
                endpoint_identity=f"local-backend:{row.backend_identity}",
                api_mode=row.api_mode,
                local_backend=row.backend_identity,
                inventory_revision=inventory_revision,
            )
        )

    def identify_persisted_inherited_runtime(self, observations, hermes_config):
        del hermes_config
        if getattr(self, "inherited_available", True) is False:
            return None
        matches = [
            item for item in observations if item.key.model == "local-model"
        ]
        return matches[0].key if len(matches) == 1 else None


class BrokenInventoryAdapter(ProbeAdapter):
    def inventory(self, refresh: bool = False) -> AdapterInventory:
        del refresh
        raise RuntimeError("inventory unavailable")


def _proposal_payload() -> dict[str, Any]:
    return {
        "llm": {
            "allow_provider_override": True,
            "allowed_providers": ["openai-codex"],
            "allow_model_override": True,
            "allowed_models": ["gpt-5.4-mini"],
        },
        "activation": {"mode": "shadow"},
        "scopes": {"fresh_sessions": True, "delegation": True},
        "classifier": {
            "provider": "openai-codex",
            "model": "gpt-5.4-mini",
            "reasoning_effort": "low",
            "timeout_seconds": 15,
            "disclosure": "full",
        },
        "safe_default": "inherit",
        "policy": {
            "eligible_sources": ["configured_providers", "installed_local_models"],
            "uninstalled_local_models": "deny",
            "local_models": {
                "require_open_weights": True,
                "require_compatible_hardware": True,
            },
            "denied_providers": [],
            "denied_models": [],
            "max_estimated_task_cost_usd": 2.0,
            "max_estimated_latency_seconds": 120.0,
            "max_routing_overhead_usd_per_day": 1.0,
            "max_experiment_cost_usd_per_day": 2.0,
            "max_evaluator_calls_per_day": 20,
            "max_canary_fraction": 0.05,
            "max_reasoning_effort": "high",
            "allow_subscription": True,
            "allow_paid_access_probes": False,
            "allowed_licenses": [],
            "minimum_context_tokens": 0,
            "canary_high_risk_tasks": False,
        },
        "adaptation": {
            "enabled": True,
            "mode": "autonomous",
            "canary_fraction": 0.05,
            "minimum_canary_samples": 20,
            "rollback_threshold": 0.10,
        },
        "profiles": {
            "coding": {
                "profile_id": "coding",
                "description": "Tool-using software development tasks",
                "base_rank": 70,
                "match": {
                    "domains": ["coding"],
                    "complexity": ["moderate"],
                    "modalities": ["text"],
                    "capabilities": ["tools"],
                },
                "objectives": {
                    "quality": 4,
                    "reliability": 3,
                    "latency": 2,
                    "cost": 1,
                },
                "primary": {
                    "runtime": {
                        "provider": "openai-codex",
                        "model": "gpt-5.4",
                        "auth_identity": "subscription:default",
                        "credential_pool_identity": "pool:codex",
                        "endpoint_identity": "endpoint:codex",
                        "api_mode": "codex_responses",
                        "local_backend": "",
                        "inventory_revision": "inventory-1",
                    },
                    "reasoning": {"default": "medium", "min": "low", "max": "high"},
                    "supported_reasoning_efforts": [],
                    "revision_status": "active",
                },
                "fallbacks": [],
                "provenance": [],
            }
        },
        "economics_overrides": {},
    }


class CliRunner:
    def __init__(self, service: AutoRoutingService):
        self.service = service

    def run(self, *arguments: str) -> dict[str, Any]:
        parser = argparse.ArgumentParser()
        cli_module.build_parser(parser)
        args = parser.parse_args(list(arguments))
        result = cli_module.execute(args, service=self.service)
        assert isinstance(result.payload, dict)
        return {**result.payload, "exit_code": result.exit_code}


def _advisor_interview(name: str = "complete_request") -> dict[str, Any]:
    fixture = Path(__file__).with_name("fixtures") / "advisor_interview.json"
    return json.loads(fixture.read_text(encoding="utf-8"))[name]


def _bind_profile_runtime_ids(
    request: dict[str, Any],
    service: AutoRoutingService,
) -> dict[str, str]:
    runtime_ids = {
        runtime["model"]: runtime["runtime_id"]
        for runtime in service.inventory(
            refresh=False,
            include_ineligible=True,
        )["runtimes"]
    }
    model_a = runtime_ids["test-model-a"]
    model_b = runtime_ids["test-model-b"]
    for index, profile in enumerate(request["profiles"]):
        primary, fallback = (
            (model_a, model_b)
            if index == 0
            else (model_b, model_a)
        )
        profile["access_paths"] = {
            "primary_runtime_id": primary,
            "fallback_runtime_ids": [fallback],
        }
        if index == 0:
            profile["reasoning_bounds"] = {
                model_a: {"default": "medium", "min": "low", "max": "high"},
                model_b: {"default": "low", "min": "none", "max": "medium"},
            }
        else:
            profile["reasoning_bounds"] = {
                model_b: {"default": "low", "min": "none", "max": "medium"},
                model_a: {"default": "medium", "min": "low", "max": "medium"},
            }
    return runtime_ids


def _refresh_profile_ranking_catalog(
    service: AutoRoutingService,
    isolated_home: Path,
) -> dict[str, str]:
    inventory = service.inventory(refresh=False, include_ineligible=True)
    runtime_ids = {
        runtime["model"]: runtime["runtime_id"]
        for runtime in inventory["runtimes"]
    }
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    values = {
        "test-model-a": {"quality": 0.95, "reliability": 0.90, "latency": 8.0},
        "test-model-b": {"quality": 0.65, "reliability": 0.70, "latency": 1.0},
        MixedVerificationPlanningAdapter.unverified_model: {
            "quality": 1.0,
            "reliability": 1.0,
            "latency": 0.1,
        },
        MixedVerificationPlanningAdapter.unavailable_model: {
            "quality": 1.0,
            "reliability": 1.0,
            "latency": 0.1,
        },
        MixedVerificationPlanningAdapter.moa_model: {
            "quality": 1.0,
            "reliability": 1.0,
            "latency": 0.1,
        },
        "local-model": {"quality": 1.0, "reliability": 1.0, "latency": 0.1},
    }
    rows: list[dict[str, Any]] = []
    for model, runtime_id in runtime_ids.items():
        for metric_name, value in values[model].items():
            rows.append(
                {
                    "canonical_provider": "test-provider",
                    "canonical_model": model,
                    "canonical_version": model,
                    "runtime_id": runtime_id,
                    "source_id": f"{metric_name}-{model}",
                    "source_url": f"https://example.com/{model}/{metric_name}",
                    "retrieved_at": observed_at,
                    "published_at": observed_at,
                    "model": model,
                    "model_version": model,
                    "domain": "coding",
                    "task_definition": "debug a service",
                    "metric_name": metric_name,
                    "metric_direction": (
                        "lower_is_better"
                        if metric_name == "latency"
                        else "higher_is_better"
                    ),
                    "metric_scale": (
                        "seconds" if metric_name == "latency" else "unit_interval"
                    ),
                    "value": value,
                    "sample_size": 100,
                    "confidence": 0.9,
                    "normalization_method": (
                        "divide_by_limit"
                        if metric_name == "latency"
                        else "identity"
                    ),
                }
            )
    catalog_path = isolated_home / "profile-rankings-catalog.json"
    catalog_path.write_text(json.dumps(rows), encoding="utf-8")
    refreshed = service.refresh_catalog(
        models_dev=False,
        hermes=False,
        files=[str(catalog_path)],
    )
    assert refreshed["record_count"] == len(rows)
    return runtime_ids


@pytest.fixture
def proposal_file(isolated_home: Path) -> Path:
    path = isolated_home / "proposal.json"
    fixture = Path(__file__).with_name("fixtures") / "approved_proposal.json"
    path.write_bytes(fixture.read_bytes())
    return path


@pytest.fixture
def fault_injector() -> FaultInjector:
    return FaultInjector()


@pytest.fixture
def saga_service(plugin_context, isolated_home: Path, fault_injector: FaultInjector):
    return AutoRoutingService.from_plugin_context(
        plugin_context,
        fault_injector=fault_injector,
    )


def test_stage1_commands_publish_explicit_write_classes() -> None:
    assert cli_module.command_metadata("status").write_class.value == "read_only"
    assert cli_module.command_metadata("explain").write_class.value == "read_only"
    assert (
        cli_module.command_metadata("inventory", refresh=True).write_class.value
        == "append_only_observation"
    )
    assert (
        cli_module.command_metadata("refresh-catalog").write_class.value
        == "append_only_observation"
    )
    assert cli_module.command_metadata("setup").write_class.value == "guarded_control_plane"
    assert (
        cli_module.command_metadata("activate").write_class.value
        == "guarded_control_plane"
    )
    assert (
        cli_module.command_metadata("verify-runtime").write_class.value
        == "guarded_control_plane"
    )
    assert (
        cli_module.command_metadata("feedback").write_class.value
        == "append_only_observation"
    )


def test_help_and_json_publish_write_class_for_every_command() -> None:
    parser = argparse.ArgumentParser()
    cli_module.build_parser(parser)
    help_text = parser.format_help()

    expected_write_classes = {
        "setup": "guarded_control_plane",
        "edit": "guarded_control_plane",
        "inventory": "read_only",
        "verify-runtime": "guarded_control_plane",
        "refresh-catalog": "append_only_observation",
        "plan": "read_only",
        "validate": "read_only",
        "activate": "guarded_control_plane",
        "explain": "read_only",
        "feedback": "append_only_observation",
        "report": "read_only",
        "status": "read_only",
        "doctor": "read_only",
        "adapt": "read_only",
    }
    adapt_write_classes = {
        "adapt status": "read_only",
        "adapt history": "read_only",
        "adapt freeze": "guarded_control_plane",
        "adapt unfreeze": "guarded_control_plane",
        "adapt rollback": "guarded_control_plane",
    }
    assert set(expected_write_classes) | set(adapt_write_classes) <= set(
        cli_module._SPEC_BY_NAME
    )
    for command, expected_write_class in expected_write_classes.items():
        metadata = cli_module.command_metadata(command)
        assert command in help_text
        assert metadata.write_class.value in help_text
        assert metadata.write_class.value == expected_write_class
    for command, expected_write_class in adapt_write_classes.items():
        metadata = cli_module.command_metadata(command)
        assert metadata.write_class.value == expected_write_class

    top_subcommands = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    adapt_parser = top_subcommands.choices["adapt"]
    adapt_subcommands = next(
        action
        for action in adapt_parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    for leaf_name, leaf_parser in adapt_subcommands.choices.items():
        metadata = cli_module.command_metadata(f"adapt {leaf_name}")
        assert metadata.write_class.value in leaf_parser.format_help()

    manage_parser = top_subcommands.choices["manage"]
    manage_subcommands = next(
        action
        for action in manage_parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    for leaf_name, leaf_parser in manage_subcommands.choices.items():
        metadata = cli_module.command_metadata(f"manage {leaf_name}")
        assert metadata.write_class.value in leaf_parser.format_help()


def test_feedback_json_usage_errors_preserve_append_only_write_class(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = argparse.ArgumentParser()
    cli_module.build_parser(parser)

    with pytest.raises(SystemExit) as stopped:
        parser.parse_args(["feedback", "--evidence-id", "0" * 64])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert stopped.value.code == 2
    assert captured.err == ""
    assert payload["ok"] is False
    assert payload["command"] == "feedback"
    assert payload["write_class"] == "append_only_observation"


def test_registered_handler_emits_structured_json_and_real_exit_two(
    service: AutoRoutingService,
    proposal_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = argparse.ArgumentParser()
    cli_module.build_parser(parser)
    args = parser.parse_args(
        ["setup", "--proposal", str(proposal_file), "--apply", "--json"]
    )

    with pytest.raises(SystemExit) as stopped:
        cli_module.auto_routing_command(args, service=service)

    payload = json.loads(capsys.readouterr().out)
    assert stopped.value.code == 2
    assert payload["ok"] is False
    assert payload["command"] == "setup"
    assert payload["write_class"] == "guarded_control_plane"


def test_argparse_usage_errors_are_structured_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = argparse.ArgumentParser()
    cli_module.build_parser(parser)

    with pytest.raises(SystemExit) as stopped:
        parser.parse_args(["setup", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert stopped.value.code == 2
    assert captured.err == ""
    assert payload["ok"] is False
    assert payload["command"] == "setup"
    assert payload["write_class"] == "guarded_control_plane"


def test_setup_requires_preview_hash_and_explicit_apply(
    service: AutoRoutingService,
    proposal_file: Path,
) -> None:
    cli = CliRunner(service)

    preview = cli.run("setup", "--proposal", str(proposal_file), "--json")
    assert preview["applied"] is False
    assert len(preview["expected_config_sha256"]) == 64
    assert preview["expected_config_sha256"] == preview["precondition_sha256"]

    applied = cli.run(
        "setup",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--expected-config-sha",
        preview["expected_config_sha256"],
        "--json",
    )
    assert applied["exit_code"] == 0
    assert applied["applied"] is True
    assert applied["activation"]["mode"] == "shadow"
    assert service.store.read_active_revision(applied["authority_id"]).is_baseline


def test_objective_weights_are_materialized_normalized(
    service: AutoRoutingService,
    proposal_file: Path,
) -> None:
    result = CliRunner(service).run("validate", "--proposal", str(proposal_file), "--json")
    weights = result["proposal"]["profiles"]["coding"]["objectives"]
    assert weights == {"quality": 0.4, "reliability": 0.3, "latency": 0.2, "cost": 0.1}
    parse_config({"plugins": {"entries": {"auto-routing": result["proposal"]}}})


def test_golden_advisor_readiness_is_ordered_and_complete() -> None:
    fixture = Path(__file__).with_name("fixtures") / "advisor_interview.json"
    turns = json.loads(fixture.read_text(encoding="utf-8"))
    request_type = advisor_module.AdvisorRequest

    for case in turns["partial_requests"]:
        readiness = request_type.validate_readiness(case["request"])
        assert readiness.ready is False
        assert list(readiness.missing_facts) == case["expected_missing_facts"]

    readiness = request_type.validate_readiness(turns["complete_request"])
    assert readiness.ready is True
    assert readiness.missing_facts == ()


def test_each_profile_requires_complete_authoring_choices() -> None:
    fixture = Path(__file__).with_name("fixtures") / "advisor_interview.json"
    request = json.loads(fixture.read_text(encoding="utf-8"))["complete_request"]
    deep_code = request["profiles"][1]
    deep_code.pop("objectives")
    deep_code.pop("limits")
    deep_code.pop("reasoning_bounds")

    readiness = advisor_module.AdvisorRequest.validate_readiness(request)

    assert "profiles.latency-heavy.objectives" in readiness.missing_facts
    assert "profiles.latency-heavy.limits" in readiness.missing_facts
    assert "profiles.latency-heavy.reasoning_bounds" in readiness.missing_facts
    assert readiness.ready is False


def test_complete_advisor_request_owns_profiles_rules_and_complexity() -> None:
    fixture = Path(__file__).with_name("fixtures") / "advisor_interview.json"
    request = json.loads(fixture.read_text(encoding="utf-8"))["complete_request"]

    parsed = advisor_module.AdvisorRequest.model_validate(request)

    assert parsed.profiles[0].objectives != parsed.profiles[1].objectives
    assert parsed.profiles[0].access_paths != parsed.profiles[1].access_paths
    assert parsed.rules[0].profile_id == "quality-heavy"
    assert parsed.complexity_bands.moderate_max == pytest.approx(0.65)


def test_chat_skill_collects_profile_rules_and_complexity_before_finalizing() -> None:
    skill_path = (
        Path(__file__).parents[3]
        / "plugins"
        / "auto_routing"
        / "skills"
        / "auto-routing"
        / "SKILL.md"
    )
    skill_text = skill_path.read_text(encoding="utf-8").casefold()

    for concept in (
        "complexity bands",
        "profile match",
        "quality",
        "reliability",
        "latency",
        "cost",
        "rules",
    ):
        assert concept in skill_text


def test_rank_profiles_is_available_before_target_finalization(
    plugin_context,
    isolated_home: Path,
) -> None:
    adapter = MixedVerificationPlanningAdapter()
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=adapter,
    )
    request = _advisor_interview()
    request["hard_limits"]["allow_local"] = True
    for profile in request["profiles"]:
        profile.pop("access_paths")
        profile.pop("reasoning_bounds")
    request["explicit_approval"] = False
    secret_prompt = "TASK2A_PRIVATE_PROMPT_SENTINEL"
    request["representative_prompts"] = [secret_prompt]
    runtime_ids = _refresh_profile_ranking_catalog(service, isolated_home)
    before = service.store.connection.total_changes

    result = service.rank_profiles(request)
    repeated = service.rank_profiles(copy.deepcopy(request))

    assert service.store.connection.total_changes == before
    assert adapter.request_count == 0
    assert json.dumps(result, sort_keys=True) == json.dumps(repeated, sort_keys=True)
    assert secret_prompt not in repr(result)
    for path in isolated_home.rglob("*"):
        if path.is_file():
            assert secret_prompt.encode() not in path.read_bytes()
    rankings = result["profile_rankings"]
    assert tuple(rankings) == ("quality-heavy", "latency-heavy")
    assert rankings["quality-heavy"]["runtime_ids"][0] == runtime_ids[
        "test-model-a"
    ]
    assert rankings["latency-heavy"]["runtime_ids"][0] == runtime_ids[
        "test-model-b"
    ]
    accepted_ids = {runtime_ids["test-model-a"], runtime_ids["test-model-b"]}
    excluded_ids = {
        runtime_ids[MixedVerificationPlanningAdapter.unverified_model],
        runtime_ids[MixedVerificationPlanningAdapter.unavailable_model],
        runtime_ids[MixedVerificationPlanningAdapter.moa_model],
        runtime_ids["local-model"],
    }
    for ranking in rankings.values():
        assert set(ranking["runtime_ids"]) == accepted_ids
        assert excluded_ids.isdisjoint(ranking["runtime_ids"])
        assert ranking["candidates"]
        for candidate in ranking["candidates"]:
            assert candidate["inventory_state"] == "verified"
            assert candidate["provenance"]
            assert candidate["source_dates"]
            assert candidate["confidence"] is not None
            assert candidate["uncertainty"] is not None
            assert candidate["staleness_penalty"] == round(
                candidate["staleness_penalty"],
                advisor_module.STALENESS_PENALTY_DECIMALS,
            )
        assert excluded_ids <= set(ranking["rejected_candidates"])
        assert "hardware_compatibility_unproven" in ranking[
            "rejected_candidates"
        ][runtime_ids["local-model"]]["reasons"]
    assert "proposal" not in result


def test_partial_plan_returns_rankings_without_selecting_targets(
    plugin_context,
    isolated_home: Path,
) -> None:
    adapter = MixedVerificationPlanningAdapter()
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=adapter,
    )
    request = _advisor_interview()
    request["hard_limits"]["allow_local"] = True
    for profile in request["profiles"]:
        profile.pop("access_paths")
        profile.pop("reasoning_bounds")
    request["explicit_approval"] = False
    _refresh_profile_ranking_catalog(service, isolated_home)
    request_path = isolated_home / "ranking-only-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    result = CliRunner(service).run(
        "plan",
        "--request",
        str(request_path),
        "--json",
    )

    assert result["exit_code"] == 2
    assert result["ready"] is False
    assert "profiles.quality-heavy.access_paths" in result["missing_facts"]
    assert "profiles.latency-heavy.reasoning_bounds" in result["missing_facts"]
    assert result["profile_rankings"]
    assert adapter.request_count == 0
    assert "proposal" not in result
    assert "targets" not in result


def test_profile_rankings_ignore_base_rank_and_selected_access_order(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=PlanningAdapter(),
    )
    request = _advisor_interview()
    _refresh_profile_ranking_catalog(service, isolated_home)
    _bind_profile_runtime_ids(request, service)
    baseline = service.rank_profiles(request)["profile_rankings"]

    changed = copy.deepcopy(request)
    changed["profiles"][0]["base_rank"] = -1_000_000
    access = changed["profiles"][0]["access_paths"]
    primary = access["primary_runtime_id"]
    access["primary_runtime_id"] = access["fallback_runtime_ids"][0]
    access["fallback_runtime_ids"] = [primary]
    changed_ranking = service.rank_profiles(changed)["profile_rankings"]

    assert changed_ranking["quality-heavy"]["runtime_ids"] == baseline[
        "quality-heavy"
    ]["runtime_ids"]
    assert changed_ranking["quality-heavy"]["candidates"] == baseline[
        "quality-heavy"
    ]["candidates"]


def test_profile_specific_limit_rejects_only_for_that_profile(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=PlanningAdapter(),
    )
    request = _advisor_interview()
    for profile in request["profiles"]:
        profile.pop("access_paths")
        profile.pop("reasoning_bounds")
    request["profiles"][1]["limits"]["max_estimated_latency_seconds"] = 2.0
    runtime_ids = _refresh_profile_ranking_catalog(service, isolated_home)

    rankings = service.rank_profiles(request)["profile_rankings"]

    model_a = runtime_ids["test-model-a"]
    assert model_a in rankings["quality-heavy"]["runtime_ids"]
    assert model_a not in rankings["latency-heavy"]["runtime_ids"]
    assert "estimated_latency_exceeds_limit" in rankings["latency-heavy"][
        "rejected_candidates"
    ][model_a]["reasons"]


def test_profile_match_capabilities_affect_only_that_profile_ranking(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=PlanningAdapter(),
    )
    request = _advisor_interview()
    request["required_capabilities"] = []
    for profile in request["profiles"]:
        profile.pop("access_paths")
        profile.pop("reasoning_bounds")
    request["profiles"][0]["match"]["capabilities"] = ["structured_output"]
    request["profiles"][1]["match"]["capabilities"] = ["tools"]
    runtime_ids = _refresh_profile_ranking_catalog(service, isolated_home)
    model_b = runtime_ids["test-model-b"]
    runtime_b = next(
        runtime
        for runtime in service.adapter.inventory().provider_rows[0].models
        if runtime == "test-model-b"
    )
    assert runtime_b == "test-model-b"

    original_inventory = service.adapter.inventory

    def inventory_without_structured_output(refresh: bool = False) -> AdapterInventory:
        inventory = original_inventory(refresh=refresh)
        row = inventory.provider_rows[0]
        capabilities = {
            model: dict(values) for model, values in row.capabilities.items()
        }
        capabilities["test-model-b"]["supports_structured_output"] = False
        return replace(
            inventory,
            provider_rows=(replace(row, capabilities=capabilities),),
        )

    service.adapter.inventory = inventory_without_structured_output

    rankings = service.rank_profiles(request)["profile_rankings"]

    assert model_b not in rankings["quality-heavy"]["runtime_ids"]
    assert "required_capability_unsupported:structured_output" in rankings[
        "quality-heavy"
    ]["rejected_candidates"][model_b]["reasons"]
    assert model_b in rankings["latency-heavy"]["runtime_ids"]


def test_complete_plan_builds_distinct_profile_authority_and_rules(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=MixedVerificationPlanningAdapter(),
    )
    request = _advisor_interview()
    request["hard_limits"]["allow_local"] = True
    runtime_ids = _refresh_profile_ranking_catalog(service, isolated_home)
    _bind_profile_runtime_ids(request, service)
    request["rules"].reverse()
    request_path = isolated_home / "distinct-profiles-request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    result = CliRunner(service).run(
        "plan",
        "--request",
        str(request_path),
        "--json",
    )

    assert result["exit_code"] == 0, result
    proposal = result["proposal"]
    profiles = proposal["profiles"]
    assert profiles["quality-heavy"]["primary"]["runtime"]["model"] == (
        "test-model-a"
    )
    assert profiles["latency-heavy"]["primary"]["runtime"]["model"] == (
        "test-model-b"
    )
    assert profiles["quality-heavy"]["objectives"] != profiles[
        "latency-heavy"
    ]["objectives"]
    assert profiles["quality-heavy"]["match"] != profiles["latency-heavy"][
        "match"
    ]
    assert profiles["latency-heavy"]["limits"] == {
        "max_estimated_task_cost_usd": None,
        "max_estimated_latency_seconds": 10.0,
        "max_reasoning_effort": "medium",
        "allowed_licenses": None,
        "minimum_context_tokens": None,
        "canary_high_risk_tasks": None,
    }
    for profile in profiles.values():
        assert profile["primary"]["revision_status"] == "active"
        assert all(
            fallback["revision_status"] == "fallback"
            for fallback in profile["fallbacks"]
        )
    assert [rule["rule_id"] for rule in proposal["rules"]] == [
        "declared-coding",
        "interactive-cli",
    ]
    assert proposal["complexity_bands"] == request["complexity_bands"]
    assert proposal["routing_vocabulary"] == request["routing_vocabulary"]
    assert result["profile_rankings"]["quality-heavy"]["runtime_ids"][0] == (
        runtime_ids["test-model-a"]
    )
    assert result["profile_rankings"]["latency-heavy"]["runtime_ids"][0] == (
        runtime_ids["test-model-b"]
    )


def test_complete_plan_preserves_deliberate_verified_non_top_selection(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=PlanningAdapter(),
    )
    request = _advisor_interview()
    runtime_ids = _refresh_profile_ranking_catalog(service, isolated_home)
    _bind_profile_runtime_ids(request, service)
    quality = request["profiles"][0]
    quality["access_paths"] = {
        "primary_runtime_id": runtime_ids["test-model-b"],
        "fallback_runtime_ids": [runtime_ids["test-model-a"]],
    }
    request_path = isolated_home / "deliberate-non-top-selection.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    result = CliRunner(service).run(
        "plan",
        "--request",
        str(request_path),
        "--json",
    )

    assert result["exit_code"] == 0, result
    assert result["profile_rankings"]["quality-heavy"]["runtime_ids"][0] == (
        runtime_ids["test-model-a"]
    )
    assert result["proposal"]["profiles"]["quality-heavy"]["primary"][
        "runtime"
    ]["model"] == "test-model-b"


def test_advisor_rules_and_profile_limits_are_same_request_authority() -> None:
    dangling = _advisor_interview()
    dangling["rules"][0]["profile_id"] = "missing-profile"
    with pytest.raises(ValidationError, match="missing-profile"):
        advisor_module.AdvisorRequest.model_validate(dangling)

    loosening = _advisor_interview()
    loosening["profiles"][0]["limits"]["max_estimated_task_cost_usd"] = 1.01
    with pytest.raises(ValidationError, match="loosen.*max_estimated_task_cost"):
        advisor_module.AdvisorRequest.model_validate(loosening)

    malformed = _advisor_interview()
    malformed["profiles"] = [1]
    with pytest.raises(ValueError, match="profile must be a mapping"):
        advisor_module.AdvisorRequest.ranking_request(malformed)


def test_legacy_single_profile_upgrades_but_cloning_and_hybrids_are_rejected() -> None:
    legacy = _advisor_interview("legacy_complete_request")
    upgraded = advisor_module.AdvisorRequest.model_validate(legacy)
    assert len(upgraded.profiles) == 1
    assert upgraded.profiles[0].profile_id == "coding"
    assert upgraded.profiles[0].access_paths.primary_runtime_id
    assert upgraded.profiles[0].objectives.quality == pytest.approx(0.4)
    assert upgraded.rules == ()
    assert upgraded.complexity_bands.moderate_max == pytest.approx(0.7)

    cloned = _advisor_interview("legacy_complete_request")
    cloned["profiles"].append(
        {"profile_id": "second", "description": "Second", "base_rank": 5.0}
    )
    with pytest.raises(ValidationError, match="single-profile legacy"):
        advisor_module.AdvisorRequest.model_validate(cloned)

    hybrid = _advisor_interview()
    hybrid_legacy = _advisor_interview("legacy_complete_request")
    for field in ("objectives", "access_paths", "reasoning_bounds"):
        hybrid[field] = hybrid_legacy[field]
    with pytest.raises(ValidationError, match="ambiguous legacy/new"):
        advisor_module.AdvisorRequest.model_validate(hybrid)


def test_legacy_plan_materializes_one_complete_explicit_profile(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=PlanningAdapter(),
    )
    legacy = _advisor_interview("legacy_complete_request")
    runtime_ids = _refresh_profile_ranking_catalog(service, isolated_home)
    model_a = runtime_ids["test-model-a"]
    model_b = runtime_ids["test-model-b"]
    legacy["access_paths"] = {
        "primary_runtime_id": model_a,
        "fallback_runtime_ids": [model_b],
    }
    legacy["reasoning_bounds"] = {
        model_a: {"default": "medium", "min": "low", "max": "high"},
        model_b: {"default": "low", "min": "none", "max": "medium"},
    }
    request_path = isolated_home / "legacy-upgrade-request.json"
    request_path.write_text(json.dumps(legacy), encoding="utf-8")

    result = CliRunner(service).run(
        "plan",
        "--request",
        str(request_path),
        "--json",
    )

    assert result["exit_code"] == 0, result
    proposal = result["proposal"]
    assert tuple(proposal["profiles"]) == ("coding",)
    profile = proposal["profiles"]["coding"]
    assert set(profile) >= {
        "profile_id",
        "match",
        "objectives",
        "limits",
        "primary",
        "fallbacks",
    }
    assert proposal["rules"] == []
    assert proposal["complexity_bands"]
    assert proposal["routing_vocabulary"]
    assert profile["match"] == {
        "domains": ["coding"],
        "complexity": [],
        "modalities": ["text"],
        "capabilities": ["structured_output", "tools"],
    }
    parsed_proposal = parse_config(
        {"plugins": {"entries": {"auto-routing": proposal}}}
    )
    preview = preview_update(parsed_proposal, path=service.config_path)
    preview_root = fast_safe_load(preview.after_bytes)
    assert preview_root["plugins"]["entries"]["auto-routing"]["profiles"][
        "coding"
    ]["limits"] is None


@pytest.mark.parametrize("profile_id", ["Coding Profile", "p" * 256])
def test_advisor_profile_identifier_round_trips_to_proposal_and_rule(
    plugin_context,
    isolated_home: Path,
    profile_id: str,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=PlanningAdapter(),
    )
    request = _advisor_interview()
    _refresh_profile_ranking_catalog(service, isolated_home)
    _bind_profile_runtime_ids(request, service)
    old_profile_id = request["profiles"][0]["profile_id"]
    request["profiles"][0]["profile_id"] = profile_id
    for rule in request["rules"]:
        if rule["profile_id"] == old_profile_id:
            rule["profile_id"] = profile_id
    request_path = isolated_home / f"profile-id-{len(profile_id)}.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    result = CliRunner(service).run(
        "plan",
        "--request",
        str(request_path),
        "--json",
    )

    assert result["exit_code"] == 0, result
    assert result["proposal"]["profiles"][profile_id]["profile_id"] == profile_id
    assert result["proposal"]["rules"][0]["profile_id"] == profile_id


def test_plan_help_describes_profile_comparison_before_selection() -> None:
    help_text = cli_module.command_metadata("plan").help.casefold()

    for concept in ("profile", "compare", "rule"):
        assert concept in help_text


def test_guarded_apply_flag_pairing_is_fail_closed(
    service: AutoRoutingService,
    proposal_file: Path,
) -> None:
    result = CliRunner(service).run(
        "setup",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--json",
    )
    assert result["exit_code"] == 2
    assert result["write_class"] == "guarded_control_plane"
    assert not service.config_path.exists()


def test_setup_db_failure_restores_yaml_and_leaves_no_half_authority(
    saga_service: AutoRoutingService,
    proposal_file: Path,
    fault_injector: FaultInjector,
) -> None:
    saga_service.config_path.write_text("display:\n  skin: mono\n", encoding="utf-8")
    before = saga_service.config_path.read_bytes()
    cli = CliRunner(saga_service)
    preview = cli.run("setup", "--proposal", str(proposal_file), "--json")
    fault_injector.fail_next_baseline_publish()

    result = cli.run(
        "setup",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--expected-config-sha",
        preview["expected_config_sha256"],
        "--json",
    )

    assert result["exit_code"] == 2
    assert saga_service.config_path.read_bytes() == before
    assert saga_service.store.list_authority_revisions() == []
    assert not list(
        saga_service.config_path.parent.glob("auto-routing-apply-*.pending.json")
    )


def test_restart_recovers_crash_after_yaml_replace(
    saga_service: AutoRoutingService,
    proposal_file: Path,
    plugin_context,
    fault_injector: FaultInjector,
) -> None:
    cli = CliRunner(saga_service)
    preview = cli.run("setup", "--proposal", str(proposal_file), "--json")
    fault_injector.crash_after_yaml_replace()

    with pytest.raises(SimulatedProcessDeath):
        cli.run(
            "setup",
            "--proposal",
            str(proposal_file),
            "--apply",
            "--expected-config-sha",
            preview["expected_config_sha256"],
        )

    pending = list(
        saga_service.config_path.parent.glob("auto-routing-apply-*.pending.json")
    )
    assert len(pending) == 1

    restarted = AutoRoutingService.from_plugin_context(plugin_context)
    assert restarted.doctor()["incomplete_config_apply"] is False
    assert restarted.store.read_active_revision(preview["authority_id"]).is_baseline
    assert not list(
        saga_service.config_path.parent.glob("auto-routing-apply-*.pending.json")
    )


def test_post_commit_failure_rolls_back_yaml_and_new_db_authority(
    saga_service: AutoRoutingService,
    proposal_file: Path,
    fault_injector: FaultInjector,
) -> None:
    before = b"display:\n  skin: slate\n"
    saga_service.config_path.write_bytes(before)
    cli = CliRunner(saga_service)
    preview = cli.run("setup", "--proposal", str(proposal_file), "--json")
    fault_injector.fail_after_baseline_publish()

    result = cli.run(
        "setup",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--expected-config-sha",
        preview["expected_config_sha256"],
        "--json",
    )

    assert result["exit_code"] == 2
    assert saga_service.config_path.read_bytes() == before
    assert saga_service.store.list_authority_revisions() == []
    assert saga_service.store.read_active_revision(preview["authority_id"]) is None


def _prepare_identical_byte_apply(
    service: AutoRoutingService,
    proposal_file: Path,
    *,
    rows_preexisting: bool,
) -> tuple[dict[str, Any], AdaptiveRevision, bytes]:
    proposal = service.load_proposal(proposal_file)
    initial = preview_update(proposal, path=service.config_path)
    service.config_path.parent.mkdir(parents=True, exist_ok=True)
    service.config_path.write_bytes(initial.after_bytes)
    preview = service.preview_config(proposal_file)
    assert preview["before_sha256"] == preview["after_sha256"]
    baseline = AdaptiveRevision.model_validate(
        preview["initial_revision"]["document"]
    )
    if rows_preexisting:
        service.store.publish_authority_and_baseline(
            authority_id=preview["authority_id"],
            document=preview["authority"]["document"],
            baseline=baseline,
        )
    return preview, baseline, service.config_path.read_bytes()


@pytest.mark.parametrize(
    "phase",
    ["prepared", "yaml_replaced", "db_published", "complete"],
)
@pytest.mark.parametrize("rows_preexisting", [False, True])
def test_restart_recovers_identical_byte_apply_by_phase_and_database_ownership(
    saga_service: AutoRoutingService,
    proposal_file: Path,
    plugin_context,
    fault_injector: FaultInjector,
    phase: str,
    rows_preexisting: bool,
) -> None:
    preview, baseline, exact_config = _prepare_identical_byte_apply(
        saga_service,
        proposal_file,
        rows_preexisting=rows_preexisting,
    )
    fault_injector.crash_after_phase(phase)

    with pytest.raises(SimulatedProcessDeath):
        saga_service.apply_config(
            proposal_file,
            expected_config_sha256=preview["expected_config_sha256"],
        )

    pending = list(
        saga_service.config_path.parent.glob("auto-routing-apply-*.pending.json")
    )
    assert len(pending) == 1
    journal = json.loads(pending[0].read_text(encoding="utf-8"))
    assert journal["phase"] == phase
    assert journal["config_noop"] is True
    saga_service.store.close()

    restarted = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=ProbeAdapter(),
    )
    should_be_published = rows_preexisting or phase != "prepared"
    try:
        assert restarted._has_incomplete_config_apply() is False
        assert restarted.config_path.read_bytes() == exact_config
        assert not list(
            restarted.config_path.parent.glob("auto-routing-apply-*.pending.json")
        )
        authority = restarted.store.read_authority_revision(
            preview["authority_id"]
        )
        stored_baseline = restarted.store.read_revision(baseline.revision_id)
        active = restarted.store.read_active_revision(preview["authority_id"])
        if should_be_published:
            assert authority is not None
            assert stored_baseline == baseline
            assert active == baseline
        else:
            assert authority is None
            assert stored_baseline is None
            assert active is None
    finally:
        restarted.store.close()

    second_restart = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=ProbeAdapter(),
    )
    try:
        assert second_restart._has_incomplete_config_apply() is False
        counts = tuple(
            second_restart.store.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "authority_revisions",
                "adaptive_revisions",
                "active_adaptive_revisions",
            )
        )
        expected_count = 1 if should_be_published else 0
        assert counts == (expected_count, expected_count, expected_count)
    finally:
        second_restart.store.close()


def test_recovery_refuses_to_overwrite_external_config_change(
    saga_service: AutoRoutingService,
    proposal_file: Path,
    plugin_context,
    fault_injector: FaultInjector,
) -> None:
    cli = CliRunner(saga_service)
    preview = cli.run("setup", "--proposal", str(proposal_file), "--json")
    fault_injector.crash_after_yaml_replace()
    with pytest.raises(SimulatedProcessDeath):
        cli.run(
            "setup",
            "--proposal",
            str(proposal_file),
            "--apply",
            "--expected-config-sha",
            preview["expected_config_sha256"],
        )
    external = b"display:\n  skin: external-edit\n"
    saga_service.config_path.write_bytes(external)

    restarted = AutoRoutingService.from_plugin_context(plugin_context)

    doctor = restarted.doctor()
    assert doctor["incomplete_config_apply"] is True
    assert restarted.status()["activation_mode"] == "off"
    assert restarted.config_path.read_bytes() == external
    assert list(
        restarted.config_path.parent.glob("auto-routing-apply-*.pending.json")
    )


@pytest.mark.parametrize("source_existed", [False, True])
def test_recovery_distinguishes_missing_config_from_existing_empty_config(
    saga_service: AutoRoutingService,
    proposal_file: Path,
    plugin_context,
    fault_injector: FaultInjector,
    source_existed: bool,
) -> None:
    if source_existed:
        saga_service.config_path.write_bytes(b"")
    else:
        saga_service.config_path.unlink(missing_ok=True)
    cli = CliRunner(saga_service)
    preview = cli.run("setup", "--proposal", str(proposal_file), "--json")
    fault_injector.crash_after_yaml_replace()
    with pytest.raises(SimulatedProcessDeath):
        cli.run(
            "setup",
            "--proposal",
            str(proposal_file),
            "--apply",
            "--expected-config-sha",
            preview["expected_config_sha256"],
        )

    if source_existed:
        saga_service.config_path.unlink()
    else:
        saga_service.config_path.write_bytes(b"")
    saga_service.store.close()

    restarted = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=ProbeAdapter(),
    )
    try:
        doctor = restarted.doctor()
        assert doctor["incomplete_config_apply"] is True
        assert restarted.status()["activation_mode"] == "off"
        assert restarted.config_path.exists() is (not source_existed)
        if restarted.config_path.exists():
            assert restarted.config_path.read_bytes() == b""
        assert list(
            restarted.config_path.parent.glob(
                "auto-routing-apply-*.pending.json"
            )
        )
    finally:
        restarted.store.close()


def _prepared_journal_with_matching_database_rows(
    service: AutoRoutingService,
    proposal_file: Path,
    *,
    rows_preexisting: bool,
) -> tuple[dict[str, Any], AdaptiveRevision, Path]:
    before = b"display:\n  skin: prepared-before\n"
    service.config_path.write_bytes(before)
    preview = service.preview_config(proposal_file)
    authority_id = preview["authority_id"]
    authority_document = preview["authority"]["document"]
    baseline = AdaptiveRevision.model_validate(
        preview["initial_revision"]["document"]
    )
    if rows_preexisting:
        service.store.publish_authority_and_baseline(
            authority_id=authority_id,
            document=authority_document,
            baseline=baseline,
        )
    operation_id = "prepared-recovery"
    backup_path = service.config_path.with_name(
        f"{service.config_path.name}.auto-routing.{operation_id}.bak"
    )
    backup_path.write_bytes(before)
    journal_path = service.config_path.with_name(
        f"auto-routing-apply-{operation_id}.pending.json"
    )
    journal = {
        "version": 1,
        "operation_id": operation_id,
        "phase": "prepared",
        "config_path": str(service.config_path),
        "backup_path": str(backup_path),
        "source_existed": True,
        "before_sha256": preview["before_sha256"],
        "after_sha256": preview["after_sha256"],
        "authority_id": authority_id,
        "authority_checksum": preview["authority"]["checksum"],
        "baseline_revision_id": baseline.revision_id,
        "baseline_checksum": preview["initial_revision"]["checksum"],
        "baseline_created_at": baseline.created_at,
        "authority_preexisting": rows_preexisting,
        "baseline_preexisting": rows_preexisting,
        "active_pointer_preexisting": rows_preexisting,
    }
    service._write_journal(journal_path, journal)
    if not rows_preexisting:
        service.store.publish_authority_and_baseline(
            authority_id=authority_id,
            document=authority_document,
            baseline=baseline,
        )
    return preview, baseline, journal_path


@pytest.mark.parametrize("rows_preexisting", [False, True])
def test_prepared_before_state_recovery_removes_only_saga_created_rows(
    plugin_context,
    proposal_file: Path,
    rows_preexisting: bool,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=ProbeAdapter(),
    )
    preview, baseline, journal_path = _prepared_journal_with_matching_database_rows(
        service,
        proposal_file,
        rows_preexisting=rows_preexisting,
    )
    authority_before = service.store.read_authority_revision(
        preview["authority_id"]
    )
    service.store.close()

    restarted = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=ProbeAdapter(),
    )
    try:
        authority_after = restarted.store.read_authority_revision(
            preview["authority_id"]
        )
        baseline_after = restarted.store.read_revision(baseline.revision_id)
        active_after = restarted.store.read_active_revision(preview["authority_id"])
        assert restarted.doctor()["incomplete_config_apply"] is False
        assert not journal_path.exists()
        if rows_preexisting:
            assert authority_after == authority_before
            assert baseline_after == baseline
            assert active_after == baseline
        else:
            assert authority_after is None
            assert baseline_after is None
            assert active_after is None
    finally:
        restarted.store.close()


def test_prepared_recovery_preserves_rows_when_config_has_a_foreign_edit(
    plugin_context,
    proposal_file: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=ProbeAdapter(),
    )
    preview, baseline, journal_path = _prepared_journal_with_matching_database_rows(
        service,
        proposal_file,
        rows_preexisting=False,
    )
    foreign = b"display:\n  skin: foreign-prepared-edit\n"
    service.config_path.write_bytes(foreign)
    service.store.close()

    restarted = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=ProbeAdapter(),
    )
    try:
        assert restarted.doctor()["incomplete_config_apply"] is True
        assert restarted.config_path.read_bytes() == foreign
        assert journal_path.exists()
        assert restarted.store.read_authority_revision(
            preview["authority_id"]
        ) is not None
        assert restarted.store.read_revision(baseline.revision_id) == baseline
        assert restarted.store.read_active_revision(preview["authority_id"]) == baseline
    finally:
        restarted.store.close()


def test_verify_runtime_preview_survives_service_restart_and_requires_ack(
    plugin_context,
    proposal_file: Path,
) -> None:
    payload = json.loads(proposal_file.read_text(encoding="utf-8"))
    payload["policy"]["allow_paid_access_probes"] = True
    proposal_file.write_text(json.dumps(payload), encoding="utf-8")
    adapter = ProbeAdapter()
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=adapter,
    )
    cli = CliRunner(service)
    setup = cli.run("setup", "--proposal", str(proposal_file), "--json")
    cli.run(
        "setup",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--expected-config-sha",
        setup["expected_config_sha256"],
        "--json",
    )
    inventory = cli.run("inventory", "--refresh", "--json")
    runtime_id = inventory["runtimes"][0]["runtime_id"]

    preview = cli.run("verify-runtime", runtime_id, "--json")
    assert preview["applied"] is False
    assert preview["billable"] is True
    assert preview["maximum_cost_usd"] > 0
    assert adapter.request_count == 0

    restarted = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=adapter,
    )
    mismatch = CliRunner(restarted).run(
        "verify-runtime",
        "0" * 64,
        "--apply",
        "--expect-hash",
        preview["precondition_hash"],
        "--ack-billable",
        "--json",
    )
    assert mismatch["exit_code"] == 2
    assert adapter.request_count == 0
    rejected = CliRunner(restarted).run(
        "verify-runtime",
        runtime_id,
        "--apply",
        "--expect-hash",
        preview["precondition_hash"],
        "--json",
    )
    assert rejected["exit_code"] == 2
    assert adapter.request_count == 0

    applied = CliRunner(restarted).run(
        "verify-runtime",
        runtime_id,
        "--apply",
        "--expect-hash",
        preview["precondition_hash"],
        "--ack-billable",
        "--json",
    )
    assert applied["state"] == "verified"
    assert adapter.request_count == 1


def test_inventory_read_is_read_only_and_refresh_appends_snapshot(
    plugin_context,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=ProbeAdapter(),
    )
    before = service.store.connection.execute(
        "SELECT COUNT(*) FROM inventory_snapshots"
    ).fetchone()[0]

    read = CliRunner(service).run("inventory", "--json")
    after_read = service.store.connection.execute(
        "SELECT COUNT(*) FROM inventory_snapshots"
    ).fetchone()[0]
    refreshed = CliRunner(service).run("inventory", "--refresh", "--json")
    after_refresh = service.store.connection.execute(
        "SELECT COUNT(*) FROM inventory_snapshots"
    ).fetchone()[0]

    assert read["write_class"] == "read_only"
    assert refreshed["write_class"] == "append_only_observation"
    assert after_read == before
    assert after_refresh == before + 1


def test_refresh_catalog_file_is_immutable_and_deduplicated(
    service: AutoRoutingService,
    isolated_home: Path,
) -> None:
    catalog_file = isolated_home / "catalog.json"
    catalog_file.write_text(
        json.dumps(
            [
                {
                    "canonical_provider": "test-provider",
                    "canonical_model": "test-model",
                    "canonical_version": "test-model",
                    "source_id": "operator-catalog",
                    "source_url": "https://example.com/model-card",
                    "retrieved_at": "2026-07-16T12:00:00Z",
                    "published_at": "2026-07-15T12:00:00Z",
                    "model": "test-model",
                    "model_version": "test-model",
                    "domain": "coding",
                    "task_definition": "debug a service",
                    "metric_name": "quality",
                    "metric_direction": "higher_is_better",
                    "metric_scale": "unit_interval",
                    "value": 0.8,
                    "sample_size": 100,
                    "confidence": 0.9,
                    "normalization_method": "identity"
                }
            ]
        ),
        encoding="utf-8",
    )
    cli = CliRunner(service)

    first = cli.run("refresh-catalog", "--file", str(catalog_file), "--json")
    second = cli.run("refresh-catalog", "--file", str(catalog_file), "--json")

    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["record_count"] == 1
    assert service.store.connection.execute(
        "SELECT COUNT(*) FROM catalog_snapshots"
    ).fetchone()[0] == 1


def test_plan_returns_ordered_missing_facts_without_writing(
    service: AutoRoutingService,
    isolated_home: Path,
) -> None:
    request_file = isolated_home / "request.json"
    request_file.write_text("{}", encoding="utf-8")
    before = service.store.connection.total_changes

    result = CliRunner(service).run("plan", "--request", str(request_file), "--json")

    assert result["exit_code"] == 2
    assert result["missing_facts"] == list(advisor_module.ADVISOR_REQUIRED_FACTS)
    assert service.store.connection.total_changes == before


def test_doctor_missing_authority_is_invalid_but_stage2_contract_is_visible(
    service: AutoRoutingService,
) -> None:
    result = CliRunner(service).run("doctor", "--json")
    checks = {item["name"]: item for item in result["checks"]}

    assert result["exit_code"] == 2
    assert result["healthy"] is False
    assert checks["config_schema"]["status"] == "error"
    assert checks["runtime_adapter"]["status"] == "ok"
    assert checks["post_call_model_failover"] == {
        "name": "post_call_model_failover",
        "status": "warning",
        "detail": "disabled",
    }


def test_complete_golden_plan_is_read_only_provenance_complete_and_never_applies(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=PlanningAdapter(),
    )
    fixture = Path(__file__).with_name("fixtures") / "advisor_interview.json"
    request = json.loads(fixture.read_text(encoding="utf-8"))["complete_request"]
    request_file = isolated_home / "complete-request.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")
    catalog_file = isolated_home / "planning-catalog.json"
    runtime_ids = {
        runtime["model"]: runtime["runtime_id"]
        for runtime in service.inventory(
            refresh=False,
            include_ineligible=True,
        )["runtimes"]
    }
    rows = []
    for model, value in (("test-model-a", 0.9), ("test-model-b", 0.8)):
        shared = {
                "canonical_provider": "test-provider",
                "canonical_model": model,
                "canonical_version": model,
                "retrieved_at": "2026-07-16T12:00:00Z",
                "published_at": "2026-07-15T12:00:00Z",
                "model": model,
                "model_version": model,
                "domain": "coding",
                "task_definition": "debug a service",
                "sample_size": 100,
                "confidence": 0.9,
            }
        rows.extend(
            (
                {
                    **shared,
                    "source_id": f"quality-{model}",
                    "source_url": f"https://example.com/{model}/quality",
                    "metric_name": "quality",
                    "metric_direction": "higher_is_better",
                    "metric_scale": "unit_interval",
                    "value": value,
                    "normalization_method": "identity",
                },
                {
                    **shared,
                    "source_id": f"latency-{model}",
                    "source_url": f"https://example.com/{model}/latency",
                    "runtime_id": runtime_ids[model],
                    "metric_name": "latency",
                    "metric_direction": "lower_is_better",
                    "metric_scale": "seconds",
                    "value": 1.0,
                    "normalization_method": "divide_by_limit",
                },
            )
        )
    catalog_file.write_text(json.dumps(rows), encoding="utf-8")
    refreshed = CliRunner(service).run(
        "refresh-catalog",
        "--file",
        str(catalog_file),
        "--json",
    )
    assert refreshed["exit_code"] == 0, refreshed
    assert refreshed["record_count"] == 4
    before = {
        table: service.store.connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
        for table in (
            "authority_revisions",
            "adaptive_revisions",
            "active_adaptive_revisions",
            "routing_decisions",
        )
    }

    result = CliRunner(service).run("plan", "--request", str(request_file), "--json")

    assert result["exit_code"] == 0, result
    assert result["readiness"] == {"ready": True, "missing_facts": []}
    assert result["dry_run"]["results"]
    assert result["ranking"]
    assert all(item["resolution_status"] == "verified" for item in result["targets"])
    assert all(item["supported_reasoning_efforts"] for item in result["targets"])
    assert all(item["sources"] and item["uncertainty"] is not None for item in result["ranking"])
    assert result["initial_revision"]["canonical_json"]
    assert all(item["exact_match"] for item in result["resolver_validation"])
    assert {item["runtime_id"] for item in result["targets"]} == {
        item["runtime_id"] for item in result["ranking"]
    }
    assert "--apply" not in result["next_command"]
    assert request["representative_prompts"][0] not in json.dumps(result)
    after = {
        table: service.store.connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
        for table in before
    }
    assert after == before
    prompt_bytes = request["representative_prompts"][0].encode("utf-8")
    for path in (
        service.store.path,
        Path(f"{service.store.path}-wal"),
        Path(f"{service.store.path}-shm"),
    ):
        if path.exists():
            assert prompt_bytes not in path.read_bytes()
    assert not list(
        service.config_path.parent.glob("auto-routing-apply-*.pending.json")
    )


def test_subscription_prohibition_survives_plan_apply_restart_and_doctor(
    plugin_context,
    isolated_home: Path,
) -> None:
    adapter = SubscriptionBaselinePlanningAdapter()
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=adapter,
    )
    inventory = service.inventory(refresh=True, include_ineligible=True)
    runtime_by_model = {
        runtime["key"]["model"]: runtime for runtime in inventory["runtimes"]
    }
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    catalog_file = isolated_home / "subscription-policy-catalog.json"
    catalog_file.write_text(
        json.dumps(
            [
                {
                    "canonical_provider": runtime["key"]["provider"],
                    "canonical_model": model,
                    "canonical_version": model,
                    "runtime_id": runtime["runtime_id"],
                    "source_id": f"latency-{model}",
                    "source_url": f"https://example.com/{model}/latency",
                    "retrieved_at": now,
                    "published_at": now,
                    "model": model,
                    "model_version": model,
                    "domain": "coding",
                    "task_definition": "debug a service",
                    "metric_name": "latency",
                    "metric_direction": "lower_is_better",
                    "metric_scale": "seconds",
                    "value": 1.0,
                    "sample_size": 100,
                    "confidence": 1.0,
                    "normalization_method": "divide_by_limit",
                }
                for model, runtime in runtime_by_model.items()
            ]
        ),
        encoding="utf-8",
    )
    service.refresh_catalog(models_dev=False, hermes=False, files=[str(catalog_file)])
    fixture = Path(__file__).with_name("fixtures") / "advisor_interview.json"
    request = json.loads(fixture.read_text(encoding="utf-8"))["complete_request"]
    request["hard_limits"]["allow_subscription"] = False
    request_file = isolated_home / "subscription-policy-request.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")

    plan = CliRunner(service).run(
        "plan",
        "--request",
        str(request_file),
        "--json",
    )

    assert plan["exit_code"] == 0, plan
    assert plan["proposal"]["policy"]["allow_subscription"] is False
    proposal_file = isolated_home / "subscription-policy-proposal.json"
    proposal_file.write_text(json.dumps(plan["proposal"]), encoding="utf-8")
    preview = CliRunner(service).run(
        "setup", "--proposal", str(proposal_file), "--json"
    )
    applied = CliRunner(service).run(
        "setup",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--expected-config-sha",
        preview["expected_config_sha256"],
        "--json",
    )
    assert applied["exit_code"] == 0, applied
    service.store.close()

    restarted = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=adapter,
    )
    try:
        persisted = parse_config(
            {"plugins": {"entries": {"auto-routing": plan["proposal"]}}}
        )
        doctor = restarted.doctor()
        safe_default = next(
            check for check in doctor["checks"] if check["name"] == "safe_default"
        )
    finally:
        restarted.store.close()

    assert persisted.policy.allow_subscription is False
    assert doctor["healthy"] is False
    assert safe_default["status"] == "error"
    assert safe_default["detail"]["reason"] == "inherit_runtime_policy_noncompliant"
    assert "subscription_access_disallowed" in safe_default["detail"]["reasons"]


def test_doctor_rejects_explicit_subscription_default_when_policy_disallows_it(
    plugin_context,
    isolated_home: Path,
) -> None:
    adapter = SubscriptionBaselinePlanningAdapter()
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=adapter,
    )
    inventory = service.inventory(refresh=True, include_ineligible=True)
    runtime = next(
        item
        for item in inventory["runtimes"]
        if item["key"]["model"] == adapter.baseline_model
    )
    payload = _proposal_payload()
    payload["policy"]["allow_subscription"] = False
    target = payload["profiles"]["coding"]["primary"]
    target["runtime"] = runtime["key"]
    target["supported_reasoning_efforts"] = ["none", "low", "medium", "high"]
    payload["safe_default"] = json.loads(json.dumps(target))
    proposal_file = isolated_home / "explicit-subscription-default.json"
    proposal_file.write_text(json.dumps(payload), encoding="utf-8")
    preview = CliRunner(service).run(
        "setup", "--proposal", str(proposal_file), "--json"
    )
    applied = CliRunner(service).run(
        "setup",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--expected-config-sha",
        preview["expected_config_sha256"],
        "--json",
    )
    assert applied["exit_code"] == 0, applied

    doctor = service.doctor()
    safe_default = next(
        check for check in doctor["checks"] if check["name"] == "safe_default"
    )

    assert doctor["healthy"] is False
    assert safe_default["status"] == "error"
    assert safe_default["detail"]["reason"] == "explicit_runtime_policy_noncompliant"
    assert "subscription_access_disallowed" in safe_default["detail"]["reasons"]


def test_edit_observation_only_change_reuses_semantic_authority_id(
    service: AutoRoutingService,
    proposal_file: Path,
) -> None:
    cli = CliRunner(service)
    initial = cli.run("setup", "--proposal", str(proposal_file), "--json")
    applied = cli.run(
        "setup",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--expected-config-sha",
        initial["expected_config_sha256"],
        "--json",
    )
    payload = json.loads(proposal_file.read_text(encoding="utf-8"))
    target = payload["profiles"]["coding"]["primary"]
    target["runtime"]["inventory_revision"] = "inventory-2"
    target["supported_reasoning_efforts"] = ["low", "medium", "high"]
    payload["profiles"]["coding"]["provenance"] = ["inventory:inventory-2"]
    proposal_file.write_text(json.dumps(payload), encoding="utf-8")

    edited = cli.run("edit", "--proposal", str(proposal_file), "--json")
    result = cli.run(
        "edit",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--expected-config-sha",
        edited["expected_config_sha256"],
        "--json",
    )

    assert result["exit_code"] == 0
    assert result["authority_id"] == applied["authority_id"]
    assert len(service.store.list_authority_revisions()) == 1


def test_control_plane_refuses_config_outside_service_profile(
    plugin_context,
    isolated_home: Path,
    proposal_file: Path,
) -> None:
    outside_profile = isolated_home / "different-profile"
    outside_profile.mkdir()
    service = AutoRoutingService(
        plugin_context=plugin_context,
        hermes_home=outside_profile,
        store=RoutingStore.open(home=outside_profile),
        adapter=ProbeAdapter(),
    )
    try:
        result = CliRunner(service).run(
            "setup",
            "--proposal",
            str(proposal_file),
            "--json",
        )
        inventory_result = CliRunner(service).run(
            "inventory",
            "--refresh",
            "--json",
        )
        doctor = service.doctor()
        inventory_rows = service.store.connection.execute(
            "SELECT COUNT(*) FROM inventory_snapshots"
        ).fetchone()[0]
    finally:
        service.store.close()

    assert result["exit_code"] == 2
    assert "profile" in result["error"]
    assert inventory_result["exit_code"] == 2
    assert inventory_result["write_class"] == "append_only_observation"
    assert inventory_rows == 0
    assert doctor["healthy"] is False
    assert doctor["checks"][0]["name"] == "profile_isolation"
    assert not service.config_path.exists()


def test_doctor_rejects_expired_target_verification_and_warns_stale_inventory(
    service: AutoRoutingService,
    proposal_file: Path,
) -> None:
    cli = CliRunner(service)
    preview = cli.run("setup", "--proposal", str(proposal_file), "--json")
    cli.run(
        "setup",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--expected-config-sha",
        preview["expected_config_sha256"],
        "--json",
    )
    proposal = service.load_proposal(proposal_file)
    runtime = proposal.profiles["coding"].primary.runtime
    service.store.write_inventory_snapshot(
        runtime.inventory_revision,
        [
            RuntimeObservation(
                key=runtime.model_dump(mode="json"),
                state="verified",
                reasons=(),
                economics=AccessEconomics(
                    billing_kind="subscription",
                    subscription_plan="test-plan",
                    subscription_quota_remaining=1,
                    subscription_quota_unit="request",
                    subscription_state="active",
                    source_id="test-economics",
                    provenance="test",
                    observed_at="2020-01-01T00:00:00Z",
                ),
                verification_source="explicit_probe",
                verified_at="2020-01-01T00:00:00Z",
                verification_expires_at="2020-01-02T00:00:00Z",
                provenance=("test",),
                observed_at="2020-01-01T00:00:00Z",
            )
        ],
        created_at="2020-01-01T00:00:00Z",
    )

    doctor = service.doctor()
    checks = {item["name"]: item for item in doctor["checks"]}

    assert doctor["healthy"] is False
    assert checks["runtime_verification"]["status"] == "error"
    assert checks["inventory_freshness"]["status"] == "warning"


def _apply_local_authority_for_doctor(
    service: AutoRoutingService,
    isolated_home: Path,
    *,
    max_cost_usd: float = 2.0,
) -> dict[str, Any]:
    inventory = service.inventory(refresh=True, include_ineligible=True)
    assert len(inventory["runtimes"]) == 1
    runtime = inventory["runtimes"][0]
    assert runtime["state"] == "verified"
    payload = _proposal_payload()
    payload["llm"]["allowed_providers"] = [runtime["key"]["provider"]]
    payload["llm"]["allowed_models"] = [runtime["key"]["model"]]
    payload["classifier"]["provider"] = runtime["key"]["provider"]
    payload["classifier"]["model"] = runtime["key"]["model"]
    payload["policy"]["max_estimated_task_cost_usd"] = max_cost_usd
    payload["policy"]["minimum_context_tokens"] = 1
    primary = payload["profiles"]["coding"]["primary"]
    primary["runtime"] = runtime["key"]
    primary["supported_reasoning_efforts"] = ["low", "medium", "high"]
    primary["reasoning"] = {"default": "medium", "min": "low", "max": "high"}
    primary["revision_status"] = "active"
    payload["profiles"]["coding"]["fallbacks"] = []
    proposal_path = isolated_home / "local-doctor-proposal.json"
    proposal_path.write_text(json.dumps(payload), encoding="utf-8")
    preview = CliRunner(service).run(
        "setup", "--proposal", str(proposal_path), "--json"
    )
    applied = CliRunner(service).run(
        "setup",
        "--proposal",
        str(proposal_path),
        "--apply",
        "--expected-config-sha",
        preview["expected_config_sha256"],
        "--json",
    )
    assert applied["exit_code"] == 0, applied

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    catalog_path = isolated_home / "local-doctor-catalog.json"
    catalog_path.write_text(
        json.dumps(
            [
                {
                    "canonical_provider": runtime["key"]["provider"],
                    "canonical_model": runtime["key"]["model"],
                    "canonical_version": runtime["key"]["model"],
                    "runtime_id": runtime["runtime_id"],
                    "source_id": "local-latency",
                    "source_url": "https://example.com/local-latency",
                    "retrieved_at": now,
                    "published_at": now,
                    "model": runtime["key"]["model"],
                    "model_version": runtime["key"]["model"],
                    "domain": "general",
                    "task_definition": "safe default policy validation",
                    "metric_name": "latency",
                    "metric_direction": "lower_is_better",
                    "metric_scale": "seconds",
                    "value": 1.0,
                    "sample_size": 1,
                    "confidence": 1.0,
                    "normalization_method": "divide_by_limit",
                }
            ]
        ),
        encoding="utf-8",
    )
    service.refresh_catalog(models_dev=False, hermes=False, files=[str(catalog_path)])
    return runtime


def test_local_plan_apply_doctor_accepts_fresh_inherited_baseline(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=LocalPlanningAdapter(),
    )
    runtime = _apply_local_authority_for_doctor(service, isolated_home)

    doctor = service.doctor()
    checks = {item["name"]: item for item in doctor["checks"]}

    assert doctor["healthy"] is True, checks
    assert checks["safe_default"] == {
        "name": "safe_default",
        "status": "ok",
        "detail": {
            "mode": "inherit",
            "runtime_id": runtime["runtime_id"],
            "verification_source": "installed_local",
            "policy_compliant": True,
        },
    }
    assert checks["runtime_verification"]["status"] == "ok"


def test_doctor_rejects_unresolvable_inherited_baseline(
    plugin_context,
    isolated_home: Path,
) -> None:
    adapter = LocalPlanningAdapter()
    adapter.inherited_available = False
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=adapter,
    )
    _apply_local_authority_for_doctor(service, isolated_home)

    doctor = service.doctor()
    check = next(item for item in doctor["checks"] if item["name"] == "safe_default")

    assert doctor["healthy"] is False
    assert check["status"] == "error"
    assert check["detail"]["reason"] == "inherit_runtime_unresolvable"


def test_doctor_rejects_inherited_baseline_outside_cost_policy(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=LocalPlanningAdapter(),
    )
    _apply_local_authority_for_doctor(
        service,
        isolated_home,
        max_cost_usd=0.001,
    )

    doctor = service.doctor()
    check = next(item for item in doctor["checks"] if item["name"] == "safe_default")

    assert doctor["healthy"] is False
    assert check["status"] == "error"
    assert "estimated_cost_exceeds_limit" in check["detail"]["reasons"]


def test_doctor_rejects_expired_installed_local_evidence(
    plugin_context,
    isolated_home: Path,
) -> None:
    adapter = LocalPlanningAdapter()
    adapter.observed_at = "2020-01-01T00:00:00Z"
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=adapter,
    )
    _apply_local_authority_for_doctor(service, isolated_home)

    doctor = service.doctor()
    checks = {item["name"]: item for item in doctor["checks"]}

    assert doctor["healthy"] is False
    assert checks["safe_default"]["status"] == "error"
    assert (
        checks["safe_default"]["detail"]["reason"]
        == "inherit_runtime_evidence_stale"
    )
    assert checks["runtime_verification"]["status"] == "error"


def test_read_only_inventory_overlays_persisted_explicit_probe_across_services(
    plugin_context,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=ProbeAdapter(),
    )
    initial = service.inventory(refresh=False, include_ineligible=True)
    runtime = initial["runtimes"][0]
    service.store.write_inventory_snapshot(
        runtime["key"]["inventory_revision"],
        [
            RuntimeObservation(
                key=runtime["key"],
                state="verified",
                reasons=(),
                economics=runtime["economics"],
                verification_source="explicit_probe",
                verified_at="2026-07-16T12:00:00Z",
                verification_expires_at="2999-07-16T12:05:00Z",
                provenance=("explicit-access-probe",),
                observed_at=runtime["observed_at"],
            )
        ],
    )

    restarted = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=ProbeAdapter(),
    )
    try:
        result = restarted.inventory(refresh=False, include_ineligible=True)
    finally:
        restarted.store.close()
        service.store.close()

    assert result["runtimes"][0]["state"] == "verified"
    assert result["runtimes"][0]["verification_source"] == "explicit_probe"


def test_status_and_doctor_fail_closed_on_journal_created_by_another_process(
    service: AutoRoutingService,
    proposal_file: Path,
) -> None:
    cli = CliRunner(service)
    preview = cli.run("setup", "--proposal", str(proposal_file), "--json")
    applied = cli.run(
        "setup",
        "--proposal",
        str(proposal_file),
        "--apply",
        "--expected-config-sha",
        preview["expected_config_sha256"],
        "--json",
    )
    assert applied["exit_code"] == 0
    journal = service.config_path.with_name(
        "auto-routing-apply-external.pending.json"
    )
    journal.write_text("{}", encoding="utf-8")

    status = service.status()
    doctor = service.doctor()

    assert status["activation_mode"] == "off"
    assert status["incomplete_config_apply"] is True
    assert doctor["incomplete_config_apply"] is True


def test_failed_saga_never_overwrites_a_concurrent_external_config_edit(
    plugin_context,
    proposal_file: Path,
) -> None:
    class ExternalEditFailure:
        def before_baseline_publish(self) -> None:
            service.config_path.write_text("external: edit\n", encoding="utf-8")
            raise RuntimeError("simulated database failure after external edit")

    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=ProbeAdapter(),
        fault_injector=ExternalEditFailure(),
    )
    preview = service.preview_config(proposal_file)

    with pytest.raises(Exception, match="recovery is incomplete"):
        service.apply_config(
            proposal_file,
            expected_config_sha256=preview["expected_config_sha256"],
        )

    assert service.config_path.read_text(encoding="utf-8") == "external: edit\n"
    assert service.status()["activation_mode"] == "off"
    assert service.status()["incomplete_config_apply"] is True
    assert list(
        service.config_path.parent.glob("auto-routing-apply-*.pending.json")
    )


def test_plan_rejects_local_runtime_outside_requested_license_allowlist(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=LocalPlanningAdapter(),
    )
    inventory = service.inventory(refresh=False, include_ineligible=True)
    runtime_id = inventory["runtimes"][0]["runtime_id"]
    fixture = Path(__file__).with_name("fixtures") / "advisor_interview.json"
    request = json.loads(fixture.read_text(encoding="utf-8"))[
        "legacy_complete_request"
    ]
    request["hard_limits"]["allow_local"] = True
    request["hard_limits"]["allowed_licenses"] = ["apache-2.0"]
    request["access_paths"] = {
        "primary_runtime_id": runtime_id,
        "fallback_runtime_ids": [],
    }
    request["reasoning_bounds"] = {
        runtime_id: {"default": "medium", "min": "low", "max": "high"}
    }
    request_file = isolated_home / "license-request.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")

    result = CliRunner(service).run(
        "plan",
        "--request",
        str(request_file),
        "--json",
    )

    assert result["exit_code"] == 2
    assert "license" in result["error"]


def _refresh_capability_test_catalog(
    service: AutoRoutingService,
    isolated_home: Path,
    *,
    name: str,
) -> None:
    inventory = service.inventory(refresh=False, include_ineligible=True)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    catalog_path = isolated_home / f"{name}-catalog.json"
    catalog_path.write_text(
        json.dumps(
            [
                {
                    "canonical_provider": runtime["key"]["provider"],
                    "canonical_model": runtime["key"]["model"],
                    "canonical_version": runtime["key"]["model"],
                    "runtime_id": runtime["runtime_id"],
                    "source_id": f"latency-{runtime['runtime_id']}",
                    "source_url": "https://example.com/capability-latency",
                    "retrieved_at": now,
                    "published_at": now,
                    "model": runtime["key"]["model"],
                    "model_version": runtime["key"]["model"],
                    "domain": "coding",
                    "task_definition": "debug a service",
                    "metric_name": "latency",
                    "metric_direction": "lower_is_better",
                    "metric_scale": "seconds",
                    "value": 1.0,
                    "sample_size": 100,
                    "confidence": 1.0,
                    "normalization_method": "divide_by_limit",
                }
                for runtime in inventory["runtimes"]
            ]
        ),
        encoding="utf-8",
    )
    service.refresh_catalog(
        models_dev=False,
        hermes=False,
        files=[str(catalog_path)],
    )


@pytest.mark.parametrize(
    ("required_capability", "capability_updates", "reason"),
    [
        (
            "structured_output",
            {"supports_structured_output": False},
            "required_capability_unsupported:structured_output",
        ),
        (
            "batch_reasoning",
            {},
            "required_capability_unproven:batch_reasoning",
        ),
    ],
)
def test_complete_plan_rejects_unmet_declared_capabilities_before_ranking(
    plugin_context,
    isolated_home: Path,
    required_capability: str,
    capability_updates: dict[str, Any],
    reason: str,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=CapabilityPlanningAdapter(capability_updates),
    )
    _refresh_capability_test_catalog(
        service,
        isolated_home,
        name=f"negative-{required_capability}",
    )
    fixture = Path(__file__).with_name("fixtures") / "advisor_interview.json"
    request = json.loads(fixture.read_text(encoding="utf-8"))[
        "legacy_complete_request"
    ]
    request["required_capabilities"] = [required_capability]
    request_file = isolated_home / f"negative-{required_capability}.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")

    result = CliRunner(service).run(
        "plan", "--request", str(request_file), "--json"
    )

    assert result["exit_code"] == 2
    assert reason in result["error"]


def test_complete_plan_accepts_and_persists_exact_custom_capability(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=CapabilityPlanningAdapter({"supports_batch_reasoning": True}),
    )
    _refresh_capability_test_catalog(
        service,
        isolated_home,
        name="positive-batch-reasoning",
    )
    fixture = Path(__file__).with_name("fixtures") / "advisor_interview.json"
    request = json.loads(fixture.read_text(encoding="utf-8"))[
        "legacy_complete_request"
    ]
    request["required_capabilities"] = ["batch_reasoning"]
    request_file = isolated_home / "positive-batch-reasoning.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")

    result = CliRunner(service).run(
        "plan", "--request", str(request_file), "--json"
    )

    assert result["exit_code"] == 0, result
    assert result["proposal"]["profiles"]["coding"]["match"]["capabilities"] == [
        "batch_reasoning",
        "tools",
    ]
    assert len(result["ranking"]) == 2


def test_plan_refuses_to_materialize_targets_rejected_by_advisor_hard_gates(
    plugin_context,
    isolated_home: Path,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=PlanningAdapter(),
    )
    fixture = Path(__file__).with_name("fixtures") / "advisor_interview.json"
    request = json.loads(fixture.read_text(encoding="utf-8"))["complete_request"]
    request_file = isolated_home / "missing-latency-evidence.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")

    result = CliRunner(service).run(
        "plan",
        "--request",
        str(request_file),
        "--json",
    )

    assert result["exit_code"] == 2
    assert "estimated_latency_unknown" in result["error"]


def test_failed_inventory_refresh_keeps_append_only_write_class(
    plugin_context,
) -> None:
    service = AutoRoutingService.from_plugin_context(
        plugin_context,
        adapter=BrokenInventoryAdapter(),
    )

    result = CliRunner(service).run("inventory", "--refresh", "--json")

    assert result["exit_code"] == 2
    assert result["write_class"] == "append_only_observation"
