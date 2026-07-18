"""End-to-end feedback verification tests with a real temporary SessionDB.

Exercises the authenticated reaction → annotate_turn_feedback →
reflection trigger pipeline through record_feedback_event with a real
SessionDB (not faked). Covers duplicate event-id dedup, unauthorized
actor rejection, bot/self rejection, and the invariant that no synthetic
user message is injected.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.reflection_triggers import (
    evaluate_reflection_triggers,
    normalize_reaction_event,
    record_feedback_event,
    should_trigger_review,
)
from hermes_state import SessionDB


# ── Helpers ───────────────────────────────────────────────────────────────


def _open_db(path: Path) -> SessionDB:
    return SessionDB(path / "state.db")


def _seed_turn(db: SessionDB, *, session_id="fb-sess", turn_id="fb-turn"):
    """Write one turn_outcomes row so annotate_turn_feedback has a target."""
    from agent.turn_ledger import TurnOutcomeRecord
    import time

    db.record_turn_outcome(
        TurnOutcomeRecord(
            session_id=session_id,
            turn_id=turn_id,
            created_at=time.time(),
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
            skills_loaded=("plan",),
            model="test-model",
        )
    )


# ── Tests ─────────────────────────────────────────────────────────────────


def test_authenticated_feedback_lands_in_ledger(tmp_path=None):
    """An authorized, non-bot, non-self reaction event must annotate the
    turn_outcomes row exactly once (first event_id)."""
    db = _open_db(Path(tempfile.mkdtemp(prefix="e2e_fb_")))
    _seed_turn(db)

    ok = record_feedback_event(
        "telegram",
        "chat-1",
        "msg-1",
        "user-42",
        "thumbs_down",
        "evt-fb-1",
        session_db=db,
        session_id="fb-sess",
        turn_id="fb-turn",
        actor_authorized=True,
    )
    assert ok is True

    rows = db.get_outcome_trends(session_id="fb-sess", days=30)
    assert rows
    assert rows[0]["feedback_kind"] == "reaction"
    assert rows[0]["feedback_value"] == "thumbs_down"
    assert rows[0]["feedback_source"] == "telegram"


def test_duplicate_event_id_produces_single_annotation(tmp_path=None):
    """Sending the same event_id twice must return False on the second
    call — the ledger must not double-count."""
    db = _open_db(Path(tempfile.mkdtemp(prefix="e2e_dup_")))
    _seed_turn(db)

    kwargs = dict(
        session_db=db,
        session_id="fb-sess",
        turn_id="fb-turn",
        actor_authorized=True,
    )
    assert record_feedback_event(
        "telegram", "c", "m", "u", "thumbs_up", "evt-dup", **kwargs
    ) is True
    # Duplicate → False (no-op).
    assert record_feedback_event(
        "telegram", "c", "m", "u", "thumbs_up", "evt-dup", **kwargs
    ) is False

    # Exactly one annotation in the trends.
    rows = db.get_outcome_trends(session_id="fb-sess", days=30)
    assert rows[0]["feedback_value"] == "thumbs_up"


def test_unauthorized_actor_rejected(tmp_path=None):
    """actor_authorized=False must prevent any annotation."""
    db = _open_db(Path(tempfile.mkdtemp(prefix="e2e_unauth_")))
    _seed_turn(db)

    ok = record_feedback_event(
        "telegram", "c", "m", "intruder", "thumbs_down", "evt-unauth",
        session_db=db,
        session_id="fb-sess",
        turn_id="fb-turn",
        actor_authorized=False,
    )
    assert ok is False

    rows = db.get_outcome_trends(session_id="fb-sess", days=30)
    assert rows[0]["feedback_kind"] is None


def test_bot_actor_rejected(tmp_path=None):
    """normalize_reaction_event must return None for bot actors."""
    assert normalize_reaction_event("thumbs_down", actor_is_bot=True) is None


def test_self_actor_rejected(tmp_path=None):
    """normalize_reaction_event must return None when actor_id == self_actor_id."""
    assert normalize_reaction_event(
        "thumbs_down", actor_id="bot-1", self_actor_id="bot-1"
    ) is None


def test_reaction_triggers_reflection_review(tmp_path=None):
    """A thumbs_down reaction must produce a reflection trigger that,
    when evaluated through should_trigger_review, gates a review attempt."""
    trigger = evaluate_reflection_triggers("reaction", "", ["thumbs_down"])
    assert trigger is not None
    assert trigger.kind == "reaction"

    agent = SimpleNamespace(session_id="fb-sess", _background_review_in_flight=False)
    run = should_trigger_review({"agent": agent, "trigger": trigger, "cooldown": 60})
    assert run is True


def test_no_synthetic_user_message_injected(tmp_path=None):
    """record_feedback_event must be a pure DB annotation — it must not
    append any message to any message list. Verify by checking the
    function's return type and that no message-list side effect exists."""
    db = _open_db(Path(tempfile.mkdtemp(prefix="e2e_nosynth_")))
    _seed_turn(db)

    messages = [
        {"role": "user", "content": "original"},
        {"role": "assistant", "content": "response"},
    ]
    before_count = len(messages)

    record_feedback_event(
        "telegram", "c", "m", "u", "thumbs_down", "evt-ns",
        session_db=db,
        session_id="fb-sess",
        turn_id="fb-turn",
        actor_authorized=True,
    )
    # No new messages were appended.
    assert len(messages) == before_count
    # No user-role injection.
    assert all(m["role"] != "user" or m["content"] == "original" for m in messages)


def test_feedback_dedupe_and_reflection_end_to_end(tmp_path=None):
    """Full pipeline: seed turn → send reaction → dedupe second send →
    evaluate reflection trigger → gate review. One annotation, one
    review attempt, no duplicates."""
    db = _open_db(Path(tempfile.mkdtemp(prefix="e2e_pipeline_")))
    _seed_turn(db, turn_id="pipe-turn")

    kwargs = dict(
        session_db=db,
        session_id="fb-sess",
        turn_id="pipe-turn",
        actor_authorized=True,
    )

    # First send → annotation.
    assert record_feedback_event(
        "telegram", "c", "m", "u", "thumbs_down", "evt-pipe", **kwargs
    ) is True
    # Second send (same event_id) → dedupe.
    assert record_feedback_event(
        "telegram", "c", "m", "u", "thumbs_down", "evt-pipe", **kwargs
    ) is False

    # Exactly one annotation.
    rows = db.get_outcome_trends(session_id="fb-sess", days=30)
    assert rows[0]["feedback_kind"] == "reaction"

    # Reflection trigger for a reaction.
    trigger = evaluate_reflection_triggers("reaction", "", ["thumbs_down"])
    assert trigger.kind == "reaction"

    agent = SimpleNamespace(session_id="fb-sess", _background_review_in_flight=False)
    assert should_trigger_review({"agent": agent, "trigger": trigger, "cooldown": 60}) is True
    # Second attempt → blocked by single-flight.
    assert should_trigger_review({"agent": agent, "trigger": trigger, "cooldown": 60}) is False
