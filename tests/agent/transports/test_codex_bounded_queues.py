"""Bounded notification queues for CodexAppServerClient (audit L2-03)."""

from __future__ import annotations

import queue
from unittest.mock import MagicMock

from agent.transports.codex_app_server import CodexAppServerClient


def _client_without_spawn():
    """Build a client object without spawning the real codex binary."""
    client = object.__new__(CodexAppServerClient)
    client._notifications = queue.Queue(maxsize=2)
    client._server_requests = queue.Queue(maxsize=2)
    client._dropped_notifications = 0
    client._dropped_server_requests = 0
    client._pending = {}
    client._pending_lock = __import__("threading").Lock()
    return client


def test_put_bounded_drops_oldest_on_overflow():
    client = _client_without_spawn()
    client._put_bounded(client._notifications, {"method": "a"}, kind="notification")
    client._put_bounded(client._notifications, {"method": "b"}, kind="notification")
    # Overflow — should drop "a" and keep "b" then "c"
    client._put_bounded(client._notifications, {"method": "c"}, kind="notification")
    assert client._dropped_notifications == 1
    first = client._notifications.get_nowait()
    second = client._notifications.get_nowait()
    assert first["method"] == "b"
    assert second["method"] == "c"
    assert client._notifications.empty()


def test_dispatch_routes_notification_and_server_request():
    client = _client_without_spawn()
    client._dispatch({"method": "item/agentMessage/delta", "params": {}})
    client._dispatch({"id": 7, "method": "item/commandExecution/requestApproval", "params": {}})
    assert client._notifications.qsize() == 1
    assert client._server_requests.qsize() == 1
