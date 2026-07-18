"""Pure, token-free reflection trigger and feedback helpers."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from agent.reactions import detect_user_correction

_NEGATIVE_REACTIONS = {
    "👎",
    "thumbs_down",
    "thumbsdown",
    "no",
    "dislike",
    "-1",
}

ReflectionTriggerKind = Literal[
    "failure", "correction", "tool_failure_streak", "reaction"
]


@dataclass(frozen=True)
class ReflectionTrigger:
    kind: ReflectionTriggerKind
    dedupe_key: str


def _stable_key(kind: ReflectionTriggerKind, value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str).encode()
    return f"{kind}:{hashlib.sha256(encoded).hexdigest()[:16]}"


def _tool_failed(result: object) -> bool:
    if isinstance(result, dict):
        if result.get("error"):
            return True
        status = str(result.get("status") or "").lower()
        if status in {"error", "failed", "failure"}:
            return True
        result = result.get("content", result.get("result"))
    if isinstance(result, str):
        text = result.lower()
        return '"error"' in text or '"success": false' in text
    return False


def _tool_results(messages: object) -> list[object]:
    if not isinstance(messages, (list, tuple)):
        return []
    return [
        message.get("content", message)
        for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    ]


def evaluate_reflection_triggers(
    outcome: object,
    user_text: object,
    tool_results: object,
) -> ReflectionTrigger | None:
    """Return the highest-priority reflection signal for one turn."""
    outcome_name = str(
        outcome.get("outcome") if isinstance(outcome, dict) else outcome or ""
    ).lower()
    if outcome_name in {"failed", "blocked", "unresolved"}:
        return ReflectionTrigger("failure", _stable_key("failure", outcome_name))
    if outcome_name == "reaction":
        values = list(tool_results) if isinstance(tool_results, (list, tuple)) else [tool_results]
        reaction = str(values[0] if values else "").strip().lower()
        if reaction not in _NEGATIVE_REACTIONS:
            return None
        return ReflectionTrigger("reaction", _stable_key("reaction", tool_results))

    text = user_text if isinstance(user_text, str) else ""
    if detect_user_correction(text):
        return ReflectionTrigger("correction", _stable_key("correction", text.lower()))

    results = list(tool_results) if isinstance(tool_results, (list, tuple)) else []
    if len(results) >= 3 and all(_tool_failed(item) for item in results[-3:]):
        return ReflectionTrigger(
            "tool_failure_streak",
            _stable_key("tool_failure_streak", results[-3:]),
        )
    return None


def should_trigger_review(context: object) -> bool:
    """Apply per-agent single-flight and failure-cooldown review gating."""
    if not isinstance(context, dict):
        return False
    agent = context.get("agent")
    trigger = context.get("trigger")
    if trigger is not None and context.get("signal_enabled") is False:
        return False
    if trigger is None:
        if not (
            context.get("interval_triggered")
            and context.get("outcome") == "verified"
            and context.get("has_response")
            and not context.get("interrupted")
        ):
            return False
    elif not isinstance(trigger, ReflectionTrigger):
        return False

    if context.get("interrupted"):
        return False
    if agent is None or getattr(agent, "_background_review_in_flight", False):
        return False

    now = time.monotonic()
    cooldown = max(float(context.get("cooldown", 300) or 0), 0.0)
    session_key = str(getattr(agent, "session_id", "") or "")
    last_at = getattr(agent, "_background_review_last_at", {})
    if not isinstance(last_at, dict):
        last_at = {}
    trigger_kind = trigger.kind if isinstance(trigger, ReflectionTrigger) else "interval"
    cooldown_key = f"{session_key}:{trigger_kind}"
    last = last_at.get(cooldown_key)
    if last is not None and now - float(last) < cooldown:
        return False

    last_at[cooldown_key] = now
    agent._background_review_last_at = last_at
    agent._background_review_in_flight = True
    return True


def release_review_flight(agent: Any) -> None:
    if agent is not None:
        agent._background_review_in_flight = False


def normalize_reaction_event(
    reaction: object,
    *,
    actor_id: object = None,
    self_actor_id: object = None,
    actor_is_bot: bool = False,
) -> str | None:
    """Normalize feedback reactions while rejecting bot/self events."""
    if actor_is_bot or (
        actor_id is not None
        and self_actor_id is not None
        and str(actor_id) == str(self_actor_id)
    ):
        return None
    normalized = str(reaction or "").strip()
    return normalized or None


def record_feedback_event(
    platform: object,
    conversation_id: object,
    message_id: object,
    actor_id: object,
    reaction: object,
    event_id: object,
    *,
    session_db: Any,
    session_id: object,
    turn_id: object,
    actor_authorized: bool = False,
    actor_is_bot: bool = False,
    self_actor_id: object = None,
) -> bool:
    """Authenticate and dedupe one reaction annotation through SessionDB."""
    value = normalize_reaction_event(
        reaction,
        actor_id=actor_id,
        self_actor_id=self_actor_id,
        actor_is_bot=actor_is_bot,
    )
    if not (
        actor_authorized
        and value
        and event_id
        and session_db is not None
        and session_id
        and turn_id
    ):
        return False
    try:
        return bool(
            session_db.annotate_turn_feedback(
                str(session_id),
                str(turn_id),
                kind="reaction",
                value=value,
                source=str(platform or ""),
                event_id=str(event_id),
            )
        )
    except Exception:
        return False


__all__ = [
    "ReflectionTrigger",
    "_tool_results",
    "evaluate_reflection_triggers",
    "normalize_reaction_event",
    "record_feedback_event",
    "release_review_flight",
    "should_trigger_review",
]
