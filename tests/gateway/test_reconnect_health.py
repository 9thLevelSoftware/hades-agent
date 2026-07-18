"""Deterministic reconnect health integration tests."""

from __future__ import annotations

import asyncio
import threading
import time
import weakref
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.runtime_health import RuntimeHealthRegistry
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner


class _Adapter(BasePlatformAdapter):
    def __init__(self, *, result: bool = False, error: str | None = None):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)
        self.result = result
        self.error = error
        self.disconnect_calls = 0

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self.error is not None:
            raise RuntimeError(self.error)
        return self.result

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _make_runner(registry: RuntimeHealthRegistry | None = None) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")}
    )
    runner._runtime_health = registry or RuntimeHealthRegistry()
    runner._running = True
    runner._adapter_state_lock = threading.Lock()
    runner._fatal_adapter_claims = weakref.WeakSet()
    runner._failed_platforms = {
        Platform.TELEGRAM: {
            "config": PlatformConfig(enabled=True, token="test"),
            "attempts": 0,
            "next_retry": time.monotonic() - 1,
        }
    }
    runner.adapters = {}
    runner.delivery_router = MagicMock()
    runner.session_store = MagicMock()
    runner._busy_text_mode = "interrupt"
    runner._sync_voice_mode_state_to_adapter = MagicMock()
    runner._schedule_resume_pending_sessions = MagicMock()
    runner._update_platform_runtime_status = MagicMock()
    return runner


def test_runtime_health_status_contains_only_bounded_fields():
    runner = _make_runner()
    runner._runtime_health.record_failure(
        "platform:telegram",
        "https://user:password@example.test/token",
        now=100.0,
    )

    assert runner._runtime_health_status() == {
        "telegram": {
            "health_state": "degraded",
            "next_probe_at": 130.0,
            "suppressed_failures": 0,
        }
    }


async def _run_one_tick(runner: GatewayRunner) -> None:
    real_sleep = asyncio.sleep
    calls = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls > 2:
            runner._running = False
        await real_sleep(0)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        await runner._platform_reconnect_watcher()


@pytest.mark.asyncio
async def test_reconnect_records_one_failure_for_one_connect_exception():
    registry = RuntimeHealthRegistry()
    record_failure = MagicMock(side_effect=registry.record_failure)
    registry.record_failure = record_failure
    runner = _make_runner(registry)
    runner._create_adapter = MagicMock(return_value=_Adapter(error="same outage"))
    runner._connect_adapter_with_timeout = AsyncMock(side_effect=RuntimeError("same outage"))

    await _run_one_tick(runner)

    assert record_failure.call_count == 1
    assert record_failure.call_args.args[0] == "platform:telegram"
    assert runner._runtime_health.snapshot()["platform:telegram"].consecutive_failures == 1


@pytest.mark.asyncio
async def test_same_failure_logs_degradation_and_circuit_transition_once(caplog):
    runner = _make_runner()
    runner._create_adapter = MagicMock(side_effect=lambda *_: _Adapter(error="same outage"))
    runner._connect_adapter_with_timeout = AsyncMock(side_effect=RuntimeError("same outage"))

    with caplog.at_level("WARNING", logger="gateway.run"):
        await _run_one_tick(runner)
        for _ in range(2):
            runner._running = True
            runner._failed_platforms[Platform.TELEGRAM]["next_retry"] = time.monotonic() - 1
            await _run_one_tick(runner)

    reconnect_errors = [record for record in caplog.records if "Reconnect telegram error" in record.message]
    assert len(reconnect_errors) == 2
    snapshot = runner._runtime_health.snapshot()["platform:telegram"]
    assert snapshot.state == "open_circuit"
    assert snapshot.suppressed_failures == 1


@pytest.mark.asyncio
async def test_changed_failure_fingerprint_logs_once_and_success_resets_health(caplog):
    runner = _make_runner()
    adapters = iter((_Adapter(error="dns outage"), _Adapter(result=True)))
    runner._create_adapter = MagicMock(side_effect=lambda *_: next(adapters))
    runner._connect_adapter_with_timeout = AsyncMock(
        side_effect=(RuntimeError("dns outage"), True)
    )

    with caplog.at_level("WARNING", logger="gateway.run"):
        await _run_one_tick(runner)
        runner._running = True
        runner._failed_platforms[Platform.TELEGRAM]["next_retry"] = time.monotonic() - 1
        runner._connect_adapter_with_timeout = AsyncMock(side_effect=RuntimeError("auth outage"))
        await _run_one_tick(runner)

    reconnect_errors = [record for record in caplog.records if "Reconnect telegram error" in record.message]
    assert len(reconnect_errors) == 2

    runner._running = True
    runner._failed_platforms[Platform.TELEGRAM]["next_retry"] = time.monotonic() - 1
    runner._create_adapter = MagicMock(return_value=_Adapter(result=True))
    runner._connect_adapter_with_timeout = AsyncMock(return_value=True)
    await _run_one_tick(runner)

    snapshot = runner._runtime_health.snapshot()["platform:telegram"]
    assert snapshot.state == "healthy"
    assert snapshot.suppressed_failures == 0


@pytest.mark.asyncio
async def test_scheduler_does_not_gate_due_retry_on_registry_probe_window():
    registry = MagicMock(spec=RuntimeHealthRegistry)
    registry.should_probe.return_value = False
    registry.record_failure.side_effect = RuntimeHealthRegistry().record_failure
    runner = _make_runner(registry)
    runner._create_adapter = MagicMock(return_value=_Adapter(result=True))
    runner._connect_adapter_with_timeout = AsyncMock(return_value=True)

    await _run_one_tick(runner)

    assert Platform.TELEGRAM in runner.adapters
    registry.should_probe.assert_not_called()
    registry.record_success.assert_called_once_with("platform:telegram")


@pytest.mark.asyncio
async def test_concurrent_fatal_notifications_record_health_and_disconnect_once(caplog):
    registry = RuntimeHealthRegistry()
    record_failure = MagicMock(side_effect=registry.record_failure)
    registry.record_failure = record_failure
    runner = _make_runner(registry)
    adapter = _Adapter()
    adapter._set_fatal_error("telegram_runtime", "same outage", retryable=True)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.delivery_router.adapters = runner.adapters

    disconnect_started = asyncio.Event()
    release_disconnect = asyncio.Event()
    disconnect_calls = 0

    async def slow_disconnect():
        nonlocal disconnect_calls
        disconnect_calls += 1
        disconnect_started.set()
        await release_disconnect.wait()

    adapter.disconnect = slow_disconnect

    with caplog.at_level("ERROR", logger="gateway.run"):
        first = asyncio.create_task(runner._handle_adapter_fatal_error(adapter))
        await disconnect_started.wait()
        second = asyncio.create_task(runner._handle_adapter_fatal_error(adapter))
        await asyncio.sleep(0)
        release_disconnect.set()
        await asyncio.gather(first, second)

    assert record_failure.call_count == 1
    assert len([record for record in caplog.records if "Fatal telegram adapter error" in record.message]) == 1
    assert disconnect_calls == 1
    assert Platform.TELEGRAM not in runner.adapters


@pytest.mark.asyncio
async def test_secondary_profile_fatal_is_handled_when_primary_owns_same_platform():
    runner = _make_runner()
    primary = _Adapter()
    secondary = _Adapter()
    secondary._set_fatal_error("telegram_runtime", "secondary outage", retryable=True)
    runner.adapters = {Platform.TELEGRAM: primary}
    runner._profile_adapters = {"secondary": {Platform.TELEGRAM: secondary}}
    runner.delivery_router.adapters = runner.adapters
    runner.stop = AsyncMock()
    secondary.disconnect = AsyncMock()
    primary.disconnect = AsyncMock()

    await runner._handle_adapter_fatal_error(secondary)

    secondary.disconnect.assert_awaited_once_with()
    primary.disconnect.assert_not_awaited()
    assert Platform.TELEGRAM not in runner._profile_adapters["secondary"]
    assert Platform.TELEGRAM in runner.adapters
    runner.stop.assert_not_awaited()
