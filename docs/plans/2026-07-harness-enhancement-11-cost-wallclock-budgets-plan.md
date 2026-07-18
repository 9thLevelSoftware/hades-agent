# Cost, Token, and Wall-Clock Budget Enforcement Implementation Plan

> For agentic workers: extend the existing `IterationBudget` contract rather than replacing it. Enforce at the core loop/accounting seams, preserve the existing summary-on-exhaustion path, and describe post-call USD/stream limitations honestly.

**Goal:** Add multi-dimensional budgets for iterations, tokens, estimated USD, and wall-clock at turn, session/day, and delegation-tree scopes, with graceful degradation before hard exhaustion and live status/notices.

**Architecture:** Extend `agent/iteration_budget.py` with `TurnBudget`/thread-safe `BudgetPool`; preserve `.consume()`, `.refund()`, `.remaining`, and the existing loop gate. Charge after normalized usage/cost at `conversation_loop.py` and `codex_runtime.py`, with a pre-call deadline/retry check. At soft thresholds lower reasoning effort; then choose a cheaper fallback-chain entry; then use existing budget-exhaustion summary. Session/day pool reads persisted `estimated_cost_usd`/tokens at turn start; child delegation receives an allocated sub-pool and charges live. Auxiliary/opaque routes that cannot be charged are surfaced as unmetered rather than silently treated as safe.

**Tech Stack:** `IterationBudget`, `CanonicalUsage`, `normalize_usage`, `estimate_usage_cost`, models.dev pricing, SessionDB sessions cost/token columns, failover/fallback runtime, `AgentNotice`/credits tracker, status/usage surfaces, delegation cost rollup, cron job config.

## Global Constraints

- Existing callers using `IterationBudget(max_total=...)` continue to work; default budgets remain current behavior unless users configure new ceilings.
- Budget enforcement is stop-before-next-call after accounting. A single in-flight request/stream can exceed a ceiling; a wall-clock deadline is checked between calls/retries in v1.
- USD is estimated. If pricing is unknown/included, token/time budgets still enforce and the notice says USD is unavailable.
- Charge each provider dollar once. Child tree pools charge live; final `delegate_tool` rollup updates display totals only and must not re-charge session/day scope.
- Per-turn budget and per-child pool are independent from the persistent session cost aggregate; no double counting on resume/retry.
- Soft degradation is monotonic within a turn: lower reasoning effort once, then fallback once; do not restore primary merely because a generic turn-start helper runs.
- Do not mutate user-configured fallback chains permanently. Budget state is a per-turn/runtime override.
- Summary-on-exhaustion is a controlled grace call only if a separate summary allowance remains; otherwise return the best accumulated result and notice.
- Wall-clock checks use monotonic time and account for retries/backoff; do not kill an in-flight stream using user interrupt semantics.
- Background/cron/delegation runs with no human cannot ask for budget approval; they degrade/stop and persist outcome.
- All dimensions are visible in status/usage/gateway notices, with provider/model/pricing source and unmetered flags.
- Tests use fake clocks/pricing and real temp SessionDB for daily/tree state; no sleep-based flaky tests.

## Current-State Review

- `IterationBudget` is a small thread-safe iteration counter used by turn context, agent init, delegate children, and many tests.
- The loop gate is in `conversation_loop.py`; retry path has its own loop. Budget-exhaustion finalizer calls `handle_max_iterations`/summary.
- `CanonicalUsage`/pricing/cost normalization and per-session persisted cost already exist in `conversation_loop.py`, `codex_runtime.py`, `usage_pricing.py`, and `hermes_state.py`; enforcement is absent.
- Delegation captures/rolls up child cost but each child gets a fresh budget and tree scope is not enforced.
- `AgentNotice`/credits tracker and CLI/TUI/gateway usage/status surfaces already exist.
- Reasoning config is consumed by transports, providing the first degradation knob. Fallback activation/restore-primary code must be made budget-aware.
- Codex app-server may bypass the normal conversation loop; it needs either a dedicated charge hook or an explicit unsupported notice.

The plan skips external billing APIs, hard cancellation of in-flight streams, and changing defaults for cron/kanban fleets.

## Release Order

1. Backward-compatible TurnBudget dimensions/fake-clock tests.
2. Main/codex charge points and loop/retry enforcement.
3. Degradation/finalizer/fallback semantics.
4. Session/day and delegation-tree pools.
5. Scheduler config/status surfaces and full verification.

## File Map

- Modify: `agent/iteration_budget.py` — `TurnBudget`, `BudgetPool`, state/charge/degrade data.
- Modify: `agent/turn_context.py`, `agent/agent_init.py` — construct configured budget/pools.
- Modify: `agent/conversation_loop.py` — loop/retry gates and charge integration.
- Modify: `agent/codex_runtime.py` — charge alternate accounting path.
- Modify: `agent/turn_finalizer.py`, `agent/chat_completion_helpers.py` — exhausted summary/budget failover reason.
- Modify: `agent/usage_pricing.py`, `hermes_state.py` — cost/token source/aggregate query as required.
- Modify: `tools/delegate_tool.py` — child allocation/tree pool reconciliation.
- Modify: `agent/credits_tracker.py`, `cli.py`, `tui_gateway/server.py`, `gateway/run.py` — notices/status/usage.
- Modify: `cron/scheduler.py`, `hermes_cli/config.py`, `cli-config.yaml.example` — job budgets/config.
- Test: new `tests/agent/test_turn_budget.py`, `tests/agent/test_budget_pools.py`, `tests/agent/test_budget_enforcement_e2e.py`.
- Test: extend conversation/compression/failover/delegation/usage/status/cron suites.

## Data Contracts

```python
@dataclass(frozen=True)
class BudgetLimits:
    iterations: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    usd: float | None = None
    seconds: float | None = None
    soft_fraction: float = 0.8
```

```python
@dataclass(frozen=True)
class BudgetState:
    used_iterations: int
    used_input_tokens: int
    used_output_tokens: int
    used_usd: float
    elapsed_seconds: float
    limits: BudgetLimits
    exhausted_dimension: str | None
    degraded: tuple[str, ...]
    unmetered_calls: int
```

```python
@dataclass(frozen=True)
class BudgetCharge:
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    elapsed_seconds: float
    pricing_status: str
    source: str
```

## Task 1: Extend IterationBudget to Multi-Dimensional TurnBudget

**Files:**
- Modify: `agent/iteration_budget.py`
- Modify: `agent/turn_context.py`, `agent/agent_init.py`
- Test: new `tests/agent/test_turn_budget.py`

- [ ] Step 1: Add fake-clock/dimension tests before implementation.

```python
def test_remaining_is_zero_when_any_dimension_is_exhausted():
    budget = TurnBudget(BudgetLimits(iterations=10, output_tokens=5), clock=FakeClock())
    budget.consume()
    budget.charge(BudgetCharge(0, 5, 0.01, 1.0, "known", "test"))
    assert budget.remaining == 0
    assert budget.state().exhausted_dimension == "output_tokens"


def test_existing_iteration_contract_remains_valid():
    budget = IterationBudget(max_total=2)
    assert budget.remaining == 2
    assert budget.consume() is True
    assert budget.refund() is True
    assert budget.remaining == 2
```

- [ ] Step 2: Implement `TurnBudget` as a compatible subclass/wrapper with lock-protected iteration usage, token/USD/elapsed counters, monotonic deadline, `charge`, `state`, `soft_threshold_reached`, `exhausted`, and `summary_allowance`.

- [ ] Step 3: Define stop-before-next-call semantics. `can_start_call()` checks hard dimensions/deadline; `consume()` still reserves an iteration; `charge()` records post-call usage and refunds reservation only through existing error path.

- [ ] Step 4: Add config parsing/defaults. Absent new limits creates the current iteration-only budget; invalid negative/NaN/too-large values fail config validation.

- [ ] Step 5: Construct per-turn budget at `turn_context.py` and preserve agent-init construction for legacy/direct callers. Pass a fake clock only in tests.

- [ ] Step 6: Run focused tests and commit.

```bash
python -m pytest tests/agent/test_turn_budget.py tests/agent/test_iteration_budget.py -q
 git add agent/iteration_budget.py agent/turn_context.py agent/agent_init.py tests/agent/test_turn_budget.py tests/agent/test_iteration_budget.py
 git diff --cached --check
git commit -m "feat(budget): add multi-dimensional turn budget"
```

## Task 2: Charge Main/Codex Paths and Enforce Loop/Retry Deadlines

**Files:**
- Modify: `agent/conversation_loop.py`
- Modify: `agent/codex_runtime.py`
- Modify: `agent/usage_pricing.py` only for explicit unknown-pricing status.
- Test: new `tests/agent/test_budget_enforcement_e2e.py`
- Test: usage/codex loop suites.

- [ ] Step 1: Add fake-provider tests.

```python
def test_main_loop_charges_tokens_cost_and_elapsed_once():
    agent = build_agent_with_budget(BudgetLimits(input_tokens=100, output_tokens=50, usd=1.0, seconds=30))
    run_fake_response(agent, input_tokens=10, output_tokens=5, cost=0.10, elapsed=2.0)
    state = agent.turn_budget.state()
    assert (state.used_input_tokens, state.used_output_tokens, state.used_usd) == (10, 5, 0.10)


def test_unknown_pricing_enforces_tokens_and_marks_unmetered_usd():
    agent = build_agent_with_budget(BudgetLimits(output_tokens=5, usd=1.0))
    run_fake_response(agent, input_tokens=0, output_tokens=5, cost=None, elapsed=1.0)
    assert agent.turn_budget.state().exhausted_dimension == "output_tokens"
    assert agent.turn_budget.state().unmetered_calls == 1
```

- [ ] Step 2: Add a single `charge_budget_from_usage` helper at the existing normalized usage/cost block. It consumes `CanonicalUsage`, cost result/pricing status, elapsed monotonic interval, and source (`main_loop`/`codex_runtime`). Guard with request id/turn id so retries/duplicate finalization cannot charge twice.

- [ ] Step 3: Call the helper from both main loop accounting and Codex runtime accounting. Do not route through optional plugin middleware. Pass `budget_state` to existing LLM middleware context for observability only.

- [ ] Step 4: Insert `can_start_call()` at loop gate and before retry/fallback attempt. On deadline, stop starting new calls and send existing budget-exhaustion/finalizer path.

- [ ] Step 5: Add tests for retry backoff deadline, duplicate charge, stream usage, exception/refund, and codex path.

```bash
python -m pytest \
  tests/agent/test_budget_enforcement_e2e.py \
  tests/agent/test_conversation_loop.py \
  tests/agent/test_usage_pricing.py \
  tests/agent/test_codex_runtime.py -q
```

- [ ] Step 6: Commit charge/gates.

```bash
git add agent/conversation_loop.py agent/codex_runtime.py agent/usage_pricing.py tests/agent/test_budget_enforcement_e2e.py tests/agent/test_conversation_loop.py tests/agent/test_usage_pricing.py tests/agent/test_codex_runtime.py
git diff --cached --check
git commit -m "feat(budget): enforce loop and retry ceilings"
```

## Task 3: Graceful Degradation and Exhaustion Semantics

**Files:**
- Modify: `agent/conversation_loop.py`
- Modify: `agent/chat_completion_helpers.py`
- Modify: `agent/turn_finalizer.py`
- Modify: `agent/agent_init.py`/runtime reasoning config.
- Test: budget/failover/finalizer suites.

- [ ] Step 1: Add degradation tests.

```python
def test_soft_budget_lowers_reasoning_effort_once():
    agent = build_agent_with_budget(BudgetLimits(usd=1.0, soft_fraction=0.8), cost=0.81)
    apply_budget_degradation(agent)
    assert agent.reasoning_config.effort == "low"
    apply_budget_degradation(agent)
    assert agent.reasoning_config.effort == "low"


def test_hard_budget_uses_summary_without_extra_main_call():
    agent = build_agent_with_budget(BudgetLimits(output_tokens=5), accumulated_output=5)
    result = finalize_budget_exhaustion(agent)
    assert result.outcome == "budget_exhausted"
    assert result.summary_call_count <= 1
```

- [ ] Step 2: Implement soft threshold detection per dimension. Lower reasoning effort only if transport/provider supports the configured field; otherwise skip to fallback and notice.

- [ ] Step 3: Add `FailoverReason.BUDGET`/equivalent runtime reason, choose the next cheaper configured fallback entry, and prevent generic `restore_primary_runtime()` from undoing the budget downgrade for the current turn. Restore normal config at turn end.

- [ ] Step 4: Preserve existing summary-on-iteration exhaustion. Add summary allowance/reserved tokens/time; if no allowance remains, return current partial result with a budget notice and persist reason.

- [ ] Step 5: Emit one notice per transition (`soft`, `fallback`, `hard`) with dimension, used/limit, pricing status, and whether a summary was attempted. Do not emit every token charge.

- [ ] Step 6: Run tests and commit.

```bash
python -m pytest \
  tests/agent/test_turn_finalizer.py \
  tests/agent/test_budget_enforcement_e2e.py \
  tests/agent/test_provider_failover.py \
  tests/agent/test_chat_completion_helpers.py -q
 git add agent/conversation_loop.py agent/chat_completion_helpers.py agent/turn_finalizer.py agent/agent_init.py tests/agent/test_turn_finalizer.py tests/agent/test_budget_enforcement_e2e.py tests/agent/test_provider_failover.py tests/agent/test_chat_completion_helpers.py
 git diff --cached --check
git commit -m "feat(budget): degrade gracefully on budget pressure"
```

## Task 4: Session/Day Budget Pools

**Files:**
- Modify: `agent/iteration_budget.py`
- Modify: `hermes_state.py`
- Modify: `agent/turn_context.py`, `agent/agent_init.py`
- Test: new `tests/agent/test_budget_pools.py`
- Test: state/migration/usage suites.

- [ ] Step 1: Add temp-DB tests.

```python
def test_day_pool_loads_today_cost_and_tokens(tmp_path):
    db = open_temp_state_db(tmp_path)
    insert_session_usage(db, day="2026-07-13", cost=0.60, input_tokens=100, output_tokens=20)
    pool = load_day_pool(db, day="2026-07-13", limits=BudgetLimits(usd=1.0, output_tokens=100))
    assert pool.state().used_usd == 0.60
    assert pool.state().remaining == 1


def test_session_pool_and_turn_pool_charge_once():
    session = BudgetPool(BudgetLimits(usd=2.0))
    turn = TurnBudget(BudgetLimits(usd=1.0), parent=session)
    charge = BudgetCharge(0, 0, 0.5, 1.0, "known", "test")
    turn.charge(charge)
    assert session.state().used_usd == 0.5
```

- [ ] Step 2: Add migration/query helper for daily session estimated cost/token totals. Do not alter historical session rows; unknown pricing is separate status and does not become zero-cost proof.

- [ ] Step 3: Build session/day pool at turn start, with configured limits and current aggregate. Charge turn → session → day once through parent references; lock across threads/process-local children.

- [ ] Step 4: Define precedence: a configured turn limit can be smaller than session/day; any exhausted parent blocks a new call. Day rollover creates a new pool using the next date.

- [ ] Step 5: Run temp DB/migration/concurrency tests and commit.

```bash
python -m pytest tests/agent/test_budget_pools.py tests/hermes_state/test_state_migrations.py tests/agent/test_usage_pricing.py -q
 git add agent/iteration_budget.py hermes_state.py agent/turn_context.py agent/agent_init.py tests/agent/test_budget_pools.py tests/hermes_state/test_state_migrations.py
 git diff --cached --check
git commit -m "feat(budget): add session and daily pools"
```

## Task 5: Delegation-Tree Allocation and Cron Budgets

**Files:**
- Modify: `tools/delegate_tool.py`
- Modify: `cron/scheduler.py`
- Modify: `agent/iteration_budget.py`
- Modify: `hermes_cli/config.py`, `cli-config.yaml.example`
- Test: delegation/cron/budget suites.

- [ ] Step 1: Add tests for child allocation, fan-out, over-allocation, cancellation/refund, and parent rollup.

```python
def test_children_draw_down_parent_tree_pool_without_double_charge():
    parent = BudgetPool(BudgetLimits(usd=2.0))
    child_a = parent.allocate(BudgetLimits(usd=1.0))
    child_b = parent.allocate(BudgetLimits(usd=1.0))
    child_a.charge(BudgetCharge(0, 1, 0.75, 1.0, "known", "child"))
    child_b.charge(BudgetCharge(0, 1, 0.75, 1.0, "known", "child"))
    assert parent.state().used_usd == 1.5
    assert parent.allocate(BudgetLimits(usd=1.0)).ok is False
```

- [ ] Step 2: Add `budget` fields to delegation input/config: child max USD/tokens/seconds or fraction of parent remaining, inherited day/session ceiling, and no-budget legacy behavior.

- [ ] Step 3: Allocate before spawning; pass the child `TurnBudget`/tree id into `delegate_tool`/agent init. If requested allocation exceeds parent remaining, clamp or reject according to config and report it before spawn.

- [ ] Step 4: Reconcile `delegate_tool` final child cost rollup with live charges: child pool/session/day charges are authoritative; final rollup updates parent display/session totals without charging again. Test concurrent children with locks.

- [ ] Step 5: Add cron job per-run budgets and detached behavior: no approval prompt; soft degrade/fallback then persist `budget_exhausted`/`timed_out` outcome with budget state.

- [ ] Step 6: Run focused tests and commit.

```bash
python -m pytest \
  tests/tools/test_delegate_tool.py \
  tests/tools/test_delegate_concurrency.py \
  tests/cron/test_scheduler_mcp_init.py \
  tests/agent/test_budget_pools.py -q
 git add tools/delegate_tool.py cron/scheduler.py agent/iteration_budget.py hermes_cli/config.py cli-config.yaml.example tests/tools/test_delegate_tool.py tests/tools/test_delegate_concurrency.py tests/cron/test_scheduler_mcp_init.py tests/agent/test_budget_pools.py
 git diff --cached --check
git commit -m "feat(budget): enforce delegation and cron allocations"
```

## Task 6: Status, Usage, Notices, and Codex-App-Server Decision

**Files:**
- Modify: `agent/credits_tracker.py`
- Modify: `cli.py`, `tui_gateway/server.py`, `gateway/run.py`
- Modify: `agent/codex_runtime.py`/app-server path if charge hook is feasible.
- Modify: docs/config.
- Test: status/usage/gateway/codex suites.

- [ ] Add budget state to `_get_status_bar_snapshot`, `/usage`, TUI usage response, and gateway notices: used/limit for iterations/tokens/USD/time, current degradation, pricing status, unmetered count, and tree id where relevant.
- [ ] Add `budget`/`budget status` config/CLI inspection without exposing secret provider data.
- [ ] Decide Codex app-server explicitly: if its runtime supplies canonical usage/cost/deadline callbacks, wire the same `BudgetCharge`; otherwise mark budget coverage `unsupported: codex_app_server` and keep configured wall-clock/process watchdog protection separate. Do not claim full coverage from the normal loop hook.
- [ ] Add notices once per threshold and tests for gateway delivery/backpressure.

```bash
python -m pytest \
  tests/agent/test_credits_tracker.py \
  tests/tui/test_usage.py \
  tests/gateway/test_usage.py \
  tests/agent/test_codex_app_server_compaction.py \
  tests/agent/test_budget_enforcement_e2e.py -q
```

- [ ] Commit surfaces/docs.

```bash
git add agent/credits_tracker.py cli.py tui_gateway/server.py gateway/run.py agent/codex_runtime.py docs website cli-config.yaml.example tests
 git diff --cached --check
git commit -m "docs(budget): surface cost and time ceilings"
```

## Task 7: Full Verification

**Files:**
- Test: all budget/loop/provider/delegation/cron/status suites.

- [ ] Run fake-clock overrun tests for each dimension and assert no call starts after hard exhaustion/deadline.
- [ ] Run real temp SessionDB daily aggregation/restart/resume and verify no double charge.
- [ ] Run concurrent delegation fan-out with child allocations and parent rollup.
- [ ] Run unknown pricing/included route and verify token/time enforcement plus explicit USD-unmetered notice.
- [ ] Run fallback/degradation/summary transitions and ensure current partial result is preserved.
- [ ] Run cron/kanban/detached budget exhaustion and verify persisted outcome/notice with no interactive approval.
- [ ] Run `codex_app_server` coverage test and verify the documented supported/unsupported result.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_turn_budget.py \
  tests/agent/test_budget_pools.py \
  tests/agent/test_budget_enforcement_e2e.py \
  tests/agent/test_conversation_loop.py \
  tests/agent/test_turn_finalizer.py \
  tests/agent/test_provider_failover.py \
  tests/agent/test_usage_pricing.py \
  tests/tools/test_delegate_tool.py \
  tests/tools/test_delegate_concurrency.py \
  tests/cron/test_scheduler_mcp_init.py \
  tests/agent/test_credits_tracker.py \
  tests/tui/test_usage.py \
  tests/gateway/test_usage.py -q
python3 -m compileall -q agent/iteration_budget.py
 git diff --check
```

- [ ] Commit final evidence/config docs.

```bash
git add tests docs website cli-config.yaml.example
git diff --cached --check
git commit -m "test(budget): verify multi-scope enforcement"
```

## Acceptance Checklist

- [ ] Existing iteration-budget callers/tests remain valid.
- [ ] Turn budgets enforce configured iterations/tokens/USD/time at loop/retry gates.
- [ ] Main and alternate Codex accounting paths charge exactly once.
- [ ] Soft degradation, cheaper fallback, and summary/partial-result paths are deterministic and visible.
- [ ] Session/day and delegation-tree pools are thread-safe and avoid double counting.
- [ ] Cron/detached children enforce budgets without interactive approval.
- [ ] Unknown pricing/unmetered routes still enforce non-USD dimensions and disclose coverage.
- [ ] CLI/TUI/gateway/status notices show budget progress without spam.
- [ ] In-flight streams/unsupported Codex app-server limitations are documented accurately.

## Deliberate Simplifications

- Skipped billing-provider reconciliation; USD remains estimated from current pricing metadata.
- Skipped hard cancellation of an in-flight stream; v1 is stop-before-next-call and retry-aware.
- Skipped making new limits default-on; unattended fleets need explicit rollout.
- Skipped charging every auxiliary/MoA route until those paths expose a common usage contract; report unmetered calls instead of false precision.
