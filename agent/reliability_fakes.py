"""Deterministic fault fixtures for the harness reliability scenarios.

Every fake here moves only when told to: no wall-clock reads, no real
threads, no randomness. Faults are scheduled up front and fire exactly
once, so a scenario replays identically every run. Each fake exposes a
``reset()`` that returns it to its constructed state for repeatable use
across scenarios.

Lives in ``agent/`` (not ``tests/``) because the CLI
``hermes reliability check`` runs these scenarios in-process as part
of the matrix readback. The CLI is offline by contract — it does not
spin up the network, the model adapters, or the time module. These
fakes are the only clock/transport/db the matrix touches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class FakeDBClosedError(RuntimeError):
    """Raised when a scenario touches a DB handle after it has closed."""


class FakeClock:
    """A clock that only advances when explicitly told to.

    ``time()`` and ``monotonic()`` return the same virtual seconds so a
    scenario can wire both to this one source of truth.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._start = float(start)
        self._now = float(start)

    def time(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("cannot advance a clock backwards")
        self._now += float(seconds)
        return self._now

    def reset(self) -> None:
        self._now = self._start


class FakeFuture:
    """A tool future that can be cancelled and can complete late.

    Models the race the harness cares about: a call is cancelled, then its
    underlying work finishes anyway. ``late_completion`` flags exactly that
    so a scenario can count a cancelled-but-still-ran side effect.
    """

    def __init__(self) -> None:
        self._done = False
        self._cancelled = False
        self._has_result = False
        self._result: Any = None
        self.late_completion = False

    @property
    def done(self) -> bool:
        return self._done

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> bool:
        if self._done:
            return False
        self._cancelled = True
        self._done = True
        return True

    def complete(self, value: Any) -> None:
        if self._cancelled:
            self.late_completion = True
        self._has_result = True
        self._result = value
        self._done = True

    def result(self) -> Any:
        if not self._has_result:
            raise LookupError("future has no result")
        return self._result


@dataclass(frozen=True)
class RateLimitResponse:
    """A provider 429 carrying the retry hint the harness should honor."""

    retry_after: float
    status: int = 429


class FakeProvider:
    """A provider that serves N rate-limits, then succeeds.

    Faults are front-loaded and deterministic: the first
    ``rate_limit_before_success`` calls return a ``RateLimitResponse``; every
    call after that returns a success dict.
    """

    def __init__(
        self, rate_limit_before_success: int = 0, retry_after: float = 1.0
    ) -> None:
        self._limit = int(rate_limit_before_success)
        self._retry_after = float(retry_after)
        self.calls = 0

    def send(self, payload: Any = None) -> Any:
        self.calls += 1
        if self.calls <= self._limit:
            return RateLimitResponse(retry_after=self._retry_after)
        return {"ok": True, "payload": payload}

    def reset(self) -> None:
        self.calls = 0


class FakeDelivery:
    """A delivery channel that drops acks on scheduled attempts.

    The message IS delivered every call (recorded in ``deliveries``); only the
    acknowledgement is lost on attempts listed in ``lose_acks_on``. That is
    the dangerous case: the sender retries and produces a duplicate side
    effect because it never learned the first attempt landed.
    """

    def __init__(self, lose_acks_on: tuple[int, ...] = ()) -> None:
        self._lose_acks_on = set(lose_acks_on)
        self.attempts = 0
        self.deliveries: list[Any] = []

    def send(self, message: Any = None) -> dict[str, Any] | None:
        self.attempts += 1
        self.deliveries.append(message)
        if self.attempts in self._lose_acks_on:
            return None
        return {"acked": True, "attempt": self.attempts}

    def reset(self) -> None:
        self.attempts = 0
        self.deliveries = []


class FakeDBHandle:
    """A DB handle that closes mid-scenario, deterministically.

    Closes either after ``close_after`` successful ops or on an explicit
    ``close()``. Any op on a closed handle raises ``FakeDBClosedError`` so a
    scenario can assert the harness fails once rather than looping.
    """

    def __init__(self, close_after: int | None = None) -> None:
        self._close_after = close_after
        self.ops = 0
        self.is_open = True

    def execute(self) -> int:
        if not self.is_open:
            raise FakeDBClosedError("db handle is closed")
        self.ops += 1
        if self._close_after is not None and self.ops >= self._close_after:
            self.is_open = False
        return self.ops

    def close(self) -> None:
        self.is_open = False

    def reset(self) -> None:
        self.ops = 0
        self.is_open = True


@dataclass(frozen=True)
class ScenarioRow:
    """One scenario's readback — what happened vs. what should have.

    Used as the wire format for ``hermes reliability check --json`` and as
    the row shape the matrix renderer walks. Kept as a dataclass so the
    JSON serializer picks it up cleanly.
    """

    scenario: str
    passed: bool
    unresolved: bool
    wrong_side_effect_count: int
    recovery_steps: tuple[str, ...] = field(default_factory=tuple)
