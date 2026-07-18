# Reversible & Revisable Action Transactions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Hermes prepare and preview a bounded action graph, commit each effect under freshly rechecked authority, revise all still-uncommitted work, reconcile crashes without blind retries, and exactly reverse or semantically compensate only the effects whose adapters truthfully support it.

**Architecture:** Add a profile-local transaction aggregate beside the existing `agent_operations` journal in `state.db`. A small adapter SDK owns effect-specific prepare/preview/commit/reconcile/compensate behavior, while a generic coordinator owns graph revisions, current-authority checks, journal ordering, dependency-aware compensation, immutable receipts, and recovery. Expose the feature through `hermes transaction` and a native Ink TUI RPC; extend registry metadata and existing workflow/delivery services without adding a model-visible tool, rebuilding prompts, or creating another workflow engine.

**Tech Stack:** Python 3.13, SQLite/WAL through `SessionDB`, frozen dataclasses and enums, Pydantic workflow models, existing tool registry/middleware/approval/checkpoint/operation-journal services, Git CLI with argument arrays, Rich/classic CLI, Ink/TypeScript JSON-RPC TUI, pytest through `scripts/run_tests.sh`, Vitest, YAML benchmark manifests.

## Global Constraints

- Work from the branch containing this plan and preserve unrelated changes. Each task ends in exactly one conventional commit.
- TDD is mandatory. Run Python tests only through `scripts/run_tests.sh`; use the package-local npm scripts for TypeScript and documentation checks.
- Add no model-visible core tool and do not change any existing tool JSON schema. Transaction metadata is registry-internal; user/model operation is through CLI + skill or an existing orchestrator.
- The system prompt, effective model tool definitions, provider, and model remain byte-stable for the conversation. Do not inject synthetic user messages or mutate history outside existing compression.
- Profiles remain isolated. Every database, checkpoint, config, cron, workflow, receipt, and benchmark artifact resolves from `get_hermes_home()`; no transaction may cross `HERMES_HOME`.
- Stable settings live under `transactions:` in `config.yaml`. Credentials stay in `.env`/secret stores. `HERMES_TRANSACTION_ID`, `HERMES_TRANSACTION_REVISION`, and `HERMES_TRANSACTION_NODE_ID` are internal subprocess-correlation values, never documented as user configuration.
- `agent_operations` remains the durable certainty journal. Transaction tables add graph, preview, authority, adapter, evidence, revision, and compensation facts; they never replace journal states or make an unknown effect retryable.
- An adapter cannot manufacture guarantees. Exact reversal, semantic compensation, native idempotency, query reconciliation, and irreversible boundaries are separate declared capabilities and are verified at registration.
- Recheck authority after preview and immediately before every commit or compensation. Bind irreversible approval to transaction id, revision, node id, final argument hash, preview hash, destination/resource set, authority version, requester/channel, and expiry.
- Persist effect result/evidence before reporting success. If the underlying effect may have landed but its acknowledgement or durable state cannot be confirmed, record `unknown_effect`, stop descendants, and require reconciliation/review.
- Revisions may alter only uncommitted work. A committed, compensating, compensated, or unknown node is frozen in every later revision; no revision may remove it, change its arguments, rewrite its incoming causality, or pretend it never happened.
- Compensation walks the committed dependency graph in reverse topological order. It stops before an irreversible boundary, unknown effect, drifted resource, expired compensation window, or live uncompensated dependent; `--cascade` never means “best effort through danger.”
- First adapter families are exactly: workspace/filesystem plus local disposable-worktree Git; Hermes-owned workflow/cron/config state; delayed outbound-message outbox. Remote push, arbitrary shell wrapping, production databases, browser/service writes, account deletion, purchases, and cross-profile writes are excluded.
- Message send is an irreversible boundary after dispatch unless a concrete platform adapter proves edit/delete compensation for that exact message. A provider idempotency key means dedupe support, not exactly-once delivery.
- Real-path tests use a temporary `HERMES_HOME`, real SQLite, real temp files and Git worktrees, real config/cron/workflow stores, and object-graph/process restart. Mock only the final network adapter and deterministic crash boundary.
- No outbound telemetry. Benchmark output is local JSON/Markdown and reports denominators, Wilson intervals, p50/p95 latency, cost source, exclusions, and safety slices separately.
- Consume the shared immutable receipt contract owned by portfolio item #12 and introduced by the approved Missions/Transactions/Receipts vertical slice. Do not create a second receipt table, status vocabulary, scorer registry, or observation format inside `agent/effects`.

---

## Approved Portfolio Contract

**Layman outcome:** Hermes can show what an action will do, commit it under current permission, revise the remaining plan, and undo or compensate completed steps when the underlying service makes that possible.

**90-day proof:** Exercise 100 preregistered revisions, stale approvals, crashes, duplicate deliveries, and partial failures across workspace/local-worktree Git, Hermes workflow/cron/config state, and delayed outbound messaging. Pass only with zero unauthorized irreversible commits, no duplicate instrumented effects, correct compensation ordering, explicit classification of every non-reversible operation, and less than 15% median overhead on eligible flows.

**Dependencies and failure conditions:** Portfolio item #6 supplies deterministic authority and item #12 supplies the shared receipt/evidence contract. A service without native idempotency, queryable state, or compensation remains guarded/reconcilable/irreversible; this plan never markets it as transactional or undoable.

**Delivery:** Footprint Ladder rung 1 extends generic registry/journal metadata and middleware. Adapter implementations remain owned by their existing tool, service, plugin, or MCP boundary; CLI + skill and Ink TUI are the primary control surfaces.

---

## Scope, Ownership, and Truthful Vocabulary

```text
state.db
├── agent_operations                  existing certainty journal
├── action_transactions               aggregate + current revision/status
├── transaction_revisions             immutable selected-plan snapshots
├── transaction_revision_nodes        node spec at one revision
├── transaction_revision_edges        dependency edges at one revision
├── effect_transactions               one prepared/committed attempt per node revision
├── transaction_events                append-only audit/recovery history
├── transaction_approvals             exact, expiring approval bindings
├── effect_compensations               journaled compensation attempts
├── transaction_outbox                delayed/revisable outbound sends
└── receipts / receipt_observations    immutable proof + later observations

workflows.db                          existing workflow definitions/executions
cron/jobs.json                        existing cron store + cross-process lock
config.yaml                           existing atomic, managed-scope-aware config
checkpoint shadow store               exact workspace before-state references
```

The following terms are user-visible contracts:

| Term | Exact meaning |
|---|---|
| `reversible` | Adapter can restore the exact observed before-state if the stored drift/window predicates still hold. |
| `compensatable` | Adapter can apply a declared semantic counter-action, but cannot promise byte-for-byte restoration. |
| `irreversible` | No adapter-proven reversal exists after the boundary; stronger approval is required. |
| `reconcilable` | Adapter can query durable external/local state and classify `landed`, `not_landed`, or `unknown`. |
| `idempotent` | Repeating commit with the same key is provider/adapter-safe; this does not imply exactly-once. |
| `unknown_effect` | Hermes cannot prove whether an outward effect landed; it will not retry or compensate blindly. |
| `undo eligible` | Current dynamic eligibility is `eligible_exact`; semantic compensation is displayed separately as `eligible_compensation`. |

Canonical state sets:

```python
TransactionStatus = Literal[
    "draft", "previewing", "ready", "committing", "committed",
    "revising", "compensating", "compensated", "partially_compensated",
    "blocked", "failed", "unknown_effect", "cancelled",
]
EffectPhase = Literal[
    "planned", "prepared", "previewed", "committing", "committed", "verified",
    "superseded", "compensating", "compensated", "blocked", "failed",
    "unknown_effect",
]
CompensationFidelity = Literal["exact", "semantic", "none"]
ReconcileDisposition = Literal["landed", "not_landed", "unknown"]
EligibilityCode = Literal[
    "eligible_exact", "eligible_compensation", "already_compensated",
    "blocked_live_dependents", "blocked_irreversible_boundary", "blocked_unknown",
    "blocked_drift", "blocked_window_expired", "blocked_authority",
    "unsupported",
]
```

## Revision and Commit Semantics

1. `create` writes revision 1 as an immutable node/edge snapshot after schema, adapter, resource, and DAG validation.
2. `preview` prepares nodes in topological order, persists before-state and redacted preview, and hashes the complete preview set. Preparation does not call an effect handler.
3. `commit` uses only the current ready revision. Before each node, it reloads the transaction and current authority, rejects stale preview/authority, verifies all parents, creates/loads the operation-journal identity, and invokes the adapter once.
4. `revise --expected-revision N` is optimistic-CAS. It copies frozen nodes into revision N+1, validates their exact spec and incoming edges, permits add/change/remove only for phases `planned|prepared|previewed|blocked|failed`, marks old prepared attempts `superseded`, and requires a new preview.
5. A node in `committing` or `unknown_effect` freezes the transaction frontier. Revision may describe unrelated pending work for review, but no descendant commit proceeds until reconciliation classifies the node.
6. Committed nodes remain facts in later graphs. New pending nodes may depend on them. A revision cannot add a new parent to an already committed node or remove any edge between frozen nodes.
7. Normal commit uses topological order with stable node-id tie breaking. Failure policy defaults to `stop`; optional `compensate_prefix` compensates only the eligible committed prefix after a separate authority recheck.
8. Cascade compensation computes the active committed descendants of the target, rejects the request if any descendant crosses an unsafe boundary, then compensates reverse topological order. Each compensation is its own journaled operation.

## File Map

### New production files

- `agent/effects/__init__.py` — stable public SDK exports only.
- `agent/effects/models.py` — frozen transaction, graph, effect, preview, authority, reconciliation, eligibility, and compensation value objects.
- `agent/effects/store.py` — typed `SessionDB` access, CAS transitions, immutable revisions/events/approvals/outbox/receipt queries.
- `agent/effects/registry.py` — adapter registration/discovery and capability-contract validation.
- `agent/effects/graph.py` — DAG validation, revision diff rules, frontier, topological commit, and reverse-cascade selection.
- `agent/effects/authority.py` — maps prepared effects into item #6's canonical `ActionContext`, reloads the canonical `AuthorityProvider`, and owns only exact transaction approval binding.
- `agent/effects/coordinator.py` — prepare/preview/commit/revise/reconcile/compensate orchestration around `OperationJournal`.
- `agent/effects/recovery.py` — bounded owner-fenced startup reconciliation shared by CLI, TUI, and gateway.
- `agent/effects/eligibility.py` — dynamic exact-undo/semantic-compensation eligibility and human explanations.
- `agent/effects/receipts.py` — transaction evidence snapshot and canonical receipt builder over the generic receipt schema.
- `agent/effects/adapters/__init__.py` — registers the three built-in adapter families.
- `agent/effects/adapters/workspace.py` — file/V4A/local-worktree prepare, diff, commit, verify, reconcile, restore/reset.
- `agent/effects/adapters/hermes_state.py` — workflow, cron, and config adapters over owner-module mutation services.
- `agent/effects/adapters/message_outbox.py` — delayed message prepare/revise/release/reconcile/optional edit-delete compensation.
- `gateway/transaction_outbox.py` — leased outbox dispatcher over `DeliveryRouter`.
- `hermes_cli/transactions.py` — shared top-level/classic-slash command parser, service calls, JSON/text renderers.
- `skills/action-transactions/SKILL.md` — full terminal-first operating instructions and exclusions.

### Existing production files modified

- `hermes_state.py` — declarative tables/indexes only; retain schema reconciliation convention.
- `tools/registry.py` — internal effect metadata; schema generation remains byte-identical.
- `hermes_cli/middleware.py` — terminal-handler transaction coordinator wrapper after plugin argument finalization.
- `agent/operation_journal.py` — exact query/CAS helpers; keep unknown non-retryable.
- `tools/checkpoint_manager.py` — forced checkpoint refs and exact restore API while preserving `ensure_checkpoint()`.
- `tools/file_tools.py` — workspace adapter metadata on existing `write_file` and `patch` registrations.
- `hermes_cli/workflows_db.py` — immutable workflow state snapshot/apply/restore services.
- `hermes_cli/workflows_spec.py`, `hermes_cli/workflows_capabilities.py`, `hermes_cli/workflows_dispatcher.py` — typed durable `send_message` node and outbox resume.
- `cron/jobs.py` — revision-hashed snapshot/apply/restore services inside the existing cross-process lock.
- `hermes_cli/config.py` — `transactions` defaults plus non-printing validated config mutation/restore services.
- `gateway/delivery.py` — durable acknowledgement failures surface unknown; adapter edit/delete capabilities are explicit.
- `gateway/run.py`, `cli.py`, `tui_gateway/server.py` — construct adapters/store/coordinator, run bounded recovery, and expose lifecycle/RPC entry points.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `hermes_cli/cli_commands_mixin.py` — register `transaction`/`tx` top-level and classic slash routes.
- `ui-tui/src/gatewayTypes.ts`, `ui-tui/src/app/slash/commands/ops.ts` — native structured `/transaction` route through the already-registered ops command group.

### Benchmark and documentation files

- `benchmarks/transactions/manifest.yaml` — frozen 100-case denominator, faults, metrics, thresholds, exclusions.
- `benchmarks/__init__.py`, `benchmarks/transactions/__init__.py` — explicit importable benchmark package markers.
- `benchmarks/transactions/cases.py` — deterministic case expansion and invariant validation.
- `benchmarks/transactions/runner.py` — local report-only baseline/candidate executor.
- `benchmarks/transactions/fixtures/plan.yaml` — three-family transaction graph.
- `benchmarks/transactions/fixtures/authority.yaml` — bounded, expiring example authority.
- `website/docs/user-guide/features/action-transactions.md` — operator guide.
- `website/docs/development/effect-adapters.md` — SDK contract and standalone-plugin guidance.
- `website/docs/reference/cli-commands.md`, `website/docs/reference/slash-commands.md`, `website/sidebars.ts` — command references/navigation.

### Focused tests

- `tests/agent/effects/test_store.py`
- `tests/agent/effects/test_registry.py`
- `tests/agent/effects/test_graph.py`
- `tests/agent/effects/test_authority.py`
- `tests/agent/effects/test_coordinator.py`
- `tests/agent/effects/test_recovery.py`
- `tests/agent/effects/test_eligibility.py`
- `tests/agent/effects/test_receipts.py`
- `tests/agent/effects/adapters/test_workspace.py`
- `tests/agent/effects/adapters/test_hermes_state.py`
- `tests/agent/effects/adapters/test_message_outbox.py`
- `tests/gateway/test_transaction_outbox.py`
- `tests/hermes_cli/test_transaction_cli.py`
- `tests/hermes_cli/test_transaction_e2e.py`
- `tests/benchmarks/test_transaction_benchmark.py`
- `tests/tui_gateway/test_transaction_rpc.py`
- `ui-tui/src/__tests__/transactionCommand.test.ts`

---

### Task 0: Preregister the 100-Case Contract Before Production Code

**Files:**
- Create: `benchmarks/__init__.py`
- Create: `benchmarks/transactions/__init__.py`
- Create: `benchmarks/transactions/manifest.yaml`
- Create: `benchmarks/transactions/cases.py`
- Create: `benchmarks/transactions/fixtures/plan.yaml`
- Create: `benchmarks/transactions/fixtures/authority.yaml`
- Create: `tests/benchmarks/test_transaction_benchmark.py`

**Interfaces:**
- Produces `load_cases(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]` and a frozen benchmark denominator consumed by Task 13.
- The exact case strata are 20 plan revisions, 15 stale-authority commits, 25 crashes (five each at five boundaries), 10 duplicate-delivery attempts, 15 partial failures, and 15 compensation/irreversible-boundary cases: 100 total.

- [ ] **Step 1: Write the failing benchmark-contract test**

```python
from pathlib import Path

from benchmarks.transactions.cases import load_cases

ROOT = Path(__file__).resolve().parents[2]


def test_transaction_benchmark_is_frozen_bounded_and_truthful():
    manifest, cases = load_cases(ROOT / "benchmarks/transactions/manifest.yaml")
    assert manifest["schema"] == "hermes.action-transactions-benchmark.v1"
    assert len(cases) == 100
    assert {case["stratum"] for case in cases} == {
        "revision", "stale_authority", "crash", "duplicate_delivery",
        "partial_failure", "compensation_boundary",
    }
    assert manifest["gates"] == {
        "unauthorized_irreversible_commits": 0,
        "duplicate_instrumented_effects": 0,
        "incorrect_compensation_order": 0,
        "unclassified_non_reversible_effects": 0,
        "false_success_receipts": 0,
        "median_eligible_overhead_ratio_max": 0.15,
    }
    assert manifest["reporting"]["rate_interval"] == "wilson_95"
    assert manifest["baseline"] == "current_hermes_without_transaction_coordinator"
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_transaction_benchmark.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'benchmarks.transactions'`.

- [ ] **Step 3: Add the deterministic manifest loader and fixtures**

`cases.py` must reject duplicate case ids, a non-100 denominator, missing expected outcomes, or an unknown fault point. Use this complete expansion contract:

```python
STRATUM_COUNTS = {
    "revision": 20,
    "stale_authority": 15,
    "crash": 25,
    "duplicate_delivery": 10,
    "partial_failure": 15,
    "compensation_boundary": 15,
}
FAULT_POINTS = (
    "after_prepare", "after_preview", "after_commit_intent",
    "after_handler_return", "after_delivery_dispatch",
)


def expand_cases(manifest):
    cases = []
    for stratum, count in STRATUM_COUNTS.items():
        for index in range(count):
            case = {
                "id": f"{stratum}-{index + 1:03d}",
                "stratum": stratum,
                "expected": manifest["expected_by_stratum"][stratum],
            }
            if stratum == "crash":
                case["fault_point"] = FAULT_POINTS[index % len(FAULT_POINTS)]
            cases.append(case)
    if len({case["id"] for case in cases}) != 100:
        raise ValueError("benchmark case ids must be unique and total 100")
    return cases
```

The plan fixture contains `workspace_write -> config_set -> delayed_message`, with explicit adapter ids, actions, node ids, dependency edges, disposable-worktree root, test destination, and no credential. Authority expires at a fixture-controlled clock, allows only those resources/actions, and sets irreversible policy to `ask`.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_transaction_benchmark.py -q`

Expected: PASS; the parsed graph has three nodes/two edges and the expansion reports exactly the six preregistered strata.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/transactions tests/benchmarks/test_transaction_benchmark.py
git commit -m "test: preregister action transaction benchmark"
```

---

### Task 1: Persist Immutable Transaction Graphs and Effect Attempts

**Files:**
- Create: `agent/effects/__init__.py`
- Create: `agent/effects/models.py`
- Create: `agent/effects/store.py`
- Modify: `hermes_state.py`
- Create: `tests/agent/effects/test_store.py`

**Interfaces:**
- Produces `TransactionStore(SessionDB)`, frozen records, canonical JSON/hash helpers, CAS phase/status transitions, immutable revision snapshots, and append-only events.
- Consumes the existing `SessionDB._execute_read()` / `_execute_write()` transaction chokepoints and declarative `SCHEMA_SQL` reconciliation.

- [ ] **Step 1: Write failing real-SQLite storage tests**

```python
def test_revision_and_effect_storage_is_immutable_and_cas(session_db):
    store = TransactionStore(session_db)
    created = store.create_transaction(
        transaction_id="tx-1", profile="default", title="bounded change",
        authority=authority_fixture(), graph=graph_fixture(), failure_policy="stop",
    )
    assert created.current_revision == 1
    assert store.get_revision("tx-1", 1).content_hash
    OperationJournal(session_db).create(operation_id="op-1", kind="effect_commit")
    effect = store.create_effect_attempt(
        effect_id="ef-1", transaction_id="tx-1", revision=1,
        node_id="workspace_write", operation_id="op-1", adapter_id="workspace.v1",
    )
    assert effect.phase == "planned"
    assert store.transition_effect("ef-1", {"planned"}, "prepared")
    assert not store.transition_effect("ef-1", {"planned"}, "prepared")
    with pytest.raises(ImmutableRecordError):
        store.replace_revision("tx-1", 1, graph_fixture())


def test_reopen_preserves_graph_events_approval_outbox_and_receipt(tmp_path):
    first = SessionDB(tmp_path / "state.db")
    seed_complete_transaction(TransactionStore(first))
    first.close()
    second = SessionDB(tmp_path / "state.db")
    snapshot = TransactionStore(second).load_snapshot("tx-1")
    assert snapshot.transaction.status == "committed"
    assert [event.kind for event in snapshot.events] == [
        "transaction_created", "revision_previewed", "effect_committed",
        "receipt_issued",
    ]
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/effects/test_store.py -q`

Expected: FAIL importing `agent.effects.store`.

- [ ] **Step 3: Add frozen models and canonical serialization**

Define enum-backed frozen dataclasses for `ActionTransaction`, `TransactionRevision`, `RevisionNode`, `RevisionEdge`, `EffectTransaction`, `TransactionEvent`, `ApprovalBinding`, `CompensationAttempt`, `OutboxRecord`, and `UndoEligibility`. Canonical hashes use:

```python
def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
```

Validate every enum before SQL. Decode JSON to defensive copies. Timestamps are integer Unix milliseconds so event ordering can use `(created_at_ms, event_id)` without float ambiguity.

- [ ] **Step 4: Add declarative tables and typed store methods**

Add tables with DB-level keys/checks. The full schema must include these identities:

```sql
CREATE TABLE IF NOT EXISTS action_transactions (
    transaction_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    authority_version INTEGER NOT NULL,
    authority_json TEXT NOT NULL,
    failure_policy TEXT NOT NULL,
    receipt_id TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transaction_revisions (
    transaction_id TEXT NOT NULL REFERENCES action_transactions(transaction_id),
    revision INTEGER NOT NULL,
    base_revision INTEGER,
    reason TEXT NOT NULL,
    graph_hash TEXT NOT NULL,
    preview_hash TEXT,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (transaction_id, revision),
    UNIQUE (transaction_id, graph_hash)
);
CREATE TABLE IF NOT EXISTS transaction_revision_nodes (
    transaction_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    action TEXT NOT NULL,
    args_json TEXT NOT NULL,
    resource_keys_json TEXT NOT NULL,
    PRIMARY KEY (transaction_id, revision, node_id),
    FOREIGN KEY (transaction_id, revision)
        REFERENCES transaction_revisions(transaction_id, revision)
);
CREATE TABLE IF NOT EXISTS transaction_revision_edges (
    transaction_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_node_id TEXT NOT NULL,
    child_node_id TEXT NOT NULL,
    PRIMARY KEY (transaction_id, revision, parent_node_id, child_node_id)
);
CREATE TABLE IF NOT EXISTS effect_transactions (
    effect_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES action_transactions(transaction_id),
    revision INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE REFERENCES agent_operations(operation_id),
    adapter_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    semantics_json TEXT NOT NULL,
    prepared_json TEXT,
    preview_json TEXT,
    preview_hash TEXT,
    authority_json TEXT,
    result_json TEXT,
    verification_json TEXT,
    reconciliation_json TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE (transaction_id, revision, node_id)
);
CREATE TABLE IF NOT EXISTS transaction_events (
    event_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES action_transactions(transaction_id),
    kind TEXT NOT NULL,
    effect_id TEXT,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (transaction_id, idempotency_key)
);
```

Add the approval, compensation, outbox, receipt, and observation tables in their owning tasks, not speculative columns here. Do not bump `SCHEMA_VERSION` for additive tables; current `SessionDB` creates tables declaratively and reserves version bumps for data transforms.

- [ ] **Step 5: Run GREEN and schema regressions**

Run: `scripts/run_tests.sh tests/agent/effects/test_store.py tests/test_hermes_state.py tests/test_hermes_state_wal_fallback.py -q`

Expected: PASS on new and reopened databases; existing state schema tests remain green.

- [ ] **Step 6: Commit**

```bash
git add agent/effects hermes_state.py tests/agent/effects/test_store.py
git commit -m "feat: persist action transaction graphs"
```

---

### Task 2: Define the Effect Adapter SDK and Registry Metadata

**Files:**
- Create: `agent/effects/registry.py`
- Modify: `agent/effects/models.py`
- Modify: `agent/effects/__init__.py`
- Modify: `tools/registry.py`
- Modify: `tests/tools/test_registry.py`
- Create: `tests/agent/effects/test_registry.py`

**Interfaces:**
- Produces `EffectAdapter`, `AdapterDescriptor`, `EffectAdapterRegistry`, `register_effect_adapter()`, `get_effect_adapter()`, and registry metadata `effect_adapter` / `effect_action`.
- Adapter methods consume frozen requests and return frozen values; adapters never mutate transaction status directly.

- [ ] **Step 1: Write RED capability and schema-invariance tests**

```python
def test_effect_metadata_never_changes_model_schema(registry):
    before = registry.get_definitions({"write_file"})
    entry = registry.get_entry("write_file")
    registry.register(
        name="write_file", toolset=entry.toolset, schema=entry.schema,
        handler=entry.handler, override=True,
        effect_adapter="workspace.v1", effect_action="write_file",
    )
    assert registry.get_definitions({"write_file"}) == before
    assert registry.get_operation_metadata("write_file")["effect_adapter"] == "workspace.v1"


def test_adapter_registry_rejects_false_capability_claims():
    registry = EffectAdapterRegistry()
    with pytest.raises(AdapterContractError, match="exact compensation"):
        registry.register(AdapterWithExactClaimButNoCompensate())
    adapter = valid_fake_adapter()
    registry.register(adapter)
    with pytest.raises(AdapterContractError, match="duplicate adapter_id"):
        registry.register(adapter)
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/effects/test_registry.py tests/tools/test_registry.py -q`

Expected: FAIL because effect adapter metadata and SDK types do not exist.

- [ ] **Step 3: Add the complete SDK value contract**

```python
@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    actions: frozenset[str]
    idempotency: Literal["none", "keyed", "native"]
    reconciliation: Literal["none", "query"]
    compensation: Literal["exact", "semantic", "none"]
    irreversible_after: Literal["never", "dispatch", "commit"]
    compensation_window_seconds: int | None = None


class EffectAdapter(ABC):
    descriptor: AdapterDescriptor

    @abstractmethod
    def normalize(self, node: RevisionNode, context: EffectContext) -> NormalizedEffect:
        raise NotImplementedError

    @abstractmethod
    def prepare(self, effect: NormalizedEffect, context: EffectContext) -> PreparedEffect:
        raise NotImplementedError

    @abstractmethod
    def preview(self, prepared: PreparedEffect, context: EffectContext) -> EffectPreview:
        raise NotImplementedError

    @abstractmethod
    def commit(self, request: CommitRequest, context: EffectContext) -> CommitOutcome:
        raise NotImplementedError

    @abstractmethod
    def verify(self, outcome: CommitOutcome, context: EffectContext) -> VerificationResult:
        raise NotImplementedError

    @abstractmethod
    def reconcile(self, effect: EffectTransaction, context: EffectContext) -> ReconciliationResult:
        raise NotImplementedError

    @abstractmethod
    def compensate(self, request: CompensationRequest, context: EffectContext) -> CompensationResult:
        raise NotImplementedError
```

`CommitRequest` carries the prepared token, stable `operation_id`, stable idempotency key, and an optional single-use `invoke(effective_args)` callback for existing tool handlers. `EffectPreview` includes redacted summary, before/after claims, affected resources, semantics, approval requirement, and uncertainty; it never carries credentials.

- [ ] **Step 4: Validate descriptors against concrete method support**

At registration, reject an empty/versionless adapter id, no actions, `reconciliation="query"` when `reconcile` is inherited unchanged, compensation claims without an override, `compensation_window_seconds` with `compensation="none"`, or `irreversible_after="never"` with `compensation="none"`. Return immutable descriptor snapshots and fail on duplicate ids; plugin adapters register through this same function without modifying core.

- [ ] **Step 5: Extend `ToolEntry` metadata only**

Add `effect_adapter: str | None` and `effect_action: str | None` to `__slots__`, `__init__`, `ToolRegistry.register()`, and `get_operation_metadata()`. Defaults are `None`. Validate that the pair is both present or both absent. Do not include either value in `get_definitions()`, `get_schema()`, dynamic schema overrides, toolset snapshots, or cache generation.

- [ ] **Step 6: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/effects/test_registry.py tests/tools/test_registry.py tests/test_get_tool_definitions_cache_isolation.py -q`

Expected: PASS; tool-definition hashes before/after metadata registration are identical.

- [ ] **Step 7: Commit**

```bash
git add agent/effects tools/registry.py tests/agent/effects/test_registry.py tests/tools/test_registry.py
git commit -m "feat: define effect adapter sdk"
```

---

### Task 3: Implement DAG Validation and Immutable Plan Revisions

**Files:**
- Create: `agent/effects/graph.py`
- Modify: `agent/effects/models.py`
- Modify: `agent/effects/store.py`
- Create: `tests/agent/effects/test_graph.py`
- Modify: `tests/agent/effects/test_store.py`

**Interfaces:**
- Produces `validate_graph()`, `topological_order()`, `reverse_compensation_order()`, `validate_revision()`, `create_revision()` and `RevisionDiff`.
- Consumes immutable revision snapshots and latest effect phases from `TransactionStore`.

- [ ] **Step 1: Write RED tests for the complete revision truth table**

```python
@pytest.mark.parametrize("phase", ["committed", "verified", "compensating", "compensated", "unknown_effect"])
def test_revision_cannot_remove_or_change_frozen_node(store, phase):
    seed_revision_with_phase(store, "tx-1", "write", phase)
    changed = graph_without_node("write")
    with pytest.raises(RevisionConflict, match="frozen node write"):
        create_revision(store, "tx-1", expected_revision=1, graph=changed, reason="change")


def test_revision_supersedes_prepared_attempt_and_preserves_committed_causality(store):
    seed_mixed_graph(store)
    revised = graph_with_changed_pending_message_and_new_audit_node()
    record = create_revision(store, "tx-1", expected_revision=1, graph=revised, reason="new recipient")
    assert record.revision == 2
    assert store.effect_for("tx-1", 1, "message").phase == "superseded"
    assert store.get_node("tx-1", 2, "write") == store.get_node("tx-1", 1, "write")
    assert topological_order(revised) == ["write", "message", "audit"]


def test_revision_rejects_cycle_stale_cas_and_new_parent_for_committed_node(store):
    seed_committed_parent(store)
    with pytest.raises(GraphCycleError):
        create_revision(store, "tx-1", 1, cyclic_graph(), "cycle")
    with pytest.raises(RevisionConflict, match="expected revision 1, current 2"):
        create_revision(store, "tx-1", 1, valid_graph(), "stale")
    with pytest.raises(RevisionConflict, match="incoming edges of committed node"):
        create_revision(store, "tx-1", 2, graph_adding_parent_to_committed(), "rewrite history")
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/effects/test_graph.py tests/agent/effects/test_store.py -q`

Expected: FAIL importing `agent.effects.graph`.

- [ ] **Step 3: Implement deterministic graph algorithms**

Use Kahn topological sort with lexical node-id tie breaking. Validate node ids against `^[a-z][a-z0-9_-]{0,63}$`, unique edges, existing endpoints, no self-edge, registered adapter/action pair, canonical JSON arguments, and at least one node. `reverse_compensation_order(graph, selected)` returns the reverse of the stable topological order restricted to selected committed nodes.

- [ ] **Step 4: Implement revision validation and atomic CAS**

`validate_revision(old, proposed, phases)` enforces the eight rules in “Revision and Commit Semantics.” `TransactionStore.create_revision()` performs `UPDATE action_transactions SET current_revision=?, status='draft' WHERE transaction_id=? AND current_revision=?` in the same `_execute_write` transaction as revision/node/edge inserts, superseded attempt transitions, and `revision_created` event. A rowcount of zero raises `RevisionConflict` without partial writes.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/effects/test_graph.py tests/agent/effects/test_store.py -q`

Expected: PASS, including deterministic ordering across input dictionary order.

- [ ] **Step 6: Commit**

```bash
git add agent/effects tests/agent/effects/test_graph.py tests/agent/effects/test_store.py
git commit -m "feat: add revisable transaction dags"
```

---

### Task 4: Bind Preview to Current Authority and Exact Approval

**Files:**
- Create: `agent/effects/authority.py`
- Modify: `agent/effects/models.py`
- Modify: `agent/effects/store.py`
- Modify: `hermes_state.py`
- Create: `tests/agent/effects/test_authority.py`
- Modify: `tests/tools/test_approval.py`

**Interfaces:**
- Consumes item #6's canonical `AuthorityProvider`, `StoredAuthorityProvider`, `AutonomyContract`, `ActionContext`, `AuthorityDecision`, and `authorize_effect()`, plus `tools.approval.request_tool_approval()` and its immutable identity/hash/expiry semantics.
- Produces `build_action_context()`, `request_bound_approval()`, and `consume_bound_approval()`; it persists only transaction-specific approval bindings in `state.db` so restart never broadens consent.

- [ ] **Step 1: Write RED tests for expiry, drift, and approval replay**

```python
def test_authority_is_reloaded_immediately_before_commit(store, clock):
    provider = StoredAuthorityProvider.open_current(clock=clock)
    context = build_action_context(prepared_effect(), operation_key="tx-1:config")
    decision = authorize_effect(provider, context, stage="preview", consume=False)
    assert decision.allowed
    autonomy_service().apply_rule_change(deny_rule("config.set"), expected_version=1)
    context = build_action_context(prepared_effect(), operation_key="tx-1:config")
    decision = authorize_effect(provider, context, stage="commit", consume=True)
    assert not decision.allowed
    assert decision.code == "authority_changed"


def test_irreversible_approval_is_exact_expiring_and_single_use(store, clock):
    binding = approved_binding(
        transaction_id="tx-1", revision=2, node_id="send", args_hash="a",
        preview_hash="p", resources=("telegram:123",), authority_version=4,
        requester="user-7", channel="tui", expires_at_ms=clock.now_ms() + 30_000,
    )
    store.insert_approval(binding)
    assert consume_bound_approval(store, binding.identity(), clock=clock).approved
    assert consume_bound_approval(store, binding.identity(), clock=clock).code == "consumed"
    assert consume_bound_approval(store, replace(binding, args_hash="changed").identity(), clock=clock).code == "mismatch"
```

Also cover denied irreversible policy, destination/path/action allowlists, symlink-resolved resource matching, expired authority, authority-version CAS, changed preview, requester/channel mismatch, no-human context fail-closed, and compensation authority distinct from commit authority.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/effects/test_authority.py tests/tools/test_approval.py -q`

Expected: FAIL importing `agent.effects.authority`.

- [ ] **Step 3: Integrate the canonical autonomy contract without duplicating it**

```python
from agent.autonomy import (
    ActionContext,
    AuthorityDecision,
    AuthorityProvider,
    StoredAuthorityProvider,
    authorize_effect,
)


def build_action_context(prepared: PreparedEffect, *, operation_key: str) -> ActionContext:
    return ActionContext(
        operation_key=operation_key,
        stage="preview",
        action_class=prepared.action,
        resources=prepared.resources,
        recipient_classes=prepared.recipient_classes,
        recipient_hashes=prepared.recipient_hashes,
        data_classes=prepared.data_classes,
        cost_usd_micros=prepared.cost_usd_micros,
        reversibility=prepared.semantics.fidelity,
        uncertainty_ppm=prepared.uncertainty_ppm,
        required_evidence=prepared.required_evidence,
    )
```

Item #6 owns rule parsing, expiry/consumption, conflict resolution, budgets, and allow/ask/deny. This adapter supplies only trusted adapter-normalized facts: stable operation key, resolved path resources, recipient classes and hashed recipient identities, declared data classes, integer USD micros, reversibility, uncertainty ppm, and evidence. `build_action_context()` initializes the immutable record at `stage="preview"`; only item #6's `authorize_effect(..., stage=...)` may replace the decision stage. Unknown or missing high-risk facts fail closed in the canonical evaluator. At transaction creation persist the autonomy contract version/hash used for preview; immediately before commit or compensation reload `StoredAuthorityProvider.open_current()` and call `authorize_effect(..., stage="commit" | "compensate", consume=True)`.

- [ ] **Step 4: Persist exact approval bindings**

```sql
CREATE TABLE IF NOT EXISTS transaction_approvals (
    approval_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES action_transactions(transaction_id),
    revision INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    preview_hash TEXT NOT NULL,
    resources_json TEXT NOT NULL,
    authority_version INTEGER NOT NULL,
    requester TEXT NOT NULL,
    channel TEXT NOT NULL,
    decision TEXT NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    consumed_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (transaction_id, revision, node_id, args_hash, preview_hash,
            authority_version, requester, channel)
);
```

`request_bound_approval()` calls `request_tool_approval()` with arguments containing the exact identity above and `rule_key="transaction:<adapter>:<action>"`. Only an `approved=True` result creates an approved binding. Never translate session/permanent allowlisting into a transaction approval; an irreversible effect always needs its exact binding.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/effects/test_authority.py tests/tools/test_approval.py tests/tools/test_approval_fallback_identity.py -q`

Expected: PASS; stale or replayed approval never authorizes a commit.

- [ ] **Step 6: Commit**

```bash
git add agent/effects hermes_state.py tests/agent/effects/test_authority.py tests/tools/test_approval.py
git commit -m "feat: bind transactions to current authority"
```

---

### Task 5: Coordinate Preview, Commit, Journal Certainty, and Recovery

**Files:**
- Create: `agent/effects/coordinator.py`
- Create: `agent/effects/context.py`
- Create: `agent/effects/recovery.py`
- Modify: `hermes_cli/middleware.py`
- Modify: `agent/operation_journal.py`
- Modify: `agent/tool_executor.py`
- Modify: `agent/agent_runtime_helpers.py`
- Modify: `model_tools.py`
- Modify: `gateway/run.py`
- Modify: `cli.py`
- Modify: `tui_gateway/server.py`
- Modify: `agent/effects/store.py`
- Create: `tests/agent/effects/test_coordinator.py`
- Create: `tests/agent/effects/test_recovery.py`
- Modify: `tests/agent/test_operation_journal.py`
- Modify: `tests/hermes_cli/test_plugins.py`

**Interfaces:**
- Produces `TransactionCoordinator.preview()`, `.commit()`, `.revise()`, `.reconcile()`, `.compensate()`, `recover_transactions()`, and `TransactionExecutionContext`.
- Consumes final post-plugin tool arguments, adapter registry, store, authority provider, `OperationJournal`, and a dependency-injected fault hook.

- [ ] **Step 1: Write RED state-machine and middleware tests**

```python
def test_commit_orders_durable_intent_before_handler_and_result_before_success(harness):
    harness.preview("tx-1")
    result = harness.coordinator.commit("tx-1")
    assert result.status == "committed"
    assert harness.trace == [
        "authority_rechecked", "journal_running", "effect_committing",
        "handler_called", "raw_result_persisted", "verified",
        "journal_confirmed", "receipt_persisted",
    ]


@pytest.mark.parametrize("fault", [
    "after_prepare", "after_preview", "after_commit_intent",
    "after_handler_return", "after_delivery_dispatch",
])
def test_restart_never_blind_retries_ambiguous_effect(harness, fault):
    harness.crash_at(fault)
    harness.restart()
    recovered = harness.coordinator.reconcile("tx-1")
    assert harness.adapter.commit_calls <= 1
    if fault in {"after_handler_return", "after_delivery_dispatch"}:
        assert recovered.status in {"committed", "unknown_effect"}
```

Middleware tests prove: no transaction context preserves current behavior and writes no row; read-only passes through; unsupported transactional mutation fails before handler; plugin short-circuit creates no effect; plugin-modified arguments are the normalized/persisted identity; operation key is calculated from effective arguments only at the terminal-handler boundary; middleware `next_call()` remains single-use.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/effects/test_coordinator.py tests/agent/effects/test_recovery.py tests/agent/test_operation_journal.py tests/hermes_cli/test_plugins.py -q`

Expected: FAIL importing coordinator/recovery.

- [ ] **Step 3: Implement preview and commit algorithms**

`preview(transaction_id)` CASes `draft|blocked -> previewing`, normalizes/prepares/previews every active node in topological order, persists each attempt, hashes the ordered previews, then CASes to `ready`. Failure stores `preview_failed` and returns `blocked`; it never invokes commit.

`commit(transaction_id, through_node=None)` requires `ready`, reloads the current revision before each node, verifies preview hash and parent phases, reloads authority, consumes exact approval when required, and uses this ordering:

```text
journal.create(pending)
journal.transition(pending -> running/none)
effect CAS previewed -> committing
adapter.commit exactly once
persist raw outcome
adapter.verify and persist evidence
journal transition running|dispatched -> confirmed/(landed|none)
effect CAS committing -> verified|committed
append event
```

If the handler returns but any later durable write fails, attempt adapter reconciliation once. Classify `landed` as committed with recovered evidence, `not_landed` as failed/none, and everything else as `unknown_effect`. Do not return handler success when journal confirmation fails.

Add `TransactionExecutionContext` in `agent/effects/context.py` using a `ContextVar` for in-process calls. `transaction_context(transaction_id, revision, node_id)` is a context manager that restores the prior value in `finally`. `transaction_context_from_runtime()` first reads the ContextVar, then accepts internal subprocess correlation only when `HERMES_TRANSACTION_ID`, `HERMES_TRANSACTION_REVISION`, and `HERMES_TRANSACTION_NODE_ID` are all set by a trusted workflow/worker launcher; user plan text, tool arguments, and config values can never supply them. A context without an exact planned `node_id` fails closed rather than appending a hidden graph node.

- [ ] **Step 4: Add exact journal helpers without weakening transitions**

Add `get_by_identity(kind, destination, payload_hash)`, `list_inflight(kind=None)`, and `transition_if_current()` returning `None` on CAS miss. Do not add an `unknown -> running` transition. Existing `reconcile_after_restart(owner_fenced=True)` remains the first owner-fence pass.

- [ ] **Step 5: Wrap only the terminal tool handler**

Refactor `run_tool_execution_middleware()` so plugin callbacks retain their order and short-circuit behavior, but the terminal call is:

```python
def terminal(payload):
    effective = payload if isinstance(payload, dict) else args
    context = transaction_context_from_runtime(context_kwargs=context_kwargs)
    if context is None:
        return next_call(effective)
    return context.coordinator.commit_tool_effect(
        tool_name=tool_name,
        effective_args=effective,
        operation_key=operation_key_factory(effective),
        invoke=next_call,
        execution=context,
    )
```

Change operation-key factories in `agent/tool_executor.py`, `agent/agent_runtime_helpers.py`, and `model_tools.py` to accept the final effective argument dict. Preserve non-transaction results byte-for-byte.

- [ ] **Step 6: Implement bounded recovery**

`recover_transactions(store, journal, adapters, limit=100)` runs after owner-fenced journal reconciliation in existing CLI, TUI, and gateway startup seams. It processes oldest in-flight effects, invokes only adapter `reconcile`, projects unknown review state once, resumes safe `not_landed` nodes only after an explicit later `commit`, finalizes recovered `landed` nodes, and returns counts `{landed, not_landed, unknown, skipped}`. Recovery never edits a transaction already terminal.

- [ ] **Step 7: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/effects/test_coordinator.py tests/agent/effects/test_recovery.py tests/agent/test_operation_journal.py tests/hermes_cli/test_plugins.py tests/test_get_tool_definitions_cache_isolation.py -q`

Expected: PASS; ambiguous faults produce at most one adapter commit and no success before durable evidence.

- [ ] **Step 8: Commit**

```bash
git add agent/effects agent/operation_journal.py hermes_cli/middleware.py \
  agent/tool_executor.py agent/agent_runtime_helpers.py model_tools.py \
  gateway/run.py cli.py tui_gateway/server.py \
  tests/agent/effects tests/agent/test_operation_journal.py tests/hermes_cli/test_plugins.py
git commit -m "feat: coordinate recoverable effect commits"
```

---

### Task 6: Implement Exact Workspace and Disposable-Worktree Git Adapters

**Files:**
- Create: `agent/effects/adapters/__init__.py`
- Create: `agent/effects/adapters/workspace.py`
- Modify: `tools/checkpoint_manager.py`
- Modify: `tools/file_tools.py`
- Create: `tests/agent/effects/adapters/test_workspace.py`
- Modify: `tests/tools/test_checkpoint_manager.py`
- Modify: `tests/tools/test_file_tools.py`
- Modify: `tests/integration/test_checkpoint_resumption.py`

**Interfaces:**
- Produces adapters `workspace.v1` actions `write_file|patch`, and `workspace-git.v1` action `commit_local`.
- Consumes the exact path resolution used by `tools.file_tools`, per-path `file_state` locks, forced `CheckpointRef`, and Git subprocess argument arrays.

- [ ] **Step 1: Write RED real-filesystem/Git tests**

```python
def test_workspace_preview_commit_and_exact_compensation(tmp_worktree, coordinator):
    tx = workspace_transaction(tmp_worktree, path="README.md", content="new\n")
    preview = coordinator.preview(tx.transaction_id)
    assert preview.nodes[0].before["sha256"] == sha256_bytes(b"old\n")
    assert "-old" in preview.nodes[0].summary and "+new" in preview.nodes[0].summary
    coordinator.commit(tx.transaction_id)
    assert (tmp_worktree / "README.md").read_text() == "new\n"
    result = coordinator.compensate(tx.transaction_id, "write")
    assert result.fidelity == "exact"
    assert (tmp_worktree / "README.md").read_text() == "old\n"


def test_workspace_refuses_escape_primary_branch_drift_and_push(tmp_repo, coordinator):
    for tx, message in [
        (outside_root_transaction(tmp_repo), "outside allowed workspace"),
        (symlink_escape_transaction(tmp_repo), "resolved path escapes"),
        (main_checkout_commit_transaction(tmp_repo), "disposable non-main worktree"),
        (push_transaction(tmp_repo), "unsupported action"),
    ]:
        with pytest.raises(EffectBlocked, match=message):
            coordinator.preview(tx.transaction_id)
    committed = commit_then_human_edit(tmp_repo, coordinator)
    assert coordinator.eligibility(committed.id, "write").code == "blocked_drift"
```

Also cover create/delete/mode restoration, V4A update/add/delete/move headers, line endings/BOM, multi-file path ordering, reconciliation by before/after hashes after handler return, checkpoint root mismatch, detached branch, `main`/`master`, HEAD drift, index restoration, and no remote invocation.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/effects/adapters/test_workspace.py tests/tools/test_checkpoint_manager.py tests/tools/test_file_tools.py -q`

Expected: FAIL because forced checkpoint refs and workspace adapters do not exist.

- [ ] **Step 3: Add immutable forced checkpoint APIs**

```python
@dataclass(frozen=True)
class CheckpointRef:
    checkpoint_id: str
    working_dir: str
    commit_hash: str
    created_at_ms: int


def create_checkpoint(self, working_dir: str, *, reason: str, force: bool = False) -> CheckpointRef:
    abs_dir = self._validated_checkpoint_root(working_dir)
    with self._directory_checkpoint_lock(abs_dir):
        if not force and abs_dir in self._checkpointed_dirs:
            raise CheckpointAlreadyCreated(abs_dir)
        if not self._take(abs_dir, reason):
            raise CheckpointCreationError(abs_dir)
        commit_hash = self._project_ref_tip(abs_dir)
        return CheckpointRef(
            checkpoint_id=f"checkpoint:{_project_hash(abs_dir)}:{commit_hash}",
            working_dir=abs_dir,
            commit_hash=commit_hash,
            created_at_ms=int(time.time() * 1000),
        )


def restore_checkpoint(self, ref: CheckpointRef, *, expected_current: Mapping[str, str]) -> dict:
    abs_dir = self._validated_checkpoint_root(ref.working_dir)
    current = self._hash_paths(abs_dir, expected_current)
    if current != dict(expected_current):
        raise CheckpointDriftError(abs_dir)
    result = self.restore(abs_dir, ref.commit_hash)
    if not result.get("success"):
        raise CheckpointRestoreError(str(result.get("error") or "restore failed"))
    return result
```

Add private helpers with these exact contracts in the same class: `_validated_checkpoint_root(str) -> str` rejects disabled/Git-missing/root/home targets; `_directory_checkpoint_lock(str)` returns a per-normalized-root context manager; `_project_ref_tip(str) -> str` resolves and validates the shadow ref commit; and `_hash_paths(str, Mapping[str, str]) -> dict[str, str]` hashes only the declared relative paths. Add `CheckpointAlreadyCreated`, `CheckpointCreationError`, `CheckpointDriftError`, and `CheckpointRestoreError`; `ensure_checkpoint()` catches them and preserves its current boolean/non-raising contract.

Keep `ensure_checkpoint()` as the non-raising one-per-turn compatibility wrapper. Transaction prepare uses `force=True`, so every distinct effect gets a distinct durable before-state even in one turn.

- [ ] **Step 4: Implement workspace normalize/prepare/preview/commit/reconcile/compensate**

Reuse `_resolve_path_for_task()` and the V4A path extraction rules from `patch_tool`; extract a shared pure `resolve_mutation_paths(args, task_id)` rather than duplicating regex/path behavior. Prepare records path, existence, bytes hash, mode, BOM/line-ending metadata, Git status, checkpoint ref, and expected after hashes. Commit invokes the existing handler callback. Verify rereads every path. Reconcile compares observed hashes with before and expected-after sets. Exact compensation requires unchanged verified-after hashes and restores the checkpoint under the same sorted path locks.

Register existing tools with internal metadata only:

```python
registry.register(
    name="write_file", toolset="file", schema=WRITE_FILE_SCHEMA,
    handler=_handle_write_file, check_fn=_check_file_reqs,
    effect_adapter="workspace.v1", effect_action="write_file",
)
registry.register(
    name="patch", toolset="file", schema=PATCH_SCHEMA,
    handler=_handle_patch, check_fn=_check_file_reqs,
    effect_adapter="workspace.v1", effect_action="patch",
)
```

- [ ] **Step 5: Implement bounded local Git commit**

The non-model `workspace-git.v1/commit_local` adapter accepts only `worktree`, `paths`, and `message`. Verify `git rev-parse --show-toplevel`, `.git` worktree linkage, branch not detached/main/master, and authority root. Preview `git diff -- <paths>`. Commit with `git -C root add -- paths...` then `git -C root commit -m message`; record parent HEAD, created commit, and index tree. Exact compensation is allowed only while HEAD equals the created commit and affected paths match, using `git reset --mixed <parent>` followed by checkpoint restoration. No caller-supplied Git flags, remotes, or push.

- [ ] **Step 6: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/effects/adapters/test_workspace.py tests/tools/test_checkpoint_manager.py tests/tools/test_file_tools.py tests/integration/test_checkpoint_resumption.py -q`

Expected: PASS; ordinary file tools and per-turn checkpoint behavior are unchanged outside a transaction.

- [ ] **Step 7: Commit**

```bash
git add agent/effects/adapters tools/checkpoint_manager.py tools/file_tools.py \
  tests/agent/effects/adapters/test_workspace.py tests/tools/test_checkpoint_manager.py \
  tests/tools/test_file_tools.py tests/integration/test_checkpoint_resumption.py
git commit -m "feat: add reversible workspace effects"
```

---

### Task 7: Implement Versioned Workflow, Cron, and Config Adapters

**Files:**
- Create: `agent/effects/adapters/hermes_state.py`
- Modify: `hermes_cli/workflows_db.py`
- Modify: `cron/jobs.py`
- Modify: `hermes_cli/config.py`
- Create: `tests/agent/effects/adapters/test_hermes_state.py`
- Modify: `tests/hermes_cli/test_workflows_db.py`
- Modify: `tests/cron/test_jobs.py`
- Modify: `tests/cron/test_jobs_crossprocess_lock.py`
- Modify: `tests/hermes_cli/test_config.py`
- Modify: `tests/hermes_cli/test_managed_scope_config.py`

**Interfaces:**
- Produces `hermes-workflow.v1`, `hermes-cron.v1`, and `hermes-config.v1` adapters plus owner-module `prepare_*_mutation`, `apply_*_mutation`, and `restore_*_mutation` services.
- Existing public CLI/model-tool behavior remains wrapped around the same services.

- [ ] **Step 1: Write RED real-store contract tests**

```python
@pytest.mark.parametrize("family", ["workflow", "cron", "config"])
def test_hermes_state_adapter_detects_revision_drift_and_restores(family, state_harness):
    tx = state_harness.transaction(family)
    preview = state_harness.coordinator.preview(tx.id)
    assert preview.nodes[0].before != preview.nodes[0].after
    state_harness.coordinator.commit(tx.id)
    assert state_harness.durable_value(family) == state_harness.expected_after(family)
    state_harness.coordinator.compensate(tx.id, tx.node_id)
    assert state_harness.durable_value(family) == state_harness.expected_before(family)


def test_config_adapter_rejects_secrets_managed_keys_and_absent_null_confusion(config_harness):
    assert config_harness.preview_set("feature.key", "null").before == {"present": False}
    for key in ("OPENAI_API_KEY", "model.api_key", "managed.locked"):
        with pytest.raises(EffectBlocked):
            config_harness.preview_set(key, "secret")
```

Cover workflow deploy/version selection/enable/disable, cron create/update/disable (never hard-delete in a transaction), exact prior cron fields, config alias normalization/coercion/env bridge, unreadable config, managed installation/scope, concurrent revision changes, and compensation idempotency.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/effects/adapters/test_hermes_state.py tests/hermes_cli/test_workflows_db.py tests/cron/test_jobs.py tests/hermes_cli/test_config.py -q`

Expected: FAIL importing Hermes-state adapters/services.

- [ ] **Step 3: Extract typed non-printing owner services**

```python
@dataclass(frozen=True)
class StateMutation:
    resource: str
    action: str
    expected_revision: str
    before: Mapping[str, Any]
    after: Mapping[str, Any]


def prepare_config_mutation(key: str, raw_value: str) -> StateMutation:
    canonical_key, value = validate_transaction_config_value(key, raw_value)
    config_path = get_config_path()
    require_readable_config_before_write(config_path)
    raw = read_raw_config()
    present, old_value = get_nested_presence(raw, canonical_key)
    after = copy.deepcopy(raw)
    _set_nested(after, canonical_key, value)
    return StateMutation(
        resource=f"config:{canonical_key}",
        action="set",
        expected_revision=content_hash(raw),
        before={"document": raw, "present": present, "value": old_value},
        after={"document": after, "present": True, "value": value},
    )


def apply_config_mutation(change: StateMutation) -> Mapping[str, Any]:
    with _CONFIG_LOCK:
        current = read_raw_config()
        if content_hash(current) != change.expected_revision:
            raise StateRevisionConflict(change.resource)
        document = copy.deepcopy(change.after["document"])
        atomic_config_write(get_config_path(), document, sort_keys=False)
        refresh_config_and_terminal_env(change.resource, change.after["value"])
        return {"revision": content_hash(document), "document": document}


def restore_config_mutation(change: StateMutation, *, expected_after_revision: str) -> Mapping[str, Any]:
    with _CONFIG_LOCK:
        current = read_raw_config()
        if content_hash(current) != expected_after_revision:
            raise StateRevisionConflict(change.resource)
        document = copy.deepcopy(change.before["document"])
        atomic_config_write(get_config_path(), document, sort_keys=False)
        refresh_config_and_terminal_env(
            change.resource,
            change.before["value"] if change.before["present"] else None,
        )
        return {"revision": content_hash(document), "document": document}
```

Implement equivalent workflow services inside `workflows_db.py` under `write_txn(conn)` and cron services inside one `_jobs_lock()` load/compare/save critical section. Revisions are SHA-256 of canonical resource records; do not use mtimes alone.

- [ ] **Step 4: Implement and register the three adapters**

Workflow compensation selects the prior immutable definition version and enabled state; it never edits a published spec. Cron compensation restores the exact prior normalized job or disables a newly created one. Config compensation restores the raw user leaf while preserving unrelated concurrent keys; a whole-document revision mismatch blocks instead of overwriting.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/effects/adapters/test_hermes_state.py tests/hermes_cli/test_workflows_db.py tests/hermes_cli/test_workflows_db_versions.py tests/cron/test_jobs.py tests/cron/test_jobs_crossprocess_lock.py tests/hermes_cli/test_config.py tests/hermes_cli/test_managed_scope_config.py tests/hermes_cli/test_managed_scope_cli_config.py -q`

Expected: PASS; legacy workflow/cron/config callers retain current output and locking semantics.

- [ ] **Step 6: Commit**

```bash
git add agent/effects/adapters/hermes_state.py hermes_cli/workflows_db.py cron/jobs.py \
  hermes_cli/config.py tests/agent/effects/adapters/test_hermes_state.py \
  tests/hermes_cli/test_workflows_db.py tests/cron/test_jobs.py \
  tests/cron/test_jobs_crossprocess_lock.py tests/hermes_cli/test_config.py \
  tests/hermes_cli/test_managed_scope_config.py
git commit -m "feat: transact versioned hermes state"
```

---

### Task 8: Implement the Delayed Outbox and Explicit Irreversible Boundary

**Files:**
- Create: `agent/effects/adapters/message_outbox.py`
- Create: `gateway/transaction_outbox.py`
- Modify: `hermes_state.py`
- Modify: `gateway/delivery.py`
- Modify: `gateway/platforms/base.py`
- Modify: `gateway/run.py`
- Modify: `hermes_cli/workflows_spec.py`
- Modify: `hermes_cli/workflows_capabilities.py`
- Modify: `hermes_cli/workflows_dispatcher.py`
- Create: `tests/agent/effects/adapters/test_message_outbox.py`
- Create: `tests/gateway/test_transaction_outbox.py`
- Modify: `tests/gateway/test_delivery_operation_journal.py`
- Modify: `tests/hermes_cli/test_workflows_spec.py`
- Modify: `tests/hermes_cli/test_workflows_dispatcher.py`

**Interfaces:**
- Produces `message-outbox.v1/send`, durable revision/cancel/release, leased dispatch, stable delivery ids, and optional platform-proven semantic edit/delete compensation.
- Consumes `DeliveryRouter`, `DeliveryTarget`, `OperationJournal`, and existing adapter `send`/`edit_message`/`delete_message` methods.

- [ ] **Step 1: Write RED outbox lifecycle and crash tests**

```python
def test_outbox_can_revise_before_release_but_not_after_dispatch(outbox_harness):
    row = outbox_harness.prepare(message="first", not_before_ms=20_000)
    revised = outbox_harness.revise(row.outbox_id, expected_revision=1, message="final")
    assert revised.revision == 2
    assert revised.content_hash != row.content_hash
    outbox_harness.release(revised.outbox_id, exact_approval(revised))
    outbox_harness.dispatch_once()
    with pytest.raises(OutboxConflict, match="already dispatched"):
        outbox_harness.revise(row.outbox_id, expected_revision=2, message="late")


@pytest.mark.parametrize("fault", [
    "before_claim", "after_claim", "after_delivery_dispatch", "after_confirmation",
])
def test_outbox_restart_never_blind_resends(outbox_harness, fault):
    outbox_harness.crash_at(fault)
    outbox_harness.restart_and_drain()
    assert outbox_harness.network_send_count <= 1
    assert outbox_harness.status in {"confirmed", "unknown_effect"}
```

Also prove cancellation before release, lease expiry, exact approval revision/content/destination binding, unknown adapter acknowledgement, provider failure before dispatch, stable idempotency metadata, workflow waiting/resume, and edit/delete capability detection from concrete overrides rather than optimistic platform names.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/effects/adapters/test_message_outbox.py tests/gateway/test_transaction_outbox.py tests/gateway/test_delivery_operation_journal.py tests/hermes_cli/test_workflows_dispatcher.py -q`

Expected: FAIL because transaction outbox storage/dispatcher do not exist and `send_message` is unsupported.

- [ ] **Step 3: Add durable outbox storage**

```sql
CREATE TABLE IF NOT EXISTS transaction_outbox (
    outbox_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES action_transactions(transaction_id),
    effect_id TEXT NOT NULL UNIQUE REFERENCES effect_transactions(effect_id),
    delivery_id TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    target TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    not_before_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_ms INTEGER,
    approval_id TEXT,
    result_json TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transaction_outbox_due
    ON transaction_outbox(status, not_before_ms, created_at_ms);
```

CAS claims order by `(not_before_ms, created_at_ms, outbox_id)`. Revision updates require `status IN ('prepared','previewed')` and exact expected revision. Release requires current authority plus unconsumed approval and sets `ready`; cancellation is terminal before dispatch.

- [ ] **Step 4: Make delivery certainty truthful**

Add `supports_message_edit` and `supports_message_delete` properties computed from whether the concrete class overrides the base methods. In `DeliveryRouter`, a failure to persist `dispatched` before the adapter call must abort before send; a failure to persist confirmed/landed after a successful send must raise `DeliveryEffectUnknown` carrying the bounded acknowledgement. Never log-and-return-success on those journal failures.

- [ ] **Step 5: Implement adapter and dispatcher**

Prepare normalizes `DeliveryTarget`, rendered content, delay, final content hash, and semantics. It is reversible by cancellation until dispatch; at dispatch it becomes irreversible unless the concrete adapter proves edit/delete. `TransactionOutboxDispatcher.drain(limit=20)` claims, rechecks exact authority/approval, calls `DeliveryRouter` with stable `metadata["delivery_id"]`, persists bounded acknowledgement, and settles the effect. Unknown acknowledgement leaves the outbox/effect/transaction unknown and never eligible for automatic claim again.

- [ ] **Step 6: Type and resume workflow `send_message`**

Add `platform`, `target`, `message`, and `not_before_seconds: int = Field(default=30, ge=1)` to `NodeSpec`, validated only for `send_message`. `_persist_waiting_nodes()` creates/loads the durable outbox; `_resume_terminal_outbox_nodes()` marks confirmed nodes succeeded, cancelled/failed nodes failed, and unknown nodes blocked for review. Move `send_message` into `IMPLEMENTED_NODE_TYPES` only after these tests pass.

- [ ] **Step 7: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/effects/adapters/test_message_outbox.py tests/gateway/test_transaction_outbox.py tests/gateway/test_delivery_operation_journal.py tests/gateway/test_delivery.py tests/hermes_cli/test_workflows_spec.py tests/hermes_cli/test_workflows_capabilities.py tests/hermes_cli/test_workflows_dispatcher.py tests/hermes_cli/test_workflows_e2e.py -q`

Expected: PASS; every ambiguous post-dispatch case is unknown, never successful or automatically redelivered.

- [ ] **Step 8: Commit**

```bash
git add agent/effects/adapters/message_outbox.py gateway/transaction_outbox.py \
  hermes_state.py gateway/delivery.py gateway/platforms/base.py gateway/run.py \
  hermes_cli/workflows_spec.py hermes_cli/workflows_capabilities.py \
  hermes_cli/workflows_dispatcher.py tests/agent/effects/adapters/test_message_outbox.py \
  tests/gateway/test_transaction_outbox.py tests/gateway/test_delivery_operation_journal.py \
  tests/hermes_cli/test_workflows_spec.py tests/hermes_cli/test_workflows_dispatcher.py
git commit -m "feat: add transactional message outbox"
```

---

### Task 9: Compute Truthful Eligibility and Dependency-Aware Cascade Compensation

**Files:**
- Create: `agent/effects/eligibility.py`
- Modify: `agent/effects/coordinator.py`
- Modify: `agent/effects/store.py`
- Modify: `hermes_state.py`
- Create: `tests/agent/effects/test_eligibility.py`
- Modify: `tests/agent/effects/test_coordinator.py`
- Modify: `tests/agent/effects/test_graph.py`

**Interfaces:**
- Produces `eligibility_for_effect()`, `eligibility_for_transaction()`, `plan_compensation()`, and journaled `CompensationAttempt` records.
- Consumes the current revision graph, all later revisions containing frozen nodes, live adapter inspection, authority, and latest effect/compensation states.

- [ ] **Step 1: Write RED eligibility matrix and ordering tests**

```python
@pytest.mark.parametrize(("scenario", "code"), [
    ("exact_clean", "eligible_exact"),
    ("semantic_clean", "eligible_compensation"),
    ("live_dependent", "blocked_live_dependents"),
    ("irreversible_descendant", "blocked_irreversible_boundary"),
    ("unknown_descendant", "blocked_unknown"),
    ("resource_drift", "blocked_drift"),
    ("window_expired", "blocked_window_expired"),
    ("authority_revoked", "blocked_authority"),
    ("no_compensate", "unsupported"),
    ("already_done", "already_compensated"),
])
def test_truthful_eligibility_codes(eligibility_harness, scenario, code):
    result = eligibility_harness.evaluate(scenario)
    assert result.code == code
    assert result.can_execute == code in {"eligible_exact", "eligible_compensation"}


def test_cascade_compensates_reverse_topological_order_once(compensation_harness):
    plan = compensation_harness.plan(target="a", cascade=True)
    assert plan.node_ids == ("d", "c", "b", "a")
    compensation_harness.execute(plan)
    assert compensation_harness.calls == ["d", "c", "b", "a"]
    compensation_harness.execute(plan)
    assert compensation_harness.calls == ["d", "c", "b", "a"]
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/effects/test_eligibility.py tests/agent/effects/test_coordinator.py tests/agent/effects/test_graph.py -q`

Expected: FAIL importing `agent.effects.eligibility`.

- [ ] **Step 3: Add compensation-attempt persistence**

```sql
CREATE TABLE IF NOT EXISTS effect_compensations (
    compensation_id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL REFERENCES effect_transactions(effect_id),
    operation_id TEXT NOT NULL UNIQUE REFERENCES agent_operations(operation_id),
    fidelity TEXT NOT NULL,
    status TEXT NOT NULL,
    authority_json TEXT NOT NULL,
    before_json TEXT,
    result_json TEXT,
    verification_json TEXT,
    error TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
```

Stable operation id is `sha256("compensate\0" + effect_id + "\0" + verified_result_hash)`. A second request loads the same terminal attempt and returns its result; it never executes compensation twice.

- [ ] **Step 4: Implement eligibility without optimistic labels**

Evaluate, in order: effect terminal state; adapter declared fidelity; unknown nodes in selected subgraph; uncompensated committed descendants; irreversible descendants/boundaries; compensation window; current authority; adapter live inspection/drift. Return `UndoEligibility` with `can_execute`, exact code, human reason, blockers, fidelity, and required cascade node ids. CLI/TUI render “exact undo” only for `eligible_exact`; render “semantic compensation” for `eligible_compensation`.

- [ ] **Step 5: Implement compensation execution**

Without `--cascade`, any live committed dependent blocks. With `--cascade`, freeze the computed plan hash, re-evaluate every node immediately before its turn, recheck compensation authority, journal running intent, CAS effect to `compensating`, invoke adapter once, verify the counter-effect, persist evidence, confirm the compensation journal, then CAS `compensated`. Stop at the first changed eligibility and report `partially_compensated`; never continue across an unknown or irreversible node.

- [ ] **Step 6: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/effects/test_eligibility.py tests/agent/effects/test_coordinator.py tests/agent/effects/test_graph.py -q`

Expected: PASS with exact reverse dependency order and no false “undoable” classification.

- [ ] **Step 7: Commit**

```bash
git add agent/effects hermes_state.py tests/agent/effects/test_eligibility.py \
  tests/agent/effects/test_coordinator.py tests/agent/effects/test_graph.py
git commit -m "feat: add truthful cascade compensation"
```

---

### Task 10: Integrate Transaction Evidence with Shared Receipts and Recheck Observations

**Files:**
- Create: `agent/effects/receipts.py`
- Modify: `agent/effects/store.py`
- Create: `tests/agent/effects/test_receipts.py`
- Modify: `tests/agent/test_receipts.py`

**Interfaces:**
- Consumes item #12's `ReceiptStatus`, `ReceiptStore.insert()`, `ReceiptStore.append_observation()`, and scorer-only `VerifiedReceiptDecision`, plus graph/revision hashes, authority decisions, operation/effect states, previews, before/after evidence, verification, reconciliation, compensation, and outbox acknowledgements.
- Produces `TransactionReceiptBuilder.issue()` / `.recheck()` and transaction-specific canonical claims without changing the shared receipt schema or status vocabulary.

- [ ] **Step 1: Write RED receipt truth-table tests**

```python
@pytest.mark.parametrize(("evidence", "status"), [
    ("all_verified", "verified"),
    ("committed_missing_verification", "completed_unverified"),
    ("blocked_authority", "blocked"),
    ("known_failure", "failed"),
    ("ambiguous_effect", "unknown_effect"),
    ("all_exactly_compensated", "verified"),
    ("mixed_compensation", "completed_unverified"),
])
def test_receipt_status_follows_persisted_evidence(receipt_harness, evidence, status):
    receipt = receipt_harness.issue(evidence)
    assert receipt.status == status
    assert receipt.content_hash == receipt_harness.rehash(receipt)


def test_recheck_appends_observation_without_mutating_receipt(receipt_harness):
    original = receipt_harness.issue("all_verified")
    receipt_harness.drift_workspace()
    observation = receipt_harness.recheck(original.receipt_id)
    assert observation.status == "evidence_changed"
    assert receipt_harness.load(original.receipt_id) == original
```

For compensated cases, store `terminal_mode="compensated"` and exact/semantic fidelity in claims; never invent compensation-specific receipt statuses. Seed at least 50 false-success combinations across unknown journal, missing verification, stale preview, stale authority, failed outbox, missing artifact, drifted config/cron/workflow/file, and model/user-facing “done” text. None may emit `verified`.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/effects/test_receipts.py -q`

Expected: FAIL importing `agent.effects.receipts.TransactionReceiptBuilder` while the shared `agent.receipts` tests remain importable.

- [ ] **Step 3: Pin the shared receipt contract before adding transaction claims**

```python
from agent.receipts import RECEIPT_STATUSES, ReceiptStore

assert RECEIPT_STATUSES == frozenset({
    "verified", "completed_unverified", "failed", "blocked", "unknown_effect"
})
assert hasattr(ReceiptStore, "insert")
assert hasattr(ReceiptStore, "append_observation")
```

The item #12 plan owns `receipts`, `receipt_observations`, canonical hashing, scorer registration, and immutable insertion. This task adds only transaction claim construction and projects `action_transactions.receipt_id` after shared receipt insertion; restart links an existing receipt by subject/content hash if projection previously failed.

- [ ] **Step 4: Build transaction claims and the only verified scorer**

Claims include transaction/revision/graph/preview hashes, node/edge ids, exact semantics, authority version/expiry, operation ids/states, before/after summaries, verifier evidence, reconciliation disposition, compensation fidelity/order, outbox message id/destination hash, uncertainty, and exclusions. Only the shared private scorer path may choose `verified`; model output, handler success, workflow success, or a journal row alone are insufficient.

- [ ] **Step 5: Implement observation-only recheck**

Recheck calls adapter `inspect`/`reconcile` without committing or compensating. It appends an observation with current evidence and uncertainty; it never changes the original receipt or transaction terminal status.

- [ ] **Step 6: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/effects/test_receipts.py tests/agent/test_receipts.py tests/test_hermes_state.py -q`

Expected: PASS; all 50 false-success cases remain non-verified.

- [ ] **Step 7: Commit**

```bash
git add agent/effects/receipts.py agent/effects/store.py \
  tests/agent/effects/test_receipts.py tests/agent/test_receipts.py
git commit -m "feat: issue immutable transaction receipts"
```

---

### Task 11: Deliver the Top-Level and Classic CLI Transaction UX

**Files:**
- Create: `hermes_cli/transactions.py`
- Create: `skills/action-transactions/SKILL.md`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/main.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Modify: `cli.py`
- Create: `tests/hermes_cli/test_transaction_cli.py`
- Modify: `tests/hermes_cli/test_commands.py`
- Modify: `tests/gateway/test_gateway_command_help.py`

**Interfaces:**
- Produces `build_parser()`, `transaction_command(args) -> int`, `run_argv(argv, output_mode) -> CommandResult`, and `run_slash(rest) -> str` over one service path.
- Consumes coordinator/store/receipt/outbox APIs; no UI-specific state enters the coordinator.

- [ ] **Step 1: Write RED parser, output, and rejection tests**

```python
@pytest.mark.parametrize("argv", [
    ["create", "--plan", "plan.yaml", "--authority", "authority.yaml"],
    ["list", "--status", "ready"],
    ["show", "tx-1"],
    ["graph", "tx-1", "--revision", "2"],
    ["preview", "tx-1"],
    ["revise", "tx-1", "--plan", "revised.yaml", "--expected-revision", "1", "--reason", "recipient changed"],
    ["commit", "tx-1"],
    ["reconcile", "tx-1"],
    ["eligibility", "tx-1"],
    ["compensate", "tx-1", "write", "--cascade"],
    ["receipt", "tx-1", "--recheck"],
    ["outbox", "list", "tx-1"],
    ["outbox", "revise", "ob-1", "--message", "final", "--expected-revision", "1"],
    ["outbox", "cancel", "ob-1"],
    ["outbox", "release", "ob-1"],
])
def test_transaction_parser_accepts_only_bounded_commands(argv, parser):
    assert parser.parse_args(["transaction", *argv]).transaction_action
```

Reject unknown adapters/actions, cycles, stale expected revision, cross-profile paths, secret config keys, remote Git/push, arbitrary shell, browser/service writes, unapproved irreversible commit, outbox post-dispatch edits, non-cascade live dependents, changed preview, and every unrecognized trailing argument. JSON output must redact content/secrets but preserve hashes and stable ids.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_transaction_cli.py tests/hermes_cli/test_commands.py -q`

Expected: FAIL importing `hermes_cli.transactions`.

- [ ] **Step 3: Implement one shared argv/service surface**

`build_parser(parent_subparsers)` uses required subparsers. `transaction_command()` calls `run_argv()` and renders its result. `run_slash()` uses `shlex.split(rest, posix=os.name != "nt")`, calls the same `run_argv()`, and returns bounded text. YAML/JSON files are capped at 1 MiB and loaded only from explicit paths. `create` prints transaction id, revision, classifications, expiry, and the exact `preview` command. `preview` renders an ordered table with resource, before/after, fidelity, uncertainty, and approval boundary. `eligibility` never abbreviates blocker reasons.

- [ ] **Step 4: Register terminal surfaces**

```python
CommandDef(
    "transaction",
    "Preview, revise, commit, reconcile, and compensate bounded actions",
    "Tools & Skills",
    aliases=("tx",),
    args_hint="[subcommand]",
    cli_only=True,
    subcommands=(
        "create", "list", "show", "graph", "preview", "revise", "commit",
        "reconcile", "eligibility", "compensate", "receipt", "outbox",
    ),
)
```

Add `cmd_transaction` next to `cmd_workflow`, parser setup next to the workflow parser, `_handle_transaction_command()` next to `_handle_workflow_command()`, and the canonical dispatch branch in `cli.py`. Do not add gateway command parity, Desktop parity, or a new model tool.

- [ ] **Step 5: Add the complete action-transactions skill**

The skill must direct the agent to create/preview before commit, show exact classifications, re-preview after revisions, use `eligibility` before compensation, stop on unknown, reconcile before further dependent work, and call success only from a verified receipt. It explicitly forbids remote push, arbitrary shell wrapping, production DB/browser/account/purchase effects, cross-profile work, and claiming semantic compensation is exact undo. It contains complete copyable plan/authority YAML for the three first adapters and no pagination escape hatch.

- [ ] **Step 6: Run GREEN and smoke tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_transaction_cli.py tests/hermes_cli/test_commands.py tests/hermes_cli/test_workflow_cli.py tests/gateway/test_gateway_command_help.py -q`

Expected: PASS; `/transaction` is in CLI/TUI catalogs and absent from gateway help because it is `cli_only`.

Run: `uv run hermes transaction --help`

Expected: exit 0 and list all bounded subcommands without starting chat, Dashboard, or Desktop.

- [ ] **Step 7: Commit**

```bash
git add hermes_cli/transactions.py skills/action-transactions/SKILL.md \
  hermes_cli/commands.py hermes_cli/main.py hermes_cli/cli_commands_mixin.py cli.py \
  tests/hermes_cli/test_transaction_cli.py tests/hermes_cli/test_commands.py \
  tests/gateway/test_gateway_command_help.py
git commit -m "feat: add action transaction cli"
```

---

### Task 12: Route Mutating Transaction Commands Natively in the Ink TUI

**Files:**
- Modify: `tui_gateway/server.py`
- Modify: `ui-tui/src/gatewayTypes.ts`
- Modify: `ui-tui/src/app/slash/commands/ops.ts`
- Create: `tests/tui_gateway/test_transaction_rpc.py`
- Create: `ui-tui/src/__tests__/transactionCommand.test.ts`
- Modify: `ui-tui/src/__tests__/createSlashHandler.test.ts`
- Modify: `ui-tui/src/__tests__/slashParity.test.ts`

**Interfaces:**
- Produces `transaction.exec` JSON-RPC and native `/transaction` TUI rendering.
- Consumes `hermes_cli.transactions.run_argv()` in the live TUI gateway process so mutations never run in `_SlashWorker`.

- [ ] **Step 1: Write RED backend and frontend routing tests**

```python
def test_transaction_rpc_uses_live_profile_and_structured_result(rpc_client, temp_home):
    result = rpc_client.call("transaction.exec", {
        "session_id": "sid-1",
        "argv": ["show", "tx-1"],
    })
    assert result["ok"] is True
    assert result["action"] == "show"
    assert result["transaction"]["transaction_id"] == "tx-1"
```

```typescript
it('routes /transaction through native transaction.exec, never slash.exec', () => {
  findSlashCommand('transaction')!.run('preview tx-1', ctx, '/transaction preview tx-1')
  expect(ctx.gateway.rpc).toHaveBeenCalledWith('transaction.exec', {
    argv: ['preview', 'tx-1'],
    session_id: 'sid-1'
  })
  expect(ctx.gateway.gw.request).not.toHaveBeenCalledWith('slash.exec', expect.anything())
})
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/tui_gateway/test_transaction_rpc.py -q`

Expected: FAIL with unknown RPC method.

Run: `cd ui-tui && npm test -- --run src/__tests__/transactionCommand.test.ts src/__tests__/slashParity.test.ts`

Expected: FAIL because no native transaction command exists.

- [ ] **Step 3: Implement live-process `transaction.exec`**

Validate `argv` as a list of at most 64 UTF-8 strings and 64 KiB total, resolve the active profile/session, call `run_argv(argv, output_mode="structured")`, and return `{ok, action, output, transaction, nodes, preview, eligibility, receipt, approval_pending}`. Errors use JSON-RPC 4xxx for validation/conflict and 5xxx for storage failures; no traceback or secret content leaves the backend.

When an irreversible release/commit needs approval, the existing `request_tool_approval` gateway callback emits `approval.request`. The transaction command remains pending/retryable with its exact request id; `approval.respond` resolves it and a repeated native command consumes the exact binding. Do not create a second TUI modal protocol.

- [ ] **Step 4: Implement native Ink rendering**

Add `transaction` with alias `tx` to `opsCommands`. Parse arguments with the existing slash parser utility into argv; do not spawn a shell. Render list/show/graph/preview/eligibility/receipt as `panel`/`page`, short commit/revise/reconcile/outbox results as `sys`, and unknown effects with a persistent warning that names the reconcile command. Keep the command flight/stale-session guard.

- [ ] **Step 5: Enforce the mutating-command parity invariant**

Add `transaction` to both `NATIVE_MUTATING_COMMANDS` and `MUTATING_COMMANDS` in `slashParity.test.ts`. Add a regression that a catalog-discovered `/transaction` cannot fall through to `slash.exec`, even when the local registry is temporarily missing.

- [ ] **Step 6: Run GREEN and typecheck**

Run: `scripts/run_tests.sh tests/tui_gateway/test_transaction_rpc.py -q`

Expected: PASS.

Run: `cd ui-tui && npm test -- --run src/__tests__/transactionCommand.test.ts src/__tests__/createSlashHandler.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS; `/transaction` uses native RPC and TypeScript has no errors.

- [ ] **Step 7: Commit**

```bash
git add tui_gateway/server.py ui-tui/src/gatewayTypes.ts \
  ui-tui/src/app/slash/commands/ops.ts \
  tests/tui_gateway/test_transaction_rpc.py ui-tui/src/__tests__/transactionCommand.test.ts \
  ui-tui/src/__tests__/createSlashHandler.test.ts ui-tui/src/__tests__/slashParity.test.ts
git commit -m "feat: add native tui transaction controls"
```

---

### Task 13: Prove Recovery, Revision, Safety, and Overhead Across 100 Cases

**Files:**
- Create: `benchmarks/transactions/runner.py`
- Modify: `benchmarks/transactions/cases.py`
- Modify: `tests/benchmarks/test_transaction_benchmark.py`
- Create: `tests/hermes_cli/test_transaction_e2e.py`
- Modify: `tests/agent/effects/test_coordinator.py`
- Modify: `tests/agent/effects/test_recovery.py`
- Modify: `tests/test_get_tool_definitions_cache_isolation.py`
- Modify: `tests/agent/test_system_prompt.py`
- Modify: `tests/agent/test_turn_finalizer_interrupt_alternation.py`

**Interfaces:**
- Produces `run_benchmark(manifest_path, *, repeats, output) -> BenchmarkReport` and local JSON/Markdown reports.
- Consumes only public transaction/coordinator/CLI services plus an injected `FaultHook(point, context)`; production uses a no-op hook.

- [ ] **Step 1: Write the real-path E2E test before the runner**

```python
@pytest.mark.parametrize("fault_point", [
    "after_prepare", "after_preview", "after_commit_intent",
    "after_handler_return", "after_delivery_dispatch",
])
def test_three_family_transaction_recovers_without_duplicate(
    transaction_e2e, fault_point,
):
    transaction_e2e.preview()
    transaction_e2e.commit_with_process_exit(fault_point)
    final = transaction_e2e.reopen_and_reconcile()
    assert final.adapter_commit_counts["workspace.v1"] <= 1
    assert final.adapter_commit_counts["hermes-config.v1"] <= 1
    assert final.network_delivery_count <= 1
    assert final.status in {"committed", "unknown_effect"}
    assert final.receipt.status != "verified" or final.all_evidence_verified


def test_revision_and_cascade_end_to_end(transaction_e2e):
    transaction_e2e.preview()
    transaction_e2e.commit_through("workspace_write")
    transaction_e2e.revise_message(expected_revision=1, message="corrected")
    transaction_e2e.preview()
    transaction_e2e.commit()
    assert transaction_e2e.network_messages == ["corrected"]
    assert transaction_e2e.frozen_node_unchanged("workspace_write", revisions=(1, 2))
    transaction_e2e.compensate("workspace_write", cascade=True)
    assert transaction_e2e.compensation_order == ["config_set", "workspace_write"]
```

Use a temp `HERMES_HOME`, real `state.db`, real config/workflow/cron stores, real file/checkpoint/Git worktree, real CLI parser/service, and a fake final platform adapter. `after_handler_return` and `after_delivery_dispatch` kill a subprocess before the next durable confirmation, then construct new `SessionDB`, journal, store, registry, adapters, and coordinator objects.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_transaction_e2e.py -q`

Expected: FAIL at any missing cross-module wiring; repair the owning production module, never weaken the assertion or classify ambiguity as success.

- [ ] **Step 3: Implement the report-only benchmark runner**

The runner executes the frozen case expansion from Task 0. Each case returns:

```python
@dataclass(frozen=True)
class CaseResult:
    case_id: str
    stratum: str
    passed: bool
    transaction_status: str
    duplicate_effects: int
    unauthorized_irreversible_commits: int
    compensation_order_correct: bool
    every_non_reversible_classified: bool
    false_success_receipt: bool
    baseline_latency_ms: float
    transaction_latency_ms: float
    excluded_reason: str | None
```

`BenchmarkReport` reports denominator/exclusions, pass rate + Wilson 95% interval per stratum, zero-count safety metrics, baseline/candidate p50/p95, eligible-flow median overhead ratio, and environment metadata (OS, Python, SQLite, filesystem class, Git version, network=fake). It writes only to stdout or an explicit output path; it never uploads.

Execute every frozen case as a separately named pytest case as well as through the report runner:

```python
MANIFEST, CASES = load_cases(ROOT / "benchmarks/transactions/manifest.yaml")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_preregistered_transaction_case(case, benchmark_harness):
    result = benchmark_harness.run_case(case)
    assert result.passed, result
    assert result.unauthorized_irreversible_commits == 0
    assert result.duplicate_effects == 0
    assert result.compensation_order_correct
    assert result.every_non_reversible_classified
    assert not result.false_success_receipt
```

- [ ] **Step 4: Implement exact expected outcomes for all strata**

- Revision: committed nodes remain frozen, revised pending args are the only committed args, stale CAS loses cleanly.
- Stale authority: zero handler/network calls; terminal state `blocked`; approval cannot override changed/expired authority.
- Crash: at most one effect; recovered `landed|not_landed|unknown` matches adapter evidence; no blind retry.
- Duplicate delivery: one Hermes-boundary dispatch for a stable id; provider ambiguity is unknown rather than exactly-once.
- Partial failure: descendants do not commit; default policy stops; optional prefix compensation follows eligibility/order.
- Compensation boundary: exact/semantic/none labels match the adapter, cascade order is correct, and no irreversible/unknown boundary is crossed.

- [ ] **Step 5: Run GREEN on E2E and all 100 cases**

Run: `scripts/run_tests.sh tests/hermes_cli/test_transaction_e2e.py tests/benchmarks/test_transaction_benchmark.py -q`

Expected: PASS with 100 collected benchmark cases, zero unauthorized irreversible commits, zero duplicate instrumented effects, zero incorrect compensation orders, zero unclassified non-reversible effects, and zero false-success receipts.

Run: `uv run python benchmarks/transactions/runner.py --manifest benchmarks/transactions/manifest.yaml --repeats 5 --output-json build/transaction-benchmark.json`

Expected: exit 0 only if all safety floors pass and median overhead on eligible flows is at most 0.15; report names every exclusion and never replaces the separate metrics with a composite score.

- [ ] **Step 6: Prove cache, tool-schema, provider/model, and role invariants**

Run a multi-turn transaction-scoped agent fixture and independently hash the system message and effective tool definitions before preview, after revision, after commit, and after reconciliation. Assert provider/model identity and strict role alternation. Run:

`scripts/run_tests.sh tests/test_get_tool_definitions_cache_isolation.py tests/run_agent -q -k 'system_prompt or tool_schema or cache or alternation'`

Expected: PASS; all four snapshots remain identical across transaction state changes.

- [ ] **Step 7: Run the focused regression matrix**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/effects \
  tests/agent/test_operation_journal.py \
  tests/tools/test_registry.py \
  tests/tools/test_checkpoint_manager.py \
  tests/tools/test_file_tools.py \
  tests/hermes_cli/test_transaction_cli.py \
  tests/hermes_cli/test_transaction_e2e.py \
  tests/hermes_cli/test_workflows_db.py \
  tests/hermes_cli/test_workflows_dispatcher.py \
  tests/cron/test_jobs.py \
  tests/gateway/test_transaction_outbox.py \
  tests/gateway/test_delivery_operation_journal.py \
  tests/tui_gateway/test_transaction_rpc.py \
  tests/benchmarks/test_transaction_benchmark.py -q
git diff --check
```

Expected: all pass and diff check is clean.

- [ ] **Step 8: Commit**

```bash
git add benchmarks/transactions tests/benchmarks/test_transaction_benchmark.py \
  tests/hermes_cli/test_transaction_e2e.py tests/agent/effects/test_coordinator.py \
  tests/agent/effects/test_recovery.py tests/test_get_tool_definitions_cache_isolation.py \
  tests/agent/test_system_prompt.py tests/agent/test_turn_finalizer_interrupt_alternation.py
git commit -m "test: prove transaction revision and recovery"
```

---

### Task 14: Add Config-Gated Rollout, Adapter SDK Documentation, and Operator Guide

**Files:**
- Modify: `hermes_cli/config.py`
- Modify: `tests/hermes_cli/test_config.py`
- Create: `website/docs/user-guide/features/action-transactions.md`
- Create: `website/docs/development/effect-adapters.md`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`
- Modify: `website/sidebars.ts`
- Modify: `tests/benchmarks/test_transaction_benchmark.py`

**Interfaces:**
- Produces stable `transactions` config, an explicit staged rollout contract, complete operator docs, and a plugin-author SDK guide.
- No telemetry, Desktop dependency, Dashboard React rewrite, gateway command, or new core model tool.

- [ ] **Step 1: Write RED config and documentation behavior tests**

```python
def test_transaction_defaults_are_safe_and_non_secret():
    cfg = load_config()
    assert cfg["transactions"] == {
        "mode": "preview",
        "auto_reconcile_on_start": True,
        "recovery_batch_size": 100,
        "outbox_max_delay_seconds": 86400,
        "compensation_default": "manual",
    }


def test_invalid_transaction_mode_falls_back_to_preview(tmp_config):
    tmp_config.write_text("transactions:\n  mode: unrestricted\n")
    assert load_config()["transactions"]["mode"] == "preview"
```

Extend the benchmark contract test to assert rollout gates name the 100-case pass, zero safety regressions, and `<15%` overhead before `mode: commit` is recommended.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_config.py tests/benchmarks/test_transaction_benchmark.py -q`

Expected: FAIL because `transactions` defaults and rollout metadata do not exist.

- [ ] **Step 3: Add safe config defaults and validation**

Add the exact defaults above under `DEFAULT_CONFIG`. Mode meanings:

- `off`: no transaction CLI commit/preview; schema/recovery reads remain available for existing records.
- `preview`: create/revise/preview/reconcile/eligibility/receipt work; commit/release/compensate return a clear config-gate error.
- `commit`: all three first adapter families are enabled subject to authority/approval.

Validation accepts only those modes, integer batch size `1..1000`, delay `1..604800`, and compensation default `manual|compensate_prefix`. No environment-variable bridge is added for these behavioral settings.

- [ ] **Step 4: Write the complete operator guide**

Document one copyable three-family CLI/TUI walkthrough; plan and authority YAML; create/preview/revise/commit/reconcile/eligibility/compensate/receipt/outbox commands; every status and eligibility code; exact vs semantic vs irreversible wording; approval expiry; crash/unknown recovery; profile storage paths using `display_hermes_home()` semantics; config modes; local benchmark invocation; and local data deletion/export behavior.

State the exclusions prominently: no arbitrary shell transaction, remote push, browser/service write, production DB, account deletion, purchase, live commerce/federation, cross-profile action, gateway command, Desktop parity promise, or exactly-once claim. Dashboard receives the feature through the existing embedded Ink TUI only.

- [ ] **Step 5: Write the complete adapter SDK guide**

Document descriptor fields and every method contract; canonical resource keys; redaction; idempotency distinctions; reconciliation dispositions; exact/semantic/no compensation; irreversible boundary; compensation windows; commit-time authority; stable operation ids; exceptions and unknown handling; thread/process safety; profile isolation; and required real-path tests.

Include a complete standalone-plugin example whose adapter declares one semantic compensation action, registers through `register_effect_adapter()`, and does not modify core. Explain that vendor integrations belong in standalone plugin repos or MCP servers and that widening the ABC requires a concrete consumer.

- [ ] **Step 6: Document the staged rollout**

1. Dark schema + recovery read path with `mode: off` on internal builds.
2. Default `mode: preview`; dogfood previews and local receipts without effects.
3. Opt-in `mode: commit` for designated test profiles after all 100 benchmark cases pass.
4. Keep commit opt-in until 30 real CLI/TUI transactions across the three adapter families show zero unauthorized irreversible commits, zero duplicates, every unknown surfaced, correct compensation, and `<15%` median eligible overhead.
5. Stop rollout on any false verified receipt, unclassified irreversible effect, approval replay, cross-profile write, duplicate instrumented effect, or compensation across a forbidden boundary. Do not relax a preregistered gate after results.

- [ ] **Step 7: Run GREEN, TUI tests, and docs build**

Run: `scripts/run_tests.sh tests/hermes_cli/test_config.py tests/benchmarks/test_transaction_benchmark.py -q`

Expected: PASS.

Run: `cd ui-tui && npm test -- --run src/__tests__/transactionCommand.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS.

Run: `cd website && npm run lint:diagrams && npm run typecheck && npm run build`

Expected: PASS; the built site includes the action-transactions operator and adapter-SDK pages with resolved links.

- [ ] **Step 8: Final clean-tree verification and commit**

Run:

```bash
git status --short
git diff --check
scripts/run_tests.sh tests/agent/effects tests/hermes_cli/test_transaction_cli.py \
  tests/hermes_cli/test_transaction_e2e.py tests/gateway/test_transaction_outbox.py \
  tests/tui_gateway/test_transaction_rpc.py tests/benchmarks/test_transaction_benchmark.py -q
```

Expected: only intended implementation/docs/test files are changed, diff check is clean, and all focused tests pass.

```bash
git add hermes_cli/config.py tests/hermes_cli/test_config.py \
  website/docs/user-guide/features/action-transactions.md \
  website/docs/development/effect-adapters.md website/docs/reference/cli-commands.md \
  website/docs/reference/slash-commands.md website/sidebars.ts \
  tests/benchmarks/test_transaction_benchmark.py
git commit -m "docs: roll out action transactions safely"
```

---

## Completion Gate

Do not call Reversible & Revisable Action Transactions complete until fresh evidence proves every item:

- Revision 1 can be previewed; committed nodes remain immutable facts; revision 2 changes only pending work and requires a fresh preview/approval.
- Every commit and compensation rechecks current authority immediately before the effect; stale, expired, changed, mismatched, replayed, or no-human approval fails closed.
- Prepare and preview perform no outward effect. A successful user-facing commit is reported only after durable result/evidence and receipt writes.
- Crash recovery classifies through the adapter and never blindly retries `running`, `dispatched`, or `unknown_effect` operations.
- Dynamic eligibility distinguishes exact undo, semantic compensation, and unsupported/blocked cases using live drift, window, authority, dependency, unknown, and irreversible-boundary facts.
- Cascade compensation uses reverse topological order, is idempotent, and stops before any changed/unsafe node.
- Workspace/V4A/local-worktree Git effects restore exact state when eligible and never push or operate on primary/main checkouts.
- Workflow/cron/config effects use version hashes and existing locks/atomic writers, preserve absent-vs-null and immutable versions, and never overwrite concurrent or managed changes.
- Delayed messages can be revised/cancelled before release, require exact final approval, dispatch at most once at the Hermes boundary, and become truthfully irreversible unless edit/delete compensation is proven.
- Every terminal transaction has one immutable shared receipt; later rechecks append observations; the 50 false-success seeds yield zero false `verified` results and use `completed_unverified` whenever effects completed without sufficient proof.
- All 100 preregistered cases pass the safety floors; eligible median overhead is at most 15% against current Hermes; denominators, exclusions, Wilson intervals, p50/p95, and safety strata are reported separately.
- System prompt, effective tool schemas, provider, model, and role alternation remain stable across preview, revision, commit, reconciliation, and compensation.
- Non-transaction tool, workflow, cron, config, delivery, CLI, TUI, and gateway behavior remains unchanged.
- CLI and native Ink TUI are the primary surfaces. Dashboard inherits the embedded TUI; gateway messaging and Desktop parity remain excluded.
- Focused Python, TUI, and documentation suites pass from a clean checkout, with no secrets, generated databases, benchmark reports, or caches committed.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-reversible-revisable-action-transactions.md`. Two execution options:

1. **Subagent-Driven (recommended)** — use `superpowers:subagent-driven-development`, one fresh implementation subagent per task with review between tasks.
2. **Inline Execution** — use `superpowers:executing-plans`, execute task batches with explicit checkpoints.
