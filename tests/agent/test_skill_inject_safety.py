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
    from agent.skill_commands import _apply_skill_body_budget

    huge = "x" * 100_000
    out = _apply_skill_body_budget(huge, {"max_inject_tokens": 100})  # ~400 chars
    assert len(out) < len(huge)
    assert "truncated" in out.lower()
    assert out.startswith("x")


def test_skill_inject_budget_noop_when_under_limit():
    from agent.skill_commands import _apply_skill_body_budget

    msg = "small skill body"
    assert _apply_skill_body_budget(msg, {"max_inject_tokens": 48000}) == msg


def test_build_skill_message_preserves_user_instruction_when_body_truncated(monkeypatch):
    """Tail scaffolding (user instruction) must survive body truncation."""
    from agent import skill_commands as sc

    monkeypatch.setattr(sc, "_load_skills_config", lambda: {"template_vars": False, "inline_shell": False, "max_inject_tokens": 50})
    monkeypatch.setattr(sc, "_inject_skill_config", lambda *a, **k: None)
    msg = sc._build_skill_message(
        {"content": "BODY" * 5000, "name": "demo"},
        skill_dir=None,
        activation_note='[IMPORTANT: The user has invoked the "demo" skill. The full skill content is loaded below.]',
        user_instruction="please do XYZ carefully",
    )
    assert "please do XYZ carefully" in msg
    assert "The user has provided the following instruction" in msg
