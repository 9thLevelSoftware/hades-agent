"""Tests for tui_gateway JSON-RPC protocol plumbing."""

import io
import json
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_original_stdout = sys.stdout


@pytest.fixture(autouse=True)
def _restore_stdout():
    yield
    sys.stdout = _original_stdout


@pytest.fixture()
def server():
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test")),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        import importlib
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        # Reset module-level session state without re-importing. importlib.reload
        # would re-register the module's atexit hooks (ThreadPoolExecutor
        # shutdown, _shutdown_sessions); the duplicates race the stderr
        # buffer at interpreter shutdown and surface as Fatal Python error:
        # _enter_buffered_busy. Clearing the per-session dicts gives the
        # next test a clean slate; _methods is NOT cleared because it's
        # populated at module import time and re-registration only happens
        # via reload (which we don't do).
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()


@pytest.fixture()
def capture(server):
    """Redirect server's real stdout to a StringIO and return (server, buf)."""
    buf = io.StringIO()
    server._real_stdout = buf
    return server, buf


# ── JSON-RPC envelope ────────────────────────────────────────────────


def test_unknown_method(server):
    resp = server.handle_request({"id": "1", "method": "bogus"})
    assert resp["error"]["code"] == -32601


def test_ok_envelope(server):
    assert server._ok("r1", {"x": 1}) == {
        "jsonrpc": "2.0", "id": "r1", "result": {"x": 1},
    }


def test_err_envelope(server):
    assert server._err("r2", 4001, "nope") == {
        "jsonrpc": "2.0", "id": "r2", "error": {"code": 4001, "message": "nope"},
    }


# ── write_json ───────────────────────────────────────────────────────


def test_write_json(capture):
    server, buf = capture
    assert server.write_json({"test": True})
    assert json.loads(buf.getvalue()) == {"test": True}


def test_write_json_broken_pipe(server):
    class _Broken:
        def write(self, _): raise BrokenPipeError
        def flush(self): raise BrokenPipeError

    server._real_stdout = _Broken()
    assert server.write_json({"x": 1}) is False


def test_write_json_closed_stream_returns_false(server):
    """ValueError ('I/O on closed file') used to bubble up; treat as gone."""

    class _Closed:
        def write(self, _): raise ValueError("I/O operation on closed file")
        def flush(self): raise ValueError("I/O operation on closed file")

    server._real_stdout = _Closed()
    assert server.write_json({"x": 1}) is False


def test_write_json_unicode_encode_error_re_raises(server):
    """A non-UTF-8 stdout encoding raises UnicodeEncodeError (a ValueError
    subclass).  It must NOT be swallowed as 'peer gone' — that would let
    `entry.py` exit cleanly via the False path and hide the real config
    bug.  We re-raise so the existing crash-log infrastructure records it."""

    class _AsciiOnly:
        def write(self, line):
            line.encode("ascii")  # raises UnicodeEncodeError on non-ascii
        def flush(self): pass

    server._real_stdout = _AsciiOnly()
    with pytest.raises(UnicodeEncodeError):
        server.write_json({"msg": "héllo"})


def test_write_json_unrelated_value_error_re_raises(server):
    """Only ValueError('...closed file...') means peer gone.  Other
    ValueErrors are programming errors and must surface."""

    class _BadValue:
        def write(self, _): raise ValueError("something else entirely")
        def flush(self): pass

    server._real_stdout = _BadValue()
    with pytest.raises(ValueError, match="something else entirely"):
        server.write_json({"x": 1})


def test_write_json_non_serializable_payload_re_raises(server):
    """Non-JSON-safe payloads are programming errors — they must NOT be
    silently dropped via the False path (which would trigger a clean exit
    in entry.py and mask the real bug)."""
    import io

    server._real_stdout = io.StringIO()
    with pytest.raises(TypeError):
        server.write_json({"obj": object()})


def test_write_json_peer_gone_oserror_on_flush_returns_false(server):
    """A flush that raises a peer-gone OSError (EPIPE) must not strand
    the lock or crash; it returns False so the dispatcher exits cleanly."""
    import errno

    written = []

    class _FlushPeerGone:
        def write(self, line): written.append(line)
        def flush(self): raise OSError(errno.EPIPE, "broken pipe")

    server._real_stdout = _FlushPeerGone()
    assert server.write_json({"x": 1}) is False
    assert written and json.loads(written[0]) == {"x": 1}


def test_write_json_non_peer_gone_oserror_re_raises(server):
    """Host I/O failures (ENOSPC, EACCES, EIO …) are NOT peer-gone — they
    must re-raise so the crash log records them instead of looking like
    a clean disconnect via the False path."""
    import errno

    class _DiskFull:
        def write(self, _): raise OSError(errno.ENOSPC, "no space left")
        def flush(self): pass

    server._real_stdout = _DiskFull()
    with pytest.raises(OSError, match="no space"):
        server.write_json({"x": 1})


def test_write_json_skips_flush_when_disable_flush_true(monkeypatch):
    """`StdioTransport` skips flush when `_DISABLE_FLUSH` is true.

    Tests the runtime *behaviour* via direct module-attr patch.  The env
    var → module constant wiring is covered by the dedicated env test
    below; reloading server.py here would re-register atexit hooks and
    recreate the worker pool.
    """
    import importlib

    transport_mod = importlib.import_module("tui_gateway.transport")
    monkeypatch.setattr(transport_mod, "_DISABLE_FLUSH", True)

    flushed = {"count": 0}
    written = []

    class _Stream:
        def write(self, line): written.append(line)
        def flush(self): flushed["count"] += 1

    stream = _Stream()
    transport = transport_mod.StdioTransport(lambda: stream, threading.Lock())

    assert transport.write({"x": 1}) is True
    assert flushed["count"] == 0


def test_disable_flush_env_var_actually_wires_to_module_constant(monkeypatch):
    """End-to-end: setting `HERMES_TUI_GATEWAY_NO_FLUSH=1` and importing
    `tui_gateway.transport` fresh actually flips `_DISABLE_FLUSH` true.

    Reloads only the transport module — server.py is untouched so its
    atexit hooks/worker pool stay intact."""
    import importlib

    monkeypatch.setenv("HERMES_TUI_GATEWAY_NO_FLUSH", "1")
    transport_mod = importlib.reload(importlib.import_module("tui_gateway.transport"))

    try:
        assert transport_mod._DISABLE_FLUSH is True
    finally:
        # Restore the env-disabled state so other tests see the default.
        monkeypatch.delenv("HERMES_TUI_GATEWAY_NO_FLUSH", raising=False)
        importlib.reload(transport_mod)


# ── _emit ────────────────────────────────────────────────────────────


def test_emit_with_payload(capture):
    server, buf = capture
    server._emit("test.event", "s1", {"key": "val"})
    msg = json.loads(buf.getvalue())

    assert msg["method"] == "event"
    assert msg["params"]["type"] == "test.event"
    assert msg["params"]["session_id"] == "s1"
    assert msg["params"]["payload"]["key"] == "val"


def test_emit_without_payload(capture):
    server, buf = capture
    server._emit("ping", "s2")

    assert "payload" not in json.loads(buf.getvalue())["params"]


# ── Blocking prompt round-trip ───────────────────────────────────────


def test_block_and_respond(capture):
    server, _ = capture
    result = [None]

    threading.Thread(
        target=lambda: result.__setitem__(0, server._block("test.prompt", "s1", {"q": "?"}, timeout=5)),
    ).start()

    for _ in range(100):
        if server._pending:
            break
        threading.Event().wait(0.01)

    rid = next(iter(server._pending))
    server._answers[rid] = "my_answer"
    # _pending values are (sid, Event) tuples — unpack to set the Event
    _, ev = server._pending[rid]
    ev.set()

    threading.Event().wait(0.1)
    assert result[0] == "my_answer"


@pytest.mark.parametrize("event", ["secret.request", "sudo.request"])
def test_sensitive_prompt_timeout_emits_expiry(capture, event):
    server, buf = capture

    assert server._block(event, "s1", {}, timeout=0) == ""

    messages = [json.loads(line) for line in buf.getvalue().splitlines()]
    request, expiry = [message["params"] for message in messages]
    assert request["type"] == event
    assert expiry["type"] == event.removesuffix(".request") + ".expire"
    assert expiry["session_id"] == "s1"
    assert expiry["payload"]["request_id"] == request["payload"]["request_id"]


@pytest.mark.parametrize(
    ("method", "value_key"),
    [("secret.respond", "value"), ("sudo.respond", "password")],
)
def test_late_sensitive_prompt_response_is_idempotent(server, method, value_key):
    response = server.handle_request(
        {
            "id": "late-response",
            "method": method,
            "params": {"request_id": "expired-request", value_key: ""},
        }
    )

    assert response["result"] == {"status": "expired"}


def test_late_clarify_response_remains_protocol_error(server):
    response = server.handle_request(
        {
            "id": "late-clarify",
            "method": "clarify.respond",
            "params": {"request_id": "expired-request", "answer": ""},
        }
    )

    assert response["error"]["code"] == 4009


def test_clear_pending(server):
    ev = threading.Event()
    # _pending values are (sid, Event) tuples
    server._pending["r1"] = ("sid-x", ev)
    server._clear_pending()

    assert ev.is_set()
    assert server._answers["r1"] == ""


# ── Session lookup ───────────────────────────────────────────────────


def test_sess_missing(server):
    _, err = server._sess({"session_id": "nope"}, "r1")
    assert err["error"]["code"] == 4001


def test_sess_found(server):
    server._sessions["abc"] = {"agent": MagicMock()}
    s, err = server._sess({"session_id": "abc"}, "r1")

    assert s is not None
    assert err is None


# ── session.resume payload ────────────────────────────────────────────


def test_session_resume_returns_hydrated_messages(server, monkeypatch):
    class _DB:
        def get_session(self, _sid):
            return {"id": "20260409_010101_abc123"}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True),
            )

        def get_ancestor_display_prefix(self, _sid):
            return []

        def get_messages_as_conversation(self, _sid, include_ancestors=False, repair_alternation=False):
            return [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "yo", "reasoning": "thoughts"},
                {"role": "tool", "content": "searched"},
                {"role": "assistant", "content": "   "},
                {"role": "assistant", "content": None},
                {"role": "narrator", "content": "skip"},
            ]

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_make_agent", lambda sid, key, session_id=None, session_db=None, **_kwargs: object())
    monkeypatch.setattr(server, "_init_session", lambda sid, key, agent, history, cols=80, **_kwargs: None)
    monkeypatch.setattr(server, "_session_info", lambda _agent, _session=None: {"model": "test/model"})

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            # eager_build: exercise the synchronous build path (this test
            # monkeypatches _make_agent/_init_session/_session_info).
            "params": {"session_id": "20260409_010101_abc123", "cols": 100, "eager_build": True},
        }
    )

    assert "error" not in resp
    assert resp["result"]["message_count"] == 3
    assert resp["result"]["messages"] == [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "yo", "reasoning": "thoughts"},
        {"role": "tool", "name": "tool", "context": ""},
    ]


def test_session_resume_invalid_cols_defaults_to_80(server, monkeypatch):
    """The validated handler width must reach the implementation unchanged."""
    target = "20260723_010101_invalidcols"
    init_cols = []

    class _DB:
        def get_session(self, _sid):
            return {"id": target}

        def get_session_by_title(self, _title):
            return None

        def resolve_resume_session_id(self, sid):
            return sid

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, _sid):
            history = [{"role": "user", "content": "hello"}]
            return history, history

        def get_ancestor_display_prefix(self, _sid):
            return []

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_make_agent", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        server,
        "_init_session",
        lambda _sid, _key, _agent, _history, *, cols=80, **_kwargs: init_cols.append(cols),
    )
    monkeypatch.setattr(server, "_session_info", lambda *_args: {"model": "test/model"})

    resp = server.handle_request(
        {
            "id": "invalid-cols",
            "method": "session.resume",
            "params": {
                "session_id": target,
                "cols": "not-a-number",
                "eager_build": True,
            },
        }
    )

    assert "error" not in resp, resp
    assert init_cols == [80]
    assert resp["result"]["message_count"] == 1


def test_session_resume_defaults_to_deferred_build(server, monkeypatch):
    """A normal cold resume (no ``eager_build``) must return the full display
    transcript immediately and register an upgradable live session WITHOUT
    building the agent on the response path — that eager build is the
    multi-second switch latency. Deferred is the default; ``eager_build: true``
    opts back into the synchronous path."""

    target = "20260409_010101_abc123"

    class _DB:
        def get_session(self, _sid):
            return {
                "id": target,
                "model": "vendor/cool-model",
                "model_config": {"provider": "vendor"},
            }

        def get_session_by_title(self, _title):
            return None

        def resolve_resume_session_id(self, sid):
            return sid

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True),
            )

        def get_ancestor_display_prefix(self, _sid):
            return []

        def get_messages_as_conversation(self, _sid, include_ancestors=False, repair_alternation=False):
            return [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "yo"},
            ]

    builds: list = []

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    # The response path must never call _make_agent; route the deferred timer
    # through a recorder so a 50ms fire can't build (or crash) under the test.
    monkeypatch.setattr(
        server, "_make_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no eager build"))
    )
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: builds.append(sid))
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100},
        }
    )

    assert "error" not in resp
    result = resp["result"]
    assert result["resumed"] == target
    assert result["session_key"] == target
    assert result["message_count"] == 2
    assert result["messages"] == [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "yo"},
    ]
    # Lazy info contract (same shape session.create returns), with the session's
    # persisted model/provider restored rather than the global default.
    assert result["info"]["lazy"] is True
    assert result["info"]["model"] == "vendor/cool-model"
    assert result["info"]["provider"] == "vendor"
    assert result["info"]["desktop_contract"] == server.DESKTOP_BACKEND_CONTRACT

    sid = result["session_id"]
    session = server._sessions[sid]
    # Registered but not built: agent is None and the resume key is carried so a
    # later prompt.submit / _sess() upgrade continues THIS stored conversation.
    assert session["agent"] is None
    assert session["resume_session_id"] == target
    assert not session["agent_ready"].is_set()
    # Not a watch spectator: a normal deferred resume is a real session.
    assert not session.get("lazy")
    # The persisted runtime identity is stashed for the deferred build so it
    # can't drop the provider ("No LLM provider configured").
    assert session["resume_runtime_overrides"]["model_override"]["model"] == "vendor/cool-model"
    assert server._find_live_session_by_key(target) == (sid, session)


def test_enforce_session_cap_evicts_oldest_detached_only(server, monkeypatch):
    """The LRU cap frees the least-recently-active DETACHED sessions when over
    the limit, and never a live-transport / running / mid-build one."""

    monkeypatch.setattr(server, "_load_cfg", lambda: {"max_live_sessions": 2})
    evicted: list[str] = []
    monkeypatch.setattr(
        server, "_close_session_by_id", lambda sid, end_reason=None: evicted.append(sid)
    )

    def _ready() -> threading.Event:
        ev = threading.Event()
        ev.set()
        return ev

    detached = server._detached_ws_transport
    live = object()  # no _closed attr -> live transport, never evictable

    server._sessions.clear()
    server._sessions.update(
        {
            "old_detached": {"transport": detached, "last_active": 100.0, "agent_ready": _ready()},
            "new_detached": {"transport": detached, "last_active": 300.0, "agent_ready": _ready()},
            "running_detached": {
                "transport": detached,
                "last_active": 50.0,
                "running": True,
                "agent_ready": _ready(),
            },
            "focused_live": {"transport": live, "last_active": 200.0, "agent_ready": _ready()},
        }
    )

    server._enforce_session_cap()

    # 4 sessions, cap 2 -> evict 2. Only detached+idle+built are eligible, oldest
    # first; the running one and the live-transport one are exempt.
    assert evicted == ["old_detached", "new_detached"]


def test_enforce_session_cap_disabled_is_noop(server, monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"max_live_sessions": 0})
    evicted: list[str] = []
    monkeypatch.setattr(
        server, "_close_session_by_id", lambda sid, end_reason=None: evicted.append(sid)
    )
    server._sessions.clear()
    server._sessions.update(
        {
            f"s{i}": {"transport": server._detached_ws_transport, "last_active": float(i)}
            for i in range(5)
        }
    )

    server._enforce_session_cap()

    assert evicted == []


def test_session_resume_handles_multimodal_list_content(server, monkeypatch):
    """A user message persisted with list-shaped multimodal content used to
    crash session resume with ``'list' object has no attribute 'strip'``."""

    multimodal_user = {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            },
        ],
    }
    text_only_assistant = {"role": "assistant", "content": "ok"}

    class _DB:
        def get_session(self, _sid):
            return {"id": "20260502_000000_listcontent"}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True),
            )

        def get_ancestor_display_prefix(self, _sid):
            return []

        def get_messages_as_conversation(self, _sid, include_ancestors=False, repair_alternation=False):
            return [multimodal_user, text_only_assistant]

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_make_agent", lambda sid, key, session_id=None, session_db=None, **_kwargs: object())
    monkeypatch.setattr(server, "_init_session", lambda sid, key, agent, history, cols=80, **_kwargs: None)
    monkeypatch.setattr(server, "_session_info", lambda _agent, _session=None: {"model": "test/model"})

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": "20260502_000000_listcontent", "cols": 100, "eager_build": True},
        }
    )

    assert "error" not in resp
    assert resp["result"]["message_count"] == 2
    # The image_url part is preserved as a raw data URL inside the text so
    # the desktop renderer (which extracts embedded images) sees the same
    # content the optimistic local cache returns. Otherwise the inline
    # image flashes during initial cache hydration and then vanishes when
    # the resume payload overwrites it with cleaned text.
    assert resp["result"]["messages"] == [
        {
            "role": "user",
            "text": "describe this\ndata:image/png;base64,AAAA",
        },
        {"role": "assistant", "text": "ok"},
    ]


def test_session_resume_lazy_registers_watch_session_without_agent(server, monkeypatch):
    """``lazy: true`` (subagent watch windows) must register the live session
    — keyed for the child mirror, on this transport — WITHOUT building an
    agent. The eager build is what made opening a subagent window contend
    with the already-running parent turn."""

    target = "20260612_000000_child99"

    class _DB:
        def get_session(self, _sid):
            return {"id": target}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True),
            )

        def get_ancestor_display_prefix(self, _sid):
            return []

        def get_messages_as_conversation(self, _sid, include_ancestors=False, repair_alternation=False):
            return [
                {"role": "user", "content": "delegated goal"},
            ]

    def _boom(*_args, **_kwargs):
        raise AssertionError("lazy resume must not build an agent")

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_make_agent", _boom)

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100, "lazy": True},
        }
    )

    assert "error" not in resp
    result = resp["result"]
    assert result["resumed"] == target
    assert result["session_key"] == target
    assert result["info"]["lazy"] is True
    assert result["info"]["desktop_contract"] == server.DESKTOP_BACKEND_CONTRACT
    assert result["messages"] == [{"role": "user", "text": "delegated goal"}]

    sid = result["session_id"]
    session = server._sessions[sid]
    assert session["agent"] is None
    # The child mirror finds the watch window by stored key.
    assert server._find_live_session_by_key(target) == (sid, session)
    # A later prompt.submit upgrade must continue THIS stored conversation.
    assert session["resume_session_id"] == target
    # No build started: the idle reaper must still be able to evict it, and
    # the live status must not report a never-ending "starting".
    assert not session["agent_ready"].is_set()
    assert server._session_live_status(sid, session) != "starting"
    session["transport"] = server._detached_ws_transport
    far_future = time.time() + 999999
    assert server._session_is_evictable(sid, session, far_future)

    # Resuming again (window refresh) reuses the same live session.
    resp2 = server.handle_request(
        {
            "id": "r2",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100, "lazy": True},
        }
    )
    assert "error" not in resp2
    assert resp2["result"]["session_id"] == sid
    assert len(server._sessions) == 1


def test_session_resume_lazy_reports_running_for_inflight_child(server, monkeypatch):
    """A watch window attaching to a child mid-delegation must learn the run is
    live from the resume response itself — the child can sit silent inside a
    long tool call, so waiting for the next stream event leaves the window
    looking dead."""

    target = "20260612_000000_child42"

    class _DB:
        def get_session(self, _sid):
            return {"id": target}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True),
            )

        def get_ancestor_display_prefix(self, _sid):
            return []

        def get_messages_as_conversation(self, _sid, include_ancestors=False, repair_alternation=False):
            return [{"role": "user", "content": "delegated goal"}]

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(
        server, "_make_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no build"))
    )
    server._active_child_runs[target] = time.time()
    try:
        resp = server.handle_request(
            {
                "id": "r1",
                "method": "session.resume",
                "params": {"session_id": target, "cols": 100, "lazy": True},
            }
        )
    finally:
        server._active_child_runs.pop(target, None)

    assert "error" not in resp
    assert resp["result"]["running"] is True
    assert resp["result"]["status"] == "streaming"


def test_session_resume_lazy_tolerates_missing_row_for_active_child(server, monkeypatch):
    """Race regression: a watch window opens on a freshly-spawned subagent and
    resumes BEFORE the child's first run_conversation() flushes its DB row.

    The child relays ``subagent.start`` (carrying child_session_id, which opens
    the window) before ``_ensure_db_session`` writes the row, so
    ``db.get_session(target)`` is momentarily empty. On slower hosts (WSL2) the
    window's lazy resume consistently lands in this gap. It used to hard-fail
    "session not found"; the frontend then 404'd on its REST messages fallback
    and the watch window spun forever. Since the child is provably live
    (``_child_run_active``), the lazy resume must instead register the live
    session with empty history so the mirror can stream the turn.
    """

    target = "20260616_131212_racey"

    class _DB:
        def get_session(self, _sid):
            # Row not flushed yet — the whole point of the race.
            return None

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True),
            )

        def get_ancestor_display_prefix(self, _sid):
            return []

        def get_messages_as_conversation(self, _sid, include_ancestors=False, repair_alternation=False):
            # No rows for an unwritten session.
            return []

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(
        server, "_make_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no build"))
    )
    # Child is live in the relay registry even though its row isn't written.
    server._active_child_runs[target] = time.time()
    try:
        resp = server.handle_request(
            {
                "id": "r1",
                "method": "session.resume",
                "params": {"session_id": target, "cols": 100, "lazy": True},
            }
        )
    finally:
        server._active_child_runs.pop(target, None)

    # The resume must succeed (no "session not found") and register a live,
    # agent-less watch session the mirror can find by stored key.
    assert "error" not in resp
    result = resp["result"]
    assert result["resumed"] == target
    assert result["session_key"] == target
    assert result["info"]["lazy"] is True
    assert result["messages"] == []
    # Live for the mirror; reported running so the window shows a busy state.
    assert result["running"] is True
    assert result["status"] == "streaming"
    sid = result["session_id"]
    assert server._find_live_session_by_key(target) == (sid, server._sessions[sid])
    assert server._sessions[sid]["agent"] is None


def test_session_resume_missing_row_non_lazy_still_errors(server, monkeypatch):
    """The missing-row tolerance is scoped to lazy resumes of an ACTIVE child.
    A normal (non-lazy) resume of a genuinely unknown id must still fail fast
    with "session not found" rather than silently registering an empty session.
    """

    target = "20260616_000000_ghost"

    class _DB:
        def get_session(self, _sid):
            return None

        def get_session_by_title(self, _title):
            return None

    monkeypatch.setattr(server, "_get_db", lambda: _DB())

    # Non-lazy resume, no active child → hard error.
    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100},
        }
    )
    assert "error" in resp
    assert "session not found" in resp["error"]["message"].lower()

    # Lazy resume but the child is NOT live → still an error (no live mirror to
    # justify an empty session; this would just be a dead, sessionless window).
    resp2 = server.handle_request(
        {
            "id": "r2",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100, "lazy": True},
        }
    )
    assert "error" in resp2
    assert "session not found" in resp2["error"]["message"].lower()


def test_session_resume_reuses_existing_live_session(server, monkeypatch):
    """Repeated resume must not allocate duplicate live agents."""

    target = "20260409_010101_abc123"
    created_sids: list[str] = []
    closed_sids: list[str] = []
    first_agent_started = threading.Event()
    agent_can_finish = threading.Event()

    class _DB:
        def get_session(self, _sid):
            return {"id": target}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True),
            )

        def get_ancestor_display_prefix(self, _sid):
            return []

        def get_messages_as_conversation(self, _sid, include_ancestors=False, repair_alternation=False):
            return [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "yo"},
            ]

    class _Worker:
        def close(self):
            pass

    class _Agent:
        def __init__(self, sid, session_id):
            self.sid = sid
            self.model = "test/model"
            self.session_id = session_id

        def close(self):
            closed_sids.append(self.sid)

    def make_agent(sid, key, session_id=None, session_db=None, **_kwargs):
        created_sids.append(sid)
        first_agent_started.set()
        assert agent_can_finish.wait(timeout=1)
        return _Agent(sid, session_id or key)

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_make_agent", make_agent)
    monkeypatch.setattr(server, "_SlashWorker", lambda _key, _model: _Worker())
    monkeypatch.setattr(
        server,
        "_start_notification_poller",
        lambda _sid, _session: threading.Event(),
    )
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda _sid: None)
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent, _session=None: {"model": "test/model"},
    )

    fake_approval = types.SimpleNamespace(
        load_permanent_allowlist=lambda: None,
        register_gateway_notify=lambda *_args, **_kwargs: None,
    )

    with patch.dict(sys.modules, {"tools.approval": fake_approval}):
        first_holder = {}

        def resume_first():
            first_holder["resp"] = server.handle_request(
                {
                    "id": "first",
                    "method": "session.resume",
                    # eager_build: this test drives the synchronous build race +
                    # double-checked locking that only the eager path exercises.
                    "params": {"session_id": target, "cols": 100, "eager_build": True},
                }
            )

        first_thread = threading.Thread(target=resume_first)
        first_thread.start()
        assert first_agent_started.wait(timeout=1)

        second_holder = {}

        def resume_second():
            second_holder["resp"] = server.handle_request(
                {
                    "id": "second",
                    "method": "session.resume",
                    "params": {"session_id": target, "cols": 120, "eager_build": True},
                }
            )

        second_thread = threading.Thread(target=resume_second)
        second_thread.start()
        agent_can_finish.set()

        first_thread.join(timeout=1)
        second_thread.join(timeout=1)
        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
        first = first_holder["resp"]
        second = second_holder["resp"]

    assert "error" not in first
    assert "error" not in second
    # Both resumes resolve to the SAME single live session — the core invariant.
    assert second["result"]["session_id"] == first["result"]["session_id"]
    assert len(server._sessions) == 1
    assert [s.get("session_key") for s in server._sessions.values()].count(target) == 1
    winner = first["result"]["session_id"]
    # The agent build happens outside the resume lock, so a racing resume may
    # build a redundant agent; double-checked locking keeps only one live
    # session and closes any loser's agent (no worker/poller is wired for it).
    assert winner in created_sids
    survivors = [sid for sid in created_sids if sid not in closed_sids]
    assert survivors == [winner]
    assert all(sid == winner for sid in server._sessions)


def test_session_resume_reuses_live_agent_after_compression_rotation(server, monkeypatch):
    """Resume must match the live agent's current session_id, not stale session_key."""

    target = "20260409_020202_child"
    stale_parent = "20260409_010101_parent"
    sid = "live-rotated"
    server._sessions[sid] = {
        "agent": types.SimpleNamespace(model="test/model", session_id=target),
        "created_at": 123.0,
        "display_history_prefix": [],
        "history": [{"role": "assistant", "content": "live child"}],
        "history_lock": threading.RLock(),
        "last_active": 123.0,
        "running": False,
        "session_key": stale_parent,
        "transport": server._stdio_transport,
    }

    class _DB:
        def get_session(self, _sid):
            return {"id": target}

        def get_session_by_title(self, _title):
            return None

        def resolve_resume_session_id(self, _target):
            return target

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent, _session=None: {"model": "test/model"},
    )

    result = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100},
        }
    )

    assert "error" not in result
    assert result["result"]["session_id"] == sid
    assert result["result"]["session_key"] == target
    assert len(server._sessions) == 1


def test_sync_session_key_after_compress_reanchors_active_session_lease(
    server, monkeypatch, tmp_path
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli.active_sessions import (
        active_session_registry_snapshot,
        try_acquire_active_session,
    )

    lease, message = try_acquire_active_session(
        session_id="session-old",
        surface="tui",
        config={"max_concurrent_sessions": 1},
        metadata={"live_session_id": "ui-1"},
    )
    assert message is None
    assert lease is not None

    session = {
        "active_session_lease": lease,
        "agent": types.SimpleNamespace(session_id="session-new"),
        "session_key": "session-old",
    }
    fake_approval = types.SimpleNamespace(
        disable_session_yolo=lambda *_args, **_kwargs: None,
        enable_session_yolo=lambda *_args, **_kwargs: None,
        is_session_yolo_enabled=lambda *_args, **_kwargs: False,
        register_gateway_notify=lambda *_args, **_kwargs: None,
        unregister_gateway_notify=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *_args, **_kwargs: None)

    with patch.dict(sys.modules, {"tools.approval": fake_approval}):
        server._sync_session_key_after_compress("ui-1", session)

    snapshot = active_session_registry_snapshot()
    assert session["session_key"] == "session-new"
    assert lease.session_id == "session-new"
    assert [entry["session_id"] for entry in snapshot] == ["session-new"]
    lease.release()


def test_session_resume_live_payload_uses_current_history_with_ancestors(server, monkeypatch):
    """Live resume should not reuse a stale ancestor-inclusive snapshot."""

    target = "20260409_010101_child"
    ancestor_history = [{"role": "user", "content": "ancestor"}]
    current_history = [
        {"role": "user", "content": "current"},
        {"role": "assistant", "content": "current reply"},
    ]

    class _DB:
        def get_session(self, _sid):
            return {"id": target}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True),
            )

        def get_ancestor_display_prefix(self, _sid):
            return list(ancestor_history)

        def get_messages_as_conversation(self, _sid, include_ancestors=False, repair_alternation=False):
            if include_ancestors:
                return ancestor_history + current_history
            return list(current_history)

    class _Worker:
        def close(self):
            pass

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda _sid, key, session_id=None, session_db=None, **_kwargs: types.SimpleNamespace(
            model="test/model", session_id=session_id or key
        ),
    )
    monkeypatch.setattr(server, "_SlashWorker", lambda _key, _model: _Worker())
    monkeypatch.setattr(
        server,
        "_start_notification_poller",
        lambda _sid, _session: threading.Event(),
    )
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda _sid: None)
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent, _session=None: {"model": "test/model"},
    )

    fake_approval = types.SimpleNamespace(
        load_permanent_allowlist=lambda: None,
        register_gateway_notify=lambda *_args, **_kwargs: None,
    )

    with patch.dict(sys.modules, {"tools.approval": fake_approval}):
        first = server.handle_request(
            {
                "id": "first",
                "method": "session.resume",
                "params": {"session_id": target, "cols": 100},
            }
        )

        assert "error" not in first
        sid = first["result"]["session_id"]
        assert first["result"]["messages"] == [
            {"role": "user", "text": "ancestor"},
            {"role": "user", "text": "current"},
            {"role": "assistant", "text": "current reply"},
        ]

        with server._sessions[sid]["history_lock"]:
            server._sessions[sid]["history"] = current_history + [
                {"role": "user", "content": "new live turn"},
                {"role": "assistant", "content": "new live reply"},
            ]

        second = server.handle_request(
            {
                "id": "second",
                "method": "session.resume",
                "params": {"session_id": target, "cols": 120},
            }
        )

    assert "error" not in second
    assert second["result"]["session_id"] == sid
    assert second["result"]["messages"] == [
        {"role": "user", "text": "ancestor"},
        {"role": "user", "text": "current"},
        {"role": "assistant", "text": "current reply"},
        {"role": "user", "text": "new live turn"},
        {"role": "assistant", "text": "new live reply"},
    ]


def test_session_activate_rebinds_orphaned_ws_session_to_current_transport(server, monkeypatch):
    """Reconnect + activate must reattach a parked live session before orphan reap."""

    class _Transport:
        def write(self, _obj):
            return True

    sid = "runtime01"
    old_transport = server._stdio_transport
    new_transport = _Transport()
    server._sessions[sid] = {
        "agent": types.SimpleNamespace(model="test/model"),
        "created_at": 123.0,
        "history": [],
        "history_lock": threading.RLock(),
        "last_active": 123.0,
        "running": False,
        "session_key": "20260409_010101_abc123",
        "transport": old_transport,
    }
    monkeypatch.setattr(server, "current_transport", lambda: new_transport)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent, _session=None: {"model": "test/model"},
    )

    resp = server.handle_request(
        {"id": "activate", "method": "session.activate", "params": {"session_id": sid}}
    )

    assert "error" not in resp
    assert resp["result"]["session_id"] == sid
    assert server._sessions[sid]["transport"] is new_transport
    assert not server._ws_session_is_orphaned(server._sessions[sid])


def test_session_branch_persists_branched_from_marker(server, monkeypatch):
    """TUI /branch must persist a _branched_from marker so the branch stays
    visible in /resume and /sessions.

    Regression for issue #20856: the TUI branch leaves the parent live (it
    never ends it with end_reason='branched'), so list_sessions_rich's legacy
    heuristic never surfaces it — the stable model_config marker is the only
    thing that keeps a TUI branch visible.
    """
    create_calls = []

    class _DB:
        def get_session_title(self, _key):
            return "parent-title"

        def get_next_title_in_lineage(self, base):
            return f"{base} 2"

        def create_session(self, new_key, **kwargs):
            create_calls.append((new_key, kwargs))
            return new_key

        def append_message(self, **_kwargs):
            return None

        def set_session_title(self, _key, _title):
            return None

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_resolve_model", lambda: "test/model")
    monkeypatch.setattr(server, "_new_session_key", lambda: "20260101_000001_child0")
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda _sid, key, session_id=None, session_db=None, **_kwargs: types.SimpleNamespace(
            model="test/model", session_id=session_id or key
        ),
    )
    monkeypatch.setattr(server, "_init_session", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_session_cwd", lambda _s: "/tmp/branch-cwd")

    parent_sid = "parent01"
    parent_key = "20260101_000000_parent"
    server._sessions[parent_sid] = {
        "session_key": parent_key,
        "history": [{"role": "user", "content": "hello"}],
        "history_lock": threading.Lock(),
        "cols": 80,
    }

    resp = server.handle_request(
        {"id": "b1", "method": "session.branch", "params": {"session_id": parent_sid}}
    )

    assert "error" not in resp, resp
    assert len(create_calls) == 1
    new_key, kwargs = create_calls[0]
    assert new_key == "20260101_000001_child0"
    assert kwargs["parent_session_id"] == parent_key
    # The marker — without it the branch is invisible in /resume and /sessions.
    assert kwargs["model_config"] == {"_branched_from": parent_key}


def test_session_branch_forwards_original_timestamps(server, monkeypatch):
    """TUI /branch must copy the parent's messages WITH their original
    timestamps — append_message otherwise stamps time.time() at INSERT and
    the branch's whole history silently appears authored "now" (#28841).
    """
    append_calls = []

    class _DB:
        def get_session_title(self, _key):
            return "parent-title"

        def get_next_title_in_lineage(self, base):
            return f"{base} 2"

        def create_session(self, new_key, **kwargs):
            return new_key

        def append_message(self, **kwargs):
            append_calls.append(kwargs)
            return None

        def set_session_title(self, _key, _title):
            return None

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_resolve_model", lambda: "test/model")
    monkeypatch.setattr(server, "_new_session_key", lambda: "20260101_000001_child0")
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda _sid, key, session_id=None, session_db=None, **_kwargs: types.SimpleNamespace(
            model="test/model", session_id=session_id or key
        ),
    )
    monkeypatch.setattr(server, "_init_session", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_session_cwd", lambda _s: "/tmp/branch-cwd")

    original_ts = [1_700_000_000.0, 1_700_000_020.0]
    parent_sid = "parent02"
    server._sessions[parent_sid] = {
        "session_key": "20260101_000000_parent",
        "history": [
            {"role": "user", "content": "hello", "timestamp": original_ts[0]},
            {"role": "assistant", "content": "hi!", "timestamp": original_ts[1]},
        ],
        "history_lock": threading.Lock(),
        "cols": 80,
    }

    resp = server.handle_request(
        {"id": "b2", "method": "session.branch", "params": {"session_id": parent_sid}}
    )

    assert "error" not in resp, resp
    assert len(append_calls) == 2
    assert [c.get("timestamp") for c in append_calls] == original_ts


def test_session_branch_uses_parent_profile_context_for_db_and_agent(server, monkeypatch, tmp_path):
    """A cross-profile branch must publish its row and agent through one profile DB."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    events = []
    dbs = []
    active_override = False

    class _DB:
        def get_session_title(self, _key):
            assert active_override
            return "parent-title"

        def get_next_title_in_lineage(self, base):
            assert active_override
            return f"{base} 2"

        def create_session(self, new_key, **kwargs):
            assert active_override
            events.append(("create", new_key, kwargs))

        def append_message(self, **kwargs):
            assert active_override
            events.append(("append", kwargs))

        def set_session_title(self, key, title):
            assert active_override
            events.append(("title", key, title))

    class _SessionDB(_DB):
        def __init__(self, *, db_path):
            assert active_override
            self.db_path = Path(db_path)
            dbs.append(self)

    def _set_override(home):
        nonlocal active_override
        assert Path(home) == profile_home
        active_override = True
        events.append(("set", str(home)))
        return "profile-token"

    def _reset_override(token):
        nonlocal active_override
        assert token == "profile-token"
        events.append(("reset", token))
        active_override = False

    monkeypatch.setattr(server, "_get_db", lambda: pytest.fail("launch DB must not be opened"))
    monkeypatch.setattr(server, "set_hermes_home_override", _set_override)
    monkeypatch.setattr(server, "reset_hermes_home_override", _reset_override)
    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=_SessionDB))
    monkeypatch.setattr(
        server,
        "_resolve_model",
        lambda: (pytest.fail("model must resolve in profile context") if not active_override else "profile/model"),
    )
    monkeypatch.setattr(server, "_new_session_key", lambda: "20260101_000001_profile")
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(server, "_set_session_context", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *_a, **_k: None)
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda _sid, key, session_id=None, session_db=None, **_kwargs: (
            pytest.fail("agent must build in profile context")
            if not active_override
            or session_db is not dbs[0]
            or _kwargs.get("owns_session_db") is not True
            else types.SimpleNamespace(model="profile/model", session_id=session_id or key)
        ),
    )
    init_calls = []
    monkeypatch.setattr(server, "_init_session", lambda *args, **kwargs: init_calls.append((args, kwargs)))

    parent_sid = "parent-profile"
    parent_key = "20260101_000000_parent"
    workspace = tmp_path / "workspace"
    server._sessions[parent_sid] = {
        "session_key": parent_key,
        "history": [{"role": "user", "content": "hello"}],
        "history_lock": threading.Lock(),
        "cols": 100,
        "cwd": str(workspace),
        "profile_home": str(profile_home),
    }
    resp = server.handle_request(
        {"id": "profile-branch", "method": "session.branch", "params": {"session_id": parent_sid}}
    )

    assert "error" not in resp, resp
    assert dbs and dbs[0].db_path == profile_home / "state.db"
    assert [event[0] for event in events] == ["set", "create", "append", "title", "reset"]
    create = next(event for event in events if event[0] == "create")
    assert create[2]["model"] == "profile/model"
    assert create[2]["cwd"] == str(workspace)
    assert create[2]["model_config"] == {"_branched_from": parent_key}
    assert init_calls[0][1]["session_db"] is dbs[0]
    assert init_calls[0][1]["profile_home"] == str(profile_home)
    assert init_calls[0][1]["cwd"] == str(workspace)
    assert active_override is False



def test_reset_profile_builds_owned_agent_before_swapping_and_closes_old(
    server, monkeypatch, tmp_path
):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    active = False
    dbs = []
    closed = []
    context_calls = []

    class _DB:
        def __init__(self, *, db_path):
            assert active
            self.db_path = Path(db_path)
            self.closed = False
            dbs.append(self)

        def close(self):
            self.closed = True

    old_agent = types.SimpleNamespace(
        model="old/model", session_id="reset-key", close=lambda: closed.append("old")
    )
    new_agent = types.SimpleNamespace(model="new/model", session_id="reset-key")

    def _set_home(home):
        nonlocal active
        assert Path(home) == profile_home
        active = True
        return "reset-token"

    def _reset_home(token):
        nonlocal active
        assert token == "reset-token"
        active = False

    def _set_context(*args, **kwargs):
        assert active
        context_calls.append((args, kwargs))
        return ["ctx"]

    def _make_agent(*_args, **kwargs):
        assert active
        assert kwargs["owns_session_db"] is True
        assert kwargs["session_db"] is dbs[0]
        return new_agent

    monkeypatch.setattr(server, "set_hermes_home_override", _set_home)
    monkeypatch.setattr(server, "reset_hermes_home_override", _reset_home)
    monkeypatch.setattr(server, "_set_session_context", _set_context)
    monkeypatch.setattr(server, "_clear_session_context", lambda _tokens: None)
    monkeypatch.setattr(server, "_make_agent", _make_agent)
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *_args: None)
    monkeypatch.setattr(server, "_start_notification_poller", lambda *_args, **_kwargs: threading.Event())
    monkeypatch.setattr(server, "_session_info", lambda *_args: {"model": "new/model"})
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "all")
    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=_DB))

    sid = "reset-profile"
    session = {
        "agent": old_agent,
        "session_key": "reset-key",
        "profile_home": str(profile_home),
        "cwd": str(workspace),
        "source": "telegram",
        "history": [{"role": "user", "content": "old"}],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "_notif_stop": threading.Event(),
        "slash_worker": None,
        "model_override": {"model": "old/model"},
    }
    server._sessions[sid] = session
    try:
        info = server._reset_session_agent(sid, session)
    finally:
        server._sessions.pop(sid, None)

    assert info["model"] == "new/model"
    assert session["agent"] is new_agent
    assert session["session_db"] is dbs[0]
    assert context_calls == [
        (("reset-key",), {"cwd": str(workspace), "source": "telegram"})
    ]
    assert closed == ["old"]
    assert old_agent._end_session_on_close is False
    assert active is False


def test_deferred_agent_build_discards_stale_constructed_agent(server, monkeypatch, tmp_path):
    """A deferred profile build must not publish into a reaped session slot."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    sid = "deferred-stale"
    ready = threading.Event()
    replacement = {"replacement": True}
    dbs = []
    agents = []
    worker_calls = []
    poller_calls = []
    notifier_calls = []
    approval_calls = []

    class _Approval:
        register_gateway_notify = staticmethod(
            lambda *_args, **_kwargs: approval_calls.append("register")
        )
        load_permanent_allowlist = staticmethod(
            lambda: approval_calls.append("load")
        )

    monkeypatch.setitem(sys.modules, "tools.approval", _Approval)

    class _DB:
        def __init__(self, *, db_path):
            self.db_path = Path(db_path)
            self.close_calls = 0
            dbs.append(self)

        def close(self):
            self.close_calls += 1

    class _Agent:
        def __init__(self, session_db):
            self.model = "profile/model"
            self.session_db = session_db
            self._session_db = session_db
            self._owns_session_db = True
            self.close_calls = 0
            agents.append(self)

        def close(self):
            self.close_calls += 1
            self.session_db.close()

    original = {
        "agent": None,
        "agent_ready": ready,
        "session_key": "20260723_000001_deferred",
        "profile_home": str(profile_home),
        "cwd": str(tmp_path / "workspace"),
        "source": "telegram",
    }

    def _reset_home(token):
        assert token == "home-token"

    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=_DB))
    monkeypatch.setattr(server, "set_hermes_home_override", lambda _home: "home-token")
    monkeypatch.setattr(server, "reset_hermes_home_override", _reset_home)
    monkeypatch.setattr(server, "_set_session_context", lambda *_args, **_kwargs: ["context"])
    monkeypatch.setattr(server, "_clear_session_context", lambda _tokens: None)

    def _make_agent(_sid, _key, *, session_db, **_kwargs):
        agent = _Agent(session_db)
        with server._sessions_lock:
            assert server._sessions[sid] is original
            server._sessions[sid] = replacement
        return agent

    monkeypatch.setattr(server, "_make_agent", _make_agent)
    monkeypatch.setattr(server, "_SlashWorker", lambda *_args, **_kwargs: worker_calls.append(True))
    monkeypatch.setattr(
        server,
        "_start_notification_poller",
        lambda *_args, **_kwargs: poller_calls.append(True),
    )
    monkeypatch.setattr(server, "_wire_callbacks", lambda *_args: notifier_calls.append("callbacks"))
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args: notifier_calls.append("boundary"))
    monkeypatch.setattr(server, "_emit", lambda *_args: notifier_calls.append("emit"))

    server._sessions[sid] = original
    server._start_agent_build(sid, original)

    assert ready.wait(timeout=1.0)
    assert server._sessions[sid] is replacement
    assert original["agent"] is None
    assert original.get("session_db") is None
    assert len(agents) == 1
    assert agents[0].close_calls == 1
    assert len(dbs) == 1
    assert dbs[0].close_calls == 1
    assert worker_calls == []
    assert poller_calls == []
    assert notifier_calls == []
    assert approval_calls == []


def test_reset_discards_replacement_when_session_reaped_during_build(
    server, monkeypatch, tmp_path
):
    """Reset must abandon all candidate resources if its registry slot is replaced."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    sid = "reset-reaped"
    replacement = {"replacement": True}
    dbs = []
    agents = []
    worker_calls = []
    poller_calls = []
    config_calls = []
    lock_probe_results = []

    class _DB:
        def __init__(self, *, db_path):
            self.db_path = Path(db_path)
            self.close_calls = 0
            dbs.append(self)

        def close(self):
            self.close_calls += 1

    def _assert_lock_is_free_during_close():
        acquired = []

        def _probe():
            got_lock = server._sessions_lock.acquire(timeout=0.5)
            acquired.append(got_lock)
            if got_lock:
                server._sessions_lock.release()

        thread = threading.Thread(target=_probe)
        thread.start()
        thread.join(timeout=1.0)
        lock_probe_results.append(acquired == [True] and not thread.is_alive())

    class _Agent:
        def __init__(self, session_db):
            self.model = "new/model"
            self.session_db = session_db
            self._session_db = session_db
            self._owns_session_db = True
            self.close_calls = 0
            agents.append(self)

        def close(self):
            self.close_calls += 1
            _assert_lock_is_free_during_close()
            self.session_db.close()

    class _Worker:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            _assert_lock_is_free_during_close()

    old_agent = types.SimpleNamespace(model="old/model", session_id="reset-key")
    original = {
        "agent": old_agent,
        "session_key": "reset-key",
        "profile_home": str(profile_home),
        "cwd": str(tmp_path / "workspace"),
        "source": "telegram",
        "history": [{"role": "user", "content": "old"}],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "_notif_stop": threading.Event(),
        "slash_worker": None,
    }

    def _reset_home(token):
        assert token == "home-token"

    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=_DB))
    monkeypatch.setattr(server, "set_hermes_home_override", lambda _home: "home-token")
    monkeypatch.setattr(server, "reset_hermes_home_override", _reset_home)
    monkeypatch.setattr(server, "_set_session_context", lambda *_args, **_kwargs: ["context"])
    monkeypatch.setattr(server, "_clear_session_context", lambda _tokens: None)

    def _make_agent(_sid, _key, *, session_db, **_kwargs):
        agent = _Agent(session_db)
        with server._sessions_lock:
            server._sessions.pop(sid)
            server._sessions[sid] = replacement
        return agent

    monkeypatch.setattr(server, "_make_agent", _make_agent)
    monkeypatch.setattr(
        server,
        "_restart_slash_worker",
        lambda *_args, **_kwargs: worker_calls.append(_Worker()) or worker_calls[-1],
    )
    monkeypatch.setattr(
        server,
        "_start_notification_poller",
        lambda *_args, **_kwargs: poller_calls.append(threading.Event()) or poller_calls[-1],
    )
    monkeypatch.setattr(
        server,
        "_config_model_target",
        lambda: config_calls.append(True) or "new/model",
    )
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "all")
    monkeypatch.setattr(server, "_session_info", lambda *_args: {"model": "new/model"})

    server._sessions[sid] = original
    with pytest.raises(RuntimeError, match="closed or replaced during reset"):
        server._reset_session_agent(sid, original)

    assert server._sessions[sid] is replacement
    assert replacement == {"replacement": True}
    assert original["agent"] is old_agent
    assert original["history"] == [{"role": "user", "content": "old"}]
    assert not hasattr(old_agent, "_end_session_on_close")
    assert len(agents) == 1
    assert agents[0].close_calls == 1
    assert len(dbs) == 1
    assert dbs[0].close_calls == 1
    assert worker_calls == []
    assert poller_calls == []
    assert config_calls == []
    assert lock_probe_results == [True]


def test_finalize_ends_profile_agent_database_without_launch_lookup(server, monkeypatch):
    calls = []

    class _DB:
        def get_session(self, session_id):
            calls.append(("get", session_id))
            return {"source": "tui"}

        def end_session(self, session_id, reason):
            calls.append(("end", session_id, reason))

    agent = types.SimpleNamespace(session_id="profile-session", _session_db=_DB())
    monkeypatch.setattr(server, "_get_db", lambda: pytest.fail("launch DB must not be used"))
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_release_active_session_slot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_session_source", lambda _session: "tui")

    server._finalize_session(
        {"agent": agent, "session_key": "profile-session", "history": []},
        end_reason="reset",
    )

    assert calls == [("get", "profile-session"), ("end", "profile-session", "reset")]


def test_persist_branch_seed_forwards_original_timestamps(server, monkeypatch):
    """First-turn branch seed persist must carry each copied message's
    original timestamp through to append_message (#28841)."""
    import contextlib

    append_calls = []

    class _DB:
        def append_message(self, **kwargs):
            append_calls.append(kwargs)
            return None

    @contextlib.contextmanager
    def _fake_session_db(_session):
        yield _DB()

    monkeypatch.setattr(server, "_session_db", _fake_session_db)

    original_ts = [100.0, 200.0]
    session = {
        "session_key": "20260101_000002_seed00",
        "parent_session_id": "20260101_000000_parent",
        "history": [
            {"role": "user", "content": "a", "timestamp": original_ts[0]},
            {"role": "assistant", "content": "b", "timestamp": original_ts[1]},
        ],
        "history_lock": threading.Lock(),
    }

    server._persist_branch_seed(session)

    assert session.get("_branch_seed_persisted") is True
    assert [c.get("timestamp") for c in append_calls] == original_ts


def test_make_agent_accepts_list_system_prompt(server, monkeypatch):
    captured = {}

    class _Agent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.model = kwargs.get("model", "")

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=_Agent))
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        types.SimpleNamespace(
            resolve_runtime_provider=lambda **_kwargs: {
                "provider": "test",
                "base_url": None,
                "api_key": None,
                "api_mode": None,
            }
        ),
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": {"system_prompt": ["one", "two"]}})
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("test/model", "test"))
    monkeypatch.setattr(server, "_get_db", lambda: None)

    server._make_agent("sid", "session-key", session_id="session-key")

    assert captured["ephemeral_system_prompt"] == "one\ntwo"


# ── Config I/O ───────────────────────────────────────────────────────


def test_config_load_missing(server, tmp_path):
    server._hermes_home = tmp_path
    assert server._load_cfg() == {}


def test_config_roundtrip(server, tmp_path):
    server._hermes_home = tmp_path
    server._save_cfg({"model": "test/model"})
    assert server._load_cfg()["model"] == "test/model"


# ── _cli_exec_blocked ────────────────────────────────────────────────


@pytest.mark.parametrize("argv", [
    [],
    ["setup"],
    ["gateway"],
    ["sessions", "browse"],
    ["config", "edit"],
])
def test_cli_exec_blocked(server, argv):
    assert server._cli_exec_blocked(argv) is not None


@pytest.mark.parametrize("argv", [
    ["version"],
    ["sessions", "list"],
])
def test_cli_exec_allowed(server, argv):
    assert server._cli_exec_blocked(argv) is None


# ── slash.exec skill command interception ────────────────────────────


def test_slash_exec_rejects_skill_commands(server):
    """slash.exec must reject skill commands so the TUI falls through to command.dispatch."""
    # Register a mock session
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid, "agent": None}

    # Mock scan_skill_commands to return a known skill
    fake_skills = {"/hermes-agent-dev": {"name": "hermes-agent-dev", "description": "Dev workflow"}}

    with patch("agent.skill_commands.get_skill_commands", return_value=fake_skills):
        resp = server.handle_request({
            "id": "r1",
            "method": "slash.exec",
            "params": {"command": "hermes-agent-dev", "session_id": sid},
        })

    # Should return an error so the TUI's .catch() fires command.dispatch
    assert "error" in resp
    assert resp["error"]["code"] == 4018
    assert "skill command" in resp["error"]["message"]


def test_slash_exec_routes_custom_skill_bundle_away_from_worker(server):
    """slash.exec expands any custom bundle through command.dispatch."""
    sid = "test-session"

    class Worker:
        def __init__(self):
            self.calls = []

        def run(self, cmd):
            self.calls.append(cmd)
            return f"worker:{cmd}"

    worker = Worker()
    server._sessions[sid] = {
        "session_key": sid,
        "agent": None,
        "slash_worker": worker,
    }
    fake_bundles = {
        "/analysis-pack": {
            "name": "analysis-pack",
            "skills": ["source-check", "claim-audit"],
        }
    }
    fake_msg = (
        '[IMPORTANT: The user has invoked the "analysis-pack" skill bundle.]\n\n'
        "User instruction: compare vector databases"
    )

    with patch("agent.skill_bundles.get_skill_bundles", return_value=fake_bundles), \
         patch(
             "agent.skill_bundles.build_bundle_invocation_message",
             return_value=(fake_msg, ["source-check", "claim-audit"], []),
         ):
        resp = server.handle_request({
            "id": "r-bundle-slash",
            "method": "slash.exec",
            "params": {
                "command": "analysis-pack compare vector databases",
                "session_id": sid,
            },
        })

    assert "error" not in resp
    assert resp["result"] == {
        "type": "send",
        "message": fake_msg,
        "notice": "⚡ Loading bundle: analysis-pack (2 skills)",
    }
    assert worker.calls == []


def test_slash_exec_handles_plugin_commands_in_live_gateway(server):
    """Plugin slash commands return normal slash.exec output without using the worker."""
    sid = "test-session"

    class Worker:
        def __init__(self):
            self.calls = []

        def run(self, cmd):
            self.calls.append(cmd)
            return f"worker:{cmd}"

    worker = Worker()
    server._sessions[sid] = {"session_key": sid, "agent": None, "slash_worker": worker}

    with patch(
        "hermes_cli.plugins.get_plugin_command_handler",
        lambda name: (lambda arg: f"plugin:{arg}") if name == "plugin-cmd" else None,
    ):
        resp = server.handle_request({
            "id": "r-plugin-slash",
            "method": "slash.exec",
            "params": {"command": "plugin-cmd hello", "session_id": sid},
        })

    assert "error" not in resp
    assert resp["result"] == {"output": "plugin:hello"}
    assert worker.calls == []


def test_slash_exec_plugin_lookup_failure_falls_back_to_worker(server):
    """Plugin discovery failures must not break ordinary slash-worker commands."""
    sid = "test-session"

    class Worker:
        def __init__(self):
            self.calls = []

        def run(self, cmd):
            self.calls.append(cmd)
            return f"worker:{cmd}"

    worker = Worker()
    server._sessions[sid] = {"session_key": sid, "agent": None, "slash_worker": worker}

    with patch(
        "hermes_cli.plugins.get_plugin_command_handler",
        side_effect=RuntimeError("discovery boom"),
    ):
        resp = server.handle_request({
            "id": "r-plugin-lookup-failure",
            "method": "slash.exec",
            "params": {"command": "help", "session_id": sid},
        })

    assert "error" not in resp
    assert resp["result"] == {"output": "worker:help"}
    assert worker.calls == ["help"]


def test_slash_exec_plugin_handler_error_returns_output(server):
    """Plugin handler failures return slash output so the TUI does not redispatch."""
    sid = "test-session"

    class Worker:
        def __init__(self):
            self.calls = []

        def run(self, cmd):
            self.calls.append(cmd)
            return f"worker:{cmd}"

    def handler(arg):
        raise RuntimeError(f"handler boom: {arg}")

    worker = Worker()
    server._sessions[sid] = {"session_key": sid, "agent": None, "slash_worker": worker}

    with patch(
        "hermes_cli.plugins.get_plugin_command_handler",
        lambda name: handler if name == "plugin-cmd" else None,
    ):
        resp = server.handle_request({
            "id": "r-plugin-handler-error",
            "method": "slash.exec",
            "params": {"command": "plugin-cmd hello", "session_id": sid},
        })

    assert "error" not in resp
    assert resp["result"] == {"output": "Plugin command error: handler boom: hello"}
    assert worker.calls == []


@pytest.mark.parametrize("cmd", ["retry", "queue hello", "q hello", "steer fix the test", "plan", "learn create a skill from https://example.com/docs"])
def test_slash_exec_routes_pending_input_commands_to_dispatch(server, cmd):
    """slash.exec must route _pending_input commands to command.dispatch
    internally instead of returning the old 4018 "use command.dispatch"
    fallback error (#48848). Some TUI clients failed that client-side
    fallback, dropping the input and surfacing "empty command".

    The contract is that slash.exec produces exactly the response
    command.dispatch would for the same command — no fragile retry hop.
    """
    base, _, arg = cmd.partition(" ")

    def fresh_session():
        return {"session_key": "test-session", "agent": None}

    sid = "test-session"

    # Response from the (new) internal routing in slash.exec.
    server._sessions[sid] = fresh_session()
    routed = server.handle_request({
        "id": "r1",
        "method": "slash.exec",
        "params": {"command": cmd, "session_id": sid},
    })

    # Response from calling command.dispatch directly with the parsed parts.
    server._sessions[sid] = fresh_session()
    direct = server.handle_request({
        "id": "r1",
        "method": "command.dispatch",
        "params": {"name": base, "arg": arg, "session_id": sid},
    })

    # slash.exec must no longer emit the old client-fallback rejection.
    if "error" in routed:
        assert "pending-input command" not in routed["error"]["message"]

    # Internal routing must yield the same payload as command.dispatch.
    assert routed.get("result") == direct.get("result")
    assert routed.get("error") == direct.get("error")


def test_command_dispatch_queue_sends_message(server):
    """command.dispatch /queue returns {type: 'send', message: ...} for the TUI."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}

    resp = server.handle_request({
        "id": "r1",
        "method": "command.dispatch",
        "params": {"name": "queue", "arg": "tell me about quantum computing", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "tell me about quantum computing"


def test_command_dispatch_builtin_queue_wins_over_colliding_bundle(server):
    """A custom /queue bundle must not shadow the built-in /queue command."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}
    fake_bundles = {
        "/queue": {
            "name": "queue",
            "skills": ["source-check", "claim-audit"],
        }
    }

    with patch("agent.skill_bundles.get_skill_bundles", return_value=fake_bundles), \
         patch("agent.skill_bundles.build_bundle_invocation_message") as build_bundle:
        resp = server.handle_request({
            "id": "r-queue-collision",
            "method": "command.dispatch",
            "params": {
                "name": "queue",
                "arg": "tell me about quantum computing",
                "session_id": sid,
            },
        })

    assert "error" not in resp
    assert resp["result"] == {
        "type": "send",
        "message": "tell me about quantum computing",
    }
    build_bundle.assert_not_called()


def test_command_dispatch_queue_requires_arg(server):
    """command.dispatch /queue without an argument returns an error."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}

    resp = server.handle_request({
        "id": "r2",
        "method": "command.dispatch",
        "params": {"name": "queue", "arg": "", "session_id": sid},
    })

    assert "error" in resp
    assert resp["error"]["code"] == 4004


def test_command_dispatch_learn_sends_built_prompt(server):
    """command.dispatch /learn returns {type: 'send', message: <built prompt>}
    so the TUI fires a real agent turn (#51829). The CLI handler queues onto
    _pending_input — a queue the TUI slash worker has no reader for — so the
    prompt was silently dropped after the ack. Routing through command.dispatch
    injects the standards-guided prompt as a normal turn instead.
    """
    from agent.learn_prompt import build_learn_prompt

    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}

    arg = "create a skill from https://example.com/docs"
    resp = server.handle_request({
        "id": "r-learn",
        "method": "command.dispatch",
        "params": {"name": "learn", "arg": arg, "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == build_learn_prompt(arg)


def test_pending_input_commands_includes_learn(server):
    """Guard: _PENDING_INPUT_COMMANDS must list 'learn' — without it slash.exec
    routes /learn to the slash worker, which only prints the ack and drops the
    prompt onto the dead _pending_input queue (#51829)."""
    assert "learn" in server._PENDING_INPUT_COMMANDS


def test_skills_manage_search_uses_tools_hub_sources(server):
    result = type("Result", (), {
        "description": "Build better terminal demos",
        "name": "showroom",
    })()
    auth = MagicMock(return_value="auth")
    router = MagicMock(return_value=["source"])
    search = MagicMock(return_value=[result])
    fake_hub = types.SimpleNamespace(
        GitHubAuth=auth,
        create_source_router=router,
        unified_search=search,
    )

    with patch.dict(sys.modules, {"tools.skills_hub": fake_hub}):
        resp = server.handle_request({
            "id": "skills-search",
            "method": "skills.manage",
            "params": {"action": "search", "query": "showroom"},
        })

    assert "error" not in resp
    assert resp["result"] == {
        "results": [{"description": "Build better terminal demos", "name": "showroom"}]
    }
    auth.assert_called_once_with()
    router.assert_called_once_with("auth")
    search.assert_called_once_with("showroom", ["source"], source_filter="all", limit=20)


def test_command_dispatch_steer_fallback_sends_message(server):
    """command.dispatch /steer with no active agent falls back to send."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid, "agent": None}

    resp = server.handle_request({
        "id": "r3",
        "method": "command.dispatch",
        "params": {"name": "steer", "arg": "focus on testing", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "focus on testing"


def test_command_dispatch_retry_finds_last_user_message(server):
    """command.dispatch /retry walks session['history'] to find the last user message."""
    sid = "test-session"
    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]
    server._sessions[sid] = {
        "session_key": sid,
        "agent": None,
        "history": history,
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    resp = server.handle_request({
        "id": "r4",
        "method": "command.dispatch",
        "params": {"name": "retry", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "second question"
    # Verify history was truncated: everything from last user message onward removed
    assert len(server._sessions[sid]["history"]) == 2
    assert server._sessions[sid]["history"][-1]["role"] == "assistant"
    assert server._sessions[sid]["history_version"] == 1


def test_command_dispatch_retry_empty_history(server):
    """command.dispatch /retry with empty history returns error."""
    sid = "test-session"
    server._sessions[sid] = {
        "session_key": sid,
        "agent": None,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    resp = server.handle_request({
        "id": "r5",
        "method": "command.dispatch",
        "params": {"name": "retry", "session_id": sid},
    })

    assert "error" in resp
    assert resp["error"]["code"] == 4018


def test_command_dispatch_retry_handles_multipart_content(server):
    """command.dispatch /retry extracts text from multipart content lists."""
    sid = "test-session"
    history = [
        {"role": "user", "content": [
            {"type": "text", "text": "analyze this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        ]},
        {"role": "assistant", "content": "I see the image."},
    ]
    server._sessions[sid] = {
        "session_key": sid,
        "agent": None,
        "history": history,
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    resp = server.handle_request({
        "id": "r6",
        "method": "command.dispatch",
        "params": {"name": "retry", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "analyze this"


def test_command_dispatch_returns_skill_payload(server):
    """command.dispatch returns structured skill payload for the TUI to send()."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}

    fake_skills = {"/hermes-agent-dev": {"name": "hermes-agent-dev", "description": "Dev workflow"}}
    fake_msg = "Loaded skill content here"

    with patch("agent.skill_commands.scan_skill_commands", return_value=fake_skills), \
         patch("agent.skill_commands.build_skill_invocation_message", return_value=fake_msg):
        resp = server.handle_request({
            "id": "r2",
            "method": "command.dispatch",
            "params": {"name": "hermes-agent-dev", "session_id": sid},
        })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "skill"
    assert result["message"] == fake_msg
    assert result["name"] == "hermes-agent-dev"


def test_command_dispatch_returns_custom_bundle_payload(server):
    """command.dispatch preserves bundle arguments in a sendable agent turn."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}
    fake_bundles = {
        "/review-suite": {
            "name": "review-suite",
            "skills": ["source-check", "claim-audit", "enough-research"],
        }
    }
    arg = "audit the migration plan"
    fake_msg = (
        '[IMPORTANT: The user has invoked the "review-suite" skill bundle.]\n\n'
        f"User instruction: {arg}"
    )

    with patch("agent.skill_bundles.get_skill_bundles", return_value=fake_bundles), \
         patch(
             "agent.skill_bundles.build_bundle_invocation_message",
             return_value=(
                 fake_msg,
                 ["source-check", "claim-audit", "enough-research"],
                 [],
             ),
         ) as build_bundle, \
         patch("agent.skill_commands.build_skill_invocation_message") as build_skill, \
         patch.object(server, "_resolve_session_platform", return_value="tui"):
        resp = server.handle_request({
            "id": "r-bundle-dispatch",
            "method": "command.dispatch",
            "params": {"name": "review-suite", "arg": arg, "session_id": sid},
        })

    assert "error" not in resp
    assert resp["result"] == {
        "type": "send",
        "message": fake_msg,
        "notice": "⚡ Loading bundle: review-suite (3 skills)",
    }
    build_bundle.assert_called_once_with(
        "/review-suite",
        arg,
        task_id=sid,
        platform="tui",
    )
    build_skill.assert_not_called()


def test_command_dispatch_awaits_async_plugin_handler(server):
    async def _handler(arg):
        return f"async:{arg}"

    with patch(
        "hermes_cli.plugins.get_plugin_command_handler",
        lambda name: _handler if name == "async-cmd" else None,
    ):
        resp = server.handle_request({
            "id": "r-plugin",
            "method": "command.dispatch",
            "params": {"name": "async-cmd", "arg": "hello"},
        })

    assert "error" not in resp
    assert resp["result"] == {"type": "plugin", "output": "async:hello"}


# ── dispatch(): pool routing for long handlers (#12546) ──────────────


def test_dispatch_runs_short_handlers_inline(server):
    """Non-long handlers return their response synchronously from dispatch()."""
    server._methods["fast.ping"] = lambda rid, params: server._ok(rid, {"pong": True})

    resp = server.dispatch({"id": "r1", "method": "fast.ping", "params": {}})

    assert resp == {"jsonrpc": "2.0", "id": "r1", "result": {"pong": True}}


def test_dispatch_offloads_long_handlers_and_emits_via_stdout(capture):
    """Long handlers run on the pool and write their response via write_json."""
    server, buf = capture
    server._methods["slash.exec"] = lambda rid, params: server._ok(rid, {"output": "hi"})

    resp = server.dispatch({"id": "r2", "method": "slash.exec", "params": {}})
    assert resp is None

    for _ in range(50):
        if buf.getvalue():
            break
        time.sleep(0.01)

    written = json.loads(buf.getvalue())
    assert written == {"jsonrpc": "2.0", "id": "r2", "result": {"output": "hi"}}


@pytest.mark.parametrize(
    "method", ["autonomy.exec", "receipt.exec", "transaction.exec"]
)
def test_dispatch_routes_native_exec_handlers_to_pool(server, method):
    """Native profile-I/O RPCs must not run on the JSON-RPC reader thread."""
    assert method in server._methods
    assert method in server._LONG_HANDLERS

    transport = MagicMock()
    handler = lambda rid, _params: server._ok(rid, {"method": method})
    with (
        patch.dict(server._methods, {method: handler}),
        patch.object(server._pool, "submit") as submit,
    ):
        assert server.dispatch(
            {"id": "native", "method": method, "params": {}}, transport,
        ) is None
        submit.assert_called_once()
        submitted = submit.call_args.args[0]
        submitted()

    transport.write.assert_called_once()
    assert transport.write.call_args.args[0] == {
        "jsonrpc": "2.0",
        "id": "native",
        "result": {"method": method},
    }


def test_dispatch_long_handler_does_not_block_fast_handler(server):
    """A slow long handler must not prevent a concurrent fast handler from completing."""
    released = threading.Event()
    server._methods["slash.exec"] = lambda rid, params: (released.wait(timeout=5), server._ok(rid, {"done": True}))[1]
    server._methods["fast.ping"] = lambda rid, params: server._ok(rid, {"pong": True})

    t0 = time.monotonic()
    assert server.dispatch({"id": "slow", "method": "slash.exec", "params": {}}) is None

    fast_resp = server.dispatch({"id": "fast", "method": "fast.ping", "params": {}})
    fast_elapsed = time.monotonic() - t0

    assert fast_resp["result"] == {"pong": True}
    assert fast_elapsed < 2.0, f"fast handler blocked for {fast_elapsed:.2f}s behind slow handler"

    released.set()


def test_dispatch_session_compress_does_not_block_fast_handler(server):
    """Manual TUI compaction can take minutes, so it must not block the RPC loop."""
    released = threading.Event()

    def slow_compress(rid, params):
        released.wait(timeout=5)
        return server._ok(rid, {"done": True})

    server._methods["session.compress"] = slow_compress
    server._methods["fast.ping"] = lambda rid, params: server._ok(rid, {"pong": True})

    t0 = time.monotonic()
    assert server.dispatch({"id": "slow", "method": "session.compress", "params": {}}) is None

    fast_resp = server.dispatch({"id": "fast", "method": "fast.ping", "params": {}})
    fast_elapsed = time.monotonic() - t0

    assert fast_resp["result"] == {"pong": True}
    assert fast_elapsed < 2.0, f"fast handler blocked for {fast_elapsed:.2f}s behind session.compress"

    released.set()


def test_dispatch_long_handler_exception_produces_error_response(capture):
    """An exception inside a pool-dispatched handler still yields a JSON-RPC error."""
    server, buf = capture

    def boom(rid, params):
        raise RuntimeError("kaboom")

    server._methods["slash.exec"] = boom

    server.dispatch({"id": "r3", "method": "slash.exec", "params": {}})

    for _ in range(50):
        if buf.getvalue():
            break
        time.sleep(0.01)

    written = json.loads(buf.getvalue())
    assert written["id"] == "r3"
    assert written["error"]["code"] == -32000
    assert "kaboom" in written["error"]["message"]


def test_dispatch_unknown_long_method_still_goes_inline(server):
    """Method name not in _LONG_HANDLERS takes the sync path even if handler is slow."""
    server._methods["some.method"] = lambda rid, params: server._ok(rid, {"ok": True})

    resp = server.dispatch({"id": "r4", "method": "some.method", "params": {}})

    assert resp["result"] == {"ok": True}


@pytest.mark.parametrize("completion_method", ["complete.path", "complete.slash"])
def test_completion_handlers_are_pool_routed(completion_method, server):
    """complete.path/complete.slash must run on the pool, never the reader thread.

    Regression for #21123: completion ran inline, so a slow git ls-files /
    skill-scan blocked prompt.submit and froze the TUI for the 120s RPC timeout.
    """
    assert completion_method in server._LONG_HANDLERS


@pytest.mark.parametrize("completion_method", ["complete.path", "complete.slash"])
def test_slow_completion_does_not_block_fast_handler(completion_method, server):
    """A slow completion RPC must not block a concurrent fast handler (#21123)."""
    released = threading.Event()

    def slow_completion(rid, params):
        released.wait(timeout=5)
        return server._ok(rid, {"items": []})

    server._methods[completion_method] = slow_completion
    server._methods["fast.ping"] = lambda rid, params: server._ok(rid, {"pong": True})

    t0 = time.monotonic()
    assert server.dispatch({"id": "slow", "method": completion_method, "params": {}}) is None

    fast_resp = server.dispatch({"id": "fast", "method": "fast.ping", "params": {}})
    fast_elapsed = time.monotonic() - t0

    assert fast_resp["result"] == {"pong": True}
    assert fast_elapsed < 2.0, f"fast handler blocked for {fast_elapsed:.2f}s behind {completion_method}"

    released.set()


def test_skin_live_switch_end_to_end(server, tmp_path, monkeypatch):
    """Real config + skin files: activating a skin (as `hermes config set` does)
    makes the per-tool reconcile broadcast skin.changed with the resolved palette.
    Exercises _load_cfg → _skin_sig → resolve_skin → _emit with no mocks in between."""
    import hermes_cli.skin_engine as skin_engine

    (tmp_path / "skins").mkdir()
    (tmp_path / "skins" / "midnight.yaml").write_text(
        "name: midnight\ndescription: t\ncolors:\n  banner_title: '#00ffcc'\n  background: '#001010'\n"
    )
    monkeypatch.setattr(skin_engine, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_last_skin_sig", None, raising=False)
    server._cfg_cache = server._cfg_mtime = server._cfg_path = None

    emitted = []
    monkeypatch.setattr(server, "_emit", lambda ev, sid, payload=None: emitted.append((ev, payload)))

    # Baseline (default) — seeds the signature.
    (tmp_path / "config.yaml").write_text("display:\n  skin: default\n")
    server._broadcast_skin_if_changed()
    emitted.clear()

    # Activate midnight, as `hermes config set display.skin midnight` would.
    time.sleep(0.01)  # ensure the config mtime moves
    (tmp_path / "config.yaml").write_text("display:\n  skin: midnight\n")
    server._broadcast_skin_if_changed()

    assert [ev for ev, _ in emitted] == ["skin.changed"]
    assert emitted[0][1]["name"] == "midnight"
    assert emitted[0][1]["colors"]["banner_title"] == "#00ffcc"


def test_broadcast_skin_if_changed_on_any_signature_move(server, monkeypatch):
    """A skin the agent changes mid-turn goes live once per real move: a name
    switch (incl. switch-then-revert) OR an in-place color edit to the active skin
    (same name, new file mtime). An unchanged signature never re-broadcasts."""
    emitted = []
    # switch, no-op, switch, then a color edit (same name, bumped mtime).
    sigs = iter([("neon", 1.0), ("neon", 1.0), ("forest", 1.0), ("forest", 2.0)])
    monkeypatch.setattr(server, "_emit", lambda ev, sid, payload=None: emitted.append((ev, payload)))
    monkeypatch.setattr(server, "_last_skin_sig", None, raising=False)
    monkeypatch.setattr(server, "_skin_sig", lambda: next(sigs))
    monkeypatch.setattr(server, "resolve_skin", lambda: {"name": "x", "colors": {}})

    for _ in range(4):
        server._broadcast_skin_if_changed()

    assert [ev for ev, _ in emitted] == ["skin.changed"] * 3


def test_gateway_owned_teardown_disables_agent_row_finalization(server, monkeypatch):
    calls = []

    class DB:
        def get_session(self, _session_id):
            return {"source": "telegram"}

        def end_session(self, *args):
            calls.append(("end", args))

    class Agent:
        session_id = "gateway-session"
        _session_db = DB()
        _end_session_on_close = True

        def close(self):
            calls.append(("close", self._end_session_on_close))
            if self._end_session_on_close:
                self._session_db.end_session(self.session_id, "agent_close")

    agent = Agent()
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args: None)
    server._teardown_session(
        {
            "agent": agent,
            "session_key": "gateway-session",
            "history": [],
            "history_lock": threading.Lock(),
        }
    )

    assert calls == [("close", False)]


def test_profile_resume_lookup_closes_db_on_early_error(server, monkeypatch, tmp_path):
    class DB:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

        def get_session(self, _session_id):
            return None

        def get_session_by_title(self, _target):
            return None

    db = DB()
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setattr(server, "_profile_home", lambda _profile: profile_home)
    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=lambda **_: db))

    response = server._methods["session.resume"](
        "resume", {"session_id": "missing", "profile": "worker"}
    )

    assert response["error"]["code"] == 4007
    assert db.closed == 1


def test_deferred_profile_db_open_failure_is_fatal(server, monkeypatch, tmp_path):
    ready = threading.Event()
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    session = {
        "agent": None,
        "agent_error": None,
        "agent_ready": ready,
        "agent_build_started": False,
        "session_key": "profile-session",
        "profile_home": str(profile_home),
        "source": "tui",
        "history_lock": threading.Lock(),
    }
    server._sessions["profile-sid"] = session
    monkeypatch.setitem(
        sys.modules,
        "hermes_state",
        types.SimpleNamespace(SessionDB=lambda **_: (_ for _ in ()).throw(OSError("locked"))),
    )
    monkeypatch.setattr(
        server, "_make_agent", lambda *_args, **_kwargs: pytest.fail("must not build")
    )

    server._start_agent_build("profile-sid", session)
    assert ready.wait(2)
    assert "locked" in session["agent_error"]


def test_deferred_local_build_constructs_and_attaches_agent(server, monkeypatch):
    """A launch-profile deferred build must execute the common build path too."""
    sid = "deferred-local"
    ready = threading.Event()
    session = {
        "agent": None,
        "agent_error": None,
        "agent_ready": ready,
        "session_key": "local-key",
        "profile_home": None,
        "cwd": "/tmp",
        "source": "tui",
        "history_lock": threading.Lock(),
    }
    built = types.SimpleNamespace(model="local/model")
    builds = []

    class _Worker:
        def close(self):
            return None

    monkeypatch.setattr(server, "_set_session_context", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *_a: None)
    monkeypatch.setattr(server, "_make_agent", lambda *_a, **_k: builds.append(True) or built)
    monkeypatch.setattr(server, "_SlashWorker", lambda *_a, **_k: _Worker())
    monkeypatch.setattr(server, "_attach_worker", lambda _sid, current, worker: current.__setitem__("slash_worker", worker))
    monkeypatch.setattr(server, "_start_notification_poller", lambda *_a, **_k: threading.Event())
    monkeypatch.setattr(server, "_wire_callbacks", lambda *_a: None)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_a: None)
    monkeypatch.setattr(server, "_emit", lambda *_a: None)
    monkeypatch.setattr(server, "_schedule_mcp_late_refresh", lambda *_a: None)
    monkeypatch.setattr(server, "_probe_config_health", lambda *_a: None)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_config_model_target", lambda: "local/model")
    monkeypatch.setattr(server, "_resolve_model", lambda: "local/model")
    fake_approval = types.SimpleNamespace(
        register_gateway_notify=lambda *_a, **_k: None,
        load_permanent_allowlist=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "tools.approval", fake_approval)

    server._sessions[sid] = session
    server._start_agent_build(sid, session)

    assert ready.wait(2)
    assert builds == [True]
    assert session["agent"] is built
    assert session["session_db"] is None
    assert session.get("agent_error") is None


def test_deferred_build_initial_identity_fence_skips_replacement(server, monkeypatch):
    """A deferred build queued for an old record must not touch its replacement."""
    sid = "deferred-initial-fence"
    ready = threading.Event()
    original = {
        "agent": None,
        "agent_ready": ready,
        "session_key": "old-key",
        "profile_home": None,
        "cwd": "/tmp",
        "source": "tui",
    }
    replacement = {"replacement": True}
    calls = []

    monkeypatch.setattr(
        server,
        "_set_session_context",
        lambda *_a, **_k: calls.append("context") or [],
    )
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda *_a, **_k: calls.append("agent") or pytest.fail("stale build constructed an agent"),
    )

    with server._sessions_lock:
        server._sessions[sid] = replacement
    server._start_agent_build(sid, original)

    assert ready.wait(2)
    assert server._sessions[sid] is replacement
    assert replacement == {"replacement": True}
    assert calls == []


def test_reset_swaps_before_starting_candidate_side_machinery(server, monkeypatch, tmp_path):
    """Reset must not start worker/poller after a candidate loses its slot."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    sid = "reset-side-machinery-fence"
    replacement = {"replacement": True}
    worker_calls = []
    poller_calls = []
    dbs = []
    agents = []

    class _DB:
        def __init__(self, *, db_path):
            self.close_calls = 0
            dbs.append(self)

        def close(self):
            self.close_calls += 1

    class _Agent:
        model = "new/model"

        def __init__(self, db):
            self._session_db = db
            self.close_calls = 0
            agents.append(self)

        def close(self):
            self.close_calls += 1
            self._session_db.close()

    old_agent = types.SimpleNamespace(model="old/model")
    session = {
        "agent": old_agent,
        "session_key": "reset-side-key",
        "profile_home": str(profile_home),
        "cwd": str(tmp_path),
        "source": "tui",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=_DB))
    monkeypatch.setattr(server, "set_hermes_home_override", lambda *_a: "home-token")
    monkeypatch.setattr(server, "reset_hermes_home_override", lambda *_a: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *_a: None)
    monkeypatch.setattr(server, "_make_agent", lambda *_a, session_db, **_k: _Agent(session_db))
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda *_a: (server._sessions.__setitem__(sid, replacement) or {"model": "new/model"}),
    )
    monkeypatch.setattr(
        server,
        "_restart_slash_worker",
        lambda *_a, **_k: worker_calls.append(True),
    )
    monkeypatch.setattr(
        server,
        "_start_notification_poller",
        lambda *_a, **_k: poller_calls.append(True),
    )
    monkeypatch.setattr(server, "_config_model_target", lambda: "new/model")
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "all")
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_a: None)
    monkeypatch.setattr(server, "_emit", lambda *_a: None)

    with server._sessions_lock:
        server._sessions[sid] = session
    with pytest.raises(RuntimeError, match="closed or replaced during reset"):
        server._reset_session_agent(sid, session)

    assert server._sessions[sid] is replacement
    assert worker_calls == []
    assert poller_calls == []
    assert len(agents) == 1
    assert agents[0].close_calls == 1
    assert dbs[0].close_calls == 1


def test_reset_publication_failure_cleans_old_and_candidate_resources(
    server, monkeypatch, tmp_path
):
    """A reset that loses its slot after publication must release both generations."""
    sid = "reset-published-failure"
    replacement = {"replacement": True}
    old_stop = threading.Event()
    old_thread_done = threading.Event()

    def _old_poller():
        old_stop.wait(5)
        old_thread_done.set()

    old_thread = threading.Thread(target=_old_poller, daemon=True)
    old_thread.start()

    class _Closable:
        def __init__(self, name):
            self.name = name
            self.close_calls = 0
            self._end_session_on_close = True

        def close(self):
            self.close_calls += 1

    old_agent = _Closable("old-agent")
    new_agent = _Closable("new-agent")
    old_worker = _Closable("old-worker")
    new_worker = _Closable("new-worker")
    session = {
        "agent": old_agent,
        "session_key": "reset-published-key",
        "profile_home": None,
        "cwd": str(tmp_path),
        "source": "tui",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "_notif_stop": old_stop,
        "_notif_thread": old_thread,
        "slash_worker": old_worker,
    }

    monkeypatch.setattr(server, "_set_session_context", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda _tokens: None)
    monkeypatch.setattr(server, "_make_agent", lambda *_args, **_kwargs: new_agent)
    monkeypatch.setattr(server, "_session_info", lambda *_args: {"model": "new/model"})
    monkeypatch.setattr(server, "_config_model_target", lambda: "new/model")
    monkeypatch.setattr(server, "_load_show_reasoning", lambda: False)
    monkeypatch.setattr(server, "_load_tool_progress_mode", lambda: "all")

    def _reap_after_publication(_sid, _session, build_only=False):
        assert build_only is True
        with server._sessions_lock:
            server._sessions[sid] = replacement
        return new_worker

    monkeypatch.setattr(server, "_restart_slash_worker", _reap_after_publication)
    monkeypatch.setattr(
        server,
        "_start_notification_poller",
        lambda *_args, **_kwargs: pytest.fail("replacement must not receive a poller"),
    )
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args: None)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)

    with server._sessions_lock:
        server._sessions[sid] = session
    try:
        with pytest.raises(RuntimeError, match="closed or replaced during reset"):
            server._reset_session_agent(sid, session)

        old_stop_set = old_stop.is_set()
        old_thread_alive = old_thread.is_alive()
        old_thread_finished = old_thread_done.is_set()
        replacement_snapshot = dict(server._sessions[sid])
        assert old_stop_set
        assert not old_thread_alive
        assert old_thread_finished
        assert old_worker.close_calls == 1
        assert old_agent.close_calls == 1
        assert old_agent._end_session_on_close is False
        assert new_agent.close_calls == 1
        assert new_worker.close_calls == 1
        assert replacement_snapshot == replacement
    finally:
        old_stop.set()
        old_thread.join(timeout=1)
        server._sessions.pop(sid, None)


def test_profile_branch_limit_and_row_error_close_db_once(server, monkeypatch, tmp_path):
    """Profile branch failures before ownership transfer close their DB once."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    close_counts = []
    lease_releases = []

    class _Lease:
        def release(self):
            lease_releases.append(True)

    class _DB:
        def __init__(self, *, db_path):
            self.close_calls = 0
            close_counts.append(self)

        def close(self):
            self.close_calls += 1

        def get_session_title(self, _key):
            return "parent"

        def get_next_title_in_lineage(self, title):
            return title + " 2"

        def create_session(self, *_a, **_k):
            raise RuntimeError("row write failed")

    parent = {
        "agent": types.SimpleNamespace(),
        "agent_ready": threading.Event(),
        "session_key": "parent-key",
        "profile_home": str(profile_home),
        "cwd": str(tmp_path),
        "source": "tui",
        "history": [{"role": "user", "content": "hello"}],
        "history_lock": threading.Lock(),
        "cols": 80,
    }
    parent["agent_ready"].set()
    server._sessions["branch-parent-errors"] = parent
    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=_DB))
    monkeypatch.setattr(server, "set_hermes_home_override", lambda *_a: "home")
    monkeypatch.setattr(server, "reset_hermes_home_override", lambda *_a: None)
    monkeypatch.setattr(server, "_new_session_key", lambda: "branch-error-key")
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *_a, **_k: (None, "at limit"))
    limited = server._methods["session.branch"](
        "limit", {"session_id": "branch-parent-errors"}
    )
    assert limited["error"]["code"] == 4090
    assert close_counts[0].close_calls == 1

    close_counts.clear()
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *_a, **_k: (_Lease(), None))
    failed = server._methods["session.branch"](
        "row", {"session_id": "branch-parent-errors"}
    )
    assert failed["error"]["code"] == 5008
    assert close_counts[0].close_calls == 1
    assert lease_releases == [True]


def test_profile_branch_agent_failure_rolls_back_only_new_row(server, monkeypatch, tmp_path):
    """A failed branch agent build ends only the newly-created branch row."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    dbs = []
    events = []

    class _Lease:
        def release(self):
            events.append(("release",))

    class _DB:
        def __init__(self, *, db_path):
            self.close_calls = 0
            dbs.append(self)

        def close(self):
            self.close_calls += 1

        def get_session_title(self, _key):
            return "parent"

        def get_next_title_in_lineage(self, title):
            return title + " 2"

        def create_session(self, key, **_kwargs):
            events.append(("create", key))

        def append_message(self, **_kwargs):
            events.append(("append",))

        def set_session_title(self, key, _title):
            events.append(("title", key))

        def end_session(self, key, reason):
            events.append(("end", key, reason))

    parent = {
        "agent": types.SimpleNamespace(),
        "agent_ready": threading.Event(),
        "session_key": "parent-key",
        "profile_home": str(profile_home),
        "cwd": str(tmp_path),
        "source": "tui",
        "history": [{"role": "user", "content": "hello"}],
        "history_lock": threading.Lock(),
        "cols": 80,
    }
    parent["agent_ready"].set()
    server._sessions["branch-agent-error"] = parent
    monkeypatch.setitem(sys.modules, "hermes_state", types.SimpleNamespace(SessionDB=_DB))
    monkeypatch.setattr(server, "set_hermes_home_override", lambda *_a: "home")
    monkeypatch.setattr(server, "reset_hermes_home_override", lambda *_a: events.append(("reset",)))
    monkeypatch.setattr(server, "_new_session_key", lambda: "branch-new-key")
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *_a, **_k: (_Lease(), None))
    monkeypatch.setattr(server, "_make_agent", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("agent failed")))
    failed = server._methods["session.branch"](
        "agent", {"session_id": "branch-agent-error"}
    )

    assert failed["error"]["code"] == 5000
    assert ("end", "branch-new-key", "branch_agent_init_failed") in events
    assert not any(event[0] == "end" and event[1] == "parent-key" for event in events)
    assert events.count(("release",)) == 1
    assert dbs[0].close_calls == 1
    assert ("reset",) in events


def test_eager_resume_loser_disables_end_on_close_before_close(server, monkeypatch):
    """A concurrent eager-resume loser must not end the winner's durable row."""
    target = "resume-race-target"
    calls = []
    winner = types.SimpleNamespace(model="winner/model")
    winner_session = {"agent": winner, "session_key": target, "history": []}
    live_calls = iter([None, ("winner-sid", winner_session)])

    class _DB:
        def get_session(self, _sid):
            return {"id": target, "source": "tui"}

        def resolve_resume_session_id(self, _sid):
            return target

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, _sid):
            return ([], [])

        def get_ancestor_display_prefix(self, _sid):
            return []

        def end_session(self, *args):
            calls.append(("end", args))

    class _Lease:
        db = _DB()

        def release(self):
            calls.append(("release",))

        def transfer_to_agent(self, _agent):
            return None

    class _Loser:
        model = "loser/model"

        def __init__(self):
            self._end_session_on_close = True
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            calls.append(("close", self._end_session_on_close))
            if self._end_session_on_close:
                _Lease.db.end_session(target, "agent_close")

    loser = _Loser()
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda _key: next(live_calls))
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *_a, **_k: (_Lease(), None))
    monkeypatch.setattr(server, "_set_session_context", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *_a: None)
    monkeypatch.setattr(server, "_make_agent", lambda *_a, **_k: loser)
    monkeypatch.setattr(server, "_stored_session_runtime_overrides", lambda _row: {})
    monkeypatch.setattr(server, "_live_session_payload", lambda *_a, **_k: {"session_id": "winner-sid"})
    monkeypatch.setattr(server, "current_transport", lambda: server._stdio_transport)

    response = server._session_resume_impl(
        "race",
        {"session_id": target, "eager_build": True},
        _Lease(),
    )

    assert response["result"]["session_id"] == "winner-sid"
    assert loser.close_calls == 1
    assert ("close", False) in calls
    assert not any(event[0] == "end" for event in calls)
    assert calls.count(("release",)) == 1


def test_close_agent_once_is_atomic_for_non_weakref_concurrent_agent(server):
    """Two teardown threads close a managed proxy exactly once."""
    entered = threading.Event()
    release = threading.Event()

    class _Agent:
        __slots__ = ("close_calls",)

        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            entered.set()
            release.wait(2)

    agent = _Agent()
    managed = server._ManagedAgentProxy(agent)
    first = threading.Thread(target=server._close_agent_once, args=(managed,))
    second = threading.Thread(target=server._close_agent_once, args=(managed,))
    first.start()
    assert entered.wait(1)
    second.start()
    time.sleep(0.05)
    assert agent.close_calls == 1
    release.set()
    first.join(1)
    second.join(1)
    assert not first.is_alive() and not second.is_alive()
    assert agent.close_calls == 1


def test_close_agent_once_serializes_virgin_mutable_agent_lock_installation(
    server, monkeypatch
):
    """Concurrent first close calls must select one object-owned lock."""
    real_lock = threading.Lock
    factory_barrier = threading.Barrier(2)
    factory_guard = real_lock()
    factory_calls = [0]
    close_count_lock = real_lock()
    thread_state = threading.local()
    installed_locks = []

    class _Lock:
        def __init__(self):
            self._raw = real_lock()

        def acquire(self, *args, **kwargs):
            return self._raw.acquire(*args, **kwargs)

        def release(self):
            return self._raw.release()

        def __enter__(self):
            self.acquire()
            thread_state.inside_close_lock = True
            return self

        def __exit__(self, exc_type, exc, tb):
            thread_state.inside_close_lock = False
            self.release()
            return False

    def _lock_factory():
        with factory_guard:
            factory_calls[0] += 1
            call_number = factory_calls[0]
        if call_number <= 2:
            try:
                factory_barrier.wait(timeout=1)
            except threading.BrokenBarrierError:
                pass
        lock = _Lock()
        thread_state.expected_lock = lock
        return lock

    class _Agent:
        def __init__(self):
            self.close_calls = 0

        def __setattr__(self, name, value):
            if name == "_tui_close_lock":
                installed_locks.append(value)
            object.__setattr__(self, name, value)

        def __getattribute__(self, name):
            if name == "_tui_close_lock":
                expected = getattr(thread_state, "expected_lock", None)
                if expected is not None:
                    return expected
            if name == "_tui_closed":
                expected = getattr(thread_state, "expected_lock", None)
                if expected is not None and getattr(
                    thread_state, "inside_close_lock", False
                ):
                    return False
            return object.__getattribute__(self, name)

        def close(self):
            with close_count_lock:
                self.close_calls += 1

    agent = _Agent()
    errors = []
    threads = [
        threading.Thread(
            target=lambda: server._close_agent_once(agent),
        )
        for _ in range(2)
    ]
    monkeypatch.setattr(server.threading, "Lock", _lock_factory)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        if thread.is_alive():
            errors.append("close thread did not finish")

    assert not errors
    assert len(installed_locks) == 1
    assert agent.close_calls == 1
    assert agent._tui_closed is True


def test_close_agent_once_is_safe_when_agent_close_reenters(server):
    """The close-once marker must short-circuit a recursive teardown."""

    class _Agent:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            server._close_agent_once(self)

    agent = _Agent()
    thread = threading.Thread(target=server._close_agent_once, args=(agent,), daemon=True)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert agent.close_calls == 1


def test_close_agent_once_survives_non_weakref_slots_agent_churn(server):
    """Close stamps on mutable non-weakref slots agents survive registry churn."""

    class _Agent:
        __slots__ = ("close_calls", "_tui_close_lock", "_tui_closed")

        def __init__(self):
            self.close_calls = 0
            self._tui_close_lock = None
            self._tui_closed = False

        def close(self):
            self.close_calls += 1

    agents = [_Agent() for _ in range(257)]
    for agent in agents:
        server._close_agent_once(agent)

    server._close_agent_once(agents[0])

    assert [agent.close_calls for agent in agents] == [1] * len(agents)


def test_close_agent_once_survives_immutable_nonweakref_churn(server):
    """Agents normalized by _make_agent use object-owned proxy state."""

    class _Agent:
        __slots__ = ("close_calls",)

        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    agents = []
    managed_agents = []

    def _build(*_args, **_kwargs):
        agent = _Agent()
        agents.append(agent)
        return agent

    synthetic_module = types.SimpleNamespace(maybe_build_synthetic_agent=_build)
    with patch.dict(sys.modules, {"tui_gateway.synthetic_turn": synthetic_module}):
        for index in range(257):
            managed = server._make_agent(f"sid-{index}", f"key-{index}")
            managed_agents.append(managed)
            assert managed is not agents[-1]
            server._close_agent_once(managed)

    server._close_agent_once(managed_agents[0])

    assert [agent.close_calls for agent in agents] == [1] * len(agents)
    assert len(server._agent_close_weak_registry) == 0


def test_close_agent_once_rejects_unmanaged_immutable_nonweakref_agent(server):
    """Direct unsupported immutable agents fail closed before close."""

    class _Agent:
        __slots__ = ("close_calls",)

        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    agent = _Agent()
    with pytest.raises(RuntimeError, match="managed agent"):
        server._close_agent_once(agent)
    assert agent.close_calls == 0


def test_synthetic_profile_agent_closes_transferred_db(server, monkeypatch):
    """The synthetic build seam must preserve profile DB ownership."""
    db = types.SimpleNamespace(close_calls=0)
    db.close = lambda: setattr(db, "close_calls", db.close_calls + 1)
    synthetic = types.SimpleNamespace(close_calls=0, model="synthetic/model")
    synthetic.close = lambda: setattr(synthetic, "close_calls", synthetic.close_calls + 1)
    monkeypatch.setitem(
        sys.modules,
        "tui_gateway.synthetic_turn",
        types.SimpleNamespace(maybe_build_synthetic_agent=lambda *_a, **_k: synthetic),
    )

    built = server._make_agent(
        "synthetic-sid",
        "synthetic-key",
        session_db=db,
        owns_session_db=True,
    )
    server._close_agent_once(built)

    assert built is synthetic
    assert synthetic.close_calls == 1
    assert db.close_calls == 1


def test_immutable_synthetic_profile_agent_uses_managed_proxy(server, monkeypatch):
    """Immutable synthetic agents transfer DB ownership into the proxy."""
    db = types.SimpleNamespace(close_calls=0)
    db.close = lambda: setattr(db, "close_calls", db.close_calls + 1)

    class _Agent:
        __slots__ = ("close_calls", "model", "session_id", "platform")

        def __init__(self):
            self.close_calls = 0
            self.model = "immutable/model"
            self.session_id = "immutable-session"
            self.platform = "tui"

        def close(self):
            self.close_calls += 1

    synthetic = _Agent()
    monkeypatch.setitem(
        sys.modules,
        "tui_gateway.synthetic_turn",
        types.SimpleNamespace(maybe_build_synthetic_agent=lambda *_a, **_k: synthetic),
    )

    built = server._make_agent(
        "immutable-sid",
        "immutable-key",
        session_db=db,
        owns_session_db=True,
    )

    assert isinstance(built, server._ManagedAgentProxy)
    assert built.model == "immutable/model"
    assert built.session_id == "immutable-session"
    assert built.platform == "tui"
    built.close()
    built.close()
    assert synthetic.close_calls == 1
    assert db.close_calls == 1
