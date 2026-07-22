"""Oneshot (-z) mode: send a prompt, get the final content block, exit.

Bypasses cli.py entirely.  No banner, no spinner, no session_id line,
no stderr chatter.  Just the agent's final text to stdout.

Toolsets = explicit --toolsets when provided, otherwise whatever the user has
configured for "cli" in `hermes tools`.
Rules / memory / AGENTS.md / preloaded skills = same as a normal chat turn.
Approvals = auto-bypassed (HERMES_YOLO_MODE=1 is set for the call).
Working directory = the user's CWD (AGENTS.md etc. resolve from there as usual).

Model / provider selection mirrors `hermes chat`:
    - Both optional. If omitted, use the user's configured default.
    - If both given, pair them exactly as given.
    - If only --model given, auto-detect the provider that serves it.
    - If only --provider given, error out (ambiguous — caller must pick a model).

Env var fallbacks (used when the corresponding arg is not passed):
    - HERMES_INFERENCE_MODEL
"""

from __future__ import annotations

import logging
import os
from hades_constants import env_get, env_set
import sys
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Optional

from hades_cli.fallback_config import get_fallback_chain


# EX_TEMPFAIL from sysexits.h. Non-interactive callers can distinguish a
# transient routing lease from an ordinary failed run and retry safely.
ONESHOT_RETRYABLE_EXIT_CODE = 75


def _normalize_toolsets(toolsets: object = None) -> list[str] | None:
    if not toolsets:
        return None

    raw_items = [toolsets] if isinstance(toolsets, str) else toolsets
    if not isinstance(raw_items, (list, tuple)):
        raw_items = [raw_items]

    normalized: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            normalized.extend(part.strip() for part in item.split(","))
        else:
            normalized.append(str(item).strip())

    return [item for item in normalized if item] or None


def _validate_explicit_toolsets(toolsets: object = None) -> tuple[list[str] | None, str | None]:
    normalized = _normalize_toolsets(toolsets)
    if normalized is None:
        return None, None

    try:
        from toolsets import validate_toolset
    except Exception as exc:
        return None, f"hermes -z: failed to validate --toolsets: {exc}\n"

    built_in = [name for name in normalized if validate_toolset(name)]
    unresolved = [name for name in normalized if name not in built_in]

    if unresolved:
        try:
            from hades_cli.plugins import discover_plugins

            discover_plugins()
            plugin_valid = [name for name in unresolved if validate_toolset(name)]
        except Exception:
            plugin_valid = []

        if plugin_valid:
            built_in.extend(plugin_valid)
            unresolved = [name for name in unresolved if name not in plugin_valid]

    if any(name in {"all", "*"} for name in built_in):
        ignored = [name for name in normalized if name not in {"all", "*"}]
        if ignored:
            sys.stderr.write(
                "hermes -z: --toolsets all enables every toolset; "
                f"ignoring additional entries: {', '.join(ignored)}\n"
            )
        return None, None

    mcp_names: set[str] = set()
    mcp_disabled: set[str] = set()
    if unresolved:
        try:
            from hades_cli.config import read_raw_config
            from hades_cli.tools_config import _parse_enabled_flag

            cfg = read_raw_config()
            mcp_servers = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), dict) else {}
            for name, server_cfg in mcp_servers.items():
                if not isinstance(server_cfg, dict):
                    continue
                if _parse_enabled_flag(server_cfg.get("enabled", True), default=True):
                    mcp_names.add(str(name))
                else:
                    mcp_disabled.add(str(name))
        except Exception:
            mcp_names = set()
            mcp_disabled = set()

    mcp_valid = [name for name in unresolved if name in mcp_names]
    disabled = [name for name in unresolved if name in mcp_disabled]
    unknown = [name for name in unresolved if name not in mcp_names and name not in mcp_disabled]
    valid = built_in + mcp_valid

    if unknown:
        sys.stderr.write(f"hermes -z: ignoring unknown --toolsets entries: {', '.join(unknown)}\n")
    if disabled:
        sys.stderr.write(
            "hermes -z: ignoring disabled MCP servers (set enabled: true in config.yaml to use): "
            f"{', '.join(disabled)}\n"
        )

    if not valid:
        return None, "hermes -z: --toolsets did not contain any valid toolsets.\n"

    return valid, None


def _write_usage_file(path: Optional[str], result: dict, failure: Optional[str] = None) -> None:
    """Best-effort JSON usage report for pipelines (``-z --usage-file``).

    Written even on failure so callers can always account for spend. Never
    raises — a broken usage write must not mask the run's own outcome.
    """
    if not path:
        return
    try:
        import json

        report = {
            "estimated_cost_usd": result.get("estimated_cost_usd"),
            "cost_status": result.get("cost_status"),
            "cost_source": result.get("cost_source"),
            "input_tokens": result.get("input_tokens"),
            "output_tokens": result.get("output_tokens"),
            "cache_read_tokens": result.get("cache_read_tokens"),
            "cache_write_tokens": result.get("cache_write_tokens"),
            "reasoning_tokens": result.get("reasoning_tokens"),
            "total_tokens": result.get("total_tokens"),
            "api_calls": result.get("api_calls"),
            "model": result.get("model"),
            "provider": result.get("provider"),
            "session_id": result.get("session_id"),
            "completed": result.get("completed"),
            "failed": bool(result.get("failed")) or failure is not None,
            # Billing-audit field: the service tier this run REQUESTED via
            # request_overrides.extra_body (e.g. OpenAI "flex"). None when
            # unset. Lets batch pipelines verify the tier they think they're
            # paying for actually went out on the wire (July 2026 incident:
            # a config-matching bug silently dropped flex -> 2.3x billing).
            "service_tier": result.get("service_tier"),
        }
        if failure is not None:
            report["failure"] = failure
        out = Path(path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def run_oneshot(
    prompt: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    toolsets: object = None,
    usage_file: Optional[str] = None,
) -> int:
    """Execute a single prompt and print only the final content block.

    Args:
        prompt: The user message to send.
        model: Optional model override. Falls back to HERMES_INFERENCE_MODEL
            env var, then config.yaml's model.default / model.model.
        provider: Optional provider override. Falls back to config.yaml's
            model.provider, then "auto".
        toolsets: Optional comma-separated string or iterable of toolsets.
        usage_file: Optional path; when set, a JSON usage report (estimated
            cost, token counts, model, api_calls) is written there after the
            run — even when the run fails — so pipelines can account for
            spend per invocation.

    Returns the exit code.  Caller should sys.exit() with the return.
    """
    # Silence every stdlib logger for the duration.  AIAgent, tools, and
    # provider adapters all log to stderr through the root logger; file
    # handlers added by setup_logging() keep working (they're attached to
    # the root logger's handler list, not affected by level), but no
    # bytes reach the terminal.
    logging.disable(logging.CRITICAL)

    # --provider without --model is ambiguous: carrying the user's configured
    # model across to a different provider is usually wrong (that provider may
    # not host it), and silently picking the provider's catalog default hides
    # the mismatch.  Require the caller to be explicit.  Validate BEFORE the
    # stderr redirect so the message actually reaches the terminal.
    env_model_early = env_get("HADES_INFERENCE_MODEL", "").strip()
    if provider and not ((model or "").strip() or env_model_early):
        sys.stderr.write(
            "hermes -z: --provider requires --model (or HERMES_INFERENCE_MODEL). "
            "Pass both explicitly, or neither to use your configured defaults.\n"
        )
        return 2

    explicit_toolsets, toolsets_error = _validate_explicit_toolsets(toolsets)
    if toolsets_error:
        sys.stderr.write(toolsets_error)
        return 2
    use_config_toolsets = _normalize_toolsets(toolsets) is None

    # Auto-approve any shell / tool approvals.  Non-interactive by
    # definition — a prompt would hang forever.
    env_set("HADES_YOLO_MODE", "1")
    env_set("HADES_ACCEPT_HOOKS", "1")

    # Redirect stderr AND stdout to devnull for the entire call tree.
    # We'll print the final response to the real stdout at the end.
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    devnull = open(os.devnull, "w", encoding="utf-8")

    response: Optional[str] = None
    result: dict = {}
    failure: BaseException | None = None
    try:
        with redirect_stdout(devnull), redirect_stderr(devnull):
            try:
                response, result = _run_agent(
                    prompt,
                    model=model,
                    provider=provider,
                    toolsets=explicit_toolsets,
                    use_config_toolsets=use_config_toolsets,
                )
            except BaseException as exc:  # noqa: BLE001
                # Capture anything that escapes the agent (including OSError
                # from prompt_toolkit/Vt100 when stdout is a non-TTY pipe,
                # KeyboardInterrupt, SystemExit, etc.) so we can surface it on
                # the real stderr instead of crashing past the redirect with a
                # traceback that the caller never sees. A silent exit in a
                # cron / SSH / subprocess context is the worst failure mode.
                # See #30623.
                failure = exc
    finally:
        try:
            devnull.close()
        except Exception:
            pass

    if failure is not None:
        # Re-raise control-flow exceptions so the parent handles them as usual
        # (Ctrl-C / explicit sys.exit() inside the agent).
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            _write_usage_file(usage_file, result, failure=repr(failure))
            raise failure
        from agent.runtime_routing import RuntimeRoutingDeferred

        if isinstance(failure, RuntimeRoutingDeferred):
            retry_hint = (
                f" after {failure.retry_after_seconds:g} seconds"
                if failure.retry_after_seconds is not None
                else " shortly"
            )
            message = f"Runtime routing is busy; retry{retry_hint}."
            _write_usage_file(usage_file, result, failure=message)
            real_stderr.write(f"hermes -z: {message}\n")
            real_stderr.flush()
            return ONESHOT_RETRYABLE_EXIT_CODE
        _write_usage_file(usage_file, result, failure=str(failure))
        real_stderr.write(f"hermes -z: agent failed: {failure}\n")
        real_stderr.flush()
        return 1

    _write_usage_file(usage_file, result)

    if response:
        real_stdout.write(response)
        if not response.endswith("\n"):
            real_stdout.write("\n")
        real_stdout.flush()

    if (result.get("failed") or result.get("partial")) and not (response or "").strip():
        return 2

    if not (response or "").strip():
        real_stderr.write("hermes -z: no final response was produced; treating the run as failed.\n")
        real_stderr.flush()
        return 1

    return 0


def _create_session_db_for_oneshot():
    """Best-effort SessionDB for ``hermes -z`` / oneshot mode.

    Oneshot bypasses ``HermesCLI._init_agent()``, so it must wire the SQLite
    session store itself. Without this, the ``session_search``/recall tool is
    advertised but every call returns "Session database not available.".
    """
    try:
        from hades_state import SessionDB

        return SessionDB()
    except Exception as exc:
        logging.debug("SQLite session store not available for oneshot mode: %s", exc)
        return None


def _run_agent(
    prompt: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    toolsets: object = None,
    use_config_toolsets: bool = True,
) -> tuple[str, dict]:
    """Build an AIAgent exactly like a normal CLI chat turn would, then
    run a single conversation.  Returns ``(final_response, run_result)``."""
    # Imports are local so they don't run when hermes is invoked for
    # other commands (keeps top-level CLI startup cheap).
    from hades_cli.config import load_config
    from hades_cli.models import detect_provider_for_model
    from hades_cli.runtime_provider import resolve_runtime_provider
    from hades_cli.tools_config import _get_platform_tools
    from run_agent import AIAgent

    cfg = load_config()

    # Resolve effective model: explicit arg → env var → config.
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        cfg_model = model_cfg
    else:
        cfg_model = model_cfg.get("default") or model_cfg.get("model") or ""

    env_model = env_get("HADES_INFERENCE_MODEL", "").strip()
    effective_model = (model or "").strip() or env_model or cfg_model

    # Resolve effective provider: explicit arg → (auto-detect from model if
    # model was explicit) → env / config (handled inside resolve_runtime_provider).
    #
    # When --model is given without --provider, auto-detect the provider that
    # serves that model — same semantic as `/model <name>` in an interactive
    # session.  Without this, resolve_runtime_provider() would fall back to
    # the user's configured default provider, which may not host the model
    # the caller just asked for.
    effective_provider = (provider or "").strip() or None
    explicit_base_url_from_alias: Optional[str] = None
    if effective_provider is None and (model or env_model):
        # Only auto-detect when the model was explicitly requested via arg or
        # env var (not when it came from config — that's the "use my defaults"
        # path and the configured provider is already correct).
        explicit_model = (model or "").strip() or env_model
        if explicit_model:
            # First check DIRECT_ALIASES populated from config.yaml `model_aliases:`.
            # These map a user-defined alias to (model, provider, base_url) for
            # endpoints not in any catalog (local servers, custom proxies, etc.).
            try:
                from hades_cli import model_switch as _ms
                _ms._ensure_direct_aliases()
                direct = _ms.DIRECT_ALIASES.get(explicit_model.strip().lower())
            except Exception:
                direct = None
            if direct is not None:
                effective_model = direct.model
                effective_provider = direct.provider
                if direct.base_url:
                    explicit_base_url_from_alias = direct.base_url.rstrip("/")
            else:
                cfg_provider = ""
                if isinstance(model_cfg, dict):
                    cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
                current_provider = (
                    cfg_provider
                    or env_get("HADES_INFERENCE_PROVIDER", "").strip().lower()
                    or "auto"
                )
                detected = detect_provider_for_model(explicit_model, current_provider)
                if detected:
                    effective_provider, effective_model = detected

    from agent.runtime_routing import (
        AgentRuntimeContext,
        runtime_resolver_requires_initial_task,
    )

    routing_enabled = runtime_resolver_requires_initial_task("fresh_session")
    manual_runtime_pin = bool(
        (isinstance(model, str) and model.strip())
        or (isinstance(provider, str) and provider.strip())
    )
    session_id = None
    routing_context = None
    if routing_enabled:
        # Keep this as a requested baseline.  AIAgent's routing preflight runs
        # before ordinary Hermes credential resolution, so an active projected
        # route is not vetoed by an unavailable configured provider.
        cfg_provider = ""
        cfg_base_url = ""
        if isinstance(model_cfg, dict):
            cfg_provider = str(model_cfg.get("provider") or "").strip()
            cfg_base_url = str(model_cfg.get("base_url") or "").strip()
        if effective_provider is None:
            effective_provider = (
                cfg_provider
                or env_get("HERMES_INFERENCE_PROVIDER", "").strip()
                or "auto"
            )
            requested_base_url = (
                explicit_base_url_from_alias or cfg_base_url or None
            )
        else:
            # An explicit/detected provider resolves its own endpoint.  Only a
            # direct alias carries a caller-selected base URL across.
            requested_base_url = explicit_base_url_from_alias
        requested_api_key = None
        requested_api_mode = None
        requested_credential_pool = None
        session_id = (
            f"oneshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
            f"{uuid.uuid4().hex[:6]}"
        )
        routing_context = AgentRuntimeContext(
            scope="fresh_session",
            task=prompt,
            session_id=session_id,
            task_id=session_id,
            manual_runtime_pin=manual_runtime_pin,
            manual_pin_source=(
                "oneshot_explicit_runtime" if manual_runtime_pin else None
            ),
            metadata={"platform": "cli"},
        )
    else:
        # No resolver: retain the historical one-shot construction path.
        runtime = resolve_runtime_provider(
            requested=effective_provider,
            target_model=effective_model or None,
            explicit_base_url=explicit_base_url_from_alias,
        )
        requested_api_key = runtime.get("api_key")
        requested_base_url = runtime.get("base_url")
        effective_provider = runtime.get("provider")
        requested_api_mode = runtime.get("api_mode")
        requested_credential_pool = runtime.get("credential_pool")

    # Pull in explicit toolsets when provided; otherwise use whatever the user
    # has enabled for "cli". sorted() gives stable ordering for config-derived
    # sets; explicit values preserve user order.
    toolsets_list = _normalize_toolsets(toolsets)
    if toolsets_list is None and use_config_toolsets:
        toolsets_list = sorted(_get_platform_tools(cfg, "cli"))

    session_db = _create_session_db_for_oneshot()
    # Read the effective fallback chain from profile config so oneshot workers
    # honour the same merge semantics as interactive CLI and gateway sessions.
    _fb = get_fallback_chain(cfg)

    agent = AIAgent(
        api_key=requested_api_key,
        base_url=requested_base_url,
        provider=effective_provider,
        api_mode=requested_api_mode,
        model=effective_model,
        enabled_toolsets=toolsets_list,
        quiet_mode=True,
        platform="cli",
        session_id=session_id,
        session_db=session_db,
        credential_pool=requested_credential_pool,
        fallback_model=_fb or None,
        runtime_routing_context=routing_context,
        # Interactive callbacks are intentionally NOT wired beyond this
        # one.  In oneshot mode there's no user sitting at a terminal:
        #   - clarify  → returns a synthetic "pick a default" instruction
        #                so the agent continues instead of stalling on
        #                the tool's built-in "not available" error
        #   - sudo password prompt → terminal_tool gates on
        #                HERMES_INTERACTIVE which we never set
        #   - shell-hook approval → auto-approved via HERMES_ACCEPT_HOOKS=1
        #                (set above); also falls back to deny on non-tty
        #   - dangerous-command approval → bypassed via HERMES_YOLO_MODE=1
        #   - skill secret capture → returns gracefully when no callback set
        clarify_callback=_oneshot_clarify_callback,
    )

    if routing_enabled and manual_runtime_pin:
        # The context suppresses policy selection for this construction; this
        # transition makes the explicit intent durable for the session and
        # restores ordinary Hermes fallback authority on the live agent.
        try:
            from agent.runtime_routing import (
                apply_manual_runtime_transition,
                constructor_runtime_spec,
            )

            runtime = constructor_runtime_spec(
                model=getattr(agent, "model", effective_model) or "",
                provider=getattr(agent, "provider", effective_provider),
                base_url=getattr(agent, "base_url", requested_base_url),
                api_key=getattr(agent, "api_key", None),
                api_mode=getattr(agent, "api_mode", None),
                acp_command=getattr(agent, "acp_command", None),
                acp_args=getattr(agent, "acp_args", None),
                credential_pool=getattr(agent, "_credential_pool", None),
                reasoning_config=getattr(agent, "reasoning_config", None),
                fallback_model=_fb,
            )
            apply_manual_runtime_transition(
                agent,
                session_id=session_id,
                source="oneshot_explicit_runtime",
                runtime=runtime,
                fallback_model=_fb,
            )
        except Exception:
            logging.warning(
                "Could not persist explicit oneshot runtime pin",
                exc_info=True,
            )

    # Belt-and-braces: make sure AIAgent doesn't invoke any streaming
    # display callbacks that would bypass our stdout capture.
    agent.suppress_status_output = True
    agent.stream_delta_callback = None
    agent.tool_gen_callback = None

    if routing_enabled:
        result = agent.run_conversation(prompt, task_id=session_id)
    else:
        result = agent.run_conversation(prompt)
    return (result.get("final_response") or "", result)


def _oneshot_clarify_callback(question: str, choices=None) -> str:
    """Clarify is disabled in oneshot mode — tell the agent to pick a
    default and proceed instead of stalling or erroring."""
    if choices:
        return (
            f"[oneshot mode: no user available. Pick the best option from "
            f"{choices} using your own judgment and continue.]"
        )
    return (
        "[oneshot mode: no user available. Make the most reasonable "
        "assumption you can and continue.]"
    )
