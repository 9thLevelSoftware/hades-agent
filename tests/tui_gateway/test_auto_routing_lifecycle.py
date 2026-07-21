import contextlib
import json
import threading
import types

import pytest

from tui_gateway import server


@pytest.fixture(autouse=True)
def _isolate_sessions():
    original = dict(server._sessions)
    server._sessions.clear()
    try:
        yield
    finally:
        server._sessions.clear()
        server._sessions.update(original)


class _TrackingDB:
    def __init__(self, row=None):
        self.row = row
        self.closed = 0

    def close(self):
        self.closed += 1

    def get_session(self, _target):
        return self.row

    def get_session_by_title(self, _target):
        return None

    def resolve_resume_session_id(self, target):
        return target

    def reopen_session(self, _target):
        return None

    def get_messages_as_conversation(self, _target, include_ancestors=False):
        return [{"role": "user", "content": "stored"}] if self.row else []


def _patch_profile_resume(monkeypatch, tmp_path, db):
    profile_home = tmp_path / "profiles" / "worker"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(server, "_profile_home", lambda _profile: profile_home)
    monkeypatch.setattr("hermes_state.SessionDB", lambda db_path=None: db)
    return profile_home


def test_profile_resume_closes_lookup_db_on_not_found(monkeypatch, tmp_path):
    db = _TrackingDB()
    _patch_profile_resume(monkeypatch, tmp_path, db)

    response = server._methods["session.resume"](
        "resume", {"session_id": "missing", "profile": "worker"}
    )

    assert response["error"]["code"] == 4007
    assert db.closed == 1


def test_profile_lazy_resume_closes_lookup_db_after_record_registration(
    monkeypatch, tmp_path
):
    db = _TrackingDB({"id": "stored", "message_count": 1})
    _patch_profile_resume(monkeypatch, tmp_path, db)
    monkeypatch.setattr(
        server, "_claim_active_session_slot", lambda *_args, **_kwargs: (None, None)
    )
    monkeypatch.setattr(server, "_register_session_cwd", lambda *_args: None)
    monkeypatch.setattr(server, "_child_run_active", lambda *_args: False)

    response = server._methods["session.resume"](
        "resume",
        {"session_id": "stored", "profile": "worker", "lazy": True},
    )

    assert response["result"]["resumed"] == "stored"
    assert db.closed == 1


def test_profile_eager_resume_transfers_db_ownership_to_published_agent(
    monkeypatch, tmp_path
):
    db = _TrackingDB({"id": "stored", "message_count": 1})
    profile_home = _patch_profile_resume(monkeypatch, tmp_path, db)
    captured = {}

    class Agent:
        model = "selected/model"

    agent = Agent()

    def make_agent(*_args, **kwargs):
        captured.update(kwargs)
        agent._session_db = kwargs["session_db"]
        agent._owns_session_db = kwargs.get("owns_session_db", False)
        return agent

    def init_session(sid, key, built, history, **_kwargs):
        captured["init_profile_home"] = _kwargs.get("profile_home")
        server._sessions[sid] = {
            "agent": built,
            "session_key": key,
            "history": history,
            "history_lock": threading.Lock(),
            "created_at": 1.0,
            "cwd": str(tmp_path),
            "source": "tui",
            "profile_home": (
                str(_kwargs["profile_home"])
                if _kwargs.get("profile_home")
                else None
            ),
        }

    monkeypatch.setattr(server, "_make_agent", make_agent)
    monkeypatch.setattr(server, "_init_session", init_session)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {})
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(
        server, "_claim_active_session_slot", lambda *_args, **_kwargs: (None, None)
    )
    monkeypatch.setattr(
        server, "_runtime_routing_requires_initial_task", lambda: False
    )

    response = server._methods["session.resume"](
        "resume",
        {"session_id": "stored", "profile": "worker", "eager_build": True},
    )

    sid = response["result"]["session_id"]
    assert captured["session_db"] is db
    assert captured["owns_session_db"] is True
    assert str(captured["init_profile_home"]) == str(profile_home)
    assert db.closed == 0
    assert server._sessions[sid]["profile_home"] == str(profile_home)


def test_init_session_stores_profile_before_constructing_slash_worker(
    monkeypatch, tmp_path
):
    profile_home = tmp_path / "profiles" / "worker"
    profile_home.mkdir(parents=True)
    captured = []

    class Worker:
        def __init__(self, _key, _model, *, profile_home=None):
            captured.append(profile_home)

        def close(self):
            return None

    agent = types.SimpleNamespace(model="selected/model")
    db = _TrackingDB({"id": "stored", "cwd": str(tmp_path)})
    monkeypatch.setattr(server, "_SlashWorker", Worker)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *_args: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda *_args: None)
    monkeypatch.setattr(server, "_start_notification_poller", lambda *_args: None)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args: None)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {})
    monkeypatch.setattr(server, "_schedule_mcp_late_refresh", lambda *_args: None)

    server._init_session(
        "ui",
        "stored",
        agent,
        [],
        cwd=str(tmp_path),
        session_db=db,
        source="tui",
        profile_home=str(profile_home),
    )

    assert captured == [str(profile_home)]
    assert server._sessions["ui"]["profile_home"] == str(profile_home)


def test_profile_eager_resume_loser_soft_disposes_without_ending_shared_row(
    monkeypatch, tmp_path
):
    db = _TrackingDB({"id": "stored", "message_count": 1})
    _patch_profile_resume(monkeypatch, tmp_path, db)
    calls = {"find": 0, "released": 0, "shutdown": 0, "closed": 0}
    winner = {
        "agent": types.SimpleNamespace(model="winner/model"),
        "session_key": "stored",
        "history": [],
        "history_lock": threading.Lock(),
        "created_at": 1.0,
        "running": False,
        "source": "tui",
    }

    class LoserAgent:
        model = "loser/model"
        _owns_session_db = True
        _session_db = db

        def close(self):
            calls["closed"] += 1

        def release_clients(self):
            calls["released"] += 1

        def shutdown_memory_provider(self, _messages):
            calls["shutdown"] += 1

    def find_live(_key):
        calls["find"] += 1
        return None if calls["find"] == 1 else ("winner", winner)

    monkeypatch.setattr(server, "_find_live_session_by_key", find_live)
    monkeypatch.setattr(server, "_make_agent", lambda *_args, **_kwargs: LoserAgent())
    monkeypatch.setattr(server, "_live_session_payload", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(
        server, "_claim_active_session_slot", lambda *_args, **_kwargs: (None, None)
    )
    monkeypatch.setattr(
        server, "_runtime_routing_requires_initial_task", lambda: False
    )

    response = server._methods["session.resume"](
        "resume",
        {"session_id": "stored", "profile": "worker", "eager_build": True},
    )

    assert response["result"]["resumed"] == "stored"
    assert calls == {"find": 2, "released": 1, "shutdown": 1, "closed": 0}
    assert db.closed == 1


def test_cold_resume_model_switch_replaces_complete_first_build_runtime(
    monkeypatch, tmp_path
):
    """A pre-build /model choice must replace, not sit beside, resume state."""
    import agent.runtime_routing as routing
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(
        "stored",
        source="tui",
        model="old/model",
        model_config={
            "model": "old/model",
            "provider": "openrouter",
            "base_url": "https://old.example/v1",
            "api_mode": "chat_completions",
            "reasoning_config": {"enabled": True, "effort": "high"},
            "service_tier": "priority",
        },
    )
    db.append_message("stored", "user", "stored task")
    captured = {}

    class Agent:
        def __init__(self, kwargs):
            runtime = kwargs["model_override"]
            self.model = runtime["model"]
            self.provider = kwargs["provider_override"]
            self.reasoning_config = kwargs["reasoning_config_override"]
            self.service_tier = kwargs["service_tier_override"]

    class Worker:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    switch_result = types.SimpleNamespace(
        success=True,
        new_model="new/model",
        target_provider="anthropic",
        base_url="https://api.anthropic.com",
        api_key="runtime-secret",
        api_mode="anthropic_messages",
        warning_message="",
    )

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(
        server, "_claim_active_session_slot", lambda *_args, **_kwargs: (None, None)
    )
    monkeypatch.setattr(server, "_register_session_cwd", lambda *_args: None)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(
        server,
        "_schedule_agent_build",
        lambda *_args, **_kwargs: pytest.fail(
            "semantic routing must keep the resumed session deferred"
        ),
    )
    monkeypatch.setattr(
        server, "_runtime_routing_requires_initial_task", lambda: True
    )
    monkeypatch.setattr(server, "_load_service_tier", lambda: None)
    monkeypatch.setattr(server, "_load_fallback_model", lambda: [])
    monkeypatch.setattr(
        routing, "apply_manual_runtime_transition", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model", lambda **_kwargs: switch_result
    )
    monkeypatch.setattr(
        "hermes_cli.model_cost_guard.expensive_model_warning",
        lambda *_args, **_kwargs: None,
    )

    resumed = server._methods["session.resume"](
        "resume", {"session_id": "stored"}
    )
    sid = resumed["result"]["session_id"]
    session = server._sessions[sid]

    switched = server._methods["config.set"](
        "switch",
        {
            "session_id": sid,
            "key": "model",
            "value": "new/model --provider anthropic",
        },
    )
    assert switched["result"]["value"] == "new/model"

    expected_runtime = {
        "model_override": {
            "model": "new/model",
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": "runtime-secret",
            "api_mode": "anthropic_messages",
        },
        "provider_override": "anthropic",
        "reasoning_config_override": {"enabled": True, "effort": "high"},
        "service_tier_override": "priority",
    }
    assert session["resume_runtime_overrides"] == expected_runtime

    persisted = json.loads(db.get_session("stored")["model_config"])
    assert persisted["model"] == "new/model"
    assert persisted["provider"] == "anthropic"
    assert persisted["reasoning_config"] == {
        "enabled": True,
        "effort": "high",
    }
    assert persisted["service_tier"] == "priority"
    assert "runtime-secret" not in json.dumps(persisted)

    def make_agent(*_args, **kwargs):
        captured.update(kwargs)
        return Agent(kwargs)

    monkeypatch.setattr(server, "_make_agent", make_agent)
    monkeypatch.setattr(server, "_set_session_context", lambda *_args: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *_args: None)
    monkeypatch.setattr(server, "_config_model_target", lambda: ("new/model", "anthropic"))
    monkeypatch.setattr(server, "_SlashWorker", Worker)
    monkeypatch.setattr(server, "_wire_callbacks", lambda *_args: None)
    monkeypatch.setattr(
        server, "_start_notification_poller", lambda *_args: threading.Event()
    )
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args: None)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {})
    monkeypatch.setattr(server, "_probe_config_health", lambda *_args: None)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_emit", lambda *_args: None)
    monkeypatch.setattr(server, "_schedule_mcp_late_refresh", lambda *_args: None)
    monkeypatch.setattr(
        "tools.approval.register_gateway_notify", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("tools.approval.load_permanent_allowlist", lambda: None)

    server._start_agent_build(sid, session, initial_task="new task")
    assert session["agent_ready"].wait(2)
    assert session["agent_error"] is None
    assert {key: captured[key] for key in expected_runtime} == expected_runtime
    db.close()


def test_pending_manual_model_switch_uses_active_profile_service_tier(
    monkeypatch, tmp_path
):
    """Profile fast mode is the final tier fallback for an unbuilt session."""
    import agent.runtime_routing as routing
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("stored", source="tui", model="old/model")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_load_service_tier", lambda: "priority")
    monkeypatch.setattr(server, "_load_fallback_model", lambda: [])
    monkeypatch.setattr(
        routing, "apply_manual_runtime_transition", lambda *_args, **_kwargs: None
    )

    session = {
        "agent": None,
        "session_key": "stored",
        "resume_session_id": "stored",
        "resume_runtime_overrides": {},
    }
    result = types.SimpleNamespace(
        new_model="new/model",
        target_provider="anthropic",
        base_url=None,
        api_key="runtime-secret",
        api_mode="anthropic_messages",
    )

    server._record_tui_manual_runtime_transition(
        sid="sid", session=session, result=result
    )

    persisted = json.loads(db.get_session("stored")["model_config"])
    assert persisted["service_tier"] == "priority"
    db.close()


def test_manual_model_switch_service_tier_precedence(monkeypatch):
    calls = {"profile": 0}
    result = types.SimpleNamespace(
        new_model="new/model",
        target_provider="anthropic",
        base_url=None,
        api_key=None,
        api_mode="anthropic_messages",
    )

    def profile_tier():
        calls["profile"] += 1
        return "profile-tier"

    monkeypatch.setattr(server, "_load_service_tier", profile_tier)

    live = server._manual_model_runtime_overrides(
        {
            "agent": types.SimpleNamespace(
                reasoning_config=None, service_tier="live-tier"
            ),
            "create_service_tier_override": "create-tier",
            "resume_runtime_overrides": {
                "service_tier_override": "resume-tier"
            },
        },
        result,
    )
    created = server._manual_model_runtime_overrides(
        {
            "agent": None,
            "create_service_tier_override": "create-tier",
            "resume_runtime_overrides": {
                "service_tier_override": "resume-tier"
            },
        },
        result,
    )
    resumed = server._manual_model_runtime_overrides(
        {
            "agent": None,
            "resume_runtime_overrides": {
                "service_tier_override": "resume-tier"
            },
        },
        result,
    )
    profile = server._manual_model_runtime_overrides(
        {"agent": None, "resume_runtime_overrides": {}}, result
    )

    assert [
        live["service_tier_override"],
        created["service_tier_override"],
        resumed["service_tier_override"],
        profile["service_tier_override"],
    ] == ["live-tier", "create-tier", "resume-tier", "profile-tier"]
    assert calls["profile"] == 1


def test_eager_resume_rolls_back_partially_published_session_without_ending_row(
    monkeypatch, tmp_path
):
    db = _TrackingDB(
        {"id": "stored", "message_count": 1, "cwd": str(tmp_path)}
    )
    _patch_profile_resume(monkeypatch, tmp_path, db)
    calls = {
        "agent_close": 0,
        "agent_release": 0,
        "memory_shutdown": 0,
        "end_session": 0,
        "lease_release": 0,
        "notify_registered": 0,
        "notify_unregistered": 0,
        "worker_close": 0,
    }
    captured = {}
    poller_stop = threading.Event()

    class Lease:
        def release(self):
            calls["lease_release"] += 1

    class Agent:
        model = "selected/model"
        _owns_session_db = False
        _session_db = db

        def close(self):
            calls["agent_close"] += 1
            db.end_session("stored", "agent_close")

        def release_clients(self):
            calls["agent_release"] += 1

        def shutdown_memory_provider(self, _messages):
            calls["memory_shutdown"] += 1

    class Worker:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            calls["worker_close"] += 1

    def end_session(_session_id, _reason):
        calls["end_session"] += 1

    def wire_then_fail(sid):
        captured["sid"] = sid
        captured["record"] = server._sessions[sid]
        # Rollback must also stop a poller if initialization published one
        # before a later callback-wiring failure.
        captured["record"]["_notif_stop"] = poller_stop
        raise RuntimeError("callback wiring failed")

    db.end_session = end_session
    monkeypatch.setattr(server, "_make_agent", lambda *_args, **_kwargs: Agent())
    monkeypatch.setattr(server, "_SlashWorker", Worker)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *_args: None)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(
        server, "_claim_active_session_slot", lambda *_args, **_kwargs: (Lease(), None)
    )
    monkeypatch.setattr(
        server, "_runtime_routing_requires_initial_task", lambda: False
    )
    monkeypatch.setattr(server, "_set_session_context", lambda *_args: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *_args: None)
    monkeypatch.setattr(server, "_wire_callbacks", wire_then_fail)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args: None)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)
    monkeypatch.setattr(server, "_schedule_mcp_late_refresh", lambda *_args: None)
    monkeypatch.setattr(
        "tools.approval.register_gateway_notify",
        lambda *_args, **_kwargs: calls.__setitem__(
            "notify_registered", calls["notify_registered"] + 1
        ),
    )
    monkeypatch.setattr(
        "tools.approval.unregister_gateway_notify",
        lambda *_args, **_kwargs: calls.__setitem__(
            "notify_unregistered", calls["notify_unregistered"] + 1
        ),
    )
    monkeypatch.setattr("tools.approval.load_permanent_allowlist", lambda: None)

    response = server._methods["session.resume"](
        "resume",
        {"session_id": "stored", "profile": "worker", "eager_build": True},
    )

    assert response["error"]["code"] == 5000
    assert "callback wiring failed" in response["error"]["message"]
    assert server._sessions.get(captured["sid"]) is not captured["record"]
    assert poller_stop.is_set()
    assert db.closed == 1
    assert calls == {
        "agent_close": 0,
        "agent_release": 1,
        "memory_shutdown": 1,
        "end_session": 0,
        "lease_release": 1,
        "notify_registered": 1,
        "notify_unregistered": 1,
        "worker_close": 1,
    }


def test_start_agent_build_deferred_closes_profile_db(monkeypatch, tmp_path):
    from agent.runtime_routing import RuntimeRoutingDeferred

    db = _TrackingDB()
    profile_home = tmp_path / "profiles" / "worker"
    profile_home.mkdir(parents=True)
    ready = threading.Event()
    session = {
        "agent": None,
        "agent_error": None,
        "agent_ready": ready,
        "agent_build_started": False,
        "session_key": "stored",
        "profile_home": str(profile_home),
        "history_lock": threading.Lock(),
        "source": "tui",
    }
    server._sessions["sid"] = session
    monkeypatch.setattr("hermes_state.SessionDB", lambda db_path=None: db)
    monkeypatch.setattr(
        server, "_runtime_routing_requires_initial_task", lambda: False
    )
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeRoutingDeferred(retry_after_seconds=2)
        ),
    )

    server._start_agent_build("sid", session)
    assert ready.wait(2)

    assert session["runtime_routing_deferred"] == 2
    assert session["agent_error"] is None
    assert db.closed == 1


def test_start_agent_build_stale_result_is_soft_disposed_and_wakes_waiter(
    monkeypatch, tmp_path
):
    db = _TrackingDB()
    profile_home = tmp_path / "profiles" / "worker"
    profile_home.mkdir(parents=True)
    entered = threading.Event()
    release = threading.Event()
    ready = threading.Event()
    calls = {"released": 0, "shutdown": 0, "closed": 0}

    class Agent:
        model = "selected/model"
        _owns_session_db = True
        _session_db = db

        def close(self):
            calls["closed"] += 1

        def release_clients(self):
            calls["released"] += 1

        def shutdown_memory_provider(self, _messages):
            calls["shutdown"] += 1

    def make_agent(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        return Agent()

    session = {
        "agent": None,
        "agent_error": None,
        "agent_ready": ready,
        "agent_build_started": False,
        "session_key": "stored",
        "profile_home": str(profile_home),
        "history_lock": threading.Lock(),
        "source": "tui",
    }
    server._sessions["sid"] = session
    monkeypatch.setattr("hermes_state.SessionDB", lambda db_path=None: db)
    monkeypatch.setattr(server, "_make_agent", make_agent)
    monkeypatch.setattr(
        server, "_runtime_routing_requires_initial_task", lambda: False
    )

    server._start_agent_build("sid", session)
    assert entered.wait(2)
    with server._sessions_lock:
        server._sessions.pop("sid")
        session["_finalized"] = True
    release.set()
    assert ready.wait(2)

    assert "closed" in session["agent_error"]
    assert session["agent"] is None
    assert calls == {"released": 1, "shutdown": 1, "closed": 0}
    assert db.closed == 1


def test_start_agent_build_error_after_construction_detaches_and_disposes_agent(
    monkeypatch, tmp_path
):
    db = _TrackingDB()
    profile_home = tmp_path / "profiles" / "worker"
    profile_home.mkdir(parents=True)
    ready = threading.Event()
    calls = {
        "released": 0,
        "shutdown": 0,
        "worker_closed": 0,
        "notify_registered": 0,
        "notify_unregistered": 0,
    }

    class Worker:
        def close(self):
            calls["worker_closed"] += 1

    class Agent:
        model = "selected/model"
        _owns_session_db = True
        _session_db = db

        def release_clients(self):
            calls["released"] += 1

        def shutdown_memory_provider(self, _messages):
            calls["shutdown"] += 1

    session = {
        "agent": None,
        "agent_error": None,
        "agent_ready": ready,
        "agent_build_started": False,
        "session_key": "stored",
        "profile_home": str(profile_home),
        "history_lock": threading.Lock(),
        "source": "tui",
    }
    server._sessions["sid"] = session
    monkeypatch.setattr("hermes_state.SessionDB", lambda db_path=None: db)
    monkeypatch.setattr(server, "_make_agent", lambda *_args, **_kwargs: Agent())
    monkeypatch.setattr(server, "_config_model_target", lambda: ("model", "provider"))
    monkeypatch.setattr(server, "_SlashWorker", lambda *_args, **_kwargs: Worker())
    import tools.approval as approval

    monkeypatch.setattr(
        approval,
        "register_gateway_notify",
        lambda *_args, **_kwargs: calls.__setitem__(
            "notify_registered", calls["notify_registered"] + 1
        ),
    )
    monkeypatch.setattr(
        approval,
        "unregister_gateway_notify",
        lambda *_args, **_kwargs: calls.__setitem__(
            "notify_unregistered", calls["notify_unregistered"] + 1
        ),
    )
    monkeypatch.setattr(approval, "load_permanent_allowlist", lambda: None)
    monkeypatch.setattr(
        server,
        "_wire_callbacks",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("callback wiring failed")),
    )
    monkeypatch.setattr(
        server, "_runtime_routing_requires_initial_task", lambda: False
    )

    server._start_agent_build("sid", session)
    assert ready.wait(2)

    assert session["agent"] is None
    assert session["slash_worker"] is None
    assert session["agent_error"] == "callback wiring failed"
    assert calls == {
        "released": 1,
        "shutdown": 1,
        "worker_closed": 1,
        "notify_registered": 1,
        "notify_unregistered": 1,
    }
    assert db.closed == 1


def test_prompt_waiter_aborts_if_session_closes_after_build_ready(monkeypatch):
    captured = {}

    class CapturedThread:
        def __init__(self, target=None, daemon=None):
            captured["target"] = target

        def start(self):
            return None

        def is_alive(self):
            return False

    ready = threading.Event()
    ready.set()
    session = {
        "agent": types.SimpleNamespace(),
        "agent_error": None,
        "agent_ready": ready,
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
        "session_key": "stored",
        "attached_images": [],
    }
    server._sessions["sid"] = session
    monkeypatch.setattr(server.threading, "Thread", CapturedThread)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda *_args, **_kwargs: pytest.fail("closed waiter must not run a prompt"),
    )

    response = server._methods["prompt.submit"](
        "submit", {"session_id": "sid", "text": "work"}
    )
    assert response["result"]["status"] == "streaming"
    with server._sessions_lock:
        server._sessions.pop("sid")
        session["_finalized"] = True
    captured["target"]()

    assert session["running"] is False


def test_reset_session_agent_discards_stale_replacement_atomically(
    monkeypatch, tmp_path
):
    db = _TrackingDB()
    profile_home = tmp_path / "profiles" / "worker"
    profile_home.mkdir(parents=True)
    old_agent = types.SimpleNamespace(reasoning_config=None)
    calls = {"released": 0, "shutdown": 0, "closed": 0}

    class Agent:
        model = "replacement/model"
        _owns_session_db = True
        _session_db = db

        def close(self):
            calls["closed"] += 1

        def release_clients(self):
            calls["released"] += 1

        def shutdown_memory_provider(self, _messages):
            calls["shutdown"] += 1

    session = {
        "agent": old_agent,
        "session_key": "stored",
        "profile_home": str(profile_home),
        "history": [],
        "history_lock": threading.Lock(),
    }
    server._sessions["sid"] = session
    monkeypatch.setattr("hermes_state.SessionDB", lambda db_path=None: db)

    def make_agent(*_args, **_kwargs):
        with server._sessions_lock:
            server._sessions.pop("sid")
            session["_finalized"] = True
        return Agent()

    monkeypatch.setattr(server, "_make_agent", make_agent)

    with pytest.raises(RuntimeError, match="closed"):
        server._reset_session_agent("sid", session)

    assert session["agent"] is old_agent
    assert calls == {"released": 1, "shutdown": 1, "closed": 0}
    assert db.closed == 1


def test_finalize_session_ends_row_in_profile_db(monkeypatch):
    calls = []

    class DB:
        def get_session(self, session_id):
            calls.append(("get", session_id))
            return {"source": "tui"}

        def end_session(self, session_id, reason):
            calls.append(("end", session_id, reason))

    @contextlib.contextmanager
    def profile_db(session):
        calls.append(("scope", session["profile_home"]))
        yield DB()

    monkeypatch.setattr(server, "_session_db", profile_db)
    monkeypatch.setattr(
        server,
        "_get_db",
        lambda: pytest.fail("profile finalization must not use the launch db"),
    )
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args: None)

    server._finalize_session(
        {
            "agent": None,
            "session_key": "stored",
            "profile_home": "C:/profiles/worker",
            "source": "tui",
            "history": [],
            "history_lock": threading.Lock(),
        }
    )

    assert calls == [
        ("scope", "C:/profiles/worker"),
        ("get", "stored"),
        ("end", "stored", "tui_close"),
    ]


def test_background_agent_closes_before_profile_db_scope_exits(monkeypatch):
    db_scope_open = {"value": False}
    closed_while_open = []

    @contextlib.contextmanager
    def profile_db(_session):
        db_scope_open["value"] = True
        try:
            yield object()
        finally:
            db_scope_open["value"] = False

    class BackgroundAgent:
        def run_conversation(self, **_kwargs):
            return {"final_response": "done"}

        def close(self):
            closed_while_open.append(db_scope_open["value"])

    class ImmediateThread:
        def __init__(self, target=None, daemon=None):
            self.target = target

        def start(self):
            self.target()

    sid = "background"
    server._sessions[sid] = {
        "agent": None,
        "session_key": "stored",
        "history_lock": threading.Lock(),
        "cwd": "C:/workspace",
    }
    monkeypatch.setattr(server, "_session_db", profile_db)
    monkeypatch.setattr(
        server, "_make_background_agent", lambda *_args, **_kwargs: BackgroundAgent()
    )
    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)

    response = server._methods["prompt.background"](
        "background", {"session_id": sid, "text": "work"}
    )

    assert response["result"]["task_id"].startswith("bg_")
    assert closed_while_open == [True]
    assert db_scope_open["value"] is False
