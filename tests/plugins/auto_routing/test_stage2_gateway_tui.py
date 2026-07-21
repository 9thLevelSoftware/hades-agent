from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
import types
from collections import OrderedDict

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from tui_gateway import server


def _gateway_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = None
    runner.session_store = None
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._fallback_model = []
    return runner


class _GatewayImageAdapter:
    platform = Platform.TELEGRAM

    async def send(self, *_args, **_kwargs):
        return types.SimpleNamespace(success=True, message_id="sent-1")

    async def send_typing(self, *_args, **_kwargs):
        return None

    async def stop_typing(self, *_args, **_kwargs):
        return None

    def extract_media(self, response):
        return [], response

    def extract_images(self, response):
        return [], response

    def get_pending_message(self, _session_key):
        return None


def _gateway_image_runner() -> gateway_run.GatewayRunner:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")}
    )
    runner.adapters = {Platform.TELEGRAM: _GatewayImageAdapter()}
    runner.session_store = None
    runner._session_db = None
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_native_image_paths_by_session = {}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = []
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = types.SimpleNamespace(loaded_hooks=False)
    return runner


def _gateway_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="routing-images",
        chat_type="dm",
        user_id="user-1",
        user_name="Image User",
    )


def _selected_gateway_handoff(api_mode="selected-api-mode") -> dict:
    return {
        "model": "selected-vision-model",
        "runtime": {
            "provider": "selected-provider",
            "base_url": "https://selected.invalid/v1",
            "api_key": "selected-key",
            "api_mode": api_mode,
            "command": None,
            "args": [],
            "credential_pool": None,
        },
        "reasoning_config": None,
        "fallback_model": [],
        "context": types.SimpleNamespace(task=None),
        "prepared": types.SimpleNamespace(
            plan=types.SimpleNamespace(bound_route_identity="selected/vision")
        ),
    }


def _install_selected_gateway_image_runtime(
    monkeypatch,
    runner,
    *,
    selected_api_mode="selected-api-mode",
):
    events = []
    run_messages = []

    async def fake_enrich(text, paths):
        events.append(("enrich", text, list(paths)))
        return f"[vision summary]\n\n{text}"

    def fake_decide(**kwargs):
        events.append(("decide", dict(kwargs)))
        selected = (
            kwargs.get("provider") == "selected-provider"
            and kwargs.get("model") == "selected-vision-model"
            and kwargs.get("api_mode") == selected_api_mode
        )
        if not selected or selected_api_mode == "codex_app_server":
            return "text"
        return "native"

    def fake_route(**kwargs):
        events.append(("route", kwargs["task"]))
        return _selected_gateway_handoff(selected_api_mode)

    class FakeAgent:
        def __init__(self, **kwargs):
            self.model = kwargs["model"]
            self.provider = kwargs["provider"]
            self.base_url = kwargs.get("base_url")
            self.api_key = kwargs.get("api_key")
            self.api_mode = kwargs.get("api_mode")
            plan = kwargs["prepared_agent_runtime"].plan
            self._runtime_routing_binding = types.SimpleNamespace(
                action="project",
                runtime=types.SimpleNamespace(reasoning_config=None),
                bound_route_identity=plan.bound_route_identity,
            )
            self.tools = []

        def run_conversation(
            self,
            message=None,
            *,
            user_message=None,
            **_kwargs,
        ):
            run_messages.append(
                user_message if user_message is not None else message
            )
            return {"final_response": "done", "messages": [], "api_calls": 1}

    async def run_in_thread(fn):
        return await asyncio.to_thread(fn)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda _cfg, _platform: set(),
    )
    monkeypatch.setattr(runner, "_runtime_routing_requires_initial_task", lambda: True)
    monkeypatch.setattr(runner, "_prepare_gateway_agent_runtime", fake_route)
    monkeypatch.setattr(runner, "_decide_image_input_mode", fake_decide)
    monkeypatch.setattr(runner, "_enrich_message_with_vision", fake_enrich)
    monkeypatch.setattr(runner, "_load_service_tier", lambda: None)
    monkeypatch.setattr(runner, "_refresh_fallback_model", lambda: [])
    monkeypatch.setattr(runner, "_cleanup_agent_resources", lambda _agent: None)
    monkeypatch.setattr(runner, "_run_in_executor_with_context", run_in_thread)
    monkeypatch.setattr(
        runner,
        "_resolve_turn_agent_config",
        lambda _task, model, runtime: {
            "model": model,
            "runtime": runtime,
            "request_overrides": {},
        },
    )
    return events, run_messages


def _assert_selected_gateway_image_flow(
    events,
    run_messages,
    *,
    image_path,
    image_bytes,
    source,
    session_key,
    selected_api_mode="selected-api-mode",
    expect_native=True,
):
    assert events[0][0] == "route"
    routing_task = events[0][1]
    assert routing_task[0] == {"type": "text", "text": "inspect this"}
    assert routing_task[1]["type"] == "image"
    assert routing_task[1]["data"] == image_bytes
    assert str(image_path) not in repr(routing_task)
    assert events[1] == (
        "decide",
        {
            "source": source,
            "session_key": session_key,
            "user_config": {},
            "provider": "selected-provider",
            "model": "selected-vision-model",
            "api_mode": selected_api_mode,
        },
    )
    if expect_native:
        assert all(event[0] != "enrich" for event in events)
        assert run_messages[0][0]["type"] == "text"
        assert run_messages[0][0]["text"].startswith("inspect this")
        assert any(part.get("type") == "image_url" for part in run_messages[0])
    else:
        assert events[2] == (
            "enrich",
            "inspect this",
            [str(image_path)],
        )
        assert run_messages == ["[vision summary]\n\ninspect this"]


@pytest.mark.asyncio
async def test_gateway_foreground_routes_clean_multimodal_task_then_shapes_for_selected_agent(
    monkeypatch,
    tmp_path,
):
    image_path = tmp_path / "private-screenshot.png"
    image_bytes = b"foreground-image-bytes"
    image_path.write_bytes(image_bytes)
    runner = _gateway_image_runner()
    source = _gateway_source()
    events, run_messages = _install_selected_gateway_image_runtime(
        monkeypatch,
        runner,
    )

    prepared_text = await runner._prepare_inbound_message_text(
        event=MessageEvent(
            text="inspect this",
            message_type=MessageType.PHOTO,
            source=source,
            media_urls=[str(image_path)],
            media_types=["image/png"],
        ),
        source=source,
        history=[],
        session_key="gateway-image-session",
    )
    result = await runner._run_agent(
        message=prepared_text,
        context_prompt="",
        history=[],
        source=source,
        session_id="gateway-image-session-id",
        session_key="gateway-image-session",
    )

    assert result["final_response"] == "done"
    _assert_selected_gateway_image_flow(
        events,
        run_messages,
        image_path=image_path,
        image_bytes=image_bytes,
        source=source,
        session_key="gateway-image-session",
    )


@pytest.mark.asyncio
async def test_gateway_branch_first_prompt_binds_fresh_then_second_turn_reuses_route(
    monkeypatch,
    tmp_path,
):
    """A branch is independent even though its initial transcript is copied."""
    from hermes_state import AsyncSessionDB, SessionDB

    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session("branch-parent", source="telegram")
    db.end_session("branch-parent", "branched")
    db.create_session(
        "branch-child",
        source="telegram",
        model_config={
            "_branched_from": "branch-parent",
            "_branch_point_message_count": 2,
        },
        parent_session_id="branch-parent",
    )
    copied_history = [
        {"role": "user", "content": "parent question"},
        {"role": "assistant", "content": "parent answer"},
    ]
    for message in copied_history:
        db.append_message(
            "branch-child",
            role=message["role"],
            content=message["content"],
        )

    runner = _gateway_image_runner()
    runner._session_db = AsyncSessionDB(db)
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._enforce_agent_cache_cap = lambda: None
    source = _gateway_source()
    route_calls = []
    _events, run_messages = _install_selected_gateway_image_runtime(
        monkeypatch,
        runner,
    )

    def route_branch(**kwargs):
        route_calls.append(kwargs)
        return _selected_gateway_handoff()

    monkeypatch.setattr(runner, "_prepare_gateway_agent_runtime", route_branch)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_gateway_model",
        lambda _cfg=None: "selected-vision-model",
    )
    monkeypatch.setattr(
        "agent.title_generator.maybe_auto_title",
        lambda *_args, **_kwargs: None,
    )

    first = await runner._run_agent(
        message="new branch direction",
        context_prompt="",
        history=copied_history,
        source=source,
        session_id="branch-child",
        session_key="gateway-branch-session",
    )
    assert first["final_response"] == "done"

    db.append_message("branch-child", role="user", content="new branch direction")
    db.append_message("branch-child", role="assistant", content="done")
    await runner._refresh_agent_cache_message_count(
        "gateway-branch-session",
        "branch-child",
    )
    second_history = copied_history + [
        {"role": "user", "content": "new branch direction"},
        {"role": "assistant", "content": "done"},
    ]
    assert runner._gateway_runtime_is_resume(
        session_id="branch-child",
        history=second_history,
    ) is True
    second = await runner._run_agent(
        message="continue branch",
        context_prompt="",
        history=second_history,
        source=source,
        session_id="branch-child",
        session_key="gateway-branch-session",
    )

    db.append_message("branch-child", role="user", content="continue branch")
    db.append_message("branch-child", role="assistant", content="done")
    await runner._refresh_agent_cache_message_count(
        "gateway-branch-session",
        "branch-child",
    )
    with runner._agent_cache_lock:
        runner._agent_cache.pop("gateway-branch-session")
    third_history = second_history + [
        {"role": "user", "content": "continue branch"},
        {"role": "assistant", "content": "done"},
    ]
    third = await runner._run_agent(
        message="cold branch continuation",
        context_prompt="",
        history=third_history,
        source=source,
        session_id="branch-child",
        session_key="gateway-branch-session",
    )

    assert second["final_response"] == "done"
    assert third["final_response"] == "done"
    assert route_calls[0]["is_resume"] is False
    assert route_calls[0]["task"] == "new branch direction"
    assert route_calls[1]["is_resume"] is True
    assert len(route_calls) == 2
    assert run_messages == [
        "new branch direction",
        "continue branch",
        "cold branch continuation",
    ]
    with runner._agent_cache_lock:
        cached = runner._agent_cache["gateway-branch-session"]
    assert cached[3:] == ("branch-child", "selected/vision")


@pytest.mark.asyncio
async def test_gateway_background_routes_clean_multimodal_task_then_shapes_for_selected_agent(
    monkeypatch,
    tmp_path,
):
    image_path = tmp_path / "private-background.png"
    image_bytes = b"background-image-bytes"
    image_path.write_bytes(image_bytes)
    runner = _gateway_image_runner()
    source = _gateway_source()
    events, run_messages = _install_selected_gateway_image_runtime(
        monkeypatch,
        runner,
        selected_api_mode="codex_app_server",
    )

    await runner._run_background_task(
        "inspect this",
        source,
        "background-image-task",
        media_urls=[str(image_path)],
        media_types=["image/png"],
    )

    _assert_selected_gateway_image_flow(
        events,
        run_messages,
        image_path=image_path,
        image_bytes=image_bytes,
        source=source,
        session_key=runner._session_key_for_source(source),
        selected_api_mode="codex_app_server",
        expect_native=False,
    )


@pytest.mark.parametrize(
    ("api_mode", "expect_native"),
    [("chat_completions", True), ("codex_app_server", False)],
)
def test_tui_prompt_shaping_uses_live_selected_runtime_not_process_globals(
    monkeypatch,
    tmp_path,
    api_mode,
    expect_native,
):
    image_path = tmp_path / "selected-runtime.png"
    image_path.write_bytes(b"tui-image-bytes")
    decisions = []
    run_messages = []

    class ImmediateThread:
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

        def is_alive(self):
            return False

    class FakeAgent:
        model = "selected-live-model"
        provider = "selected-live-provider"
        base_url = "https://selected.invalid/v1"
        api_key = "selected-key"

        def __init__(self):
            self.api_mode = api_mode

        def run_conversation(
            self,
            message,
            conversation_history=None,
            stream_callback=None,
            task_id=None,
        ):
            run_messages.append(message)
            return {
                "final_response": "done",
                "messages": [{"role": "assistant", "content": "done"}],
            }

    def fake_decide(provider, model, _cfg):
        decisions.append((provider, model))
        return "native"

    session = {
        "agent": FakeAgent(),
        "session_key": "tui-selected-runtime",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": True,
        "attached_images": [str(image_path)],
        "cols": 80,
        "show_reasoning": False,
        "tool_progress_mode": "all",
    }
    server._sessions["tui-selected-runtime"] = session
    monkeypatch.setattr(
        server,
        "threading",
        types.SimpleNamespace(Thread=ImmediateThread),
    )
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "make_stream_renderer", lambda _cols: None)
    monkeypatch.setattr(server, "render_message", lambda _raw, _cols: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda *_args: None)
    monkeypatch.setattr(server, "_enrich_with_attached_images", lambda text, _images: f"[vision]\n\n{text}")
    monkeypatch.setattr("agent.image_routing.decide_image_input_mode", fake_decide)
    monkeypatch.setattr("agent.auxiliary_client._read_main_provider", lambda: "wrong-global-provider")
    monkeypatch.setattr("agent.auxiliary_client._read_main_model", lambda: "wrong-global-model")
    monkeypatch.setattr(
        "agent.title_generator.maybe_auto_title",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})

    try:
        server._run_prompt_submit("request-1", "tui-selected-runtime", session, "inspect this")
    finally:
        server._sessions.pop("tui-selected-runtime", None)

    assert decisions == [("selected-live-provider", "selected-live-model")]
    if expect_native:
        assert run_messages[0][0]["type"] == "text"
        assert run_messages[0][0]["text"].startswith("inspect this")
        assert any(part.get("type") == "image_url" for part in run_messages[0])
    else:
        assert run_messages == ["[vision]\n\ninspect this"]


def test_gateway_active_route_is_selected_before_unavailable_baseline(monkeypatch):
    import agent.runtime_routing as routing

    target = routing.AgentRuntimeSpec(
        model="selected-model",
        provider="openrouter",
        base_url="https://selected.invalid/v1",
        api_key="selected-key",
        resolution_state="resolved",
        api_mode="chat_completions",
    )

    def fake_prepare(request):
        return routing._new_prepared(
            request,
            routing.AgentRuntimePlan(
                action="project",
                runtime=target,
                decision_id="decision-gateway-1",
                bound_route_identity="openrouter/selected-model",
                owns_fallbacks=True,
                reason_code="active_projected",
            ),
        )

    runner = _gateway_runner()
    monkeypatch.setattr(routing, "prepare_agent_runtime", fake_prepare)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_gateway_model",
        lambda _cfg=None: "unavailable-baseline",
    )
    monkeypatch.setattr(runner, "_refresh_fallback_model", lambda: [])
    monkeypatch.setattr(
        runner,
        "_resolve_session_reasoning_config",
        lambda **_kwargs: None,
    )
    baseline_attempts = []

    def unavailable_baseline(**_kwargs):
        baseline_attempts.append(True)
        raise RuntimeError("baseline unavailable")

    monkeypatch.setattr(runner, "_resolve_session_agent_runtime", unavailable_baseline)
    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="cli",
        user_id="user-1",
        chat_type="dm",
    )

    handoff = runner._prepare_gateway_agent_runtime(
        task="clean first task",
        source=source,
        session_id="session-1",
        session_key="agent-main-local",
        user_config={},
        is_resume=False,
    )

    assert baseline_attempts == []
    assert handoff["model"] == "selected-model"
    assert handoff["runtime"]["provider"] == "openrouter"
    assert handoff["context"].task == "clean first task"
    assert handoff["context"].metadata["platform"] == "local"
    assert handoff["prepared"].plan.decision_id == "decision-gateway-1"


def test_gateway_fallback_refresh_does_not_overwrite_plugin_owned_chain():
    plugin_chain = [{"provider": "selected", "model": "fallback"}]
    agent = types.SimpleNamespace(
        _runtime_fallback_authority="plugin",
        _fallback_chain=list(plugin_chain),
        _fallback_model=plugin_chain[0],
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
    )

    gateway_run.GatewayRunner._apply_fallback_chain_to_agent(
        agent,
        [{"provider": "host", "model": "must-not-appear"}],
    )

    assert agent._fallback_chain == plugin_chain
    assert agent._fallback_model == plugin_chain[0]


def test_gateway_cached_projected_runtime_keeps_bound_reasoning():
    import agent.runtime_routing as routing

    selected = routing.AgentRuntimeSpec(
        model="selected-model",
        provider="openrouter",
        base_url="https://selected.invalid/v1",
        api_key="selected-key",
        resolution_state="resolved",
        api_mode="chat_completions",
        reasoning_config={"enabled": True, "effort": "high"},
    )
    binding = routing.RuntimeRoutingBinding(
        scope="fresh_session",
        session_id="session-1",
        task_id="task-1",
        operation_id=None,
        action="project",
        runtime=selected,
        decision_id="decision-1",
        bound_route_identity="route-1",
        owns_fallbacks=True,
        reason_code="active_projected",
    )
    agent = types.SimpleNamespace(
        model="selected-model",
        provider="openrouter",
        base_url="https://selected.invalid/v1",
        api_key="selected-key",
        api_mode="chat_completions",
        reasoning_config={"enabled": True, "effort": "low"},
        _runtime_routing_binding=binding,
    )

    _model, runtime = gateway_run.GatewayRunner._effective_runtime_from_agent(agent)

    assert runtime["reasoning_config"] == {"enabled": True, "effort": "high"}


def test_gateway_pending_model_switch_records_generic_manual_transition(monkeypatch):
    import agent.runtime_routing as routing

    calls = []
    runner = _gateway_runner()
    monkeypatch.setattr(runner, "_refresh_fallback_model", lambda: [])
    monkeypatch.setattr(
        routing,
        "apply_manual_runtime_transition",
        lambda agent, **kwargs: calls.append((agent, kwargs)),
    )

    runner._record_gateway_manual_runtime_transition(
        agent=None,
        session_key="gateway-session",
        source_code="gateway_model_command",
        model="manual-model",
        provider="openrouter",
        base_url="https://manual.invalid/v1",
        api_key="manual-key",
        api_mode="chat_completions",
    )

    assert calls[0][0] is None
    assert calls[0][1]["session_id"] == "gateway-session"
    assert calls[0][1]["runtime"].model == "manual-model"
    assert calls[0][1]["runtime"].provider == "openrouter"


def test_api_default_alias_is_unpinned_and_routes_before_baseline(monkeypatch):
    import agent.runtime_routing as routing

    target = routing.AgentRuntimeSpec(
        model="vision-model",
        provider="openrouter",
        base_url="https://vision.invalid/v1",
        api_key="vision-key",
        resolution_state="resolved",
        api_mode="chat_completions",
    )
    requests = []

    def fake_prepare(request):
        requests.append(request)
        return routing._new_prepared(
            request,
            routing.AgentRuntimePlan(
                action="project",
                runtime=target,
                decision_id="decision-api-1",
                bound_route_identity="openrouter/vision-model",
                owns_fallbacks=True,
                reason_code="active_projected",
            ),
        )

    adapter = object.__new__(APIServerAdapter)
    monkeypatch.setattr(routing, "prepare_agent_runtime", fake_prepare)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        gateway_run,
        "_resolve_gateway_model",
        lambda _cfg=None: "hermes-agent",
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_load_reasoning_config",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_load_fallback_model",
        staticmethod(lambda: []),
    )
    monkeypatch.setattr(
        adapter,
        "_session_model_override_for",
        lambda _key: None,
    )
    baseline_attempts = []

    def unavailable_baseline(**_kwargs):
        baseline_attempts.append(True)
        raise RuntimeError("baseline unavailable")

    monkeypatch.setattr(adapter, "_resolve_api_baseline_runtime", unavailable_baseline)
    first_task = [
        {"type": "input_text", "text": "inspect this"},
        {
            "type": "input_image",
            "image_url": {"url": "data:image/png;base64,aGVsbG8="},
        },
    ]

    handoff = adapter._prepare_api_agent_runtime(
        initial_task=first_task,
        conversation_history=[],
        session_id="api-session-1",
        gateway_session_key=None,
        route=None,
    )

    assert baseline_attempts == []
    assert handoff["model"] == "vision-model"
    assert requests[0].context.task == first_task
    assert requests[0].context.manual_runtime_pin is False
    assert requests[0].context.metadata["platform"] == "api_server"


def test_api_configured_model_route_is_a_manual_pin(monkeypatch):
    import agent.runtime_routing as routing

    target = routing.AgentRuntimeSpec(
        model="configured-model",
        provider="openrouter",
        base_url="https://configured.invalid/v1",
        api_key="configured-key",
        resolution_state="resolved",
        api_mode="chat_completions",
    )
    contexts = []

    def fake_prepare(request):
        contexts.append(request.context)
        return routing._new_prepared(
            request,
            routing.AgentRuntimePlan(
                action="project",
                runtime=target,
                decision_id="decision-api-manual",
                bound_route_identity="openrouter/configured-model",
                owns_fallbacks=True,
                reason_code="active_projected",
            ),
        )

    adapter = object.__new__(APIServerAdapter)
    monkeypatch.setattr(routing, "prepare_agent_runtime", fake_prepare)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda _cfg=None: "hermes-agent")
    monkeypatch.setattr(gateway_run.GatewayRunner, "_load_reasoning_config", staticmethod(lambda: None))
    monkeypatch.setattr(gateway_run.GatewayRunner, "_load_fallback_model", staticmethod(lambda: []))
    monkeypatch.setattr(adapter, "_session_model_override_for", lambda _key: None)

    adapter._prepare_api_agent_runtime(
        initial_task="task",
        conversation_history=[],
        session_id="api-session-2",
        gateway_session_key=None,
        route={"model": "configured-model", "provider": "openrouter"},
    )

    assert contexts[0].manual_runtime_pin is True
    assert contexts[0].manual_pin_source == "api_configured_model_route"


def test_api_caller_supplied_history_is_not_a_persisted_resume(monkeypatch):
    import agent.runtime_routing as routing

    target = routing.AgentRuntimeSpec(
        model="selected-model",
        provider="openrouter",
        base_url="https://selected.invalid/v1",
        api_key="selected-key",
        resolution_state="resolved",
        api_mode="chat_completions",
    )
    contexts = []

    def fake_prepare(request):
        contexts.append(request.context)
        return routing._new_prepared(
            request,
            routing.AgentRuntimePlan(
                action="project",
                runtime=target,
                decision_id="decision-api-history",
                bound_route_identity="route-api-history",
                owns_fallbacks=True,
                reason_code="active_projected",
            ),
        )

    adapter = object.__new__(APIServerAdapter)
    monkeypatch.setattr(routing, "prepare_agent_runtime", fake_prepare)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda _cfg=None: "hermes-agent")
    monkeypatch.setattr(gateway_run.GatewayRunner, "_load_reasoning_config", staticmethod(lambda: None))
    monkeypatch.setattr(gateway_run.GatewayRunner, "_load_fallback_model", staticmethod(lambda: []))
    monkeypatch.setattr(adapter, "_session_model_override_for", lambda _key: None)
    monkeypatch.setattr(adapter, "_api_session_is_persisted", lambda _sid: False, raising=False)

    adapter._prepare_api_agent_runtime(
        initial_task="current first task",
        conversation_history=[
            {"role": "user", "content": "caller context"},
            {"role": "assistant", "content": "caller context response"},
        ],
        session_id="new-api-session",
        gateway_session_key=None,
        route=None,
    )

    assert contexts[0].is_resume is False
    assert contexts[0].task == "current first task"


def test_api_persisted_session_replays_without_current_task(monkeypatch):
    import agent.runtime_routing as routing

    target = routing.AgentRuntimeSpec(
        model="selected-model",
        provider="openrouter",
        base_url="https://selected.invalid/v1",
        api_key="selected-key",
        resolution_state="resolved",
        api_mode="chat_completions",
    )
    contexts = []

    def fake_prepare(request):
        contexts.append(request.context)
        return routing._new_prepared(
            request,
            routing.AgentRuntimePlan(
                action="project",
                runtime=target,
                decision_id="decision-api-resume",
                bound_route_identity="route-api-resume",
                owns_fallbacks=True,
                reason_code="active_projected",
            ),
        )

    adapter = object.__new__(APIServerAdapter)
    monkeypatch.setattr(routing, "prepare_agent_runtime", fake_prepare)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda _cfg=None: "hermes-agent")
    monkeypatch.setattr(gateway_run.GatewayRunner, "_load_reasoning_config", staticmethod(lambda: None))
    monkeypatch.setattr(gateway_run.GatewayRunner, "_load_fallback_model", staticmethod(lambda: []))
    monkeypatch.setattr(adapter, "_session_model_override_for", lambda _key: None)
    monkeypatch.setattr(adapter, "_api_session_is_persisted", lambda _sid: True, raising=False)

    adapter._prepare_api_agent_runtime(
        initial_task="must not reclassify",
        conversation_history=[],
        session_id="persisted-api-session",
        gateway_session_key=None,
        route=None,
    )

    assert contexts[0].is_resume is True
    assert contexts[0].task is None


def test_api_empty_created_session_routes_first_prompt_then_replays_later(
    monkeypatch,
    tmp_path,
):
    """Creating a REST session row is not itself a completed first turn."""
    import agent.runtime_routing as routing
    from hermes_state import SessionDB

    target = routing.AgentRuntimeSpec(
        model="selected-model",
        provider="openrouter",
        base_url="https://selected.invalid/v1",
        api_key="selected-key",
        resolution_state="resolved",
        api_mode="chat_completions",
    )
    contexts = []

    def fake_prepare(request):
        contexts.append(request.context)
        return routing._new_prepared(
            request,
            routing.AgentRuntimePlan(
                action="project",
                runtime=target,
                decision_id="decision-api-created-session",
                bound_route_identity="route-api-created-session",
                owns_fallbacks=True,
                reason_code=(
                    "recorded_replay"
                    if request.context.is_resume
                    else "active_projected"
                ),
            ),
        )

    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session(
        "created-api-session",
        "api_server",
        model="hermes-agent",
    )
    adapter = object.__new__(APIServerAdapter)
    adapter._session_db = db
    monkeypatch.setattr(routing, "prepare_agent_runtime", fake_prepare)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        gateway_run,
        "_resolve_gateway_model",
        lambda _cfg=None: "hermes-agent",
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_load_reasoning_config",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_load_fallback_model",
        staticmethod(lambda: []),
    )
    monkeypatch.setattr(adapter, "_session_model_override_for", lambda _key: None)

    first = adapter._prepare_api_agent_runtime(
        initial_task="classify this first prompt",
        conversation_history=[],
        session_id="created-api-session",
        gateway_session_key=None,
        route=None,
    )

    db.append_message(
        "created-api-session",
        role="user",
        content="classify this first prompt",
    )
    db.append_message(
        "created-api-session",
        role="assistant",
        content="first response",
    )
    replay = adapter._prepare_api_agent_runtime(
        initial_task="must not reclassify",
        conversation_history=db.get_messages("created-api-session"),
        session_id="created-api-session",
        gateway_session_key=None,
        route=None,
    )

    assert contexts[0].is_resume is False
    assert contexts[0].task == "classify this first prompt"
    assert contexts[1].is_resume is True
    assert contexts[1].task is None
    assert first["prepared"].plan.decision_id == replay["prepared"].plan.decision_id
    assert (
        first["prepared"].plan.bound_route_identity
        == replay["prepared"].plan.bound_route_identity
    )


def test_api_resume_detection_requires_durable_lifecycle_evidence(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "sessions.db")
    for session_id in (
        "empty",
        "branch-point-only",
        "transcript",
        "binding",
        "ended",
        "unreadable",
    ):
        db.create_session(session_id, "api_server", model="hermes-agent")
    db.update_session_meta(
        "branch-point-only",
        json.dumps({"_branch_point_message_count": 0}),
        model="hermes-agent",
    )
    db.append_message("transcript", role="user", content="prior turn")
    db.update_session_meta(
        "binding",
        json.dumps(
            {
                "model": "selected-model",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            }
        ),
        model="selected-model",
    )
    db.end_session("ended", "agent_close")
    db._conn.execute(
        "UPDATE sessions SET model_config = ? WHERE id = ?",
        ("{not-json", "unreadable"),
    )
    db._conn.commit()

    adapter = object.__new__(APIServerAdapter)
    adapter._session_db = db

    assert adapter._api_session_is_persisted("empty") is False
    assert adapter._api_session_is_persisted("branch-point-only") is False
    assert adapter._api_session_is_persisted("transcript") is True
    assert adapter._api_session_is_persisted("binding") is True
    assert adapter._api_session_is_persisted("ended") is True
    assert adapter._api_session_is_persisted("unreadable") is True


def test_api_fork_routes_first_child_prompt_then_replays_child_binding(
    monkeypatch,
    tmp_path,
):
    import agent.runtime_routing as routing
    from hermes_state import SessionDB

    target = routing.AgentRuntimeSpec(
        model="fork-selected-model",
        provider="openrouter",
        base_url="https://selected.invalid/v1",
        api_key="fork-selected-key",
        resolution_state="resolved",
        api_mode="chat_completions",
    )
    contexts = []

    def fake_prepare(request):
        contexts.append(request.context)
        return routing._new_prepared(
            request,
            routing.AgentRuntimePlan(
                action="project",
                runtime=target,
                decision_id="decision-api-fork-child",
                bound_route_identity="route-api-fork-child",
                owns_fallbacks=True,
                reason_code=(
                    "recorded_replay"
                    if request.context.is_resume
                    else "active_projected"
                ),
            ),
        )

    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session("fork-parent", "api_server")
    db.end_session("fork-parent", "branched")
    db.create_session(
        "fork-child",
        "api_server",
        model_config={
            "_branched_from": "fork-parent",
            "_branch_point_message_count": 2,
        },
        parent_session_id="fork-parent",
    )
    db.append_message("fork-child", role="user", content="parent question")
    db.append_message("fork-child", role="assistant", content="parent answer")

    adapter = object.__new__(APIServerAdapter)
    adapter._session_db = db
    monkeypatch.setattr(routing, "prepare_agent_runtime", fake_prepare)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        gateway_run,
        "_resolve_gateway_model",
        lambda _cfg=None: "hermes-agent",
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_load_reasoning_config",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_load_fallback_model",
        staticmethod(lambda: []),
    )
    monkeypatch.setattr(adapter, "_session_model_override_for", lambda _key: None)

    first = adapter._prepare_api_agent_runtime(
        initial_task="take the fork in a new direction",
        conversation_history=db.get_messages("fork-child"),
        session_id="fork-child",
        gateway_session_key=None,
        route=None,
    )
    db.append_message(
        "fork-child",
        role="user",
        content="take the fork in a new direction",
    )
    db.append_message("fork-child", role="assistant", content="fork answer")
    second = adapter._prepare_api_agent_runtime(
        initial_task="continue the fork",
        conversation_history=db.get_messages("fork-child"),
        session_id="fork-child",
        gateway_session_key=None,
        route=None,
    )

    assert contexts[0].is_resume is False
    assert contexts[0].task == "take the fork in a new direction"
    assert contexts[1].is_resume is True
    assert contexts[1].task is None
    assert first["prepared"].plan.decision_id == second["prepared"].plan.decision_id
    assert (
        first["prepared"].plan.bound_route_identity
        == second["prepared"].plan.bound_route_identity
    )


def test_api_runtime_metadata_merge_preserves_branch_boundary_without_secrets(
    tmp_path,
):
    import agent.runtime_routing as routing
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session(
        "fork-child",
        "api_server",
        model_config={
            "_branched_from": "fork-parent",
            "_branch_point_message_count": 2,
        },
    )
    runtime = routing.AgentRuntimeSpec(
        model="selected-model",
        provider="openrouter",
        base_url="https://selected.invalid/v1",
        api_key="must-never-reach-session-db",
        resolution_state="resolved",
        api_mode="chat_completions",
        reasoning_config={"enabled": True, "effort": "high"},
    )
    binding = routing.RuntimeRoutingBinding(
        scope="fresh_session",
        session_id="fork-child",
        task_id="api-fork-child",
        operation_id=None,
        action="project",
        runtime=runtime,
        decision_id="decision-api-fork-child",
        bound_route_identity="route-api-fork-child",
        owns_fallbacks=True,
        reason_code="active_projected",
    )
    adapter = object.__new__(APIServerAdapter)
    adapter._session_db = db

    adapter._sync_api_session_runtime_metadata(
        "fork-child",
        types.SimpleNamespace(_runtime_routing_binding=binding),
    )

    row = db.get_session("fork-child")
    persisted = json.loads(row["model_config"])
    assert persisted["_branched_from"] == "fork-parent"
    assert persisted["_branch_point_message_count"] == 2
    assert persisted["model"] == "selected-model"
    assert persisted["provider"] == "openrouter"
    assert persisted["api_mode"] == "chat_completions"
    assert "must-never-reach-session-db" not in row["model_config"]


def test_tui_session_create_stays_unbuilt_until_first_task_when_router_requires_it(
    monkeypatch,
):
    scheduled = []
    monkeypatch.setattr(
        server,
        "_runtime_routing_requires_initial_task",
        lambda: True,
        raising=False,
    )
    monkeypatch.setattr(server, "_schedule_agent_build", scheduled.append)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)

    response = server._methods["session.create"]("create", {"cols": 80})
    sid = response["result"]["session_id"]
    try:
        assert scheduled == []
        assert server._sessions[sid]["agent"] is None
        assert server._sessions[sid]["agent_ready"].is_set() is False
    finally:
        server._sessions.pop(sid, None)


def test_tui_first_prompt_is_passed_only_to_initial_agent_build(monkeypatch):
    captured = []

    class FakeWorker:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    def fake_make_agent(_sid, _key, **kwargs):
        captured.append(kwargs)
        return types.SimpleNamespace(model="selected-model")

    monkeypatch.setattr(server, "_runtime_routing_requires_initial_task", lambda: True, raising=False)
    monkeypatch.setattr(server, "_set_session_context", lambda _key: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda _tokens: None)
    monkeypatch.setattr(server, "_make_agent", fake_make_agent)
    monkeypatch.setattr(server, "_SlashWorker", FakeWorker)
    monkeypatch.setattr(server, "_attach_worker", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda _sid: None)
    monkeypatch.setattr(server, "_emit", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_session_info", lambda *_a, **_k: {})
    monkeypatch.setattr(server, "_start_notification_poller", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_probe_config_health", lambda *_a: None)

    sid = "route-build"
    session = {
        "agent": None,
        "agent_ready": threading.Event(),
        "session_key": "route-session",
        "profile_home": None,
        "model_override": None,
        "create_reasoning_override": None,
        "create_service_tier_override": None,
    }
    server._sessions[sid] = session
    try:
        server._start_agent_build(sid, session, initial_task="clean first task")
        assert session["agent_ready"].wait(timeout=3)
        assert captured == [
            {
                "session_db": None,
                "platform_override": "tui",
                "initial_task": "clean first task",
                "is_resume": False,
            }
        ]
    finally:
        server._sessions.pop(sid, None)


def test_tui_routing_task_includes_attached_image_bytes(tmp_path):
    image_path = tmp_path / "private-first.png"
    image_path.write_bytes(b"bounded-image-bytes")

    task = server._routing_task_for_prompt(
        {"attached_images": [str(image_path)]},
        "inspect this",
    )

    assert task[0] == {"type": "text", "text": "inspect this"}
    assert task[1]["type"] == "image"
    assert task[1]["data"] == b"bounded-image-bytes"
    assert task[1]["mime_type"] == "image/png"
    assert str(image_path) not in repr(task)
    assert image_path.name not in repr(task)


def test_tui_lazy_image_upload_queues_without_building_baseline_agent(
    monkeypatch,
    tmp_path,
):
    sid = "lazy-image-upload"
    session = {
        "agent": None,
        "agent_ready": threading.Event(),
        "session_key": "lazy-image-session",
        "attached_images": [],
        "image_counter": 0,
        "last_active": 0.0,
    }
    server._sessions[sid] = session
    builds = []
    fake_cli = types.ModuleType("cli")
    fake_cli._IMAGE_EXTENSIONS = {".png"}
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_runtime_routing_requires_initial_task", lambda: True)
    monkeypatch.setattr(server, "_start_agent_build", lambda *args, **kwargs: builds.append((args, kwargs)))
    monkeypatch.setattr(
        server,
        "_wait_agent",
        lambda _session, rid: server._err(rid, 5032, "agent initialization timed out"),
    )
    monkeypatch.setitem(__import__("sys").modules, "cli", fake_cli)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nminimal").decode("ascii")

    try:
        response = server._methods["image.attach_bytes"](
            "upload",
            {
                "session_id": sid,
                "content_base64": png,
                "filename": "first.png",
            },
        )
    finally:
        server._sessions.pop(sid, None)

    assert response["result"]["attached"] is True
    assert builds == []


def test_tui_lazy_model_selection_stays_pending_without_building(monkeypatch):
    sid = "lazy-model-selection"
    session = {
        "agent": None,
        "agent_ready": threading.Event(),
        "session_key": "lazy-model-session",
        "last_active": 0.0,
    }
    server._sessions[sid] = session
    builds = []
    switches = []
    monkeypatch.setattr(server, "_runtime_routing_requires_initial_task", lambda: True)
    monkeypatch.setattr(server, "_start_agent_build", lambda *args, **kwargs: builds.append((args, kwargs)))
    monkeypatch.setattr(
        server,
        "_wait_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must stay lazy")),
    )

    def fake_switch(_sid, target, raw, **_kwargs):
        switches.append((_sid, target.get("agent"), raw))
        return {"value": "manual-model", "warning": ""}

    monkeypatch.setattr(server, "_apply_model_switch", fake_switch)
    try:
        response = server._methods["config.set"](
            "model",
            {"session_id": sid, "key": "model", "value": "manual-model"},
        )
    finally:
        server._sessions.pop(sid, None)

    assert response["result"]["value"] == "manual-model"
    assert builds == []
    assert switches == [(sid, None, "manual-model")]


def test_tui_lazy_slash_model_selection_stays_pending_without_building(monkeypatch):
    sid = "lazy-slash-model-selection"
    switches = []

    class FakeWorker:
        def run(self, command):
            assert command == "/model manual-model"
            return "model selected"

    session = {
        "agent": None,
        "agent_ready": threading.Event(),
        "session_key": "lazy-slash-model-session",
        "last_active": 0.0,
        "running": False,
        "slash_worker": FakeWorker(),
    }
    server._sessions[sid] = session
    builds = []
    monkeypatch.setattr(server, "_runtime_routing_requires_initial_task", lambda: True)
    monkeypatch.setattr(
        server,
        "_start_agent_build",
        lambda *args, **kwargs: builds.append((args, kwargs)),
    )
    monkeypatch.setattr(
        server,
        "_wait_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must stay lazy")),
    )

    def fake_switch(_sid, target, raw, **_kwargs):
        switches.append((_sid, target.get("agent"), raw))
        return {"value": "manual-model", "warning": ""}

    monkeypatch.setattr(server, "_apply_model_switch", fake_switch)
    try:
        response = server._methods["slash.exec"](
            "slash-model",
            {"session_id": sid, "command": "/model manual-model"},
        )
    finally:
        server._sessions.pop(sid, None)

    assert response["result"]["output"] == "model selected"
    assert builds == []
    assert switches == [(sid, None, "manual-model")]


def test_tui_pending_model_switch_records_generic_manual_transition(monkeypatch):
    import agent.runtime_routing as routing

    calls = []
    result = types.SimpleNamespace(
        new_model="manual-model",
        target_provider="openrouter",
        base_url="https://manual.invalid/v1",
        api_key="manual-key",
        api_mode="chat_completions",
    )
    monkeypatch.setattr(server, "_load_fallback_model", lambda: [])
    monkeypatch.setattr(
        routing,
        "apply_manual_runtime_transition",
        lambda agent, **kwargs: calls.append((agent, kwargs)),
    )

    server._record_tui_manual_runtime_transition(
        sid="lazy-model-selection",
        session={"agent": None, "session_key": "lazy-model-session"},
        result=result,
    )

    assert calls[0][0] is None
    assert calls[0][1]["session_id"] == "lazy-model-session"
    assert calls[0][1]["runtime"].model == "manual-model"


_TUI_LAUNCH_RUNTIME_PIN_ENV = "HERMES_TUI_LAUNCH_RUNTIME_PIN"


def _capture_tui_launch_env(monkeypatch, **launch_kwargs):
    import hermes_cli.main as main_mod

    captured = {}
    monkeypatch.setattr(
        main_mod,
        "_make_tui_argv",
        lambda _tui_dir, _tui_dev: (["node", "dist/entry.js"], main_mod.PROJECT_ROOT),
    )
    monkeypatch.setattr(
        main_mod.subprocess,
        "call",
        lambda _argv, cwd=None, env=None: captured.update({"env": dict(env)}) or 1,
    )

    with pytest.raises(SystemExit):
        main_mod._launch_tui(**launch_kwargs)

    return captured["env"]


def _build_tui_agent_from_launch_env(monkeypatch, launch_env, *, resume_runtime=None):
    import agent.runtime_routing as routing
    import run_agent

    handoff_names = (
        "HERMES_MODEL",
        "HERMES_INFERENCE_MODEL",
        "HERMES_TUI_PROVIDER",
        "HERMES_INFERENCE_PROVIDER",
        _TUI_LAUNCH_RUNTIME_PIN_ENV,
    )
    for name in handoff_names:
        monkeypatch.delenv(name, raising=False)
        if name in launch_env:
            monkeypatch.setenv(name, launch_env[name])

    selected = routing.AgentRuntimeSpec(
        model="auto-selected-model",
        provider="auto-selected-provider",
        base_url="https://selected.invalid/v1",
        api_key="selected-key",
        resolution_state="resolved",
        api_mode="chat_completions",
    )
    requests = []

    def fake_prepare(request):
        requests.append(request)
        if request.context.manual_runtime_pin:
            plan = routing.AgentRuntimePlan(
                action="inherit",
                runtime=request.baseline,
                owns_fallbacks=False,
                reason_code="manual_runtime_pin",
            )
        else:
            plan = routing.AgentRuntimePlan(
                action="project",
                runtime=selected,
                decision_id="decision-tui-launch",
                bound_route_identity="auto-selected-provider/auto-selected-model",
                owns_fallbacks=True,
                reason_code="active_projected",
            )
        return routing._new_prepared(request, plan)

    monkeypatch.setattr(routing, "prepare_agent_runtime", fake_prepare)
    monkeypatch.setattr(
        routing,
        "runtime_resolver_requires_initial_task",
        lambda _scope: True,
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": {}})
    monkeypatch.setattr(server, "_parse_tui_skills_env", lambda: [])
    monkeypatch.setattr(server, "_load_provider_routing", lambda: {})
    monkeypatch.setattr(server, "_load_reasoning_config", lambda _model: None)
    monkeypatch.setattr(server, "_load_service_tier", lambda: None)
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda: None)
    monkeypatch.setattr(server, "_load_fallback_model", lambda: [])
    monkeypatch.setattr(server, "_agent_cbs", lambda _sid: {})
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.models.detect_static_provider_for_model",
        lambda model, _provider: (
            ("seed-provider", model) if model == "hosted-seed-model" else None
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested=None, target_model=None, **_kwargs: {
            "provider": requested or "baseline-provider",
            "base_url": "https://baseline.invalid/v1",
            "api_key": "baseline-key",
            "api_mode": "chat_completions",
            "command": None,
            "args": None,
            "credential_pool": None,
        },
    )

    constructed = []

    class FakeAgent:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    build_specs = (
        [("ui-resume", "session-resume", {"model_override": resume_runtime, "is_resume": True})]
        if resume_runtime is not None
        else [
            (f"ui-launch-{index}", f"session-launch-{index}", {})
            for index in range(2)
        ]
    )
    for sid, session_id, kwargs in build_specs:
        server._make_agent(
            sid,
            session_id,
            initial_task="route this fresh task",
            **kwargs,
        )
    return requests, constructed


@pytest.mark.parametrize(
    ("launch_kwargs", "expected_marker", "expected_model", "expected_provider"),
    [
        (
            {"model": "manual-model"},
            "model",
            "manual-model",
            "baseline-provider",
        ),
        (
            {"provider": "manual-provider"},
            "provider",
            "hosted-seed-model",
            "manual-provider",
        ),
        (
            {"model": "manual-model", "provider": "manual-provider"},
            "model,provider",
            "manual-model",
            "manual-provider",
        ),
    ],
)
def test_explicit_tui_launch_runtime_flags_bypass_auto_routing(
    monkeypatch,
    launch_kwargs,
    expected_marker,
    expected_model,
    expected_provider,
):
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "hosted-seed-model")
    monkeypatch.delenv("HERMES_TUI_PROVIDER", raising=False)
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "seed-provider")
    monkeypatch.setenv(_TUI_LAUNCH_RUNTIME_PIN_ENV, "stale-inherited-value")

    launch_env = _capture_tui_launch_env(monkeypatch, **launch_kwargs)
    requests, constructed = _build_tui_agent_from_launch_env(monkeypatch, launch_env)

    assert launch_env[_TUI_LAUNCH_RUNTIME_PIN_ENV] == expected_marker
    assert len(requests) == len(constructed) == 2
    for request, agent_kwargs in zip(requests, constructed):
        assert request.context.manual_runtime_pin is True
        assert request.context.manual_pin_source == "tui_launch_runtime"
        assert request.context.task == "route this fresh task"
        assert agent_kwargs["model"] == expected_model
        assert agent_kwargs["provider"] == expected_provider
        assert agent_kwargs["prepared_agent_runtime"].plan.action == "inherit"


def test_hosted_tui_runtime_seed_remains_auto_routable(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "hosted-seed-model")
    monkeypatch.delenv("HERMES_TUI_PROVIDER", raising=False)
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "seed-provider")
    monkeypatch.setenv(_TUI_LAUNCH_RUNTIME_PIN_ENV, "stale-inherited-value")

    launch_env = _capture_tui_launch_env(monkeypatch)
    requests, constructed = _build_tui_agent_from_launch_env(monkeypatch, launch_env)

    assert _TUI_LAUNCH_RUNTIME_PIN_ENV not in launch_env
    assert len(requests) == len(constructed) == 2
    for request, agent_kwargs in zip(requests, constructed):
        assert request.baseline.model == "hosted-seed-model"
        assert request.context.manual_runtime_pin is False
        assert request.context.manual_pin_source is None
        assert agent_kwargs["model"] == "auto-selected-model"
        assert agent_kwargs["provider"] == "auto-selected-provider"
        assert agent_kwargs["prepared_agent_runtime"].plan.action == "project"


def test_explicit_tui_launch_pin_does_not_override_durable_resume(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_TUI_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)

    launch_env = _capture_tui_launch_env(
        monkeypatch,
        model="launch-model",
        provider="launch-provider",
    )
    requests, constructed = _build_tui_agent_from_launch_env(
        monkeypatch,
        launch_env,
        resume_runtime={},
    )

    assert len(requests) == len(constructed) == 1
    request = requests[0]
    assert request.context.is_resume is True
    assert request.context.manual_runtime_pin is False
    assert request.context.manual_pin_source is None
    assert request.context.task is None
    # The launch values remain the ordinary requested baseline, but the
    # current durable resume binding (represented by the fake project below)
    # retains authority and supersedes it.
    assert request.baseline.model == "launch-model"
    assert request.baseline.provider == "launch-provider"
    assert constructed[0]["model"] == "auto-selected-model"
    assert constructed[0]["provider"] == "auto-selected-provider"
    assert constructed[0]["prepared_agent_runtime"].plan.action == "project"


def test_tui_active_route_is_selected_before_unavailable_baseline(monkeypatch):
    import agent.runtime_routing as routing
    import run_agent

    target = routing.AgentRuntimeSpec(
        model="selected-model",
        provider="openrouter",
        base_url="https://selected.invalid/v1",
        api_key="selected-key",
        resolution_state="resolved",
        api_mode="chat_completions",
    )
    requests = []

    def fake_prepare(request):
        requests.append(request)
        return routing._new_prepared(
            request,
            routing.AgentRuntimePlan(
                action="project",
                runtime=target,
                decision_id="decision-tui-1",
                bound_route_identity="openrouter/selected-model",
                owns_fallbacks=True,
                reason_code="active_projected",
            ),
        )

    monkeypatch.setattr(routing, "prepare_agent_runtime", fake_prepare)
    monkeypatch.setattr(
        routing,
        "runtime_resolver_requires_initial_task",
        lambda _scope: True,
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": {}})
    monkeypatch.setattr(server, "_parse_tui_skills_env", lambda: [])
    monkeypatch.setattr(
        server,
        "_resolve_startup_runtime",
        lambda: ("unavailable-baseline", "openrouter"),
    )
    baseline_attempts = []

    def unavailable_baseline(*_args, **_kwargs):
        baseline_attempts.append(True)
        raise RuntimeError("baseline unavailable")

    monkeypatch.setattr(server, "_resolve_runtime_with_fallback", unavailable_baseline)
    monkeypatch.setattr(server, "_load_provider_routing", lambda: {})
    monkeypatch.setattr(server, "_load_reasoning_config", lambda _model: None)
    monkeypatch.setattr(server, "_load_service_tier", lambda: None)
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda: None)
    monkeypatch.setattr(server, "_load_fallback_model", lambda: [])
    monkeypatch.setattr(server, "_agent_cbs", lambda _sid: {})
    monkeypatch.setattr(server, "_get_db", lambda: None)

    constructed = []

    class FakeAgent:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)

    server._make_agent(
        "ui-1",
        "session-1",
        initial_task="clean first task",
    )

    assert baseline_attempts == []
    assert len(requests) == 1
    assert len(constructed) == 1
    kwargs = constructed[0]
    assert kwargs["model"] == "selected-model"
    assert kwargs["provider"] == "openrouter"
    assert kwargs["runtime_routing_context"].task == "clean first task"
    assert kwargs["runtime_routing_context"].metadata["platform"] == "tui"
    assert kwargs["prepared_agent_runtime"].plan.decision_id == "decision-tui-1"


def test_tui_first_db_row_uses_effective_routed_runtime(monkeypatch):
    created = []

    class FakeDb:
        def create_session(self, session_id, source, **kwargs):
            created.append((session_id, source, kwargs))

    selected_agent = types.SimpleNamespace(
        model="selected-model",
        provider="openrouter",
        base_url="https://selected.invalid/v1",
        api_mode="chat_completions",
        reasoning_config={"enabled": True, "effort": "high"},
        service_tier=None,
    )
    monkeypatch.setattr(server, "_get_db", lambda: FakeDb())
    monkeypatch.setattr(server, "_session_source", lambda _session: "tui")

    server._ensure_session_db_row(
        {
            "session_key": "selected-session",
            "agent": selected_agent,
            "model_override": None,
            "create_reasoning_override": None,
            "create_service_tier_override": None,
            "parent_session_id": None,
            "explicit_cwd": False,
        }
    )

    assert created[0][0] == "selected-session"
    assert created[0][2]["model"] == "selected-model"
    assert created[0][2]["model_config"]["provider"] == "openrouter"
    assert created[0][2]["model_config"]["reasoning_config"]["effort"] == "high"
