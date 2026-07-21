"""External prefetch last-good cache + deferred sync (audit L1-03 / L1-04)."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class _SlowExternal(MemoryProvider):
    name = "slow-external"

    def __init__(self, gate: threading.Event, result: str = "RECALL"):
        self._gate = gate
        self._result = result
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def initialize(self, **kwargs) -> None:
        return None

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self.calls += 1
        # Block until gate is set (simulates wedged network).
        self._gate.wait(timeout=30)
        return self._result

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        return None

    def sync_turn(self, user_content: str, assistant_content: str, **kwargs) -> None:
        return None

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        return "{}"


def test_external_prefetch_returns_last_good_while_stuck():
    gate = threading.Event()
    provider = _SlowExternal(gate, result="CACHED-RECALL")
    mgr = MemoryManager(external_prefetch_timeout=0.15)
    mgr.add_provider(provider)

    # First call will time out while still running.
    assert mgr.prefetch_all("hello") == ""
    assert provider.calls == 1

    # Unblock first call and let it store last-good.
    gate.set()
    # Give the daemon thread a moment to finish.
    for _ in range(50):
        if provider.name in mgr._last_external_prefetch:
            break
        time.sleep(0.02)
    # Force a second in-flight by resetting gate mid-second-call is complex;
    # instead set last-good manually and simulate stuck thread.
    mgr._last_external_prefetch[provider.name] = "CACHED-RECALL"
    stuck = threading.Thread(target=lambda: time.sleep(10), daemon=True)
    stuck.start()
    mgr._external_prefetch_threads[provider.name] = stuck
    out = mgr.prefetch_all("hello again")
    assert out == "CACHED-RECALL"


def test_submit_background_defers_external_write_without_executor(monkeypatch):
    mgr = MemoryManager()
    mgr._has_external = True
    mgr._shutting_down = False
    ran = {"n": 0}

    def _fn():
        ran["n"] += 1

    monkeypatch.setattr(mgr, "_get_sync_executor", lambda: None)
    mgr._submit_background(_fn, kind="write")
    assert ran["n"] == 0
    assert len(mgr._deferred_background) == 1
