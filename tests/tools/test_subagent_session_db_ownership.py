import threading
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB
from run_agent import AIAgent


def test_session_db_fork_keeps_child_connection_after_parent_close(tmp_path):
    db_path = tmp_path / "state.db"
    parent = SessionDB(db_path=db_path)
    child = None
    try:
        parent.create_session("parent-session", source="test")
        child = parent.fork()

        assert child.db_path == parent.db_path
        assert child.read_only == parent.read_only
        assert child._conn is not parent._conn
        assert child._lock is not parent._lock

        parent.close()
        child.create_session("child-session", source="test")
        assert child.get_session("child-session") is not None
    finally:
        if child is not None:
            child.close()
        else:
            parent.close()


def _make_agent(session_db, *, owns_session_db):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            owns_session_db=owns_session_db,
        )
    agent.client = MagicMock()
    agent.session_id = "session-under-test"
    return agent


def test_borrowed_session_db_is_not_closed_by_agent_close():
    session_db = MagicMock()
    agent = _make_agent(session_db, owns_session_db=False)

    agent.close()

    session_db.close.assert_not_called()
    session_db.end_session.assert_called_once_with("session-under-test", "agent_close")


def test_owned_session_db_is_ended_and_closed_once():
    session_db = MagicMock()
    agent = _make_agent(session_db, owns_session_db=True)

    agent.close()
    agent.close()

    session_db.end_session.assert_called_once_with("session-under-test", "agent_close")
    session_db.close.assert_called_once_with()


def test_owned_session_db_closes_after_failed_end(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    session_db.create_session("session-under-test", source="test")
    agent = _make_agent(session_db, owns_session_db=True)
    original_end_session = session_db.end_session
    attempts = 0

    def fail_once(session_id, reason):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        return original_end_session(session_id, reason)

    setattr(session_db, "end_session", fail_once)
    try:
        agent.close()
        assert agent._session_end_called is False
        assert agent._session_db_closed is True
        assert session_db.is_open is False

        verification_db = SessionDB(db_path=session_db.db_path)
        try:
            row = verification_db.get_session("session-under-test")
        finally:
            verification_db.close()
        assert row is not None
        assert row["ended_at"] is None
    finally:
        session_db.close()


def test_owned_session_db_close_is_serialized_across_concurrent_close_calls():
    session_db = MagicMock()
    end_entered = threading.Event()
    release_end = threading.Event()
    end_calls = 0
    end_calls_lock = threading.Lock()

    def end_session(*args):
        nonlocal end_calls
        with end_calls_lock:
            end_calls += 1
        end_entered.set()
        assert release_end.wait(timeout=2)

    session_db.end_session.side_effect = end_session
    agent = _make_agent(session_db, owns_session_db=True)
    start = threading.Barrier(3)
    threads = [threading.Thread(target=lambda: (start.wait(), agent.close())) for _ in range(2)]

    for thread in threads:
        thread.start()
    start.wait()
    assert end_entered.wait(timeout=2)
    release_end.set()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert end_calls == 1
    session_db.end_session.assert_called_once_with("session-under-test", "agent_close")
    session_db.close.assert_called_once_with()


def test_owned_session_db_closes_when_session_end_is_disabled():
    session_db = MagicMock()
    agent = _make_agent(session_db, owns_session_db=True)
    setattr(agent, "_end_session_on_close", False)

    agent.close()

    session_db.end_session.assert_not_called()
    session_db.close.assert_called_once_with()


def test_owned_session_db_retries_failed_close():
    session_db = MagicMock()
    session_db.close.side_effect = [RuntimeError("transient"), None]
    agent = _make_agent(session_db, owns_session_db=True)

    agent.close()
    assert agent._session_db_closed is False

    agent.close()

    session_db.end_session.assert_called_once_with("session-under-test", "agent_close")
    assert session_db.close.call_count == 2
    assert agent._session_db_closed is True


def test_owned_session_db_close_that_raises_after_side_effect_is_not_retried():
    """A close() that performs its side effect (sets _conn=None) and then
    raises must be treated as closed: _session_db_closed flips True,
    and a follow-up agent.close() must NOT issue another close() call
    against a non-idempotent DB.
    """

    class NonIdempotentConn:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            if self.close_calls > 1:
                raise AssertionError("session DB closed twice")

    class PartialCloseDB:
        def __init__(self):
            self._conn = NonIdempotentConn()

        def end_session(self, session_id, reason):
            return None

        def close(self):
            self._conn = None
            raise RuntimeError("close failed after side effect")

    session_db = PartialCloseDB()
    agent = _make_agent(session_db, owns_session_db=True)

    agent.close()

    # Close() raised, but the side effect happened: _conn is None, so the
    # agent's _session_db_closed flag must reflect observed state.
    assert session_db._conn is None
    assert agent._session_db_closed is True

    # A second close() must observe the flag and skip — the partial close
    # is durable; we don't get to retry a non-idempotent operation.
    agent.close()
    assert agent._session_db_closed is True
