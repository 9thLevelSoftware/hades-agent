"""Regression coverage for auto-reset conversation-boundary cleanup."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _StopAfterAutoReset(Exception):
    """Deliberately stop the large handler immediately after the boundary."""


@pytest.mark.asyncio
async def test_auto_reset_clears_scope_evicts_agent_and_consumes_marker():
    """A real auto-reset turn must establish a clean cached-agent boundary."""
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="10710", chat_type="dm")
    event = MessageEvent(text="fresh turn", message_type=MessageType.TEXT, source=source)
    session_entry = SimpleNamespace(
        session_key="agent:main:telegram:dm:10710",
        session_id="session-10710",
        was_auto_reset=True,
        created_at=1,
        updated_at=2,
        is_fresh_reset=False,
    )
    runner = object.__new__(GatewayRunner)
    runner.session_store = object()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(return_value=session_entry),
    )
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = MagicMock()
    runner._is_telegram_topic_lane = lambda _source: False
    runner._session_model_overrides = {session_entry.session_key: {"model": "stale"}}
    runner._last_resolved_model = {session_entry.session_key: "stale/model"}
    runner._agent_cache = {session_entry.session_key: None}
    runner._clear_conversation_scope = MagicMock(
        wraps=runner._clear_conversation_scope,
    )
    runner._evict_cached_agent = MagicMock(wraps=runner._evict_cached_agent)
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock(side_effect=_StopAfterAutoReset)

    with pytest.raises(_StopAfterAutoReset):
        await runner._handle_message_with_agent(event, source, "turn-10710", 1)

    runner._clear_conversation_scope.assert_called_once_with(
        session_entry.session_key,
        reason="auto_reset",
    )
    runner._evict_cached_agent.assert_called_once_with(session_entry.session_key)
    assert session_entry.was_auto_reset is False
    assert session_entry.session_key not in runner._session_model_overrides
    assert session_entry.session_key not in runner._last_resolved_model
    assert session_entry.session_key not in runner._agent_cache


def test_auto_reset_cleanup_clears_last_resolved_model():
    """The conversation-scope funnel clears stale model routing for one session."""
    runner = object.__new__(GatewayRunner)
    key = "agent:main:telegram:dm:58403"
    runner._last_resolved_model = {key: "stale/model", "other": "keep/me"}

    runner._clear_conversation_scope(key, reason="auto_reset")

    assert key not in runner._last_resolved_model
    assert runner._last_resolved_model.get("other") == "keep/me"
