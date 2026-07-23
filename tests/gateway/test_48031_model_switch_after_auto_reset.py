"""Regression coverage for a typed /model switch after auto-reset."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.model_switch import ModelSwitchResult


def _make_event() -> MessageEvent:
    return MessageEvent(
        text="/model gpt-5.5",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="48031", chat_type="dm"),
    )


def _successful_switch() -> ModelSwitchResult:
    return ModelSwitchResult(
        success=True,
        new_model="gpt-5.5",
        target_provider="openrouter",
        provider_changed=True,
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
        provider_label="OpenRouter",
    )


@pytest.mark.asyncio
async def test_model_switch_after_auto_reset_consumes_marker_and_keeps_override(
    tmp_path,
    monkeypatch,
):
    """A typed /model first after reset must survive the next regular turn."""
    hermes_home = tmp_path / ".hades"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "old", "provider": "openrouter"}}),
        encoding="utf-8",
    )
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **_kwargs: _successful_switch(),
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *_args, **_kwargs: 0,
    )

    event = _make_event()
    session_key = "agent:main:telegram:dm:48031"
    session_entry = SimpleNamespace(
        session_key=session_key,
        session_id="session-48031",
        was_auto_reset=True,
    )
    session_db = SimpleNamespace(update_session_model=AsyncMock())
    session_store = SimpleNamespace(
        get_or_create_session=AsyncMock(return_value=session_entry),
        set_model_override=AsyncMock(),
    )
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._running_agents = {}
    runner._session_model_overrides = {}
    runner._session_db = session_db
    runner.session_store = object()
    session_store._store = runner.session_store
    runner._async_session_store = session_store
    runner._normalize_source_for_session_key = lambda source: source
    runner._evict_cached_agent = MagicMock()

    result = await runner._handle_model_command(event)

    assert result is not None and "gpt-5.5" in result
    assert session_entry.was_auto_reset is False
    session_db.update_session_model.assert_awaited_once_with("session-48031", "gpt-5.5")
    assert runner._session_model_overrides[session_key]["model"] == "gpt-5.5"
    session_store.set_model_override.assert_awaited_once_with(
        session_key,
        runner._session_model_overrides[session_key],
    )
