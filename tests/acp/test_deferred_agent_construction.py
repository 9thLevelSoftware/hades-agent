"""ACP regressions for first-prompt runtime routing and manual intent."""

from __future__ import annotations

import asyncio
import threading

import pytest
from acp.schema import McpServerStdio, TextContentBlock

from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager
from agent.runtime_routing import RuntimeRoutingDeferred
from hermes_state import SessionDB


def test_create_session_persists_identity_without_constructing_agent(tmp_path):
    calls: list[object] = []
    db = SessionDB(db_path=tmp_path / "state.db")
    manager = SessionManager(agent_factory=lambda: calls.append(object()), db=db)

    state = manager.create_session(cwd="/workspace")

    assert state.agent is None
    assert calls == []
    row = db.get_session(state.session_id)
    assert row is not None
    assert row["source"] == "acp"
    assert row["model"] is None


@pytest.mark.asyncio
async def test_first_prompt_constructs_agent_with_fresh_routing_context(
    tmp_path,
    monkeypatch,
):
    constructed: list[dict[str, object]] = []
    runs: list[dict[str, object]] = []

    class FakeAgent:
        def __init__(self, kwargs: dict[str, object]):
            self.model = "selected-model"
            self.provider = "selected-provider"
            self.session_id = str(kwargs["session_id"])

        def run_conversation(self, **kwargs):
            runs.append(kwargs)
            return {"final_response": "done", "messages": []}

    def factory(**kwargs):
        constructed.append(kwargs)
        return FakeAgent(kwargs)

    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        "agent.runtime_routing.runtime_resolver_requires_initial_task",
        lambda _scope: True,
    )
    manager = SessionManager(
        agent_factory=factory,
        db=SessionDB(db_path=tmp_path / "state.db"),
    )
    state = manager.create_session(cwd="/workspace")
    server = HermesACPAgent(session_manager=manager)

    response = await server.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="first task")],
    )

    assert response.stop_reason == "end_turn"
    assert state.agent is not None
    assert len(constructed) == 1
    context = constructed[0]["runtime_routing_context"]
    assert context.scope == "fresh_session"
    assert context.task == "first task"
    assert context.session_id == state.session_id
    assert context.task_id == state.session_id
    assert context.is_resume is False
    assert context.manual_runtime_pin is False
    assert context.metadata == {"platform": "acp"}
    assert runs[0]["task_id"] == state.session_id


@pytest.mark.asyncio
async def test_preconstruction_model_selection_survives_restore_without_agent(tmp_path):
    calls: list[dict[str, object]] = []
    db = SessionDB(db_path=tmp_path / "state.db")
    manager = SessionManager(agent_factory=lambda **kwargs: calls.append(kwargs), db=db)
    state = manager.create_session(cwd="/workspace")
    server = HermesACPAgent(session_manager=manager)
    server._resolve_model_selection = lambda _raw, _current: (
        "anthropic",
        "manual-model",
    )

    response = await server.set_session_model(
        "anthropic:manual-model",
        state.session_id,
    )

    assert response is not None
    assert state.agent is None
    assert calls == []
    assert state.model == "manual-model"
    assert state.requested_provider == "anthropic"
    assert state.manual_runtime_pin is True
    assert state.manual_pin_source == "acp_model_selection"

    manager._sessions.clear()
    restored = manager.get_session(state.session_id)

    assert restored is not None
    assert restored.agent is None
    assert calls == []
    assert restored.model == "manual-model"
    assert restored.requested_provider == "anthropic"
    assert restored.manual_runtime_pin is True
    assert restored.manual_pin_source == "acp_model_selection"


@pytest.mark.asyncio
async def test_first_prompt_applies_pending_manual_intent_with_host_fallbacks(
    tmp_path,
    monkeypatch,
):
    constructed: list[dict[str, object]] = []
    transitions: list[dict[str, object]] = []
    credential_pool = object()

    class FakeAgent:
        def __init__(self, kwargs):
            self.model = kwargs["model"]
            self.provider = kwargs["requested_provider"]
            self.base_url = "https://manual.example/v1"
            self.api_mode = "anthropic_messages"
            self._credential_pool = credential_pool
            self.session_id = kwargs["session_id"]

        def run_conversation(self, **_kwargs):
            return {"final_response": "done", "messages": []}

    def factory(**kwargs):
        constructed.append(kwargs)
        return FakeAgent(kwargs)

    def record_transition(agent, **kwargs):
        transitions.append({"agent": agent, **kwargs})
        agent._runtime_fallback_authority = "host"
        agent._fallback_chain = list(kwargs["fallback_model"])

    fallback = {"provider": "openrouter", "model": "fallback-model"}
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        "agent.runtime_routing.runtime_resolver_requires_initial_task",
        lambda _scope: True,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {"default": "baseline", "provider": "openrouter"},
            "fallback_model": [fallback],
            "mcp_servers": {},
        },
    )
    monkeypatch.setattr(
        "agent.runtime_routing.apply_manual_runtime_transition",
        record_transition,
    )

    manager = SessionManager(
        agent_factory=factory,
        db=SessionDB(db_path=tmp_path / "state.db"),
    )
    state = manager.create_session(cwd="/workspace")
    state.model = "manual-model"
    state.requested_provider = "anthropic"
    state.manual_runtime_pin = True
    state.manual_pin_source = "acp_model_selection"
    manager.save_session(state.session_id)
    server = HermesACPAgent(session_manager=manager)

    await server.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="first task")],
    )

    assert len(constructed) == 1
    context = constructed[0]["runtime_routing_context"]
    assert context.manual_runtime_pin is True
    assert context.manual_pin_source == "acp_model_selection"
    assert constructed[0]["model"] == "manual-model"
    assert constructed[0]["requested_provider"] == "anthropic"
    assert constructed[0]["fallback_model"] == [fallback]
    assert len(transitions) == 1
    assert transitions[0]["agent"] is state.agent
    assert transitions[0]["session_id"] == state.session_id
    assert transitions[0]["source"] == "acp_model_selection"
    assert transitions[0]["runtime"].model == "manual-model"
    assert transitions[0]["runtime"].provider == "anthropic"
    assert transitions[0]["runtime"].credential_pool is credential_pool
    assert transitions[0]["fallback_model"] == [fallback]
    assert state.agent._runtime_fallback_authority == "host"
    assert state.agent._fallback_chain == [fallback]


@pytest.mark.asyncio
async def test_routing_defer_returns_retryable_refusal_without_persisting_prompt(
    tmp_path,
    monkeypatch,
):
    def factory(**_kwargs):
        raise RuntimeRoutingDeferred(retry_after_seconds=0.25)

    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {"default": "baseline", "provider": "openrouter"},
            "mcp_servers": {},
        },
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    manager = SessionManager(agent_factory=factory, db=db)
    state = manager.create_session(cwd="/workspace")
    server = HermesACPAgent(session_manager=manager)

    response = await server.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="first task")],
    )

    assert response.stop_reason == "refusal"
    assert state.agent is None
    assert state.history == []
    assert state.is_running is False
    assert state.current_prompt_text == ""
    assert db.get_messages_as_conversation(state.session_id) == []


@pytest.mark.asyncio
async def test_construction_error_releases_session_for_retry(tmp_path, monkeypatch):
    def factory(**_kwargs):
        raise RuntimeError("credential setup failed")

    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        "agent.runtime_routing.runtime_resolver_requires_initial_task",
        lambda _scope: False,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {"default": "baseline", "provider": "openrouter"},
            "mcp_servers": {},
        },
    )
    manager = SessionManager(
        agent_factory=factory,
        db=SessionDB(db_path=tmp_path / "state.db"),
    )
    state = manager.create_session(cwd="/workspace")
    server = HermesACPAgent(session_manager=manager)

    with pytest.raises(RuntimeError, match="credential setup failed"):
        await server.prompt(
            session_id=state.session_id,
            prompt=[TextContentBlock(type="text", text="first task")],
        )

    assert state.agent is None
    assert state.is_running is False
    assert state.current_prompt_text == ""
    assert state.history == []


@pytest.mark.asyncio
async def test_cancel_during_deferred_construction_skips_provider_call(
    tmp_path,
    monkeypatch,
):
    runs: list[str] = []
    construction_started = threading.Event()
    allow_construction_to_finish = threading.Event()

    class FakeAgent:
        model = "baseline"
        provider = "openrouter"
        session_id = "internal"

        def run_conversation(self, **_kwargs):
            runs.append("called")
            return {"final_response": "should not run", "messages": []}

        def interrupt(self):
            return None

    def factory(**_kwargs):
        construction_started.set()
        allow_construction_to_finish.wait(timeout=0.5)
        return FakeAgent()

    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        "agent.runtime_routing.runtime_resolver_requires_initial_task",
        lambda _scope: False,
    )
    manager = SessionManager(
        agent_factory=factory,
        db=SessionDB(db_path=tmp_path / "state.db"),
    )
    state = manager.create_session(cwd="/workspace")
    server = HermesACPAgent(session_manager=manager)

    prompt_task = asyncio.create_task(
        server.prompt(
            session_id=state.session_id,
            prompt=[TextContentBlock(type="text", text="first task")],
        )
    )
    assert await asyncio.to_thread(construction_started.wait, 1)
    await server.cancel(session_id=state.session_id)
    allow_construction_to_finish.set()
    response = await prompt_task

    assert response.stop_reason == "cancelled"
    assert runs == []
    assert state.history == []
    assert state.is_running is False
    assert state.current_prompt_text == ""


def test_fork_copies_history_and_pending_manual_intent_without_agent(tmp_path):
    calls: list[dict[str, object]] = []
    manager = SessionManager(
        agent_factory=lambda **kwargs: calls.append(kwargs),
        db=SessionDB(db_path=tmp_path / "state.db"),
    )
    original = manager.create_session(cwd="/original")
    original.history = [{"role": "user", "content": "before fork"}]
    original.model = "manual-model"
    original.requested_provider = "anthropic"
    original.manual_runtime_pin = True
    original.manual_pin_source = "acp_model_selection"
    manager.save_session(original.session_id)

    forked = manager.fork_session(original.session_id, cwd="/fork")

    assert forked is not None
    assert forked.session_id != original.session_id
    assert forked.agent is None
    assert calls == []
    assert forked.history == original.history
    assert forked.history is not original.history
    assert forked.model == "manual-model"
    assert forked.requested_provider == "anthropic"
    assert forked.manual_runtime_pin is True
    assert forked.manual_pin_source == "acp_model_selection"


@pytest.mark.asyncio
async def test_postconstruction_model_selection_records_manual_transition_and_fallbacks(
    tmp_path,
    monkeypatch,
):
    constructed: list[dict[str, object]] = []
    transitions: list[dict[str, object]] = []
    fallback = {"provider": "openrouter", "model": "fallback-model"}

    class FakeAgent:
        def __init__(self, kwargs):
            self.model = kwargs["model"] or "selected-model"
            self.provider = kwargs["requested_provider"] or "selected-provider"
            self.base_url = f"https://{self.provider}.example/v1"
            self.api_mode = "chat_completions"
            self.session_id = kwargs["session_id"]

        def run_conversation(self, **_kwargs):
            return {"final_response": "", "messages": []}

    def factory(**kwargs):
        constructed.append(kwargs)
        return FakeAgent(kwargs)

    def record_transition(agent, **kwargs):
        transitions.append({"agent": agent, **kwargs})
        agent._runtime_fallback_authority = "host"
        agent._fallback_chain = list(kwargs["fallback_model"])

    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {"default": "baseline", "provider": "openrouter"},
            "fallback_model": [fallback],
            "mcp_servers": {},
        },
    )
    monkeypatch.setattr(
        "agent.runtime_routing.apply_manual_runtime_transition",
        record_transition,
    )

    manager = SessionManager(
        agent_factory=factory,
        db=SessionDB(db_path=tmp_path / "state.db"),
    )
    state = manager.create_session(cwd="/workspace")
    server = HermesACPAgent(session_manager=manager)
    server._resolve_model_selection = lambda _raw, _current: (
        "anthropic",
        "manual-model",
    )
    await server.prompt(
        session_id=state.session_id,
        prompt=[TextContentBlock(type="text", text="first task")],
    )
    first_agent = state.agent

    response = await server.set_session_model(
        "anthropic:manual-model",
        state.session_id,
    )

    assert response is not None
    assert state.agent is not first_agent
    assert state.model == "manual-model"
    assert state.requested_provider == "anthropic"
    assert state.manual_runtime_pin is True
    assert state.manual_pin_source == "acp_model_selection"
    assert len(transitions) == 1
    assert transitions[0]["agent"] is state.agent
    assert transitions[0]["source"] == "acp_model_selection"
    assert transitions[0]["fallback_model"] == [fallback]
    assert state.agent._runtime_fallback_authority == "host"
    assert state.agent._fallback_chain == [fallback]


@pytest.mark.asyncio
async def test_new_session_stages_mcp_servers_without_registering_or_constructing(
    tmp_path,
    monkeypatch,
):
    factory_calls: list[dict[str, object]] = []
    register_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "tools.mcp_tool.register_mcp_servers",
        lambda config: register_calls.append(config),
    )
    manager = SessionManager(
        agent_factory=lambda **kwargs: factory_calls.append(kwargs),
        db=SessionDB(db_path=tmp_path / "state.db"),
    )
    server = HermesACPAgent(session_manager=manager)
    mcp_server = McpServerStdio(
        name="workspace",
        command="mcp-workspace",
        args=[],
        env=[],
    )

    response = await server.new_session(cwd="/workspace", mcp_servers=[mcp_server])
    state = manager.get_session(response.session_id)

    assert state is not None
    assert state.agent is None
    assert factory_calls == []
    assert register_calls == []
    assert state.mcp_servers == [mcp_server]


def test_first_prompt_without_route_uses_legacy_provider_construction(
    tmp_path,
    monkeypatch,
):
    captured: dict[str, object] = {}

    class FakeAgent:
        model = "baseline-model"

        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "agent.runtime_routing.runtime_resolver_requires_initial_task",
        lambda _scope: False,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {"default": "baseline-model", "provider": "openrouter"},
            "fallback_model": [
                {"provider": "anthropic", "model": "host-fallback"}
            ],
            "mcp_servers": {},
        },
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested=None: {
            "provider": requested,
            "api_mode": "chat_completions",
            "base_url": "https://legacy.example/v1",
            "api_key": "legacy-key",
            "command": None,
            "args": [],
        },
    )
    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    manager = SessionManager(db=SessionDB(db_path=tmp_path / "state.db"))
    state = manager.create_session(cwd="/workspace")

    manager.ensure_agent(state, task="first task")

    assert captured["provider"] == "openrouter"
    assert captured["base_url"] == "https://legacy.example/v1"
    assert captured["api_key"] == "legacy-key"
    assert "runtime_routing_context" not in captured
    assert "fallback_model" not in captured


def test_routed_first_prompt_bypasses_legacy_credential_resolution(
    tmp_path,
    monkeypatch,
):
    captured: dict[str, object] = {}
    legacy_resolution_calls: list[str | None] = []

    class FakeAgent:
        model = "routed-model"
        provider = "routed-provider"

        def __init__(self, **kwargs):
            captured.update(kwargs)

    def fail_legacy_resolution(requested=None):
        legacy_resolution_calls.append(requested)
        raise AssertionError("legacy credentials resolved before runtime routing")

    monkeypatch.setattr(
        "agent.runtime_routing.runtime_resolver_requires_initial_task",
        lambda _scope: True,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {"default": "baseline-model", "provider": "openrouter"},
            "mcp_servers": {},
        },
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fail_legacy_resolution,
    )
    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    manager = SessionManager(db=SessionDB(db_path=tmp_path / "state.db"))
    state = manager.create_session(cwd="/workspace")

    manager.ensure_agent(state, task="route this task")

    assert legacy_resolution_calls == []
    context = captured["runtime_routing_context"]
    assert context.task == "route this task"
    assert captured["provider"] == "openrouter"
    assert "api_key" not in captured
