"""Per-turn outcome ledger — dataclass + safe writer.

The data contract lives here (NOT in ``hermes_state``) so the persistence
layer stays a thin SQL projection and callers don't accidentally
instantiate half-built records. The dataclass is frozen + hashable; the
safe writer is the only seam finalizers reach through — it must never
propagate database errors into the post-loop cleanup path.

Layering::

    turn_finalizer / codex_runtime  ->  record_turn_outcome_safely(...)
    record_turn_outcome_safely       ->  agent._session_db.record_turn_outcome(...)
    SessionDB                        ->  turn_outcomes SQL table

This module owns no schema and no logging configuration — it imports the
shared ``agent.conversation_loop.logger`` lazily, mirroring the
finalizer's lazy-logger pattern (no import cycle with conversation_loop).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class TurnOutcomeRecord:
    """One row of the per-turn outcome ledger.

    Fields mirror the schema in ``hermes_state.SCHEMA_SQL``'s
    ``turn_outcomes`` table. ``skills_loaded`` is a tuple (not a list) so
    the dataclass stays hashable; the writer serialises it to JSON.
    """

    session_id: str
    turn_id: str
    created_at: float
    outcome: str
    outcome_reason: Optional[str]
    turn_exit_reason: Optional[str]
    api_calls: int
    tool_iterations: int
    retry_count: int
    guardrail_halt: Optional[str]
    cost_usd_delta: float
    input_tokens_delta: int
    output_tokens_delta: int
    cache_read_tokens_delta: int
    skills_loaded: tuple[str, ...]
    model: Optional[str]


def record_turn_outcome_safely(
    session_db: Any,
    record: TurnOutcomeRecord,
) -> None:
    """Persist a turn outcome, swallowing DB errors.

    Finalizers call this after ``classify_turn_outcome()``. A database
    outage must NEVER escape the post-loop path — the agent has already
    decided the outcome, the cleanup is best-effort, and the user-visible
    response is already computed. We log a warning with session/turn
    identifiers so the failure is observable without raising.
    """
    if session_db is None:
        return None
    try:
        session_db.record_turn_outcome(record)
    except Exception as exc:  # noqa: BLE001 - intentional blanket catch
        # Lazy import mirrors agent/turn_finalizer.py — no module-level
        # import of conversation_loop, which would create a cycle.
        try:
            from agent.conversation_loop import logger
        except Exception:  # pragma: no cover - fallback when logger unavailable
            import logging
            logger = logging.getLogger(__name__)
        try:
            logger.warning(
                "record_turn_outcome_safely failed for session=%s turn=%s: %s",
                getattr(record, "session_id", "<unknown>"),
                getattr(record, "turn_id", "<unknown>"),
                exc,
            )
        except Exception:
            pass
        return None
    return None


def persist_turn_outcome(
    agent: Any,
    *,
    outcome: str,
    outcome_reason: str,
    turn_exit_reason: Optional[str],
    api_calls: int,
    tool_iterations: int,
    messages: Optional[Sequence[Mapping[str, Any]]] = None,
) -> TurnOutcomeRecord:
    """Build, persist, and attribute one turn outcome.

    Ledger persistence and sidecar attribution are deliberately independent:
    a database failure must not suppress the utility evidence for the turn.
    """
    record = build_turn_outcome_record(
        agent,
        outcome=outcome,
        outcome_reason=outcome_reason,
        turn_exit_reason=turn_exit_reason,
        api_calls=api_calls,
        tool_iterations=tool_iterations,
        messages=messages,
    )
    try:
        record_turn_outcome_safely(getattr(agent, "_session_db", None), record)
    finally:
        _bump_sidecar_for_skills(record)
    return record


def skills_loaded_from(agent: Any) -> tuple[str, ...]:
    """Best-effort extraction of the loaded-skill names from an agent.

    The agent tracks loaded skills on ``_skills_loaded`` (a list of
    name strings) or — depending on version — on the skill_manager.
    Anything missing → empty tuple. Missing here is fine: callers treat
    an empty tuple as "no skills recorded this turn".
    """
    candidates: Iterable[Any] = (
        getattr(agent, "_skills_loaded", None),
        getattr(getattr(agent, "_skill_manager", None), "loaded", None),
    )
    seen: list[str] = []
    for source in candidates:
        if not source:
            continue
        for name in source:
            text = str(name or "").strip()
            if text and text not in seen:
                seen.append(text)
    return tuple(seen)


def extract_loaded_skills(messages: Any) -> tuple[str, ...]:
    """Extract unique skill names from ``skill_view`` tool calls in messages.

    Reads the assistant message ``tool_calls`` entries whose ``function.name``
    is ``skill_view`` and returns the ``name`` argument value. Tolerates the
    repository's two message shapes — ``function.arguments`` may be a dict
    (after parsing) or a JSON-encoded string — and never raises on malformed
    messages, missing fields, bad JSON, or unknown tool names.

    Ordering is first-seen across the turn; duplicates collapse. Empty/whitespace
    names are dropped. Returning ``()`` for turns with no skill views lets
    callers treat missing-skills as a non-event without a None check.
    """
    seen: list[str] = []
    if not messages:
        return tuple(seen)
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        calls = msg.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            func = call.get("function")
            if not isinstance(func, dict):
                continue
            if func.get("name") != "skill_view":
                continue
            args = func.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (TypeError, ValueError):
                    continue
            if not isinstance(args, dict):
                continue
            name = args.get("name")
            if not isinstance(name, str):
                continue
            text = name.strip()
            if text and text not in seen:
                seen.append(text)
    return tuple(seen)


def build_turn_outcome_record(
    agent: Any,
    *,
    outcome: str,
    outcome_reason: Optional[str],
    turn_exit_reason: Optional[str],
    api_calls: int,
    tool_iterations: int,
    turn_context: Optional[Any] = None,
    created_at: Optional[float] = None,
    messages: Optional[Any] = None,
) -> TurnOutcomeRecord:
    """Build a ``TurnOutcomeRecord`` from an agent + turn-context snapshot.

    The ``turn_context`` snapshot is the per-turn token/cost baseline
    captured at prologue (turn_context.py); the agent's
    ``_turn_token_cost_snapshot`` (or any equivalent) is used as a
    fallback so codex-runtime and tests can pass a snapshot through the
    agent instead of threading ``turn_context`` everywhere. Missing
    counters yield zero deltas rather than ``None`` arithmetic, so the
    writer can always serialise the row.

    ``messages`` is an optional pass-through used by the finalizer paths
    to extract exact skill names from ``skill_view`` tool calls when
    ``agent._skills_loaded`` is empty. Falls back to the legacy seam
    (skills from the agent) when no messages are provided.
    """
    import time as _time

    snapshot = getattr(turn_context, "token_cost_snapshot", None) or {}
    if not snapshot:
        snapshot = getattr(agent, "_turn_token_cost_snapshot", None) or {}

    def _delta(field: str) -> int:
        start = snapshot.get(field, 0) or 0
        end = getattr(agent, field, 0) or 0
        try:
            return int(end) - int(start)
        except (TypeError, ValueError):
            return 0

    def _cost_delta() -> float:
        start = snapshot.get("session_estimated_cost_usd", 0.0) or 0.0
        end = getattr(agent, "session_estimated_cost_usd", 0.0) or 0.0
        try:
            return float(end) - float(start)
        except (TypeError, ValueError):
            return 0.0

    if messages is not None:
        # Finalizers that have a messages list attribute exact skill
        # names from ``skill_view`` tool calls; the legacy _skills_loaded
        # set is the fallback only when no messages list is supplied.
        try:
            skills_loaded = extract_loaded_skills(messages)
        except Exception:
            skills_loaded = ()
    else:
        skills_loaded = skills_loaded_from(agent)

    return TurnOutcomeRecord(
        session_id=str(getattr(agent, "session_id", "") or ""),
        turn_id=str(
            getattr(turn_context, "turn_id", None)
            or getattr(agent, "_current_turn_id", "")
            or ""
        ),
        created_at=float(created_at if created_at is not None else _time.time()),
        outcome=str(outcome),
        outcome_reason=outcome_reason,
        turn_exit_reason=turn_exit_reason,
        api_calls=int(api_calls),
        tool_iterations=int(tool_iterations),
        retry_count=int(
            getattr(agent, "_invalid_tool_retries", 0)
            + getattr(agent, "_invalid_json_retries", 0)
            + getattr(agent, "_empty_content_retries", 0)
            + getattr(agent, "_incomplete_scratchpad_retries", 0)
            + getattr(agent, "_codex_incomplete_retries", 0)
            + getattr(agent, "_thinking_prefill_retries", 0)
        ),
        guardrail_halt=_guardrail_halt_json(agent),
        cost_usd_delta=_cost_delta(),
        input_tokens_delta=_delta("session_input_tokens"),
        output_tokens_delta=_delta("session_output_tokens"),
        cache_read_tokens_delta=_delta("session_cache_read_tokens"),
        skills_loaded=skills_loaded,
        model=getattr(agent, "model", None),
    )


def _guardrail_halt_json(agent: Any) -> Optional[str]:
    """Render the guardrail halt decision as JSON text for the ledger row."""
    import json as _json

    decision = getattr(agent, "_tool_guardrail_halt_decision", None)
    if decision is None:
        return None
    try:
        metadata = decision.to_metadata()
    except Exception:
        metadata = {"reason": str(decision)}
    try:
        return _json.dumps(metadata)
    except (TypeError, ValueError):
        return None


def _bump_sidecar_for_skills(record: "TurnOutcomeRecord") -> None:
    """Best-effort sidecar bump for each skill in ``record.skills_loaded``.

    Finalizers call this after ``record_turn_outcome_safely`` so the curator's
    utility ranking picks up the outcome. The sidecar is observability —
    failures (DB outage, missing tools module, raised exceptions in
    ``bump_outcome``) MUST NEVER escape this function. A broken sidecar
    cannot be allowed to break a user turn.

    Bumps run whether or not the DB write succeeded, as long as we have a
    record with a non-empty ``skills_loaded``. If the record has no skills,
    there is nothing to attribute and we no-op.
    """
    skills = getattr(record, "skills_loaded", ()) or ()
    if not skills:
        return None
    try:
        # ponytail: lazy import keeps the ledger module from pulling
        # tools.skill_usage (file-locking, fcntl) at import time, and
        # means a missing tools package is a silent no-op rather than
        # an ImportError that escapes the finalizer.
        from tools.skill_usage import bump_outcome as _bump
    except Exception:
        return None
    outcome = str(getattr(record, "outcome", "") or "")
    try:
        cost = float(getattr(record, "cost_usd_delta", 0.0) or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    per_skill_cost = cost / len(skills)
    for skill in skills:
        try:
            _bump(str(skill), outcome, per_skill_cost)
        except Exception:
            # ponytail: per-skill swallow; one bad skill must not stop
            # the others. bump_outcome is already best-effort internally
            # but defensive on the call site protects against callers
            # that pre-date the internal guard.
            continue
    return None


__all__ = [
    "TurnOutcomeRecord",
    "build_turn_outcome_record",
    "record_turn_outcome_safely",
    "persist_turn_outcome",
    "skills_loaded_from",
    "extract_loaded_skills",
]
