"""Regression for #49225 — codex app-server turns must reach the session DB
exactly once.

The codex app-server runtime (``run_codex_app_server_turn``) is an early-return
path that bypasses ``conversation_loop`` and therefore never runs the loop's
per-step ``_persist_session()`` flushes. Before the fix, the projected
assistant/tool messages were persisted *nowhere* (state.db got only
session_meta rows), leaving ``session_search`` (FTS) and conversation-distill
blind to real gateway conversations.

The fix has the codex runtime flush its own projected messages via
``_flush_messages_to_session_db()`` (idempotent through the intrinsic
``_DB_PERSISTED_MARKER``) and return ``agent_persisted=True`` so the gateway
skips its own ``append_to_transcript`` DB write. This is critical: the inbound
user turn is already flushed at turn start (``turn_context._persist_session``),
and ``append_message`` is a raw INSERT with no dedup — a gateway re-write would
duplicate the user turn (#860 / #42039). This test locks in:

1. ``run_codex_app_server_turn`` flushes projected messages and returns
   ``agent_persisted=True``.
2. Exactly-once persistence: the already-flushed user turn is NOT re-written,
   and the new projected assistant message lands once.
3. The gateway resolution expression preserves standard-runtime behaviour.
"""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.codex_runtime import run_codex_app_server_turn
from hermes_state import SessionDB
from run_agent import AIAgent


def _make_turn():
    return SimpleNamespace(
        interrupted=False,
        error=None,
        thread_id="thread-1",
        turn_id="turn-1",
        projected_messages=[{"role": "assistant", "content": "CODEX_ASSISTANT"}],
        tool_iterations=0,
        final_text="CODEX_ASSISTANT",
        should_retire=False,
    )


def _make_agent(session_db=None, session_id="sess-codex"):
    agent = MagicMock()
    # Pre-seed the session so run_codex_app_server_turn skips the spawn block.
    agent._codex_session = MagicMock()
    agent._codex_session.run_turn.return_value = _make_turn()
    agent.tool_progress_callback = None
    agent._iters_since_skill = 0
    agent._skill_nudge_interval = 0
    agent.valid_tool_names = set()
    agent._session_db = session_db
    agent._session_db_created = True
    agent.session_id = session_id
    return agent


def test_codex_success_flushes_and_reports_persisted():
    """Codex success turn must self-persist and return agent_persisted=True."""
    agent = _make_agent(session_db=None)  # no DB -> flush is a no-op, still True
    result = run_codex_app_server_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task-1",
    )
    assert result["completed"] is True
    # With the agent as sole persister, the gateway must SKIP its DB write.
    assert result["agent_persisted"] is True


def test_codex_turn_exit_reason_is_not_turn_id():
    """Regression: codex_runtime must NOT store turn.turn_id as the
    turn_exit_reason. The ``turn_id`` is an opaque codex identifier (UUID
    like ``01938e75-...``) — it is not an exit reason and would corrupt any
    downstream analytics that group by exit reason. The ledger should see
    a stable fallback (``codex_response`` on success, ``codex_error`` on
    failure).
    """
    from agent.codex_runtime import run_codex_app_server_turn
    from agent.turn_ledger import TurnOutcomeRecord

    captured = []

    agent = _make_agent(session_db=None)
    agent._session_db = _CapturingDB(captured)

    # Success path
    success_turn = SimpleNamespace(
        interrupted=False,
        error=None,
        thread_id="thread-1",
        turn_id="OPQAQUE-UUID-01938e75-DO-NOT-LEAK",
        projected_messages=[{"role": "assistant", "content": "ok"}],
        tool_iterations=0,
        final_text="ok",
        should_retire=False,
    )
    agent._codex_session.run_turn.return_value = success_turn
    run_codex_app_server_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task-1",
    )
    assert captured, "ledger write never happened on codex success path"
    success_record = captured[-1]
    assert isinstance(success_record, TurnOutcomeRecord)
    # Must be the stable fallback, NOT the opaque codex turn_id.
    assert success_record.turn_exit_reason == "codex_response"
    assert "01938e75" not in (success_record.turn_exit_reason or "")

    # Error path
    captured.clear()
    error_turn = SimpleNamespace(
        interrupted=False,
        error="RPC timeout",
        thread_id="thread-2",
        turn_id="OPQAQUE-UUID-2",
        projected_messages=[],
        tool_iterations=0,
        final_text="",
        should_retire=False,
    )
    agent._codex_session.run_turn.return_value = error_turn
    run_codex_app_server_turn(
        agent,
        user_message="hi",
        original_user_message="hi",
        messages=[{"role": "user", "content": "hi"}],
        effective_task_id="task-2",
    )
    assert captured
    error_record = captured[-1]
    assert error_record.turn_exit_reason == "codex_error"


def test_codex_turn_exit_reason_classifies_non_response_exits():
    from agent.turn_ledger import TurnOutcomeRecord

    for fields, expected in (
        ({"interrupted": True, "final_text": "partial"}, "codex_interrupted"),
        ({"should_retire": True, "final_text": "done"}, "codex_retired"),
        ({"final_text": ""}, "codex_no_response"),
    ):
        captured = []
        agent = _make_agent(session_db=None)
        agent._session_db = _CapturingDB(captured)
        turn_fields = {
            "interrupted": False,
            "error": None,
            "thread_id": "thread-1",
            "turn_id": "opaque-id",
            "projected_messages": [],
            "tool_iterations": 0,
            "final_text": "ok",
            "should_retire": False,
        }
        turn_fields.update(fields)
        turn = SimpleNamespace(**turn_fields)
        agent._codex_session.run_turn.return_value = turn

        run_codex_app_server_turn(
            agent,
            user_message="hello",
            original_user_message="hello",
            messages=[{"role": "user", "content": "hello"}],
            effective_task_id="task-1",
        )

        assert isinstance(captured[-1], TurnOutcomeRecord)
        assert captured[-1].turn_exit_reason == expected


class _CapturingDB:
    def __init__(self, sink):
        self._sink = sink

    def record_turn_outcome(self, record):
        self._sink.append(record)


def test_codex_turn_persists_each_message_exactly_once():
    """The user turn (flushed at turn start) must not be duplicated; the
    projected assistant message must land once.  Uses a real SessionDB and the
    real AIAgent._flush_messages_to_session_db to prove no #860/#42039
    duplicate-write regression on the codex path."""
    tmp = tempfile.mkdtemp(prefix="codex_persist_")
    try:
        db = SessionDB(Path(tmp) / "state.db")
        sid = "sess-codex-once"
        db.create_session(session_id=sid, source="telegram", model="codex")

        # Real agent bound to this DB/session, minimal construction.
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=db,
            session_id=sid,
        )
        agent._session_db_created = True
        agent._codex_session = MagicMock()
        agent._codex_session.run_turn.return_value = _make_turn()
        agent.tool_progress_callback = None

        # Model the real flow: the inbound user turn is flushed at turn start
        # (turn_context._persist_session) on the SAME `messages` list the codex
        # path later reuses. That flush stamps _DB_PERSISTED_MARKER on the user
        # dict, so the codex-path flush skips it — no duplicate.
        user_msg = {"role": "user", "content": "USER_TURN"}
        messages = [user_msg]
        agent._flush_messages_to_session_db(messages)  # turn-start flush

        result = run_codex_app_server_turn(
            agent,
            user_message="USER_TURN",
            original_user_message="USER_TURN",
            messages=messages,
            effective_task_id="task-1",
        )
        assert result["agent_persisted"] is True

        rows = db.get_messages(sid, include_inactive=True)
        contents = [r["content"] for r in rows]
        # Exactly one user turn, exactly one assistant turn — no duplicates.
        assert contents.count("USER_TURN") == 1, contents
        assert contents.count("CODEX_ASSISTANT") == 1, contents
        # session_search can now see the codex conversation.
        hits = {r["session_id"] for r in db.search_messages("CODEX_ASSISTANT")}
        assert sid in hits
    finally:
        import shutil

        shutil.rmtree(tmp)


class TestGatewayPersistedResolution:
    """The gateway default must preserve standard-runtime skip-db behaviour."""

    @staticmethod
    def _resolve_persistence_block(agent_result, session_db_present):
        # gateway/run.py persistence block:
        #   agent_persisted = agent_result.get("agent_persisted", self._session_db is not None)
        return agent_result.get("agent_persisted", session_db_present)

    @staticmethod
    def _resolve_passthrough(result_holder0):
        # gateway/run.py result_holder passthrough:
        #   result_holder[0].get("agent_persisted", True) if result_holder[0] else True
        return result_holder0.get("agent_persisted", True) if result_holder0 else True

    def test_codex_result_keeps_gateway_skip(self):
        # Codex now self-persists → gateway must SKIP (agent_persisted True).
        codex = {"agent_persisted": True}
        assert self._resolve_persistence_block(codex, True) is True
        assert self._resolve_persistence_block(codex, False) is True
        assert self._resolve_passthrough(codex) is True

    def test_standard_runtime_preserves_skip_db(self):
        # Standard runtime omits the key → old behaviour: skip iff DB present.
        standard = {"final_response": "ok"}
        assert self._resolve_persistence_block(standard, True) is True
        assert self._resolve_persistence_block(standard, False) is False
        assert self._resolve_passthrough(standard) is True

    def test_missing_result_holder_defaults_persisted(self):
        assert self._resolve_passthrough(None) is True
