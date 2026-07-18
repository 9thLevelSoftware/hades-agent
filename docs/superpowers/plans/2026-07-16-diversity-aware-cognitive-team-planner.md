# Diversity-Aware Cognitive Team Planner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a terminal-first planner that often chooses one capable agent, but can create, verify, and adapt a one-or-two-agent team when measured capability diversity improves verified outcomes within fixed dollar, wall-clock, and coordination budgets.

**Architecture:** Add a pure planner beside existing delegation, MoA, mission, Kanban, and async-delivery code. The planner owns decomposition, typed deliverables, topology, verification arrangement, bounded shared context, and immutable replanning decisions; item #10 continues to choose each execution unit's provider/model/effort, item #12 alone establishes verified outcomes, and item #1 carries durable mission execution state. Plans project onto existing Kanban tasks/comments/runs and delegate-child construction, never onto a second scheduler, receipt store, router, mailbox, or model-visible tool.

**Tech Stack:** Python 3.13, frozen dataclasses and `typing.Literal`, SQLite/WAL through the existing mission/Kanban/SessionDB stores, item #10 `plugins.auto_routing`, item #12 `agent.receipts`, existing delegate/async-delegation/MoA/Kanban seams, Rich classic CLI, Ink/TypeScript JSON-RPC, React Dashboard inspection, pytest through `scripts/run_tests.sh`, Vitest, and YAML benchmark manifests.

## Approved Portfolio Contract

- **Layman outcome:** Hermes decides when one agent is enough and when a carefully chosen team of genuinely different models or capabilities is worth the overhead, then assigns, checks, and revises that work automatically.
- **Design boundary:** The Team Planner owns decomposition, topology, typed units/deliverables, evidence requirements, shared-context policy, verification arrangement, merge readiness, and replanning. The Adaptive Intelligence Router (#10) exclusively chooses runtime/provider/model/effort for an assigned unit. Verified Outcome & Artifact Receipts (#12) exclusively owns proof and the five receipt statuses. Durable Missions (#1) exclusively owns long-lived objective and execution state. Persona names, prompts, provider labels, or different random seeds do not constitute diversity without fresh measured capability differences and lower correlated-error risk.
- **90-day proof:** Preregister exactly 200 stratified tasks and compare current one-agent Hermes, four homogeneous agents, and the adaptive one-or-two-agent planner under identical per-task dollar and wall-clock ceilings. Pass only with at least five percentage points higher verified success than both fixed baselines, or non-inferior verified success versus both with at least 25% lower cost; every high-risk floor must pass, at least 30% of tasks must choose one agent, team-mode correlated error must be materially lower than the homogeneous baseline, and every task must respect its coordination allocation.
- **Dependencies:** Consume item #10 `AutoRoutingService.decide(RoutingRequest) -> RoutingDecision`, item #12 `ReceiptIssuer.issue(ReceiptSourceKey) -> Receipt` and immutable rechecks, and item #1 mission records/events/execution links. If any dependency is unavailable or incompatible, simulation remains available but automatic team execution fails closed.
- **Failure conditions:** More agents, opinions, tokens, latency, or UI without better verified outcomes is a regression. Any router bypass, self-verification, fake diversity, receipt-status invention, stale-evidence promotion, coordination-budget overrun, primary cache-lineage mutation, or high-risk floor violation stops rollout.
- **Delivery:** Footprint Ladder rung 1. Extend existing delegate/MoA/Kanban orchestration and terminal surfaces; add no model-visible core tool or tool-schema field. Alternative planning algorithms may later ship as standalone plugins after a concrete provider needs a generic algorithm registry; v1 ships one deterministic built-in algorithm and no speculative plugin hook.

## Global Constraints

- Preserve a byte-stable system prompt, effective model tool-definition snapshot, provider, and model for every conversation cache lineage; a reassign starts a fresh child execution and never mutates a running child's cache identity.
- Preserve strict message-role alternation. Planner, mailbox, and completion records are local state/activity events, not synthetic mid-loop user messages; existing async completion delivery may create a new turn only when its owner session is idle.
- Add no model-visible core tool and do not change `delegate_task`, Kanban tool, or MoA JSON schemas. Primary controls are `hermes team ...`, `/team ...`, and native Ink RPC/rendering.
- Keep stable behavior under `team_planner:` in profile-local `config.yaml`. Credentials remain in secret stores or `.env`; no new user-facing `HERMES_*` variable is introduced.
- Profiles remain independent islands. All planner state, router evidence, receipts, artifacts, and Kanban boards resolve from the active `HERMES_HOME`; cross-profile plans fail before persistence.
- One agent is the default topology until measured marginal verification value exceeds measured coordination cost. V1 may run at most two cognitive agents for the adaptive arm; an independent deterministic receipt scorer is not counted as a cognitive agent.
- A team is diverse only when routed units have different `RuntimeKey.stable_id()` values and a fresh, sufficiently sampled capability-vector distance. Provider/model strings, roles, personas, temperatures, and prompt variations alone are not evidence.
- Hard authority, privacy, residency, modality, capability, provider availability, dollar, wall-clock, and reasoning constraints filter before topology scoring and cannot be weakened by the planner.
- Item #10 persists every `RoutingDecision` before projection. Planner rows store only decision IDs, non-secret stable runtime IDs, capability buckets, and normalized scores; never credentials, endpoints, prompts, or raw responses.
- Item #12 alone may seal `verified`. Planner/worker success, consensus, majority vote, verifier prose, artifact existence, and a Kanban `done` state yield at most `completed_unverified` until an independent scorer issues the canonical receipt.
- Item #1 mission events and existing Kanban tasks/links/runs/comments remain runtime sources of truth. Planner revisions are immutable intent/projection records, not another workflow engine, retry queue, worker state machine, or mailbox.
- Shared context is bounded, typed, provenance-bearing, and compiled only for a newly created child. It contains deliverable/evidence references and redacted summaries, not another worker's hidden reasoning or mutable transcript.
- Existing `IterationBudget`, session cost rollup, delegation concurrency/depth caps, async delegation operation journal, active-subagent registry, `/stop`, and `subagent.interrupt` remain authoritative. The planner reserves from them; it never maintains competing counters.
- Real-path E2E tests use a temporary `HERMES_HOME`, real imports, real SQLite databases, real Kanban graph operations, real temp Git/files, fresh object graphs or subprocess restarts, and item #12 artifact hashing. Mock only provider/network completions, monotonic clock/randomness, and process termination.
- Run Python tests only through `scripts/run_tests.sh`. Each task starts with a focused failing behavior test, records RED, adds the smallest complete implementation, records GREEN and relevant regressions, runs `git diff --check`, and ends in exactly one conventional commit.

---

## Current Code Map

- `tools/delegate_tool.py:138-208,804-1037,1053-1460,1788-2387,2411-3084` owns live child registration/interruption, child construction, progress, provider/model overrides, parallel execution, result shaping, cost rollup, summary budgets, and background dispatch. It remains the only in-process delegate executor.
- `tools/async_delegation.py:110-1340` and `hermes_state.py:904-926` own durable background dispatch/completion identity, operation-journal recovery, delivery claim/acknowledgement, the shared completion queue, and `/agents` status. This is the planner's asynchronous mailbox lifecycle; no second completion queue is added.
- `agent/iteration_budget.py:17-57` owns thread-safe per-agent iteration use/refund. `session_estimated_cost_usd` and child cost rollup in `tools/delegate_tool.py:2174-2300,2786-2841` remain cost truth.
- `agent/moa_loop.py:264-785` owns parallel reference calls, per-runtime resolution/accounting, context shaping, and aggregator synthesis. It remains an explicit `/moa` mode; the team planner may reuse its pure correlation/accounting helpers after extraction but does not route through a virtual `provider="moa"` or duplicate its prompt loop.
- `hermes_cli/kanban_db.py:839-1099,1107-1284,2405-2935,3940-4220,5240-5462,5945-7825,8090-8585` owns durable tasks, links, runs, comments, artifacts, assignments, provider/model projections, dispatch/recovery, and bounded worker context.
- `hermes_cli/kanban_swarm.py:29-269` already creates worker/verifier/synthesizer graphs and uses structured root-task comments as a blackboard. The planner generalizes its fixed topology through typed metadata and reuses comments rather than adding a mailbox table.
- `hermes_cli/kanban_decompose.py:127-458` already turns one triage task into a fixed LLM-proposed graph. It remains a user-directed decomposition command; adaptive planning uses the pure estimator and never silently invokes a self-authoring decomposition model.
- `plugins/auto_routing/auto_routing/service.py` (item #10) owns `AutoRoutingService.decide`; `models.py` owns `RuntimeKey`, `TaskAssessment`, `RoutingTarget`, `RoutingDecision`, and `EvidenceEvent`; `decisions.py` persists decisions before projection. The planner imports only the roadmap's stable public names.
- `hermes_cli/missions_db.py` and mission events/execution links (item #1) own durable objective, constraints, authority version, status, and terminal receipt projection.
- `agent/receipts.py` (item #12) owns `ReceiptStatus`, `ReceiptSourceKey`, `Receipt`, `ReceiptObservation`, `ReceiptStore`, `ReceiptIssuer`, content hashing, independent scorer seals, and artifact digests.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `hermes_cli/cli_commands_mixin.py`, and `cli.py` own top-level/classic command discovery and dispatch.
- `tui_gateway/server.py` and `ui-tui/src/app/slash/commands/ops.ts` own native operational JSON-RPC and Ink rendering. `hermes_cli/web_server.py` and `web/src/App.tsx` own authenticated, profile-scoped secondary Dashboard inspection.

## Proposed File Map

### New production files

- `agent/team_planner/__init__.py` — stable public exports only.
- `agent/team_planner/models.py` — frozen assessment, unit, edge, deliverable, evidence, topology, budget, revision, outcome, and explanation values.
- `agent/team_planner/assessment.py` — deterministic decomposability, parallelism, verification-value, correlated-error-risk, and coordination-cost estimates.
- `agent/team_planner/capabilities.py` — receipt/router-derived capability vectors, sample/freshness/confidence gates, distance, and pairwise error correlation.
- `agent/team_planner/algorithm.py` — one deterministic bounded optimizer; explicit one-agent candidate; no provider/model selection.
- `agent/team_planner/service.py` — composition root for assess/simulate/plan/explain/replan.
- `agent/team_planner/projection.py` — idempotent mission/Kanban projection and delegate execution adapter.
- `agent/team_planner/context.py` — bounded typed context compiler over Kanban comments, parent summaries, artifacts, and receipt references.
- `agent/team_planner/verification.py` — central-versus-cross verification arrangement and receipt source construction.
- `agent/team_planner/adaptation.py` — shrink/expand/reassign rules and immutable revision CAS.
- `hermes_cli/team_planner.py` — shared top-level/classic CLI parser, guarded preview/apply, text/JSON renderers.
- `benchmarks/team_planner/manifest.yaml` — frozen exact 200-task, three-arm, equal-budget contract.
- `benchmarks/team_planner/cases.py` — strict manifest loader and stratified case expansion.
- `benchmarks/team_planner/runner.py` — local three-arm runner and disaggregated report.
- `website/docs/user-guide/features/cognitive-team-planner.md` — operator guide and rollout/stop rules.
- `website/docs/development/team-planner-contract.md` — consumed contracts and algorithm boundary.
- `web/src/pages/TeamPlannerPage.tsx` — secondary read-only plan/revision/evidence inspector.

### Existing production files modified

- `hermes_cli/missions_db.py` — immutable team-plan revisions, active-revision CAS, mission-event linkage; no execution state duplication.
- `hermes_cli/kanban_db.py` — typed planner metadata validation/projection helpers while retaining task/link/run/comment ownership.
- `hermes_cli/kanban_swarm.py` — delegate fixed swarm creation to the generic typed projector and preserve public behavior.
- `tools/delegate_tool.py` — one internal `run_planned_child` adapter and non-secret planner correlation fields; tool definition/schema byte-identical.
- `tools/async_delegation.py` — attach/recover planner unit/revision IDs in existing durable completion records; no new queue/table.
- `hermes_cli/config.py` — validated `team_planner` settings and defaults.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `hermes_cli/cli_commands_mixin.py`, `cli.py` — `team`/`teams` command routing.
- `tui_gateway/server.py`, `ui-tui/src/gatewayTypes.ts`, `ui-tui/src/app/slash/commands/ops.ts` — native `team.exec` RPC and Ink plan/explain/simulate/status views.
- `hermes_cli/web_server.py`, `web/src/lib/api.ts`, `web/src/App.tsx` — authenticated read-only Dashboard plan endpoints and route.
- `website/docs/reference/cli-commands.md`, `website/docs/reference/slash-commands.md`, `website/sidebars.ts` — commands and navigation.

### Focused tests

- `tests/agent/team_planner/test_models.py`
- `tests/agent/team_planner/test_assessment.py`
- `tests/agent/team_planner/test_capabilities.py`
- `tests/agent/team_planner/test_algorithm.py`
- `tests/agent/team_planner/test_projection.py`
- `tests/agent/team_planner/test_execution.py`
- `tests/agent/team_planner/test_context.py`
- `tests/agent/team_planner/test_verification.py`
- `tests/agent/team_planner/test_adaptation.py`
- `tests/agent/team_planner/test_security.py`
- `tests/hermes_cli/test_team_planner_cli.py`
- `tests/hermes_cli/test_team_planner_e2e.py`
- `tests/tui_gateway/test_team_planner_rpc.py`
- `tests/hermes_cli/test_team_planner_dashboard.py`
- `tests/benchmarks/test_team_planner_manifest.py`
- `tests/benchmarks/test_team_planner_runner.py`
- `ui-tui/src/__tests__/teamPlannerCommand.test.ts`
- `web/src/pages/TeamPlannerPage.test.tsx`

## Frozen Produced and Consumed Interfaces

The package `agent.team_planner` exports the following names. Consumer code does not import sibling private helpers.

```python
TopologyKind = Literal["one_agent", "central_verification", "cross_verification"]
UnitKind = Literal["execute", "verify", "merge"]
UnitState = Literal["planned", "routed", "running", "completed", "blocked", "failed", "cancelled"]
ReplanAction = Literal["keep", "shrink", "expand", "reassign", "stop"]

@dataclass(frozen=True)
class TeamTaskAssessment:
    decomposability: Decimal
    parallelism: Decimal
    verification_value: Decimal
    coordination_cost: Decimal
    correlated_error_risk: Decimal
    risk_class: Literal["low", "normal", "high"]
    required_capabilities: tuple[str, ...]
    explanation_codes: tuple[str, ...]

@dataclass(frozen=True)
class TeamBudget:
    dollar_limit: Decimal
    wall_clock_seconds: int
    iteration_limit: int
    coordination_dollar_limit: Decimal
    coordination_seconds_limit: int

@dataclass(frozen=True)
class DeliverableSpec:
    deliverable_id: str
    kind: str
    description: str
    media_type: str | None
    required_claims: tuple[str, ...]
    artifact_required: bool

@dataclass(frozen=True)
class EvidenceRequirement:
    requirement_id: str
    claim_kind: str
    scorer_id: str
    max_age_seconds: int
    independent_of_unit_ids: tuple[str, ...]

@dataclass(frozen=True)
class TeamUnitSpec:
    unit_id: str
    unit_kind: UnitKind
    objective: str
    required_capabilities: tuple[str, ...]
    deliverable_ids: tuple[str, ...]
    evidence_requirement_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    max_iterations: int

@dataclass(frozen=True)
class TeamPlanRevision:
    plan_id: str
    revision: int
    mission_id: str
    parent_revision: int | None
    topology: TopologyKind
    assessment: TeamTaskAssessment
    budget: TeamBudget
    units: tuple[TeamUnitSpec, ...]
    deliverables: tuple[DeliverableSpec, ...]
    evidence_requirements: tuple[EvidenceRequirement, ...]
    routing_decision_ids: tuple[str, ...]
    content_hash: str
    created_at: str

@dataclass(frozen=True)
class CapabilityVector:
    runtime_stable_id: str
    dimensions: tuple[tuple[str, Decimal], ...]
    sample_count: int
    confidence: Decimal
    fresh_until: str
    evidence_receipt_ids: tuple[str, ...]

@dataclass(frozen=True)
class PartialTeamOutcome:
    unit_id: str
    state: UnitState
    receipt_id: str | None
    receipt_status: ReceiptStatus | None
    routing_decision_id: str
    cost_usd: Decimal
    elapsed_seconds: float
    evidence_fresh: bool

@dataclass(frozen=True)
class ReplanDecision:
    action: ReplanAction
    reason_codes: tuple[str, ...]
    next_revision: TeamPlanRevision | None

@dataclass(frozen=True)
class TeamTaskFacts:
    independent_units: int
    dependency_depth: int
    shared_write_roots: int
    independently_scorable_claims: int
    ambiguity: Decimal
    risk_class: Literal["low", "normal", "high"]
    expected_tokens: int
    expected_artifacts: int
    required_capabilities: tuple[str, ...]

@dataclass(frozen=True)
class TeamPlanRequest:
    mission_id: str
    mission_authority_version: int
    objective: str
    constraints: tuple[str, ...]
    facts: TeamTaskFacts
    budget: TeamBudget
    requested_deliverables: tuple[DeliverableSpec, ...]

@dataclass(frozen=True)
class CorrelationEstimate:
    value: Decimal | None
    matched_cases: int
    confidence: Decimal
    unknown_reason: str | None

    @property
    def unknown(self) -> bool: ...

@dataclass(frozen=True)
class TeamPlanExplanation:
    plan_id: str
    revision: int
    topology: TopologyKind
    selected_agent_count: int
    assessment: TeamTaskAssessment
    reason_codes: tuple[str, ...]
    routing_decision_ids: tuple[str, ...]
    diversity_distance: Decimal | None
    correlation: CorrelationEstimate
    budget: TeamBudget

@dataclass(frozen=True)
class ProjectionResult:
    plan_id: str
    revision: int
    task_ids: tuple[str, ...]
    created_task_count: int
    reused_task_count: int
    active: bool

@dataclass(frozen=True)
class TeamContextItem:
    producer_unit_id: str
    deliverable_ids: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    observed_at: str
    fresh: bool
    redacted_summary: str
    content_hash: str

@dataclass(frozen=True)
class TeamContextBundle:
    plan_id: str
    revision: int
    consumer_unit_id: str
    items: tuple[TeamContextItem, ...]
    source_receipt_ids: tuple[str, ...]
    rendered: str
    content_hash: str

@dataclass(frozen=True)
class VerificationAssignment:
    topology: TopologyKind
    assignments: tuple[tuple[str, str], ...]
    central_verifier_unit_id: str | None
    reason_codes: tuple[str, ...]

@dataclass(frozen=True)
class MergeReadiness:
    ready: bool
    status: ReceiptStatus
    reason_codes: tuple[str, ...]

@dataclass(frozen=True)
class BudgetReservation:
    plan_id: str
    revision: int
    unit_id: str
    task_index: int
    dollar_reserved: Decimal
    wall_clock_seconds_reserved: int
    iterations_reserved: int

@dataclass(frozen=True)
class PlannedChildResult:
    plan_id: str
    revision: int
    unit_id: str
    routing_decision_id: str
    runtime_stable_id: str
    state: UnitState
    summary: str
    receipt_id: str | None
    cost_usd: Decimal
    elapsed_seconds: float

class TeamPlannerService:
    def simulate(self, request: TeamPlanRequest) -> TeamPlanExplanation: ...
    def create(self, request: TeamPlanRequest, *, expected_mission_version: int) -> TeamPlanRevision: ...
    def explain(self, plan_id: str, revision: int | None = None) -> TeamPlanExplanation: ...
    def replan(self, plan_id: str, outcomes: tuple[PartialTeamOutcome, ...], *, expected_revision: int) -> ReplanDecision: ...

class TeamPlanProjector:
    def apply(self, revision: TeamPlanRevision, *, expected_hash: str) -> ProjectionResult: ...
    def recover(self, plan_id: str) -> ProjectionResult: ...
```

Consumed interfaces remain owned elsewhere and are never redefined:

```python
AutoRoutingService.decide(request: RoutingRequest) -> RoutingDecision
RoutingStore.record_decision(decision: RoutingDecision) -> None
ReceiptIssuer.issue(source: ReceiptSourceKey) -> Receipt
ReceiptIssuer.recheck(receipt_id: str) -> ReceiptObservation
ReceiptStore.get(receipt_id: str) -> Receipt | None
create_mission_and_execution(...) -> tuple[MissionRecord, WorkflowExecution]
mission_for_execution(conn, execution_id: str) -> MissionRecord | None
kanban_db.create_task(...) -> Task
kanban_db.link_tasks(conn, parent_id: str, child_id: str) -> None
kanban_db.complete_task(...) -> bool
kanban_db.add_comment(conn, task_id: str, author: str, body: str) -> int
delegate_tool.list_active_subagents() -> list[dict[str, object]]
async_delegation.list_async_delegations() -> list[dict[str, object]]
```

---

### Task 0: Preregister the Exact 200-Task Equal-Budget Proof

**Files:**
- Create: `benchmarks/team_planner/manifest.yaml`
- Create: `benchmarks/team_planner/cases.py`
- Create: `tests/benchmarks/test_team_planner_manifest.py`

**Interfaces:**
- Consumes: portfolio default workflow archetypes and item #12 canonical `ReceiptStatus`.
- Produces: `load_team_planner_cases(path: Path) -> tuple[TeamPlannerBenchmarkManifest, tuple[TeamPlannerCase, ...]]`, `BENCHMARK_ARMS`, and an immutable 200-case denominator used by Task 12.

- [ ] **Step 1: Write the failing manifest-contract test**

```python
def test_manifest_freezes_exact_equal_budget_contract():
    manifest, cases = load_team_planner_cases(MANIFEST)
    assert len(cases) == 200
    assert Counter(c.archetype for c in cases) == {
        "software_maintenance": 40,
        "sourced_research": 40,
        "data_artifact": 40,
        "recovery_operations": 40,
        "mixed_high_risk": 40,
    }
    assert manifest.arms == ("one_agent", "four_homogeneous", "adaptive_team")
    assert manifest.gates.verified_success_gain_pp == Decimal("5")
    assert manifest.gates.noninferiority_margin_pp == Decimal("2")
    assert manifest.gates.cost_reduction_fraction == Decimal("0.25")
    assert manifest.gates.minimum_one_agent_fraction == Decimal("0.30")
    assert manifest.gates.maximum_coordination_fraction == Decimal("0.15")
    assert all(c.dollar_limit > 0 and c.wall_clock_seconds > 0 for c in cases)
    assert all(c.arm_budget("one_agent") == c.arm_budget("adaptive_team") for c in cases)
```

Also assert 80 preregistered high-risk cases cover stale evidence, privacy/residency, unavailable providers, adversarial handoffs, ambiguous effects, and budget exhaustion; exclusions/aborts remain denominator failures; scorer/version, hardware/network class, randomized arm order seed `20260716`, cost source, and expected end states are frozen before any run.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_team_planner_manifest.py -q`

Expected: FAIL importing `benchmarks.team_planner.cases` because the loader and manifest do not exist.

- [ ] **Step 3: Implement the strict loader and exact gates**

```python
BENCHMARK_ARMS = ("one_agent", "four_homogeneous", "adaptive_team")

@dataclass(frozen=True)
class TeamPlannerGates:
    verified_success_gain_pp: Decimal = Decimal("5")
    noninferiority_margin_pp: Decimal = Decimal("2")
    cost_reduction_fraction: Decimal = Decimal("0.25")
    minimum_one_agent_fraction: Decimal = Decimal("0.30")
    maximum_coordination_fraction: Decimal = Decimal("0.15")
    correlated_error_relative_reduction: Decimal = Decimal("0.20")

@dataclass(frozen=True)
class TeamPlannerCase:
    case_id: str
    archetype: str
    risk_class: Literal["low", "normal", "high"]
    dollar_limit: Decimal
    wall_clock_seconds: int
    scorer_id: str
    expected_end_state_hash: str

    def arm_budget(self, arm: str) -> tuple[Decimal, int]:
        if arm not in BENCHMARK_ARMS:
            raise ValueError(f"unknown benchmark arm: {arm}")
        return self.dollar_limit, self.wall_clock_seconds

@dataclass(frozen=True)
class TeamPlannerBenchmarkManifest:
    version: str
    arms: tuple[str, ...]
    seed: int
    hardware_class: str
    network_class: str
    gates: TeamPlannerGates

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "TeamPlannerBenchmarkManifest":
        return cls(
            version=str(raw["version"]),
            arms=tuple(str(v) for v in raw["arms"]),
            seed=int(raw["seed"]),
            hardware_class=str(raw["hardware_class"]),
            network_class=str(raw["network_class"]),
            gates=TeamPlannerGates(**raw["gates"]),
        )

    def validate(self, cases, *, exact_count: int, arms: tuple[str, ...]) -> None:
        if len(cases) != exact_count or self.arms != arms:
            raise ValueError("benchmark denominator or arms differ from preregistration")
        if len({case.case_id for case in cases}) != exact_count:
            raise ValueError("benchmark case ids must be unique")

def load_team_planner_cases(path: Path):
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest = TeamPlannerBenchmarkManifest.from_mapping(raw)
    cases = tuple(expand_case(c) for c in raw["cases"])
    manifest.validate(cases, exact_count=200, arms=BENCHMARK_ARMS)
    return manifest, cases
```

The YAML gives every case the same dollar and wall-clock cap in all three arms. `four_homogeneous` runs four copies of the one-agent arm's routed runtime plus the same deterministic merge budget. The adaptive arm can choose one or two routed agents. Coordination includes planning, handoff compilation, verification arrangement, and merge calls, and may consume at most 15% of both the task's dollar cap and wall-clock cap. High-risk floors are exact: zero authority/privacy/residency violations, zero unapproved irreversible effects, zero stale/unknown/failed outcomes labeled verified, zero duplicate effects after replay, and 100% fail-closed behavior when a dependency is unavailable.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_team_planner_manifest.py -q && git diff --check`

Expected: PASS with exactly 200 unique case IDs, 40 cases per stratum, identical arm budgets, and no mutable or missing gate.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/team_planner/manifest.yaml benchmarks/team_planner/cases.py \
  tests/benchmarks/test_team_planner_manifest.py
git commit -m "test: preregister cognitive team planner proof"
```

---

### Task 1: Freeze Planner Models, Hashes, and Estimator Semantics

**Files:**
- Create: `agent/team_planner/__init__.py`
- Create: `agent/team_planner/models.py`
- Create: `agent/team_planner/assessment.py`
- Create: `tests/agent/team_planner/test_models.py`
- Create: `tests/agent/team_planner/test_assessment.py`

**Interfaces:**
- Consumes: item #12 `ReceiptStatus` and `canonical_content_hash`.
- Produces: every frozen type in “Frozen Produced and Consumed Interfaces,” `assess_team_task(facts: TeamTaskFacts) -> TeamTaskAssessment`, and `hash_team_revision(revision_fields: Mapping[str, object]) -> str`.

- [ ] **Step 1: Write RED model and estimator tests**

```python
def test_sequential_low_verification_work_prefers_one_agent_facts():
    assessment = assess_team_task(TeamTaskFacts(
        independent_units=1, dependency_depth=4, shared_write_roots=1,
        independently_scorable_claims=0, ambiguity=Decimal("0.1"),
        risk_class="normal", expected_tokens=4000, expected_artifacts=0,
    ))
    assert assessment.parallelism <= Decimal("0.20")
    assert assessment.verification_value <= Decimal("0.25")
    assert assessment.coordination_cost >= Decimal("0.60")

def test_revision_hash_is_order_stable_and_models_are_frozen(revision_fields):
    assert hash_team_revision(revision_fields) == hash_team_revision(dict(reversed(revision_fields.items())))
    with pytest.raises(FrozenInstanceError):
        revision_fields["assessment"].parallelism = Decimal("1")
```

Parameterize exact boundary cases for values outside `[0,1]`, cycles, missing deliverables, duplicate unit IDs, an evidence requirement depending on its own producer, an iteration allocation above the plan cap, and non-finite decimals.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_models.py tests/agent/team_planner/test_assessment.py -q`

Expected: FAIL importing `agent.team_planner`.

- [ ] **Step 3: Implement immutable models and deterministic estimates**

```python
def assess_team_task(f: TeamTaskFacts) -> TeamTaskAssessment:
    decomposability = clamp01(Decimal(f.independent_units - 1) / Decimal(max(1, f.independent_units)))
    parallelism = clamp01(decomposability * (Decimal("1") - depth_ratio(f)))
    verification_value = clamp01(
        Decimal("0.45") * claim_ratio(f)
        + Decimal("0.35") * f.ambiguity
        + Decimal("0.20") * risk_weight(f.risk_class)
    )
    coordination_cost = clamp01(
        Decimal("0.35") * dependency_ratio(f)
        + Decimal("0.30") * shared_write_ratio(f)
        + Decimal("0.20") * context_transfer_ratio(f)
        + Decimal("0.15") * merge_ratio(f)
    )
    return TeamTaskAssessment(
        decomposability=decomposability,
        parallelism=parallelism,
        verification_value=verification_value,
        coordination_cost=coordination_cost,
        correlated_error_risk=prior_correlation_risk(f),
        risk_class=f.risk_class,
        required_capabilities=tuple(sorted(set(f.required_capabilities))),
        explanation_codes=explain_assessment(f),
    )
```

Validate a DAG with Kahn's algorithm, require every non-merge deliverable to have an evidence requirement, use `Decimal` for budget/score math, canonical UTC timestamps, deterministic IDs derived from the canonical content hash, and explicit enums only. Re-export only the frozen public contract from `__init__.py`.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_models.py tests/agent/team_planner/test_assessment.py -q && git diff --check`

Expected: PASS for all boundary/invariant cases and byte-identical hashes across key order and process-style reload.

- [ ] **Step 5: Commit**

```bash
git add agent/team_planner tests/agent/team_planner/test_models.py \
  tests/agent/team_planner/test_assessment.py
git commit -m "feat: define cognitive team planning contract"
```

---

### Task 2: Derive Genuine Capability Diversity from Router Decisions and Independent Receipts

**Files:**
- Create: `agent/team_planner/capabilities.py`
- Create: `tests/agent/team_planner/test_capabilities.py`
- Modify: `agent/team_planner/__init__.py`

**Interfaces:**
- Consumes: item #10 `RoutingDecision`, `RuntimeKey.stable_id()`, `TaskAssessment`, `EvidenceEvent`, decision lookup/report APIs, and item #12 `ReceiptStore.get()`.
- Produces: `CapabilityEvidenceStore.build_vector(runtime_stable_id, *, as_of) -> CapabilityVector | None`, `capability_distance(left, right) -> Decimal`, and `correlated_error_risk(left_id, right_id, *, stratum) -> CorrelationEstimate`.

- [ ] **Step 1: Write RED evidence, freshness, and fake-diversity tests**

```python
def test_personas_and_provider_labels_do_not_create_diversity(capability_store):
    a = capability_store.build_vector("runtime-a", as_of=NOW)
    renamed = replace(a, runtime_stable_id="runtime-b")
    assert capability_distance(a, renamed) == Decimal("0")
    assert not qualifies_as_diverse(a, renamed)

def test_only_independent_fresh_receipts_feed_vectors(capability_store):
    vector = capability_store.build_vector("runtime-a", as_of=NOW)
    assert vector.sample_count == 24
    assert vector.confidence >= Decimal("0.80")
    assert set(vector.evidence_receipt_ids) == set(FRESH_INDEPENDENT_RECEIPTS)
    assert not set(vector.evidence_receipt_ids) & set(SELF_GRADED_OR_STALE_RECEIPTS)
```

Also prove two model names sharing the same full runtime stable ID are homogeneous; different runtime IDs with distance below `0.20`, fewer than 20 independently scored samples, confidence below `0.80`, or expired evidence are `diversity_unproven`; and pairwise error correlation is computed only on matched cases in the same preregistered stratum.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_capabilities.py -q`

Expected: FAIL because capability aggregation and correlation functions are absent.

- [ ] **Step 3: Implement receipt-grounded vectors and correlation**

```python
MIN_SAMPLES = 20
MIN_CONFIDENCE = Decimal("0.80")
MIN_DISTANCE = Decimal("0.20")

def qualifies_as_diverse(a: CapabilityVector, b: CapabilityVector, *, now: datetime) -> bool:
    return (
        a.runtime_stable_id != b.runtime_stable_id
        and a.sample_count >= MIN_SAMPLES and b.sample_count >= MIN_SAMPLES
        and a.confidence >= MIN_CONFIDENCE and b.confidence >= MIN_CONFIDENCE
        and parse_time(a.fresh_until) >= now and parse_time(b.fresh_until) >= now
        and capability_distance(a, b) >= MIN_DISTANCE
    )
```

Aggregate Beta-smoothed verified-success rates for normalized capability/domain/modality buckets from item #10 decisions joined to item #12 receipts by source identity. Store no raw prompt/response. Compute cosine distance over the union of dimensions and a shrinkage phi coefficient over matched binary independent-scorer errors. Missing/unmatched evidence returns an explicit unknown estimate that prevents a diversity claim. The planner may ask the router for another unit decision, but it never edits the returned runtime or ranks candidates itself.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_capabilities.py tests/plugins/auto_routing/test_decisions.py tests/agent/test_receipt_scoring.py -q && git diff --check`

Expected: PASS; stale, self-authored, mismatched, and persona-only evidence never qualifies as diversity.

- [ ] **Step 5: Commit**

```bash
git add agent/team_planner/capabilities.py agent/team_planner/__init__.py \
  tests/agent/team_planner/test_capabilities.py
git commit -m "feat: measure evidence-backed agent diversity"
```

---

### Task 3: Select One-Agent, Central-Verification, or Cross-Verification Topology

**Files:**
- Create: `agent/team_planner/algorithm.py`
- Create: `agent/team_planner/service.py`
- Create: `tests/agent/team_planner/test_algorithm.py`
- Modify: `agent/team_planner/__init__.py`

**Interfaces:**
- Consumes: Tasks 1–2 assessments/vectors/correlation, item #10 `AutoRoutingService.decide`, and immutable authority/budget from the mission request.
- Produces: `choose_team_plan(request, assessment, route_fn, capability_store) -> TeamPlanRevision`, plus `TeamPlannerService.simulate/create/explain`.

- [ ] **Step 1: Write RED topology table tests**

```python
@pytest.mark.parametrize(("assessment", "routes", "expected"), [
    (ASSESS_SEQUENTIAL, (ROUTE_A,), "one_agent"),
    (ASSESS_LOW_VALUE, (ROUTE_A, ROUTE_B), "one_agent"),
    (ASSESS_PARALLEL_LOW_CORR, (ROUTE_A, ROUTE_B), "central_verification"),
    (ASSESS_HIGH_CORR_HIGH_RISK, (ROUTE_A, ROUTE_B), "cross_verification"),
])
def test_topology_is_selected_by_value_not_agent_count(optimizer, assessment, routes, expected):
    plan = optimizer.choose(assessment, routes, BUDGET)
    assert plan.topology == expected
    assert len([u for u in plan.units if u.unit_kind != "merge"]) <= 2
```

Assert one agent when diversity is unproven, coordination reserve would exceed 15%, remaining dollars/time cannot cover worst-case unit reservations, decomposability `<0.45`, or `verification_value - coordination_cost <0.15`. Assert central verification when producer-error correlation `<0.35` and one independent verifier covers all required claims. Assert cross verification when correlation is `>=0.35`, risk is high, or central coverage `<0.80`; each of two agents must verify only the other's deliverables and disagreement prevents merge readiness.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_algorithm.py -q`

Expected: FAIL importing the optimizer/service.

- [ ] **Step 3: Implement deterministic candidate scoring and router boundary**

```python
TEAM_VALUE_MARGIN = Decimal("0.15")
CORRELATION_CROSS_THRESHOLD = Decimal("0.35")

def choose_topology(a, routes, vectors, budget):
    if a.decomposability < Decimal("0.45"):
        return "one_agent", ("low_decomposability",)
    if a.verification_value - a.coordination_cost < TEAM_VALUE_MARGIN:
        return "one_agent", ("coordination_exceeds_marginal_value",)
    if not two_routes_are_measurably_diverse(routes, vectors):
        return "one_agent", ("diversity_unproven",)
    if not budget.can_reserve_two_units(max_coordination_fraction=Decimal("0.15")):
        return "one_agent", ("team_budget_unavailable",)
    corr = correlated_error_risk(routes[0], routes[1], stratum=a.risk_class)
    if corr.unknown or corr.value >= CORRELATION_CROSS_THRESHOLD or a.risk_class == "high":
        return "cross_verification", ("correlated_error_requires_cross_check",)
    return "central_verification", ("independent_central_check_is_cheaper",)
```

Build one-agent first. Only after it is valid may the service create a second typed execution/verification unit and call item #10 separately for that assigned unit. Persist router decisions before referencing their IDs. `simulate()` performs the same pure selection against a read-only router shadow decision and writes nothing. `create()` checks exact mission authority/version and stores no provider/model fields beyond the router's non-secret stable ID. Do not invoke MoA or an LLM to pick topology.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_algorithm.py tests/plugins/auto_routing/test_selector.py -q && git diff --check`

Expected: PASS; all seeded cases choose at most two agents, never bypass hard router filters, and choose one agent for every unproven or negative-value team.

- [ ] **Step 5: Commit**

```bash
git add agent/team_planner/algorithm.py agent/team_planner/service.py \
  agent/team_planner/__init__.py tests/agent/team_planner/test_algorithm.py
git commit -m "feat: choose bounded cognitive team topologies"
```

---

### Task 4: Persist Immutable Revisions and Project onto Mission and Kanban State

**Files:**
- Create: `agent/team_planner/projection.py`
- Create: `tests/agent/team_planner/test_projection.py`
- Modify: `hermes_cli/missions_db.py`
- Modify: `hermes_cli/kanban_db.py`
- Modify: `hermes_cli/kanban_swarm.py`
- Modify: `tests/hermes_cli/test_missions_db.py`
- Modify: `tests/hermes_cli/test_kanban_swarm.py`

**Interfaces:**
- Consumes: item #1 mission/event/version APIs and existing Kanban `create_task/link_tasks/add_comment` plus task metadata.
- Produces: `insert_team_plan_revision(conn, revision, *, expected_mission_version)`, `compare_and_set_active_team_revision(...)`, and `TeamPlanProjector.apply/recover`.

- [ ] **Step 1: Write RED atomicity, idempotency, and no-second-graph tests**

```python
def test_projection_reuses_existing_kanban_graph_after_crash(harness, revision):
    harness.project(revision, crash_after="first_task")
    recovered = harness.reopen().projector.recover(revision.plan_id)
    assert recovered.created_task_count == len(revision.units)
    assert len(set(recovered.task_ids)) == len(revision.units)
    assert harness.kanban_link_count() == len(expected_edges(revision))
    assert harness.mission_event_count("team_plan_projected") == 1
```

Also prove an incorrect mission authority/version writes no revision/tasks, a conflicting same `(plan_id, revision)` hash raises, active-revision CAS prevents concurrent replans, task status/run/retry fields live only in Kanban, and `create_swarm()` retains its current graph/result behavior through the generic projector.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_projection.py tests/hermes_cli/test_missions_db.py tests/hermes_cli/test_kanban_swarm.py -q`

Expected: FAIL because mission team revision storage and typed projection do not exist.

- [ ] **Step 3: Add immutable revision/projection records and idempotency keys**

Add `mission_team_plan_revisions(plan_id, revision, mission_id, parent_revision, content_hash, plan_json, created_at, PRIMARY KEY(plan_id, revision))` and `mission_team_plan_projection(plan_id, revision, unit_id, kanban_task_id, routing_decision_id, PRIMARY KEY(plan_id, revision, unit_id))` beside mission ownership. Store full canonical plan JSON, but no mutable unit state. Project every unit with metadata:

```python
metadata = {
    "team_plan": {
        "plan_id": revision.plan_id,
        "revision": revision.revision,
        "unit_id": unit.unit_id,
        "unit_kind": unit.unit_kind,
        "deliverable_ids": list(unit.deliverable_ids),
        "evidence_requirement_ids": list(unit.evidence_requirement_ids),
        "routing_decision_id": decision_id,
        "plan_hash": revision.content_hash,
    }
}
```

Use deterministic task idempotency identity `sha256(plan_id:revision:unit_id)`, existing Kanban links for `depends_on`, existing task comments for projection audit, and mission events for revision activation. Recovery reconciles projections by identity; it never infers completion or rewrites old revisions.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_projection.py tests/hermes_cli/test_missions_db.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_swarm.py -q && git diff --check`

Expected: PASS across every crash point with one revision, one task per unit, one edge per dependency, and unchanged fixed-swarm behavior.

- [ ] **Step 5: Commit**

```bash
git add agent/team_planner/projection.py hermes_cli/missions_db.py \
  hermes_cli/kanban_db.py hermes_cli/kanban_swarm.py \
  tests/agent/team_planner/test_projection.py tests/hermes_cli/test_missions_db.py \
  tests/hermes_cli/test_kanban_swarm.py
git commit -m "feat: project team plans onto durable missions"
```

---

### Task 5: Execute Routed Units Through Existing Delegate, Active-Agent, Mailbox, and Budget Seams

**Files:**
- Modify: `agent/team_planner/projection.py`
- Modify: `tools/delegate_tool.py`
- Modify: `tools/async_delegation.py`
- Create: `tests/agent/team_planner/test_execution.py`
- Modify: `tests/tools/test_delegate.py`
- Modify: `tests/tools/test_async_delegation.py`

**Interfaces:**
- Consumes: item #10 persisted `RoutingDecision`; existing `_build_child_agent`, `_run_single_child`, `IterationBudget`, `list_active_subagents`, async dispatch/recovery, parent cost rollup, and Kanban worker route overrides.
- Produces: `run_planned_child(spec, decision, parent_agent, budget_reservation) -> PlannedChildResult` and durable non-secret planner correlation on existing async completion events.

- [ ] **Step 1: Write RED routing, cap, cancellation, and cost tests**

```python
def test_planned_child_uses_exact_persisted_route_and_existing_accounting(harness):
    result = harness.run_unit(UNIT, ROUTING_DECISION, TEAM_BUDGET)
    assert result.routing_decision_id == ROUTING_DECISION.decision_id
    assert result.runtime_stable_id == ROUTING_DECISION.target.runtime_key.stable_id()
    assert harness.parent.session_estimated_cost_usd == result.cost_usd
    assert harness.active_registry_empty()
    assert harness.iteration_budget.used <= UNIT.max_iterations
```

Also prove max two active planned children, `delegation.max_concurrent_children` can reduce that to one, depth and orchestrator kill switches remain enforced, `/stop` and `interrupt_subagent` cancel a planned child, pool capacity rejects rather than queues unbounded work, completion replay preserves `plan_id/revision/unit_id`, and a cost/time reservation exhaustion stops before another provider call.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_execution.py tests/tools/test_delegate.py tests/tools/test_async_delegation.py -q`

Expected: FAIL because the internal planned-child adapter/correlation fields are absent.

- [ ] **Step 3: Add a non-model-visible internal execution adapter**

```python
def run_planned_child(*, spec, decision, parent_agent, reservation):
    reservation.require_remaining_before_call()
    child = _build_child_agent(
        spec.objective, parent_agent,
        model=decision.target.runtime_key.model,
        override_provider=decision.target.runtime_key.provider,
        override_runtime_access_binding=decision.runtime_access_binding,
        internal_reasoning_override=decision.reasoning_effort,
        max_iterations=min(spec.max_iterations, reservation.iterations_remaining),
    )
    verify_child_runtime_identity(child, decision.target.runtime_key)
    child._team_plan_ref = (reservation.plan_id, reservation.revision, spec.unit_id)
    return _run_single_child(reservation.task_index, spec.objective, child, parent_agent)
```

This function is internal and omitted from all tool definitions. Reuse `_register_subagent`, progress callbacks, child cleanup, summary truncation, session cost rollup, and async operation journal. Extend durable async records/events with bounded `plan_id`, integer `revision`, and `unit_id`; startup recovery restores them through the same shared completion queue and ownership filter. Check the route's full non-secret runtime access binding before the child's first request; mismatch destroys the unstarted child and marks the unit blocked.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_execution.py tests/tools/test_delegate.py tests/tools/test_async_delegation.py tests/gateway/test_async_delegation_session_binding.py -q && git diff --check`

Expected: PASS with exact route identity, existing cost/iteration/concurrency accounting, cancellation propagation, and one recoverable completion per planned unit.

- [ ] **Step 5: Commit**

```bash
git add agent/team_planner/projection.py tools/delegate_tool.py tools/async_delegation.py \
  tests/agent/team_planner/test_execution.py tests/tools/test_delegate.py \
  tests/tools/test_async_delegation.py
git commit -m "feat: execute routed cognitive team units"
```

---

### Task 6: Compile Bounded Shared Context and Typed Deliverable Handoffs

**Files:**
- Create: `agent/team_planner/context.py`
- Create: `tests/agent/team_planner/test_context.py`
- Modify: `hermes_cli/kanban_db.py`
- Modify: `tools/kanban_tools.py`
- Modify: `tests/tools/test_kanban_tools.py`

**Interfaces:**
- Consumes: existing Kanban task comments, parent run summaries, attachments/artifacts, task links, item #12 receipt IDs, and `build_worker_context()` limits.
- Produces: `compile_team_context(conn, plan_ref, unit_id, *, max_chars) -> TeamContextBundle` and `post_team_handoff(...) -> Comment` using existing comments.

- [ ] **Step 1: Write RED provenance, staleness, injection, and size tests**

```python
def test_context_contains_typed_refs_not_hidden_reasoning(context_harness):
    bundle = context_harness.compile("plan-1", 2, "verify-a", max_chars=12000)
    assert bundle.source_receipt_ids == ("rct_fresh",)
    assert "private chain of thought" not in bundle.rendered.lower()
    assert len(bundle.rendered) <= 12000
    assert all(item.producer_unit_id and item.content_hash for item in bundle.items)
```

Prove malicious comment text is framed as untrusted worker data, forged author fields are ignored, stale parent evidence is marked stale and cannot satisfy a requirement, missing artifacts remain explicit, old comments are summarized with hashes, and compiling context never edits a running child's messages or system prompt.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_context.py tests/tools/test_kanban_tools.py -q`

Expected: FAIL importing `agent.team_planner.context`.

- [ ] **Step 3: Implement the comment-backed typed context compiler**

Use structured comment prefix `team-handoff:v1:` followed by canonical JSON containing `plan_id`, `revision`, producer/consumer unit IDs, deliverable IDs, receipt IDs, artifact IDs, bounded redacted summary, observed timestamp, freshness, and content hash. `post_team_handoff()` calls existing `kanban_db.add_comment`; it does not insert SQL directly. `compile_team_context()` traverses declared parent edges only, verifies plan/revision/unit identity, resolves receipts/artifacts through their owners, orders items by dependency then stable ID, and renders explicit `<untrusted-worker-handoff>` boundaries. It returns a new child-input block only; active agents receive progress events, not context mutation.

```python
def post_team_handoff(conn, *, root_task_id: str, item: TeamContextItem, author: str) -> int:
    payload = canonical_team_context_json(item)
    return kanban_db.add_comment(
        conn,
        root_task_id,
        author=author,
        body="team-handoff:v1:" + payload,
    )

def compile_team_context(conn, plan_ref, unit_id, *, max_chars):
    items = validate_and_order_parent_handoffs(conn, plan_ref, unit_id)
    rendered = render_untrusted_handoffs(items, max_chars=max_chars)
    return build_context_bundle(plan_ref, unit_id, items, rendered)
```

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_context.py tests/tools/test_kanban_tools.py tests/hermes_cli/test_kanban_db.py -q && git diff --check`

Expected: PASS with bounded, deterministic, profile-local context and no new mailbox table/tool schema.

- [ ] **Step 5: Commit**

```bash
git add agent/team_planner/context.py hermes_cli/kanban_db.py tools/kanban_tools.py \
  tests/agent/team_planner/test_context.py tests/tools/test_kanban_tools.py
git commit -m "feat: compile typed cognitive team handoffs"
```

---

### Task 7: Enforce Central/Cross Verification and Canonical Receipt Proof

**Files:**
- Create: `agent/team_planner/verification.py`
- Create: `tests/agent/team_planner/test_verification.py`
- Modify: `agent/team_planner/service.py`
- Modify: `agent/team_planner/__init__.py`

**Interfaces:**
- Consumes: topology, deliverables/evidence requirements, item #10 route identity/correlation, and item #12 `ReceiptIssuer.issue/recheck`, `ReceiptStore.get`, `ReceiptSourceKey`, and canonical statuses.
- Produces: `arrange_verification(revision, routed_units, correlation) -> VerificationAssignment`, `team_receipt_source(plan_id, revision) -> ReceiptSourceKey`, and `evaluate_merge_readiness(...) -> MergeReadiness`.

- [ ] **Step 1: Write RED independence and truthful-status tests**

```python
def test_cross_verification_never_allows_self_check_or_consensus_to_verify(service):
    readiness = service.evaluate(CROSS_PLAN, outcomes=(A_CHECKS_B, B_CHECKS_A))
    assert readiness.ready is False
    assert readiness.status == "completed_unverified"
    receipt = service.issue_receipt(CROSS_PLAN)
    assert receipt.status != "verified"
```

Also prove central verifier route differs measurably from producer route; cross verification assigns each deliverable to a non-author; correlation unknown/high selects cross verification; disagreement, stale evidence, missing claim, unknown operation, or artifact mismatch blocks merge or yields canonical non-verified status; only a fresh independent item #12 scorer can seal `verified`.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_verification.py -q`

Expected: FAIL because verification arrangement and team receipt source do not exist.

- [ ] **Step 3: Implement assignments and receipt-only terminal proof**

```python
def evaluate_merge_readiness(requirements, unit_outcomes, receipt_store):
    missing, stale, disputed = inspect_requirements(requirements, unit_outcomes, receipt_store)
    if any(o.receipt_status == "unknown_effect" for o in unit_outcomes):
        return MergeReadiness(False, "unknown_effect", ("unknown_effect",))
    if missing or stale or disputed:
        return MergeReadiness(False, "completed_unverified", tuple(missing + stale + disputed))
    return MergeReadiness(True, "completed_unverified", ("awaiting_independent_team_receipt",))
```

Build a team evidence source from immutable mission intent, revision hash, unit route decision IDs, unit receipt IDs, deliverable/artifact hashes, verifier assignments, disagreement records, cost/time totals, and uncertainty. Call `ReceiptIssuer.issue(ReceiptSourceKey("mission", mission_id))` through the mission source adapter; add team-specific claims but never touch receipt SQL or construct `VerifiedReceiptDecision`. Persist the receipt before projecting terminal success.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_verification.py tests/agent/test_receipt_scoring.py tests/agent/test_receipt_ingest.py -q && git diff --check`

Expected: PASS with zero self-verification paths and exactly the five canonical receipt statuses.

- [ ] **Step 5: Commit**

```bash
git add agent/team_planner/verification.py agent/team_planner/service.py \
  agent/team_planner/__init__.py tests/agent/team_planner/test_verification.py
git commit -m "feat: verify cognitive teams with receipts"
```

---

### Task 8: Shrink, Expand, Reassign, and Recover After Partial Outcomes

**Files:**
- Create: `agent/team_planner/adaptation.py`
- Create: `tests/agent/team_planner/test_adaptation.py`
- Modify: `agent/team_planner/service.py`
- Modify: `agent/team_planner/projection.py`
- Modify: `hermes_cli/missions_db.py`

**Interfaces:**
- Consumes: immutable active revision, `PartialTeamOutcome`, remaining reservations, mission authority version, router decision API, receipts, Kanban state, and async operation journal.
- Produces: `decide_replan(...) -> ReplanDecision`, immutable next revisions, CAS activation, and idempotent `recover(plan_id)`.

- [ ] **Step 1: Write RED adaptive-policy and crash tests**

```python
@pytest.mark.parametrize(("outcomes", "expected"), [
    ((VERIFIED_PRIMARY,), "shrink"),
    ((BLOCKED_CAPABILITY_GAP,), "expand"),
    ((FAILED_RUNTIME,), "reassign"),
    ((UNKNOWN_EFFECT,), "stop"),
    ((COORDINATION_EXHAUSTED,), "stop"),
])
def test_partial_outcome_policy(adaptation, outcomes, expected):
    assert adaptation.decide(ACTIVE_PLAN, outcomes, REMAINING).action == expected
```

Inject crashes after new revision insert, after first replacement task, after route persistence, and before active-revision CAS. Assert restart produces one active immutable revision, never reruns a completed/unknown unit, never rewrites prior receipts, and never spends beyond the original plan budgets.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_adaptation.py -q`

Expected: FAIL importing adaptation policy.

- [ ] **Step 3: Implement conservative deterministic replan rules**

```python
def decide_replan(plan, outcomes, remaining):
    if any(o.receipt_status == "unknown_effect" for o in outcomes):
        return stop("unknown_effect_requires_review")
    if remaining.coordination_exhausted or remaining.wall_clock_exhausted:
        return stop("coordination_budget_exhausted")
    if all_required_verified(outcomes) and has_unstarted_optional_unit(plan, outcomes):
        return shrink(cancel_unstarted=True, reason="marginal_value_satisfied")
    if capability_gap_is_recoverable(outcomes) and plan.topology == "one_agent" and remaining.can_add_one:
        return expand(reason="partial_outcome_supports_second_unit")
    if failed_route_is_safe_to_replace(outcomes) and remaining.can_reassign:
        return reassign(reason="routed_unit_failed_before_effect")
    return keep("no_safe_positive_value_change")
```

Expansion may only move one to two agents. Shrink cancels only unstarted work through existing Kanban/async cancellation. Reassignment creates a new unit attempt ID and new item #10 decision; it never changes a running child's provider/model. Stale evidence triggers recheck or re-execution, never promotion. CAS on `(plan_id, active_revision, mission_authority_version)` prevents concurrent replans; recovery projects the winning revision and marks losing inserted revisions inactive without deleting audit history.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/team_planner/test_adaptation.py tests/agent/team_planner/test_projection.py tests/tools/test_async_delegation.py -q && git diff --check`

Expected: PASS for all crash/replay cases with bounded spend, no duplicated unit effect, and no revision history mutation.

- [ ] **Step 5: Commit**

```bash
git add agent/team_planner/adaptation.py agent/team_planner/service.py \
  agent/team_planner/projection.py hermes_cli/missions_db.py \
  tests/agent/team_planner/test_adaptation.py
git commit -m "feat: adapt cognitive teams after outcomes"
```

---

### Task 9: Deliver Guarded Classic CLI and Native Ink Controls

**Files:**
- Create: `hermes_cli/team_planner.py`
- Create: `tests/hermes_cli/test_team_planner_cli.py`
- Modify: `hermes_cli/config.py`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/main.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Modify: `cli.py`
- Modify: `tui_gateway/server.py`
- Modify: `ui-tui/src/gatewayTypes.ts`
- Modify: `ui-tui/src/app/slash/commands/ops.ts`
- Create: `tests/tui_gateway/test_team_planner_rpc.py`
- Create: `ui-tui/src/__tests__/teamPlannerCommand.test.ts`

**Interfaces:**
- Consumes: `TeamPlannerService` and `TeamPlanProjector`.
- Produces: `run_argv(argv: Sequence[str], *, output: Literal["text", "json"] = "text") -> TeamCommandResult`, top-level `hermes team`, aliases `teams`, classic `/team`, and native `team.exec` RPC.

- [ ] **Step 1: Write RED preview/apply/explain/simulate tests**

```python
def test_plan_defaults_to_simulation_and_apply_requires_exact_hash(cli):
    preview = cli.run("team plan --mission m1 --simulate --json")
    assert preview["writes"] == []
    denied = cli.run("team plan --mission m1 --apply --expect-hash 0" )
    assert denied.exit_code == 2
    applied = cli.run(f"team plan --mission m1 --apply --expect-hash {preview['plan_hash']}")
    assert applied.exit_code == 0
```

Cover `team explain`, `status`, `replan --simulate`, `replan --apply --expect-revision`, `stop`, `--json`, profile isolation, bounded IDs/argv, redaction, dependency-unavailable fail-closed output, and Ink rendering of one-agent reasons, selected topology, measured diversity, coordination reserve, route decision IDs, receipt state, and revision history.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_team_planner_cli.py tests/tui_gateway/test_team_planner_rpc.py -q && cd ui-tui && npm test -- --run src/__tests__/teamPlannerCommand.test.ts`

Expected: FAIL because commands, RPC, and Ink renderer are absent.

- [ ] **Step 3: Implement terminal-first guarded controls**

Register `CommandDef("team", "Plan and inspect cognitive teams", "Session", aliases=("teams",), args_hint="<plan|simulate|explain|status|replan|stop>")`. `plan` and `replan` default to simulation; writes require `--apply`, exact plan/precondition hash, mission authority version, and cross-process mission lock. Config defaults are `enabled: false`, `mode: simulate`, `max_agents: 2`, `max_coordination_fraction: 0.15`, `minimum_capability_samples: 20`, and `minimum_capability_confidence: 0.80`. RPC accepts tokenized bounded argv and calls the same service; it does not reconstruct planner rules in TypeScript.

```python
TEAM_COMMAND = CommandDef(
    "team",
    "Plan and inspect cognitive teams",
    "Session",
    aliases=("teams",),
    args_hint="<plan|simulate|explain|status|replan|stop>",
)

TEAM_PLANNER_DEFAULTS = {
    "enabled": False,
    "mode": "simulate",
    "max_agents": 2,
    "max_coordination_fraction": 0.15,
    "minimum_capability_samples": 20,
    "minimum_capability_confidence": 0.80,
}
```

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_team_planner_cli.py tests/tui_gateway/test_team_planner_rpc.py -q && cd ui-tui && npm test -- --run src/__tests__/teamPlannerCommand.test.ts && npm run typecheck && cd .. && git diff --check`

Expected: PASS; simulation is write-free, guarded apply is conflict-safe, and CLI/Ink expose the complete explanation without secrets.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/team_planner.py hermes_cli/config.py hermes_cli/commands.py \
  hermes_cli/main.py hermes_cli/cli_commands_mixin.py cli.py tui_gateway/server.py \
  ui-tui/src/gatewayTypes.ts ui-tui/src/app/slash/commands/ops.ts \
  tests/hermes_cli/test_team_planner_cli.py tests/tui_gateway/test_team_planner_rpc.py \
  ui-tui/src/__tests__/teamPlannerCommand.test.ts
git commit -m "feat: add terminal cognitive team controls"
```

---

### Task 10: Add Secondary Read-Only Dashboard Inspection

**Files:**
- Create: `web/src/pages/TeamPlannerPage.tsx`
- Create: `web/src/pages/TeamPlannerPage.test.tsx`
- Modify: `hermes_cli/web_server.py`
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/App.tsx`
- Create: `tests/hermes_cli/test_team_planner_dashboard.py`

**Interfaces:**
- Consumes: profile-scoped planner explanation/list queries and existing Dashboard authentication.
- Produces: authenticated `GET /api/team-plans`, `GET /api/team-plans/{plan_id}`, and a secondary read-only Dashboard route.

- [ ] **Step 1: Write RED authentication, profile, and read-only tests**

```python
def test_dashboard_team_plan_is_profile_scoped_redacted_and_read_only(client, profiles):
    response = client.get("/api/team-plans/plan-a", headers=profiles.a_auth)
    assert response.status_code == 200
    assert "api_key" not in response.text and "raw_prompt" not in response.text
    assert client.get("/api/team-plans/plan-b", headers=profiles.a_auth).status_code == 404
    assert client.post("/api/team-plans/plan-a/replan", headers=profiles.a_auth).status_code == 405
```

The Vitest case asserts list/detail/revision/topology/budget/receipt/correlation rendering and a command hint to `hermes team replan`; it must not provide execute, stop, or replan buttons.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_team_planner_dashboard.py -q && cd web && npm test -- --run src/pages/TeamPlannerPage.test.tsx`

Expected: FAIL because endpoints/page do not exist.

- [ ] **Step 3: Implement bounded read-only endpoints and page**

Use existing auth/profile resolution, bounded plan IDs, cursor limit `1..200`, and public redaction. Expose assessment scores/reason codes, topology, unit/dependency graph, route decision IDs and non-secret stable runtime IDs, capability sample/confidence/distance, budgets consumed/reserved, revision history, receipt IDs/statuses, and uncertainties. Never expose prompts, child reasoning, credentials, endpoints, raw artifact locators, private comments, or another profile's existence. Desktop files and shared Desktop packages remain untouched; headless `hermes serve` may expose authenticated APIs but Desktop has no route/client dependency.

```python
@app.get("/api/team-plans/{plan_id}")
async def team_plan_detail(plan_id: str, request: Request):
    profile = require_authenticated_profile(request)
    bounded_id = validate_bounded_id(plan_id, prefix="plan_", max_length=96)
    detail = open_team_planner_service(profile).explain(bounded_id)
    return JSONResponse(team_plan_public_view(detail))
```

```tsx
export function TeamPlannerPage() {
  const query = useTeamPlansQuery()
  return <TeamPlanInspector plans={query.data ?? []} mutationControls={false} />
}
```

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_team_planner_dashboard.py -q && cd web && npm test -- --run src/pages/TeamPlannerPage.test.tsx src/lib/api.test.ts && npm run typecheck && cd .. && git diff --check`

Expected: PASS with read-only, redacted, profile-local inspection and no Desktop dependency.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/web_server.py web/src/lib/api.ts web/src/App.tsx \
  web/src/pages/TeamPlannerPage.tsx web/src/pages/TeamPlannerPage.test.tsx \
  tests/hermes_cli/test_team_planner_dashboard.py
git commit -m "feat: inspect cognitive team plans in dashboard"
```

---

### Task 11: Prove Real-Path Recovery, Adversarial Coordination, and Cache Invariants

**Files:**
- Create: `tests/hermes_cli/test_team_planner_e2e.py`
- Create: `tests/agent/team_planner/test_security.py`
- Modify: `tests/test_get_tool_definitions_cache_isolation.py`
- Modify: `tests/run_agent/test_iteration_budget_race.py`
- Modify: `tests/gateway/test_agent_cache.py`

**Interfaces:**
- Consumes: complete plan/create/project/execute/handoff/verify/replan flow through real imports and temporary profile state.
- Produces: a real-path recovery/security proof and invariant regression coverage.

- [ ] **Step 1: Write RED E2E and adversarial cases**

Create a temporary `HERMES_HOME`, real mission DB, Kanban board, SessionDB, receipts store, temp Git repo/artifacts, fake provider completion boundary, and subprocess restart. Parameterize crashes at `after_route_persist`, `after_revision_insert`, `after_first_task_projection`, `after_child_result`, `after_receipt_insert`, and `before_active_revision_cas`. Include stale receipt evidence, forged receipt IDs, prompt injection in handoff comments, route identity mismatch, provider loss, budget exhaustion, duplicate completion replay, dependency cycles, self-verification, correlated collusion, malicious artifact paths/symlink swaps, cross-profile IDs, and concurrent replan races.

```python
def test_real_path_recovery_never_duplicates_or_false_verifies(team_e2e, fault_point):
    result = team_e2e.run_then_restart(fault_point)
    assert result.effect_duplicates == 0
    assert result.active_revision_count == 1
    assert result.cost_usd <= result.budget.dollar_limit
    assert result.elapsed_seconds <= result.budget.wall_clock_seconds
    assert not result.false_verified
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_team_planner_e2e.py tests/agent/team_planner/test_security.py -q`

Expected: FAIL until cross-module recovery and security behavior is fully wired; do not weaken assertions to obtain GREEN.

- [ ] **Step 3: Complete owning-module wiring and invariant checks**

Fix behavior only in the owning production modules from Tasks 1–10. Add a multi-turn parent conversation plus fresh children and independently hash before/after:

```python
assert sha256(system_prompt_before) == sha256(system_prompt_after)
assert sha256(canonical_tool_schema_before) == sha256(canonical_tool_schema_after)
assert (provider_before, model_before) == (provider_after, model_after)
assert valid_role_and_tool_call_alternation(messages)
assert "team_plan" not in model_visible_delegate_schema
```

Prove a reassign creates a new child/cache namespace; no running child route changes; no planner/receipt/mailbox record is inserted as a user message; async completion enters only as the existing idle-session new-turn mechanism; stale or adversarial coordination data remains untrusted; all high-risk cases fail closed.

- [ ] **Step 4: Run GREEN and focused regressions**

Run: `scripts/run_tests.sh tests/hermes_cli/test_team_planner_e2e.py tests/agent/team_planner tests/tools/test_delegate.py tests/tools/test_async_delegation.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_missions_db.py tests/agent/test_receipt_scoring.py tests/test_get_tool_definitions_cache_isolation.py tests/run_agent/test_iteration_budget_race.py tests/gateway/test_agent_cache.py -q && git diff --check`

Expected: PASS across every fault/security case with stable cache/schema/provider/model hashes, valid roles, bounded budgets, and zero duplicate/false-verified outcomes.

- [ ] **Step 5: Commit**

```bash
git add tests/hermes_cli/test_team_planner_e2e.py \
  tests/agent/team_planner/test_security.py \
  tests/test_get_tool_definitions_cache_isolation.py \
  tests/run_agent/test_iteration_budget_race.py tests/gateway/test_agent_cache.py
git commit -m "test: prove cognitive team recovery and safety"
```

---

### Task 12: Run the 200-Task Proof, Document Rollout, and Enforce Stop Conditions

**Files:**
- Create: `benchmarks/team_planner/runner.py`
- Create: `tests/benchmarks/test_team_planner_runner.py`
- Create: `website/docs/user-guide/features/cognitive-team-planner.md`
- Create: `website/docs/development/team-planner-contract.md`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`
- Modify: `website/sidebars.ts`

**Interfaces:**
- Consumes: Task 0 manifest/cases, full planner, item #12 receipts, and locally captured cost/latency/safety facts.
- Produces: `run_team_planner_benchmark(manifest_path: Path, *, repeats: int, output: TextIO) -> TeamPlannerBenchmarkReport`, rollout gate evaluation, local JSON/Markdown report, and operator/developer documentation.

- [ ] **Step 1: Write RED report/gate tests**

```python
def test_candidate_pass_requires_quality_or_cost_branch_and_every_floor(report_factory):
    quality = report_factory(candidate_gain_pp=Decimal("5"), one_agent_fraction=Decimal("0.35"))
    assert quality.passes
    cost = report_factory(candidate_gain_pp=Decimal("-1"), cost_reduction=Decimal("0.25"),
                          within_noninferiority=True, one_agent_fraction=Decimal("0.40"))
    assert cost.passes
    assert not replace(cost, high_risk_violations=1).passes
    assert not replace(cost, coordination_overruns=1).passes
    assert not replace(cost, correlated_error_relative_reduction=Decimal("0.19")).passes
```

Assert all 200 cases stay in the denominator in each arm; rate reports include Wilson 95% intervals; latency reports p50/p95; costs include total and cost per verified success; safety and archetype slices are separate; aborted/excluded cases list reasons; and no composite score exists.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_team_planner_runner.py -q`

Expected: FAIL because runner/report/gate evaluation are absent.

- [ ] **Step 3: Implement exact three-arm evaluation and stop logic**

Run arms in seeded randomized order on the frozen hardware/network class. Enforce identical per-case dollar and wall-clock ceilings. Count timeout, budget abort, dependency unavailable, and scorer ambiguity as non-success, not exclusions. Calculate candidate pass as:

```python
quality_branch = candidate.success_rate >= one.success_rate + Decimal("0.05") \
    and candidate.success_rate >= homogeneous.success_rate + Decimal("0.05")
cost_branch = noninferior(candidate, one, margin=Decimal("0.02")) \
    and noninferior(candidate, homogeneous, margin=Decimal("0.02")) \
    and candidate.total_cost <= Decimal("0.75") * min(one.total_cost, homogeneous.total_cost)
passes = (quality_branch or cost_branch) \
    and candidate.one_agent_fraction >= Decimal("0.30") \
    and candidate.correlated_error_relative_reduction >= Decimal("0.20") \
    and candidate.coordination_overruns == 0 \
    and candidate.high_risk_violations == 0
```

“Lower correlated error” additionally requires the adaptive team-mode matched-case error phi to be at least `0.10` below four-homogeneous and the stratified bootstrap 95% upper bound on the difference to remain below zero. If the 200-case sample is underpowered, report `inconclusive` and increase a future frozen corpus; never relax a threshold after results. Reports remain local and contain denominator, confidence intervals, model/runtime revision IDs, scorer versions, costs, p50/p95 latency, coordination fractions, one-agent selections, topology reasons, safety slices, and stop conditions.

- [ ] **Step 4: Document staged rollout and optional-algorithm boundary**

Document copyable `hermes team simulate/plan/explain/status/replan/stop` and `/team` flows, exact status/receipt language, why personas do not count, item #10/#12/#1 ownership, budgets, central versus cross verification, stale evidence, recovery, privacy, profile isolation, and limitations. Rollout stages are: disabled by default; simulation-only on frozen corpus; operator-applied shadow plans with no execution; 5% eligible-mission canary; 25%; then default-on only after the full proof passes. Stop and revert to simulation on any high-risk violation, cache/schema/role drift, router/receipt/mission incompatibility, coordination overrun, one-agent fraction below 30%, correlation reduction below gate, cost branch failure, success regression, or more-agent activity without better verified outcomes. V1 has no algorithm plugin registry; a later standalone algorithm plugin must demonstrate a real consumer and use a generic, versioned provider interface without editing core or adding a model tool.

```markdown
## Cognitive Team Planner

Run `hermes team simulate --mission <id>` before any apply. The explanation
shows why Hermes selected one agent, central verification, or cross
verification; its decision is not proof of success. Only the linked canonical
receipt may report `verified`.

### Rollout stops

Return to `team_planner.mode: simulate` on any safety-floor violation,
coordination overrun, cache/schema/role drift, dependency incompatibility,
benchmark regression, or evidence that team-mode errors remain correlated.
```

- [ ] **Step 5: Run final GREEN matrix**

Run: `scripts/run_tests.sh tests/benchmarks/test_team_planner_manifest.py tests/benchmarks/test_team_planner_runner.py tests/agent/team_planner tests/hermes_cli/test_team_planner_cli.py tests/hermes_cli/test_team_planner_e2e.py tests/tui_gateway/test_team_planner_rpc.py tests/hermes_cli/test_team_planner_dashboard.py -q`

Run: `cd ui-tui && npm test -- --run src/__tests__/teamPlannerCommand.test.ts && npm run typecheck && cd ../web && npm test -- --run src/pages/TeamPlannerPage.test.tsx src/lib/api.test.ts && npm run typecheck && npm run build && cd ../website && npm run lint:diagrams && npm run typecheck && npm run build && cd .. && git diff --check`

Expected: all Python/Ink/Dashboard/docs checks pass; the local 200-task runner exits success only when one exact proof branch and every safety/coordination/correlation/frequency gate pass; `git diff --check` emits no output.

- [ ] **Step 6: Commit**

```bash
git add benchmarks/team_planner/runner.py tests/benchmarks/test_team_planner_runner.py \
  website/docs/user-guide/features/cognitive-team-planner.md \
  website/docs/development/team-planner-contract.md \
  website/docs/reference/cli-commands.md website/docs/reference/slash-commands.md \
  website/sidebars.ts
git commit -m "docs: gate cognitive team planner rollout"
```

---

## Final Verification Matrix

| Contract | Required fresh evidence |
|---|---|
| One-agent default | Unit/property tests and benchmark report show at least 60/200 one-agent choices and one-agent selection whenever diversity/value/budget is unproven. |
| Genuine diversity | Every team pair has distinct full runtime stable IDs, distance `>=0.20`, at least 20 fresh independent receipt samples per runtime, confidence `>=0.80`, and no persona-only proxy. |
| Router ownership | Every unit references a persisted item #10 decision; no planner code ranks or rewrites provider/model/effort; route mismatch fails before first call. |
| Receipt ownership | Every terminal claim uses item #12 status and source/issuer/store; zero self/consensus/worker-success paths construct `verified`. |
| Mission/Kanban ownership | Revisions link item #1 missions and project onto existing Kanban tasks/links/runs/comments; crash recovery creates no duplicate graph/work/effect. |
| Adaptive behavior | Fresh partial outcomes can shrink one-or-two, expand one-to-two, or reassign into a fresh child; unknown effects and exhausted budgets stop. |
| Coordination/security | Adversarial handoffs, stale/forged evidence, confused delegation, cross-profile references, cycles, replay, provider loss, and concurrency races fail closed. |
| Cache/conversation | Independent hashes prove stable system prompt, tool schema, provider, and model for each lineage; roles remain valid; reassign creates a new child lineage. |
| Surfaces | CLI and native Ink implement explain/simulate/apply/status/replan/stop; Dashboard is authenticated read-only; Desktop has no file/build/runtime dependency. |
| 200-task product gate | Exactly 200 tasks, identical dollar/wall-clock caps, quality or cost branch, every high-risk floor, zero coordination overruns, `>=30%` one-agent, and lower correlated error. |

## Completion Gate

Do not declare the Diversity-Aware Cognitive Team Planner complete until the implementation and a fresh preregistered report prove every row above. A green mocked unit suite, a visually plausible team trace, four agreeing agents, or reduced latency without independent verified success is insufficient. Failure of the 200-task gate leaves the feature in simulation mode and is a valid result; it must not trigger threshold changes, hidden baseline exclusions, a larger default team, or a fallback to persona-based “diversity.”

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-diversity-aware-cognitive-team-planner.md`. Execute with `superpowers:subagent-driven-development` for task-by-task implementation and review, or `superpowers:executing-plans` for inline batches with checkpoints.
