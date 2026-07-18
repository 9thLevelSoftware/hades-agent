#!/usr/bin/env python3
"""

Tests for the code execution sandbox (programmatic tool calling).

These tests monkeypatch handle_function_call so they don't require API keys
or a running terminal backend. They verify the core sandbox mechanics:
UDS socket lifecycle, hermes_tools generation, timeout enforcement,
output capping, tool call counting, and error propagation.

Run with:  python -m pytest tests/test_code_execution.py -v
   or:     python tests/test_code_execution.py
"""

import pytest
# pytestmark removed — tests run fine (61 pass, ~99s)

import json
import os
import base64
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

os.environ["TERMINAL_ENV"] = "local"


@pytest.fixture(autouse=True)
def _force_local_terminal(monkeypatch):
    """Re-set TERMINAL_ENV=local before every test.

    The module-level assignment above covers import time, but under xdist
    another worker can overwrite os.environ between tests.  monkeypatch
    ensures each test starts (and ends) with the correct value.
    """
    monkeypatch.setenv("TERMINAL_ENV", "local")
import sys
import threading
import unittest
from unittest.mock import patch, MagicMock

import yaml

from tools import code_execution_tool
from tools.code_execution_tool import (
    SANDBOX_ALLOWED_TOOLS,
    execute_code,
    generate_hermes_tools_module,
    check_sandbox_requirements,
    build_execute_code_schema,
    EXECUTE_CODE_SCHEMA,
    _TOOL_DOC_LINES,
    _execute_remote,
    CodeExecutionContext,
    ArtifactBudget,
    _context_from_rpc_request,
    _dispatch_script_call,
    is_structured_image_artifact,
    normalize_image_artifact,
)


def _mock_handle_function_call(
    function_name, function_args, task_id=None, user_task=None, **_kwargs,
):
    """Mock dispatcher that returns canned responses for each tool."""
    if function_name == "terminal":
        cmd = function_args.get("command", "")
        return json.dumps({"output": f"mock output for: {cmd}", "exit_code": 0})
    if function_name == "web_search":
        return json.dumps({"results": [{"url": "https://example.com", "title": "Example", "description": "A test result"}]})
    if function_name == "read_file":
        return json.dumps({"content": "line 1\nline 2\nline 3\n", "total_lines": 3})
    if function_name == "write_file":
        return json.dumps({"status": "ok", "path": function_args.get("path", "")})
    if function_name == "search_files":
        return json.dumps({"matches": [{"file": "test.py", "line": 1, "text": "match"}]})
    if function_name == "patch":
        return json.dumps({"status": "ok", "replacements": 1})
    if function_name == "web_extract":
        return json.dumps("# Extracted content\nSome text from the page.")
    return json.dumps({"error": f"Unknown tool in mock: {function_name}"})


def _fixture_definition(name):
    return {
        "name": name,
        "description": f"Code-mode fixture {name}.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    }


class TestRpcContextTrustBoundary:
    def test_rpc_context_cannot_override_parent_scope(self):
        fallback = CodeExecutionContext(
            "parent-task", "parent-session", ("safe",), ("blocked",)
        )
        request = {
            "task_id": "child-task",
            "session_id": "child-session",
            "enabled_toolsets": ["safe", "admin"],
            "disabled_toolsets": [],
        }

        assert _context_from_rpc_request(request, fallback) == fallback

    def test_rpc_context_cannot_clear_parent_scope_with_nulls_or_empty_lists(self):
        fallback = CodeExecutionContext(
            "parent-task", "parent-session", ("safe",), ("blocked",)
        )
        request = {
            "task_id": None,
            "session_id": None,
            "enabled_toolsets": [],
            "disabled_toolsets": [],
        }

        assert _context_from_rpc_request(request, fallback) == fallback


def _dispatch_script(code, context, definitions):
    """Run generated code against the production local RPC server."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    tmpdir = tempfile.mkdtemp(prefix="hermes_code_mode_test_")
    artifact_dir = os.path.join(tmpdir, "artifacts")
    os.makedirs(artifact_dir, mode=0o700, exist_ok=True)
    context = replace(
        context,
        artifact_roots=(tmpdir, artifact_dir),
        artifact_budget=ArtifactBudget(),
    )
    sock_path = os.path.join(
        tempfile.gettempdir(), f"hermes_code_mode_{uuid.uuid4().hex}.sock",
    )
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.bind(sock_path)
    server_sock.listen(1)
    token = uuid.uuid4().hex
    stop_event = threading.Event()
    call_log = []
    call_counter = [0]
    tool_names = {
        str((item.get("function") or item).get("name")) for item in definitions
    }
    rpc_thread = threading.Thread(
        target=code_execution_tool._rpc_server_loop,
        args=(
            server_sock, context.task_id, call_log, call_counter, 20,
            frozenset(tool_names), stop_event, token, context,
        ),
        daemon=True,
    )
    try:
        with open(os.path.join(tmpdir, "hermes_tools.py"), "w", encoding="utf-8") as handle:
            handle.write(generate_hermes_tools_module(definitions, context=context))
        with open(os.path.join(tmpdir, "script.py"), "w", encoding="utf-8") as handle:
            handle.write(code)
        rpc_thread.start()
        env = dict(os.environ)
        env.update({
            "HERMES_RPC_SOCKET": sock_path,
            "HERMES_RPC_TOKEN": token,
            "HERMES_ARTIFACTS_DIR": artifact_dir,
            "PYTHONPATH": os.pathsep.join((tmpdir, repo_root)),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        completed = subprocess.run(
            [sys.executable, os.path.join(tmpdir, "script.py")],
            cwd=tmpdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
        assert output_lines, completed.stderr
        return json.loads(output_lines[-1])
    finally:
        stop_event.set()
        try:
            server_sock.close()
        except OSError:
            pass
        rpc_thread.join(timeout=5)
        try:
            os.unlink(sock_path)
        except OSError:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="local UDS RPC is POSIX-only")
class TestCodeModeDispatchIntegration:
    """Exercise the production RPC and dispatcher seams with real registry tools."""

    def test_scripted_calls_keep_middleware_context_metadata_and_approval(self, monkeypatch):
        import model_tools
        from tools.registry import registry

        read_name = "code_mode_read_fixture"
        write_name = "code_mode_write_fixture"
        definitions = [_fixture_definition(read_name), _fixture_definition(write_name)]
        seen = {"dispatch": [], "request": [], "execution": [], "hooks": [], "handlers": []}

        def read_handler(args, **kwargs):
            seen["handlers"].append((read_name, dict(args), dict(kwargs)))
            return json.dumps({"tool": read_name, "args": args})

        def write_handler(args, **kwargs):
            seen["handlers"].append((write_name, dict(args), dict(kwargs)))
            return json.dumps({"tool": write_name, "args": args})

        registry.register(read_name, "mcp-code-mode-fixture", definitions[0], read_handler,
                          read_only=True, destructive=False, idempotent=True)
        registry.register(write_name, "mcp-code-mode-fixture", definitions[1], write_handler,
                          read_only=False, destructive=True, idempotent=False)
        try:
            def request_middleware(kind, **kwargs):
                assert kind == "tool_request"
                seen["request"].append(dict(kwargs))
                return [{"args": {**kwargs["args"], "request_rewritten": True}}]

            def execution_middleware(**kwargs):
                seen["execution"].append(dict(kwargs))
                if kwargs["operation_metadata"]["destructive"]:
                    return json.dumps({
                        "status": "approval_required",
                        "blocked": True,
                        "tool_name": kwargs["tool_name"],
                    })
                return kwargs["next_call"]({**kwargs["args"], "execution_rewritten": True})

            manager = SimpleNamespace(_middleware={"tool_execution": [execution_middleware]})
            monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
            monkeypatch.setattr("hermes_cli.plugins.invoke_middleware", request_middleware)
            monkeypatch.setattr(
                "hermes_cli.plugins.has_middleware",
                lambda kind: kind in {"tool_request", "tool_execution"},
            )
            monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda _name: True)
            monkeypatch.setattr(
                "hermes_cli.plugins.invoke_hook",
                lambda hook_name, **kwargs: seen["hooks"].append((hook_name, kwargs)) or [],
            )

            real_handle = model_tools.handle_function_call

            def recording_dispatch(function_name, function_args, **kwargs):
                seen["dispatch"].append((function_name, dict(function_args), dict(kwargs)))
                return real_handle(function_name, function_args, **kwargs)

            monkeypatch.setattr(model_tools, "handle_function_call", recording_dispatch)
            context = CodeExecutionContext(
                "code-task", "code-session", ("mcp-code-mode-fixture",), ("blocked",)
            )
            result = _dispatch_script(
                """
import json
from hermes_tools import code_mode_read_fixture, code_mode_write_fixture
print(json.dumps([
    code_mode_read_fixture(value='read'),
    code_mode_write_fixture(value='write'),
]))
""",
                context,
                definitions,
            )

            assert result[0]["tool"] == read_name
            assert result[0]["args"] == {
                "value": "read",
                "request_rewritten": True,
                "execution_rewritten": True,
            }
            assert result[1]["status"] == "approval_required"
            assert [item[0] for item in seen["dispatch"]] == [read_name, write_name]
            assert all(item[2]["task_id"] == context.task_id for item in seen["dispatch"])
            assert all(item[2]["session_id"] == context.session_id for item in seen["dispatch"])
            assert all(item[2]["enabled_toolsets"] == list(context.enabled_toolsets)
                       for item in seen["dispatch"])
            assert all(item[2]["disabled_toolsets"] == list(context.disabled_toolsets)
                       for item in seen["dispatch"])
            assert seen["dispatch"][0][2]["operation_metadata"] == {
                "read_only": True, "destructive": False, "idempotent": True,
            }
            assert seen["dispatch"][1][2]["operation_metadata"] == {
                "read_only": False, "destructive": True, "idempotent": False,
            }
            assert len(seen["request"]) == 2
            assert len(seen["execution"]) == 2
            assert seen["handlers"] == [
                (read_name, result[0]["args"], {
                    "task_id": context.task_id,
                    "session_id": context.session_id,
                    "user_task": None,
                }),
            ]
            assert {name for name, _kwargs in seen["hooks"]} >= {
                "pre_tool_call", "post_tool_call", "transform_tool_result",
            }
            assert seen["execution"][0]["operation_key"] == registry.operation_key(
                read_name,
                {"value": "read", "request_rewritten": True},
                task_id=context.task_id,
                tool_call_id="",
            )
        finally:
            registry.deregister(read_name)
            registry.deregister(write_name)

    def test_destructive_scripted_call_requires_approval_without_execution_middleware(self, monkeypatch):
        from tools.registry import registry

        tool_name = "code_mode_destructive_without_middleware"
        called = []

        def handler(args, **kwargs):
            called.append((dict(args), dict(kwargs)))
            return json.dumps({"ok": True})

        registry.register(
            tool_name,
            "mcp-code-mode-approval-fixture",
            _fixture_definition(tool_name),
            handler,
            read_only=False,
            destructive=True,
            idempotent=False,
        )
        try:
            monkeypatch.setattr(
                "hermes_cli.plugins.get_plugin_manager",
                lambda: SimpleNamespace(_middleware={}),
            )
            context = CodeExecutionContext(
                "approval-task", "approval-session", (), (),
            )
            result = _dispatch_script_call(tool_name, {"value": "secret"}, context)

            assert result == {
                "status": "error",
                "error": (
                    f"Tool '{tool_name}' requires interactive approval, but the "
                    "tool_execution approval middleware is not configured."
                ),
                "approval_required": True,
                "tool_name": tool_name,
                "requester": "approval-session",
                "task_id": "approval-task",
                "session_id": "approval-session",
                "operation_metadata": {
                    "read_only": False,
                    "destructive": True,
                    "idempotent": False,
                },
            }
            assert called == []
        finally:
            registry.deregister(tool_name)

    def test_catalog_bridge_scopes_tool_call_before_handler(self, monkeypatch):
        import model_tools
        from tools.registry import registry

        safe_name = "code_mode_catalog_safe"
        unsafe_name = "code_mode_catalog_unsafe"
        definitions = [_fixture_definition(safe_name), _fixture_definition(unsafe_name)]
        called = []
        middleware_seen = {"request": [], "execution": []}

        def handler(args, **kwargs):
            called.append((dict(args), dict(kwargs)))
            return json.dumps({"ok": True, "args": args})

        registry.register(safe_name, "mcp-code-mode-catalog-safe", definitions[0], handler,
                          read_only=True, destructive=False, idempotent=True)
        registry.register(unsafe_name, "mcp-code-mode-catalog-unsafe", definitions[1], handler,
                          read_only=True, destructive=False, idempotent=True)
        try:
            context = CodeExecutionContext(
                "catalog-task", "catalog-session",
                ("mcp-code-mode-catalog-safe", "mcp-code-mode-catalog-unsafe"),
                ("mcp-code-mode-catalog-unsafe",),
            )
            for raw_name in ("tool_search", "tool_describe", "tool_call"):
                denied = _dispatch_script_call(raw_name, {}, context)
                assert "not available" in str(denied.get("error"))
            unknown = _dispatch_script_call("code_mode_catalog_unknown", {}, context)
            assert "Unknown scripted tool" in str(unknown.get("error"))
            out_of_scope = _dispatch_script_call(unsafe_name, {"value": "raw"}, context)
            assert "not available in this session" in str(out_of_scope.get("error"))

            real_handle = model_tools.handle_function_call

            def request_middleware(kind, **kwargs):
                if kind == "tool_request":
                    middleware_seen["request"].append(dict(kwargs))
                return []

            def execution_middleware(**kwargs):
                middleware_seen["execution"].append(dict(kwargs))
                return kwargs["next_call"](kwargs["args"])

            monkeypatch.setattr(
                "hermes_cli.plugins.get_plugin_manager",
                lambda: SimpleNamespace(_middleware={"tool_execution": [execution_middleware]}),
            )
            monkeypatch.setattr("hermes_cli.plugins.invoke_middleware", request_middleware)
            monkeypatch.setattr("hermes_cli.plugins.has_middleware", lambda kind: kind == "tool_request")
            monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda _name: False)
            dispatch_seen = []

            def recording_dispatch(function_name, function_args, **kwargs):
                dispatch_seen.append((function_name, dict(function_args), dict(kwargs)))
                return real_handle(function_name, function_args, **kwargs)

            monkeypatch.setattr(model_tools, "handle_function_call", recording_dispatch)
            result = _dispatch_script(
                """
import json
from hermes_tools import call_tool
print(json.dumps([
    call_tool('code_mode_catalog_safe', {'value': 'safe'}),
    call_tool('code_mode_catalog_unsafe', {'value': 'unsafe'}),
]))
""",
                context,
                definitions,
            )
            assert result[0]["ok"] is True
            assert "error" in result[1]
            assert "not available in this session" in result[1]["error"]
            assert len(called) == 1
            assert called[0][0] == {"value": "safe"}
            assert [item[0] for item in dispatch_seen] == ["tool_call", safe_name, "tool_call"]
            assert [item["tool_name"] for item in middleware_seen["request"]] == [safe_name]
            assert [item["tool_name"] for item in middleware_seen["execution"]] == [safe_name]
            assert middleware_seen["execution"][0]["operation_metadata"] == {
                "read_only": True, "destructive": False, "idempotent": True,
            }
        finally:
            registry.deregister(safe_name)
            registry.deregister(unsafe_name)

    def test_catalog_bridge_requires_approval_for_destructive_tool_without_middleware(
        self, monkeypatch,
    ):
        from tools.registry import registry

        tool_name = "code_mode_catalog_destructive"
        called = []

        def handler(args, **kwargs):
            called.append((dict(args), dict(kwargs)))
            return json.dumps({"ok": True})

        registry.register(
            tool_name,
            "mcp-code-mode-catalog-destructive",
            _fixture_definition(tool_name),
            handler,
            read_only=False,
            destructive=True,
            idempotent=False,
        )
        try:
            monkeypatch.setattr(
                "hermes_cli.plugins.get_plugin_manager",
                lambda: SimpleNamespace(_middleware={}),
            )
            context = CodeExecutionContext(
                "catalog-approval-task",
                "catalog-approval-session",
                (),
                (),
                allowed_tools=(tool_name,),
            )
            result = _dispatch_script_call(
                "__code_mode_catalog__",
                {
                    "action": "tool_call",
                    "arguments": {"name": tool_name, "arguments": {"value": "secret"}},
                },
                context,
            )

            assert result == {
                "status": "error",
                "error": (
                    f"Tool '{tool_name}' requires interactive approval, but the "
                    "tool_execution approval middleware is not configured."
                ),
                "approval_required": True,
                "tool_name": tool_name,
                "requester": "catalog-approval-session",
                "task_id": "catalog-approval-task",
                "session_id": "catalog-approval-session",
                "operation_metadata": {
                    "read_only": False,
                    "destructive": True,
                    "idempotent": False,
                },
            }
            assert called == []
        finally:
            registry.deregister(tool_name)

    def test_scoped_tool_names_accepts_flat_definitions(self):
        from tools.tool_search import scoped_tool_names

        assert scoped_tool_names([_fixture_definition("flat_fixture")]) == {
            "flat_fixture",
        }


class TestSandboxRequirements(unittest.TestCase):
    def test_available_on_posix(self):
        if sys.platform != "win32":
            self.assertTrue(check_sandbox_requirements())

    def test_schema_is_valid(self):
        self.assertEqual(EXECUTE_CODE_SCHEMA["name"], "execute_code")
        self.assertIn("code", EXECUTE_CODE_SCHEMA["parameters"]["properties"])
        self.assertNotIn("code", EXECUTE_CODE_SCHEMA["parameters"]["required"])


class TestHermesToolsGeneration(unittest.TestCase):
    def test_generates_all_allowed_tools(self):
        src = generate_hermes_tools_module(list(SANDBOX_ALLOWED_TOOLS))
        for tool in SANDBOX_ALLOWED_TOOLS:
            self.assertIn(f"def {tool}(", src)

    def test_generates_subset(self):
        src = generate_hermes_tools_module(["terminal", "web_search"])
        self.assertIn("def terminal(", src)
        self.assertIn("def web_search(", src)
        self.assertNotIn("def read_file(", src)

    def test_empty_list_generates_nothing(self):
        src = generate_hermes_tools_module([])
        self.assertNotIn("def terminal(", src)
        self.assertIn("def _call(", src)  # infrastructure still present

    def test_non_allowed_tools_ignored(self):
        src = generate_hermes_tools_module(["vision_analyze", "terminal"])
        self.assertIn("def terminal(", src)
        self.assertNotIn("def vision_analyze(", src)

    def test_rpc_infrastructure_present(self):
        src = generate_hermes_tools_module(["terminal"])
        self.assertIn("HERMES_RPC_SOCKET", src)
        self.assertIn("AF_UNIX", src)
        self.assertIn("def _connect(", src)
        self.assertIn("def _call(", src)

    def test_convenience_helpers_present(self):
        """Verify json_parse, shell_quote, and retry helpers are generated."""
        src = generate_hermes_tools_module(["terminal"])
        self.assertIn("def json_parse(", src)
        self.assertIn("def shell_quote(", src)
        self.assertIn("def retry(", src)
        self.assertIn("import json, os, socket, shlex, threading, time", src)

    def test_registry_definitions_generate_typed_required_and_optional_params(self):
        definitions = {
            "read_file": {
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer", "default": 1},
                    },
                    "required": ["path"],
                },
            },
        }
        src = generate_hermes_tools_module(
            definitions,
            context=CodeExecutionContext("task", "session", (), ()),
        )
        self.assertIn("def read_file(path: str, offset: int = 1)", src)
        compile(src, "hermes_tools.py", "exec")

    def test_arbitrary_unicode_names_compile_and_preserve_registered_names(self):
        registered_name = "tool_²"
        parameter_name = "value²"
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": registered_name,
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "typed_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {parameter_name: {"type": "string"}},
                        "required": [parameter_name],
                    },
                },
            },
        ]
        src = generate_hermes_tools_module(definitions)
        compile(src, "hermes_tools.py", "exec")
        self.assertIn(f"_call({registered_name!r}", src)
        self.assertIn(f"{parameter_name!r}", src)

    def test_registry_wrapper_names_cannot_overwrite_catalog_helpers(self):
        registered_name = "call_tool"
        definition = {
            "type": "function",
            "function": {
                "name": registered_name,
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            },
        }
        src = generate_hermes_tools_module([definition])
        namespace = {}
        exec(compile(src, "hermes_tools.py", "exec"), namespace)
        calls = []
        namespace["_call"] = lambda name, args: calls.append((name, args)) or {"ok": True}

        self.assertIn("call_tool_2", namespace)
        self.assertEqual(namespace["call_tool"]("catalog", {"x": 1}), {"ok": True})
        self.assertEqual(namespace["call_tool_2"]("registered"), {"ok": True})
        self.assertEqual(calls, [
            ("__code_mode_catalog__", {
                "action": "tool_call",
                "arguments": {"name": "catalog", "arguments": {"x": 1}},
            }),
            (registered_name, {"value": "registered"}),
        ])

    def test_explicit_free_form_additional_properties_use_kwargs(self):
        for name, additional_properties in (
            ("free_form_true", True),
            ("free_form_schema", {"type": "string"}),
        ):
            definition = {
                "type": "function",
                "function": {
                    "name": name,
                    "parameters": {
                        "type": "object",
                        "properties": {"known": {"type": "string"}},
                        "required": ["known"],
                        "additionalProperties": additional_properties,
                    },
                },
            }
            src = generate_hermes_tools_module([definition])
            self.assertIn(f"def {name}(**kwargs)", src)
            self.assertIn("Sanitized schema:", src)
            compile(src, "hermes_tools.py", "exec")

    def test_exotic_schema_uses_kwargs_and_sanitized_schema_docstring(self):
        definitions = [{
            "type": "function",
            "function": {
                "name": "complex-tool",
                "description": "A complex tool.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "anyOf": [
                                {"type": "string"},
                                {"$ref": "#/$defs/Value"},
                            ],
                        },
                    },
                },
            },
        }]
        src = generate_hermes_tools_module(definitions)
        self.assertIn("def complex_tool(**kwargs)", src)
        self.assertIn("anyOf", src)
        self.assertIn("$ref", src)
        compile(src, "hermes_tools.py", "exec")

    def test_registry_definitions_generate_catalog_helpers_and_bounded_source(self):
        definitions = {
            f"tool_{index}": {
                "description": "Small tool description.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            }
            for index in range(80)
        }
        src = generate_hermes_tools_module(
            definitions,
            context=CodeExecutionContext("task", "session", ("tools",), ()),
        )
        for helper in ("search_tools", "describe_tool", "call_tool", "save_artifact"):
            self.assertIn(f"def {helper}(", src)
        self.assertIn("__code_mode_catalog__", src)
        self.assertIn("'tool_search'", src)
        self.assertIn("'tool_describe'", src)
        self.assertIn("'tool_call'", src)
        self.assertLess(len(src), 100_000)
        compile(src, "hermes_tools.py", "exec")

    def test_catalog_internal_action_preserves_context_and_raw_bridge_denylist(self):
        context = CodeExecutionContext("task", "session", ("mcp-tools",), ("blocked",))
        with patch("model_tools.handle_function_call", return_value=json.dumps({"ok": True})) as handler:
            result = _dispatch_script_call(
                "__code_mode_catalog__",
                {"action": "tool_search", "arguments": {"query": "files"}},
                context,
            )
        self.assertEqual(result, {"ok": True})
        handler.assert_called_once_with(
            "tool_search",
            {"query": "files"},
            task_id="task",
            session_id="session",
            enabled_toolsets=["mcp-tools"],
            disabled_toolsets=["blocked"],
        )
        denied = _dispatch_script_call("tool_search", {"query": "files"}, context)
        self.assertIn("not available", denied["error"])

    def test_file_transport_uses_tempfile_fallback_for_rpc_dir(self):
        src = generate_hermes_tools_module(["terminal"], transport="file")
        self.assertIn("import json, os, shlex, tempfile, threading, time", src)
        self.assertIn("os.path.join(tempfile.gettempdir(), \"hermes_rpc\")", src)
        self.assertNotIn('os.environ.get("HERMES_RPC_DIR", "/tmp/hermes_rpc")', src)

    def test_uds_transport_serializes_concurrent_calls(self):
        """Regression: UDS _call() must hold a lock across send+recv so that
        concurrent tool calls from multiple threads don't interleave on the
        shared socket and receive each other's responses."""
        src = generate_hermes_tools_module(["terminal"], transport="uds")
        self.assertIn("_call_lock = threading.Lock()", src)
        self.assertIn("with _call_lock:", src)

    def test_file_transport_serializes_seq_allocation(self):
        """Regression: file transport _call() must allocate `_seq` under a
        lock, otherwise concurrent threads can pick the same seq and clobber
        each other's request files."""
        src = generate_hermes_tools_module(["terminal"], transport="file")
        self.assertIn("_seq_lock = threading.Lock()", src)
        self.assertIn("with _seq_lock:", src)

    def test_dispatch_rejects_legacy_handler_that_cannot_receive_operation_metadata(self):

        seen = {}

        def legacy_handler(function_name, function_args, task_id=None, user_task=None):
            seen.update(function_name=function_name, function_args=function_args,
                        task_id=task_id)
            return json.dumps({"ok": True})

        context = CodeExecutionContext("task", "session", ("terminal",), ())
        with patch(
            "hermes_cli.plugins.get_plugin_manager",
            return_value=SimpleNamespace(_middleware={"tool_execution": [object()]}),
        ), patch("model_tools.get_tool_definitions",
                   return_value=[{"function": {"name": "terminal"}}]), \
             patch("model_tools.handle_function_call", side_effect=legacy_handler):
            result = _dispatch_script_call(
                "terminal", {"command": "echo hi"}, context,
            )

        self.assertIn("unexpected keyword argument", result["error"])
        self.assertEqual(seen, {})


class TestExecuteCodeRemoteTempDir(unittest.TestCase):
    def test_execute_remote_uses_backend_temp_dir_for_sandbox(self):
        class FakeEnv:
            def __init__(self):
                self.commands = []

            def get_temp_dir(self):
                return "/data/data/com.termux/files/usr/tmp"

            def execute(self, command, cwd=None, timeout=None):
                self.commands.append((command, cwd, timeout))
                if "command -v python3" in command:
                    return {"output": "OK\n"}
                if "python3 script.py" in command:
                    return {"output": "hello\n", "returncode": 0}
                return {"output": ""}

        env = FakeEnv()
        fake_thread = MagicMock()

        with patch("tools.code_execution_tool._load_config", return_value={"timeout": 30, "max_tool_calls": 5}), \
             patch("tools.code_execution_tool._get_or_create_env", return_value=(env, "ssh")), \
             patch("tools.code_execution_tool._ship_file_to_remote"), \
             patch("tools.code_execution_tool.threading.Thread", return_value=fake_thread):
            result = json.loads(_execute_remote("print('hello')", "task-1", ["terminal"]))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["stdout_truncated"])
        self.assertEqual(result["stdout_bytes_total"], len("hello\n".encode("utf-8")))
        mkdir_cmd = env.commands[1][0]
        run_cmd = next(cmd for cmd, _, _ in env.commands if "python3 script.py" in cmd)
        cleanup_cmd = env.commands[-1][0]
        self.assertIn("mkdir -p /data/data/com.termux/files/usr/tmp/hermes_exec_", mkdir_cmd)
        self.assertIn("HERMES_RPC_DIR=/data/data/com.termux/files/usr/tmp/hermes_exec_", run_cmd)
        self.assertIn("rm -rf /data/data/com.termux/files/usr/tmp/hermes_exec_", cleanup_cmd)
        self.assertNotIn("mkdir -p /tmp/hermes_exec_", mkdir_cmd)

    def test_remote_artifacts_are_collected_before_cleanup(self):
        class FakeEnv:
            def __init__(self):
                self.commands = []
                self.artifact_path = "/tmp/hermes_exec_test/artifacts/chart.png"
                self.artifact_bytes = b"PNG-REMOTE"

            def get_temp_dir(self):
                return "/tmp"

            def execute(self, command, cwd=None, timeout=None):
                self.commands.append((command, cwd, timeout))
                if "command -v python3" in command:
                    return {"output": "OK\n"}
                if "python3 script.py" in command:
                    return {"output": "hello\n", "returncode": 0}
                if command.startswith("find "):
                    root = command.split("find ", 1)[1].split(" -maxdepth", 1)[0].strip("'")
                    self.artifact_path = root + "/chart.png"
                    return {"output": f"{self.artifact_path}\n"}
                if command.startswith("wc -c"):
                    return {"output": f"{len(self.artifact_bytes)}\n"}
                if command.startswith("head -c"):
                    return {"output": base64.b64encode(self.artifact_bytes).decode() + "\n"}
                return {"output": ""}

        env = FakeEnv()
        fake_thread = MagicMock()
        with patch("tools.code_execution_tool._load_config", return_value={"timeout": 30, "max_tool_calls": 5}), \
             patch("tools.code_execution_tool._get_or_create_env", return_value=(env, "ssh")), \
             patch("tools.code_execution_tool._ship_file_to_remote"), \
             patch("tools.code_execution_tool.threading.Thread", return_value=fake_thread):
            result = json.loads(_execute_remote("print('hello')", "task-remote-image", ["terminal"]))

        assert result["status"] == "success"
        assert result["_multimodal"] is True
        image_url = result["content"][0]["image_url"]["url"]
        assert image_url.startswith("file://")
        assert Path(image_url[7:]).is_file()
        find_index = next(i for i, (cmd, _, _) in enumerate(env.commands) if cmd.startswith("find "))
        cleanup_index = next(i for i, (cmd, _, _) in enumerate(env.commands) if cmd.startswith("rm -rf "))
        assert find_index < cleanup_index

    def test_timezone_shell_quoted_in_remote_execution(self):
        """HERMES_TIMEZONE must be shell-quoted in remote env_prefix to prevent injection."""
        class FakeEnv:
            def __init__(self):
                self.commands = []

            def get_temp_dir(self):
                return "/tmp"

            def execute(self, command, cwd=None, timeout=None):
                self.commands.append((command, cwd, timeout))
                if "command -v python3" in command:
                    return {"output": "OK\n"}
                if "python3 script.py" in command:
                    return {"output": "hello\n", "returncode": 0}
                return {"output": ""}

        env = FakeEnv()
        fake_thread = MagicMock()

        malicious_tz = "US/Eastern; echo PWNED"

        with patch("tools.code_execution_tool._load_config",
                   return_value={"timeout": 30, "max_tool_calls": 5}), \
             patch("tools.code_execution_tool._get_or_create_env",
                   return_value=(env, "ssh")), \
             patch("tools.code_execution_tool._ship_file_to_remote"), \
             patch("tools.code_execution_tool.threading.Thread",
                   return_value=fake_thread), \
             patch.dict(os.environ, {"HERMES_TIMEZONE": malicious_tz}):
            result = json.loads(_execute_remote("print('hello')", "task-1", ["terminal"]))

        self.assertEqual(result["status"], "success")
        run_cmd = next(cmd for cmd, _, _ in env.commands if "python3 script.py" in cmd)
        # The TZ value must be shell-quoted — it should NOT contain unescaped semicolons
        self.assertNotIn("TZ=US/Eastern; echo PWNED", run_cmd,
                         "TZ value with shell metacharacters must not appear unquoted")
        # shlex.quote wraps values containing special characters in single quotes
        self.assertIn("TZ='US/Eastern; echo PWNED'", run_cmd,
                      "TZ value must be wrapped in single quotes by shlex.quote()")


@unittest.skipIf(sys.platform == "win32", "UDS not available on Windows")
class TestExecuteCode(unittest.TestCase):
    """Integration tests using the mock dispatcher."""

    def _run(self, code, enabled_tools=None):
        """Helper: run code with mocked handle_function_call."""
        with patch("tools.code_execution_tool._rpc_server_loop") as mock_rpc:
            # Use real execution but mock the tool dispatcher
            pass
        # Actually run with full integration, mocking at the model_tools level.
        execution_manager = SimpleNamespace(
            _middleware={"tool_execution": [
                lambda **kwargs: kwargs["next_call"](kwargs["args"]),
            ]},
        )
        with (
            patch("hermes_cli.plugins.get_plugin_manager", return_value=execution_manager),
            patch("model_tools.handle_function_call", side_effect=_mock_handle_function_call),
        ):
            result = execute_code(
                code=code,
                task_id="test-task",
                enabled_tools=enabled_tools or list(SANDBOX_ALLOWED_TOOLS),
            )
        return json.loads(result)

    def test_basic_print(self):
        """Script that just prints -- no tool calls."""
        result = self._run('print("hello world")')
        self.assertEqual(result["status"], "success")
        self.assertIn("hello world", result["output"])
        self.assertEqual(result["tool_calls_made"], 0)

    def test_no_tool_call_script_does_not_wait_for_rpc_accept_timeout(self):
        """A no-tool script should not wait seconds for the idle RPC accept thread."""
        start = time.monotonic()
        result = self._run('print("fast")')
        elapsed = time.monotonic() - start

        self.assertEqual(result["status"], "success")
        self.assertIn("fast", result["output"])
        self.assertLess(elapsed, 2.0, f"execute_code took {elapsed:.3f}s")

    def test_repo_root_modules_are_importable(self):
        """Sandboxed scripts can import modules that live at the repo root."""
        result = self._run('import hermes_constants; print(hermes_constants.__file__)')
        self.assertEqual(result["status"], "success")
        self.assertIn("hermes_constants.py", result["output"])

    def test_single_tool_call(self):
        """Script calls terminal and prints the result."""
        code = """
from hermes_tools import terminal
result = terminal("echo hello")
print(result.get("output", ""))
"""
        result = self._run(code)
        self.assertEqual(result["status"], "success")
        self.assertIn("mock output for: echo hello", result["output"])
        self.assertEqual(result["tool_calls_made"], 1)

    def test_multi_tool_chain(self):
        """Script calls multiple tools sequentially."""
        code = """
from hermes_tools import terminal, read_file
r1 = terminal("ls")
r2 = read_file("test.py")
print(f"terminal: {r1['output'][:20]}")
print(f"file lines: {r2['total_lines']}")
"""
        result = self._run(code)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["tool_calls_made"], 2)

    def test_syntax_error(self):
        """Script with a syntax error returns error status."""
        result = self._run("def broken(")
        self.assertEqual(result["status"], "error")
        self.assertIn("SyntaxError", result.get("error", "") + result.get("output", ""))

    def test_runtime_exception(self):
        """Script with a runtime error returns error status."""
        result = self._run("raise ValueError('test error')")
        self.assertEqual(result["status"], "error")

    def test_concurrent_tool_calls_match_responses(self):
        """Regression for the UDS RPC race: multiple threads inside the
        sandbox calling terminal() concurrently must each receive their own
        response, not another thread's.

        Before the fix, `_sock` and the recv-loop were shared without a
        lock, so responses (written FIFO by the single-threaded server)
        got delivered to whichever client thread happened to win the
        recv() race. That surfaced as each thread seeing another thread's
        output.

        The mock dispatcher sleeps briefly to guarantee the requests
        overlap on the socket.
        """
        code = '''
import threading
from concurrent.futures import ThreadPoolExecutor
from hermes_tools import terminal

N = 10

def call(i):
    r = terminal(f"echo TAG-{i}")
    return i, r.get("output", "")

with ThreadPoolExecutor(max_workers=N) as ex:
    results = list(ex.map(call, range(N)))

mismatches = [(i, out) for i, out in results if f"TAG-{i}" not in out]
if mismatches:
    print(f"MISMATCH {len(mismatches)}/{N}: {mismatches[:3]}")
else:
    print(f"OK {N}/{N}")
'''

        def slow_mock(
            function_name, function_args, task_id=None, user_task=None, **_kwargs,
        ):
            import time as _t
            if function_name == "terminal":
                _t.sleep(0.05)  # ensure requests overlap on the socket
                cmd = function_args.get("command", "")
                # Echo semantics: strip leading "echo " and return the rest
                out = cmd[5:] if cmd.startswith("echo ") else f"mock: {cmd}"
                return json.dumps({"output": out, "exit_code": 0})
            return _mock_handle_function_call(
                function_name, function_args, task_id=task_id, user_task=user_task
            )

        execution_manager = SimpleNamespace(
            _middleware={"tool_execution": [
                lambda **kwargs: kwargs["next_call"](kwargs["args"]),
            ]},
        )
        with (
            patch("hermes_cli.plugins.get_plugin_manager", return_value=execution_manager),
            patch("model_tools.handle_function_call", side_effect=slow_mock),
        ):
            raw = execute_code(
                code=code,
                task_id="test-concurrent",
                enabled_tools=list(SANDBOX_ALLOWED_TOOLS),
            )
        result = json.loads(raw)
        self.assertEqual(result["status"], "success", msg=result)
        self.assertIn("OK 10/10", result["output"],
                      msg=f"Concurrent tool calls mismatched: {result['output']!r}")

    def test_excluded_tool_returns_error(self):
        """Script calling a tool not in the allow-list gets an error from RPC."""
        code = """
from hermes_tools import terminal
result = terminal("echo hi")
print(result)
"""
        # Only enable web_search -- terminal should be excluded
        result = self._run(code, enabled_tools=["web_search"])
        # terminal won't be in hermes_tools.py, so import fails
        self.assertEqual(result["status"], "error")

    def test_empty_code(self):
        """Empty code string returns an error."""
        result = json.loads(execute_code("", task_id="test"))
        self.assertIn("error", result)

    def test_output_captured(self):
        """Multiple print statements are captured in order."""
        code = """
for i in range(5):
    print(f"line {i}")
"""
        result = self._run(code)
        self.assertEqual(result["status"], "success")
        for i in range(5):
            self.assertIn(f"line {i}", result["output"])

    def test_stderr_on_error(self):
        """Traceback from stderr is included in the response."""
        code = """
import sys
print("before error")
raise RuntimeError("deliberate crash")
"""
        result = self._run(code)
        self.assertEqual(result["status"], "error")
        self.assertIn("before error", result["output"])
        self.assertIn("RuntimeError", result.get("error", "") + result.get("output", ""))

    def test_timeout_enforcement(self):
        """Script that sleeps too long is killed."""
        code = "import time; time.sleep(999)"
        with patch("model_tools.handle_function_call", side_effect=_mock_handle_function_call):
            # Override config to use a very short timeout
            with patch("tools.code_execution_tool._load_config", return_value={"timeout": 2, "max_tool_calls": 50}):
                result = json.loads(execute_code(
                    code=code,
                    task_id="test-task",
                    enabled_tools=list(SANDBOX_ALLOWED_TOOLS),
                ))
        self.assertEqual(result["status"], "timeout")
        self.assertIn("timed out", result.get("error", ""))
        # The timeout message must also appear in output so the LLM always
        # surfaces it to the user (#10807).
        self.assertIn("timed out", result.get("output", ""))
        self.assertIn("\u23f0", result.get("output", ""))

    def test_web_search_tool(self):
        """Script calls web_search and processes results."""
        code = """
from hermes_tools import web_search
results = web_search("test query")
print(f"Found {len(results.get('results', []))} results")
"""
        result = self._run(code)
        self.assertEqual(result["status"], "success")
        self.assertIn("Found 1 results", result["output"])

    def test_json_parse_helper(self):
        """json_parse handles control characters that json.loads(strict=True) rejects."""
        code = r"""
from hermes_tools import json_parse
# This JSON has a literal tab character which strict mode rejects
text = '{"body": "line1\tline2\nline3"}'
result = json_parse(text)
print(result["body"])
"""
        result = self._run(code)
        self.assertEqual(result["status"], "success")
        self.assertIn("line1", result["output"])

    def test_shell_quote_helper(self):
        """shell_quote properly escapes dangerous characters."""
        code = """
from hermes_tools import shell_quote
# String with backticks, quotes, and special chars
dangerous = '`rm -rf /` && $(whoami) "hello"'
escaped = shell_quote(dangerous)
print(escaped)
# Verify it's wrapped in single quotes with proper escaping
assert "rm -rf" in escaped
assert escaped.startswith("'")
"""
        result = self._run(code)
        self.assertEqual(result["status"], "success")

    def test_retry_helper_success(self):
        """retry returns on first success."""
        code = """
from hermes_tools import retry
counter = [0]
def flaky():
    counter[0] += 1
    return f"ok on attempt {counter[0]}"
result = retry(flaky)
print(result)
"""
        result = self._run(code)
        self.assertEqual(result["status"], "success")
        self.assertIn("ok on attempt 1", result["output"])

    def test_retry_helper_eventual_success(self):
        """retry retries on failure and succeeds eventually."""
        code = """
from hermes_tools import retry
counter = [0]
def flaky():
    counter[0] += 1
    if counter[0] < 3:
        raise ConnectionError(f"fail {counter[0]}")
    return "success"
result = retry(flaky, max_attempts=3, delay=0.01)
print(result)
"""
        result = self._run(code)
        self.assertEqual(result["status"], "success")
        self.assertIn("success", result["output"])

    def test_retry_helper_all_fail(self):
        """retry raises the last error when all attempts fail."""
        code = """
from hermes_tools import retry
def always_fail():
    raise ValueError("nope")
try:
    retry(always_fail, max_attempts=2, delay=0.01)
    print("should not reach here")
except ValueError as e:
    print(f"caught: {e}")
"""
        result = self._run(code)
        self.assertEqual(result["status"], "success")
        self.assertIn("caught: nope", result["output"])


class TestStubSchemaDrift(unittest.TestCase):
    """Verify that _TOOL_STUBS in code_execution_tool.py stay in sync with
    the real tool schemas registered in tools/registry.py.

    If a tool gains a new parameter but the sandbox stub isn't updated,
    the LLM will try to use the parameter (it sees it in the system prompt)
    and get a TypeError.  This test catches that drift.
    """

    # Parameters that are internal (injected by the handler, not user-facing)
    _INTERNAL_PARAMS = {"task_id", "user_task"}
    # Parameters intentionally blocked in the sandbox
    _BLOCKED_TERMINAL_PARAMS = {"background", "pty", "notify_on_complete", "watch_patterns"}

    def test_stubs_cover_all_schema_params(self):
        """Every user-facing parameter in the real schema must appear in the
        corresponding _TOOL_STUBS entry."""
        import re
        from tools.code_execution_tool import _TOOL_STUBS

        # Import the registry and trigger tool registration
        from tools.registry import registry
        import tools.file_tools  # noqa: F401 - registers read_file, write_file, patch, search_files
        import tools.web_tools  # noqa: F401 - registers web_search, web_extract

        for tool_name, (func_name, sig, doc, args_expr) in _TOOL_STUBS.items():
            entry = registry._tools.get(tool_name)
            if not entry:
                # Tool might not be registered yet (e.g., terminal uses a
                # different registration path).  Skip gracefully.
                continue

            schema_props = entry.schema.get("parameters", {}).get("properties", {})
            schema_params = set(schema_props.keys()) - self._INTERNAL_PARAMS
            if tool_name == "terminal":
                schema_params -= self._BLOCKED_TERMINAL_PARAMS

            # Extract parameter names from the stub signature string
            # Match word before colon: "pattern: str, target: str = ..."
            stub_params = set(re.findall(r'(\w+)\s*:', sig))

            missing = schema_params - stub_params
            self.assertEqual(
                missing, set(),
                f"Stub for '{tool_name}' is missing parameters that exist in "
                f"the real schema: {missing}. Update _TOOL_STUBS in "
                f"code_execution_tool.py to include them."
            )

    def test_stubs_pass_all_params_to_rpc(self):
        """The args_dict_expr in each stub must include every parameter from
        the signature, so that all params are actually sent over RPC."""
        import re
        from tools.code_execution_tool import _TOOL_STUBS

        for tool_name, (func_name, sig, doc, args_expr) in _TOOL_STUBS.items():
            stub_params = set(re.findall(r'(\w+)\s*:', sig))
            # Check that each param name appears in the args dict expression
            for param in stub_params:
                self.assertIn(
                    f'"{param}"',
                    args_expr,
                    f"Stub for '{tool_name}' has parameter '{param}' in its "
                    f"signature but doesn't pass it in the args dict: {args_expr}"
                )

    def test_search_files_target_uses_current_values(self):
        """search_files stub should use 'content'/'files', not old 'grep'/'find'."""
        from tools.code_execution_tool import _TOOL_STUBS
        _, sig, doc, _ = _TOOL_STUBS["search_files"]
        self.assertIn('"content"', sig,
                      "search_files stub should default target to 'content', not 'grep'")
        self.assertNotIn('"grep"', sig,
                         "search_files stub still uses obsolete 'grep' target value")
        self.assertNotIn('"find"', doc,
                         "search_files stub docstring still uses obsolete 'find' target value")

    def test_generated_module_accepts_all_params(self):
        """The generated hermes_tools.py module should accept all current params
        without TypeError when called with keyword arguments."""
        src = generate_hermes_tools_module(list(SANDBOX_ALLOWED_TOOLS))

        # Compile the generated module to check for syntax errors
        compile(src, "hermes_tools.py", "exec")

        # Verify specific parameter signatures are in the source
        # search_files must accept context, offset, output_mode
        self.assertIn("context", src)
        self.assertIn("offset", src)
        self.assertIn("output_mode", src)

        # patch must accept mode and patch params
        self.assertIn("mode", src)


# ---------------------------------------------------------------------------
# build_execute_code_schema
# ---------------------------------------------------------------------------

class TestBuildExecuteCodeSchema(unittest.TestCase):
    """Tests for build_execute_code_schema — the dynamic schema generator."""

    def test_default_includes_all_tools(self):
        schema = build_execute_code_schema()
        desc = schema["description"]
        for name, _ in _TOOL_DOC_LINES:
            self.assertIn(name, desc, f"Default schema should mention '{name}'")

    def test_schema_structure(self):
        schema = build_execute_code_schema()
        self.assertEqual(schema["name"], "execute_code")
        self.assertIn("parameters", schema)
        self.assertIn("code", schema["parameters"]["properties"])
        self.assertEqual(schema["parameters"]["required"], [])

    def test_subset_only_lists_enabled_tools(self):
        enabled = {"terminal", "read_file"}
        schema = build_execute_code_schema(enabled)
        desc = schema["description"]
        self.assertIn("terminal(", desc)
        self.assertIn("read_file(", desc)
        self.assertNotIn("web_search(", desc)
        self.assertNotIn("web_extract(", desc)
        self.assertNotIn("write_file(", desc)

    def test_single_tool(self):
        schema = build_execute_code_schema({"terminal"})
        desc = schema["description"]
        self.assertIn("terminal(", desc)
        self.assertNotIn("web_search(", desc)

    def test_import_examples_prefer_web_search_and_terminal(self):
        enabled = {"web_search", "terminal", "read_file"}
        schema = build_execute_code_schema(enabled)
        code_desc = schema["parameters"]["properties"]["code"]["description"]
        self.assertIn("web_search", code_desc)
        self.assertIn("terminal", code_desc)

    def test_import_examples_fallback_when_no_preferred(self):
        """When neither web_search nor terminal are enabled, falls back to
        sorted first two tools."""
        enabled = {"read_file", "write_file", "patch"}
        schema = build_execute_code_schema(enabled)
        code_desc = schema["parameters"]["properties"]["code"]["description"]
        # Should use sorted first 2: patch, read_file
        self.assertIn("patch", code_desc)
        self.assertIn("read_file", code_desc)

    def test_empty_set_produces_valid_description(self):
        """build_execute_code_schema(set()) must not produce 'import , ...'
        in the code property description."""
        schema = build_execute_code_schema(set())
        code_desc = schema["parameters"]["properties"]["code"]["description"]
        self.assertNotIn("import , ...", code_desc,
                         "Empty enabled set produces broken import syntax in description")

    def test_real_scenario_all_sandbox_tools_disabled(self):
        """Reproduce the exact code path from model_tools.py:231-234.

        Scenario: user runs `hermes tools code_execution` (only code_execution
        toolset enabled). tools_to_include = {"execute_code"}.

        model_tools.py does:
            sandbox_enabled = SANDBOX_ALLOWED_TOOLS & tools_to_include
            dynamic_schema = build_execute_code_schema(sandbox_enabled)

        SANDBOX_ALLOWED_TOOLS = {web_search, web_extract, read_file, write_file,
                                  search_files, patch, terminal}
        tools_to_include  = {"execute_code"}
        intersection      = empty set
        """
        # Simulate model_tools.py:233
        tools_to_include = {"execute_code"}
        sandbox_enabled = SANDBOX_ALLOWED_TOOLS & tools_to_include

        self.assertEqual(sandbox_enabled, set(),
                         "Intersection should be empty when only execute_code is enabled")

        schema = build_execute_code_schema(sandbox_enabled)
        code_desc = schema["parameters"]["properties"]["code"]["description"]
        self.assertNotIn("import , ...", code_desc,
                         "Bug: broken import syntax sent to the model")

    def test_real_scenario_only_vision_enabled(self):
        """Another real path: user runs `hermes tools code_execution,vision`.

        tools_to_include = {"execute_code", "vision_analyze"}
        SANDBOX_ALLOWED_TOOLS has neither, so intersection is empty.
        """
        tools_to_include = {"execute_code", "vision_analyze"}
        sandbox_enabled = SANDBOX_ALLOWED_TOOLS & tools_to_include

        self.assertEqual(sandbox_enabled, set())

        schema = build_execute_code_schema(sandbox_enabled)
        code_desc = schema["parameters"]["properties"]["code"]["description"]
        self.assertNotIn("import , ...", code_desc)

    def test_description_mentions_limits(self):
        schema = build_execute_code_schema()
        desc = schema["description"]
        self.assertIn("5-minute timeout", desc)
        self.assertIn("50KB", desc)
        self.assertIn("50 tool calls", desc)

    def test_description_mentions_helpers(self):
        schema = build_execute_code_schema()
        desc = schema["description"]
        self.assertIn("json_parse", desc)
        self.assertIn("shell_quote", desc)
        self.assertIn("retry", desc)

    def test_none_defaults_to_all_tools(self):
        schema_none = build_execute_code_schema(None)
        schema_all = build_execute_code_schema(SANDBOX_ALLOWED_TOOLS)
        self.assertEqual(schema_none["description"], schema_all["description"])


# ---------------------------------------------------------------------------
# Environment variable filtering (security critical)
# ---------------------------------------------------------------------------

@unittest.skipIf(sys.platform == "win32", "UDS not available on Windows")
class TestEnvVarFiltering(unittest.TestCase):
    """Verify that execute_code filters environment variables correctly.

    The child process should NOT receive API keys, tokens, or secrets.
    It should receive safe vars like PATH, HOME, LANG, etc.
    """

    def _get_child_env(self, extra_env=None):
        """Run a script that dumps its environment and return the env dict."""
        code = (
            "import os, json\n"
            "print(json.dumps(dict(os.environ)))\n"
        )
        env_backup = os.environ.copy()
        try:
            if extra_env:
                os.environ.update(extra_env)
            with patch("model_tools.handle_function_call", return_value='{}'), \
                 patch("tools.code_execution_tool._load_config",
                       return_value={"timeout": 10, "max_tool_calls": 50}):
                raw = execute_code(code, task_id="test-env",
                                   enabled_tools=list(SANDBOX_ALLOWED_TOOLS))
        finally:
            os.environ.clear()
            os.environ.update(env_backup)

        result = json.loads(raw)
        self.assertEqual(result["status"], "success", result.get("error", ""))
        return json.loads(result["output"].strip())

    def test_api_keys_excluded(self):
        child_env = self._get_child_env({
            "OPENAI_API_KEY": "sk-secret123",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "FIRECRAWL_API_KEY": "fc-secret",
        })
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("ANTHROPIC_API_KEY", child_env)
        self.assertNotIn("FIRECRAWL_API_KEY", child_env)

    def test_tokens_excluded(self):
        child_env = self._get_child_env({
            "GITHUB_TOKEN": "ghp_secret",
            "MODAL_TOKEN_ID": "tok-123",
            "MODAL_TOKEN_SECRET": "tok-sec",
        })
        self.assertNotIn("GITHUB_TOKEN", child_env)
        self.assertNotIn("MODAL_TOKEN_ID", child_env)
        self.assertNotIn("MODAL_TOKEN_SECRET", child_env)

    def test_password_vars_excluded(self):
        child_env = self._get_child_env({
            "DB_PASSWORD": "hunter2",
            "MY_PASSWD": "secret",
            "AUTH_CREDENTIAL": "cred",
        })
        self.assertNotIn("DB_PASSWORD", child_env)
        self.assertNotIn("MY_PASSWD", child_env)
        self.assertNotIn("AUTH_CREDENTIAL", child_env)

    def test_path_included(self):
        child_env = self._get_child_env()
        self.assertIn("PATH", child_env)

    def test_home_included(self):
        child_env = self._get_child_env()
        self.assertIn("HOME", child_env)

    def test_hermes_rpc_socket_injected(self):
        child_env = self._get_child_env()
        self.assertIn("HERMES_RPC_SOCKET", child_env)

    def test_pythondontwritebytecode_set(self):
        child_env = self._get_child_env()
        self.assertEqual(child_env.get("PYTHONDONTWRITEBYTECODE"), "1")

    def test_timezone_injected_when_set(self):
        env_backup = os.environ.copy()
        try:
            os.environ["HERMES_TIMEZONE"] = "America/New_York"
            child_env = self._get_child_env()
            self.assertEqual(child_env.get("TZ"), "America/New_York")
        finally:
            os.environ.clear()
            os.environ.update(env_backup)

    def test_timezone_not_set_when_empty(self):
        env_backup = os.environ.copy()
        try:
            os.environ.pop("HERMES_TIMEZONE", None)
            child_env = self._get_child_env()
            if "TZ" in child_env:
                self.assertNotEqual(child_env["TZ"], "")
        finally:
            os.environ.clear()
            os.environ.update(env_backup)


class TestScriptSurfaceAndSecurity(unittest.TestCase):
    def test_active_registry_definitions_are_used_for_explicit_tools(self):
        import model_tools
        from tools.registry import registry

        fixture = [{"type": "function", "function": _fixture_definition("vision_analyze")}]
        with patch.object(registry, "get_definitions", return_value=fixture):
            sandbox_tools, active_definitions = code_execution_tool._script_tool_surface(
                ["vision_analyze"], None, None,
            )
        assert "vision_analyze" in sandbox_tools
        assert active_definitions == fixture

    def test_explicit_recursive_only_scope_does_not_expand_to_legacy_tools(self):
        sandbox_tools, active_definitions = code_execution_tool._script_tool_surface(
            ["execute_code"], None, None,
        )
        assert sandbox_tools == set()
        assert active_definitions == []

    def test_control_plane_and_browser_tools_are_denied(self):
        denied = code_execution_tool.SCRIPT_DENIED_TOOLS
        for name in (
            "workflow_tick", "project_list", "browser_cdp", "browser_dialog",
            "computer_use",
        ):
            assert name in denied

    def test_multimodal_content_obeys_output_cap(self):
        payload = base64.b64encode(b"\\x89PNG\\r\\n" + b"x" * 200_000).decode()
        cleaned, artifact, parts = code_execution_tool._prepare_execute_output(
            "data:image/png;base64," + payload,
            50_000,
        )
        assert artifact
        assert len(cleaned) <= 50_100
        assert parts
        assert len(parts[0]["image_url"]["url"]) <= 50_000



    def test_windows_returns_error(self):
        """When SANDBOX_AVAILABLE is False (e.g. when the backend deems
        the sandbox unusable for this environment), execute_code returns
        an error JSON with a readable message pointing the caller at
        regular tool calls.  Previously this was a Windows-only gate;
        execute_code now works on Windows via loopback TCP, so the
        error is only emitted when SANDBOX_AVAILABLE is explicitly
        flipped off (e.g. for future platform-specific disables)."""
        with patch("tools.code_execution_tool.SANDBOX_AVAILABLE", False):
            result = json.loads(execute_code("print('hi')", task_id="test"))
            self.assertIn("error", result)
            self.assertIn("unavailable", result["error"].lower())

    def test_whitespace_only_code(self):
        result = json.loads(execute_code("   \n\t  ", task_id="test"))
        self.assertIn("error", result)
        self.assertIn("No code", result["error"])

    def test_config_include_narrows_legacy_surface(self):
        with patch(
            "tools.code_execution_tool._load_config",
            return_value={"tools": {"include": ["terminal"], "exclude": []}},
        ):
            allowed = json.loads(execute_code(
                "from hermes_tools import terminal; print('included')",
                task_id="config-include-allowed",
                enabled_tools=[],
            ))
            denied = json.loads(execute_code(
                "from hermes_tools import web_search",
                task_id="config-include-denied",
                enabled_tools=[],
            ))
        assert allowed["status"] == "success"
        assert "included" in allowed["output"]
        assert denied["status"] == "error"
        assert "ImportError" in denied["output"]

    def test_catalog_bridge_honors_explicit_allowed_tools_snapshot(self):
        context = CodeExecutionContext(
            "catalog-deny-task",
            "catalog-deny-session",
            (),
            (),
            allowed_tools=("terminal",),
        )
        result = code_execution_tool._dispatch_script_call(
            "__code_mode_catalog__",
            {
                "action": "tool_call",
                "arguments": {
                    "name": "project_list",
                    "arguments": {"path": "outside", "content": "blocked"},
                },
            },
            context,
        )
        assert "not available in this session" in str(result["error"])

    @unittest.skipIf(sys.platform == "win32", "UDS not available on Windows")
    def test_none_enabled_tools_uses_all(self):
        code = (
            "from hermes_tools import terminal, web_search, read_file\n"
            "print('all imports ok')\n"
        )
        with patch("model_tools.handle_function_call",
                    return_value=json.dumps({"ok": True})):
            result = json.loads(execute_code(code, task_id="test-none",
                                             enabled_tools=None))
        self.assertEqual(result["status"], "success")
        self.assertIn("all imports ok", result["output"])

    @unittest.skipIf(sys.platform == "win32", "UDS not available on Windows")
    def test_script_denylist_blocks_control_plane_and_lifecycle_tools(self):
        denied_names = {
            "execute_code", "delegate_task", "tool_call", "clarify", "memory",
            "todo", "cronjob", "kanban_create", "project_create", "project_list",
            "workflow_run", "browser_cdp", "browser_dialog", "computer_use",
        }
        assert not (denied_names & code_execution_tool._scriptable_tool_names(denied_names))
        context = CodeExecutionContext("deny-task", "deny-session", ("all",), ())
        for name in sorted(denied_names):
            result = code_execution_tool._dispatch_script_call(name, {}, context)
            assert "not available" in str(result["error"])

    @unittest.skipIf(sys.platform == "win32", "UDS not available on Windows")
    def test_empty_enabled_tools_uses_all(self):
        """When enabled_tools is [] (empty), all sandbox tools should be available."""
        code = (
            "from hermes_tools import terminal, web_search\n"
            "print('imports ok')\n"
        )
        with patch("model_tools.handle_function_call",
                    return_value=json.dumps({"ok": True})):
            result = json.loads(execute_code(code, task_id="test-empty",
                                             enabled_tools=[]))
        self.assertEqual(result["status"], "success")
        self.assertIn("imports ok", result["output"])

    @unittest.skipIf(sys.platform == "win32", "UDS not available on Windows")
    def test_nonoverlapping_tools_do_not_expand_scope(self):
        """An explicit scope with no legacy tools remains restricted."""
        code = (
            "from hermes_tools import terminal\n"
            "print('should not run')\n"
        )
        result = json.loads(execute_code(
            code, task_id="test-nonoverlap",
            enabled_tools=["vision_analyze", "browser_snapshot"],
        ))
        self.assertEqual(result["status"], "error")
        self.assertIn("ImportError", result["output"])


# ---------------------------------------------------------------------------
# _load_config
# ---------------------------------------------------------------------------

class TestLoadConfig(unittest.TestCase):
    def test_returns_empty_dict_when_cli_config_unavailable(self):
        from tools.code_execution_tool import _load_config
        with patch.dict("sys.modules", {"cli": None}):
            result = _load_config()
            self.assertIsInstance(result, dict)

    def test_returns_code_execution_section(self):
        from tools.code_execution_tool import _load_config
        with patch("hermes_cli.config.read_raw_config",
                   return_value={"code_execution": {"timeout": 120, "max_tool_calls": 10}}):
            result = _load_config()
        self.assertEqual(result, {"timeout": 120, "max_tool_calls": 10})

    def test_does_not_import_interactive_cli(self):
        from tools.code_execution_tool import _load_config
        mock_cli = MagicMock()
        mock_cli.CLI_CONFIG = {"code_execution": {"timeout": 999}}
        with patch.dict("sys.modules", {"cli": mock_cli}), \
             patch("hermes_cli.config.read_raw_config", return_value={}):
            result = _load_config()
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# Interrupt event
# ---------------------------------------------------------------------------

@unittest.skipIf(sys.platform == "win32", "UDS not available on Windows")
class TestInterruptHandling(unittest.TestCase):
    def test_interrupt_event_stops_execution(self):
        """When interrupt is set for the execution thread, execute_code should stop."""
        code = "import time; time.sleep(60); print('should not reach')"
        from tools.interrupt import set_interrupt

        # Capture the main thread ID so we can target the interrupt correctly.
        # execute_code runs in the current thread; set_interrupt needs its ID.
        main_tid = threading.current_thread().ident

        def set_interrupt_after_delay():
            import time as _t
            _t.sleep(1)
            set_interrupt(True, main_tid)

        t = threading.Thread(target=set_interrupt_after_delay, daemon=True)
        t.start()

        try:
            with patch("model_tools.handle_function_call",
                        return_value=json.dumps({"ok": True})), \
                 patch("tools.code_execution_tool._load_config",
                       return_value={"timeout": 30, "max_tool_calls": 50}):
                result = json.loads(execute_code(
                    code, task_id="test-interrupt",
                    enabled_tools=list(SANDBOX_ALLOWED_TOOLS),
                ))
            self.assertEqual(result["status"], "interrupted")
            self.assertIn("interrupted", result["output"])
        finally:
            set_interrupt(False, main_tid)
            t.join(timeout=3)


def test_temp_home_config_controls_output_limit_and_artifact_directory():
    with tempfile.TemporaryDirectory() as home:
        artifact_dir = Path(home) / "artifacts"
        (Path(home) / "config.yaml").write_text(yaml.safe_dump({
            "code_execution": {
                "mode": "strict",
                "max_stdout_bytes": 80,
                "max_stderr_bytes": 40,
                "artifact_dir": str(artifact_dir),
            },
        }), encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": home}, clear=False):
            os.environ.pop("HERMES_CONFIG", None)
            with patch("model_tools.handle_function_call", side_effect=_mock_handle_function_call):
                result = json.loads(execute_code(
                    "print('HEAD')\nprint('x' * 200)\nprint('TAIL')",
                    task_id="config-e2e",
                    enabled_tools=list(SANDBOX_ALLOWED_TOOLS),
                ))

            artifact_path = Path(result["artifact_path"])
            assert artifact_path.parent == artifact_dir
            assert artifact_path.read_text(encoding="utf-8").startswith("HEAD")

    assert result["status"] == "success"
    assert "HEAD" in result["output"]
    assert "TAIL" in result["output"]
    assert "TRUNCATED" in result["output"]


class TestHeadTailTruncation(unittest.TestCase):
    """Tests for head+tail truncation of large stdout in execute_code."""

    def _run(self, code):
        with patch("model_tools.handle_function_call", side_effect=_mock_handle_function_call):
            result = execute_code(
                code=code,
                task_id="test-task",
                enabled_tools=list(SANDBOX_ALLOWED_TOOLS),
            )
        return json.loads(result)

    def test_short_output_not_truncated(self):
        """Output under MAX_STDOUT_BYTES should not be truncated."""
        result = self._run('print("small output")')
        self.assertEqual(result["status"], "success")
        self.assertIn("small output", result["output"])
        self.assertNotIn("TRUNCATED", result["output"])

    def test_large_output_preserves_head_and_tail(self):
        """Output exceeding MAX_STDOUT_BYTES keeps both head and tail."""
        code = '''
# Print HEAD marker, then filler, then TAIL marker
print("HEAD_MARKER_START")
for i in range(15000):
    print(f"filler_line_{i:06d}_padding_to_fill_buffer")
print("TAIL_MARKER_END")
'''
        result = self._run(code)
        self.assertEqual(result["status"], "success")
        output = result["output"]
        # Head should be preserved
        self.assertIn("HEAD_MARKER_START", output)
        # Tail should be preserved (this is the key improvement)
        self.assertIn("TAIL_MARKER_END", output)
        # Truncation notice should be present
        self.assertIn("TRUNCATED", output)
        self.assertTrue(result["stdout_truncated"])
        self.assertGreater(result["stdout_bytes_total"], result["stdout_bytes_captured"])
        self.assertGreater(result["stdout_bytes_omitted"], 0)
        self.assertIn("execute_code stdout was truncated", result["warning"])

    def test_truncation_notice_format(self):
        """Truncation notice includes byte counts."""
        code = '''
for i in range(15000):
    print(f"padding_line_{i:06d}_xxxxxxxxxxxxxxxxxxxxxxxxxx")
'''
        result = self._run(code)
        output = result["output"]
        if "TRUNCATED" in output:
            self.assertIn("bytes omitted", output)
            self.assertIn("total", output)

    def test_short_output_has_explicit_non_truncated_metadata(self):
        """Even non-truncated output exposes unambiguous truncation metadata."""
        result = self._run('print("small output")')
        self.assertFalse(result["stdout_truncated"])
        self.assertEqual(result["stdout_bytes_omitted"], 0)
        self.assertEqual(result["stdout_bytes_total"], result["stdout_bytes_captured"])
        self.assertEqual(result["exit_code"], 0)

    def test_remote_large_output_gets_truncation_metadata(self):
        """Remote backend output capping is explicit in the JSON result."""
        class FakeEnv:
            def __init__(self):
                self.commands = []

            def get_temp_dir(self):
                return "/tmp"

            def execute(self, command, cwd=None, timeout=None):
                self.commands.append((command, cwd, timeout))
                if "command -v python3" in command:
                    return {"output": "OK\n"}
                if "python3 script.py" in command:
                    return {"output": "HEAD\n" + ("x" * 80_000) + "\nTAIL\n", "returncode": 0}
                return {"output": ""}

        fake_thread = MagicMock()

        with patch("tools.code_execution_tool._load_config", return_value={"timeout": 30, "max_tool_calls": 5}), \
             patch("tools.code_execution_tool._get_or_create_env", return_value=(FakeEnv(), "ssh")), \
             patch("tools.code_execution_tool._ship_file_to_remote"), \
             patch("tools.code_execution_tool.threading.Thread", return_value=fake_thread):
            result = json.loads(_execute_remote("print('large')", "task-1", ["terminal"]))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["stdout_truncated"])
        self.assertIn("HEAD", result["output"])
        self.assertIn("TAIL", result["output"])
        self.assertGreater(result["stdout_bytes_total"], result["stdout_bytes_captured"])
        self.assertGreater(result["stdout_bytes_omitted"], 0)
        self.assertIn("execute_code stdout was truncated", result["warning"])


class TestRpcTokenAuthorization(unittest.TestCase):
    """The per-session RPC token must gate socket dispatch (fail-closed).

    Regression coverage for the execute_code tool-socket hardening: a
    request without the matching HERMES_RPC_TOKEN must be rejected before
    the tool is dispatched, while a request carrying the correct token
    round-trips normally.
    """

    def _drive_server(self, rpc_token, requests):
        """Run _rpc_server_loop against a real AF_UNIX socketpair.

        Sends each dict in *requests* as a newline-delimited JSON message
        and returns the list of decoded JSON responses.
        """
        from tools.code_execution_tool import _rpc_server_loop

        # socketpair gives us a connected client end and a "server" end we
        # can hand to accept() by wrapping it in a tiny listener shim.
        srv, cli = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

        class _OneShotListener:
            """Minimal object exposing the .accept()/.settimeout() the loop uses."""

            def __init__(self, conn):
                self._conn = conn
                self._served = False

            def settimeout(self, _t):
                pass

            def accept(self):
                if self._served:
                    raise socket.timeout()
                self._served = True
                return self._conn, ("peer", 0)

        listener = _OneShotListener(srv)
        stop_event = threading.Event()
        tool_call_log = []
        tool_call_counter = [0]

        def _run():
            with (
                patch(
                    "hermes_cli.plugins.get_plugin_manager",
                    return_value=SimpleNamespace(_middleware={"tool_execution": [object()]}),
                ),
                patch(
                    "model_tools.handle_function_call",
                    side_effect=_mock_handle_function_call,
                ),
            ):
                _rpc_server_loop(
                    listener,
                    "test-task",
                    tool_call_log,
                    tool_call_counter,
                    max_tool_calls=10,
                    allowed_tools=frozenset({"terminal"}),
                    stop_event=stop_event,
                    rpc_token=rpc_token,
                )

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        responses = []
        try:
            for req in requests:
                cli.sendall((json.dumps(req) + "\n").encode())
            cli.settimeout(5)
            buf = b""
            while len(responses) < len(requests):
                chunk = cli.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if line:
                        responses.append(json.loads(line.decode()))
        finally:
            stop_event.set()
            cli.close()
            srv.close()
            t.join(timeout=5)
        return responses

    def test_missing_token_rejected(self):
        """A request with no token is rejected as Unauthorized."""
        resp = self._drive_server(
            "secret-token", [{"tool": "terminal", "args": {"command": "echo hi"}}]
        )
        self.assertEqual(len(resp), 1)
        self.assertIn("Unauthorized", resp[0].get("error", ""))

    def test_wrong_token_rejected(self):
        """A request with a mismatched token is rejected as Unauthorized."""
        resp = self._drive_server(
            "secret-token",
            [{"tool": "terminal", "args": {"command": "echo hi"}, "token": "nope"}],
        )
        self.assertEqual(len(resp), 1)
        self.assertIn("Unauthorized", resp[0].get("error", ""))

    def test_matching_token_dispatched(self):
        """A request carrying the correct token round-trips to the tool."""
        resp = self._drive_server(
            "secret-token",
            [{"tool": "terminal", "args": {"command": "echo hi"}, "token": "secret-token"}],
        )
        self.assertEqual(len(resp), 1)
        self.assertNotIn("Unauthorized", json.dumps(resp[0]))
        self.assertIn("mock output for: echo hi", json.dumps(resp[0]))

    def test_empty_server_token_fails_closed(self):
        """An empty server-side token rejects everything (fail-closed)."""
        resp = self._drive_server(
            "", [{"tool": "terminal", "args": {"command": "echo hi"}, "token": ""}]
        )
        self.assertEqual(len(resp), 1)
        self.assertIn("Unauthorized", resp[0].get("error", ""))

    def test_generated_module_sends_token(self):
        """The generated hermes_tools module reads HERMES_RPC_TOKEN and sends it."""
        src = generate_hermes_tools_module(["terminal"], transport="uds")
        self.assertIn("HERMES_RPC_TOKEN", src)
        self.assertIn('"token"', src)


def test_code_mode_denies_recursive_and_interactive_tools():
    allowed = code_execution_tool._scriptable_tool_names(
        session_tools={"read_file", "execute_code", "delegate_task", "clarify"},
        operation_metadata={"read_file": {"read_only": True}},
    )
    assert allowed == {"read_file"}


def test_unknown_mcp_operation_is_destructive_by_default():
    decision = code_execution_tool._script_operation_decision(
        "mcp__server__write", {"read_only": False, "destructive": True}
    )
    assert decision == "approval_required"


def test_execute_code_returns_structured_image_for_data_url():
    result = json.loads(execute_code(
        "print('data:image/png;base64,AAAA')",
        task_id="structured-image-data",
    ))

    assert result["_multimodal"] is True
    assert result["status"] == "success"
    assert isinstance(result["duration_seconds"], (int, float))
    assert result["tool_calls_made"] == 0
    assert result["content"] == [{
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }]


def test_execute_code_returns_structured_image_for_local_path(tmp_path):
    image_path = tmp_path / "generated.webp"
    image_path.write_bytes(b"RIFF")
    result = json.loads(execute_code(
        f"print({str(image_path)!r})",
        task_id="structured-image-path",
    ))

    assert result["_multimodal"] is True
    assert result["content"] == [{
        "type": "image_url",
        "image_url": {"url": image_path.resolve().as_uri()},
    }]


def test_execute_code_collects_generated_image_artifact():
    result = json.loads(execute_code(
        "import os\nfrom pathlib import Path\n"
        "p = Path(os.environ['HERMES_ARTIFACTS_DIR']) / 'chart.png'\n"
        "p.write_bytes(b'PNG')\nprint(p)",
        task_id="structured-generated-image",
    ))

    image_url = result["content"][0]["image_url"]["url"]
    assert result["_multimodal"] is True
    assert image_url.startswith("file://")
    assert Path(image_url[7:]).is_file()


def test_image_artifact_normalization_accepts_urls_paths_and_structured_values(tmp_path):
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"\x89PNG\r\n")
    expected = image_path.resolve().as_uri()

    values = [
        "https://example.test/chart.png",
        "data:image/png;base64,AAAA",
        str(image_path),
        {"type": "image_url", "image_url": {"url": str(image_path)}},
    ]
    for value in values:
        assert is_structured_image_artifact(value)
        assert normalize_image_artifact(value) == {
            "type": "image_url",
            "image_url": {"url": expected if value in (str(image_path), values[-1]) else (value if isinstance(value, str) else value["image_url"]["url"])},
        }

    assert not is_structured_image_artifact("ordinary tool result")
    assert normalize_image_artifact("ordinary tool result") is None


def test_execute_code_spills_redacted_large_output_to_durable_file(tmp_path, monkeypatch):
    monkeypatch.setattr(code_execution_tool, "MAX_STDOUT_BYTES", 128)

    def redact(text, **_kwargs):
        return text.replace("TOP_SECRET", "[REDACTED]")

    monkeypatch.setattr("agent.redact.redact_sensitive_text", redact)
    result = json.loads(execute_code(
        "print('TOP_SECRET')\nprint('x' * 2000)",
        task_id="structured-image-large-output",
    ))

    assert result["status"] == "success"
    assert result["truncated"] is True
    artifact_path = result["artifact_path"]
    assert os.path.isfile(artifact_path)
    artifact = Path(artifact_path).read_text(encoding="utf-8")
    assert "TOP_SECRET" not in artifact
    assert "[REDACTED]" in artifact
    assert len(result["output"]) < 2000


def test_execute_code_spills_oversized_text_even_with_image_content(monkeypatch):
    monkeypatch.setattr(code_execution_tool, "MAX_STDOUT_BYTES", 128)
    result = json.loads(execute_code(
        "import json\n"
        "print(json.dumps(['x' * 2000, 'data:image/png;base64,AAAA']))",
        task_id="structured-image-mixed-large-output",
    ))

    assert result["status"] == "success"
    assert result["_multimodal"] is True
    assert result["content"] == [{
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }]
    assert result["truncated"] is True
    assert os.path.isfile(result["artifact_path"])
    assert len(result["output"]) < 2000


def test_generated_save_artifact_persists_a_real_file():
    result = _dispatch_script(
        """
import json
import os
from pathlib import Path
from hermes_tools import save_artifact
source = Path(os.environ['HERMES_ARTIFACTS_DIR']) / 'report.txt'
source.write_text('artifact output', encoding='utf-8')
print(json.dumps(save_artifact(str(source))))
""",
        CodeExecutionContext("save-artifact-task", None, (), ()),
        [_fixture_definition("fixture")],
    )

    saved = result
    assert saved["status"] == "ok"
    assert os.path.isfile(saved["artifact_path"])
    assert open(saved["artifact_path"], encoding="utf-8").read() == "artifact output"


def test_generated_save_artifact_accepts_bytes_and_rejects_external_paths():
    result = _dispatch_script(
        """
import json
from hermes_tools import save_artifact
print(json.dumps(save_artifact(b'artifact bytes', name='bytes.txt')))
""",
        CodeExecutionContext("save-artifact-bytes", None, (), ()),
        [_fixture_definition("fixture")],
    )
    assert result["status"] == "ok"
    assert open(result["artifact_path"], encoding="utf-8").read() == "artifact bytes"

    external = _dispatch_script(
        """
import json
from hermes_tools import save_artifact
print(json.dumps(save_artifact('/etc/hosts')))
""",
        CodeExecutionContext("save-artifact-external", None, (), ()),
        [_fixture_definition("fixture")],
    )
    assert "inside the execution artifact roots" in external["error"]

    escaped = _dispatch_script(
        """
import json
import os
from pathlib import Path
from hermes_tools import save_artifact
link = Path(os.environ['HERMES_ARTIFACTS_DIR']) / 'escape.txt'
link.symlink_to('/etc/hosts')
print(json.dumps(save_artifact(str(link))))
""",
        CodeExecutionContext("save-artifact-symlink", None, (), ()),
        [_fixture_definition("fixture")],
    )
    assert "inside the execution artifact roots" in escaped["error"]


def test_registry_dispatch_exposes_only_multimodal_execute_code_results(monkeypatch):
    from tools.registry import registry

    monkeypatch.setattr(
        code_execution_tool,
        "execute_code",
        lambda **_kwargs: json.dumps({
            "status": "success",
            "_multimodal": True,
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
        }),
    )
    multimodal = registry.dispatch("execute_code", {"code": "print('x')"}, task_id="registry-image")
    assert isinstance(multimodal, dict)
    assert multimodal["_multimodal"] is True

    monkeypatch.setattr(code_execution_tool, "execute_code", lambda **_kwargs: '{"status":"success"}')
    ordinary = registry.dispatch("execute_code", {"code": "print('x')"}, task_id="registry-text")
    assert isinstance(ordinary, str)
    assert ordinary == '{"status":"success"}'


def test_execute_code_keeps_ordinary_string_output_shape():
    result = json.loads(execute_code("print('ordinary tool result')", task_id="ordinary-output"))

    assert result["status"] == "success"
    assert result["output"] == "ordinary tool result\n"
    assert "_multimodal" not in result
    assert "artifact_path" not in result


if __name__ == "__main__":
    unittest.main()
