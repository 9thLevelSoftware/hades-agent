# Code-Mode Tool Orchestration Implementation Plan

> For agentic workers: implement this plan task-by-task. Preserve the existing `execute_code` tool contract while extending its generated module and RPC dispatch surface. Every task ends with a focused test run and a commit.

**Goal:** Turn `execute_code` from a seven-tool stub surface into a registry-backed, schema-aware API over the active builtin/plugin/MCP tools, with optional persistent sessions and structured artifacts, without inflating the model-visible tool schema.

**Architecture:** Keep `execute_code` as the single model tool and reuse its existing UDS (local) and file-RPC (remote) transports. Generate typed wrappers for the current session's non-deferred tools and expose `search_tools`, `describe_tool`, and `call_tool` for the deferred long tail. Every scripted call still re-enters `model_tools.handle_function_call`, so the normal middleware, operation metadata, approval, guardrail, redaction, and tool-result paths remain authoritative.

**Tech Stack:** Python 3, existing `ToolRegistry` JSON schemas, existing `tools/tool_search.py` catalog, UDS/TCP/file RPC, `DaemonThreadPoolExecutor`-style lifecycle registries, existing `_multimodal` result envelope, and `tool_result_storage` spill storage.

## Global Constraints

- Do not add a second model tool; `execute_code` remains the only model-visible entry point.
- Do not inline the entire registry into the `execute_code` schema; the long tail is reached through generic catalog functions.
- Reuse `ToolRegistry`, `handle_function_call`, `tool_search`, `thread_context`, and the existing local/remote environment lifecycle.
- `execute_code` itself, delegation recursion, interactive prompts, `clarify`, `memory`, and lifecycle-only tools remain denied from scripts by default.
- Read-only tools may execute without a second approval prompt; destructive and unknown tools remain conservative until the permission-policy engine is available. Unknown MCP/plugin operations default to destructive.
- Persistent kernels are opt-in, keyed by `(task_id, session)`, and must be resettable and idle-reaped. They never outlive their terminal environment.
- Never put recalled/script-generated content into the frozen system prompt. Artifacts and tool results enter only through the existing result envelope or readable file paths.
- No secrets are copied into generated module source, kernel output, artifact previews, or persisted RPC records.
- Existing POSIX/Windows transport behavior remains supported. A platform that cannot provide the requested execution mode returns a clear tool error rather than silently running unsandboxed.
- Verification must include real registry dispatch and a temporary `HERMES_HOME`/terminal environment; mocks alone are insufficient for RPC, approvals, or artifact persistence.

## Current-State Review

The source-tree review confirms that the proposal is an extension, not a replacement:

- `tools/code_execution_tool.py` already has `_TOOL_STUBS`, `generate_hermes_tools_module`, `_rpc_server_loop`, `_rpc_poll_loop`, `_get_or_create_env`, `_execute_remote`, `execute_code`, and `build_execute_code_schema`.
- Local execution uses a fresh child process per call; remote execution uses a persistent terminal environment but a fresh RPC child/sandbox per call. No Python variables survive between calls.
- RPC dispatch already reaches `model_tools.handle_function_call`; the missing context is full session/toolset/operation metadata propagation, not a new dispatcher.
- `tools/registry.py` already exposes `ToolEntry` schemas and `get_operation_metadata`; `_normalize_handler_result` already understands `_multimodal` results.
- `tools/tool_search.py` already owns catalog construction, deferral classification, search, description, scope checks, and underlying-call resolution.
- `tools/tool_result_storage.py` already provides the non-image spill pattern.
- Existing regression anchors are `tests/tools/test_code_execution.py`, `tests/tools/test_code_execution_modes.py`, and `tests/tools/test_execute_code_approval_cluster.py`.

The plan therefore skips a new code-mode framework, a new catalog implementation, and a new artifact protocol.

## Dependency and Release Order

1. Registry-derived wrapper contract and schema conversion.
2. Full-surface RPC context and conservative operation gating.
3. Optional persistent kernel and lifecycle cleanup.
4. Artifact collection and multimodal/spill return handling.
5. Remote-backend parity, documentation, and end-to-end verification.

The first two slices are independently useful. Persistent sessions depend on the generated module and full dispatch contract. Artifacts depend on the child-environment contract but do not change tool dispatch.

## File Map

- Modify: `tools/code_execution_tool.py` — generated module, RPC request context, allowed-tool calculation, kernel registry, artifact collection, schema description.
- Modify: `tools/tool_search.py` — expose a small in-process catalog adapter usable by generated `search_tools`/`describe_tool`/`call_tool` wrappers without duplicating scoring or scope rules.
- Modify: `tools/registry.py` — expose the minimum schema/metadata snapshot needed by code-mode generation; do not add a second registry.
- Modify: `model_tools.py` — pass session/toolset context to RPC dispatch only where the current `execute_code` call already has it; keep the existing dynamic-schema rebuild.
- Modify: `tools/thread_context.py` — reuse existing propagation helper if the new RPC worker path needs a named context field; do not add a parallel context mechanism.
- Modify: `tools/terminal_tool.py` — reuse or expose the existing environment/task-id resolution for kernel cleanup; no new environment registry.
- Modify: `tools/tool_result_storage.py` — call the existing spill primitive; only add a narrowly scoped artifact-registration helper if the current interface cannot register a returned file.
- Modify: `agent/tool_executor.py` — no behavior redesign; verify the existing `_multimodal` envelope and spilled-file paths accept the new result shape, changing only if an actual integration test proves a gap.
- Modify: `hermes_cli/config.py` and `cli-config.yaml.example` — document `code_execution.tools` and `code_execution.sessions` opt-in settings using YAML config, not new non-secret environment variables.
- Modify: `website/docs/user-guide/features/code-execution.md` — document generated wrappers, generic catalog calls, persistent-session reset, limits, and artifact paths.
- Test: `tests/tools/test_code_execution.py` — generator, RPC, kernel, artifact, and dispatch behavior.
- Test: `tests/tools/test_code_execution_modes.py` — local/remote mode and cleanup behavior.
- Test: `tests/tools/test_execute_code_approval_cluster.py` — read-only/destructive approval and recursion guards.
- Test: `tests/tools/test_tool_search.py` or the existing tool-search test module — catalog adapter scope and generic calls.
- Test: `tests/run_agent/` or `tests/agent/` focused tool-result tests — multimodal and spill integration only if existing coverage is insufficient.

## Data Contracts

Use explicit, JSON-serializable RPC records. The implementation must normalize all calls to this shape before dispatch:

```python
{
    "request_id": "uuid-or-existing-rpc-sequence",
    "tool_name": "read_file",
    "arguments": {"path": "README.md"},
    "task_id": "parent-task-id-or-null",
    "session_id": "conversation-session-id-or-null",
    "enabled_toolsets": ["coding", "mcp-github"],
    "disabled_toolsets": [],
    "operation_key": "computed-at-dispatch",
}
```

The parent-side helper uses one immutable context object:

```python
@dataclass(frozen=True)
class CodeExecutionContext:
    task_id: str | None
    session_id: str | None
    enabled_toolsets: tuple[str, ...]
    disabled_toolsets: tuple[str, ...]
```

Internal helpers have these exact contracts: `_scriptable_tool_names(session_tools, operation_metadata) -> set[str]`, `_script_operation_decision(tool_name, metadata) -> Literal["allow", "approval_required", "deny"]`, and `_dispatch_script_call(tool_name, arguments, context) -> dict[str, object]`. Tests may use a local `compile_generated_module(source, rpc_stub)` helper, but production code never executes generated source in the parent process.

Generated wrappers must preserve required parameters and defaults where JSON Schema expresses them. Unsupported union/`anyOf`/free-form object shapes use `**kwargs` plus the original sanitized schema in the docstring; generation must never invent an unsafe positional signature. Generic calls accept `name: str` and `arguments: dict[str, object]` and must apply the same scoped catalog check before dispatch.

Persistent sessions use:

```python
execute_code(
    code: str,
    task_id: str | None = None,
    enabled_tools: list[str] | None = None,
    session: str | None = None,
    reset: bool = False,
    timeout: int | None = None,
) -> str
```

`session=None` preserves current fresh-process behavior. `reset=True` terminates the matching kernel before executing no code; the response states whether a kernel existed. A kernel is scoped to the resolved terminal environment and parent task, not globally reusable by another conversation.

## Task 1: Freeze the Code-Mode Contract and Security Boundaries

**Files:**
- Modify: `tools/code_execution_tool.py`
- Modify: `tools/tool_search.py`
- Test: `tests/tools/test_code_execution.py`
- Test: `tests/tools/test_execute_code_approval_cluster.py`

**Interfaces:**
- Consumes: current `ToolRegistry.get_definitions`, `get_operation_metadata`, `tool_search.build_catalog`, `scoped_deferrable_names`, and `handle_function_call`.
- Produces: a documented internal `CodeExecutionContext` mapping and a deterministic `session_tools minus denylist` calculation used by later tasks.

- [ ] Step 1: Add failing tests for the contract before changing implementation.

```python
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


def test_generic_catalog_call_respects_session_scope():
    result = _dispatch_script_call(
        "mcp__other__secret", {}, CodeExecutionContext("task", "session", ("mcp-safe",), ())
    )
    assert result["error"]
```

- [ ] Step 2: Run the focused tests and record the expected failure for the missing helpers.

```bash
python -m pytest tests/tools/test_code_execution.py tests/tools/test_execute_code_approval_cluster.py -q
```

Expected: FAIL only for the new contract tests; existing code-execution tests remain green.

- [ ] Step 3: Implement the smallest internal context helpers.
  - Build the denylist from named constants, not string checks spread across RPC handlers.
  - Use `ToolRegistry.get_operation_metadata` for every scriptable name.
  - Treat missing metadata as `{read_only: False, destructive: True, idempotent: False}`.
  - Keep existing whole-script `check_execute_code_guard` in front of execution.
  - Carry `session_id`, `enabled_toolsets`, `disabled_toolsets`, and the parent `task_id` into every RPC request.

- [ ] Step 4: Re-run the focused tests and verify that a denied name cannot reach `handle_function_call`.

```bash
python -m pytest tests/tools/test_code_execution.py tests/tools/test_execute_code_approval_cluster.py -q
```

Expected: PASS, with destructive calls still routed through the existing approval path rather than auto-allowed.

- [ ] Step 5: Commit the boundary before adding generated wrappers.

```bash
git add tools/code_execution_tool.py tools/tool_search.py tests/tools/test_code_execution.py tests/tools/test_execute_code_approval_cluster.py
git diff --cached --check
git commit -m "feat(code-mode): define full-surface execution boundaries"
```

## Task 2: Generate Typed Active-Tool Wrappers and Generic Catalog Calls

**Files:**
- Modify: `tools/code_execution_tool.py`
- Modify: `tools/registry.py` only if a read-only schema snapshot accessor is needed
- Modify: `tools/tool_search.py`
- Test: `tests/tools/test_code_execution.py`
- Test: existing `tests/tools/test_tool_search.py` if present, otherwise add `tests/tools/test_code_execution_tool_search.py`

**Interfaces:**
- Consumes: Task 1's scriptable-name and context helpers.
- Produces: `generate_hermes_tools_module(tool_defs, context)` with typed wrappers plus `search_tools`, `describe_tool`, `call_tool`, and `save_artifact` helper stubs.

- [ ] Step 1: Add failing tests for schema conversion and token-bounded generation.

```python
def test_generated_wrapper_preserves_required_and_optional_schema_fields():
    schema = {"type": "object", "properties": {
        "path": {"type": "string"}, "offset": {"type": "integer", "default": 1}
    }, "required": ["path"]}
    source = generate_hermes_tools_module({"read_file": {"parameters": schema}}, context=CodeExecutionContext("task", "session", ("coding",), ()))
    assert "def read_file(path: str, offset: int = 1)" in source


def test_exotic_schema_falls_back_to_kwargs_without_dropping_schema():
    schema = {"type": "object", "properties": {"value": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}}
    source = generate_hermes_tools_module({"mcp__s__union": {"parameters": schema}}, context=CodeExecutionContext("task", "session", ("mcp-s",), ()))
    assert "def mcp__s__union(**kwargs)" in source
    assert "anyOf" in source


def test_full_registry_generation_uses_generic_long_tail():
    active_defs = {name: {"parameters": {"type": "object", "properties": {}}} for name in ("read_file", "mcp__s__one")}
    source = generate_hermes_tools_module(active_defs, context=CodeExecutionContext("task", "session", ("coding",), ()))
    assert "def search_tools(" in source
    assert "def describe_tool(" in source
    assert "def call_tool(" in source
    assert len(source) < 100_000
```

- [ ] Step 2: Run the new tests and confirm they fail against the seven string-template stubs.

```bash
python -m pytest tests/tools/test_code_execution.py -k 'wrapper or generic or exotic' -q
```

- [ ] Step 3: Replace `_TOOL_STUBS` generation with registry-driven generation.
  - Sanitize every schema using the existing `tools.schema_sanitizer` path before conversion.
  - Map JSON Schema string/integer/number/boolean/array/object to Python annotations.
  - Emit required parameters first, then optional parameters with schema defaults.
  - Use `**kwargs` for unions, unresolved `$ref`, free-form objects, or invalid names; preserve sanitized schema in a docstring for model-written code.
  - Use safe Python identifiers for MCP names while retaining the exact registered tool name in the RPC request.
  - Generate only non-deferred active tools. The generic functions call the existing catalog adapter for deferred names and never bypass scope checks.

- [ ] Step 4: Add generic function implementations that call the existing RPC helper with the original registered name and arguments.

```python
def call_tool(name: str, arguments: dict[str, object] | None = None):
    return _rpc_call(
        tool_name=name,
        arguments=arguments or {},
        context=_CODE_EXECUTION_CONTEXT,
    )
```

- [ ] Step 5: Re-run generator, schema-drift, and current execution tests.

```bash
python -m pytest tests/tools/test_code_execution.py -q
```

Expected: all existing seven-tool tests pass plus the new registry/generic tests.

- [ ] Step 6: Commit the generated API slice.

```bash
git add tools/code_execution_tool.py tools/tool_search.py tools/registry.py tests/tools/test_code_execution.py tests/tools/test_code_execution_tool_search.py
 git diff --cached --check
git commit -m "feat(code-mode): generate registry-backed tool API"
```

## Task 3: Route Full-Surface Calls Through Existing Middleware and Approvals

**Files:**
- Modify: `tools/code_execution_tool.py`
- Modify: `model_tools.py` only for context forwarding at the existing execute-code dispatch site
- Modify: `hermes_cli/middleware.py` only if context keys are currently filtered
- Test: `tests/tools/test_execute_code_approval_cluster.py`
- Test: `tests/tools/test_code_execution.py`

**Interfaces:**
- Consumes: generated wrappers and Task 1's `CodeExecutionContext`.
- Produces: RPC calls that reach `handle_function_call` with exact tool name, session/toolset scope, metadata, and approval identity.

The test file defines `recording_dispatch(seen)` as a fake `handle_function_call` implementation and `_dispatch_script(code, context)` as the local-RPC driver; both call the production RPC server rather than a copied handler.

- [ ] Step 1: Add a regression test that registers a temporary read-only and destructive tool, invokes both through generated code, and records middleware arguments.

```python
def test_scripted_dispatch_preserves_real_tool_name_and_context(monkeypatch):
    seen = []
    monkeypatch.setattr(model_tools, "handle_function_call", recording_dispatch(seen))
    context = CodeExecutionContext("task", "session", ("fixture",), ())
    result = _dispatch_script("print(read_only_fixture())\nprint(write_fixture(value='x'))", context)
    assert [call.tool_name for call in seen] == ["read_only_fixture", "write_fixture"]
    assert seen[0].operation_metadata["read_only"] is True
    assert seen[1].operation_metadata["destructive"] is True
    assert all(call.session_id == context.session_id for call in seen)
```

- [ ] Step 2: Run the test to prove the current RPC path drops the new context or rejects non-stub names.

```bash
python -m pytest tests/tools/test_code_execution.py -k scripted_dispatch -q
```

- [ ] Step 3: Extend the existing RPC payload and server loop.
  - Forward `session_id`, enabled/disabled toolsets, and the parent `task_id`.
  - Resolve the underlying registered tool through `handle_function_call`, not by invoking a handler directly.
  - Reapply `scoped_deferrable_names`/tool-search scope inside the server, so generated code cannot forge a name outside the session snapshot.
  - Use the existing approval context propagation for RPC worker threads.
  - Batch approvals at the whole-script guard where possible; do not invent a second prompt UI.
  - Enforce `code_execution.tools.include` and `.exclude` only as local narrowing filters. Do not let configuration expand beyond the session's enabled toolsets.

- [ ] Step 4: Add recursion, scope, and approval tests for local and file-RPC paths.

```python
def test_destructive_script_call_uses_existing_approval_identity():
    context = CodeExecutionContext("task", "session", ("fixture",), ())
    result = _dispatch_script("write_fixture(value='x')", context)
    assert result["approval_request"]["tool_name"] == "write_fixture"
    assert result["approval_request"]["requester"] == context.session_id


def test_remote_rpc_rejects_tool_outside_enabled_toolsets():
    context = CodeExecutionContext("task", "session", ("mcp-safe",), ())
    result = _execute_remote("call_tool('mcp__disabled__write', {})", context.task_id, ["mcp__disabled__write"])
    assert "not enabled" in result["error"]
```

- [ ] Step 5: Run the focused approval and mode tests.

```bash
python -m pytest tests/tools/test_code_execution.py tests/tools/test_code_execution_modes.py tests/tools/test_execute_code_approval_cluster.py -q
```

- [ ] Step 6: Commit the dispatch slice.

```bash
git add tools/code_execution_tool.py model_tools.py hermes_cli/middleware.py tests/tools/test_code_execution.py tests/tools/test_code_execution_modes.py tests/tools/test_execute_code_approval_cluster.py
git diff --cached --check
git commit -m "feat(code-mode): dispatch registry tools through policy rails"
```

## Task 4: Add the Opt-In Persistent Kernel

**Files:**
- Modify: `tools/code_execution_tool.py`
- Modify: `tools/terminal_tool.py` only to reuse task/environment cleanup if needed
- Modify: `hermes_cli/config.py` and `cli-config.yaml.example`
- Test: `tests/tools/test_code_execution.py`
- Test: `tests/tools/test_code_execution_modes.py`

**Interfaces:**
- Consumes: generated module and full dispatch contract from Tasks 1–3.
- Produces: kernel registry keyed by `(resolved_task_id, session)` with `execute`, `reset`, idle cleanup, timeout escalation, and environment-teardown cleanup.

- [ ] Step 1: Add failing lifecycle tests.

```python
def test_persistent_kernel_reuses_globals_between_calls():
    first = json.loads(execute_code("rows = [1, 2, 3]", task_id="task", enabled_tools=[]))
    second = json.loads(execute_code("print(sum(rows))", task_id="task", enabled_tools=[]))
    assert first["status"] == "completed"
    assert "6" in second["stdout"]


def test_reset_terminates_kernel_and_removes_state():
    execute_code("secret = 'not persisted after reset'", task_id="task", enabled_tools=[])
    result = json.loads(execute_code("print('fresh')", task_id="task", enabled_tools=[]))
    assert result["kernel_reset"] is True
    assert "secret" not in result["stdout"]


def test_kernel_timeout_kills_execution_but_keeps_kernel_until_reset():
    execute_code("x = 7", task_id="task", enabled_tools=[])
    result = json.loads(execute_code("while True: pass", task_id="task", enabled_tools=[]))
    assert result["timed_out"] is True
    assert "7" in json.loads(execute_code("print(x)", task_id="task", enabled_tools=[]))["stdout"]
```

- [ ] Step 2: Run lifecycle tests and verify the current fresh-process implementation fails persistence assertions.

```bash
python -m pytest tests/tools/test_code_execution.py -k 'persistent or reset or kernel' -q
```

- [ ] Step 3: Implement a minimal kernel protocol over the existing transport.
  - Keep the child process as a small REPL server with one shared globals dict.
  - Use a separate framed request/response channel inside the existing UDS/file-RPC temp directory; do not execute arbitrary code in the parent process.
  - Store registry entries with process handle, environment identity, last activity, and a lock that serializes one `exec` at a time per kernel.
  - Reuse `_kill_process_group` for an in-flight timeout. Escalate from cooperative interrupt to process-group termination, preserving the kernel only when the process remains healthy.
  - `reset=True` kills and removes the entry before returning a reset result.
  - Add idle cleanup using the same task/environment resolution as terminal environments. Cleanup runs on environment teardown and never crosses task/profile boundaries.
  - Remote kernels live inside the already-persistent terminal environment under its temp directory; the parent only polls the existing file-RPC channel.
  - Sanitize the kernel environment once at creation and never append secrets from later parent turns.

- [ ] Step 4: Add cross-task and environment-teardown tests.

```python
def test_same_session_name_in_two_tasks_does_not_share_globals():
    execute_code("value = 'a'", task_id="task-a", enabled_tools=[])
    execute_code("value = 'b'", task_id="task-b", enabled_tools=[])
    assert json.loads(execute_code("print(value)", task_id="task-a", enabled_tools=[]))["stdout"].strip() == "a"
    assert json.loads(execute_code("print(value)", task_id="task-b", enabled_tools=[]))["stdout"].strip() == "b"


def test_environment_cleanup_reaps_all_kernels_for_task():
    execute_code("value = 1", task_id="task-a", enabled_tools=[])
    reap_kernels_for_task("task-a")
    assert not code_execution_tool._kernel_registry_for_task("task-a")
```

`reap_kernels_for_task(task_id: str) -> None` is the explicit cleanup hook called by terminal-environment teardown; it is not a second environment registry.

- [ ] Step 5: Run local and remote mode tests.

```bash
python -m pytest tests/tools/test_code_execution.py tests/tools/test_code_execution_modes.py -q
```

- [ ] Step 6: Commit the opt-in kernel.

```bash
git add tools/code_execution_tool.py tools/terminal_tool.py hermes_cli/config.py cli-config.yaml.example tests/tools/test_code_execution.py tests/tools/test_code_execution_modes.py
git diff --cached --check
git commit -m "feat(code-mode): add opt-in persistent execution kernels"
```

## Task 5: Return Structured Artifacts Without Breaking Tool Results

**Files:**
- Modify: `tools/code_execution_tool.py`
- Modify: `tools/tool_result_storage.py` only if registration needs a small adapter
- Modify: `agent/tool_executor.py` only if an integration test exposes a missing envelope path
- Test: `tests/tools/test_code_execution.py`
- Test: focused existing multimodal/tool-result tests as needed

**Interfaces:**
- Consumes: child environment and kernel lifecycle.
- Produces: `save_artifact(path_or_bytes, name)` in generated code; images in `_multimodal` content; non-images as a readable spilled-file reference.

- [ ] Step 1: Add failing artifact tests.

```python

def test_png_artifact_returns_multimodal_content(tmp_path):
    result = json.loads(execute_code(
        "import base64\nfrom pathlib import Path\nPath('plot.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='))",
        task_id="task", enabled_tools=[]
    ))
    assert result["content"][0]["type"] == "image"
    assert result["artifact"]["name"] == "plot.png"


def test_non_image_artifact_returns_readable_spill_reference(tmp_path):
    result = json.loads(execute_code("save_artifact(b'abc', 'report.csv')", task_id="task", enabled_tools=[]))
    assert result["artifact"]["path"]
    assert Path(result["artifact"]["path"]).read_text() == "abc"


def test_artifact_limits_reject_oversized_or_unsafe_paths():
    result = json.loads(execute_code("save_artifact('/etc/passwd', 'x')", task_id="task", enabled_tools=[]))
    assert "artifact" in result["error"]
```

- [ ] Step 2: Run artifact tests and confirm there is no current artifact helper/return envelope.

```bash
python -m pytest tests/tools/test_code_execution.py -k artifact -q
```

- [ ] Step 3: Implement artifact collection with existing safety primitives.
  - Create an artifact directory beneath the per-call local temp directory or remote sandbox directory.
  - Expose only `save_artifact` and reject paths escaping that directory; accept bytes or a path already inside the directory.
  - Enforce per-artifact and aggregate size caps from `code_execution` config.
  - For PNG/JPEG/WebP, use the existing image-size/downscale utility and return the established `_multimodal` envelope. Do not invent a new content block format.
  - For all other files, use `tool_result_storage` or a narrow equivalent to persist a preview plus path. The model can use `read_file` on that path through the same session environment.
  - Redact artifact preview text and filenames with existing redaction rules; never include raw secrets in JSON metadata.
  - Collect artifacts after code completion and before sandbox cleanup. For persistent kernels, copy artifacts out before the kernel directory is reused.

- [ ] Step 4: Run existing tool-result integration tests plus the focused code-mode suite.

```bash
python -m pytest tests/tools/test_code_execution.py tests/agent/test_tool_result_storage.py -q
```

If `tests/agent/test_tool_result_storage.py` is absent, run the repository's existing `tool_result_storage` test module discovered by `search_files` and record that path in the implementation PR.

- [ ] Step 5: Commit the artifact channel.

```bash
git add tools/code_execution_tool.py tools/tool_result_storage.py agent/tool_executor.py tests/tools/test_code_execution.py
git diff --cached --check
git commit -m "feat(code-mode): return structured execution artifacts"
```

## Task 6: Configuration, Documentation, and End-to-End Gates

**Files:**
- Modify: `hermes_cli/config.py`
- Modify: `cli-config.yaml.example`
- Modify: `website/docs/user-guide/features/code-execution.md`
- Test: `tests/tools/test_code_execution.py`
- Test: `tests/tools/test_code_execution_modes.py`
- Test: `tests/tools/test_execute_code_approval_cluster.py`

- [ ] Step 1: Add configuration tests with a temporary home.

```python
def test_code_execution_config_defaults_are_backward_compatible(tmp_path, monkeypatch):
    config = load_config(tmp_path / "config.yaml")
    assert config["code_execution"]["sessions"]["enabled"] is False
    assert config["code_execution"]["tools"]["include"] == []
    assert config["code_execution"]["tools"]["exclude"] == []
```

- [ ] Step 2: Document the exact configuration and security model.

The example must show only YAML settings:

```yaml
code_execution:
  sessions:
    enabled: false
    idle_timeout_seconds: 900
  tools:
    include: []
    exclude: []
  artifacts:
    max_bytes: 10485760
    max_total_bytes: 52428800
```

The user guide must state that `include` only narrows the enabled session toolset, unknown/MCP tools are conservative by default, persistent sessions retain variables until reset/idle cleanup, and artifacts are size-capped and subject to the same approval and redaction rails.

- [ ] Step 3: Run the complete focused gate from a clean temporary `HERMES_HOME`.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/tools/test_code_execution.py \
  tests/tools/test_code_execution_modes.py \
  tests/tools/test_execute_code_approval_cluster.py -q
```

Expected: PASS with no writes outside the temporary home and no generated kernel/artifact directories left in the repository.

- [ ] Step 4: Run static checks and inspect the staged diff.

```bash
python -m compileall -q tools/code_execution_tool.py tools/tool_search.py
python -m pytest tests/tools/test_code_execution.py -q
 git diff --check
```

- [ ] Step 5: Commit documentation and configuration.

```bash
git add hermes_cli/config.py cli-config.yaml.example website/docs/user-guide/features/code-execution.md tests/tools/test_code_execution.py tests/tools/test_code_execution_modes.py tests/tools/test_execute_code_approval_cluster.py
git diff --cached --check
git commit -m "docs(code-mode): document registry sessions and artifacts"
```

## Acceptance Checklist

- [ ] Generated wrappers are derived from current `ToolRegistry` schemas and pass schema-drift tests.
- [ ] Generic catalog calls reuse `tool_search` scoring and scope enforcement.
- [ ] All scripted calls reach `handle_function_call` with the real registered name and session context.
- [ ] Recursion/interactivity are denied; unknown operations are destructive by default.
- [ ] Destructive calls use existing approval identity and gateway/CLI approval surfaces.
- [ ] Fresh-process behavior remains the default; persistent kernels are opt-in, resettable, idle-reaped, and environment-scoped.
- [ ] Timeout kills the execution without leaving an orphan kernel/process group.
- [ ] Image artifacts use `_multimodal`; other artifacts use a readable spill path; limits and path confinement are tested.
- [ ] Local and remote RPC modes pass focused tests under a temporary `HERMES_HOME`.
- [ ] No schema bloat, new core tool, new dependency, or non-secret environment configuration was introduced.

## Deliberate Simplifications

- Skipped a full generated class hierarchy: functions plus a generic catalog API cover the use case with less schema and maintenance.
- Skipped a new sandbox/security subsystem: the existing execution guard, middleware, approvals, and later policy engine remain authoritative.
- Skipped cross-session kernel sharing and automatic persistence: add only if measured workflows need it and a durable secret/lifecycle model exists.
- Skipped TLS-terminating artifact transport: existing local/file RPC and spill storage are sufficient for v1.
