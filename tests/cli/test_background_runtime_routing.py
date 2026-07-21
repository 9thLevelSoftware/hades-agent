from __future__ import annotations

from types import SimpleNamespace

from agent.runtime_routing import AgentRuntimeContext, AgentRuntimeSpec
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
from hermes_cli.cli_commands_mixin import CLICommandsMixin


def test_background_task_routes_once_in_its_own_session(monkeypatch):
    import cli as cli_mod
    import hermes_cli.cli_commands_mixin as commands_mod

    captured = {}
    projected = AgentRuntimeSpec(
        model="background-model",
        provider="background-provider",
        base_url="https://background.invalid/v1",
        api_key="background-secret",
        resolution_state="resolved",
        api_mode="chat_completions",
    )
    finalized = object()

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["agent_kwargs"] = kwargs
            self._print_fn = None
            self.thinking_callback = None

        def run_conversation(self, *, user_message, task_id):
            captured["run"] = (user_message, task_id)
            return {"final_response": ""}

    class SyncThread:
        def __init__(self, *, target, **_kwargs):
            self._target = target

        def start(self):
            self._target()

    class Shell:
        _background_task_counter = 0
        _background_tasks = {}
        _initial_runtime_manual_pin = False
        _fallback_model = []
        max_tokens = None
        max_turns = 2
        enabled_toolsets = None
        _session_db = None
        reasoning_config = None
        service_tier = None
        _providers_only = None
        _providers_ignore = None
        _providers_order = None
        _provider_sort = None
        _provider_require_params = False
        _provider_data_collection = None
        _openrouter_min_coding_score = None
        _sudo_password_callback = None
        _approval_callback = None
        _secret_capture_callback = None
        _agent_running = False
        _app = None
        _spinner_text = ""
        bell_on_complete = False

        def _resolve_turn_agent_config(self, prompt):
            return {
                "model": "missing-baseline",
                "runtime": {
                    "provider": "missing-baseline",
                    "base_url": None,
                    "api_key": None,
                },
                "request_overrides": None,
            }

        def _runtime_routing_handoff(self, **kwargs):
            captured["handoff_kwargs"] = kwargs
            context = AgentRuntimeContext(
                scope="fresh_session",
                task=kwargs["initial_task"],
                session_id=kwargs["session_id"],
                task_id=kwargs["task_id"],
                metadata={"platform": "cli"},
            )
            captured["handoff"] = context
            return context, finalized, projected

        def _ensure_runtime_credentials(self):
            raise AssertionError("projected background route touched baseline credentials")

        def _invalidate(self, **_kwargs):
            return None

    monkeypatch.setattr(
        "agent.runtime_routing.runtime_resolver_requires_initial_task",
        lambda scope: scope == "fresh_session",
    )
    monkeypatch.setattr(cli_mod, "AIAgent", FakeAgent)
    monkeypatch.setattr(commands_mod.threading, "Thread", SyncThread)
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_a, **_k: None)

    shell = Shell()
    CLICommandsMixin._handle_background_command(shell, "/background route this")

    context = captured["handoff"]
    assert context.task == "route this"
    assert context.metadata == {"platform": "cli"}
    assert context.session_id == context.task_id
    assert context.session_id.startswith("bg_")
    assert captured["agent_kwargs"]["runtime_routing_context"] is context
    assert captured["agent_kwargs"]["prepared_agent_runtime"] is finalized
    assert captured["run"] == ("route this", context.task_id)
    assert captured["handoff_kwargs"]["update_host_state"] is False


def test_background_handoff_does_not_mutate_foreground_runtime(monkeypatch):
    foreground_agent = object()
    shell = SimpleNamespace(
        api_key="foreground-key",
        base_url="https://foreground.invalid/v1",
        provider="foreground-provider",
        api_mode="chat_completions",
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        model="foreground-model",
        reasoning_config=None,
        _fallback_model=[],
        agent=foreground_agent,
    )
    effective = AgentRuntimeSpec(
        model="background-fallback-model",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="background-key",
        resolution_state="resolved",
        api_mode="chat_completions",
    )
    prepared = SimpleNamespace(
        plan=SimpleNamespace(action="inherit", owns_fallbacks=False)
    )
    monkeypatch.setattr(
        "agent.runtime_routing.prepare_agent_runtime",
        lambda _request: prepared,
    )
    monkeypatch.setattr(
        "agent.runtime_routing.resolve_ordinary_hermes_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(runtime=effective),
    )
    monkeypatch.setattr(
        "agent.runtime_routing.finalize_prepared_agent_runtime",
        lambda *_args: object(),
    )

    handoff_kwargs = {
        "initial_task": "background task",
        "session_id": "background-session",
        "task_id": "background-task",
    }
    if "update_host_state" in __import__("inspect").signature(
        CLIAgentSetupMixin._runtime_routing_handoff
    ).parameters:
        handoff_kwargs["update_host_state"] = False
    context, _sealed, selected = CLIAgentSetupMixin._runtime_routing_handoff(
        shell,
        **handoff_kwargs,
    )

    assert context.metadata == {"platform": "cli"}
    assert selected is effective
    assert shell.model == "foreground-model"
    assert shell.provider == "foreground-provider"
    assert shell.base_url == "https://foreground.invalid/v1"
    assert shell.api_key == "foreground-key"
    assert shell.agent is foreground_agent
