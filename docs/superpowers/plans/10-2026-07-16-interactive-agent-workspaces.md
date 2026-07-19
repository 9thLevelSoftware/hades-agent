# Interactive Agent Workspaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Hermes create, persist, resume, and safely operate task-specific visual workspaces through audited declarative components, with CLI and native Ink as the first-class surfaces and Dashboard as a secondary rendering of the same state.

**Architecture:** Add a strict, versioned, non-executable `WorkspaceEnvelope` and a closed first-party component registry beside profile-local workspace state in `state.db`. Producers may populate only five audited component families; stable action IDs resolve through separately persisted trusted bindings, then reuse item #6 `AuthorityProvider`/`ActionContext`, item #2 transaction coordination, and item #12 receipt/evidence vocabulary. The classic CLI and native Ink TUI render the same semantic projection; Dashboard consumes the same service secondarily, while Desktop is deliberately untouched.

**Tech Stack:** Python 3.13, Pydantic strict models, frozen dataclasses, canonical JSON/SHA-256, SQLite/WAL through `SessionDB`, Rich/classic CLI, `tui_gateway` JSON-RPC, React 19 + Ink/TypeScript, React Dashboard, MCP `structuredContent`, pytest through `scripts/run_tests.sh`, Vitest, and versioned YAML benchmark fixtures.

## Global Constraints

- Work in a fresh git worktree created from the branch containing this plan; preserve unrelated user changes.
- TDD is mandatory. Every production behavior starts with the smallest focused failing test and a recorded RED result.
- The shared V1 protocol is `hermes.workspace.v1`; unknown versions, unknown fields, unknown components, invalid origins, and unbound actions fail closed.
- `WorkspaceEnvelope` is declarative data only. Reject executable JavaScript, HTML, CSS, SVG, event code, shell commands, templates that evaluate code, `javascript:`/`data:` URLs, and arbitrary renderer/plugin names.
- V1 has exactly five audited families: `review_form.v1`, `comparison_table.v1`, `timeline_progress.v1`, `evidence_artifact.v1`, and `approval.v1`. A producer can select and populate them but cannot extend the registry.
- Stable action IDs are display references, never commands. A trusted backend creates a separate `WorkspaceActionBinding`; an envelope, model, artifact, or MCP server cannot supply a handler, authority grant, transaction ID, receipt status, or trusted data label.
- Reuse item #6 `agent.autonomy.AuthorityProvider`, `StoredAuthorityProvider`, `ActionContext`, `AuthorityDecision`, and `authorize_effect()`. Do not create a workspace permission store, evaluator, rule type, or approval protocol.
- Reuse item #2 `agent.effects.TransactionCoordinator`, `TransactionStore`, `TransactionRevision`, `RevisionNode`, `EffectAdapter`, and the existing `OperationJournal` for mutating action effects. Workspace invocation rows are an audited UI projection, not another effect state machine.
- Reuse item #12's canonical `Receipt`, `ArtifactDigest`, `ReceiptStatus`, evidence freshness, and statuses `verified`, `completed_unverified`, `failed`, `blocked`, and `unknown_effect`. Until the standalone #12 plan lands, consume the canonical `agent.receipts` contract established by item #1; do not define a local receipt schema.
- Persist immutable envelopes, resumable instance state, trusted action bindings, append-only events, and invocation projections in profile-local `state.db` resolved through `get_hermes_home()`. Profiles remain independent islands.
- Non-secret settings live under `workspaces:` in `config.yaml`. Credentials and secret values are not valid workspace fields and never enter envelope/state/audit JSON.
- The system prompt, cached prefix, effective tool definitions, provider, and model remain byte-stable for a conversation. Workspace state changes never rewrite history, rebuild prompts, reload tools, inject a synthetic user message, or change role alternation.
- Workspace events travel only through durable workspace tables and out-of-band UI events; they are never appended as chat messages.
- Delivery is Footprint Ladder rung 1 at the existing terminal UI edge, plus the existing CLI-command path. There is no new core model-visible tool. Optional third-party producers ship later as standalone plugins (rung 4) or MCP servers/Apps (rung 5) and may emit only the audited envelope.
- CLI and native Ink are primary. Dashboard is secondary. Do not modify `apps/desktop/`, import Desktop packages, require Desktop parity, or make Dashboard/desktop a dependency of the workspace service.
- Real-path tests use temporary `HERMES_HOME`, real `SessionDB`, real imports, real files/artifacts, real CLI/service paths, real TUI RPC dispatch, and restart by reopening stores or spawning a fresh process. Mock only external MCP/network/process-kill boundaries.
- Bound every envelope to 1 MiB canonical JSON, 100 components, 500 rows/items per component, 64 KiB per text value, 128 actions, and 24 hours maximum uncommitted action-binding lifetime. Enforce limits before persistence and rendering.
- Each task ends with focused GREEN tests, relevant regressions, `git diff --check`, and exactly one conventional commit.

---

## Approved Portfolio Contract

**Layman outcome:** Hermes can create the right safe visual workspace for a task—plans, forms, comparisons, timelines, inspectors, or approval panels—instead of forcing everything through prose chat.

**Design boundary:** A workspace is a declarative, host-rendered view over canonical mission, transaction, authority, and receipt state. Producers may propose structured documents and action references, but only host-registered actions can cross the existing authority/approval/transaction boundary; the workspace does not own chat, model sessions, arbitrary code, effect truth, or a second Dashboard composer/transcript.

**90-day proof:** Pre-register exactly 20 paired tasks: four plan-review tasks, four multi-option comparisons, four mission-monitoring tasks, four evidence inspections, and four approvals. Run each task once through current chat-only Hermes and once through the candidate workspace with order counterbalanced. Pass only with at least 20% lower median time-to-correct-completion or at least 25% fewer incorrect committed actions, without worsening the other metric; no comprehension regression; every action reachable by keyboard and screen-reader semantic output; correct state resume after restart; and zero arbitrary-code, stale-action, cross-origin, or privilege-bypass paths.

**Dependencies and failure conditions:** Items #1 Missions, #2 Transactions, #6 Preferences & Autonomy, and #12 Receipts provide durable work, effects, authority, evidence, and truthful outcomes. Visual novelty, denser screens, more actions, or higher rendering complexity without lower cognitive load is failure. A workspace action that bypasses current authority, exact approval, transaction, origin, or receipt semantics is a release blocker.

**Delivery:** Footprint Ladder rung 1 for the shared first-party envelope, store, CLI renderer, and native Ink renderer. Dashboard is a secondary binding over the same semantics. Optional producer integrations follow at rung 4/5. No new model tool and no Desktop delivery dependency.

---

## Current-Code Audit and File Map

### Existing seams this plan extends

- `hermes_state.py:710-938` owns declarative additive SQLite schema; `SessionDB._execute_write()`/`_execute_read()` provide bounded profile-local access. Additive tables do not require a schema-version bump under the current reconciliation convention.
- `hermes_cli/commands.py:213-215` registers `/workflow`; `cli.py:8427-8833` dispatches canonical slash commands; `hermes_cli/cli_commands_mixin.py:1536-1557` shows the shared classic-handler pattern; `hermes_cli/workflows.py:31-125,565-600` shows a shared top-level/slash parser and renderer.
- `hermes_cli/main.py:4328-4334,13252-13255` wires top-level workflow parsing/dispatch. Workspace CLI wiring follows this path and does not invoke a shell.
- `tui_gateway/server.py:1123-1167` emits typed session events; `@method("commands.catalog")` at 11801 and `@method("slash.exec")` at 13278 expose registry/fallback routing; `approval.request`/`approval.respond` at 1147 and 10324 are the existing prompt transport.
- `ui-tui/src/app/slash/commands/ops.ts:64` owns native operational commands; `ui-tui/src/app/slash/registry.ts` composes them; `ui-tui/src/__tests__/slashParity.test.ts:16-111` prevents mutating commands from falling through `slash.exec`; `components/prompts.tsx` and `app/useInputHandlers.ts` own approval and keyboard overlays.
- `web/src/pages/ChatPage.tsx:1278-1504` hosts the embedded TUI and complementary sidebar; `web/src/components/ChatSidebar.tsx` already uses an independent JSON-RPC sidecar and passive PTY event feed; `web/src/plugins/slots.ts:58-132` supplies optional page slots. The workspace panel must complement, not replace, the TUI.
- `tools/approval.py:2925-3034` is the existing tool approval entry point; `tui_gateway/server.py:1147-1167` redacts approval payloads. Item #6 strengthens this with exact `AuthorityGrant` and `ActionContext`; workspaces consume that landed contract.
- `agent/operation_journal.py:87-321` owns durable effect disposition. Item #1 adds transaction coordination and item #12 adds canonical receipts; workspace actions reference their IDs rather than cloning their state.
- `tools/mcp_tool.py:4140-4193` preserves MCP text plus `structuredContent`; `tests/tools/test_mcp_structured_content.py` proves its current JSON shape. Workspace intake recognizes one reserved structured-content member without dropping existing content.
- `tools/code_execution_tool.py:67-916` already bounds durable artifacts and rejects active SVG; Kanban completion preserves declared artifacts through `hermes_cli/kanban_db.py:4277-4417`. Workspace resources reference verified artifacts/receipt evidence; they do not create another blob store.
- The Ink codebase has no explicit screen-reader abstraction today. Existing `useInput` overlays and deterministic text render tests are the fork seam. V1 adds a shared linear semantic projection and keyboard contract; Dashboard adds equivalent HTML roles/labels and manual screen-reader gates.

### New production files

- `agent/workspaces/__init__.py` — stable public exports and `WORKSPACE_SCHEMA_ID`.
- `agent/workspaces/models.py` — strict immutable envelope, origin, component, resource, action-reference, instance, event, binding, and invocation records.
- `agent/workspaces/registry.py` — closed audited definitions for exactly five families and shared semantic projection.
- `agent/workspaces/store.py` — `SessionDB` facade for immutable envelopes, CAS instance state, bindings, append-only events, and invocation projections.
- `agent/workspaces/intake.py` — bounded canonical validation, origin normalization, artifact/Mission/Receipt/MCP projections, and content hashing.
- `agent/workspaces/actions.py` — stable-ID resolution, replay/staleness checks, `AuthorityProvider` call, transaction dispatch, and canonical receipt projection.
- `hermes_cli/workspaces.py` — shared top-level/classic-slash parser and service functions.
- `hermes_cli/workspace_render.py` — Rich/plain semantic CLI renderer; no execution logic.
- `skills/interactive-workspaces/SKILL.md` — complete instructions for producing a file artifact and opening it through existing CLI/file capabilities.
- `ui-tui/src/app/workspaceStore.ts` — feature-owned nanostore for the active immutable envelope, instance revision, focus, and pending action.
- `ui-tui/src/components/workspacePanel.tsx` — native keyboard renderer for all five families.
- `web/src/components/WorkspacePanel.tsx` — secondary accessible Dashboard renderer of the same structured response.
- `benchmarks/workspaces/manifest.yaml` — fixed paired design, metrics, thresholds, safety floors, and exclusions.
- `benchmarks/workspaces/tasks.yaml` — exactly 20 task fixtures.
- `benchmarks/workspaces/run.py`, `benchmarks/workspaces/score.py`, `benchmarks/workspaces/README.md` — local paired runner/scorer and preregistration instructions.
- `website/docs/user-guide/features/interactive-workspaces.md` — user/operator guide.
- `website/docs/developer-guide/workspace-envelope.md` — producer, renderer, authority, origin, and security contract.

### Existing production files modified

- `hermes_state.py` — additive workspace tables and lazy `SessionDB.workspaces` facade.
- `hermes_cli/config.py` — bounded `workspaces:` settings only.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `hermes_cli/cli_commands_mixin.py`, `cli.py` — top-level/classic `/workspace` registration and dispatch.
- `tui_gateway/server.py` — `workspace.exec`, `workspace.action`, `workspace.state.update`, and `workspace.updated` events.
- `ui-tui/src/gatewayTypes.ts`, `ui-tui/src/app/slash/commands/ops.ts`, `ui-tui/src/app/useMainApp.ts`, `ui-tui/src/__tests__/slashParity.test.ts` — native route/types/panel lifecycle.
- `tools/mcp_tool.py` — recognize and validate the reserved `structuredContent["hermes.workspace"]` member after a successful MCP call; never accept action bindings from it.
- `hermes_cli/web_server.py`, `web/src/lib/api.ts`, `web/src/pages/ChatPage.tsx` — profile-scoped secondary fetch/action endpoints and complementary panel.
- `website/sidebars.ts`, `website/docs/reference/cli-commands.md`, `website/docs/reference/slash-commands.md` — documentation navigation/reference.

### Tests created or extended

- `tests/agent/workspaces/test_models.py`
- `tests/agent/workspaces/test_registry.py`
- `tests/agent/workspaces/test_store.py`
- `tests/agent/workspaces/test_intake.py`
- `tests/agent/workspaces/test_actions.py`
- `tests/agent/workspaces/test_security.py`
- `tests/agent/workspaces/test_e2e.py`
- `tests/hermes_cli/test_workspaces.py`
- `tests/tui_gateway/test_workspace_rpc.py`
- `tests/tools/test_mcp_workspace_content.py`
- `tests/benchmarks/test_workspace_benchmark.py`
- `ui-tui/src/__tests__/workspacePanel.test.ts`
- `ui-tui/src/__tests__/workspaceCommand.test.ts`
- `ui-tui/src/__tests__/workspaceAccessibility.test.ts`
- `web/src/components/WorkspacePanel.test.tsx`

---

## Public Interfaces and Ownership

```python
WORKSPACE_SCHEMA_ID = "hermes.workspace.v1"

WorkspaceFamily = Literal[
    "review_form.v1", "comparison_table.v1", "timeline_progress.v1",
    "evidence_artifact.v1", "approval.v1",
]

WorkspaceOriginKind = Literal[
    "mission", "receipt", "artifact", "mcp_structured_content", "user_file"
]

WorkspaceInvocationState = Literal[
    "requested", "blocked", "delegated", "settled", "unknown_effect"
]
```

`WorkspaceEnvelope` contains `schema_id`, `workspace_id`, `title`, optional `summary`, `origin: WorkspaceOrigin`, `components: tuple[WorkspaceComponent, ...]`, `resources: tuple[WorkspaceResourceRef, ...]`, `initial_state`, `created_at_ms`, and optional `expires_at_ms`. It has no command, code, HTML, CSS, script, template, callback, renderer, grant, transaction, or receipt-status field.

`WorkspaceActionRef` contains only `action_id`, `label`, `intent`, `risk_hint`, and `requires_confirmation`. `WorkspaceActionBinding` is stored separately and contains `workspace_id`, `envelope_hash`, `action_id`, `binding_kind`, trusted canonical `ActionContext`, normalized redacted arguments plus hash, `target_ref`, optional `effect_adapter_id`, `created_at_ms`, and `expires_at_ms`.

`WorkspaceRecord` contains the immutable envelope and content hash. `WorkspaceInstance` contains instance/workspace/session IDs, `status`, `WorkspaceState`, integer `state_revision`, and timestamps. `WorkspaceState` contains only non-secret field values, selected option IDs, expanded node IDs, focused node ID, and page offsets. `WorkspaceStatePatch` contains the corresponding bounded partial fields. `WorkspaceView` contains record, instance, semantic tree, and an action-availability map. `WorkspaceSummary` contains IDs, title, origin kind, status, revision, and update time.

`WorkspaceActionRequest` contains `instance_id`, `action_id`, `expected_state_revision`, `idempotency_key`, `requester_id`, and `channel`; profile/origin/arguments/authority come only from trusted runtime state. `WorkspaceActionResult` contains `invocation_id`, invocation state, action ID, authority decision ID, transaction ID, receipt ID, canonical outcome or `None`, resulting revision, and redacted explanation.

`WorkspaceService` exposes these exact calls:

```python
class WorkspaceService:
    def ingest(self, envelope: Mapping[str, Any], *, origin: WorkspaceOrigin,
               bindings: Sequence[WorkspaceActionBinding] = ()) -> WorkspaceRecord: ...
    def open(self, workspace_id: str, *, session_id: str | None) -> WorkspaceView: ...
    def list(self, *, session_id: str | None, status: str = "open") -> tuple[WorkspaceSummary, ...]: ...
    def update_state(self, instance_id: str, patch: WorkspaceStatePatch,
                     *, expected_revision: int, idempotency_key: str) -> WorkspaceView: ...
    def invoke(self, request: WorkspaceActionRequest) -> WorkspaceActionResult: ...
    def close(self, instance_id: str, *, expected_revision: int) -> WorkspaceView: ...
```

Mutating bindings call item #6 exactly as `authorize_effect(provider, action_context, stage="execute", consume=True)`. If allowed, effectful bindings create or load an item #2 `ActionTransaction`/`TransactionRevision`, call `TransactionCoordinator.preview()`, and commit only through `TransactionCoordinator.commit()` with the exact current revision, preview, approval, and authority identities; they never invoke a handler directly. Results reference item #12 `Receipt` and `ArtifactDigest`; only the canonical receipt scorer may return `verified`.

---

### Task 1: Freeze the Envelope and Exact 20-Pair Evaluation Contract

**Files:**
- Create: `benchmarks/workspaces/manifest.yaml`
- Create: `benchmarks/workspaces/tasks.yaml`
- Create: `tests/benchmarks/test_workspace_benchmark.py`

**Interfaces:**
- Consumes: approved item #11 contract and portfolio cross-gates.
- Produces: `workspace-paired-20-v1`, the exact denominator, task IDs, baseline, counterbalancing, metrics, safety floors, and stop rules consumed by Task 11.

- [ ] **Step 1: Write the failing manifest contract test**

```python
def test_workspace_proof_is_exact_and_preregistered():
    manifest, tasks = load_workspace_fixtures()
    assert manifest["schema"] == "hermes.workspace-benchmark.v1"
    assert manifest["version"] == "workspace-paired-20-v1"
    assert manifest["baseline"] == "current_hermes_chat_only"
    assert len(tasks) == 20
    assert Counter(t["family"] for t in tasks) == {
        "plan_review": 4, "multi_option_comparison": 4,
        "mission_monitoring": 4, "evidence_inspection": 4, "approval": 4,
    }
    assert manifest["gates"]["median_time_reduction"] == 0.20
    assert manifest["gates"]["incorrect_action_reduction"] == 0.25
    assert manifest["safety_floors"] == {
        "arbitrary_code_paths": 0, "privilege_bypasses": 0,
        "unreachable_keyboard_actions": 0, "unreachable_screen_reader_actions": 0,
        "incorrect_resumes": 0,
    }
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_workspace_benchmark.py -q`

Expected: FAIL because the workspace benchmark fixtures do not exist.

- [ ] **Step 3: Add the exact 20 paired tasks**

Encode these IDs and meanings, with a deterministic correct answer, seeded state, comprehension questions, allowed actions, forbidden actions, and expected receipt/evidence state:

| ID | Exact paired task |
|---|---|
| `PLAN-01` | Review a four-step patch plan and identify the missing verification step before accepting it. |
| `PLAN-02` | Resolve the single dependency-order error in a six-step recovery plan. |
| `PLAN-03` | Complete a non-secret release-review form containing exactly two validation errors. |
| `PLAN-04` | Find the one declared safety constraint violated by a proposed rollback plan. |
| `COMP-01` | Choose among three library options using six fixed compatibility, cost, and maintenance criteria. |
| `COMP-02` | Compare three remediation diffs and select the least-privileged option that passes every required check. |
| `COMP-03` | Choose one workflow candidate under fixed time and cost ceilings. |
| `COMP-04` | Identify the only option whose evidence freshness satisfies the declared gate. |
| `MISSION-01` | Locate a blocked mission node and name its required review action. |
| `MISSION-02` | Detect the stalled progress segment after a forced restart and select resume. |
| `MISSION-03` | Distinguish one `unknown_effect` from failed, blocked, completed-unverified, and verified work. |
| `MISSION-04` | Resume a mission and identify the next authority-gated action without repeating a settled effect. |
| `EVID-01` | Find the artifact whose current hash differs from its canonical receipt hash. |
| `EVID-02` | Distinguish `completed_unverified` from `verified` using one absent independent check. |
| `EVID-03` | Trace one outcome claim to its receipt evidence and exact artifact reference. |
| `EVID-04` | Identify a missing required check without opening an untrusted external resource. |
| `APPR-01` | Deny a transaction approval whose authority version expired after preview. |
| `APPR-02` | Revise, re-preview, then approve a bounded reversible workspace write. |
| `APPR-03` | Reject an outbound action whose resolved recipient differs from the approved recipient. |
| `APPR-04` | Approve the only candidate whose exact diff, authority version, and irreversible boundary all match. |

The manifest fixes a within-subject paired design, AB/BA counterbalancing by task ID parity, same machine/network class, same artifact inputs, current Hermes chat-only baseline, local monotonic timing, committed-action error counts, comprehension accuracy, restart injection, keyboard path log, screen-reader semantic transcript, excluded/aborted reasons, session-ledger cost, and Wilson 95% intervals for rates. No private history and no aggregate score.

`other_metric_non_worsening` means candidate median completion time is no greater than baseline when the error gate wins, and candidate incorrect committed actions are no greater than baseline when the time gate wins. `no_comprehension_regression` means candidate correct answers are at least baseline over the same 20 pairs; missing answers count incorrect.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_workspace_benchmark.py -q`

Expected: PASS with exactly 20 complete, uniquely identified pairs and immutable thresholds.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/workspaces/manifest.yaml benchmarks/workspaces/tasks.yaml tests/benchmarks/test_workspace_benchmark.py
git commit -m "test: preregister interactive workspace proof"
```

---

### Task 2: Define the Strict Non-Executable Envelope and Closed Component Registry

**Files:**
- Create: `agent/workspaces/__init__.py`
- Create: `agent/workspaces/models.py`
- Create: `agent/workspaces/registry.py`
- Create: `tests/agent/workspaces/test_models.py`
- Create: `tests/agent/workspaces/test_registry.py`

**Interfaces:**
- Produces: `WORKSPACE_SCHEMA_ID`, strict records named in Public Interfaces, `validate_envelope()`, `canonical_envelope_json()`, `envelope_hash()`, `component_definition()`, and `project_semantics()`.
- Consumes: no runtime state; imports item #6 `ActionContext` and item #12 artifact/status names only for binding/result annotations.

- [ ] **Step 1: Write RED schema, limits, and executable-content tests**

```python
@pytest.mark.parametrize("field", ["script", "javascript", "html", "css", "on_click", "renderer"])
def test_executable_or_renderer_fields_are_rejected(field):
    raw = minimal_envelope()
    raw["components"][0][field] = "alert(1)"
    with pytest.raises(WorkspaceValidationError, match="non-executable"):
        validate_envelope(raw)


def test_only_five_audited_families_exist_and_actions_are_references():
    assert set(audited_component_families()) == {
        "review_form.v1", "comparison_table.v1", "timeline_progress.v1",
        "evidence_artifact.v1", "approval.v1",
    }
    action = validate_envelope(approval_envelope()).components[0].actions[0]
    assert action.action_id == "approve-exact-diff"
    assert not hasattr(action, "command")
    assert not hasattr(action, "handler")
```

Also reject: extra fields at every level; duplicate component/resource/action IDs; action references absent from the envelope action index; floats/NaN/Infinity; controls that collect secrets/passwords; `javascript:`/`data:`/`file:` remote references; embedded SVG/HTML; excessive bytes/components/rows/actions/text; timestamps outside integer bounds; expiry before creation; receipt status supplied by an untrusted origin; and canonical-hash changes under key reordering.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/workspaces/test_models.py tests/agent/workspaces/test_registry.py -q`

Expected: FAIL importing `agent.workspaces`.

- [ ] **Step 3: Implement strict immutable records and canonicalization**

Use Pydantic models with `ConfigDict(extra="forbid", frozen=True, strict=True)`. Use integer timestamps and values, tuples rather than mutable lists after validation, canonical JSON with sorted keys/compact separators/UTF-8, and SHA-256. `workspace_id`, component/resource/action IDs match `^[a-z0-9][a-z0-9._:-]{0,127}$`.

The five exact payloads are:

```python
ReviewFormComponent(checklist, fields, validation_messages, actions)
ComparisonTableComponent(criteria, options, cells, selected_option_id, actions)
TimelineProgressComponent(status, progress_ppm, events, checkpoints, actions)
EvidenceArtifactComponent(receipt_ref, claims, evidence_refs, artifact_refs, freshness, actions)
ApprovalComponent(subject, summary, risk, exact_diff, authority_ref, effect_kind, actions)
```

Fields accept only `text`, `multiline`, `single_choice`, `multi_choice`, and `checkbox`; no password/file-upload/rich-HTML control exists in V1. Artifact resources are references with canonical hash/size/MIME, never embedded executable bytes.

- [ ] **Step 4: Implement a closed audited registry and one semantic tree**

```python
@dataclass(frozen=True)
class SemanticNode:
    node_id: str
    role: Literal["workspace", "heading", "group", "field", "table", "row", "status", "link", "button"]
    label: str
    value: str | None
    description: str | None
    action_id: str | None
    children: tuple["SemanticNode", ...]
```

`component_definition(family)` performs a lookup in an immutable module constant. There is no public `register_component()` in V1. `project_semantics(envelope, state)` produces deterministic reading/navigation order used by plain CLI, Ink, Dashboard labels, and accessibility tests. Unknown/disabled action bindings are projected as unavailable with the reason; they are not omitted.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/workspaces/test_models.py tests/agent/workspaces/test_registry.py -q`

Expected: PASS; all five families validate and produce deterministic semantic nodes, while every active-content attempt fails closed.

- [ ] **Step 6: Commit**

```bash
git add agent/workspaces tests/agent/workspaces/test_models.py tests/agent/workspaces/test_registry.py
git commit -m "feat: define audited workspace envelope"
```

---

### Task 3: Persist Immutable Envelopes, Resumable State, Bindings, and Audit Projections

**Files:**
- Create: `agent/workspaces/store.py`
- Modify: `hermes_state.py`
- Create: `tests/agent/workspaces/test_store.py`
- Modify: `tests/test_hermes_state.py`

**Interfaces:**
- Produces: `WorkspaceStore`, lazy `SessionDB.workspaces`, immutable envelope reads, CAS instance updates, append-only events, binding reads, and idempotent invocation projection methods.
- Consumes: Task 2 records and `SessionDB._execute_write()`/`_execute_read()`.

- [ ] **Step 1: Write RED persistence, CAS, replay, and profile-isolation tests**

```python
def test_restart_resumes_exact_state_and_focus(session_db):
    store = session_db.workspaces
    record = store.insert_envelope(envelope_record())
    view = store.open_instance(record.workspace_id, session_id="s1")
    saved = store.update_instance(
        view.instance_id,
        WorkspaceStatePatch(values={"risk": "low"}, focused_node_id="approve"),
        expected_revision=0, idempotency_key="edit-1",
    )
    reopened = SessionDB(session_db.path).workspaces.get_instance(saved.instance_id)
    assert reopened.state_revision == 1
    assert reopened.state.values == {"risk": "low"}
    assert reopened.state.focused_node_id == "approve"


def test_stale_revision_and_replayed_invocation_do_not_duplicate(store):
    with pytest.raises(StaleWorkspaceState):
        store.update_instance("i1", patch(), expected_revision=0, idempotency_key="edit-2")
    first = store.create_invocation(invocation(idempotency_key="act-1"))
    replay = store.create_invocation(invocation(idempotency_key="act-1"))
    assert replay.invocation_id == first.invocation_id
```

Also prove immutable envelope bytes/hash, binding replacement refusal, monotonically increasing event sequence, one open instance per `(workspace_id, session_id)`, close/reopen behavior, no cross-profile lookup, restart after every write boundary, and no secret/raw artifact bytes in tables.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/workspaces/test_store.py tests/test_hermes_state.py -q`

Expected: FAIL because workspace tables and facade do not exist.

- [ ] **Step 3: Add the additive schema**

```sql
CREATE TABLE IF NOT EXISTS workspace_envelopes (
    workspace_id TEXT PRIMARY KEY,
    schema_id TEXT NOT NULL,
    origin_json TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS workspace_instances (
    instance_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace_envelopes(workspace_id),
    session_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('open','closed','superseded')),
    state_json TEXT NOT NULL,
    state_revision INTEGER NOT NULL DEFAULT 0,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE(workspace_id, session_id)
);
CREATE TABLE IF NOT EXISTS workspace_action_bindings (
    workspace_id TEXT NOT NULL REFERENCES workspace_envelopes(workspace_id),
    action_id TEXT NOT NULL,
    envelope_hash TEXT NOT NULL,
    binding_kind TEXT NOT NULL,
    action_context_json TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    effect_adapter_id TEXT,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    PRIMARY KEY(workspace_id, action_id)
);
CREATE TABLE IF NOT EXISTS workspace_events (
    event_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL REFERENCES workspace_instances(instance_id),
    sequence_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE(instance_id, sequence_no),
    UNIQUE(instance_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS workspace_action_invocations (
    invocation_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL REFERENCES workspace_instances(instance_id),
    action_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    expected_state_revision INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    authority_decision_id TEXT,
    transaction_id TEXT,
    receipt_id TEXT,
    outcome TEXT,
    error_code TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE(instance_id, action_id, idempotency_key)
);
```

Use the current additive reconciliation path and do not bump `SCHEMA_VERSION` merely for new tables. Add indexes on open instances, event sequence, invocation state, and binding expiry.

- [ ] **Step 4: Implement bounded transactional methods**

Every update validates/canonicalizes before SQL. Envelope and binding inserts are immutable. Store `session_id or ""` and decode the empty scope to `None`, so SQLite uniqueness also holds for sessionless CLI instances. Instance mutation uses `UPDATE ... WHERE state_revision=?` and appends its event in the same `_execute_write()`. Invocation creation returns the existing row only when the request hash matches; a reused idempotency key with changed data raises `WorkspaceReplayMismatch`. Transition methods use compare-and-set and accept canonical item #12 outcomes only. `target_ref` is an opaque canonical local reference, never a raw recipient or secret.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/workspaces/test_store.py tests/test_hermes_state.py tests/test_hermes_state_wal_fallback.py -q`

Expected: PASS on fresh/reopened databases; stale/replayed writes cannot corrupt or duplicate state.

- [ ] **Step 6: Commit**

```bash
git add agent/workspaces/store.py hermes_state.py tests/agent/workspaces/test_store.py tests/test_hermes_state.py
git commit -m "feat: persist resumable workspace state"
```

---

### Task 4: Ingest Trusted Projections, Artifacts, User Files, and MCP Structured Content

**Files:**
- Create: `agent/workspaces/intake.py`
- Create: `tests/agent/workspaces/test_intake.py`
- Modify: `tools/mcp_tool.py`
- Create: `tests/tools/test_mcp_workspace_content.py`
- Modify: `tests/tools/test_mcp_structured_content.py`

**Interfaces:**
- Produces: `WorkspaceIntake`, `origin_for_mission()`, `origin_for_receipt()`, `origin_for_artifact()`, `origin_for_mcp()`, `project_mission_workspace()`, `project_receipt_workspace()`, and `ingest_mcp_workspace()`.
- Consumes: Tasks 2–3 validation/store, item #1 mission records, item #12 `Receipt`/`ArtifactDigest`, existing artifact paths, and the current MCP `structuredContent` result.

- [ ] **Step 1: Write RED origin, artifact, and MCP boundary tests**

```python
def test_mcp_structured_workspace_is_display_only_without_trusted_binding(intake):
    record = intake.ingest_mcp_workspace(
        server_name="fixture", tool_name="inspect",
        structured={"hermes.workspace": approval_envelope_with_action()},
        session_id="s1",
    )
    view = intake.service.open(record.workspace_id, session_id="s1")
    assert view.actions["approve"].available is False
    assert view.actions["approve"].reason == "no trusted backend binding"


def test_receipt_projection_uses_canonical_artifact_evidence(intake, receipt):
    record = intake.project_receipt_workspace(receipt)
    resource = record.envelope.resources[0]
    assert resource.sha256 == receipt.artifacts[0].sha256
    assert resource.size_bytes == receipt.artifacts[0].size_bytes
```

Test local artifact paths inside the declared workspace/receipt roots, symlink escape, file replacement after hashing, missing/stale artifact, MIME mismatch, active SVG/HTML, oversized file, another profile's artifact, forged mission/receipt ID, MCP server/tool mismatch, MCP error results, duplicate ingestion, malformed reserved member, and preservation of all non-workspace `content`/`structuredContent` fields.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/workspaces/test_intake.py tests/tools/test_mcp_workspace_content.py tests/tools/test_mcp_structured_content.py -q`

Expected: FAIL because intake/projection and MCP reserved-member handling do not exist.

- [ ] **Step 3: Implement explicit origin and sandbox rules**

`WorkspaceOrigin` contains kind, producer ID, active profile ID, optional session/mission/receipt/MCP server/tool IDs, source content hash, and creation time. It never accepts an authority version from producer data. Rules are exact:

- mission/receipt projections load the referenced canonical record from the active profile and create trusted bindings only through their owning service;
- artifact/user-file origins resolve symlinks, require a regular file under an allowed workspace or canonical receipt artifact root, hash bytes before parse, re-stat after parse, and reject drift;
- MCP origin is exactly the connected `server_name + tool_name + call result hash`; it cannot impersonate a mission/receipt/artifact origin;
- HTTP(S) text may be displayed as inert citation text but V1 never fetches/navigates it from a component action;
- all producer-supplied action refs remain disabled unless a first-party projector supplies the separately typed binding sequence.

- [ ] **Step 4: Add mission and receipt projectors without duplicate schemas**

`project_mission_workspace()` maps mission status/events/checkpoints/review items into `timeline_progress.v1` plus `approval.v1` only when item #1 exposes an actual pending review action. `project_receipt_workspace()` maps canonical claims/evidence/freshness/artifacts into `evidence_artifact.v1`. Store only source IDs/hashes and redacted summaries; retrieve authoritative current records when an action runs.

The projector must never translate workflow success into `verified`. It displays the exact canonical receipt status. If #12 is unavailable at import time, receipt projection returns `producer_unavailable`; it does not synthesize a compatibility receipt.

- [ ] **Step 5: Recognize the reserved MCP member after successful calls**

In `_make_tool_handler()`, after `result.isError` is false and `structuredContent` exists, pass only `structured["hermes.workspace"]` plus handler-owned `server_name`, `tool_name`, and call context from `kwargs` to `ingest_mcp_workspace()`. Return the existing JSON result plus:

```json
{"hermesWorkspace": {"workspace_id": "...", "content_hash": "...", "actions_bound": 0}}
```

Validation failure appends a bounded `hermesWorkspaceError` string while preserving ordinary MCP content. It does not disconnect the server, execute an action, or expose a traceback. Existing structured-content behavior stays byte-for-byte unchanged when the reserved member is absent.

- [ ] **Step 6: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/workspaces/test_intake.py tests/tools/test_mcp_workspace_content.py tests/tools/test_mcp_structured_content.py tests/tools/test_mcp_resource_content.py -q`

Expected: PASS; mission/receipt/artifact projections are origin-bound and MCP can create only validated display state without privileged bindings.

- [ ] **Step 7: Commit**

```bash
git add agent/workspaces/intake.py tools/mcp_tool.py tests/agent/workspaces/test_intake.py tests/tools/test_mcp_workspace_content.py tests/tools/test_mcp_structured_content.py
git commit -m "feat: ingest bounded workspace producers"
```

---

### Task 5: Bind Stable Actions to Authority, Transactions, and Canonical Receipts

**Files:**
- Create: `agent/workspaces/actions.py`
- Create: `tests/agent/workspaces/test_actions.py`
- Modify: `agent/workspaces/__init__.py`
- Modify: `tests/agent/workspaces/test_store.py`

**Interfaces:**
- Produces: `WorkspaceActionRequest`, `WorkspaceActionResult`, `WorkspaceActionHandler` protocol, closed `BUILTIN_ACTION_HANDLERS`, `bind_workspace_action()`, and `WorkspaceService.invoke()`.
- Consumes: Task 3 store; item #6 `AuthorityProvider`, `ActionContext`, `AuthorityDecision`, `authorize_effect()`; item #2 `TransactionStore.create_transaction()`, `TransactionCoordinator.preview()`/`.commit()`, revision/effect results and exact approval binding; item #12 `Receipt`, `ReceiptStatus`, and artifact/evidence references.

- [ ] **Step 1: Write RED identity, authority, transaction, replay, and truthfulness tests**

```python
def test_stable_action_reloads_authority_and_delegates_effect(service, fakes):
    request = WorkspaceActionRequest(
        instance_id="i1", action_id="approve-exact-diff",
        expected_state_revision=3, idempotency_key="press-1",
        requester_id="user-1", channel="tui",
    )
    result = service.invoke(request)
    assert fakes.authority.context == stored_binding().action_context
    assert fakes.authority.consume is True
    assert fakes.transactions.operation_key == "workspace:i1:approve-exact-diff:press-1"
    assert result.transaction_id == fakes.transactions.transaction_id


@pytest.mark.parametrize("drift", [
    "state_revision", "envelope_hash", "binding_expiry", "authority_version",
    "arguments_hash", "requester", "channel", "profile", "origin",
])
def test_stale_or_mismatched_action_has_zero_effect_calls(service, drift):
    result = service.invoke(request_with(drift))
    assert result.error_code in {"stale_action", "authority_changed", "binding_mismatch"}
    assert service.effect_calls == 0
```

Also cover deny, ask, exact existing approval transport, consumed mandate, duplicate button press, same idempotency key/different request, crash before authority, crash after authority/before transaction, crash after external effect/before projection, transaction `unknown_effect`, compensation request, canonical receipt reference, missing receipt, and a handler/model claim attempting to force `verified`.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/workspaces/test_actions.py tests/agent/workspaces/test_store.py -q`

Expected: FAIL importing workspace action orchestration.

- [ ] **Step 3: Implement a closed handler table and trusted binding API**

First-party `binding_kind` values are finite:

```python
BuiltinBindingKind = Literal[
    "workspace.state.submit", "mission.review.resolve", "mission.reconcile",
    "mission.compensate", "receipt.recheck", "transaction.approve",
    "transaction.deny", "artifact.inspect",
]
```

There is no generic command, URL, Python import, function name, shell argv, or plugin callback binding. `bind_workspace_action()` is callable only with an already constructed canonical `ActionContext` and normalized target from the owning first-party projector. It verifies that the action ID exists in the immutable envelope and stores the binding with the envelope hash and at most 24-hour expiry.

- [ ] **Step 4: Implement the exact invocation algorithm**

```text
load instance/envelope/binding in active profile
verify open instance + expected revision + envelope hash + origin + expiry
insert/replay-check invocation identity before any effect
reload authoritative target and reconstruct trusted ActionContext
authorize_effect(provider, context, stage="execute", consume=True)
deny -> blocked; ask -> existing exact approval/clarify path, then re-authorize
read-only artifact inspect -> verify current hash, append audit event
mutating action -> TransactionStore.create_transaction() -> TransactionCoordinator.preview()/commit()
project transaction/receipt IDs and canonical outcome into invocation/event
ambiguous disposition -> unknown_effect; never blind retry
CAS instance revision and emit workspace.updated only after durable projection
```

Never hold a SQLite transaction across approval, model, adapter, filesystem, network, or transaction-coordinator work. On restart, an invocation in `delegated` queries the existing transaction by stable operation key and reconciles it; it never calls the handler again unless the transaction contract proves safe retry.

- [ ] **Step 5: Preserve receipt and effect vocabulary**

`WorkspaceActionResult` contains invocation state, authority decision ID, transaction ID, receipt ID, canonical outcome, new instance revision, and a redacted explanation. It cannot manufacture a `Receipt`. A local form state submission without an external effect returns no receipt status; UI copy says “saved,” not “verified.” Only a referenced canonical receipt may display `verified`.

- [ ] **Step 6: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/workspaces/test_actions.py tests/agent/workspaces/test_store.py tests/agent/test_operation_journal.py -q`

Expected: PASS; every effect crosses current authority/transaction paths, stale/replayed requests have zero duplicate effects, and truthful receipt vocabulary is preserved.

- [ ] **Step 7: Commit**

```bash
git add agent/workspaces/actions.py agent/workspaces/__init__.py tests/agent/workspaces/test_actions.py tests/agent/workspaces/test_store.py
git commit -m "feat: bind workspace actions to authority"
```

---

### Task 6: Deliver the First-Class Classic CLI and Plain/Rich Renderers

**Files:**
- Create: `hermes_cli/workspaces.py`
- Create: `hermes_cli/workspace_render.py`
- Create: `skills/interactive-workspaces/SKILL.md`
- Create: `tests/hermes_cli/test_workspaces.py`
- Modify: `hermes_cli/config.py`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/main.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Modify: `cli.py`
- Modify: `tests/hermes_cli/test_commands.py`

**Interfaces:**
- Produces: `build_parser()`, `workspace_command(args) -> int`, `run_argv(argv, *, output_mode="text") -> WorkspaceCommandResponse`, and `render_workspace(view, *, mode) -> str`.
- Consumes: `WorkspaceService`, Task 2 semantic projection, existing Rich console/classic slash patterns, and existing approval callbacks.

- [ ] **Step 1: Write RED command, renderer, and parser-parity tests**

```python
def test_top_level_and_slash_render_the_same_semantics(cli, envelope_file):
    top = cli.run_top(["workspace", "open", str(envelope_file), "--plain"])
    slash = cli.run_slash(f"open {envelope_file} --plain")
    assert top.exit_code == slash.exit_code == 0
    assert top.semantic_lines == slash.semantic_lines
    assert "Approve exact diff [action: approve-exact-diff]" in top.output


def test_action_requires_exact_revision_and_idempotency(cli, workspace):
    result = cli.run(
        f"action {workspace.instance_id} approve-exact-diff "
        "--revision 3 --idempotency-key press-1"
    )
    assert result.request.expected_state_revision == 3
```

Test JSON/text output, all five component families, validation errors, stale revision, disabled/unbound actions, non-interactive approval fail-closed, `--plain` deterministic reading order, terminal-width fallbacks, rows over the page size, secret redaction, profile isolation, missing artifact, close/resume, and every unrecognized trailing argument.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_workspaces.py tests/hermes_cli/test_commands.py -q`

Expected: FAIL because the workspace CLI and renderers do not exist.

- [ ] **Step 3: Implement the exact command grammar**

```text
hermes workspace validate <envelope.json>
hermes workspace open <envelope.json> [--session ID] [--plain|--json]
hermes workspace open --mission <mission-id> [--session ID] [--plain|--json]
hermes workspace open --receipt <receipt-id> [--session ID] [--plain|--json]
hermes workspace list [--session ID] [--status open|closed] [--json]
hermes workspace show <instance-id> [--plain|--json]
hermes workspace resume <instance-id> [--plain|--json]
hermes workspace state <instance-id> --revision N --idempotency-key KEY --patch <patch.json>
hermes workspace action <instance-id> <action-id> --revision N --idempotency-key KEY
hermes workspace events <instance-id> [--limit N] [--json]
hermes workspace close <instance-id> --revision N
hermes workspace accessibility <on|off|status>
```

Input JSON and state-patch JSON are UTF-8 and bounded before parse. Exit codes: 0 success/render, 2 validation/stale request, 3 authority denied/approval unavailable, 4 transaction/store/recovery failure. `workspaces.screen_reader_mode` defaults false in `config.yaml`; adding it does not require a config-version bump.

- [ ] **Step 4: Implement semantic Rich/plain rendering**

Rich mode uses tables, diffs, progress bars, headings, and pagers but preserves the semantic order. Plain/screen-reader mode emits one stable line per semantic node with role, label, value, state, and action ID; it contains no spinner, color-only meaning, cursor-addressed overwrite, glyph-only control, or hidden disabled action. Narrow terminals fall back to vertical key/value rows rather than truncating decision facts.

State edits never invoke an action implicitly. `workspace action` re-renders the exact subject/diff/risk/authority summary, then calls `WorkspaceService.invoke()`; existing approval UI remains the only approval prompt.

- [ ] **Step 5: Register classic and top-level surfaces**

Add `CommandDef("workspace", "Open and operate interactive task workspaces", "Tools & Skills", aliases=("ws",), args_hint="[open|list|show|resume|state|action|events|close]")`. Wire `main.py` beside workflow, `_handle_workspace_command()` beside `_handle_workflow_command()`, and `elif canonical == "workspace"` in `cli.py`. Do not register a gateway messaging command or Desktop command.

- [ ] **Step 6: Write the producer skill**

The complete skill instructs the agent to write `hermes.workspace.v1` JSON through existing file tools, run `hermes workspace validate`, then open it. It lists the five exact families and limits, forbids bindings/code/HTML/CSS/JS/secrets/receipt claims, tells the agent to use mission/receipt projectors for privileged actions, and says an unbound MCP/file action is intentionally display-only. Publishing/installing a changed skill affects only a new conversation unless the user explicitly chooses the existing cache-invalidating path.

- [ ] **Step 7: Run GREEN and smoke tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_workspaces.py tests/hermes_cli/test_commands.py tests/cli/test_cli_approval_ui.py -q`

Expected: PASS; top-level/classic output agrees, approval reuse works, and `/workspace` is discoverable.

Run: `python -m hermes_cli.main workspace --help`

Expected: bounded subcommands are listed without starting Dashboard, Desktop, or a model call.

- [ ] **Step 8: Commit**

```bash
git add hermes_cli/workspaces.py hermes_cli/workspace_render.py skills/interactive-workspaces/SKILL.md hermes_cli/config.py hermes_cli/commands.py hermes_cli/main.py hermes_cli/cli_commands_mixin.py cli.py tests/hermes_cli/test_workspaces.py tests/hermes_cli/test_commands.py
git commit -m "feat: add terminal workspace controls"
```

---

### Task 7: Add the First-Class Native Ink Workspace Panel

**Files:**
- Modify: `tui_gateway/server.py`
- Modify: `ui-tui/src/gatewayTypes.ts`
- Create: `ui-tui/src/app/workspaceStore.ts`
- Create: `ui-tui/src/components/workspacePanel.tsx`
- Modify: `ui-tui/src/app/slash/commands/ops.ts`
- Modify: `ui-tui/src/app/useMainApp.ts`
- Create: `tests/tui_gateway/test_workspace_rpc.py`
- Create: `ui-tui/src/__tests__/workspacePanel.test.ts`
- Create: `ui-tui/src/__tests__/workspaceCommand.test.ts`
- Modify: `ui-tui/src/__tests__/slashParity.test.ts`

**Interfaces:**
- Produces JSON-RPC `workspace.exec`, `workspace.state.update`, and `workspace.action`; event `workspace.updated`; typed `WorkspaceViewResponse`/`WorkspaceActionResponse`; feature-owned `$workspace` nanostore; native `/workspace` handler.
- Consumes: shared `hermes_cli.workspaces.run_argv(..., output_mode="structured")`, `WorkspaceService`, existing `approval.request`/`approval.respond`, existing session transport/stale-session guards, and Task 2 semantic nodes.

- [ ] **Step 1: Write RED RPC, native-route, replay, and event tests**

```python
def test_workspace_action_rpc_is_session_bound_and_structured(rpc, workspace):
    result = rpc("workspace.action", {
        "session_id": "s1", "instance_id": workspace.instance_id,
        "action_id": "approve", "expected_revision": 2,
        "idempotency_key": "key-enter-1",
    })
    assert result["instance"]["state_revision"] == 3
    assert result["action"]["action_id"] == "approve"
    assert result["action"]["receipt_status"] in {None, "verified", "completed_unverified", "failed", "blocked", "unknown_effect"}
```

```typescript
it('routes /workspace action through native workspace.action, never slash.exec', () => {
  findSlashCommand('workspace')!.run('action i1 approve --revision 2 --idempotency-key k1', ctx, '/workspace action i1 approve --revision 2 --idempotency-key k1')
  expect(ctx.gateway.rpc).toHaveBeenCalledWith('workspace.action', expect.objectContaining({
    session_id: 'sid-1', instance_id: 'i1', action_id: 'approve', expected_revision: 2
  }))
  expect(ctx.gateway.gw.request).not.toHaveBeenCalledWith('slash.exec', expect.anything())
})
```

Test invalid argv/count/bytes, another session/profile, stale event after newer revision, duplicate Enter, reconnect/reopen, approval overlay reuse, action denial, disabled action visibility, and no event before state is durable.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/tui_gateway/test_workspace_rpc.py -q`

Expected: FAIL with unknown workspace RPC methods.

Run: `cd ui-tui && npm test -- --run src/__tests__/workspacePanel.test.ts src/__tests__/workspaceCommand.test.ts src/__tests__/slashParity.test.ts`

Expected: FAIL because the native panel/route do not exist.

- [ ] **Step 3: Implement bounded live-process RPCs**

`workspace.exec` accepts at most 64 argv entries/64 KiB and returns the structured CLI response. `workspace.state.update` and `workspace.action` require a live session, exact instance/revision, a client-generated idempotency key, and active profile match. Convert validation/staleness/authority errors to bounded 4xxx responses and store/transaction errors to redacted 5xxx responses. After the service commits, emit `workspace.updated` with instance ID, workspace ID, revision, changed semantic-node IDs, and optional canonical outcome—never field values, artifact bytes, arguments, or secrets.

- [ ] **Step 4: Implement the nanostore and all-five-family panel**

The store owns active view, revision, focused semantic-node index, draft non-secret field values, pending action ID, error, and screen-reader mode. The panel renders from semantic nodes and family payloads; it never evaluates Markdown/HTML or imports a producer renderer.

Exact keyboard contract:

| Key | Behavior |
|---|---|
| `Tab` / `Shift+Tab` | Next/previous interactive semantic node, including disabled actions for explanation |
| `Up/Down` | Previous/next row, timeline event, field, or action |
| `Left/Right` | Previous/next comparison option or finite choice |
| `Space` | Toggle checkbox/multi-choice only |
| `Enter` | Edit/select or invoke the focused available action once |
| `e` | Enter/leave field-edit mode |
| `PgUp/PgDn` | Page bounded tables/timelines |
| `r` | Reload persisted instance; discard no dirty draft without confirmation |
| `Esc` | Leave edit mode, then close panel; never deny/approve implicitly |

While existing approval/clarify overlays are active, their `useInput` handler owns input and the workspace panel is inactive. Every mutation sends current revision/idempotency identity and applies only a response/event with a higher revision.

- [ ] **Step 5: Add native command parity and lifecycle**

Add `workspace`/`ws` to `opsCommands`, `NATIVE_MUTATING_COMMANDS`, and `MUTATING_COMMANDS`. Opening/resuming mounts the panel without replacing the transcript/composer. Session switch closes only the visual attachment; persisted state remains. Gateway reconnect reloads the active instance. A missing local handler must fail closed instead of falling through `slash.exec` for `state`, `action`, or `close`.

- [ ] **Step 6: Run GREEN and typecheck**

Run: `scripts/run_tests.sh tests/tui_gateway/test_workspace_rpc.py -q`

Expected: PASS.

Run: `cd ui-tui && npm test -- --run src/__tests__/workspacePanel.test.ts src/__tests__/workspaceCommand.test.ts src/__tests__/slashParity.test.ts src/__tests__/approvalAction.test.ts && npm run typecheck`

Expected: PASS; all five families render, keyboard state is deterministic, mutations are native, and the existing approval overlay remains authoritative.

- [ ] **Step 7: Commit**

```bash
git add tui_gateway/server.py ui-tui/src/gatewayTypes.ts ui-tui/src/app/workspaceStore.ts ui-tui/src/components/workspacePanel.tsx ui-tui/src/app/slash/commands/ops.ts ui-tui/src/app/useMainApp.ts tests/tui_gateway/test_workspace_rpc.py ui-tui/src/__tests__/workspacePanel.test.ts ui-tui/src/__tests__/workspaceCommand.test.ts ui-tui/src/__tests__/slashParity.test.ts
git commit -m "feat: add native ink workspace panel"
```

---

### Task 8: Add the Secondary Dashboard Panel Without Creating a Second Chat Surface

**Files:**
- Modify: `hermes_cli/web_server.py`
- Modify: `web/src/lib/api.ts`
- Create: `web/src/components/WorkspacePanel.tsx`
- Create: `web/src/components/WorkspacePanel.test.tsx`
- Modify: `web/src/components/ChatSidebar.tsx`
- Modify: `web/src/pages/ChatPage.tsx`

**Interfaces:**
- Produces: profile-scoped `GET /api/workspaces`, `GET /api/workspaces/{instance_id}`, `PATCH /api/workspaces/{instance_id}/state`, and `POST /api/workspaces/{instance_id}/actions/{action_id}`; typed Dashboard client; complementary accessible panel.
- Consumes: the same `WorkspaceService`/semantic nodes, dashboard auth/CSRF/profile scope, and passive `workspace.updated` events from the PTY event feed.

- [ ] **Step 1: Write RED API, scope, secondary-surface, and rendered-semantics tests**

```python
def test_dashboard_action_uses_stored_binding_and_never_body_command(client, workspace):
    response = client.post(
        f"/api/workspaces/{workspace.instance_id}/actions/approve",
        json={"expected_revision": 2, "idempotency_key": "dash-1", "command": "rm -rf /"},
    )
    assert response.status_code == 422
    assert workspace.effect_calls == 0
```

```typescript
it('renders semantic roles, exact action labels, and unavailable reasons', () => {
  const html = renderToStaticMarkup(<WorkspacePanel view={fixtureView} readOnly={false} />)
  expect(html).toContain('role="region"')
  expect(html).toContain('aria-labelledby="workspace-title-w1"')
  expect(html).toContain('aria-label="Approve exact diff"')
  expect(html).toContain('No trusted backend binding')
})
```

Python tests cover auth/CSRF, invalid/cross-profile instance, stale revision, request limits, no arbitrary body fields, no secret/raw artifact bytes, no await while `_profile_scope` holds its process-global lock, ask/approval-required response with zero effects, and action replay. TypeScript tests cover all five families, event revision ordering, action disabled state, focus labels, non-destructive error rendering, and continued terminal-pane availability.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_web_server.py -k workspace -q`

Expected: FAIL with 404 workspace endpoints.

Run: `cd web && npm test -- --run src/components/WorkspacePanel.test.tsx`

Expected: FAIL because the Dashboard component/client do not exist.

- [ ] **Step 3: Implement bounded profile-scoped APIs**

Validate path/body synchronously; run the complete service call inside a worker thread with the shortest existing `_profile_scope`; never hold that scope across an await. Request bodies use strict Pydantic models and accept only revision, idempotency key, and typed state patch. The server derives requester/channel/profile/origin from authenticated context and stored binding.

If authority returns `ask`, respond `409` with `{code: "primary_approval_required", action_id, explanation}` and zero effect calls; users approve through the existing primary CLI/Ink prompt. `deny`, stale, and replay mismatches are distinct bounded responses. An already-allowed action still crosses `WorkspaceService.invoke()` and the transaction coordinator.

- [ ] **Step 4: Implement a complementary secondary panel**

Add a collapsible “Workspace” section within the existing `ChatSidebar`, driven by the PTY event feed's latest instance ID and REST fetches. It does not own transcript, composer, model session, or PTY state. Render native HTML tables, progress, lists, fieldsets, buttons, and links from the shared structured response—never `dangerouslySetInnerHTML` and never a producer component.

Use `role="region"`, a visible labelled heading, native form labels, `<table>` captions/headers, `aria-current` for selected comparison state, `<progress>` plus text, `aria-live="polite"` for durable updates, and `role="alert"` only for actionable errors. On ask, keep the terminal working and announce “Approval required in CLI/TUI.”

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_web_server.py -k workspace -q`

Expected: PASS with profile isolation and strict action bodies.

Run: `cd web && npm test -- --run src/components/WorkspacePanel.test.tsx src/lib/api.test.ts && npm run typecheck`

Expected: PASS; Dashboard renders the same semantics secondarily and never becomes a second chat surface.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/web_server.py web/src/lib/api.ts web/src/components/WorkspacePanel.tsx web/src/components/WorkspacePanel.test.tsx web/src/components/ChatSidebar.tsx web/src/pages/ChatPage.tsx
git commit -m "feat: add secondary dashboard workspaces"
```

---

### Task 9: Prove Real-Path Crash Recovery, Replay Safety, Origin Isolation, and Cache Invariants

**Files:**
- Create: `tests/agent/workspaces/test_e2e.py`
- Create: `tests/agent/workspaces/test_security.py`
- Modify: `tests/agent/workspaces/test_actions.py`
- Modify: `tests/tools/test_mcp_workspace_content.py`
- Modify: `tests/tui_gateway/test_workspace_rpc.py`

**Interfaces:**
- Consumes: complete Tasks 2–8 implementation plus landed item #1/#6/#12 contracts.
- Produces: no new public production interface; this task is the release security/recovery gate.

- [ ] **Step 1: Write the real-path temp-`HERMES_HOME` scenario**

Create a temporary profile home, real `SessionDB`, real config, real mission/receipt fixtures through their public stores, a real workspace artifact file, real CLI intake, real TUI RPC dispatch, and a disposable Git worktree for the bounded effect. Use a fake external effect only at the final adapter boundary.

Parameterize process interruption at:

1. after immutable envelope insert/before instance open;
2. after state event/before response;
3. after invocation insert/before authority;
4. after authority/before transaction dispatch;
5. after effect handler return/before transaction confirmation;
6. after transaction confirmation/before workspace projection;
7. after projection/before `workspace.updated` emit.

At each boundary, destroy the object graph, reopen stores in a fresh process or fresh `SessionDB`, resume the instance, and replay the same action identity. Assert one external effect, one transaction, one invocation, monotonically increasing state revision, preserved focus/form state, and canonical receipt/effect outcome.

- [ ] **Step 2: Add stale-action and adversarial origin/security cases**

```python
@pytest.mark.parametrize("attack", [
    "model_embeds_javascript", "model_embeds_shell_binding", "mcp_forges_authority",
    "mcp_forges_receipt_verified", "artifact_symlink_escape", "artifact_changes_after_hash",
    "cross_profile_workspace_id", "cross_session_action", "stale_revision_replay",
    "binding_expired", "binding_argument_drift", "approval_requester_drift",
    "approval_channel_drift", "unknown_component", "oversized_component",
    "ssrf_resource_uri", "html_svg_active_content", "secret_form_field",
    "prompt_claims_user_approved", "transaction_unknown_blind_retry",
])
def test_attack_never_expands_workspace_authority(security_harness, attack):
    result = security_harness.attempt(attack)
    assert result.effect_calls == 0
    assert result.allowed is False
    assert security_harness.no_secret_in_db_logs_events_or_output()
```

Threat-model prompt injection, confused delegation, SSRF, replay, privilege drift, derived-memory leakage, compromised plugin/MCP producers, malicious artifact paths, stale approval, and cross-profile/cross-session multiplexing. Producer text cannot select a trusted origin, action context, binding kind, transaction ID, receipt status, profile, requester, or channel.

- [ ] **Step 3: Add partial-failure and rollback/recovery cases**

Prove authority store unavailable, workspace DB busy, mission/receipt record missing, MCP disconnect, artifact disappears, transaction reconciliation unavailable, event emit failure, and Dashboard/Ink renderer crash. Fail closed before effects where disposition is known; surface `unknown_effect` where it is not. Compensation is requested through item #2's transaction path and never overwrites newer workspace/artifact drift. UI failures do not mutate durable effect truth.

- [ ] **Step 4: Add prompt-cache, tool-schema, provider/model, and role tests**

Run a multi-turn real agent harness and independently hash system message bytes, effective tool-definition JSON, provider, and model before/after file-produced workspace intake, MCP-produced intake, state save, approval ask/deny/allow, action settlement, receipt projection, restart/resume, and close. Assert all four identities unchanged, strict role alternation, no history mutation outside compression, and no synthetic user message. Assert `WorkspaceEnvelope`, component registry, action binding, and MCP reserved-member metadata never appear in serialized model tool definitions.

- [ ] **Step 5: Run RED before recovery wiring is complete**

Run: `scripts/run_tests.sh tests/agent/workspaces/test_e2e.py tests/agent/workspaces/test_security.py tests/agent/workspaces/test_actions.py tests/tools/test_mcp_workspace_content.py tests/tui_gateway/test_workspace_rpc.py -q`

Expected: FAIL at injected restart/replay/security boundaries until Tasks 3–8 preserve every invariant.

- [ ] **Step 6: Make the smallest owning-module corrections and run GREEN**

Corrections stay in Tasks 2–8 owning files and preserve all public types/limits. Do not weaken assertions, add a compatibility permission system, or retry unknown effects.

Run: `scripts/run_tests.sh tests/agent/workspaces tests/hermes_cli/test_workspaces.py tests/tui_gateway/test_workspace_rpc.py tests/tools/test_mcp_workspace_content.py tests/tools/test_mcp_structured_content.py tests/agent/test_operation_journal.py tests/test_get_tool_definitions_cache_isolation.py -q`

Expected: PASS; restarts converge, all attacks fail closed, effects do not duplicate, profiles/origins isolate, and cache/role invariants hold.

- [ ] **Step 7: Commit**

```bash
git add tests/agent/workspaces/test_e2e.py tests/agent/workspaces/test_security.py tests/agent/workspaces/test_actions.py tests/tools/test_mcp_workspace_content.py tests/tui_gateway/test_workspace_rpc.py agent/workspaces tui_gateway/server.py tools/mcp_tool.py
git commit -m "test: prove workspace recovery and isolation"
```

---

### Task 10: Prove Keyboard and Screen-Reader Reachability Across Every Family

**Files:**
- Create: `ui-tui/src/__tests__/workspaceAccessibility.test.ts`
- Modify: `ui-tui/src/__tests__/workspacePanel.test.ts`
- Modify: `tests/hermes_cli/test_workspaces.py`
- Modify: `web/src/components/WorkspacePanel.test.tsx`
- Modify: `tests/benchmarks/test_workspace_benchmark.py`
- Create: `benchmarks/workspaces/accessibility-checklist.md`

**Interfaces:**
- Consumes: Task 2 semantic projection, Task 6 plain renderer, Task 7 keyboard state machine, Task 8 semantic HTML.
- Produces: machine-readable action reachability evidence and the manual assistive-technology release checklist; no new runtime interface.

- [ ] **Step 1: Write RED semantic equivalence and action-reachability tests**

For one maximal fixture per family, collect action IDs from the validated envelope, CLI plain semantic lines, Ink navigation model, and Dashboard rendered HTML. Assert identical action-ID sets, identical label/available-state meaning, and a finite keyboard path from initial focus to every action.

```typescript
it.each(maximalFamilyFixtures)('reaches and announces every action in %s', (_name, fixture) => {
  const model = createWorkspaceNavigationModel(fixture.view)
  for (const action of fixture.actions) {
    const path = shortestKeyboardPath(model, action.action_id)
    expect(path.length).toBeGreaterThan(0)
    const focused = replayKeys(model, path)
    expect(focused.semantic.action_id).toBe(action.action_id)
    expect(announce(focused.semantic)).toContain(action.label)
    expect(announce(focused.semantic)).toContain(action.available ? 'available' : 'unavailable')
  }
})
```

Test empty/large tables, long labels, Unicode/wide characters, color disabled, 40-column terminal, stale action, validation error, pending approval, unknown effect, and resumed focus.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_workspaces.py tests/benchmarks/test_workspace_benchmark.py -q -k accessibility`

Expected: FAIL until CLI semantic reachability evidence is exposed.

Run: `cd ui-tui && npm test -- --run src/__tests__/workspaceAccessibility.test.ts src/__tests__/workspacePanel.test.ts`

Expected: FAIL until the navigation/announcement contract is complete.

Run: `cd web && npm test -- --run src/components/WorkspacePanel.test.tsx`

Expected: FAIL until native roles/labels/states match the semantic projection.

- [ ] **Step 3: Implement deterministic screen-reader output and focus restoration**

The CLI plain renderer is the canonical screen-reader transcript. Ink screen-reader mode emits the active node as append-only plain text—`role; label; value; position; available state; action id; help`—and suppresses cursor-rewrite animation. Focus restoration uses semantic node ID, falling forward to the next node if removed. No status is conveyed by color/glyph alone. Validation errors identify the field and recovery key. Progress includes numeric percent and status text.

Dashboard uses native elements and visible focus. The component never traps focus; `Esc` returns to the workspace heading/terminal control; updates announce once after durable revision; disabled actions remain described with `aria-disabled` and reason.

- [ ] **Step 4: Add manual assistive-technology gates**

`accessibility-checklist.md` records tester, build SHA, OS/terminal/browser, fixture IDs, action IDs, observed announcements, failures, and artifacts for:

- NVDA + Windows Terminal for plain CLI and native Ink;
- VoiceOver + Terminal for plain CLI and native Ink;
- Orca + a supported Linux terminal for plain CLI and native Ink;
- NVDA + Chrome and VoiceOver + Safari for the secondary Dashboard.

For each of the five maximal fixtures: read title/summary, navigate all semantic nodes, edit a field where present, inspect disabled reasons, invoke/deny one bounded action, observe stale/error/unknown states, restart, and resume focus/state. Every action must be discoverable, named, state-announced, and operable without a mouse. Any failure is a release blocker, not an averaged metric.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_workspaces.py tests/benchmarks/test_workspace_benchmark.py -q -k accessibility`

Expected: PASS with zero missing semantic actions and exact resumed focus.

Run: `cd ui-tui && npm test -- --run src/__tests__/workspaceAccessibility.test.ts src/__tests__/workspacePanel.test.ts && npm run typecheck`

Expected: PASS; all actions have finite keyboard paths and complete announcements.

Run: `cd web && npm test -- --run src/components/WorkspacePanel.test.tsx && npm run typecheck`

Expected: PASS; semantic HTML exposes the same action names/states.

- [ ] **Step 6: Commit**

```bash
git add ui-tui/src/__tests__/workspaceAccessibility.test.ts ui-tui/src/__tests__/workspacePanel.test.ts tests/hermes_cli/test_workspaces.py web/src/components/WorkspacePanel.test.tsx tests/benchmarks/test_workspace_benchmark.py benchmarks/workspaces/accessibility-checklist.md
git commit -m "test: prove workspace accessibility"
```

---

### Task 11: Run the Paired Proof, Document Operations, and Gate Rollout

**Files:**
- Create: `benchmarks/workspaces/run.py`
- Create: `benchmarks/workspaces/score.py`
- Create: `benchmarks/workspaces/README.md`
- Modify: `tests/benchmarks/test_workspace_benchmark.py`
- Create: `website/docs/user-guide/features/interactive-workspaces.md`
- Create: `website/docs/developer-guide/workspace-envelope.md`
- Modify: `website/sidebars.ts`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`

**Interfaces:**
- Produces: `run_pairs(manifest_path, tasks_path, mode, output_dir)`, `score_pairs(baseline_path, candidate_path) -> WorkspaceBenchmarkReport`, local `results.json`, and `report.md`.
- Consumes: Task 1 fixtures, real CLI/service/renderers, Task 10 reachability evidence, and complete implementation.

- [ ] **Step 1: Write RED denominator, paired-metric, and safety-floor scorer tests**

```python
def test_paired_gate_requires_exact_denominator_and_disjunction_without_tradeoff():
    report = score_pairs(*complete_synthetic_runs(
        time_reduction=0.21, incorrect_action_reduction=0.00,
        candidate_errors_equal_baseline=True, comprehension_regression=False,
    ))
    assert report.denominator == 20
    assert report.passed is True
    assert report.safety_failures == []


def test_faster_run_cannot_trade_away_one_incorrect_committed_action():
    report = score_pairs(*complete_synthetic_runs(
        time_reduction=0.40, candidate_errors_equal_baseline=False,
    ))
    assert report.passed is False
```

Also assert missing/duplicate/aborted pair remains in the denominator, AB/BA order balance, comprehension non-regression, exact family slices, confidence intervals, zero safety-floor tolerance, restart correctness, and no post-hoc threshold changes.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_workspace_benchmark.py -q`

Expected: FAIL because the runner/scorer are incomplete.

- [ ] **Step 3: Implement paired runner and scorer**

Baseline uses current chat-only Hermes with no workspace commands/panel. Candidate uses the same task inputs and allowed authority with workspace mode interactive. Record per pair: order, start/end monotonic timestamps, correct completion, incorrect committed action count, comprehension answers, approval/clarification count, restart state/focus correctness, keyboard path/action IDs, screen-reader action IDs, effect/authority/transaction/receipt IDs, cache identity hashes, session-ledger cost, exclusion/abort reason, and safety flags.

Compute paired median time reduction and total incorrect-action reduction using fixed definitions. Report all 20 pairs, five family slices, exact denominators, bootstrap 95% CI for paired median difference, Wilson 95% intervals for binary rates, p50/p95 latency, cost per correct completion, and every exclusion. Do not invent a composite score or omit a safety failure.

- [ ] **Step 4: Execute the exact proof commands**

Run: `python benchmarks/workspaces/run.py --manifest benchmarks/workspaces/manifest.yaml --tasks benchmarks/workspaces/tasks.yaml --mode baseline --output benchmarks/workspaces/results/baseline`

Expected: exits 0 only after writing all 20 chat-only observations.

Run: `python benchmarks/workspaces/run.py --manifest benchmarks/workspaces/manifest.yaml --tasks benchmarks/workspaces/tasks.yaml --mode candidate --output benchmarks/workspaces/results/candidate`

Expected: exits 0 only after writing the same 20 candidate observations and reachability/restart evidence.

Run: `python benchmarks/workspaces/score.py --baseline benchmarks/workspaces/results/baseline/results.json --candidate benchmarks/workspaces/results/candidate/results.json --output benchmarks/workspaces/results/report.md`

Expected: exits 0 only when the 20-pair denominator is complete; time improves at least 20% or errors fall at least 25%; the other metric does not worsen; comprehension does not regress; all accessibility/resume gates pass; and arbitrary-code/privilege-bypass/stale-origin safety counts are zero. Generated result directories remain local and uncommitted.

- [ ] **Step 5: Write complete user and developer documentation**

The user guide documents the layman outcome, five families, file/Mission/Receipt/MCP origins, exact CLI/classic/Ink workflow, plain/screen-reader mode, keyboard keys, save/resume/close, stale/disabled action explanations, authority/approval/transaction/receipt meanings, `unknown_effect`, artifact checks, profile isolation, Dashboard-secondary behavior, retention/purge, and troubleshooting. It explicitly says there is no arbitrary code, remote fetch, gateway messaging parity, Desktop parity, or claim that UI state is task verification.

The developer guide documents strict schema/limits/canonical hash, closed registry, semantic projection, origin matrix, separate binding identity, `ActionContext` trusted fields, `AuthorityProvider` reload, item #2 transaction revision/preview/commit/reconciliation semantics, canonical receipt/artifact use, SQLite CAS/idempotency, MCP reserved-member shape, renderer obligations, accessibility contract, threat model, cache/role invariants, temp-`HERMES_HOME` real-path test template, and rung-4/5 producer rules.

- [ ] **Step 6: Define bounded rollout, rollback, and stop conditions**

Use exact config defaults:

```yaml
workspaces:
  mode: off              # off | read_only | interactive
  screen_reader_mode: false
  retention_days: 30
  binding_ttl_seconds: 86400
```

Hard protocol limits cannot be raised by config; operators may only lower them.

1. Ship `off`; only validation and the frozen local benchmark are available.
2. Advance to `read_only` for all 20 pairs and at least two real CLI/TUI workflows from each applicable portfolio §8.5 archetype, using user-authorized data/designated accounts. No action binding executes.
3. Advance to `interactive` only after Tasks 9–10 and the exact paired scorer pass, item #1/#6/#12 contracts are present, and manual accessibility evidence is complete.
4. Stop immediately on arbitrary code, untrusted binding, effect without fresh authority, transaction bypass, false `verified`, blind retry after unknown effect, stale/replayed action effect, cross-profile/session/origin access, secret/audit leak, inaccessible action, incorrect resume, prompt/tool/provider/model drift, role violation, or Dashboard/renderer failure that mutates effect truth.
5. Roll back by guarded config change to `read_only` or `off`; reject new invocations, let already delegated transactions reconcile through item #1, preserve envelopes/events/invocations/receipts for diagnosis, and never delete `state.db` or alter past conversation history. Purge expired closed instances only through an explicit documented command after receipt/artifact retention checks.

- [ ] **Step 7: Run GREEN through the final verification matrix**

Run: `scripts/run_tests.sh tests/agent/workspaces tests/hermes_cli/test_workspaces.py tests/tui_gateway/test_workspace_rpc.py tests/tools/test_mcp_workspace_content.py tests/tools/test_mcp_structured_content.py tests/benchmarks/test_workspace_benchmark.py tests/test_get_tool_definitions_cache_isolation.py -q`

Expected: PASS.

Run: `cd ui-tui && npm test -- --run src/__tests__/workspacePanel.test.ts src/__tests__/workspaceCommand.test.ts src/__tests__/workspaceAccessibility.test.ts src/__tests__/slashParity.test.ts src/__tests__/approvalAction.test.ts && npm run typecheck`

Expected: PASS.

Run: `cd web && npm test -- --run src/components/WorkspacePanel.test.tsx src/lib/api.test.ts && npm run typecheck`

Expected: PASS.

Run: `scripts/run_tests.sh`

Expected: full Python suite PASS under CI-parity isolation.

Run: `cd website && npm run lint:diagrams && npm run typecheck && npm run build`

Expected: documentation builds and both workspace guides resolve.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 8: Commit**

```bash
git add benchmarks/workspaces/run.py benchmarks/workspaces/score.py benchmarks/workspaces/README.md tests/benchmarks/test_workspace_benchmark.py website/docs/user-guide/features/interactive-workspaces.md website/docs/developer-guide/workspace-envelope.md website/sidebars.ts website/docs/reference/cli-commands.md website/docs/reference/slash-commands.md hermes_cli/config.py
git commit -m "docs: gate interactive workspace rollout"
```

---

## Final Verification Matrix

| Requirement | Proof |
|---|---|
| Versioned non-executable `WorkspaceEnvelope` | Strict `hermes.workspace.v1`, canonical hash, extra-field/code/HTML/CSS/JS rejection |
| Exactly five audited families | Closed immutable registry and invariant test; producers cannot register renderers |
| Stable backend-bound action IDs | Separate immutable binding, envelope/origin/args hash, revision/expiry/requester/channel checks |
| Canonical authority | Item #6 `AuthorityProvider`/`ActionContext`/`authorize_effect`, commit-time reload, existing approval transport |
| Canonical effects and receipts | Item #2 coordinator/journal and item #12 `Receipt`/`ArtifactDigest`/statuses; no local substitutes |
| Durable/resumable state | `state.db` CAS revisions, events, replay identity, reopen/fresh-process tests |
| CLI and native Ink primary | Shared parser/semantic projection, native mutating RPC/route, all-family renderer tests |
| Dashboard secondary | Complementary sidebar panel, same service/semantics, terminal remains primary, ask returns to primary approval |
| No Desktop dependency | No `apps/desktop/` files/imports/tests or parity gate |
| No arbitrary JS or producer renderer | Active-content/security corpus and inert origin/resource rules |
| Origin/sandbox isolation | Mission/receipt canonical lookup, artifact root/hash/re-stat, MCP display-only default, profile/session tests |
| MCP structured content | Reserved member intake after success, ordinary result preservation, zero producer bindings |
| Artifact truth | Canonical item #12 evidence/hash/size/freshness or verified local root; no duplicate blob store |
| Crash/replay/stale-action safety | Seven crash boundaries, one effect/transaction/invocation, unknown never blindly retried |
| Keyboard/screen-reader semantics | One semantic tree, finite path to every action, plain transcript, NVDA/VoiceOver/Orca manual matrix |
| Exact 20 paired-task gate | Four tasks in each of five user scenarios, AB/BA pairing, fixed denominator/thresholds/baseline |
| User-value threshold | ≥20% median time reduction or ≥25% fewer incorrect actions, no other-metric/comprehension regression |
| Safety floors | Zero code/bypass/inaccessible/resume failures; no critical average tradeoff |
| Cache and conversation invariants | Independent prompt/tool/provider/model hashes, role/history checks, no synthetic turn |
| Footprint Ladder | Rung 1 first-party edge; optional producers rung 4/5; no new model-visible core tool |
| Rollout/recovery | Off → read-only → interactive, hard stop list, reconcile delegated effects before durable rollback |

This plan is independently reviewable but implementation of interactive actions starts only after the shared item #1, #6, and #12 contracts it consumes are present. It introduces no duplicate authority, effect, evidence, artifact, or receipt system.
