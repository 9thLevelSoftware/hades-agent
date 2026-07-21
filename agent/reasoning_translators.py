"""Pure reasoning-effort translators shared by discovery and request paths.

These helpers deliberately contain no provider discovery or request I/O.  A
request builder supplies the model/catalog facts it already resolved, while
reasoning-support inventory reuses the identical translation instead of
maintaining a second alias table.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def translate_codex_reasoning_effort(
    model: str,
    requested_effort: Any,
    *,
    is_xai_responses: bool = False,
) -> Any:
    """Return the Responses API effort emitted for a Hermes effort value."""
    effort = requested_effort or "medium"
    clamps: dict[str, str] = {"minimal": "low"}
    if "gpt-5.6" in (model or "").lower():
        clamps["ultra"] = "max"
    if is_xai_responses:
        clamps.update({"xhigh": "high", "max": "high", "ultra": "high"})
    return clamps.get(effort, effort)


def translate_github_reasoning_effort(
    requested_effort: Any,
    supported_efforts: Sequence[str] | None,
) -> str | None:
    """Clamp a requested effort to the authenticated GitHub model catalog.

    GitHub catalogs are model-specific.  Unsupported generic strengths fall
    back exactly as the request path historically did: xhigh prefers high,
    minimal prefers low, and every other unsupported value prefers medium
    before falling back to the catalog's first entry.
    """
    supported = tuple(
        dict.fromkeys(
            str(effort).strip().lower()
            for effort in (supported_efforts or ())
            if str(effort).strip()
        )
    )
    if not supported:
        return None

    effort = str(requested_effort or "medium").strip().lower()
    if effort == "xhigh" and "high" in supported:
        return "high"
    if effort in supported:
        return effort
    if effort == "minimal" and "low" in supported:
        return "low"
    if "medium" in supported:
        return "medium"
    return supported[0]


def translate_kimi_reasoning_effort(reasoning_config: Any) -> str | None:
    """Return Kimi's top-level reasoning_effort, or None when disabled."""
    if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is False:
        return None

    effort = "medium"
    if isinstance(reasoning_config, dict):
        requested = str(reasoning_config.get("effort") or "").strip().lower()
        if requested in {"low", "medium", "high"}:
            effort = requested
    return effort


__all__ = [
    "translate_codex_reasoning_effort",
    "translate_github_reasoning_effort",
    "translate_kimi_reasoning_effort",
]
