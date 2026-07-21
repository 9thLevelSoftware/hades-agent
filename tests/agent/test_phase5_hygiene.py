"""Phase 5 hygiene unit tests (secret_scope, raise_if_read_blocked, azure marker)."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_global_env_prefix_excludes_secret_suffixes():
    from agent.secret_scope import _is_global_env

    assert _is_global_env("HERMES_TELEGRAM_BATCH_DELAY") is True
    assert _is_global_env("HERMES_TELEGRAM_BOT_TOKEN") is False
    assert _is_global_env("TERMINAL_TIMEOUT") is True
    assert _is_global_env("TERMINAL_API_KEY") is False
    assert _is_global_env("PATH") is True


def test_raise_if_read_blocked_fail_closed_for_env_basename(monkeypatch, tmp_path):
    from agent import file_safety as fs

    def boom(path):
        raise RuntimeError("classifier broke")

    monkeypatch.setattr(fs, "get_read_block_error", boom)
    with pytest.raises(ValueError, match="credential-like"):
        fs.raise_if_read_blocked(str(tmp_path / ".env"))


def test_raise_if_read_blocked_fail_open_for_normal_file(monkeypatch, tmp_path):
    from agent import file_safety as fs

    def boom(path):
        raise RuntimeError("classifier broke")

    monkeypatch.setattr(fs, "get_read_block_error", boom)
    # Should not raise for ordinary sources
    fs.raise_if_read_blocked(str(tmp_path / "main.py"))


def test_is_token_provider_respects_marker():
    from agent.azure_identity_adapter import is_token_provider

    def plain():
        return "tok"

    assert is_token_provider(plain) is True  # legacy bare callable
    plain._hades_token_provider = True
    assert is_token_provider(plain) is True
    assert is_token_provider("sk-static") is False


def test_safe_error_text_redacts(monkeypatch):
    from agent.trace_upload import _safe_error_text

    class E(Exception):
        pass

    # Line-start env assignment is a known redactor target; avoid sk-*/Bearer
    # fixtures that trip secret scanners.
    text = _safe_error_text(E("api_key=abcdefghabcdefgh"))
    assert "abcdefghabcdefgh" not in text
    assert "api_key=" in text
