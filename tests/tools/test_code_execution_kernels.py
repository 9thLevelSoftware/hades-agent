import json
import os
import base64
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest

os.environ["TERMINAL_ENV"] = "local"

from tools import code_execution_tool
from tools.code_execution_tool import (
    ExecutionKernelRegistry,
    cleanup_execution_kernels,
    execute_code,
)


@pytest.fixture(autouse=True)
def _local_kernel_environment(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    yield
    cleanup_execution_kernels("kernel-test")
    cleanup_execution_kernels("kernel-a")
    cleanup_execution_kernels("kernel-b")
    cleanup_execution_kernels("kernel-timeout")


def _run(code, task_id, **kwargs):
    kernel_id = kwargs.pop("kernel_id", "test")
    return json.loads(
        execute_code(
            code,
            task_id=task_id,
            enabled_tools=[],
            persistent=True,
            kernel_id=kernel_id,
            **kwargs,
        )
    )


def test_persistent_kernel_preserves_state_and_fresh_calls_do_not():
    first = _run("counter = 41", "kernel-test")
    second = _run("print(counter + 1)", "kernel-test")

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert second["output"].strip() == "42"

    fresh_first = json.loads(
        execute_code("counter = 41", task_id="kernel-test", enabled_tools=[])
    )
    fresh_second = json.loads(
        execute_code("print(counter + 1)", task_id="kernel-test", enabled_tools=[])
    )
    assert fresh_first["status"] == "success"
    assert fresh_second["status"] == "error"
    assert "NameError" in fresh_second["output"]


def test_persistent_kernel_reports_stdout_truncation_metadata():
    with patch(
        "tools.code_execution_tool._load_config",
        return_value={"max_stdout_bytes": 80},
    ):
        result = _run("print('x' * 200)", "kernel-test", kernel_id="metadata")

    assert result["status"] == "success"
    assert result["stdout_truncated"] is True
    assert result["stdout_bytes_total"] > result["stdout_bytes_captured"]
    assert result["stdout_bytes_omitted"] > 0
    assert "execute_code stdout was truncated" in result["warning"]


def test_config_persistent_default_is_opt_in_without_overriding_explicit_false(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "code_execution:\n  persistent: true\n  kernel_idle_ttl: 900\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    try:
        first = json.loads(execute_code(
            "counter = 41", task_id="kernel-config", enabled_tools=[], kernel_id="configured",
        ))
        second = json.loads(execute_code(
            "print(counter + 1)", task_id="kernel-config", enabled_tools=[], kernel_id="configured",
        ))
        fresh_first = json.loads(execute_code(
            "counter = 41", task_id="kernel-config-fresh", enabled_tools=[], persistent=False,
        ))
        fresh_second = json.loads(execute_code(
            "print(counter + 1)", task_id="kernel-config-fresh", enabled_tools=[], persistent=False,
        ))
    finally:
        cleanup_execution_kernels("kernel-config")
        cleanup_execution_kernels("kernel-config-fresh")

    assert first["persistent"] is True
    assert second["output"].strip() == "42"
    assert fresh_first["status"] == "success"
    assert fresh_second["status"] == "error"


def test_persistent_kernel_exports_artifact_root_and_multimodal_results():
    env_probe = _run(
        "import os; print(bool(os.environ.get('HERMES_ARTIFACTS_DIR')))",
        "kernel-test",
        kernel_id="artifacts",
    )
    assert env_probe["output"].strip() == "True"

    payload = base64.b64encode(b"\\x89PNG\\r\\n" + b"x" * 1024).decode()
    image_result = _run(
        f"print('data:image/png;base64,{payload}')",
        "kernel-test",
        kernel_id="artifacts",
    )
    assert image_result.get("_multimodal") is True
    assert image_result.get("content")


    execution_manager = SimpleNamespace(_middleware={"tool_execution": [object()]})
    with (
        patch(
            "hermes_cli.plugins.get_plugin_manager",
            return_value=execution_manager,
        ),
        patch(
            "model_tools.handle_function_call",
            return_value=json.dumps({"output": "rpc-ok", "exit_code": 0}),
        ),
    ):
        result = _run(
            "from hermes_tools import terminal\nprint(terminal('echo rpc'))",
            "kernel-test",
        )
    assert result["status"] == "success"
    assert "rpc-ok" in result["output"]
    assert result["tool_calls_made"] == 1


def test_nested_sessions_config_enables_persistent_reuse():
    config = {
        "sessions": {"enabled": True, "idle_timeout_seconds": 30},
    }
    try:
        with patch("tools.code_execution_tool._load_config", return_value=config):
            first = json.loads(execute_code(
                "nested_value = 41",
                task_id="kernel-nested-config",
                enabled_tools=[],
            ))
            second = json.loads(execute_code(
                "print(nested_value + 1)",
                task_id="kernel-nested-config",
                enabled_tools=[],
            ))
    finally:
        cleanup_execution_kernels("kernel-nested-config")
    assert first["persistent"] is True
    assert second["output"].strip() == "42"


def test_persistent_kernel_interrupt_kills_worker_and_returns_interrupted_result():
    from tools.interrupt import set_interrupt

    result_holder = {}
    started = threading.Event()
    pid_holder = {}

    def run_code():
        started.set()
        result_holder["result"] = _run("while True: pass", "kernel-test", timeout=10)

    worker = threading.Thread(target=run_code)
    worker.start()
    assert started.wait(1)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        kernel = code_execution_tool._kernel_registry.get("kernel-test", "test")
        if kernel is not None and kernel.process is not None:
            pid_holder["pid"] = kernel.process.pid
            set_interrupt(True, thread_id=worker.ident)
            break
        time.sleep(0.02)
    else:
        pytest.fail("persistent kernel did not start")
    worker.join(5)
    set_interrupt(False, thread_id=worker.ident)

    assert not worker.is_alive()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and psutil.pid_exists(pid_holder["pid"]):
        time.sleep(0.02)
    assert not psutil.pid_exists(pid_holder["pid"])
    result = result_holder["result"]
    assert result["status"] == "interrupted"
    assert result.get("timed_out") is not True
    assert "execution interrupted" in result["output"]
    assert result["kernel_restarted"] is True


def test_persistent_kernel_resets_tool_call_limit_per_execution():
    execution_manager = SimpleNamespace(_middleware={"tool_execution": [object()]})

    def dispatch(function_name, function_args, **kwargs):
        return json.dumps({"output": function_args.get("command", "rpc-ok"), "exit_code": 0})

    with (
        patch("tools.code_execution_tool._load_config", return_value={"max_tool_calls": 1}),
        patch("hermes_cli.plugins.get_plugin_manager", return_value=execution_manager),
        patch("model_tools.handle_function_call", side_effect=dispatch),
    ):
        first = _run(
            "from hermes_tools import terminal\n"
            "one = terminal('first')\n"
            "two = terminal('second')\n"
            "print(one.get('output'), two.get('error'))",
            "kernel-test",
        )
        second = _run(
            "from hermes_tools import terminal\n"
            "print(terminal('again').get('output'))",
            "kernel-test",
        )

    assert first["status"] == "success"
    assert first["tool_calls_made"] == 1
    assert "Tool call limit reached" in first["output"]
    assert second["status"] == "success"
    assert second["tool_calls_made"] == 1
    assert "again" in second["output"]


def test_persistent_kernel_updates_dispatch_context_on_reuse():
    execution_manager = SimpleNamespace(_middleware={"tool_execution": [object()]})
    seen = []

    def dispatch(function_name, function_args, **kwargs):
        from tools.approval import get_current_session_key

        seen.append({**kwargs, "approval_scope": get_current_session_key()})
        return json.dumps({"output": "rpc-ok", "exit_code": 0})

    with (
        patch("hermes_cli.plugins.get_plugin_manager", return_value=execution_manager),
        patch("model_tools.handle_function_call", side_effect=dispatch),
    ):
        first = _run(
            "from hermes_tools import terminal\nprint(terminal('first').get('output'))",
            "kernel-test",
            session_id="session-a",
        )
        second = _run(
            "from hermes_tools import terminal\nprint(terminal('second').get('output'))",
            "kernel-test",
            session_id="session-b",
        )

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert [item["session_id"] for item in seen] == ["session-a", "session-b"]
    assert [item["approval_scope"] for item in seen] == ["session-a", "session-b"]


def test_reset_only_execute_code_call_accepts_omitted_code():
    first = _run("secret = 'stale'", "kernel-test")
    reset = json.loads(execute_code(
        task_id="kernel-test",
        enabled_tools=[],
        persistent=True,
        kernel_id="test",
        reset=True,
    ))

    assert first["status"] == "success"
    assert reset["status"] == "success"
    assert reset["kernel_reset"] is True


def test_persistent_execute_without_task_id_returns_structured_error():
    result = json.loads(execute_code(
        "print('must not run')",
        enabled_tools=[],
        persistent=True,
        kernel_id="test",
    ))

    assert result["status"] == "error"
    assert "explicit task_id" in result["error"]
    assert result["tool_calls_made"] == 0


def test_distinct_explicit_task_ids_do_not_share_persistent_kernel():
    first = _run("value = 'task-a'", "kernel-a", kernel_id="shared")
    second = _run("value = 'task-b'", "kernel-b", kernel_id="shared")
    registry = code_execution_tool._kernel_registry

    assert first["status"] == "success"
    assert second["status"] == "success"
    kernel_a = registry.get("kernel-a", "shared")
    kernel_b = registry.get("kernel-b", "shared")
    assert kernel_a is not None and kernel_b is not None
    assert kernel_a.process.pid != kernel_b.process.pid
    assert _run("print(value)", "kernel-a", kernel_id="shared")["output"].strip() == "task-a"
    assert _run("print(value)", "kernel-b", kernel_id="shared")["output"].strip() == "task-b"


def test_reset_clears_state_and_timeout_restarts_clean_kernel():
    assert _run("secret = 'stale'", "kernel-test")["status"] == "success"
    reset = _run("", "kernel-test", reset=True)
    assert reset["status"] == "success"
    assert reset["kernel_reset"] is True
    assert "stale" not in _run("print(secret)", "kernel-test")["output"]

    assert _run("value = 7", "kernel-timeout")["status"] == "success"
    timed_out = _run("while True: pass", "kernel-timeout", timeout=0.15)
    assert timed_out["status"] == "timeout"
    assert timed_out["timed_out"] is True
    clean = _run("print('clean')", "kernel-timeout")
    assert clean["status"] == "success"
    assert clean["output"].strip() == "clean"
    assert "value" not in clean["output"]


def test_same_kernel_id_isolated_between_tasks():
    assert _run("value = 'task-a'", "kernel-a")["status"] == "success"
    assert _run("value = 'task-b'", "kernel-b")["status"] == "success"
    assert _run("print(value)", "kernel-a")["output"].strip() == "task-a"
    assert _run("print(value)", "kernel-b")["output"].strip() == "task-b"


def test_idle_expiry_terminates_and_removes_kernel():
    now = [100.0]
    registry = ExecutionKernelRegistry(clock=lambda: now[0], idle_ttl=1.0)
    old_registry = code_execution_tool._kernel_registry
    code_execution_tool._kernel_registry = registry
    try:
        result = _run("value = 1", "kernel-test")
        assert result["status"] == "success"
        kernel = registry.get("kernel-test", "test")
        assert kernel is not None
        pid = kernel.process.pid
        tmpdir = kernel._tmpdir

        now[0] += 2.0
        assert registry.cleanup_expired() == 1
        assert registry.get("kernel-test", "test") is None
        assert not os.path.exists(tmpdir)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and psutil.pid_exists(pid):
            time.sleep(0.02)
        assert not psutil.pid_exists(pid)
    finally:
        registry.close_all()
        code_execution_tool._kernel_registry = old_registry


def test_idle_reaper_expires_kernel_without_registry_access_after_wait():
    registry = ExecutionKernelRegistry(idle_ttl=0.1)
    old_registry = code_execution_tool._kernel_registry
    code_execution_tool._kernel_registry = registry
    try:
        result = _run("value = 1", "kernel-test", kernel_id="reaper")
        assert result["status"] == "success"
        kernel = registry.get("kernel-test", "reaper")
        assert kernel is not None
        pid = kernel.process.pid
        tmpdir = kernel._tmpdir

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not psutil.pid_exists(pid) and not os.path.exists(tmpdir):
                break
            time.sleep(0.1)
        else:
            pytest.fail("Kernel process did not terminate or clean up within timeout")
    finally:
        registry.close_all()
        code_execution_tool._kernel_registry = old_registry


def test_new_kernel_preserves_active_tool_definitions():
    registry = ExecutionKernelRegistry()
    definitions = [{
        "type": "function",
        "function": {"name": "kernel_fixture", "parameters": {}},
    }]
    try:
        kernel = registry.get_or_create(
            "kernel-definitions",
            "persistent",
            frozenset({"kernel_fixture"}),
            code_execution_tool.CodeExecutionContext(
                "kernel-definitions", "session", (), (),
            ),
            active_tool_definitions=definitions,
        )
        assert kernel.active_tool_definitions == definitions
    finally:
        registry.close_all()


def test_cleanup_is_task_scoped_idempotent_and_reaps_children():
    assert _run("value = 'a'", "kernel-a", kernel_id="one")["status"] == "success"
    assert _run("value = 'a'", "kernel-a", kernel_id="two")["status"] == "success"
    assert _run("value = 'b'", "kernel-b", kernel_id="one")["status"] == "success"

    registry = code_execution_tool._kernel_registry
    task_a_pids = [
        kernel.process.pid
        for kernel in registry.for_task("kernel-a").values()
    ]
    task_a_tmpdirs = [
        kernel._tmpdir
        for kernel in registry.for_task("kernel-a").values()
    ]
    task_b_pids = [
        kernel.process.pid
        for kernel in registry.for_task("kernel-b").values()
    ]
    assert len(task_a_pids) == 2
    assert len(task_b_pids) == 1

    cleanup_execution_kernels("kernel-a")
    cleanup_execution_kernels("kernel-a")
    assert registry.for_task("kernel-a") == {}
    assert registry.for_task("kernel-b")
    assert all(not os.path.exists(tmpdir) for tmpdir in task_a_tmpdirs)

    for pid in task_a_pids:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and psutil.pid_exists(pid):
            time.sleep(0.02)
        assert not psutil.pid_exists(pid)
    assert all(psutil.pid_exists(pid) for pid in task_b_pids)
