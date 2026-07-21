"""ACP file:// attach must honor agent.file_safety read denylist (audit L2-05)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _resource_block(uri: str, name: str = "attach", mime_type: str | None = None):
    return SimpleNamespace(
        uri=uri,
        name=name,
        title=None,
        mime_type=mime_type,
    )


def test_acp_denies_auth_json_under_hades_home(tmp_path, monkeypatch):
    from acp_adapter import server as acp_server

    home = tmp_path / ".hades"
    home.mkdir()
    auth = home / "auth.json"
    auth.write_text('{"secret": "sk-test-should-not-leak"}', encoding="utf-8")
    monkeypatch.setenv("HADES_HOME", str(home))

    parts = acp_server._resource_link_to_parts(_resource_block(str(auth)))
    assert len(parts) == 1
    assert parts[0]["type"] == "text"
    body = parts[0]["text"]
    assert "Access denied" in body or "credential" in body.lower()
    assert "sk-test-should-not-leak" not in body


def test_acp_denies_project_env_basename(tmp_path, monkeypatch):
    from acp_adapter import server as acp_server

    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))

    env_file = tmp_path / "project" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("API_KEY=super-secret\n", encoding="utf-8")

    parts = acp_server._resource_link_to_parts(_resource_block(str(env_file)))
    assert len(parts) == 1
    assert "Access denied" in parts[0]["text"] or "secret-bearing" in parts[0]["text"].lower()
    assert "super-secret" not in parts[0]["text"]


def test_acp_allows_normal_workspace_file(tmp_path, monkeypatch):
    from acp_adapter import server as acp_server

    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))

    src = tmp_path / "project" / "main.py"
    src.parent.mkdir()
    src.write_text("print('hello')\n", encoding="utf-8")

    parts = acp_server._resource_link_to_parts(
        _resource_block(str(src), mime_type="text/x-python")
    )
    assert len(parts) == 1
    assert parts[0]["type"] == "text"
    assert "print('hello')" in parts[0]["text"]
    assert "Access denied" not in parts[0]["text"]
