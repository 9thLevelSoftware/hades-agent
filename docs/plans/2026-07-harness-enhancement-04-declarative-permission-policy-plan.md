# Declarative Permission-Policy Engine Implementation Plan

> For agentic workers: implement the policy engine as the single decision layer before tool execution. Preserve hardline terminal blocks and the existing approval UI; do not replace them with a new permission prompt or silently make the middleware fail-open.

**Goal:** Consume the fork's existing `read_only`/`destructive`/`idempotent` metadata and `operation_key` at the tool-execution chokepoint, providing layered allow/ask/deny rules, structural plan mode, scoped grants, and a reviewable permissions surface for builtin, plugin, MCP, cron, and delegated tools.

**Architecture:** Create `tools/policy.py` with a compiled `PolicyEngine`. It evaluates a normalized operation against rule layers and returns a decision plus provenance. Register one built-in policy middleware at the existing `run_tool_execution_middleware` chain. The engine calls the existing `_run_approval_gate`/`request_tool_approval` only for `ask`; hardline patterns and sensitive-path checks remain independent floors. Project rules can narrow by default; project allows require an explicit global trust grant. All modes and grants are session-scoped through the existing approval session key and durable grant store.

**Tech Stack:** Python/YAML config, `ToolRegistry` metadata, existing approval normalization/dangerous-command variants, plugin middleware context, file safety, MCP schema/annotation conversion, atomic JSON writes, CLI slash commands, gateway RPC/TUI approval surfaces, and current `HERMES_HOME` profile stores.

## Global Constraints

- Decision precedence is fixed: hardline floor → user deny → policy deny → mode gate → policy allow → policy ask → mode default.
- Hardline terminal patterns, sensitive-path blocks, and trust-boundary denials cannot be relaxed by policy, YOLO, or a project file.
- Missing or contradictory metadata is conservative: `read_only=False`, `destructive=True`, `idempotent=False`.
- MCP `readOnlyHint`/`destructiveHint`/`idempotentHint` only relax conservative defaults after the server is explicitly trusted and the tool annotation passes schema validation. A remote server cannot self-promote into plan-mode allow.
- Project `.hermes/permissions.yaml` may deny/ask unconditionally but may allow only after a user-approved trust record scoped to the project root and file digest.
- Rule matchers are limited to tool-name glob, terminal command prefix/glob, path glob through a small extractor table, and metadata selectors. Do not build a general policy language or embedded expression interpreter.
- `yolo` is an existing convenience mode, not a security boundary; it remains below hardline/sensitive-path floors and must be visibly reported.
- Policy evaluation is cached by global config mtime, project-rule mtime/digest, trust/grant store mtime, and session mode. No stat storm per tool call.
- Every decision includes `policy_key`, rule provenance, operation key, and mode in the approval/audit context, but never raw secrets or unredacted arguments.
- Policy errors fail closed (`deny` for a mutating operation, `ask` for a read-only operation only when a human gate is available) and are logged. They never fall through to the current fail-open plugin chain.
- Changes to metadata flags must be reviewed with focused high-risk tests. Do not mass-mark tools read-only based on names alone.
- Test with real temporary config/project files and real middleware dispatch. MagicMock-only policy tests are insufficient.

## Current-State Review

The review found substantial substrate and no consumer:

- `tools/registry.py` has `ToolEntry` metadata, `get_operation_metadata()` with conservative fallback, and `operation_key()`; all three execution paths already pass metadata and key factories into middleware.
- `hermes_cli/middleware.py` has `TOOL_EXECUTION_MIDDLEWARE` and `run_tool_execution_middleware`, but plugin execution errors are swallowed and the chain proceeds. A built-in policy callback must own its own fail-closed behavior.
- `tools/approval.py` already has `_run_approval_gate`, `request_tool_approval`, session grants, deny globs, command normalization/deobfuscation, and persisted fallback approvals. The policy engine should call those functions rather than duplicate gateway wait loops.
- `tools/file_tools.py` and `tools/write_approval.py` have specialized sensitive-path/staged-write floors that remain in force.
- `tools/mcp_tool.py` registers remote tools but does not map MCP annotations into `ToolEntry` metadata. The mapping must default conservative and require trust to relax.
- Config has `approvals`, `command_allowlist`, and mode handling but no `permissions` block or grant ledger. CLI/gateway boot sites are known and can register one policy engine per profile.
- Existing `acp_adapter/permissions.py` is an approval bridge, not a policy source; use its callback for `ask` rather than create an ACP-specific decision engine.

The plan skips a regex rewrite, a new approval UI, a new sandbox, and policy decisions in every individual tool.

## Release Order and Dependencies

1. Rule model, precedence, matching, and fail-closed evaluation.
2. Middleware wiring and mode semantics.
3. Layered config, trust/grant persistence, and permissions surfaces.
4. Metadata/annotation sweep and delegated/cron inheritance.
5. Regression, fuzz/property, and user-facing verification.

## File Map

- Create: `tools/policy.py` — rule dataclasses, matchers, compiler/cache, `PolicyEngine.evaluate`, decision/provenance types.
- Create: `tools/permission_grants.py` — atomic scoped grant/trust store and digest validation.
- Modify: `hermes_cli/config.py` — `permissions` defaults, YAML normalization, legacy approvals migration.
- Modify: `hermes_cli/middleware.py` — built-in policy middleware registration/context and explicit fail-closed path.
- Modify: `tools/approval.py` — expose shared normalized command matcher/policy identity and consume scoped grants without duplicating prompts.
- Modify: `tools/registry.py` — metadata validation/diagnostic helpers only; keep operation key semantics.
- Modify: `tools/mcp_tool.py` — trusted annotation mapping into registration metadata.
- Modify: `agent/tool_executor.py`, `model_tools.py`, `agent/agent_runtime_helpers.py` — verify/inject policy context on all paths; no independent decisions.
- Modify: `tools/file_tools.py`, `tools/environments/local.py`, `tools/terminal_tool.py` — add project policy file and grant ledger to sensitive-write/env scrub floors.
- Modify: `tools/delegate_tool.py`, `tools/async_delegation.py`, `cron/scheduler.py` — child/job mode and policy snapshot inheritance.
- Modify: `cli.py`, `gateway/run.py`, `gateway/slash_commands.py`, `tui_gateway/server.py` — permissions list/test/grant/revoke/trust actions.
- Modify: `ui-tui/src/` only if the existing approval/overlay surface cannot render provenance and scoped grant actions.
- Test: new `tests/tools/test_policy.py`, `tests/tools/test_permission_grants.py`.
- Test: extend `tests/tools/test_approval.py`, `tests/tools/test_approval_deny_rules.py`, `tests/tools/test_mcp_tool.py`, `tests/tools/test_mcp_security.py`, `tests/tools/test_execute_code_approval_cluster.py`.
- Test: new `tests/gateway/test_permissions_rpc.py`, `tests/cli/test_cli_permissions.py`, and delegation/cron policy tests.

## Data Contracts

```python
@dataclass(frozen=True)
class PolicyRule:
    matcher: str
    decision: Literal["allow", "ask", "deny"]
    argument: str | None
    metadata: frozenset[str]
    source: Literal["default", "global", "project", "grant", "legacy"]
    rule_id: str
```

```python
@dataclass(frozen=True)
class OperationContext:
    tool_name: str
    arguments: dict[str, object]
    operation_metadata: dict[str, bool]
    operation_key: str
    mode: Literal["default", "plan", "acceptEdits", "yolo"]
    cwd: str
    backend: str
    session_key: str
    project_root: str | None
    project_rule_digest: str | None
    server_trust: str | None
```

```python
@dataclass(frozen=True)
class PolicyDecision:
    action: Literal["allow", "ask", "deny"]
    policy_key: str
    reason: str
    provenance: tuple[str, ...]
    requires_approval: bool
```

Rule examples, normalized from YAML:

```yaml
permissions:
  mode: default
  rules:
    - match: "terminal"
      command: "git *"
      decision: allow
    - match: "write_file"
      path: "**/.env*"
      decision: deny
    - match: "mcp_*"
      metadata: "@destructive"
      decision: ask
```

Global rules are stored in config. Project rules use the same shape under `<project>/.hermes/permissions.yaml`. Scoped grants use `~/.hermes/permission_grants.json` with `{rule, scope, cwd, backend, session_key, expires_at, project_digest, granted_by, created_at}`. Raw command/path arguments are never persisted; only normalized matcher and operation key/policy identity are stored.

## Task 1: Implement Rule Matching and Precedence

**Files:**
- Create: `tools/policy.py`
- Modify: `tools/approval.py` to expose normalized command variants
- Test: `tests/tools/test_policy.py`
- Test: `tests/tools/test_approval_deny_rules.py`

**Interfaces:**
- Consumes: `OperationContext`, existing command/path normalization, registry metadata.
- Produces: `PolicyEngine.evaluate(context) -> PolicyDecision` and deterministic `policy_key(context) -> str`.

- [ ] Step 1: Add pure tests for matchers and precedence.

```python
def test_hardline_floor_beats_global_allow():
    engine = PolicyEngine(global_rules=(PolicyRule("terminal", "allow", "rm -rf /", frozenset(), "global", "r1"),))
    decision = engine.evaluate(operation("terminal", command="rm -rf /"))
    assert decision.action == "deny"
    assert "hardline" in decision.reason


def test_user_deny_beats_project_allow():
    engine = PolicyEngine(global_rules=(deny("terminal", "r1"),), project_rules=(allow("terminal", "r2"),))
    assert engine.evaluate(operation("terminal", command="git status")).action == "deny"


def test_plan_mode_blocks_unknown_and_all_mutations():
    engine = PolicyEngine()
    assert engine.evaluate(operation("read_file", metadata={"read_only": True}, mode="plan")).action == "allow"
    assert engine.evaluate(operation("mcp__server__unknown", metadata={}, mode="plan")).action == "deny"


def test_policy_error_is_fail_closed_for_mutation(monkeypatch):
    engine = PolicyEngine(global_rules=(invalid_rule(),))
    decision = engine.evaluate(operation("write_file", metadata={"destructive": True}))
    assert decision.action == "deny"
```

- [ ] Step 2: Run the tests and confirm the policy module is missing.

```bash
python -m pytest tests/tools/test_policy.py -q
```

- [ ] Step 3: Implement exact matchers:
  - `fnmatch` tool names after MCP prefix sanitization;
  - terminal command prefix/glob over the same normalized/deobfuscated variants used by approval.py;
  - path glob through a fixed extractor map for `write_file`, `patch`, `read_file`, `search_files`, and `terminal` cwd;
  - metadata selectors `@destructive`, `@read_only`, `@idempotent`, and negated forms.

- [ ] Step 4: Implement precedence exactly as documented. Return provenance strings and a stable policy key composed from decision inputs, not raw argument values.

- [ ] Step 5: Add YAML parser normalization for `off/no/yes/on` strings and reject unknown decisions/modes with a config error that points to the rule id.

- [ ] Step 6: Run pure policy/approval tests plus a property test generating command quoting/spacing/comment variants. The property assertion is that a hardline command never becomes `allow`.

```bash
python -m pytest tests/tools/test_policy.py tests/tools/test_approval_deny_rules.py -q
```

- [ ] Step 7: Commit the policy core.

```bash
git add tools/policy.py tools/approval.py tests/tools/test_policy.py tests/tools/test_approval_deny_rules.py
git diff --cached --check
git commit -m "feat(permissions): add declarative policy evaluator"
```

## Task 2: Wire Policy Into the Tool-Execution Middleware

**Files:**
- Modify: `hermes_cli/middleware.py`
- Modify: `tools/approval.py`
- Modify: `model_tools.py`
- Modify: `agent/tool_executor.py`
- Modify: `agent/agent_runtime_helpers.py`
- Test: `tests/tools/test_policy.py`
- Test: `tests/tools/test_approval.py`

- [ ] Step 1: Add a real dispatch test registering read-only, destructive, and unknown fixtures. Capture decisions at the middleware layer and assert no handler runs for deny.

```python
def test_policy_middleware_blocks_mutation_in_plan_mode(temp_profile):
    register_fixture_tools()
    with policy_session(mode="plan"):
        denied = handle_function_call("write_fixture", {"value": "x"})
        allowed = handle_function_call("read_fixture", {})
    assert denied["blocked"] is True
    assert allowed["value"] == "fixture"
```

- [ ] Step 2: Run the test against the current no-consumer path.

```bash
python -m pytest tests/tools/test_policy.py -k middleware -q
```

- [ ] Step 3: Register one built-in policy middleware for each profile at the existing CLI/gateway boot sites. The callback:
  - creates `OperationContext` from existing middleware metadata/key fields;
  - evaluates policy before any plugin `approve` directive can auto-allow;
  - returns `block` for deny;
  - calls existing `request_tool_approval`/`_run_approval_gate` for ask with `policy_key`, reason, provenance, and scoped-grant options;
  - passes allow through without changing handler arguments;
  - catches internal evaluation exceptions and returns a deny decision for mutations.

- [ ] Step 4: Fix the fail-open edge without changing plugin behavior globally. Mark the built-in callback as a protected middleware whose exception is converted to a decision. Existing third-party callback exceptions may retain current observation semantics, but they cannot override a built-in deny.

- [ ] Step 5: Deduplicate policy ask with existing regex approval by sharing normalized `policy_key`/`rule_key` in the pending approval identity. One terminal command may still show hardline/approval reason text, but it must not create two gateway requests.

- [ ] Step 6: Add tests for all three dispatch sites and the ACP callback.

```bash
python -m pytest \
  tests/tools/test_policy.py \
  tests/tools/test_approval.py \
  tests/acp/test_edit_approval.py \
  tests/acp/test_approval_isolation.py -q
```

- [ ] Step 7: Commit middleware enforcement.

```bash
git add hermes_cli/middleware.py tools/approval.py model_tools.py agent/tool_executor.py agent/agent_runtime_helpers.py tests/tools/test_policy.py tests/tools/test_approval.py
git diff --cached --check
git commit -m "feat(permissions): enforce policy at tool dispatch"
```

## Task 3: Add Modes, Layered Rules, and Scoped Grants

**Files:**
- Create: `tools/permission_grants.py`
- Modify: `hermes_cli/config.py`
- Modify: `tools/policy.py`
- Modify: `tools/file_tools.py`
- Modify: `tools/environments/local.py`
- Modify: `tools/terminal_tool.py`
- Test: `tests/tools/test_permission_grants.py`
- Test: `tests/tools/test_policy.py`

- [ ] Step 1: Add temp-home grant/trust tests.

```python
def test_cwd_grant_expires_and_does_not_apply_elsewhere(tmp_path, monkeypatch):
    store = PermissionGrantStore(tmp_path / "permission_grants.json")
    store.grant("terminal", scope="cwd", cwd=str(tmp_path), backend="local", expires_at=10.0, now=1.0)
    assert store.matches("terminal", cwd=str(tmp_path), backend="local", session_key="s", now=9.0)
    assert not store.matches("terminal", cwd=str(tmp_path / "other"), backend="local", session_key="s", now=9.0)
    assert not store.matches("terminal", cwd=str(tmp_path), backend="local", session_key="s", now=11.0)


def test_project_allow_requires_digest_trust(tmp_path):
    store = PermissionGrantStore(tmp_path / "permission_grants.json")
    assert store.project_allow_trusted(str(tmp_path), "digest-a") is False
    store.trust_project(str(tmp_path), "digest-a", granted_by="user")
    assert store.project_allow_trusted(str(tmp_path), "digest-a") is True
    assert store.project_allow_trusted(str(tmp_path), "digest-b") is False
```

- [ ] Step 2: Add config defaults:

```yaml
permissions:
  mode: default
  project_rules: true
  trusted_project_allows: false
  grants_file: permission_grants.json
  default_ask_ttl_seconds: 86400
```

The loader must resolve the grant path under the active profile home and reject paths outside it. Existing `approvals.deny`, `command_allowlist`, `approvals.mode`, and `cron_mode` become legacy policy layers without behavior loss.

- [ ] Step 3: Implement mode semantics:
  - `default`: current approval behavior plus declarative rules;
  - `plan`: allow only trusted/read-only metadata; unknown/MCP-untrusted tools deny;
  - `acceptEdits`: allow file edits under the active cwd safe roots, ask outside; terminal commands still use hardline/policy rules;
  - `yolo`: allow policy-approved operations without prompting but retain hardline, sensitive-path, and project-trust floors.

- [ ] Step 4: Load layers global → project → grants. Project deny/ask always applies. Project allow is ignored until the digest is trusted; the evaluation result says `project allow requires trust` instead of silently falling through.

- [ ] Step 5: Add the project policy path to `_check_sensitive_path`, terminal dangerous-path detection, and environment write restrictions. A model cannot edit its own project permissions file and thereby grant itself access.

- [ ] Step 6: Add session/cwd/backend TTL grant APIs using atomic JSON writes and existing profile locking. Approval UI choices `once`, `session`, and `always` map to explicit scope/expiry rather than the old unbounded in-memory set.

- [ ] Step 7: Run grant/policy tests under concurrent two-process writes to the same temporary file.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest tests/tools/test_permission_grants.py tests/tools/test_policy.py -q
```

- [ ] Step 8: Commit modes and persistence.

```bash
git add tools/permission_grants.py hermes_cli/config.py tools/policy.py tools/file_tools.py tools/environments/local.py tools/terminal_tool.py tests/tools/test_permission_grants.py tests/tools/test_policy.py
git diff --cached --check
git commit -m "feat(permissions): add scoped modes and grants"
```

## Task 4: Consume MCP Annotations and Sweep Registry Metadata

**Files:**
- Modify: `tools/mcp_tool.py`
- Modify: `tools/registry.py`
- Modify: the affected `tools/*.py` registration sites
- Test: `tests/tools/test_mcp_tool.py`
- Test: `tests/tools/test_mcp_security.py`
- Test: new `tests/tools/test_tool_operation_metadata.py`

- [ ] Step 1: Add MCP annotation tests with trusted and untrusted server contexts.

```python
def test_untrusted_mcp_annotations_do_not_relax_conservative_metadata():
    metadata = mcp_metadata(
        {"annotations": {"readOnlyHint": True, "destructiveHint": False}},
        server_trust="untrusted",
    )
    assert metadata == {"read_only": False, "destructive": True, "idempotent": False}


def test_trusted_mcp_annotations_map_only_valid_booleans():
    metadata = mcp_metadata(
        {"annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}},
        server_trust="trusted",
    )
    assert metadata == {"read_only": True, "destructive": False, "idempotent": True}
```

- [ ] Step 2: Map the 2025-11-25 annotation names at MCP registration. Preserve explicit operator overrides and keep unknown/missing annotations conservative. A server trust record must be part of the registration context, not inferred from a description.

- [ ] Step 3: Inventory all `registry.register` sites and tag only tools whose side-effect behavior is verified. At minimum, tag existing `read_file`, `search_files`, `web_search` and known mutating terminal/file/cron/memory/approval tools. Leave uncertain tools at destructive defaults and emit a diagnostic report for follow-up.

- [ ] Step 4: Add metadata invariant tests for high-risk tools: `web_extract` remains mutating, `terminal` is destructive, `write_file`/`patch` are destructive, `memory`/`skill_manage` are mutating, and `clarify` is non-side-effect but interactive and therefore mode-gated separately.

- [ ] Step 5: Run MCP/metadata tests and the code-execution approval cluster.

```bash
python -m pytest \
  tests/tools/test_mcp_tool.py \
  tests/tools/test_mcp_security.py \
  tests/tools/test_tool_operation_metadata.py \
  tests/tools/test_execute_code_approval_cluster.py -q
```

- [ ] Step 6: Commit annotation/metadata consumption.

```bash
git add tools/mcp_tool.py tools/registry.py tools tests/tools/test_mcp_tool.py tests/tools/test_mcp_security.py tests/tools/test_tool_operation_metadata.py
git diff --cached --check
git commit -m "feat(permissions): consume trusted tool metadata"
```

## Task 5: Inherit Policy Into Cron, Delegation, and Execute-Code Paths

**Files:**
- Modify: `tools/delegate_tool.py`
- Modify: `tools/async_delegation.py`
- Modify: `cron/scheduler.py`
- Modify: `tools/code_execution_tool.py`
- Modify: `gateway/run.py`
- Test: `tests/tools/test_delegate.py`
- Test: `tests/cron/test_cron_approval_mode.py`
- Test: `tests/tools/test_execute_code_approval_cluster.py`

- [ ] Step 1: Add tests proving child/job policy is a snapshot, not a mutable parent reference.

```python
def test_delegated_child_inherits_plan_mode_and_cannot_self_escalate():
    child = build_child_policy(parent_mode="plan", child_rules=())
    assert child.evaluate(operation("write_file", metadata={"destructive": True})).action == "deny"


def test_cron_policy_denies_mutation_even_when_no_interactive_approval_exists():
    decision = run_cron_tool_under_policy("write_file", mode="default", cron_mode=True)
    assert decision["action"] == "deny"
```

- [ ] Step 2: Persist policy mode/rule digest/permission snapshot id in delegation and cron records. Child and job startup loads that snapshot and cannot read a newly written project allow rule unless the parent explicitly restarts it.

- [ ] Step 3: Replace the binary cron auto-approve shortcut with policy evaluation. Cron `ask` becomes `deny` unless the job has a pre-granted scoped rule; human approval is not possible in a detached job. Hardline and sensitive-path floors remain.

- [ ] Step 4: Pass the policy context through `execute_code` RPC. Scripted calls use the parent policy snapshot, cannot write the permissions file, and cannot call `policy`/approval internals to self-grant.

- [ ] Step 5: Add subagent auto-approve policy inheritance. Keep the existing `delegation.subagent_auto_approve` as a legacy fallback only when the snapshot explicitly permits it; default unknown child operations to deny/needs-input rather than silently approve.

- [ ] Step 6: Run delegation/cron/code-execution tests.

```bash
python -m pytest \
  tests/tools/test_delegate.py \
  tests/tools/test_async_delegation.py \
  tests/cron/test_cron_approval_mode.py \
  tests/tools/test_execute_code_approval_cluster.py -q
```

- [ ] Step 7: Commit inheritance.

```bash
git add tools/delegate_tool.py tools/async_delegation.py cron/scheduler.py tools/code_execution_tool.py gateway/run.py tests/tools/test_delegate.py tests/tools/test_async_delegation.py tests/cron/test_cron_approval_mode.py tests/tools/test_execute_code_approval_cluster.py
git diff --cached --check
git commit -m "feat(permissions): inherit policy into workers and jobs"
```

## Task 6: Add the Permissions Review Surface

**Files:**
- Modify: `cli.py`
- Modify: `gateway/slash_commands.py`
- Modify: `tui_gateway/server.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Modify: `ui-tui/src/` only where existing approval overlay/action routing needs the new command.
- Test: new `tests/cli/test_cli_permissions.py`, `tests/gateway/test_permissions_rpc.py`.

- [ ] Step 1: Add command/RPC tests for list, test, grant, revoke, trust, and mode.

```python
def test_permissions_test_reports_provenance_without_raw_arguments():
    result = permissions_test("terminal", {"command": "git status"}, cwd=project_root)
    assert result["action"] == "allow"
    assert result["provenance"]
    assert "git status" not in json.dumps(result)


def test_permissions_revoke_removes_only_matching_scope():
    grant = permissions_grant("terminal", scope="cwd", cwd=project_root, ttl=60)
    permissions_revoke(grant["id"])
    assert permissions_list()["grants"] == []
```

- [ ] Step 2: Add `/permissions` commands and `permissions.*` RPC methods using existing profile/session routing. `list` shows mode, rules, grants, trust digests, and provenance; `test` shows the decision for a redacted operation; `grant`/`revoke`/`trust` use existing approval/user-identity checks.

- [ ] Step 3: Expose `plan`, `acceptEdits`, and `default` mode changes as session-scoped actions. `yolo` continues to use the existing explicit opt-in path and reports its floors.

- [ ] Step 4: If the TUI needs a panel, reuse the existing approval overlay and list/table primitives. Do not add a web-only permission implementation; the gateway RPC is the source of truth.

- [ ] Step 5: Run CLI/gateway tests and verify redaction of commands, paths, tokens, and raw tool arguments.

```bash
python -m pytest tests/cli/test_cli_permissions.py tests/gateway/test_permissions_rpc.py tests/gateway/test_approval_prompt_redaction.py -q
```

- [ ] Step 6: Commit the review surface.

```bash
git add cli.py gateway/slash_commands.py tui_gateway/server.py hermes_cli/cli_commands_mixin.py ui-tui/src tests/cli/test_cli_permissions.py tests/gateway/test_permissions_rpc.py
git diff --cached --check
git commit -m "feat(permissions): add reviewable permissions surface"
```

## End-to-End Verification

- [ ] In a temporary project, create a deny rule for `write_file`, an allow rule for `terminal(git *)`, and an untrusted MCP tool with `readOnlyHint=true`. Verify deny/allow/deny respectively.
- [ ] Modify the project permissions file from an agent tool and verify `_check_sensitive_path`/terminal guards block the write.
- [ ] Change the project file digest, restart, and verify the old trust grant no longer enables project allows.
- [ ] Run one CLI, one gateway, one ACP, one execute-code, one cron, one delegation, and one MCP dispatch through the policy middleware; compare `policy_key`/provenance and action.
- [ ] Force a policy evaluator exception and verify a destructive operation is denied and the turn continues with a user-visible policy error.
- [ ] Generate shell obfuscation variants with the existing approval test helper; assert hardline commands never become allow.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/tools/test_policy.py \
  tests/tools/test_permission_grants.py \
  tests/tools/test_approval.py \
  tests/tools/test_approval_deny_rules.py \
  tests/tools/test_mcp_tool.py \
  tests/tools/test_mcp_security.py \
  tests/tools/test_tool_operation_metadata.py \
  tests/tools/test_delegate.py \
  tests/cron/test_cron_approval_mode.py \
  tests/tools/test_execute_code_approval_cluster.py \
  tests/cli/test_cli_permissions.py \
  tests/gateway/test_permissions_rpc.py -q
python3 -m compileall -q tools/policy.py tools/permission_grants.py
 git diff --check
```

## Acceptance Checklist

- [ ] Every tool dispatch path passes through one built-in policy decision.
- [ ] Default/plan/acceptEdits/yolo modes have structural semantics, not prompt-only instructions.
- [ ] Rule precedence and hardline/sensitive-path floors are tested.
- [ ] Project allows require a trusted, digest-bound grant; project denies/asks cannot be bypassed.
- [ ] Grants are scope/TTL/backend aware and stored atomically without raw secrets/arguments.
- [ ] MCP annotations never relax an untrusted server's conservative metadata.
- [ ] High-risk registry metadata is verified; uncertain tools remain destructive.
- [ ] Cron, delegation, and execute-code inherit a policy snapshot and cannot self-escalate.
- [ ] `/permissions`/RPC surfaces expose redacted provenance and revoke/trust actions.
- [ ] Policy failures fail closed for mutating operations without breaking the turn loop.

## Deliberate Simplifications

- Skipped a general expression DSL; name/command/path/metadata matchers cover the documented use cases without an interpreter attack surface.
- Skipped replacing regex hardline detection; the policy engine consumes it as a security floor and can later add a sandbox.
- Skipped automatic trust of project files; require explicit digest-bound trust because the agent can write the project directory.
- Skipped OS-level sandboxing; that is roadmap item 9 and should consume this policy decision rather than duplicate it.
