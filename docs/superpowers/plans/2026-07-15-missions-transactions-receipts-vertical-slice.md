# Missions, Transactions, and Receipts Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a terminal-first vertical slice in which a user can start one long-running Hermes mission, safely execute workspace, Hermes-state, and delayed-message effects across restarts, and receive an evidence-scored receipt that never calls an unknown or unverified outcome successful.

**Architecture:** Add a small mission aggregate beside existing workflow executions in profile-local `workflows.db`; extend the existing `OperationJournal` with transaction records in profile-local `state.db`; route only mission-scoped, declared effects through adapter-specific `prepare -> preview -> commit -> verify/reconcile -> compensate` contracts; and generate immutable receipts from persisted evidence. Reuse the existing workflow engine, Kanban workers, checkpoint manager, approval engine, delivery router, verification evidence store, CLI command registry, and TUI slash bridge. Do not create a second task graph, retry engine, model tool, chat surface, or mutable system-prompt state.

**Tech Stack:** Python 3.13, SQLite/WAL, Pydantic, pytest, existing Hermes workflow/Kanban/cron/config/gateway modules, Rich/classic CLI, Ink TUI through the existing slash-command bridge, YAML benchmark manifests, Git worktrees and local Git subprocesses.

## Global Constraints

- Work in a fresh git worktree created from the branch containing this plan; preserve unrelated user changes.
- TDD is mandatory: each behavior begins with the smallest failing test and a recorded RED result.
- Do not add a model-visible core tool. Mission-only capabilities are CLI commands + a skill, existing tool execution metadata, or a workflow node.
- Preserve the byte-stable system prompt, effective tool schema, provider, and model for the life of a conversation; preserve strict role alternation.
- A mission links existing `WorkflowExecution` rows. It must not duplicate workflow nodes, attempts, event cursors, retry state, or Kanban task state.
- `OperationJournal` remains the source of truth for whether an effect is pending, running, dispatched, confirmed, failed, or unknown. `effect_transactions` adds preview, authority, dependency, verification, and compensation facts; it does not replace or weaken the journal.
- Non-mission tool calls retain current behavior and incur no transaction persistence. In a mission, read-only tools pass through; supported mutating effects use an adapter; unsupported mutating/destructive effects fail closed before the handler runs.
- Recheck mission authority immediately before commit. An expired or changed grant blocks the effect even if prepare/preview succeeded.
- Never retry an effect whose disposition is unknown. Reconcile it or surface `unknown_effect` for review.
- The only receipt scorer may emit `verified`. Workflow success or a model assertion alone yields at most `completed_unverified`.
- Receipts are immutable. Rechecks append linked observations; they never edit a prior receipt.
- V1 supports one active profile/HERMES_HOME per mission. Reject `agent_task.profile` values that cross the mission's profile boundary.
- First adapters are strictly bounded: file `write_file`/`patch` inside an allowed workspace or disposable worktree; versioned workflow/cron/config mutations; and a delayed outbound-message outbox. No arbitrary shell wrapping, production DB writes, browser form writes, remote Git push, account deletion, or purchases.
- Local Git support is limited to staging and committing inside a disposable non-main worktree. It never pushes.
- Network message delivery is irreversible unless the selected platform adapter proves edit/delete compensation. Unknown acknowledgement is not success.
- Stable settings live in `config.yaml`; durable mission authority and audit decisions live in SQLite. `HERMES_MISSION_ID` is an internal runtime correlation variable, not user configuration.
- Real-path integration tests use a temporary `HERMES_HOME`, real SQLite connections, real imports, real temp files/Git repositories, and restart by reopening stores or spawning a subprocess. Mock only the final external network boundary and process-kill boundary.
- Each task ends with focused tests, relevant regressions, `git diff --check`, and one conventional commit.

---

## Scope and Data Ownership

```text
workflows.db (mission intent/orchestration)       state.db (effects/evidence)
┌──────────────────────────────┐                  ┌──────────────────────────────┐
│ missions                     │  mission_id      │ agent_operations (existing)  │
│ mission_execution_links ─────┼─────────────────>│ effect_transactions           │
│ mission_review_items         │                  │ receipts                     │
│ mission_events               │                  │ receipt_observations         │
└──────────────┬───────────────┘                  │ mission_outbox                │
               │ execution_id                     └──────────────┬───────────────┘
               v                                                  │ delivery_id
      workflow_executions (existing)                              v
      workflow_node_runs/events (existing)              DeliveryRouter + OperationJournal
```

The databases are intentionally not joined in one SQLite transaction. The mission is created atomically with its workflow execution/link in `workflows.db`. Each effect is then made crash-safe by the existing operation journal plus an idempotent transaction record in `state.db`; reconciliation repairs cross-store projections after restart.

### Canonical state machines

```python
MissionStatus = Literal["queued", "running", "waiting", "review", "terminal"]
MissionVerdict = Literal[
    "verified", "completed_unverified", "failed", "blocked", "unknown_effect"
]
TransactionPhase = Literal[
    "prepared", "previewed", "committing", "committed", "verified",
    "compensating", "compensated", "failed", "blocked", "unknown_effect"
]
EffectKind = Literal[
    "read_only", "reversible", "compensatable", "irreversible"
]
```

Terminal `verified` is a receipt status and mission verdict produced by the scorer. `TransactionPhase == "verified"` only means that adapter-specific postconditions passed; the mission scorer must still prove the whole objective.

## File Map

### New production files

- `hermes_cli/missions_db.py` — mission records, atomic workflow link creation, events/review queue, authority snapshots, reconciliation projections.
- `hermes_cli/missions.py` — top-level and slash CLI commands; no UI-specific logic.
- `agent/effect_transactions.py` — effect types, adapter protocol/registry, coordinator, authority recheck, dependency/compensation rules.
- `agent/effect_adapters.py` — workspace and Hermes-owned-state adapters; pure adapter implementations over existing services.
- `agent/receipts.py` — immutable receipt records, scorer protocol, built-in workflow end-state scorer, recheck observations.
- `gateway/mission_outbox.py` — durable delayed outbox dispatcher over `DeliveryRouter` and `OperationJournal`.
- `skills/mission-control/SKILL.md` — terminal-first instructions for using mission, state, workspace, review, compensation, and receipt commands.
- `benchmarks/missions/manifest.yaml` — preregistered corpus strata, fault points, metrics, and gates.
- `benchmarks/missions/fixtures/three-effect-mission.yaml` — real vertical-slice workflow fixture.

### Existing files modified

- `hermes_state.py` — declarative tables and SessionDB accessors for transactions, receipts, observations, and outbox.
- `hermes_cli/workflows_db.py` — one transaction-aware execution-start helper used by mission creation.
- `hermes_cli/workflows_capabilities.py` — promote `send_message` only after dispatcher support exists.
- `hermes_cli/workflows_dispatcher.py` — persist/resume delayed message nodes and project execution terminal state into missions.
- `hermes_cli/workflows_spec.py` — typed delayed-message fields and validation.
- `tools/registry.py` — optional adapter id and effect semantics metadata; tool JSON schemas remain unchanged.
- `hermes_cli/middleware.py` — terminal middleware seam invokes the coordinator after plugins have finalized arguments.
- `tools/checkpoint_manager.py` — forced per-transaction checkpoint reference and exact restore API while preserving `ensure_checkpoint()`.
- `tools/file_tools.py` — register existing `write_file` and `patch` with the workspace adapter metadata.
- `agent/operation_journal.py` — query helpers needed by reconciliation; no competing state machine.
- `cron/jobs.py` — return-safe snapshot/apply helpers used by the cron adapter while retaining public CLI behavior.
- `hermes_cli/config.py` — non-printing validated set/restore helpers used by the config adapter while retaining `set_config_value()`.
- `gateway/run.py` — construct and drain the mission outbox service with the existing gateway lifecycle.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `hermes_cli/cli_commands_mixin.py`, `cli.py` — register and dispatch `hermes mission` and `/mission`.

### New focused tests

- `tests/hermes_cli/test_missions_db.py`
- `tests/hermes_cli/test_mission_cli.py`
- `tests/hermes_cli/test_mission_e2e.py`
- `tests/agent/test_effect_transactions.py`
- `tests/agent/test_effect_adapters.py`
- `tests/agent/test_receipts.py`
- `tests/gateway/test_mission_outbox.py`
- `tests/benchmarks/test_mission_manifest.py`

### Existing regression tests extended

- `tests/agent/test_operation_journal.py`
- `tests/tools/test_registry.py`
- `tests/tools/test_checkpoint_manager.py`
- `tests/tools/test_file_tools.py`
- `tests/hermes_cli/test_workflows_db.py`
- `tests/hermes_cli/test_workflows_dispatcher.py`
- `tests/hermes_cli/test_workflows_capabilities.py`
- `tests/hermes_cli/test_workflow_cli.py`
- `tests/hermes_cli/test_workflows_e2e.py`
- `tests/gateway/test_delivery_operation_journal.py`
- command-registry tests that currently assert the `workflow` command catalog.

---

### Task 0: Freeze the Vertical-Slice Contract and Benchmark Before Production Code

**Files:**
- Create: `benchmarks/missions/manifest.yaml`
- Create: `benchmarks/missions/fixtures/three-effect-mission.yaml`
- Create: `tests/benchmarks/test_mission_manifest.py`

**Interfaces:**
- Consumes: approved portfolio gates and existing workflow YAML schema.
- Produces: a machine-readable, versioned denominator for success, safety, recovery, latency, and overhead.

- [ ] **Step 1: Write the failing manifest contract test**

```python
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_mission_benchmark_is_preregistered_and_bounded():
    manifest = yaml.safe_load(
        (ROOT / "benchmarks/missions/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["schema"] == "hermes.mission-benchmark.v1"
    assert manifest["corpus"]["minimum_missions"] == 30
    assert len(manifest["corpus"]["archetypes"]) == 6
    assert manifest["vertical_slice"]["effect_types"] == [
        "workspace", "hermes_state", "delayed_message"
    ]
    assert set(manifest["faults"]) >= {
        "after_prepare", "after_preview", "after_commit_started",
        "after_handler_return", "after_delivery_dispatch"
    }
    assert manifest["gates"]["duplicate_effects"] == 0
    assert manifest["gates"]["false_verified"] == 0
    assert manifest["gates"]["mission_correct_state_rate"] >= 0.90
    assert manifest["gates"]["transaction_median_overhead_ratio"] <= 0.15
```

- [ ] **Step 2: Run RED**

Run:

```bash
uv run --extra dev python -m pytest tests/benchmarks/test_mission_manifest.py -q
```

Expected: failure because the manifest does not exist.

- [ ] **Step 3: Add the preregistered manifest**

The YAML must name six archetypes with at least two real tasks each: software maintenance, sourced research, data/artifact pipeline, repeated web operation, personal-knowledge lifecycle, and proactive/recovery. Record the initial event sources (`cron`, `filesystem_git`, `webhook`, `gateway_channel`), the three-effect vertical slice, 30-mission/100-fault/50-false-success sample gates, current-Hermes baseline, excluded cases, costs, p50/p95 latency, verified success, user attention, recovery burden, and Wilson confidence intervals. Do not invent an aggregate score.

The fixture workflow must use `agent_task -> wait -> send_message`, declare an artifact path and verification command, and target a designated test channel with `not_before_seconds: 30`; it must not use a real credential or address.

- [ ] **Step 4: Run GREEN and validate the workflow fixture**

```bash
uv run --extra dev python -m pytest \
  tests/benchmarks/test_mission_manifest.py \
  tests/hermes_cli/test_workflows_docs_examples.py -q
```

Expected: all pass. The benchmark test parses the fixture as a declared `WorkflowSpec` and separately asserts that `implemented_primitive_errors()` reports `send_message` as unsupported at this stage. Task 7 updates that assertion to require runtime support.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/missions tests/benchmarks/test_mission_manifest.py
git commit -m "test: preregister mission transaction benchmark"
```

---

### Task 1: Persist the Mission Aggregate Beside Existing Workflow Executions

**Files:**
- Create: `hermes_cli/missions_db.py`
- Create: `tests/hermes_cli/test_missions_db.py`
- Modify: `hermes_cli/workflows_db.py`
- Modify: `tests/hermes_cli/test_workflows_db.py`

**Interfaces:**
- Consumes: `workflows_db.connect_closing()`, `write_txn()`, `get_definition()`, and `start_execution()`.
- Produces: immutable mission intent, execution links, append-only events/review items, and atomic `create_mission_and_execution()`.

- [ ] **Step 1: Write RED tests for ownership and atomicity**

Cover these contracts with a real temporary `HERMES_HOME`:

```python
mission, execution = create_mission_and_execution(
    conn,
    workflow_id="three_effects",
    objective="Patch, verify, record state, then notify me",
    constraints=["never push", "only modify the worktree"],
    authority={
        "allowed_effects": ["workspace", "hermes_state", "delayed_message"],
        "workspace_roots": [str(worktree)],
        "message_targets": ["test:mission-user"],
        "expires_at": 1_800_000_000,
        "irreversible": "ask",
    },
    evidence={
        "checks": ["workflow_succeeded", "all_effects_settled", "fresh_verification"],
        "artifacts": ["report.json"],
    },
    input_data={"issue": "real fixture"},
    profile="default",
)
assert get_execution(conn, execution.execution_id).workflow_id == "three_effects"
assert list_execution_links(conn, mission.mission_id) == [execution.execution_id]
assert mission.objective == "Patch, verify, record state, then notify me"
```

Also prove: failure in `start_execution()` leaves no mission; constraints/authority/evidence are returned as deep copies; an `agent_task` naming another profile is rejected before any row is written; terminal verdict cannot transition back to running; duplicate review/event idempotency keys create one row.

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest tests/hermes_cli/test_missions_db.py -q
```

Expected: import failure for `hermes_cli.missions_db`.

- [ ] **Step 3: Add mission tables and frozen records**

Use `workflows.db`, not `state.db`, for these tables:

```sql
CREATE TABLE IF NOT EXISTS missions (
    mission_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    objective TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    authority_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    authority_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    verdict TEXT,
    receipt_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    terminal_at INTEGER
);
CREATE TABLE IF NOT EXISTS mission_execution_links (
    mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
    execution_id TEXT NOT NULL REFERENCES workflow_executions(execution_id),
    relation TEXT NOT NULL DEFAULT 'primary',
    linked_at INTEGER NOT NULL,
    PRIMARY KEY (mission_id, execution_id)
);
CREATE TABLE IF NOT EXISTS mission_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (mission_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS mission_review_items (
    review_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
    transaction_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER
);
```

Expose frozen `MissionRecord`/`MissionReviewItem` dataclasses and explicit transition methods. Do not accept arbitrary status strings at the SQL boundary.

- [ ] **Step 4: Make execution creation transaction-aware**

Refactor `workflows_db.start_execution()` so it remains backward compatible and participates in an outer `write_txn(conn)`. `create_mission_and_execution()` must execute mission insert, execution insert, link insert, and both initial events in one outer transaction. Do not open another connection.

- [ ] **Step 5: Run GREEN and workflow DB regressions**

```bash
uv run --extra dev python -m pytest \
  tests/hermes_cli/test_missions_db.py \
  tests/hermes_cli/test_workflows_db.py \
  tests/hermes_cli/test_workflows_db_upgrade.py \
  tests/hermes_cli/test_workflows_db_versions.py -q
git diff --check
```

Expected: all pass; existing workflow-only callers are unchanged.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/missions_db.py hermes_cli/workflows_db.py \
  tests/hermes_cli/test_missions_db.py tests/hermes_cli/test_workflows_db.py
git commit -m "feat: add durable mission aggregate"
```

---

### Task 2: Add Transaction, Receipt, Observation, and Outbox Storage to SessionDB

**Files:**
- Modify: `hermes_state.py`
- Create: `tests/agent/test_receipts.py`
- Create: `tests/agent/test_effect_transactions.py`
- Create: `tests/gateway/test_mission_outbox.py`

**Interfaces:**
- Consumes: SessionDB declarative schema reconciliation and `_execute_read`/`_execute_write`.
- Produces: idempotent transaction records, immutable receipts, append-only observations, and durable outbox records.

- [ ] **Step 1: Write RED storage tests**

Test a brand-new database and a copied v21 schema. Assert one transaction per `operation_id`, unique `(mission_id, sequence_no)`, compare-and-set phase transitions, immutable receipt rows, appended recheck observations, and one outbox row per stable `delivery_id`. Reopening SessionDB must preserve every record.

```python
tx = db.create_effect_transaction(
    transaction_id="tx-1", operation_id="op-1", mission_id="m-1",
    execution_id="ex-1", step_id="write", adapter_id="workspace.v1",
    sequence_no=1, semantics={"kind": "reversible", "idempotent": True},
    depends_on=[], prepared={"path": "README.md"}, preview={"diff": "+ok"},
)
assert db.transition_effect_transaction("tx-1", "previewed", "committing")
assert not db.transition_effect_transaction("tx-1", "previewed", "committing")
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest \
  tests/agent/test_effect_transactions.py \
  tests/agent/test_receipts.py \
  tests/gateway/test_mission_outbox.py -q
```

Expected: missing SessionDB methods/tables.

- [ ] **Step 3: Add declarative schema without a needless data migration**

Add tables to `SCHEMA_SQL`; increment `SCHEMA_VERSION` only if the repository's schema-version convention requires it for new tables. Use canonical JSON and integer timestamps:

```sql
CREATE TABLE IF NOT EXISTS effect_transactions (
    transaction_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES agent_operations(operation_id),
    mission_id TEXT NOT NULL,
    execution_id TEXT,
    step_id TEXT,
    adapter_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    semantics_json TEXT NOT NULL,
    phase TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    prepared_json TEXT,
    preview_json TEXT,
    authority_json TEXT,
    result_json TEXT,
    verification_json TEXT,
    compensation_json TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (mission_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    status TEXT NOT NULL,
    objective TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    execution_ids_json TEXT NOT NULL,
    transaction_ids_json TEXT NOT NULL,
    before_after_json TEXT NOT NULL,
    claims_json TEXT NOT NULL,
    verifier_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    uncertainty_json TEXT NOT NULL,
    freshness_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    signature_json TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS receipt_observations (
    observation_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL REFERENCES receipts(receipt_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mission_outbox (
    outbox_id TEXT PRIMARY KEY,
    mission_id TEXT,
    execution_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    transaction_id TEXT UNIQUE,
    delivery_id TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    target TEXT NOT NULL,
    content_json TEXT NOT NULL,
    not_before INTEGER NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    approval_json TEXT,
    result_json TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

- [ ] **Step 4: Add typed SessionDB accessors**

All writes validate state transitions before SQL, use CAS predicates, and return frozen records. `insert_receipt()` rejects an existing `receipt_id` even if content differs; `append_receipt_observation()` never updates `receipts`; `claim_due_outbox()` must atomically claim in `not_before, created_at` order and recover expired claims.

- [ ] **Step 5: Run GREEN plus schema regressions**

```bash
uv run --extra dev python -m pytest \
  tests/agent/test_effect_transactions.py \
  tests/agent/test_receipts.py \
  tests/gateway/test_mission_outbox.py \
  tests/test_hermes_state.py \
  tests/test_hermes_state_wal_fallback.py -q
git diff --check
```

Expected: all pass on new and upgraded databases.

- [ ] **Step 6: Commit**

```bash
git add hermes_state.py tests/agent/test_effect_transactions.py \
  tests/agent/test_receipts.py tests/gateway/test_mission_outbox.py
git commit -m "feat: persist mission effects and receipts"
```

---

### Task 3: Define Effect Contracts and Extend Tool Metadata Without Schema Footprint

**Files:**
- Create: `agent/effect_transactions.py`
- Modify: `tools/registry.py`
- Modify: `hermes_cli/middleware.py`
- Modify: `agent/operation_journal.py`
- Modify: `tests/tools/test_registry.py`
- Modify: `tests/agent/test_operation_journal.py`
- Modify: `tests/agent/test_effect_transactions.py`

**Interfaces:**
- Consumes: final post-plugin tool arguments, `operation_key`, SessionDB, mission authority lookup, approval engine.
- Produces: adapter protocol/registry and a coordinator invoked only for mission-scoped effects.

- [ ] **Step 1: Write RED contract and metadata tests**

Prove that adding effect metadata does not alter `registry.get_tool_schemas()`; metadata returns defensive copies; duplicate adapter ids fail; no mission id calls the original handler exactly once without SessionDB transaction writes; mission read-only calls pass; mission unsupported mutation blocks before handler; authority expiry between preview and commit blocks; handler timeout/interrupt becomes `unknown_effect`; same operation key resumes/reconciles instead of committing twice.

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest \
  tests/tools/test_registry.py \
  tests/agent/test_effect_transactions.py \
  tests/agent/test_operation_journal.py -q
```

Expected: new metadata/coordinator tests fail.

- [ ] **Step 3: Add frozen contracts and adapter protocol**

```python
@dataclass(frozen=True)
class EffectSemantics:
    kind: Literal["read_only", "reversible", "compensatable", "irreversible"]
    idempotent: bool
    reconcilable: bool


@dataclass(frozen=True)
class PreparedEffect:
    adapter_id: str
    normalized_args: Mapping[str, Any]
    before: Mapping[str, Any]
    preview: Mapping[str, Any]
    semantics: EffectSemantics
    compensation: Mapping[str, Any] | None


class EffectAdapter(Protocol):
    adapter_id: str
    def prepare(self, request: EffectRequest) -> PreparedEffect: ...
    def commit(self, prepared: PreparedEffect, invoke: Callable[[], Any]) -> Any: ...
    def verify(self, prepared: PreparedEffect, result: Any) -> VerificationResult: ...
    def reconcile(self, record: EffectTransactionRecord) -> ReconciliationResult: ...
    def compensate(self, record: EffectTransactionRecord) -> CompensationResult: ...
```

The coordinator accepts injected `mission_loader`, `session_db`, `operation_journal`, `approval_request`, and clock factories so unit tests are deterministic.

- [ ] **Step 4: Extend `ToolEntry` metadata only**

Add optional `effect_adapter: str | None` plus semantic overrides to `ToolEntry`/`register()`. Extend `get_operation_metadata()` to return them. Keep `schema`, discovery, core tool lists, and cached schema snapshots byte-identical when only metadata changes.

- [ ] **Step 5: Put the coordinator at the terminal middleware seam**

In `run_tool_execution_middleware()`, keep plugin ordering intact. Invoke the coordinator around the terminal `handler(**effective_args)` after all `next_call` argument changes. A plugin that short-circuits does not create a transaction. Move `operation_key_factory()` evaluation to the point where the terminal handler will execute so mission transactions have a stable key even when no observer plugin is configured.

The coordinator algorithm is explicit:

```text
no mission -> invoke unchanged
read-only -> invoke unchanged
unsupported mission mutation -> block, do not invoke
existing confirmed tx -> return stored result
existing running/dispatched tx -> reconcile, never blind retry
new tx -> journal.create + adapter.prepare + persist preview
reload authority -> deny/approve irreversible as required
journal running + tx committing -> adapter.commit(invoke)
persist raw result -> adapter.verify/reconcile
confirmed -> journal confirmed + tx verified/committed
ambiguous exception/interrupt -> journal unknown + tx unknown_effect + review item
```

- [ ] **Step 6: Add journal query helpers**

Add only `get_by_operation_id()`/terminal-result helpers required by the coordinator. Keep existing restart semantics: `running`/`dispatched` recover to unknown unless an adapter can reconcile from a durable acknowledgement.

- [ ] **Step 7: Run GREEN and middleware regressions**

```bash
uv run --extra dev python -m pytest \
  tests/agent/test_effect_transactions.py \
  tests/agent/test_operation_journal.py \
  tests/tools/test_registry.py \
  tests/test_get_tool_definitions_cache_isolation.py -q
git diff --check
```

Expected: all pass and schema snapshot tests show no model-tool change.

- [ ] **Step 8: Commit**

```bash
git add agent/effect_transactions.py agent/operation_journal.py \
  tools/registry.py hermes_cli/middleware.py \
  tests/agent/test_effect_transactions.py tests/agent/test_operation_journal.py \
  tests/tools/test_registry.py
git commit -m "feat: coordinate mission effect transactions"
```

---

### Task 4: Implement the Reversible Workspace Adapter and Per-Transaction Checkpoints

**Files:**
- Create: `agent/effect_adapters.py`
- Create: `tests/agent/test_effect_adapters.py`
- Modify: `tools/checkpoint_manager.py`
- Modify: `tools/file_tools.py`
- Modify: `tests/tools/test_checkpoint_manager.py`
- Modify: `tests/tools/test_file_tools.py`
- Modify: `tests/agent/test_effect_transactions.py`

**Interfaces:**
- Consumes: existing `write_file`/`patch` handlers, file-tool working-directory resolution, `CheckpointManager`, Git CLI.
- Produces: diff preview, immutable checkpoint reference, postcondition verification, dependency-safe restore, and bounded local worktree commit.

- [ ] **Step 1: Write RED workspace safety tests**

Use a real temp Git repository and disposable worktree. Test:

- a path outside `authority.workspace_roots` is blocked before file mutation;
- symlink traversal outside the allowed root is blocked;
- a Kanban `repository` workspace on `main` or the primary checkout is rejected;
- `write_file` and `patch` preview the exact path/diff and create a distinct checkpoint per transaction;
- kill/failure after the handler returns is reconciled by comparing before/after hashes rather than invoking the handler again;
- compensation restores bytes, mode, deletion state, and Git diff;
- compensation detects post-commit workspace drift and blocks for review instead of overwriting a human or non-mission edit;
- a dependent later transaction prevents non-cascade compensation;
- `--cascade` compensates reverse dependency order and stops at an irreversible boundary;
- local stage/commit succeeds in the disposable worktree and a request containing a remote/push operation is rejected.

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest \
  tests/agent/test_effect_adapters.py \
  tests/tools/test_checkpoint_manager.py \
  tests/tools/test_file_tools.py -q
```

Expected: failures for the forced checkpoint and workspace adapter.

- [ ] **Step 3: Add a forced checkpoint API without changing existing callers**

Add an immutable record:

```python
@dataclass(frozen=True)
class CheckpointRef:
    checkpoint_id: str
    working_dir: str
    commit_hash: str
    created_at: int

def create_checkpoint(self, working_dir: str, *, reason: str, force: bool = False) -> CheckpointRef:
    ...

def restore_checkpoint(self, ref: CheckpointRef) -> None:
    ...
```

Keep `ensure_checkpoint()` as the one-per-turn compatibility wrapper. Transaction prepare calls `create_checkpoint(..., force=True)`. Serialize checkpoint creation/restore per resolved workspace path using the existing lock style; reject a ref whose resolved root differs from the transaction root.

- [ ] **Step 4: Implement `WorkspaceEffectAdapter`**

Normalize and resolve paths using the same file-tool helpers the handler uses. `prepare()` records SHA-256, existence, mode, Git status, a forced checkpoint, and a unified diff preview. `verify()` proves actual after hashes and expected changed paths. `reconcile()` compares durable before/after state. `compensate()` restores the exact checkpoint only after the coordinator confirms all dependents are already compensated and the current affected-path hashes still match the transaction's verified after-state; otherwise it creates a review item rather than clobbering drift.

Register existing tools without changing their schemas:

```python
@registry.register(
    "write_file",
    ...,
    destructive=True,
    effect_adapter="workspace.v1",
)
def write_file(...): ...

@registry.register(
    "patch",
    ...,
    destructive=True,
    effect_adapter="workspace.v1",
)
def patch(...): ...
```

- [ ] **Step 5: Add a non-model local commit service**

Expose a `WorkspaceCommitEffectAdapter` operation consumed later by `hermes mission workspace commit` through the same coordinator. Preview the selected diff and record parent HEAD/index state. Use subprocess argument arrays with `git -C <worktree> add -- <paths>` and `git -C <worktree> commit -m <message>`. Verify `git rev-parse --show-toplevel`, `git rev-parse --is-inside-work-tree`, and that the current branch is neither detached nor `main`/`master`. Compensation may run `git reset --mixed <parent>` only when HEAD is still exactly the commit created by this transaction and all later dependents are compensated; otherwise block for review. Do not accept arbitrary Git arguments and do not implement push.

- [ ] **Step 6: Run GREEN plus real-file regressions**

```bash
uv run --extra dev python -m pytest \
  tests/agent/test_effect_adapters.py \
  tests/agent/test_effect_transactions.py \
  tests/tools/test_checkpoint_manager.py \
  tests/tools/test_file_tools.py \
  tests/integration/test_checkpoint_resumption.py -q
git diff --check
```

Expected: all pass; existing one-per-turn checkpoint behavior remains intact outside missions.

- [ ] **Step 7: Commit**

```bash
git add agent/effect_adapters.py tools/checkpoint_manager.py tools/file_tools.py \
  tests/agent/test_effect_adapters.py tests/agent/test_effect_transactions.py \
  tests/tools/test_checkpoint_manager.py tests/tools/test_file_tools.py
git commit -m "feat: add reversible workspace transactions"
```

---

### Task 5: Add Versioned Hermes-State Adapters Through CLI Services

**Files:**
- Modify: `agent/effect_adapters.py`
- Modify: `cron/jobs.py`
- Modify: `hermes_cli/config.py`
- Modify: `hermes_cli/workflows_db.py`
- Modify: `tests/agent/test_effect_adapters.py`
- Modify: `tests/hermes_cli/test_workflows_db.py`
- Modify: `tests/hermes_cli/test_cron.py`
- Modify: `tests/hermes_cli/test_config.py`
- Modify: `tests/hermes_cli/test_managed_scope_config.py`
- Modify: `tests/hermes_cli/test_managed_scope_cli_config.py`

**Interfaces:**
- Consumes: workflow deploy/enable state, cron jobs JSON store and lock, raw config + atomic YAML write.
- Produces: typed preview/apply/verify/restore services usable by `hermes mission state`; no new env var or model tool.

- [ ] **Step 1: Write RED adapter tests over real temporary stores**

Test three adapter ids:

```text
hermes.workflow-state.v1  deploy version / enable / disable
hermes.cron-state.v1      create / update / disable
hermes.config-state.v1    set one validated dotted key
```

For each, assert preview includes old/new canonical values, commit uses the existing lock/atomic writer, verify rereads durable state, and compensation restores the prior immutable version/value. Also assert:

- workflow rollback selects or disables a version; it never edits a published definition;
- cron compensation removes a newly created job or restores the exact prior job including schedule/delivery fields;
- config compensation distinguishes “key absent” from a key whose value was `null`;
- unreadable or managed config fails closed;
- secrets/credential keys are rejected from the config-state adapter;
- concurrent revision mismatch blocks instead of overwriting another process.

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest tests/agent/test_effect_adapters.py -q -k hermes_state
```

Expected: missing Hermes-state adapters/services.

- [ ] **Step 3: Extract non-printing snapshot/apply services**

Keep public command behavior, but make adapters call typed services:

```python
@dataclass(frozen=True)
class StateMutation:
    resource: str
    action: str
    expected_revision: str
    before: Mapping[str, Any]
    after: Mapping[str, Any]

def prepare_config_mutation(key: str, value: str) -> StateMutation: ...
def apply_config_mutation(change: StateMutation) -> Mapping[str, Any]: ...
def restore_config_mutation(change: StateMutation) -> Mapping[str, Any]: ...
```

Implement equivalent workflow and cron services in their owning modules. Existing `set_config_value()` may print around the service; the service itself must not print or call `sys.exit`.

- [ ] **Step 4: Register adapters only in the coordinator registry**

Do not register new model tools. The later CLI constructs an `EffectRequest` and calls the same coordinator with a stable key such as `mission:<id>:config:<key>:<expected_revision>`.

- [ ] **Step 5: Run GREEN and resource regressions**

Run the exact focused suites:

```bash
uv run --extra dev python -m pytest \
  tests/agent/test_effect_adapters.py \
  tests/hermes_cli/test_workflows_db.py \
  tests/hermes_cli/test_cron.py \
  tests/hermes_cli/test_config.py \
  tests/hermes_cli/test_managed_scope_config.py \
  tests/hermes_cli/test_managed_scope_cli_config.py -q
git diff --check
```

Expected: all pass; public `hermes config set`, cron, and workflow behavior remains unchanged.

- [ ] **Step 6: Commit**

```bash
git add agent/effect_adapters.py cron/jobs.py hermes_cli/config.py \
  hermes_cli/workflows_db.py tests/agent/test_effect_adapters.py \
  tests/hermes_cli/test_workflows_db.py tests/hermes_cli/test_cron.py \
  tests/hermes_cli/test_config.py tests/hermes_cli/test_managed_scope_config.py \
  tests/hermes_cli/test_managed_scope_cli_config.py
git commit -m "feat: transact versioned Hermes state"
```

---

### Task 6: Build Deterministic Receipts and the Only `verified` Scorer

**Files:**
- Create: `agent/receipts.py`
- Modify: `hermes_cli/missions_db.py`
- Modify: `agent/verification_evidence.py`
- Modify: `tests/agent/test_receipts.py`
- Modify: `tests/hermes_cli/test_missions_db.py`
- Modify: existing verification-evidence tests found by `rg --files tests | rg 'verification_evidence'`

**Interfaces:**
- Consumes: mission intent, linked workflow executions/events, effect transactions, operation journal, verification evidence, artifact files/hashes, outbox confirmations.
- Produces: canonical immutable receipt and append-only recheck observations.

- [ ] **Step 1: Write RED scorer truth-table tests**

Seed at least 50 false-success combinations across parametrized cases. Required outcomes:

| Evidence state | Required receipt status |
|---|---|
| execution failed | `failed` |
| review/authority blocked | `blocked` |
| any operation/transaction/outbox unknown | `unknown_effect` |
| workflow succeeded but a required check/artifact is absent or stale | `completed_unverified` |
| model says “done” but no independent evidence | `completed_unverified` |
| all declared checks, fresh verification, artifacts, transactions and delivery pass | `verified` |

Assert zero direct call sites can construct a receipt with status `verified` except the scorer's private factory.

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest tests/agent/test_receipts.py -q
```

Expected: import/contract failures.

- [ ] **Step 3: Define the scorer and evidence manifest**

```python
class EndStateScorer(Protocol):
    scorer_id: str
    scorer_version: str
    def score(self, snapshot: MissionEvidenceSnapshot) -> ReceiptDecision: ...


class WorkflowEndStateScorer:
    scorer_id = "hermes.workflow-end-state"
    scorer_version = "1"
```

Support only these V1 evidence checks: `workflow_succeeded`, `all_effects_settled`, `fresh_verification`, `artifacts_exist`, and `outbox_confirmed`. Unknown check names block mission start. `fresh_verification` uses the existing `verification_status(session_id, cwd)` and records its timestamp/source; do not duplicate the evidence DB. Artifact evidence contains resolved path, size, SHA-256, and mtime after verifying the path remains in an allowed root.

- [ ] **Step 4: Canonicalize and hash immutable receipts**

Build canonical JSON with sorted keys and compact separators, excluding optional signature from the content hash. Include objective/constraints, execution/transaction ids, before/after claims, verifier id/version, evidence, artifacts, uncertainty, freshness, and optional signature. Persist the receipt before setting the mission terminal verdict/receipt projection. If the second write crashes, restart reconciliation links the already-hashed receipt rather than creating another.

- [ ] **Step 5: Implement recheck as an observation**

`recheck_receipt(receipt_id)` reruns current observations and appends `receipt_observations(recheck_of=receipt_id conceptually through receipt_id FK)`. It must not modify the receipt or retroactively change the mission verdict. CLI rendering later shows original status plus latest observation.

- [ ] **Step 6: Run GREEN and evidence regressions**

```bash
uv run --extra dev python -m pytest \
  tests/agent/test_receipts.py \
  tests/agent/test_verification_evidence.py \
  tests/hermes_cli/test_missions_db.py \
  tests/test_evidence_store.py -q
git diff --check
```

Also include `tests/agent/test_verification_evidence.py` in the command. Expected: all pass and the 50-case false-success corpus emits no false `verified`.

- [ ] **Step 7: Commit**

```bash
git add agent/receipts.py agent/verification_evidence.py hermes_cli/missions_db.py \
  tests/agent/test_receipts.py tests/agent/test_verification_evidence.py \
  tests/hermes_cli/test_missions_db.py
git commit -m "feat: issue evidence-scored mission receipts"
```

---

### Task 7: Implement Delayed Outbox Delivery and the Workflow `send_message` Node

**Files:**
- Create: `gateway/mission_outbox.py`
- Modify: `hermes_cli/workflows_spec.py`
- Modify: `hermes_cli/workflows_capabilities.py`
- Modify: `hermes_cli/workflows_dispatcher.py`
- Modify: `gateway/run.py`
- Modify: `tests/gateway/test_mission_outbox.py`
- Modify: `tests/gateway/test_delivery_operation_journal.py`
- Modify: `tests/hermes_cli/test_workflows_spec.py`
- Modify: `tests/hermes_cli/test_workflows_capabilities.py`
- Modify: `tests/hermes_cli/test_workflows_dispatcher.py`
- Modify: `tests/hermes_cli/test_workflows_e2e.py`
- Modify: `tests/benchmarks/test_mission_manifest.py`

**Interfaces:**
- Consumes: declared `send_message` node, SessionDB outbox, `DeliveryRouter`, `OperationJournal`.
- Produces: previewable/revisable delayed messages, stable delivery ids, exactly-once-at-Hermes-boundary dispatch, and workflow resume on terminal delivery state.

- [ ] **Step 1: Write RED workflow/outbox lifecycle tests**

Test a mission-linked workflow entering `waiting` on `send_message`, outbox preview, revision/cancellation before `not_before`, explicit irreversible approval/release, confirmed delivery resuming the node, rejected edits after dispatch, and unknown acknowledgement projecting `unknown_effect`. Also prove an ordinary non-mission workflow can use the same durable outbox and operation journal without creating an `effect_transactions` row or receipt. Inject a crash:

1. before claim — one later dispatch;
2. after claim/before router call — lease recovery and one dispatch;
3. after router call/before local confirmation — reconcile by stable `delivery_id`, never blind resend;
4. after confirmation/before workflow resume — no redelivery, later tick resumes.

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest \
  tests/gateway/test_mission_outbox.py \
  tests/hermes_cli/test_workflows_spec.py \
  tests/hermes_cli/test_workflows_capabilities.py \
  tests/hermes_cli/test_workflows_dispatcher.py -q
```

Expected: `send_message` remains unsupported and dispatcher lacks an outbox path.

- [ ] **Step 3: Type and validate the node**

Add NodeSpec fields used only when `type == "send_message"`:

```python
platform: str | None = None
target: str | None = None
message: Any = None
not_before_seconds: int = Field(default=30, ge=1)
```

Require non-empty platform, target, and rendered message; cap delay using a stable `config.yaml` setting under `missions.outbox.max_delay_seconds`. A linked mission also requires the normalized `platform:target` in its immutable `authority.message_targets` allowlist. Never place target/message in the system prompt.

- [ ] **Step 4: Materialize and resume the waiting node**

Extend `_persist_waiting_nodes()` for `send_message`: use `execution_id + node_id` as idempotency identity, resolve an optional linked mission, call the coordinator/outbox adapter for mission effects or the operation-journal-backed outbox service for ordinary workflows, and store `outbox_id` in node-run output/reference state. Add `_resume_terminal_outbox_nodes()` beside wait/retry/agent-task resume functions. Confirmed delivery marks the node succeeded with a redacted result; cancelled/failed blocks or fails according to workflow semantics; unknown blocks a linked mission for review and fails an ordinary workflow with an explicit unknown-delivery error.

Only after these paths and tests pass, move `send_message` from `UNSUPPORTED_NODE_TYPES` to `IMPLEMENTED_NODE_TYPES`.

- [ ] **Step 5: Add `MissionOutboxDispatcher` to the gateway lifecycle**

Construct it with the gateway's SessionDB, one `OperationJournal`, and `DeliveryRouter`. Drain bounded batches on the existing background tick/lifecycle, with clean shutdown. For mission outbox rows, require an unexpired recorded irreversible approval immediately before claim; `hermes mission outbox release` obtains that approval through the existing identity-bound approval path after showing platform, destination, final content hash, and cancellation deadline. The dispatcher passes a stable `delivery_id` and persists the router acknowledgement before releasing the claim. Platform adapters that lack idempotency remain reconcilable/irreversible, not falsely exactly-once.

- [ ] **Step 6: Run GREEN and delivery/workflow regressions**

```bash
uv run --extra dev python -m pytest \
  tests/gateway/test_mission_outbox.py \
  tests/gateway/test_delivery_operation_journal.py \
  tests/gateway/test_delivery.py \
  tests/hermes_cli/test_workflows_spec.py \
  tests/hermes_cli/test_workflows_capabilities.py \
  tests/hermes_cli/test_workflows_dispatcher.py \
  tests/hermes_cli/test_workflows_e2e.py -q
git diff --check
```

Expected: all pass, including duplicate/restart delivery tests.

- [ ] **Step 7: Commit**

```bash
git add gateway/mission_outbox.py gateway/run.py \
  hermes_cli/workflows_spec.py hermes_cli/workflows_capabilities.py \
  hermes_cli/workflows_dispatcher.py tests/gateway/test_mission_outbox.py \
  tests/gateway/test_delivery_operation_journal.py \
  tests/hermes_cli/test_workflows_spec.py tests/hermes_cli/test_workflows_capabilities.py \
  tests/hermes_cli/test_workflows_dispatcher.py tests/hermes_cli/test_workflows_e2e.py \
  tests/benchmarks/test_mission_manifest.py
git commit -m "feat: add transactional delayed message outbox"
```

---

### Task 8: Propagate Mission Context to Workers and Reconcile Cross-Store State

**Files:**
- Modify: `hermes_cli/workflows_dispatcher.py`
- Modify: `hermes_cli/kanban_db.py`
- Modify: `hermes_cli/missions_db.py`
- Modify: `agent/effect_transactions.py`
- Modify: `tests/hermes_cli/test_workflows_dispatcher.py`
- Modify: `tests/hermes_cli/test_kanban_worker_spawn_toolsets.py`
- Modify: `tests/hermes_cli/test_kanban_worker_terminal_cwd.py`
- Modify: `tests/hermes_cli/test_missions_db.py`
- Modify: `tests/agent/test_effect_transactions.py`

**Interfaces:**
- Consumes: workflow-created Kanban provenance `workflow:<execution>:version:<n>:node:<id>`, profile-local workflows DB, worker subprocess environment.
- Produces: internal `HERMES_MISSION_ID` correlation, stable transaction sequence/dependencies, and restart reconciliation without profile leakage.

- [ ] **Step 1: Write RED propagation/isolation tests**

Assert that `_default_spawn()`:

- resolves the workflow execution from the existing `task.created_by` value;
- resolves a mission link from the same active profile-local `workflows.db`;
- adds `HERMES_MISSION_ID=<id>` only for linked workflow tasks;
- never accepts a mission id supplied through task body/title/user config;
- leaves ordinary Kanban workers unchanged;
- rejects a mission workflow whose agent task assignee resolves to a different profile/HERMES_HOME;
- preserves `HERMES_HOME`, `TERMINAL_CWD`, board, branch, provider, and toolset behavior.

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest \
  tests/hermes_cli/test_kanban_worker_spawn_toolsets.py \
  tests/hermes_cli/test_kanban_worker_terminal_cwd.py \
  tests/hermes_cli/test_workflows_dispatcher.py \
  tests/hermes_cli/test_missions_db.py -q
```

Expected: linked worker lacks mission context.

- [ ] **Step 3: Add a strict provenance parser and mission resolver**

Put parsing in the workflow/mission owner, not ad hoc string splitting in the worker:

```python
@dataclass(frozen=True)
class WorkflowTaskOrigin:
    execution_id: str
    version: int
    node_id: str

def parse_workflow_task_origin(created_by: str | None) -> WorkflowTaskOrigin | None: ...
def mission_for_execution(conn: sqlite3.Connection, execution_id: str) -> MissionRecord | None: ...
```

Malformed or non-workflow provenance returns `None`; it never guesses. `_default_spawn()` resolves against the worker's already selected profile root and sets the internal env only after the profile match check passes.

- [ ] **Step 4: Allocate effect dependencies conservatively**

At transaction creation, reserve a monotonically increasing `sequence_no` per mission and set `depends_on` to the latest uncompensated mutating transaction. This total order is the V1 dependency DAG: conservative but correct. Reads do not enter the chain. Add a documented future seam for adapter-declared resource dependencies only after this proof.

- [ ] **Step 5: Implement restart reconciliation**

`reconcile_mission(mission_id)` must be idempotent and perform, in order:

1. reconcile operation-journal `running`/`dispatched` entries through their adapters;
2. project any unresolved ambiguity to one open review item and `unknown_effect` candidate state;
3. resume queued/waiting workflow work through the existing dispatcher;
4. project terminal workflow execution state;
5. invoke the receipt scorer only when every linked execution is terminal and every transaction/outbox record is settled;
6. insert/link one content-addressed receipt;
7. never change a terminal mission on a later reconciliation.

If receipt insertion succeeds but mission projection fails, the next run finds the receipt by `mission_id + content_hash` and links it. No distributed transaction is claimed.

- [ ] **Step 6: Run GREEN and worker regressions**

```bash
uv run --extra dev python -m pytest \
  tests/hermes_cli/test_kanban_worker_spawn_toolsets.py \
  tests/hermes_cli/test_kanban_worker_terminal_cwd.py \
  tests/hermes_cli/test_workflows_dispatcher.py \
  tests/hermes_cli/test_missions_db.py \
  tests/agent/test_effect_transactions.py -q
git diff --check
```

Expected: all pass; ordinary workflow/Kanban runs contain no mission env/state changes.

- [ ] **Step 7: Commit**

```bash
git add hermes_cli/workflows_dispatcher.py hermes_cli/kanban_db.py \
  hermes_cli/missions_db.py agent/effect_transactions.py \
  tests/hermes_cli/test_workflows_dispatcher.py \
  tests/hermes_cli/test_kanban_worker_spawn_toolsets.py \
  tests/hermes_cli/test_kanban_worker_terminal_cwd.py \
  tests/hermes_cli/test_missions_db.py tests/agent/test_effect_transactions.py
git commit -m "feat: propagate and reconcile mission execution"
```

---

### Task 9: Deliver the Terminal-First Mission CLI and Skill

**Files:**
- Create: `hermes_cli/missions.py`
- Create: `skills/mission-control/SKILL.md`
- Create: `tests/hermes_cli/test_mission_cli.py`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/main.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Modify: `cli.py`
- Modify: `tests/hermes_cli/test_commands.py`
- Modify: `tests/gateway/test_gateway_command_help.py`

**Interfaces:**
- Consumes: mission DB/service, transaction coordinator, receipt renderer, outbox/state/workspace adapters.
- Produces: `hermes mission ...` and classic `/mission ...`; the existing TUI `slash.exec` path supplies Ink TUI and Dashboard embedded-TUI access automatically.

- [ ] **Step 1: Write RED parser and command tests**

Test both top-level parser dispatch and `run_slash()` for:

```text
mission start <workflow> --objective <text> --constraints <yaml> --authority <yaml> --evidence <yaml> [--input <yaml>]
mission list [--status <status>]
mission show <mission-id>
mission events <mission-id>
mission review <mission-id>
mission reconcile <mission-id>
mission compensate <mission-id> <transaction-id> [--cascade]
mission receipt <mission-id> [--recheck]
mission outbox list <mission-id>
mission outbox revise <outbox-id> --message <text> [--not-before <timestamp>]
mission outbox cancel <outbox-id>
mission outbox release <outbox-id>
mission state workflow <mission-id> <deploy|enable|disable> ...
mission state cron <mission-id> <create|update|disable> ...
mission state config <mission-id> set <key> <value>
mission workspace commit <mission-id> --message <text> [--path <path> ...]
```

Reject missing manifest files, unknown evidence checks, expired authority, cross-profile workflows, unsupported effects, secret config keys, destinations outside `authority.message_targets`, outbox edits after dispatch, compensation with live dependents, main-branch commits, and every unrecognized trailing argument. `outbox release` shows the final destination/content hash, requires the existing interactive approval, and persists a short-lived approval bound to outbox id, revision, content hash, destination, and authority version; non-interactive release fails closed. Render statuses exactly as `verified`, `completed_unverified`, `failed`, `blocked`, or `unknown_effect`.

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest tests/hermes_cli/test_mission_cli.py -q
```

Expected: mission parser/module absent.

- [ ] **Step 3: Implement one shared parser/service surface**

Follow `hermes_cli/workflows.py`: `build_parser(subparsers)`, `mission_command(args) -> int`, and `run_slash(rest) -> str` must call the same command functions. YAML/JSON inputs are loaded from explicit files or inline values with size limits and helpful validation errors. Output is redacted using existing helpers; JSON mode, if offered, emits stable machine-readable objects.

`mission start` prints the mission id, linked execution id, authority expiry, effect allowlist, and next inspection command. It does not automatically broaden the workflow's authority.

- [ ] **Step 4: Register terminal surfaces only**

Add:

```python
CommandDef(
    "mission",
    "Run durable, reversible, evidence-scored missions",
    "Tools & Skills",
    aliases=("missions",),
    args_hint="[subcommand]",
    cli_only=True,
    subcommands=(
        "start", "list", "show", "events", "review", "reconcile",
        "compensate", "receipt", "outbox", "state", "workspace",
    ),
)
```

Add `_handle_mission_command()` beside `_handle_workflow_command()`, main parser registration beside `_build_workflow_parser`, and `elif canonical == "mission"` in `cli.py`. Do not add gateway messaging command parity, a Desktop command, Dashboard React state, or a new TUI RPC.

- [ ] **Step 5: Add the mission-control skill**

The complete `SKILL.md` instructs the agent to:

- inspect with `hermes mission show/events/review/receipt`;
- use `hermes mission state`, `workspace commit`, and outbox commands instead of direct untracked mutations;
- explain previews and irreversible boundaries to the user;
- never call a mission successful without a `verified` receipt;
- stop on `unknown_effect` and request reconciliation/review;
- never push, purchase, delete an account, submit a browser form, or wrap arbitrary shell commands in V1;
- read the skill fully before acting and use no pagination escape hatch.

- [ ] **Step 6: Run GREEN and command catalog regressions**

```bash
uv run --extra dev python -m pytest \
  tests/hermes_cli/test_mission_cli.py \
  tests/hermes_cli/test_workflow_cli.py \
  tests/hermes_cli/test_workflows_prompts.py \
  tests/hermes_cli/test_workflows_redaction.py \
  tests/hermes_cli/test_commands.py \
  tests/gateway/test_gateway_command_help.py -q
git diff --check
```

Expected: all pass; `/mission` is discoverable in CLI/TUI catalogs but absent from gateway menus because it is `cli_only`.

- [ ] **Step 7: Smoke-test the actual terminal entry points**

```bash
uv run hermes mission --help
uv run hermes mission list
```

Expected: help lists the bounded subcommands; list succeeds against the active profile and does not start the Desktop app or Dashboard.

- [ ] **Step 8: Commit**

```bash
git add hermes_cli/missions.py skills/mission-control/SKILL.md \
  hermes_cli/commands.py hermes_cli/main.py hermes_cli/cli_commands_mixin.py cli.py \
  tests/hermes_cli/test_mission_cli.py tests/hermes_cli/test_commands.py \
  tests/gateway/test_gateway_command_help.py
git commit -m "feat: add terminal-first mission control"
```

---

### Task 10: Prove the Three-Effect Mission Across Crashes, Revisions, and Receipts

**Files:**
- Create: `tests/hermes_cli/test_mission_e2e.py`
- Create: `benchmarks/missions/run_vertical_slice.py`
- Modify: `benchmarks/missions/fixtures/three-effect-mission.yaml`
- Modify: `tests/benchmarks/test_mission_manifest.py`
- Create: `website/docs/user-guide/features/missions.md`
- Modify: `website/sidebars.ts`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`

**Interfaces:**
- Consumes: the complete vertical slice through real CLI/service entry points.
- Produces: one reproducible, forced-recovery proof and truthful operator documentation.

- [ ] **Step 1: Write the end-to-end test before documentation**

The test creates a temporary `HERMES_HOME`, a real Git repo + disposable worktree, deploys the fixture workflow, starts a mission through the same function used by `hermes mission start`, and performs all three effects:

1. worker `write_file`/`patch`, fresh verification evidence, local commit;
2. versioned Hermes config or workflow state change;
3. delayed message, revised once, then released to a fake in-process platform boundary that records stable delivery ids.

Parameterize process interruption at `after_prepare`, `after_preview`, `after_commit_started`, `after_handler_return`, and `after_delivery_dispatch`. Reopen all databases and tick/reconcile through public services. Assert:

```python
assert final.mission.verdict == "verified"
assert final.delivery_count == 1
assert final.effect_commit_counts == {
    "workspace.v1": 1,
    "hermes.config-state.v1": 1,
    "message-outbox.v1": 1,
}
assert final.receipt.objective == original_objective
assert final.receipt.transaction_ids == final.transaction_ids
assert all(artifact.sha256 for artifact in final.receipt.artifacts)
```

Add negative cases: stale authority immediately before commit -> `blocked`; ambiguous delivery acknowledgement -> `unknown_effect`; missing verification -> `completed_unverified`; tampered artifact -> recheck observation fails while original receipt is unchanged.

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest tests/hermes_cli/test_mission_e2e.py -q
```

Expected: failures expose any missing cross-module wiring; fix production code in the owning task's files, never by weakening assertions.

- [ ] **Step 3: Run GREEN on the real-path proof**

Do not mock SQLite, filesystem/Git, workflow dispatcher, coordinator, scorer, or CLI parsing. Mock only the final platform `send()` and deterministic crash injector. Each crash parameter must create a new Python object graph and reopen storage; at least `after_handler_return` must use a subprocess that is terminated before local confirmation.

```bash
uv run --extra dev python -m pytest tests/hermes_cli/test_mission_e2e.py -q
```

Expected: all interruption and negative cases pass with zero duplicate effects and zero false verified receipts.

- [ ] **Step 4: Run the complete focused regression matrix**

```bash
uv run --extra dev python -m pytest \
  tests/benchmarks/test_mission_manifest.py \
  tests/agent/test_operation_journal.py \
  tests/agent/test_effect_transactions.py \
  tests/agent/test_effect_adapters.py \
  tests/agent/test_receipts.py \
  tests/agent/test_verification_evidence.py \
  tests/tools/test_registry.py \
  tests/tools/test_checkpoint_manager.py \
  tests/tools/test_file_tools.py \
  tests/hermes_cli/test_missions_db.py \
  tests/hermes_cli/test_mission_cli.py \
  tests/hermes_cli/test_mission_e2e.py \
  tests/hermes_cli/test_workflows_db.py \
  tests/hermes_cli/test_workflows_dispatcher.py \
  tests/hermes_cli/test_workflows_e2e.py \
  tests/gateway/test_mission_outbox.py \
  tests/gateway/test_delivery_operation_journal.py -q
git diff --check
```

Expected: all pass.

- [ ] **Step 5: Verify cache, role, and tool-schema invariants**

Add or extend tests that run a multi-turn mission-scoped conversation and independently hash the system message plus effective tool definitions before and after transaction state changes. Assert provider/model identity and strict role alternation. Run:

```bash
uv run --extra dev python -m pytest \
  tests/test_get_tool_definitions_cache_isolation.py \
  tests/run_agent -q -k 'system_prompt or alternation or tool_schema or cache'
```

Expected: all selected tests pass and hashes remain identical.

- [ ] **Step 6: Write operator documentation after behavior is proven**

Document:

- the layman outcome and one copyable terminal walkthrough;
- mission/transaction/receipt status meanings;
- exact authority/evidence YAML examples;
- workspace/state/outbox boundaries;
- cancel/revise/compensate/reconcile behavior;
- why `unknown_effect` requires review;
- why `completed_unverified` is not failure but is not proof;
- no remote push, arbitrary shell, browser writes, purchases, cross-profile mission, gateway command, Desktop dependency, or live commerce/federation support;
- Dashboard access is through its existing embedded Ink TUI, with no separate parity promise.

- [ ] **Step 7: Run the preregistered proof harness in report-only mode**

Add the report-only runner as `benchmarks/missions/run_vertical_slice.py`. It executes the fixture matrix and writes JSON to stdout; it must not relax the 30/100/50 portfolio gates. Record that the vertical slice is an engineering proof, while the full 90-day user-value gate still requires 30 real missions, 100 revisions/faults, and 50 false-success seeds.

- [ ] **Step 8: Final verification and commit**

Run the focused matrix in Step 4, cache invariants in Step 5, and the repository's docs checks:

```bash
cd website
npm run lint:diagrams
npm run typecheck
npm run build
cd ..
```

Expected: the Docusaurus build includes `/user-guide/features/missions` and all links resolve. Then run:

```bash
git status --short
git diff --check
```

Expected: only intended files are modified, all checks pass, no generated caches/secrets/test databases are present.

```bash
git add tests/hermes_cli/test_mission_e2e.py benchmarks/missions \
  tests/benchmarks/test_mission_manifest.py \
  website/docs/user-guide/features/missions.md website/sidebars.ts \
  website/docs/reference/cli-commands.md website/docs/reference/slash-commands.md
git commit -m "test: prove mission transaction receipt recovery"
```

---

## Completion Gate

Do not declare the vertical slice complete until fresh evidence proves all of the following:

- one real long-running workflow performs workspace, Hermes-state, and delayed-message effects;
- every forced crash boundary recovers without a duplicate effect;
- stale authority blocks at commit time;
- ambiguous effects surface as `unknown_effect` and are never retried blindly;
- compensation respects dependency order and irreversible boundaries;
- every terminal mission has one immutable receipt;
- the scorer emits zero false `verified` across at least 50 seeded false-success cases;
- system prompt, model tool schema, provider, and model stay stable across mission turns;
- ordinary non-mission tool/workflow/Kanban/gateway behavior remains unchanged;
- CLI and Ink TUI are the primary surfaces, Dashboard inherits the TUI, and Desktop has no dependency;
- focused and relevant regression suites pass from a clean checkout.

The implementation proves feasibility; it does not by itself satisfy the broader product gate. Production rollout remains contingent on 30 multi-hour real-user missions with at least 90% correct state, zero duplicate effects, all unknowns surfaced, one receipt per completion, 100 transaction revision/fault cases with under 15% median overhead, and the preregistered safety floors.
