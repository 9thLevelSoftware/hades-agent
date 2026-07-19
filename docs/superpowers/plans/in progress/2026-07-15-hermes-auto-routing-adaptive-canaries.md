# Hermes Auto Routing Conservative Adaptation and Canaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Learn contextual rankings among already approved route targets and reasoning efforts, evaluate changes through deterministic low-risk canaries, and promote, reject, cool down, freeze, or exactly roll back immutable adaptive revisions without ever widening user authority.

**Architecture:** Append-only evidence from Stage 3 updates profile/runtime/effort-specific posterior estimates. A coalescing background scheduler wakes one lease-elected optimizer after new evidence or another due trigger; the optimizer proposes a complete conservative overlay containing only approved target order and in-bounds reasoning changes. Experiments assign a bounded deterministic subset of eligible future decisions, persist the assignment before routing, and atomically advance the active revision only after policy and confidence gates pass.

**Tech Stack:** Python standard-library `dataclasses`, `hashlib`, `json`, `math`, `statistics.NormalDist`, SQLite/WAL transactions and leases, Pydantic v2 immutable records, the Stage 1 budget ledger, the Stage 2 decision pipeline, the Stage 3 evidence store, and pytest.

## Global Constraints

- This stage may reorder only targets already named in the authority profile and may tune only reasoning defaults inside each target's approved minimum/maximum. It cannot add runtimes, alter profile topology, change objectives, or change classifier/evaluator settings.
- Adaptation defaults to `adaptation.mode: disabled`. A separate preview/hash-gated operator transition to `conservative` is required before automatic optimization, proposal publication, or canary assignment; installing/upgrading the stage never enables it.
- Estimates are contextual by authority revision, profile lineage, domain, complexity, normalized capabilities, runtime key, and reasoning effort. Evidence from one context cannot promote a target in an unrelated context.
- External catalog evidence is a bounded cold-start prior. Local weighted evidence dominates only its matching context as observations accumulate.
- Canary assignment is deterministic, persisted before projection, and capped by both `adaptation.canary_fraction` and immutable `policy.max_canary_fraction`.
- High-risk tasks, policy-excluded tasks, unavailable runtimes, budget exhaustion, frozen adaptation, and unresolved authority changes always receive the control revision.
- A canary never changes a session after its first route. Resume and durable child recovery reuse the recorded decision, revision, experiment, and assignment.
- One optimizer lease spans proposal plus revision publication. Runtime readers never acquire that lease and only read a complete checksum-valid revision.
- Enabled, unfrozen adaptation schedules optimizer work automatically after evidence, at due session starts, inventory-state changes, and active-target policy breaches. It never waits for a human to run `optimize`, and it never runs provider/catalog work on the request thread.
- Promotion requires minimum samples and a positive configured confidence bound. Policy breach rolls back immediately; repeated operational failure or statistically credible regression rejects or rolls back and starts cooldown.
- `freeze` stops new posterior-driven mutations and experiment assignments without disabling current routing. `rollback` changes only the adaptive revision pointer, never YAML authority.
- Every guarded control-plane CLI operation in this stage remains preview-first and requires `--apply --expect-hash PREVIEW_HASH_FROM_THE_IMMEDIATELY_PRECEDING_PREVIEW`. Append-only evidence/observation commands inherited from earlier stages remain explicitly labeled exceptions and cannot invoke this stage's optimizer or move a revision pointer synchronously.
- No task content is copied into posterior, experiment, or revision rows.
- Before every Step 5 commit, run both `git diff --check` and the shown `git diff --cached --check`; stop on any output/error.
- Complete this plan only after all three preceding plans and their gates pass.

---

## File Map

### Plugin files created

- `plugins/auto_routing/auto_routing/learner.py` — contextual accumulators, utility estimates, and conservative proposals.
- `plugins/auto_routing/auto_routing/experiments.py` — deterministic assignment and experiment lifecycle.
- `plugins/auto_routing/auto_routing/optimizer.py` — coalescing non-blocking trigger scheduler and the single optimizer pass shared by automatic and CLI execution.

### Plugin files modified

- `plugins/auto_routing/__init__.py` — register due session-start and post-turn optimizer triggers without adding a model-visible tool.
- `plugins/auto_routing/auto_routing/models.py` — context, estimate, revision, proposal, experiment, and assignment records.
- `plugins/auto_routing/auto_routing/config.py` — adaptation controls and conservative authority validation.
- `plugins/auto_routing/auto_routing/storage.py` — contextual stats, optimizer lease, immutable revisions, experiments, and atomic pointers.
- `plugins/auto_routing/auto_routing/selector.py` — apply one complete revision and optional persisted canary overlay.
- `plugins/auto_routing/auto_routing/decisions.py` — record revision and experiment attribution.
- `plugins/auto_routing/auto_routing/service.py` — optimizer, freeze state, experiment assignment, and recovery orchestration.
- `plugins/auto_routing/auto_routing/cli.py` — `optimize`, `history`, `freeze`, `unfreeze`, and `rollback` commands.
- `plugins/auto_routing/README.md` — conservative adaptation operations.

### Tests created

- `tests/plugins/auto_routing/test_contextual_learner.py`
- `tests/plugins/auto_routing/test_optimizer_lease.py`
- `tests/plugins/auto_routing/test_adaptive_revisions.py`
- `tests/plugins/auto_routing/test_conservative_mutations.py`
- `tests/plugins/auto_routing/test_experiments.py`
- `tests/plugins/auto_routing/test_canary_integration.py`
- `tests/plugins/auto_routing/test_adaptation_cli.py`
- `tests/plugins/auto_routing/test_adaptation_properties.py`
- `tests/plugins/auto_routing/test_learning_simulation.py`
- `tests/plugins/auto_routing/test_optimizer_scheduler.py`

---

### Task 1: Accumulate Contextual Quality, Reliability, Latency, and Cost Estimates

**Files:**
- Create: `plugins/auto_routing/auto_routing/learner.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Create: `tests/plugins/auto_routing/test_contextual_learner.py`

- [ ] **Step 1: Write focused failing tests for contextual isolation and weighted evidence**

```python
from plugins.auto_routing.auto_routing.learner import ContextualAccumulator
from plugins.auto_routing.auto_routing.models import (
    ContextKey,
    EvidenceEvent,
    RuntimeKey,
    TaskContextBucket,
)


def _runtime_key() -> RuntimeKey:
    return RuntimeKey(
        provider="openai-codex",
        model="gpt-5.4",
        auth_identity="oauth:default",
        credential_pool_identity="pool:default",
        api_mode="codex_responses",
        inventory_revision="inv-1",
    )


def _key(domain: str, effort: str = "medium") -> ContextKey:
    return ContextKey(
        authority_id="auth-1",
        profile_id="coding",
        profile_lineage="coding",
        domain=domain,
        complexity="hard",
        capabilities=("tools",),
        runtime_id=_runtime_key().stable_id(),
        reasoning_effort=effort,
    )


def _event(
    event_id: str,
    *,
    quality: float | None,
    weight: float,
    reliability: float | None = None,
) -> EvidenceEvent:
    return EvidenceEvent(
        source_event_id=event_id,
        decision_id="ard-1",
        route_epoch=0,
        runtime_key=_runtime_key(),
        signal_type="objective_quality",
        source="objective",
        occurred_at="2026-07-15T12:00:00Z",
        quality_value=quality,
        reliability_value=reliability,
        latency_seconds=4.0,
        effective_cost_usd=0.02,
        input_tokens=100,
        output_tokens=20,
        confidence_weight=weight,
        verifier_identity="local-tests",
        context_bucket=TaskContextBucket(
            authority_id="auth-1",
            profile_id="coding",
            profile_lineage="coding",
            domains=("coding",),
            complexity_band="hard",
            capabilities=("tools",),
            runtime_id=_key("coding").runtime_id,
            reasoning_effort="medium",
        ),
        metadata={},
    )


def test_observations_update_only_the_exact_context() -> None:
    coding = ContextualAccumulator.cold_start(_key("coding"), quality_prior=0.60)
    writing = ContextualAccumulator.cold_start(_key("writing"), quality_prior=0.60)

    updated = coding.observe(_event("evt-1", quality=1.0, weight=0.9))

    assert updated.quality.mean > coding.quality.mean
    assert writing.quality == ContextualAccumulator.cold_start(
        _key("writing"), quality_prior=0.60
    ).quality


def test_strong_objective_signal_outweighs_weak_behavioral_proxy() -> None:
    accumulator = ContextualAccumulator.cold_start(_key("coding"), quality_prior=0.50)
    weak = _event("evt-weak", quality=1.0, weight=0.10).model_copy(
        update={"source": "behavioral"}
    )
    strong = _event("evt-strong", quality=0.0, weight=1.0)

    result = accumulator.observe(weak).observe(strong)

    assert result.quality.mean < 0.50


def test_reliability_only_event_does_not_change_quality() -> None:
    accumulator = ContextualAccumulator.cold_start(_key("coding"), quality_prior=0.50)
    event = _event("evt-reliability", quality=None, reliability=0.0, weight=0.20)
    result = accumulator.observe(event)
    assert result.quality == accumulator.quality
    assert result.reliability.mean < accumulator.reliability.mean


def test_quality_prior_never_becomes_reliability_prior() -> None:
    accumulator = ContextualAccumulator.cold_start(
        _key("coding"), quality_prior=0.90, reliability_prior=0.50
    )
    assert accumulator.quality.mean > accumulator.reliability.mean
```

- [ ] **Step 2: Run the focused tests and record RED**

Run:

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_contextual_learner.py
```

Expected: collection fails because `learner.py` and the contextual records do not exist.

- [ ] **Step 3: Add immutable context and accumulator records**

Implement these exact update rules in `learner.py`:

```python
@dataclass(frozen=True)
class WeightedBeta:
    alpha: float
    beta: float
    weight_sum: float = 0.0

    def observe(self, value: float, weight: float) -> "WeightedBeta":
        if not 0.0 <= value <= 1.0 or not 0.0 < weight <= 1.0:
            raise ValueError("weighted beta observations require value in [0,1] and weight in (0,1]")
        return WeightedBeta(
            alpha=self.alpha + (weight * value),
            beta=self.beta + (weight * (1.0 - value)),
            weight_sum=self.weight_sum + weight,
        )

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        total = self.alpha + self.beta
        return (self.alpha * self.beta) / ((total * total) * (total + 1.0))


@dataclass(frozen=True)
class WeightedMoments:
    weight_sum: float = 0.0
    mean: float = 0.0
    m2: float = 0.0

    def observe(self, value: float, weight: float) -> "WeightedMoments":
        if value < 0.0 or not 0.0 < weight <= 1.0:
            raise ValueError("weighted moments require a non-negative value and weight in (0,1]")
        total = self.weight_sum + weight
        delta = value - self.mean
        mean = self.mean + ((weight / total) * delta)
        m2 = self.m2 + (weight * delta * (value - mean))
        return WeightedMoments(total, mean, m2)

    @property
    def sample_variance(self) -> float:
        return self.m2 / self.weight_sum if self.weight_sum > 0.0 else 0.0


def _prior_beta(prior: float, confidence: float) -> WeightedBeta:
    if not 0.0 <= prior <= 1.0:
        raise ValueError("prior must be in [0,1]")
    strength = 4.0 * confidence
    return WeightedBeta(
        alpha=1.0 + (strength * prior),
        beta=1.0 + (strength * (1.0 - prior)),
    )


@dataclass(frozen=True)
class ContextualAccumulator:
    key: ContextKey
    quality: WeightedBeta
    reliability: WeightedBeta
    latency: WeightedMoments = WeightedMoments()
    cost: WeightedMoments = WeightedMoments()

    @classmethod
    def cold_start(
        cls,
        key: ContextKey,
        *,
        quality_prior: float,
        reliability_prior: float = 0.50,
        confidence: float = 0.5,
    ) -> "ContextualAccumulator":
        bounded = min(max(confidence, 0.0), 1.0)
        return cls(
            key=key,
            quality=_prior_beta(quality_prior, bounded),
            reliability=_prior_beta(reliability_prior, bounded),
        )

    def observe(self, event: EvidenceEvent) -> "ContextualAccumulator":
        weight = event.confidence_weight
        quality = self.quality
        reliability = self.reliability
        latency = self.latency
        cost = self.cost
        if event.quality_value is not None:
            quality = quality.observe(event.quality_value, weight)
        if event.reliability_value is not None:
            reliability = reliability.observe(event.reliability_value, weight)
        if event.latency_seconds is not None:
            latency = latency.observe(event.latency_seconds, weight)
        if event.effective_cost_usd is not None:
            cost = cost.observe(event.effective_cost_usd, weight)
        return dataclasses.replace(
            self,
            quality=quality,
            reliability=reliability,
            latency=latency,
            cost=cost,
        )
```

Initialize each catalog quality or reliability prior independently as `alpha=1 + 4 * prior * confidence`, `beta=1 + 4 * (1 - prior) * confidence`, with catalog confidence clamped to `[0, 1]`. Missing reliability evidence uses the explicit conservative `0.50` prior, never the quality prior. Reliability observations come from operational success/failure or an event explicitly normalized with a separate reliability label; a quality outcome is not implicitly dual-labeled. Normalize domain and capabilities before constructing the frozen `ContextKey`; do not put prompt text, session IDs, or task IDs in it. Consume each `(context_key, source_event_id)` at most once through the storage transaction introduced in Task 2.

- [ ] **Step 4: Run learner tests and the Stage 3 evidence regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_contextual_learner.py \
  tests/plugins/auto_routing/test_evidence_normalization.py \
  tests/plugins/auto_routing/test_evidence_hooks.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the contextual accumulator**

```bash
git add plugins/auto_routing/auto_routing/models.py plugins/auto_routing/auto_routing/learner.py tests/plugins/auto_routing/test_contextual_learner.py
git diff --cached --check
git commit -m "feat(auto-routing): add contextual outcome estimates"
```

---

### Task 2: Persist Estimates, Elect One Optimizer, and Publish Complete Revisions Atomically

**Files:**
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Create: `tests/plugins/auto_routing/test_optimizer_lease.py`
- Create: `tests/plugins/auto_routing/test_adaptive_revisions.py`

- [ ] **Step 1: Write failing lease and interrupted-publication tests**

```python
def test_only_one_live_optimizer_owns_the_lease(routing_store, frozen_clock) -> None:
    assert routing_store.acquire_optimizer_lease("owner-a", ttl_seconds=30)
    assert not routing_store.acquire_optimizer_lease("owner-b", ttl_seconds=30)
    frozen_clock.advance(seconds=31)
    assert routing_store.acquire_optimizer_lease("owner-b", ttl_seconds=30)


def test_reader_never_observes_an_incomplete_revision(routing_store, baseline_revision) -> None:
    routing_store.publish_revision(baseline_revision, expected_active_id=None)
    interrupted = baseline_revision.model_copy(
        update={"revision_id": "rev-interrupted", "parent_revision_id": baseline_revision.revision_id}
    )

    with pytest.raises(InjectedPublicationFailure):
        routing_store.publish_revision(
            interrupted,
            expected_active_id=baseline_revision.revision_id,
            fault_after_insert=True,
        )

    assert routing_store.read_active_revision("auth-1") == baseline_revision
    assert routing_store.read_revision("rev-interrupted") is None
```

Define `routing_store`, `frozen_clock`, and `baseline_revision` as local pytest fixtures at the top of the two test files: create a temporary profile-local database with `RoutingStore`, inject a mutable UTC clock, and build an `AdaptiveRevision` whose overlay preserves the `coding` profile's two approved targets. Define `InjectedPublicationFailure` in `storage.py` solely as the typed exception raised by the fault-injection branch; production callers never pass `fault_after_insert=True`.

- [ ] **Step 2: Run both tests and record RED**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_optimizer_lease.py \
  tests/plugins/auto_routing/test_adaptive_revisions.py
```

Expected: failures report missing lease and revision APIs.

- [ ] **Step 3: Add declarative tables and bounded transactional methods**

Add these tables to the existing `SCHEMA_SQL` source of truth:

```sql
CREATE TABLE IF NOT EXISTS contextual_estimates (
    context_key TEXT PRIMARY KEY,
    authority_id TEXT NOT NULL,
    accumulator_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contextual_estimate_events (
    context_key TEXT NOT NULL REFERENCES contextual_estimates(context_key),
    source_event_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    PRIMARY KEY (context_key, source_event_id)
);

CREATE TABLE IF NOT EXISTS optimizer_leases (
    lease_name TEXT PRIMARY KEY,
    owner_token TEXT NOT NULL,
    expires_at REAL NOT NULL,
    renewed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS adaptive_revisions (
    revision_id TEXT PRIMARY KEY,
    authority_id TEXT NOT NULL,
    parent_revision_id TEXT,
    document_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS active_adaptive_revisions (
    authority_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES adaptive_revisions(revision_id),
    updated_at TEXT NOT NULL
);
```

Use canonical JSON (`sort_keys=True`, separators `(",", ":")`) and SHA-256 checksums. In one immediate transaction, `consume_evidence()` first inserts the cold-start `contextual_estimates` row with `ON CONFLICT DO NOTHING`, then inserts `(context_key, source_event_id)` with `ON CONFLICT DO NOTHING`; it updates the accumulator only when the event insert changed one row. `publish_revision()` retains the Stage 1 table contract: verify the expected active pointer, insert `complete=0`, read and checksum the inserted document, mark it complete, compare-and-swap the active pointer, and commit. Any exception rolls back the whole transaction. `read_active_revision()` joins on `complete=1`, verifies the checksum, and returns the last complete revision or the baseline overlay. Use the existing bounded SQLite busy retry helper; do not wait indefinitely.

Acquire the lease with one conditional upsert whose `WHERE` clause permits the current owner or an expired row. Read the row back in the same immediate transaction and return true only when `owner_token` matches. Renewal and release require the same owner token.

- [ ] **Step 4: Run revision, lease, migration, and WAL tests**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_optimizer_lease.py \
  tests/plugins/auto_routing/test_adaptive_revisions.py \
  tests/plugins/auto_routing/test_storage.py \
  tests/plugins/auto_routing/test_storage_concurrency.py
```

Expected: all selected tests pass, including the injected interruption.

- [ ] **Step 5: Commit atomic adaptive storage**

```bash
git add plugins/auto_routing/auto_routing/models.py plugins/auto_routing/auto_routing/storage.py tests/plugins/auto_routing/test_optimizer_lease.py tests/plugins/auto_routing/test_adaptive_revisions.py
git diff --cached --check
git commit -m "feat(auto-routing): publish adaptive revisions atomically"
```

---

### Task 3: Propose Only Approved Target-Order and Reasoning Changes

**Files:**
- Modify: `plugins/auto_routing/auto_routing/learner.py`
- Modify: `plugins/auto_routing/auto_routing/config.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Create: `tests/plugins/auto_routing/test_conservative_mutations.py`

- [ ] **Step 1: Write failing authority-boundary and utility tests**

```python
def test_conservative_proposal_cannot_add_a_runtime(authority, learner) -> None:
    proposal = learner.propose(
        authority,
        requested_targets={"coding": ["unapproved|model|chat_completions|api_key"]},
    )

    assert proposal.rejected
    assert proposal.reason == "target_not_present_in_authority_profile"


def test_proposal_can_reorder_approved_targets_and_raise_effort_in_bounds(
    authority,
    learner,
    contextual_estimates,
    anthropic_runtime_id,
    codex_runtime_id,
) -> None:
    proposal = learner.propose(authority, estimates=contextual_estimates)

    assert proposal.overlay.profiles["coding"].target_order == (
        anthropic_runtime_id,
        codex_runtime_id,
    )
    assert proposal.overlay.profiles["coding"].reasoning_defaults[
        anthropic_runtime_id
    ] == "high"
    assert proposal.changed_fields == (
        "profiles.coding.target_order",
        f"profiles.coding.reasoning_defaults.{anthropic_runtime_id}",
    )
```

At the top of the test file, build `anthropic_runtime_id` and
`codex_runtime_id` from concrete `RuntimeKey(...).stable_id()` records. Define
`authority` with exactly those two targets, `learner` with a deterministic
`NormalDist().inv_cdf` confidence level of `0.95`, and `contextual_estimates`
where the Anthropic/high combination has the larger lower confidence utility
bound.

- [ ] **Step 2: Run the focused mutation tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_conservative_mutations.py
```

Expected: failures report missing proposal and conservative-envelope validation.

- [ ] **Step 3: Implement explicit utility and mutation guards**

For profile weights `wq`, `wr`, `wl`, and `wc`, compute:

```text
quality_utility    = quality_mean
reliability_utility = reliability_mean
latency_utility    = 1 - min(latency_mean / max_estimated_latency_seconds, 1)
cost_utility       = 1 - min(cost_mean / max_estimated_task_cost_usd, 1)
utility_mean       = wq*quality_utility + wr*reliability_utility + wl*latency_utility + wc*cost_utility
utility_variance   = wq^2*quality_variance + wr^2*reliability_variance
                   + wl^2*(latency_sample_variance / max(weight_sum, 1)) / max_estimated_latency_seconds^2
                   + wc^2*(cost_sample_variance / max(weight_sum, 1)) / max_estimated_task_cost_usd^2
utility_lcb        = utility_mean - NormalDist().inv_cdf(0.95) * sqrt(utility_variance)
```

If either ceiling is absent, use the maximum finite eligible-candidate estimate for that normalization; if all estimates are unknown, assign zero utility and maximum uncertainty for that component. Tie-break in this order: higher lower confidence bound, higher reliability mean, lower estimated cost, lower estimated latency, original authority order, stable runtime ID.

`validate_conservative_overlay()` must prove all of the following before returning a proposal:

1. the authority ID matches;
2. profile IDs and lineage are unchanged;
3. each target-order list is an exact permutation of that profile's authority primary plus fallbacks;
4. reasoning defaults use the existing ordered effort lattice and fall inside both target and global bounds;
5. objectives, match rules, immutable policy, classifier, evaluator, and profile constraints are byte-equivalent to authority;
6. every changed field is listed in `changed_fields` and every listed field actually changed.

- [ ] **Step 4: Run mutation plus selector regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_conservative_mutations.py \
  tests/plugins/auto_routing/test_selector.py \
  tests/plugins/auto_routing/test_models_config.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit conservative proposals**

```bash
git add plugins/auto_routing/auto_routing/learner.py plugins/auto_routing/auto_routing/config.py plugins/auto_routing/auto_routing/models.py tests/plugins/auto_routing/test_conservative_mutations.py
git diff --cached --check
git commit -m "feat(auto-routing): constrain adaptive route proposals"
```

---

### Task 4: Assign Deterministic Budgeted Canaries Before Route Projection

**Files:**
- Create: `plugins/auto_routing/auto_routing/experiments.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Create: `tests/plugins/auto_routing/test_experiments.py`

- [ ] **Step 1: Write failing deterministic-allocation and risk-gate tests**

```python
from plugins.auto_routing.auto_routing.experiments import assignment_bucket


def test_assignment_bucket_is_stable_and_bounded() -> None:
    first = assignment_bucket("exp-1", "operation-hmac-1")
    second = assignment_bucket("exp-1", "operation-hmac-1")
    assert first == second
    assert 0.0 <= first < 1.0


def test_assignment_never_exceeds_effective_fraction(experiment_service, low_risk_requests) -> None:
    assignments = [experiment_service.assign(request) for request in low_risk_requests]
    canaries = sum(item.arm == "challenger" for item in assignments)
    expected = sum(
        assignment_bucket("exp-1", request.assignment_key) < 0.03
        for request in low_risk_requests
    )
    assert expected > 0
    assert canaries == expected


def test_high_risk_or_exhausted_budget_always_gets_control(
    experiment_service, high_risk_request
) -> None:
    experiment_service.budget_ledger.exhaust("experiment", day="2026-07-15")
    assignment = experiment_service.assign(high_risk_request)
    assert assignment.arm == "control"
    assert assignment.reason in {"high_risk", "experiment_budget_exhausted"}


def test_shadow_assignment_never_reserves_or_attributes_challenger_spend(
    experiment_service, low_risk_requests
) -> None:
    before = experiment_service.budget_ledger.snapshot("experiment")
    assignment = experiment_service.assign(low_risk_requests[0], mode="shadow")
    assert assignment.hypothetical is True
    assert assignment.reservation_id is None
    experiment_service.record_outcome(assignment, quality=1.0)
    assert experiment_service.challenger_observation_count(assignment.experiment_id) == 0
    assert experiment_service.budget_ledger.snapshot("experiment") == before
```

Build `low_risk_requests` in the test file as 10,000 `ExperimentRequest` instances with sequential stable assignment keys derived from their content-free operation keys, unrelated random decision IDs, an authority canary fraction of `0.03`, immutable maximum `0.05`, verified control/challenger targets, and finite worst-case cost. Build `high_risk_request` by copying one request with `risk="high"`.

- [ ] **Step 2: Run experiment tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_experiments.py
```

Expected: collection fails because experiment assignment does not exist.

- [ ] **Step 3: Implement the exact hash, gates, reservation, and idempotent insert**

```python
def assignment_bucket(experiment_id: str, assignment_key: str) -> float:
    digest = hashlib.sha256(
        f"{experiment_id}\0{assignment_key}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)
```

`ExperimentRequest.assignment_key` is the Plan 2 stable, profile-keyed HMAC of canonical `(scope, operation_id, task_index, route_epoch)`; it is not the random `decision_id`. The effective fraction is `min(adaptation.canary_fraction, policy.max_canary_fraction)`. Evaluate gates in this order and store the first reason: adaptation frozen/disabled, incomplete experiment, authority mismatch, risk exclusion, target not verified, target unavailable, cooldown, fraction zero, hash outside fraction, unknown/unbounded worst-case price, or budget reservation failure. Only then choose `challenger`; otherwise choose `control`.

In `active`, reserve the conservative worst-case incremental experiment cost before storing a challenger assignment and reconcile it after Stage 3 operational evidence arrives. In `shadow`, persist only a hypothetical arm with `reservation_id=NULL` and `outcome_attributable=0`; the baseline outcome cannot update challenger posterior or experiment spend. Add `experiments` and `experiment_assignments` tables with `UNIQUE(experiment_id, assignment_key)` and a foreign decision ID for attribution. Insert or read the assignment in one immediate transaction so concurrent processes return the same arm. Persist revision IDs, target IDs, reasoning efforts, bucket, reason, and reservation ID—never request content.

- [ ] **Step 4: Run experiments, budgets, and concurrency regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_experiments.py \
  tests/plugins/auto_routing/test_budget_ledger.py \
  tests/plugins/auto_routing/test_storage_concurrency.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit deterministic canaries**

```bash
git add plugins/auto_routing/auto_routing/experiments.py plugins/auto_routing/auto_routing/storage.py plugins/auto_routing/auto_routing/models.py tests/plugins/auto_routing/test_experiments.py
git diff --cached --check
git commit -m "feat(auto-routing): assign deterministic budgeted canaries"
```

---

### Task 5: Promote, Reject, Cool Down, and Roll Back with Confidence Gates

**Files:**
- Modify: `plugins/auto_routing/auto_routing/experiments.py`
- Modify: `plugins/auto_routing/auto_routing/config.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Create: `tests/plugins/auto_routing/test_learning_simulation.py`

- [ ] **Step 1: Write failing lifecycle simulations**

```python
def test_challenger_promotes_only_after_sample_and_confidence_gates(simulation) -> None:
    experiment = simulation.start(minimum_samples=20, confidence=0.95)
    simulation.feed(experiment, control=[0.70] * 20, challenger=[0.82] * 19)
    assert simulation.evaluate(experiment).action == "continue"

    simulation.feed(experiment, challenger=[0.82])
    result = simulation.evaluate(experiment)
    assert result.action == "promote"
    assert result.delta_lcb > 0.0


def test_policy_breach_rolls_back_without_waiting_for_samples(simulation) -> None:
    promoted = simulation.promoted_revision()
    result = simulation.evaluate_promoted(
        promoted,
        observations=[],
        policy_breach="runtime_became_ineligible",
    )
    assert result.action == "rollback"
    assert result.cooldown_until is not None
    assert simulation.store.read_active_revision("auth-1").revision_id == promoted.parent_revision_id


def test_three_consecutive_operational_failures_reject_challenger(simulation) -> None:
    experiment = simulation.start(minimum_samples=20)
    simulation.record_operational_failures(experiment, count=3)
    assert simulation.evaluate(experiment).action == "reject"


def test_experiment_budget_exhaustion_rejects_or_rolls_back_with_cooldown(
    simulation,
) -> None:
    experiment = simulation.start(minimum_samples=20)
    rejected = simulation.evaluate(experiment, budget_exhausted=True)
    assert rejected.action == "reject"
    assert rejected.cooldown_until is not None

    promoted = simulation.promoted_revision()
    rolled_back = simulation.evaluate_promoted(
        promoted, observations=[], budget_exhausted=True
    )
    assert rolled_back.action == "rollback"
    assert rolled_back.cooldown_until is not None
```

Define the `simulation` fixture inside the test file with an in-memory fake UTC clock, real `RoutingStore`, fixed profile objectives, and `ExperimentLifecycle`; it translates each numeric list item into a quality observation of weight `1.0`, emits a separate successful operational reliability observation for that sample, and records fixed compliant cost/latency. The two dimensions must remain separate events/fields even when the fixture gives both the same sample index.

- [ ] **Step 2: Run the simulation and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_learning_simulation.py
```

Expected: failures report missing lifecycle evaluation and rollback transitions.

- [ ] **Step 3: Implement unambiguous lifecycle thresholds**

Add validated controls:

```yaml
adaptation:
  minimum_canary_samples: 20
  maximum_canary_samples: 200
  confidence: 0.95
  rollback_threshold: 0.10
  consecutive_failure_limit: 3
  cooldown_hours: 24
  rollback_window_samples: 20
```

Validate `maximum_canary_samples >= minimum_canary_samples` and
`rollback_window_samples > 0`.

For independent control and challenger utility posteriors, calculate:

```text
delta_mean = challenger.utility_mean - control.utility_mean
delta_sd   = sqrt(challenger.utility_variance + control.utility_variance)
z          = NormalDist().inv_cdf(confidence)
delta_lcb  = delta_mean - z * delta_sd
delta_ucb  = delta_mean + z * delta_sd
```

Apply transitions in this order:

1. policy breach: reject a canary or roll back a promoted revision immediately;
2. experiment/monitoring overhead-budget exhaustion: reject or roll back immediately and start cooldown;
3. `consecutive_failure_limit` operational failures: reject/roll back immediately;
4. before both arms reach `minimum_canary_samples`: continue;
5. `delta_ucb < -rollback_threshold`: reject and cool down;
6. `delta_lcb > 0`: promote the complete challenger revision atomically;
7. otherwise continue until the configured maximum sample count, then reject as inconclusive.

Monitor a promoted revision over `rollback_window_samples`; if its comparison `delta_ucb < -rollback_threshold`, atomically restore its recorded parent and cool down the mutation. Every transition appends an event with the evidence window IDs, bounds, authority/revision IDs, and reason.

- [ ] **Step 4: Run lifecycle and revision tests**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_learning_simulation.py \
  tests/plugins/auto_routing/test_experiments.py \
  tests/plugins/auto_routing/test_adaptive_revisions.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit experiment lifecycle behavior**

```bash
git add plugins/auto_routing/auto_routing/experiments.py plugins/auto_routing/auto_routing/config.py plugins/auto_routing/auto_routing/models.py plugins/auto_routing/auto_routing/storage.py tests/plugins/auto_routing/test_learning_simulation.py
git diff --cached --check
git commit -m "feat(auto-routing): gate promotion and automatic rollback"
```

---

### Task 6: Integrate Revisions and Canaries with Decisions and Durable Recovery

**Files:**
- Modify: `plugins/auto_routing/auto_routing/selector.py`
- Modify: `plugins/auto_routing/auto_routing/decisions.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Create: `tests/plugins/auto_routing/test_canary_integration.py`

- [ ] **Step 1: Write failing routing and restart tests**

```python
def test_canary_decision_records_complete_attribution(auto_service, canary_request) -> None:
    decision = auto_service.decide(canary_request)
    stored = auto_service.store.read_decision(decision.decision_id)

    assert stored.adaptive_revision_id == "rev-control"
    assert stored.experiment_id == "exp-1"
    assert stored.experiment_arm == "challenger"
    assert stored.projected_revision_id == "rev-challenger"


def test_restart_reuses_original_canary_assignment(service_factory, canary_request) -> None:
    first_service = service_factory()
    first = first_service.decide(canary_request)
    first_service.close()

    recovered_service = service_factory()
    recovered = recovered_service.decide(canary_request)

    assert recovered.decision_id == first.decision_id
    assert recovered.experiment_id == first.experiment_id
    assert recovered.experiment_arm == first.experiment_arm
    assert recovered.projected_revision_id == first.projected_revision_id


def test_crash_retry_uses_same_arm_despite_new_random_decision_id(
    service_factory, canary_request, profile_hmac_key
) -> None:
    crashing = service_factory(fail_after_assignment=True)
    with pytest.raises(InjectedCrash):
        crashing.decide(canary_request)
    abandoned_id = crashing.id_factory.issued_ids[-1]
    crashing.close()

    recovered = service_factory().decide(canary_request)
    expected_key = canary_request.operation.assignment_key(profile_hmac_key)
    assert recovered.decision_id != abandoned_id
    assert recovered.assignment_key == expected_key
    assert recovered.experiment_arm == "challenger"
```

Define `service_factory` in the test file so every instance opens the same
temporary on-disk database and the same verified two-target inventory. Inject
the Plan 2 `DecisionService.id_factory` with a counted factory that returns a
different valid random decision ID on each attempted construction. Define
`canary_request` with a fixed operation ID/task index whose
`assignment_bucket("exp-1", canary_request.operation.assignment_key(profile_hmac_key))` falls inside the
configured fraction. Add a crash-before-commit retry: despite receiving a new
decision ID, it must compute the same bucket/arm from the stable assignment
key. After a committed restart, the unique operation lookup returns the
committed decision before the ID factory is consulted again.

- [ ] **Step 2: Run the integration test and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_canary_integration.py
```

Expected: failures show missing revision/experiment attribution or a new assignment after restart.

- [ ] **Step 3: Apply one immutable snapshot in the decision transaction**

`AutoRoutingService.decide()` must perform this sequence:

1. look up an existing decision by session identity or child operation ID/task index and return it unchanged;
2. read YAML authority and one complete active adaptive revision;
3. validate/rebase status; use baseline authority when the overlay is invalid;
4. classify and select against that fixed control snapshot;
5. derive `assignment_key = request.operation.assignment_key(service.profile_hmac_key)` from the Plan 2 profile-keyed canonical operation HMAC, construct an unrelated proposed decision ID, then open one short `BEGIN IMMEDIATE` transaction and recheck the unique operation key;
6. call `ExperimentService.assign_in_txn()` using `assignment_key`, fixed context, and the same connection, including budget reservation and assignment insert; the random decision ID is stored only as attribution after assignment and never enters the bucket hash or assignment uniqueness key;
7. if the assignment is challenger, apply its already-loaded complete validated overlay through the pure selector before final target selection;
8. insert both control/projected revision IDs, experiment attribution, and the complete decision, then commit assignment and decision together;
9. only after commit return the decision for adapter projection. If the operation-key recheck found an existing committed decision, roll back the proposed ID and return that row unchanged.

Do not re-read the active revision between steps 2 and 9 and do not hold the
write transaction across classifier/provider/network calls. A crash before
commit leaves neither an assignment nor a decision; a committed row is reused
after restart. `shadow` may calculate and record a hypothetical assignment with
no spend reservation and `outcome_attributable=0`, always projects the baseline
Hermes runtime, and never feeds that baseline outcome to challenger estimates.
`freeze` returns control
assignments and leaves the currently active revision intact.

- [ ] **Step 4: Run route, cache, delegation, and restart regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_canary_integration.py \
  tests/plugins/auto_routing/test_fresh_session_routing.py \
  tests/plugins/auto_routing/test_delegation_routing.py \
  tests/plugins/auto_routing/test_auto_fallback.py \
  tests/gateway/test_agent_cache.py \
  tests/tools/test_async_delegation.py
```

Expected: all selected tests pass; later turns never get reassigned.

- [ ] **Step 5: Commit canary route integration**

```bash
git add plugins/auto_routing/auto_routing/selector.py plugins/auto_routing/auto_routing/decisions.py plugins/auto_routing/auto_routing/service.py plugins/auto_routing/auto_routing/storage.py tests/plugins/auto_routing/test_canary_integration.py
git diff --cached --check
git commit -m "feat(auto-routing): persist canary route attribution"
```

---

### Task 7: Automate Optimization and Add Freeze, History, Exact Rollback, and the Stage Gate

**Files:**
- Modify: `plugins/auto_routing/__init__.py`
- Create: `plugins/auto_routing/auto_routing/optimizer.py`
- Modify: `plugins/auto_routing/auto_routing/cli.py`
- Modify: `plugins/auto_routing/auto_routing/config.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Modify: `plugins/auto_routing/README.md`
- Create: `tests/plugins/auto_routing/test_optimizer_scheduler.py`
- Create: `tests/plugins/auto_routing/test_adaptation_cli.py`
- Create: `tests/plugins/auto_routing/test_adaptation_properties.py`

- [ ] **Step 1: Write failing CLI and seeded property tests**

```python
def test_freeze_is_preview_first_and_hash_guarded(run_auto_cli) -> None:
    preview = run_auto_cli("freeze")
    assert preview.exit_code == 0
    assert preview.json["applied"] is False
    assert preview.json["mutation"] == {"adaptation_frozen": True}

    rejected = run_auto_cli("freeze", "--apply", "--expect-hash", "wrong")
    assert rejected.exit_code == 2

    applied = run_auto_cli(
        "freeze", "--apply", "--expect-hash", preview.json["precondition_hash"]
    )
    assert applied.exit_code == 0
    assert applied.json["applied"] is True


def test_conservative_mode_requires_explicit_hash_guarded_transition(run_auto_cli) -> None:
    assert run_auto_cli("status", "--json").json["adaptation_mode"] == "disabled"
    preview = run_auto_cli("mode", "conservative")
    assert preview.json["applied"] is False
    assert preview.json["from"] == "disabled"
    assert preview.json["to"] == "conservative"

    applied = run_auto_cli(
        "mode", "conservative", "--apply", "--expect-hash",
        preview.json["precondition_hash"],
    )
    assert applied.json["applied"] is True


def test_seeded_mutations_never_escape_authority(seed_authority_and_mutations) -> None:
    authority, proposals = seed_authority_and_mutations(seed=20260715, count=500)
    for proposal in proposals:
        result = validate_conservative_overlay(authority, proposal.overlay)
        if result.accepted:
            assert set(result.overlay.profiles) == set(authority.profiles)
            for profile_id, authority_profile in authority.profiles.items():
                assert set(result.overlay.profiles[profile_id].target_order) == set(
                    authority_profile.approved_runtime_ids
                )


def test_rollback_restores_canonical_revision_bytes(routing_store, revision_chain) -> None:
    original = routing_store.read_revision(revision_chain[0]).canonical_json()
    routing_store.rollback(revision_chain[0], expected_active_id=revision_chain[-1])
    restored = routing_store.read_active_revision("auth-1").canonical_json()
    assert restored == original


def test_new_evidence_schedules_one_nonblocking_optimizer_pass(
    optimizer_scheduler, manual_thread_launcher
) -> None:
    assert optimizer_scheduler.trigger("new_evidence") is True
    assert optimizer_scheduler.trigger("new_evidence") is False
    assert manual_thread_launcher.pending_count == 1
    assert optimizer_scheduler.runner.run_count == 0

    manual_thread_launcher.run_next()

    assert optimizer_scheduler.runner.run_count == 1


def test_disabled_or_frozen_adaptation_never_schedules(
    optimizer_scheduler, manual_thread_launcher
) -> None:
    optimizer_scheduler.set_enabled(False)
    assert optimizer_scheduler.trigger("new_evidence") is False
    optimizer_scheduler.set_enabled(True)
    optimizer_scheduler.set_frozen(True)
    assert optimizer_scheduler.trigger("session_start_due") is False
    assert manual_thread_launcher.pending_count == 0
```

Define `run_auto_cli`, `seed_authority_and_mutations`, `routing_store`, and `revision_chain` in the CLI/property test files. In `test_optimizer_scheduler.py`, define `ManualThreadLauncher`, a real `OptimizerScheduler`, a counted fake `OptimizerRunner`, and a fake monotonic/UTC clock so the test controls thread execution without sleeping. The seeded generator uses only `random.Random(seed)` and emits both valid permutations/in-bounds efforts and invalid additions/deletions/out-of-bounds efforts; it does not require Hypothesis.

- [ ] **Step 2: Run CLI/property tests and record RED**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_optimizer_scheduler.py \
  tests/plugins/auto_routing/test_adaptation_cli.py \
  tests/plugins/auto_routing/test_adaptation_properties.py
```

Expected: failures report the missing scheduler, commands, and freeze/rollback guards.

- [ ] **Step 3: Implement the operator surface and exact state transitions**

Register:

```text
hermes auto-routing optimize [--apply --expect-hash HASH]
hermes auto-routing history [--limit N] [--json]
hermes auto-routing mode {disabled,conservative} [--apply --expect-hash HASH]
hermes auto-routing freeze [--apply --expect-hash HASH]
hermes auto-routing unfreeze [--apply --expect-hash HASH]
hermes auto-routing rollback REVISION_ID [--apply --expect-hash HASH]
```

Extend config with `adaptation.mode: Literal["disabled", "conservative"]`, defaulting to `disabled`, plus `optimizer_min_interval_seconds: 300` validated as a finite integer of at least `30`. `mode conservative` previews the standing authority and current authority/revision/config hashes; apply requires the matching hash. `mode disabled` prevents new scheduler work and cancels pending experiments without changing the selected runtime of any existing session or deleting the current complete overlay. Execute both transitions through Stage 1's recoverable config/SQLite saga: the preview hash covers YAML plus the exact experiment-state diff, and restart recovery completes or restores both sides before routing/adaptation can continue. Install, upgrade, schema migration, `setup`, `edit`, and `optimize` never change the mode implicitly.

Put the complete deterministic optimizer sequence in `OptimizerRunner.run_once(trigger)`. `OptimizerScheduler` has one process-local pending/running bit protected by a lock, injects a `ThreadLauncher` in tests, and uses a daemon `threading.Thread` in production. A trigger returns immediately, coalesces while work is pending/running, and records the latest trigger reasons. The worker first takes the cross-process SQLite lease, then performs the same `run_once` used by the CLI. It stores `last_run_at`, `next_due_at`, and the last result.

Register automatic triggers for: a successfully persisted post-turn evidence event, `on_session_start` when `next_due_at` has passed, a verified runtime inventory-state change, and an active-target policy breach. Disabled or frozen adaptation suppresses scheduling. The post-turn hook enqueues only after evidence commit; it performs no provider/catalog call, config rewrite, optimization, or thread join itself. Worker failure is bounded, locally logged without prompts/responses/secrets, clears the pending bit in `finally`, advances `next_due_at` with capped retry backoff, and never changes the routed session. The next trigger or process may retry after the lease/interval permits.

`optimize` invokes the same runner but remains preview-first: it acquires the lease, consumes new evidence exactly once, proposes/validates a revision, and previews its full diff and experiment budget before publishing an experiment only with the hash-gated apply form. Automatic runs are authorized solely when adaptation was explicitly enabled in config and may publish only the conservative overlay/experiment operations already authorized by this stage; no interactive hash is required for those pre-authorized bounded mutations. `history` is read-only and shows authority, revision lineage, status, changed fields, experiment bounds, transition reason, trigger, and timestamp. Freeze/unfreeze use plugin state rather than rewriting YAML. Rollback requires the target revision to be complete, checksum-valid, and under the current authority; it atomically moves the pointer and records a new rollback event. Every CLI preview hash covers command, arguments, current authority hash, current active revision, and proposed result.

Document explicit conservative-mode activation, automatic trigger/coalescing behavior, conservative-stage limits, deterministic allocation, evidence thresholds, budget accounting, mode-versus-freeze semantics, and recovery commands in the README. State explicitly that conservative adaptation progresses without a manual `optimize` command and that `optimize` is an inspection/forced-run operator surface.

- [ ] **Step 4: Run the complete Stage 4 gate**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing
uv run --extra dev python -m pytest -q \
  tests/gateway/test_agent_cache.py \
  tests/tools/test_delegate.py \
  tests/tools/test_async_delegation.py \
  tests/run_agent/test_provider_fallback.py \
  tests/run_agent/test_fallback_reasoning_override.py
uv run --extra dev ruff check plugins/auto_routing tests/plugins/auto_routing
git diff --check
```

Expected: all tests pass, Ruff reports no errors, and `git diff --check` emits no output. The seeded allocation report must show zero policy escapes, zero high-risk canaries, zero duplicate assignments, no canary fraction breach, and byte-exact rollback.

- [ ] **Step 5: Commit the Stage 4 operator controls**

```bash
git add plugins/auto_routing/auto_routing/cli.py plugins/auto_routing/auto_routing/service.py plugins/auto_routing/README.md tests/plugins/auto_routing/test_adaptation_cli.py tests/plugins/auto_routing/test_adaptation_properties.py
git add plugins/auto_routing/__init__.py plugins/auto_routing/auto_routing/optimizer.py plugins/auto_routing/auto_routing/config.py tests/plugins/auto_routing/test_optimizer_scheduler.py
git diff --cached --check
git commit -m "feat(auto-routing): ship conservative adaptation controls"
```
