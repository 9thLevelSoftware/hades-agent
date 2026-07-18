# OS-Sandboxed Local Execution and Credential-Brokered Egress Implementation Plan

> For agentic workers: this plan changes the security boundary. Ship each phase behind an opt-in mode, fail closed when the promised boundary cannot be established, and never claim sandbox protection from a heuristic wrapper.

**Goal:** Add an opt-in `local-sandboxed` terminal backend for Linux/macOS, deny-by-default domain egress through an existing approval surface, and eventually broker credentials outside the jail while adding value-based redaction. Reuse the same wrapper for execute-code and MCP subprocesses.

**Architecture:** Phase 1 adds value-based secret registration/redaction independent of sandboxing. Phase 2 adds `LocalSandboxedEnvironment`, wrapping the existing `LocalEnvironment` command with Linux bubblewrap plus optional Landlock/seccomp and macOS Seatbelt, binding only the workspace/safe roots/snapshot/temp paths. Phase 3 runs a host-side raw-tunnel proxy over a Unix socket with domain/private-IP policy and existing approval. Phase 4 adds an opt-in credential broker/lease layer; children receive opaque tokens and no real provider credentials. Windows reports unsupported and never falls back silently when `local-sandboxed` is requested.

**Tech Stack:** `BaseEnvironment`, `LocalEnvironment`, `terminal_tool`, `file_safety.get_safe_write_roots`, bubblewrap/Landlock/seccomp, macOS Seatbelt/sandbox-exec, UDS proxy, `website_policy`/`url_safety`, `request_tool_approval`, `SecretSource`/`secret_scope`/`credential_pool`, `agent.redact`, MCP watchdog, execute-code RPC, temp profile state.

## Global Constraints

- `TERMINAL_ENV=local` behavior is unchanged until a later separately reviewed default flip. `local-sandboxed` is explicit.
- Linux must verify bwrap/user-namespace/jail capabilities before execution. If the configured strictness is `refuse`, no command runs when unavailable. A `degrade` option may use Landlock-only only when it can prove the configured file/network boundary; it must state the reduced boundary.
- macOS must verify the Seatbelt wrapper/profile and deny all writes/network except declared roots/proxy loopback. If `sandbox-exec` is unavailable or profile compilation fails, fail closed.
- Windows returns an actionable unsupported error for `local-sandboxed`; it does not silently select local.
- Existing file-tool paths/approval/redaction remain defense-in-depth, not the claimed boundary. The jail must cover shell, execute-code, background process, and MCP stdio spawns.
- Safe roots are computed from `get_safe_write_roots()` plus the session workspace/snapshot/temp paths. Never bind the whole home writable.
- The session snapshot files (`hermes-snap-*.sh`), cwd file, and temp/RPC paths must be visible and writable inside the jail or shells degrade; test this explicitly.
- Egress is deny-by-default when network isolation is active. Domain approvals are bound to normalized host, port, policy key, session/cwd/backend, and TTL; deny wins over allow.
- Phase 3 raw tunneling does not terminate TLS or inject credentials. Credential brokering is Phase 4 opt-in and must use fresh upstream connections/host mapping; no MITM by default.
- Secret values are never passed to child env/argv/files. Opaque tokens are scoped, short-lived, host-bound, and useless outside the broker. Broker failures deny requests requiring credentials.
- Value redaction has minimum length/dedupe/cap safeguards to avoid over-masking and hot-path blowups. Redaction is applied before logs/history/display/transport outputs.
- No dependency is mandatory at install time. Capability checks are runtime and testable with fake binaries/profiles.
- Tests must include actual subprocesses and network-loopback fixtures where possible; shell command strings alone do not prove a jail.

## Current-State Review

- `terminal_tool._create_environment` supports local/docker/singularity/modal/daytona/ssh, not local-sandboxed.
- `LocalEnvironment` has env scrubbing, process spawning, and background/PTY paths, but no OS jail or egress proxy.
- `BaseEnvironment` already exposes snapshot/temp/sandbox paths and `_run_bash`; subclassing local and changing argv is the smallest backend delta.
- `file_safety.get_safe_write_roots()` is a ready policy input but is currently heuristic at file-tool level.
- `tools/code_execution_tool._scrub_child_env` and `tools/mcp_tool._build_safe_env` are separate escape-path env filters; both need the sandbox wrapper, not a third filter.
- `tools/process_registry.spawn_local` is another background shell spawn path that must be jailed when the parent backend is local-sandboxed.
- `website_policy` and `url_safety` provide policy/private-IP inputs; `approval.request_tool_approval` provides generic approval/gateway persistence.
- `SecretSource`, `secret_scope`, and `credential_pool` load/rotate secrets but no broker/lease exists. `agent.redact` is regex/prefix based, not value based.

The plan skips Windows AppContainer, TLS proxying in the first sandbox release, and a new approval UI.

## Release Order

1. Value-based redaction registry.
2. Local sandbox backend and capability/strictness checks.
3. Raw-tunnel egress proxy and domain approval.
4. Credential broker/leases and remote credential-file reduction.
5. Execute-code/MCP/background integration, documentation, and adversarial tests.

## File Map

- Create: `agent/secret_redaction.py` — scoped value registry/caps/normalization.
- Create: `tools/environments/local_sandboxed.py` — jail command construction/runtime.
- Create: `tools/environments/sandbox_capabilities.py` — bwrap/Seatbelt/Landlock detection and diagnostics.
- Create: `tools/environments/egress_proxy.py` — UDS listener/raw CONNECT tunnel/domain policy.
- Create: `agent/credential_broker.py` — optional opaque-token/lease/upstream secret mapping.
- Modify: `agent/redact.py`, `agent/secret_sources/registry.py`, `agent/secret_scope.py`, `agent/credential_pool.py` — value registration/lease hooks.
- Modify: `tools/environments/base.py`, `tools/environments/local.py`, `tools/terminal_tool.py`, `hermes_cli/config.py` — backend/config/capability paths.
- Modify: `tools/process_registry.py`, `tools/code_execution_tool.py`, `tools/mcp_tool.py`, `tools/mcp_stdio_watchdog.py` — jail all child paths.
- Modify: `tools/approval.py`, `tools/url_safety.py`, `tools/website_policy.py` — egress policy/approval identity reuse.
- Modify: `tools/environments/file_sync.py`, `tools/credential_files.py` — later brokered remote credential option.
- Modify: `SECURITY.md`, terminal/code execution/MCP docs.
- Test: new `tests/agent/test_secret_redaction.py`, `tests/tools/environments/test_sandbox_capabilities.py`, `tests/tools/environments/test_local_sandboxed.py`, `tests/tools/environments/test_egress_proxy.py`, `tests/agent/test_credential_broker.py`.
- Test: extend process/code-execution/MCP/env/redaction/approval suites.

## Data Contracts

```python
@dataclass(frozen=True)
class SandboxCapabilities:
    platform: Literal["linux", "darwin", "win32"]
    bwrap: bool
    landlock: bool
    seccomp: bool
    seatbelt: bool
    network_isolation: bool
    reason: str
```

```python
@dataclass(frozen=True)
class SandboxSpec:
    cwd: str
    writable_roots: tuple[str, ...]
    readable_roots: tuple[str, ...]
    temp_dir: str
    rpc_dir: str | None
    network: Literal["deny", "proxy"]
    allowed_domains: tuple[str, ...]
    strictness: Literal["refuse", "degrade"]
```

```python
@dataclass(frozen=True)
class SecretLease:
    lease_id: str
    opaque_token: str
    host_pattern: str
    expires_at: float
    session_key: str
    source_name: str
```

## Phase 1: Value-Based Secret Redaction

### Task 1.1: Register loaded secret values safely

**Files:**
- Create: `agent/secret_redaction.py`
- Modify: `agent/redact.py`
- Modify: `agent/secret_sources/registry.py`, `agent/secret_scope.py`, `agent/credential_pool.py`
- Test: `tests/agent/test_secret_redaction.py`

- [ ] Step 1: Add pure registry tests.

```python
def test_secret_values_are_redacted_even_without_known_prefix():
    registry = SecretValueRegistry()
    registry.register("opaque-secret-value-123", source="fixture")
    assert registry.redact("url=opaque-secret-value-123") == "url=[REDACTED]"


def test_short_or_common_values_are_not_registered():
    registry = SecretValueRegistry()
    assert registry.register("true", source="fixture") is False
    assert registry.register("1234567", source="fixture") is False
```

- [ ] Step 2: Implement minimum length (12), entropy/common-value exclusion, exact/deduped value set, maximum entry count/total bytes, and session/profile scoping. Never persist raw values; clear registry on profile/session teardown.

- [ ] Step 3: Register values at `SecretSource`/secret-scope/credential-pool load points, including rotated values, and expose a snapshot to `redact_sensitive_text`/`RedactingFormatter`. Keep regex redaction as first pass and literal values as second pass.

- [ ] Step 4: Ensure value redaction runs before display/log/history/upload and does not mutate canonical secret stores. Add overlap/longest-first ordering to avoid leaking suffixes.

- [ ] Step 5: Run redaction/env tests and commit.

```bash
python -m pytest tests/agent/test_secret_redaction.py tests/agent/test_redact.py tests/agent/test_secret_scope.py -q
 git add agent/secret_redaction.py agent/redact.py agent/secret_sources/registry.py agent/secret_scope.py agent/credential_pool.py tests/agent/test_secret_redaction.py
 git diff --cached --check
git commit -m "feat(security): add value-based secret redaction"
```

## Phase 2: Local Sandbox Backend

### Task 2.1: Detect capabilities and build Linux/macOS jail argv

**Files:**
- Create: `tools/environments/sandbox_capabilities.py`
- Create: `tools/environments/local_sandboxed.py`
- Modify: `tools/environments/base.py` only for public path/spec helpers.
- Modify: `tools/terminal_tool.py`, `hermes_cli/config.py`
- Test: `tests/tools/environments/test_sandbox_capabilities.py`
- Test: `tests/tools/environments/test_local_sandboxed.py`

- [ ] Step 1: Add capability matrix tests using fake executable/probe results.

```python
def test_linux_refuse_when_bwrap_missing():
    caps = detect_sandbox_capabilities(platform="linux", executable_exists=lambda name: False)
    assert caps.bwrap is False
    assert choose_sandbox_mode(caps, strictness="refuse").action == "refuse"


def test_windows_is_explicitly_unsupported():
    caps = detect_sandbox_capabilities(platform="win32", executable_exists=lambda name: True)
    assert choose_sandbox_mode(caps, strictness="refuse").action == "unsupported"
```

- [ ] Step 2: Implement probes without running privileged commands. Linux checks bwrap binary, user namespace usability, Landlock/seccomp availability; macOS checks Seatbelt command/profile execution; Windows returns unsupported. Cache per process/runtime config and expose diagnostics.

- [ ] Step 3: Implement `SandboxSpec` path normalization. Include cwd, workspace, `get_safe_write_roots()`, `get_sandbox_dir()`, `get_temp_dir()`, snapshot/cwd files, and RPC/artifact directories. Deduplicate/resolve paths and reject roots outside allowed policy.

- [ ] Step 4: Build Linux argv with `bwrap --unshare-net --unshare-pid` and explicit ro/rw binds. Add Landlock/seccomp only when probes support it; no broad `--bind /` writable shortcut. Build macOS Seatbelt profile with read-only baseline, root write denies, allowed writable roots, and network deny/loopback proxy allowance.

- [ ] Step 5: Preserve `start_new_session`, process-group interruption, shell snapshot sourcing, cwd, temp, and env arguments. The wrapper must return structured diagnostics if a path/profile cannot be represented.

- [ ] Step 6: Wire `TERMINAL_ENV=local-sandboxed` into `_get_env_config`, `_create_environment`, requirement checks, and YAML config. Default `sandbox.strictness=refuse`, `network=deny`, no domains.

- [ ] Step 7: Run builder tests and commit the backend.

```bash
python -m pytest tests/tools/environments/test_sandbox_capabilities.py tests/tools/environments/test_local_sandboxed.py tests/tools/test_terminal_tool.py -q
 git add tools/environments/sandbox_capabilities.py tools/environments/local_sandboxed.py tools/environments/base.py tools/terminal_tool.py hermes_cli/config.py tests/tools/environments/test_sandbox_capabilities.py tests/tools/environments/test_local_sandboxed.py
 git diff --cached --check
git commit -m "feat(security): add opt-in local sandbox backend"
```

### Task 2.2: Prove filesystem/process isolation with real subprocesses

**Files:**
- Test: `tests/tools/environments/test_local_sandboxed.py`
- Test: `tests/tools/test_code_execution_modes.py`
- Test: `tests/tools/test_mcp_stability.py`

- [ ] Run a Linux fixture when bwrap is available: write inside workspace/safe root succeeds, write `/tmp`/home/outside fails, read allowed roots works, read blocked roots fails, process cannot see parent provider-key env, and network is unavailable in deny mode.
- [ ] Run a macOS fixture through Seatbelt when available with the same workspace/write/env assertions.
- [ ] Run capability-negative tests when binaries are absent; assert `local-sandboxed` refuses and `local` remains unchanged.
- [ ] Verify timeout/interrupt kills the wrapped process group and no child remains.
- [ ] Verify snapshot/cwd/temp files are accessible and shell commands do not degrade to an unexpected login-shell path.

```bash
python -m pytest tests/tools/environments/test_local_sandboxed.py tests/tools/test_code_execution_modes.py tests/tools/test_mcp_stability.py -q
```

- [ ] Commit the isolation evidence.

```bash
git add tests/tools/environments/test_local_sandboxed.py tests/tools/test_code_execution_modes.py tests/tools/test_mcp_stability.py
git diff --cached --check
git commit -m "test(security): verify local sandbox isolation"
```

## Phase 3: Domain-Allowlist Raw-Tunnel Proxy

### Task 3.1: Build host-side proxy and connect-time policy

**Files:**
- Create: `tools/environments/egress_proxy.py`
- Modify: `tools/url_safety.py`, `tools/website_policy.py`, `tools/approval.py`
- Modify: `hermes_cli/config.py`
- Test: `tests/tools/environments/test_egress_proxy.py`

- [ ] Step 1: Add loopback proxy tests with a local HTTP CONNECT fixture and fake DNS/private-IP resolver.

```python
def test_proxy_denies_unknown_domain_before_connect(proxy):
    response = proxy.connect("unknown.example", 443, session_key="s")
    assert response.action == "ask"
    assert proxy.upstream_connections == []


def test_proxy_denies_private_ip_after_dns_resolution(proxy):
    response = proxy.connect("public.example", 443, session_key="s", resolved_ip="127.0.0.1")
    assert response.action == "deny"


def test_allowed_domain_tunnels_bytes_without_tls_termination(proxy, upstream):
    proxy.allow("api.example", scope="session")
    assert proxy.connect("api.example", 443, session_key="s").action == "allow"
    assert proxy.tunnel(b"CONNECT payload", upstream).bytes_forwarded > 0
```

- [ ] Step 2: Implement a host-side listener on a UDS under the sandbox directory. Because a network namespace cannot share normal loopback, provide the smallest in-jail TCP-to-UDS shim or a bwrap-visible proxy endpoint; the child receives HTTP(S)/ALL proxy variables pointing to that endpoint.

- [ ] Step 3: Normalize host/domain with IDNA/lowercase/trailing-dot removal and port. Evaluate deny-first `website_policy`, `url_safety` private-IP/DNS rebinding checks at connect time, configured `terminal.sandbox.allowed_domains`, and session grants.

- [ ] Step 4: Unknown domains call existing `request_tool_approval` with `pattern_key=egress_domain:<host>:<port>` and once/session/TTL scope. No new prompt/approval UI. Approval identity includes session/cwd/backend.

- [ ] Step 5: Phase 3 is raw tunnel only: no TLS termination, credential injection, DNS forwarding outside policy, or arbitrary SOCKS CONNECT. Unsupported protocols fail with a clear network-policy error.

- [ ] Step 6: Add denial/approval/rebinding/IPv6/private-range/rate-limit tests and commit.

```bash
python -m pytest tests/tools/environments/test_egress_proxy.py tests/tools/test_url_safety.py tests/tools/test_approval.py -q
 git add tools/environments/egress_proxy.py tools/url_safety.py tools/website_policy.py tools/approval.py hermes_cli/config.py tests/tools/environments/test_egress_proxy.py
 git diff --cached --check
git commit -m "feat(security): add sandbox egress policy proxy"
```

### Task 3.2: Compose proxy with sandboxed terminal/code/MCP paths

**Files:**
- Modify: `tools/environments/local_sandboxed.py`
- Modify: `tools/process_registry.py`
- Modify: `tools/code_execution_tool.py`
- Modify: `tools/mcp_tool.py`, `tools/mcp_stdio_watchdog.py`
- Test: code-execution/MCP/process environment suites.

- [ ] Pass one `SandboxSpec`/proxy handle through terminal child creation, background `spawn_local`, execute-code child spawn, and MCP stdio watchdog wrapping. Do not allow an unsandboxed helper to inherit `TERMINAL_ENV=local-sandboxed` context.
- [ ] Ensure env scrubbing removes real provider credentials even when proxy mode is active; only proxy endpoint/session opaque-token variables are injected.
- [ ] Add tests that execute shell, code-mode, and an MCP fixture under the same jail and assert the same file/network boundary.
- [ ] Add fail-closed tests for proxy unavailable, UDS permission mismatch, and wrapper omission.

```bash
python -m pytest \
  tests/tools/environments/test_local_sandboxed.py \
  tests/tools/test_code_execution_modes.py \
  tests/tools/test_code_execution.py \
  tests/tools/test_mcp_stability.py \
  tests/tools/test_mcp_stdio_init_timeout.py -q
```

- [ ] Commit composition.

```bash
git add tools/environments/local_sandboxed.py tools/process_registry.py tools/code_execution_tool.py tools/mcp_tool.py tools/mcp_stdio_watchdog.py tests/tools/environments/test_local_sandboxed.py tests/tools/test_code_execution_modes.py tests/tools/test_code_execution.py tests/tools/test_mcp_stability.py
git diff --cached --check
git commit -m "feat(security): jail code and MCP child paths"
```

## Phase 4: Credential Broker and Secret Leases

### Task 4.1: Opaque-token lease broker without TLS MITM

**Files:**
- Create: `agent/credential_broker.py`
- Modify: `agent/secret_sources/registry.py`, `agent/secret_scope.py`, `agent/credential_pool.py`
- Modify: `tools/environments/egress_proxy.py`
- Modify: `hermes_cli/config.py`
- Test: `tests/agent/test_credential_broker.py`

- [ ] Step 1: Add tests for host-bound opaque token, expiry, rotation, and no raw secret in child env/log.

```python
def test_opaque_token_is_host_bound_and_expires():
    broker = CredentialBroker(secret_source=fixture_secret_source("opaque"))
    lease = broker.issue("api.example", session_key="s", ttl=30)
    assert broker.resolve(lease.opaque_token, host="api.example", session_key="s", now=10) == "opaque"
    assert broker.resolve(lease.opaque_token, host="other.example", session_key="s", now=10) is None
    assert broker.resolve(lease.opaque_token, host="api.example", session_key="s", now=31) is None
```

- [ ] Step 2: Implement lease records in memory/profile state without persisting raw values. Use `SecretSource` lookup at resolution time, credential-pool rotation metadata, session/backend/host binding, and short TTL.

- [ ] Step 3: Keep broker protocol outside the jail. Phase 4 may inject credentials only into a fresh upstream connection selected by normalized host; the child never receives the real value. Raw-tunnel mode remains default; TLS-terminating injection is a separate explicit `credential_broker.tls_terminate` opt-in with a CA/cert hygiene gate.

- [ ] Step 4: Add value-redaction registration for any broker-side response/header containing loaded secret values.

- [ ] Step 5: Run broker tests and commit.

```bash
python -m pytest tests/agent/test_credential_broker.py tests/agent/test_secret_redaction.py -q
 git add agent/credential_broker.py agent/secret_sources/registry.py agent/secret_scope.py agent/credential_pool.py tools/environments/egress_proxy.py hermes_cli/config.py tests/agent/test_credential_broker.py
 git diff --cached --check
git commit -m "feat(security): add scoped credential leases"
```

### Task 4.2: Reduce remote credential-file shipping

**Files:**
- Modify: `tools/environments/file_sync.py`, `tools/credential_files.py`
- Modify: SSH/Modal environment integration only where existing credential mounts are selected.
- Test: existing remote environment/credential tests.

- [ ] Add `credential_mode=brokered|legacy` with legacy default for remote backends until broker availability is proven.
- [ ] In brokered mode, sync only non-secret config and proxy/lease endpoint; never copy `~/.hermes` credentials. Failure to acquire a lease prevents the remote operation rather than reverting to legacy automatically.
- [ ] Test no provider key/auth file appears in remote sync payload; test explicit legacy mode preserves current behavior and warnings.

```bash
python -m pytest tests/tools/test_code_execution_modes.py tests/tools/test_terminal_tool.py tests/tools/test_credential_files.py -q
```

- [ ] Commit remote lease option.

```bash
git add tools/environments/file_sync.py tools/credential_files.py tests/tools/test_code_execution_modes.py tests/tools/test_terminal_tool.py tests/tools/test_credential_files.py
git diff --cached --check
git commit -m "feat(security): add brokered remote credential mode"
```

## Phase 5: Documentation and Adversarial Verification

**Files:**
- Modify: `SECURITY.md`
- Modify: terminal/code-execution/MCP docs and example config.
- Test: all sandbox/proxy/broker/env/redaction suites.

- [ ] Document capability detection, strictness/degrade semantics, root bindings, unsupported Windows behavior, proxy domain grants, raw-tunnel limitations, credential broker opt-in, and legacy remote modes.
- [ ] Run a real subprocess matrix for Linux/macOS capability-present/absent, workspace/write-root escapes, network deny/allow/rebinding, secret env/log redaction, background spawn, execute-code, and MCP stdio.
- [ ] Run a prompt-injection fixture that reads env/files, writes outside roots, uses DNS rebinding/private IP, and exfiltrates an opaque token; assert boundary/deny/redaction results.
- [ ] Run process interruption/cleanup tests: proxy/socket/process group/temporary lease cleanup leaves no orphan.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_secret_redaction.py \
  tests/agent/test_credential_broker.py \
  tests/tools/environments/test_sandbox_capabilities.py \
  tests/tools/environments/test_local_sandboxed.py \
  tests/tools/environments/test_egress_proxy.py \
  tests/tools/test_code_execution.py \
  tests/tools/test_code_execution_modes.py \
  tests/tools/test_mcp_stability.py \
  tests/tools/test_mcp_stdio_init_timeout.py \
  tests/tools/test_approval.py -q
python3 -m compileall -q agent/secret_redaction.py agent/credential_broker.py tools/environments/local_sandboxed.py tools/environments/egress_proxy.py
 git diff --check
```

- [ ] Commit documentation/evidence.

```bash
git add SECURITY.md docs website cli-config.yaml.example tests
 git diff --cached --check
git commit -m "docs(security): document sandbox and credential boundaries"
```

## Acceptance Checklist

- [ ] Value-based redaction masks loaded secrets across outputs without persisting raw values.
- [ ] `local-sandboxed` has explicit capability checks and fail-closed strict mode.
- [ ] Linux/macOS filesystem/process/network boundaries are proven with real subprocess tests.
- [ ] Snapshot/temp/RPC paths remain usable inside the jail.
- [ ] Background, execute-code, and MCP subprocess paths cannot escape the selected sandbox mode.
- [ ] Egress is deny-by-default, connect-time private-IP safe, domain/TTL/session scoped, and uses existing approval identity.
- [ ] Raw-tunnel phase does not claim credential brokering; brokered credentials are opt-in, host-bound, short-lived, and absent from child env.
- [ ] Remote credential shipping has an explicit brokered path and no silent fallback.
- [ ] Windows requested sandbox is unsupported/fail-closed; local backend remains unchanged.
- [ ] Security docs accurately distinguish heuristics, jail, proxy, and broker guarantees.

## Deliberate Simplifications

- Skipped changing the default local backend; enable by explicit config until platform coverage is proven.
- Skipped Windows sandboxing; a false boundary is worse than an honest unsupported error.
- Skipped TLS termination in the first proxy; raw tunnel gives domain control without a fragile MITM.
- Skipped a custom approval UI; existing gateway/CLI approval flows already support the needed domain decision.
- Skipped full remote credential migration until local broker/lease behavior is tested; legacy mode stays explicit and visible.
