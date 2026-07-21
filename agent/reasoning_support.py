"""Content-free discovery of exactly controllable reasoning effort levels.

Provider request translation stays in the existing Hermes transports.  This
module inventories only the generic inputs those translators can map without
guessing, so callers can fail closed before projecting a runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent.reasoning_translators import (
    translate_codex_reasoning_effort,
    translate_github_reasoning_effort,
)


REASONING_SUPPORT_CONTRACT_VERSION = 1

_GENERIC_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
_CONTROL_EFFORTS = tuple(effort for effort in _GENERIC_EFFORTS if effort != "none")
_GITHUB_PROVIDERS = {"copilot", "copilot-acp", "github", "github-models"}
_XAI_PROVIDERS = {"xai", "xai-oauth"}


@dataclass(frozen=True)
class ReasoningSupport:
    """Immutable exact reasoning controls and their provider wire aliases."""

    efforts: tuple[str, ...]
    provider_aliases: tuple[tuple[str, str], ...]
    provenance: str
    exact: bool


def _empty(provenance: str) -> ReasoningSupport:
    return ReasoningSupport(
        efforts=(),
        provider_aliases=(),
        provenance=provenance,
        exact=False,
    )


def _normalized_options(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    normalized: list[str] = []
    for item in value:
        effort = str(item or "").strip().lower()
        if effort and effort not in normalized:
            normalized.append(effort)
    return tuple(normalized)


def _translated_aliases(
    options: tuple[str, ...],
    translate: Callable[[str], str | None],
) -> dict[str, str]:
    """Derive aliases from the request translator and selectable wire levels."""
    supported = set(options)
    aliases: dict[str, str] = {}
    for candidate in _CONTROL_EFFORTS:
        translated = translate(candidate)
        if translated in supported and translated != candidate:
            aliases[candidate] = translated
    return aliases


def _provider_aliases(
    provider: str,
    model: str,
    api_mode: str,
    options: tuple[str, ...],
) -> dict[str, str]:
    if provider == "lmstudio":
        return {
            alias: target
            for alias, target in (("off", "none"), ("on", "medium"))
            if target in options
        }

    if provider in _GITHUB_PROVIDERS:
        return _translated_aliases(
            options,
            lambda effort: translate_github_reasoning_effort(effort, options),
        )

    if provider in {"gemini", "google", "google-gemini"}:
        from agent.transports.chat_completions import _build_gemini_thinking_config

        def translate_gemini(effort: str) -> str | None:
            config = _build_gemini_thinking_config(
                model,
                {"enabled": True, "effort": effort},
            )
            if not isinstance(config, dict):
                return None
            value = config.get("thinkingLevel")
            return str(value) if value else None

        return _translated_aliases(options, translate_gemini)

    if provider in _XAI_PROVIDERS:
        return _translated_aliases(
            options,
            lambda effort: translate_codex_reasoning_effort(
                model,
                effort,
                is_xai_responses=True,
            ),
        )

    if api_mode == "codex_responses" or "gpt-5.6" in model:
        return _translated_aliases(
            options,
            lambda effort: translate_codex_reasoning_effort(model, effort),
        )

    return {}


def _with_options(
    *,
    provider: str,
    model: str,
    api_mode: str,
    options: tuple[str, ...],
    provenance: str,
) -> ReasoningSupport:
    if provider == "lmstudio":
        lm_aliases = {"off": "none", "on": "medium"}
        options = tuple(
            dict.fromkeys(lm_aliases.get(option, option) for option in options)
        )
    valid_options = tuple(option for option in options if option in _GENERIC_EFFORTS)
    if not valid_options:
        return _empty(f"{provenance}:no-controllable-efforts")
    aliases = _provider_aliases(provider, model, api_mode, valid_options)
    return ReasoningSupport(
        efforts=valid_options,
        provider_aliases=tuple(aliases.items()),
        provenance=provenance,
        exact=True,
    )


def _anthropic_reasoning_support(
    *,
    provider: str,
    model: str,
    api_mode: str,
    declared_options: tuple[str, ...],
) -> ReasoningSupport:
    from agent.anthropic_adapter import translate_anthropic_reasoning_effort

    mode, _ = translate_anthropic_reasoning_effort(model, "medium")
    if mode == "adaptive":
        candidates = ("low", "medium", "high", "xhigh", "max")
        efforts = tuple(
            effort
            for effort in candidates
            if translate_anthropic_reasoning_effort(model, effort)
            == ("adaptive", effort)
        )
    else:
        efforts = ("low", "medium", "high", "xhigh")

    if declared_options:
        declared_wire_values = {
            translate_anthropic_reasoning_effort(model, effort)
            for effort in declared_options
        }
        efforts = tuple(
            effort
            for effort in efforts
            if translate_anthropic_reasoning_effort(model, effort)
            in declared_wire_values
        )
    if not efforts:
        return _empty("hermes:anthropic-translator:no-controllable-efforts")

    canonical_by_wire = {
        translate_anthropic_reasoning_effort(model, effort): effort
        for effort in efforts
    }
    aliases: dict[str, str] = {}
    for candidate in _CONTROL_EFFORTS:
        target = canonical_by_wire.get(
            translate_anthropic_reasoning_effort(model, candidate)
        )
        if target is not None and target != candidate:
            aliases[candidate] = target

    provenance = (
        "metadata:reasoning_options+hermes:anthropic-translator"
        if declared_options
        else "hermes:anthropic-translator"
    )
    return ReasoningSupport(
        efforts=efforts,
        provider_aliases=tuple(aliases.items()),
        provenance=provenance,
        exact=True,
    )


def resolve_reasoning_support(
    *,
    provider: str,
    model: str,
    api_mode: str,
    metadata: Mapping[str, Any] | None = None,
) -> ReasoningSupport:
    """Return exact generic reasoning controls without inspecting content.

    A bare capability boolean is deliberately insufficient.  Exact options
    come from provider metadata or an existing Hermes translator whose accepted
    set and clamps are closed-world for the selected model/API mode.
    """
    provider_norm = str(provider or "").strip().lower()
    model_norm = str(model or "").strip().lower()
    api_mode_norm = str(api_mode or "").strip().lower()
    metadata = metadata if isinstance(metadata, Mapping) else {}

    if metadata.get("supports_reasoning") is False:
        return _empty("metadata:not-reasoning")

    options = _normalized_options(metadata.get("reasoning_options"))

    if provider_norm in _GITHUB_PROVIDERS:
        if options and metadata.get("reasoning_options_authenticated") is True:
            return _with_options(
                provider=provider_norm,
                model=model_norm,
                api_mode=api_mode_norm,
                options=options,
                provenance="metadata:reasoning_options",
            )
        return _empty("github-catalog:unauthenticated-or-missing")

    if provider_norm in _XAI_PROVIDERS:
        if api_mode_norm != "codex_responses":
            return _empty("hermes:xai-non-responses-surface")
        try:
            from agent.model_metadata import grok_supports_reasoning_effort

            supports_effort = grok_supports_reasoning_effort(model_norm)
        except Exception:
            supports_effort = False
        if not supports_effort:
            return _empty("hermes:xai-uncontrolled-model")
        return _with_options(
            provider=provider_norm,
            model=model_norm,
            api_mode=api_mode_norm,
            options=options or ("low", "medium", "high"),
            provenance=(
                "metadata:reasoning_options+hermes:xai-responses-allowlist"
                if options
                else "hermes:xai-responses-allowlist"
            ),
        )

    if provider_norm in {"anthropic", "bedrock"} or (
        api_mode_norm == "anthropic_messages" and "claude" in model_norm
    ):
        if "claude" not in model_norm:
            return _empty("hermes:anthropic-non-claude-uncontrolled")
        return _anthropic_reasoning_support(
            provider=provider_norm,
            model=model_norm,
            api_mode=api_mode_norm,
            declared_options=options,
        )

    if options:
        return _with_options(
            provider=provider_norm,
            model=model_norm,
            api_mode=api_mode_norm,
            options=options,
            provenance="metadata:reasoning_options",
        )

    if provider_norm in {"gemini", "google", "google-gemini"}:
        if not model_norm.startswith("gemini"):
            return _empty("hermes:gemini-non-reasoning-model")
        if model_norm.startswith(("gemini-3", "gemini-3.1")):
            return _with_options(
                provider=provider_norm,
                model=model_norm,
                api_mode=api_mode_norm,
                options=_CONTROL_EFFORTS,
                provenance="hermes:gemini-thinking-config",
            )
        return _empty("hermes:gemini-thinking-budget-uncontrolled")

    if provider_norm in {"kimi", "kimi-coding", "kimi-coding-cn", "moonshot"}:
        if api_mode_norm != "chat_completions":
            return _empty("hermes:kimi-non-chat-effort-surface")
        return _with_options(
            provider=provider_norm,
            model=model_norm,
            api_mode=api_mode_norm,
            options=("low", "medium", "high"),
            provenance="hermes:kimi-translator",
        )

    return _empty("uncontrolled-or-unknown")


__all__ = [
    "REASONING_SUPPORT_CONTRACT_VERSION",
    "ReasoningSupport",
    "resolve_reasoning_support",
]
