"""Kanban writable_roots path safety for codex app-server spawn (audit L2-01)."""

from __future__ import annotations

import json

import pytest

from agent.transports.codex_app_server import _safe_kanban_writable_root


@pytest.mark.parametrize(
    "path",
    [
        "/Users/me/.hermes/kanban",
        "/var/lib/hades/kanban",
        "/tmp/board",
    ],
)
def test_safe_absolute_kanban_roots_accepted(path):
    safe = _safe_kanban_writable_root(path)
    assert safe is not None
    assert safe.startswith("/")
    # JSON-escaped form is embeddable in writable_roots=[...]
    encoded = json.dumps(safe)
    assert encoded.startswith('"')
    assert "\n" not in encoded


@pytest.mark.parametrize(
    "path",
    [
        None,
        "",
        "  ",
        "relative/kanban",
        '/tmp/evil","network_access=true',
        "/tmp/with'quote",
        "/tmp/with`backtick",
        "/tmp/with[brackets]",
        "/tmp/with\nnewline",
    ],
)
def test_unsafe_or_relative_kanban_roots_rejected(path):
    assert _safe_kanban_writable_root(path) is None
