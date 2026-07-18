# Plan Preview & What-If Dry Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user compare materially different action plans in bounded, effect-free shadow state, see predicted diffs, cost/time/risk, uncertainty, assumptions, unknowns, and irreversible boundaries, then accept one prediction only by creating a fresh, exactly hashed action transaction that must pass current authority and transaction preview again before any commit.

**Architecture:** Add `agent.preview` as a counterfactual layer above item #2's immutable transaction graphs and effect adapters, item #6's authority provider, and item #12's receipt store. Candidate graphs execute only against profile-local shadow overlays and pure/declarative simulators; the coordinator never calls `EffectAdapter.commit()`, an existing tool handler, browser/CDP/computer-use backends, workflow dispatch, cron mutation, delivery, or network I/O. CLI and native Ink TUI are the primary surfaces, the Dashboard is a secondary comparison/inspection surface, and acceptance materializes a new transaction from the selected candidate hash after source and authority rechecks rather than turning a prediction into executable authority.

**Tech Stack:** Python 3.13, frozen dataclasses/enums, canonical JSON/SHA-256, SQLite/WAL through `SessionDB`, item #2 `TransactionRevision`/`RevisionNode`/`RevisionEdge`/`EffectAdapter`/`TransactionCoordinator`, item #6 `AuthorityProvider`, item #12 immutable receipts/observations, Git subprocess argument arrays for read-only inspection and shadow-repository operations, Pydantic workflow models, Rich/classic CLI, Ink/TypeScript JSON-RPC TUI, React Dashboard, pytest through `scripts/run_tests.sh`, Vitest, and versioned YAML benchmark manifests.

## Global Constraints

- Implementation starts only after the public contracts from item #2 (`agent.effects`), item #6 (`agent.autonomy`), and item #12 (`agent.receipts`) have landed. This plan consumes those contracts and must not create local substitutes.
- Work from the branch containing this plan and preserve unrelated changes. Each task ends in exactly one conventional commit.
- TDD is mandatory. Run Python tests only through `scripts/run_tests.sh`; use package-local npm commands for Ink, Dashboard, and documentation checks.
- A dry run causes zero outward effect. It may write only its profile-local `state.db`, the bounded `get_hermes_home()/preview-shadow/` store, and an explicitly requested local report path. It never changes the declared source workspace/repository, Hermes workflow/cron/config source, browser or desktop UI, remote service, channel, account, or network peer.
- The dry-run coordinator never calls `EffectAdapter.commit()`, `EffectAdapter.compensate()`, a tool handler callback, `DeliveryRouter`, a workflow dispatcher/tick, a cron writer, `browser_*`, raw CDP, or `computer_use`. Tests enforce this with poison callbacks and egress traps.
- Built-in simulation is limited to declared filesystem/local-Git overlays and Hermes-owned workflow/cron/config state. Other effects require a validated declarative simulator bundle from a standalone plugin; otherwise every affected field is `unknown`.
- Browser and computer-use prediction is limited to preregistered, instrumented state-transition fixtures. There is no live navigation, screenshot, click, type, key, form submission, Office automation, arbitrary website model, or generic multi-step CUA world model in dry run.
- Predicting people, human replies, markets, prices, arbitrary websites, arbitrary service behavior, or the physical world is out of scope. The UI and receipts say `unknown`, never “likely,” when no explicit model covers a field.
- Candidate plans are materially different immutable transaction graphs. Duplicate graph hashes or candidates differing only in title, node IDs, presentation order, or prose are rejected.
- Candidate revision is dependency-aware: changing a node invalidates its prediction and every reachable descendant. Unaffected ancestor predictions may be reused only when their full input/state/provider hashes still match.
- Every prediction enumerates modeled and unmodeled fields, evidence, predicted before/after claims, cost/time ranges, risk/severity, assumptions, unknowns, confidence basis, and the first irreversible boundary on every reachable path. Point estimates without bounds are rejected.
- User-specified comparison criteria and their order/weights are persisted before execution. The UI always shows per-criterion values and contributions; a scalar score never hides a safety floor, unknown, critical risk, or irreversible boundary.
- Acceptance is not execution, approval, or verification. It rechecks source fingerprints and current authority, creates an exact item #2 transaction from the selected candidate revision/hash, records the preview-run binding, and invokes a fresh transaction `preview()`. Commit remains a separate item #2 command with fresh commit-time authority and exact approval.
- The system prompt, cached prefix, effective model-tool definition snapshot, provider, and model remain byte-stable for the conversation. Preview state never rewrites messages, injects a synthetic user turn, changes role alternation, or dynamically changes model-visible tools.
- Add no model-visible core tool and do not change any existing tool JSON schema. Delivery is Footprint Ladder rung 2: CLI + skill over transaction adapters; external-state simulator data remains in standalone plugins.
- Stable non-secret settings live under `what_if:` in `config.yaml`. No new user-facing environment variable is added. Credentials are neither needed nor exposed to simulation providers.
- Profiles remain independent islands. Every DB, shadow directory, manifest, report, receipt, and provider lookup resolves from the active `get_hermes_home()`; no live default-profile inheritance or cross-profile source path is allowed.
- Runtime/security/state changes receive real-path tests with temporary `HERMES_HOME`, real SQLite, real files and local Git repositories, real workflow/cron/config readers, and real CLI imports. Mock only clocks and the final prohibited external/network/process boundary.
- Preview receipts are prediction artifacts and therefore use `completed_unverified`; only item #12's private scorer may emit `verified` after observed outcome evidence. A simulated outcome is never completion evidence.
- No outbound telemetry. Benchmark reports are local JSON/Markdown with denominator, exclusions/aborts, Wilson intervals, p50/p95, cost source, safety slices, and statistical test details reported separately.

---

## Approved Portfolio Contract

**Layman outcome:** Before touching reality, Hermes can compare several approaches inside a bounded simulation and show likely changes, irreversible boundaries, and missing assumptions.

**Design boundary:** Item #2 owns effect preview, revision, compensation, and commit semantics for one selected action graph. This item generates/ingests materially different candidates, executes each through bounded shadows of those semantics, compares predicted end states and uncertainty, and selects one without touching reality. External-state simulators are standalone-provider plugins; unsupported effects are unknown. This is not a general digital twin.

**Exact 90-day proof:** Evaluate exactly 30 preregistered tasks across filesystem refactors, workflow changes, and instrumented browser/service actions. Before any run, enumerate every observable effect field and severity class in fixture manifests. Pass only with precision at least 90%, recall at least 90%, no missed critical or irreversible effect, severity-weighted false-negative loss below the preregistered bound, and significantly better selected-plan success than a no-simulator baseline under the same time/cost budget. Declaring fewer fields cannot improve the score.

**Dependencies and failure conditions:** Item #2 supplies the effect semantics and immutable selected transaction; item #6 supplies current authority; item #12 owns immutable receipts and predicted-versus-observed comparison. Predicting human responses, market prices, or arbitrary websites without an explicit model is excluded. Any missing simulator, stale source, stale authority, provider ambiguity, unbounded shadow, or unmodeled field blocks confident selection and remains visible as `unknown`.

**Delivery:** Footprint Ladder rung 2—`hermes what-if` plus `skills/what-if-preview/SKILL.md` over transaction adapters. No Desktop dependency, gateway command, or new core tool. CLI/classic slash and native Ink are primary; Dashboard comparison is secondary.

## Product Boundary and Truthful Vocabulary

| Term | Exact meaning |
|---|---|
| `dry_run` | Evaluation that invokes no outward-effect path and writes only bounded preview-owned state. |
| `modeled` | The exact adapter/action and every claimed field are covered by a versioned simulator contract. |
| `unknown` | No valid provider covers the effect/field, inputs are incomplete, or isolation cannot be proven. |
| `predicted` | A simulator-produced claim; never a verified observed outcome. |
| `assumption` | A declared input held constant by the simulation and shown to the user. |
| `irreversible boundary` | The first node on a reachable path whose item #2 semantics become irreversible after dispatch/commit. |
| `accepted` | A selected candidate was rebound to current source/authority and copied into a new exact transaction. No effect has committed. |
| `materially different` | Candidate changes adapter/action, canonical resources, dependencies, effect arguments, or an explicit strategy field that changes predicted effects. |

Canonical state sets:

```python
PreviewRunStatus = Literal[
    "draft", "running", "ready", "blocked", "failed", "accepted", "expired"
]
PredictionStatus = Literal["modeled", "partial", "unknown", "blocked"]
ConfidenceBasis = Literal["exact_shadow", "declarative_fixture", "none"]
Severity = Literal["low", "moderate", "high", "critical", "irreversible"]
CriterionDirection = Literal["minimize", "maximize", "must_equal", "must_not_exceed"]
```

The accepted-preview lifecycle is deliberately two-stage:

```text
what-if candidate revision + prediction hash
  -> source fingerprint recheck
  -> current authority explain/preview recheck (non-consuming)
  -> new immutable item #2 transaction graph + acceptance binding
  -> fresh item #2 TransactionCoordinator.preview()
  -> user separately invokes item #2 commit
  -> authority and exact approval rechecked immediately before each effect
```

No `what-if` command can call transaction `commit`, `compensate`, or outbox `release`.

## Current-Code Audit and Feasibility Findings

- `tools/checkpoint_manager.py::CheckpointManager.diff()` and `.restore()` operate on a real workspace shadow Git index, while item #2 adds forced immutable checkpoint refs. They are suitable for transaction before-state evidence, not for counterfactual mutation. What-If therefore reads source state and applies changes to its own overlay; it never calls `restore()` or stages the source index.
- Item #2's `workspace.v1`, `workspace-git.v1`, `hermes-workflow.v1`, `hermes-cron.v1`, and `hermes-config.v1` adapters provide canonical normalize/prepare/preview semantics. What-If consumes their normalized requests and prepared previews, but replaces commit with pure shadow application.
- `hermes_cli/workflows_spec.py::load_spec_from_object()` and `validate_graph()` provide strict workflow ingestion; `hermes_cli/workflows_engine.py::run_in_memory_until_waiting()` is pure and can predict deterministic pass/switch/parallel/join reachability. `agent_task`, `wait`, `send_message`, and subworkflow effects stay waiting/unknown unless the manifest supplies explicit outputs.
- `hermes_cli/workflows_db.py` stores immutable definition versions/checksums and uses `write_txn()`. The simulator reads records and copies canonical state; it never calls deploy/enable/publish or schedule registration.
- Current browser tools (`browser_navigate`, click/type/press/back, raw CDP) and `computer_use` call live backends and are registered destructive by default. They have no truthful generic dry-run mode. What-If blocks these handlers and supports only declarative instrumented transition bundles.
- `tools/approval.py` already hashes exact arguments and binds requester/channel/expiry; items #2/#6 strengthen transaction and authority bindings. What-If reuses those decisions but never treats preview acceptance as irreversible approval.
- `agent/operation_journal.py` truthfully records effect certainty. Dry run creates no operation-journal rows because no operation is attempted; only the later accepted item #2 transaction uses the journal.
- `hermes_cli/commands.py`, `tui_gateway/server.py`, and `ui-tui/src/app/slash/commands/ops.ts` provide the native terminal command path. Dashboard APIs must use short `_profile_scope` blocks and never hold the process-global profile scope across `await`.

## File Map

### New production files

- `agent/preview/__init__.py` — stable public exports only.
- `agent/preview/models.py` — frozen run, candidate, revision, effect manifest, prediction, estimate, score, comparison, and acceptance records.
- `agent/preview/store.py` — typed `SessionDB` persistence, immutable revisions/predictions/events, CAS transitions, retention.
- `agent/preview/manifests.py` — strict YAML/JSON candidate, criteria, observable-field, and provider-bundle ingestion.
- `agent/preview/graph.py` — material-difference fingerprinting, candidate revision validation, dependency invalidation, path boundaries.
- `agent/preview/shadow.py` — bounded profile-local overlay/blob store and source fingerprints.
- `agent/preview/simulators.py` — simulator registry and built-in filesystem/Git/Hermes-state pure simulators.
- `agent/preview/workflow.py` — deterministic workflow reachability/end-state prediction.
- `agent/preview/providers.py` — data-only standalone-plugin simulator bundle discovery/validation.
- `agent/preview/coordinator.py` — candidate execution, dependency ordering, estimate aggregation, comparison, and selection.
- `agent/preview/acceptance.py` — exact source/authority/hash recheck and item #2 transaction materialization.
- `agent/preview/receipts.py` — item #12 preview receipt builder and observed-comparison observations.
- `hermes_cli/what_if.py` — shared top-level/classic-slash parser, service construction, text/JSON/structured renderers.
- `skills/what-if-preview/SKILL.md` — terminal-first candidate authoring/comparison/acceptance instructions.
- `web/src/pages/WhatIfPage.tsx` — secondary Dashboard run/candidate comparison and exact acceptance UI.

### Existing production files modified

- `hermes_state.py` — additive preview tables/indexes and lazy `SessionDB.preview` facade.
- `hermes_cli/config.py` — safe `what_if` defaults and validation.
- `hermes_cli/plugins.py` — discover a data-only `what_if_simulator` manifest section without importing provider code.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `hermes_cli/cli_commands_mixin.py`, `cli.py` — register top-level/classic `what-if`/`dry-run` routes.
- `tui_gateway/server.py` — live-process `what-if.exec` RPC.
- `ui-tui/src/gatewayTypes.ts`, `ui-tui/src/app/slash/commands/ops.ts` — native structured command and comparison rendering.
- `hermes_cli/web_server.py` — profile-scoped secondary What-If APIs.
- `web/src/lib/api.ts`, `web/src/App.tsx` — typed client, route, and navigation.

### Benchmark and documentation files

- `benchmarks/what_if/__init__.py`
- `benchmarks/what_if/manifest.yaml`
- `benchmarks/what_if/cases.py`
- `benchmarks/what_if/runner.py`
- `benchmarks/what_if/scorer.py`
- `benchmarks/what_if/fixtures/filesystem/*.yaml` — 10 preregistered refactor tasks.
- `benchmarks/what_if/fixtures/workflow/*.yaml` — 10 preregistered workflow tasks.
- `benchmarks/what_if/fixtures/instrumented/*.yaml` — 5 browser and 5 service transition tasks.
- `website/docs/user-guide/features/what-if-preview.md`
- `website/docs/developer-guide/what-if-simulator-bundles.md`
- `website/docs/reference/cli-commands.md`, `website/docs/reference/slash-commands.md`, `website/sidebars.ts`

### Focused tests

- `tests/agent/preview/test_models.py`
- `tests/agent/preview/test_store.py`
- `tests/agent/preview/test_manifests.py`
- `tests/agent/preview/test_graph.py`
- `tests/agent/preview/test_shadow.py`
- `tests/agent/preview/test_simulators.py`
- `tests/agent/preview/test_workflow.py`
- `tests/agent/preview/test_providers.py`
- `tests/agent/preview/test_coordinator.py`
- `tests/agent/preview/test_acceptance.py`
- `tests/agent/preview/test_receipts.py`
- `tests/agent/preview/test_security.py`
- `tests/hermes_cli/test_what_if_cli.py`
- `tests/hermes_cli/test_what_if_e2e.py`
- `tests/hermes_cli/test_web_server_what_if.py`
- `tests/tui_gateway/test_what_if_rpc.py`
- `tests/benchmarks/test_what_if_benchmark.py`
- `ui-tui/src/__tests__/whatIfCommand.test.ts`
- `web/src/pages/WhatIfPage.test.tsx`

---

### Task 0: Preregister the Exact 30-Task Proof and False-Confidence Gates

**Files:**
- Create: `benchmarks/what_if/__init__.py`
- Create: `benchmarks/what_if/manifest.yaml`
- Create: `benchmarks/what_if/cases.py`
- Create: `benchmarks/what_if/fixtures/filesystem/*.yaml`
- Create: `benchmarks/what_if/fixtures/workflow/*.yaml`
- Create: `benchmarks/what_if/fixtures/instrumented/*.yaml`
- Create: `tests/benchmarks/test_what_if_benchmark.py`

**Interfaces:**
- Produces `load_benchmark(path: Path) -> tuple[BenchmarkManifest, tuple[BenchmarkCase, ...]]` and the immutable denominator consumed by Task 12.
- Produces the exact observable-field universe, severity weights, no-simulator baseline budget, and paired significance test before production code exists.

- [ ] **Step 1: Write the failing frozen-corpus test**

```python
from pathlib import Path

from benchmarks.what_if.cases import load_benchmark

ROOT = Path(__file__).resolve().parents[2]


def test_what_if_corpus_and_gates_are_frozen_before_execution():
    manifest, cases = load_benchmark(ROOT / "benchmarks/what_if/manifest.yaml")
    assert manifest.schema == "hermes.what-if-benchmark.v1"
    assert len(cases) == 30
    assert {case.stratum for case in cases} == {
        "filesystem_refactor", "workflow_change", "instrumented_browser", "instrumented_service"
    }
    assert sum(c.stratum == "filesystem_refactor" for c in cases) == 10
    assert sum(c.stratum == "workflow_change" for c in cases) == 10
    assert sum(c.stratum == "instrumented_browser" for c in cases) == 5
    assert sum(c.stratum == "instrumented_service" for c in cases) == 5
    assert manifest.gates.precision_min == 0.90
    assert manifest.gates.recall_min == 0.90
    assert manifest.gates.missed_critical_max == 0
    assert manifest.gates.missed_irreversible_max == 0
    assert manifest.gates.severity_weighted_false_negative_loss_bound == 0.05
    assert manifest.gates.selected_plan_test == "mcnemar_exact_one_sided"
    assert manifest.gates.selected_plan_p_max == 0.05
    assert manifest.baseline == "same_candidates_no_simulator"
    assert all(case.observable_fields for case in cases)
    assert all(field.severity for case in cases for field in case.observable_fields)
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_what_if_benchmark.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'benchmarks.what_if'`.

- [ ] **Step 3: Add exact manifest/case validation**

```python
STRATUM_COUNTS = {
    "filesystem_refactor": 10,
    "workflow_change": 10,
    "instrumented_browser": 5,
    "instrumented_service": 5,
}
SEVERITY_WEIGHTS = {
    "low": 1,
    "moderate": 3,
    "high": 9,
    "critical": 27,
    "irreversible": 27,
}


def validate_cases(manifest, cases):
    if len(cases) != 30 or len({case.id for case in cases}) != 30:
        raise ValueError("what-if benchmark must contain 30 unique cases")
    actual = {name: sum(case.stratum == name for case in cases) for name in STRATUM_COUNTS}
    if actual != STRATUM_COUNTS:
        raise ValueError(f"invalid what-if strata: {actual}")
    for case in cases:
        if not case.observable_fields:
            raise ValueError(f"{case.id}: observable field denominator is empty")
        keys = {(field.effect_id, field.field) for field in case.observable_fields}
        if len(keys) != len(case.observable_fields):
            raise ValueError(f"{case.id}: duplicate observable effect field")
        if any(field.severity not in SEVERITY_WEIGHTS for field in case.observable_fields):
            raise ValueError(f"{case.id}: unknown severity")
        if case.candidate_budget != case.baseline_budget:
            raise ValueError(f"{case.id}: baseline and candidate budgets differ")
```

Each fixture freezes at least two materially different candidate graphs, exact initial state, all observable effect fields, severity, expected modeled/unknown result, cost/time budget, success oracle, assumptions, and irreversible boundaries. Browser/service fixtures are local declarative transition maps; URLs use `.test` and no fixture authorizes network.

Precision is `TP / (TP + FP)` and recall is `TP / (TP + FN)` over the preregistered `(case, candidate, effect, field, normalized value)` universe. A missing prediction, an `unknown` where the fixture has an expected value, or omission from the provider declaration is a false negative. Extra declared fields do not increase the denominator; deleting expected fields is rejected, so declaring fewer fields cannot improve either metric. Severity-weighted false-negative loss is `sum(weight(FN)) / sum(weight(expected fields))` with the frozen weights above.

Selected-plan success uses the same candidates, wall-clock ceiling, and modeled cost budget for baseline and candidate. The baseline sees plan text plus item #2 effect previews but no counterfactual end-state simulator. Use a paired, one-sided exact McNemar test with `p <= 0.05`; a non-positive improvement or underpowered result is inconclusive and fails the gate.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_what_if_benchmark.py -q`

Expected: PASS with exactly 30 cases and all field/severity/budget invariants frozen.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/what_if tests/benchmarks/test_what_if_benchmark.py
git commit -m "test: preregister what-if preview proof"
```

---

### Task 1: Define Immutable Preview Models and Durable State

**Files:**
- Create: `agent/preview/__init__.py`
- Create: `agent/preview/models.py`
- Create: `agent/preview/store.py`
- Modify: `hermes_state.py`
- Create: `tests/agent/preview/test_models.py`
- Create: `tests/agent/preview/test_store.py`

**Interfaces:**
- Produces `PreviewStore(SessionDB)`, canonical `PreviewRun`, `CandidateRevision`, `PredictionRecord`, `ComparisonRecord`, `AcceptanceRecord`, `canonical_json()`, and `content_hash()`.
- Consumes item #2 `TransactionRevision`, `RevisionNode`, `RevisionEdge`, adapter/action identifiers, and `SessionDB._execute_read()` / `_execute_write()`.

- [ ] **Step 1: Write RED model, immutability, reopen, and CAS tests**

```python
def test_preview_revisions_predictions_and_events_are_immutable(session_db):
    store = PreviewStore(session_db)
    run = store.create_run(run_fixture())
    candidate = store.add_candidate(run.run_id, candidate_fixture(), expected_run_revision=1)
    prediction = store.insert_prediction(prediction_fixture(candidate))
    assert run.status == "draft"
    assert candidate.revision == 1 and candidate.graph_hash
    assert prediction.status == "modeled"
    with pytest.raises(ImmutablePreviewRecord):
        store.replace_candidate(candidate)
    assert store.transition_run(run.run_id, {"draft"}, "running")
    assert not store.transition_run(run.run_id, {"draft"}, "running")


def test_reopen_preserves_hashes_unknowns_scores_and_acceptance(tmp_path):
    first = SessionDB(tmp_path / "state.db")
    seed_ready_preview(PreviewStore(first))
    first.close()
    reopened = PreviewStore(SessionDB(tmp_path / "state.db"))
    snapshot = reopened.load_snapshot("preview-1")
    assert snapshot.run.status == "ready"
    assert snapshot.candidates[0].graph_hash == content_hash(snapshot.candidates[0].graph)
    assert snapshot.predictions[0].unknowns == ("human response",)
    assert snapshot.comparison.criteria_hash
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/preview/test_models.py tests/agent/preview/test_store.py -q`

Expected: FAIL importing `agent.preview`.

- [ ] **Step 3: Add frozen value objects and canonical hashing**

```python
def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EstimateRange:
    lower: int
    upper: int
    unit: Literal["usd_micros", "milliseconds"]
    basis: str

    def __post_init__(self):
        if self.lower < 0 or self.upper < self.lower or not self.basis:
            raise ValueError("invalid estimate range")


@dataclass(frozen=True)
class EffectField:
    effect_id: str
    field: str
    severity: Severity
    modeled: bool
    predicted_value: object | None
    evidence: tuple[Mapping[str, object], ...]
    assumptions: tuple[str, ...]
    unknown_reason: str | None
```

Add frozen records for `PreviewRun`, `CandidateRevision`, `SourceFingerprint`, `PredictionRecord`, `CriterionSpec`, `CriterionResult`, `ComparisonRecord`, `PreviewEvent`, and `AcceptanceRecord`. Validate finite vocabularies, integer fixed-point values, nonempty IDs, sorted tuples, no secrets/raw credentials, estimate lower/upper order, and `modeled=False` whenever `unknown_reason` is present.

- [ ] **Step 4: Add the complete additive schema and typed store methods**

```sql
CREATE TABLE IF NOT EXISTS preview_runs (
    run_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    run_revision INTEGER NOT NULL,
    criteria_json TEXT NOT NULL,
    criteria_hash TEXT NOT NULL,
    effect_manifest_json TEXT NOT NULL,
    effect_manifest_hash TEXT NOT NULL,
    selected_candidate_id TEXT,
    selected_candidate_revision INTEGER,
    run_hash TEXT,
    receipt_id TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS preview_candidates (
    run_id TEXT NOT NULL REFERENCES preview_runs(run_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    base_revision INTEGER,
    strategy TEXT NOT NULL,
    graph_json TEXT NOT NULL,
    graph_hash TEXT NOT NULL,
    source_fingerprints_json TEXT NOT NULL,
    invalidated_nodes_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, candidate_id, revision),
    UNIQUE (run_id, graph_hash)
);
CREATE TABLE IF NOT EXISTS preview_predictions (
    prediction_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES preview_runs(run_id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    candidate_revision INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    status TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    unknowns_json TEXT NOT NULL,
    cost_range_json TEXT NOT NULL,
    time_range_json TEXT NOT NULL,
    risk_json TEXT NOT NULL,
    irreversible_boundary_json TEXT,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (run_id, candidate_id, candidate_revision, node_id)
);
CREATE TABLE IF NOT EXISTS preview_comparisons (
    comparison_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES preview_runs(run_id) ON DELETE CASCADE,
    run_revision INTEGER NOT NULL,
    candidate_revisions_json TEXT NOT NULL,
    criteria_hash TEXT NOT NULL,
    results_json TEXT NOT NULL,
    pareto_front_json TEXT NOT NULL,
    blocked_candidates_json TEXT NOT NULL,
    comparison_hash TEXT NOT NULL UNIQUE,
    created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS preview_acceptances (
    acceptance_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES preview_runs(run_id),
    run_hash TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_revision INTEGER NOT NULL,
    candidate_hash TEXT NOT NULL,
    source_fingerprints_hash TEXT NOT NULL,
    authority_version INTEGER NOT NULL,
    authority_hash TEXT NOT NULL,
    transaction_id TEXT NOT NULL UNIQUE,
    transaction_graph_hash TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (run_id, run_hash)
);
CREATE TABLE IF NOT EXISTS preview_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES preview_runs(run_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (run_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_preview_runs_status_updated
    ON preview_runs(status, updated_at_ms);
CREATE INDEX IF NOT EXISTS idx_preview_predictions_candidate
    ON preview_predictions(run_id, candidate_id, candidate_revision, node_id);
```

Store methods verify hashes on every read, use exact revision/status CAS, insert immutable rows, deep-copy decoded JSON, and reject a second acceptance with changed identity. Add `SessionDB.preview` as a lazy facade; additive schema does not bump the schema version.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/preview/test_models.py tests/agent/preview/test_store.py tests/test_hermes_state.py -q`

Expected: PASS on new/reopened databases; immutable hashes and CAS transitions hold.

- [ ] **Step 6: Commit**

```bash
git add agent/preview hermes_state.py tests/agent/preview/test_models.py tests/agent/preview/test_store.py
git commit -m "feat: persist immutable what-if previews"
```

---

### Task 2: Ingest Materially Different Candidates and Revise Them Dependency-Aware

**Files:**
- Create: `agent/preview/manifests.py`
- Create: `agent/preview/graph.py`
- Modify: `agent/preview/models.py`
- Modify: `agent/preview/store.py`
- Create: `tests/agent/preview/test_manifests.py`
- Create: `tests/agent/preview/test_graph.py`

**Interfaces:**
- Produces `load_preview_manifest(path, *, max_bytes) -> PreviewManifest`, `candidate_fingerprint()`, `validate_material_difference()`, `revise_candidate()`, `invalidated_descendants()`, and `first_irreversible_boundaries()`.
- Consumes item #2 `TransactionRevision`, `RevisionNode`, `RevisionEdge`, `validate_graph()`, `topological_order()`, registered adapter descriptors, and immutable candidate rows from Task 1.

- [ ] **Step 1: Write RED strict-ingestion and dependency-invalidation tests**

```python
def test_candidates_must_be_effectfully_different(tmp_path, effect_registry):
    manifest = manifest_with_candidates(
        candidate("safe", graph_fixture()),
        candidate("renamed", graph_fixture(rename_nodes=True, reorder=True)),
    )
    with pytest.raises(CandidateNotMateriallyDifferent):
        load_preview_manifest(write_yaml(tmp_path, manifest), max_bytes=1_048_576)


def test_revision_invalidates_changed_node_and_all_descendants(store):
    original = graph("read -> transform -> config -> report")
    seed_candidate(store, original)
    revised = graph("read -> alternate_transform -> config -> report")
    record = revise_candidate(
        store, "preview-1", "candidate-a", expected_revision=1,
        graph=revised, reason="use bounded transformation",
    )
    assert record.revision == 2
    assert record.invalidated_nodes == ("alternate_transform", "config", "report")
    assert reusable_prediction_nodes(store, record) == ("read",)


def test_every_reachable_path_reports_first_irreversible_boundary(effect_registry):
    boundaries = first_irreversible_boundaries(branching_graph(), effect_registry)
    assert boundaries == {
        ("prepare", "local_write"): None,
        ("prepare", "outbox_send"): "outbox_send",
    }
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/preview/test_manifests.py tests/agent/preview/test_graph.py -q`

Expected: FAIL importing `agent.preview.manifests` and `agent.preview.graph`.

- [ ] **Step 3: Define the exact manifest grammar**

```yaml
schema: hermes.what-if.v1
title: Compare release-note update approaches
criteria:
  - id: critical_safety
    field: risk.max_severity
    direction: must_not_exceed
    target: high
    weight_ppm: 0
  - id: verified_changes
    field: effects.modeled_fraction_ppm
    direction: maximize
    weight_ppm: 600000
  - id: elapsed
    field: time.upper_ms
    direction: minimize
    weight_ppm: 400000
observable_effects:
  - effect_id: update_docs
    field: workspace.files.changed
    severity: moderate
  - effect_id: publish_message
    field: message.dispatched
    severity: irreversible
candidates:
  - id: local_only
    strategy: update files and stop before outbound notification
    graph: {nodes: {}, edges: []}
  - id: local_then_notify
    strategy: update files then prepare delayed notification
    graph: {nodes: {}, edges: []}
```

Reject unknown keys at every level, duplicate IDs, more than configured candidates/nodes/edges, criteria weights outside `0..1_000_000`, weighted criteria not totaling `1_000_000`, absent hard safety criteria, unknown adapter/action pairs, cycles, cross-profile resources, credential-shaped values, noncanonical resources, and omitted observable fields. YAML aliases are capped and recursive aliases are rejected. Files are UTF-8 and at most 1 MiB.

- [ ] **Step 4: Implement semantic candidate fingerprints and revisions**

```python
def candidate_fingerprint(graph: TransactionRevision) -> str:
    normalized_nodes = sorted(
        (
            node.adapter_id,
            node.action,
            tuple(sorted(node.resource_keys)),
            canonicalize_effect_args(node.adapter_id, node.action, node.args),
        )
        for node in graph.nodes.values()
    )
    node_semantics = {
        node_id: content_hash((node.adapter_id, node.action, node.resource_keys, node.args))
        for node_id, node in graph.nodes.items()
    }
    normalized_edges = sorted(
        (node_semantics[edge.parent_node_id], node_semantics[edge.child_node_id])
        for edge in graph.edges
    )
    return content_hash({"nodes": normalized_nodes, "edges": normalized_edges})
```

`validate_material_difference()` requires unique semantic fingerprints. `revise_candidate()` uses item #2 graph validation, immutable revision CAS, and rejects changes to the run's frozen criteria/effect denominator. It invalidates changed/added nodes plus all reachable descendants. A prior prediction is reusable only if candidate node canonical hash, parent output hashes, source fingerprints, provider ID/version, and effect-manifest hash are identical.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/preview/test_manifests.py tests/agent/preview/test_graph.py tests/agent/effects/test_graph.py -q`

Expected: PASS; cosmetic duplicates are rejected and revisions invalidate exactly the dependent subgraph.

- [ ] **Step 6: Commit**

```bash
git add agent/preview tests/agent/preview/test_manifests.py tests/agent/preview/test_graph.py
git commit -m "feat: validate revisable preview candidates"
```

---

### Task 3: Build Bounded Filesystem and Local-Git Shadow State

**Files:**
- Create: `agent/preview/shadow.py`
- Create: `agent/preview/simulators.py`
- Create: `tests/agent/preview/test_shadow.py`
- Create: `tests/agent/preview/test_simulators.py`

**Interfaces:**
- Produces `ShadowStore`, `ShadowSession`, `ShadowWorkspaceSimulator`, `ShadowGitSimulator`, `capture_source_fingerprints()`, and `assert_sources_unchanged()`.
- Consumes item #2 normalized/prepared `workspace.v1` and `workspace-git.v1` effects, source reads, V4A patch parsing, and Git read-only commands.

- [ ] **Step 1: Write RED real-filesystem/Git zero-effect and bound tests**

```python
def test_workspace_simulation_changes_only_preview_shadow(tmp_home, tmp_repo, coordinator):
    before_source = snapshot_tree_and_git(tmp_repo)
    result = coordinator.run(candidate_with_write_patch_and_local_commit(tmp_repo))
    assert result.predictions["write"].fields["workspace.files.changed"] == ["README.md"]
    assert "+new" in result.predictions["write"].diff
    assert snapshot_tree_and_git(tmp_repo) == before_source
    assert all(path.is_relative_to(tmp_home / "preview-shadow") for path in result.shadow_paths)


def test_shadow_rejects_escape_symlink_special_file_and_bounds(shadow_store, tmp_repo):
    for request, code in [
        (path_outside_root(), "outside_declared_root"),
        (symlink_escape(), "resolved_path_escape"),
        (named_pipe(), "unsupported_file_type"),
        (too_many_files(), "shadow_file_limit"),
        (too_many_bytes(), "shadow_byte_limit"),
    ]:
        with pytest.raises(ShadowLimitError, match=code):
            shadow_store.open(request)


def test_git_simulation_never_invokes_source_mutating_command(git_trace, coordinator):
    coordinator.run(candidate_with_local_commit())
    source_calls = [call.argv for call in git_trace if call.cwd == git_trace.source_root]
    assert all(call[:2] in (["git", "status"], ["git", "rev-parse"], ["git", "diff"]) for call in source_calls)
    assert not any(token in {"add", "commit", "reset", "checkout", "worktree", "push"} for call in source_calls for token in call)
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/preview/test_shadow.py tests/agent/preview/test_simulators.py -q`

Expected: FAIL importing `agent.preview.shadow`.

- [ ] **Step 3: Implement the bounded overlay and content-addressed blobs**

```python
@dataclass(frozen=True)
class ShadowLimits:
    max_candidates: int = 5
    max_nodes_per_candidate: int = 100
    max_files: int = 2_000
    max_bytes: int = 268_435_456
    max_single_file_bytes: int = 16_777_216


class ShadowStore:
    def open(self, run_id, candidate_id, roots, declared_paths, limits) -> ShadowSession:
        root = (get_hermes_home() / "preview-shadow" / run_id / candidate_id).resolve()
        assert root.is_relative_to((get_hermes_home() / "preview-shadow").resolve())
        return self._materialize_declared_overlay(root, roots, declared_paths, limits)
```

Copy only declared regular files, directories needed to contain them, file mode, BOM/line-ending metadata, and symlink identity after canonical escape checks. Store bytes once under `preview-shadow/<run>/blobs/<sha256>` and candidate overlays as immutable references plus writable copies. Reject devices, sockets, FIFOs, unreadable paths, hardlink ambiguity, case-fold collisions, and any limit breach before creating candidate state. Use sorted path locks and fsync metadata before marking a shadow ready.

Source fingerprints contain canonical path, existence, type, size, mode, SHA-256, Git root/HEAD/branch/index tree/status for declared paths, and adapter version. They never contain file content. `assert_sources_unchanged()` re-reads every field after simulation and converts any mismatch into `blocked/source_changed_during_dry_run`.

- [ ] **Step 4: Implement filesystem/V4A/Git prediction in the overlay only**

Reuse item #2 normalization and the shared V4A mutation-path parser. Apply write/patch/move/delete to overlay paths, calculate exact unified diffs and after hashes, and emit file-count/byte/mode/path fields. Never call the item #2 handler callback.

For `workspace-git.v1/commit_local`, inspect the source using `git status --porcelain=v2`, `git rev-parse`, and `git diff -- <declared paths>` only. Predict staged paths, tree diff, parent branch/HEAD requirements, and commit risk in shadow metadata. Do not predict an exact commit hash because author/time/hooks can change it. Reject remote/push arguments, primary `main`/`master`, detached HEAD, hooks whose effect is not modeled, submodules, LFS filters, and undeclared dirty paths as blocked or unknown rather than running Git against reality.

- [ ] **Step 5: Run GREEN and checkpoint regressions**

Run: `scripts/run_tests.sh tests/agent/preview/test_shadow.py tests/agent/preview/test_simulators.py tests/agent/effects/adapters/test_workspace.py tests/tools/test_checkpoint_manager.py -q`

Expected: PASS; source bytes, index, HEAD, refs, worktree list, and checkpoint refs are byte-for-byte unchanged after every dry run.

- [ ] **Step 6: Commit**

```bash
git add agent/preview/shadow.py agent/preview/simulators.py tests/agent/preview/test_shadow.py tests/agent/preview/test_simulators.py
git commit -m "feat: simulate workspace effects in bounded shadows"
```

---

### Task 4: Simulate Hermes Workflow, Cron, and Config State Without Dispatch

**Files:**
- Create: `agent/preview/workflow.py`
- Modify: `agent/preview/simulators.py`
- Create: `tests/agent/preview/test_workflow.py`
- Modify: `tests/agent/preview/test_simulators.py`

**Interfaces:**
- Produces `HermesStateShadow`, `WorkflowDryRunner`, and simulator actions for `hermes-workflow.v1`, `hermes-cron.v1`, and `hermes-config.v1`.
- Consumes item #2 owner-module `prepare_*_mutation` results, `WorkflowSpec`, `load_spec_from_object()`, `validate_graph()`, `run_in_memory_until_waiting()`, and read-only current-store snapshots.

- [ ] **Step 1: Write RED state-copy and workflow-reachability tests**

```python
@pytest.mark.parametrize("family", ["workflow", "cron", "config"])
def test_hermes_state_dry_run_changes_shadow_not_profile_store(state_harness, family):
    before = state_harness.profile_snapshot(family)
    result = state_harness.simulate(family)
    assert result.before != result.after
    assert state_harness.profile_snapshot(family) == before


def test_workflow_runner_predicts_only_deterministic_nodes(workflow_harness):
    result = workflow_harness.run("pass -> switch -> parallel -> join -> agent_task -> send_message")
    assert result.reached == ("pass", "switch", "parallel", "join", "agent_task")
    assert result.waiting == ("agent_task",)
    assert result.unknown_nodes == ("agent_task", "send_message")
    assert workflow_harness.dispatch_calls == 0
    assert workflow_harness.outbox_calls == 0


def test_cron_preview_computes_schedule_without_registering(cron_harness):
    before = cron_harness.jobs_bytes()
    prediction = cron_harness.simulate_create("*/15 * * * *")
    assert len(prediction.next_fire_times) == 5
    assert cron_harness.jobs_bytes() == before
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/preview/test_workflow.py tests/agent/preview/test_simulators.py -q`

Expected: FAIL importing `agent.preview.workflow`.

- [ ] **Step 3: Add canonical Hermes-owned shadow documents**

`HermesStateShadow.open()` reads and hashes only the named workflow definition/version/enabled state, cron job record, or config leaf/document required by the normalized effect. It deep-copies records into the candidate shadow and applies the item #2 `StateMutation.before/after` contract without calling `apply_*_mutation()` or `restore_*_mutation()`.

Config simulation preserves absent-versus-null, alias/coercion, raw key ordering, unrelated keys, and managed-scope checks; secret/credential keys are blocked. Workflow simulation preserves immutable versions and predicts deploy/publish/enable/disable plus schedule-registration deltas without a DB write. Cron predicts create/update/disable, normalized fields, and the next five UTC fire times using the fixture clock; it never invokes `_jobs_lock()` save or scheduler wakeup.

- [ ] **Step 4: Implement pure workflow graph execution and uncertainty propagation**

```python
@dataclass(frozen=True)
class WorkflowPrediction:
    reached: tuple[str, ...]
    skipped: tuple[str, ...]
    waiting: tuple[str, ...]
    unknown_nodes: tuple[str, ...]
    output_claims: Mapping[str, object]
    irreversible_boundaries: tuple[str, ...]


def run_workflow_shadow(spec, input_data, supplied_outputs):
    completed = {node_id: value for node_id, value in supplied_outputs.items() if node_id in spec.nodes}
    result = run_in_memory_until_waiting(
        spec, input_data,
        completed_wait_nodes=set(completed),
        completed_node_outputs=completed,
    )
    unknown = tuple(sorted(node_id for node_id in result.waiting_nodes if node_id not in completed))
    return workflow_prediction_from_engine(result, unknown)
```

Only explicit fixture/user-supplied outputs may advance an `agent_task`, wait, subworkflow, or send boundary. The assumption and provenance for each supplied output are displayed. If a branch condition depends on an unknown value, enumerate bounded possible branches up to `max_branch_states=32`; beyond that mark descendants unknown rather than choosing a path.

- [ ] **Step 5: Run GREEN and owner-store regressions**

Run: `scripts/run_tests.sh tests/agent/preview/test_workflow.py tests/agent/preview/test_simulators.py tests/hermes_cli/test_workflows_db.py tests/hermes_cli/test_workflows_spec.py tests/cron/test_jobs.py tests/hermes_cli/test_config.py -q`

Expected: PASS; real workflow DB, jobs file, config file, dispatcher, outbox, and scheduler state remain unchanged.

- [ ] **Step 6: Commit**

```bash
git add agent/preview/workflow.py agent/preview/simulators.py tests/agent/preview/test_workflow.py tests/agent/preview/test_simulators.py
git commit -m "feat: simulate hermes state and workflow graphs"
```

---

### Task 5: Add Data-Only External Simulator Bundles and Hard-Block Live Browser/CUA

**Files:**
- Create: `agent/preview/providers.py`
- Modify: `agent/preview/manifests.py`
- Modify: `hermes_cli/plugins.py`
- Create: `tests/agent/preview/test_providers.py`
- Create: `tests/agent/preview/test_security.py`
- Modify: `tests/hermes_cli/test_plugins.py`

**Interfaces:**
- Produces `SimulatorBundleDescriptor`, `DeclarativeSimulatorProvider`, `discover_simulator_bundles()`, and `simulate_transition()`.
- Consumes installed standalone plugin manifests as data; it never imports or executes provider Python/JavaScript and never invokes browser/CDP/CUA/service tools.

- [ ] **Step 1: Write RED provider validation, egress, and unknown-fallback tests**

```python
def test_browser_and_cua_handlers_are_poisoned_in_dry_run(preview_harness):
    preview_harness.poison("browser_navigate", "browser_click", "browser_type", "browser_cdp", "computer_use")
    result = preview_harness.run(instrumented_browser_candidate_without_bundle())
    assert result.status == "blocked"
    assert result.predictions["browser"].status == "unknown"
    assert result.tool_calls == []
    assert result.network_calls == []


def test_data_only_bundle_predicts_exact_declared_transition(plugin_home, preview_harness):
    install_data_only_bundle(plugin_home, fixture_bundle())
    provider = discover_simulator_bundles()["instrumented.test-site.v1"]
    result = preview_harness.simulate(provider, action="click", state="cart", input={"ref": "checkout"})
    assert result.next_state == "review"
    assert result.fields == {"ui.page": "review", "purchase.committed": False}
    assert result.confidence_basis == "declarative_fixture"


@pytest.mark.parametrize("attack", [
    "bundle_path_escape", "recursive_json", "oversized_bundle", "unknown_field",
    "undeclared_transition", "credential_value", "http_url", "shell_command",
    "python_entrypoint", "cross_profile_path", "ssrf_literal", "state_explosion",
])
def test_bundle_attack_never_executes_or_expands_prediction(provider_harness, attack):
    result = provider_harness.load_or_run(attack)
    assert result.status in {"blocked", "unknown"}
    assert provider_harness.executed_code == []
    assert provider_harness.egress == []
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/preview/test_providers.py tests/agent/preview/test_security.py tests/hermes_cli/test_plugins.py -q`

Expected: FAIL importing `agent.preview.providers`.

- [ ] **Step 3: Define the data-only standalone-plugin contract**

```yaml
what_if_simulator:
  schema: hermes.what-if-simulator.v1
  provider_id: instrumented.test-site.v1
  version: 1.0.0
  bundle: simulators/test-site.json
  sha256: 6a5d4d78b4e56d9b6f67ad1b5df9db9ef6be1d36fcd5c73a52fe507e946ca5d3
```

The JSON bundle contains finite `adapter_actions`, `observable_fields`, `initial_states`, and a list of `{state, action, normalized_input, next_state, fields, cost_range, time_range, assumptions, irreversible_boundary}` transitions. It contains no URL fetch instruction, code, template execution, regex with backtracking, environment reference, file read, subprocess, or credential.

`hermes_cli.plugins` discovers the manifest and bundle path but does not import the plugin module for simulation. Validate realpath containment in the installed plugin directory, exact SHA-256, maximum 4 MiB, maximum 1,000 states, maximum 10,000 transitions, maximum 128 fields, finite acyclic JSON, unique transition keys, complete field declarations, semantic version, and no secret-like values. Third-party bundles remain standalone plugin repos under `~/.hermes/plugins/` or pip entry points; none are added to the core `plugins/` tree.

- [ ] **Step 4: Enforce exact coverage and truthful unknowns**

`DeclarativeSimulatorProvider.simulate()` performs a dictionary lookup over canonical input/state hashes. Exact match returns declared fields. No match, multiple matches, missing initial state, field not in `observable_fields`, or state-count expansion beyond the configured limit returns `unknown`; it never interpolates or guesses. Provider confidence can be `declarative_fixture` only, never `exact_shadow`.

Add a dry-run denylist at the coordinator boundary for all tool names beginning `browser_`, raw CDP, `computer_use`, terminal/shell, message/delivery, webhook, account, purchase, remote Git, and arbitrary service adapters. A matching declarative bundle predicts fields without calling the tool; absence remains unknown. Navigation itself is an outward network effect and is forbidden even when the target is localhost or `.test`.

- [ ] **Step 5: Run GREEN and live-tool regressions**

Run: `scripts/run_tests.sh tests/agent/preview/test_providers.py tests/agent/preview/test_security.py tests/hermes_cli/test_plugins.py tests/tools/test_browser_ssrf_guard.py tests/computer_use -q`

Expected: PASS; instrumented fixture transitions predict locally, all live handlers/egress remain at zero, and normal non-preview browser/CUA behavior is unchanged.

- [ ] **Step 6: Commit**

```bash
git add agent/preview/providers.py agent/preview/manifests.py hermes_cli/plugins.py tests/agent/preview/test_providers.py tests/agent/preview/test_security.py tests/hermes_cli/test_plugins.py
git commit -m "feat: load declarative what-if simulators"
```

---

### Task 6: Execute Candidate DAGs, Propagate Uncertainty, and Compare Alternatives

**Files:**
- Create: `agent/preview/coordinator.py`
- Modify: `agent/preview/models.py`
- Modify: `agent/preview/store.py`
- Modify: `agent/preview/graph.py`
- Create: `tests/agent/preview/test_coordinator.py`

**Interfaces:**
- Produces `PreviewCoordinator.run()`, `.compare()`, `.revise()`, `.select()`, `aggregate_estimates()`, `score_candidate()`, and `pareto_front()`.
- Consumes Tasks 1–5 stores/graphs/shadows/simulators and item #2 normalize/prepare/preview methods only.

- [ ] **Step 1: Write RED ordering, poison-commit, unknown, estimate, and scoring tests**

```python
def test_coordinator_never_crosses_preview_boundary(preview_harness):
    preview_harness.poison_all_effect_methods_except("normalize", "prepare", "preview")
    result = preview_harness.run(three_candidate_manifest())
    assert result.status == "ready"
    assert preview_harness.trace == [
        "source_fingerprinted", "shadow_opened", "normalized", "prepared", "previewed",
        "shadow_simulated", "prediction_persisted", "sources_rechecked", "compared",
    ]
    assert preview_harness.operation_journal_rows == 0


def test_unknown_parent_propagates_without_hallucinating_descendants(preview_harness):
    result = preview_harness.run(graph_with_unknown_service_parent())
    assert result.node("service").status == "unknown"
    assert result.node("local_report").status == "unknown"
    assert result.node("local_report").unknowns == ("parent service output unknown",)


def test_comparison_keeps_safety_floors_and_ranges_visible(preview_harness):
    comparison = preview_harness.compare(candidate_set())
    assert comparison.candidates["fast"].blocked_by == ("critical_safety",)
    assert comparison.candidates["safe"].cost.upper >= comparison.candidates["safe"].cost.lower
    assert comparison.pareto_front == ("safe", "cheap")
    assert comparison.recommended_candidate is None
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/preview/test_coordinator.py -q`

Expected: FAIL importing `agent.preview.coordinator`.

- [ ] **Step 3: Implement the zero-effect candidate execution algorithm**

```text
CAS draft|blocked -> running
freeze candidate revisions + criteria/effect-manifest hashes
capture all source fingerprints
for candidate in lexical candidate-id order:
    open bounded shadow
    for node in item-#2 stable topological order:
        adapter.normalize(node, preview_context)
        adapter.prepare(normalized, preview_context)
        adapter.preview(prepared, preview_context)
        choose exact-shadow/declarative/unknown simulator
        apply only to candidate shadow
        persist prediction before exposing it to descendants
    close/finalize candidate shadow
re-read source fingerprints; block on any drift
compare immutable predictions
persist comparison and run hash
CAS running -> ready
```

The `preview_context` has no `invoke` callback, transaction context, credentials, delivery router, browser session, CUA backend, network client, or environment capable of spawning commands. If an item #2 adapter cannot prepare without an external read, mark it unknown; do not weaken the adapter or perform the read.

- [ ] **Step 4: Aggregate effects, estimates, risks, assumptions, and boundaries**

Cost ranges sum lower/upper integer USD micros only when units/bases are compatible; unknown cost makes aggregate upper unknown rather than zero. Sequential time sums ranges, parallel branches use min/max critical-path bounds, and unknown duration remains unknown. Risk is a visible list plus maximum severity, never averaged down. Assumptions/unknowns are stable de-duplicated tuples with node/provider provenance. Every reachable path reports its first item #2 irreversible boundary; a candidate with an unmodeled boundary is safety-blocked.

For each manifest field, persist exactly one `EffectField`. Missing provider output becomes unknown. Providers may emit extra diagnostics but cannot add benchmark-scored fields after the denominator is frozen.

- [ ] **Step 5: Implement explicit criteria and Pareto comparison**

Evaluate hard `must_equal`/`must_not_exceed` criteria first; violations block selection. Normalize only fully modeled numeric criteria using manifest bounds. Weighted score is the sum of integer ppm contributions and is shown with every contribution. `pareto_front()` compares unblocked candidates on raw user criteria. The service does not auto-select: `.select(candidate_id, expected_comparison_hash)` records the user's/agent's explicit choice after hash CAS and reports all unknown/high-risk facts again.

- [ ] **Step 6: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/preview/test_coordinator.py tests/agent/preview/test_graph.py tests/agent/preview/test_simulators.py -q`

Expected: PASS; commits/handlers/journal/network remain unused, unknowns propagate, and comparison is deterministic across input ordering.

- [ ] **Step 7: Commit**

```bash
git add agent/preview/coordinator.py agent/preview/models.py agent/preview/store.py agent/preview/graph.py tests/agent/preview/test_coordinator.py
git commit -m "feat: compare bounded what-if candidates"
```

---

### Task 7: Accept a Prediction Only as a Fresh Exact Transaction

**Files:**
- Create: `agent/preview/acceptance.py`
- Modify: `agent/preview/store.py`
- Modify: `agent/preview/coordinator.py`
- Create: `tests/agent/preview/test_acceptance.py`
- Modify: `tests/agent/effects/test_authority.py`
- Modify: `tests/agent/effects/test_coordinator.py`

**Interfaces:**
- Produces `accept_candidate(request, *, store, transaction_store, transaction_coordinator, authority_provider) -> AcceptedTransaction`.
- Consumes item #2 `TransactionStore.create_transaction()`, `TransactionCoordinator.preview()`, item #6 `AuthorityProvider.current_contract()` / `authorize_effect(..., stage="preview", consume=False)`, exact hashes, and source fingerprints.

- [ ] **Step 1: Write RED hash/source/authority/replay/zero-commit tests**

```python
def test_acceptance_creates_exact_transaction_then_fresh_preview(acceptance_harness):
    ready = acceptance_harness.ready_selected_run()
    accepted = acceptance_harness.accept(
        run_id=ready.run_id,
        expected_run_hash=ready.run_hash,
        candidate_id="safe",
        expected_candidate_revision=2,
        expected_candidate_hash=ready.candidate_hash("safe", 2),
    )
    assert accepted.transaction.graph_hash == ready.candidate_hash("safe", 2)
    assert accepted.transaction.status == "ready"
    assert accepted.transaction_preview_hash != ready.prediction_hash("safe", 2)
    assert acceptance_harness.trace == [
        "run_reloaded", "source_rechecked", "authority_reloaded", "authority_explained",
        "transaction_created", "acceptance_persisted", "transaction_previewed",
    ]
    assert acceptance_harness.adapter_commit_calls == 0


@pytest.mark.parametrize("drift", [
    "run_hash", "candidate_hash", "candidate_revision", "source_file", "git_head",
    "workflow_version", "cron_revision", "config_hash", "authority_version", "authority_expiry",
])
def test_acceptance_fails_closed_on_any_drift(acceptance_harness, drift):
    acceptance_harness.introduce(drift)
    with pytest.raises(PreviewAcceptanceConflict):
        acceptance_harness.accept_current()
    assert acceptance_harness.transaction_count == 0
    assert acceptance_harness.adapter_commit_calls == 0


def test_acceptance_replay_returns_same_transaction(acceptance_harness):
    first = acceptance_harness.accept_current()
    replay = acceptance_harness.accept_current()
    assert replay.transaction.transaction_id == first.transaction.transaction_id
    assert acceptance_harness.transaction_count == 1
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/preview/test_acceptance.py tests/agent/effects/test_authority.py tests/agent/effects/test_coordinator.py -q`

Expected: FAIL importing `agent.preview.acceptance`.

- [ ] **Step 3: Add exact request/result contracts**

```python
@dataclass(frozen=True)
class AcceptCandidateRequest:
    run_id: str
    expected_run_hash: str
    candidate_id: str
    expected_candidate_revision: int
    expected_candidate_hash: str
    requester: str
    channel: str


@dataclass(frozen=True)
class AcceptedTransaction:
    acceptance_id: str
    transaction_id: str
    transaction_graph_hash: str
    transaction_preview_hash: str
    authority_version: int
    authority_hash: str
    next_command: str
```

- [ ] **Step 4: Implement the acceptance transaction boundary**

Under a preview-store CAS transaction, reload the ready run, selected candidate, comparison, and all predictions; verify every expected hash/revision and that no hard criterion is blocked. Outside the DB lock, re-read every source fingerprint and current authority. Map each normalized effect to item #6 `ActionContext` and call `authorize_effect(..., stage="preview", consume=False)`. Any `deny`, expiry, changed contract, newly required fact, or changed source blocks acceptance and requires a new dry run.

Create a deterministic transaction ID from `sha256("what-if-accept\0" + run_hash + "\0" + candidate_hash + "\0" + authority_hash)`. Call item #2 `TransactionStore.create_transaction()` with the exact graph, current authority snapshot, failure policy `stop`, and title/source metadata in an append-only `preview_accepted` transaction event. Insert `preview_acceptances` with the same identity; on a unique-key race, load and verify the existing transaction.

After durable acceptance, call `TransactionCoordinator.preview(transaction_id)`. This is a fresh real-state effect preview, not reuse of predicted state. If it blocks or differs, return the transaction as `blocked` with a structured diff; never edit the accepted graph or call commit. The next command is always `hermes transaction show <id>` or `hermes transaction commit <id>`, never an automatic commit.

- [ ] **Step 5: Preserve exact irreversible approval semantics**

Acceptance creates no `transaction_approvals` row and consumes no approval/mandate. Item #2 later binds irreversible approval to transaction ID, revision, node ID, final argument hash, fresh transaction preview hash, resources/destination, current authority version, requester/channel, and expiry immediately before commit. Add a regression proving a What-If selection or approval-looking manifest field cannot satisfy that binding.

- [ ] **Step 6: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/preview/test_acceptance.py tests/agent/effects/test_authority.py tests/agent/effects/test_coordinator.py -q`

Expected: PASS; acceptance is replay-safe, stale facts fail closed, and zero adapter commits occur.

- [ ] **Step 7: Commit**

```bash
git add agent/preview/acceptance.py agent/preview/store.py agent/preview/coordinator.py tests/agent/preview/test_acceptance.py tests/agent/effects/test_authority.py tests/agent/effects/test_coordinator.py
git commit -m "feat: bind accepted previews to exact transactions"
```

---

### Task 8: Issue Prediction Receipts and Compare Them with Observed Outcomes

**Files:**
- Create: `agent/preview/receipts.py`
- Modify: `agent/preview/store.py`
- Create: `tests/agent/preview/test_receipts.py`
- Modify: `tests/agent/test_receipts.py`

**Interfaces:**
- Consumes item #12 `ReceiptStatus`, `ReceiptStore.insert()`, `ReceiptStore.append_observation()`, `VerifiedReceiptDecision`, and accepted transaction receipt lookup.
- Produces `PreviewReceiptBuilder.issue()` and `.compare_observed()` without changing item #12's schema/status vocabulary or allowing prediction evidence to emit `verified`.

- [ ] **Step 1: Write RED prediction-truth and append-only comparison tests**

```python
def test_preview_receipt_is_never_verified(receipt_harness):
    receipt = receipt_harness.issue_ready_preview()
    assert receipt.status == "completed_unverified"
    assert receipt.claims["prediction_only"] is True
    assert receipt.claims["outward_effects"] == 0
    assert receipt.claims["modeled_fields"] + receipt.claims["unknown_fields"] == receipt.claims["declared_fields"]


def test_observed_comparison_appends_without_mutating_preview_receipt(receipt_harness):
    original = receipt_harness.issue_ready_preview()
    transaction_receipt = receipt_harness.issue_observed_transaction_receipt()
    observation = receipt_harness.compare_observed(original.receipt_id, transaction_receipt.receipt_id)
    assert observation.kind == "prediction_observed_comparison"
    assert observation.claims["field_confusion"] == {"tp": 8, "fp": 1, "fn": 1}
    assert receipt_harness.load(original.receipt_id) == original


@pytest.mark.parametrize("false_confidence", [
    "model_text_says_certain", "provider_omits_field", "unknown_parent", "stale_source",
    "missing_bundle", "bundle_hash_mismatch", "unmodeled_irreversible", "point_estimate_only",
    "browser_handler_success_fixture", "transaction_preview_without_observed_receipt",
])
def test_false_confidence_never_becomes_verified(receipt_harness, false_confidence):
    receipt = receipt_harness.issue(false_confidence)
    assert receipt.status != "verified"
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/preview/test_receipts.py tests/agent/test_receipts.py -q`

Expected: FAIL importing `agent.preview.receipts`.

- [ ] **Step 3: Build immutable prediction claims on the shared schema**

Claims include run/candidate/revision/graph/criteria/effect-manifest/comparison/provider/input/output/source hashes; per-field modeled/unknown status and severity; before/after predictions; assumptions; unknowns; cost/time bounds and basis; risk; path-specific irreversible boundaries; selected candidate and explicit actor; zero outward-handler/journal/network counts; and accepted transaction link when present. Redact content/recipient/resource details according to item #12 while preserving stable hashes.

`issue()` always requests `completed_unverified` through the shared non-verified insertion path. It cannot access item #12's private verified factory. Persist the receipt before projecting `preview_runs.receipt_id`; restart links by subject/content hash after a projection crash.

- [ ] **Step 4: Append predicted-versus-observed comparison only from immutable receipts**

`compare_observed()` requires the accepted transaction binding and a terminal item #12 transaction receipt. It aligns fields by canonical `(adapter, action, resource_hash, field)` identity; reports TP/FP/FN, value error, severity-weighted FN loss, cost/time error, missed/earlier/later irreversible boundary, observed unknowns, and exclusions. It appends an observation to the preview receipt and a cross-link observation to the transaction receipt. It never changes either receipt or retroactively labels the preview verified.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/preview/test_receipts.py tests/agent/test_receipts.py tests/agent/effects/test_receipts.py -q`

Expected: PASS; all false-confidence seeds remain non-verified and comparison is append-only.

- [ ] **Step 6: Commit**

```bash
git add agent/preview/receipts.py agent/preview/store.py tests/agent/preview/test_receipts.py tests/agent/test_receipts.py
git commit -m "feat: record what-if prediction receipts"
```

---

### Task 9: Deliver the Top-Level, Classic CLI, and Operating Skill

**Files:**
- Create: `hermes_cli/what_if.py`
- Create: `skills/what-if-preview/SKILL.md`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/main.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Modify: `cli.py`
- Create: `tests/hermes_cli/test_what_if_cli.py`
- Modify: `tests/hermes_cli/test_commands.py`
- Modify: `tests/gateway/test_gateway_command_help.py`

**Interfaces:**
- Produces `build_parser()`, `what_if_command(args) -> int`, `run_argv(argv, *, output_mode) -> CommandResult`, and `run_slash(rest) -> str` over one service path.
- Consumes preview coordinator/store/acceptance/receipt APIs; no UI-specific state enters `agent.preview`.

- [ ] **Step 1: Write RED grammar, rendering, redaction, and rejection tests**

```python
@pytest.mark.parametrize("argv", [
    ["create", "--manifest", "what-if.yaml"],
    ["list", "--status", "ready"],
    ["show", "preview-1"],
    ["run", "preview-1"],
    ["compare", "preview-1"],
    ["revise", "preview-1", "safe", "--plan", "safe-v2.yaml", "--expected-revision", "1", "--reason", "remove send"],
    ["select", "preview-1", "safe", "--expected-comparison-hash", "abc"],
    ["accept", "preview-1", "--expected-run-hash", "abc", "--candidate", "safe", "--expected-revision", "2", "--expected-candidate-hash", "def"],
    ["receipt", "preview-1"],
    ["receipt", "preview-1", "--compare-transaction-receipt", "receipt-2"],
    ["providers"],
    ["doctor"],
    ["delete", "preview-1", "--apply"],
])
def test_what_if_parser_accepts_only_bounded_commands(argv, parser):
    args = parser.parse_args(["what-if", *argv])
    assert args.what_if_action
```

Reject one candidate, cosmetic duplicates, unbounded manifests, cycles, unknown adapters/actions, cross-profile paths, secrets, arbitrary shell, remote Git, live browser/CDP/CUA, nondeclarative service provider, stale hashes/revisions, acceptance without explicit selection, blocked safety criteria, and every trailing argument. JSON/structured output redacts content and raw recipient/resource values while preserving hashes and stable IDs.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_what_if_cli.py tests/hermes_cli/test_commands.py -q`

Expected: FAIL importing `hermes_cli.what_if`.

- [ ] **Step 3: Implement one parser/service/renderer path**

`create` ingests and persists but does not automatically run. `run` performs only dry-run simulation. `compare` renders one candidate per column/section with predicted diffs, per-criterion values/contributions, cost/time ranges, risk, assumptions, unknowns, modeled/unmodeled counts, provider basis, and irreversible boundaries. `select` is exact comparison-hash CAS. `accept` prints the new transaction ID, fresh transaction-preview differences, current authority version, and exact next transaction command; it never invokes commit.

Exit codes: 0 success/ready/preview artifact, 2 validation or stale hash, 3 safety-blocked/unknown prevents selection, 4 storage/provider failure, 5 source changed during dry run. Input files cap at 1 MiB, argv at 64 entries/64 KiB, list limit at 500, and output reports write only to explicit paths.

- [ ] **Step 4: Register top-level and classic slash routes**

```python
CommandDef(
    "what-if",
    "Compare bounded plans without touching reality",
    "Tools & Skills",
    aliases=("dry-run", "whatif"),
    args_hint="[create|run|compare|revise|select|accept|receipt|providers|doctor]",
    subcommands=(
        "create", "list", "show", "run", "compare", "revise", "select",
        "accept", "receipt", "providers", "doctor", "delete",
    ),
    cli_only=True,
)
```

Add `cmd_what_if` next to `cmd_workflow`, parser setup next to workflow/transaction parsers, `_handle_what_if_command()` next to corresponding classic handlers, and canonical dispatch in `cli.py`. Use `shlex.split(rest, posix=os.name != "nt")`; never spawn a shell. Do not add gateway or Desktop routing.

- [ ] **Step 5: Write the complete terminal-first skill**

The skill contains copyable two- and three-candidate manifests for filesystem, workflow/cron/config, and an instrumented provider. It instructs the agent to propose materially different strategies, enumerate observable effect fields/severity before running, compare ranges/unknowns instead of confident prose, show every irreversible boundary, revise dependency-aware, select by exact comparison hash, and accept only through exact run/candidate hashes. It says acceptance is not approval/commit, requires transaction preview inspection, and calls completion only from item #12 observed receipts.

It forbids live browser/CUA/network/service calls in dry-run, arbitrary shell, remote push, production DB, account changes, purchases, people/market prediction, cross-profile state, invented provider outputs, omission of unknown fields, and pagination/lazy reading of the skill.

- [ ] **Step 6: Run GREEN and smoke tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_what_if_cli.py tests/hermes_cli/test_commands.py tests/gateway/test_gateway_command_help.py -q`

Expected: PASS; `/what-if` is in CLI/TUI catalogs and absent from gateway help.

Run: `uv run hermes what-if --help`

Expected: exit 0 and list bounded subcommands without starting chat, Dashboard, Desktop, browser, or a model call.

- [ ] **Step 7: Commit**

```bash
git add hermes_cli/what_if.py skills/what-if-preview/SKILL.md hermes_cli/commands.py hermes_cli/main.py hermes_cli/cli_commands_mixin.py cli.py tests/hermes_cli/test_what_if_cli.py tests/hermes_cli/test_commands.py tests/gateway/test_gateway_command_help.py
git commit -m "feat: add terminal what-if preview controls"
```

---

### Task 10: Route What-If Natively Through the Ink TUI

**Files:**
- Modify: `tui_gateway/server.py`
- Modify: `ui-tui/src/gatewayTypes.ts`
- Modify: `ui-tui/src/app/slash/commands/ops.ts`
- Create: `tests/tui_gateway/test_what_if_rpc.py`
- Create: `ui-tui/src/__tests__/whatIfCommand.test.ts`
- Modify: `ui-tui/src/__tests__/createSlashHandler.test.ts`
- Modify: `ui-tui/src/__tests__/slashParity.test.ts`

**Interfaces:**
- Produces JSON-RPC `what-if.exec` and native `/what-if`/`/dry-run` rendering.
- Consumes `hermes_cli.what_if.run_argv(..., output_mode="structured")` inside the live TUI gateway process.

- [ ] **Step 1: Write RED RPC and native-route tests**

```python
def test_what_if_rpc_is_profile_local_structured_and_zero_effect(rpc, temp_home):
    result = rpc("what-if.exec", {"session_id": "sid-1", "argv": ["compare", "preview-1"]})
    assert result["ok"] is True
    assert result["profile_home"] == str(temp_home)
    assert {"run", "candidates", "comparison", "output"} <= result
    assert result["outward_effect_count"] == 0
```

```typescript
it('routes /what-if through native what-if.exec and never slash.exec', () => {
  findSlashCommand('what-if')!.run('compare preview-1', ctx, '/what-if compare preview-1')
  expect(ctx.gateway.rpc).toHaveBeenCalledWith('what-if.exec', {
    argv: ['compare', 'preview-1'], session_id: 'sid-1'
  })
  expect(ctx.gateway.gw.request).not.toHaveBeenCalledWith('slash.exec', expect.anything())
})
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/tui_gateway/test_what_if_rpc.py -q`

Expected: FAIL with unknown RPC method.

Run: `cd ui-tui && npm test -- --run src/__tests__/whatIfCommand.test.ts src/__tests__/slashParity.test.ts`

Expected: FAIL because no native What-If command exists.

- [ ] **Step 3: Implement bounded live-process RPC**

Validate list argv, 64 entries, 64 KiB total, UTF-8 strings, active session/profile, and no request-supplied profile override. Call the shared service and return `{ok, action, output, run, candidates, predictions, comparison, acceptance, receipt, outward_effect_count}`. Validation/conflict errors use JSON-RPC 4xxx; storage/provider errors use 5xxx. Return no traceback, raw file content, recipient, credential, or provider bundle body.

- [ ] **Step 4: Implement native Ink comparison rendering**

Add `what-if` with aliases `dry-run|whatif` to `opsCommands`. Render list/show/compare/receipt as `panel`/`page`; render create/run/revise/select/accept/delete as short `sys` results. Candidate sections show strategy, modeled/unknown field counts, diffs, cost/time bounds, maximum risk, assumptions, unknowns, criterion contributions, Pareto membership, and first irreversible boundaries. Persistent warnings state “prediction, not observed outcome” and “accept creates a transaction; it does not commit.” Keep command-flight/stale-session guards.

- [ ] **Step 5: Enforce native mutation and alias parity**

Add canonical `what-if` and aliases to the local registry tests; add `what-if` to `NATIVE_MUTATING_COMMANDS` and `MUTATING_COMMANDS` because create/revise/select/accept/delete persist state. Prove catalog discovery cannot fall through to `_SlashWorker`, even if the local registry is temporarily missing.

- [ ] **Step 6: Run GREEN and typecheck**

Run: `scripts/run_tests.sh tests/tui_gateway/test_what_if_rpc.py -q`

Expected: PASS.

Run: `cd ui-tui && npm test -- --run src/__tests__/whatIfCommand.test.ts src/__tests__/createSlashHandler.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS; aliases use native RPC and structured predictions render without TypeScript errors.

- [ ] **Step 7: Commit**

```bash
git add tui_gateway/server.py ui-tui/src/gatewayTypes.ts ui-tui/src/app/slash/commands/ops.ts tests/tui_gateway/test_what_if_rpc.py ui-tui/src/__tests__/whatIfCommand.test.ts ui-tui/src/__tests__/createSlashHandler.test.ts ui-tui/src/__tests__/slashParity.test.ts
git commit -m "feat: add native tui what-if comparison"
```

---

### Task 11: Add the Secondary Dashboard Comparison Surface

**Files:**
- Modify: `hermes_cli/web_server.py`
- Modify: `web/src/lib/api.ts`
- Create: `web/src/pages/WhatIfPage.tsx`
- Create: `web/src/pages/WhatIfPage.test.tsx`
- Modify: `web/src/App.tsx`
- Create: `tests/hermes_cli/test_web_server_what_if.py`

**Interfaces:**
- Produces profile-scoped `/api/what-if` read/run/revise/select/accept/receipt endpoints and Dashboard `/what-if`.
- Consumes the same `hermes_cli.what_if.run_argv(..., output_mode="structured")`; Dashboard does not implement simulation/scoring logic.

- [ ] **Step 1: Write RED authenticated/profile-scoped API and page tests**

```python
def test_dashboard_what_if_accept_requires_every_exact_hash(client, seeded_preview):
    body = {
        "run_id": seeded_preview.run_id,
        "expected_run_hash": seeded_preview.run_hash,
        "candidate_id": "safe",
        "expected_candidate_revision": 2,
        "expected_candidate_hash": seeded_preview.candidate_hash,
    }
    response = client.post("/api/what-if/accept?profile=work", json=body, headers=csrf_headers())
    assert response.status_code == 200
    assert response.json()["acceptance"]["transaction_id"]
    assert response.json()["outward_effect_count"] == 0
```

```typescript
it('compares alternatives and accepts only the displayed exact hashes', async () => {
  render(<WhatIfPage />)
  await screen.findByText('Safe local plan')
  expect(screen.getByText(/prediction, not observed outcome/i)).toBeVisible()
  expect(screen.getByText(/first irreversible boundary/i)).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Select safe' }))
  await user.click(screen.getByRole('button', { name: 'Create exact transaction' }))
  expect(api.acceptWhatIf).toHaveBeenCalledWith(expect.objectContaining({
    expected_run_hash: 'run-hash', expected_candidate_hash: 'candidate-hash'
  }))
})
```

Python tests also cover auth/CSRF, invalid/cross-profile name, stale hash, managed scope, request/file/list bounds, redaction, no raw simulator bundle, and no `await` while holding `_profile_scope`.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_web_server_what_if.py -q`

Expected: FAIL with 404 What-If endpoints.

Run: `cd web && npm test -- --run src/pages/WhatIfPage.test.tsx`

Expected: FAIL because page/client methods do not exist.

- [ ] **Step 3: Implement bounded secondary APIs**

Add typed Pydantic bodies for create (1 MiB manifest), run, revise, select, accept, and receipt comparison. Resolve the requested profile synchronously; execute the shared service in `asyncio.to_thread()` with a short `_profile_scope` wholly inside the worker function. Never hold the process-global profile scope across an await. Mutations require existing Dashboard auth/CSRF, exact expected hashes/revisions, and structured size caps.

Add `/api/what-if` to `PROFILE_SCOPED_PREFIXES` in `web/src/lib/api.ts`. Define exact TypeScript response interfaces matching the Ink structured result; do not duplicate state/status vocabularies.

- [ ] **Step 4: Implement the comparison/inspection page**

Add `/what-if` navigation and route. The page lists profile-local runs; displays candidate strategies, diffs, effects, modeled/unknown fields, providers, assumptions, unknowns, cost/time ranges, risk, per-path irreversible boundaries, criteria contributions, hard blockers, and Pareto front; supports run/revise/select and exact transaction creation. Accept confirmation states that it creates a transaction only. Link to the resulting transaction command/ID rather than adding transaction commit controls.

The page is secondary and independent of `/chat`; failures render non-destructively and never disturb the embedded Ink TUI. Do not touch `apps/desktop/`.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_web_server_what_if.py tests/hermes_cli/test_dashboard_auth_middleware.py -q`

Expected: PASS.

Run: `cd web && npm test -- --run src/pages/WhatIfPage.test.tsx src/lib/api.test.ts && npm run typecheck`

Expected: PASS; profile-local Dashboard comparison works and exact hashes reach acceptance.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/web_server.py web/src/lib/api.ts web/src/pages/WhatIfPage.tsx web/src/pages/WhatIfPage.test.tsx web/src/App.tsx tests/hermes_cli/test_web_server_what_if.py
git commit -m "feat: add dashboard what-if comparison"
```

---

### Task 12: Prove Zero Effects, Recovery, Security, Cache Stability, and the 30-Task Gate

**Files:**
- Create: `benchmarks/what_if/runner.py`
- Create: `benchmarks/what_if/scorer.py`
- Modify: `benchmarks/what_if/cases.py`
- Modify: `tests/benchmarks/test_what_if_benchmark.py`
- Create: `tests/hermes_cli/test_what_if_e2e.py`
- Modify: `tests/agent/preview/test_security.py`
- Modify: `tests/agent/preview/test_coordinator.py`
- Modify: `tests/agent/preview/test_acceptance.py`
- Modify: `tests/test_get_tool_definitions_cache_isolation.py`
- Modify: `tests/agent/test_system_prompt.py`
- Modify: `tests/agent/test_turn_finalizer_interrupt_alternation.py`

**Interfaces:**
- Produces `run_benchmark(manifest_path, *, mode, repeats, output_dir) -> BenchmarkRun` and `score_runs(baseline, candidate) -> BenchmarkScore`.
- Consumes only public preview/transaction/authority/receipt/CLI services and frozen Task 0 fixtures; production has no benchmark fault behavior.

- [ ] **Step 1: Write the real-path E2E and crash/restart tests**

```python
def test_three_domain_preview_is_zero_effect_and_acceptance_only_creates_transaction(what_if_e2e):
    before = what_if_e2e.snapshot_all_reality()
    created = what_if_e2e.cli("create --manifest three-domain.yaml")
    what_if_e2e.cli(f"run {created.run_id}")
    compared = what_if_e2e.cli(f"compare {created.run_id}")
    assert what_if_e2e.snapshot_all_reality() == before
    assert what_if_e2e.operation_journal_rows() == []
    assert what_if_e2e.browser_calls == what_if_e2e.cua_calls == what_if_e2e.network_calls == []
    what_if_e2e.cli(
        f"select {created.run_id} safe --expected-comparison-hash {compared.comparison_hash}"
    )
    accepted = what_if_e2e.accept_exact(created.run_id, "safe")
    assert accepted.transaction.status in {"ready", "blocked"}
    assert what_if_e2e.effect_commit_calls == 0


@pytest.mark.parametrize("fault", [
    "after_run_insert", "after_shadow_open", "after_node_prediction", "after_comparison",
    "after_transaction_create", "after_acceptance_insert", "after_transaction_preview",
])
def test_restart_converges_without_effect_or_duplicate(fault, what_if_e2e):
    what_if_e2e.crash_subprocess_at(fault)
    reopened = what_if_e2e.reopen()
    result = reopened.resume_or_inspect()
    assert result.outward_effect_count == 0
    assert reopened.unique_prediction_identities()
    assert reopened.accepted_transaction_count <= 1
```

The harness uses temporary `HERMES_HOME`, real `state.db`, real files and local Git repo/disposable source worktree, real config/workflow/cron stores, real plugin manifest discovery, real CLI parser/service, and local declarative browser/service fixtures. It snapshots source files/modes/hashes, `.git` refs/index/worktree list, workflow DB bytes/logical rows, jobs file, config, checkpoint refs, browser/CUA process counts, sockets/network trap, operation journal, gateway/outbox rows, and environment before/after.

- [ ] **Step 2: Add adversarial source-to-sink security tests**

```python
@pytest.mark.parametrize("attack", [
    "prompt_claims_dry_run", "manifest_requests_commit", "manifest_calls_shell",
    "tool_argument_injects_profile", "source_symlink_swap", "git_hook_execution",
    "browser_localhost_ssrf", "raw_cdp_runtime_evaluate", "cua_type_secret",
    "plugin_python_entrypoint", "bundle_path_escape", "bundle_hash_replay",
    "provider_field_omission", "severity_downgrade", "observable_denominator_shrink",
    "score_weight_overflow", "unknown_as_zero_cost", "approval_text_in_manifest",
    "stale_source_acceptance", "stale_authority_acceptance", "cross_profile_shadow",
])
def test_attack_never_causes_effect_or_false_confidence(security_harness, attack):
    result = security_harness.attempt(attack)
    assert result.outward_effect_count == 0
    assert result.status in {"blocked", "unknown"}
    assert not result.verified
    assert security_harness.no_secret_in_db_logs_output_or_shadow()
```

Threat-model prompt injection, confused delegation, SSRF, replay, privilege drift, compromised plugin manifests, bundle path/hash substitution, source TOCTOU, symlink/hardlink/case-fold escape, Git hooks/filters/submodules, decompression/JSON bombs, score/estimate overflow, severity/field-denominator manipulation, cross-profile access, derived-memory leakage, and acceptance-as-approval confusion. Run real boundary tests, not mocked policy functions alone.

- [ ] **Step 3: Implement report-only baseline/candidate runners and exact scorer**

```python
@dataclass(frozen=True)
class CaseResult:
    case_id: str
    stratum: str
    selected_candidate_id: str | None
    selected_plan_success: bool
    true_positive_fields: int
    false_positive_fields: int
    false_negative_fields: int
    missed_critical: int
    missed_irreversible: int
    severity_weighted_fn_numerator: int
    severity_weighted_expected_denominator: int
    outward_effect_count: int
    elapsed_ms: float
    modeled_cost_micros: int
    excluded_reason: str | None


def precision(results):
    tp = sum(r.true_positive_fields for r in results)
    fp = sum(r.false_positive_fields for r in results)
    return tp / (tp + fp) if tp + fp else 0.0


def recall(results):
    tp = sum(r.true_positive_fields for r in results)
    fn = sum(r.false_negative_fields for r in results)
    return tp / (tp + fn) if tp + fn else 0.0
```

Baseline and candidate receive identical task/candidates, clocks, fixture state, time ceiling, and modeled cost ceiling. Baseline may inspect item #2 single-graph previews but receives no simulated end states/comparison. Candidate uses What-If. The runner refuses missing/extra case IDs, changed manifests, excluded safety cases, unequal budgets, or any outward-effect count. It writes local results only.

`BenchmarkScore` reports 30-case denominator, exclusions/aborts, micro precision/recall and Wilson 95% intervals, each stratum separately, missed critical/irreversible counts, severity-weighted FN loss, paired successes, exact McNemar discordant counts/p-value, p50/p95 time, modeled cost and cost per selected-plan success, unknown rate, and provider coverage. It never replaces these with one composite score.

- [ ] **Step 4: Execute every frozen case as a named pytest case**

```python
MANIFEST, CASES = load_benchmark(ROOT / "benchmarks/what_if/manifest.yaml")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_preregistered_what_if_case(case, benchmark_harness):
    result = benchmark_harness.run_candidate_case(case)
    assert result.outward_effect_count == 0
    assert result.missed_critical == 0
    assert result.missed_irreversible == 0
    assert result.declared_field_denominator == len(case.observable_fields) * len(case.candidates)
```

- [ ] **Step 5: Run RED before final recovery/boundary corrections**

Run: `scripts/run_tests.sh tests/hermes_cli/test_what_if_e2e.py tests/agent/preview/test_security.py tests/benchmarks/test_what_if_benchmark.py -q`

Expected: FAIL at any incomplete crash, boundary, or scorer path; fix the owning module without weakening zero-effect, denominator, or unknown assertions.

- [ ] **Step 6: Run GREEN on E2E and all 30 cases**

Run: `scripts/run_tests.sh tests/hermes_cli/test_what_if_e2e.py tests/agent/preview/test_security.py tests/benchmarks/test_what_if_benchmark.py -q`

Expected: PASS with 30 named cases, zero outward effects, no missed critical/irreversible fields, and immutable denominators.

Run: `uv run python benchmarks/what_if/runner.py --manifest benchmarks/what_if/manifest.yaml --mode baseline --repeats 5 --output-dir build/what-if/baseline`

Expected: exit 0 and write exactly 30 baseline case results with frozen budgets.

Run: `uv run python benchmarks/what_if/runner.py --manifest benchmarks/what_if/manifest.yaml --mode candidate --repeats 5 --output-dir build/what-if/candidate`

Expected: exit 0 and write exactly 30 candidate case results with zero outward effects.

Run: `uv run python benchmarks/what_if/scorer.py --baseline build/what-if/baseline/results.json --candidate build/what-if/candidate/results.json --output build/what-if/report.md`

Expected: exit 0 only if precision and recall are each at least 0.90, missed critical and irreversible effects are zero, severity-weighted FN loss is below the frozen 0.05 bound, and candidate selected-plan success is significantly better by the preregistered one-sided exact McNemar test at `p <= 0.05` under equal budgets. Otherwise exit nonzero and label the proof failed/inconclusive without relaxing a gate.

- [ ] **Step 7: Prove cache, tool-schema, provider/model, and role invariants**

Run a multi-turn real agent fixture and independently hash the system message and effective tool definitions before create, after run, after candidate revision, after selection, and after acceptance. Assert provider/model identity, strict role alternation, compression-only history mutation, and no synthetic user message. Run:

`scripts/run_tests.sh tests/test_get_tool_definitions_cache_isolation.py tests/run_agent -q -k 'system_prompt or tool_schema or cache or alternation'`

Expected: PASS; all four cached identity snapshots remain identical across What-If state changes.

- [ ] **Step 8: Run the focused regression matrix**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/preview \
  tests/agent/effects \
  tests/agent/autonomy \
  tests/agent/test_receipts.py \
  tests/tools/test_checkpoint_manager.py \
  tests/hermes_cli/test_what_if_cli.py \
  tests/hermes_cli/test_what_if_e2e.py \
  tests/hermes_cli/test_web_server_what_if.py \
  tests/hermes_cli/test_workflows_db.py \
  tests/cron/test_jobs.py \
  tests/tui_gateway/test_what_if_rpc.py \
  tests/benchmarks/test_what_if_benchmark.py -q
git diff --check
```

Expected: all tests pass and diff check is clean.

- [ ] **Step 9: Commit**

```bash
git add benchmarks/what_if tests/benchmarks/test_what_if_benchmark.py tests/hermes_cli/test_what_if_e2e.py tests/agent/preview/test_security.py tests/agent/preview/test_coordinator.py tests/agent/preview/test_acceptance.py tests/test_get_tool_definitions_cache_isolation.py tests/agent/test_system_prompt.py tests/agent/test_turn_finalizer_interrupt_alternation.py
git commit -m "test: prove zero-effect what-if previews"
```

---

### Task 13: Add Safe Configuration, Documentation, Retention, and Staged Rollout

**Files:**
- Modify: `hermes_cli/config.py`
- Modify: `tests/hermes_cli/test_config.py`
- Modify: `agent/preview/store.py`
- Modify: `tests/agent/preview/test_store.py`
- Create: `website/docs/user-guide/features/what-if-preview.md`
- Create: `website/docs/developer-guide/what-if-simulator-bundles.md`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`
- Modify: `website/sidebars.ts`
- Modify: `tests/benchmarks/test_what_if_benchmark.py`

**Interfaces:**
- Produces stable `what_if` config, `PreviewStore.prune_expired()`, rollout/rollback contract, operator guide, and simulator-bundle guide.
- No telemetry, gateway command, Desktop dependency, new model tool, or executable simulator extension point.

- [ ] **Step 1: Write RED config, retention, and rollout tests**

```python
def test_what_if_defaults_are_bounded_and_non_secret():
    assert load_config()["what_if"] == {
        "mode": "off",
        "max_candidates": 5,
        "max_nodes_per_candidate": 100,
        "max_shadow_files": 2000,
        "max_shadow_bytes": 268435456,
        "max_single_file_bytes": 16777216,
        "max_branch_states": 32,
        "retention_days": 14,
        "allow_declarative_plugin_simulators": True,
    }


def test_invalid_mode_and_unbounded_values_fall_back_safe(tmp_config):
    tmp_config.write_text("what_if:\n  mode: execute\n  max_candidates: 1000000\n")
    cfg = load_config()["what_if"]
    assert cfg["mode"] == "off"
    assert cfg["max_candidates"] == 5


def test_prune_deletes_only_expired_terminal_shadows(preview_store, clock):
    seed_active_ready_and_expired_runs(preview_store, clock)
    result = preview_store.prune_expired(now_ms=clock.now_ms())
    assert result.deleted_run_ids == ("expired-terminal",)
    assert preview_store.get_run("active-running")
    assert preview_store.get_acceptance("accepted-retained")
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_config.py tests/agent/preview/test_store.py tests/benchmarks/test_what_if_benchmark.py -q`

Expected: FAIL because defaults, retention, and rollout metadata do not exist.

- [ ] **Step 3: Add safe config validation and explicit mode semantics**

Add the exact defaults above under `DEFAULT_CONFIG` without writing them into existing files. Accept only:

- `off`: list/show/receipt/doctor existing records; create/run/revise/select/accept disabled;
- `shadow`: create/run/compare/revise/select/receipt enabled; acceptance disabled;
- `accept`: all bounded commands enabled, but acceptance still only creates/fresh-previews an item #2 transaction.

Validate candidates `2..10`, nodes `1..500`, files `1..20_000`, bytes `1 MiB..2 GiB`, single-file bytes `1 KiB..256 MiB`, branches `1..128`, retention `1..365`, and boolean provider flag. Invalid values fall back to the safe exact defaults and emit a local warning. Add no environment bridge.

`prune_expired()` marks old nonaccepted terminal runs expired, deletes their shadow directories after canonical containment verification, and then deletes DB rows in bounded batches. Never prune running runs, accepted bindings, receipts, transactions, provider bundles, unknown recovery evidence, or paths outside `preview-shadow`.

- [ ] **Step 4: Write the complete user/operator guide**

Document the layman outcome and bounded design; one copyable three-candidate CLI/Ink walkthrough; manifest grammar; material difference; exact effect-field/severity enumeration; filesystem/Git/workflow/cron/config coverage; instrumented provider setup; modeled/partial/unknown; cost/time ranges; risk/assumptions/unknowns; irreversible boundaries; dependency-aware revision; criteria/Pareto/scalar contribution display; select/accept exact hashes; fresh transaction preview/current authority/approval; receipts/observed comparison; profile storage/export/delete/retention; modes; local 30-case benchmark; and recovery/doctor commands.

State prominently: dry run performs no outward effect; browser/CUA are never live; arbitrary people/markets/websites/services are not simulated; acceptance is not commit/approval/verification; a predicted receipt is `completed_unverified`; unsupported fields are unknown; no shell, remote push, production DB, account deletion, purchase, message dispatch, gateway parity, Desktop parity, or exactly-once claim.

- [ ] **Step 5: Write the data-only simulator-bundle guide**

Document standalone plugin layout, manifest/bundle schemas, canonical transition keys, observable fields/severity, SHA-256/path/size/state limits, finite transitions, range/assumption/unknown rules, no code execution/network/credentials, browser/service instrumented fixtures, version upgrades, provider coverage reports, profile discovery, and required real-path/egress tests. Explain that an executable simulator is not accepted until a separately approved OS-isolation contract exists; vendors publish standalone plugins, not core plugin directories.

- [ ] **Step 6: Define staged rollout and stop/rollback gates**

1. Ship additive schema/read paths with `what_if.mode: off`; run unit/E2E and inspect no-effect traces.
2. Enable `shadow` for the frozen 30-case corpus; verify every source snapshot and egress counter remains unchanged.
3. Keep `shadow` for at least 30 additional real CLI/Ink dry runs across the three approved strata using user-authorized data/designated fixtures; Dashboard is inspection secondary.
4. Advance to `accept` only after the exact Task 0 gates pass: precision/recall each at least 90%, zero missed critical/irreversible effects, severity-weighted FN loss below the frozen 0.05 bound, significant selected-plan improvement under equal budget, and zero outward dry-run effects.
5. Stop immediately on any source mutation, network/browser/CUA/tool-handler call, operation-journal row from dry run, false `verified`, omitted declared field, severity downgrade, cross-profile access, stale acceptance, approval reuse, unmodeled irreversible boundary, cache/tool/provider/model drift, role violation, or provider code execution.
6. Roll back by setting `what_if.mode: off`, leaving accepted transactions/receipts intact for diagnosis, and pruning only eligible preview shadow state through the explicit command. Never delete `state.db`, transaction evidence, or past conversation history.

- [ ] **Step 7: Run final GREEN matrix and docs builds**

Run: `scripts/run_tests.sh tests/hermes_cli/test_config.py tests/agent/preview tests/hermes_cli/test_what_if_cli.py tests/hermes_cli/test_what_if_e2e.py tests/hermes_cli/test_web_server_what_if.py tests/tui_gateway/test_what_if_rpc.py tests/benchmarks/test_what_if_benchmark.py -q`

Expected: PASS.

Run: `cd ui-tui && npm test -- --run src/__tests__/whatIfCommand.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS.

Run: `cd web && npm test -- --run src/pages/WhatIfPage.test.tsx src/lib/api.test.ts && npm run typecheck`

Expected: PASS.

Run: `cd website && npm run lint:diagrams && npm run typecheck && npm run build`

Expected: PASS; What-If user and developer pages build with resolved links.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 8: Commit**

```bash
git add hermes_cli/config.py tests/hermes_cli/test_config.py agent/preview/store.py tests/agent/preview/test_store.py website/docs/user-guide/features/what-if-preview.md website/docs/developer-guide/what-if-simulator-bundles.md website/docs/reference/cli-commands.md website/docs/reference/slash-commands.md website/sidebars.ts tests/benchmarks/test_what_if_benchmark.py
git commit -m "docs: gate what-if preview rollout"
```

---

## Final Verification Matrix

| Requirement | Proof |
|---|---|
| Materially different alternatives | Semantic graph fingerprints reject cosmetic duplicates; exact candidate revisions/hashes |
| Bounded filesystem/Git shadow | Declared-path overlay/blob limits; source file/index/HEAD/ref/worktree/checkpoint snapshots unchanged |
| Workflow/cron/config simulation | Pure copies, workflow engine reachability, no DB/jobs/config mutation or dispatch |
| Browser/CUA/service boundary | Live handler denylist, zero egress/process/tool calls, data-only exact fixture transitions, unknown fallback |
| Predicted diffs/cost/time/risk | Per-effect fields, bounded estimates with basis, visible severity/risk and source hashes |
| Assumptions and unknowns | Mandatory provenance-bearing tuples; unknown propagation through dependencies |
| Irreversible boundaries | First boundary for every reachable path; unmodeled boundaries block selection |
| Dependency-aware revision | Immutable candidate revisions; changed nodes and descendants invalidated; exact safe reuse hashes |
| Explicit comparison | Persisted criteria, hard floors, visible weighted contributions, Pareto front, no hidden auto-selection |
| Zero outward dry-run effect | Poison callbacks, egress traps, operation-journal absence, before/after full reality snapshots |
| Accepted preview transition | Source/current-authority recheck, deterministic transaction ID, exact graph binding, fresh transaction preview, no commit |
| Approval remains fresh | Acceptance creates/consumes no approval; item #2 exact binding required at commit |
| Truthful receipts | Prediction receipt always `completed_unverified`; immutable observed comparison observations |
| Exact 30-task gate | 10 filesystem, 10 workflow, 5 browser, 5 service tasks with frozen observable fields/severity |
| Metrics and false-confidence floor | Precision/recall ≥90%, zero critical/irreversible miss, weighted FN loss <0.05, no denominator shrink |
| Better plan selection | Same-budget no-simulator baseline and preregistered one-sided exact McNemar `p <= 0.05` |
| Crash/replay/recovery | Seven fault boundaries, immutable/idempotent persistence, at most one accepted transaction |
| Security/privacy/profile isolation | Real source-to-sink adversarial cases, no secrets/bundle code/cross-profile paths |
| Cache and conversation invariants | Independent system/tool/provider/model hashes, role/history assertions across all lifecycle changes |
| Primary/secondary surfaces | Top-level/classic CLI and native Ink primary; Dashboard secondary; no gateway/Desktop files |
| Narrow-waist delivery | Existing effect/authority/receipt/plugin-manifest seams, CLI + skill, no model tool/schema change |

Do not call Plan Preview & What-If Dry Run complete until fresh evidence proves every matrix row, all 30 frozen cases, every zero-effect snapshot, focused Python/Ink/Dashboard/docs suites, and `git diff --check` from a clean checkout. If the selected-plan significance test is underpowered, increase the preregistered sample and rerun; do not relax or reinterpret the gate after results.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-plan-preview-what-if-dry-run.md`. Two execution options:

1. **Subagent-Driven (recommended)** — use `superpowers:subagent-driven-development`, one fresh implementation subagent per task with review between tasks.
2. **Inline Execution** — use `superpowers:executing-plans`, execute task batches with explicit checkpoints.
