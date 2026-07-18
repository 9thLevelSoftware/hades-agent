---
sidebar_position: 8
title: "Code Execution"
description: "Programmatic Python execution with RPC tool access — collapse multi-step workflows into a single turn"
---

# Code Execution (Programmatic Tool Calling)

The `execute_code` tool runs a Python script in a child process and gives that
script RPC access to the Hermes tools in the current session. Use it to collapse
loops, filtering, and multi-step workflows into one model turn: intermediate
results stay in the parent process and only the script's final output returns to
the model.

## How It Works

1. The model supplies Python source to `execute_code`.
2. Hermes generates a `hermes_tools.py` module for the current tool scope.
3. Hermes starts a private RPC listener and a child Python process.
4. Calls made by the script cross the RPC channel and are dispatched by the
   normal Hermes tool path.
5. Hermes captures stdout/stderr, applies redaction and configured limits, and
   returns a JSON result.

```python
from hermes_tools import web_search, web_extract

results = web_search("Python 3.13 features", limit=5)
summary = []
for item in results.get("data", {}).get("web", []):
    page = web_extract([item["url"]])
    summary.append({"title": item["title"], "content": page})
print(summary)
```

## Generated APIs: typed wrappers and generic catalog helpers

Wrappers are generated from the active `ToolRegistry` schemas, not from a
second hand-written API list. JSON-schema fields that can be represented safely
become typed Python parameters (`str`, `int`, `float`, `bool`, `list`, or
`dict`), with required fields first and schema defaults preserved.

```python
from hermes_tools import read_file, search_files

page: dict = read_file("README.md", offset=1, limit=100)
matches: dict = search_files("deprecated", path="src/", file_glob="*.py")
print({"lines": page.get("total_lines"), "matches": len(matches.get("matches", []))})
```

A tool name that is not a valid Python identifier is sanitized for the wrapper
(and collisions receive a deterministic suffix). Schemas containing unions,
references, unsupported types, or malformed object definitions use a safe
`**kwargs` wrapper instead; the wrapper includes a bounded sanitized-schema
summary rather than guessing a signature.

When the active registry surface is available, `hermes_tools` also provides
these generic helpers:

```python
from hermes_tools import search_tools, describe_tool, call_tool, save_artifact
from pathlib import Path
import os

hits = search_tools("issue tracker read-only lookup", limit=5)
metadata = describe_tool(hits["results"][0]["name"])
result = call_tool(metadata["name"], {"limit": 10})
source = Path(os.environ["HERMES_ARTIFACTS_DIR"]) / "report.json"
source.write_text('{"result": "generated"}', encoding="utf-8")
artifact = save_artifact(str(source), name="report.json", mime_type="application/json")
print({"result": result, "artifact": artifact})
```

`json_parse`, `shell_quote`, and `retry` are also embedded convenience helpers.
`save_artifact` accepts bytes directly or copies a path that is inside the
execution's `HERMES_ARTIFACTS_DIR` into the configured durable artifact
directory; it is not a replacement for `write_file` or `terminal`.

## Tool scope, denylist, and approval behavior

The generated module is limited to the model-facing tool scope for the
session. `enabled_toolsets` and `disabled_toolsets` are carried over the RPC
boundary; when a scope is supplied, Hermes checks the requested registered tool
against that scope before dispatch. A tool that is not registered or not in the
current scope returns an error rather than being discovered by guessing.

The script denylist is always applied, even if a caller tries to request these
names directly:

- **Recursion/catalog control:** `execute_code`, `delegate_task`,
  `tool_search`, `tool_describe`, and `tool_call`.
- **Interactive or memory state:** `clarify` and `memory`.
- **Lifecycle/control-plane operations:** process and terminal lifecycle tools,
  todo/cron tools, `kanban_*`, `workflow_*`, `project_*`, `browser_cdp`,
  `browser_dialog`, and `computer_use`.

The generic catalog helpers use an internal bridge action and still reach the
same parent dispatcher; they do not grant a second execution path. Read-only,
non-destructive registered operations can run without an extra approval. Other
registered operations—including unknown or conservatively classified ones—use
the normal Hermes middleware and approval path. Code execution itself is also
checked by the existing `execute_code` approval guard before a child is spawned.

## When the agent uses this

`execute_code` is useful when there are:

- three or more tool calls with processing between them;
- loops over search or extraction results;
- bulk filtering, aggregation, or conditional branching; or
- large intermediate results that should be reduced before entering context.

Use normal tool calls when one call is enough, when you need to reason over the
full raw result, or when the task needs interactive user input.

## Execution mode

`code_execution.mode` controls the child process working directory and Python
interpreter:

| Mode | Working directory | Python interpreter |
|------|-------------------|--------------------|
| **`project`** (default) | The session's working directory, matching `terminal()` | Active `VIRTUAL_ENV`/`CONDA_PREFIX` Python, falling back to Hermes's Python |
| **`strict`** | A temporary staging directory | Hermes's `sys.executable` |

`strict` changes staging and interpreter selection; it is not a new permission
boundary. Both modes retain the same environment scrubbing, tool scope, denylist,
approval, timeout, and output limits.

```yaml
# ~/.hermes/config.yaml
code_execution:
  mode: project   # project (default) or strict
```

In `project` mode, a missing, unusable, or too-old active environment falls back
to `sys.executable`. In `strict` mode, relative paths resolve inside the
staging directory and project-only dependencies are not expected to be present.

## Resource, session, and artifact limits

The canonical nested settings are:

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

`tools.include` and `tools.exclude` only narrow the model/session tool scope;
they cannot expand it. Unknown and MCP tools are conservative by default and
must be present in the active session snapshot. Persistent sessions retain
Python variables until reset or idle cleanup, and artifacts are size-capped and
subject to the same approval, path-confinement, and redaction rails as normal
execution results.

The existing top-level `persistent`, `kernel_idle_ttl`, and `artifact_dir` keys
remain accepted as backward-compatible aliases. When both forms are present,
the explicit top-level alias wins for that setting. Other resource defaults are:

| Setting | Default | Effect |
|---------|---------|--------|
| `timeout` | `300` seconds | Kills a script after five minutes |
| `max_tool_calls` | `50` | Caps RPC calls in one execution |
| `max_stdout_bytes` | `50000` | Caps returned stdout; oversized redacted text is spilled |
| `max_stderr_bytes` | `10000` | Caps returned stderr |
| `artifacts.max_bytes` | `10485760` | Caps one persisted artifact |
| `artifacts.max_total_bytes` | `52428800` | Caps all artifacts in one execution |
| `artifact_dir` | `/tmp/hermes-results` | Durable directory for redacted spills and copied artifacts |

All settings are under `code_execution` in `config.yaml`. For example:

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
  timeout: 300
  max_tool_calls: 50
  max_stdout_bytes: 50000
  max_stderr_bytes: 10000
  artifact_dir: /tmp/hermes-results
```

The configured artifact directory is created with owner-only permissions when it
is first used. For oversized stdout, Hermes strips ANSI/control data and
redacts sensitive text **before** writing the durable spill file. The response
keeps a bounded head/tail preview and adds:

```json
{
  "truncated": true,
  "artifact_path": "/tmp/hermes-results/execute_code_....txt"
}
```

The local fresh-process child receives `HERMES_ARTIFACTS_DIR` for generated
files. Those files are collected before local staging cleanup when they are
recognized as image artifacts or oversized text. `save_artifact(path,
name=None, mime_type=None)` copies a child file to `artifact_dir` and returns
its durable path. Remote execution uses the remote sandbox for code and RPC.
Recognized remote image artifacts and oversized remote text files are collected
before sandbox cleanup, transferred as bounded content, and persisted under the
parent `artifact_dir`; remote-only source paths are never returned to the model.

## Structured image results

Image values printed directly, returned as JSON, or written by the child into
its artifact directory are normalized to the OpenAI-compatible shape:

```json
{
  "_multimodal": true,
  "content": [
    {"type": "image_url", "image_url": {"url": "file:///.../plot.png"}}
  ],
  "text_summary": "..."
}
```

Supported image inputs include data URLs, HTTP(S) image URLs with a recognized
image suffix, local image paths, `file://` paths, and structured
`{"type":"image_url", "image_url":{"url":"..."}}` parts. Ordinary text
results keep their existing `status`, `output`, `tool_calls_made`,
`duration_seconds`, and error metadata; the multimodal fields are additive.

## Persistent kernels (explicit opt-in)

Fresh-process execution remains the default. A caller can explicitly request a
persistent task-scoped interpreter:

```python
from hermes_tools import read_file

# The tool API passes persistent=True when the execute_code call requests it.
state = {"rows": [1, 2, 3]}
print(sum(state["rows"]))
```

At the tool API boundary, use `persistent: true` with an optional `kernel_id`
(or the compatibility alias `session`). The same task ID and kernel ID reuse
one child interpreter, so Python globals, imported modules, and state survive
between calls. An omitted `persistent` argument uses
`code_execution.sessions.enabled` from YAML; its default is `false`. The
legacy `code_execution.persistent` key remains an alias and wins when present.
An explicit `persistent: false` always keeps fresh-process behavior, while an
explicit `persistent: true` opts in regardless of the configured default.

```yaml
code_execution:
  sessions:
    enabled: false
    idle_timeout_seconds: 900
```

Lifecycle controls:

- `reset: true` terminates the selected kernel before the next code block;
  passing empty code with reset clears state without running a script.
- A per-call `timeout` kills a stuck kernel. The next persistent call starts a
  clean interpreter and reports the timeout in structured output.
- `kernel_idle_ttl` reaps kernels that have not been used within the configured
  number of seconds. The default is 900 seconds.
- Task/environment cleanup and process exit close remaining kernels, their RPC
  sockets, child process groups, and temporary directories.
- Kernels are local-backend only; remote terminal backends return a structured
  error instead of silently pretending to persist state.

Persistent mode does not broaden tool scope or bypass the denylist, approval,
redaction, or output limits. Do not use it for secrets that should outlive one
short script; state is process memory and is intentionally not durable storage.

## How tool calls work inside scripts

When a script calls a generated wrapper:

1. Arguments are serialized to JSON and sent over the private RPC channel.
2. The parent applies scope checks, denylist rules, middleware, and approval.
3. The normal registered handler runs with the parent task/session context.
4. The result is serialized back and decoded by the wrapper.

The `terminal()` wrapper is foreground-only: it does not accept `background` or
`pty` parameters. Use the normal `terminal` tool for interactive or background
processes.

## Error handling

The JSON result always includes `status`, `output`, `tool_calls_made`, and
`duration_seconds` for an execution that reaches the child process. Depending
on the path it can also include `error`, `stderr`, `truncated`, `artifact_path`,
`persistent`, `kernel_id`, `kernel_reset`, or multimodal fields.

- **Non-zero exit:** redacted stderr is included so the model can see the
  traceback.
- **Timeout:** the process or kernel is killed and the result says it timed out.
- **Interruption:** a user message terminates the child and the result includes
  an interruption marker.
- **Tool-call limit:** calls after the configured maximum receive an error from
  the RPC server.
- **Approval denial or scope denial:** the child receives an ordinary structured
  tool error; the parent does not spawn a second unrestricted path.

## Security and environment

The child receives a minimal environment. API keys, tokens, credentials, and
other secret-like variables are stripped by default; the child accesses Hermes
capabilities through RPC instead of direct provider credentials.

Environment names containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`,
`PASSWD`, or `AUTH` are excluded. Safe system variables and the operational
`HERMES_HOME`, `HERMES_PROFILE`, `HERMES_CONFIG`, and `HERMES_ENV` names are
allowed as required for runtime location. `HERMES_RPC_*`, `TZ`, and `HOME` are
injected explicitly for the RPC process.

Skill-declared `required_environment_variables` and exact names listed in
`terminal.env_passthrough` can be passed through for approved skill use cases.
This does not re-enable Hermes-managed provider credentials automatically.

## `execute_code` vs `terminal`

| Use case | `execute_code` | `terminal` |
|----------|----------------|------------|
| Multi-step workflows with tool calls between | ✅ | ❌ |
| Simple shell command | ❌ | ✅ |
| Filtering or aggregating tool output | ✅ | ❌ |
| Build or test suite | ❌ | ✅ |
| Looping over search results | ✅ | ❌ |
| Interactive/background processes | ❌ | ✅ |
| Direct API keys in child environment | Only via explicit passthrough | Backend-dependent; still follow terminal policy |

**Rule of thumb:** use `execute_code` for programmatic Hermes-tool workflows;
use `terminal` for shell commands, builds, and process management.

## Platform support

Local execution uses Unix domain sockets on macOS/Linux and a loopback TCP
fallback on Windows. Remote execution uses the terminal backend's file-based
RPC and requires Python 3 in that environment. Availability and backend
requirements are checked before the script is spawned.
