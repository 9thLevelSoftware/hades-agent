# Safe Capability Exchange Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Hermes one fail-closed, content-addressed package lifecycle for discovering, inspecting, installing, quarantining, granting, canarying, promoting, updating, rolling back, and removing skills, standalone plugins, and MCP servers while proving that undeclared effects cannot escape their grants.

**Architecture:** Add an internal `agent.capabilities` narrow waist containing immutable package manifests, content/provenance identity, a profile-local lifecycle store, source/verifier/scanner/runtime adapter registries, and a crash-safe coordinator. Item #6 remains the authority owner and persists explicit attenuable `CapabilityGrant` values; item #15 evaluates every source-to-sink flow at the final effect boundary; item #12 records immutable lifecycle and execution receipts. `hermes capability` and native Ink are the primary package-manager surfaces, Dashboard is a read-only inspector, and existing skill, plugin, and MCP entry points become adapters rather than parallel trust models.

**Tech Stack:** Python 3.13, frozen dataclasses and `typing.Literal`, canonical JSON/SHA-256, SQLite/WAL through `SessionDB`, existing Skills Hub/plugin/MCP loaders, item #6 `AuthorityProvider`, item #15 `InformationFlowGuard`, item #12 `ReceiptStore`, optional service-gated Sigstore/TUF-style verifier providers, WASI component runtime adapters when compatible, Python/MCP sandbox adapters otherwise, Rich/classic CLI, Ink/TypeScript JSON-RPC TUI, React Dashboard, pytest through `scripts/run_tests.sh`, Vitest, and versioned YAML benchmark fixtures.

## Global Constraints

- Work from a branch containing item #6's canonical `AuthorityProvider`, `StoredAuthorityProvider`, `ActionContext`, `AuthorityDecision`, and `authorize_effect()`, item #15's `FlowContext` and `InformationFlowGuard`, and item #12's `ReceiptStore`. If a prerequisite name is absent, its contract test fails; this item must not create a local substitute.
- `agent.capabilities` owns package identity, declarations, content/provenance verification, lifecycle, locks, runtime adapter selection, drift, and execution attribution. Item #6 owns grant authority and persistence, item #15 owns deterministic source-to-sink enforcement, and item #12 owns proof/status vocabulary.
- A signature, transparency-log entry, TUF-style metadata result, source reputation, allow-list entry, or clean scan proves only its narrow claim. None means trusted, safe, verified behavior, or permission to execute.
- Every install begins in profile-local quarantine. No package imports, skill activation, MCP connection, bootstrap command, entry point, hook, or subprocess runs before exact bytes are locked, declarations are validated, scans finish, and an explicit current grant exists.
- Grants are explicit, digest-bound, profile-bound, attenuable, revocable, expiring, use-bounded, and no broader than both the manifest declaration and the parent/item #6 authority decision. Update permission drift always requires a new grant; scan/signature continuity never carries authority across a changed manifest or content digest.
- WASI components are the preferred high-isolation runtime only when the package declares a compatible component ABI and a configured adapter can satisfy it. Python and MCP packages use sandbox adapters with OS/container/process/network/filesystem/secret/model mediation; compatibility fallback may reduce availability, never isolation.
- Stable non-secret policy lives under `capability_exchange:` in profile-local `config.yaml`. Publisher keys, signing credentials, OAuth tokens, API keys, and secret values remain in secret providers or `.env`; manifests and audit rows contain secret references only.
- Profiles remain independent islands. Package objects, locks, grants, quarantine, rollback generations, receipts, and audit resolve from `get_hermes_home()` and cannot be reused across profiles merely because bytes match.
- Third-party products and vendor integrations do not enter the core tree. They remain standalone plugin repositories or MCP servers; core contains only generic contracts, adapters, CLI workflow, and benchmark fixtures.
- This is Footprint Ladder rung 2: one CLI/package-manager workflow and bundled skill instructions over existing terminal/file facilities. Runtime providers are standalone plugins; acquired capabilities remain skills, standalone plugins, or MCP edges. No new model-visible core tool or toolset is added.
- Promotion never mutates a live conversation's system prompt, cached prefix, effective tool-definition snapshot, provider, or model. A changed skill catalog, plugin hooks, MCP tools, or package generation takes effect only in a new conversation/process cache lineage; old conversations retain their pinned generation or receive a deterministic unavailable result.
- Strict message-role alternation remains valid. Capability state is sidecar/runtime metadata; it never rewrites past messages, inserts a synthetic user message mid-loop, or mutates history outside existing compression.
- Every state, security, resolution, sandbox, secret, subprocess, and remote-I/O change receives real-path E2E coverage against a temporary `HERMES_HOME`; mocks are limited to external registries, transparency/TUF services, OAuth endpoints, OS-specific sandbox engines unavailable in CI, and deterministic crash points.
- Audit, receipts, logs, CLI/TUI output, and benchmark reports contain package/grant/authority/flow/operation identities and hashes, never raw credentials, secret-bearing URLs, environment values, file bodies, MCP payload bodies, or model prompts.
- User-visible outcomes use item #12's exact statuses: `verified`, `completed_unverified`, `failed`, `blocked`, and `unknown_effect`. Package installation or signing cannot mint `verified`; an independent end-state scorer must prove the requested package/lifecycle state.
- No outbound telemetry is introduced. Reports remain local and publish denominator, exclusions, Wilson intervals, p50/p95 latency, cost per verified success, safety slices, recovery burden, and stop conditions separately.
- The frozen 90-day proof denominator is exactly 60 cases over one real wrapped extension: 12 lifecycle, 20 isolation, 12 supply-chain, 8 crash/replay, and 8 benign-use cases. Pass requires zero undeclared effects, 60/60 complete identity-to-grant audit chains, reproducible content in every rebuild, 8/8 benign normal-use successes, and package-plus-grant rollback restoration in every rollback case.

---

## Approved Portfolio Contract

**Layman outcome:** Hermes can safely find, install, authorize, isolate, update, and remove a new skill, plugin, or MCP capability with the same confidence expected from a modern package manager.

**Design boundary:** One canonical manifest records publisher/signature evidence, content digest, provenance, reproducible version, requested credentials, runtime, network origins, filesystem access, process access, model access, and data categories. Installation scans and quarantines before explicit grants; WASI is preferred for compatible high-isolation packages and sandboxed Python/MCP adapters cover other packages. Updates display permission drift and run a canary before promotion; rollback atomically restores both the prior package generation and its prior grant snapshot. Existing Skills Hub, standalone plugin, and MCP machinery remain transport/runtime adapters, not independent authority stores.

**90-day proof:** Wrap the repository's real `plugins/disk-cleanup` extension as a standalone capability package without copying it into core, build/sign/lock/install version 1, update to version 2 with a declared network permission increase, canary it, promote it only after an explicit replacement grant, and roll back package plus grants. Exercise undeclared filesystem, network, process, credential, model, and data-flow attempts. Pass only if every undeclared attempt fails before its effect boundary, normal disk-report behavior remains usable, repeated builds are byte/content reproducible, and each audit chain names the exact package identity, content digest, grant, authority decision, flow decision, operation, and receipt.

**Dependencies and failure conditions:** Item #6 exclusively owns whether a grant may be issued, renewed, attenuated, or revoked. Item #15 enforces declared and granted data flows. Item #12 records lifecycle/execution proof. Stop if a scan/signature is treated as trust, a permission increase inherits an old grant, a capability can access undeclared filesystem/network/process/credential/model scope, rollback does not restore both bytes and grants, normal behavior becomes unusable, or the audit cannot map an effect to identity plus grant.

**Delivery:** Footprint Ladder rung 2 for `hermes capability` + skill + native Ink package management. Generic runtime/source/verifier adapters extend existing code; WASI/Python sandbox providers and niche capabilities remain standalone plugins or MCP servers. Dashboard is a secondary read-only inspector, Desktop has no dependency, and the model-visible tool schema does not change.

---

## Canonical Public Contract — Frozen for Portfolio Consumers

`agent.capabilities` is the only public import surface. Extra implementation helpers live in sibling modules. Manifest and grant hashes use UTF-8 canonical JSON with sorted string keys, compact separators, NFC strings, tuples encoded as arrays, finite decimal values, normalized forward-slash relative paths, lowercase DNS hosts, explicit ports, and UTC RFC 3339 timestamps. IDs are derived from full `sha256:` digests, never truncated scan hashes.

```python
CAPABILITY_SCHEMA_VERSION = "hermes.capability.v1"

CapabilityKind = Literal["skill", "plugin", "mcp"]
RuntimeKind = Literal[
    "skill_instructions", "wasi_component", "python_plugin", "mcp_stdio", "mcp_http"
]
LifecycleState = Literal[
    "discovered", "quarantined", "blocked", "staged", "canary",
    "active", "rolled_back", "revoked", "removed"
]
AccessMode = Literal["read", "write", "create", "delete", "execute"]
DataCategory = Literal[
    "public", "internal", "personal", "confidential", "credential",
    "financial", "health", "unknown"
]

@dataclass(frozen=True)
class PublisherIdentity:
    publisher_id: str
    display_name: str
    identity_kind: Literal["local", "oidc", "key", "registry", "unknown"]
    issuer: str
    subject: str

@dataclass(frozen=True)
class ProvenanceStatement:
    source_uri: str
    source_revision: str
    builder_id: str
    build_recipe_digest: str
    materials: tuple[str, ...]
    reproducible: bool

@dataclass(frozen=True)
class SignatureEvidence:
    provider_id: str
    signature_kind: Literal["sigstore", "tuf", "key", "local_test", "none"]
    signed_digest: str
    signature_ref: str
    certificate_issuer: str
    certificate_subject: str
    transparency_log_ref: str | None
    issued_at: str | None
    expires_at: str | None

@dataclass(frozen=True)
class PackageIdentity:
    kind: CapabilityKind
    namespace: str
    name: str
    version: str
    package_id: str
    content_digest: str
    manifest_digest: str
    publisher: PublisherIdentity
    provenance: ProvenanceStatement
    signatures: tuple[SignatureEvidence, ...]

@dataclass(frozen=True)
class RuntimeDeclaration:
    kind: RuntimeKind
    entrypoint: str
    abi: str
    isolation: Literal["high", "sandboxed", "instruction_only"]
    memory_limit_bytes: int
    cpu_time_ms: int
    wall_time_ms: int

@dataclass(frozen=True)
class NetworkDeclaration:
    scheme: Literal["https", "http", "none"]
    host: str
    port: int
    methods: tuple[str, ...]

@dataclass(frozen=True)
class FilesystemDeclaration:
    root: str
    modes: tuple[AccessMode, ...]
    follow_symlinks: bool

@dataclass(frozen=True)
class ProcessDeclaration:
    executable_digest: str
    argv_prefix: tuple[str, ...]
    allow_children: bool

@dataclass(frozen=True)
class SecretDeclaration:
    reference: str
    purpose: str
    inject_as: Literal["broker_handle", "env", "file", "header"]

@dataclass(frozen=True)
class ModelDeclaration:
    purpose: str
    allowed_models: tuple[str, ...]
    max_input_tokens: int
    max_output_tokens: int
    remote_allowed: bool

@dataclass(frozen=True)
class CapabilityManifest:
    schema_version: Literal["hermes.capability.v1"]
    package: PackageIdentity
    runtime: RuntimeDeclaration
    network: tuple[NetworkDeclaration, ...]
    filesystem: tuple[FilesystemDeclaration, ...]
    processes: tuple[ProcessDeclaration, ...]
    secrets: tuple[SecretDeclaration, ...]
    models: tuple[ModelDeclaration, ...]
    input_data_categories: tuple[DataCategory, ...]
    output_data_categories: tuple[DataCategory, ...]
    reproducible_version: str
    min_hermes_version: str

@dataclass(frozen=True)
class CapabilityGrant:
    grant_id: str
    profile_id: str
    package_id: str
    content_digest: str
    manifest_digest: str
    parent_grant_id: str | None
    authority_version: int
    authority_hash: str
    filesystem: tuple[FilesystemDeclaration, ...]
    network: tuple[NetworkDeclaration, ...]
    processes: tuple[ProcessDeclaration, ...]
    secret_references: tuple[str, ...]
    models: tuple[ModelDeclaration, ...]
    input_data_categories: tuple[DataCategory, ...]
    output_data_categories: tuple[DataCategory, ...]
    issued_at: str
    expires_at: str
    maximum_uses: int
    remaining_uses: int

@dataclass(frozen=True)
class CapabilityGrantSnapshot:
    package_id: str
    generation_id: str
    grant_ids: tuple[str, ...]
    snapshot_hash: str

class CapabilityGrantStore(Protocol):
    def issue(self, manifest: CapabilityManifest, grant: CapabilityGrant,
              decision: AuthorityDecision) -> CapabilityGrant: ...
    def attenuate(self, parent_grant_id: str, grant: CapabilityGrant,
                  decision: AuthorityDecision) -> CapabilityGrant: ...
    def get(self, grant_id: str) -> CapabilityGrant | None: ...
    def active_for(self, package_id: str, content_digest: str,
                   at: str) -> tuple[CapabilityGrant, ...]: ...
    def consume(self, grant_id: str, operation_key: str,
                expected_remaining_uses: int) -> CapabilityGrant: ...
    def revoke(self, grant_id: str, decision: AuthorityDecision,
               reason: str) -> None: ...
    def snapshot(self, package_id: str, generation_id: str) -> CapabilityGrantSnapshot: ...
    def restore(self, snapshot: CapabilityGrantSnapshot,
                decision: AuthorityDecision) -> tuple[CapabilityGrant, ...]: ...
```

`PackageIdentity.package_id` is `cap_<sha256(kind, namespace, name)>`; it is stable across versions. `content_digest` hashes a deterministic archive of all package bytes, normalized modes, and paths, excluding transport metadata. `manifest_digest` hashes the manifest with `package.manifest_digest`, `package.content_digest`, signatures, and transport locations omitted. Each `SignatureEvidence.signed_digest` must equal a domain-separated envelope over both content and manifest digests. `CapabilityGrant` scopes must be structural subsets of declarations and of a parent grant; wildcard hosts, path traversal, symlink following, arbitrary executables, raw secret values, and model aliases that resolve after issuance are rejected.

No runtime may accept a caller-supplied identity or grant. The coordinator installs a host-owned `CapabilityExecutionContext(package_id, generation_id, content_digest, grant_ids, authority_hash, operation_key)` in a `ContextVar`; final filesystem/network/process/secret/model and IFC gates consume that context immediately before the effect.

## Unified Lifecycle and Failure Semantics

The single lifecycle is `discover -> inspect -> quarantine -> grant -> install(staged) -> canary -> promote(active) -> update(quarantine/grant/canary) -> promote`, with `drift`, `lock`, `rollback`, `remove`, and `revoke` available from all non-removed states. `install` never means active. Promotion uses compare-and-swap over the expected active generation and grant snapshot; rollback is one journaled transaction that restores the previous active generation and its grant snapshot before the next process/conversation may see it.

| Operation | Durable result | Failure behavior |
|---|---|---|
| discover/inspect | normalized source candidate and untrusted metadata | no import, bootstrap, OAuth, or execution |
| quarantine | immutable object bytes + manifest + scan/verifier observations | malformed paths/digests/signatures become `blocked` |
| grant | item #6 authority-bound grant | missing/stale/overbroad authority is deny/ask, never implicit allow |
| install | staged generation and exact lock | crash replays by operation key; no partial active package |
| canary | isolated execution receipt | cannot use production grants or secrets unless separately canary-scoped |
| promote | active-generation pointer + grant snapshot | new conversations/processes only; CAS conflict leaves old active |
| update | quarantined candidate + declaration/permission/content drift | any increase requires replacement grant and canary |
| rollback | prior generation + prior grant snapshot | package-only or grant-only restoration is invalid and blocks activation |
| remove/revoke | disabled activation plus retained tombstone/receipts | running contexts fail their next mediated effect; bytes prune after retention |

Crash recovery distinguishes `failed` before effects, `blocked` by policy, and `unknown_effect` when an external sandbox/MCP boundary cannot prove disposition. It never automatically retries an unknown non-idempotent effect.

## Current-Code Audit and Exact File Map

### Existing seams to preserve

- `tools/skills_guard.py:632-766` already scans full skill trees, computes `sha256:` digests, caches exact scanner attestations, and returns provenance. Its scan result becomes one observation; its trust-level policy cannot authorize execution.
- `tools/skills_hub.py:3390-3690` already owns skill quarantine, path/symlink validation, `HubLockFile`, append-only audit, install move, and safe uninstall. It becomes a skill source/installer adapter while lock/audit truth moves to the canonical capability store.
- `hermes_cli/skills_hub.py:502-1150` already provides install, inspect, update, audit, and uninstall UX. It delegates managed-package operations to `CapabilityService` while preserving unmanaged built-in/local skill compatibility.
- `hermes_cli/plugins.py:281-330`, `hermes_cli/plugins.py:1248-1490`, and `hermes_cli/plugins.py:1658-1895` define `PluginManifest`, opt-in discovery, pip entry points, directory imports, and `register(ctx)`. Managed third-party plugins must be active-generation resolved and grant-checked before `_load_plugin()` imports bytes.
- `hermes_cli/plugins_cmd.py:449-676` installs/updates/removes user plugins, and `hermes_cli/plugins_cmd.py:814-915` owns explicit enable/tool-override UX. Managed packages route through the unified service; unmanaged directories retain explicit legacy behavior and are labeled unmanaged.
- `hermes_cli/mcp_catalog.py:59-132`, `hermes_cli/mcp_catalog.py:390-437`, and `hermes_cli/mcp_catalog.py:687-791` define catalog manifests, pinned Git refs, bootstrap, OAuth/config install, tool selection, and removal. A catalog pin is provenance input, not a content lock or grant.
- `tools/mcp_tool.py:409-449` builds credential-minimal subprocess environments, `tools/mcp_tool.py:513-560` scans descriptions, and `tools/mcp_tool.py:1672-1985` owns server lifecycle/dynamic tool registration. Managed MCP config resolves an active generation and brokered grant before connect or tool refresh.
- `agent/secret_scope.py:71-153` is the profile-scoped credential boundary. Capability runtimes receive broker handles or explicit declared injection only; they never enumerate the profile secret mapping.
- `tools/env_passthrough.py:91-176` and `tools/credential_files.py:57-186` mediate skill-requested environment/file credentials. Managed packages require matching manifest declarations and grants in addition to these existing blocklists.
- `tools/approval.py:1560-2021` binds approvals to normalized arguments/requester/channel/expiry and consumes exact pending approvals. Item #6 capability-grant issuance uses this identity rather than a parallel prompt queue.
- `tools/environments/base.py`, `tools/environments/local.py`, `tools/environments/docker.py`, `tools/environments/singularity.py`, `tools/environments/modal.py`, and `tools/environments/daytona.py` provide process backends. Capability runtimes wrap these backends with declared mounts, egress, environment, and process policy; local execution is never presented as high isolation.
- `hermes_cli/middleware.py`, item #15's final effective-argument gate, and `model_tools.py::handle_function_call()` remain the last in-process effect boundary. Capability enforcement composes with them and never lets plugin argument rewrites broaden a grant.
- `hermes_state.py` supplies profile-local SQLite/WAL and bounded `BEGIN IMMEDIATE` writes. Additive capability tables and lazy facades use this existing connection discipline.
- `tui_gateway/server.py:14370-14430` already serves structured skill operations, and `ui-tui/src/app/slash/commands/ops.ts` owns native operational commands. Add one `capability.exec` route rather than shelling out.
- `hermes_cli/web_server.py` and `web/src/App.tsx` provide profile-scoped Dashboard APIs/routes. Dashboard remains inspection-only; `apps/desktop/` is untouched.

### New production files

- `agent/capabilities/__init__.py` — frozen public exports and schema version.
- `agent/capabilities/models.py` — immutable manifest, identity, declaration, lifecycle, drift, operation, execution-context, and adapter values.
- `agent/capabilities/canonical.py` — canonical JSON, deterministic archive, path/origin normalization, content/manifest/package/grant hashes.
- `agent/capabilities/store.py` — profile-local package objects, generations, locks, lifecycle events, scans, verifier evidence, operations, tombstones, and audit queries.
- `agent/capabilities/sources.py` — `CapabilitySource` protocol/registry and skill, plugin-Git, pip-entrypoint, MCP-catalog, local-archive adapters.
- `agent/capabilities/verification.py` — scanner/verifier protocols, registry, declaration reconciliation, signature freshness/revocation, reproducibility checks, and service-gated providers.
- `agent/capabilities/runtimes.py` — `CapabilityRuntimeAdapter` registry, execution context, sandbox request/result, WASI preference/compatibility selection, and Python/MCP/skill adapters.
- `agent/capabilities/enforcement.py` — filesystem/network/process/secret/model grant checks and item #15 `FlowContext` construction at final boundaries.
- `agent/capabilities/service.py` — crash-safe unified lifecycle coordinator and CLI-facing service.
- `agent/autonomy/capability_grants.py` — item #6-owned `CapabilityGrantStore` implementation, subset checks, issue/attenuate/consume/revoke/snapshot/restore.
- `hermes_cli/capabilities.py` — shared parser, `run_argv()`, text/JSON renderers, confirmations, and service wiring.
- `skills/safe-capability-exchange/SKILL.md` — CLI-first discover/inspect/install/grant/update/canary/promote/rollback/remove/drift/lock guidance.
- `benchmarks/capability_exchange/manifest.yaml`, `cases.yaml`, `runner.py`, `score.py`, `README.md` — frozen 60-case proof and local reports.
- `benchmarks/capability_exchange/wrap_disk_cleanup.py` — deterministic wrapper of the existing real extension into v1/v2 packages without copying it into core.
- `website/docs/user-guide/features/safe-capability-exchange.md` — operator lifecycle, trust limits, recovery, and rollout.
- `website/docs/development/capability-package-contract.md` — standalone package/adapter author contract.
- `web/src/pages/CapabilitiesPage.tsx` — secondary read-only lifecycle/grant/receipt inspector.

### Existing production files modified

- `hermes_state.py` — additive capability/grant tables and lazy `SessionDB.capabilities`/`SessionDB.capability_grants` facades.
- `hermes_cli/config.py` — safe `capability_exchange` defaults and validation.
- `tools/skills_guard.py`, `tools/skills_hub.py`, `hermes_cli/skills_hub.py` — full canonical digest and managed-skill adapter/delegation.
- `hermes_cli/plugins.py`, `hermes_cli/plugins_cmd.py` — active-generation resolution, pre-import grant gate, managed lifecycle delegation, and unmanaged labeling.
- `hermes_cli/mcp_catalog.py`, `hermes_cli/mcp_config.py`, `tools/mcp_tool.py` — catalog/package adapter, bootstrap quarantine, grant-bound config/connect, fixed active-generation tool snapshots, and runtime mediation.
- `tools/environments/base.py`, `tools/environments/local.py`, `tools/environments/docker.py` — generic capability sandbox request and enforceable mount/env/network/process result metadata.
- `tools/file_tools.py`, `tools/terminal_tool.py`, `agent/secret_scope.py`, `agent/plugin_llm.py` — final filesystem/process/secret/model capability checks; existing protections still run.
- `agent/information_flow/runtime.py` — consume capability identity/grant data in final `FlowContext`; no second flow policy.
- `agent/receipt_ingest.py`, `agent/receipt_scoring.py` — lifecycle/execution evidence source and independent scorer.
- `hermes_cli/subcommands/capability.py`, `hermes_cli/main.py`, `hermes_cli/commands.py`, `hermes_cli/cli_commands_mixin.py`, `cli.py` — top-level `hermes capability` and classic `/capability` routes.
- `tui_gateway/server.py`, `ui-tui/src/gatewayTypes.ts`, `ui-tui/src/app/slash/commands/ops.ts` — native `capability.exec` and package-manager views.
- `hermes_cli/web_server.py`, `web/src/lib/api.ts`, `web/src/App.tsx` — authenticated read-only Dashboard endpoints/client/route.
- `website/docs/reference/cli-commands.md`, `website/docs/reference/slash-commands.md`, `website/sidebars.ts` — command/reference navigation.

### Focused tests

- `tests/agent/capabilities/test_models.py`, `test_canonical.py`, `test_store.py`, `test_sources.py`, `test_verification.py`, `test_grants.py`, `test_runtimes.py`, `test_enforcement.py`, `test_service.py`
- `tests/tools/test_skills_capability_adapter.py`, `test_plugin_capability_adapter.py`, `test_mcp_capability_adapter.py`, `test_capability_environment.py`
- `tests/hermes_cli/test_capability_cli.py`, `test_capability_e2e.py`, `test_capability_dashboard.py`
- `tests/tui_gateway/test_capability_rpc.py`, `tests/benchmarks/test_capability_exchange_benchmark.py`, `tests/integration/test_capability_exchange_e2e.py`
- `ui-tui/src/__tests__/capabilityCommand.test.ts`, `ui-tui/src/__tests__/slashParity.test.ts`, `web/src/pages/CapabilitiesPage.test.tsx`

---

### Task 0: Preregister the Exact 60-Case Proof

**Files:**
- Create: `benchmarks/capability_exchange/manifest.yaml`
- Create: `benchmarks/capability_exchange/cases.yaml`
- Create: `benchmarks/capability_exchange/README.md`
- Create: `tests/benchmarks/test_capability_exchange_benchmark.py`

**Interfaces:**
- Produces proof version `capability-exchange-60-v1`, exact case IDs/strata, baseline, metrics, gates, abort rules, and stop conditions consumed by Task 13.
- Consumes no production capability implementation.

- [ ] **Step 1: Write the failing fixture contract test**

```python
def test_capability_exchange_corpus_is_frozen(load_capability_exchange_fixtures):
    manifest, cases = load_capability_exchange_fixtures()
    counts = {name: sum(c["stratum"] == name for c in cases) for name in {
        "lifecycle", "isolation", "supply_chain", "crash_replay", "benign"
    }}
    assert manifest["version"] == "capability-exchange-60-v1"
    assert manifest["baseline"] == "current_separate_skill_plugin_mcp_workflows"
    assert counts == {"lifecycle": 12, "isolation": 20, "supply_chain": 12,
                      "crash_replay": 8, "benign": 8}
    assert len(cases) == len({c["id"] for c in cases}) == 60
    assert manifest["gates"] == {
        "undeclared_effects": 0,
        "complete_audit_chains": 60,
        "reproducible_rebuilds": 60,
        "benign_successes": 8,
        "rollback_package_and_grants": 4,
    }
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_capability_exchange_benchmark.py -v`

Expected: FAIL because the manifest/case fixtures and loader do not exist.

- [ ] **Step 3: Freeze all case meanings and scoring rules**

```yaml
version: capability-exchange-60-v1
baseline: current_separate_skill_plugin_mcp_workflows
extension_source: plugins/disk-cleanup
candidate_modes: [shadow, enforce]
strata: {lifecycle: 12, isolation: 20, supply_chain: 12, crash_replay: 8, benign: 8}
gates:
  undeclared_effects: 0
  complete_audit_chains: 60
  reproducible_rebuilds: 60
  benign_successes: 8
  rollback_package_and_grants: 4
stop_on:
  - undeclared_filesystem_network_process_secret_model_or_data_effect
  - permission_increase_inherits_old_grant
  - package_only_or_grant_only_rollback
  - signature_or_scan_treated_as_authority
  - audit_contains_secret_or_cannot_resolve_identity_and_grant
```

Freeze IDs `LIFE-01..12`, `ISO-01..20`, `SUP-01..12`, `RST-01..08`, and `OK-01..08`. The lifecycle cases cover signed lock/install, explicit grant, canary, promotion, permission-increase update, denial without replacement grant, replacement grant, promotion, package+grant rollback, drift, removal, and post-removal denial. Isolation cases contain four attempts each for undeclared filesystem traversal/symlink, network DNS/IP/redirect, process direct/child/shell/alternate binary, credential env/file/broker/enumeration, plus model/data-category remote/fallback/persistence/cross-profile access. Supply-chain cases cover digest mismatch, manifest substitution, stale/expired signature, revoked publisher, stale TUF-style metadata, rollback version, equivocation, non-reproducible build, malicious archive path, scan-cache mismatch, unknown publisher, and clean-scan/no-grant denial. Recovery cases inject crashes after object write, lock write, grant snapshot, canary receipt, active CAS, rollback pointer, revocation, and external unknown effect. Benign cases prove inspect, report generation, bounded filesystem read, denied-but-explained optional cleanup, canary, promoted execution in a new conversation, rollback execution, and clean removal.

- [ ] **Step 4: Run the fixture test and verify GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_capability_exchange_benchmark.py -v`

Expected: PASS with exactly 60 unique cases and the frozen gates.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/capability_exchange tests/benchmarks/test_capability_exchange_benchmark.py
git commit -m "test: freeze safe capability exchange proof"
```

### Task 1: Freeze Manifest, Identity, Digest, Drift, and Execution Types

**Files:**
- Create: `agent/capabilities/__init__.py`
- Create: `agent/capabilities/models.py`
- Create: `agent/capabilities/canonical.py`
- Create: `tests/agent/capabilities/test_models.py`
- Create: `tests/agent/capabilities/test_canonical.py`

**Interfaces:**
- Produces every exact public name in “Canonical Public Contract,” plus `PermissionDrift`, `CapabilityLock`, `CapabilityGeneration`, `CapabilityExecutionContext`, `CapabilityOperation`, `CapabilityAuditEvent`, `canonical_json()`, `manifest_digest()`, `archive_content_digest()`, `package_id()`, and `grant_id()`.
- Consumes item #6's `AuthorityDecision` type only for annotations; performs no I/O.

- [ ] **Step 1: Write failing immutable/canonicalization tests**

```python
def test_manifest_hash_binds_bytes_declarations_publisher_and_provenance(v1_manifest, v1_tree):
    first = build_manifest(v1_manifest, v1_tree)
    second = build_manifest(v1_manifest, v1_tree)
    assert first == second
    assert first.package.content_digest.startswith("sha256:")
    assert first.package.manifest_digest.startswith("sha256:")
    assert replace(first, network=(NetworkDeclaration("https", "api.example", 443, ("GET",)),)) != first

def test_grant_cannot_exceed_manifest_or_parent(v1_manifest, authority_allow):
    with pytest.raises(CapabilityContractError, match="network scope is not declared"):
        validate_grant(v1_manifest, grant(network=(NetworkDeclaration("https", "*", 443, ("GET",)),)), authority_allow)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_models.py tests/agent/capabilities/test_canonical.py -v`

Expected: FAIL importing `agent.capabilities`.

- [ ] **Step 3: Implement the frozen values and canonical hash rules**

```python
def manifest_digest(manifest: CapabilityManifest) -> str:
    payload = dataclass_dict(manifest)
    package = payload["package"]
    package["manifest_digest"] = ""
    package["content_digest"] = ""
    package["signatures"] = []
    return sha256_prefixed(canonical_json(payload))

def validate_grant(manifest: CapabilityManifest, grant: CapabilityGrant,
                   decision: AuthorityDecision,
                   parent: CapabilityGrant | None = None) -> None:
    require_exact_identity(manifest.package, grant)
    require_allow_decision(decision, action_class="capability.grant")
    require_structural_subset(grant, manifest)
    if parent is not None:
        require_structural_subset(grant, parent)
```

Archive hashing sorts normalized paths, rejects absolute/traversal/reserved/device names and links, records only regular-file/executable modes, and hashes `path NUL mode NUL length NUL bytes`. Origin normalization resolves no DNS; it lowercases IDNA hosts and requires an exact scheme/host/port/method set. Filesystem roots resolve at grant time against host-approved roots and retain both lexical and canonical identities.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_models.py tests/agent/capabilities/test_canonical.py -v`

Expected: PASS for immutability, round trips, deterministic archives, Unicode/path/origin rejection, digest binding, and grant subset rules.

- [ ] **Step 5: Commit**

```bash
git add agent/capabilities tests/agent/capabilities/test_models.py tests/agent/capabilities/test_canonical.py
git commit -m "feat: define capability package contracts"
```

### Task 2: Persist Content Objects, Generations, Locks, Journal, and Audit

**Files:**
- Create: `agent/capabilities/store.py`
- Modify: `hermes_state.py`
- Create: `tests/agent/capabilities/test_store.py`

**Interfaces:**
- Consumes Task 1 values and `SessionDB._execute_read/_execute_write`.
- Produces `CapabilityStore.put_object/get_object`, `record_candidate`, `transition`, `begin_operation`, `complete_operation`, `recover_operations`, `compare_and_swap_active`, `lock`, `detect_drift`, `append_audit`, `list_audit`, `tombstone`, and lazy `SessionDB.capabilities`.

- [ ] **Step 1: Write failing persistence, CAS, and replay tests**

```python
def test_active_generation_and_lock_are_atomic_and_replay_safe(temp_session_db, generation, grant_snapshot):
    store = CapabilityStore(temp_session_db)
    op = store.begin_operation("promote", "op-1", generation.package_id, generation.generation_id)
    store.compare_and_swap_active(op, expected_generation_id=None,
                                  generation=generation, grants=grant_snapshot)
    assert store.recover_operations()[0].disposition == "completed"
    assert store.compare_and_swap_active(op, None, generation, grant_snapshot).replayed
```

- [ ] **Step 2: Run the store test and verify RED**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_store.py -v`

Expected: FAIL because capability tables and `CapabilityStore` are absent.

- [ ] **Step 3: Add additive schema and transactional store behavior**

```sql
CREATE TABLE IF NOT EXISTS capability_generations (... UNIQUE(package_id, content_digest, manifest_digest));
CREATE TABLE IF NOT EXISTS capability_active (... PRIMARY KEY(package_id));
CREATE TABLE IF NOT EXISTS capability_locks (... PRIMARY KEY(package_id));
CREATE TABLE IF NOT EXISTS capability_operations (... UNIQUE(operation_key));
CREATE TABLE IF NOT EXISTS capability_audit (... UNIQUE(event_id));
CREATE TABLE IF NOT EXISTS capability_objects (... PRIMARY KEY(content_digest));
```

Object bytes live under `<HERMES_HOME>/capabilities/objects/sha256/<hex>` and are written temp-file + fsync + atomic rename before the database references them. Quarantine lives under `<HERMES_HOME>/capabilities/quarantine/<operation_key>`. `compare_and_swap_active()` writes active generation, lock, grant-snapshot hash, lifecycle event, and audit event in one `BEGIN IMMEDIATE`; replay returns the original result. Recovery completes provably durable projections, deletes unreferenced temp objects, and marks unprovable external effects `unknown_effect` without retry.

- [ ] **Step 4: Run the store and schema tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_store.py tests/test_hermes_state.py -v`

Expected: PASS for clean creation, restart, concurrent CAS, replay, corrupted lock rejection, orphan cleanup, and profile isolation.

- [ ] **Step 5: Commit**

```bash
git add agent/capabilities/store.py hermes_state.py tests/agent/capabilities/test_store.py
git commit -m "feat: persist capability generations and locks"
```

### Task 3: Add Source, Scan, Signature, Revocation, and Reproducibility Adapters

**Files:**
- Create: `agent/capabilities/sources.py`
- Create: `agent/capabilities/verification.py`
- Modify: `tools/skills_guard.py`
- Create: `tests/agent/capabilities/test_sources.py`
- Create: `tests/agent/capabilities/test_verification.py`

**Interfaces:**
- Produces `CapabilitySource.discover/inspect/fetch`, `CapabilitySourceRegistry`, `CapabilityScanner`, `CapabilityVerifier`, `register_capability_source()`, `register_capability_scanner()`, `register_capability_verifier(provider_id, factory, check_fn)`, `VerificationService.verify()`, and `ReproducibilityChecker.rebuild()`.
- Consumes Task 1 canonicalization and Task 2 quarantine/object store; adapts `scan_skill_cached()` without treating its verdict as authority.

- [ ] **Step 1: Write failing no-execution and stale-evidence tests**

```python
def test_inspect_and_verify_never_import_or_bootstrap(fake_source, import_spy, process_spy):
    candidate = CapabilitySourceRegistry([fake_source]).inspect("plugin:example")
    result = VerificationService(test_registries()).verify(candidate)
    assert result.signature_valid is True
    assert import_spy.calls == process_spy.calls == []

@pytest.mark.parametrize("fault", ["expired", "revoked", "wrong_digest", "stale_root", "equivocation"])
def test_verification_evidence_fails_closed(fault, signed_candidate):
    with pytest.raises(CapabilityVerificationError, match=fault):
        VerificationService(verifier_for(fault)).verify(signed_candidate)
```

- [ ] **Step 2: Run source/verifier tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_sources.py tests/agent/capabilities/test_verification.py -v`

Expected: FAIL importing source and verification registries.

- [ ] **Step 3: Implement generic adapters and service gates**

```python
class CapabilityVerifier(Protocol):
    provider_id: str
    def verify(self, identity: PackageIdentity, envelope: SignatureEvidence,
               *, now: datetime) -> VerificationObservation: ...
    def refresh_revocations(self, *, now: datetime) -> RevocationSnapshot: ...

def register_capability_verifier(provider_id: str, factory: Callable,
                                 check_fn: Callable[[dict], bool]) -> None:
    _VERIFIERS.register(provider_id, factory, check_fn)
```

Ship only `local_key` and `local_test` generic verifier code. Sigstore/TUF-style implementations register from standalone plugins and appear only when configured and `check_fn(config)` passes. Offline/unavailable optional verification yields an explicit observation according to policy (`required` blocks; `optional` remains unsigned/unverified) and never changes grants. Reproducibility runs in a clean sandbox with fixed locale/time/umask and demands the rebuilt digest equal the fetched digest. Full skill digest replaces the truncated legacy lock digest for managed skills; legacy fields remain readable.

- [ ] **Step 4: Run source/verifier tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_sources.py tests/agent/capabilities/test_verification.py tests/tools/test_skills_guard.py -v`

Expected: PASS for no-code inspection, source normalization, full digests, signature binding, expiry/revocation, scan cache binding, provider gating, and reproducible rebuild.

- [ ] **Step 5: Commit**

```bash
git add agent/capabilities/sources.py agent/capabilities/verification.py tools/skills_guard.py tests/agent/capabilities/test_sources.py tests/agent/capabilities/test_verification.py
git commit -m "feat: verify capability provenance and content"
```

### Task 4: Extend Item #6 with Explicit Attenuable Capability Grants

**Files:**
- Create: `agent/autonomy/capability_grants.py`
- Modify: `agent/autonomy/__init__.py`
- Modify: `agent/capabilities/__init__.py`
- Modify: `hermes_state.py`
- Create: `tests/agent/capabilities/test_grants.py`

**Interfaces:**
- Consumes Task 1 `CapabilityManifest`/`CapabilityGrant`, Task 2 store transaction helpers, and item #6 `AuthorityDecision`/`authorize_effect()`.
- Produces the frozen `CapabilityGrantStore` API, `StoredCapabilityGrantStore`, and `authorize_capability_grant(action_context, manifest, requested_grant) -> AuthorityDecision` owned and exported by `agent.autonomy`, then re-exported by `agent.capabilities` for package consumers.

- [ ] **Step 1: Write failing issuance, attenuation, stale-authority, and rollback tests**

```python
def test_grant_is_authority_and_digest_bound(grant_store, manifest, allow_decision):
    issued = grant_store.issue(manifest, exact_grant(manifest), allow_decision)
    assert issued.authority_hash == allow_decision.authority_hash
    with pytest.raises(CapabilityGrantError, match="stale authority"):
        grant_store.consume(issued.grant_id, "op-2", issued.remaining_uses)

def test_attenuation_can_only_remove_scope(grant_store, issued_grant, allow_decision):
    narrower = replace(issued_grant, network=(), parent_grant_id=issued_grant.grant_id)
    assert grant_store.attenuate(issued_grant.grant_id, narrower, allow_decision).network == ()
```

- [ ] **Step 2: Run grant tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_grants.py -v`

Expected: FAIL because item #6 has no capability grant store.

- [ ] **Step 3: Persist and enforce authority-owned grant semantics**

```python
def authorize_capability_grant(action_context: ActionContext,
                               manifest: CapabilityManifest,
                               requested_grant: CapabilityGrant) -> AuthorityDecision:
    context = replace(action_context, action_class="capability.grant",
                      resource_ids=(manifest.package.package_id,),
                      data_classes=requested_grant.input_data_categories)
    return authorize_effect(context)
```

Store immutable grant bodies plus append-only issuance/revocation/consumption rows. `issue()` verifies current allow, exact profile/package/digests, declaration subset, expiry, and nonzero bound. `consume()` reloads current authority and revocation state atomically; expired/revoked/stale authority or use mismatch blocks before effect. `restore()` is authorized as `capability.rollback_grants`, revokes candidate-generation grants, and reactivates only the exact prior snapshot with a new audit event; it never resurrects a separately user-revoked grant.

- [ ] **Step 4: Run grant/autonomy tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_grants.py tests/agent/autonomy -v`

Expected: PASS for issue, attenuation, expiry, single-use replay, revocation, cross-profile rejection, authority change, drift, and atomic snapshot restore.

- [ ] **Step 5: Commit**

```bash
git add agent/autonomy/capability_grants.py agent/autonomy/__init__.py agent/capabilities/__init__.py hermes_state.py tests/agent/capabilities/test_grants.py
git commit -m "feat: add authority-owned capability grants"
```

### Task 5: Mediate WASI, Python, MCP, Skill, and Host Effects

**Files:**
- Create: `agent/capabilities/runtimes.py`
- Create: `agent/capabilities/enforcement.py`
- Modify: `tools/environments/base.py`
- Modify: `tools/environments/local.py`
- Modify: `tools/environments/docker.py`
- Modify: `tools/file_tools.py`
- Modify: `tools/terminal_tool.py`
- Modify: `agent/secret_scope.py`
- Modify: `agent/plugin_llm.py`
- Modify: `agent/information_flow/runtime.py`
- Create: `tests/agent/capabilities/test_runtimes.py`
- Create: `tests/agent/capabilities/test_enforcement.py`
- Create: `tests/tools/test_capability_environment.py`

**Interfaces:**
- Consumes manifest/grants, item #15 `FlowContext`/`InformationFlowGuard`, existing environment backends, secret broker, and model facade.
- Produces `CapabilityRuntimeAdapter`, `CapabilityRuntimeRegistry`, `SandboxRequest`, `SandboxResult`, `CapabilityExecutionContext`, `capability_execution()`, `select_runtime()`, and final `enforce_filesystem/network/process/secret/model()` gates.

- [ ] **Step 1: Write failing adversarial boundary tests**

```python
@pytest.mark.parametrize("attempt", [
    "filesystem_traversal", "filesystem_symlink", "network_redirect",
    "network_ip_alias", "process_child", "alternate_executable",
    "secret_enumeration", "secret_env", "remote_model", "data_persist",
])
def test_undeclared_attempt_never_reaches_effect(attempt, runtime, effect_spies):
    result = runtime.execute(adversarial_request(attempt))
    assert result.status == "blocked"
    assert all(spy.calls == [] for spy in effect_spies)
    assert result.audit.package_id and result.audit.grant_ids
```

- [ ] **Step 2: Run runtime/enforcement tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_runtimes.py tests/agent/capabilities/test_enforcement.py tests/tools/test_capability_environment.py -v`

Expected: FAIL because runtime selection and final capability gates are absent.

- [ ] **Step 3: Implement runtime selection and final mediation**

```python
def select_runtime(manifest: CapabilityManifest, registry: CapabilityRuntimeRegistry) -> CapabilityRuntimeAdapter:
    if manifest.runtime.kind == "wasi_component" and registry.compatible("wasi", manifest.runtime.abi):
        return registry.require("wasi")
    adapter = registry.require(manifest.runtime.kind)
    if not adapter.can_enforce(manifest):
        raise CapabilityIsolationError("no compatible adapter can enforce every declaration")
    return adapter

def enforce_network(request: NetworkEffect, ctx: CapabilityExecutionContext) -> FlowDecision:
    grant = require_exact_active_grant(ctx)
    require_origin_and_method(request, grant.network)
    return information_flow_guard().evaluate(build_flow_context(request, ctx), consume_grants=True)
```

WASI adapters expose only declared component imports. Python adapters run out-of-process in a sandbox with a read-only object mount, declared mounts only, minimal environment, broker RPC handles, no ambient network, and bounded process tree. MCP stdio uses the same process sandbox; MCP HTTP goes through a host proxy enforcing exact origin/method/redirect/DNS re-resolution and OAuth handle scope. Skill instructions get a turn-scoped package context; all resulting effects still pass item #6, capability, item #15, and existing approval gates. Local backend reports `sandboxed`, never `high`, and is disallowed when declarations need enforceable egress/process isolation it cannot provide.

- [ ] **Step 4: Run runtime/enforcement tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_runtimes.py tests/agent/capabilities/test_enforcement.py tests/tools/test_capability_environment.py -v`

Expected: PASS with zero effect-spy calls for undeclared attempts, WASI preference when compatible, safe fallback/blocking, grant consumption, IFC blocks, secret handle isolation, and truthful isolation labels.

- [ ] **Step 5: Commit**

```bash
git add agent/capabilities/runtimes.py agent/capabilities/enforcement.py tools/environments/base.py tools/environments/local.py tools/environments/docker.py tools/file_tools.py tools/terminal_tool.py agent/secret_scope.py agent/plugin_llm.py agent/information_flow/runtime.py tests/agent/capabilities/test_runtimes.py tests/agent/capabilities/test_enforcement.py tests/tools/test_capability_environment.py
git commit -m "feat: enforce capability runtime isolation"
```

### Task 6: Implement the Crash-Safe Unified Lifecycle Service

**Files:**
- Create: `agent/capabilities/service.py`
- Modify: `hermes_cli/config.py`
- Create: `tests/agent/capabilities/test_service.py`

**Interfaces:**
- Consumes Tasks 1-5 source/store/verifier/grant/runtime APIs.
- Produces `CapabilityService.discover/inspect/quarantine/grant/install/canary/promote/update/rollback/remove/revoke/drift/lock/audit/recover`, `CapabilityCommandResult`, and `PermissionDrift` rendering data.

- [ ] **Step 1: Write failing state-machine and rollback tests**

```python
def test_permission_increase_requires_new_grant_and_rollback_restores_pair(service, v1, v2):
    active_v1 = service.install_grant_canary_promote(v1)
    candidate = service.update(v2, expected_active=active_v1.generation_id)
    assert candidate.permission_drift.network.added
    with pytest.raises(CapabilityGrantRequired):
        service.promote(candidate.generation_id)
    service.grant(candidate.package_id, replacement_grant(v2))
    service.canary(candidate.generation_id)
    service.promote(candidate.generation_id)
    rolled = service.rollback(candidate.package_id)
    assert (rolled.generation_id, rolled.grant_snapshot_hash) == active_v1.pair
```

- [ ] **Step 2: Run lifecycle tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_service.py -v`

Expected: FAIL because `CapabilityService` does not exist.

- [ ] **Step 3: Implement the state machine and recovery order**

```python
def promote(self, package_id: str, generation_id: str,
            expected_active_generation_id: str | None) -> CapabilityGeneration:
    generation = self.store.require_state(generation_id, "canary")
    self.verification.require_current(generation)
    grants = self.grants.active_for(package_id, generation.content_digest, self.clock.now())
    self.require_canary_receipt(generation, grants)
    return self.store.compare_and_swap_active(
        self.operation("promote", generation), expected_active_generation_id,
        generation, self.grants.snapshot(package_id, generation_id))
```

`update()` compares every declaration plus publisher/provenance/runtime/content/version. Any addition or semantic broadening sets `requires_new_grant=True`; reductions may attenuate but still canary. `canary()` uses disposable data and canary grants, blocks production secret handles by default, and records independent results. `recover()` revalidates digests/signatures/revocations/authority before completing a pending promotion; rollback recovery requires both active pointer and grant snapshot to match or disables the package.

- [ ] **Step 4: Run service tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_service.py -v`

Expected: PASS across every transition, invalid transition, CAS race, drift class, revocation, crash point, canary failure, rollback pair, tombstone, and retained receipt.

- [ ] **Step 5: Commit**

```bash
git add agent/capabilities/service.py hermes_cli/config.py tests/agent/capabilities/test_service.py
git commit -m "feat: coordinate capability package lifecycle"
```

### Task 7: Adapt Skills, Standalone Plugins, and MCP Without Parallel Trust Stores

**Files:**
- Modify: `tools/skills_hub.py`
- Modify: `hermes_cli/skills_hub.py`
- Modify: `hermes_cli/plugins.py`
- Modify: `hermes_cli/plugins_cmd.py`
- Modify: `hermes_cli/mcp_catalog.py`
- Modify: `hermes_cli/mcp_config.py`
- Modify: `tools/mcp_tool.py`
- Create: `tests/tools/test_skills_capability_adapter.py`
- Create: `tests/tools/test_plugin_capability_adapter.py`
- Create: `tests/tools/test_mcp_capability_adapter.py`

**Interfaces:**
- Consumes `CapabilityService` and active `CapabilityLock` resolution.
- Produces `SkillCapabilitySource`, `PluginCapabilitySource`, `McpCapabilitySource`, managed/unmanaged introspection, pre-load `resolve_active_capability()`, and pinned-generation MCP discovery.

- [ ] **Step 1: Write failing adapter and pre-import gate tests**

```python
def test_managed_plugin_is_not_imported_without_active_exact_grant(plugin_manager, import_spy, staged_plugin):
    plugin_manager.discover_and_load()
    assert import_spy.calls == []
    assert plugin_manager.get(staged_plugin.key).error == "managed capability is not active"

def test_mcp_tool_change_cannot_mutate_live_schema(active_conversation, updated_server):
    before = active_conversation.tool_schema_hash
    updated_server.emit_tools_list_changed()
    assert active_conversation.tool_schema_hash == before
```

- [ ] **Step 2: Run adapter tests and verify RED**

Run: `scripts/run_tests.sh tests/tools/test_skills_capability_adapter.py tests/tools/test_plugin_capability_adapter.py tests/tools/test_mcp_capability_adapter.py -v`

Expected: FAIL because existing loaders bypass canonical capability locks/grants.

- [ ] **Step 3: Route managed packages through exact active generations**

```python
def resolve_active_capability(kind: CapabilityKind, key: str) -> ActiveCapability | None:
    active = get_session_db().capabilities.active_by_key(kind, key)
    if active is None:
        return None
    active.verify_object_digest()
    active.require_current_grants()
    return active
```

Skills Hub keeps legacy unmanaged built-ins/local directories but marks them `unmanaged`; new remote installs use the unified service. Plugin discovery reads manifests from active object generations and performs the grant gate before `spec.loader.exec_module()` or entry-point `ep.load()`. Pinned pip packages record wheel/sdist digest and installed-distribution file digest; unresolved mutable entry points cannot be managed. MCP bootstrap occurs inside quarantine/sandbox, OAuth stores a broker reference, and active config names a generation rather than a mutable install directory. MCP `tools/list_changed` is recorded as drift for the next conversation and never alters a pinned live schema.

- [ ] **Step 4: Run adapter and regression tests and verify GREEN**

Run: `scripts/run_tests.sh tests/tools/test_skills_capability_adapter.py tests/tools/test_plugin_capability_adapter.py tests/tools/test_mcp_capability_adapter.py tests/tools/test_skills_hub.py tests/hermes_cli/test_plugins_cmd.py tests/hermes_cli/test_mcp_catalog.py -v`

Expected: PASS for managed exact-generation loading, legacy labeling, no pre-grant import/bootstrap/connect, OAuth handle scope, permission drift, and live schema stability.

- [ ] **Step 5: Commit**

```bash
git add tools/skills_hub.py hermes_cli/skills_hub.py hermes_cli/plugins.py hermes_cli/plugins_cmd.py hermes_cli/mcp_catalog.py hermes_cli/mcp_config.py tools/mcp_tool.py tests/tools/test_skills_capability_adapter.py tests/tools/test_plugin_capability_adapter.py tests/tools/test_mcp_capability_adapter.py
git commit -m "feat: unify skill plugin and mcp packages"
```

### Task 8: Deliver the Top-Level, Classic Slash, and Skill Workflow

**Files:**
- Create: `hermes_cli/capabilities.py`
- Create: `hermes_cli/subcommands/capability.py`
- Modify: `hermes_cli/main.py`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Modify: `cli.py`
- Create: `skills/safe-capability-exchange/SKILL.md`
- Create: `tests/hermes_cli/test_capability_cli.py`

**Interfaces:**
- Consumes `CapabilityService` only.
- Produces `build_parser(subparsers)`, `run_argv(argv: Sequence[str], *, output: Literal["text", "json"] = "text") -> CapabilityCommandResult`, top-level `hermes capability`, alias `hermes capabilities`, and classic `/capability`/`/capabilities`.

- [ ] **Step 1: Write failing parser and lifecycle UX tests**

```python
def test_cli_exposes_complete_lifecycle_and_permission_diff(run_capability_cli):
    assert run_capability_cli(["inspect", "plugin:disk-cleanup"]).code == 0
    diff = run_capability_cli(["update", "cap_disk_cleanup", "--check"], output="json").data
    assert diff["requires_new_grant"] is True
    assert diff["network"]["added"] == [{"scheme": "https", "host": "reports.example", "port": 443, "methods": ["POST"]}]
```

- [ ] **Step 2: Run CLI tests and verify RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_capability_cli.py -v`

Expected: FAIL because the command parser/routes do not exist.

- [ ] **Step 3: Implement one shared parser/service/rendering path**

```text
hermes capability discover <query>
hermes capability inspect <source-or-package>
hermes capability install <source> [--expected-digest sha256:...]
hermes capability grant <package> --from manifest|file --expires <rfc3339>
hermes capability canary <package>
hermes capability promote <package> --expected-generation <id>
hermes capability update <package> [--check]
hermes capability rollback <package> [--to <generation>]
hermes capability remove|revoke|drift|lock|audit|doctor <package>
```

Mutating commands show publisher/provenance/signature/scan facts separately from requested permissions, then bind confirmation to package/content/manifest/diff/authority hashes. `install` ends staged and prints the exact next `grant`/`canary` command. The skill instructs the agent to use the CLI, never edits state/lock/config directly, never describes a scan/signature as trust, and states that promotion affects new conversations only.

- [ ] **Step 4: Run CLI/slash/skill tests and verify GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_capability_cli.py tests/hermes_cli/test_commands_registry.py -v`

Expected: PASS for every subcommand, JSON/text parity, exact confirmations, aliases/help, stale identity rejection, and no model-tool registration.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/capabilities.py hermes_cli/subcommands/capability.py hermes_cli/main.py hermes_cli/commands.py hermes_cli/cli_commands_mixin.py cli.py skills/safe-capability-exchange/SKILL.md tests/hermes_cli/test_capability_cli.py
git commit -m "feat: add capability package manager cli"
```

### Task 9: Add Native Ink Package Management

**Files:**
- Modify: `tui_gateway/server.py`
- Modify: `ui-tui/src/gatewayTypes.ts`
- Modify: `ui-tui/src/app/slash/commands/ops.ts`
- Create: `tests/tui_gateway/test_capability_rpc.py`
- Create: `ui-tui/src/__tests__/capabilityCommand.test.ts`
- Modify: `ui-tui/src/__tests__/slashParity.test.ts`

**Interfaces:**
- Consumes `hermes_cli.capabilities.run_argv(..., output="json")`.
- Produces JSON-RPC `capability.exec` with `{argv: string[], session_id?: string}` and native list/detail/diff/confirmation/result views.

- [ ] **Step 1: Write failing RPC and Ink route tests**

```python
def test_capability_rpc_returns_structured_permission_diff(tui_rpc, seeded_candidate):
    result = tui_rpc("capability.exec", {"argv": ["drift", seeded_candidate.package_id]})
    assert result["kind"] == "permission_diff"
    assert result["requires_new_grant"] is True
```

- [ ] **Step 2: Run RPC/Ink tests and verify RED**

Run: `scripts/run_tests.sh tests/tui_gateway/test_capability_rpc.py -v && cd ui-tui && npm test -- --run src/__tests__/capabilityCommand.test.ts src/__tests__/slashParity.test.ts`

Expected: FAIL because `capability.exec` and the native command route are absent.

- [ ] **Step 3: Implement native structured views and exact confirmation binding**

```typescript
interface CapabilityExecResponse {
  ok: boolean
  kind: 'list' | 'detail' | 'permission_diff' | 'confirmation' | 'result' | 'error'
  packageId?: string
  generationId?: string
  contentDigest?: string
  manifestDigest?: string
  authorityHash?: string
  requiresNewGrant?: boolean
  sections?: CapabilitySection[]
}
```

Use the existing prompt overlay to confirm the exact hash tuple returned by the RPC; re-run the mutation with those expected hashes. The Ink route never invokes a shell subprocess or `slash.exec` fallback for capability commands. Long scan/canary work emits bounded progress events and remains cancel-safe.

- [ ] **Step 4: Run RPC/Ink tests and verify GREEN**

Run: `scripts/run_tests.sh tests/tui_gateway/test_capability_rpc.py -v && cd ui-tui && npm test -- --run src/__tests__/capabilityCommand.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS for native routing, structured diffs, stale confirmation rejection, cancellation, accessibility text, and slash parity.

- [ ] **Step 5: Commit**

```bash
git add tui_gateway/server.py ui-tui/src/gatewayTypes.ts ui-tui/src/app/slash/commands/ops.ts tests/tui_gateway/test_capability_rpc.py ui-tui/src/__tests__/capabilityCommand.test.ts ui-tui/src/__tests__/slashParity.test.ts
git commit -m "feat: manage capabilities in ink tui"
```

### Task 10: Connect Receipts and a Complete Identity-to-Grant Audit Chain

**Files:**
- Modify: `agent/receipt_ingest.py`
- Modify: `agent/receipt_scoring.py`
- Modify: `agent/capabilities/service.py`
- Create: `tests/agent/capabilities/test_receipts.py`

**Interfaces:**
- Consumes item #12 `ReceiptStore`, `ReceiptClaim`, `EvidenceDigest`, `ReceiptSourceKey`, scorer registry, capability operation/audit rows, item #6 decisions, and item #15 decisions.
- Produces `CapabilityLifecycleEvidenceSource.snapshot(operation_id)`, `CapabilityExecutionEvidenceSource.snapshot(operation_id)`, and `CapabilityEndStateScorer`; no receipt status/schema/store duplication.

- [ ] **Step 1: Write failing proof-chain and truthful-status tests**

```python
def test_effect_receipt_resolves_identity_grant_authority_flow_and_operation(receipt_issuer, executed_operation):
    receipt = receipt_issuer.issue(ReceiptSourceKey("external", executed_operation.operation_id))
    kinds = {claim.claim_kind for claim in receipt.claims}
    assert {"capability_identity", "capability_grant", "authority_decision",
            "flow_decision", "operation_effect"} <= kinds
    assert receipt.status != "verified" or receipt.scorer_id == "capability_end_state"
```

- [ ] **Step 2: Run receipt tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_receipts.py -v`

Expected: FAIL because capability evidence sources/scorer are absent.

- [ ] **Step 3: Add redacted immutable evidence and independent scoring**

```python
class CapabilityEndStateScorer(EndStateScorer):
    scorer_id = "capability_end_state"
    def decide(self, snapshot: EvidenceSnapshot) -> ReceiptDecision | VerifiedReceiptDecision:
        return verify_package_grant_flow_operation_chain(snapshot)
```

Every lifecycle/effect source records package/generation/content/manifest, publisher-evidence IDs, scan IDs, grant IDs and snapshot hash, authority decision/version/hash, flow decision/context/audit hash, sandbox identity/result, operation disposition, artifact digests, and uncertainty. Missing/stale/revoked/ambiguous links yield `completed_unverified`, `blocked`, `failed`, or `unknown_effect`; signatures and scans never affect status by themselves. Audit insertion failure blocks promotion and mediated effects because unexplained execution violates the approved proof.

- [ ] **Step 4: Run receipt and recheck tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/capabilities/test_receipts.py tests/agent/test_receipt_ingest.py tests/agent/test_receipt_scoring.py -v`

Expected: PASS for complete chain resolution, redaction, independent verification, immutable recheck observations, unknown effects, and audit failure blocking.

- [ ] **Step 5: Commit**

```bash
git add agent/receipt_ingest.py agent/receipt_scoring.py agent/capabilities/service.py tests/agent/capabilities/test_receipts.py
git commit -m "feat: issue capability lifecycle receipts"
```

### Task 11: Add Secondary Read-Only Dashboard Inspection

**Files:**
- Modify: `hermes_cli/web_server.py`
- Modify: `web/src/lib/api.ts`
- Create: `web/src/pages/CapabilitiesPage.tsx`
- Create: `web/src/pages/CapabilitiesPage.test.tsx`
- Modify: `web/src/App.tsx`
- Create: `tests/hermes_cli/test_capability_dashboard.py`

**Interfaces:**
- Consumes `CapabilityStore` read APIs and public redacted receipt summaries only.
- Produces authenticated profile-scoped `GET /api/capabilities`, `/api/capabilities/{package_id}`, `/generations`, `/audit`, and `/receipts`, plus `/capabilities` secondary inspector.

- [ ] **Step 1: Write failing read-only API/page tests**

```python
def test_dashboard_capability_api_is_profile_scoped_and_read_only(client, profile_a_package):
    detail = client.get(f"/api/capabilities/{profile_a_package.package_id}").json()
    assert detail["content_digest"] == profile_a_package.content_digest
    assert "secret_values" not in repr(detail)
    assert client.post(f"/api/capabilities/{profile_a_package.package_id}/promote").status_code == 405
```

- [ ] **Step 2: Run Dashboard tests and verify RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_capability_dashboard.py -v && cd web && npm test -- --run src/pages/CapabilitiesPage.test.tsx`

Expected: FAIL because endpoints/page do not exist.

- [ ] **Step 3: Implement redacted inspection only**

The page displays active/quarantined generation, publisher/signature/scan facts as separate evidence, declared versus granted permissions, drift, canary/receipt status, rollback lineage, revocation, and audit chain. It contains command hints such as `hermes capability grant ...` and `hermes capability rollback ...`; it has no mutation API. Headless `hermes serve` may expose authenticated reads, but Desktop imports no page/client/type and gains no parity dependency.

- [ ] **Step 4: Run Dashboard tests and verify GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_capability_dashboard.py -v && cd web && npm test -- --run src/pages/CapabilitiesPage.test.tsx src/lib/api.test.ts && npm run typecheck`

Expected: PASS for profile isolation, redaction, read-only routing, responsive states, truthful labels, and no Desktop references.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/web_server.py web/src/lib/api.ts web/src/pages/CapabilitiesPage.tsx web/src/pages/CapabilitiesPage.test.tsx web/src/App.tsx tests/hermes_cli/test_capability_dashboard.py
git commit -m "feat: inspect capabilities in dashboard"
```

### Task 12: Prove Cache, Schema, Role, Revocation, and New-Conversation Boundaries

**Files:**
- Create: `tests/integration/test_capability_exchange_e2e.py`
- Create: `tests/hermes_cli/test_capability_e2e.py`
- Modify: `tests/tools/test_mcp_capability_adapter.py`
- Modify: `tests/tools/test_plugin_capability_adapter.py`

**Interfaces:**
- Consumes the real CLI/service/store/loaders with temporary `HERMES_HOME` and injected external verifier/registry/sandbox boundaries.
- Produces no production interface; this is the release invariant gate.

- [ ] **Step 1: Write failing real-path invariant tests**

```python
def test_promotion_does_not_mutate_live_conversation(temp_hermes_home, live_agent, signed_package):
    before = snapshot_cache_identity(live_agent)
    promote_via_real_cli(signed_package)
    after = snapshot_cache_identity(live_agent)
    assert after == before
    assert_role_alternation(live_agent.messages)
    fresh = new_agent_for_same_profile()
    assert fresh.capability_generation != live_agent.capability_generation
```

- [ ] **Step 2: Run E2E invariant tests and verify RED**

Run: `scripts/run_tests.sh tests/integration/test_capability_exchange_e2e.py tests/hermes_cli/test_capability_e2e.py -v`

Expected: FAIL until active-generation pinning/recovery is complete end to end.

- [ ] **Step 3: Exercise real imports and all restart/security boundaries**

Use a temp profile with real `config.yaml`, `.env` canary, SQLite/WAL, package object files, quarantine, locks, skill scan, plugin discovery, MCP config/tool registry, local HTTP fixture, filesystem fixture, and fresh Python processes. Inject crashes and revocation between inspect/quarantine/grant/install/canary/promote/effect/rollback/remove. Assert byte-identical system prompt and tool schema plus same provider/model across every existing-conversation turn; assert changed capabilities appear only in a new conversation/cache lineage. Assert no same-role adjacency or synthetic user insertion.

- [ ] **Step 4: Run E2E invariant tests and verify GREEN**

Run: `scripts/run_tests.sh tests/integration/test_capability_exchange_e2e.py tests/hermes_cli/test_capability_e2e.py tests/tools/test_mcp_capability_adapter.py tests/tools/test_plugin_capability_adapter.py -v`

Expected: PASS for crash/replay, stale signatures, revocation, confused deputy, cross-profile access, live-schema pinning, new-conversation activation, prompt/tool/provider/model hashes, and role alternation.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_capability_exchange_e2e.py tests/hermes_cli/test_capability_e2e.py tests/tools/test_mcp_capability_adapter.py tests/tools/test_plugin_capability_adapter.py
git commit -m "test: prove capability exchange invariants"
```

### Task 13: Run the Real Disk-Cleanup Extension Proof and Score All 60 Cases

**Files:**
- Create: `benchmarks/capability_exchange/wrap_disk_cleanup.py`
- Create: `benchmarks/capability_exchange/runner.py`
- Create: `benchmarks/capability_exchange/score.py`
- Modify: `tests/integration/test_capability_exchange_e2e.py`
- Modify: `tests/benchmarks/test_capability_exchange_benchmark.py`

**Interfaces:**
- Consumes the frozen Task 0 corpus, real `plugins/disk-cleanup` source tree, public capability CLI/service, item #6/#15/#12 contracts, and fake external signature/registry/sandbox boundaries only.
- Produces `wrap_disk_cleanup(source: Path, version: Literal["v1", "v2"], output: Path) -> CapabilityManifest`, `run_capability_exchange_benchmark(manifest_path: Path, *, repeats: int, output: TextIO) -> CapabilityBenchmarkReport`, local `results.json`, and `report.md`.

- [ ] **Step 1: Write failing wrapper/runner/score tests**

```python
def test_real_extension_rebuild_update_canary_and_rollback(tmp_path, benchmark_runner):
    first = wrap_disk_cleanup(Path("plugins/disk-cleanup"), "v1", tmp_path / "a")
    second = wrap_disk_cleanup(Path("plugins/disk-cleanup"), "v1", tmp_path / "b")
    assert first.package.content_digest == second.package.content_digest
    report = benchmark_runner.run_all()
    assert report.total == 60
    assert report.undeclared_effects == 0
    assert report.complete_audit_chains == 60
    assert report.benign_successes == 8
```

- [ ] **Step 2: Run benchmark tests and verify RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_capability_exchange_benchmark.py tests/integration/test_capability_exchange_e2e.py -v`

Expected: FAIL because the deterministic wrapper, runner, and scorer are absent.

- [ ] **Step 3: Implement the exact realistic proof**

`v1` wraps the actual extension at `plugins/disk-cleanup` with bounded read-only workspace/cache roots, instruction/Python sandbox declarations required by its real reporting path, no network, one exact process executable if the real implementation needs it, no secret, and no remote model. `v2` uses the same real source plus a benchmark-only adapter revision declaring `POST https://reports.example:443`, thereby creating permission drift; it never sends outside the local HTTP fixture. Build twice in clean directories, local-test sign the exact digest envelope, lock/install/grant/canary/promote v1, update v2, prove old-grant denial, issue explicit v2 grant, canary/promote, then rollback and compare both generation/content digest and grant snapshot.

For every isolation case, verify OS/proxy/broker/model/IFC effect counters remain zero and read back the audit/receipt in a fresh process. For crash/replay, terminate at the named fault hook and recover with a new object graph/process. `CapabilityBenchmarkReport` includes Wilson 95% intervals for benign success/audit completeness, baseline/candidate p50/p95 latency, user prompts, recovery steps, local cost per verified success, exclusions/aborts retained in the denominator, OS/Python/SQLite/filesystem/sandbox classes, and separate safety results. It exits nonzero on any stop condition or unmet gate.

- [ ] **Step 4: Run the 60-case proof and verify GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_capability_exchange_benchmark.py tests/integration/test_capability_exchange_e2e.py -v && python -m benchmarks.capability_exchange.runner --manifest benchmarks/capability_exchange/manifest.yaml --repeats 3 --output .artifacts/capability-exchange`

Expected: PASS/exit 0 with exactly 60 cases, zero undeclared effects, 60 complete audit chains, 60 reproducible rebuild checks, 8 benign successes, all four rollback-pair checks, and local JSON/Markdown reports.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/capability_exchange tests/benchmarks/test_capability_exchange_benchmark.py tests/integration/test_capability_exchange_e2e.py
git commit -m "test: prove safe capability exchange end to end"
```

### Task 14: Document Operations, Compatibility, Rollout, and Stop Conditions

**Files:**
- Create: `website/docs/user-guide/features/safe-capability-exchange.md`
- Create: `website/docs/development/capability-package-contract.md`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`
- Modify: `website/sidebars.ts`
- Create: `tests/docs/test_capability_exchange_docs.py`

**Interfaces:**
- Consumes every proven public interface and the Task 13 report.
- Produces operator/author documentation, migration behavior, rollout stages, stop/rollback rules, and the completion matrix; no runtime API expansion.

- [ ] **Step 1: Write failing documentation contract tests**

```python
def test_capability_docs_state_security_and_rollout_contract(read_doc):
    user = read_doc("website/docs/user-guide/features/safe-capability-exchange.md")
    for phrase in ["signature is not trust", "scan is not trust", "permission drift",
                   "package and grants", "new conversation", "hermes capability rollback"]:
        assert phrase in user.lower()
```

- [ ] **Step 2: Run documentation tests and verify RED**

Run: `scripts/run_tests.sh tests/docs/test_capability_exchange_docs.py -v`

Expected: FAIL because capability exchange documentation is absent.

- [ ] **Step 3: Write operator, author, migration, and rollout guidance**

Document the canonical manifest with full skill/plugin/MCP examples; package/sign/inspect/install/grant/canary/promote/update/rollback/remove commands; verifier/scanner/runtime plugin registration; secret broker references; WASI preference and Python/MCP limitations; permission drift; revocation/offline behavior; lock repair; crash recovery; receipt/audit interpretation; profile isolation; new-conversation activation; and unmanaged legacy package labels.

Rollout is fixed:

1. `off`: legacy behavior; managed state is inspectable only.
2. `shadow`: produce manifests/drift/audit without claiming isolation or blocking legacy packages.
3. `enforce_new`: all new remote skill/plugin/MCP installs require the unified lifecycle; existing unmanaged packages are labeled and may be explicitly imported.
4. `enforce_all`: only after the 60-case gates pass on Linux plus one additional supported OS/sandbox class and migration rehearsal has zero package/grant loss.

Stop promotion and revert to the prior stage on any undeclared effect, credential/audit leak, permission inheritance, stale/revoked signature acceptance where verification is required, confused-deputy success, package/grant rollback mismatch, live-conversation schema mutation, normal-use success below 8/8, or unexplainable audit chain. A stop disables affected managed generations, preserves locks/objects/receipts for diagnosis, restores the last proven package+grant pair, and never silently re-enables unmanaged execution.

- [ ] **Step 4: Run docs and final verification matrix and verify GREEN**

Run: `scripts/run_tests.sh tests/docs/test_capability_exchange_docs.py tests/agent/capabilities tests/hermes_cli/test_capability_cli.py tests/hermes_cli/test_capability_e2e.py tests/tui_gateway/test_capability_rpc.py tests/benchmarks/test_capability_exchange_benchmark.py tests/integration/test_capability_exchange_e2e.py -v && cd ui-tui && npm test -- --run src/__tests__/capabilityCommand.test.ts src/__tests__/slashParity.test.ts && npm run typecheck && cd ../web && npm test -- --run src/pages/CapabilitiesPage.test.tsx src/lib/api.test.ts && npm run typecheck && npm run build`

Expected: all Python, Ink, Dashboard, docs, benchmark-structure, cache/schema/role, and real-path E2E gates PASS; no Desktop file changes and no model-visible tool-definition diff.

- [ ] **Step 5: Commit**

```bash
git add website/docs/user-guide/features/safe-capability-exchange.md website/docs/development/capability-package-contract.md website/docs/reference/cli-commands.md website/docs/reference/slash-commands.md website/sidebars.ts tests/docs/test_capability_exchange_docs.py
git commit -m "docs: publish safe capability exchange contract"
```

## Final Verification Matrix

| Requirement | Proof |
|---|---|
| Canonical manifest/identity/digests/declarations | Tasks 1 and 3 canonical, mutation, provenance, signature, and reproducibility tests |
| Explicit attenuable grants owned by #6 | Task 4 authority-bound store, subset, expiry, consumption, revocation, and snapshot restore |
| Flow enforcement owned by #15 | Task 5 final source-to-sink `FlowContext` checks and zero-effect adversarial counters |
| Receipts owned by #12 | Task 10 shared receipt source/scorer with immutable identity-to-grant chain |
| Unified skill/plugin/MCP lifecycle | Tasks 6-8 service, loader adapters, CLI/slash/skill workflow |
| Quarantine/lock/drift/canary/promote/rollback/remove | Tasks 2, 6, and 13 state/recovery and real extension proof |
| Signatures/scans never equal trust | Tasks 3, 4, 8, 10, 13, and documentation language tests |
| WASI preferred; Python/MCP sandboxed otherwise | Task 5 compatibility selection and truthful isolation tests |
| Undeclared filesystem/network/process/credential/model/data fail | Tasks 5, 12, and all 20 frozen isolation cases |
| Permission-increase update and package+grant rollback | Tasks 6 and 13 exact v1/v2 proof |
| Crash/replay/supply-chain/confused-deputy/staleness/revocation | Tasks 3, 4, 6, 12, and frozen `SUP`/`RST` strata |
| Temporary `HERMES_HOME` real-path E2E | Tasks 12 and 13 real config/SQLite/files/imports/processes |
| CLI/Ink primary, Dashboard secondary, no Desktop | Tasks 8, 9, 11, 14 |
| No core model tool; cache/schema/provider/model/role stable | Tasks 7, 8, 12, and final schema/hash assertions |
| Rung 2 core footprint; runtimes/capabilities at edges | Global constraints, Tasks 3, 5, 8, and author docs |
| Exact 90-day gate | Task 0 frozen 60 cases and Task 13 scored three-repeat run |

## Completion Gate

Do not call Safe Capability Exchange complete until fresh evidence proves all of the following:

- `agent.capabilities` exports the frozen `CapabilityManifest`, `CapabilityGrant`, `CapabilityGrantStore`, identity/declaration types, canonical digest functions, lifecycle service, and adapter protocols without competing public schemas.
- The real `plugins/disk-cleanup` source is wrapped, reproducibly built, locally signed for proof, locked, installed, explicitly granted, canaried, promoted, updated with visible network permission drift, denied under the old grant, re-granted, promoted, and rolled back with its exact prior grant snapshot.
- All 20 undeclared isolation cases and every supply-chain/confused-deputy/stale-signature/revocation/replay case fail before effect, with zero protected boundary calls.
- Every one of 60 cases has a redacted audit/receipt chain resolving package identity, content and manifest digests, publisher evidence, grant, authority decision, flow decision, operation, and disposition.
- All 60 rebuild checks reproduce content, all eight benign cases remain usable, and all four rollback checks restore package plus grants.
- Existing conversations retain byte-identical system prompt, effective tool schema, provider, and model; only new conversations/processes observe promotion. Message roles remain alternating.
- Existing skill, plugin, MCP, approval, sandbox, secret, SSRF, hardline, and IFC defenses remain active as stronger independent layers.
- CLI and native Ink support the full lifecycle; Dashboard is read-only; no `apps/desktop/` file or model-visible tool schema changes.
- Rollout remains at `shadow` until the frozen proof passes; `enforce_all` additionally requires a second supported OS/sandbox class and successful unmanaged-package migration rehearsal.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-safe-capability-exchange.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, with review between tasks.
2. **Inline Execution** — use `superpowers:executing-plans` in this session, executing in batches with checkpoints.
