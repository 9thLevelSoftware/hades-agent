"""Tests for the /branch (/fork) command — session branching.

Verifies that:
- Branching creates a new session with copied conversation history
- The original session is preserved (ended with "branched" reason)
- Auto-generated titles use lineage numbering
- Custom branch names are used when provided
- parent_session_id links are set correctly
- Edge cases: empty conversation, missing session DB
"""

import json
import os
import threading
from datetime import datetime
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def session_db(tmp_path):
    """Create a real SessionDB for testing."""
    os.environ["HADES_HOME"] = str(tmp_path / ".hades")
    os.makedirs(tmp_path / ".hades", exist_ok=True)
    from hades_state import SessionDB
    db = SessionDB(db_path=tmp_path / ".hades" / "test_sessions.db")
    yield db
    db.close()


@pytest.fixture
def cli_instance(tmp_path, session_db):
    """Create a minimal HermesCLI-like object for testing _handle_branch_command."""
    # We'll mock the CLI enough to test the branch logic without full init
    from unittest.mock import MagicMock

    cli = MagicMock()
    cli._session_db = session_db
    cli.session_id = "20260403_120000_abc123"
    cli.model = "anthropic/claude-sonnet-4.6"
    cli.max_turns = 90
    cli.reasoning_config = {"enabled": True, "effort": "medium"}
    cli.session_start = datetime.now()
    cli._pending_title = None
    cli._resumed = False
    cli.agent = None
    from cli import HermesCLI
    cli._restore_invocation_runtime_baseline = MethodType(
        HermesCLI._restore_invocation_runtime_baseline,
        cli,
    )
    cli.conversation_history = [
        {"role": "user", "content": "Hello, can you help me?"},
        {"role": "assistant", "content": "Of course! How can I help?"},
        {"role": "user", "content": "Write a Python function to sort a list."},
        {"role": "assistant", "content": "def sort_list(lst): return sorted(lst)"},
    ]

    # Create the original session in the DB
    session_db.create_session(
        session_id=cli.session_id,
        source="cli",
        model=cli.model,
    )
    session_db.set_session_title(cli.session_id, "My Coding Session")

    return cli


class TestBranchCommandCLI:
    """Test the /branch command logic for the CLI."""

    def test_branch_creates_new_session(self, cli_instance, session_db):
        """Branching should create a new session in the DB."""
        from cli import HermesCLI

        # Call the real method on the mock, using the real implementation
        HermesCLI._handle_branch_command(cli_instance, "/branch")

        # Verify a new session was created
        assert cli_instance.session_id != "20260403_120000_abc123"
        new_session = session_db.get_session(cli_instance.session_id)
        assert new_session is not None

    def test_branch_copies_history(self, cli_instance, session_db):
        """Branching should copy all messages to the new session."""
        from cli import HermesCLI

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        messages = session_db.get_messages_as_conversation(cli_instance.session_id)
        assert len(messages) == 4  # All 4 messages copied

    def test_branch_preserves_parent_link(self, cli_instance, session_db):
        """The new session should reference the original as parent."""
        from cli import HermesCLI
        original_id = cli_instance.session_id

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        new_session = session_db.get_session(cli_instance.session_id)
        assert new_session["parent_session_id"] == original_id

    def test_branch_persists_fresh_runtime_boundary(self, cli_instance, session_db):
        """Copied transcript is history, not evidence that the child already ran."""
        from cli import HermesCLI

        original_id = cli_instance.session_id
        copied_count = len(cli_instance.conversation_history)

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        child = session_db.get_session(cli_instance.session_id)
        model_config = json.loads(child["model_config"])
        assert model_config["_branched_from"] == original_id
        assert model_config["_branch_point_message_count"] == copied_count
        assert child["message_count"] == copied_count

    def test_branch_ends_original_session(self, cli_instance, session_db):
        """The original session should be marked as ended with 'branched' reason."""
        from cli import HermesCLI
        original_id = cli_instance.session_id

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        original = session_db.get_session(original_id)
        assert original["end_reason"] == "branched"

    def test_branch_with_custom_name(self, cli_instance, session_db):
        """Custom branch name should be used as the title."""
        from cli import HermesCLI

        HermesCLI._handle_branch_command(cli_instance, "/branch refactor approach")

        title = session_db.get_session_title(cli_instance.session_id)
        assert title == "refactor approach"

    def test_branch_auto_title_lineage(self, cli_instance, session_db):
        """Without a name, branch should auto-generate a title from the parent's title."""
        from cli import HermesCLI

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        title = session_db.get_session_title(cli_instance.session_id)
        assert title == "My Coding Session #2"

    def test_branch_empty_conversation(self, cli_instance, session_db):
        """Branching with no history should show an error."""
        from cli import HermesCLI
        cli_instance.conversation_history = []

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        # session_id should not have changed
        assert cli_instance.session_id == "20260403_120000_abc123"

    def test_branch_no_session_db(self, cli_instance):
        """Branching without a session DB should show an error."""
        from cli import HermesCLI
        cli_instance._session_db = None

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        # session_id should not have changed
        assert cli_instance.session_id == "20260403_120000_abc123"

    def test_branch_discards_parent_agent_and_defers_child_construction(
        self, cli_instance, session_db
    ):
        """A branch cannot relabel the parent's routed client or prompt cache."""
        from cli import HermesCLI

        agent = MagicMock()
        agent.session_id = cli_instance.session_id
        agent._last_flushed_db_idx = 0
        cli_instance.agent = agent
        cli_instance._active_agent_route_signature = ("parent", "route")
        original_id = cli_instance.session_id

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        assert agent.session_id == original_id
        agent.reset_session_state.assert_not_called()
        agent.release_clients.assert_not_called()
        assert cli_instance.agent is None
        assert cli_instance._active_agent_route_signature is None
        assert cli_instance._runtime_branch_pending_fresh is True

    def test_branch_does_not_block_on_parent_client_retirement(
        self, cli_instance, session_db
    ):
        """The child boundary returns while detached agent.close is still blocked."""
        import cli as cli_mod
        from cli import HermesCLI

        agent = MagicMock()
        agent.session_id = cli_instance.session_id
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

        agent.release_clients.side_effect = _blocking_release
        agent.close.side_effect = _blocking_close
        cli_instance.agent = agent
        command_thread = threading.Thread(
            target=lambda: (
                HermesCLI._handle_branch_command(cli_instance, "/branch"),
                command_done.set(),
            ),
            daemon=True,
        )
        command_thread.start()
        try:
            assert command_done.wait(0.5), "/branch waited for detached client teardown"
            assert release_called.is_set() is False
            assert close_started.wait(1)
        finally:
            allow_release.set()
            allow_close.set()
            command_thread.join(timeout=1)

        assert cli_mod._drain_retired_cli_agents(timeout=1) is True
        agent.release_clients.assert_not_called()
        agent.close.assert_called_once_with()

    def test_branch_lineage_switch_runs_on_tracked_retirement(
        self, cli_instance, session_db
    ):
        """Queued parent work drains before a nonblocking lineage switch."""
        import cli as cli_mod
        from cli import HermesCLI

        agent = MagicMock()
        agent.session_id = cli_instance.session_id
        manager = MagicMock()
        flush_started = threading.Event()
        allow_flush = threading.Event()
        switch_started = threading.Event()
        command_done = threading.Event()
        ordering = []

        def _blocking_flush(*, timeout):
            ordering.append(("flush", timeout))
            flush_started.set()
            allow_flush.wait(2)
            return True

        def _switch(new_session_id, **kwargs):
            ordering.append(("switch", new_session_id, kwargs))
            switch_started.set()

        manager.flush_pending.side_effect = _blocking_flush
        manager.on_session_switch.side_effect = _switch
        manager.shutdown_all.side_effect = lambda: ordering.append(("shutdown",))
        agent.close.side_effect = lambda: ordering.append(("close",))
        agent._memory_manager = manager
        cli_instance.agent = agent
        parent_session_id = cli_instance.session_id
        command_thread = threading.Thread(
            target=lambda: (
                HermesCLI._handle_branch_command(cli_instance, "/branch"),
                command_done.set(),
            ),
            daemon=True,
        )

        previous_done = cli_mod._cleanup_done
        previous_active = cli_mod._active_agent_ref
        cli_mod._active_agent_ref = agent
        release_timer = None
        command_thread.start()
        try:
            assert flush_started.wait(1)
            assert command_done.wait(0.5), "/branch waited for queued parent work"
            assert switch_started.is_set() is False
            assert ordering == [("flush", None)]

            # Simulate quitting immediately after /branch returns. Cleanup must
            # wait for the tracked retirement worker, which owns the provider
            # drain-before-switch-before-shutdown-before-close sequence. Do not
            # clear the active reference here: /branch itself must detach it
            # before retirement starts so cleanup cannot finalize it twice.
            release_timer = threading.Timer(0.05, allow_flush.set)
            release_timer.daemon = True
            release_timer.start()
            cli_mod._cleanup_done = False
            assert cli_mod._active_agent_ref is None
            cli_mod._run_cleanup(notify_session_finalize=False)
        finally:
            allow_flush.set()
            command_thread.join(timeout=1)
            if release_timer is not None:
                release_timer.join(timeout=1)
            cli_mod._cleanup_done = previous_done
            cli_mod._active_agent_ref = previous_active
            cli_mod._drain_retired_cli_agents(timeout=1)

        assert ordering == [
            ("flush", None),
            (
                "switch",
                cli_instance.session_id,
                {
                    "parent_session_id": parent_session_id,
                    "reset": False,
                    "reason": "branch",
                },
            ),
            ("shutdown",),
            ("close",),
        ]

    def test_branch_create_failure_leaves_parent_runtime_and_agent_intact(
        self, cli_instance, session_db, monkeypatch
    ):
        """A failed child insert must not partially cross the branch boundary."""
        from cli import HermesCLI

        parent_id = cli_instance.session_id
        parent_agent = MagicMock()
        cli_instance.agent = parent_agent
        cli_instance.model = "parent/session-model"
        cli_instance.provider = "parent-provider"
        cli_instance.requested_provider = "parent-provider"
        cli_instance._runtime_manual_pin = True
        cli_instance._runtime_manual_pin_source = "cli_model_command"
        cli_instance._pending_model_switch_note = "[parent switch]"
        monkeypatch.setattr(
            session_db,
            "create_session",
            MagicMock(side_effect=RuntimeError("disk full")),
        )

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        assert cli_instance.session_id == parent_id
        assert cli_instance.agent is parent_agent
        assert cli_instance.model == "parent/session-model"
        assert cli_instance.provider == "parent-provider"
        assert cli_instance._runtime_manual_pin is True
        assert cli_instance._runtime_manual_pin_source == "cli_model_command"
        assert cli_instance._pending_model_switch_note == "[parent switch]"
        assert session_db.get_session(parent_id)["end_reason"] is None
        parent_agent._flush_messages_to_session_db.assert_not_called()
        parent_agent.release_clients.assert_not_called()

    def test_branch_retires_parent_memory_manager_without_session_end(
        self, cli_instance, session_db, monkeypatch
    ):
        """Branch lineage switches once, then the unused manager is shut down."""
        import cli as cli_mod
        from cli import HermesCLI

        agent = MagicMock()
        agent.session_id = cli_instance.session_id
        manager = MagicMock()
        manager.flush_pending.return_value = True
        agent._memory_manager = manager
        cli_instance.agent = agent
        retirement_state = []
        detach_order = []
        real_retire = cli_mod._retire_cli_agent
        real_setattr = type(cli_instance).__setattr__

        def _observe_setattr(instance, name, value):
            if (
                instance is cli_instance
                and name == "agent"
                and value is None
                and getattr(instance, "agent", None) is agent
            ):
                detach_order.append(cli_mod._active_agent_ref is None)
            return real_setattr(instance, name, value)

        def _observe_retirement(agent_arg, **kwargs):
            retirement_state.append(
                (
                    cli_instance.agent is None,
                    cli_mod._active_agent_ref is None,
                )
            )
            return real_retire(agent_arg, **kwargs)

        monkeypatch.setattr(cli_mod, "_active_agent_ref", agent)
        monkeypatch.setattr(cli_mod, "_retire_cli_agent", _observe_retirement)
        monkeypatch.setattr(type(cli_instance), "__setattr__", _observe_setattr)

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        assert cli_mod._drain_retired_cli_agents(timeout=1) is True
        assert detach_order == [True]
        assert retirement_state == [(True, True)]
        manager.on_session_switch.assert_called_once()
        manager.on_session_end.assert_not_called()
        manager.shutdown_all.assert_called_once_with()
        agent.close.assert_called_once_with()

    def test_branch_drops_parent_model_pin_and_restores_unpinned_invocation(
        self, cli_instance, session_db
    ):
        """A /model choice belongs to the parent conversation, not its child."""
        from cli import HermesCLI

        cli_instance._initial_runtime_manual_pin = False
        cli_instance._initial_runtime_baseline = {
            "model": "configured/default",
            "provider": "auto",
            "requested_provider": "auto",
            "api_key": "configured-secret",
            "base_url": "https://configured.invalid/v1",
            "api_mode": "chat_completions",
            "acp_command": None,
            "acp_args": [],
            "_credential_pool": None,
            "_explicit_api_key": None,
            "_explicit_base_url": None,
            "_provider_source": None,
            "reasoning_config": {"effort": "medium"},
        }
        cli_instance.model = "parent/session-pin"
        cli_instance.provider = "parent-provider"
        cli_instance.requested_provider = "parent-provider"
        cli_instance._runtime_manual_pin = True
        cli_instance._runtime_manual_pin_source = "cli_model_command"
        cli_instance._pending_model_switch_note = "[Note: parent model switch]"

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        assert cli_instance.model == "configured/default"
        assert cli_instance.provider == "auto"
        assert cli_instance.requested_provider == "auto"
        assert cli_instance._runtime_manual_pin is False
        assert cli_instance._runtime_manual_pin_source is None
        assert cli_instance._pending_model_switch_note is None

    def test_branch_preserves_explicit_invocation_pin_after_parent_model_switch(
        self, cli_instance, session_db
    ):
        """An explicit CLI model/provider remains the baseline for every session."""
        from cli import HermesCLI

        cli_instance._initial_runtime_manual_pin = True
        cli_instance._initial_runtime_baseline = {
            "model": "invocation/model",
            "provider": "invocation-provider",
            "requested_provider": "invocation-provider",
            "api_key": "invocation-secret",
            "base_url": "https://invocation.invalid/v1",
            "api_mode": "chat_completions",
            "acp_command": None,
            "acp_args": [],
            "_credential_pool": None,
            "_explicit_api_key": "invocation-secret",
            "_explicit_base_url": "https://invocation.invalid/v1",
            "_provider_source": None,
            "reasoning_config": {"effort": "low"},
        }
        cli_instance.model = "parent/session-pin"
        cli_instance.provider = "parent-provider"
        cli_instance.requested_provider = "parent-provider"
        cli_instance._runtime_manual_pin = True
        cli_instance._runtime_manual_pin_source = "cli_model_command"

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        assert cli_instance.model == "invocation/model"
        assert cli_instance.provider == "invocation-provider"
        assert cli_instance.requested_provider == "invocation-provider"
        assert cli_instance._runtime_manual_pin is True
        assert cli_instance._runtime_manual_pin_source == "cli_explicit_runtime"

    def test_branch_sets_resumed_flag(self, cli_instance, session_db):
        """Branch keeps resume-style transcript display but marks routing fresh."""
        from cli import HermesCLI

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        assert cli_instance._resumed is True
        assert cli_instance._runtime_branch_pending_fresh is True

    def test_branch_rotates_hermes_session_id_env_and_context(self, cli_instance, session_db):
        """Branching must update process-local session-id readers too."""
        from cli import HermesCLI
        from gateway.session_context import _UNSET, _VAR_MAP, get_session_env

        old_session_id = cli_instance.session_id
        os.environ["HADES_SESSION_ID"] = old_session_id
        _VAR_MAP["HERMES_SESSION_ID"].set(old_session_id)

        try:
            HermesCLI._handle_branch_command(cli_instance, "/branch")

            assert cli_instance.session_id != old_session_id
            assert os.environ["HADES_SESSION_ID"] == cli_instance.session_id
            assert get_session_env("HERMES_SESSION_ID") == cli_instance.session_id
        finally:
            os.environ.pop("HERMES_SESSION_ID", None)
            _VAR_MAP["HERMES_SESSION_ID"].set(_UNSET)

    def test_branch_fires_on_session_switch_hook(self, cli_instance, session_db):
        """The /branch command must notify memory providers of the rotation.

        Without this, providers that cache per-session state in
        initialize() keep writing under the old session_id. See #6672.
        """
        import cli as cli_mod
        from cli import HermesCLI

        # Wire a real-ish agent object with a MagicMock memory_manager
        agent = MagicMock()
        mm = MagicMock()
        agent._memory_manager = mm
        cli_instance.agent = agent
        original_id = cli_instance.session_id

        HermesCLI._handle_branch_command(cli_instance, "/branch")
        assert cli_mod._drain_retired_cli_agents(timeout=1) is True

        # Hook must have been called exactly once with the new session_id,
        # parent pointing at the branched-from session, reset=False, and
        # reason="branch" for diagnostics.
        assert mm.on_session_switch.call_count == 1
        _, kwargs = mm.on_session_switch.call_args
        assert mm.on_session_switch.call_args.args[0] == cli_instance.session_id
        assert kwargs["parent_session_id"] == original_id
        assert kwargs["reset"] is False
        assert kwargs["reason"] == "branch"

    def test_fork_alias(self):
        """The /fork alias should resolve to 'branch'."""
        from hades_cli.commands import resolve_command
        result = resolve_command("fork")
        assert result is not None
        assert result.name == "branch"


class TestBranchCommandDef:
    """Test the CommandDef registration for /branch."""

    def test_branch_in_registry(self):
        """The branch command should be in the command registry."""
        from hades_cli.commands import COMMAND_REGISTRY
        names = [c.name for c in COMMAND_REGISTRY]
        assert "branch" in names

    def test_branch_has_fork_alias(self):
        """The branch command should have 'fork' as an alias."""
        from hades_cli.commands import COMMAND_REGISTRY
        branch = next(c for c in COMMAND_REGISTRY if c.name == "branch")
        assert "fork" in branch.aliases

    def test_branch_in_session_category(self):
        """The branch command should be in the Session category."""
        from hades_cli.commands import COMMAND_REGISTRY
        branch = next(c for c in COMMAND_REGISTRY if c.name == "branch")
        assert branch.category == "Session"


class TestBranchFlushesBeforeEndSession:
    """Regression for #47202: /branch must flush un-persisted messages to
    the session DB before ending the old session, just like /new and
    compress_context() already do."""

    def test_branch_flushes_when_agent_present(self, cli_instance, session_db):
        from cli import HermesCLI

        agent = MagicMock()
        cli_instance.agent = agent

        HermesCLI._handle_branch_command(cli_instance, "/branch")

        agent._flush_messages_to_session_db.assert_called_once_with(
            cli_instance.conversation_history
        )


class TestBranchRuntimeResumeBoundary:
    def test_cold_untouched_branch_is_fresh_until_a_child_message_is_written(
        self, cli_instance, session_db
    ):
        from cli import HermesCLI

        HermesCLI._handle_branch_command(cli_instance, "/branch")
        child_id = cli_instance.session_id

        cold_cli = SimpleNamespace(
            _resumed=True,
            _session_db=session_db,
            session_id=child_id,
        )
        assert HermesCLI._runtime_routing_is_resume(cold_cli) is False

        session_db.append_message(child_id, "user", "first child task")
        assert HermesCLI._runtime_routing_is_resume(cold_cli) is True

    def test_cold_branch_with_selected_runtime_metadata_replays_before_message_write(
        self, cli_instance, session_db
    ):
        """A completed route handoff is lifecycle evidence even if the call failed."""
        from cli import HermesCLI

        HermesCLI._handle_branch_command(cli_instance, "/branch")
        child_id = cli_instance.session_id
        row = session_db.get_session(child_id)
        config = json.loads(row["model_config"])
        config.update(
            {
                "model": "selected/model",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            }
        )
        session_db.update_session_meta(
            child_id,
            json.dumps(config),
            model="selected/model",
        )

        cold_cli = SimpleNamespace(
            _resumed=True,
            _session_db=session_db,
            session_id=child_id,
        )
        assert HermesCLI._runtime_routing_is_resume(cold_cli) is True

    def test_first_child_build_is_fresh_and_later_rebuild_is_resume(
        self, monkeypatch, session_db
    ):
        import cli as cli_mod
        from agent.runtime_routing import AgentRuntimeContext, AgentRuntimeSpec

        child_id = "classic-cli-branch-runtime-boundary"
        history = [
            {"role": "user", "content": "parent task"},
            {"role": "assistant", "content": "parent response"},
        ]
        session_db.create_session("parent-session", "cli")
        session_db.create_session(
            child_id,
            "cli",
            model="baseline-model",
            model_config={
                "_branched_from": "parent-session",
                "_branch_point_message_count": len(history),
            },
            parent_session_id="parent-session",
        )
        for message in history:
            session_db.append_message(
                child_id, message["role"], message["content"]
            )

        shell = cli_mod.HermesCLI(compact=True)
        original_db = shell._session_db
        if original_db is not None and original_db is not session_db:
            original_db.close()
        shell._session_db = session_db
        shell.session_id = child_id
        shell._resumed = True
        shell._runtime_branch_pending_fresh = True
        shell.conversation_history = list(history)
        shell.agent = None
        shell.model = "baseline-model"
        shell.provider = "openrouter"
        shell.requested_provider = "openrouter"
        shell.api_key = "baseline-secret"
        shell.base_url = "https://baseline.invalid/v1"
        shell.api_mode = "chat_completions"
        shell._credential_pool = None
        shell._runtime_manual_pin = False
        shell._runtime_manual_pin_source = None
        shell._pending_title = None
        shell._install_tool_callbacks = lambda: None
        shell._ensure_tirith_security = lambda: None

        selected = AgentRuntimeSpec(
            model="selected-model",
            provider="openrouter",
            base_url="https://selected.invalid/v1",
            api_key="selected-secret",
            resolution_state="resolved",
            api_mode="chat_completions",
        )
        prepared = SimpleNamespace(
            plan=SimpleNamespace(action="project", owns_fallbacks=True)
        )
        lifecycle: list[tuple[object, bool]] = []

        def handoff(**kwargs):
            lifecycle.append((kwargs["initial_task"], kwargs["is_resume"]))
            context = AgentRuntimeContext(
                scope="fresh_session",
                task=None if kwargs["is_resume"] else kwargs["initial_task"],
                session_id=child_id,
                task_id=child_id,
                is_resume=kwargs["is_resume"],
                metadata={"platform": "cli"},
            )
            return context, prepared, selected

        built: list[dict] = []

        def fake_agent(**kwargs):
            built.append(kwargs)
            return SimpleNamespace(
                _print_fn=None,
                model=kwargs["model"],
                provider=kwargs["provider"],
            )

        shell._runtime_routing_handoff = handoff
        monkeypatch.setattr(
            "agent.runtime_routing.runtime_resolver_requires_initial_task",
            lambda scope: scope == "fresh_session",
        )
        monkeypatch.setattr(
            "hermes_cli.mcp_startup.wait_for_mcp_discovery", lambda: None
        )
        monkeypatch.setattr(cli_mod, "_active_agent_ref", None)
        monkeypatch.setattr(cli_mod, "_prepare_deferred_agent_startup", lambda: None)
        monkeypatch.setattr(cli_mod, "AIAgent", fake_agent)

        assert shell._init_agent(initial_task="first child task") is True
        assert lifecycle == [("first child task", False)]
        assert built[0]["model"] == "selected-model"
        child = session_db.get_session(child_id)
        persisted = json.loads(child["model_config"])
        assert persisted["_branched_from"] == "parent-session"
        assert persisted["_branch_point_message_count"] == len(history)
        assert persisted["model"] == "selected-model"
        assert persisted["provider"] == "openrouter"
        assert persisted["base_url"] == "https://selected.invalid/v1"
        assert "selected-secret" not in child["model_config"]

        shell.agent = None
        assert shell._init_agent(initial_task="later child task") is True
        assert lifecycle[-1] == ("later child task", True)

    def test_process_cold_shell_replays_the_exact_bound_runtime(
        self, monkeypatch, cli_instance, session_db
    ):
        from agent.runtime_routing import AgentRuntimePlan, AgentRuntimeSpec
        from cli import HermesCLI
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

        HermesCLI._handle_branch_command(cli_instance, "/branch")
        child_id = cli_instance.session_id
        selected = AgentRuntimeSpec(
            model="selected-model",
            provider="custom:selected",
            base_url="https://selected.invalid/v1",
            api_key="selected-secret",
            resolution_state="resolved",
            api_mode="chat_completions",
            reasoning_config={"effort": "high"},
        )

        class DurableLikeResolver:
            def __init__(self):
                self.bindings = {}
                self.decision_count = 0
                self.requests = []

            def requires_initial_task(self, scope):
                return scope == "fresh_session"

            def resolve(self, request):
                self.requests.append(request)
                runtime = self.bindings.get(request.context.session_id)
                if runtime is None:
                    assert request.context.is_resume is False
                    self.decision_count += 1
                    runtime = selected
                    self.bindings[request.context.session_id] = runtime
                return AgentRuntimePlan(
                    action="project",
                    runtime=runtime,
                    decision_id="decision-classic-cli-branch",
                    bound_route_identity="decision-classic-cli-branch",
                    owns_fallbacks=True,
                    reason_code="active_projected",
                )

            def record_manual_pin(self, _request):
                return None

            def record_session_continuation(self, _request):
                return None

        resolver = DurableLikeResolver()
        manager = PluginManager()
        manager._discovered = True
        context = PluginContext(
            PluginManifest(name="classic-cli-branch-router", key="branch-router"),
            manager,
        )
        context.register_agent_runtime_resolver(resolver)
        monkeypatch.setattr("hermes_cli.plugins._plugin_manager", manager)

        def shell(*, pending):
            return SimpleNamespace(
                api_key="baseline-secret",
                base_url="https://baseline.invalid/v1",
                provider="openrouter",
                api_mode="chat_completions",
                acp_command=None,
                acp_args=[],
                _credential_pool=None,
                model="baseline-model",
                reasoning_config={"effort": "low"},
                _fallback_model=[],
                service_tier=None,
                _session_db=session_db,
                _resumed=True,
                _runtime_branch_pending_fresh=pending,
                session_id=child_id,
            )

        first_shell = shell(pending=True)
        first_resume = HermesCLI._runtime_routing_is_resume(first_shell)
        first_context, _first_prepared, first_runtime = (
            HermesCLI._runtime_routing_handoff(
                first_shell,
                initial_task="first child task",
                session_id=child_id,
                task_id=child_id,
                is_resume=first_resume,
            )
        )

        session_db.append_message(child_id, "user", "first child task")
        cold_shell = shell(pending=None)
        cold_resume = HermesCLI._runtime_routing_is_resume(cold_shell)
        cold_context, _cold_prepared, cold_runtime = (
            HermesCLI._runtime_routing_handoff(
                cold_shell,
                initial_task="must not be classified again",
                session_id=child_id,
                task_id=child_id,
                is_resume=cold_resume,
            )
        )

        assert first_context.is_resume is False
        assert first_context.task == "first child task"
        assert cold_context.is_resume is True
        assert cold_context.task is None
        assert resolver.decision_count == 1
        assert first_runtime == cold_runtime == selected
        assert cold_runtime.reasoning_config == {"effort": "high"}
        assert cold_runtime.api_key == "selected-secret"
