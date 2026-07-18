from __future__ import annotations

from contextlib import nullcontext
import json
from typing import Any

import pytest


def _td(name: str, description: str = "tool") -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_measure_tool_schema_cost_reports_aggregates(monkeypatch):
    from scripts.measure_tool_schema_cost import measure_tool_schema_cost
    from tools import tool_search

    raw_defs = [_td("core_tool"), _td("deferred_tool", "x" * 200)]
    monkeypatch.setattr(
        tool_search,
        "classify_tools",
        lambda defs: ([defs[0]], [defs[1]]),
    )

    result = measure_tool_schema_cost(
        raw_defs,
        config=tool_search.ToolSearchConfig.from_raw({"enabled": "on"}),
        context_length=200_000,
    )

    assert set(result) == {
        "raw_tools",
        "raw_tokens",
        "visible_tools",
        "visible_tokens",
        "deferred_tools",
        "saved_tokens",
    }
    assert result["raw_tools"] == len(raw_defs)
    assert result["raw_tokens"] > 0
    assert result["visible_tools"] == 4  # one upfront tool plus three bridges
    assert result["visible_tokens"] > 0
    assert result["deferred_tools"] == 1
    assert result["saved_tokens"] == max(
        0, result["raw_tokens"] - result["visible_tokens"]
    )


def test_measure_tool_schema_cost_keeps_all_tools_when_threshold_skips(monkeypatch):
    from scripts.measure_tool_schema_cost import measure_tool_schema_cost
    from tools import tool_search

    raw_defs = [_td("core_tool"), _td("deferred_tool", "x" * 200)]
    monkeypatch.setattr(
        tool_search,
        "classify_tools",
        lambda defs: ([defs[0]], [defs[1]]),
    )

    result = measure_tool_schema_cost(
        raw_defs,
        config=tool_search.ToolSearchConfig.from_raw(
            {"enabled": "auto", "absolute_threshold_tokens": 20_000}
        ),
        context_length=200_000,
    )

    assert result["visible_tools"] == result["raw_tools"]
    assert result["visible_tokens"] == result["raw_tokens"]
    # This is the eligible deferrable catalog count, even when auto assembly
    # is inactive.
    assert result["deferred_tools"] == 1
    assert result["saved_tokens"] == 0


def test_current_cost_suppresses_discovery_and_shuts_down_after_measurement(
    monkeypatch, capsys
):
    from scripts import measure_tool_schema_cost as script
    from tools import tool_search

    calls = []

    class SuppressInteractiveOAuth:
        def __enter__(self):
            calls.append("suppress_enter")
            return self

        def __exit__(self, *_exc):
            calls.append("suppress_exit")

    monkeypatch.setattr(
        script,
        "suppress_interactive_oauth",
        lambda: SuppressInteractiveOAuth(),
    )

    def discover():
        print("discover diagnostic")
        calls.append("discover")

    def get_tool_definitions(**kwargs):
        print("get diagnostic")
        calls.append("get_tool_definitions")
        return []

    def measure(*args, **kwargs):
        print("measure diagnostic")
        calls.append("measure")
        return {}

    def shutdown():
        print("shutdown diagnostic")
        calls.append("shutdown")

    monkeypatch.setattr(script, "discover_mcp_tools", discover)
    monkeypatch.setattr(script, "get_tool_definitions", get_tool_definitions)
    monkeypatch.setattr(
        script,
        "load_config",
        lambda: tool_search.ToolSearchConfig.from_raw({}),
    )
    monkeypatch.setattr(script, "_resolve_active_context_length", lambda: 0)
    monkeypatch.setattr(script, "measure_tool_schema_cost", measure)
    monkeypatch.setattr(script, "shutdown_mcp_servers", shutdown)

    assert script._current_cost() == {}

    assert calls == [
        "suppress_enter",
        "discover",
        "get_tool_definitions",
        "measure",
        "suppress_exit",
        "shutdown",
    ]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_current_cost_shuts_down_when_discovery_raises(monkeypatch):
    from scripts import measure_tool_schema_cost as script

    calls = []
    monkeypatch.setattr(script, "suppress_interactive_oauth", nullcontext)

    def fail_discovery():
        calls.append("discover")
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(script, "discover_mcp_tools", fail_discovery)
    monkeypatch.setattr(script, "shutdown_mcp_servers", lambda: calls.append("shutdown"))

    with pytest.raises(RuntimeError, match="discovery failed"):
        script._current_cost()

    assert calls == ["discover", "shutdown"]


def test_current_cost_shuts_down_when_measurement_raises(monkeypatch):
    from scripts import measure_tool_schema_cost as script
    from tools import tool_search

    calls = []
    monkeypatch.setattr(script, "suppress_interactive_oauth", nullcontext)
    monkeypatch.setattr(script, "discover_mcp_tools", lambda: calls.append("discover"))
    monkeypatch.setattr(
        script,
        "get_tool_definitions",
        lambda **kwargs: calls.append("get_tool_definitions") or [],
    )
    monkeypatch.setattr(
        script,
        "load_config",
        lambda: tool_search.ToolSearchConfig.from_raw({}),
    )
    monkeypatch.setattr(script, "_resolve_active_context_length", lambda: 0)

    def fail_measurement(*args, **kwargs):
        calls.append("measure")
        raise RuntimeError("measurement failed")

    monkeypatch.setattr(script, "measure_tool_schema_cost", fail_measurement)
    monkeypatch.setattr(script, "shutdown_mcp_servers", lambda: calls.append("shutdown"))

    with pytest.raises(RuntimeError, match="measurement failed"):
        script._current_cost()

    assert calls == ["discover", "get_tool_definitions", "measure", "shutdown"]


def test_current_cost_preserves_body_error_when_cleanup_also_raises(monkeypatch):
    from scripts import measure_tool_schema_cost as script

    monkeypatch.setattr(script, "suppress_interactive_oauth", nullcontext)
    monkeypatch.setattr(
        script,
        "discover_mcp_tools",
        lambda: (_ for _ in ()).throw(RuntimeError("body failed")),
    )
    monkeypatch.setattr(
        script,
        "shutdown_mcp_servers",
        lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    with pytest.raises(RuntimeError, match="body failed"):
        script._current_cost()


def test_current_cost_keeps_result_when_cleanup_raises(monkeypatch):
    from scripts import measure_tool_schema_cost as script
    from tools import tool_search

    result = {"raw_tools": 1}
    monkeypatch.setattr(script, "suppress_interactive_oauth", nullcontext)
    monkeypatch.setattr(script, "discover_mcp_tools", lambda: None)
    monkeypatch.setattr(script, "get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr(
        script,
        "load_config",
        lambda: tool_search.ToolSearchConfig.from_raw({}),
    )
    monkeypatch.setattr(script, "_resolve_active_context_length", lambda: 0)
    monkeypatch.setattr(script, "measure_tool_schema_cost", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        script,
        "shutdown_mcp_servers",
        lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    assert script._current_cost() == result


def test_current_cost_rejects_existing_mcp_servers_before_discovery(monkeypatch):
    from scripts import measure_tool_schema_cost as script

    calls = []
    monkeypatch.setattr(script, "_servers", {"live": object()}, raising=False)
    monkeypatch.setattr(script, "discover_mcp_tools", lambda: calls.append("discover"))
    monkeypatch.setattr(script, "shutdown_mcp_servers", lambda: calls.append("shutdown"))

    with pytest.raises(RuntimeError, match="separate process"):
        script._current_cost()

    assert calls == []


def test_main_json_emits_only_measurement_json(monkeypatch, capsys):
    from scripts import measure_tool_schema_cost as script
    from tools import tool_search

    result = {
        "raw_tools": 1,
        "raw_tokens": 2,
        "visible_tools": 3,
        "visible_tokens": 4,
        "deferred_tools": 5,
        "saved_tokens": 6,
    }
    monkeypatch.setattr(script, "suppress_interactive_oauth", nullcontext)
    monkeypatch.setattr(script, "discover_mcp_tools", lambda: print("discover diagnostic"))
    monkeypatch.setattr(
        script,
        "get_tool_definitions",
        lambda **kwargs: print("get diagnostic") or [],
    )
    monkeypatch.setattr(
        script,
        "load_config",
        lambda: tool_search.ToolSearchConfig.from_raw({}),
    )
    monkeypatch.setattr(script, "_resolve_active_context_length", lambda: 0)
    monkeypatch.setattr(
        script,
        "measure_tool_schema_cost",
        lambda *args, **kwargs: print("measure diagnostic") or result,
    )
    monkeypatch.setattr(script, "shutdown_mcp_servers", lambda: print("shutdown diagnostic"))

    assert script.main(["--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload == result
    assert set(payload) == {
        "raw_tools",
        "raw_tokens",
        "visible_tools",
        "visible_tokens",
        "deferred_tools",
        "saved_tokens",
    }


def test_current_cost_forwards_runtime_context_length(monkeypatch):
    from scripts import measure_tool_schema_cost as script
    from tools import tool_search

    received = {}
    monkeypatch.setattr(script, "discover_mcp_tools", lambda: None)
    monkeypatch.setattr(script, "get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr(
        script,
        "load_config",
        lambda: tool_search.ToolSearchConfig.from_raw({}),
    )
    monkeypatch.setattr(
        script,
        "_resolve_active_context_length",
        lambda: 123_456,
    )

    def capture_measurement(tool_defs, **kwargs):
        received.update(kwargs)
        return {}

    monkeypatch.setattr(script, "measure_tool_schema_cost", capture_measurement)

    script._current_cost()

    assert received["context_length"] == 123_456
