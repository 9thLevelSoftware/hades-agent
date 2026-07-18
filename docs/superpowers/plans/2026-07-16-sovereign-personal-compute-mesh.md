# Sovereign Personal Compute Mesh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that one Hermes profile can securely hand durable work among two user-owned devices and one controlled remote node while compute follows authorized data, offline work resumes, conflicts remain deterministic and visible, revoked nodes receive no new work, and sensitive plaintext never leaves its authorized boundary.

**Architecture:** Build `hermes-sovereign-mesh` as a separately distributed Hermes plugin plus a headless node service, installed into the active profile rather than committed under Hermes core `plugins/`. The plugin consumes canonical mission, authority, receipt, information-flow, capability-grant, and Auto Routing contracts; a signed encrypted append-only envelope log replicates only selected mission/checkpoint/evidence metadata, while node-local workers use existing environment and local-model seams to execute beside files, credentials, or hardware. CLI and the existing Ink plugin-command path are primary; a plugin-owned Dashboard inspector is secondary; Desktop and the model-visible tool schema are untouched.

**Tech Stack:** Python `>=3.11,<3.14`, Hermes plugin entry points, Python frozen dataclasses and `typing.Literal`, SQLite/WAL, `cryptography==46.0.7`, `PyNaCl==1.5.0`/libsodium sealed boxes and XChaCha20-Poly1305, `cbor2==5.8.0`, BLAKE2b/SHA-256, `aiohttp==3.14.1`/WebSocket over TLS, existing `SessionDB`, `BaseEnvironment`, local-model runtime resolution, Rich/argparse, Ink JSON-RPC plugin-command fallthrough, FastAPI plugin Dashboard APIs, pytest through `scripts/run_tests.sh`, Vitest, packet/storage trace scanners, and Linux `tc`/Toxiproxy-equivalent deterministic fault fixtures.

## Global Constraints

- This is portfolio item #13 only: one user's trust domain, exactly two user devices plus one user-controlled remote node, durable mission/task handoff, placement, selective replication, recovery, revocation, and optional attestation. Multi-owner federation, delegated external agents, commerce, live sensing, general-purpose file sync, and fleet management are excluded.
- Delivery is Footprint Ladder rung 4, with a possible transport-only rung-5 service catalog entry after incubation. The implementation lives in the standalone `hermes-sovereign-mesh` distribution and profile install directory; it does not land as a third-party/vendor directory in Hermes core.
- Add no model-visible core tool, no MCP tool exposed to the conversation, and no tool-definition field. The agent uses existing mission/workflow/terminal capabilities; users govern the mesh through `hermes mesh` and `/mesh`.
- The system prompt, cached prefix, effective tool-definition snapshot, provider, and model remain byte-stable for a conversation. No mesh event injects a synthetic user message or rewrites history; a primary runtime change starts a new conversation lineage.
- Profiles are independent islands. All plugin config/state/locks/keys resolve from the active `get_hermes_home()` and `PluginContext.profile_name`; no live default-profile inheritance or cross-profile pairing exists.
- Stable non-secret settings live under `plugins.entries.sovereign-mesh` in profile-local `config.yaml`. Device private keys, recovery material, bootstrap tokens, and transport credentials live in an OS secret provider or a user-held encrypted recovery bundle; `.env` is credentials-only.
- Import `AuthorityProvider`, `StoredAuthorityProvider`, `ActionContext`, `AuthorityDecision`, and `authorize_effect()` from canonical `agent.autonomy`. Preview never authorizes commit; dispatch, grant, revocation, recovery, and compensation reload authority immediately before mutation.
- Import `ReceiptStore`, `ReceiptStatus`, `ReceiptSourceKey`, `Receipt`, `ReceiptObservation`, and `RECEIPT_STATUSES` from canonical `agent.receipts`. The only statuses are `verified`, `completed_unverified`, `failed`, `blocked`, and `unknown_effect`; mesh code cannot mint `verified` or define a sixth status.
- Import canonical item #15 `FlowContext` and its reference-monitor decision API. The mesh never defines a second label vocabulary, declassifier, or source-to-sink policy engine; sender and receiver both fail closed when the flow guard is absent, stale, or denies.
- Import canonical item #17 `CapabilityManifest` and `CapabilityGrant`. The mesh may add device observations to a manifest but cannot create, broaden, or silently renew grants; install/update/rollback/isolation remain owned by Capability Exchange.
- Use item #10 `AutoRoutingService.decide(RoutingRequest) -> RoutingDecision` for eligible runtime placement. Mesh code may construct hard node/data/hardware eligibility facts and honor an explicit user node pin, but it cannot implement a second scorer, learner, model inventory, fallback chain, or autonomous optimizer.
- Use item #1 mission/workflow execution and operation-journal semantics. A mesh work lease is at-least-once scheduling evidence, not exactly-once execution; stable logical operation keys and transaction reconciliation prevent duplicate external effects.
- Replication is minimal and explicit. Raw conversation history, system prompts, provider credentials, secret values, arbitrary home directories, full `state.db`, full `workflows.db`, memory databases, and environment snapshots are never replicated.
- Every envelope is signed, content-addressed, replay-bound, encrypted end to end to currently authorized recipients, and stored as ciphertext outside its authorized plaintext node. TLS is defense in depth, not the E2E confidentiality boundary.
- Revocation prevents new envelopes, new leases, and post-rotation decryption. Documentation must state that revocation cannot erase plaintext already legitimately received or recover a compromised private key.
- Optional attestation is a provider protocol implemented by standalone providers. No Apple, cloud, TPM vendor, or confidential-compute SDK becomes a Hermes core dependency; absence of required fresh evidence makes a node ineligible.
- Real-path tests use temporary profile homes, three real service processes, real SQLite/WAL, real sockets, real encryption/signatures, real temp files, and real process termination/partitions. Mock only optional attestation-provider network evidence and outward provider/model completion boundaries.
- No outbound telemetry. Benchmark and trace artifacts are local, redacted, content-addressed, and excluded from source control unless they contain only synthetic canaries.
- Each task starts with a focused failing test, records its expected RED failure, implements the smallest complete behavior, records GREEN plus relevant regressions, runs `git diff --check`, and ends in exactly one conventional commit in the repository that owns the changed files.

---

## Approved Portfolio Contract

**Layman outcome:** Hermes can securely move a task among the user's own devices and an optional attested remote machine so work runs where the required data, credentials, hardware, or privacy boundary exists.

**Design boundary:** Each paired `DeviceNode` has a cryptographic identity, user-approved capabilities, data-residency labels, availability/cost telemetry, and optional attestation evidence. Missions move as scoped task/evidence envelopes; sensitive raw context stays on its owning node whenever possible. The existing router sends computation to data rather than copying data to computation. Replicated state is minimal, encrypted, lineage-aware, and conflict-safe; key recovery and device revocation are first-class user flows. Attestation is an optional provider capability, not a vendor dependency.

**90-day proof:** Pair two user devices and one controlled remote node. Execute the frozen 12-workload corpus under four schedules for exactly 48 candidate mission runs: connected, data-owner offline before claim, worker crash after durable claim, and a bidirectional partition after result creation. Separately run 24 deterministic concurrent-lineage cases and 48 security cases covering grants/residency/IFC, replay/lease/late-result behavior, recovery/revocation/attestation, and plaintext sentinels in network/storage traces. Compare the 12 connected candidate runs with current Hermes using explicit local/SSH/environment selection and manual artifact transfer.

The proof passes only when all of these gates hold:

- at least 44/48 candidate missions end `verified`, all 48 receive one canonical receipt, and every non-verified run uses one of the other four canonical statuses with a visible reason;
- 100% of the recoverable offline/crash/partition runs resume without user reconstruction after connectivity/process restoration, with zero duplicate committed external effects and zero blindly retried `unknown_effect` operations;
- all 24 conflict cases produce byte-identical ordered lineage/conflict projections across 100 randomized delivery permutations, and every unresolved conflict is visible in CLI/Ink output;
- required file, credential, local-model, or GPU work is placed on an eligible node holding that resource; there are zero privacy, residency, authority, capability-grant, or IFC violations;
- a revoked node receives zero new accepted work/replication envelopes, cannot decrypt any post-rotation envelope, and cannot renew a lease; recovery and subsequent rotation restore the two active nodes without re-authorizing the revoked node;
- all 12 plaintext-sentinel trace cases find zero sentinel bytes outside authorized plaintext roots across packet captures, relay storage, SQLite/WAL, queues, temp directories, logs, crash dumps, exports, and the other nodes' storage;
- the candidate reports verified success, user attention minutes, recovery actions, cost, bytes transferred, p50/p95 handoff latency, p50/p95 resume latency, and cost per verified success against current Hermes; no critical safety floor is traded for convenience or cost.

**Dependencies and failure conditions:** Items #1, #6, #10, #12, #15, and #17 must expose their canonical contracts before active remote execution. Until then the plugin may run identity/crypto/transport tests and a no-execution shadow inventory, but it must fail closed with `dependency_unavailable`. Any plaintext leak, policy/grant/flow escape, implicit primary-model swap, cross-profile read, undetected conflict, accepted post-revocation work, duplicate effect, false `verified`, or requirement to disable normal local file/browser use stops rollout. A signature or attestation proves identity/posture evidence, not task truth.

**Delivery and incubation:** Ship as the standalone `hermes-sovereign-mesh` plugin/service, disabled by default. The first public artifact is a local installable wheel/source distribution with no Hermes-core plugin directory and no model tool; a transport catalog/MCP listing is considered only if it remains control-plane-only and does not expose a conversational tool. Advance from lab to opt-in preview only after the three-node proof passes; require three recurring user-owned workflows over 30 days before any broader compatibility commitment.

## Ownership Boundaries

| Concern | Canonical owner | Mesh responsibility |
|---|---|---|
| Durable objective, workflow state, task events, resume | #1 Missions | Reference mission/execution/checkpoint IDs and hand off a bounded execution unit; never create a second task graph. |
| User permission, residency preference, expiry, budget | #6 Autonomy | Build `ActionContext`, call `authorize_effect()` at preview and commit, persist only decision IDs/hashes. |
| Outcome truth and five statuses | #12 Receipts | Add mesh handoff/placement/trace claims and evidence, then call the scorer/store; never self-verify. |
| Data labels and source-to-sink enforcement | #15 IFC | Carry a `FlowContext` hash and encrypted label metadata, check at both boundaries, request explicit declassification through IFC. |
| Extension/runtime capabilities and grants | #17 Capability Exchange | Advertise signed node observations and consume exact `CapabilityGrant` IDs/revisions; never widen a grant. |
| Model/provider/runtime selection | #10 Adaptive Router | Supply eligible node-bound runtime targets and use `AutoRoutingService.decide`; no local ranking. |
| Device transport, E2E envelopes, workload lease, replication lineage | Mesh | Own these profile-local, user-trust-domain mechanisms only. |
| External-agent delegation | #19 Federation | Excluded; every node is controlled by the same user and enrolled under one mesh root. |

## Current-Code Audit

The strict inspection timebox found reusable seams, but none currently provides mesh trust or encrypted replication:

- `gateway/run.py::_get_proxy_url()` and `_run_agent_via_proxy()` forward chat/SSE to a remote API server and preserve platform delivery, but they send prompt/history material and use bearer/TLS transport rather than device identity, E2E selective replication, grants, IFC, or revocation. The mesh must not relabel this path as private E2E compute.
- `gateway/platforms/api_server.py::_handle_create_session()`, `_handle_session_chat()`, `_handle_session_chat_stream()`, `_handle_fork_session()`, and the `/api/sessions` family provide authenticated client-safe session resources. They are useful compatibility references; the proof sends bounded mesh work envelopes, not whole session history.
- `hermes_state.py::SessionDB.claim_session_lease()`, `touch_session_lease()`, `release_session_lease()`, and `reconcile_expired_session_leases()` are conservative process/session leases. Mesh workload leases use the same end-only/owner-checked principles in plugin storage; they do not overload or weaken session leases.
- `apps/shared/src/json-rpc-gateway.ts::JsonRpcGatewayClient` supplies request IDs, timeouts, cancellation, reconnect cleanup, and event dispatch for Dashboard/Desktop clients. The mesh does not fork this client; Ink uses existing `slash.exec`/`command.dispatch`, and an optional Dashboard plugin may use the shared client only for existing gateway events.
- `tui_gateway/server.py::slash.exec` already detects typed plugin commands and dispatches their handlers without the slash-worker subprocess. `commands.catalog` currently catalogs built-ins, quick commands, and skills rather than plugin commands, so the proof promises typed `/mesh` execution plus the packaged `mesh-control` skill for discovery—not unimplemented palette parity and not a new JSON-RPC method.
- `tools/environments/base.py::BaseEnvironment.execute()` and the local, Docker, SSH, Modal, Daytona, and Singularity adapters provide controlled execution backends. `tools/environments/file_sync.py::FileSyncManager` is deliberately not the replication primitive because its broad file-copy semantics cannot prove selective E2E residency.
- `hermes_cli/runtime_provider.py`, `hermes_cli/model_switch.py`, and `hermes_cli/models.py` already resolve custom/local endpoints and LM Studio/local-model inventory. Nodes advertise these existing runtimes; the mesh neither installs models nor invents provider resolution.
- `hermes_cli/plugins.py::PluginContext.register_cli_command()` and `register_command()` provide top-level and in-session controls, `PluginContext.profile_name` identifies the active profile, and entry-point discovery supports a standalone distribution.
- `hermes_constants.py::get_hermes_home()` plus context-local overrides enforce profile-local paths. `agent.secret_scope.get_secret()` and `build_profile_secret_scope()` provide fail-closed multiplexed secret resolution; secrets are never copied into envelopes or child environments.
- `tools/environments/local.py::_sanitize_subprocess_env()` and `hermes_subprocess_env()` provide credential-stripped subprocess construction. Node workers start from the stripped environment and receive only exact granted secret handles resolved locally.
- `hermes_cli/web_server.py::_discover_dashboard_plugins()` and `_mount_plugin_api_routes()` can mount a standalone plugin's optional static UI/API under `/api/plugins/<name>` after the primary CLI/Ink proof passes.

## Proposed File Map

### Standalone distribution `hermes-sovereign-mesh/` (separate repository/package)

- `pyproject.toml` — package metadata, exact compatible Hermes range, direct crypto/transport pins, `hermes.plugins` entry point, console service entry point, and no core-tool entry point.
- `src/hermes_sovereign_mesh/plugin.py` — `register(ctx: PluginContext)`, `hermes mesh`, `/mesh`, dependency doctor, and lifecycle wiring only.
- `src/hermes_sovereign_mesh/models.py` — frozen public mesh records and finite vocabularies.
- `src/hermes_sovereign_mesh/canonical.py` — canonical CBOR/JSON, IDs, hashes, counters, signatures, and validation.
- `src/hermes_sovereign_mesh/config.py` — read/validate `plugins.entries.sovereign-mesh`, guarded preview/apply, profile paths, and dependency modes.
- `src/hermes_sovereign_mesh/store.py` — profile-local SQLite/WAL schema for public identities, pairings, ciphertext envelopes, lineage heads, conflicts, replay windows, node observations, leases, revocations, and audit.
- `src/hermes_sovereign_mesh/identity.py` — Ed25519 identity lifecycle, Curve25519 recipient conversion, pairing transcript/SAS, and secret-provider handles.
- `src/hermes_sovereign_mesh/recovery.py` — Argon2id-encrypted offline recovery bundle, restore, root/device epoch rotation, and recovery audit.
- `src/hermes_sovereign_mesh/crypto.py` — libsodium sealed content keys, XChaCha20-Poly1305 payloads, AAD binding, signatures, nonce/counter discipline, and test-vector verification.
- `src/hermes_sovereign_mesh/contracts.py` — dependency adapters importing autonomy, receipts, IFC `FlowContext`, capability grants, missions, and Auto Routing; contains no substitute domain models.
- `src/hermes_sovereign_mesh/replication.py` — selective immutable envelope log, acknowledgement, retransmission, compaction, lineage reduction, and visible conflict records.
- `src/hermes_sovereign_mesh/transport.py` — mutually authenticated node channels, framed ciphertext only, bounded queues/backpressure, reconnect, and trace hooks.
- `src/hermes_sovereign_mesh/service.py` — composition root and headless node server lifecycle.
- `src/hermes_sovereign_mesh/work.py` — mission execution-unit export/import, workload leases, attempt epochs, replay/late-result handling, receipt evidence, and offline resume.
- `src/hermes_sovereign_mesh/placement.py` — hard eligibility facts, explicit pins, router request/decision mapping, and compute-to-data explanation.
- `src/hermes_sovereign_mesh/executor.py` — exact capability-granted `BaseEnvironment` worker and local model binding; never accepts arbitrary replicated shell state.
- `src/hermes_sovereign_mesh/attestation.py` — provider protocol/registry, evidence freshness/key binding, and null provider.
- `src/hermes_sovereign_mesh/revocation.py` — grant cancellation, node/key epochs, queue tombstones, lease fencing, rotation, and active-set reconciliation.
- `src/hermes_sovereign_mesh/cli.py` — one parser/service path for status, node, pair, grant, route, mission, conflict, trace, recover, revoke, rotate, and doctor.
- `src/hermes_sovereign_mesh/dashboard/plugin.json`, `plugin_api.py`, `index.js`, `style.css` — secondary read-only/approval UI loaded by existing Dashboard plugin discovery after the proof gate.
- `src/hermes_sovereign_mesh/assets/mesh-control/SKILL.md` — explicit-load terminal guidance; it calls CLI commands and adds no tool.
- `tests/` — focused model/store/crypto/pairing/contracts/replication/transport/work/placement/executor/revocation/attestation/CLI/Dashboard/security suites.
- `tests/e2e/three_node_harness.py` — three-process, three-profile-root lab with network/storage tracing and deterministic partitions.
- `benchmarks/mesh/manifest.yaml`, `cases.yaml`, `run.py`, `score.py` — frozen 12 workloads, 48 mission runs, 24 lineage cases, 48 security cases, baseline, metrics, and gates.
- `docs/operator.md`, `docs/security-model.md`, `docs/protocol.md`, `docs/incubation.md` — user operations, threat model/limits, wire/state protocol, and bounded rollout.

### Hermes core repository

No Hermes core file is created or modified for the proof. The standalone compatibility suite installs the wheel against a Hermes checkout and invokes existing `tests/gateway/test_agent_cache.py`, `tests/tools/test_registry.py`, environment, runtime-provider, and Ink slash-command regressions to prove the generic seams remain stable.

If implementation finds a missing host capability, stop and propose a separately reviewed, feature-neutral ABC with a second concrete consumer. Do not special-case `sovereign-mesh` in `run_agent.py`, `model_tools.py`, `gateway/run.py`, `tui_gateway/server.py`, `apps/desktop/`, or an environment adapter.

## Frozen Mesh Interfaces

These names are owned by the standalone distribution and remain stable after Task 1:

```python
MeshNodeKind = Literal["user_device", "controlled_remote"]
MeshNodeState = Literal["pending", "active", "offline", "revoked", "recovery_required"]
EnvelopeKind = Literal[
    "node_observation", "mission_handoff", "checkpoint", "result",
    "receipt_evidence", "conflict_resolution", "revocation", "key_rotation",
]
LeaseState = Literal["offered", "claimed", "running", "result_ready", "settled", "expired", "superseded"]
ConflictState = Literal["open", "resolved"]

@dataclass(frozen=True)
class DeviceNode:
    node_id: str
    profile_id: str
    kind: MeshNodeKind
    signing_public_key: bytes
    encryption_public_key: bytes
    key_epoch: int
    state: MeshNodeState
    capability_manifest_digest: str
    residency_labels: tuple[str, ...]
    attestation_provider_id: str | None

@dataclass(frozen=True)
class MeshEnvelopeHeader:
    envelope_id: str
    mesh_id: str
    kind: EnvelopeKind
    sender_node_id: str
    sender_counter: int
    key_epoch: int
    parent_ids: tuple[str, ...]
    recipient_node_ids: tuple[str, ...]
    capability_grant_ids: tuple[str, ...]
    authority_decision_id: str
    flow_context_hash: str
    created_at_ms: int
    expires_at_ms: int
    payload_hash: str

@dataclass(frozen=True)
class WorkEnvelope:
    work_id: str
    mission_id: str
    execution_id: str
    logical_operation_keys: tuple[str, ...]
    required_resource_refs: tuple[str, ...]
    required_capability_grant_ids: tuple[str, ...]
    required_residency_labels: tuple[str, ...]
    attempt_epoch: int
    lease_id: str
    checkpoint_envelope_id: str | None
    requested_outcome_hash: str

@dataclass(frozen=True)
class ConflictRecord:
    conflict_id: str
    object_id: str
    ordered_head_ids: tuple[str, ...]
    reason: str
    state: ConflictState
    resolution_envelope_id: str | None

class MeshService:
    def preview_pair(self, invitation: str) -> PairingPreview: ...
    def confirm_pair(self, pairing_id: str, sas: tuple[str, ...]) -> DeviceNode: ...
    def place(self, request: MeshPlacementRequest) -> MeshPlacementDecision: ...
    def handoff(self, request: HandoffRequest) -> WorkEnvelope: ...
    def reconcile(self, *, mission_id: str | None = None) -> ReconcileReport: ...
    def revoke(self, node_id: str, expected_mesh_revision: str) -> RevocationPreview: ...
```

`contracts.py` imports, rather than redefines, these external names and exact calls:

```python
from agent.autonomy import ActionContext, AuthorityProvider, authorize_effect
from agent.receipts import RECEIPT_STATUSES, ReceiptSourceKey, ReceiptStore
from agent.information_flow import FlowContext, InformationFlowGuard
from agent.capabilities import CapabilityGrant, CapabilityManifest, CapabilityGrantStore
from plugins.auto_routing.auto_routing import AutoRoutingService, RoutingRequest, RoutingDecision

def authorize_mesh_action(
    provider: AuthorityProvider,
    context: ActionContext,
    *,
    stage: Literal["preview", "commit", "compensate"],
): ...

def enforce_mesh_flow(
    guard: InformationFlowGuard,
    context: FlowContext,
    *,
    sink_node_id: str,
): ...

def require_grants(
    store: CapabilityGrantStore,
    grant_ids: tuple[str, ...],
    *,
    node_id: str,
    now_ms: int,
) -> tuple[CapabilityGrant, ...]: ...

def route_eligible_runtime(
    router: AutoRoutingService,
    request: RoutingRequest,
) -> RoutingDecision: ...
```

If the final #15 or #17 canonical module uses a different import path, update only `contracts.py` and its compatibility tests; never copy their dataclasses into mesh production code.

---

### Task 0: Preregister the Three-Node Proof and Threat Model

**Files:**
- Create: `hermes-sovereign-mesh/benchmarks/mesh/manifest.yaml`
- Create: `hermes-sovereign-mesh/benchmarks/mesh/cases.yaml`
- Create: `hermes-sovereign-mesh/tests/test_benchmark_contract.py`
- Create: `hermes-sovereign-mesh/docs/security-model.md`

**Interfaces:**
- Consumes: the approved portfolio contract, §8.5 six workflow archetypes, current-Hermes baseline, and cross-portfolio acceptance gates.
- Produces: `mesh-proof-v1`, exact workload/fault/security denominators, trace boundaries, scorer definitions, hardware/network classes, and immutable pass/fail thresholds consumed by Task 12.

- [ ] **Step 1 — RED: write the frozen-manifest test**

```python
def test_mesh_proof_is_exact_and_has_non_aggregatable_safety_floors():
    manifest, cases = load_mesh_benchmark()
    assert manifest["schema"] == "hermes.mesh-proof.v1"
    assert len(cases["workloads"]) == 12
    assert len(cases["mission_runs"]) == 48
    assert len(cases["lineage_cases"]) == 24
    assert len(cases["security_cases"]) == 48
    assert set(run["schedule"] for run in cases["mission_runs"]) == {
        "connected", "owner_offline_before_claim",
        "worker_crash_after_claim", "partition_after_result",
    }
    assert manifest["gates"]["minimum_verified_missions"] == 44
    assert manifest["gates"]["duplicate_committed_effects"] == 0
    assert manifest["gates"]["privacy_residency_authority_violations"] == 0
    assert manifest["gates"]["plaintext_sentinel_leaks"] == 0
    assert manifest["gates"]["accepted_post_revocation_envelopes"] == 0
```

- [ ] **Step 2 — RED: run the focused test**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_benchmark_contract.py -q`

Expected: FAIL because `benchmarks/mesh/manifest.yaml` and `cases.yaml` do not exist.

- [ ] **Step 3 — Implementation: freeze cases, baseline, and trace scope**

Create two workloads for each approved archetype. Exactly four require a file available on only one user device, four require a locally resolved credential handle, and four require a GPU/local-model runtime; rotate ownership across `device-a`, `device-b`, and `remote-c`. Use public/synthetic data, disposable repositories, designated test accounts, and credential canaries that cannot access production.

For every case record: input/artifact hashes, requested outcome/scorer version, initial node/grant/FlowContext/authority revisions, resource owner, eligible nodes, explicit baseline steps, network class, hardware class, expected terminal status, expected receipt claims, fault schedule, cost source, and exclusion rule. The baseline is current Hermes with the operator manually choosing local or SSH/environment execution and explicitly transferring the minimum artifact. Freeze results fields for verified success, attention minutes, recovery actions, bytes transferred, p50/p95 handoff/resume latency, local/model cost, and cost per verified success.

The 48 security cases are four exact 12-case strata: authority/capability/IFC/residency; replay/lease/late result; recovery/revocation/attestation; and plaintext canaries. Every canary case declares authorized plaintext roots and scans packet capture, relay store, every node's SQLite/WAL/temp/log/crash/export roots, with zero tolerance outside the allowlist.

- [ ] **Step 4 — GREEN: validate the frozen denominator**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_benchmark_contract.py -q`

Expected: PASS with exactly 12 workloads, 48 mission runs, 24 lineage cases, 48 security cases, one current-Hermes baseline definition, and every safety gate represented directly rather than in a composite score.

- [ ] **Step 5 — Commit**

```bash
git add benchmarks/mesh tests/test_benchmark_contract.py docs/security-model.md
git commit -m "test: preregister sovereign mesh proof"
```

---

### Task 1: Freeze Mesh Models, Canonical Encoding, Config, and Profile-Local State

**Files:**
- Create: `hermes-sovereign-mesh/pyproject.toml`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/__init__.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/models.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/canonical.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/config.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/store.py`
- Create: `hermes-sovereign-mesh/tests/test_models.py`
- Create: `hermes-sovereign-mesh/tests/test_store.py`
- Create: `hermes-sovereign-mesh/tests/test_profile_isolation.py`

**Interfaces:**
- Consumes: `get_hermes_home()`, `PluginContext.profile_name`, guarded profile-local config reads, SQLite/WAL, and frozen Task 0 fixture IDs.
- Produces: all frozen mesh dataclasses/vocabularies above, `canonical_cbor()`, `content_hash()`, `MeshConfig.load_current()`, and `MeshStore.open_current()` plus typed identity/envelope/lineage/conflict/replay/lease/revocation/audit methods.

- [ ] **Step 1 — RED: write validation, canonicalization, and isolation tests**

```python
def test_envelope_id_binds_header_parents_recipients_and_payload():
    a = envelope_header(parent_ids=("env_b", "env_a"), recipient_node_ids=("n2", "n1"))
    b = envelope_header(parent_ids=("env_a", "env_b"), recipient_node_ids=("n1", "n2"))
    assert canonical_cbor(a) == canonical_cbor(b)
    assert content_hash(a) == content_hash(b)


def test_opposite_profiles_never_share_mesh_identity_state_or_config(profile_harness):
    work = profile_harness.open("work")
    personal = profile_harness.open("personal")
    work.store.put_node(node_fixture("work-node"))
    assert personal.store.get_node("work-node") is None
    assert work.config.mesh_id != personal.config.mesh_id
    assert work.store.path != personal.store.path
```

Reject unknown enums, empty identities, duplicate/surplus recipients, unsorted parents after normalization, counters below one, expiry before creation, unknown receipt status, grant-less work, raw paths/secret values in an envelope header, and payload/header hashes that do not match.

- [ ] **Step 2 — RED: run the focused suite**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_models.py tests/test_store.py tests/test_profile_isolation.py -q`

Expected: FAIL importing `hermes_sovereign_mesh.models` and `store`.

- [ ] **Step 3 — Implementation: package and exact configuration boundary**

Declare the plugin entry point and no Hermes tool entry point:

```toml
[project.entry-points."hermes.plugins"]
sovereign-mesh = "hermes_sovereign_mesh.plugin:register"

[project.scripts]
hermes-mesh-node = "hermes_sovereign_mesh.service:main"
```

`MeshConfig.load_current()` reads only `plugins.entries.sovereign-mesh` and defaults to `enabled: false`, `mode: off`, `listen: 127.0.0.1:0`, `transport: direct`, bounded queue/lease/retention values, and no remote endpoint. Validate `off|shadow|active`, deny wildcard listen without an explicit authenticated ingress flag, cap envelope size at 1 MiB for the proof, and reject keys/tokens/recovery phrases in config. Persist `config_schema: 1` and `protocol_min/protocol_max`; a newer unsupported config or wire version disables active mode with an exact doctor error rather than guessing.

- [ ] **Step 4 — Implementation: typed WAL schema and replay-safe methods**

Create tables `mesh_meta`, `mesh_nodes`, `mesh_pairings`, `mesh_envelopes`, `mesh_envelope_recipients`, `mesh_lineage_heads`, `mesh_conflicts`, `mesh_replay_windows`, `mesh_work_leases`, `mesh_revocations`, and `mesh_audit`. Store envelope headers, signatures, wrapped keys, and payload ciphertext; never decrypted payloads. Each insert verifies canonical hashes and uses a unique `(sender_node_id, sender_counter)` plus `envelope_id`. Lease compare-and-set methods require owner, epoch, previous revision, and expiry. Audit detail is finite-vocabulary plus hashes/IDs only. Use additive idempotent schema creation for compatible changes; data transforms require an ordered migration under an exclusive service-drain lock, a verified ciphertext-store backup, crash journal, post-migration hash scan, and restore-on-failure. Refuse downgrade/open of a newer schema while preserving bytes for a compatible newer binary.

- [ ] **Step 5 — GREEN: reopen, race, and isolation verification**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_models.py tests/test_store.py tests/test_profile_isolation.py -q`

Expected: PASS; canonical bytes are stable, counter/envelope replay inserts once, lease races have one winner, SQLite/WAL contains no plaintext canary, and named profiles share no config/state path or ID.

- [ ] **Step 6 — Commit**

```bash
git add pyproject.toml src/hermes_sovereign_mesh tests/test_models.py tests/test_store.py tests/test_profile_isolation.py
git commit -m "feat: add profile-local mesh contract and store"
```

---

### Task 2: Implement Cryptographic Device Identity, Pairing, and Recovery Bundles

**Files:**
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/identity.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/recovery.py`
- Create: `hermes-sovereign-mesh/tests/test_identity.py`
- Create: `hermes-sovereign-mesh/tests/test_pairing.py`
- Create: `hermes-sovereign-mesh/tests/test_recovery.py`

**Interfaces:**
- Consumes: Task 1 `DeviceNode`, `MeshStore`, canonical hashing, active profile/secret-provider handle, and monotonic/wall clocks injected for tests.
- Produces: `DeviceIdentityStore`, `PairingInvitation`, `PairingPreview`, `PairingService.preview_pair/confirm_pair/cancel_pair`, `RecoveryService.create_bundle/inspect_bundle/restore_bundle`, and root/device key epochs.

- [ ] **Step 1 — RED: write identity, SAS, expiry, MITM, and recovery tests**

```python
def test_pairing_requires_same_transcript_sas_on_both_devices(pairing):
    left, right = pairing.exchange()
    assert left.sas_words == right.sas_words
    with pytest.raises(PairingRejected, match="safety code mismatch"):
        pairing.confirm_left(("wrong",) * 6)
    assert pairing.store.list_active_nodes() == []


def test_recovery_restore_rotates_epoch_and_does_not_restore_revoked_node(recovery):
    bundle = recovery.create(passphrase="correct horse battery staple")
    recovery.revoke("node-b")
    restored = recovery.restore_fresh(bundle, passphrase="correct horse battery staple")
    assert restored.root_epoch == recovery.root_epoch + 1
    assert restored.node("node-b").state == "revoked"
```

Also cover invitation expiry/replay, Unicode device names, wrong profile/mesh, altered identity key, altered capability digest, concurrent confirmation, secret-provider failure, wrong recovery passphrase, corrupted bundle, interrupted bundle fsync/rename, and redacted output/logs.

- [ ] **Step 2 — RED: run the focused suite**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_identity.py tests/test_pairing.py tests/test_recovery.py -q`

Expected: FAIL because identity/pairing/recovery services do not exist.

- [ ] **Step 3 — Implementation: identity and explicit pairing protocol**

Generate an Ed25519 signing key per device in the active OS secret provider and derive its Curve25519 recipient key through libsodium. Persist public keys and opaque secret handles only. A 10-minute `PairingInvitation` contains mesh/profile IDs, identity and ephemeral public keys, nonce, endpoint hint, capability-manifest digest, and signature. Both peers sign the canonical transcript and derive a six-word SAS from BLAKE2b over both identities, both ephemeral keys, nonce, mesh/profile, and transcript version. Persist an active pairing only after both local explicit confirmations match; cancellation/expiry destroys the ephemeral private key and records a redacted audit event.

- [ ] **Step 4 — Implementation: encrypted offline recovery**

Build a versioned recovery payload containing root enrollment secret, device public records, revocations, mesh/key epochs, and config hash—never state ciphertext or user data. Derive a 32-byte key with libsodium Argon2id using stored random salt and frozen interactive parameters, encrypt/authenticate with XChaCha20-Poly1305, fsync to a temporary file, atomically replace the destination, and set owner-only permissions where supported. Restore validates mesh/profile intent, requires explicit node keep/revoke choices, advances the root epoch, rotates active device wrapping epochs, and never silently reactivates a revoked device.

- [ ] **Step 5 — GREEN: verify pairing and recovery**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_identity.py tests/test_pairing.py tests/test_recovery.py -q`

Expected: PASS; MITM/transcript changes fail, confirmation is explicit/two-sided, keys never enter SQLite/log/output, bundle corruption/wrong passphrase fails closed, and restore advances epochs while preserving revocations.

- [ ] **Step 6 — Commit**

```bash
git add src/hermes_sovereign_mesh/identity.py src/hermes_sovereign_mesh/recovery.py tests/test_identity.py tests/test_pairing.py tests/test_recovery.py
git commit -m "feat: add mesh identity pairing and recovery"
```

---

### Task 3: Bind Authority, Capability Grants, IFC, Receipts, and Router Dependencies

**Files:**
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/contracts.py`
- Create: `hermes-sovereign-mesh/tests/test_contracts.py`
- Create: `hermes-sovereign-mesh/tests/test_dependency_doctor.py`

**Interfaces:**
- Consumes: canonical `AuthorityProvider`/`ActionContext`, `ReceiptStore`/five statuses, IFC `FlowContext`/reference monitor, `CapabilityManifest`/`CapabilityGrantStore`, mission lookup, and `AutoRoutingService` imports.
- Produces: `MeshContracts`, `authorize_mesh_action()`, `enforce_mesh_flow()`, `require_grants()`, `receipt_store()`, `mission_execution_unit()`, `route_eligible_runtime()`, and `DependencyDoctorReport`; no mesh-local authority/flow/grant/receipt/router type.

- [ ] **Step 1 — RED: write fail-closed dependency and no-duplication tests**

```python
def test_active_dispatch_requires_all_canonical_dependencies(contract_harness):
    for missing in ("autonomy", "receipts", "information_flow", "capabilities", "router", "missions"):
        result = contract_harness.without(missing).authorize_dispatch()
        assert result.code == "dependency_unavailable"
        assert result.effect_calls == 0


def test_plugin_registers_commands_but_zero_model_tools(plugin_context, registry):
    before = registry.get_tool_definitions()
    register(plugin_context)
    assert registry.get_tool_definitions() == before
    assert plugin_context.registered_cli_commands == {"mesh"}
    assert plugin_context.registered_commands == {"mesh"}
```

Assert preview uses `consume=False`; commit/compensate reload and consume; revoked/expired grants fail; `FlowContext` hash mismatch fails at both sender/receiver; receipt status validation uses `RECEIPT_STATUSES`; router is called only after hard eligibility; and no raw content/secret/path is passed into audit explanations.

- [ ] **Step 2 — RED: run standalone compatibility tests**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_contracts.py tests/test_dependency_doctor.py -q`

Expected: FAIL importing `hermes_sovereign_mesh.contracts`.

- [ ] **Step 3 — Implementation: one compatibility adapter, no substitutes**

`MeshContracts.open_current(mode)` imports exact public modules lazily. In `off`, doctor may report missing dependencies without state writes. In `shadow`, node inventory and candidate placement may be recorded but no envelope/lease is emitted. In `active`, any missing/incompatible dependency is a hard block. Build `ActionContext(action_class="compute.dispatch")` with hashed node recipient, trusted data classes, canonical resource references, mission/transaction scope, cost, uncertainty, and `reversibility="compensatable"` only when cancellation fencing is proven. Sender and receiver reconstruct and compare the canonical `FlowContext` hash and exact grant IDs/revisions.

- [ ] **Step 4 — Implementation: truthful receipt integration**

Mesh contributes claims/evidence for selected node, authority decision, grant revisions, flow decision, encrypted envelope hashes, lease/attempt lineage, result hash, trace hashes, and late/conflict state. It calls the canonical scorer/issuer and `ReceiptStore.insert()` or `append_observation()`. A transport acknowledgement, signature, successful process exit, or attestation alone yields at most `completed_unverified`; ambiguous disposition maps to `unknown_effect`.

- [ ] **Step 5 — GREEN: prove compatibility and schema stability**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_contracts.py tests/test_dependency_doctor.py -q`

Expected: PASS; every absent/stale dependency blocks active dispatch and imported public types are used directly.

Run: `cd hermes-agent && scripts/run_tests.sh tests/tools/test_registry.py tests/gateway/test_agent_cache.py -q`

Expected: PASS; standalone plugin discovery works, command registration is visible, and effective model tool definitions are byte-identical.

- [ ] **Step 6 — Commit**

```bash
git add src/hermes_sovereign_mesh/contracts.py tests/test_contracts.py tests/test_dependency_doctor.py
git commit -m "feat: consume canonical mesh dependencies"
```

---

### Task 4: Encrypt Selective Replication and Deterministic Conflict Lineage

**Files:**
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/crypto.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/replication.py`
- Create: `hermes-sovereign-mesh/tests/test_crypto.py`
- Create: `hermes-sovereign-mesh/tests/test_replication.py`
- Create: `hermes-sovereign-mesh/tests/test_conflicts.py`
- Create: `hermes-sovereign-mesh/tests/vectors/mesh-envelope-v1.json`

**Interfaces:**
- Consumes: Task 1 canonical/store and Task 2 active identity/recipient keys; Task 3 pre-authorized grant/flow/authority references.
- Produces: `EnvelopeCipher.seal/open`, `ReplicationService.append/receive/ack/pending_for/reconcile/compact`, deterministic envelope IDs, replay windows, `ConflictRecord`, and lineage projections.

- [ ] **Step 1 — RED: write vector, tamper, replay, minimality, and permutation tests**

```python
def test_only_authorized_recipient_can_open_signed_envelope(crypto):
    sealed = crypto.seal(payload=b"sentinel", recipients=("node-b",), aad=header())
    assert crypto.as_node("node-b").open(sealed) == b"sentinel"
    with pytest.raises(EnvelopeRejected):
        crypto.as_node("node-c").open(sealed)


def test_conflict_projection_is_delivery_order_independent(replication, permutations):
    envelopes = concurrent_checkpoint_envelopes()
    projections = {replication.replay(order).projection_bytes for order in permutations(envelopes, 100)}
    assert len(projections) == 1
    assert replication.open_conflicts()[0].ordered_head_ids == tuple(sorted(e.envelope_id for e in envelopes))
```

Test header/payload/signature/wrapped-key tampering, nonce reuse refusal, wrong epoch, sender-counter gaps/duplicates, unknown parent, expired envelope, unauthorized recipient, padding bucket, acknowledged retransmit, crash between ciphertext insert and head projection, set-union commutativity, terminal-state lattice conflicts, and zero replication of banned fields.

- [ ] **Step 2 — RED: run crypto/replication tests**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_crypto.py tests/test_replication.py tests/test_conflicts.py -q`

Expected: FAIL because `crypto.py` and `replication.py` do not exist.

- [ ] **Step 3 — Implementation: authenticated multi-recipient envelope**

Generate a random 32-byte content key per envelope. Encrypt canonical CBOR payload using libsodium XChaCha20-Poly1305 with a random 24-byte nonce and the canonical header (excluding signature/wrapped keys) as AAD. Wrap the content key independently to each active recipient with libsodium sealed boxes. Sign the canonical header, ciphertext hash, nonce, and sorted wrapped-key hashes using the sender Ed25519 key. Pad ciphertext to configured buckets before signing; reject oversize payloads before allocation. Verify signature, mesh/profile/epoch/recipient, grant/flow references, expiry, sender counter, payload hash, and replay window before decryption.

- [ ] **Step 4 — Implementation: minimal append-only lineage**

Allow only versioned payload schemas for node observations, bounded mission execution units, checkpoint/result metadata, receipt evidence digests, conflict resolutions, revocations, and rotations. Reject keys matching banned semantic classes (`messages`, `system_prompt`, `api_key`, `secret_value`, `memory_db`, `state_db`, raw absolute locator). Append ciphertext before updating heads. Heads are a set ordered by envelope ID; parent-covered heads are removed atomically. Commutative sets union by content ID. Monotone lifecycle projections advance only along an allowed lattice. Any concurrent non-commutative heads create one content-addressed open conflict and block dependent handoff until an explicit signed resolution references every head.

- [ ] **Step 5 — GREEN: verify vectors and deterministic lineage**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_crypto.py tests/test_replication.py tests/test_conflicts.py -q`

Expected: PASS; vectors are stable, tampering/replay fails before plaintext release, unauthorized nodes cannot unwrap, crash recovery converges, and all 24 lineage fixtures are byte-identical over 100 randomized delivery orders.

- [ ] **Step 6 — Commit**

```bash
git add src/hermes_sovereign_mesh/crypto.py src/hermes_sovereign_mesh/replication.py tests/test_crypto.py tests/test_replication.py tests/test_conflicts.py tests/vectors
git commit -m "feat: add encrypted deterministic mesh replication"
```

---

### Task 5: Build the Ciphertext-Only Node Transport and Service Lifecycle

**Files:**
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/transport.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/service.py`
- Create: `hermes-sovereign-mesh/tests/test_transport.py`
- Create: `hermes-sovereign-mesh/tests/test_service.py`
- Create: `hermes-sovereign-mesh/tests/test_transport_backpressure.py`

**Interfaces:**
- Consumes: active pairings/keys, Task 4 sealed envelopes and pending acknowledgements, profile-local config/store, and injected clocks/connectors.
- Produces: `MeshTransport.connect/send/receive/close`, `PeerSession`, `TransportFrame`, `NodeService.start/drain/stop/health`, reconnect cursors, bounded queues, and content-free trace events.

- [ ] **Step 1 — RED: write mutual-authentication, reconnect, framing, and backpressure tests**

```python
async def test_channel_rejects_unpaired_or_revoked_peer(transport_pair):
    await transport_pair.left.connect(transport_pair.right.endpoint)
    transport_pair.left.store.revoke_node("right", revision="rev-1")
    with pytest.raises(PeerRejected, match="revoked"):
        await transport_pair.left.reconnect()


async def test_disconnect_replays_unacked_ciphertext_once(transport_pair):
    envelope = transport_pair.left.append_envelope(payload=b"bounded")
    transport_pair.cut_after_remote_insert_before_ack()
    await transport_pair.restore()
    assert transport_pair.right.store.envelope_count(envelope.envelope_id) == 1
    assert transport_pair.left.pending_for("right") == ()
```

Cover TLS certificate/key mismatch, signed challenge replay, unknown mesh/profile, frame length overflow, malformed CBOR, sender counter gap, queue full, slow reader, reconnect cursor rollback, half-close, service crash before/after ack, drain timeout, port collision, and log redaction.

- [ ] **Step 2 — RED: run transport/service tests**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_transport.py tests/test_service.py tests/test_transport_backpressure.py -q`

Expected: FAIL because transport and node-service implementations are absent.

- [ ] **Step 3 — Implementation: authenticated ciphertext framing**

Use TLS 1.3 for channel confidentiality and peer liveness, then bind the channel to mesh identity with a fresh nonce challenge signed by each paired Ed25519 key and transcript containing both TLS exporter values, mesh/profile IDs, node IDs, key epochs, and protocol version. Accept only these bounded frame types: `hello`, `envelope`, `ack`, `cursor`, `ping`, `pong`, and `close`. The `envelope` frame contains header bytes, nonce, wrapped keys, ciphertext, and signature—never decrypted payload. Reject before buffering when the 4-byte length prefix exceeds 2 MiB or the configured envelope limit.

- [ ] **Step 4 — Implementation: durable queues and lifecycle**

Insert received ciphertext idempotently before ack. Sender deletes no durable pending-recipient row until a signed ack binds peer, envelope ID, payload hash, and key epoch. On reconnect, exchange content-free per-sender counters and retransmit missing authorized envelopes. Bound in-memory queues and pause socket reads under pressure; never spill plaintext. `NodeService.drain()` stops offers, completes current frame inserts/acks, fences outstanding work leases, then closes. Startup reconciles incomplete envelope projections before accepting peers.

- [ ] **Step 5 — GREEN: run transport and crypto regressions**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_transport.py tests/test_service.py tests/test_transport_backpressure.py tests/test_crypto.py tests/test_replication.py -q`

Expected: PASS; unauthenticated/revoked peers fail, every accepted frame is bounded ciphertext, reconnect converges without duplicates, slow peers cannot exhaust memory, and crash/drain preserves pending work.

- [ ] **Step 6 — Commit**

```bash
git add src/hermes_sovereign_mesh/transport.py src/hermes_sovereign_mesh/service.py tests/test_transport.py tests/test_service.py tests/test_transport_backpressure.py
git commit -m "feat: add authenticated mesh node transport"
```

---

### Task 6: Hand Off and Resume Offline Mission Work with Fenced Leases

**Files:**
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/work.py`
- Create: `hermes-sovereign-mesh/tests/test_work.py`
- Create: `hermes-sovereign-mesh/tests/test_work_leases.py`
- Create: `hermes-sovereign-mesh/tests/test_offline_resume.py`
- Create: `hermes-sovereign-mesh/tests/test_receipt_integration.py`

**Interfaces:**
- Consumes: canonical mission execution-unit/checkpoint APIs, operation journal/logical operation keys, Task 3 authority/grant/flow/receipt adapters, Task 4 replication, and Task 5 transport.
- Produces: `HandoffRequest`, `WorkEnvelope`, `WorkLease`, `WorkResult`, `ReconcileReport`, `WorkService.preview_handoff/handoff/claim/heartbeat/complete/reconcile`, attempt epochs, late-result observations, and mesh receipt evidence.

- [ ] **Step 1 — RED: write handoff, lease, crash, replay, and late-result tests**

```python
def test_offline_target_queues_bounded_handoff_then_resumes(harness):
    harness.take_offline("device-b")
    work = harness.device_a.handoff(request_for_file_on("device-b"))
    assert work.lease_id
    assert harness.device_a.pending_ciphertext(work.work_id)
    harness.bring_online("device-b")
    harness.reconcile_all()
    assert harness.mission(work.mission_id).receipt.status == "verified"


def test_expired_reassigned_attempt_cannot_commit_duplicate_effect(harness):
    first = harness.claim("node-b", work_id="work-1", epoch=1)
    harness.partition("node-b")
    second = harness.reassign_after_expiry("node-c", work_id="work-1", epoch=2)
    harness.complete(second)
    harness.heal_partition("node-b")
    late = harness.complete(first)
    assert late.disposition == "superseded_observation"
    assert harness.operation_commit_count("logical-op-1") == 1
```

Cover crash before offer insert, after offer insert, after claim, after process start, after result envelope insert, after receipt insert, and before mission projection; duplicate offer/claim/result; wrong owner/epoch; heartbeat after expiry; partition longer than retention; ambiguous operation disposition; revoked worker; authority/grant expiry while queued; and one receipt per terminal source.

- [ ] **Step 2 — RED: run work/recovery tests**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_work.py tests/test_work_leases.py tests/test_offline_resume.py tests/test_receipt_integration.py -q`

Expected: FAIL importing `hermes_sovereign_mesh.work`.

- [ ] **Step 3 — Implementation: export only a bounded execution unit**

Resolve an existing mission/execution/checkpoint; reject raw transcript export. The payload contains objective/requested-outcome hash, exact next execution-unit descriptor, stable logical operation keys, declared resource references, input artifact digests or node-local opaque handles, required grants/residency labels, `FlowContext` hash, checkpoint lineage, and expiry. Run sender-side preview checks, then immediately before envelope commit reload authority, grants, IFC, mission state, target pairing/revocation, and router decision. A changed fact blocks with no envelope.

- [ ] **Step 4 — Implementation: at-least-once lease with effect fencing**

Offers use epoch 1. Claim is a compare-and-set over current epoch, target node, expiry, and ciphertext envelope ID. Heartbeats extend only the current live owner. After expiry the coordinator may create a new epoch, but stable logical operation keys do not change; operation-journal/transaction reconciliation decides whether an external effect already landed. A late superseded result is retained as a signed receipt observation and never overwrites the current checkpoint. Unknown effect disposition stops automatic reassignment and emits `unknown_effect` review.

Reconciliation order is fixed: validate revocations/epochs, ingest envelopes, reconcile operation journal, fence expired leases, resume current mission unit, ingest results, append evidence, ask canonical scorer, insert/reuse receipt by source/content hash, then project terminal status. No distributed transaction or exactly-once claim is made.

- [ ] **Step 5 — GREEN: verify offline/crash/replay recovery**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_work.py tests/test_work_leases.py tests/test_offline_resume.py tests/test_receipt_integration.py -q`

Expected: PASS; recoverable offline work resumes, lease replay is idempotent, superseded results remain visible, effects commit at most once through canonical operation keys, ambiguous effects stop, and every terminal mission has one canonical five-status receipt.

- [ ] **Step 6 — Commit**

```bash
git add src/hermes_sovereign_mesh/work.py tests/test_work.py tests/test_work_leases.py tests/test_offline_resume.py tests/test_receipt_integration.py
git commit -m "feat: add offline mission handoff and resume"
```

---

### Task 7: Place Compute Beside Data Through the Existing Router and Environment Seams

**Files:**
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/placement.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/executor.py`
- Create: `hermes-sovereign-mesh/tests/test_placement.py`
- Create: `hermes-sovereign-mesh/tests/test_executor.py`
- Create: `hermes-sovereign-mesh/tests/test_local_runtime.py`
- Create: `hermes-sovereign-mesh/tests/test_secret_handles.py`

**Interfaces:**
- Consumes: `CapabilityGrant`, IFC/authority decisions, signed node observations, item #10 `RoutingRequest`/`RoutingDecision`, existing `RuntimeKey` full access identity, `BaseEnvironment.execute()`, `_sanitize_subprocess_env()`, `get_secret()` scoped local resolution, and existing local-model providers.
- Produces: `MeshPlacementRequest`, `MeshPlacementDecision`, `PlacementExplanation`, `NodeRuntimeObservation`, `PlacementService.place()`, and `MeshExecutor.execute(work, placement)`.

- [ ] **Step 1 — RED: write hard-filter, router-delegation, data-locality, and secret tests**

```python
def test_file_requirement_sends_compute_to_file_owner_without_copy(placement):
    decision = placement.place(request(required_resource_refs=("file:dataset-1",)))
    assert decision.node_id == "device-b"
    assert decision.data_transfer_ids == ()
    assert decision.routing_decision_id


def test_mesh_never_ranks_eligible_runtimes_itself(placement, router_spy):
    placement.place(gpu_request())
    assert router_spy.decide.call_count == 1
    assert placement.local_score_calls == 0


def test_worker_receives_handle_not_origin_secret(executor, secret_scope):
    result = executor.execute(work_requiring("secret:github-test"))
    assert "github-test" in result.resolved_handles
    assert secret_scope.value not in result.env_snapshot
    assert secret_scope.value not in result.audit_json
```

Also test explicit `--node` pin, pin ineligibility, stale/unverified observation, node offline, missing GPU/model, local provider inaccessible, unknown residency, required attestation absent/stale, cost/budget cap, remote model denied, secret handle from wrong profile, environment symlink/path escape, arbitrary env injection, and router unavailable.

- [ ] **Step 2 — RED: run placement/executor tests**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_placement.py tests/test_executor.py tests/test_local_runtime.py tests/test_secret_handles.py -q`

Expected: FAIL because placement and executor modules do not exist.

- [ ] **Step 3 — Implementation: hard eligibility before router**

Reject nodes lacking exact current grants, matching residency, IFC allow, authority allow, required resource handles, runtime/hardware, fresh observation, active pairing/key epoch, and required attestation. Convert each eligible node-bound local/provider runtime into the router's existing `RuntimeKey`/target inventory with node access identity; call `AutoRoutingService.decide(RoutingRequest)` once. If Auto Routing is unavailable or returns a runtime not in the eligible set, fail closed unless the user supplied an exact eligible node/runtime pin. Never fall back to copying restricted raw data.

- [ ] **Step 4 — Implementation: bounded execution on the selected node**

Map the canonical execution unit to an existing `BaseEnvironment` configured locally on the selected node. Start with the credential-stripped subprocess environment. Resolve only exact `CapabilityGrant` resource roots, network origins, process permissions, and secret handles locally under the active profile's fail-closed secret scope; never serialize the secret. Use existing local provider/LM Studio/custom endpoint resolution and exact runtime access identity. Return result/artifact/evidence digests, bounded stdout/stderr, operation-journal disposition, cost/usage, and runtime identity; raw restricted artifacts remain node-local.

- [ ] **Step 5 — GREEN: verify placement and existing environment regressions**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_placement.py tests/test_executor.py tests/test_local_runtime.py tests/test_secret_handles.py -q`

Expected: PASS; every proof request runs only on a hard-eligible node, router owns ranking, file/credential/GPU cases send compute to the owning node, secrets remain local handles, and missing/stale facts block.

Run: `cd hermes-agent && scripts/run_tests.sh tests/tools/environments tests/hermes_cli/test_runtime_provider_resolution.py tests/agent/test_runtime_access.py -q`

Expected: PASS; existing environment/local-runtime/access behavior is unchanged.

- [ ] **Step 6 — Commit**

```bash
git add src/hermes_sovereign_mesh/placement.py src/hermes_sovereign_mesh/executor.py tests/test_placement.py tests/test_executor.py tests/test_local_runtime.py tests/test_secret_handles.py
git commit -m "feat: place mesh compute beside authorized data"
```

---

### Task 8: Revoke Devices, Rotate Keys, and Verify Optional Attestation Providers

**Files:**
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/revocation.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/attestation.py`
- Create: `hermes-sovereign-mesh/tests/test_revocation.py`
- Create: `hermes-sovereign-mesh/tests/test_key_rotation.py`
- Create: `hermes-sovereign-mesh/tests/test_attestation.py`
- Create: `hermes-sovereign-mesh/tests/fixtures/attestation-provider/entrypoint.py`

**Interfaces:**
- Consumes: active device/key epochs, Capability Exchange grant revocation, autonomy commit authorization, IFC flow checks, work leases/queues, and signed node identity.
- Produces: `AttestationProvider.verify(evidence, *, expected_node_key, now_ms) -> AttestationDecision`, `AttestationRegistry`, `RevocationService.preview/apply/reconcile`, `RotationPlan`, and post-revocation fencing.

- [ ] **Step 1 — RED: write revocation race, epoch, recovery, and attestation tests**

```python
def test_revocation_fences_queue_lease_and_future_decryption(mesh):
    preview = mesh.preview_revoke("remote-c")
    mesh.apply_revoke(preview, expected_mesh_revision=preview.before_revision)
    assert mesh.accepted_new_envelopes("remote-c") == 0
    assert not mesh.can_renew_lease("remote-c")
    assert not mesh.node("remote-c").can_decrypt(mesh.new_epoch_envelope())


def test_attestation_is_key_bound_fresh_and_optional(attestation):
    good = attestation.verify(fixture(node_key="remote-key", fresh=True))
    assert good.eligible
    assert not attestation.verify(fixture(node_key="other-key", fresh=True)).eligible
    assert not attestation.verify(fixture(node_key="remote-key", fresh=False)).eligible
```

Cover authority change between preview/apply, duplicate/replayed revocation, revoked node offline, crash after grant revoke/before epoch publish, crash after epoch publish/before queue tombstone, concurrent send/claim, stale recovery bundle, all devices lost, attestation parser failure, oversized evidence, unknown provider, signature/measurement/nonce/key/expiry mismatch, and provider network outage.

- [ ] **Step 2 — RED: run revocation/attestation tests**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_revocation.py tests/test_key_rotation.py tests/test_attestation.py -q`

Expected: FAIL because revocation and attestation services do not exist.

- [ ] **Step 3 — Implementation: guarded revocation and epoch rotation**

Preview lists affected current grants, queued recipients, leases, rotations, required recovery confirmation, and exact before revision. Apply reloads authority, requires exact hash, records the revocation before network action, calls canonical grant revocation, fences lease renew/claim, tombstones undelivered recipient rows, advances mesh/root wrapping epoch, rewraps only future content keys to the remaining active nodes, emits a signed revocation/rotation envelope, and reconciles interrupted stages idempotently. Never claim deletion from the revoked device; show last authorized delivery and warning that previously received plaintext may remain.

- [ ] **Step 4 — Implementation: standalone attestation provider protocol**

Providers are discovered by a `hermes_sovereign_mesh.attestation` entry-point group. They receive bounded opaque evidence plus expected node identity key, nonce, accepted measurement policy ID, and time; return provider ID/version, verified identity binding, measurement, issued/expiry time, evidence hash, decision, and reason. Store evidence hash/decision, not raw vendor token by default. The null provider returns `not_present`. A grant/authority rule may require a provider and freshness; otherwise attestation is informational and never silently increases capability.

- [ ] **Step 5 — GREEN: verify revoke/recover/attest behavior**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_revocation.py tests/test_key_rotation.py tests/test_attestation.py -q`

Expected: PASS; races/crashes reconcile, revoked nodes accept no new work/keys/leases, remaining nodes advance epochs, stale recovery does not reactivate revocation, and optional attestation is provider-neutral, key-bound, fresh, and non-authorizing by itself.

- [ ] **Step 6 — Commit**

```bash
git add src/hermes_sovereign_mesh/revocation.py src/hermes_sovereign_mesh/attestation.py tests/test_revocation.py tests/test_key_rotation.py tests/test_attestation.py tests/fixtures/attestation-provider
git commit -m "feat: add mesh revocation rotation and attestation"
```

---

### Task 9: Deliver Complete CLI and Ink Controls Through Existing Plugin Commands

**Files:**
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/plugin.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/cli.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/assets/mesh-control/SKILL.md`
- Create: `hermes-sovereign-mesh/tests/test_plugin.py`
- Create: `hermes-sovereign-mesh/tests/test_cli.py`
- Create: `hermes-sovereign-mesh/tests/test_ink_command_path.py`

**Interfaces:**
- Consumes: `PluginContext.register_cli_command()`, `register_command()`, Tasks 1–8 `MeshService`, typed-plugin handling in existing `slash.exec`, and skill discovery in `commands.catalog`/`complete.slash`.
- Produces: `setup_parser()`, `mesh_command(args)`, `run_argv(argv, output_mode="text")`, `register(ctx)`, structured text/JSON renderers, and the `/mesh` command; no tool or new JSON-RPC method.

- [ ] **Step 1 — RED: write grammar, preview/apply, no-tool, and Ink-path tests**

```python
def test_revoke_previews_then_requires_exact_revision(cli):
    preview = cli.run("node revoke remote-c --json")
    assert preview.json["applied"] is False
    stale = cli.run("node revoke remote-c --apply --expected-revision wrong --json")
    assert stale.exit_code == 2


def test_ink_discovers_and_dispatches_mesh_without_new_rpc(ink_harness):
    result = ink_harness.rpc("slash.exec", {"command": "mesh status --json"})
    assert result["type"] == "output"
    assert ink_harness.plugin_handler_calls == [("mesh", "status --json")]
    assert ink_harness.new_rpc_methods == set()
```

Assert unrecognized trailing arguments fail, interactive confirmation is required for pairing SAS/recovery/revoke/rotation, non-interactive mutation requires `--apply` plus exact hash/revision, JSON contains no secret/raw sensitive path, mode defaults off, and plugin registration calls no `register_tool()`.

- [ ] **Step 2 — RED: run CLI/plugin tests**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_plugin.py tests/test_cli.py tests/test_ink_command_path.py -q`

Expected: FAIL because plugin/CLI entry points do not exist.

- [ ] **Step 3 — Implementation: exact command grammar**

```text
hermes mesh status|doctor [--json]
hermes mesh node list|show <node> [--json]
hermes mesh node serve [--foreground]
hermes mesh pair invite [--expires 10m] [--json]
hermes mesh pair accept <invitation> [--json]
hermes mesh pair confirm <pairing-id> --sas "word1 ... word6"
hermes mesh grant list|show <grant-id> [--json]
hermes mesh route preview --mission <id> [--node <id>] [--json]
hermes mesh mission handoff <mission-id> [--node <id>] [--apply --expected-hash <hash>]
hermes mesh mission reconcile <mission-id> [--json]
hermes mesh conflict list|show <id> [--json]
hermes mesh conflict resolve <id> --choose <head-id> [--apply --expected-hash <hash>]
hermes mesh node revoke <node-id> [--apply --expected-revision <hash>]
hermes mesh keys rotate [--apply --expected-revision <hash>]
hermes mesh recovery create|inspect|restore <path>
hermes mesh trace run|inspect <trace-id> [--json]
```

`/mesh <same arguments>` uses the same parser/service/renderers. Pair/recovery passphrases use a masked terminal prompt and are never accepted as a command-line flag. In Ink, commands that need a full secret/SAS confirmation return a bounded instruction to run the terminal command; status/list/show/preview/reconcile render natively through the existing pager path.

- [ ] **Step 4 — Implementation: operator skill without prompt mutation**

The explicit-load skill explains dependency doctor, three-node setup, SAS comparison, grants/residency, shadow/active modes, placement preview, handoff/reconcile, conflicts, recovery, revocation limits, trace capture, and five receipt statuses. It calls `hermes mesh`; it does not expose keys, suggest enabling broad sync, claim exactly-once/private/verified beyond evidence, or alter the current conversation's system prompt/toolset.

- [ ] **Step 5 — GREEN: verify terminal and Ink primary controls**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_plugin.py tests/test_cli.py tests/test_ink_command_path.py -q`

Expected: PASS; all controls share one parser/service, mutations are guarded, Ink uses existing plugin-command routing, output is redacted, and the model tool schema remains empty for the plugin.

Run: `cd hermes-agent && cd ui-tui && npm test -- --run src/__tests__/createSlashHandler.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS; existing plugin slash discovery/dispatch remains intact.

- [ ] **Step 6 — Commit**

```bash
git add src/hermes_sovereign_mesh/plugin.py src/hermes_sovereign_mesh/cli.py src/hermes_sovereign_mesh/assets tests/test_plugin.py tests/test_cli.py tests/test_ink_command_path.py
git commit -m "feat: add terminal-first mesh controls"
```

---

### Task 10: Add a Secondary Dashboard Inspector Without Desktop Dependency

**Files:**
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/dashboard/plugin.json`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/dashboard/plugin_api.py`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/dashboard/index.js`
- Create: `hermes-sovereign-mesh/src/hermes_sovereign_mesh/dashboard/style.css`
- Create: `hermes-sovereign-mesh/tests/test_dashboard_api.py`
- Create: `hermes-sovereign-mesh/tests/test_dashboard_assets.py`

**Interfaces:**
- Consumes: existing Dashboard plugin discovery/API mount, active profile scope/auth/CSRF middleware, and the same `MeshService` read/preview/apply methods used by CLI.
- Produces: `/api/plugins/sovereign-mesh/status`, `/nodes`, `/missions`, `/conflicts`, `/traces`, `/preview`, and `/apply` plus a secondary plugin page; no transcript, composer, PTY, JSON-RPC, or Desktop surface.

- [ ] **Step 1 — RED: write scoped/redacted API and failure-isolation tests**

```python
def test_dashboard_never_exposes_private_keys_ciphertext_or_raw_paths(client):
    payload = client.get("/api/plugins/sovereign-mesh/nodes").json()
    encoded = json.dumps(payload)
    assert "private_key" not in encoded
    assert "wrapped_key" not in encoded
    assert "/private/dataset" not in encoded


def test_mesh_api_failure_does_not_break_chat_or_pty(client, broken_mesh):
    assert client.get("/api/plugins/sovereign-mesh/status").status_code == 503
    assert client.get("/api/sessions").status_code == 200
```

Cover wrong profile, stale apply hash, body/limit bounds, auth/CSRF, disabled plugin, no await while holding profile scope, XSS-safe labels, conflict/revocation warning language, and absence of `apps/desktop` imports/files.

- [ ] **Step 2 — RED: run Dashboard tests**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_dashboard_api.py tests/test_dashboard_assets.py -q`

Expected: FAIL because Dashboard plugin assets/routes do not exist.

- [ ] **Step 3 — Implementation: bounded secondary API**

Expose redacted summaries only. Detail endpoints cap audit/trace rows at 500 and never return ciphertext, wrapped keys, raw attestations, raw resource locators, payloads, secrets, passphrases, or private keys. Preview/apply uses exact hashes/revisions and same authority recheck. Run synchronous profile-scoped service work in a worker without holding a process-global profile scope across `await`.

- [ ] **Step 4 — Implementation: inspector page**

Show mode/dependency health, nodes/state/key epoch/capability digest/attestation freshness, placement explanations, queued/running work, open conflicts, receipt statuses, revocation/rotation warnings, and trace-scan results. Allow preview/apply only for conflict resolution and revocation after explicit confirmation. Render errors locally so primary Dashboard chat/PTY continues. Do not create a second chat surface.

- [ ] **Step 5 — GREEN: verify Dashboard and host plugin loader**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_dashboard_api.py tests/test_dashboard_assets.py -q`

Expected: PASS; API/page are scoped/redacted and failure-isolated.

Run: `cd hermes-agent && scripts/run_tests.sh tests/hermes_cli/test_dashboard_plugins.py -q`

Expected: PASS; standalone Dashboard plugin discovery/mount semantics remain unchanged.

- [ ] **Step 6 — Commit**

```bash
git add src/hermes_sovereign_mesh/dashboard tests/test_dashboard_api.py tests/test_dashboard_assets.py
git commit -m "feat: add secondary mesh dashboard inspector"
```

---

### Task 11: Prove the Real Three-Node Boundary Across Crashes, Partitions, Replay, and Plaintext Traces

**Files:**
- Create: `hermes-sovereign-mesh/tests/e2e/three_node_harness.py`
- Create: `hermes-sovereign-mesh/tests/e2e/test_three_node_mesh.py`
- Create: `hermes-sovereign-mesh/tests/e2e/test_fault_matrix.py`
- Create: `hermes-sovereign-mesh/tests/e2e/test_security_matrix.py`
- Create: `hermes-sovereign-mesh/tests/e2e/test_plaintext_traces.py`
- Create: `hermes-sovereign-mesh/tests/e2e/test_cache_role_schema.py`
- Create: `hermes-sovereign-mesh/tests/e2e/test_profile_secret_isolation.py`

**Interfaces:**
- Consumes: complete Tasks 1–10 implementation, frozen Task 0 cases, real canonical dependency implementations, real three-process services/stores/sockets/files, and only external model/attestation completion fixtures.
- Produces: `ThreeNodeHarness`, packet/storage trace artifacts, fault/recovery reports, and the release safety gate; no new production interface.

- [ ] **Step 1 — RED: create the three-node real-process harness**

Start `device-a`, `device-b`, and `remote-c` as separate processes with separate temporary profile homes, secret providers, SQLite/WAL stores, ports, local resource roots, and runtime observations. Pair through the public CLI path and compare SAS. Use real loopback TCP/TLS plus a deterministic proxy that can drop, duplicate, reorder, delay, half-close, and partition ciphertext frames. Capture PCAP-equivalent byte streams and recursively snapshot storage after every fault. The controlled remote node is user-operated; no SaaS is involved.

```python
def test_three_nodes_are_process_and_profile_isolated(mesh_lab):
    assert len({n.pid for n in mesh_lab.nodes}) == 3
    assert len({n.hermes_home for n in mesh_lab.nodes}) == 3
    assert len({n.identity_public_key for n in mesh_lab.nodes}) == 3
    assert mesh_lab.cross_profile_secret_reads == 0
```

- [ ] **Step 2 — RED: encode the complete fault/security matrix**

Exercise all 48 mission schedules, 24 conflict cases over 100 permutations, and 48 security cases. Kill/restart at envelope insert, ack, lease claim, worker start, effect return, result insert, receipt insert, revocation record, key rotation, and queue tombstone. Attack prompt-claimed approval, forged identity/signature/attestation, confused mission/grant/node, path and symlink escape, SSRF-shaped endpoint/origin, counter/ack/result replay, stale authority/IFC/grant/router inventory, privilege drift, cross-profile key/ciphertext copy, revoked reconnect, recovery rollback, oversized/compression-bomb frame, and malicious node observations.

Run: `cd hermes-sovereign-mesh && python -m pytest tests/e2e/test_three_node_mesh.py tests/e2e/test_fault_matrix.py tests/e2e/test_security_matrix.py -q`

Expected: FAIL at incomplete real-process wiring or any fault boundary that does not yet converge/fail closed.

- [ ] **Step 3 — Implementation: apply only owner-module corrections**

Fix failures in the production module that owns the violated invariant. Preserve the frozen public types, canonical encodings, receipt statuses, authority/grant/flow ordering, router ownership, and no-tool boundary. Do not weaken the case, shorten the denominator, skip a platform-independent fault, or catch-and-label an unresolved effect as success.

- [ ] **Step 4 — RED/GREEN: scan network and storage for plaintext canaries**

For each of the 12 canary cases, place unique high-entropy sentinels in a file, credential, mission input, model prompt fragment, result, and recovery phrase. Declare the exact authorized plaintext process/root. Scan raw transport capture, relay/proxy buffers, all other nodes' SQLite/WAL/SHM, envelope queues, logs, temp, crash artifacts, exports, and Dashboard/CLI outputs. Also scan authorized-node persistent locations not explicitly approved for that sentinel.

Run: `cd hermes-sovereign-mesh && python -m pytest tests/e2e/test_plaintext_traces.py -q`

Expected before corrections: FAIL with the exact boundary/path/offset if any sentinel escapes.

Expected after corrections: PASS with zero sentinel bytes outside each declared authorized boundary and a trace manifest proving every required location was scanned.

- [ ] **Step 5 — GREEN: prove cache, role, tool-schema, profile, and secret invariants**

Run a multi-turn conversation while pairing, handoff, conflict, recovery, revoke, and receipt state changes externally. Independently hash system prompt, effective tool definitions, primary provider/model/runtime access identity, and history before/after every change. Assert strict role alternation, compression-only history mutation, no synthetic user message, no primary route change, and no mesh tool.

Run: `cd hermes-sovereign-mesh && python -m pytest tests/e2e/test_cache_role_schema.py tests/e2e/test_profile_secret_isolation.py -q`

Expected: PASS; all hashes/roles stay valid, three profiles cannot read each other's config/state/keys/secrets, subprocess environments contain no unrelated credential, and mesh events remain control-plane state.

- [ ] **Step 6 — GREEN: run the complete E2E safety gate**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/e2e -q`

Expected: PASS; 48/48 runs have truthful receipts, recoverable fault runs resume, no duplicate effect commits occur, unknown effects stop, 24/24 conflicts are deterministic/visible, all 48 attacks fail closed, revoked nodes accept no new work, and plaintext leaks are zero.

- [ ] **Step 7 — Commit**

```bash
git add tests/e2e src/hermes_sovereign_mesh
git commit -m "test: prove sovereign mesh end to end"
```

---

### Task 12: Run the Frozen Proof, Document Operations, and Gate Incubation

**Files:**
- Create: `hermes-sovereign-mesh/benchmarks/mesh/run.py`
- Create: `hermes-sovereign-mesh/benchmarks/mesh/score.py`
- Create: `hermes-sovereign-mesh/benchmarks/mesh/README.md`
- Create: `hermes-sovereign-mesh/tests/test_benchmark_score.py`
- Create: `hermes-sovereign-mesh/docs/operator.md`
- Create: `hermes-sovereign-mesh/docs/protocol.md`
- Create: `hermes-sovereign-mesh/docs/incubation.md`
- Modify: `hermes-sovereign-mesh/README.md`

**Interfaces:**
- Consumes: frozen Task 0 fixtures, complete real-process Task 11 harness, current-Hermes baseline runner, and all proof metrics/evidence.
- Produces: `run_proof(manifest, cases, mode, output_dir)`, `score_proof(baseline, candidate)`, local `results.json`, `trace-manifest.json`, `report.md`, operator/security/protocol docs, and explicit rollout/rollback/stop decisions.

- [ ] **Step 1 — RED: write denominator, status, safety-floor, and baseline scorer tests**

```python
def test_score_requires_every_case_and_each_safety_floor():
    baseline, candidate = complete_synthetic_runs()
    report = score_proof(baseline, candidate)
    assert report.workload_denominator == 12
    assert report.mission_run_denominator == 48
    assert report.lineage_denominator == 24
    assert report.security_denominator == 48
    assert report.verified_missions >= 44
    assert report.duplicate_committed_effects == 0
    assert report.plaintext_sentinel_leaks == 0
    assert report.policy_violations == 0


def test_missing_case_or_trace_scan_is_inconclusive_not_pass():
    baseline, candidate = complete_synthetic_runs()
    candidate.security_cases.pop()
    with pytest.raises(ProofIncomplete, match="expected 48 security cases"):
        score_proof(baseline, candidate)
```

- [ ] **Step 2 — RED: run scorer tests**

Run: `cd hermes-sovereign-mesh && python -m pytest tests/test_benchmark_contract.py tests/test_benchmark_score.py -q`

Expected: FAIL because proof runner/scorer are absent.

- [ ] **Step 3 — Implementation: baseline, candidate, and exact metrics**

Baseline executes the 12 connected workloads with current Hermes and explicit user-selected local/SSH/environment placement plus manual minimum artifact transfer. Candidate executes all 48 schedules plus lineage/security matrices. Record per run: terminal `ReceiptStatus`, scorer/receipt/evidence hashes, effect counts/dispositions, placement and reason, authority/FlowContext/grant/router revisions, reconnect/recovery actions, conflict IDs, transfer bytes, attention time, handoff/resume/total latency, compute/model/network cost, trace coverage, excluded/aborted reason, and hardware/network class.

Report exact denominators and Wilson 95% intervals for rates; p50/p95 latencies; verified success; user attention/recovery burden; bytes copied; total cost and cost per verified success; every non-verified status; every conflict/security/trace slice; and baseline/candidate differences. Never replace safety floors with an aggregate score. Missing hardware/runtime or an underpowered slice is `inconclusive`; do not shrink cases or relax a threshold after results.

- [ ] **Step 4 — GREEN: execute and score the preregistered proof**

Run:

```bash
cd hermes-sovereign-mesh
python -m benchmarks.mesh.run --mode baseline --manifest benchmarks/mesh/manifest.yaml --cases benchmarks/mesh/cases.yaml --output .local-proof/baseline
python -m benchmarks.mesh.run --mode candidate --manifest benchmarks/mesh/manifest.yaml --cases benchmarks/mesh/cases.yaml --output .local-proof/candidate
python -m benchmarks.mesh.score --baseline .local-proof/baseline/results.json --candidate .local-proof/candidate/results.json --output .local-proof/report.md
```

Expected: baseline writes exactly 12 connected cases; candidate writes 48 mission, 24 lineage, and 48 security cases plus complete trace manifests; scorer exits 0 only when all Approved Portfolio Contract gates pass. `.local-proof/` remains uncommitted.

- [ ] **Step 5 — Implementation: write truthful operator, protocol, and recovery documentation**

Document installation as a standalone plugin, exact compatible Hermes versions, off/shadow/active modes, two-device/one-controlled-remote boundary, profile isolation, node service startup, pairing/SAS, grants/residency/IFC, placement preview, local file/credential/GPU examples, offline handoff/reconcile, conflict inspection/resolution, receipt status meanings, recovery bundle creation/storage/restore, revocation/rotation limits, optional attestation, trace capture/inspection, backups, migration/downgrade, and complete command reference.

The protocol document specifies canonical encodings, signed/encrypted envelope fields, sender counters, recipient wrapping, AAD, padding, acknowledgements, replay window, lineage/conflict rules, lease epochs/fencing, late results, revocation/rotation ordering, attestation binding, dependency versions, and compatibility negotiation. State unsupported guarantees explicitly: not complete semantic noninterference, not exactly-once compute, no deletion guarantee for previously delivered plaintext, no external-agent federation, no general file sync, and no proof from signature/attestation alone.

- [ ] **Step 6 — Implementation: bounded rollout, rollback, and failure conditions**

1. Ship `enabled: false`, `mode: off`; allow only doctor, identity creation, and local benchmark inspection.
2. Enter lab mode with synthetic canaries and exactly two user devices plus one controlled remote node; keep execution in shadow until canonical #1/#6/#10/#12/#15/#17 dependencies pass doctor.
3. Enable active only after all Task 11 tests and the frozen proof pass, then operate three explicitly opted-in user-owned workflows for 30 days with weekly trace/revocation/recovery review.
4. Advance incubation only if at least three recurring workflows complete with zero critical safety failures, understandable conflict/recovery UX, and user attention no worse than the preregistered baseline. A catalog listing remains control-plane-only and adds no conversational MCP/model tool.
5. Stop immediately on plaintext leak; authority/grant/IFC/residency escape; false `verified`; accepted revoked work; duplicate effect; cross-profile access; undetected conflict; implicit primary route change; schema/cache/role drift; unrecoverable key rotation; or any mitigation that disables normal local file/browser use.
6. Roll back by guarded `mode: off`, stop new offers, drain/fence leases, preserve ciphertext/audit/receipts for diagnosis, revoke remote grants, rotate active keys if compromise is suspected, restore the prior standalone package/config backup, and start a new conversation only if provider/model/tool-schema identity changed. Never delete evidence to make rollback look clean.

- [ ] **Step 7 — GREEN: run final standalone and Hermes regressions**

Run:

```bash
cd hermes-sovereign-mesh
python -m pytest -q
python -m ruff check src tests benchmarks
python -m build
git diff --check
```

Expected: all standalone tests pass, Ruff/build pass, and whitespace check is clean.

Run:

```bash
cd hermes-agent
scripts/run_tests.sh tests/tools/test_registry.py tests/gateway/test_agent_cache.py tests/tools/environments -q
cd ui-tui
npm test -- --run src/__tests__/createSlashHandler.test.ts src/__tests__/slashParity.test.ts
npm run typecheck
```

Expected: host/plugin/cache/schema/environment and Ink command regressions pass with no Desktop test or dependency.

- [ ] **Step 8 — Commit**

```bash
git add benchmarks/mesh tests/test_benchmark_score.py docs README.md
git commit -m "docs: ship sovereign mesh proof and incubation"
```

---

## Final Verification Matrix

| Requirement | Release evidence |
|---|---|
| Two user devices + one controlled remote | Three real processes, distinct profile roots/identities, explicit pairing and SAS confirmation |
| Cryptographic device identity/pairing | Ed25519/Curve25519 identity, signed transcript, two-sided six-word SAS, expiry/replay/MITM tests |
| Explicit capabilities and residency | Canonical `CapabilityGrant` plus `ActionContext` and `FlowContext` at sender/receiver; zero bypasses |
| E2E minimal selective replication | Per-envelope content key, recipient sealed boxes, XChaCha20-Poly1305, signed AAD, banned-field/minimality tests |
| Deterministic visible conflicts | Immutable parent lineage, ordered heads, 24 cases × 100 delivery permutations, explicit resolutions |
| Offline mission/task resume | Canonical mission execution units, durable ciphertext queue, attempt epochs, fenced leases, real restart/partition tests |
| Compute follows data | Hard eligibility plus existing router decision; file/credential/GPU remains on owning node |
| Local model/environment reuse | Existing runtime access identity and `BaseEnvironment`; no model installation/provider fork |
| Key recovery and revocation | Encrypted offline bundle, epoch rotation, preserved revocations, queue/lease fencing, truthful prior-plaintext warning |
| Optional attestation | Entry-point provider, identity/nonce/measurement/freshness binding, no vendor core dependency or automatic grant |
| Lease/replay/crash/partition | CAS owner/epoch/expiry, stable logical operation keys, late-result observation, no blind unknown retry |
| No plaintext outside boundary | 12 sentinel cases scanning transport and all required storage/log/temp/export surfaces with zero leaks |
| Truthful outcomes | Canonical `ReceiptStore`, five statuses only, independent scorer, one receipt per terminal mission |
| Cache/tool/role invariants | Independent hashes across mesh changes, pinned provider/model/runtime, strict role alternation, no mesh tool |
| Profile/secret isolation | `get_hermes_home()`, secret handles/local resolution, distinct state/key stores, adversarial cross-profile tests |
| Primary/secondary surfaces | `hermes mesh` + `/mesh` through existing Ink plugin path; optional Dashboard inspector; no Desktop files |
| Narrow-waist delivery | Standalone rung-4 plugin/service, canonical dependency adapters only, no core production change |
| 90-day proof and comparison | 12 baseline cases; 48 mission, 24 lineage, 48 security candidate cases; exact metrics/gates and local reports |
| Bounded rollout/failure stops | Off → shadow → lab active → three-workflow incubation, explicit stop/rollback/key-rotation procedure |

## Completion Gate

Do not call the Sovereign Personal Compute Mesh complete until fresh artifacts show that the frozen proof passed without exclusions, every trace location was scanned, all canonical dependency versions were recorded, every non-verified run was explained with one of the four non-verified statuses, and rollback/recovery/revocation were rehearsed. The result proves one user's three-node composition only; it does not establish external federation, vendor-cloud trust, general semantic noninterference, universal exactly-once execution, or deletion of data previously authorized for delivery.
