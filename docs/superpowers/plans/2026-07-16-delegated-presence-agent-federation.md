# Delegated Presence & Agent Federation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Hermes delegate a bounded mission task to an independently operated agent under an explicit expiring user mandate, track/cancel/resume it safely, quarantine returned artifacts, and accept completion only after local evidence verification.

**Architecture:** Add a provider-neutral `agent.delegation_provider` contract and a profile-local `agent.federation` coordinator for remote identity, mandate binding, durable task/event state, authentication brokerage, artifact intake, and receipt projection. The existing local nested-delegation path becomes the first provider; A2A v1 remains an optional standalone plugin/service loaded only through the generic contract once multiple concrete consumers justify that surface. Item #1 owns mission intent, item #6 owns authority, item #12 owns receipt truth, item #15 owns information flow, and item #17 owns the installed provider's `CapabilityGrant` and isolation.

**Tech Stack:** Python 3.13, frozen dataclasses and `typing.Literal`, canonical JSON/SHA-256, SQLite/WAL through `SessionDB`, existing mission/operation journal and artifact catalog, `httpx` with existing URL-safety/redirect guards, OAuth 2.1 device authorization + PKCE and mTLS through credential brokers, provider entry points through the existing plugin loader, Rich/classic CLI, Ink/TypeScript JSON-RPC TUI, React Dashboard, pytest through `scripts/run_tests.sh`, Vitest, versioned YAML benchmark fixtures, and external A2A conformance implementations used only at the release-proof boundary.

## Global Constraints

- Work from a branch containing item #1 mission state, item #6 `AuthorityProvider`, `ActionContext`, `AuthorityDecision`, and `authorize_effect()`, item #12 `ReceiptStore` and its five statuses, item #15 `InformationFlowGuard`/`FlowContext`, and item #17 `CapabilityGrant`/`CapabilityGrantStore`. Missing prerequisites fail contract tests; this item creates no local substitutes.
- Every federated task carries a user-visible, immutable, expiring `DelegationMandate` binding exact remote identity, requested objective, input/data scope, artifact scope, allowed actions, cost/token/time budget, callback destinations, receipt requirements, tenant/profile, authority version/hash, and provider `CapabilityGrant`. There is no ambient, implied, learned, card-derived, or signature-derived delegation authority.
- Item #1 owns the parent mission objective, checkpoints, review, and terminal projection. Federation owns only a delegated remote task's protocol lifecycle and evidence adapter; it does not create a second mission graph.
- Item #6 exclusively decides whether a mandate may be issued, renewed, narrowed, resumed, or cancelled. Authority is reloaded before submit, callback acceptance, resume, artifact fetch, and any local effect derived from remote output.
- Item #15 labels every outgoing datum/artifact and incoming remote event/artifact outside model control. The final source-to-sink check uses the exact remote identity, tenant, purpose, callback, content digest, and mandate; an autonomy allow cannot override an IFC block.
- Item #12's `ReceiptStore` and exact statuses—`verified`, `completed_unverified`, `failed`, `blocked`, `unknown_effect`—are the only completion vocabulary. Remote text, task status, signed event, Agent Card, protocol conformance, or remote receipt is an untrusted claim until a local scorer verifies the requested end state and artifact digests.
- Item #17 owns acquisition, grant, isolation, update, revocation, and lock state for any external delegation-provider package. A provider executes only under an active exact `CapabilityGrant`; this plan does not add a second package trust model.
- Signed discovery proves key possession over exact bytes, not honesty, competence, permission, availability, tenant ownership, or output truth. Discovery performs no task submission, OAuth consent, callback registration, secret release, or capability grant.
- Federation is explicit opt-in and incubation-only. Stable non-secret settings live under `federation:` in profile-local `config.yaml`; OAuth tokens, client secrets, private keys, and mTLS key passwords remain in secret providers or `.env`.
- A2A v1 support ships only as a standalone plugin or standalone service at Footprint Ladder rung 4/5. No A2A SDK, vendor implementation, Agent Card catalog, third-party product, permanent core model tool, or new model-visible schema enters the core repository.
- Widen `PluginContext` with `register_delegation_provider()` only because the implementation has the built-in local provider and the external proof provider as concrete consumers. Do not add protocol-specific methods to the generic interface.
- Profiles and tenants are independent. Every mandate, auth handle, task, event cursor, callback nonce, artifact, receipt, and audit row resolves from `get_hermes_home()` and an exact tenant identity. Cross-profile or cross-tenant IDs return not-found without revealing existence.
- Remote task submission is an external effect journaled before dispatch with an idempotency key. An ambiguous timeout becomes `unknown_effect`; never resubmit until read-only reconciliation proves no task exists.
- Cancellation is best effort unless the provider returns authenticated terminal cancellation. Expiry blocks new/resume/fetch/effect work, requests cancellation, and reports residual remote uncertainty; it does not claim the remote process stopped.
- Callback destinations are exact HTTPS origins/paths or a profile-local polling channel. Redirects never broaden callback scope. Signed callback events still require nonce, task, mandate, tenant, sequence, freshness, and replay checks.
- Discovery and every HTTP hop are SSRF-hardened: HTTPS by default, no URL credentials, bounded DNS resolution, all resolved addresses public/allowed, private/link-local/loopback/metadata/Unix/file schemes blocked, redirect revalidation, response size/type/time limits, and DNS rebinding checks at connect.
- OAuth device authorization and authorization-code PKCE bind issuer, client, audience, scopes, redirect URI, state, nonce, mandate/provider identity, and tenant. mTLS binds configured certificate identity and server name. Tokens/certificates are broker handles, never model or remote-task payload fields.
- Streaming events and polling results pass through the same durable sequence/hash/replay validation. Event gaps trigger read-only reconciliation; they never silently advance terminal state.
- The system prompt, cached prefix, effective model tool definitions, primary provider, and primary model remain byte-stable for a conversation. Federation state stays in sidecar/SQLite and appears through CLI/TUI, not prompt mutation; strict role alternation and compression-only history mutation remain intact.
- Provider install/update or registration changes become available only in a new conversation/process cache lineage. Existing conversations retain their pinned provider/tool snapshot.
- Real-path E2E tests use a temporary `HERMES_HOME`, real imports, SQLite/WAL, local TLS/HTTP servers, real artifact files/hashes, real CLI parsers, real provider loading, fresh processes, and deterministic crash hooks. Mock only external IdP signing, external A2A implementations in focused tests, unavailable OS certificate stores, and process termination.
- No outbound telemetry is added. Incubation evidence is local, opt-in, redacted, and cannot mine private conversations/logs. Promotion requires at least three distinct repeated real user workflows, each intentionally delegated at least three times across at least two weeks with local verified receipts.
- The frozen technical proof denominator is exactly 60 cases: 12 interoperability, 12 lifecycle, 20 security, 8 crash/replay, and 8 benign/conformance. Pass requires both independent A2A implementations to complete scheduling negotiation and artifact handoff, zero scope/tenant/replay/SSRF violations, zero false `verified`, 60/60 audit chains, and 8/8 benign successes.

---

## Approved Portfolio Contract

**Layman outcome:** Hermes can safely represent the user to another trusted agent—for example, negotiate a schedule, exchange an artifact, or hand off a task—under signed and tightly limited authority.

**Design boundary:** A provider-neutral delegation contract carries a user-visible expiring mandate into an independently operated trust domain. Signed, SSRF-hardened discovery authenticates exact remote metadata but grants no permission. The coordinator negotiates a version, authenticates with OAuth device/PKCE or mTLS, journals submission, tracks streaming/polling lifecycle, supports cancellation/resume, validates tenant-bound callbacks, quarantines artifacts, and projects only locally verified evidence into the parent mission and canonical receipt. A2A v1 is an optional standalone plugin/service, never a core tool or bundled third-party implementation.

**90-day proof:** Interoperate with two independent A2A implementations on (1) a scheduling negotiation against synthetic designated calendars and (2) an artifact-producing handoff against disposable files/repositories. Repeat each implementation/workflow combination three times, then exercise cancel/resume, expiry, malicious Agent Cards, replay, data/artifact-scope violations, tenant confusion, unverifiable completion, partial streams, and crash recovery. Pass protocol conformance/security gates with zero false verification; remain in incubation until at least three distinct real user workflows recur at least three times each over two weeks with local verified receipts.

**Dependencies and failure conditions:** Items #1, #6, #12, #15, and #17 are hard prerequisites for broad use. Stop on any delegation without exact current authority, SSRF/private-network discovery, cross-tenant access, replay acceptance, scope leak, callback confusion, secret exposure, false `verified`, unjournaled external effect, or cache/schema mutation. A signed card proves key possession, not trustworthiness or competence.

**Delivery:** Footprint Ladder rung 4/5. Core gains only a generic provider contract, durable coordinator, CLI/Ink governance, and read-only inspection. A2A protocol support and other external federation transports remain standalone plugins/services or MCP edges; no permanent model-visible tool is introduced.

---

## Canonical Federation Contract — Frozen for Consumers

`agent.federation` is the public federation facade; `agent.delegation_provider` is the transport-neutral provider protocol. Canonical hashes use UTF-8 sorted-key compact JSON, NFC strings, UTC RFC 3339, finite integer minor units/tokens, lowercase IDNA hosts, exact ports/paths, and full `sha256:` digests.

```python
FEDERATION_SCHEMA_VERSION = "hermes.federation.v1"

RemoteTaskState = Literal[
    "proposed", "submitted", "working", "input_required", "cancel_requested",
    "cancelled", "completed", "failed", "expired", "unknown_effect"
]
AuthMethod = Literal["oauth_device_pkce", "oauth_code_pkce", "mtls", "none_local"]
ActionClass = Literal[
    "negotiate.schedule", "read.scoped_data", "produce.artifact",
    "send.callback", "request.input", "cancel.task"
]

@dataclass(frozen=True)
class RemoteAgentIdentity:
    agent_id: str
    card_url: str
    card_digest: str
    signature_issuer: str
    signature_subject: str
    key_thumbprint: str
    tenant_id: str
    trust_domain: str

@dataclass(frozen=True)
class DelegationBudget:
    currency: str | None
    maximum_minor_units: int
    maximum_input_tokens: int
    maximum_output_tokens: int
    maximum_wall_time_ms: int
    maximum_round_trips: int

@dataclass(frozen=True)
class DataScope:
    provenance_ids: tuple[str, ...]
    labels: tuple[FlowLabel, ...]
    allowed_fields: tuple[str, ...]
    content_digests: tuple[str, ...]

@dataclass(frozen=True)
class ArtifactScope:
    allowed_media_types: tuple[str, ...]
    maximum_files: int
    maximum_total_bytes: int
    required_names: tuple[str, ...]
    allowed_content_digests: tuple[str, ...]

@dataclass(frozen=True)
class CallbackDestination:
    callback_id: str
    url: str | None
    event_types: tuple[str, ...]
    public_key_thumbprint: str

@dataclass(frozen=True)
class ReceiptRequirement:
    required_claim_kinds: tuple[str, ...]
    required_artifact_names: tuple[str, ...]
    local_scorer_id: str
    fresh_until: str

@dataclass(frozen=True)
class DelegationMandate:
    mandate_id: str
    profile_id: str
    tenant_id: str
    mission_id: str
    requested_outcome: RequestedOutcome
    remote_identity: RemoteAgentIdentity
    provider_id: str
    provider_capability_grant_id: str
    data_scope: DataScope
    artifact_scope: ArtifactScope
    allowed_actions: tuple[ActionClass, ...]
    budget: DelegationBudget
    callbacks: tuple[CallbackDestination, ...]
    receipt_requirement: ReceiptRequirement
    authority_version: int
    authority_hash: str
    issued_at: str
    expires_at: str
    maximum_uses: int

@dataclass(frozen=True)
class DelegationRequest:
    request_id: str
    mandate: DelegationMandate
    objective_hash: str
    input_artifact_ids: tuple[str, ...]
    idempotency_key: str
    protocol_version: str

@dataclass(frozen=True)
class RemoteTaskHandle:
    provider_id: str
    remote_task_id: str
    mandate_id: str
    remote_identity: RemoteAgentIdentity
    protocol_version: str
    event_cursor: str | None

@dataclass(frozen=True)
class RemoteArtifactDescriptor:
    """Untrusted remote claim; it is never an ArtifactDigest until locally fetched and hashed."""
    remote_artifact_id: str
    name: str
    media_type: str
    claimed_size_bytes: int
    claimed_content_digest: str | None
    download_reference: str

@dataclass(frozen=True)
class RemoteTaskEvent:
    event_id: str
    remote_task_id: str
    sequence: int
    previous_event_hash: str | None
    state: RemoteTaskState
    payload_digest: str
    artifact_descriptors: tuple[RemoteArtifactDescriptor, ...]
    remote_claims: tuple[ReceiptClaim, ...]
    observed_at: str
    signature_ref: str | None
    event_hash: str

class DelegationProvider(Protocol):
    provider_id: str
    def discover(self, reference: str, policy: DiscoveryPolicy) -> RemoteAgentIdentity: ...
    def negotiate(self, identity: RemoteAgentIdentity,
                  supported_versions: tuple[str, ...]) -> NegotiatedProtocol: ...
    def submit(self, request: DelegationRequest,
               auth: AuthHandle) -> RemoteTaskHandle: ...
    def events(self, handle: RemoteTaskHandle,
               cursor: str | None) -> Iterator[RemoteTaskEvent]: ...
    def status(self, handle: RemoteTaskHandle) -> RemoteTaskEvent: ...
    def cancel(self, handle: RemoteTaskHandle, reason: str) -> RemoteTaskEvent: ...
    def resume(self, handle: RemoteTaskHandle, mandate: DelegationMandate,
               cursor: str | None) -> RemoteTaskHandle: ...
    def fetch_artifact(self, handle: RemoteTaskHandle,
                       descriptor: RemoteArtifactDescriptor) -> BinaryIO: ...
```

Every provider method receives or resolves the exact mandate even when it is not a positional argument; `FederationService` installs it in a host-owned execution context. Providers cannot mint/expand mandates, lower labels, choose callback destinations, alter identity, or claim local verification. `RemoteTaskEvent.remote_claims` remain untrusted evidence inputs. The local scorer alone may create `VerifiedReceiptDecision`.

## Lifecycle, Mandate, and Certainty Rules

The lifecycle is `discover -> inspect -> mandate preview -> user authorize -> negotiate/authenticate -> submit -> stream/poll -> input_required|working -> completed|failed|cancelled|expired|unknown_effect -> local verify -> mission projection`. Resume requires a new current authority decision and an unexpired mandate or an explicitly authorized replacement mandate whose scope is no broader.

| Boundary | Required binding | Failure result |
|---|---|---|
| discovery | URL policy, exact card bytes/signature/key evidence | untrusted candidate only; failure is `blocked` |
| mandate issuance | exact identity/objective/data/artifacts/actions/budget/callbacks/receipt/expiry | item #6 `ask`/`deny`; no remote call |
| provider execution | active item #17 `CapabilityGrant` + exact package digest | provider unavailable/blocked |
| submit/resume/cancel | current authority hash, mandate hash, idempotency key, tenant, auth audience | journaled failure or `unknown_effect` |
| outgoing data/artifact | item #15 `FlowContext` with remote sink/purpose | block before network write |
| callback/event | tenant/task/mandate/nonce/sequence/hash/signature/freshness | reject and append security audit |
| artifact fetch | declared descriptor + size/type/name + scope + safe quarantine | block/delete quarantine on mismatch |
| completion | local artifact/effect/end-state scorer | one of canonical five receipt statuses |

Remote submission/cancel may be idempotent only when the negotiated provider proves the exact idempotency contract. Otherwise a timeout after dispatch is `unknown_effect`; read-only status reconciliation precedes any retry. Remote callbacks cannot cause local mutations directly; they append validated events and wake the mission coordinator, which re-runs authority/IFC/transaction gates.

## Current-Code Audit and Exact File Map

### Existing seams to preserve

- `tools/delegate_tool.py:1788-2326` runs local child agents with streamed progress, file/tool attribution, timeout diagnostics, summaries, and cancellation; `tools/delegate_tool.py:2411-3070` owns local batch/background delegation and `/stop` integration. `LocalDelegationProvider` adapts this path without changing the existing `delegate_task` tool schema.
- `acp_adapter/server.py:1024-1221` provides session load/resume/cancel, and `acp_adapter/server.py:1308-1677` streams one Hermes run. ACP remains an editor protocol, not remote federation; its lifecycle patterns inform the provider contract.
- `mcp_serve.py:293-540` bridges durable events and `mcp_serve.py:543-977` exposes Hermes sessions over MCP. MCP serving remains a tool/resource surface, not peer authority; an external provider may use MCP only through the generic contract and mandate gates.
- `gateway/run.py:17220-17490` forwards gateway messages to a remote Hermes API with SSE streaming and generation checks. Proxy mode is whole-session relay, not per-task federation; federation reuses its cancellation/stale-stream lessons without sending conversation history.
- `tools/mcp_tool.py:910-1035` validates remote URLs and mTLS certificate configuration; `tools/mcp_tool.py:2517-2573` wires OAuth 2.1 PKCE and mTLS. Federation factors reusable auth/transport helpers rather than copying secrets or token storage.
- `tools/mcp_oauth.py` and `tools/mcp_oauth_manager.py` already provide profile-local token storage, PKCE, dynamic registration, and auth-provider caching. Add device-flow support through a generic broker extension, not protocol-specific token files.
- `gateway/platforms/base.py:398-880`, `tools/url_safety.py`, and `tools/website_policy.py` implement proxy DNS and SSRF/redirect checks. Signed-card discovery composes these and fails closed if the safety oracle is unavailable.
- `gateway/relay/auth.py`, `gateway/relay/transport.py`, and `gateway/relay/__init__.py` bind signed inbound delivery to per-tenant identities/routes. Federation uses the same principle but separate task/mandate/callback tables.
- `agent/operation_journal.py:45-336` already persists pending/running/dispatched/confirmed/failed/unknown/cancelled effects and never prunes unknown effects. Remote submit/cancel operations use it rather than a competing certainty state.
- Item #1 mission stores link workflow execution and mission review; federation projects one remote child task/evidence source and never duplicates parent orchestration.
- Item #12 `ArtifactCatalog.register_path/register_bytes/recheck` and `ReceiptStore.insert/append_observation` own artifact hashes and immutable proof. Remote artifact bytes enter bounded quarantine before catalog registration.
- Item #6 final-argument authority gate, item #15 final source-to-sink gate, and item #17 provider execution context remain stronger mandatory gates.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `hermes_cli/cli_commands_mixin.py`, `tui_gateway/server.py`, and `ui-tui/src/app/slash/commands/ops.ts` provide top-level/classic/native operational command patterns.
- `hermes_cli/web_server.py` and `web/src/App.tsx` provide authenticated profile-scoped read APIs/routes. Dashboard is secondary and read-only; `apps/desktop/` is untouched.

### New production files

- `agent/delegation_provider.py` — generic `DelegationProvider`, registry, auth/transport/provider result values, and local provider registration.
- `agent/federation/__init__.py` — stable public exports and schema version.
- `agent/federation/models.py` — frozen identity, mandate, scope, budget, request, task, event, protocol, audit, and demand values.
- `agent/federation/canonical.py` — canonical JSON, mandate/request/event/task/callback hashes and identifier normalization.
- `agent/federation/store.py` — profile/tenant-local identities, mandates, tasks, event chains/cursors, callback nonces, auth references, recovery, demand evidence, and audit.
- `agent/federation/discovery.py` — SSRF-hardened bounded card fetch, signature/key evidence verification, version/capability parsing, revocation/freshness, and no-authority result.
- `agent/federation/auth.py` — broker interfaces for OAuth device+PKCE, code+PKCE, mTLS, audience/scope/tenant binding, refresh/revoke, and opaque handles.
- `agent/federation/local_provider.py` — adapter over existing local nested delegation for a second concrete provider and contract testing.
- `agent/federation/artifacts.py` — bounded download/quarantine, descriptor/digest/media/name checks, malware/skill scan adapters, and canonical artifact registration.
- `agent/federation/service.py` — crash-safe mandate/task lifecycle, authority/IFC/capability/operation gates, streaming/poll reconciliation, cancel/resume/expiry.
- `agent/federation/receipts.py` — item #12 evidence source and local end-state scorer; no receipt schema.
- `hermes_cli/federation.py` — shared parser/service, text/JSON renderers, exact mandate confirmation, inspect/cancel/resume/reconcile commands.
- `hermes_cli/subcommands/federation.py` — top-level argparse registration.
- `skills/delegated-presence/SKILL.md` — CLI-first mandate/inspect/cancel/reconcile operating instructions.
- `benchmarks/federation/manifest.yaml`, `cases.yaml`, `runner.py`, `score.py`, `README.md` — frozen 60-case proof and external interoperability harness.
- `benchmarks/federation/fixtures/scheduling.json`, `artifact-handoff.json` — synthetic calendar and disposable artifact outcomes.
- `website/docs/user-guide/features/agent-federation.md` — incubation operator guide.
- `website/docs/development/delegation-provider-contract.md` — standalone provider/A2A adapter contract.
- `web/src/pages/FederationPage.tsx` — secondary read-only remote identity/task/mandate/receipt inspector.

### Existing production files modified

- `hermes_state.py` — additive federation tables and lazy facade.
- `hermes_cli/config.py` — `federation` stable settings/default-off validation.
- `hermes_cli/plugins.py` — generic `PluginContext.register_delegation_provider()` and attribution; no A2A import.
- `tools/delegate_tool.py` — internal local-provider adapter hooks while preserving model schema/behavior.
- `tools/mcp_oauth.py`, `tools/mcp_oauth_manager.py` — generic device-flow and audience/tenant-bound token broker support.
- `tools/url_safety.py`, `tools/website_policy.py` — reusable strict discovery/connect policy and DNS-rebinding result.
- `agent/operation_journal.py` — read-only reconciliation helpers for remote submit/cancel operation classes.
- `agent/information_flow/runtime.py` — trusted remote sink/source identities and mandate purpose binding.
- `agent/receipt_ingest.py`, `agent/receipt_scoring.py` — federation evidence source/scorer registration.
- `hermes_cli/missions_db.py` — remote task/evidence projection into existing mission review/terminal rules.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `hermes_cli/cli_commands_mixin.py`, `cli.py` — `federation`/`federate` top-level and slash routes.
- `tui_gateway/server.py`, `ui-tui/src/gatewayTypes.ts`, `ui-tui/src/app/slash/commands/ops.ts` — native `federation.exec`.
- `hermes_cli/web_server.py`, `web/src/lib/api.ts`, `web/src/App.tsx` — read-only Dashboard APIs/route.
- `website/docs/reference/cli-commands.md`, `website/docs/reference/slash-commands.md`, `website/sidebars.ts` — commands/navigation.

### Focused tests

- `tests/agent/federation/test_models.py`, `test_store.py`, `test_discovery.py`, `test_auth.py`, `test_local_provider.py`, `test_artifacts.py`, `test_service.py`, `test_receipts.py`, `test_security.py`
- `tests/hermes_cli/test_federation_cli.py`, `test_federation_e2e.py`, `test_federation_dashboard.py`
- `tests/tui_gateway/test_federation_rpc.py`, `tests/integration/test_federation_real_path.py`, `tests/benchmarks/test_federation_benchmark.py`
- `ui-tui/src/__tests__/federationCommand.test.ts`, `ui-tui/src/__tests__/slashParity.test.ts`, `web/src/pages/FederationPage.test.tsx`

---

### Task 0: Preregister the Exact 60-Case Incubation Proof

**Files:**
- Create: `benchmarks/federation/manifest.yaml`
- Create: `benchmarks/federation/cases.yaml`
- Create: `benchmarks/federation/README.md`
- Create: `tests/benchmarks/test_federation_benchmark.py`

**Interfaces:**
- Produces `federation-60-v1`, exact case IDs/strata, two external implementation slots, workflows, metrics, thresholds, safety floors, baseline, demand gate, and stop conditions consumed by Task 12.
- Consumes no production federation code.

- [ ] **Step 1: Write the failing corpus contract test**

```python
def test_federation_corpus_and_incubation_gate_are_frozen(load_federation_fixtures):
    manifest, cases = load_federation_fixtures()
    counts = Counter(case["stratum"] for case in cases)
    assert manifest["version"] == "federation-60-v1"
    assert manifest["baseline"] == "current_local_delegation_and_manual_remote_handoff"
    assert counts == {"interoperability": 12, "lifecycle": 12, "security": 20,
                      "crash_replay": 8, "benign_conformance": 8}
    assert len(cases) == len({case["id"] for case in cases}) == 60
    assert manifest["gates"]["false_verified"] == 0
    assert manifest["gates"]["scope_tenant_replay_ssrf_violations"] == 0
    assert manifest["advance_gate"] == {
        "minimum_distinct_repeated_real_user_workflows": 3,
        "minimum_verified_runs_per_workflow": 3,
        "minimum_observation_days": 14,
    }
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_federation_benchmark.py -v`

Expected: FAIL because the federation fixtures and loader do not exist.

- [ ] **Step 3: Freeze all exact cases and reporting rules**

```yaml
version: federation-60-v1
baseline: current_local_delegation_and_manual_remote_handoff
external_implementations: [implementation_one, implementation_two]
workflows: [scheduling_negotiation, artifact_handoff]
strata: {interoperability: 12, lifecycle: 12, security: 20, crash_replay: 8, benign_conformance: 8}
gates:
  false_verified: 0
  scope_tenant_replay_ssrf_violations: 0
  complete_audit_chains: 60
  benign_successes: 8
  implementation_workflow_pairs_passing: 4
advance_gate:
  minimum_distinct_repeated_real_user_workflows: 3
  minimum_verified_runs_per_workflow: 3
  minimum_observation_days: 14
```

Freeze IDs `INT-01..12` as both implementations × both workflows × three repeats; `LIFE-01..12` as negotiate, submit, stream, poll, input-required, cancel, cancelled, resume, expire, completed-unverified, locally verified, and provider unavailable; `SEC-01..20` as unsigned/wrong-key/expired/revoked/oversized/redirect/private-IP/DNS-rebind cards, OAuth state/audience/scope mismatch, mTLS identity mismatch, tenant confusion, callback nonce replay, event sequence replay, objective/data/artifact/action/budget scope violations, and unverifiable remote completion; `RST-01..08` as crashes before/after journal dispatch, remote acceptance, cursor persist, callback event, cancel dispatch, artifact quarantine, and receipt insertion; `OK-01..08` as discovery-only, version negotiation, scheduling stream, scheduling poll, artifact fetch, local verification, cancellation explanation, and expiry explanation. Aborts remain in the denominator. Report Wilson intervals, verified success, attention prompts, recovery burden, p50/p95 latency, cost per verified success, and safety slices separately.

- [ ] **Step 4: Run the fixture test and verify GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_federation_benchmark.py -v`

Expected: PASS with exactly 60 cases and the closed incubation advance gate.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/federation tests/benchmarks/test_federation_benchmark.py
git commit -m "test: freeze federation incubation proof"
```

### Task 1: Define the Generic Provider and Frozen Federation Values

**Files:**
- Create: `agent/delegation_provider.py`
- Create: `agent/federation/__init__.py`
- Create: `agent/federation/models.py`
- Create: `agent/federation/canonical.py`
- Create: `tests/agent/federation/test_models.py`

**Interfaces:**
- Produces every name/signature in “Canonical Federation Contract,” `DelegationProviderRegistry`, `DiscoveryPolicy`, `NegotiatedProtocol`, `AuthHandle`, `FederationAuditEvent`, `mandate_hash()`, `request_id()`, `event_hash()`, and strict constructors.
- Consumes shared `RequestedOutcome`, `ArtifactDigest`, `ReceiptClaim`, `FlowLabel`, `AuthorityDecision`, and `CapabilityGrant` types without I/O.

- [ ] **Step 1: Write failing immutability and complete-mandate tests**

```python
def test_mandate_requires_every_user_visible_bound(mandate_fields):
    for field in ["remote_identity", "requested_outcome", "data_scope", "artifact_scope",
                  "allowed_actions", "budget", "callbacks", "receipt_requirement",
                  "expires_at", "authority_hash", "provider_capability_grant_id"]:
        with pytest.raises(FederationContractError, match=field):
            DelegationMandate(**{**mandate_fields, field: None})

def test_remote_claims_cannot_construct_verified_decision(remote_event):
    with pytest.raises(TypeError):
        VerifiedReceiptDecision(scorer_id="remote")
```

- [ ] **Step 2: Run model tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/federation/test_models.py -v`

Expected: FAIL importing `agent.federation` and `agent.delegation_provider`.

- [ ] **Step 3: Implement immutable values, validation, and generic registry**

```python
class DelegationProviderRegistry:
    def register(self, provider_id: str, factory: Callable[[], DelegationProvider],
                 check_fn: Callable[[dict], bool], capability_package_id: str) -> None: ...
    def resolve(self, provider_id: str, config: dict,
                grant: CapabilityGrant) -> DelegationProvider: ...
```

Reject absent/high-risk unknown scope, non-exact remote identity, empty objective, unbounded budget/time/round trips, wildcard callback/action, expiry beyond configured maximum, tenant/profile mismatch, duplicate IDs, remote-created mandate IDs, and provider capability grant mismatch. Hashes bind every field. Provider registration remains internal/non-model-visible and service-gated.

- [ ] **Step 4: Run model tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/federation/test_models.py -v`

Expected: PASS for immutable round trips, canonical hashes, scope/budget/callback validation, provider registration gates, and remote-claim non-authority.

- [ ] **Step 5: Commit**

```bash
git add agent/delegation_provider.py agent/federation tests/agent/federation/test_models.py
git commit -m "feat: define federation provider contracts"
```

### Task 2: Persist Tenant-Isolated Mandates, Tasks, Events, Cursors, and Demand Evidence

**Files:**
- Create: `agent/federation/store.py`
- Modify: `hermes_state.py`
- Create: `tests/agent/federation/test_store.py`

**Interfaces:**
- Consumes Task 1 values and `SessionDB._execute_read/_execute_write`.
- Produces `FederationStore.put_identity/issue_mandate/get_mandate/create_task/transition/append_event/advance_cursor/consume_callback_nonce/record_auth_ref/record_demand/list_demand/recover/list_audit` and lazy `SessionDB.federation`.

- [ ] **Step 1: Write failing isolation, event-chain, and replay tests**

```python
def test_event_append_is_tenant_bound_hash_chained_and_idempotent(store, task, event):
    stored = store.append_event(task.tenant_id, event)
    assert store.append_event(task.tenant_id, event) == stored
    with pytest.raises(FederationReplayError):
        store.append_event(task.tenant_id, replace(event, sequence=event.sequence + 2))
    assert store.get_task("other-tenant", task.task_id) is None
```

- [ ] **Step 2: Run store tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/federation/test_store.py -v`

Expected: FAIL because federation tables/store are absent.

- [ ] **Step 3: Add additive tables and transactional invariants**

```sql
CREATE TABLE IF NOT EXISTS federation_mandates (... UNIQUE(profile_id, tenant_id, mandate_id));
CREATE TABLE IF NOT EXISTS federation_tasks (... UNIQUE(provider_id, remote_task_id));
CREATE TABLE IF NOT EXISTS federation_events (... UNIQUE(task_id, sequence), UNIQUE(event_hash));
CREATE TABLE IF NOT EXISTS federation_callbacks (... UNIQUE(callback_id, nonce_hash));
CREATE TABLE IF NOT EXISTS federation_audit (... UNIQUE(event_id));
CREATE TABLE IF NOT EXISTS federation_demand (... UNIQUE(workflow_fingerprint, receipt_id));
```

Mandates/events are immutable canonical JSON. Task state/cursor updates use compare-and-set. Duplicate identical event/request returns the prior row; divergent reuse is replay/conflict. Demand stores only user-approved workflow fingerprints, dates, receipt IDs, provider class, and redacted labels—never objectives/content. Recovery marks dispatched-without-status operations for read-only reconciliation and never retries unknown effects.

- [ ] **Step 4: Run store tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/federation/test_store.py tests/test_hermes_state.py -v`

Expected: PASS for clean migration, restart, CAS, event gaps, callback replay, profile/tenant isolation, expiry, and demand deduplication.

- [ ] **Step 5: Commit**

```bash
git add agent/federation/store.py hermes_state.py tests/agent/federation/test_store.py
git commit -m "feat: persist federation lifecycle state"
```

### Task 3: Implement Signed SSRF-Hardened Discovery and Version Negotiation

**Files:**
- Create: `agent/federation/discovery.py`
- Modify: `tools/url_safety.py`
- Modify: `tools/website_policy.py`
- Create: `tests/agent/federation/test_discovery.py`
- Create: `tests/agent/federation/test_security.py`

**Interfaces:**
- Consumes `DiscoveryPolicy`, URL-safety primitives, Task 1 identities, and Task 2 identity/audit store.
- Produces `AgentCardFetcher.fetch()`, `AgentCardVerifier.verify() -> DiscoveredAgent`, `VersionNegotiator.negotiate()`, and reusable `StrictRemoteUrlPolicy.resolve_and_connect()`.

- [ ] **Step 1: Write failing malicious-card and SSRF tests**

```python
@pytest.mark.parametrize("case", [
    "unsigned", "wrong_key", "expired", "revoked", "oversized", "redirect_private",
    "loopback", "link_local", "metadata", "dns_rebind", "url_credentials", "bad_type",
])
def test_discovery_blocks_before_auth_or_task(case, discovery, spies):
    with pytest.raises(DiscoveryBlocked):
        discovery.inspect(card_fixture(case))
    assert spies.oauth.calls == spies.submit.calls == []
```

- [ ] **Step 2: Run discovery/security tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/federation/test_discovery.py tests/agent/federation/test_security.py -v`

Expected: FAIL because strict card discovery is absent.

- [ ] **Step 3: Implement bounded fetch, exact signature evidence, and negotiation**

```python
def inspect(reference: str, policy: DiscoveryPolicy) -> DiscoveredAgent:
    response = StrictRemoteUrlPolicy(policy).get(reference, max_bytes=262_144,
                                                   content_types=("application/json",))
    card_digest = sha256_prefixed(response.body)
    evidence = verifier.verify_exact(response.body, response.headers, policy.trust_roots)
    return parse_card_without_authority(response.body, card_digest, evidence)
```

Revalidate every redirect and connected peer IP; require the original host/SNI after DNS; cache only exact card digest with freshness/revocation evidence. Negotiation intersects exact supported stable versions and security features; downgrade below policy or changing identity/card digest blocks. Inspection output explicitly says `permission: none`.

- [ ] **Step 4: Run discovery/security tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/federation/test_discovery.py tests/agent/federation/test_security.py tests/tools/test_url_safety.py -v`

Expected: PASS for all malicious cards, redirect/DNS rebinding, signature freshness/revocation, no auth side effects, and deterministic version negotiation/downgrade denial.

- [ ] **Step 5: Commit**

```bash
git add agent/federation/discovery.py tools/url_safety.py tools/website_policy.py tests/agent/federation/test_discovery.py tests/agent/federation/test_security.py
git commit -m "feat: harden federated agent discovery"
```

### Task 4: Broker OAuth Device/PKCE, Code/PKCE, and mTLS by Tenant and Mandate

**Files:**
- Create: `agent/federation/auth.py`
- Modify: `tools/mcp_oauth.py`
- Modify: `tools/mcp_oauth_manager.py`
- Create: `tests/agent/federation/test_auth.py`

**Interfaces:**
- Consumes negotiated auth requirements, profile secret scope, Task 2 auth-reference store, and existing MCP OAuth/mTLS helpers.
- Produces `FederationAuthBroker.begin_device/complete_device/begin_code/complete_code/get_handle/refresh/revoke`, opaque `AuthHandle`, and `MutualTlsIdentity`.

- [ ] **Step 1: Write failing audience/state/tenant/certificate tests**

```python
@pytest.mark.parametrize("mutation", ["state", "nonce", "issuer", "audience", "scope", "tenant", "redirect_uri"])
def test_oauth_binding_mismatch_never_yields_handle(mutation, auth_broker, flow):
    with pytest.raises(FederationAuthError, match=mutation):
        auth_broker.complete_code(flow.mutated(mutation))

def test_mtls_identity_must_match_card_and_tenant(auth_broker, wrong_tenant_cert):
    with pytest.raises(FederationAuthError, match="tenant"):
        auth_broker.get_handle(mtls_request(wrong_tenant_cert))
```

- [ ] **Step 2: Run auth tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/federation/test_auth.py -v`

Expected: FAIL because federation-bound auth brokerage is absent.

- [ ] **Step 3: Implement opaque, exact, revocable auth handles**

```python
@dataclass(frozen=True)
class AuthHandle:
    handle_id: str
    method: AuthMethod
    issuer: str
    audience: str
    scope_hash: str
    tenant_id: str
    remote_agent_id: str
    expires_at: str
```

Device verification URI passes strict URL policy. PKCE uses S256, high-entropy verifier/state/nonce, exact redirect binding, single-use completion, and issuer/audience validation. mTLS keys never leave secret storage; the broker returns an HTTP client factory bound to expected server identity. Refresh reloads current mandate/authority and cannot add scopes. Revoke deletes token material and records opaque evidence.

- [ ] **Step 4: Run auth tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/federation/test_auth.py tests/tools/test_mcp_oauth.py tests/tools/test_mcp_oauth_manager.py -v`

Expected: PASS for device/code PKCE, mTLS, refresh narrowing, revocation, expiry, cross-profile/tenant denial, and zero token serialization.

- [ ] **Step 5: Commit**

```bash
git add agent/federation/auth.py tools/mcp_oauth.py tools/mcp_oauth_manager.py tests/agent/federation/test_auth.py
git commit -m "feat: broker federation authentication"
```

### Task 5: Register Providers and Adapt Existing Local Delegation

**Files:**
- Create: `agent/federation/local_provider.py`
- Modify: `agent/delegation_provider.py`
- Modify: `hermes_cli/plugins.py`
- Modify: `tools/delegate_tool.py`
- Create: `tests/agent/federation/test_local_provider.py`
- Modify: `tests/hermes_cli/test_plugins.py`

**Interfaces:**
- Consumes Task 1 registry, item #17 active `CapabilityGrant`, and existing local child-agent lifecycle.
- Produces `LocalDelegationProvider`, `PluginContext.register_delegation_provider(provider_id, factory, check_fn)`, provider attribution/introspection, and no change to `delegate_task` schema.

- [ ] **Step 1: Write failing registration and local lifecycle tests**

```python
def test_provider_registration_requires_active_capability_grant(plugin_context, factory):
    with pytest.raises(DelegationProviderRegistrationError):
        plugin_context.register_delegation_provider("a2a", factory, lambda cfg: True)

def test_local_provider_stream_cancel_resume_uses_same_contract(local_provider, local_mandate):
    handle = local_provider.submit(request(local_mandate), local_auth_handle())
    assert [event.state for event in local_provider.events(handle, None)][0] == "working"
    assert local_provider.cancel(handle, "user").state == "cancelled"
```

- [ ] **Step 2: Run provider tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/federation/test_local_provider.py tests/hermes_cli/test_plugins.py -v`

Expected: FAIL because the generic provider registration/local adapter are absent.

- [ ] **Step 3: Implement the two-consumer generic surface**

```python
def register_delegation_provider(self, provider_id: str, factory: Callable,
                                 check_fn: Callable[[dict], bool]) -> None:
    grant = resolve_active_plugin_capability_grant(self.manifest)
    delegation_provider_registry.register(provider_id, factory, check_fn,
                                          grant.package_id)
```

Local adapter maps child progress/cancel/summary/file attribution into canonical events/artifacts for CLI-created mandates only. Existing model-invoked `delegate_task` continues its current behavior. External A2A adapters register from installed standalone packages; core tests use a minimal entry-point fixture solely to prove registration, not protocol behavior.

- [ ] **Step 4: Run provider/plugin regressions and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/federation/test_local_provider.py tests/hermes_cli/test_plugins.py tests/tools/test_delegate_tool.py -v`

Expected: PASS for registration gating, attribution, local lifecycle mapping, cancellation, provider failure isolation, and byte-identical `delegate_task` schema.

- [ ] **Step 5: Commit**

```bash
git add agent/federation/local_provider.py agent/delegation_provider.py hermes_cli/plugins.py tools/delegate_tool.py tests/agent/federation/test_local_provider.py tests/hermes_cli/test_plugins.py
git commit -m "feat: add delegation provider registry"
```

### Task 6: Quarantine and Verify Remote Artifacts

**Files:**
- Create: `agent/federation/artifacts.py`
- Create: `tests/agent/federation/test_artifacts.py`

**Interfaces:**
- Consumes item #12 `ArtifactCatalog`, Task 1 `ArtifactScope`, item #15 labels, strict HTTP policy, provider fetch method, and Task 2 audit/store.
- Produces `FederatedArtifactIntake.fetch_quarantine_verify_register() -> ArtifactDigest` and `FederatedArtifactIntake.recheck()`.

- [ ] **Step 1: Write failing descriptor/scope/race tests**

```python
@pytest.mark.parametrize("fault", ["oversize", "wrong_digest", "wrong_media", "wrong_name",
                                   "extra_file", "redirect_private", "archive_traversal", "symlink"])
def test_bad_remote_artifact_never_enters_catalog(fault, intake, catalog):
    with pytest.raises(FederatedArtifactError):
        intake.fetch_quarantine_verify_register(artifact_fixture(fault))
    assert catalog.list_all() == []
```

- [ ] **Step 2: Run artifact tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/federation/test_artifacts.py -v`

Expected: FAIL because federated intake is absent.

- [ ] **Step 3: Implement bounded streaming quarantine and local hashing**

Download to `<HERMES_HOME>/federation/quarantine/<task>/<artifact>` using no-follow exclusive creation, streaming byte/count limits, exact media/name checks, redirect/connect SSRF validation, and local SHA-256. Archives remain opaque unless the mandate explicitly requests extraction; extraction rejects links/devices/traversal and applies per-file/total limits. Register only locally hashed bytes. Remote descriptors/signatures remain provenance evidence, not truth.

- [ ] **Step 4: Run artifact/catalog tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/federation/test_artifacts.py tests/agent/test_receipt_artifacts.py -v`

Expected: PASS for bounded intake, quarantine cleanup, race/symlink defense, exact scope, local catalog registration, and read-only recheck.

- [ ] **Step 5: Commit**

```bash
git add agent/federation/artifacts.py tests/agent/federation/test_artifacts.py
git commit -m "feat: verify federated artifacts locally"
```

### Task 7: Coordinate Mandates, Streaming, Polling, Cancel, Resume, Expiry, and Recovery

**Files:**
- Create: `agent/federation/service.py`
- Modify: `agent/operation_journal.py`
- Modify: `agent/information_flow/runtime.py`
- Modify: `hermes_cli/config.py`
- Create: `tests/agent/federation/test_service.py`

**Interfaces:**
- Consumes Tasks 1-6, item #6 authority, item #15 IFC, item #17 provider grant, `OperationJournal`, and item #1 mission IDs.
- Produces `FederationService.inspect/preview_mandate/issue_mandate/submit/watch/status/cancel/resume/expire/reconcile/fetch_artifacts/recover` and `FederationExecutionContext`.

- [ ] **Step 1: Write failing stale-authority, replay, lifecycle, and unknown-effect tests**

```python
def test_submit_rechecks_every_shared_gate_before_network(service, mandate, spies):
    service.authority.rotate_after_preview()
    with pytest.raises(FederationBlocked, match="authority_changed"):
        service.submit(mandate.mandate_id)
    assert spies.network.calls == []

def test_dispatch_timeout_reconciles_before_retry(service, mandate, provider):
    provider.timeout_after_accept = True
    assert service.submit(mandate.mandate_id).state == "unknown_effect"
    service.reconcile(mandate.mandate_id)
    assert provider.submit_count == 1
```

- [ ] **Step 2: Run service tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/federation/test_service.py -v`

Expected: FAIL because the federation coordinator is absent.

- [ ] **Step 3: Implement ordered gates and durable lifecycle**

```python
def submit(self, mandate_id: str) -> FederatedTask:
    mandate = self.store.require_current_mandate(mandate_id)
    decision = self.authority.authorize(action_context(mandate, "federation.submit"), consume=True)
    grant = self.capability_grants.get(mandate.provider_capability_grant_id)
    if grant is None or grant not in self.capability_grants.active_for(mandate.provider_id):
        raise FederationBlocked("provider_capability_grant_inactive")
    flow = self.ifc.evaluate(outbound_flow_context(mandate, decision, grant), consume_grants=True)
    op = self.journal.begin("federation.submit", idempotency_key(mandate), mandate_hash(mandate))
    return self.dispatch_or_mark_unknown(op, mandate, decision, flow, grant)
```

Streaming and polling share `append_event`; gaps call authenticated `status()` without applying later events first. Input requests are displayed and require a narrowed/new mandate if they add data/action/budget. Cancel journals before dispatch and remains `cancel_requested` until authenticated status confirms. Resume requires current authority, exact identity/task/event cursor, and unexpired or replacement no-broader mandate. Expiry blocks all outward/fetch/effect actions, requests cancel, and truthfully retains unknown remote disposition. Provider callbacks only append events; all local effects use a transaction/authority/IFC recheck.

- [ ] **Step 4: Run service/operation/IFC tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/federation/test_service.py tests/agent/test_operation_journal.py tests/agent/information_flow/test_runtime.py -v`

Expected: PASS for submit/stream/poll/input/cancel/resume/expiry, authority/provider/flow staleness, callback replay, unknown reconciliation, crash recovery, and zero duplicate submissions.

- [ ] **Step 5: Commit**

```bash
git add agent/federation/service.py agent/operation_journal.py agent/information_flow/runtime.py hermes_cli/config.py tests/agent/federation/test_service.py
git commit -m "feat: coordinate federated task lifecycle"
```

### Task 8: Project Local Evidence, Receipts, and Mission State

**Files:**
- Create: `agent/federation/receipts.py`
- Modify: `agent/receipt_ingest.py`
- Modify: `agent/receipt_scoring.py`
- Modify: `hermes_cli/missions_db.py`
- Create: `tests/agent/federation/test_receipts.py`

**Interfaces:**
- Consumes item #12 receipt/artifact contracts, Task 7 task/event state, Task 6 local artifacts, and item #1 mission projection.
- Produces `FederationEvidenceSource.snapshot(task_id)`, `FederationEndStateScorer`, `FederationReceiptProjector.issue/recheck/project_to_mission`.

- [ ] **Step 1: Write failing false-completion and mission-projection tests**

```python
def test_remote_completed_and_signed_receipt_are_not_local_verification(projector, remote_completed):
    receipt = projector.issue(remote_completed.task_id)
    assert receipt.status == "completed_unverified"
    assert projector.mission(remote_completed.mission_id).verdict != "verified"

def test_local_artifact_and_schedule_checks_can_verify(projector, locally_proven):
    receipt = projector.issue(locally_proven.task_id)
    assert receipt.status == "verified"
    assert receipt.scorer_id == "federation_local_end_state"
```

- [ ] **Step 2: Run receipt tests and verify RED**

Run: `scripts/run_tests.sh tests/agent/federation/test_receipts.py -v`

Expected: FAIL because federation evidence/scorer/projection are absent.

- [ ] **Step 3: Build one immutable evidence chain and local scorers**

Evidence binds mandate/objective/identity/card/signature evidence, negotiated version/auth method, provider capability grant, authority/flow decisions, operation journal, every event hash/cursor, locally hashed artifacts, budget usage, cancellation/expiry, uncertainty, and callback audit. Scheduling scorer checks the synthetic calendar end state through a read-only local adapter; artifact scorer reopens/catalog-rechecks required files and validation commands. Remote claims/signatures never mint the sealed decision. Persist receipt before mission terminal projection; restart finds by source key and repairs projection idempotently.

- [ ] **Step 4: Run receipt/mission tests and verify GREEN**

Run: `scripts/run_tests.sh tests/agent/federation/test_receipts.py tests/agent/test_receipt_ingest.py tests/agent/test_receipt_scoring.py tests/hermes_cli/test_missions_db.py -v`

Expected: PASS for all five statuses, zero false verification, append-only recheck, artifact freshness, projection recovery, and remote-claim distrust.

- [ ] **Step 5: Commit**

```bash
git add agent/federation/receipts.py agent/receipt_ingest.py agent/receipt_scoring.py hermes_cli/missions_db.py tests/agent/federation/test_receipts.py
git commit -m "feat: verify federated task outcomes"
```

### Task 9: Deliver CLI, Classic Slash, Skill, and Native Ink Controls

**Files:**
- Create: `hermes_cli/federation.py`
- Create: `hermes_cli/subcommands/federation.py`
- Modify: `hermes_cli/main.py`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Modify: `cli.py`
- Create: `skills/delegated-presence/SKILL.md`
- Modify: `tui_gateway/server.py`
- Modify: `ui-tui/src/gatewayTypes.ts`
- Modify: `ui-tui/src/app/slash/commands/ops.ts`
- Create: `tests/hermes_cli/test_federation_cli.py`
- Create: `tests/tui_gateway/test_federation_rpc.py`
- Create: `ui-tui/src/__tests__/federationCommand.test.ts`
- Modify: `ui-tui/src/__tests__/slashParity.test.ts`

**Interfaces:**
- Consumes `FederationService` only.
- Produces `run_argv(argv, *, output="text") -> FederationCommandResult`, `hermes federation`/`hermes federate`, `/federation`/`/federate`, and JSON-RPC `federation.exec`.

- [ ] **Step 1: Write failing mandate visibility and control tests**

```python
def test_preview_shows_every_mandate_bound_before_submit(run_federation_cli):
    result = run_federation_cli(["mandate", "preview", "--request", "request.json"], output="json")
    assert set(result.data) >= {"remote_identity", "objective", "data_scope", "artifact_scope",
                                "allowed_actions", "budget", "callbacks", "receipt_requirement",
                                "expires_at", "authority_hash", "capability_grant_id"}
```

- [ ] **Step 2: Run CLI/RPC/Ink tests and verify RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_federation_cli.py tests/tui_gateway/test_federation_rpc.py -v && cd ui-tui && npm test -- --run src/__tests__/federationCommand.test.ts src/__tests__/slashParity.test.ts`

Expected: FAIL because federation commands/RPC/views are absent.

- [ ] **Step 3: Implement one shared primary workflow**

```text
hermes federation discover|inspect <agent-card-url>
hermes federation mandate preview|issue|show|revoke <request-or-id>
hermes federation submit|watch|status|cancel|resume|reconcile <task-or-mandate>
hermes federation artifacts|receipt|audit|doctor|demand <task-or-filter>
```

Exact confirmation binds mandate hash and displays every field in plain language. `submit` is separate from `mandate issue`. Cancel is always reachable while a watch is active and explains best-effort uncertainty. Ink calls `federation.exec` directly, renders event streams/progress and input-required panels, and never shells out or falls through `slash.exec`. The skill uses CLI only and never treats discovery/signature/remote completion as permission/truth.

- [ ] **Step 4: Run CLI/RPC/Ink tests and verify GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_federation_cli.py tests/tui_gateway/test_federation_rpc.py -v && cd ui-tui && npm test -- --run src/__tests__/federationCommand.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS for full mandate visibility, exact confirmation, inspect/submit/watch/cancel/resume/reconcile, structured errors, live cancel reachability, and no model-tool schema change.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/federation.py hermes_cli/subcommands/federation.py hermes_cli/main.py hermes_cli/commands.py hermes_cli/cli_commands_mixin.py cli.py skills/delegated-presence/SKILL.md tui_gateway/server.py ui-tui/src/gatewayTypes.ts ui-tui/src/app/slash/commands/ops.ts tests/hermes_cli/test_federation_cli.py tests/tui_gateway/test_federation_rpc.py ui-tui/src/__tests__/federationCommand.test.ts ui-tui/src/__tests__/slashParity.test.ts
git commit -m "feat: add federation mandate controls"
```

### Task 10: Add Secondary Read-Only Dashboard Inspection

**Files:**
- Modify: `hermes_cli/web_server.py`
- Modify: `web/src/lib/api.ts`
- Create: `web/src/pages/FederationPage.tsx`
- Create: `web/src/pages/FederationPage.test.tsx`
- Modify: `web/src/App.tsx`
- Create: `tests/hermes_cli/test_federation_dashboard.py`

**Interfaces:**
- Consumes redacted `FederationStore`/receipt queries only.
- Produces authenticated profile-scoped `GET /api/federation/agents`, `/mandates`, `/tasks`, `/tasks/{id}/events`, `/artifacts`, `/receipts`, `/demand`, and `/federation` inspector.

- [ ] **Step 1: Write failing read-only/tenant-redaction tests**

```python
def test_dashboard_is_read_only_redacted_and_tenant_scoped(client, task):
    detail = client.get(f"/api/federation/tasks/{task.task_id}").json()
    assert detail["mandate_id"] == task.mandate_id
    assert "token" not in repr(detail).lower()
    assert client.post(f"/api/federation/tasks/{task.task_id}/cancel").status_code == 405
```

- [ ] **Step 2: Run Dashboard tests and verify RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_federation_dashboard.py -v && cd web && npm test -- --run src/pages/FederationPage.test.tsx`

Expected: FAIL because federation endpoints/page are absent.

- [ ] **Step 3: Implement a secondary inspector only**

Show identity/signature facts separately from authority, exact mandate scope/budget/expiry, lifecycle/event gaps, auth method without tokens, artifacts/digests, receipt status/uncertainty, audit, and demand gate. All mutating controls are command hints to CLI/Ink. Cross-tenant IDs are indistinguishable from absent. No Desktop types, routes, or client calls are added.

- [ ] **Step 4: Run Dashboard tests and verify GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_federation_dashboard.py -v && cd web && npm test -- --run src/pages/FederationPage.test.tsx src/lib/api.test.ts && npm run typecheck`

Expected: PASS for read-only behavior, redaction, profile/tenant isolation, truthful uncertainty, responsive states, and no Desktop dependency.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/web_server.py web/src/lib/api.ts web/src/pages/FederationPage.tsx web/src/pages/FederationPage.test.tsx web/src/App.tsx tests/hermes_cli/test_federation_dashboard.py
git commit -m "feat: inspect federation in dashboard"
```

### Task 11: Prove Real-Path Recovery, Tenant Security, and Cache Invariants

**Files:**
- Create: `tests/integration/test_federation_real_path.py`
- Create: `tests/hermes_cli/test_federation_e2e.py`

**Interfaces:**
- Consumes public provider/service/CLI/store/receipt interfaces with injected external A2A/IdP/process boundaries only.
- Produces no production API; this is the release safety gate.

- [ ] **Step 1: Write failing temporary-profile E2E tests**

```python
def test_federation_restart_preserves_mandate_and_cache_identity(temp_hermes_home, live_agent, tls_peer):
    before = snapshot_cache_identity(live_agent)
    task = submit_real_cli_mandate(tls_peer)
    restart_gateway_and_reconcile(task)
    assert snapshot_cache_identity(live_agent) == before
    assert_role_alternation(live_agent.messages)
    assert read_receipt_in_fresh_process(task).source.source_kind == "external"
```

- [ ] **Step 2: Run real-path E2E tests and verify RED**

Run: `scripts/run_tests.sh tests/integration/test_federation_real_path.py tests/hermes_cli/test_federation_e2e.py -v`

Expected: FAIL until all real stores/transports/loaders/recovery paths are wired.

- [ ] **Step 3: Exercise the complete real path**

Use temp `HERMES_HOME`, real config/.env canaries, SQLite/WAL, plugin entry-point loading, local TLS card/task/callback servers, DNS/redirect safety resolver, OAuth device/code callbacks, generated mTLS certificates, real artifact files/catalog hashes, operation journal, mission projection, CLI and fresh subprocess restarts. Inject all eight `RST` crashes and malicious callbacks/cards/scopes. Hash system prompt, effective tools, provider, and model before/after every turn; assert no history mutation or same-role adjacency. Provider activation is visible only in a new conversation/process.

- [ ] **Step 4: Run real-path E2E tests and verify GREEN**

Run: `scripts/run_tests.sh tests/integration/test_federation_real_path.py tests/hermes_cli/test_federation_e2e.py -v`

Expected: PASS for crash/replay/reconciliation, OAuth/mTLS, tenant isolation, SSRF, scope enforcement, artifacts/receipts, new-conversation activation, cache/schema hashes, and role alternation.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_federation_real_path.py tests/hermes_cli/test_federation_e2e.py
git commit -m "test: prove federation real path"
```

### Task 12: Prove Two Independent A2A Implementations and Score All 60 Cases

**Files:**
- Create: `benchmarks/federation/fixtures/scheduling.json`
- Create: `benchmarks/federation/fixtures/artifact-handoff.json`
- Create: `benchmarks/federation/runner.py`
- Create: `benchmarks/federation/score.py`
- Modify: `tests/benchmarks/test_federation_benchmark.py`
- Modify: `tests/integration/test_federation_real_path.py`

**Interfaces:**
- Consumes frozen Task 0 corpus, public standalone-provider entry point, two externally launched independent A2A v1 implementations, and public federation CLI/service only.
- Produces `run_federation_benchmark(manifest_path, implementation_endpoints, *, repeats, output) -> FederationBenchmarkReport`, `results.json`, `report.md`, and signed local conformance metadata.

- [ ] **Step 1: Write failing runner/scorer contract tests**

```python
def test_report_requires_two_distinct_implementations_and_four_pairs(run_report):
    assert len({r.implementation_identity for r in run_report.interop_runs}) == 2
    assert {(r.implementation_identity, r.workflow) for r in run_report.interop_runs} == {
        ("implementation_one", "scheduling_negotiation"),
        ("implementation_one", "artifact_handoff"),
        ("implementation_two", "scheduling_negotiation"),
        ("implementation_two", "artifact_handoff"),
    }
    assert run_report.false_verified == 0
```

- [ ] **Step 2: Run benchmark tests and verify RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_federation_benchmark.py tests/integration/test_federation_real_path.py -v`

Expected: FAIL because runner/scorer/fixtures are absent.

- [ ] **Step 3: Implement conformance/security and real interoperability execution**

Runner requires two distinct implementation identities/build digests and refuses two endpoints backed by the same implementation. It uses synthetic calendars and disposable artifact workspaces only. Each implementation/workflow pair runs three times across streaming and polling where supported, version negotiation, OAuth device or code PKCE, mTLS when advertised, cancel/resume/expiry, malicious cards, replay, scope/tenant violation, artifact mismatch, and unverifiable completion. Focused CI may use protocol doubles; the release report is valid only when both external implementations identify independently and pass the official/provider conformance suite version recorded in the manifest.

Score exact denominator/exclusions, zero critical violations/false verified, 60 audit chains, 8 benign successes, four implementation/workflow pairs, event loss/duplication, cancel latency, resume success, verified success, user prompts, recovery burden, p50/p95 latency, and cost per verified success. Do not combine safety and utility. The demand gate is reported separately and remains closed with only the two benchmark workflows.

- [ ] **Step 4: Run the full proof and verify GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_federation_benchmark.py tests/integration/test_federation_real_path.py -v && python -m benchmarks.federation.runner --manifest benchmarks/federation/manifest.yaml --implementation-one https://127.0.0.1:9441 --implementation-two https://127.0.0.1:9442 --repeats 3 --output .artifacts/federation`

Expected: PASS/exit 0 with two distinct implementations, all four workflow pairs, exactly 60 cases, zero critical violations/false verified, 60 complete audit chains, 8 benign successes, and `advance_beyond_incubation=false` until real demand evidence reaches three workflows.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/federation tests/benchmarks/test_federation_benchmark.py tests/integration/test_federation_real_path.py
git commit -m "test: prove a2a federation interoperability"
```

### Task 13: Document Provider Contract, Operations, Rollback, and Incubation Stops

**Files:**
- Create: `website/docs/user-guide/features/agent-federation.md`
- Create: `website/docs/development/delegation-provider-contract.md`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`
- Modify: `website/sidebars.ts`
- Create: `tests/docs/test_federation_docs.py`

**Interfaces:**
- Consumes all proven contracts/reports from Tasks 0-12.
- Produces operator/provider documentation, migration/compatibility, rollback, stop conditions, and final completion matrix; no API expansion.

- [ ] **Step 1: Write failing documentation contract tests**

```python
def test_federation_docs_state_mandate_and_incubation_limits(read_doc):
    text = read_doc("website/docs/user-guide/features/agent-federation.md").lower()
    for phrase in ["signed card is not permission", "remote completion is untrusted",
                   "expiring mandate", "three repeated real user workflows",
                   "hermes federation cancel", "unknown_effect"]:
        assert phrase in text
```

- [ ] **Step 2: Run docs tests and verify RED**

Run: `scripts/run_tests.sh tests/docs/test_federation_docs.py -v`

Expected: FAIL because federation docs are absent.

- [ ] **Step 3: Write exact author/operator/rollback guidance**

Document every mandate field, discovery/signature limits, provider registration and item #17 grants, A2A standalone packaging, lifecycle/version negotiation, OAuth device/code PKCE, mTLS, callbacks/polling/streaming, cancel/resume/expiry uncertainty, artifact quarantine, receipt statuses, local verification, tenant/profile isolation, SSRF, replay, CLI/Ink workflows, Dashboard read-only scope, and no Desktop/model-tool commitment.

Rollout is fixed: `off` -> `local_contract_only` -> `external_shadow` -> `incubation_enforce`. There is no general-availability stage in this plan. External shadow sends only synthetic proof data. Incubation enforce requires the 60-case gates and explicit per-profile opt-in, but remains capped to allow-listed remote identities/workflows. Advancing beyond incubation requires three distinct repeated real user workflows, each at least three locally verified intentional runs over at least 14 days, plus a separate human design review.

Stop immediately on any scope/tenant/replay/SSRF violation, secret leak, false verified, unjournaled/duplicate external effect, ambiguous effect retry, malicious callback acceptance, package grant bypass, live schema/cache mutation, or inability to cancel/expire truthfully. Rollback disables the external provider in config, revokes its item #17 grants and auth handles, requests cancellation for active tasks, preserves evidence/artifacts/unknown states, returns missions to review, and keeps local delegation available.

- [ ] **Step 4: Run docs and final verification matrix and verify GREEN**

Run: `scripts/run_tests.sh tests/docs/test_federation_docs.py tests/agent/federation tests/hermes_cli/test_federation_cli.py tests/hermes_cli/test_federation_e2e.py tests/tui_gateway/test_federation_rpc.py tests/benchmarks/test_federation_benchmark.py tests/integration/test_federation_real_path.py -v && cd ui-tui && npm test -- --run src/__tests__/federationCommand.test.ts src/__tests__/slashParity.test.ts && npm run typecheck && cd ../web && npm test -- --run src/pages/FederationPage.test.tsx src/lib/api.test.ts && npm run typecheck && npm run build`

Expected: PASS for all Python, Ink, Dashboard, docs, real-path, conformance-structure, security, cache/schema/role, and receipt gates; no Desktop file or model-visible tool-definition change.

- [ ] **Step 5: Commit**

```bash
git add website/docs/user-guide/features/agent-federation.md website/docs/development/delegation-provider-contract.md website/docs/reference/cli-commands.md website/docs/reference/slash-commands.md website/sidebars.ts tests/docs/test_federation_docs.py
git commit -m "docs: publish federation incubation contract"
```

## Final Verification Matrix

| Requirement | Proof |
|---|---|
| Exact user-visible expiring mandate on every external task | Tasks 1, 2, 7, 9 and mandate hash/UI tests |
| Canonical #1 mission ownership | Task 8 projection without a second mission graph |
| Canonical #6 authority | Tasks 1 and 7 `AuthorityProvider`/`ActionContext` rechecks |
| Canonical #12 receipts/five statuses | Task 8 local scorer and false-completion tests |
| Canonical #15 flow enforcement | Task 7 exact remote source/sink `FlowContext` checks |
| Canonical #17 provider grant/isolation | Tasks 1, 5, 7 registration/runtime grant checks |
| Signed discovery never grants permission | Tasks 3, 7, 9, 13 |
| SSRF/malicious Agent Cards | Task 3 and `SEC-01..08` |
| Version negotiation/stream/poll/cancel/resume/expiry | Tasks 3, 7, 9, 12 |
| OAuth device+PKCE/code+PKCE/mTLS | Task 4 and external interop report |
| Tenant isolation/callback replay | Tasks 2, 4, 7, 11, 12 |
| Artifact scope/quarantine/local hashing | Task 6 and artifact handoff interop |
| Remote claims untrusted/local verification | Task 8 and zero false `verified` gate |
| Crash/replay/unknown-effect recovery | Tasks 2, 7, 11 and `RST-01..08` |
| Two independent A2A implementations | Task 12 four implementation/workflow pairs × three repeats |
| At least three recurring workflows before advancement | Tasks 0, 2, 12, 13 separate closed demand gate |
| CLI/Ink primary, Dashboard secondary, no Desktop | Tasks 9, 10, 13 |
| No permanent core tool/cache/schema/role mutation | Tasks 5, 9, 11 and final invariant tests |
| Footprint rung 4/5 | Global constraints, provider contract, standalone author docs |

## Completion Gate

Do not call Delegated Presence & Agent Federation complete until fresh evidence proves:

- Every submitted external task has an exact immutable current `DelegationMandate` containing remote identity, objective, data/artifact scope, actions, budget, callbacks, receipt requirement, expiry, authority hash, tenant/profile, and provider `CapabilityGrant`.
- Signed discovery is SSRF/redirect/DNS-rebinding hardened and produces no authority/auth/task side effect.
- Provider registration is generic, capability-gated, used by local delegation and an external proof provider, and adds no model-visible tool/schema.
- Both independent A2A implementations complete scheduling negotiation and artifact handoff three times each with recorded version/auth/conformance identities.
- Cancel/resume/expiry, malicious cards, replay, tenant/data/action/budget/artifact scope violations, partial streams, crashes, and unverifiable completion all yield the expected safe state with zero false `verified`.
- All returned artifacts are locally quarantined/hashed/rechecked; all remote claims remain untrusted until the local scorer seals a `VerifiedReceiptDecision`.
- All 60 cases have complete redacted identity/mandate/authority/capability/flow/operation/event/artifact/receipt audit chains.
- Existing conversations retain byte-identical system prompt, effective tool schema, provider, and model; strict role alternation holds; provider changes require a new conversation/process.
- CLI and native Ink own mandate/inspect/cancel/resume/reconcile controls; Dashboard is read-only; Desktop is untouched.
- The feature remains `incubation_enforce` or lower. Advancement is blocked until three distinct real user workflows each have at least three locally verified intentional runs across at least 14 days and a separate human review approves expansion.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-delegated-presence-agent-federation.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, with review between tasks.
2. **Inline Execution** — use `superpowers:executing-plans` in this session, executing in batches with checkpoints.
