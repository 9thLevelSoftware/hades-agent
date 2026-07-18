"""In-process runtime health state for retryable capabilities."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, replace


_BACKOFF_SECONDS = (30.0, 60.0, 120.0, 240.0, 300.0)


@dataclass(frozen=True)
class HealthSnapshot:
    key: str
    state: str
    consecutive_failures: int
    last_success_at: float | None
    last_failure_at: float | None
    next_probe_at: float
    error_fingerprint: str
    suppressed_failures: int


class RuntimeHealthRegistry:
    """Thread-safe health snapshots keyed by runtime capability."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, HealthSnapshot] = {}

    @staticmethod
    def _timestamp(now: float | None) -> float:
        return time.time() if now is None else now

    @staticmethod
    def _fingerprint(error: BaseException | str) -> str:
        message = str(error).strip()
        normalized = f"{type(error).__name__}:{message}"
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def record_success(self, key: str, *, now: float | None = None) -> HealthSnapshot:
        timestamp = self._timestamp(now)
        with self._lock:
            previous = self._states.get(key)
            snapshot = HealthSnapshot(
                key=key,
                state="healthy",
                consecutive_failures=0,
                last_success_at=timestamp,
                last_failure_at=previous.last_failure_at if previous else None,
                next_probe_at=0.0,
                error_fingerprint="",
                suppressed_failures=0,
            )
            self._states[key] = snapshot
            return snapshot

    def record_failure(
        self,
        key: str,
        error: BaseException | str,
        *,
        now: float | None = None,
    ) -> tuple[HealthSnapshot, bool]:
        timestamp = self._timestamp(now)
        fingerprint = self._fingerprint(error)
        with self._lock:
            previous = self._states.get(key)
            failures = (previous.consecutive_failures if previous else 0) + 1
            state = "degraded" if previous is None or previous.state == "healthy" else "open_circuit"
            changed = (
                previous is None
                or previous.state != state
                or previous.error_fingerprint != fingerprint
            )
            snapshot = HealthSnapshot(
                key=key,
                state=state,
                consecutive_failures=failures,
                last_success_at=previous.last_success_at if previous else None,
                last_failure_at=timestamp,
                next_probe_at=timestamp + _BACKOFF_SECONDS[min(failures - 1, len(_BACKOFF_SECONDS) - 1)],
                error_fingerprint=fingerprint,
                suppressed_failures=(
                    previous.suppressed_failures + 1 if previous and not changed else 0
                ),
            )
            self._states[key] = snapshot
            return snapshot, changed

    def should_probe(self, key: str, *, now: float | None = None) -> bool:
        timestamp = self._timestamp(now)
        with self._lock:
            snapshot = self._states.get(key)
            if snapshot is None or snapshot.state == "healthy":
                return True
            if snapshot.state == "probing" or timestamp < snapshot.next_probe_at:
                return False
            self._states[key] = replace(snapshot, state="probing")
            return True

    def snapshot(self) -> dict[str, HealthSnapshot]:
        with self._lock:
            return dict(self._states)
