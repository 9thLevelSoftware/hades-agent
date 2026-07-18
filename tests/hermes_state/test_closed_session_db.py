"""Tests for explicit bounded closed-handle behaviour on SessionDB.

When a ``SessionDB`` has been ``close()``d, subsequent reads and writes
must raise :class:`hermes_state.SessionDBClosedError` (a ``RuntimeError``)
instead of a raw ``sqlite3.ProgrammingError`` from a closed handle. The
agent then disables its own ``_session_db`` reference on the first such
failure so the rest of the run degrades cleanly.

Transient SQLite errors (busy/locked, transient I/O) must remain
retryable — only handle-closed is the explicit, non-retryable failure.
"""

from __future__ import annotations

import sqlite3

import pytest

import hermes_state
from hermes_state import SessionDB, SessionDBClosedError


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "closed.db"
    session_db = SessionDB(db_path=db_path)
    session_db.create_session(
        session_id="s1", source="cli", model="m",
    )
    yield session_db
    try:
        session_db.close()
    except Exception:
        pass


# ── 1. Define the error type and is_open property ─────────────────────────


def test_session_db_closed_error_is_runtime_error():
    """SessionDBClosedError must subclass RuntimeError so existing
    `except RuntimeError` callers keep working, and so callers can
    distinguish it from sqlite3 errors via isinstance checks.
    """
    assert issubclass(SessionDBClosedError, RuntimeError)


def test_is_open_true_for_fresh_db(db):
    """A freshly-constructed SessionDB is open until close()."""
    assert db.is_open is True


def test_is_open_false_after_close(db):
    """After close(), is_open must flip to False."""
    db.close()
    assert db.is_open is False


# ── 2. Writes fail explicitly when closed ─────────────────────────────────


def test_append_message_after_close_raises_closed_error(db):
    """append_message must raise SessionDBClosedError, not a raw
    sqlite3.ProgrammingError, when the handle is closed.
    """
    db.close()
    assert db.is_open is False
    with pytest.raises(SessionDBClosedError):
        db.append_message("s1", role="user", content="hello")


def test_execute_write_after_close_raises_closed_error(db):
    """Any write through _execute_write must raise the closed error
    before touching sqlite3 (no ProgrammingError leak).
    """
    db.close()
    with pytest.raises(SessionDBClosedError):
        db._execute_write(lambda conn: None)


def test_create_session_after_close_raises_closed_error(db):
    """create_session is a write — must surface SessionDBClosedError."""
    db.close()
    with pytest.raises(SessionDBClosedError):
        db.create_session("s2", source="cli", model="m")


# ── 3. Reads fail explicitly when closed ──────────────────────────────────


def test_read_after_close_raises_closed_error(db):
    """Reads on a closed handle must raise SessionDBClosedError too —
    the chokepoint guards all reads, not just writes.

    Use a representative read entrypoint: get_session.
    """
    db.close()
    with pytest.raises(SessionDBClosedError):
        db.get_session("s1")


def test_list_messages_after_close_raises_closed_error(db):
    """get_messages is another read entrypoint — must raise the closed
    error, not a sqlite3.ProgrammingError.
    """
    # insert so we know get_messages is a real query path
    db.append_message("s1", role="user", content="hi")
    db.close()
    with pytest.raises(SessionDBClosedError):
        db.get_messages("s1")


# ── 4. Retryable errors remain retryable ──────────────────────────────────


def test_busy_locked_remains_retryable(db, monkeypatch):
    """Transient 'database is locked' must still propagate as
    sqlite3.OperationalError (not SessionDBClosedError) — only handle
    closure is explicit and non-retryable.
    """
    # _execute_write calls conn.execute("BEGIN IMMEDIATE") after
    # _require_open passes (handle is open). Force that one call to
    # raise "database is locked" — the retry/jitter loop catches it.
    # sqlite3.Connection.execute is read-only, so we wrap the whole
    # connection object instead.
    real_conn = db._conn
    call_count = {"n": 0}

    class _BusyConn:
        def execute(self, sql, *args, **kwargs):
            if sql == "BEGIN IMMEDIATE":
                call_count["n"] += 1
                raise sqlite3.OperationalError("database is locked")
            return real_conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    db._conn = _BusyConn()

    with pytest.raises(sqlite3.OperationalError) as ei:
        db.append_message("s1", role="user", content="hi")
    assert "locked" in str(ei.value).lower()
    # must NOT be a closed-handle error
    assert not isinstance(ei.value, SessionDBClosedError)
    # retry path must have actually run (the retry loop hit the busy
    # exception, jittered, retried, and eventually exhausted).
    assert call_count["n"] > 1


# ── 5. Agent disables its handle on the first closed-handle failure ──────


def test_agent_disables_session_db_on_first_closed_error(tmp_path):
    """In run_agent._flush_messages_to_session_db, the first
    SessionDBClosedError must disable self._session_db (set it to None)
    and return cleanly, so subsequent turns don't repeatedly trip the
    closed handle.
    """
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "agent_disables.db")
    db.create_session(
        session_id="s1", source="cli", model="m",
    )
    db.close()  # backend is now closed

    agent = AIAgent.__new__(AIAgent)
    agent._persist_disabled = False
    agent._session_db = db
    agent._session_db_created = True
    agent._owns_session_db = False
    agent._flushed_db_message_session_id = None
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._session_init_model_config = {}
    agent._cached_system_prompt = ""
    agent._parent_session_id = None
    agent.session_id = "s1"
    agent.platform = None
    agent.model = "m"

    msgs = [{"role": "user", "content": "hello"}]
    # First call: trips closed-handle error and disables _session_db.
    # The flush wraps the error and emits a warning; it must NOT
    # re-raise SessionDBClosedError to the caller.
    agent._flush_messages_to_session_db(msgs)
    assert agent._session_db is None

    # Second call must be a clean no-op (no exception).
    agent._flush_messages_to_session_db(
        [{"role": "user", "content": "world"}]
    )
    assert agent._session_db is None


def test_flush_other_errors_still_warn_and_keep_handle(tmp_path, monkeypatch):
    """A non-closed error inside _flush_messages_to_session_db must keep
    the handle alive and NOT clobber self._session_db (transient
    failures stay retryable). This guards against an over-broad catch.
    """
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "transient.db")
    db.create_session(
        session_id="s1", source="cli", model="m",
    )

    agent = AIAgent.__new__(AIAgent)
    agent._persist_disabled = False
    agent._session_db = db
    agent._session_db_created = True
    agent._owns_session_db = False
    agent._flushed_db_message_session_id = None
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._session_init_model_config = {}
    agent._cached_system_prompt = ""
    agent._parent_session_id = None
    agent.session_id = "s1"
    agent.platform = None
    agent.model = "m"

    # Force append_message to raise a non-closed error (sqlite3 I/O).
    def _boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "append_message", _boom)

    agent._flush_messages_to_session_db(
        [{"role": "user", "content": "x"}]
    )
    # Non-closed error: _session_db stays alive for retry next turn.
    assert agent._session_db is db
