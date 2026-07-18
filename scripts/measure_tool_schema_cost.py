#!/usr/bin/env python3
"""Measure the aggregate tool-schema cost without invoking a model or tool."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from model_tools import _resolve_active_context_length, get_tool_definitions
from tools.mcp_oauth import suppress_interactive_oauth
from tools.mcp_tool import _lock, _servers, discover_mcp_tools, shutdown_mcp_servers
from tools.tool_search import (
    ToolSearchConfig,
    assemble_tool_defs,
    estimate_tokens_from_schemas,
    load_config,
)


_COST_KEYS = (
    "raw_tools",
    "raw_tokens",
    "visible_tools",
    "visible_tokens",
    "deferred_tools",
    "saved_tokens",
)


def measure_tool_schema_cost(
    tool_defs: Sequence[dict[str, Any]],
    *,
    config: ToolSearchConfig,
    context_length: int | None = None,
) -> dict[str, int]:
    """Return aggregate schema-cost measurements for one tool assembly.

    ``deferred_tools`` is the eligible deferrable catalog count, whether or not
    this assembly activated Tool Search.
    """
    raw_defs = list(tool_defs)
    assembly = assemble_tool_defs(
        raw_defs,
        context_length=context_length,
        config=config,
    )
    raw_tokens = estimate_tokens_from_schemas(raw_defs)
    visible_tokens = estimate_tokens_from_schemas(assembly.tool_defs)
    return {
        "raw_tools": len(raw_defs),
        "raw_tokens": raw_tokens,
        "visible_tools": len(assembly.tool_defs),
        "visible_tokens": visible_tokens,
        "deferred_tools": assembly.deferred_count,
        "saved_tokens": max(0, raw_tokens - visible_tokens),
    }


def _has_active_mcp_servers() -> bool:
    with _lock:
        return bool(_servers)


def _cleanup_mcp_servers() -> None:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            shutdown_mcp_servers()
    except Exception:
        pass


def _current_cost() -> dict[str, int]:
    if _has_active_mcp_servers():
        raise RuntimeError(
            "The schema-cost diagnostic cannot run in a process with active MCP "
            "servers; run scripts/measure_tool_schema_cost.py in a separate process."
        )

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with suppress_interactive_oauth():
                discover_mcp_tools()
                raw_defs = get_tool_definitions(
                    quiet_mode=True,
                    skip_tool_search_assembly=True,
                )
                result = measure_tool_schema_cost(
                    raw_defs,
                    config=load_config(),
                    context_length=_resolve_active_context_length(),
                )
    except BaseException:
        _cleanup_mcp_servers()
        raise

    _cleanup_mcp_servers()
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print aggregate JSON")
    args = parser.parse_args(argv)
    result = _current_cost()
    if args.json:
        print(json.dumps({key: result[key] for key in _COST_KEYS}))
    else:
        print(" ".join(f"{key}={result[key]}" for key in _COST_KEYS))
        print(
            "deferred_tools is the eligible deferrable catalog count; "
            "it may be nonzero when Tool Search is inactive."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
