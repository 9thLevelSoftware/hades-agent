"""Auto-specific failure, privacy, fallback, and compatibility contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from agent.runtime_routing import (
    AgentRuntimeContext,
    AgentRuntimePlan,
    AgentRuntimeRequest,
    AgentRuntimeSpec,
    RUNTIME_ROUTING_CONTRACT_VERSION,
    RuntimeSessionContinuation,
)
from plugins.auto_routing.auto_routing.cli import (
    CommandWriteClass,
    auto_routing_command,
    build_parser,
    command_metadata,
)
from plugins.auto_routing.auto_routing.storage import RoutingStore
from run_agent import AIAgent

from _stage2_test_support import LoopbackProvider, install_runtime_resolver
import test_stage2_fresh_session_e2e as fresh


def test_stage4_adaptation_cli_leaves_have_closed_write_authority() -> None:
    assert command_metadata("adapt status").write_class is CommandWriteClass.READ_ONLY
    assert command_metadata("adapt history").write_class is CommandWriteClass.READ_ONLY
    for command in ("adapt freeze", "adapt unfreeze", "adapt rollback"):
        assert (
            command_metadata(command).write_class
            is CommandWriteClass.GUARDED_CONTROL_PLANE
        )


@contextmanager
def _production_agent_log(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Use Hermes's real async, redacting ``agent.log`` in an isolated home."""
    import hermes_logging
    import run_agent as run_agent_module

    root_logger = logging.getLogger()
    route_logger = logging.getLogger("agent.runtime_routing")
    original_root_level = root_logger.level
    original_route_handlers = list(route_logger.handlers)
    original_route_level = route_logger.level
    original_route_propagate = route_logger.propagate

    hermes_logging._reset_queued_handlers()
    hermes_logging._logging_initialized = False
    route_logger.handlers = []
    route_logger.setLevel(logging.NOTSET)
    route_logger.propagate = True
    monkeypatch.setattr(run_agent_module, "_hermes_home", home)
    path = home / "logs" / "agent.log"
    try:
        yield path
        from agent.redact import RedactingFormatter

        matching_handlers = [
            handler
            for handler in hermes_logging.rotating_file_handlers()
            if Path(getattr(handler, "baseFilename", "")).resolve()
            == path.resolve()
        ]
        assert len(matching_handlers) == 1
        assert isinstance(matching_handlers[0].formatter, RedactingFormatter)
        assert any(
            getattr(handler, "_hermes_queue", False)
            for handler in root_logger.handlers
        )
    finally:
        try:
            hermes_logging.flush_log_queue()
        finally:
            hermes_logging._reset_queued_handlers()
            hermes_logging._logging_initialized = False
            root_logger.setLevel(original_root_level)
            route_logger.handlers = original_route_handlers
            route_logger.setLevel(original_route_level)
            route_logger.propagate = original_route_propagate
            assert hermes_logging.rotating_file_handlers() == []
            assert not any(
                getattr(handler, "_hermes_queue", False)
                for handler in root_logger.handlers
            )


def _baseline_spec(base_url: str, *, api_key: str = "BASELINE_KEY") -> AgentRuntimeSpec:
    return AgentRuntimeSpec(
        model="baseline-model",
        provider="openrouter",
        base_url=base_url,
        api_key=api_key,
        resolution_state="resolved",
        api_mode="chat_completions",
        reasoning_config={"enabled": True, "effort": "low"},
        fallback_model=(
            {"provider": "openrouter", "model": "ordinary-host-fallback"},
        ),
    )


def _agent_context(session_id: str, task: Any) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        scope="fresh_session",
        task=task,
        session_id=session_id,
        task_id=f"task-{session_id}",
        metadata={"platform": "cli"},
    )


def test_stage3_evidence_paths_preserve_stage2_binding_and_epochs(active_route):
    before_binding = active_route.service.store.read_session_binding(
        active_route.session_id
    )
    before_epochs = active_route.service.store.read_route_epochs(
        active_route.session_id
    )
    event = active_route.service.ingest_turn_outcome(active_route.payload()).event

    active_route.service.record_feedback(
        evidence_id=event.evidence_id,
        value="rejected",
    )
    active_route.service.report(days=30)

    assert active_route.service.store.read_session_binding(
        active_route.session_id
    ) == before_binding
    assert active_route.service.store.read_route_epochs(
        active_route.session_id
    ) == before_epochs


def _patch_baseline_resolution(monkeypatch, baseline_url: str) -> None:
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "openrouter",
            "model": "baseline-model",
            "base_url": baseline_url,
            "api_key": "BASELINE_KEY",
            "api_mode": "chat_completions",
        },
    )


def test_decision_store_failure_sends_no_selected_request_and_inherits_baseline(
    isolated_home: Path,
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[3]
    with (
        LoopbackProvider(response_text="baseline") as baseline,
        LoopbackProvider(response_text="selected-must-not-run") as selected,
    ):
        service, resolver = fresh._seed_service(
            root=root,
            home=isolated_home,
            adapter=fresh._LoopbackRuntimeAdapter(selected.base_url),
            mode="active",
        )
        _patch_baseline_resolution(monkeypatch, baseline.base_url)

        def fail_commit(*_args, **_kwargs):
            raise sqlite3.OperationalError("injected decision-store write failure")

        monkeypatch.setattr(service.store, "commit_decision", fail_commit)
        install_runtime_resolver(monkeypatch, resolver)
        try:
            agent, result = fresh._run_agent(
                baseline_url=baseline.base_url,
                context=fresh._context(
                    "stage2-store-failure",
                    "RAW_TASK_MUST_NOT_REACH_SELECTED",
                ),
                prompt="RAW_TASK_MUST_NOT_REACH_SELECTED",
            )
            try:
                assert result["final_response"].strip() == "baseline"
                assert agent.model == "baseline-model"
                assert agent.base_url == baseline.base_url
                assert agent._runtime_routing_binding.reason_code == "resolver_error"
                assert agent._runtime_fallback_authority == "host"
            finally:
                agent.close()
        finally:
            resolver.close()
            service.store.close()

    assert selected.requests == []
    chat_requests = [request for request in baseline.requests if "messages" in request]
    assert len(chat_requests) == 1
    assert chat_requests[0]["model"] == "baseline-model"
    with RoutingStore.open(home=isolated_home) as store:
        assert store.count_decisions() == 0


def test_real_shadow_changes_no_baseline_constructor_or_request_field(
    isolated_home: Path,
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[3]
    prompt = "identical shadow request"
    exact_pool = _RecordingPool()
    request_overrides = {
        "extra_body": {"service_tier": "priority"},
        "temperature": 0,
    }
    monkeypatch.setattr(
        "agent.agent_init.get_provider_request_timeout",
        lambda _provider, _model: 17.0,
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._apply_user_default_headers",
        lambda headers: {**dict(headers or {}), "X-Stage2-Contract": "same"},
    )
    with (
        LoopbackProvider(response_text="baseline") as baseline,
        LoopbackProvider(response_text="selected-must-not-run") as selected,
    ):
        _patch_baseline_resolution(monkeypatch, baseline.base_url)
        plain = AIAgent(
            api_key="BASELINE_KEY",
            base_url=baseline.base_url,
            provider="openrouter",
            model="baseline-model",
            max_iterations=4,
            enabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            save_trajectories=False,
            platform="cli",
            session_id="stage2-shadow",
            credential_pool=exact_pool,
            service_tier="priority",
            request_overrides=request_overrides,
            fallback_model={
                "provider": "openrouter",
                "model": "global-fallback-must-not-run",
            },
        )
        try:
            plain_result = plain.run_conversation(
                prompt,
                conversation_history=[],
                task_id="task-stage2-shadow",
            )
            plain_constructor = {
                "model": plain.model,
                "provider": plain.provider,
                "base_url": plain.base_url,
                "api_key": plain.api_key,
                "api_mode": plain.api_mode,
                "credential_pool_id": id(plain._credential_pool),
                "service_tier": plain.service_tier,
                "request_overrides": dict(plain.request_overrides),
                "reasoning": (
                    None
                    if plain.reasoning_config is None
                    else dict(plain.reasoning_config)
                ),
                "fallback_chain": tuple(
                    dict(item) for item in plain._fallback_chain
                ),
                "fallback_authority": plain._runtime_fallback_authority,
                "client_kwargs": dict(plain._client_kwargs),
                "primary_runtime": dict(plain._primary_runtime),
                "api_timeout": plain._resolved_api_call_timeout(),
            }
        finally:
            plain.close()

        service, resolver = fresh._seed_service(
            root=root,
            home=isolated_home,
            adapter=fresh._LoopbackRuntimeAdapter(selected.base_url),
            mode="shadow",
        )
        install_runtime_resolver(monkeypatch, resolver)
        try:
            shadow, shadow_result = fresh._run_agent(
                baseline_url=baseline.base_url,
                context=fresh._context("stage2-shadow", prompt),
                prompt=prompt,
                credential_pool=exact_pool,
                service_tier="priority",
                request_overrides=request_overrides,
            )
            try:
                shadow_constructor = {
                    "model": shadow.model,
                    "provider": shadow.provider,
                    "base_url": shadow.base_url,
                    "api_key": shadow.api_key,
                    "api_mode": shadow.api_mode,
                    "credential_pool_id": id(shadow._credential_pool),
                    "service_tier": shadow.service_tier,
                    "request_overrides": dict(shadow.request_overrides),
                    "reasoning": (
                        None
                        if shadow.reasoning_config is None
                        else dict(shadow.reasoning_config)
                    ),
                    "fallback_chain": tuple(
                        dict(item) for item in shadow._fallback_chain
                    ),
                    "fallback_authority": shadow._runtime_fallback_authority,
                    "client_kwargs": dict(shadow._client_kwargs),
                    "primary_runtime": dict(shadow._primary_runtime),
                    "api_timeout": shadow._resolved_api_call_timeout(),
                }
                assert shadow._runtime_routing_binding.action == "shadow"
                assert shadow._runtime_fallback_authority == "host"
            finally:
                shadow.close()
        finally:
            resolver.close()
            service.store.close()

    assert plain_result["final_response"] == shadow_result["final_response"]
    assert plain_constructor == shadow_constructor
    assert plain_constructor["credential_pool_id"] == id(exact_pool)
    assert plain_constructor["service_tier"] == "priority"
    assert plain_constructor["client_kwargs"]["timeout"] == 17.0
    assert plain_constructor["client_kwargs"]["default_headers"] == {
        "X-Stage2-Contract": "same"
    }
    assert plain_constructor["fallback_authority"] == "host"
    chat_requests = [request for request in baseline.requests if "messages" in request]
    assert len(chat_requests) == 2
    assert chat_requests[0] == chat_requests[1]
    assert selected.requests == []
    with RoutingStore.open(home=isolated_home) as store:
        assert store.count_decisions() == 1


class _PlanResolver:
    def __init__(self, plan_factory) -> None:
        self.plan_factory = plan_factory

    @staticmethod
    def requires_initial_task(_scope: str) -> bool:
        return True

    def resolve(self, request: AgentRuntimeRequest) -> AgentRuntimePlan:
        return self.plan_factory(request)

    def record_manual_pin(self, _request) -> None:
        return

    def record_session_continuation(self, _request) -> None:
        return

    def close(self) -> None:
        return


class _RecordingPool:
    provider = "openrouter"

    def __init__(self, base_url: str = "https://selected.invalid/v1") -> None:
        self.rotations: list[int | None] = []
        self.refreshes = 0
        self.first = SimpleNamespace(
            id="selected-a",
            last_status=None,
            runtime_api_key="SELECTED_POOL_A",
            runtime_base_url=base_url,
        )
        self.second = SimpleNamespace(
            id="selected-b",
            last_status=None,
            runtime_api_key="SELECTED_POOL_B",
            runtime_base_url=base_url,
        )
        self._current = self.first

    def current(self):
        return self._current

    def mark_exhausted_and_rotate(self, *, status_code=None, error_context=None):
        del error_context
        self.rotations.append(status_code)
        self._current = self.second
        return self.second

    def try_refresh_current(self):
        self.refreshes += 1
        self._current = self.second
        return self.second

    @staticmethod
    def has_available() -> bool:
        return True


@pytest.mark.parametrize(
    ("status_codes", "expected_authorization", "expected_rotations", "expected_refreshes"),
    [
        (
            (401, 200),
            ["Bearer SELECTED_POOL_A", "Bearer SELECTED_POOL_B"],
            [],
            1,
        ),
        (
            (429, 429, 200),
            [
                "Bearer SELECTED_POOL_A",
                "Bearer SELECTED_POOL_A",
                "Bearer SELECTED_POOL_B",
            ],
            [429],
            0,
        ),
    ],
)
def test_active_401_429_live_recovery_stays_inside_selected_exact_pool(
    monkeypatch,
    status_codes,
    expected_authorization,
    expected_rotations,
    expected_refreshes,
) -> None:
    with (
        LoopbackProvider(response_text="baseline-must-not-run") as baseline,
        LoopbackProvider(
            response_text="selected-pool-success",
            status_codes=status_codes,
        ) as selected_endpoint,
    ):
        pool = _RecordingPool(selected_endpoint.base_url)
        selected = AgentRuntimeSpec(
            model="selected-model",
            provider="openrouter",
            base_url=selected_endpoint.base_url,
            api_key="SELECTED_POOL_A",
            resolution_state="resolved",
            api_mode="chat_completions",
            credential_pool=pool,
            reasoning_config={"enabled": True, "effort": "high"},
            fallback_model=(),
        )

        def plan(_request):
            return AgentRuntimePlan(
                action="project",
                runtime=selected,
                decision_id="decision-selected-pool",
                bound_route_identity="route-selected-pool",
                owns_fallbacks=True,
                reason_code="active_projected",
                event={
                    "decision_id": "decision-selected-pool",
                    "runtime_id": "runtime-selected-pool",
                    "post_call_model_failover": False,
                },
            )

        install_runtime_resolver(monkeypatch, _PlanResolver(plan))
        monkeypatch.setattr(
            "agent.conversation_loop.jittered_backoff",
            lambda *_args, **_kwargs: 0.0,
        )
        monkeypatch.setattr(
            "agent.conversation_loop.adaptive_rate_limit_backoff",
            lambda *_args, **_kwargs: (0.0, None),
        )
        agent = None
        try:
            with (
                patch(
                    "agent.auxiliary_client.resolve_provider_client",
                    side_effect=AssertionError("global runtime must not be consulted"),
                ),
                patch(
                    "agent.credential_pool.load_pool",
                    side_effect=AssertionError("global pool must not be consulted"),
                ),
            ):
                agent = AIAgent(
                    model="baseline-model",
                    provider="openrouter",
                    base_url=baseline.base_url,
                    api_key="BASELINE_KEY",
                    fallback_model={
                        "provider": "anthropic",
                        "model": "global-fallback-must-not-run",
                    },
                    runtime_routing_context=_agent_context(
                        f"selected-pool-{status_codes[0]}",
                        "selected pool task",
                    ),
                    max_iterations=4,
                    enabled_toolsets=[],
                    quiet_mode=True,
                    skip_context_files=True,
                    skip_memory=True,
                    save_trajectories=False,
                )
                assert agent._credential_pool is pool
                assert agent._fallback_chain == []
                monkeypatch.setattr(
                    agent,
                    "_try_activate_fallback",
                    lambda: pytest.fail("global fallback must not be consulted"),
                )
                result = agent.run_conversation(
                    "selected pool task",
                    conversation_history=[],
                    task_id=f"task-selected-pool-{status_codes[0]}",
                )
        finally:
            if agent is not None:
                agent.close()

    assert result["final_response"] == "selected-pool-success"
    assert result.get("failed") is not True
    assert selected_endpoint.chat_authorization_headers == expected_authorization
    chat_requests = [
        request for request in selected_endpoint.requests if "messages" in request
    ]
    assert [request["model"] for request in chat_requests] == [
        "selected-model"
    ] * len(expected_authorization)
    assert baseline.requests == []
    assert pool.rotations == expected_rotations
    assert pool.refreshes == expected_refreshes
    assert pool.current() is pool.second


def test_active_plan_publishes_redacted_post_call_failover_capability(
    isolated_home: Path,
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[3]
    with LoopbackProvider(response_text="selected") as selected:
        service, resolver = fresh._seed_service(
            root=root,
            home=isolated_home,
            adapter=fresh._LoopbackRuntimeAdapter(selected.base_url),
            mode="active",
        )
        request = AgentRuntimeRequest(
            contract_version=RUNTIME_ROUTING_CONTRACT_VERSION,
            context=_agent_context("reduced-capability", "raw task stays ephemeral"),
            baseline=_baseline_spec("https://baseline.invalid/v1"),
        )
        try:
            plan = resolver.resolve(request)
        finally:
            resolver.close()
            service.store.close()

    assert plan.action == "project"
    assert plan.runtime.fallback_model == ()
    assert plan.event["post_call_model_failover"] is False
    assert set(plan.event) == {
        "adaptive_assignment_id",
        "decision_id",
        "post_call_model_failover",
        "profile_adaptive_revision_id",
        "projection_mode",
        "runtime_id",
    }
    assert plan.event["adaptive_assignment_id"] is None
    assert len(plan.event["profile_adaptive_revision_id"]) == 64
    assert "raw task stays ephemeral" not in json.dumps(dict(plan.event))


def test_real_agent_construction_logs_validated_redacted_runtime_binding(
    isolated_home: Path,
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[3]
    with LoopbackProvider(response_text="selected") as selected:
        service, resolver = fresh._seed_service(
            root=root,
            home=isolated_home,
            adapter=fresh._LoopbackRuntimeAdapter(
                selected.base_url,
                api_key="EVENT_API_KEY_MUST_BE_REDACTED",
            ),
            mode="active",
        )
        install_runtime_resolver(monkeypatch, resolver)
        agent = None
        try:
            with _production_agent_log(isolated_home, monkeypatch) as log_path:
                agent, result = fresh._run_agent(
                    baseline_url="https://user:pass@baseline.invalid/v1",
                    baseline_api_key="EVENT_BASELINE_TOKEN_MUST_BE_REDACTED",
                    context=fresh._context(
                        "stage2-runtime-event",
                        "EVENT_RAW_TASK_MUST_BE_REDACTED",
                    ),
                    prompt="EVENT_RAW_TASK_MUST_BE_REDACTED",
                )
            assert result["final_response"] == "selected"
            expected = agent._runtime_routing_binding.public_record()
        finally:
            if agent is not None:
                agent.close()
            resolver.close()
            service.store.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    marker = "runtime_routing="
    runtime_lines = [line for line in lines if marker in line]
    assert len(runtime_lines) == 1
    emitted = json.loads(runtime_lines[0].split(marker, 1)[1])
    assert emitted == expected
    assert emitted["event"]["post_call_model_failover"] is False
    serialized = json.dumps(emitted, sort_keys=True)
    for sentinel in (
        "EVENT_API_KEY_MUST_BE_REDACTED",
        "EVENT_BASELINE_TOKEN_MUST_BE_REDACTED",
        "EVENT_RAW_TASK_MUST_BE_REDACTED",
        "user:pass@",
    ):
        assert sentinel not in serialized


def test_nonexportable_runtime_identifier_never_breaks_construction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with LoopbackProvider(response_text="unused") as selected_endpoint:
        selected = AgentRuntimeSpec(
            model="selected model=legacy",
            provider="openrouter",
            base_url=selected_endpoint.base_url,
            api_key="SELECTED_KEY",
            resolution_state="resolved",
            api_mode="chat_completions",
            fallback_model=(),
        )

        def plan(_request):
            return AgentRuntimePlan(
                action="project",
                runtime=selected,
                decision_id="decision-nonexportable-model",
                bound_route_identity="route-nonexportable-model",
                owns_fallbacks=True,
                reason_code="active_projected",
                event={"post_call_model_failover": False},
            )

        install_runtime_resolver(monkeypatch, _PlanResolver(plan))
        agent = None
        try:
            with _production_agent_log(tmp_path, monkeypatch) as log_path:
                agent = AIAgent(
                    model="baseline-model",
                    provider="openrouter",
                    base_url=selected_endpoint.base_url,
                    api_key="BASELINE_KEY",
                    runtime_routing_context=_agent_context(
                        "nonexportable-model",
                        "construction must survive logging",
                    ),
                    enabled_toolsets=[],
                    quiet_mode=True,
                    skip_context_files=True,
                    skip_memory=True,
                    save_trajectories=False,
                )
            assert agent.model == "selected model=legacy"
        finally:
            if agent is not None:
                agent.close()

    log_text = log_path.read_text(encoding="utf-8")
    warning = "runtime_routing=unavailable reason=invalid_public_record"
    warning_lines = [line for line in log_text.splitlines() if warning in line]
    assert len(warning_lines) == 1
    assert "selected model=legacy" not in warning_lines[0]


def test_no_secret_or_raw_content_reaches_auto_routing_durable_artifacts(
    isolated_home: Path,
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[3]
    sentinels = (
        "RAW_TASK_SENTINEL",
        "RAW_RESPONSE_SENTINEL",
        "API_KEY_SENTINEL",
        "TOKEN_SENTINEL",
        "user:pass@",
    )
    with LoopbackProvider(response_text="RAW_RESPONSE_SENTINEL") as selected:
        adapter = fresh._LoopbackRuntimeAdapter(
            selected.base_url,
            api_key="API_KEY_SENTINEL",
        )
        service, resolver = fresh._seed_service(
            root=root,
            home=isolated_home,
            adapter=adapter,
            mode="active",
        )
        install_runtime_resolver(monkeypatch, resolver)
        routed_agent = None
        try:
            with _production_agent_log(isolated_home, monkeypatch) as router_log:
                routed_agent, routed_result = fresh._run_agent(
                    baseline_url="https://user:pass@baseline.invalid/v1",
                    baseline_api_key="TOKEN_SENTINEL",
                    context=fresh._context(
                        "artifact-scan",
                        "RAW_TASK_SENTINEL TOKEN_SENTINEL",
                    ),
                    prompt="RAW_TASK_SENTINEL TOKEN_SENTINEL",
                )
            assert routed_result["final_response"] == "RAW_RESPONSE_SENTINEL"
            assert routed_agent.api_key == "API_KEY_SENTINEL"
            assert routed_agent._runtime_routing_binding.action == "project"
            decision = service.store.read_session_decision("artifact-scan")
            assert decision is not None

            class SimulatedProcessDeath(BaseException):
                pass

            class PendingJournalFault:
                @staticmethod
                def after_apply_prepared() -> None:
                    raise SimulatedProcessDeath

            proposal_path = isolated_home / "approved-shadow-proposal.json"
            proposal_path.write_text(
                json.dumps(fresh._authority("shadow")),
                encoding="utf-8",
            )
            preview = service.preview_config(proposal_path)
            service._fault_injector = PendingJournalFault()
            with pytest.raises(SimulatedProcessDeath):
                service.apply_config(
                    proposal_path,
                    expected_config_sha256=preview["expected_config_sha256"],
                )

            export_dir = isolated_home / "auto-routing" / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            status_path = export_dir / "cli-status.json"
            parser = argparse.ArgumentParser(prog="hermes auto-routing")
            build_parser(parser)
            status_args = parser.parse_args(["status", "--json"])
            with status_path.open("w", encoding="utf-8") as stream:
                with redirect_stdout(stream):
                    assert auto_routing_command(status_args, service=service) == 0
            cli_status = json.loads(status_path.read_text(encoding="utf-8"))
            assert cli_status["command"] == "status"
            assert cli_status["incomplete_config_apply"] is True

            explain_path = export_dir / "cli-explain-concise.json"
            explain_args = parser.parse_args(
                ["explain", "--decision-id", decision.decision_id, "--json"]
            )
            with explain_path.open("w", encoding="utf-8") as stream:
                with redirect_stdout(stream):
                    assert auto_routing_command(explain_args, service=service) == 0
            concise_explain = json.loads(
                explain_path.read_text(encoding="utf-8")
            )
            assert concise_explain["command"] == "explain"
            assert concise_explain["detail"] == "concise"
            assert concise_explain["redacted"] is True

            detailed_explain_path = export_dir / "cli-explain-detailed.json"
            detailed_args = parser.parse_args(
                [
                    "explain",
                    "--decision-id",
                    decision.decision_id,
                    "--detailed",
                    "--json",
                ]
            )
            with detailed_explain_path.open("w", encoding="utf-8") as stream:
                with redirect_stdout(stream):
                    assert auto_routing_command(detailed_args, service=service) == 0
            detailed_explain = json.loads(
                detailed_explain_path.read_text(encoding="utf-8")
            )
            assert detailed_explain["command"] == "explain"
            assert detailed_explain["detail"] == "detailed"
            assert detailed_explain["redacted"] is True
            assert detailed_explain["candidates"]
            runtime_log_lines = router_log.read_text(encoding="utf-8").splitlines()
            marker = "runtime_routing="
            runtime_event_lines = [
                line
                for line in runtime_log_lines
                if marker in line and line.split(marker, 1)[1].startswith("{")
            ]
            assert len(runtime_event_lines) == 1
            runtime_event_payload = runtime_event_lines[0].split(marker, 1)[1]
            runtime_event = json.loads(runtime_event_payload)
            assert runtime_event["event"]["post_call_model_failover"] is False
            for sentinel in sentinels:
                assert sentinel not in runtime_event_payload

            state_db = service.store.path
            state_wal = Path(f"{state_db}-wal")
            state_shm = Path(f"{state_db}-shm")
            pending_journals = sorted(
                isolated_home.glob("auto-routing-apply-*.pending.json")
            )
            assert len(pending_journals) == 1
            referenced_backups: list[Path] = []
            for journal_path in pending_journals:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                operation_id = journal["operation_id"]
                assert Path(journal["config_path"]) == service.config_path
                backup_path = Path(journal["backup_path"])
                assert backup_path == isolated_home / (
                    f"config.yaml.auto-routing.{operation_id}.bak"
                )
                referenced_backups.append(backup_path)
            all_backups = sorted(
                isolated_home.glob("config.yaml.auto-routing.*.bak")
            )
            assert set(referenced_backups).issubset(all_backups)

            explicit_artifacts = [
                state_db,
                state_wal,
                state_shm,
                service.config_path,
                *pending_journals,
                *all_backups,
                status_path,
                explain_path,
                detailed_explain_path,
            ]
            auto_routing_files = sorted(
                path
                for path in (isolated_home / "auto-routing").rglob("*")
                if path.is_file()
            )
            exact_artifacts = sorted(set(explicit_artifacts + auto_routing_files))
            assert all(path.exists() for path in exact_artifacts), exact_artifacts
            # ``agent.log`` is a shared Hermes transcript/activity sink and may
            # contain ordinary non-router turn records.  The Auto contract is
            # enforced against the actual production routing record above;
            # plugin-owned durable files are scanned in full below.
            assert router_log.exists()
            for artifact in exact_artifacts:
                payload = artifact.read_bytes()
                for sentinel in sentinels:
                    assert sentinel.encode() not in payload, (artifact, sentinel)
        finally:
            if routed_agent is not None:
                routed_agent.close()
            resolver.close()
            service.store.close()


def test_two_profile_homes_in_one_process_never_share_route_decisions(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[3]
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    with LoopbackProvider(response_text="unused") as selected:
        service_a, resolver_a = fresh._seed_service(
            root=root,
            home=home_a,
            adapter=fresh._LoopbackRuntimeAdapter(selected.base_url),
            mode="shadow",
        )
        service_b, resolver_b = fresh._seed_service(
            root=root,
            home=home_b,
            adapter=fresh._LoopbackRuntimeAdapter(selected.base_url),
            mode="shadow",
        )
        request_a = AgentRuntimeRequest(
            contract_version=RUNTIME_ROUTING_CONTRACT_VERSION,
            context=_agent_context("same-public-session", "profile A task"),
            baseline=_baseline_spec("https://baseline.invalid/v1"),
        )
        request_b = AgentRuntimeRequest(
            contract_version=RUNTIME_ROUTING_CONTRACT_VERSION,
            context=_agent_context("same-public-session", "profile B task"),
            baseline=_baseline_spec("https://baseline.invalid/v1"),
        )
        try:
            plan_a = resolver_a.resolve(request_a)
            plan_b = resolver_b.resolve(request_b)
            decision_a = service_a.store.read_session_decision("same-public-session")
            decision_b = service_b.store.read_session_decision("same-public-session")
        finally:
            resolver_a.close()
            resolver_b.close()
            service_a.store.close()
            service_b.store.close()

    assert plan_a.action == plan_b.action == "shadow"
    assert decision_a is not None and decision_b is not None
    assert decision_a.decision_id != decision_b.decision_id
    with RoutingStore.open(home=home_a) as store_a, RoutingStore.open(
        home=home_b
    ) as store_b:
        assert store_a.count_decisions() == store_b.count_decisions() == 1
        assert store_a.read_decision(decision_b.decision_id) is None
        assert store_b.read_decision(decision_a.decision_id) is None


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        ("sqlite_lock", "plugin_state_unavailable"),
        ("sqlite_corruption", "plugin_state_unavailable"),
        ("catalog_unavailable", "resolver_error"),
        ("no_eligible_target", "resolver_error"),
        ("adapter_drift", "adapter_incompatible"),
    ],
)
def test_full_construction_failures_never_reach_an_uncommitted_selected_runtime(
    isolated_home: Path,
    monkeypatch,
    failure,
    expected_reason,
) -> None:
    root = Path(__file__).resolve().parents[3]
    with (
        LoopbackProvider(response_text="baseline") as baseline,
        LoopbackProvider(response_text="selected-must-not-run") as selected,
    ):
        adapter = fresh._LoopbackRuntimeAdapter(selected.base_url)
        service, resolver = fresh._seed_service(
            root=root,
            home=isolated_home,
            adapter=adapter,
            mode="active",
        )
        _patch_baseline_resolution(monkeypatch, baseline.base_url)
        if failure == "sqlite_lock":
            monkeypatch.setattr(
                service.store,
                "read_session_binding",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    sqlite3.OperationalError("database is locked")
                ),
            )
        elif failure == "sqlite_corruption":
            monkeypatch.setattr(
                service.store,
                "read_session_binding",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    sqlite3.DatabaseError("database disk image is malformed")
                ),
            )
        elif failure == "catalog_unavailable":
            monkeypatch.setattr(
                "plugins.auto_routing.auto_routing.service.CatalogService",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("catalog unavailable")
                ),
            )
        elif failure == "no_eligible_target":
            adapter.row = replace(
                adapter.row,
                authenticated=False,
                live_attempt_status="failed",
            )
        elif failure == "adapter_drift":
            monkeypatch.setattr(
                adapter,
                "capability_report",
                lambda: {
                    **fresh._LoopbackRuntimeAdapter.capability_report(),
                    "exact_credential_pool": False,
                },
            )
        install_runtime_resolver(monkeypatch, resolver)
        try:
            agent, result = fresh._run_agent(
                baseline_url=baseline.base_url,
                context=fresh._context(
                    f"stage2-failure-{failure}",
                    f"failure injection {failure}",
                ),
                prompt=f"failure injection {failure}",
            )
            try:
                assert result["final_response"].strip() == "baseline"
                assert agent._runtime_routing_binding.reason_code == expected_reason
                assert agent._runtime_fallback_authority == "host"
            finally:
                agent.close()
        finally:
            resolver.close()
            service.store.close()

    assert selected.requests == []
    assert len([request for request in baseline.requests if "messages" in request]) == 1


@pytest.mark.parametrize(
    ("reason_code", "manual_pin"),
    [("routing_off", False), ("manual_runtime_pin", True)],
)
def test_off_and_manual_bypass_preserve_ordinary_host_fallbacks(
    monkeypatch,
    reason_code,
    manual_pin,
) -> None:
    def plan(request):
        return AgentRuntimePlan(
            action="inherit",
            runtime=request.baseline,
            owns_fallbacks=False,
            reason_code=reason_code,
        )

    install_runtime_resolver(monkeypatch, _PlanResolver(plan))
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "openrouter",
            "model": "baseline-model",
            "base_url": "https://baseline.invalid/v1",
            "api_key": "BASELINE_KEY",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(
        AIAgent,
        "_create_openai_client",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    context = AgentRuntimeContext(
        scope="fresh_session",
        task="bypass task",
        session_id=f"bypass-{reason_code}",
        task_id=f"task-{reason_code}",
        manual_runtime_pin=manual_pin,
        manual_pin_source="test" if manual_pin else None,
        metadata={"platform": "cli"},
    )
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        agent = AIAgent(
            model="baseline-model",
            provider="openrouter",
            base_url="https://baseline.invalid/v1",
            api_key="BASELINE_KEY",
            fallback_model={
                "provider": "openrouter",
                "model": "ordinary-host-fallback",
            },
            runtime_routing_context=context,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    try:
        assert agent._runtime_fallback_authority == "host"
        assert agent._runtime_routing_binding.reason_code == reason_code
        assert [entry["model"] for entry in agent._fallback_chain] == [
            "ordinary-host-fallback"
        ]
        host_fallback_client = SimpleNamespace(
            api_key="HOST_FALLBACK_KEY",
            base_url="https://openrouter.ai/api/v1",
            _custom_headers={},
        )
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(host_fallback_client, "ordinary-host-fallback"),
        ) as host_resolver, patch(
            "agent.credential_pool.load_pool",
            return_value=None,
        ):
            assert agent._try_activate_fallback() is True
        host_resolver.assert_called_once()
        assert agent.model == "ordinary-host-fallback"
        assert agent.provider == "openrouter"
    finally:
        agent.close()


def _roles_are_strict(messages: list[dict[str, Any]]) -> bool:
    roles = [message.get("role") for message in messages]
    return all(left != right for left, right in zip(roles, roles[1:]))


def _provider_transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare the transcript fields the provider protocol actually carries."""
    return [
        {
            key: message[key]
            for key in ("role", "content", "tool_calls", "tool_call_id", "name")
            if key in message
        }
        for message in messages
    ]


def test_actual_auto_binding_preserves_prompt_tools_roles_and_transcript_on_replay(
    isolated_home: Path,
    monkeypatch,
) -> None:
    root = Path(__file__).resolve().parents[3]
    parent_id = "stage2-transcript-parent"
    child_id = "stage2-transcript-compressed"
    stable_tool = {
        "type": "function",
        "function": {
            "name": "stage2_contract_probe",
            "description": "Deterministic nonempty Stage 2 schema",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }
    with (
        LoopbackProvider(response_text="baseline-must-not-run") as baseline,
        LoopbackProvider(response_text="selected") as selected,
    ):
        service, resolver = fresh._seed_service(
            root=root,
            home=isolated_home,
            adapter=fresh._LoopbackRuntimeAdapter(selected.base_url),
            mode="active",
        )
        install_runtime_resolver(monkeypatch, resolver)
        agents: list[AIAgent] = []
        try:
            with patch("run_agent.get_tool_definitions", return_value=[stable_tool]):
                plain = AIAgent(
                    api_key="SELECTED_KEY",
                    base_url=selected.base_url,
                    provider="openrouter",
                    model="selected-model",
                    max_iterations=4,
                    quiet_mode=True,
                    skip_context_files=True,
                    skip_memory=True,
                    save_trajectories=False,
                    platform="cli",
                    session_id=parent_id,
                )
                agents.append(plain)
                plain.run_conversation(
                    "first turn",
                    conversation_history=[],
                    task_id=f"task-{parent_id}",
                )
                first_agent, first = fresh._run_agent(
                    baseline_url=baseline.base_url,
                    context=fresh._context(parent_id, "first turn"),
                    prompt="first turn",
                )
                agents.append(first_agent)
                later = first_agent.run_conversation(
                    "later turn",
                    conversation_history=first["messages"],
                    task_id="task-later-turn",
                )
                resume_agent, resumed = fresh._run_agent(
                    baseline_url=baseline.base_url,
                    context=fresh._context(parent_id, None, is_resume=True),
                    prompt="resume turn",
                    history=later["messages"],
                )
                agents.append(resume_agent)
                resolver.record_session_continuation(
                    RuntimeSessionContinuation(
                        parent_session_id=parent_id,
                        child_session_id=child_id,
                        reason="compression",
                    )
                )
                child_agent, _child = fresh._run_agent(
                    baseline_url=baseline.base_url,
                    context=fresh._context(child_id, None, is_resume=True),
                    prompt="compression continuation",
                    history=resumed["messages"],
                )
                agents.append(child_agent)
                assert all(
                    agent._runtime_routing_binding.event.get(
                        "post_call_model_failover"
                    )
                    is False
                    for agent in (first_agent, resume_agent, child_agent)
                )
                assert all(agent._fallback_chain == [] for agent in agents[1:])
        finally:
            for agent in agents:
                agent.close()
            resolver.close()
            service.store.close()

    requests = [request for request in selected.requests if "messages" in request]
    assert len(requests) == 5
    plain_request, *auto_requests = requests
    system_bytes = [
        json.dumps(
            request["messages"][0],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        for request in auto_requests
    ]
    tool_hashes = [
        hashlib.sha256(
            json.dumps(
                request.get("tools", []),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for request in auto_requests
    ]
    assert plain_request.get("tools") == [stable_tool]
    assert all(request.get("tools") == [stable_tool] for request in auto_requests)
    assert auto_requests[0]["messages"][0] == plain_request["messages"][0]
    assert len(set(system_bytes)) == 1
    assert len(set(tool_hashes)) == 1
    assert all(_roles_are_strict(request["messages"]) for request in auto_requests)
    assert auto_requests[1]["messages"][1:-1] == _provider_transcript(first["messages"])
    assert auto_requests[2]["messages"][1:-1] == _provider_transcript(later["messages"])
    assert auto_requests[3]["messages"][1:-1] == _provider_transcript(resumed["messages"])
    assert baseline.requests == []
