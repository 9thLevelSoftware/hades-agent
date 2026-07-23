"""Behavior tests for _clear_conversation_scope, the boundary cleanup funnel."""

from gateway.run import _CONVERSATION_SCOPED_STATE, GatewayRunner

KEY = "agent:main:telegram:dm:777"
OTHER = "agent:main:discord:dm:888"


def _bare_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    for attr in _CONVERSATION_SCOPED_STATE:
        setattr(runner, attr, {KEY: object(), OTHER: object()})
    runner._running_agents = {KEY: object()}
    runner._running_agents_ts = {KEY: 1.0}
    runner._session_run_generation = {KEY: 7}
    return runner


def test_funnel_clears_every_registered_dict_for_key_only():
    runner = _bare_runner()
    runner._clear_conversation_scope(KEY, reason="test")
    for attr in _CONVERSATION_SCOPED_STATE:
        store = getattr(runner, attr)
        assert KEY not in store, f"{attr} not cleared by funnel"
        assert OTHER in store, f"{attr} cleared the wrong session"


def test_funnel_leaves_turn_scoped_and_generation_state_alone():
    runner = _bare_runner()
    runner._clear_conversation_scope(KEY, reason="test")
    assert KEY in runner._running_agents
    assert KEY in runner._running_agents_ts
    assert runner._session_run_generation[KEY] == 7


def test_funnel_is_bare_runner_safe_and_empty_key_noop():
    runner = object.__new__(GatewayRunner)
    runner._clear_conversation_scope(KEY, reason="test")
    runner._clear_conversation_scope("", reason="test")


def test_funnel_clears_state_written_by_real_setters():
    runner = object.__new__(GatewayRunner)
    runner._set_session_reasoning_override(KEY, {"effort": "high"})
    assert runner._session_reasoning_overrides.get(KEY) == {"effort": "high"}
    runner._clear_conversation_scope(KEY, reason="test")
    assert KEY not in runner._session_reasoning_overrides


def test_funnel_also_clears_boundary_security_state():
    runner = _bare_runner()
    runner._pending_approvals = {KEY: {"cmd": "rm -rf"}, OTHER: {}}
    runner._update_prompt_pending = {KEY: True}
    runner._pending_skills_reload_notes = {KEY: "note"}
    runner._clear_conversation_scope(KEY, reason="test")
    assert KEY not in runner._pending_approvals
    assert OTHER in runner._pending_approvals
    assert KEY not in runner._update_prompt_pending
    assert KEY not in runner._pending_skills_reload_notes
