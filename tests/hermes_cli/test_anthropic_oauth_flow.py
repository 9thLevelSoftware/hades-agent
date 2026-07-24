"""Tests for Anthropic OAuth setup flow behavior."""

import os

from hades_cli.config import load_env, save_env_value


def test_run_anthropic_oauth_flow_prefers_claude_code_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HADES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "agent.anthropic_adapter.run_oauth_setup_token",
        lambda: "sk-ant-oat01-from-claude-setup",
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials",
        lambda: {
            "accessToken": "cc-access-token",
            "refreshToken": "cc-refresh-token",
            "expiresAt": 9999999999999,
        },
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.is_claude_code_token_valid",
        lambda creds: True,
    )

    from hades_cli.main import _run_anthropic_oauth_flow

    save_env_value("ANTHROPIC_TOKEN", "stale-env-token")
    monkeypatch.setenv("ANTHROPIC_TOKEN", "stale-process-token")
    assert _run_anthropic_oauth_flow(save_env_value) is True

    env_vars = load_env()
    assert "ANTHROPIC_TOKEN" not in env_vars
    assert "ANTHROPIC_API_KEY" not in env_vars
    assert "ANTHROPIC_TOKEN" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ
    dotenv = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "stale-env-token" not in dotenv
    assert "ANTHROPIC_TOKEN" not in dotenv
    assert "ANTHROPIC_API_KEY" not in dotenv
    output = capsys.readouterr().out
    assert "Claude Code credentials linked" in output


def test_run_anthropic_oauth_flow_manual_token_still_persists(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HADES_HOME", str(tmp_path))
    monkeypatch.setattr("agent.anthropic_adapter.run_oauth_setup_token", lambda: None)
    monkeypatch.setattr("agent.anthropic_adapter.read_claude_code_credentials", lambda: None)
    monkeypatch.setattr("agent.anthropic_adapter.is_claude_code_token_valid", lambda creds: False)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "sk-ant-oat01-manual-token")
    monkeypatch.setattr(
        "hades_cli.secret_prompt.masked_secret_prompt",
        lambda _prompt="": "sk-ant-oat01-manual-token",
    )

    from hades_cli.main import _run_anthropic_oauth_flow

    assert _run_anthropic_oauth_flow(save_env_value) is True

    env_vars = load_env()
    assert env_vars["ANTHROPIC_TOKEN"] == "sk-ant-oat01-manual-token"
    output = capsys.readouterr().out
    assert "Setup-token saved" in output
