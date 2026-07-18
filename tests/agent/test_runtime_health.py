"""Pure-state tests for the in-process runtime health registry."""

from dataclasses import FrozenInstanceError
import hashlib

import pytest

from agent.runtime_health import HealthSnapshot, RuntimeHealthRegistry


def test_first_failure_records_degraded_state_and_fingerprint():
    registry = RuntimeHealthRegistry()

    snapshot, should_log = registry.record_failure(
        "platform:telegram", ValueError("connection down"), now=100.0
    )

    assert should_log is True
    assert snapshot == HealthSnapshot(
        key="platform:telegram",
        state="degraded",
        consecutive_failures=1,
        last_success_at=None,
        last_failure_at=100.0,
        next_probe_at=130.0,
        error_fingerprint=hashlib.sha256(b"ValueError:connection down").hexdigest()[:16],
        suppressed_failures=0,
    )


def test_repeated_failure_in_open_circuit_is_suppressed():
    registry = RuntimeHealthRegistry()
    registry.record_failure("mcp:server", "unavailable", now=0.0)
    _, transition_log = registry.record_failure("mcp:server", "unavailable", now=1.0)

    snapshot, should_log = registry.record_failure("mcp:server", "unavailable", now=2.0)

    assert transition_log is True
    assert should_log is False
    assert snapshot.state == "open_circuit"
    assert snapshot.consecutive_failures == 3
    assert snapshot.suppressed_failures == 1
    assert snapshot.next_probe_at == 122.0


def test_changed_fingerprint_logs_and_clears_duplicate_suppression():
    registry = RuntimeHealthRegistry()
    registry.record_failure("mcp:server", "unavailable", now=0.0)
    registry.record_failure("mcp:server", "unavailable", now=1.0)
    registry.record_failure("mcp:server", "unavailable", now=2.0)

    snapshot, should_log = registry.record_failure("mcp:server", "timed out", now=3.0)

    assert should_log is True
    assert snapshot.state == "open_circuit"
    assert snapshot.consecutive_failures == 4
    assert snapshot.suppressed_failures == 0
    assert snapshot.error_fingerprint == hashlib.sha256(b"str:timed out").hexdigest()[:16]


def test_backoff_is_bounded_after_five_failures():
    registry = RuntimeHealthRegistry()
    expected_delays = [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]

    for now, delay in enumerate(expected_delays):
        snapshot, _ = registry.record_failure("platform:slack", "down", now=float(now))
        assert snapshot.next_probe_at == now + delay


def test_should_probe_opens_one_probe_window_and_success_resets_state():
    registry = RuntimeHealthRegistry()
    registry.record_failure("platform:discord", "down", now=100.0)

    assert registry.should_probe("platform:discord", now=129.0) is False
    assert registry.should_probe("platform:discord", now=130.0) is True
    assert registry.snapshot()["platform:discord"].state == "probing"
    assert registry.should_probe("platform:discord", now=130.0) is False

    snapshot = registry.record_success("platform:discord", now=131.0)

    assert snapshot.state == "healthy"
    assert snapshot.consecutive_failures == 0
    assert snapshot.last_success_at == 131.0
    assert snapshot.last_failure_at == 100.0
    assert snapshot.next_probe_at == 0.0
    assert snapshot.error_fingerprint == ""
    assert snapshot.suppressed_failures == 0
    assert registry.should_probe("platform:discord", now=132.0) is True


def test_success_on_new_key_creates_healthy_snapshot():
    snapshot = RuntimeHealthRegistry().record_success("tool:search", now=42.0)

    assert snapshot.state == "healthy"
    assert snapshot.key == "tool:search"
    assert snapshot.consecutive_failures == 0
    assert snapshot.last_success_at == 42.0
    assert snapshot.last_failure_at is None


def test_keys_are_independent():
    registry = RuntimeHealthRegistry()
    registry.record_failure("platform:a", "down", now=0.0)

    healthy = registry.record_success("platform:b", now=1.0)
    snapshots = registry.snapshot()

    assert healthy.state == "healthy"
    assert snapshots["platform:a"].state == "degraded"
    assert snapshots["platform:a"].consecutive_failures == 1
    assert snapshots["platform:b"] == healthy


def test_snapshots_are_immutable_and_returned_as_a_copy():
    registry = RuntimeHealthRegistry()
    registry.record_success("tool:search", now=1.0)

    snapshot = registry.snapshot()
    with pytest.raises(FrozenInstanceError):
        snapshot["tool:search"].state = "degraded"

    snapshot.clear()
    assert "tool:search" in registry.snapshot()


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("record_success", ("key", 0.0)),
        ("record_failure", ("key", "down", 0.0)),
        ("should_probe", ("key", 0.0)),
    ],
)
def test_health_timestamps_are_keyword_only(method, args):
    with pytest.raises(TypeError):
        getattr(RuntimeHealthRegistry(), method)(*args)
