"""Exact, content-free reasoning-control discovery contracts."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_constants import resolve_reasoning_config
from agent.anthropic_adapter import build_anthropic_kwargs
from agent.lmstudio_reasoning import resolve_lmstudio_effort
from agent.reasoning_support import (
    REASONING_SUPPORT_CONTRACT_VERSION,
    ReasoningSupport,
    resolve_reasoning_support,
)
from agent.transports.chat_completions import (
    ChatCompletionsTransport,
    _build_gemini_thinking_config,
)
from agent.transports.codex import ResponsesApiTransport


WireTranslator = Callable[[str], Any]


def test_plugin_requested_effort_uses_generic_config_until_transport_projection() -> None:
    config = resolve_reasoning_config(
        {"agent": {"reasoning_effort": "high"}},
        "gpt-5.4",
        requested_effort="medium",
    )

    assert config == {"enabled": True, "effort": "medium"}
    assert set(config) == {"enabled", "effort"}
    kwargs = ResponsesApiTransport().build_kwargs(
        "gpt-5.4",
        [{"role": "user", "content": "hi"}],
        instructions="test",
        reasoning_config=config,
    )
    assert kwargs["reasoning"]["effort"] == "medium"


def _assert_advertised_controls_match_wire(
    support: ReasoningSupport,
    translate: WireTranslator,
) -> None:
    """Every advertised effort and alias must resolve on the request seam."""
    assert support.exact is True
    assert support.efforts

    for effort in support.efforts:
        assert translate(effort) is not None

    aliases = dict(support.provider_aliases)
    for alias, target in aliases.items():
        assert target in support.efforts
        assert translate(alias) == translate(target)


def _codex_wire_effort(model: str, effort: str, *, xai: bool = False) -> str | None:
    kwargs = ResponsesApiTransport().build_kwargs(
        model,
        [{"role": "user", "content": "hi"}],
        instructions="test",
        reasoning_config={"enabled": True, "effort": effort},
        is_xai_responses=xai,
    )
    reasoning = kwargs.get("reasoning")
    return reasoning.get("effort") if isinstance(reasoning, dict) else None


def _anthropic_wire_effort(model: str, effort: str) -> tuple[str, str | int] | None:
    kwargs = build_anthropic_kwargs(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        max_tokens=64,
        reasoning_config={"enabled": True, "effort": effort},
    )
    thinking = kwargs.get("thinking")
    if not isinstance(thinking, dict):
        return None
    if thinking.get("type") == "adaptive":
        output_config = kwargs.get("output_config")
        if not isinstance(output_config, dict):
            return None
        return ("adaptive", str(output_config.get("effort") or ""))
    return ("manual", int(thinking["budget_tokens"]))


def _gemini_wire_effort(model: str, effort: str) -> str | None:
    config = _build_gemini_thinking_config(
        model,
        {"enabled": True, "effort": effort},
    )
    if not isinstance(config, dict):
        return None
    return str(config.get("thinkingLevel") or "") or None


def _kimi_wire_effort(model: str, effort: str) -> str | None:
    kwargs = ChatCompletionsTransport().build_kwargs(
        model,
        [{"role": "user", "content": "hi"}],
        reasoning_config={"enabled": True, "effort": effort},
        is_kimi=True,
    )
    value = kwargs.get("reasoning_effort")
    return str(value) if value is not None else None


@pytest.mark.parametrize(
    ("provider", "model", "api_mode", "metadata", "translate"),
    [
        pytest.param(
            "openai-codex",
            "gpt-5.4",
            "codex_responses",
            {"reasoning_options": ["low", "medium", "high", "xhigh"]},
            lambda effort: _codex_wire_effort("gpt-5.4", effort),
            id="codex-responses",
        ),
        pytest.param(
            "anthropic",
            "claude-sonnet-4-6",
            "anthropic_messages",
            {},
            lambda effort: _anthropic_wire_effort("claude-sonnet-4-6", effort),
            id="anthropic-adaptive",
        ),
        pytest.param(
            "gemini",
            "gemini-3-flash-preview",
            "chat_completions",
            {},
            lambda effort: _gemini_wire_effort("gemini-3-flash-preview", effort),
            id="gemini-thinking-level",
        ),
        pytest.param(
            "kimi-coding",
            "kimi-k2.5",
            "chat_completions",
            {},
            lambda effort: _kimi_wire_effort("kimi-k2.5", effort),
            id="kimi-top-level-effort",
        ),
        pytest.param(
            "xai-oauth",
            "grok-4.5",
            "codex_responses",
            {},
            lambda effort: _codex_wire_effort("grok-4.5", effort, xai=True),
            id="xai-responses-allowlist",
        ),
        pytest.param(
            "lmstudio",
            "qwen3-14b",
            "chat_completions",
            {"reasoning_options": ["off", "on"]},
            lambda effort: resolve_lmstudio_effort(
                {"enabled": True, "effort": effort},
                ["off", "on"],
            ),
            id="lm-studio-allowed-options",
        ),
    ],
)
def test_reasoning_support_matches_actual_request_translator(
    provider: str,
    model: str,
    api_mode: str,
    metadata: dict[str, Any],
    translate: WireTranslator,
) -> None:
    support = resolve_reasoning_support(
        provider=provider,
        model=model,
        api_mode=api_mode,
        metadata=metadata,
    )

    _assert_advertised_controls_match_wire(support, translate)


def test_github_aliases_follow_authenticated_request_catalog(monkeypatch) -> None:
    from hermes_cli import models as models_module
    from run_agent import AIAgent

    for supported in (
        ("low", "medium", "high"),
        ("low", "medium", "high", "max"),
    ):
        monkeypatch.setattr(
            models_module,
            "github_model_reasoning_efforts",
            lambda _model, values=supported: list(values),
        )
        support = resolve_reasoning_support(
            provider="github-models",
            model="gpt-5.4",
            api_mode="codex_responses",
            metadata={
                "reasoning_options": supported,
                "reasoning_options_authenticated": True,
            },
        )

        def translate(effort: str) -> str | None:
            fake_agent = SimpleNamespace(
                model="gpt-5.4",
                reasoning_config={"enabled": True, "effort": effort},
            )
            payload = AIAgent._github_models_reasoning_extra_body(fake_agent)
            return payload.get("effort") if isinstance(payload, dict) else None

        _assert_advertised_controls_match_wire(support, translate)
        assert dict(support.provider_aliases).get("max", "max") == translate("max")


def test_legacy_anthropic_collapsed_efforts_match_manual_budget_translator() -> None:
    model = "claude-sonnet-4-5"
    support = resolve_reasoning_support(
        provider="anthropic",
        model=model,
        api_mode="anthropic_messages",
        metadata={},
    )
    aliases = dict(support.provider_aliases)

    _assert_advertised_controls_match_wire(
        support,
        lambda effort: _anthropic_wire_effort(model, effort),
    )
    for collapsed in ("minimal", "max", "ultra"):
        assert collapsed in aliases
        assert _anthropic_wire_effort(model, collapsed) == _anthropic_wire_effort(
            model,
            aliases[collapsed],
        )


def test_xai_reasoning_is_exact_only_on_controllable_responses_surface() -> None:
    chat_support = resolve_reasoning_support(
        provider="xai-oauth",
        model="grok-4.5",
        api_mode="chat_completions",
        metadata={"reasoning_options": ["low", "medium", "high"]},
    )
    uncontrolled_model = resolve_reasoning_support(
        provider="xai-oauth",
        model="grok-4",
        api_mode="codex_responses",
        metadata={"reasoning_options": ["low", "medium", "high"]},
    )

    assert chat_support.exact is False
    assert chat_support.efforts == ()
    assert uncontrolled_model.exact is False
    assert uncontrolled_model.efforts == ()


def test_unauthenticated_github_options_do_not_prove_exact_control() -> None:
    support = resolve_reasoning_support(
        provider="github-models",
        model="gpt-5.4",
        api_mode="codex_responses",
        metadata={"reasoning_options": ["low", "medium", "high"]},
    )

    assert support.exact is False
    assert support.efforts == ()


def test_reasoning_support_never_expands_a_bare_boolean() -> None:
    assert REASONING_SUPPORT_CONTRACT_VERSION == 1
    exact = resolve_reasoning_support(
        provider="openai-codex",
        model="gpt-5.4",
        api_mode="codex_responses",
        metadata={"reasoning_options": ["low", "medium", "high", "xhigh"]},
    )
    assert exact.exact is True
    assert exact.efforts == ("low", "medium", "high", "xhigh")

    unknown = resolve_reasoning_support(
        provider="custom",
        model="unknown-reasoner",
        api_mode="chat_completions",
        metadata={"supports_reasoning": True},
    )
    assert unknown.exact is False
    assert unknown.efforts == ()


def test_non_reasoning_model_is_not_controllable() -> None:
    support = resolve_reasoning_support(
        provider="gemini",
        model="gemma-3-27b-it",
        api_mode="chat_completions",
        metadata={"supports_reasoning": False},
    )

    assert support.exact is False
    assert support.efforts == ()
    assert dict(support.provider_aliases) == {}
