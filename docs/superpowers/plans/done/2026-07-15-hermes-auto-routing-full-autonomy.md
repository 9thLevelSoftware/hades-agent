# Hermes Auto Routing Full Autonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Auto Routing discover newly verified challengers and adapt profile candidates, fallback order, objective weights, reasoning policy, classifier/evaluator settings, and profile topology while remaining completely bounded by user-owned authority and reversible deterministic experiments.

**Architecture:** A typed mutation language separates autonomous proposals from application. Candidate discovery observes only the verified executable inventory. Every proposed complete overlay passes an immutable-envelope validator, then the existing Stage 4 experiment engine evaluates one contextual mutation at a time. Classifier/evaluator candidates run in shadow before any routing influence, while split/merge operations maintain lineage aliases and tombstones so user rules and historical decisions retain stable meaning.

**Tech Stack:** Stage 1–4 plugin services, Pydantic v2 discriminated unions, SQLite immutable revisions, standard-library deterministic statistics/hashing, Hermes plugin-LLM trust enforcement and budget ledger, pytest seeded simulations, and real-import integration tests.

## Global Constraints

- Full autonomy is a separately approved `adaptation.mode: autonomous` transition. Upgrading or enabling earlier stages cannot turn it on.
- The immutable `PolicyEnvelope` is never an autonomous mutation target. Denies, cost/latency/experiment ceilings, privacy, licenses, hardware compatibility, local-install policy, risk rules, and classifier/evaluator trust remain user-owned.
- A runtime may enter the autonomous challenger pool only while its canonical inventory state is `verified`. `configured_unverified`, `temporarily_unavailable`, `ineligible`, inaccessible subscription models, and uninstalled local models cannot be proposed or tested.
- This version never initiates an autonomous access probe, even when `allow_paid_access_probes` is true. Discovery consumes only access already verified by non-billable Hermes evidence or the Stage 1 user-run `verify-runtime --apply --expect-hash ... --ack-billable` flow. No advisor plan, optimizer trigger, challenger discovery, or canary may execute that command or call its probe method. Probes never install local models.
- Classifiers continue to return task requirements only; no classifier or evaluator candidate may directly name a model/profile for the routed task.
- Classifier/evaluator candidates must remain inside plugin-LLM trust allowlists, use the approved full-disclosure setting, reserve overhead budget, and pass shadow gates before limited canaries.
- Only one semantic mutation is tested per experiment. Compound implementation details may form one atomic revision, but its explanation identifies one operator and one causal hypothesis.
- Profile weight vectors remain complete, explicit, non-negative, and normalized. Per-profile hard limits may tighten but never loosen global policy.
- Split/merge operations cannot make a user rule dangle or silently retarget it to unrelated semantics. Baseline authority IDs remain stable aliases; retired adaptive nodes leave immutable tombstones.
- A manual YAML authority change immediately invalidates the overlay. Routing uses the newly validated baseline until a complete rebase revision is published.
- All Stage 4 canary, budget, risk, confidence, rollback, cooldown, freeze, durability, and preview/hash constraints continue unchanged.
- No raw task content is persisted in mutation, lineage, shadow, or rebase records.
- Before every Step 5 commit, run both `git diff --check` and the shown `git diff --cached --check`; stop on any output/error.
- Complete this plan only after the conservative adaptation stage is demonstrably stable and exact rollback has been exercised.

---

## File Map

### Plugin files created

- `plugins/auto_routing/auto_routing/mutations.py` — typed autonomous operators and immutable-envelope validation.
- `plugins/auto_routing/auto_routing/topology.py` — profile lineage, aliases, tombstones, and deterministic rule resolution.
- `plugins/auto_routing/auto_routing/rebase.py` — manual-authority invalidation and safe overlay rebase.

### Plugin files modified

- `plugins/auto_routing/auto_routing/models.py` — autonomous controls, candidate, mutation, shadow, lineage, and rebase records.
- `plugins/auto_routing/auto_routing/config.py` — full envelope validation and explicit autonomous transition.
- `plugins/auto_routing/auto_routing/inventory.py` — verified-runtime change feed and canonical challenger eligibility.
- `plugins/auto_routing/auto_routing/classifier.py` — versioned classifier implementations and shadow comparisons.
- `plugins/auto_routing/auto_routing/evidence.py` — shadow attribution and topology-aware context keys.
- `plugins/auto_routing/auto_routing/learner.py` — autonomous proposal generation and contextual mutation hypotheses.
- `plugins/auto_routing/auto_routing/experiments.py` — shadow-to-canary lifecycle and one-mutation enforcement.
- `plugins/auto_routing/auto_routing/rules.py` — stable alias resolution.
- `plugins/auto_routing/auto_routing/selector.py` — complete topology-aware overlays.
- `plugins/auto_routing/auto_routing/storage.py` — challenger, mutation, shadow, lineage, tombstone, and rebase state.
- `plugins/auto_routing/auto_routing/service.py` — autonomous optimizer orchestration and authority invalidation.
- `plugins/auto_routing/auto_routing/cli.py` — mode transition, candidate/proposal, lineage, and rebase inspection.
- `plugins/auto_routing/README.md` — autonomy authority and operations.

### Tests created

- `tests/plugins/auto_routing/test_autonomous_mode.py`
- `tests/plugins/auto_routing/test_challenger_discovery.py`
- `tests/plugins/auto_routing/test_mutation_envelope.py`
- `tests/plugins/auto_routing/test_classifier_shadow.py`
- `tests/plugins/auto_routing/test_autonomous_proposals.py`
- `tests/plugins/auto_routing/test_profile_topology.py`
- `tests/plugins/auto_routing/test_authority_rebase.py`
- `tests/plugins/auto_routing/test_autonomy_cli.py`
- `tests/plugins/auto_routing/test_autonomy_simulation.py`

---

### Task 1: Require an Explicit Hash-Guarded Transition into Autonomous Mode

**Files:**
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Modify: `plugins/auto_routing/auto_routing/config.py`
- Modify: `plugins/auto_routing/auto_routing/cli.py`
- Create: `tests/plugins/auto_routing/test_autonomous_mode.py`

- [ ] **Step 1: Write failing activation and immutable-envelope tests**

```python
def test_install_or_upgrade_never_enables_autonomous_mode(valid_config) -> None:
    parsed = AutoRoutingConfig.model_validate(valid_config)
    assert parsed.adaptation.mode == "conservative"


def test_autonomous_transition_requires_preview_hash_and_explicit_ack(run_auto_cli) -> None:
    preview = run_auto_cli("mode", "autonomous")
    assert preview.exit_code == 0
    assert preview.json["applied"] is False
    assert preview.json["warnings"] == [
        "autonomous mode may change approved profile structure and candidates inside the immutable policy envelope"
    ]

    missing_ack = run_auto_cli(
        "mode", "autonomous", "--apply", "--expect-hash", preview.json["precondition_hash"]
    )
    assert missing_ack.exit_code == 2

    applied = run_auto_cli(
        "mode",
        "autonomous",
        "--ack-authority-bound-autonomy",
        "--apply",
        "--expect-hash",
        preview.json["precondition_hash"],
    )
    assert applied.exit_code == 0
```

Define `valid_config` and `run_auto_cli` inside the test file using a temporary `HERMES_HOME`; the config starts with `adaptation.mode: conservative`, two approved profiles, finite overhead/experiment ceilings, and explicit plugin-LLM allowlists.

- [ ] **Step 2: Run the focused tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_autonomous_mode.py
```

Expected: the mode schema/command or explicit acknowledgement behavior is absent.

- [ ] **Step 3: Add the mode enum and exact transition contract**

Extend Stage 4's `Literal["disabled", "conservative"]` adaptation mode to `Literal["disabled", "conservative", "autonomous"]`. Missing config still defaults to `disabled`; only the Stage 4 hash-guarded mode command may write `conservative`, and no install/migration writes `autonomous`. `hermes auto-routing mode autonomous` previews these facts:

- current/new mode;
- immutable authority hash;
- maximum canary fraction and experiment/overhead budgets;
- enabled autonomous operator types;
- classifier/evaluator trust allowlists;
- currently verified challenger count;
- warning text shown in the test.

The preview hash covers the full facts object. Apply requires `--ack-authority-bound-autonomy`; if authority, active revision, inventory revision, or facts change after preview, return exit code 2 without writing. Every mode transition uses the Stage 1 recoverable config/SQLite saga, with exact YAML, experiment cancellation, and revision-pointer changes in its journal/recovery contract. Transitioning back to `conservative` immediately stops new autonomous proposals and revalidates the active overlay with the Stage 4 conservative validator. Keep it only when it contains no added/removed targets, topology, weight, or classifier/evaluator changes; otherwise restore the latest compatible conservative revision or baseline through that saga. `disabled` freezes adaptation and cancels pending experiments without changing the selected runtime of an existing session.

Add validated autonomous controls with these defaults: `allowed_mutations` is
an empty tuple until the setup/edit interview explicitly chooses operators;
`max_weight_step=0.05`; `minimum_context_evidence_weight=10.0`;
`minimum_shadow_samples=100`; `minimum_shadow_agreement=0.90`;
`max_shadow_p95_latency_seconds=2.0`; `max_shadow_mean_cost_usd=0.01`; and
`minimum_downstream_delta_lcb=0.0`. The setup preview lists every value.

- [ ] **Step 4: Run mode, config, and mutation-safety regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_autonomous_mode.py \
  tests/plugins/auto_routing/test_models_config.py \
  tests/plugins/auto_routing/test_adaptation_cli.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit explicit autonomy activation**

```bash
git add plugins/auto_routing/auto_routing/models.py plugins/auto_routing/auto_routing/config.py plugins/auto_routing/auto_routing/cli.py tests/plugins/auto_routing/test_autonomous_mode.py
git diff --cached --check
git commit -m "feat(auto-routing): require explicit autonomous mode activation"
```

---

### Task 2: Discover Challengers Only from the Verified Executable Inventory

**Files:**
- Modify: `plugins/auto_routing/auto_routing/inventory.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Create: `tests/plugins/auto_routing/test_challenger_discovery.py`

- [ ] **Step 1: Write failing eligibility and state-transition tests**

```python
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("verified", True),
        ("configured_unverified", False),
        ("temporarily_unavailable", False),
        ("ineligible", False),
    ],
)
def test_only_verified_runtime_becomes_a_challenger(discovery, observation, state, expected) -> None:
    candidate = discovery.consider(observation.model_copy(update={"state": state}))
    assert (candidate is not None) is expected


def test_uninstalled_local_model_is_never_discovered(discovery, local_observation) -> None:
    candidate = discovery.consider(
        local_observation.model_copy(update={"installed": False, "state": "ineligible"})
    )
    assert candidate is None


def test_lost_access_suspends_existing_challenger(discovery, verified_observation) -> None:
    candidate = discovery.consider(verified_observation)
    discovery.consider(
        verified_observation.model_copy(update={"state": "temporarily_unavailable"})
    )
    assert discovery.store.read_challenger(candidate.candidate_id).status == "suspended"
```

Define `discovery` with a real temporary `RoutingStore` and a policy denying one provider and requiring installed open-weight local models. Define canonical observations with no secret fields.

- [ ] **Step 2: Run discovery tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_challenger_discovery.py
```

Expected: failures report missing challenger discovery/state persistence.

- [ ] **Step 3: Implement a policy-filtered inventory change feed**

`ChallengerDiscovery.consider()` must:

1. canonicalize `RuntimeKey` through the Stage 1 inventory;
2. reject every state except `verified` before catalog scoring;
3. reapply provider/model/license/hardware/source/context/cost/latency policy;
4. require installed and backend-confirmed local models;
5. require configured authenticated access for remote/subscription runtimes;
6. reject MoA identifiers and synthetic ensemble endpoints;
7. create the stable candidate ID with
   `"arc_" + hashlib.sha256(f"{authority_id}\0{runtime_id}".encode("utf-8")).hexdigest()[:24]`;
8. attach inventory/catalog revision and provenance without credentials;
9. suspend, rather than delete, a previously known candidate that loses verification.

The candidate is not inserted into any profile at discovery time. It remains `observed` until the learner produces a contextual `AddTarget` proposal and the Stage 4 experiment gates accept it. Deduplicate repeated observations by `(authority_id, runtime_id, inventory_revision)`.

- [ ] **Step 4: Run discovery, inventory, and policy regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_challenger_discovery.py \
  tests/plugins/auto_routing/test_inventory.py \
  tests/hermes_cli/test_runtime_provider_resolution.py
```

Expected: all selected tests pass; no unverified row is recommendable/canaryable.

- [ ] **Step 5: Commit verified challenger discovery**

```bash
git add plugins/auto_routing/auto_routing/inventory.py plugins/auto_routing/auto_routing/storage.py plugins/auto_routing/auto_routing/models.py tests/plugins/auto_routing/test_challenger_discovery.py
git diff --cached --check
git commit -m "feat(auto-routing): discover verified runtime challengers"
```

---

### Task 3: Define a Typed Mutation Language and Reject Every Envelope Escape

**Files:**
- Create: `plugins/auto_routing/auto_routing/mutations.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Modify: `plugins/auto_routing/auto_routing/config.py`
- Create: `tests/plugins/auto_routing/test_mutation_envelope.py`

- [ ] **Step 1: Write failing operator and malicious-mutation tests**

```python
@pytest.mark.parametrize(
    "field_path",
    [
        "policy.denied_providers",
        "policy.denied_models",
        "policy.max_estimated_task_cost_usd",
        "policy.max_estimated_latency_seconds",
        "policy.max_experiment_cost_usd_per_day",
        "policy.allowed_licenses",
        "policy.local_models.require_compatible_hardware",
        "llm.allowed_providers",
        "llm.allowed_models",
        "classifier.disclosure",
    ],
)
def test_mutation_cannot_touch_immutable_envelope(mutation_validator, baseline, field_path) -> None:
    tampered = replace_json_pointer(
        baseline,
        field_path,
        "attacker-controlled",
    )
    result = mutation_validator.validate_complete_revision(tampered)
    assert not result.accepted
    assert result.reason == f"immutable_field:{field_path}"


def test_each_accepted_mutation_has_one_declared_operator(
    mutation_validator, baseline, runtime_b_id
) -> None:
    mutation = MoveTarget(profile_id="coding", runtime_id=runtime_b_id, new_index=0)
    result = mutation_validator.validate(baseline, mutation)
    assert result.accepted
    assert result.operator == "move_target"
    assert result.changed_fields == ("profiles.coding.target_order",)
```

Construct `baseline` in the test file from a validated authority plus complete
overlay. Build `runtime_a_id` and `runtime_b_id` from concrete
`RuntimeKey(...).stable_id()` values and construct `mutation_validator` with
the exact immutable path prefixes listed below and a verified inventory
containing both runtimes. `replace_json_pointer` round-trips the model through
JSON, replaces exactly one dotted path for the malicious complete-revision
test, and revalidates it as the untrusted input envelope rather than as an
accepted `AdaptiveRevision`.

- [ ] **Step 2: Run mutation tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_mutation_envelope.py
```

Expected: collection fails because the mutation union and validator do not exist.

- [ ] **Step 3: Implement the discriminated operator union and two-pass validation**

Define a frozen Pydantic discriminated union with these operator tags:

```text
add_target
remove_target
move_target
set_objective_weights
set_reasoning_policy
set_classifier
set_evaluator
split_profile
merge_profiles
```

Do not expose a generic `set_field` production operator. The immutable path
prefixes are exactly `policy`, `llm`, `activation`, `scopes`, `safe_default`,
`privacy`, and `classifier.disclosure`/`evaluator.disclosure`.
Classifier/evaluator provider/model/reasoning may change only through their
typed operators and trust validation.

Validation pass 1 applies the operator to a deep immutable copy of the active
overlay and records actual changed JSON-pointer paths. Pass 2 validates the
complete `AdaptiveRevision` against the unchanged YAML authority and current
verified inventory. Reject if declared/actual paths differ, more than one
semantic operator is present, any target is not verified, reasoning is out of
bounds, weights are incomplete or do not normalize to `1.0 ± 1e-9`, a profile
limit loosens global policy, a baseline anchor/rule loses deterministic
meaning, or an immutable authority field differs. Allowed adaptive overlay
fields may differ from their baseline only through the corresponding enabled
typed operator; the YAML authority object itself is never rewritten.
Canonicalize and checksum the accepted result.

- [ ] **Step 4: Run all conservative and autonomous mutation tests**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_mutation_envelope.py \
  tests/plugins/auto_routing/test_conservative_mutations.py \
  tests/plugins/auto_routing/test_adaptation_properties.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the autonomous mutation boundary**

```bash
git add plugins/auto_routing/auto_routing/mutations.py plugins/auto_routing/auto_routing/models.py plugins/auto_routing/auto_routing/config.py tests/plugins/auto_routing/test_mutation_envelope.py
git diff --cached --check
git commit -m "feat(auto-routing): validate typed autonomous mutations"
```

---

### Task 4: Shadow Classifier and Evaluator Candidates Before Limited Influence

**Files:**
- Modify: `plugins/auto_routing/auto_routing/classifier.py`
- Modify: `plugins/auto_routing/auto_routing/evidence.py`
- Modify: `plugins/auto_routing/auto_routing/experiments.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Create: `tests/plugins/auto_routing/test_classifier_shadow.py`

- [ ] **Step 1: Write failing trust, shadow, and outcome-gate tests**

```python
def test_untrusted_classifier_candidate_is_rejected(shadow_service, candidate) -> None:
    result = shadow_service.register_classifier(
        candidate.model_copy(update={"provider": "not-allowed"})
    )
    assert not result.accepted
    assert result.reason == "plugin_llm_trust_denied"


def test_shadow_classifier_cannot_change_the_live_decision(shadow_service, request) -> None:
    result = shadow_service.compare(request)
    assert result.live_decision.assessment_source == "classifier:current"
    assert result.shadow_assessment.source == "classifier:candidate"
    assert result.live_decision.selected_target == request.expected_control_target


def test_classifier_needs_agreement_latency_cost_and_downstream_outcome_gates(shadow_service) -> None:
    shadow_service.feed_metrics(
        agreement=0.94,
        p95_latency_seconds=1.2,
        mean_cost_usd=0.002,
        downstream_delta_lcb=0.03,
        sample_count=100,
    )
    assert shadow_service.evaluate().action == "eligible_for_limited_canary"
```

Define the service in the test file with allowlists containing only `trusted-provider/trusted-model`, finite overhead budgets, `minimum_shadow_samples=100`, `minimum_shadow_agreement=0.90`, `max_shadow_p95_latency_seconds=2.0`, `max_shadow_mean_cost_usd=0.01`, and `minimum_downstream_delta_lcb=0.0`.

- [ ] **Step 2: Run shadow tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_classifier_shadow.py
```

Expected: failures report missing versioned shadow comparison and gates.

- [ ] **Step 3: Implement budgeted shadow execution and staged promotion**

For each eligible new decision, the current classifier remains authoritative. If the shadow fraction hash matches and a worst-case overhead reservation succeeds, call the candidate with the same transient disclosed input and the same strict `TaskAssessment` schema. Persist only assessment fields, agreement metrics, latency, token/cost totals, trust decision, and linked downstream decision/outcome IDs—never the prompt.

Classifier candidates progress:

```text
observed -> shadow -> eligible_for_limited_canary -> limited_canary -> promoted
                      |                           |                  |
                      +---------- rejected <-----+------------------+
```

Shadow eligibility requires all configured minimum samples, exact-schema validity, field-level weighted agreement, p95 latency, mean cost, trust, budget, and downstream outcome lower-bound gates. A limited classifier canary still cannot name a model/profile; it supplies requirements to the deterministic selector for the canary fraction only. Evaluator candidates use the same lifecycle but are compared only on tasks lacking stronger objective/explicit/behavioral evidence and never become stronger than their source class.

Compute classifier agreement as a weighted mean: complexity within `0.15`
(`0.20`), domain Jaccard (`0.20`), required-capability Jaccard (`0.20`), exact
risk class (`0.15`), and mean `1 - absolute_difference` across four
sensitivities (`0.25`). Empty/empty set Jaccard is `1.0`. Compute p95 by the
nearest-rank method at index `ceil(0.95 * n) - 1` after ascending sort. A
shadow record failing schema contributes a validity failure, not an agreement
zero. Limited-canary assignment reuses the deterministic hash/risk/budget
mechanism from Stage 4 with its own experiment ID and configured fraction.

- [ ] **Step 4: Run shadow, classifier, evidence, and trust regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_classifier_shadow.py \
  tests/plugins/auto_routing/test_rules_classifier.py \
  tests/plugins/auto_routing/test_evaluator.py \
  tests/agent/test_plugin_llm.py
```

Expected: all selected tests pass; live decisions remain unchanged throughout shadow.

- [ ] **Step 5: Commit shadow model-component adaptation**

```bash
git add plugins/auto_routing/auto_routing/classifier.py plugins/auto_routing/auto_routing/evidence.py plugins/auto_routing/auto_routing/experiments.py plugins/auto_routing/auto_routing/models.py plugins/auto_routing/auto_routing/storage.py tests/plugins/auto_routing/test_classifier_shadow.py
git diff --cached --check
git commit -m "feat(auto-routing): shadow classifier and evaluator challengers"
```

---

### Task 5: Generate Contextual Target, Fallback, Weight, and Reasoning Proposals

**Files:**
- Modify: `plugins/auto_routing/auto_routing/learner.py`
- Modify: `plugins/auto_routing/auto_routing/experiments.py`
- Modify: `plugins/auto_routing/auto_routing/selector.py`
- Create: `tests/plugins/auto_routing/test_autonomous_proposals.py`

- [ ] **Step 1: Write failing contextual and one-mutation tests**

```python
def test_catalog_prior_can_enter_canary_but_cannot_promote_without_local_outcomes(
    autonomous_learner, challenger_prior, experiment_service
) -> None:
    proposals = autonomous_learner.propose(challenger_prior)
    coding = [item for item in proposals if item.context.domain == "coding"]
    writing = [item for item in proposals if item.context.domain == "writing"]

    assert any(item.operator == "add_target" for item in coding)
    assert not any(item.operator == "add_target" for item in writing)
    experiment = experiment_service.start(coding[0])
    assert experiment.status == "canary"
    assert experiment_service.evaluate(experiment).action == "continue"

    experiment_service.record_local_outcomes(
        experiment, control=[0.70] * 20, challenger=[0.82] * 20
    )
    assert experiment_service.evaluate(experiment).action == "promote"


def test_experiment_contains_exactly_one_semantic_mutation(autonomous_learner, evidence) -> None:
    for proposal in autonomous_learner.propose(evidence):
        experiment = autonomous_learner.to_experiment(proposal)
        assert len(experiment.mutations) == 1
        assert experiment.hypothesis.operator == experiment.mutations[0].operator


def test_weight_proposal_stays_complete_and_normalized(autonomous_learner, evidence) -> None:
    proposal = autonomous_learner.propose_weight_change("coding", evidence)
    assert set(proposal.weights) == {"quality", "reliability", "latency", "cost"}
    assert sum(proposal.weights.values()) == pytest.approx(1.0)
```

Build `challenger_prior` from one newly verified candidate plus recent provenance-bearing contextual catalog evidence for coding, no writing evidence, explicit cost/latency observations, and a fixed baseline profile; it contains no fabricated local outcome. Build the learner with `MutationValidator` from Task 3 and the real Stage 4 experiment service. The separate `evidence` fixture used by the remaining tests contains only outcomes from already routed targets.

- [ ] **Step 2: Run proposal tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_autonomous_proposals.py
```

Expected: failures show missing autonomous proposal generation.

- [ ] **Step 3: Implement deterministic hypothesis generation**

Generate proposals in stable order by context key, operator priority, profile ID, and runtime ID. Require a configurable minimum effective evidence weight before proposing. Use these candidate sources:

- `add_target`: verified challenger whose recent contextual catalog/capability prior has a lower-confidence utility competitive with the worst current fallback; catalog evidence may authorize bounded exploration but never promotion;
- `remove_target`: sustained contextual regression/unavailability after cooldown, while retaining at least one valid target;
- `move_target`: changed lower-confidence ordering among existing targets;
- `set_objective_weights`: stable explicit/objective evidence showing current declared objective utility is improved by a bounded per-revision step;
- `set_reasoning_policy`: independent target/effort posterior with an in-bounds improvement after accounting for latency/cost;
- `set_classifier`/`set_evaluator`: shadow candidate that passed Task 4 gates.

An `AddTarget` proposal creates a one-mutation canary overlay that can route only the deterministic low-risk fraction to the verified challenger. It is the sole path that produces initial local challenger evidence. Promotion additionally requires Stage 4's minimum samples in both arms and a configured minimum effective weight of local objective/explicit outcomes for the challenger; catalog priors alone, operational success alone, injected/manual unrouted evidence, and evaluator-only evidence cannot promote. Lost verification immediately rejects/rolls back.

Limit each objective-weight component change to `adaptation.max_weight_step` per revision and renormalize all four dimensions. The profile's explicit objective vector remains the utility definition; autonomous weight change is allowed only when `adaptation.allowed_mutations` includes `set_objective_weights`. Store the before/after utility, uncertainty, evidence IDs, policy proof, and rejected alternatives in each explanation.

- [ ] **Step 4: Run proposal, selector, and simulation regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_autonomous_proposals.py \
  tests/plugins/auto_routing/test_conservative_mutations.py \
  tests/plugins/auto_routing/test_learning_simulation.py \
  tests/plugins/auto_routing/test_selector.py
```

Expected: all selected tests pass; coding evidence produces no writing mutation.

- [ ] **Step 5: Commit autonomous route proposals**

```bash
git add plugins/auto_routing/auto_routing/learner.py plugins/auto_routing/auto_routing/experiments.py plugins/auto_routing/auto_routing/selector.py tests/plugins/auto_routing/test_autonomous_proposals.py
git diff --cached --check
git commit -m "feat(auto-routing): propose contextual autonomous route changes"
```

---

### Task 6: Preserve Rule Semantics Across Profile Splits and Merges

**Files:**
- Create: `plugins/auto_routing/auto_routing/topology.py`
- Modify: `plugins/auto_routing/auto_routing/rules.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/auto_routing/selector.py`
- Create: `tests/plugins/auto_routing/test_profile_topology.py`

- [ ] **Step 1: Write failing lineage, alias, tombstone, and ambiguity tests**

```python
def test_split_keeps_baseline_rule_anchor_deterministic(topology, coding_profile) -> None:
    split = topology.split(
        coding_profile,
        discriminator=DomainPartition(left=("coding",), right=("debugging",)),
    )
    assert topology.resolve_rule_target("coding", domain="coding") == split.left.profile_id
    assert topology.resolve_rule_target("coding", domain="debugging") == split.right.profile_id


def test_merge_leaves_tombstones_and_aliases(topology, coding_profile, review_profile) -> None:
    merged = topology.merge(coding_profile, review_profile)
    assert topology.tombstone(coding_profile.profile_id).successor_ids == (merged.profile_id,)
    assert topology.tombstone(review_profile.profile_id).successor_ids == (merged.profile_id,)
    assert topology.resolve_rule_target("coding", domain="coding") == merged.profile_id


def test_ambiguous_rule_semantics_reject_topology_proposal(topology, coding_profile) -> None:
    with pytest.raises(TopologyValidationError, match="ambiguous baseline alias"):
        topology.split(coding_profile, discriminator=OverlappingPartition())
```

Define the topology fixture with an in-memory `TopologyGraph`, baseline alias
`coding -> coding`, and this exact stable-ID helper:

```python
def derived_profile_id(
    *,
    authority_id: str,
    parent_lineage: tuple[str, ...],
    operator: str,
    canonical_partition: dict[str, object],
) -> str:
    payload = json.dumps(
        {
            "authority_id": authority_id,
            "canonical_partition": canonical_partition,
            "operator": operator,
            "parent_lineage": parent_lineage,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"arp_{hashlib.sha256(payload).hexdigest()[:24]}"
```

The production helper and test fixture both call this implementation; neither
uses Python's randomized `hash()` nor a database sequence.

- [ ] **Step 2: Run topology tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_profile_topology.py
```

Expected: collection fails because topology records and resolution do not exist.

- [ ] **Step 3: Implement deterministic lineage and rule resolution**

Persist:

- immutable `profile_nodes` with profile ID, authority anchor, parents, canonical match partition, created revision, retired revision;
- `profile_aliases` mapping baseline rule-addressable ID to one or more active descendants plus mutually exclusive discriminators;
- `profile_tombstones` mapping retired node to successor IDs and retirement reason.

Split partitions must be total over the parent's existing match space and pairwise disjoint. Merge targets must share compatible immutable constraints; the merged match is the canonical union and cannot broaden beyond their union. Resolve a rule by baseline anchor plus the same deterministic task facts used by Stage 2; exactly one active descendant must match. Zero or multiple matches reject the topology revision before experimentation. Historical decisions retain their original node ID and resolve through tombstones only for display, never retroactively.

- [ ] **Step 4: Run topology, rules, decisions, and property regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_profile_topology.py \
  tests/plugins/auto_routing/test_rules_classifier.py \
  tests/plugins/auto_routing/test_decisions.py \
  tests/plugins/auto_routing/test_adaptation_properties.py
```

Expected: all selected tests pass; every generated accepted topology resolves baseline rules exactly once.

- [ ] **Step 5: Commit stable profile topology**

```bash
git add plugins/auto_routing/auto_routing/topology.py plugins/auto_routing/auto_routing/rules.py plugins/auto_routing/auto_routing/models.py plugins/auto_routing/auto_routing/storage.py plugins/auto_routing/auto_routing/selector.py tests/plugins/auto_routing/test_profile_topology.py
git diff --cached --check
git commit -m "feat(auto-routing): preserve profile lineage across topology changes"
```

---

### Task 7: Invalidate and Safely Rebase after Manual Authority Changes

**Files:**
- Create: `plugins/auto_routing/auto_routing/rebase.py`
- Modify: `plugins/auto_routing/auto_routing/config_io.py`
- Modify: `plugins/auto_routing/auto_routing/cli.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Create: `tests/plugins/auto_routing/test_authority_rebase.py`

- [ ] **Step 1: Write failing invalidation and conflict tests**

```python
def test_manual_authority_change_immediately_routes_from_new_baseline(rebase_service, request) -> None:
    old = rebase_service.route(request)
    rebase_service.replace_authority(new_authority_without(old.selected_target.runtime_id))
    during_rebase = rebase_service.route(request.model_copy(update={"session_id": "new-session"}))

    assert during_rebase.adaptive_revision_id is None
    assert during_rebase.authority_id == rebase_service.current_authority.authority_id
    assert during_rebase.selected_target.runtime_id != old.selected_target.runtime_id


def test_rebase_keeps_compatible_runtime_evidence_and_drops_conflict(rebase_service) -> None:
    result = rebase_service.rebase()
    assert result.preserved_evidence_ids == ("evt-compatible",)
    assert result.discarded_mutation_ids == ("mut-target-removed-by-user",)
    assert result.status == "complete"


def test_partial_rebase_is_never_active(rebase_service) -> None:
    with pytest.raises(InjectedRebaseFailure):
        rebase_service.rebase(fault_before_publish=True)
    assert rebase_service.store.read_active_revision(
        rebase_service.current_authority.authority_id
    ).is_baseline
```

Define the fixture with two authority documents, a stable runtime key present in both, a removed target mutation, one compatible evidence event, and real on-disk `RoutingStore` transactions.

- [ ] **Step 2: Run rebase tests and record RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_authority_rebase.py
```

Expected: failures show that the old overlay remains active or no rebase record exists.

- [ ] **Step 3: Implement immediate invalidation and complete rebase publication**

Upgrade Stage 1 `setup/edit` applies so their recoverable YAML/SQLite saga includes old-overlay invalidation, pending-experiment cancellation, and publication of the new baseline authority/revision. No CLI-managed authority write becomes usable between those phases. For an external/manual YAML edit that bypasses the CLI, each fresh decision compares the canonical authority hash to the authority ID stored with the active revision; if different, atomically record the old overlay as `invalidated`, cancel its pending experiments, and route against the newly validated baseline. Do not attempt rebase on the request path.

The lease-elected rebaser later:

1. validates the new immutable envelope;
2. maps evidence by canonical runtime key and profile lineage;
3. preserves only contexts still semantically compatible;
4. reapplies mutations in historical order through the current `MutationValidator`;
5. discards conflicts and records exact reasons;
6. validates all aliases/tombstones/rules;
7. publishes one complete checksum-valid rebase revision using the Stage 4 atomic pointer;
8. emits a local high-visibility event and full rebase report.

If no mutation survives, publish an explicit baseline-equivalent rebase revision so history records completion. A corrupt/invalid new authority disables Auto; it does not fall back to the old authority.

- [ ] **Step 4: Run rebase, config conflict, and routing regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_authority_rebase.py \
  tests/plugins/auto_routing/test_config_io.py \
  tests/plugins/auto_routing/test_fresh_session_routing.py \
  tests/plugins/auto_routing/test_adaptive_revisions.py
```

Expected: all selected tests pass; no partial or stale overlay is selected.

- [ ] **Step 5: Commit authority rebase handling**

```bash
git add plugins/auto_routing/auto_routing/rebase.py plugins/auto_routing/auto_routing/config_io.py plugins/auto_routing/auto_routing/cli.py plugins/auto_routing/auto_routing/service.py plugins/auto_routing/auto_routing/storage.py plugins/auto_routing/auto_routing/models.py tests/plugins/auto_routing/test_authority_rebase.py
git diff --cached --check
git commit -m "feat(auto-routing): rebase adaptive state after authority changes"
```

---

### Task 8: Expose Autonomous Inspection and Pass Deterministic Synthetic Traces

**Files:**
- Modify: `plugins/auto_routing/auto_routing/cli.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Modify: `plugins/auto_routing/README.md`
- Create: `tests/plugins/auto_routing/test_autonomy_cli.py`
- Create: `tests/plugins/auto_routing/test_autonomy_simulation.py`

- [ ] **Step 1: Write failing inspection and end-to-end simulation tests**

```python
def test_candidate_and_lineage_commands_explain_local_state(run_auto_cli) -> None:
    candidates = run_auto_cli("candidates", "--json")
    lineage = run_auto_cli("lineage", "coding", "--json")
    assert candidates.exit_code == lineage.exit_code == 0
    assert candidates.json[0]["inventory_state"] == "verified"
    assert "credentials" not in json.dumps(candidates.json).lower()
    assert lineage.json["authority_anchor"] == "coding"


def test_synthetic_trace_promotes_contextually_without_global_promotion(autonomy_simulator) -> None:
    result = autonomy_simulator.run(seed=20260715, decisions=5000)
    assert result.promoted("new-runtime", domain="coding")
    assert not result.promoted("new-runtime", domain="writing")
    assert result.policy_escapes == 0
    assert result.ambiguous_rule_resolutions == 0
    assert result.partial_revision_reads == 0
    assert result.raw_content_rows == 0
    assert result.manual_optimize_calls == 0
    assert result.automatic_optimizer_runs > 0
```

Define `run_auto_cli` in the CLI test with a temporary home and populated store. Define `autonomy_simulator` in the simulation test with `random.Random(seed)`, the real Stage 4 `OptimizerScheduler`/runner plus real learner/mutation/experiment/topology/storage services, two domains, three verified runtimes, one unverified runtime, objective outcomes that favor the new runtime only for coding, and a scheduled manual authority change at decision 3,000. Use a deterministic manual thread launcher that drains automatically scheduled jobs between decisions; the harness must not invoke the CLI `optimize` command or call `run_once` directly.

- [ ] **Step 2: Run CLI/simulation tests and record RED**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_autonomy_cli.py \
  tests/plugins/auto_routing/test_autonomy_simulation.py
```

Expected: failures report missing commands or incomplete autonomous orchestration.

- [ ] **Step 3: Implement the inspection surface and optimizer sequence**

Register read-only commands:

```text
hermes auto-routing candidates [--status STATUS] [--json]
hermes auto-routing proposals [--status STATUS] [--json]
hermes auto-routing lineage PROFILE_ID [--json]
hermes auto-routing rebase-status [--json]
```

Keep mutation actions under existing preview/hash-gated `optimize`, `mode`, `rollback`, `freeze`, and `unfreeze`. Extend the Stage 4 automatic runner—not a second loop—so every due pass performs: authority check/invalidation, inventory refresh, challenger discovery, evidence consumption, shadow evaluation, lifecycle evaluation/rollback, at most one new proposal per context, immutable-envelope validation, experiment-budget reservation, and immutable experiment publication. New evidence, due session start, inventory-state change, and active-target policy-breach triggers continue to enqueue this runner without blocking the request thread. Stable ordering makes the same state and clock produce the same actions; explicit autonomous mode plus the immutable envelope is the standing authority for bounded publications, while CLI `optimize` remains a preview/forced-run operator surface rather than a prerequisite for progress.

Document every autonomous operator, fields that remain immutable, verified-runtime rule, classifier/evaluator shadow lifecycle, topology semantics, authority rebase behavior, and commands for freezing/rolling back.

- [ ] **Step 4: Run the complete Stage 5 gate**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing
uv run --extra dev python -m pytest -q \
  tests/agent/test_plugin_llm.py \
  tests/hermes_cli/test_runtime_provider_resolution.py \
  tests/gateway/test_agent_cache.py \
  tests/tools/test_delegate.py \
  tests/tools/test_async_delegation.py \
  tests/run_agent/test_provider_fallback.py \
  tests/run_agent/test_fallback_reasoning_override.py
uv run --extra dev ruff check plugins/auto_routing tests/plugins/auto_routing
git diff --check
```

Expected: all tests pass, Ruff reports no errors, and `git diff --check` emits no output. The seeded trace must show contextual rather than global promotion, zero unverified challengers, zero envelope escapes, deterministic rule resolution through split/merge, and a complete authority rebase.

- [ ] **Step 5: Commit the full-autonomy stage**

```bash
git add plugins/auto_routing/auto_routing/cli.py plugins/auto_routing/auto_routing/service.py plugins/auto_routing/README.md tests/plugins/auto_routing/test_autonomy_cli.py tests/plugins/auto_routing/test_autonomy_simulation.py
git diff --cached --check
git commit -m "feat(auto-routing): complete authority-bound autonomy"
```
