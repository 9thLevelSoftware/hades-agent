"""Regression tests for CLI fresh-session commands."""

from __future__ import annotations

import importlib
import os
import sys
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from hades_cli.model_switch import ModelSwitchResult
from hades_state import SessionDB
from tools.todo_tool import TodoStore


class _FakeCompressor:
    """Minimal stand-in for ContextCompressor."""

    def __init__(self):
        self.last_prompt_tokens = 500
        self.last_completion_tokens = 200
        self.last_total_tokens = 700
        self.compression_count = 3
        self._context_probed = True


class _FakeAgent:
    def __init__(self, session_id: str, session_start):
        self.session_id = session_id
        self.session_start = session_start
        self.model = "anthropic/claude-opus-4.6"
        self._last_flushed_db_idx = 7
        self._todo_store = TodoStore()
        self._todo_store.write(
            [{"id": "t1", "content": "unfinished task", "status": "in_progress"}]
        )
        self.commit_memory_session = MagicMock()
        self._invalidate_system_prompt = MagicMock()
        self.release_clients = MagicMock()

        # Token counters (non-zero to verify reset)
        self.session_total_tokens = 1000
        self.session_input_tokens = 600
        self.session_output_tokens = 400
        self.session_prompt_tokens = 550
        self.session_completion_tokens = 350
        self.session_cache_read_tokens = 100
        self.session_cache_write_tokens = 50
        self.session_reasoning_tokens = 80
        self.session_api_calls = 5
        self.session_estimated_cost_usd = 0.42
        self.session_cost_status = "estimated"
        self.session_cost_source = "openrouter"
        self.context_compressor = _FakeCompressor()

    def reset_session_state(self):
        """Mirror the real AIAgent.reset_session_state()."""
        self.session_total_tokens = 0
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_api_calls = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self.session_cost_source = "none"
        if hasattr(self, "context_compressor") and self.context_compressor:
            self.context_compressor.last_prompt_tokens = 0
            self.context_compressor.last_completion_tokens = 0
            self.context_compressor.last_total_tokens = 0
            self.context_compressor.compression_count = 0
            self.context_compressor._context_probed = False


def _make_cli(env_overrides=None, config_overrides=None, **kwargs):
    """Create a HermesCLI instance with minimal mocking."""
    _clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    if config_overrides:
        _clean_config.update(config_overrides)
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    if env_overrides:
        clean_env.update(env_overrides)
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
        "prompt_toolkit.auto_suggest": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), patch.dict(
        "os.environ", clean_env, clear=False
    ):
        import cli as _cli_mod

        _cli_mod = importlib.reload(_cli_mod)
        with patch.object(_cli_mod, "get_tool_definitions", return_value=[]), patch.dict(
            _cli_mod.__dict__, {"CLI_CONFIG": _clean_config}
        ):
            return _cli_mod.HermesCLI(**kwargs)


def _prepare_cli_with_active_session(tmp_path):
    cli = _make_cli()
    cli._session_db = SessionDB(db_path=tmp_path / "state.db")
    cli._session_db.create_session(session_id=cli.session_id, source="cli", model=cli.model)

    cli.agent = _FakeAgent(cli.session_id, cli.session_start)
    cli.conversation_history = [{"role": "user", "content": "hello"}]

    old_session_start = cli.session_start - timedelta(seconds=1)
    cli.session_start = old_session_start
    cli.agent.session_start = old_session_start

    # Bypass the destructive-slash confirmation gate — these tests focus on
    # the new-session mechanics, not the confirm prompt itself (covered in
    # tests/cli/test_destructive_slash_confirm.py).
    cli._confirm_destructive_slash = lambda *_a, **_kw: "once"
    return cli


@pytest.fixture(autouse=True)
def _reset_session_id_context():
    from gateway.session_context import _UNSET, _VAR_MAP

    yield
    os.environ.pop("HERMES_SESSION_ID", None)
    _VAR_MAP["HERMES_SESSION_ID"].set(_UNSET)


def test_new_command_creates_fresh_session_and_discards_parent_agent(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    old_agent = cli.agent
    old_session_id = cli.session_id
    old_session_start = cli.session_start

    cli.process_command("/new")

    assert cli.session_id != old_session_id

    old_session = cli._session_db.get_session(old_session_id)
    assert old_session is not None
    assert old_session["end_reason"] == "new_session"

    new_session = cli._session_db.get_session(cli.session_id)
    assert new_session is not None

    assert cli.agent is None
    assert old_agent.session_id == old_session_id
    assert old_agent.session_start == old_session_start
    old_agent.release_clients.assert_not_called()
    old_agent._invalidate_system_prompt.assert_not_called()
    assert cli._active_agent_route_signature is None
    assert cli._runtime_routing_is_resume() is False
    assert cli.session_start > old_session_start


def test_new_after_session_model_switch_discards_agent_and_restores_invocation_runtime(
    tmp_path,
):
    """A session-only /model pin must not become the next session's baseline."""
    cli = _prepare_cli_with_active_session(tmp_path)
    old_agent = cli.agent
    old_agent.release_clients = MagicMock()
    cli._active_agent_route_signature = ("parent-model", "parent-provider")

    invocation_runtime = {
        "model": cli.model,
        "provider": cli.provider,
        "requested_provider": cli.requested_provider,
        "api_key": cli.api_key,
        "base_url": cli.base_url,
        "api_mode": cli.api_mode,
    }

    # State written by a successful session-scoped /model switch.
    cli.model = "session-only/model"
    cli.provider = "session-provider"
    cli.requested_provider = "session-provider"
    cli.api_key = "session-secret"
    cli.base_url = "https://session.invalid/v1"
    cli.api_mode = "session_responses"
    cli._runtime_manual_pin = True
    cli._runtime_manual_pin_source = "cli_model_command"
    cli._pending_model_switch_note = "[Note: parent session model switch]"

    cli.new_session(silent=True)

    old_agent.release_clients.assert_not_called()
    assert cli.agent is None
    assert cli._active_agent_route_signature is None
    assert cli._runtime_manual_pin is False
    assert cli._runtime_manual_pin_source is None
    assert cli._pending_model_switch_note is None
    assert cli._runtime_routing_is_resume() is False
    for field, expected in invocation_runtime.items():
        assert getattr(cli, field) == expected

    persisted = cli._session_db.get_session(cli.session_id)
    assert persisted["model"] == invocation_runtime["model"]


def _global_switch_result() -> ModelSwitchResult:
    return ModelSwitchResult(
        success=True,
        new_model="persisted/model-b",
        target_provider="persisted-provider",
        provider_changed=True,
        api_key="persisted-secret",
        base_url="https://persisted.invalid/v1",
        api_mode="chat_completions",
        provider_label="Persisted Provider",
    )


def test_picker_global_model_switch_becomes_next_session_configured_baseline(
    tmp_path, monkeypatch
):
    """A globally saved picker choice is the next session's unpinned default."""
    import cli as cli_mod

    cli = _prepare_cli_with_active_session(tmp_path)
    old_agent = cli.agent
    # Apply through the picker result path without an in-place agent swap.
    cli.agent = None
    saved = []
    monkeypatch.setattr(
        cli_mod,
        "save_config_value",
        lambda key, value: saved.append((key, value)) or True,
    )
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *_a, **_k: None,
    )

    cli._apply_model_switch_result(_global_switch_result(), True)

    # The live session is manually pinned, but the persisted choice has also
    # replaced the configured baseline used by the next fresh conversation.
    assert cli._runtime_manual_pin is True
    assert cli._initial_runtime_baseline["model"] == "persisted/model-b"
    assert cli._initial_runtime_baseline["provider"] == "persisted-provider"
    assert ("model.default", "persisted/model-b") in saved
    assert ("model.provider", "persisted-provider") in saved

    cli.agent = old_agent
    cli.new_session(silent=True)

    assert cli.model == "persisted/model-b"
    assert cli.provider == "persisted-provider"
    assert cli._runtime_manual_pin is False
    assert cli._runtime_manual_pin_source is None
    assert cli._session_db.get_session(cli.session_id)["model"] == "persisted/model-b"


def test_typed_global_model_switch_promotes_the_same_configured_baseline(
    tmp_path, monkeypatch
):
    """Typed /model and picker selection must share persisted-baseline semantics."""
    import cli as cli_mod

    cli = _prepare_cli_with_active_session(tmp_path)
    cli.agent = None

    class _PickerContext:
        user_providers = None
        custom_providers = None

        def with_overrides(self, **_kwargs):
            return self

    monkeypatch.setattr(
        "hermes_cli.inventory.load_picker_context", lambda: _PickerContext()
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kwargs: _global_switch_result(),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(cli_mod, "save_config_value", lambda *_a, **_k: True)
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_a, **_k: None)
    cli._confirm_expensive_model_switch = lambda _result: True

    cli._handle_model_switch("/model persisted/model-b --global")

    assert cli._initial_runtime_baseline["model"] == "persisted/model-b"
    assert cli._initial_runtime_baseline["provider"] == "persisted-provider"


def test_global_model_switch_does_not_replace_an_explicit_launch_pin(
    monkeypatch,
):
    """Explicit --model/--provider intent remains authoritative per invocation."""
    import cli as cli_mod

    cli = _make_cli(model="launch/model-a", provider="launch-provider")
    launch_baseline = dict(cli._initial_runtime_baseline)
    monkeypatch.setattr(cli_mod, "save_config_value", lambda *_a, **_k: True)
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *_a, **_k: None,
    )

    cli._apply_model_switch_result(_global_switch_result(), True)
    cli._restore_invocation_runtime_baseline()

    assert cli._initial_runtime_baseline == launch_baseline
    assert cli.model == "launch/model-a"
    assert cli.provider == "launch-provider"
    assert cli._runtime_manual_pin is True
    assert cli._runtime_manual_pin_source == "cli_explicit_runtime"


def test_new_session_does_not_block_on_parent_client_retirement(tmp_path):
    """Neither release_clients nor agent.close may block /new rotation."""
    import cli as cli_mod

    cli = _prepare_cli_with_active_session(tmp_path)
    old_agent = cli.agent
    release_called = threading.Event()
    allow_release = threading.Event()
    close_started = threading.Event()
    allow_close = threading.Event()
    command_done = threading.Event()

    def _blocking_release():
        release_called.set()
        allow_release.wait(2)

    def _blocking_close():
        close_started.set()
        allow_close.wait(2)

    old_agent.release_clients.side_effect = _blocking_release
    old_agent.close = MagicMock(side_effect=_blocking_close)
    command_thread = threading.Thread(
        target=lambda: (cli.new_session(silent=True), command_done.set()),
        daemon=True,
    )
    command_thread.start()
    try:
        assert command_done.wait(0.5), "/new waited for detached client teardown"
        assert release_called.is_set() is False
        assert close_started.wait(1)
    finally:
        allow_release.set()
        allow_close.set()
        command_thread.join(timeout=1)

    assert cli_mod._drain_retired_cli_agents(timeout=1) is True
    old_agent.release_clients.assert_not_called()
    old_agent.close.assert_called_once_with()


def test_new_session_queues_boundary_commit_with_snapshot(tmp_path):
    """/new hands the OLD session's history + ids to the memory manager's
    serialized boundary task instead of blocking on extraction inline."""
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id

    mm = MagicMock()
    cli.agent._memory_manager = mm

    cli.process_command("/new")

    mm.commit_session_boundary_async.assert_called_once()
    args, kwargs = mm.commit_session_boundary_async.call_args
    assert args[0] == [{"role": "user", "content": "hello"}]
    assert kwargs["new_session_id"] == cli.session_id
    assert kwargs["parent_session_id"] == old_session_id
    assert kwargs["reason"] == "new_session"
    # The queued path replaces the inline switch — not both.
    mm.on_session_switch.assert_not_called()


def test_new_session_retires_parent_without_blocking_or_duplicate_end(
    tmp_path, monkeypatch
):
    """Retirement waits for the queued boundary, then tears providers down.

    The boundary task owns ``on_session_end``.  Retirement must only drain it
    and shut resources down; calling ``on_session_end`` again would extract the
    same transcript twice.
    """
    cli = _prepare_cli_with_active_session(tmp_path)
    old_agent = cli.agent
    old_agent.close = MagicMock()
    manager = MagicMock()
    flush_started = threading.Event()
    allow_flush = threading.Event()

    def _flush_pending(*, timeout):
        flush_started.set()
        return allow_flush.wait(timeout)

    manager.flush_pending.side_effect = _flush_pending
    old_agent._memory_manager = manager
    import cli as cli_mod

    retirement_state = []
    detach_order = []
    real_retire = cli_mod._retire_cli_agent
    real_setattr = type(cli).__setattr__

    def _observe_setattr(instance, name, value):
        if (
            instance is cli
            and name == "agent"
            and value is None
            and getattr(instance, "agent", None) is old_agent
        ):
            detach_order.append(cli_mod._active_agent_ref is None)
        return real_setattr(instance, name, value)

    def _observe_retirement(agent_arg, **kwargs):
        retirement_state.append(
            (
                cli.agent is None,
                cli_mod._active_agent_ref is None,
            )
        )
        return real_retire(agent_arg, **kwargs)

    monkeypatch.setattr(cli_mod, "_active_agent_ref", old_agent)
    monkeypatch.setattr(cli_mod, "_retire_cli_agent", _observe_retirement)
    monkeypatch.setattr(type(cli), "__setattr__", _observe_setattr)

    cli.new_session(silent=True)

    # /new returned while the old manager was still draining.
    assert flush_started.wait(1)
    assert cli.agent is None
    old_agent.close.assert_not_called()

    allow_flush.set()

    assert cli_mod._drain_retired_cli_agents(timeout=1) is True
    assert detach_order == [True]
    assert retirement_state == [(True, True)]
    manager.shutdown_all.assert_called_once_with()
    manager.on_session_end.assert_not_called()
    old_agent.close.assert_called_once_with()


def test_run_cleanup_waits_for_new_session_retirement(tmp_path):
    """A `/new` followed immediately by process exit drains its boundary."""
    import cli as cli_mod

    cli = _prepare_cli_with_active_session(tmp_path)
    old_agent = cli.agent
    old_agent.close = MagicMock()
    manager = MagicMock()
    flush_started = threading.Event()
    allow_flush = threading.Event()

    def _flush_pending(*, timeout):
        flush_started.set()
        return allow_flush.wait(timeout)

    manager.flush_pending.side_effect = _flush_pending
    old_agent._memory_manager = manager
    previous_active = cli_mod._active_agent_ref
    cli_mod._active_agent_ref = old_agent
    cli.new_session(silent=True)
    assert flush_started.wait(1)

    timer = threading.Timer(0.05, allow_flush.set)
    timer.daemon = True
    timer.start()
    previous_done = cli_mod._cleanup_done
    cli_mod._cleanup_done = False
    try:
        assert cli_mod._active_agent_ref is None
        cli_mod._run_cleanup(notify_session_finalize=False)
    finally:
        allow_flush.set()
        timer.join(timeout=1)
        cli_mod._cleanup_done = previous_done
        cli_mod._active_agent_ref = previous_active

    manager.shutdown_all.assert_called_once_with()
    old_agent.close.assert_called_once_with()


def test_new_session_without_history_switches_inline(tmp_path):
    """No old-session history → nothing to extract → plain inline switch."""
    cli = _prepare_cli_with_active_session(tmp_path)
    cli.conversation_history = []

    mm = MagicMock()
    cli.agent._memory_manager = mm

    cli.process_command("/new")

    mm.commit_session_boundary_async.assert_not_called()
    mm.on_session_switch.assert_called_once()
    _, kwargs = mm.on_session_switch.call_args
    assert kwargs["reset"] is True


def test_new_session_delivers_context_engine_boundary_synchronously(tmp_path):
    """The context-engine on_session_end must fire during /new itself.

    It is cheap local state work and ordering-sensitive: it must land before
    reset_session_state() rebinds the engine to the new session. The LLM-bound
    provider extraction is what gets deferred, not this."""
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id

    engine_calls = []
    cli.agent.context_compressor.on_session_end = (
        lambda sid, msgs: engine_calls.append((sid, list(msgs)))
    )

    cli.process_command("/new")

    assert engine_calls == [(old_session_id, [{"role": "user", "content": "hello"}])]


def test_run_cleanup_flushes_pending_memory_manager_work(tmp_path):
    """A '/new then quit' must not drop the queued old-session extraction.

    _run_cleanup gives the manager's serialized worker a bounded drain via
    flush_pending() before shutdown_all()'s short-fuse drain runs."""
    import cli as _cli_mod

    agent = MagicMock()
    mm = MagicMock()
    mm.flush_pending.return_value = True
    agent._memory_manager = mm
    agent._session_messages = []

    old_ref = _cli_mod._active_agent_ref
    _cli_mod._active_agent_ref = agent
    _cli_mod._cleanup_done = False
    try:
        _cli_mod._run_cleanup(notify_session_finalize=False)
    finally:
        _cli_mod._cleanup_done = True
        _cli_mod._active_agent_ref = old_ref

    mm.flush_pending.assert_called_once_with(timeout=10)


def test_new_command_rotates_hermes_session_id_env_and_context(tmp_path):
    from gateway.session_context import _VAR_MAP, get_session_env

    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id
    os.environ["HADES_SESSION_ID"] = old_session_id
    _VAR_MAP["HERMES_SESSION_ID"].set(old_session_id)

    cli.process_command("/new")

    assert cli.session_id != old_session_id
    assert os.environ["HADES_SESSION_ID"] == cli.session_id
    assert get_session_env("HERMES_SESSION_ID") == cli.session_id


def test_reset_command_is_alias_for_new_session(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    old_session_id = cli.session_id

    cli.process_command("/reset")

    assert cli.session_id != old_session_id
    assert cli._session_db.get_session(old_session_id)["end_reason"] == "new_session"
    assert cli._session_db.get_session(cli.session_id) is not None


def test_clear_command_starts_new_session_before_redrawing(tmp_path):
    cli = _prepare_cli_with_active_session(tmp_path)
    cli.console = MagicMock()
    cli.show_banner = MagicMock()

    old_session_id = cli.session_id
    cli.process_command("/clear")

    assert cli.session_id != old_session_id
    assert cli._session_db.get_session(old_session_id)["end_reason"] == "new_session"
    assert cli._session_db.get_session(cli.session_id) is not None
    cli.console.clear.assert_called_once()
    cli.show_banner.assert_called_once()
    assert cli.conversation_history == []


def test_new_session_does_not_relabel_parent_agent_or_its_usage(tmp_path):
    """The replacement agent starts clean; the parent remains an audit record."""
    cli = _prepare_cli_with_active_session(tmp_path)

    # Verify counters are non-zero before reset
    agent = cli.agent
    assert agent.session_total_tokens > 0
    assert agent.session_api_calls > 0
    assert agent.context_compressor.compression_count > 0

    cli.process_command("/new")

    assert cli.agent is None
    assert agent.session_total_tokens == 1000
    assert agent.session_input_tokens == 600
    assert agent.session_output_tokens == 400
    assert agent.session_prompt_tokens == 550
    assert agent.session_completion_tokens == 350
    assert agent.session_cache_read_tokens == 100
    assert agent.session_cache_write_tokens == 50
    assert agent.session_reasoning_tokens == 80
    assert agent.session_api_calls == 5
    assert agent.session_estimated_cost_usd == 0.42
    assert agent.session_cost_status == "estimated"
    assert agent.session_cost_source == "openrouter"

    comp = agent.context_compressor
    assert comp.last_prompt_tokens == 500
    assert comp.last_completion_tokens == 200
    assert comp.last_total_tokens == 700
    assert comp.compression_count == 3
    assert comp._context_probed is True
    agent.release_clients.assert_not_called()


def test_new_session_with_title(capsys):
    """new_session(title=...) creates a session and sets the title."""
    cli = _make_cli()
    cli._session_db = MagicMock()
    cli.agent = _FakeAgent("old_session_id", datetime.now())
    cli.conversation_history = []

    cli.new_session(title="My Test Session")

    # Assert set_session_title was called with the new session ID and sanitized title
    cli._session_db.set_session_title.assert_called_once()
    call_args = cli._session_db.set_session_title.call_args
    assert call_args[0][0] == cli.session_id
    assert call_args[0][1] == "My Test Session"

    captured = capsys.readouterr()
    assert "My Test Session" in captured.out


def test_new_session_with_duplicate_title_surfaces_error(capsys):
    """new_session(title=...) handles ValueError from a duplicate-title conflict.

    The session is still created; the title assignment fails; the success banner
    must not claim the rejected title as the session name.
    """
    cli = _make_cli()
    cli._session_db = MagicMock()
    cli._session_db.set_session_title.side_effect = ValueError(
        "Title 'Dup' is already in use by session abc-123"
    )
    cli.agent = _FakeAgent("old_session_id", datetime.now())
    cli.conversation_history = []

    # Capture warnings printed via cli._cprint. After importlib.reload(),
    # the method's __globals__ dict is the one from the live module — patch
    # the exact dict the method will read.
    warnings: list[str] = []
    method_globals = cli.new_session.__globals__
    original = method_globals["_cprint"]
    method_globals["_cprint"] = lambda msg: warnings.append(msg)
    try:
        cli.new_session(title="Dup")
    finally:
        method_globals["_cprint"] = original

    cli._session_db.set_session_title.assert_called_once()
    joined = "\n".join(warnings)
    assert "already in use" in joined
    assert "session started untitled" in joined

    # The success banner must NOT claim the rejected title as the session name.
    captured = capsys.readouterr()
    assert "New session started: Dup" not in captured.out
    assert "New session started!" in captured.out
