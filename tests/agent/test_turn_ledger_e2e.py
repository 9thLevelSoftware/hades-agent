"""End-to-end ledger tests with a real temporary SessionDB.

Exercises the full finalize_turn → build_turn_outcome_record →
record_turn_outcome_safely → SessionDB.record_turn_outcome pipeline
through two canonical turn paths (verified + blocked) and then reopens
a fresh SessionDB against the same SQLite file to prove durability.

Also covers the invariant that a forced ledger-write exception must NOT
alter the user-facing final_response.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_finalizer import finalize_turn
from hermes_state import SessionDB


# ── FakeAgent (mirrors test_turn_finalizer_final_response_persistence) ────


class _FakeAgent:
    """Minimal agent stub that satisfies finalize_turn's attribute reads."""

    def __init__(self, session_db, session_id="e2e-sess"):
        self.max_iterations = 90
        self.iteration_budget = SimpleNamespace(remaining=89, used=1, max_total=90)
        self.quiet_mode = True
        self.model = "e2e-model"
        self.provider = "e2e-provider"
        self.base_url = ""
        self.session_id = session_id
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self._turn_verification_status = None
        self._memory_manager = MagicMock()
        self._session_db = session_db
        self._turn_token_cost_snapshot = {}
        self._current_turn_id = ""
        self._background_review_in_flight = False
        self._background_review_last_at = {}

        # no-op hooks
        self._save_trajectory = lambda *a, **k: None
        self._cleanup_task_resources = lambda *a, **k: None
        self._drop_trailing_empty_response_scaffolding = lambda *a, **k: None
        self._persist_session = lambda *a, **k: None
        self._file_mutation_verifier_enabled = lambda: False
        self._turn_completion_explainer_enabled = lambda: False
        self._drain_pending_steer = lambda: None
        self.clear_interrupt = lambda: None
        self._sync_external_memory_for_turn = lambda **k: None

    def _handle_max_iterations(self, messages, api_call_count):
        raise AssertionError("not expected in e2e")

    def _emit_status(self, *_a, **_k):
        pass

    def _safe_print(self, *_a, **_k):
        pass


# ── Helpers ───────────────────────────────────────────────────────────────


def _open_db(path: Path) -> SessionDB:
    """Open a SessionDB at the given path (creates schema on first use)."""
    return SessionDB(path / "state.db")


def _finalize_verified(agent, turn_id="t-verified"):
    """Run finalize_turn for a successful, verified turn."""
    agent._turn_verification_status = "passed"
    agent._current_turn_id = turn_id
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Done."},
    ]
    return finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="",
        turn_id=turn_id,
        user_message="hello",
        original_user_message="hello",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )


def _finalize_blocked(agent, turn_id="t-blocked"):
    """Run finalize_turn for a guardrail-blocked turn."""
    agent._tool_guardrail_halt_decision = SimpleNamespace(
        to_metadata=lambda: {"tool": "terminal", "reason": "deny"},
    )
    agent._turn_verification_status = None
    agent._current_turn_id = turn_id
    messages = [
        {"role": "user", "content": "rm -rf /"},
        {
            "role": "assistant",
            "content": "I can't do that.",
            "tool_calls": [
                {"function": {"name": "terminal", "arguments": '{"command":"rm -rf /"}'}},
            ],
        },
    ]
    result = finalize_turn(
        agent,
        final_response="I can't do that.",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="",
        turn_id=turn_id,
        user_message="rm -rf /",
        original_user_message="rm -rf /",
        _should_review_memory=False,
        _turn_exit_reason="guardrail_halt",
    )
    # Reset guardrail for subsequent calls if needed.
    agent._tool_guardrail_halt_decision = None
    return result


# ── Tests ─────────────────────────────────────────────────────────────────


def test_two_turns_persist_and_survive_reopen(tmp_path=None):
    """Two turns (verified + blocked) → two durable rows visible after
    reopening the DB file with a fresh SessionDB instance."""
    db_path = Path(tempfile.mkdtemp(prefix="e2e_ledger_"))
    db = _open_db(db_path)
    agent = _FakeAgent(db)

    _finalize_verified(agent, turn_id="t-ok")
    _finalize_blocked(agent, turn_id="t-blk")

    # Reopen fresh SessionDB on same file — proves SQLite durability.
    db2 = _open_db(db_path)

    rows = db2.get_outcome_trends(session_id="e2e-sess", days=30)
    outcomes = {r["outcome"]: r["count"] for r in rows}
    assert outcomes.get("verified") == 1, f"expected 1 verified, got {outcomes}"
    assert outcomes.get("blocked") == 1, f"expected 1 blocked, got {outcomes}"
    assert sum(outcomes.values()) == 2


def test_deltas_are_non_negative(tmp_path=None):
    """Token and cost deltas recorded in the ledger must be >= 0 for a
    turn that starts from a zeroed snapshot."""
    db_path = Path(tempfile.mkdtemp(prefix="e2e_deltas_"))
    db = _open_db(db_path)
    agent = _FakeAgent(db)
    # Simulate some token usage this turn.
    agent.session_input_tokens = 100
    agent.session_output_tokens = 20
    agent.session_cache_read_tokens = 10
    agent.session_estimated_cost_usd = 0.05

    _finalize_verified(agent, turn_id="t-deltas")

    rows = db.get_outcome_trends(session_id="e2e-sess", days=30)
    assert len(rows) == 1
    row = rows[0]
    assert row["input_tokens_delta"] >= 0
    assert row["output_tokens_delta"] >= 0
    assert row["cache_read_tokens_delta"] >= 0
    assert row["cost_usd_delta"] >= 0.0


def test_skills_loaded_exact_names_round_trip(tmp_path=None):
    """When messages carry skill_view tool_calls the exact skill names
    must appear in the persisted skills_loaded JSON."""
    db_path = Path(tempfile.mkdtemp(prefix="e2e_skills_"))
    db = _open_db(db_path)
    agent = _FakeAgent(db)
    agent._current_turn_id = "t-skills"
    agent._turn_verification_status = "passed"

    messages = [
        {"role": "user", "content": "plan something"},
        {
            "role": "assistant",
            "content": "let me check",
            "tool_calls": [
                {"function": {"name": "skill_view", "arguments": '{"name": "plan"}'}},
                {"function": {"name": "skill_view", "arguments": '{"name": "web"}'}},
            ],
        },
        {"role": "tool", "content": "skill plan loaded"},
        {"role": "tool", "content": "skill web loaded"},
        {"role": "assistant", "content": "Done."},
    ]

    finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="",
        turn_id="t-skills",
        user_message="plan something",
        original_user_message="plan something",
        _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )

    rows = db.get_outcome_trends(session_id="e2e-sess", days=30)
    assert rows
    loaded = json.loads(rows[0]["skills_loaded"])
    assert sorted(loaded) == ["plan", "web"]


def test_trends_and_skill_counts_available_after_restart(tmp_path=None):
    """get_outcome_trends and get_skill_outcome_counts must work on a
    freshly-reopened SessionDB (not just the instance that wrote)."""
    db_path = Path(tempfile.mkdtemp(prefix="e2e_restart_"))
    db = _open_db(db_path)
    agent = _FakeAgent(db)

    _finalize_verified(agent, turn_id="t-restart")

    # Reopen.
    db2 = _open_db(db_path)

    trends = db2.get_outcome_trends(session_id="e2e-sess", days=30)
    assert trends, "trends must survive reopen"
    assert trends[0]["outcome"] == "verified"

    # Skill counts work even when no skills were loaded (empty list).
    skill_counts = db2.get_skill_outcome_counts(days=30)
    # No skill_view calls → empty skills_loaded → skill_counts is empty.
    assert isinstance(skill_counts, list)


def test_ledger_write_exception_does_not_alter_final_response(monkeypatch):
    """When record_turn_outcome raises, the safe writer swallows it and
    the returned final_response is unchanged."""
    db_path = Path(tempfile.mkdtemp(prefix="e2e_boom_"))
    db = _open_db(db_path)
    agent = _FakeAgent(db)

    # Patch SessionDB.record_turn_outcome to raise.
    _orig = SessionDB.record_turn_outcome

    def _boom(self, record):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(SessionDB, "record_turn_outcome", _boom)

    result = _finalize_verified(agent, turn_id="t-boom")

    # The response must survive the DB error.
    assert result["final_response"] == "Done."
    assert result["outcome"] == "verified"

    # Restore for any subsequent calls.
    monkeypatch.setattr(SessionDB, "record_turn_outcome", _orig)
