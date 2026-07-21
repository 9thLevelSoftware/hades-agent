"""Skill inject budget + secret config redaction (audit L4-02 / L4-06)."""

from __future__ import annotations

from agent.skill_commands import (
    _apply_skill_inject_budget,
    _inject_skill_config,
    _is_secretish_config_key,
)


def test_secretish_config_key_detection():
    assert _is_secretish_config_key("api_key") is True
    assert _is_secretish_config_key("OPENAI_API_KEY") is True
    assert _is_secretish_config_key("refresh_token") is True
    assert _is_secretish_config_key("db_password") is True
    assert _is_secretish_config_key("workspace_root") is False
    assert _is_secretish_config_key("model_name") is False


def test_inject_skill_config_redacts_secret_values(monkeypatch):
    parts: list[str] = []
    loaded = {
        "raw_content": (
            "---\n"
            "name: demo\n"
            "metadata:\n"
            "  hermes:\n"
            "    config:\n"
            "      - key: api_key\n"
            "        description: secret\n"
            "      - key: workspace\n"
            "        description: path\n"
            "---\n"
            "body\n"
        )
    }

    monkeypatch.setattr(
        "agent.skill_utils.resolve_skill_config_values",
        lambda vars_: {"api_key": "sk-super-secret", "workspace": "/tmp/proj"},
    )
    monkeypatch.setattr(
        "agent.skill_utils.extract_skill_config_vars",
        lambda fm: [
            {"key": "api_key", "description": "secret"},
            {"key": "workspace", "description": "path"},
        ],
    )
    monkeypatch.setattr(
        "agent.skill_utils.parse_frontmatter",
        lambda raw: ({}, "body"),
    )

    _inject_skill_config(loaded, parts)
    block = "\n".join(parts)
    assert "sk-super-secret" not in block
    assert "api_key = (set)" in block
    assert "workspace = /tmp/proj" in block


def test_skill_inject_budget_truncates_large_message():
    huge = "x" * 100_000
    out = _apply_skill_inject_budget(huge, {"max_inject_tokens": 100})  # ~400 chars
    assert len(out) < len(huge)
    assert "truncated" in out.lower()
    assert out.startswith("x")


def test_skill_inject_budget_noop_when_under_limit():
    msg = "small skill body"
    assert _apply_skill_inject_budget(msg, {"max_inject_tokens": 48000}) == msg
