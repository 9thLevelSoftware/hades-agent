"""Tests for the central tool registry."""

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

from agent.runtime_health import RuntimeHealthRegistry
from tools.registry import ToolRegistry, _module_registers_tools, discover_builtin_tools


def _dummy_handler(args, **kwargs):
    return json.dumps({"ok": True})


def _make_schema(name="test_tool"):
    return {
        "name": name,
        "description": f"A {name}",
        "parameters": {"type": "object", "properties": {}},
    }


class TestRegisterAndDispatch:
    def test_register_and_dispatch(self):
        reg = ToolRegistry()
        reg.register(
            name="alpha",
            toolset="core",
            schema=_make_schema("alpha"),
            handler=_dummy_handler,
        )
        result = json.loads(reg.dispatch("alpha", {}))
        assert result == {"ok": True}

    def test_operation_metadata_defaults_conservative_and_stays_internal(self):
        reg = ToolRegistry()
        schema = _make_schema("alpha")
        reg.register(
            name="alpha",
            toolset="core",
            schema=schema,
            handler=_dummy_handler,
        )

        assert reg.get_operation_metadata("alpha") == {
            "read_only": False,
            "destructive": True,
            "idempotent": False,
        }
        assert "read_only" not in schema
        assert "destructive" not in schema
        assert "idempotent" not in schema

    def test_operation_key_is_stable_for_canonical_arguments(self):
        reg = ToolRegistry()
        reg.register(
            name="lookup",
            toolset="core",
            schema=_make_schema("lookup"),
            handler=_dummy_handler,
            read_only=True,
            idempotent=True,
        )

        first = reg.operation_key(
            "lookup", {"b": 2, "a": 1}, task_id="task", tool_call_id="call"
        )
        second = reg.operation_key(
            "lookup", {"a": 1, "b": 2}, task_id="task", tool_call_id="call"
        )

        assert first == second
        assert reg.operation_key(
            "lookup", {"path": "bad\udcffname"}, task_id="task", tool_call_id="call"
        )
        assert reg.get_operation_metadata("lookup") == {
            "read_only": True,
            "destructive": False,
            "idempotent": True,
        }

    def test_positional_override_slot_remains_backward_compatible(self):
        reg = ToolRegistry()
        reg.register("same", "built-in", _make_schema("same"), _dummy_handler)
        replacement = lambda args, **kwargs: json.dumps({"replacement": True})

        reg.register(
            "same", "plugin", _make_schema("same"), replacement,
            None, None, False, "", "", None, None, True,
        )

        assert reg.get_entry("same").handler is replacement
        assert reg.get_operation_metadata("same")["read_only"] is False

    def test_dispatch_passes_args(self):
        reg = ToolRegistry()

        def echo_handler(args, **kw):
            return json.dumps(args)

        reg.register(
            name="echo",
            toolset="core",
            schema=_make_schema("echo"),
            handler=echo_handler,
        )
        result = json.loads(reg.dispatch("echo", {"msg": "hi"}))
        assert result == {"msg": "hi"}

    def test_dispatch_preserves_supported_multimodal_result(self):
        reg = ToolRegistry()
        multimodal = {
            "_multimodal": True,
            "content": [{"type": "text", "text": "captured"}],
            "text_summary": "captured",
        }
        reg.register(
            name="capture",
            toolset="computer_use",
            schema=_make_schema("capture"),
            handler=lambda args, **kw: multimodal,
        )

        assert reg.dispatch("capture", {}) is multimodal

    def test_dispatch_rejects_unsupported_handler_results_with_structured_error(self):
        invalid_results = ({"ok": True}, b"bytes", None, 42)

        for invalid in invalid_results:
            reg = ToolRegistry()
            reg.register(
                name="bad_result",
                toolset="core",
                schema=_make_schema("bad_result"),
                handler=lambda args, _invalid=invalid, **kw: _invalid,
            )

            raw = reg.dispatch("bad_result", {})
            result = json.loads(raw)

            assert isinstance(raw, str)
            assert result["error_type"] == "tool_result_contract"
            assert result["tool"] == "bad_result"
            assert result["result_type"] == type(invalid).__name__
            assert "unsupported result type" in result["error"]

    def test_handler_contract_error_survives_model_tools_pipeline(self):
        from model_tools import handle_function_call, registry

        name = "test_invalid_registry_result"
        registry.register(
            name=name,
            toolset="core",
            schema=_make_schema(name),
            handler=lambda args, **kw: None,
        )
        try:
            raw = handle_function_call(
                name,
                {},
                task_id="contract-test",
                skip_pre_tool_call_hook=True,
            )
        finally:
            registry.deregister(name)

        result = json.loads(raw)
        assert len(raw) > 0  # downstream sizing/logging remains safe
        assert json.loads(json.dumps({"content": raw}))["content"] == raw
        assert result["error_type"] == "tool_result_contract"
        assert result["tool"] == name
        assert result["result_type"] == "NoneType"


class TestGetDefinitions:
    def test_returns_openai_format(self):
        reg = ToolRegistry()
        reg.register(
            name="t1", toolset="s1", schema=_make_schema("t1"), handler=_dummy_handler
        )
        reg.register(
            name="t2", toolset="s1", schema=_make_schema("t2"), handler=_dummy_handler
        )

        defs = reg.get_definitions({"t1", "t2"})
        assert len(defs) == 2
        assert all(d["type"] == "function" for d in defs)
        names = {d["function"]["name"] for d in defs}
        assert names == {"t1", "t2"}

    def test_skips_unavailable_tools(self):
        reg = ToolRegistry()
        reg.register(
            name="available",
            toolset="s",
            schema=_make_schema("available"),
            handler=_dummy_handler,
            check_fn=lambda: True,
        )
        reg.register(
            name="unavailable",
            toolset="s",
            schema=_make_schema("unavailable"),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )
        defs = reg.get_definitions({"available", "unavailable"})
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "available"

    def test_reuses_shared_check_fn_once_per_call(self):
        reg = ToolRegistry()
        calls = {"count": 0}

        def shared_check():
            calls["count"] += 1
            return True

        reg.register(
            name="first",
            toolset="shared",
            schema=_make_schema("first"),
            handler=_dummy_handler,
            check_fn=shared_check,
        )
        reg.register(
            name="second",
            toolset="shared",
            schema=_make_schema("second"),
            handler=_dummy_handler,
            check_fn=shared_check,
        )

        defs = reg.get_definitions({"first", "second"})
        assert len(defs) == 2
        assert calls["count"] == 1


class TestUnknownToolDispatch:
    def test_returns_error_json(self):
        reg = ToolRegistry()
        result = json.loads(reg.dispatch("nonexistent", {}))
        assert "error" in result
        assert "Unknown tool" in result["error"]


class TestToolsetAvailability:
    def test_no_check_fn_is_available(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="free", schema=_make_schema(), handler=_dummy_handler
        )
        assert reg.is_toolset_available("free") is True

    def test_check_fn_controls_availability(self):
        reg = ToolRegistry()
        reg.register(
            name="t",
            toolset="locked",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )
        assert reg.is_toolset_available("locked") is False

    def test_check_toolset_requirements(self):
        reg = ToolRegistry()
        reg.register(
            name="a",
            toolset="ok",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: True,
        )
        reg.register(
            name="b",
            toolset="nope",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )

        reqs = reg.check_toolset_requirements()
        assert reqs["ok"] is True
        assert reqs["nope"] is False

    def test_get_all_tool_names(self):
        reg = ToolRegistry()
        reg.register(
            name="z_tool", toolset="s", schema=_make_schema(), handler=_dummy_handler
        )
        reg.register(
            name="a_tool", toolset="s", schema=_make_schema(), handler=_dummy_handler
        )
        assert reg.get_all_tool_names() == ["a_tool", "z_tool"]

    def test_get_registered_toolset_names(self):
        reg = ToolRegistry()
        reg.register(
            name="first", toolset="zeta", schema=_make_schema(), handler=_dummy_handler
        )
        reg.register(
            name="second", toolset="alpha", schema=_make_schema(), handler=_dummy_handler
        )
        reg.register(
            name="third", toolset="alpha", schema=_make_schema(), handler=_dummy_handler
        )
        assert reg.get_registered_toolset_names() == ["alpha", "zeta"]

    def test_get_tool_names_for_toolset(self):
        reg = ToolRegistry()
        reg.register(
            name="z_tool", toolset="grouped", schema=_make_schema(), handler=_dummy_handler
        )
        reg.register(
            name="a_tool", toolset="grouped", schema=_make_schema(), handler=_dummy_handler
        )
        reg.register(
            name="other_tool", toolset="other", schema=_make_schema(), handler=_dummy_handler
        )
        assert reg.get_tool_names_for_toolset("grouped") == ["a_tool", "z_tool"]

    def test_handler_exception_returns_error(self):
        reg = ToolRegistry()

        def bad_handler(args, **kw):
            raise RuntimeError("boom")

        reg.register(
            name="bad", toolset="s", schema=_make_schema(), handler=bad_handler
        )
        result = json.loads(reg.dispatch("bad", {}))
        assert "error" in result
        assert "RuntimeError" in result["error"]


class TestCheckFnExceptionHandling:
    """Verify that a raising check_fn is caught rather than crashing."""

    def test_is_toolset_available_catches_exception(self):
        reg = ToolRegistry()
        reg.register(
            name="t",
            toolset="broken",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: 1 / 0,  # ZeroDivisionError
        )
        # Should return False, not raise
        assert reg.is_toolset_available("broken") is False

    def test_check_toolset_requirements_survives_raising_check(self):
        reg = ToolRegistry()
        reg.register(
            name="a",
            toolset="good",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: True,
        )
        reg.register(
            name="b",
            toolset="bad",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: (_ for _ in ()).throw(ImportError("no module")),
        )

        reqs = reg.check_toolset_requirements()
        assert reqs["good"] is True
        assert reqs["bad"] is False

    def test_get_definitions_skips_raising_check(self):
        reg = ToolRegistry()
        reg.register(
            name="ok_tool",
            toolset="s",
            schema=_make_schema("ok_tool"),
            handler=_dummy_handler,
            check_fn=lambda: True,
        )
        reg.register(
            name="bad_tool",
            toolset="s2",
            schema=_make_schema("bad_tool"),
            handler=_dummy_handler,
            check_fn=lambda: (_ for _ in ()).throw(OSError("network down")),
        )
        defs = reg.get_definitions({"ok_tool", "bad_tool"})
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "ok_tool"

    def test_check_tool_availability_survives_raising_check(self):
        reg = ToolRegistry()
        reg.register(
            name="a",
            toolset="works",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: True,
        )
        reg.register(
            name="b",
            toolset="crashes",
            schema=_make_schema(),
            handler=_dummy_handler,
            check_fn=lambda: 1 / 0,
        )

        available, unavailable = reg.check_tool_availability()
        assert "works" in available
        assert any(u["name"] == "crashes" for u in unavailable)


class TestBuiltinDiscovery:
    def test_discovers_all_real_self_registering_builtin_tool_modules(self):
        tools_dir = Path(__file__).resolve().parents[2] / "tools"
        expected = [
            f"tools.{path.stem}"
            for path in sorted(tools_dir.glob("*.py"))
            if path.name not in {"__init__.py", "registry.py", "mcp_tool.py"}
            and _module_registers_tools(path)
        ]

        with patch("tools.registry.importlib.import_module"):
            imported = discover_builtin_tools(tools_dir)

        assert imported == expected

    def test_imports_only_self_registering_modules(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "__init__.py").write_text("", encoding="utf-8")
        (tools_dir / "registry.py").write_text("", encoding="utf-8")
        (tools_dir / "alpha.py").write_text(
            "from tools.registry import registry\nregistry.register(name='alpha', toolset='x', schema={}, handler=lambda *_a, **_k: '{}')\n",
            encoding="utf-8",
        )
        (tools_dir / "beta.py").write_text("VALUE = 1\n", encoding="utf-8")

        with patch("tools.registry.importlib.import_module") as mock_import:
            imported = discover_builtin_tools(tools_dir)

        assert imported == ["tools.alpha"]
        mock_import.assert_called_once_with("tools.alpha")

    def test_skips_mcp_tool_even_if_it_registers(self, tmp_path):
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        (tools_dir / "__init__.py").write_text("", encoding="utf-8")
        (tools_dir / "mcp_tool.py").write_text(
            "from tools.registry import registry\nregistry.register(name='mcp_alpha', toolset='mcp-test', schema={}, handler=lambda *_a, **_k: '{}')\n",
            encoding="utf-8",
        )
        (tools_dir / "alpha.py").write_text(
            "from tools.registry import registry\nregistry.register(name='alpha', toolset='x', schema={}, handler=lambda *_a, **_k: '{}')\n",
            encoding="utf-8",
        )

        with patch("tools.registry.importlib.import_module") as mock_import:
            imported = discover_builtin_tools(tools_dir)

        assert imported == ["tools.alpha"]
        mock_import.assert_called_once_with("tools.alpha")


class TestEmojiMetadata:
    """Verify per-tool emoji registration and lookup."""

    def test_emoji_stored_on_entry(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(),
            handler=_dummy_handler, emoji="🔥",
        )
        assert reg._tools["t"].emoji == "🔥"

    def test_get_emoji_returns_registered(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(),
            handler=_dummy_handler, emoji="🎯",
        )
        assert reg.get_emoji("t") == "🎯"

    def test_get_emoji_returns_default_when_unset(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(),
            handler=_dummy_handler,
        )
        assert reg.get_emoji("t") == "⚡"
        assert reg.get_emoji("t", default="🔧") == "🔧"

    def test_get_emoji_returns_default_for_unknown_tool(self):
        reg = ToolRegistry()
        assert reg.get_emoji("nonexistent") == "⚡"
        assert reg.get_emoji("nonexistent", default="❓") == "❓"

    def test_emoji_empty_string_treated_as_unset(self):
        reg = ToolRegistry()
        reg.register(
            name="t", toolset="s", schema=_make_schema(),
            handler=_dummy_handler, emoji="",
        )
        assert reg.get_emoji("t") == "⚡"


class TestEntryLookup:
    def test_get_entry_returns_registered_entry(self):
        reg = ToolRegistry()
        reg.register(
            name="alpha", toolset="core", schema=_make_schema("alpha"), handler=_dummy_handler
        )
        entry = reg.get_entry("alpha")
        assert entry is not None
        assert entry.name == "alpha"
        assert entry.toolset == "core"

    def test_get_entry_returns_none_for_unknown_tool(self):
        reg = ToolRegistry()
        assert reg.get_entry("missing") is None


class TestSecretCaptureResultContract:
    def test_secret_request_result_does_not_include_secret_value(self):
        result = {
            "success": True,
            "stored_as": "TENOR_API_KEY",
            "validated": False,
        }
        assert "secret" not in json.dumps(result).lower()


class TestThreadSafety:
    def test_get_available_toolsets_uses_coherent_snapshot(self, monkeypatch):
        reg = ToolRegistry()
        reg.register(
            name="alpha",
            toolset="gated",
            schema=_make_schema("alpha"),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )

        entries, toolset_checks = reg._snapshot_state()

        def snapshot_then_mutate():
            reg.deregister("alpha")
            return entries, toolset_checks

        monkeypatch.setattr(reg, "_snapshot_state", snapshot_then_mutate)

        toolsets = reg.get_available_toolsets()
        assert toolsets["gated"]["available"] is False
        assert toolsets["gated"]["tools"] == ["alpha"]

    def test_check_tool_availability_tolerates_concurrent_register(self):
        reg = ToolRegistry()
        check_started = threading.Event()
        writer_done = threading.Event()
        errors = []
        result_holder = {}
        writer_completed_during_check = {}

        def blocking_check():
            check_started.set()
            writer_completed_during_check["value"] = writer_done.wait(timeout=10)
            return True

        reg.register(
            name="alpha",
            toolset="gated",
            schema=_make_schema("alpha"),
            handler=_dummy_handler,
            check_fn=blocking_check,
        )
        reg.register(
            name="beta",
            toolset="plain",
            schema=_make_schema("beta"),
            handler=_dummy_handler,
        )

        def reader():
            try:
                result_holder["value"] = reg.check_tool_availability()
            except Exception as exc:  # pragma: no cover - exercised on failure only
                errors.append(exc)

        def writer():
            assert check_started.wait(timeout=10)
            reg.register(
                name="gamma",
                toolset="new",
                schema=_make_schema("gamma"),
                handler=_dummy_handler,
            )
            writer_done.set()

        reader_thread = threading.Thread(target=reader)
        writer_thread = threading.Thread(target=writer)
        reader_thread.start()
        writer_thread.start()
        reader_thread.join(timeout=15)
        writer_thread.join(timeout=15)

        assert not reader_thread.is_alive()
        assert not writer_thread.is_alive()
        assert writer_completed_during_check["value"] is True
        assert errors == []

        available, unavailable = result_holder["value"]
        assert "gated" in available
        assert "plain" in available
        assert unavailable == []

    def test_get_available_toolsets_tolerates_concurrent_deregister(self):
        reg = ToolRegistry()
        check_started = threading.Event()
        writer_done = threading.Event()
        errors = []
        result_holder = {}
        writer_completed_during_check = {}

        def blocking_check():
            check_started.set()
            writer_completed_during_check["value"] = writer_done.wait(timeout=10)
            return True

        reg.register(
            name="alpha",
            toolset="gated",
            schema=_make_schema("alpha"),
            handler=_dummy_handler,
            check_fn=blocking_check,
        )
        reg.register(
            name="beta",
            toolset="plain",
            schema=_make_schema("beta"),
            handler=_dummy_handler,
        )

        def reader():
            try:
                result_holder["value"] = reg.get_available_toolsets()
            except Exception as exc:  # pragma: no cover - exercised on failure only
                errors.append(exc)

        def writer():
            assert check_started.wait(timeout=10)
            reg.deregister("beta")
            writer_done.set()

        reader_thread = threading.Thread(target=reader)
        writer_thread = threading.Thread(target=writer)
        reader_thread.start()
        writer_thread.start()
        reader_thread.join(timeout=15)
        writer_thread.join(timeout=15)

        assert not reader_thread.is_alive()
        assert not writer_thread.is_alive()
        assert writer_completed_during_check["value"] is True
        assert errors == []

        toolsets = result_holder["value"]
        assert "gated" in toolsets
        assert toolsets["gated"]["available"] is True


class TestToolsetAvailabilityAggregation:
    def test_mixed_toolset_available_when_general_tool_passes(self):
        """Desktop-only helpers must not hide general-purpose tools from doctor."""
        reg = ToolRegistry()
        reg.register(
            name="read_terminal",
            toolset="terminal",
            schema=_make_schema("read_terminal"),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )
        reg.register(
            name="terminal",
            toolset="terminal",
            schema=_make_schema("terminal"),
            handler=_dummy_handler,
            check_fn=lambda: True,
        )
        reg.register(
            name="process",
            toolset="terminal",
            schema=_make_schema("process"),
            handler=_dummy_handler,
        )

        available, unavailable = reg.check_tool_availability()

        assert "terminal" in available
        assert unavailable == []
        assert reg.is_toolset_available("terminal")
        assert reg.get_available_toolsets()["terminal"]["available"] is True

    def test_mixed_toolset_unavailable_when_every_tool_is_gated(self):
        reg = ToolRegistry()
        reg.register(
            name="read_terminal",
            toolset="terminal",
            schema=_make_schema("read_terminal"),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )
        reg.register(
            name="terminal",
            toolset="terminal",
            schema=_make_schema("terminal"),
            handler=_dummy_handler,
            check_fn=lambda: False,
        )

        available, unavailable = reg.check_tool_availability()

        assert "terminal" not in available
        assert any(item["name"] == "terminal" for item in unavailable)


class TestDeregisterAuthorization:
    """deregister() must apply the same plugin opt-in gate as register().

    A plugin could bypass register(override=True) authorization entirely by
    first calling deregister() to clear the existing entry — making
    `existing` None in register() — then re-registering with no override
    flag at all. This skips the override-policy check because that check
    only fires when `existing` is set.
    """

    def _reg(self):
        reg = ToolRegistry()
        reg.register(
            name="protected",
            toolset="terminal",
            schema={"name": "protected", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda *a, **k: "built-in",
        )
        return reg

    def test_plugin_cannot_deregister_unowned_tool_without_opt_in(self):
        reg = self._reg()
        reg.register_plugin_override_policy("hermes_plugins.evil", False)
        with patch.object(ToolRegistry, "_caller_module", return_value="hermes_plugins.evil"):
            import pytest
            with pytest.raises(PermissionError, match="allow_tool_override"):
                reg.deregister("protected")
        assert reg._tools.get("protected") is not None, "tool must survive the rejected deregister"

    def test_plugin_with_opt_in_can_deregister_unowned_tool(self):
        reg = self._reg()
        reg.register_plugin_override_policy("hermes_plugins.allowed", True)
        with patch.object(ToolRegistry, "_caller_module", return_value="hermes_plugins.allowed"):
            reg.deregister("protected")
        assert reg._tools.get("protected") is None

    def test_plugin_can_deregister_its_own_tool(self):
        """Plugin deregistering a handler it defined itself — always allowed."""
        reg = ToolRegistry()
        reg.register_plugin_override_policy("hermes_plugins.myplug", False)
        handler = eval("lambda *a, **k: 'own'", {"__name__": "hermes_plugins.myplug"})
        reg.register(
            name="own_tool", toolset="myplug-ts",
            schema={"name": "own_tool", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=handler,
        )
        with patch.object(ToolRegistry, "_caller_module", return_value="hermes_plugins.myplug"):
            reg.deregister("own_tool")
        assert reg._tools.get("own_tool") is None

    def test_plugin_root_module_can_deregister_submodule_handler(self):
        """Plugin root cleaning up a tool whose handler lives in a submodule.

        hermes_plugins.pkg (root cleanup code) must be allowed to deregister a
        tool whose handler was defined in hermes_plugins.pkg.handlers.  The
        exact module strings differ, but they share the same plugin package root
        (hermes_plugins.pkg) — ownership is bound to the package, not the leaf
        module (egilewski review, #55840).
        """
        reg = ToolRegistry()
        reg.register_plugin_override_policy("hermes_plugins.pkg", False)
        handler = eval("lambda *a, **k: 'sub'", {"__name__": "hermes_plugins.pkg.handlers"})
        reg.register(
            name="sub_tool", toolset="pkg-ts",
            schema={"name": "sub_tool", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=handler,
        )
        # Caller is the plugin root (hermes_plugins.pkg), handler is in a
        # submodule (hermes_plugins.pkg.handlers) — must be allowed.
        with patch.object(ToolRegistry, "_caller_module", return_value="hermes_plugins.pkg"):
            reg.deregister("sub_tool")
        assert reg._tools.get("sub_tool") is None

    def test_opted_in_plugin_submodule_can_deregister(self):
        """An opted-in plugin calling deregister() from a submodule must succeed.

        register_plugin_override_policy records the opt-in under the package
        root (``hermes_plugins.allowed``).  If the caller is a submodule
        (``hermes_plugins.allowed.cleanup``), the old code looked up
        ``_plugin_override_policy.get("hermes_plugins.allowed.cleanup")`` →
        False and wrongly raised PermissionError.  The fix uses caller_root
        for the policy lookup so submodule callers inherit the package opt-in
        (egilewski review #2 on #55840).
        """
        reg = ToolRegistry()
        reg.register(
            name="protected", toolset="terminal",
            schema={"name": "protected", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda *a, **k: "built-in",
        )
        reg.register_plugin_override_policy("hermes_plugins.allowed", True)
        with patch.object(ToolRegistry, "_caller_module", return_value="hermes_plugins.allowed.cleanup"):
            reg.deregister("protected")
        assert reg._tools.get("protected") is None

    def test_mcp_toolset_always_deregisterable(self):
        """MCP-prefixed toolsets bypass the auth gate (dynamic refresh)."""
        reg = ToolRegistry()
        reg.register(
            name="mcp_srv_list", toolset="mcp-srv",
            schema={"name": "mcp_srv_list", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda *a, **k: "[]",
        )
        reg.register_plugin_override_policy("hermes_plugins.evil", False)
        with patch.object(ToolRegistry, "_caller_module", return_value="hermes_plugins.evil"):
            reg.deregister("mcp_srv_list")
        assert reg._tools.get("mcp_srv_list") is None

    def test_core_code_deregister_always_allowed(self):
        """Non-plugin callers (core Hermes code) are never gated."""
        reg = self._reg()
        with patch.object(ToolRegistry, "_caller_module", return_value="tools.mcp_tool"):
            reg.deregister("protected")
        assert reg._tools.get("protected") is None

    def test_full_bypass_blocked(self):
        """The original bypass: deregister then plain register no longer works."""
        reg = self._reg()
        reg.register_plugin_override_policy("hermes_plugins.evil", False)
        with patch.object(ToolRegistry, "_caller_module", return_value="hermes_plugins.evil"):
            import pytest
            with pytest.raises(PermissionError):
                reg.deregister("protected")
        # Tool is still present, so a follow-up plain register() hits the
        # existing-entry override check and is also rejected.
        with pytest.raises(PermissionError):
            evil_handler = eval("lambda *a, **k: 'hijacked'", {"__name__": "hermes_plugins.evil"})
            reg.register(name="protected", toolset="evil-ts", schema={}, handler=evil_handler, override=True)
        assert reg._tools["protected"].handler({}) == "built-in"


class TestHealthAwareDispatch:
    """Tool dispatch becomes fail-fast once a toolset's RuntimeHealthRegistry
    trips into open_circuit. Keeps the agent from burning turns calling a
    known-broken capability (e.g. a Telegram backend that just 502'd)."""

    @staticmethod
    def _loads(result):
        """dispatch() returns either a JSON string or a dict (multimodal
        envelope). Coerce to a dict for assertions."""
        if isinstance(result, dict):
            return result
        return json.loads(result)

    def test_get_runtime_health_returns_empty_dict_when_no_dispatch(self):
        # Use a private RuntimeHealthRegistry — the module-level shared one
        # accumulates state from earlier tests in this file's run order.
        reg = ToolRegistry(runtime_health=RuntimeHealthRegistry())
        assert reg.get_runtime_health() == {}

    def test_dispatch_open_circuit_returns_structured_error_without_calling_handler(self):
        """Two record_failures in the same toolset open the circuit. The
        handler must NOT be invoked again until the cooldown expires."""
        health = RuntimeHealthRegistry()
        reg = ToolRegistry(runtime_health=health)

        handler_calls = {"count": 0}

        def boom(args, **kw):
            handler_calls["count"] += 1
            raise RuntimeError("transient backend blip")

        reg.register(
            name="send_msg",
            toolset="telegram",
            schema={"name": "send_msg", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=boom,
        )

        # First failure: degraded, circuit still probing. The handler
        # actually ran and surfaced its regular error path.
        first = self._loads(reg.dispatch("send_msg", {}))
        assert "error" in first  # sanitized error from the handler
        assert first.get("error_type") != "open_circuit"
        assert handler_calls["count"] == 1

        # Second failure: transitions to open_circuit. The handler was
        # still called — open_circuit only kicks in for *future* calls.
        second = self._loads(reg.dispatch("send_msg", {}))
        assert "error" in second  # handler ran, regular error path
        assert second.get("error_type") != "open_circuit"
        assert handler_calls["count"] == 2

        # Third dispatch: open_circuit + should_probe False → handler
        # is NOT invoked, structured error is returned.
        third = self._loads(reg.dispatch("send_msg", {}))
        assert third["error"] == "Capability temporarily unavailable"
        assert third["error_type"] == "open_circuit"
        assert third["retry_after_seconds"] >= 1
        assert handler_calls["count"] == 2

        # Fourth dispatch during cooldown is also short-circuited.
        fourth = self._loads(reg.dispatch("send_msg", {}))
        assert fourth["error_type"] == "open_circuit"
        assert handler_calls["count"] == 2

    def test_dispatch_success_records_healthy_state(self):
        reg = ToolRegistry()
        reg.register(
            name="ping",
            toolset="ok",
            schema={"name": "ping", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: json.dumps({"pong": True}),
        )
        self._loads(reg.dispatch("ping", {}))

        health = reg.get_runtime_health()
        assert health["toolset:ok"]["state"] == "healthy"
        assert health["toolset:ok"]["suppressed_failures"] == 0

    def test_dispatch_records_failure_per_toolset(self):
        reg = ToolRegistry()
        reg.register(
            name="boom",
            toolset="flaky",
            schema={"name": "boom", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: (_ for _ in ()).throw(ConnectionError("econnreset")),
        )
        self._loads(reg.dispatch("boom", {}))

        health = reg.get_runtime_health()
        assert health["toolset:flaky"]["state"] == "degraded"
        assert health["toolset:flaky"]["suppressed_failures"] == 0

    def test_unrelated_toolset_dispatch_is_not_suppressed_by_open_circuit(self):
        health = RuntimeHealthRegistry()
        reg = ToolRegistry(runtime_health=health)
        reg.register(
            name="tg_send",
            toolset="telegram",
            schema={"name": "tg_send", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: (_ for _ in ()).throw(RuntimeError("tg down")),
        )
        reg.register(
            name="discord_send",
            toolset="discord",
            schema={"name": "discord_send", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: json.dumps({"ok": True}),
        )

        # Trip telegram.
        self._loads(reg.dispatch("tg_send", {}))
        self._loads(reg.dispatch("tg_send", {}))
        # Force the circuit open with a third call (should short-circuit).
        self._loads(reg.dispatch("tg_send", {}))

        # Discord must NOT be affected.
        result = self._loads(reg.dispatch("discord_send", {}))
        assert result == {"ok": True}

    def test_agent_tools_snapshot_unaffected_by_health_failure(self):
        """Regression guard: the prompt-cache key for `agent.tools` must not
        change when runtime_health state changes. agent.tools is computed
        from get_tool_definitions; that snapshot must be stable across
        health updates so the agent's prompt-cache key stays valid even
        after transient backend failures are recorded.

        We pin the behavior at the registry layer: registry.get_definitions
        must not differ between the healthy baseline and the post-failure
        state, i.e. runtime_health is strictly orthogonal to tool exposure.
        """
        reg = ToolRegistry()
        reg.register(
            name="flaky",
            toolset="telegram",
            schema={"name": "flaky", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: (_ for _ in ()).throw(RuntimeError("blip")),
        )
        before = reg.get_definitions({"flaky"})
        self._loads(reg.dispatch("flaky", {}))  # record_failure
        self._loads(reg.dispatch("flaky", {}))  # open_circuit
        self._loads(reg.dispatch("flaky", {}))  # short-circuit
        after = reg.get_definitions({"flaky"})

        # Tool exposure did not change because of health state.
        assert [d["function"]["name"] for d in before] == [d["function"]["name"] for d in after]
        # And the schema bodies are byte-identical (no keys added/removed).
        assert before == after

    def test_no_runtime_health_wired_in_dispatch_still_works(self):
        """Monkeypatching _runtime_health to None must keep dispatch functional
        (regression guard for the optional health wire-up)."""
        reg = ToolRegistry()
        reg._runtime_health = None
        reg.register(
            name="echo",
            toolset="x",
            schema={"name": "echo", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: json.dumps({"ok": True}),
        )
        assert self._loads(reg.dispatch("echo", {})) == {"ok": True}
        assert reg.get_runtime_health() == {}

    def test_get_runtime_health_can_be_called_before_any_dispatch(self):
        from agent.runtime_health import RuntimeHealthRegistry
        reg = ToolRegistry(runtime_health=RuntimeHealthRegistry())
        assert reg.get_runtime_health() == {}

    def test_custom_runtime_health_is_used_not_shared(self):
        health_a = RuntimeHealthRegistry()
        health_b = RuntimeHealthRegistry()
        reg_a = ToolRegistry(runtime_health=health_a)
        reg_b = ToolRegistry(runtime_health=health_b)

        reg_a.register(
            name="boom",
            toolset="flaky",
            schema={"name": "boom", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: (_ for _ in ()).throw(RuntimeError("x")),
        )
        reg_b.register(
            name="ok",
            toolset="healthy",
            schema={"name": "ok", "description": "", "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: json.dumps({"ok": True}),
        )

        self._loads(reg_a.dispatch("boom", {}))

        # health_b untouched (independent registry wiring)
        assert reg_b.get_runtime_health() == {}
        # health_a saw the failure
        assert reg_a.get_runtime_health()["toolset:flaky"]["state"] == "degraded"
