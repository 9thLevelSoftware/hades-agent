from types import SimpleNamespace

from agent.reflection_triggers import (
    evaluate_reflection_triggers,
    normalize_reaction_event,
    record_feedback_event,
    should_trigger_review,
)
from agent.reactions import detect_user_correction
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult


class _FeedbackAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True)

    async def get_chat_info(self, chat_id):
        return {}


def test_base_feedback_callback_schedules_awaitable():
    import asyncio

    async def scenario():
        adapter = _FeedbackAdapter(
            PlatformConfig(enabled=True, token="test"), Platform.LOCAL
        )
        called = asyncio.Event()

        async def callback(*args):
            called.set()
            return True

        adapter.set_feedback_handler(callback)
        assert adapter.publish_feedback("local", "chat", "m1", "u1", "👎", "e1")
        await called.wait()

    asyncio.run(scenario())


def test_reaction_outcome_produces_reaction_trigger():
    trigger = evaluate_reflection_triggers("reaction", "", ["thumbs_down"])

    assert trigger.kind == "reaction"
    assert trigger.dedupe_key == evaluate_reflection_triggers(
        "reaction", "", ["thumbs_down"]
    ).dedupe_key


def test_failed_turn_produces_stable_failure_trigger():
    trigger = evaluate_reflection_triggers("failed", "", [])

    assert trigger.kind == "failure"
    assert evaluate_reflection_triggers("failed", "", []).dedupe_key == trigger.dedupe_key
    assert evaluate_reflection_triggers("blocked", "", []).kind == "failure"
    assert evaluate_reflection_triggers("unresolved", "", []).kind == "failure"


def test_correction_detector_is_narrow_and_case_insensitive():
    for text in (
        "No, I meant the staging branch",
        "I MEANT the other file",
        "That is wrong",
        "wrong file",
        "please undo that",
        "not what I asked",
    ):
        assert detect_user_correction(text) is True

    for text in ("No results yet", "no", "not yet", "ok", "thanks"):
        assert detect_user_correction(text) is False


def test_three_consecutive_tool_errors_trigger_streak():
    errors = [{"error": "x"}, {"error": "y"}, {"error": "z"}]

    assert evaluate_reflection_triggers("partial", "", errors).kind == "tool_failure_streak"
    assert evaluate_reflection_triggers("partial", "", errors[:2]) is None
    assert evaluate_reflection_triggers(
        "partial", "", [errors[0], {"result": "ok"}, errors[2]]
    ) is None


def test_review_gate_enforces_single_flight_and_failure_cooldown(monkeypatch):
    clock = iter((100.0, 101.0, 200.0))
    monkeypatch.setattr("agent.reflection_triggers.time.monotonic", lambda: next(clock))
    agent = SimpleNamespace(session_id="session-1")
    failure = evaluate_reflection_triggers("failed", "", [])

    assert should_trigger_review({"agent": agent, "trigger": failure, "cooldown": 60}) is True
    assert should_trigger_review({"agent": agent, "trigger": failure, "cooldown": 60}) is False
    agent._background_review_in_flight = False
    assert should_trigger_review({"agent": agent, "trigger": failure, "cooldown": 60}) is False
    assert should_trigger_review({"agent": agent, "trigger": failure, "cooldown": 60}) is True


def test_review_gate_can_disable_signals_without_persistent_session():
    agent = SimpleNamespace(session_id="session-1", _background_review_in_flight=False)
    failure = evaluate_reflection_triggers("failed", "", [])

    assert should_trigger_review(
        {"agent": agent, "trigger": failure, "cooldown": 0, "signal_enabled": False}
    ) is False


def test_review_gate_allows_new_signal_after_completed_review(monkeypatch):
    clock = iter((100.0, 200.0))
    monkeypatch.setattr("agent.reflection_triggers.time.monotonic", lambda: next(clock))
    agent = SimpleNamespace(session_id="session-1")
    correction = evaluate_reflection_triggers("verified", "I meant the other file", [])
    reaction = evaluate_reflection_triggers("reaction", "", ["thumbs_down"])

    assert should_trigger_review({"agent": agent, "trigger": correction, "cooldown": 60}) is True
    agent._background_review_in_flight = False
    assert should_trigger_review({"agent": agent, "trigger": reaction, "cooldown": 60}) is True


def test_review_gate_does_not_share_cooldown_between_signal_kinds(monkeypatch):
    clock = iter((100.0, 101.0))
    monkeypatch.setattr("agent.reflection_triggers.time.monotonic", lambda: next(clock))
    agent = SimpleNamespace(session_id="session-1")
    failure = evaluate_reflection_triggers("failed", "", [])
    correction = evaluate_reflection_triggers("verified", "I meant the other file", [])

    assert should_trigger_review({"agent": agent, "trigger": failure, "cooldown": 60}) is True
    agent._background_review_in_flight = False
    assert should_trigger_review({"agent": agent, "trigger": correction, "cooldown": 60}) is True


def test_positive_reaction_does_not_trigger_background_review():
    assert evaluate_reflection_triggers("reaction", "", ["thumbs_up"]) is None
    assert evaluate_reflection_triggers("reaction", "", ["👎"]).kind == "reaction"


def test_review_gate_preserves_verified_interval_fallback():
    agent = SimpleNamespace(session_id="session-1")

    assert should_trigger_review(
        {
            "agent": agent,
            "outcome": "verified",
            "has_response": True,
            "interrupted": False,
            "interval_triggered": True,
        }
    ) is True


def test_reaction_normalization_rejects_bot_and_self_actors():
    assert normalize_reaction_event("thumbs_down", actor_is_bot=True) is None
    assert normalize_reaction_event(
        "thumbs_down", actor_id="bot", self_actor_id="bot"
    ) is None
    assert normalize_reaction_event("thumbs_down", actor_id="user") == "thumbs_down"


def test_failure_prompt_forbids_tool_unavailability_claims():
    from agent.background_review import _FAILURE_REVIEW_PROMPT

    lower = _FAILURE_REVIEW_PROMPT.lower()
    assert "environment-dependent" in lower
    assert "transient" in lower
    assert "single failure" in lower
    assert "tool is unavailable" in lower


def test_feedback_annotation_requires_authentication_and_dedupes():
    class FakeDB:
        def __init__(self):
            self.events = set()
            self.last_feedback = None

        def annotate_turn_feedback(self, session_id, turn_id, **feedback):
            self.last_feedback = feedback
            event_id = feedback["event_id"]
            if event_id in self.events:
                return False
            self.events.add(event_id)
            return True

    db = FakeDB()
    kwargs = {
        "session_db": db,
        "session_id": "session-1",
        "turn_id": "turn-1",
        "actor_authorized": True,
    }

    assert record_feedback_event(
        "telegram", "chat", "m1", "user", "thumbs_down", "evt-1", **kwargs
    ) is True
    assert db.last_feedback is not None
    assert db.last_feedback["kind"] == "reaction"
    assert record_feedback_event(
        "telegram",
        "chat",
        "m1",
        "user",
        "thumbs_down",
        "evt-no-auth",
        session_db=db,
        session_id="session-1",
        turn_id="turn-1",
    ) is False
    assert record_feedback_event(
        "telegram", "chat", "m1", "user", "thumbs_down", "evt-1", **kwargs
    ) is False
    assert record_feedback_event(
        "telegram",
        "chat",
        "m1",
        "intruder",
        "thumbs_down",
        "evt-2",
        session_db=db,
        session_id="session-1",
        turn_id="turn-1",
        actor_authorized=False,
    ) is False
