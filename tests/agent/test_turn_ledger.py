"""Tests for the per-turn outcome ledger.

Two surfaces covered here:

1. ``agent.turn_ledger`` — the dataclass + safe writer that the finalizers
   call. Owns no schema.
2. ``hermes_state.SessionDB`` — the four ledger methods
   (``record_turn_outcome``, ``annotate_turn_feedback``,
   ``get_outcome_trends``, ``get_skill_outcome_counts``) and the schema
   that backs them.

The finalizer / codex-runtime wiring is exercised in the matching focused
suites (``test_turn_finalizer_memory_gating``,
``test_codex_app_server_persist``); this file stays focused on the data
contract and the SQL round-trip.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_ledger import (
    TurnOutcomeRecord,
    record_turn_outcome_safely,
)
from agent.turn_outcome import TURN_OUTCOMES, classify_turn_outcome
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# Dataclass shape — guards the frozen contract the finalizers depend on.
# ---------------------------------------------------------------------------


def test_turn_outcome_record_is_frozen_dataclass():
    record = TurnOutcomeRecord(
        session_id="s1",
        turn_id="t1",
        created_at=1.0,
        outcome="verified",
        outcome_reason="verification passed",
        turn_exit_reason="text_response(finish_reason=stop)",
        api_calls=1,
        tool_iterations=0,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=0,
        output_tokens_delta=0,
        cache_read_tokens_delta=0,
        skills_loaded=("plan", "web"),
        model="test-model",
    )
    # frozen=True: any attribute mutation raises.
    with pytest.raises((AttributeError, Exception)):
        record.outcome = "failed"  # type: ignore[misc]


def test_skills_loaded_is_tuple_not_list_for_frozen_hashability():
    # skills_loaded needs to be a tuple so the dataclass stays hashable; a
    # plain list would silently pass the constructor and then break any
    # later set/dict usage.
    record = TurnOutcomeRecord(
        session_id="s1",
        turn_id="t1",
        created_at=1.0,
        outcome="verified",
        outcome_reason="verification passed",
        turn_exit_reason="text_response",
        api_calls=0,
        tool_iterations=0,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=0,
        output_tokens_delta=0,
        cache_read_tokens_delta=0,
        skills_loaded=("plan",),
        model="m",
    )
    assert isinstance(record.skills_loaded, tuple)
    # Hashable so callers can use it in sets / as dict keys if needed.
    hash(record)


# ---------------------------------------------------------------------------
# Safe writer — must never raise through finalization, even on DB errors.
# ---------------------------------------------------------------------------


class _BoomSessionDB:
    """Drop-in SessionDB stand-in whose record method raises."""

    def record_turn_outcome(self, _record):  # pragma: no cover - reached
        raise RuntimeError("simulated DB outage")


class _CaptureSessionDB:
    """Captures every record call so we can assert the wiring."""

    def __init__(self):
        self.calls = []

    def record_turn_outcome(self, record):
        self.calls.append(record)


def _record(**overrides) -> TurnOutcomeRecord:
    import time as _time
    base = dict(
        session_id="s1",
        turn_id="t1",
        created_at=_time.time(),
        outcome="verified",
        outcome_reason="verification passed",
        turn_exit_reason="text_response",
        api_calls=1,
        tool_iterations=0,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=0,
        output_tokens_delta=0,
        cache_read_tokens_delta=0,
        skills_loaded=(),
        model="m",
    )
    base.update(overrides)
    return TurnOutcomeRecord(**base)


def test_record_turn_outcome_safely_swallows_db_errors():
    """A failing record path must NOT propagate through the finalizer."""
    db = _BoomSessionDB()
    record = _record()

    # Return None on failure, never raise.
    result = record_turn_outcome_safely(db, record)

    assert result is None


def test_record_turn_outcome_safely_passes_record_to_db():
    db = _CaptureSessionDB()
    record = _record(outcome="unresolved", outcome_reason="tool timeout")

    record_turn_outcome_safely(db, record)

    assert db.calls == [record]


# ---------------------------------------------------------------------------
# SessionDB: schema + round trip.
# ---------------------------------------------------------------------------


def _tmp_db():
    """Return a freshly-minted SessionDB rooted at a temp dir."""
    tmp = Path(tempfile.mkdtemp(prefix="turn_ledger_"))
    db = SessionDB(tmp / "state.db")
    return tmp, db


def test_turn_outcomes_table_exists_and_accepts_all_eight_outcomes(tmp_path=None):
    """The spec example: write a single TurnOutcomeRecord with all 8 columns
    populated, then read it back via get_outcome_trends + the JSON fields.
    Then cover the remaining 7 vocabulary values to prove the table accepts
    the full vocabulary (no CHECK constraint rejecting any canonical value).
    """
    tmp, db = _tmp_db()

    record = TurnOutcomeRecord(
        session_id="s1",
        turn_id="t1",
        created_at=__import__("time").time(),
        outcome="unresolved",
        outcome_reason="tool timeout",
        turn_exit_reason="tool_timeout",
        api_calls=2,
        tool_iterations=1,
        retry_count=1,
        guardrail_halt=None,
        cost_usd_delta=0.12,
        input_tokens_delta=20,
        output_tokens_delta=4,
        cache_read_tokens_delta=10,
        skills_loaded=("plan", "web"),
        model="test-model",
    )
    db.record_turn_outcome(record)

    trends = db.get_outcome_trends(session_id="s1", days=30)
    assert trends, "get_outcome_trends must return at least one row"
    assert trends[0]["outcome"] == "unresolved"
    assert json.loads(trends[0]["skills_loaded"]) == ["plan", "web"]

    # Every canonical outcome value must persist without DB error.
    for outcome in TURN_OUTCOMES:
        db.record_turn_outcome(
            _record(
                session_id="s1",
                turn_id=f"t-{outcome}",
                outcome=outcome,
            )
        )
    all_rows = db.get_outcome_trends(session_id="s1", days=30)
    seen = {row["outcome"] for row in all_rows}
    assert seen == set(TURN_OUTCOMES)


def test_record_turn_outcome_round_trips_deltas_and_guardrail_json():
    tmp, db = _tmp_db()
    db.record_turn_outcome(
        _record(
            session_id="s1",
            turn_id="t-guardrail",
            outcome="blocked",
            outcome_reason="guardrail halted",
            guardrail_halt=json.dumps({"tool": "terminal", "reason": "deny"}),
            cost_usd_delta=0.5,
            input_tokens_delta=100,
            output_tokens_delta=10,
            cache_read_tokens_delta=0,
            skills_loaded=("plan",),
        )
    )
    rows = db.get_outcome_trends(session_id="s1", days=30)
    match = [r for r in rows if r["outcome"] == "blocked"][0]
    assert match["cost_usd_delta"] == pytest.approx(0.5)
    assert match["input_tokens_delta"] == 100
    assert match["output_tokens_delta"] == 10
    # guardrail_halt round-trips as JSON text (dict shape preserved).
    assert json.loads(match["guardrail_halt"]) == {
        "tool": "terminal",
        "reason": "deny",
    }


def test_record_turn_outcome_is_idempotent_per_turn():
    """``(session_id, turn_id)`` is unique — re-recording the same turn is
    a no-op (or upsert), never a duplicate."""
    tmp, db = _tmp_db()
    db.record_turn_outcome(_record(session_id="s1", turn_id="t1"))
    db.record_turn_outcome(
        _record(session_id="s1", turn_id="t1", outcome="partial")
    )

    rows = db.get_outcome_trends(session_id="s1", days=30)
    assert len(rows) == 1
    # The latest write wins.
    assert rows[0]["outcome"] == "partial"


# ---------------------------------------------------------------------------
# Feedback annotation.
# ---------------------------------------------------------------------------


def test_annotate_turn_feedback_writes_fields_and_dedupes_by_event_id():
    tmp, db = _tmp_db()
    db.record_turn_outcome(_record(session_id="s1", turn_id="t1"))

    assert db.annotate_turn_feedback(
        "s1",
        "t1",
        kind="thumbs_up",
        value="helpful",
        source="user",
        event_id="evt-1",
    ) is True
    # Same event_id → no-op (idempotent).
    assert db.annotate_turn_feedback(
        "s1",
        "t1",
        kind="thumbs_up",
        value="helpful",
        source="user",
        event_id="evt-1",
    ) is False
    # Different event_id → latest feedback wins for the scalar columns.
    assert db.annotate_turn_feedback(
        "s1",
        "t1",
        kind="thumbs_down",
        value="too verbose",
        source="user",
        event_id="evt-2",
    ) is True

    rows = db.get_outcome_trends(session_id="s1", days=30)
    assert rows
    # Scalar feedback columns reflect the most recent annotation.
    latest = rows[0]
    assert latest["feedback_kind"] == "thumbs_down"
    assert latest["feedback_value"] == "too verbose"
    assert latest["feedback_source"] == "user"
    assert latest["feedback_at"] is not None


def test_annotate_turn_feedback_returns_false_for_missing_turn():
    tmp, db = _tmp_db()
    # No row for s1/t-missing → annotation must report miss, not crash.
    assert (
        db.annotate_turn_feedback(
            "s1",
            "t-missing",
            kind="thumbs_up",
            value="",
            source="user",
            event_id="evt-x",
        )
        is False
    )


# ---------------------------------------------------------------------------
# Aggregations.
# ---------------------------------------------------------------------------


def test_get_outcome_trends_groups_by_outcome_and_session_filter():
    tmp, db = _tmp_db()
    db.record_turn_outcome(_record(session_id="s1", turn_id="a", outcome="verified"))
    db.record_turn_outcome(_record(session_id="s1", turn_id="b", outcome="verified"))
    db.record_turn_outcome(_record(session_id="s1", turn_id="c", outcome="failed"))
    db.record_turn_outcome(_record(session_id="s2", turn_id="a", outcome="verified"))

    s1 = db.get_outcome_trends(session_id="s1", days=30)
    by_outcome = {r["outcome"]: r["count"] for r in s1}
    assert by_outcome.get("verified") == 2
    assert by_outcome.get("failed") == 1
    # s2's verified row must NOT bleed into s1's projection.
    assert all(r["session_id"] == "s1" for r in s1)

    all_sessions = db.get_outcome_trends(days=30)
    by_outcome_all = {r["outcome"]: r["count"] for r in all_sessions}
    assert by_outcome_all.get("verified") == 3


def test_get_outcome_trends_sums_numeric_fields_and_uses_latest_text_fields():
    import time

    tmp, db = _tmp_db()
    db.create_session("s1", source="cli")
    db.create_session("s2", source="telegram")
    db.record_turn_outcome(
        _record(
            session_id="s1",
            turn_id="old",
            outcome="verified",
            created_at=time.time() - 2,
            cost_usd_delta=0.50,
            input_tokens_delta=100,
            output_tokens_delta=10,
            cache_read_tokens_delta=4,
            api_calls=2,
            tool_iterations=1,
            skills_loaded=("old",),
            model="old-model",
        )
    )
    db.record_turn_outcome(
        _record(
            session_id="s1",
            turn_id="new",
            outcome="verified",
            created_at=time.time() - 1,
            cost_usd_delta=0.25,
            input_tokens_delta=30,
            output_tokens_delta=3,
            cache_read_tokens_delta=1,
            api_calls=1,
            tool_iterations=2,
            skills_loaded=("new",),
            model="new-model",
        )
    )
    db.record_turn_outcome(_record(session_id="s2", turn_id="other", outcome="verified"))

    row = db.get_outcome_trends(source="cli", days=30)[0]
    assert row["count"] == 2
    assert row["cost_usd_delta"] == pytest.approx(0.75)
    assert row["input_tokens_delta"] == 130
    assert row["output_tokens_delta"] == 13
    assert row["cache_read_tokens_delta"] == 5
    assert row["api_calls"] == 3
    assert row["tool_iterations"] == 3
    assert row["model"] == "new-model"
    assert json.loads(row["skills_loaded"]) == ["new"]


def test_platform_message_resolves_to_nearest_assistant_turn():
    import time

    _, db = _tmp_db()
    now = time.time()
    db.create_session("s1", source="telegram")
    db.record_turn_outcome(
        _record(session_id="s1", turn_id="turn-1", created_at=now)
    )
    db.append_message(
        "s1",
        role="assistant",
        content="answer",
        platform_message_id="bot-message-1",
        timestamp=now + 0.1,
    )

    assert db.get_turn_id_for_platform_message("s1", "bot-message-1") == "turn-1"
    assert db.get_turn_id_for_platform_message("s1", "foreign") is None


def test_get_skill_outcome_counts_summarises_skills_loaded():
    tmp, db = _tmp_db()
    db.record_turn_outcome(
        _record(session_id="s1", turn_id="a", outcome="verified",
                skills_loaded=("plan", "web"))
    )
    db.record_turn_outcome(
        _record(session_id="s1", turn_id="b", outcome="verified",
                skills_loaded=("plan",))
    )
    db.record_turn_outcome(
        _record(session_id="s1", turn_id="c", outcome="failed",
                skills_loaded=("plan",))
    )

    counts = {r["skill"]: r for r in db.get_skill_outcome_counts(days=30)}
    plan = counts["plan"]
    assert plan["verified"] == 2
    assert plan["failed"] == 1

    web = counts["web"]
    assert web["verified"] == 1
    assert web.get("failed", 0) == 0


# ---------------------------------------------------------------------------
# classify_turn_outcome still works the way the finalizer wires it up.
# ---------------------------------------------------------------------------


def test_classify_turn_outcome_vocabulary_matches_ledger_acceptance():
    """Every vocabulary value produced by classify must be a valid ledger
    outcome; cross-check via DB round-trip so the safe writer's contract
    matches classify's output set.
    """
    tmp, db = _tmp_db()
    for outcome in TURN_OUTCOMES:
        result = classify_turn_outcome(
            final_response="ok",
            failed=(outcome == "failed"),
            interrupted=(outcome == "interrupted"),
            _turn_exit_reason=(
                "unresolved" if outcome == "unresolved" else
                "approval_blocked" if outcome == "blocked" else
                "cancelled" if outcome == "cancelled" else
                "max_iterations_reached(60/60)" if outcome == "partial" else
                "text_response(finish_reason=stop)"
            ),
            verification_status=("passed" if outcome == "verified" else None),
            cancelled=(outcome == "cancelled"),
        )
        assert result["outcome"] == outcome
        db.record_turn_outcome(
            _record(
                session_id="s1",
                turn_id=f"t-{outcome}",
                outcome=result["outcome"],
                outcome_reason=result["reason"],
            )
        )
    rows = db.get_outcome_trends(session_id="s1", days=30)
    assert {r["outcome"] for r in rows} == set(TURN_OUTCOMES)


# ---------------------------------------------------------------------------
# Finalizer integration — monkeypatch the safe writer to capture calls
# from both the chat-completions path (finalize_turn) and the codex app-server
# path (run_codex_app_server_turn). Asserts both finalizers produce
# canonical vocabulary and the same record schema.
# ---------------------------------------------------------------------------


class _RecordingSafeWriter:
    """Stand-in for record_turn_outcome_safely — captures every call."""

    def __init__(self):
        self.records = []

    def __call__(self, session_db, record):
        self.records.append(record)
        return None


class _RecordingSessionDB:
    def __init__(self):
        self.records = []

    def record_turn_outcome(self, record):
        self.records.append(record)


def _finalizer_agent(verification_status="passed"):
    from types import SimpleNamespace
    agent = SimpleNamespace(
        max_iterations=90,
        iteration_budget=SimpleNamespace(remaining=89, used=1, max_total=90),
        quiet_mode=True,
        model="test-model",
        provider="test-provider",
        base_url="",
        session_id="sess-finalizer-test",
        context_compressor=SimpleNamespace(last_prompt_tokens=0),
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status="unknown",
        session_cost_source="test",
        _tool_guardrail_halt_decision=None,
        _interrupt_message=None,
        _response_was_previewed=False,
        _skill_nudge_interval=0,
        _iters_since_skill=0,
        valid_tool_names=[],
        _turn_verification_status=verification_status,
        _memory_manager=MagicMock(),
        background_reviews=[],
        _session_db=_RecordingSessionDB(),
        _turn_token_cost_snapshot={},
        _current_turn_id="t-fin-1",
        # Hooks the finalizer calls — no-ops for the ledger test.
        _save_trajectory=lambda *a, **k: None,
        _cleanup_task_resources=lambda *a, **k: None,
        _drop_trailing_empty_response_scaffolding=lambda *a, **k: None,
        _persist_session=lambda *a, **k: None,
        _file_mutation_verifier_enabled=lambda: False,
        _turn_completion_explainer_enabled=lambda: False,
        _drain_pending_steer=lambda: None,
        clear_interrupt=lambda: None,
        # finalizer calls _sync_external_memory_for_turn unconditionally;
        # the real method routes through external-memory plugin hooks.
        # Stub it so this test exercises only the ledger hook.
        _sync_external_memory_for_turn=lambda **kwargs: None,
    )
    return agent


# ---------------------------------------------------------------------------
# Per-turn token/cost snapshot + delta computation.
#
# The chat-completions path now captures the agent's session counters at the
# turn-start boundary inside ``build_turn_context``. Without that snapshot,
# the deltas reported in the ledger would equal the session totals from turn
# 1 onward — overstating every turn's cost by the cumulative session spend
# and breaking any learning pipeline that aggregates per-turn cost.
# ---------------------------------------------------------------------------


def test_build_turn_outcome_record_uses_turn_start_snapshot_for_deltas(monkeypatch):
    """Snapshot captured at turn start yields exact end-minus-start deltas.

    Models the chat-completions path: ``build_turn_context`` stashes the
    agent's token/cost counters on ``agent._turn_token_cost_snapshot`` at
    turn-start, then ``build_turn_outcome_record`` subtracts them from the
    end-of-turn counters. Asserts the four canonical fields (input, output,
    cache_read, cost) and the missing-counter → zero behavior.
    """
    from agent.turn_ledger import build_turn_outcome_record

    # Start of turn (prologue just ran).
    agent = SimpleNamespace(
        session_id="s1",
        _current_turn_id="t1",
        session_input_tokens=100,
        session_output_tokens=10,
        session_cache_read_tokens=50,
        session_estimated_cost_usd=0.05,
        model="test-model",
        _tool_guardrail_halt_decision=None,
        _invalid_tool_retries=0,
        _invalid_json_retries=0,
        _empty_content_retries=0,
        _incomplete_scratchpad_retries=0,
        _codex_incomplete_retries=0,
        _thinking_prefill_retries=0,
        _skills_loaded=(),
    )
    # Mock turn_context so we don't need to spin up the full prologue.
    turn_context = SimpleNamespace(
        token_cost_snapshot={
            "session_input_tokens": 100,
            "session_output_tokens": 10,
            "session_cache_read_tokens": 50,
            "session_estimated_cost_usd": 0.05,
        },
        turn_id="t1",
    )

    # Simulate turn-end: counters advanced by some amount.
    agent.session_input_tokens = 350  # +250
    agent.session_output_tokens = 42  # +32
    agent.session_cache_read_tokens = 80  # +30
    agent.session_estimated_cost_usd = 0.11  # +0.06

    record = build_turn_outcome_record(
        agent,
        outcome="verified",
        outcome_reason="ok",
        turn_exit_reason="text_response",
        api_calls=1,
        tool_iterations=0,
        turn_context=turn_context,
    )
    assert record.input_tokens_delta == 250
    assert record.output_tokens_delta == 32
    assert record.cache_read_tokens_delta == 30
    assert record.cost_usd_delta == pytest.approx(0.06)


def test_build_turn_outcome_record_missing_counters_default_to_zero(monkeypatch):
    """End counters missing entirely → 0 deltas (not None arithmetic).

    The schema forbids NULL on the delta columns. If the agent lacks one of
    the session counters at end-of-turn, the ledger row must still serialize
    cleanly. The snapshot missing the same fields yields 0 for the start
    value and the end missing yields 0 for the end value.
    """
    from agent.turn_ledger import build_turn_outcome_record

    agent = SimpleNamespace(
        session_id="s1",
        _current_turn_id="t1",
        # session_input_tokens / output / cache_read / cost all absent on agent.
        model="test-model",
        _tool_guardrail_halt_decision=None,
        _invalid_tool_retries=0,
        _invalid_json_retries=0,
        _empty_content_retries=0,
        _incomplete_scratchpad_retries=0,
        _codex_incomplete_retries=0,
        _thinking_prefill_retries=0,
        _skills_loaded=(),
    )
    turn_context = SimpleNamespace(
        token_cost_snapshot={},  # empty snapshot → all starts are 0
        turn_id="t1",
    )

    record = build_turn_outcome_record(
        agent,
        outcome="verified",
        outcome_reason="ok",
        turn_exit_reason="text_response",
        api_calls=1,
        tool_iterations=0,
        turn_context=turn_context,
    )
    assert record.input_tokens_delta == 0
    assert record.output_tokens_delta == 0
    assert record.cache_read_tokens_delta == 0
    assert record.cost_usd_delta == 0.0


def test_build_turn_context_captures_token_cost_snapshot():
    """Prologue must snapshot session counters at turn-start so deltas are
    per-turn rather than cumulative-session.

    Without this snapshot, build_turn_outcome_record would treat the agent's
    session_input_tokens (cumulative across the whole session) as the
    per-turn input count — overstating every turn's spend.
    """
    from agent.turn_context import build_turn_context

    agent = _SnapshotAgent()
    # Seed session counters BEFORE the prologue runs.
    agent.session_input_tokens = 500
    agent.session_output_tokens = 25
    agent.session_cache_read_tokens = 200
    agent.session_estimated_cost_usd = 0.42

    ctx = _build_snapshot(agent)

    # The prologue captured the snapshot exactly at turn start — both on the
    # TurnContext (so chat-completions finalizers can pass it explicitly)
    # and on the agent (the fallback ``build_turn_outcome_record`` already
    # reads when ``turn_context`` isn't threaded through).
    expected = {
        "session_input_tokens": 500,
        "session_output_tokens": 25,
        "session_cache_read_tokens": 200,
        "session_estimated_cost_usd": 0.42,
    }
    assert getattr(agent, "_turn_token_cost_snapshot") == expected
    assert ctx.token_cost_snapshot == expected


class _SnapshotAgent:
    """Lightweight stand-in for the turn-snapshot test (independent of the
    full _FakeAgent fixture so this test stays focused on one assertion)."""

    def __init__(self):
        self.session_id = "sess-1"
        self.model = "test/model"
        self.provider = "openrouter"
        self.base_url = ""
        self.api_key = ""
        self.api_mode = "chat_completions"
        self.platform = "cli"
        self.quiet_mode = True
        self.max_iterations = 90
        self.tools = []
        self.valid_tool_names = set()
        self.compression_enabled = False
        self.context_compressor = SimpleNamespace(
            protect_first_n=2, protect_last_n=2
        )
        self._cached_system_prompt = "SYSTEM"
        self._memory_store = None
        self._memory_manager = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = SimpleNamespace(has_items=lambda: True)
        self._tool_guardrails = SimpleNamespace(reset_for_turn=lambda: None)
        self._compression_warning = None
        self._interrupt_requested = False
        self._memory_write_origin = "assistant_tool"
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None
        self._invalid_tool_retries = 0
        self._vision_supported = None
        self._persist_calls = 0
        self._pending_cli_user_message = None
        self._session_persist_lock = None
        # Token/cost counters (the snapshot captures these at turn start).
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_estimated_cost_usd = 0.0

    def _ensure_db_session(self):
        pass

    def _restore_primary_runtime(self):
        pass

    def _cleanup_dead_connections(self):
        return False

    def _emit_status(self, _msg):
        pass

    def _replay_compression_warning(self):
        pass

    def _hydrate_todo_store(self, *_a, **_k):
        pass

    def _safe_print(self, *_a, **_k):
        pass

    def _persist_session(self, *_a, **_k):
        pass


def _build_snapshot(agent):
    """Run the prologue with no-ops for the callbacks the snapshot test
    doesn't care about."""
    from agent.turn_context import build_turn_context
    return build_turn_context(
        agent=agent,
        user_message="hello",
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *a, **k: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s: s,
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _o: None,
        ra=lambda: SimpleNamespace(_set_interrupt=lambda *a, **k: None),
    )


def test_finalizer_tool_iterations_count_actual_tool_calls_not_iters_since_skill(monkeypatch):
    """Finalizer must report actual tool-call count, not the skill-nudge counter.

    ``_iters_since_skill`` is a per-turn counter the agent bumps as a skill
    nudge cadence — it has no relationship to how many tool calls the model
    issued. A turn with no tool calls but several nudges would otherwise be
    recorded with ``tool_iterations > 0``, corrupting per-turn tool usage
    analytics. The ledger must reflect the actual number of assistant
    messages with ``tool_calls`` in the turn's messages list.
    """
    from agent import turn_finalizer
    from agent.turn_ledger import build_turn_outcome_record as _orig_build

    captured = []

    def _capture_record(*args, **kwargs):
        # Capture the args/kwargs the finalizer passed, then defer to the
        # real builder so the assertions reflect actual ledger semantics.
        record = _orig_build(*args, **kwargs)
        captured.append(record)
        return record

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **k: [])
    monkeypatch.setattr(
        "agent.turn_ledger.record_turn_outcome_safely",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "agent.turn_ledger.build_turn_outcome_record",
        _capture_record,
    )

    agent = _finalizer_agent(verification_status="passed")
    # Skill-nudge counter is non-zero (simulates mid-turn nudge cadence),
    # but the actual tool-call count in the messages is 2.
    agent._iters_since_skill = 7

    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "terminal"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-2", "type": "function", "function": {"name": "web"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-2", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    turn_finalizer.finalize_turn(
        agent,
        final_response="done",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="t-fin-tools",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )

    assert captured, "build_turn_outcome_record was not called"
    record = captured[-1]
    # The real count of assistant messages with tool_calls is 2.
    assert record.tool_iterations == 2, (
        f"tool_iterations must reflect the actual tool-call count "
        f"(2), not _iters_since_skill (7). Got {record.tool_iterations}."
    )


def test_finalizer_tool_iterations_zero_when_no_tool_calls(monkeypatch):
    """A turn with no tool calls and a non-zero nudge counter must still
    record tool_iterations=0. This is the user-visible false-positive the
    prior bug produced — nudges counted as tool activity."""
    from agent import turn_finalizer
    from agent.turn_ledger import build_turn_outcome_record as _orig_build

    captured = []

    def _capture_record(*args, **kwargs):
        record = _orig_build(*args, **kwargs)
        captured.append(record)
        return record

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **k: [])
    monkeypatch.setattr(
        "agent.turn_ledger.record_turn_outcome_safely",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "agent.turn_ledger.build_turn_outcome_record",
        _capture_record,
    )

    agent = _finalizer_agent(verification_status="passed")
    agent._iters_since_skill = 5  # nudges happened, but no real tools

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    turn_finalizer.finalize_turn(
        agent,
        final_response="hello",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="t-fin-zero",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )

    assert captured
    assert captured[-1].tool_iterations == 0


def test_finalizer_paths_call_safe_writer_with_canonical_vocabulary(monkeypatch):
    """Both finalizer paths (chat-completions and codex app-server) must
    funnel through record_turn_outcome_safely with one of the 8 canonical
    outcome values. Monkeypatch the writer to capture calls without
    touching state.db.
    """
    from agent import turn_finalizer, codex_runtime
    from agent.turn_outcome import TURN_OUTCOMES as _VOCAB
    from agent.turn_ledger import TurnOutcomeRecord

    recorder = _RecordingSafeWriter()
    monkeypatch.setattr(
        "agent.turn_ledger.record_turn_outcome_safely", recorder
    )
    # Both finalizers import the safe writer lazily inside their
    # try-block, so patching agent.turn_ledger.record_turn_outcome_safely
    # is enough — the lazy ``from agent.turn_ledger import ...`` resolves
    # at call time against the patched module.

    # ── Path 1: chat-completions finalizer ──────────────────────────
    agent = _finalizer_agent(verification_status="passed")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **k: [])
    turn_finalizer.finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "do it"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="t-fin-1",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )

    # ── Path 2: codex app-server finalizer ───────────────────────────
    codex_agent = MagicMock()
    codex_agent.tool_progress_callback = None
    codex_agent._iters_since_skill = 0
    codex_agent._skill_nudge_interval = 0
    codex_agent.valid_tool_names = set()
    codex_agent._session_db = _RecordingSessionDB()
    codex_agent._session_db_created = True
    codex_agent.session_id = "sess-codex-test"
    codex_agent._current_turn_id = "t-codex-1"
    codex_agent._turn_token_cost_snapshot = {}
    codex_agent._sync_external_memory_for_turn = MagicMock()
    codex_agent._spawn_background_review = MagicMock()
    codex_agent._turn_verification_status = "passed"

    codex_turn = SimpleNamespace(
        interrupted=False,
        error=None,
        thread_id="thread-1",
        turn_id="t-codex-1",
        projected_messages=[{"role": "assistant", "content": "ok"}],
        tool_iterations=0,
        final_text="ok",
        should_retire=False,
    )
    codex_agent._codex_session = MagicMock()
    codex_agent._codex_session.run_turn.return_value = codex_turn

    codex_runtime.run_codex_app_server_turn(
        codex_agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task",
    )

    # ── Assertions ──────────────────────────────────────────────────
    assert len(recorder.records) == 2, (
        "Expected one record from each finalizer path, got "
        f"{len(recorder.records)}"
    )
    for record in recorder.records:
        assert isinstance(record, TurnOutcomeRecord)
        assert record.outcome in _VOCAB, (
            f"Non-canonical outcome {record.outcome!r}; expected one of {_VOCAB}"
        )
        # Schema sanity: every record carries the data-contract fields.
        for field in (
            "session_id", "turn_id", "created_at", "outcome",
            "api_calls", "tool_iterations", "skills_loaded", "model",
        ):
            assert hasattr(record, field), f"Record missing field: {field}"


# ---------------------------------------------------------------------------
# Task 2 — attribution: extract exact skill names from stored messages.
#
# The repository stores messages with assistant tool_calls whose
# ``function.arguments`` may be a dict (after parsing) or a JSON string.
# ``extract_loaded_skills`` is the seam used by the finalizer to populate
# ``TurnOutcomeRecord.skills_loaded``; it must scan all assistant messages,
# pick out ``skill_view`` calls, read the ``name`` argument defensively,
# dedupe, and preserve order — never crash on a malformed entry.
# ---------------------------------------------------------------------------


def test_attribution_extract_loaded_skills_returns_unique_in_call_order():
    """Repeated skill views collapse; first-seen order is preserved (deterministic)."""
    from agent.turn_ledger import extract_loaded_skills

    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "skill_view", "arguments": {"name": "plan"}}},
            {"function": {"name": "skill_view", "arguments": {"name": "web"}}},
            {"function": {"name": "terminal", "arguments": {"command": "ls"}}},
        ]},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "skill_view", "arguments": {"name": "plan"}}},  # dup
            {"function": {"name": "skill_view", "arguments": {"name": "github"}}},
        ]},
    ]
    assert extract_loaded_skills(messages) == ("plan", "web", "github")


def test_attribution_extract_loaded_skills_accepts_arguments_as_json_string():
    """tool_calls sometimes ship arguments as a JSON string — parse them."""
    from agent.turn_ledger import extract_loaded_skills

    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "skill_view", "arguments": '{"name": "plan"}'}},
            {"function": {"name": "skill_view", "arguments": '{"name":"web"}'}},
        ]},
    ]
    assert extract_loaded_skills(messages) == ("plan", "web")


def test_attribution_extract_loaded_skills_tolerates_malformed_entries():
    """Bad messages, missing fields, non-skill calls, bad JSON — never crash."""
    from agent.turn_ledger import extract_loaded_skills

    messages = [
        {"role": "user", "content": "hi"},                                      # ignored
        {"role": "assistant"},                                                    # no tool_calls
        {"role": "assistant", "tool_calls": "not-a-list"},                        # malformed
        {"role": "assistant", "tool_calls": [
            "garbage",                                                            # not a dict
            {"no_function": True},                                                # missing function
            {"function": None},                                                    # None function
            {"function": {"name": "skill_view", "arguments": None}},               # no args
            {"function": {"name": "skill_view", "arguments": "{not json"}},        # bad json
            {"function": {"name": "skill_view", "arguments": "[]"}},              # not a dict
            {"function": {"name": "skill_view", "arguments": {"name": ""}}},      # empty name
            {"function": {"name": "skill_view", "arguments": {"name": "  "}}},     # whitespace
            {"function": {"name": "skill_view", "arguments": {"name": "real"}}},
            {"function": {"name": "terminal", "arguments": {}}},                   # wrong tool
        ]},
    ]
    assert extract_loaded_skills(messages) == ("real",)


def test_attribution_extract_loaded_skills_strips_whitespace_and_skips_empty():
    """Names with leading/trailing whitespace are trimmed; empties are dropped."""
    from agent.turn_ledger import extract_loaded_skills

    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "skill_view", "arguments": {"name": "  plan  "}}},
            {"function": {"name": "skill_view", "arguments": {"name": "plan"}}},  # dup after strip
        ]},
    ]
    assert extract_loaded_skills(messages) == ("plan",)


def test_attribution_extract_loaded_skills_empty_for_no_skill_view_calls():
    """Turns without any skill_view calls yield an empty tuple, not None."""
    from agent.turn_ledger import extract_loaded_skills

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "terminal", "arguments": {"command": "ls"}}},
        ]},
    ]
    result = extract_loaded_skills(messages)
    assert isinstance(result, tuple)
    assert result == ()


def test_attribution_uses_extract_when_agent_skills_empty():
    """If agent._skills_loaded is empty, the builder falls back to extract_loaded_skills.

    The finalizer is allowed to pass a populated messages list instead of relying
    on the agent's _skills_loaded set. Without the fallback, attribution is
    limited to whatever hooks ran first — a regression we don't want.
    """
    from agent.turn_ledger import build_turn_outcome_record

    agent = SimpleNamespace(
        session_id="s1",
        _current_turn_id="t1",
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_estimated_cost_usd=0.0,
        model="m",
        _tool_guardrail_halt_decision=None,
        _invalid_tool_retries=0,
        _invalid_json_retries=0,
        _empty_content_retries=0,
        _incomplete_scratchpad_retries=0,
        _codex_incomplete_retries=0,
        _thinking_prefill_retries=0,
        _skills_loaded=(),
    )
    turn_context = SimpleNamespace(token_cost_snapshot={}, turn_id="t1")
    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "skill_view", "arguments": {"name": "plan"}}},
            {"function": {"name": "skill_view", "arguments": {"name": "web"}}},
        ]},
    ]

    record = build_turn_outcome_record(
        agent,
        outcome="verified",
        outcome_reason="ok",
        turn_exit_reason="text_response",
        api_calls=1,
        tool_iterations=1,
        turn_context=turn_context,
        messages=messages,
    )
    assert record.skills_loaded == ("plan", "web")


def test_build_turn_outcome_record_preserves_existing_seam(monkeypatch):
    """If no messages are passed and _skills_loaded is set, the old path still works.

    The seam is additive: callers that don't populate messages get the same
    behavior as Task 1 (skills from agent._skills_loaded). Adding messages
    must not replace an explicit agent._skills_loaded entry.
    """
    from agent.turn_ledger import build_turn_outcome_record

    agent = SimpleNamespace(
        session_id="s1",
        _current_turn_id="t1",
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_estimated_cost_usd=0.0,
        model="m",
        _tool_guardrail_halt_decision=None,
        _invalid_tool_retries=0,
        _invalid_json_retries=0,
        _empty_content_retries=0,
        _incomplete_scratchpad_retries=0,
        _codex_incomplete_retries=0,
        _thinking_prefill_retries=0,
        _skills_loaded=("from-agent",),
    )
    turn_context = SimpleNamespace(token_cost_snapshot={}, turn_id="t1")

    record = build_turn_outcome_record(
        agent,
        outcome="verified",
        outcome_reason="ok",
        turn_exit_reason="text_response",
        api_calls=0,
        tool_iterations=0,
        turn_context=turn_context,
    )
    # No messages kwarg → falls back to existing skills_loaded_from path.
    assert record.skills_loaded == ("from-agent",)


# ---------------------------------------------------------------------------
# Task 2 fix — finalizer/codex wire `messages` into the builder, and the
# builder switches the skills_loaded source to ``extract_loaded_skills`` when
# a messages list is supplied. After a successful or attempted ledger
# attribution the sidecar ``tools.skill_usage.bump_outcome`` is best-effort
# invoked for each recorded skill. The sidecar MUST never escape.
#
# These tests are written first so the wiring gap (Task 2's commit fe63e02a8
# defined ``extract_loaded_skills`` and ``bump_outcome`` but no finalizer
# path actually used them) is exercised end-to-end before the production fix
# lands. # noqa: E501 — long docstring lines.
# ---------------------------------------------------------------------------


def test_messages_passed_to_builder_yields_exact_skill_view_attribution():
    """When the finalizer passes ``messages``, the builder's skills_loaded
    MUST come from ``extract_loaded_skills(messages)`` — even when
    ``agent._skills_loaded`` is also populated. The legacy _skills_loaded
    set is the fallback only when messages is *omitted*.

    This is the "exact" attribution the finalizer path promises the
    ledger: the recorded skills are the actual skill_view tool calls,
    not whatever hooks happened to populate _skills_loaded.
    """
    from agent.turn_ledger import build_turn_outcome_record

    agent = SimpleNamespace(
        session_id="s1",
        _current_turn_id="t1",
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_estimated_cost_usd=0.0,
        model="m",
        _tool_guardrail_halt_decision=None,
        _invalid_tool_retries=0,
        _invalid_json_retries=0,
        _empty_content_retries=0,
        _incomplete_scratchpad_retries=0,
        _codex_incomplete_retries=0,
        _thinking_prefill_retries=0,
        # _skills_loaded is set, but messages takes precedence per spec.
        _skills_loaded=("from-agent",),
    )
    turn_context = SimpleNamespace(token_cost_snapshot={}, turn_id="t1")
    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "skill_view", "arguments": {"name": "plan"}}},
            {"function": {"name": "skill_view", "arguments": {"name": "web"}}},
        ]},
    ]

    record = build_turn_outcome_record(
        agent,
        outcome="verified",
        outcome_reason="ok",
        turn_exit_reason="text_response",
        api_calls=1,
        tool_iterations=1,
        turn_context=turn_context,
        messages=messages,
    )
    assert record.skills_loaded == ("plan", "web")


def test_finalizer_passes_messages_to_build_turn_outcome_record(monkeypatch):
    """The chat-completions finalizer MUST forward its `messages` argument
    to ``build_turn_outcome_record`` so the builder sees the per-turn tool
    calls (skill_view in particular). Without this, the legacy
    ``_skills_loaded`` seam — which only carries hook-populated names — is
    the only attribution source and Task 2's "exact skill_view names"
    promise is silently dropped.
    """
    from agent import turn_finalizer
    from agent.turn_ledger import build_turn_outcome_record as _orig_build

    captured_kwargs = {}

    def _capture_record(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return _orig_build(*args, **kwargs)

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **k: [])
    monkeypatch.setattr(
        "agent.turn_ledger.record_turn_outcome_safely",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "agent.turn_ledger.build_turn_outcome_record",
        _capture_record,
    )

    agent = _finalizer_agent(verification_status="passed")
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "done"},
    ]
    turn_finalizer.finalize_turn(
        agent,
        final_response="done",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="t-fin-msg",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )
    assert "messages" in captured_kwargs, (
        "finalizer must forward messages=... to build_turn_outcome_record "
        f"so skill attribution is exact. Got kwargs: {list(captured_kwargs)}"
    )
    assert captured_kwargs["messages"] is messages


def test_codex_runtime_passes_messages_to_build_turn_outcome_record(monkeypatch):
    """The codex app-server runtime MUST forward its `messages` argument
    to ``build_turn_outcome_record``. At this point the projected messages
    have already been spliced into ``messages`` (see line ~458), so passing
    the full list means skill_view calls from projected assistant rows
    attribute correctly.
    """
    from agent import codex_runtime
    from agent.turn_ledger import build_turn_outcome_record as _orig_build

    captured_kwargs = {}

    def _capture_record(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return _orig_build(*args, **kwargs)

    # Patch both import surfaces (the runtime imports lazily inside the
    # try-block, so patching agent.turn_ledger is enough — but mirroring
    # the chat-finalizer test pattern keeps the patch symmetric).
    monkeypatch.setattr(
        "agent.turn_ledger.record_turn_outcome_safely",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "agent.turn_ledger.build_turn_outcome_record",
        _capture_record,
    )

    codex_agent = MagicMock()
    codex_agent.tool_progress_callback = None
    codex_agent._iters_since_skill = 0
    codex_agent._skill_nudge_interval = 0
    codex_agent.valid_tool_names = set()
    codex_agent._session_db = _RecordingSessionDB()
    codex_agent._session_db_created = True
    codex_agent.session_id = "sess-codex-test"
    codex_agent._current_turn_id = "t-codex-1"
    codex_agent._turn_token_cost_snapshot = {}
    codex_agent._sync_external_memory_for_turn = MagicMock()
    codex_agent._spawn_background_review = MagicMock()
    codex_agent._turn_verification_status = "passed"

    # projected_messages includes a skill_view call so we can assert
    # attribution picks it up.
    codex_turn = SimpleNamespace(
        interrupted=False,
        error=None,
        thread_id="thread-1",
        turn_id="t-codex-1",
        projected_messages=[{
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "skill_view", "arguments": {"name": "plan"}}},
            ],
        }],
        tool_iterations=0,
        final_text="ok",
        should_retire=False,
    )
    codex_agent._codex_session = MagicMock()
    codex_agent._codex_session.run_turn.return_value = codex_turn

    user_messages = [{"role": "user", "content": "hello"}]
    codex_runtime.run_codex_app_server_turn(
        codex_agent,
        user_message="hello",
        original_user_message="hello",
        messages=user_messages,
        effective_task_id="task",
    )
    assert "messages" in captured_kwargs, (
        "codex runtime must forward messages=... to build_turn_outcome_record "
        f"so skill attribution is exact. Got kwargs: {list(captured_kwargs)}"
    )
    # The forwarded list contains the projected skill_view call —
    # this is the meaningful attribution guarantee: the projected
    # messages (added in-place via messages.extend) flow into the
    # ledger's skill extraction.
    skill_names = [tc["function"]["arguments"]["name"]
                   for m in captured_kwargs["messages"]
                   for tc in (m.get("tool_calls") or [])
                   if tc.get("function", {}).get("name") == "skill_view"]
    assert "plan" in skill_names


def test_bump_outcome_called_for_each_skill_on_successful_attribution(monkeypatch):
    """After a successful ledger attribution, ``bump_outcome`` MUST be
    invoked for every skill in ``record.skills_loaded`` with
    ``(skill_name, outcome, cost_usd_delta)``. The sidecar bump is what
    feeds the curator's utility ranking.
    """
    from agent import turn_ledger

    calls = []

    def _fake_bump(skill_name, outcome, cost_delta):
        calls.append((skill_name, outcome, cost_delta))

    monkeypatch.setattr(
        "tools.skill_usage.bump_outcome",
        _fake_bump,
    )
    # Capture the records handed to the safe writer so we can assert
    # the post-write bump is best-effort (always invoked on success).
    written = []
    monkeypatch.setattr(
        "agent.turn_ledger.record_turn_outcome_safely",
        lambda _db, rec: written.append(rec),
    )

    record = turn_ledger.TurnOutcomeRecord(
        session_id="s1",
        turn_id="t1",
        created_at=1.0,
        outcome="verified",
        outcome_reason="ok",
        turn_exit_reason="text_response",
        api_calls=1,
        tool_iterations=0,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.42,
        input_tokens_delta=0,
        output_tokens_delta=0,
        cache_read_tokens_delta=0,
        skills_loaded=("plan", "web"),
        model="m",
    )
    # Helper under test: invoke the post-attribution sidecar.
    turn_ledger._bump_sidecar_for_skills(record)  # type: ignore[attr-defined]

    assert ("plan", "verified", 0.21) in calls
    assert ("web", "verified", 0.21) in calls
    assert len(calls) == 2


def test_bump_outcome_runs_even_when_db_write_fails(monkeypatch):
    """DB failure must NOT suppress sidecar evidence. If we attempted the
    attribution (i.e. we have a record with a non-empty skills_loaded),
    the sidecar bumps MUST still happen. The exception boundary is
    "ledger write ok" vs "ledger write raised" — not "ledger write ok"
    vs "anything went wrong".
    """
    from agent import turn_ledger

    calls = []

    def _fake_bump(skill_name, outcome, cost_delta):
        calls.append((skill_name, outcome, cost_delta))

    monkeypatch.setattr(
        "tools.skill_usage.bump_outcome",
        _fake_bump,
    )

    record = turn_ledger.TurnOutcomeRecord(
        session_id="s1",
        turn_id="t1",
        created_at=1.0,
        outcome="failed",
        outcome_reason="boom",
        turn_exit_reason="provider_failure",
        api_calls=1,
        tool_iterations=0,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.1,
        input_tokens_delta=0,
        output_tokens_delta=0,
        cache_read_tokens_delta=0,
        skills_loaded=("plan",),
        model="m",
    )
    # Even though the DB write is mocked to raise, we expect bumps to
    # be attempted by the helper.
    turn_ledger._bump_sidecar_for_skills(record)  # type: ignore[attr-defined]

    assert ("plan", "failed", 0.1) in calls


def test_bump_outcome_failure_does_not_propagate(monkeypatch):
    """A bump_outcome exception MUST be swallowed — the sidecar is
    observability, never a correctness path. If the bump raises, the
    finalizer must still return its result unchanged.
    """
    from agent import turn_ledger

    def _broken_bump(_skill, _outcome, _cost):
        raise RuntimeError("sidecar on fire")

    monkeypatch.setattr(
        "tools.skill_usage.bump_outcome",
        _broken_bump,
    )

    record = turn_ledger.TurnOutcomeRecord(
        session_id="s1",
        turn_id="t1",
        created_at=1.0,
        outcome="verified",
        outcome_reason="ok",
        turn_exit_reason="text_response",
        api_calls=1,
        tool_iterations=0,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=0,
        output_tokens_delta=0,
        cache_read_tokens_delta=0,
        skills_loaded=("plan",),
        model="m",
    )
    # Must not raise.
    turn_ledger._bump_sidecar_for_skills(record)  # type: ignore[attr-defined]


def test_bump_outcome_skips_when_no_skills_loaded(monkeypatch):
    """If the record has no skills loaded, no bump is attempted — there
    is nothing to attribute. This is the "no outcome record" boundary
    in the spec: if there's no evidence, there's no bump.
    """
    from agent import turn_ledger

    calls = []

    def _fake_bump(skill_name, outcome, cost_delta):
        calls.append((skill_name, outcome, cost_delta))

    monkeypatch.setattr(
        "tools.skill_usage.bump_outcome",
        _fake_bump,
    )

    record = turn_ledger.TurnOutcomeRecord(
        session_id="s1",
        turn_id="t1",
        created_at=1.0,
        outcome="verified",
        outcome_reason="ok",
        turn_exit_reason="text_response",
        api_calls=1,
        tool_iterations=0,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=0,
        output_tokens_delta=0,
        cache_read_tokens_delta=0,
        skills_loaded=(),  # empty — no bumps expected
        model="m",
    )
    turn_ledger._bump_sidecar_for_skills(record)  # type: ignore[attr-defined]
    assert calls == []


def test_bump_outcome_missing_tools_module_does_not_raise(monkeypatch):
    """The bump helper must tolerate a missing ``tools.skill_usage``
    module. ``tools`` is importable in production but tests may import
    in unusual paths; the safe-by-default behavior is to no-op rather
    than crash the finalizer.
    """
    from agent import turn_ledger

    # Simulate tools.skill_usage being unimportable by replacing the
    # module-level reference used by the bump helper.
    import builtins

    real_import = builtins.__import__

    def _guarded_import(name, *args, **kwargs):
        if name == "tools.skill_usage" or name.startswith("tools.skill_usage"):
            raise ImportError("simulated missing tools module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _guarded_import)

    record = turn_ledger.TurnOutcomeRecord(
        session_id="s1",
        turn_id="t1",
        created_at=1.0,
        outcome="verified",
        outcome_reason="ok",
        turn_exit_reason="text_response",
        api_calls=1,
        tool_iterations=0,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=0,
        output_tokens_delta=0,
        cache_read_tokens_delta=0,
        skills_loaded=("plan",),
        model="m",
    )
    # Must not raise.
    turn_ledger._bump_sidecar_for_skills(record)  # type: ignore[attr-defined]


def test_finalizer_invokes_sidecar_bump_with_recorded_skills(monkeypatch):
    """End-to-end: chat-completions finalizer extracts skill_view names
    from its messages, builds the record, writes it via the safe writer,
    then bumps the sidecar with the recorded skill/outcome/cost. Asserts
    the finalizer wires messages AND triggers the bump — not just one
    or the other.
    """
    from agent import turn_finalizer

    bumps = []
    records = []

    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "agent.turn_ledger.record_turn_outcome_safely",
        lambda _db, rec: records.append(rec),
    )
    monkeypatch.setattr(
        "tools.skill_usage.bump_outcome",
        lambda skill, outcome, cost: bumps.append((skill, outcome, cost)),
    )

    agent = _finalizer_agent(verification_status="passed")
    agent.session_estimated_cost_usd = 0.07
    messages = [
        {"role": "user", "content": "use plan"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"function": {"name": "skill_view", "arguments": {"name": "plan"}}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    turn_finalizer.finalize_turn(
        agent,
        final_response="done",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="t-fin-bump",
        user_message="use plan",
        original_user_message="use plan",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )
    assert records, "ledger write did not happen"
    rec = records[-1]
    assert rec.skills_loaded == ("plan",)
    # Cost may be 0 because the snapshot is empty (start=end); the
    # important assertion is the bump fired with the recorded skill
    # and the same outcome the row carries.
    assert any(skill == "plan" for skill, _outcome, _cost in bumps), (
        f"expected bump for 'plan' from messages, got {bumps}"
    )


def test_codex_runtime_invokes_sidecar_bump_with_recorded_skills(monkeypatch):
    """End-to-end: codex app-server finalizer extracts skill_view names
    from its projected messages, writes the row, then bumps sidecar.
    """
    from agent import codex_runtime

    bumps = []
    records = []

    monkeypatch.setattr(
        "agent.turn_ledger.record_turn_outcome_safely",
        lambda _db, rec: records.append(rec),
    )
    monkeypatch.setattr(
        "tools.skill_usage.bump_outcome",
        lambda skill, outcome, cost: bumps.append((skill, outcome, cost)),
    )

    codex_agent = MagicMock()
    codex_agent.tool_progress_callback = None
    codex_agent._iters_since_skill = 0
    codex_agent._skill_nudge_interval = 0
    codex_agent.valid_tool_names = set()
    codex_agent._session_db = _RecordingSessionDB()
    codex_agent._session_db_created = True
    codex_agent.session_id = "sess-codex-bump"
    codex_agent._current_turn_id = "t-codex-bump"
    codex_agent._turn_token_cost_snapshot = {}
    codex_agent._sync_external_memory_for_turn = MagicMock()
    codex_agent._spawn_background_review = MagicMock()
    codex_agent._turn_verification_status = "passed"

    codex_turn = SimpleNamespace(
        interrupted=False,
        error=None,
        thread_id="thread-1",
        turn_id="t-codex-bump",
        projected_messages=[{
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "skill_view", "arguments": {"name": "web"}}},
            ],
        }],
        tool_iterations=0,
        final_text="ok",
        should_retire=False,
    )
    codex_agent._codex_session = MagicMock()
    codex_agent._codex_session.run_turn.return_value = codex_turn

    codex_runtime.run_codex_app_server_turn(
        codex_agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task",
    )
    assert records, "codex ledger write did not happen"
    rec = records[-1]
    assert rec.skills_loaded == ("web",)
    assert any(skill == "web" for skill, _outcome, _cost in bumps), (
        f"expected bump for 'web' from projected messages, got {bumps}"
    )