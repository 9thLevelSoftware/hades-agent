# Hermes Auto Routing Cache-Safe Static Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route each eligible fresh Hermes session and delegated child to a deterministic provider/model/reasoning target while preserving manual precedence, prompt-cache identity, resume behavior, authoritative fallbacks, and unchanged baseline behavior when Auto is off or unsupported.

**Architecture:** Pure rules, structured classification, hard filtering, and deterministic scoring produce an immutable `RoutingDecision`. A single guarded Hermes 0.18 compatibility adapter projects that decision before `conversation_loop.build_turn_context()` and before delegated `AIAgent` construction, then stamps the session/child with its decision and route epoch. The adapter wraps existing methods idempotently; signature/version drift disables projection rather than guessing.

**Tech Stack:** Foundation-plan plugin package, Hermes `PluginLlm.complete_structured`, Pydantic schemas, SQLite/WAL, `packaging.version.Version`, `inspect.signature`, existing `AIAgent.switch_model`, `resolve_runtime_provider`, and pytest.

## Global Constraints

- Complete the Foundation and Read-Only Advisor plan first; reuse its exact public types, config keys, database path, and authority revision.
- One fresh session routes at most once before its first stable system prompt/provider call. Later user turns never invoke semantic classification.
- A resumed session reuses its recorded route and revision; it never reclassifies. A legacy resumed session without a decision bypasses Auto.
- Precedence is `off/scope disabled` → explicit manual pin → persisted Auto route → fresh Auto → original Hermes default.
- Delegation precedence is explicit fixed `delegation.provider`/`delegation.model` → persisted child decision → per-child Auto → parent inheritance.
- `shadow` computes/persists an explanation but never changes agent fields. Only explicit approved `active` projects a route.
- Rules/classifier return task requirements, never a model/profile choice. Candidate identities and scores are absent from classifier input.
- Hard eligibility and immutable policy filters run before scoring. Only `verified` inventory observations may be selected.
- Projection resolves and binds the selected full access path, including credential-pool and local-backend identity. The first call, 401/429 recovery, child construction, and recorded fallback may rotate only inside that exact path; a same-provider/model route never inherits the previously active pool or backend.
- Classifier timeout, trust denial, malformed JSON, or unbounded overhead estimate degrades silently to compliant `safe_default` and records the reason. A schema-valid low-confidence assessment remains usable but adds a conservative uncertainty penalty favoring quality and reliability.
- Route target reasoning is selected after the model and clamped to target, profile, and global bounds; unsupported values degrade to the nearest allowed supported effort.
- Auto fallback chains are complete and authoritative. Do not append Hermes global fallbacks.
- Post-call failover consumes only the recorded next fallback and creates a cache-reset epoch; it does not classify or rescore.
- If fallback reasoning/policy/cache projection cannot be proven for the installed Hermes version, keep pre-call Auto routing and disable post-call model-changing failover for Auto sessions.
- Manual `/model` or equivalent switch marks the session manual, owns its existing cache reset, and bypasses Auto on later turns.
- Every child in a delegation batch may select a different runtime without changing the model-facing `delegate_task` schema.
- Durable background-child recovery reuses the decision and revision associated with its operation ID/task index; the same stable operation key becomes the persisted canary-assignment key in Plan 4.
- Routing notices use callbacks/logs/store records only; never inject a system/user/assistant message.
- MoA and auxiliary/internal agents remain excluded.
- Adapter drift, invalid authority, no eligible candidate, or state failure preserves original Hermes behavior.
- Each task ends with focused tests, relevant regressions, `git diff --check`, and one conventional commit.

---

## File Map

### New production files

- `plugins/auto_routing/auto_routing/rules.py` — deterministic fact extraction and configured rule evaluation.
- `plugins/auto_routing/auto_routing/classifier.py` — structured task assessment with budget reservation and Pydantic revalidation.
- `plugins/auto_routing/auto_routing/selector.py` — precedence, hard filters, deterministic profile/target scoring, effort and fallback projection.
- `plugins/auto_routing/auto_routing/decisions.py` — decision IDs, persistence, concise/detailed explanations, route-epoch events.

### Generic Hermes runtime/access seams

- `agent/runtime_intent.py` — frozen, content-free provenance for the runtime supplied to an `AIAgent`.
- `agent/runtime_access.py` — frozen, memory-only exact access binding whose secrets/pool are non-serializable and redacted from repr.
- `run_agent.py` and `agent/agent_init.py` — accept/store the optional generic provenance record without changing runtime behavior.
- `agent/agent_runtime_helpers.py`, `agent/chat_completion_helpers.py`, and `run_agent.py` — accept the same internal exact runtime-access binding for main switches, direct fallback activation, and primary restoration, including rollback and same-provider recovery behavior.
- `hermes_cli/cli_agent_setup_mixin.py`, `gateway/run.py`, `gateway/platforms/api_server.py`, `tui_gateway/server.py`, `hermes_cli/oneshot.py`, and `acp_adapter/session.py` — mark config-default versus explicit/session-scoped runtime intent.
- `cron/scheduler.py` and `batch_runner.py` — mark scheduled and benchmark runtimes pinned so they remain Auto-ineligible.
- `tools/delegate_tool.py` — mark fixed delegation intent and add internal-only child reasoning and exact runtime-access overrides without changing the model-visible tool schema; the adapter stamps Auto-projected children.

### Existing plugin files modified

- `plugins/auto_routing/auto_routing/models.py` — add `RoutingRequest`, rule/filter/score records, adapter status, manual-pin provenance.
- `plugins/auto_routing/auto_routing/config.py` — structurally parse persisted `active`; prospective doctor/apply and runtime preparation enforce current adapter health without a persisted capability token.
- `plugins/auto_routing/auto_routing/storage.py` — decision lookup by session/child, operation-link reconciliation, route epochs.
- `plugins/auto_routing/auto_routing/service.py` — compose rules/classifier/selector and expose `decide`, `prepare_session`, `prepare_child`, `explain`.
- `plugins/auto_routing/auto_routing/cli.py` — add `explain`, representative `plan` through the real selector, and active activation gate.
- `plugins/auto_routing/auto_routing/adapters/base.py` — projection/manual-pin/adapter-capability protocols.
- `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py` — guarded wrappers for fresh session, child construction, manual switch, recorded fallback, and durable-link reconciliation.
- `plugins/auto_routing/__init__.py` — parse, construct service, preflight/install adapter, inject `AdapterStatus`, then register lifecycle observers.
- `plugins/auto_routing/README.md` — activation/cache/fallback contracts and supported surfaces.

### New tests

- `tests/plugins/auto_routing/test_rules_classifier.py`
- `tests/plugins/auto_routing/test_selector.py`
- `tests/plugins/auto_routing/test_decisions.py`
- `tests/plugins/auto_routing/test_adapter_contract.py`
- `tests/agent/test_runtime_intent.py`
- `tests/agent/test_runtime_access.py`
- `tests/run_agent/test_switch_model_pool_reload_52727.py` (extended)
- `tests/run_agent/test_provider_fallback.py` (extended)
- `tests/agent/test_restore_primary_pool_reselect.py` (extended)
- `tests/plugins/auto_routing/test_fresh_session_routing.py`
- `tests/plugins/auto_routing/test_delegation_routing.py`
- `tests/plugins/auto_routing/test_auto_fallback.py`
- `tests/plugins/auto_routing/test_static_routing_e2e.py`

---

### Task 1: Extract Deterministic Facts and Produce Structured Task Assessments

**Files:**
- Create: `plugins/auto_routing/auto_routing/rules.py`
- Create: `plugins/auto_routing/auto_routing/classifier.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Create: `tests/plugins/auto_routing/test_rules_classifier.py`

**Interfaces:**
- Consumes: `AutoRoutingConfig.rules`, transient full prompt, `PluginLlm`, `RoutingStore.reserve_budget`, and classifier trust config.
- Produces: `extract_facts(request) -> DeterministicFacts`, `apply_rules(facts, rules) -> RuleResult`, and `TaskClassifier.assess(request, facts) -> ClassificationResult`.

- [ ] **Step 1: Write failing precedence/schema/privacy tests**

```python
def test_deterministic_image_and_tool_facts_override_classifier(classifier, request):
    request = request.model_copy(update={
        "prompt": [{"type": "text", "text": "inspect this"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}],
        "available_capabilities": {"tools"},
    })
    classifier.llm_result = {"modalities": ["text"], "required_capabilities": []}
    result = classifier.assess(request, extract_facts(request))
    assert result.assessment.modalities == {"text", "image"}
    assert result.assessment.required_capabilities >= {"tools", "vision"}


def test_classifier_never_receives_candidate_identities(classifier, request):
    classifier.assess(request, extract_facts(request))
    sent = json.dumps(classifier.captured_input).lower()
    assert "gpt-5.4" not in sent
    assert "openai-codex" not in sent
    assert "profile_score" not in sent


def test_malformed_classifier_uses_safe_default_without_retry(classifier, request):
    classifier.llm_result = {"complexity": "hard"}
    result = classifier.assess(request, extract_facts(request))
    assert result.degraded_reason == "classifier_schema_invalid"
    assert result.assessment is None
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_rules_classifier.py
```

Expected: missing rules/classifier modules.

- [ ] **Step 3: Implement deterministic facts, rules, and one bounded call**

Define a frozen, `extra="forbid"` JSON schema/Pydantic `TaskAssessment` with
numeric complexity `[0,1]`; sorted, deduplicated tuples of normalized domain,
capability, and modality strings; non-negative context/output token
expectations; quality/reliability/latency/cost sensitivity values `[0,1]`;
`risk_class: Literal["low", "normal", "high"]`; and confidence `[0,1]`.
Classifier config adds validated defaults `minimum_confidence=0.50` and
`low_confidence_penalty=0.15`. Call:

```python
result = self.llm.complete_structured(
    instructions=CLASSIFIER_INSTRUCTIONS,
    input=[PluginLlmTextInput(text=serialize_task_and_facts(request.prompt, facts))],
    json_schema=TaskAssessment.model_json_schema(),
    json_mode=True,
    schema_name="auto_routing.task_assessment",
    provider=config.classifier.provider,
    model=config.classifier.model,
    temperature=0,
    max_tokens=config.classifier.max_output_tokens,
    timeout=config.classifier.timeout_seconds,
    reasoning_config={"effort": config.classifier.reasoning_effort},
    purpose="auto-routing classifier",
)
assessment = TaskAssessment.model_validate(result.parsed)
```

Before the call, reserve its worst-case priced output/input allowance. Unknown or unbounded price skips the call. Reconcile actual usage afterward. Apply facts after validation so observed image/tool/context and explicit configured rules win. Do not persist the prompt or parsed classifier payload; return only normalized assessment and usage metadata. If the validated confidence is low, return the assessment with a `low_confidence` flag for the selector's uncertainty penalty; do not degrade to the safe default.

Define `classifier` and `request` in this test file. The classifier uses a fake
`PluginLlm` completion boundary and a real temporary budget ledger; the request
contains the exact transient prompt blocks shown above, a content-free session
identity, and no candidate records. Add table-driven rules proving stable
priority, deterministic fact overrides, and baseline-profile alias resolution.

- [ ] **Step 4: Run GREEN and facade regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_rules_classifier.py \
  tests/agent/test_plugin_llm.py
git diff --check
```

Expected: all tests pass, including timeout/trust/budget failure with one safe degradation and no second classifier call.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/auto_routing/rules.py plugins/auto_routing/auto_routing/classifier.py plugins/auto_routing/auto_routing/models.py tests/plugins/auto_routing/test_rules_classifier.py
git diff --cached --check
git commit -m "feat: classify auto routing requirements"
```

---

### Task 2: Filter and Rank Profiles, Targets, Reasoning, and Fallbacks

**Files:**
- Create: `plugins/auto_routing/auto_routing/selector.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Create: `tests/plugins/auto_routing/test_selector.py`

**Interfaces:**
- Consumes: `TaskAssessment`, deterministic facts/rules, verified inventory, catalog evidence, Stage 1 shared `scoring.py`, immutable policy, baseline/adaptive revision.
- Produces: `Selector.select(inputs: SelectionInputs) -> SelectionResult` with selected primary, ordered fallbacks, a validated full-runtime `safe_default_target`, reasoning, accepted/rejected candidates, and normalized score components.

- [ ] **Step 1: Write failing hard-filter/determinism/effort tests**

```python
def test_hard_filters_run_before_scores(selection_inputs):
    blocked = runtime(
        "anthropic",
        "claude-sonnet-4-6",
        state="configured_unverified",
        quality=1.0,
    )
    inventory = selection_inputs.inventory.model_copy(
        update={"runtimes": (*selection_inputs.inventory.runtimes, blocked)}
    )
    result = Selector().select(
        selection_inputs.model_copy(update={"inventory": inventory})
    )
    assert result.selected.key.model != "claude-sonnet-4-6"
    assert result.rejected["anthropic/claude-sonnet-4-6"] == ["inventory_not_verified"]


def test_fixed_inputs_produce_byte_identical_selection(selection_inputs):
    first = Selector().select(selection_inputs).model_dump_json()
    second = Selector().select(copy.deepcopy(selection_inputs)).model_dump_json()
    assert first == second


def test_reasoning_is_clamped_to_target_and_global_bounds(selection_inputs):
    assessment = selection_inputs.assessment.model_copy(update={"complexity": 0.97})
    policy = selection_inputs.policy.model_copy(update={"max_reasoning_effort": "high"})
    primary = selection_inputs.profile.primary.model_copy(
        update={
            "reasoning": selection_inputs.profile.primary.reasoning.model_copy(
                update={"maximum": "medium"}
            )
        }
    )
    profile = selection_inputs.profile.model_copy(update={"primary": primary})
    bounded = selection_inputs.model_copy(
        update={"assessment": assessment, "policy": policy, "profile": profile}
    )
    assert Selector().select(bounded).reasoning_effort == "medium"


@pytest.mark.parametrize(
    ("case", "expected_profile"),
    [
        ("coding_affinity_beats_equal_writing_utility", "coding"),
        ("quality_weight_beats_cheaper_lower_quality", "quality-first"),
        ("cost_weight_beats_expensive_higher_quality", "cost-first"),
        ("fresh_evidence_beats_equal_stale_evidence", "fresh"),
        ("base_rank_then_lexical_breaks_exact_tie", "ranked-a"),
    ],
)
def test_profile_affinity_objectives_penalties_and_ties(
    multi_profile_inputs, case, expected_profile
) -> None:
    result = Selector().select(multi_profile_inputs.case(case))
    assert result.profile_id == expected_profile


def test_advisor_and_runtime_use_identical_target_utility(selection_inputs) -> None:
    result = Selector().select(selection_inputs)
    runtime_utility = result.accepted[result.selected.key.stable_id()].utility
    advisor_utility = score_target_utility(
        selection_inputs.profile.objectives,
        selection_inputs.assessment,
        selection_inputs.catalog.for_runtime(result.selected.key),
        result.selected.economics,
    )
    assert runtime_utility == advisor_utility


def test_selection_records_full_compliant_safe_default_snapshot(selection_inputs) -> None:
    result = Selector().select(selection_inputs)
    assert result.safe_default_target.runtime_key == selection_inputs.safe_default.key
    assert result.safe_default_target.reasoning_effort == "medium"
    assert result.safe_default_target.runtime_key.credential_pool_identity == "pool:safe"
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_selector.py
```

Expected: missing selector import.

- [ ] **Step 3: Implement explicit filter and score stages**

Apply filters in this exact order: inventory state; provider/model denies; source policy; open-weight/license/hardware policy; task capabilities/modalities/context; global cost/latency; stricter profile/target limits; provider cooldown; reasoning support.

Map numeric complexity to `trivial`, `simple`, `moderate`, `hard`, or
`extreme` with half-open cut points `[0,.20)`, `[.20,.40)`, `[.40,.65)`,
`[.65,.85)`, and `[.85,1]`. For each non-exclusive profile, compute domain,
complexity-band, required-capability, and modality affinity as Jaccard overlap;
an empty profile affinity set is neutral `0.5`, and empty/empty is `1.0`.
Combine them as `0.35*domain + 0.25*complexity + 0.20*capability +
0.20*modality`.

For target utility, call the shared Stage 1 scoring function after deriving effective objective weights by multiplying each
user-owned profile weight by `0.5 + 0.5 *` the corresponding assessment
sensitivity, then renormalizing all four. Use the Stage 1 conservative catalog
metrics and exact utility formula for quality, reliability, normalized latency,
and normalized cost; subtract uncertainty and staleness penalties. A profile's
score is `0.60*affinity + 0.40*best_eligible_target_utility`. Select by higher
profile score, then higher configured `base_rank`, then lexicographically
smaller stable profile ID. Within that profile, select by higher target
utility, then configured primary/fallback order, then stable runtime ID. A
fully pinned deterministic rule skips profile scoring but still runs every
target hard filter. Record every component before selecting.

Reasoning movement starts from the selected target's configured default and is
deterministic before clamping:

```python
effort_order = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
delta = (
    1 if assessment.complexity >= 0.85 or assessment.risk_class == "high" else
    0 if assessment.complexity >= 0.55 or assessment.quality_sensitivity >= 0.70 else
    -1
)
default_index = effort_order.index(target.reasoning.default)
raw_effort = effort_order[min(max(default_index + delta, 0), len(effort_order) - 1)]
effort = clamp_effort(
    raw_effort,
    target.reasoning,
    profile.limits,
    policy.max_reasoning_effort,
    target.supported_reasoning_efforts,
)
```

Use the same canonical effort order everywhere. Inventory obtains exact per-model generic reasoning support from the Stage 1 `resolve_reasoning_support()` host API, which is backed by the same provider aliases/clamps used for request translation. The selector clamps only inside an `exact=True`, non-empty tuple and the active doctor blocks a target whose configured default/bounds cannot resolve through it. A bare reasoning boolean or unknown custom transport never produces guessed efforts. Provider-specific request translation remains entirely in Hermes.

A valid classifier result with confidence below the configured threshold adds
`(1 - confidence) * low_confidence_penalty` to uncertainty and increases the
quality/reliability sensitivity used by the tie-break; it does not invoke
`safe_default`. Build fallbacks only from that profile's approved verified targets in configured order, excluding duplicates/current target and reapplying all hard filters. Independently resolve, hard-filter, and effort-clamp `safe_default` into a full immutable `safe_default_target` snapshot even when ordinary fallbacks survive; keep it outside the semantic fallback list and use it only for classifier degradation, pre-call resolution exhaustion, or resumed-route exhaustion. If no primary/fallback candidate and no compliant default exists, return `no_eligible_candidate` and preserve Hermes baseline.

Define `selection_inputs` and `runtime()` in this test file from frozen models:
two verified targets, one profile, complete objectives, finite policy limits,
catalog evidence, deterministic assessment, and supported-effort tuples. Its
verified safe default uses credential pool identity `pool:safe` and resolves to
generic reasoning effort `medium`.
`multi_profile_inputs` contains at least two eligible profiles and explicit
golden score components for every parameterized affinity/objective/penalty/tie
case; each case asserts the full normalized component mapping as well as the
winner so a changed constant cannot pass accidentally. Add a
seeded loop using `random.Random(20260715)` that creates 500 mixed inventory
states and asserts every accepted target is verified and every effort lies in
the intersection of supported/target/profile/global bounds.

- [ ] **Step 4: Run GREEN plus property loop**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_selector.py
git diff --check
```

Expected: all table-driven and seeded-random inventory cases pass; no selected runtime is unverified or outside reasoning bounds.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/auto_routing/selector.py plugins/auto_routing/auto_routing/models.py tests/plugins/auto_routing/test_selector.py
git diff --cached --check
git commit -m "feat: select policy compliant auto routes"
```

---

### Task 3: Persist Complete Decisions and Explanations Before Projection

**Files:**
- Create: `plugins/auto_routing/auto_routing/decisions.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Create: `tests/plugins/auto_routing/test_decisions.py`

**Interfaces:**
- Consumes: selection/classifier/rule results and revision IDs.
- Produces: `DecisionService.create(request, result)`, `record_before_projection(decision)`, `find_session_decision(session_id)`, `find_child_decision(child_session_id)`, `record_epoch(...)`, and `explain(...)`.

- [ ] **Step 1: Write failing completeness/redaction/idempotency tests**

```python
def test_decision_persists_complete_provenance_without_raw_content(decision_service, request):
    decision = decision_service.create(request, selected_result())
    decision_service.record_before_projection(decision)
    stored = decision_service.get(decision.decision_id)
    assert stored.policy_revision_id and stored.inventory_revision_id and stored.catalog_revision_id
    assert stored.accepted_candidates and stored.rejected_candidates
    assert stored.safe_default_target.runtime_key.credential_pool_identity == "pool:safe"
    serialized = stored.model_dump_json()
    assert request.prompt_text not in serialized
    assert "secret-token" not in serialized


def test_same_session_route_is_idempotent(decision_service):
    first = decision_service.record_before_projection(session_decision("session-1"))
    second = decision_service.record_before_projection(session_decision("session-1"))
    assert second.decision_id == first.decision_id
    assert decision_service.count_for_session("session-1") == 1


def test_assignment_key_depends_on_operation_not_random_decision_id(profile_hmac_key):
    operation = DecisionOperation(
        scope="delegation", operation_id="job-7", task_index=2, route_epoch=0
    )
    first = operation.assignment_key(profile_hmac_key)
    second = operation.assignment_key(profile_hmac_key)
    assert first == second
    assert len(first) == 64
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_decisions.py
```

Expected: missing decisions service.

- [ ] **Step 3: Implement stable IDs, transaction order, and explanations**

Generate IDs with the exact helper below, persist one row plus ordered candidate
rows in a single `BEGIN IMMEDIATE`, and enforce one semantic decision per
`(scope, operation_id, task_index, route_epoch=0)`:

```python
def new_decision_id() -> str:
    return f"ard_{time.time_ns():016x}_{uuid.uuid4().hex[:16]}"
```

`DecisionService.__init__` accepts
`id_factory: Callable[[], str] = new_decision_id`; production uses the default,
while deterministic tests inject a counted factory. Validate the returned ID
against `^ard_[0-9a-f]{16}_[0-9a-f]{16}$` before persistence.

For an ordinary session, `operation_id` is its stable session ID and task index
is zero. Define `DecisionOperation.assignment_key()` as the hex HMAC-SHA256 of
canonical JSON for `(scope, operation_id, task_index, route_epoch)` using the
profile routing key. It is stable across retries/restarts and independent of the
random decision ID; Plan 4 uses it for canary allocation and never hashes the
decision ID. Store only that assignment key, a separate profile-local keyed
HMAC of prompt content, and the content-free task-context bucket; the HMAC key
lives in the profile routing directory, is permission-restricted, and is
excluded from exports.
Persist the selection's validated `safe_default_target` with its full
`RuntimeKey` and exact reasoning effort on every decision, even when unused;
it contains no secret/endpoint value. Concise `explain` returns selected
profile/runtime/effort, top reasons, fallbacks, safe default, degradation, and revision IDs; detailed mode adds every filter and
normalized score component.

Define `request`, `selected_result()`, and `session_decision()` in this test
file using the immutable records created by Tasks 1–2. Include the literal
prompt/secret sentinels shown in the assertions and a real temporary
`RoutingStore`; do not stub persistence.

`AutoRoutingService.decide(request)` performs precedence, facts/rules/classification/inventory/selection, persists the decision, and only then returns it to an adapter. A persistence failure returns `store_unavailable` and forbids projection.

- [ ] **Step 4: Run GREEN and store reopen tests**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_decisions.py \
  tests/plugins/auto_routing/test_storage.py
git diff --check
```

Expected: all tests pass and explanations round-trip after process-style reopen.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/auto_routing/decisions.py plugins/auto_routing/auto_routing/storage.py plugins/auto_routing/auto_routing/service.py tests/plugins/auto_routing/test_decisions.py
git diff --cached --check
git commit -m "feat: persist auto routing decisions"
```

---

### Task 4: Install a Version- and Signature-Guarded Hermes 0.18 Adapter

**Files:**
- Modify: `plugins/auto_routing/auto_routing/adapters/base.py`
- Modify: `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py`
- Modify: `plugins/auto_routing/__init__.py`
- Create: `agent/runtime_intent.py`
- Create: `agent/runtime_access.py`
- Modify: `run_agent.py`
- Modify: `agent/agent_init.py`
- Modify: `agent/agent_runtime_helpers.py`
- Modify: `hermes_cli/cli_agent_setup_mixin.py`
- Modify: `gateway/run.py`
- Modify: `gateway/platforms/api_server.py`
- Modify: `tui_gateway/server.py`
- Modify: `hermes_cli/oneshot.py`
- Modify: `acp_adapter/session.py`
- Modify: `cron/scheduler.py`
- Modify: `batch_runner.py`
- Modify: `tools/delegate_tool.py`
- Create: `tests/agent/test_runtime_intent.py`
- Create: `tests/agent/test_runtime_access.py`
- Modify: `tests/run_agent/test_switch_model_pool_reload_52727.py`
- Create: `tests/plugins/auto_routing/test_adapter_contract.py`

**Interfaces:**
- Consumes: `AutoRoutingService.prepare_session/prepare_child`, resolved memory-only access bindings, Hermes `AIAgent.run_conversation`, `AIAgent.switch_model`, `AIAgent._try_activate_fallback`, and `tools.delegate_tool._build_child_agent`.
- Produces: `RuntimeAccessBinding`, idempotent `install(service) -> AdapterStatus`, `uninstall_for_test()`, and guarded wrapper attributes prefixed `_auto_routing_`.

- [ ] **Step 1: Write failing version/signature/idempotency tests**

```python
def test_aia_agent_stores_content_free_runtime_intent(minimal_agent_kwargs):
    intent = RuntimeIntent(source="explicit_session")
    agent = AIAgent(**minimal_agent_kwargs, runtime_intent=intent)
    assert agent.runtime_intent is intent
    assert dataclasses.asdict(intent) == {"source": "explicit_session"}
    assert RUNTIME_INTENT_CONTRACT_VERSION == 1
    assert tuple(field.name for field in dataclasses.fields(RuntimeIntent)) == ("source",)
    assert "auto_projection" in RUNTIME_INTENT_SOURCES


def test_runtime_access_binding_is_frozen_redacted_and_nonserializable() -> None:
    binding = RuntimeAccessBinding(
        provider="openai-codex",
        model="gpt-5.4",
        api_mode="codex_responses",
        endpoint_identity="endpoint:public-hash",
        auth_identity="api-key:work",
        credential_pool_identity="pool:work",
        local_backend="",
        base_url="https://private.example/v1",
        api_key="secret-token",
        credential_pool=object(),
    )
    assert RUNTIME_ACCESS_BINDING_CONTRACT_VERSION == 1
    assert binding.public_identity() == {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "api_mode": "codex_responses",
        "endpoint_identity": "endpoint:public-hash",
        "auth_identity": "api-key:work",
        "credential_pool_identity": "pool:work",
        "local_backend": "",
    }
    assert "secret-token" not in repr(binding)
    assert "private.example" not in repr(binding)
    assert not hasattr(binding, "__dict__")
    with pytest.raises(TypeError):
        pickle.dumps(binding)
    with pytest.raises(AttributeError):
        binding.provider = "other"


def test_runtime_access_binding_distinguishes_local_backend_identity() -> None:
    common = dict(
        provider="custom",
        model="qwen3:14b",
        api_mode="chat_completions",
        endpoint_identity="endpoint:loopback",
        auth_identity="local:none",
        credential_pool_identity="",
        base_url="http://127.0.0.1:11434/v1",
        api_key="",
        credential_pool=None,
    )
    ollama = RuntimeAccessBinding(**common, local_backend="ollama")
    lmstudio = RuntimeAccessBinding(**common, local_backend="lmstudio")
    assert ollama.public_identity() != lmstudio.public_identity()


def test_adapter_refuses_unknown_version_without_patching(monkeypatch):
    monkeypatch.setattr("hermes_cli.__version__", "0.19.0")
    before = AIAgent.run_conversation
    status = Hermes018Adapter().install(fake_service())
    assert status.state == "unsupported"
    assert AIAgent.run_conversation is before


def test_adapter_install_is_idempotent_and_preserves_signatures():
    adapter = Hermes018Adapter()
    expected_run_signature = inspect.signature(AIAgent.run_conversation)
    first = adapter.install(fake_service())
    second = adapter.install(fake_service())
    assert first.state == second.state == "healthy"
    assert inspect.signature(AIAgent.run_conversation) == expected_run_signature
    assert getattr(AIAgent.run_conversation, "__auto_routing_wrapped__", False)


def test_explicit_same_as_default_runtime_still_bypasses_auto(agent_factory, classifier):
    agent = agent_factory(
        model="gpt-5.4",
        provider="openai-codex",
        runtime_intent=RuntimeIntent(source="explicit_session"),
    )
    agent.run_conversation("hard coding task")
    assert classifier.call_count == 0
    assert agent.model == "gpt-5.4"


def test_same_provider_projection_replaces_pool_for_later_rotation(
    auto_agent, work_pool, personal_pool, fail_first_credential
):
    auto_agent._credential_pool = personal_pool
    projected = project_runtime(
        auto_agent,
        resolved_runtime(
            auth_identity="api-key:work",
            credential_pool_identity="pool:work",
            credential_pool=work_pool,
        ),
    )
    assert projected is True
    assert auto_agent._credential_pool is work_pool

    auto_agent.run_conversation("trigger one authenticated request")
    assert fail_first_credential.recovery_pool is work_pool
    assert fail_first_credential.used_pool_identities == ["pool:work", "pool:work"]
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q \
  tests/agent/test_runtime_intent.py \
  tests/agent/test_runtime_access.py \
  tests/run_agent/test_switch_model_pool_reload_52727.py \
  tests/plugins/auto_routing/test_adapter_contract.py
```

Expected: adapter contract tests fail because projection wrappers are absent.

- [ ] **Step 3: Implement guarded wrappers with originals retained**

Accept exactly `Version(hermes_cli.__version__) == Version("0.18.2")` until Plan 6 commits another reviewed host contract. At this checkpoint, compare exact parameter names/kinds/default semantics for the four wrapped callables before assigning any wrapper. Treat `agent.conversation_loop._sync_failover_system_message(agent, api_messages, active_system_prompt)` as part of the optional fallback capability group. If the fresh/child/access-binding boundary drifts, install none and return `unsupported`; if either current fallback seam drifts, install fresh/child/manual wrappers and report `post_call_failover=False`. In that reduced mode, fresh projection leaves the agent's native `_fallback_chain` empty and retains recorded fallbacks only in the immutable decision/store, so the unchanged native helper returns `False` and cannot execute an unbound target. Task 7 atomically replaces that fallback group with the final direct-fallback and primary-restore binding contracts.

Registration order is structural config parse → service construction in non-projecting state → adapter preflight/install → `service.set_adapter_status(status)` → observer registration. `prepare_session()` and `prepare_child()` require both configured `active` and a currently healthy critical adapter status. A persisted active config therefore remains readable for doctor/reporting on an unsupported/drifted host but every preparation call bypasses projection; no adapter-health token is stored in YAML or required to construct the service.

Before installing the wrapper, add the generic runtime-intent seam with
`RUNTIME_INTENT_CONTRACT_VERSION = 1` and an immutable
`RUNTIME_INTENT_SOURCES` tuple. The frozen
record has a `source` in `config_default`, `explicit_session`,
`configured_scope`, `scheduled_pin`, `batch_pin`, `internal`, `auto_projection`,
or `unknown`. Existing direct `AIAgent` callers default to `unknown`, which is
fail-safe and Auto-ineligible. Supported user-facing constructors explicitly
pass `config_default` only when the runtime came from the ordinary profile
default. CLI/API arguments, stored `/model` overrides, channel overrides, and
ACP runtime requests pass the appropriate explicit source even when their
provider/model happens to equal the default. Cron and batch pass their pinned
sources; auxiliary callers remain `internal` or `unknown`. The record contains
no credential, prompt, or endpoint value and is not sent to the model.

Add `RUNTIME_ACCESS_BINDING_CONTRACT_VERSION = 1` and a frozen slotted `RuntimeAccessBinding` in `agent/runtime_access.py`. Its exact ordered `__slots__` are `provider`, `model`, `api_mode`, `endpoint_identity`, `auth_identity`, `credential_pool_identity`, `local_backend`, `_base_url`, `_api_key`, `_credential_pool`, and `_sealed`. Read-only `base_url`, `api_key`, and `credential_pool` properties expose the three private values only to the host execution helpers; raw base URL, API key/token callable, and credential-pool object never appear in public identity. `repr()` and `public_identity()` expose only the seven public fields, assignment after construction raises, and pickling/automatic serialization raises `TypeError`. The adapter constructs this host record from a freshly validated `ResolvedRuntime`; plugin config/state never imports or stores it.

Extend the generic internal `AIAgent.switch_model`/`agent_runtime_helpers.switch_model` contract with trailing `runtime_access_binding=None`. Existing callers keep the native behavior. When a binding is supplied, require its provider/model/API mode and public identities to match the requested resolved target, snapshot the complete old runtime/pool/client state for rollback, use its private base URL and credential callable, bind its exact pool object before rebuilding the client, and skip canonical-provider `load_pool`; a binding whose pool is `None` deliberately clears rotation for a direct credential path. A successful binding stores only `binding.public_identity()` on the live agent as `_runtime_access_identity`; a failed client rebuild restores the prior identity and exact old objects. An unbound successful switch clears `_runtime_access_identity` because its exact access path is no longer proven; the adapter's manual-switch branch also clears the live `_auto_routing_primary_access_binding` while recording the durable pin. This is an internal access-binding seam, carries no task content, and is not model-visible. Add native regression cases for same-provider replacement, explicit clearing, redaction/nonserialization, rollback, and a 401/429 recovery proving rotation remains inside the replacement pool.

The fresh wrapper calls `service.prepare_session(...)` before invoking the original `AIAgent.run_conversation`, which forwards into `conversation_loop.build_turn_context`. It resolves the selected full `RuntimeKey` immediately before projection and requires the returned provider/model/API mode/endpoint identity/auth identity/pool identity/local-backend identity to match. Projection converts that result to a `RuntimeAccessBinding` and uses existing `agent.switch_model(..., runtime_access_binding=binding)` inside an adapter-local context variable. A mismatch or inability to bind the exact access path aborts Auto projection before the first call and preserves baseline state. The wrapper then explicitly sets target reasoning, authoritative fallback entries, primary-runtime reasoning/access snapshot, decision ID, revision ID, and epoch. The primary snapshot stores only `runtime_access_identity=binding.public_identity()` while the non-serializable binding itself remains on the live agent as `_auto_routing_primary_access_binding`. Each native fallback entry carries the target's non-secret provider, model, API mode, endpoint identity, auth identity, credential-pool identity, local-backend identity, and reasoning config; no endpoint URL, credential, or pool object is copied into the chain. The manual-switch wrapper sets a durable manual pin unless the context variable says Auto is projecting.

Put the first test in `tests/agent/test_runtime_intent.py` and the binding test
in `tests/agent/test_runtime_access.py`; define
`minimal_agent_kwargs` there with a fake local client boundary and no session
DB. Define `resolved_runtime(...)` in the adapter test to require the complete
`RuntimeKey`, including separate auth, pool, endpoint, API-mode, and local-backend identities, plus memory-only endpoint/credential/pool values. In the adapter test, `agent_factory` passes the intent through the real
constructor, while `classifier` is the service's counted completion fake. Add
one table-driven assertion per supported surface proving ordinary defaults are
`config_default`, explicit or stored overrides are not, and cron/batch remain
pinned.

Use `functools.wraps`, `__signature__`, a module lock, saved originals, and `__auto_routing_wrapped__`. Define the test's `work_pool` and `personal_pool` as real in-memory credential-pool instances with distinct non-secret identities; `fail_first_credential` makes the first work entry return 401/429 and records the pool consulted by native recovery. Do not patch provider clients, message lists, system-prompt builders, request middleware, or tool schemas.

- [ ] **Step 4: Run GREEN and current fallback/switch regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/agent/test_runtime_intent.py \
  tests/agent/test_runtime_access.py \
  tests/plugins/auto_routing/test_adapter_contract.py \
  tests/run_agent/test_switch_model_pool_reload_52727.py \
  tests/run_agent/test_switch_model_reasoning_override.py \
  tests/run_agent/test_provider_fallback.py
git diff --check
```

Expected: all tests pass; unsupported simulation leaves exact original callables in place.

- [ ] **Step 5: Commit**

```bash
git add agent/runtime_intent.py agent/runtime_access.py run_agent.py agent/agent_init.py agent/agent_runtime_helpers.py hermes_cli/cli_agent_setup_mixin.py gateway/run.py gateway/platforms/api_server.py tui_gateway/server.py hermes_cli/oneshot.py acp_adapter/session.py cron/scheduler.py batch_runner.py tools/delegate_tool.py plugins/auto_routing/auto_routing/adapters plugins/auto_routing/__init__.py tests/agent/test_runtime_intent.py tests/agent/test_runtime_access.py tests/run_agent/test_switch_model_pool_reload_52727.py tests/plugins/auto_routing/test_adapter_contract.py
git diff --cached --check
git commit -m "feat: install cache safe auto routing adapter"
```

---

### Task 5: Route Fresh, Resumed, Shadow, and Manually Pinned Sessions

**Files:**
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Modify: `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py`
- Modify: `plugins/auto_routing/auto_routing/config.py`
- Create: `tests/plugins/auto_routing/test_fresh_session_routing.py`

**Interfaces:**
- Consumes: decision service, adapter projection, current config baseline, conversation history, session decision/manual-pin records.
- Produces: `prepare_session(agent, prompt, conversation_history, task_id) -> PreparationResult`.

- [ ] **Step 1: Write failing cache/preference/resume tests**

```python
def test_active_routes_once_before_prompt_and_never_reprojects_live_agent(
    real_agent, classifier, projection_spy
):
    first = real_agent.run_conversation("fix the parser", conversation_history=[])
    prompt = real_agent._cached_system_prompt
    client = real_agent.client
    resolver_calls = projection_spy.resolve_call_count
    switch_calls = projection_spy.switch_call_count
    second = real_agent.run_conversation("now add a test", conversation_history=first["messages"])
    assert classifier.call_count == 1
    assert real_agent._cached_system_prompt == prompt
    assert real_agent.client is client
    assert projection_spy.resolve_call_count == resolver_calls
    assert projection_spy.switch_call_count == switch_calls
    assert routing_store().count_for_session(real_agent.session_id) == 1


def test_resume_reapplies_recorded_route_without_classification(
    resumed_agent, classifier, primary_work_pool, fail_first_credential,
):
    resumed_agent.run_conversation("continue", conversation_history=stored_history())
    assert classifier.call_count == 0
    assert resumed_agent.model == "gpt-5.4"
    assert resumed_agent.reasoning_config == {"effort": "medium"}
    assert resumed_agent._credential_pool is primary_work_pool
    assert resumed_agent._runtime_access_identity["auth_identity"] == "api-key:work"
    assert fail_first_credential.used_pool_identities == ["pool:work", "pool:work"]


def test_shadow_and_manual_pin_never_project(shadow_agent, manually_switched_agent):
    shadow_agent.run_conversation("hard coding task", conversation_history=[])
    manually_switched_agent.run_conversation("hard coding task", conversation_history=[])
    assert shadow_agent.model == shadow_agent.baseline_model
    assert manually_switched_agent.model == manually_switched_agent.manual_model
    assert manually_switched_agent._runtime_access_identity is None
    assert manually_switched_agent._auto_routing_primary_access_binding is None
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_fresh_session_routing.py
```

Expected: session routing/reuse assertions fail.

- [ ] **Step 3: Implement fail-safe session precedence**

`prepare_session` must:

1. bypass off/disabled/excluded internal platforms;
2. bypass every runtime intent except `config_default`, plus adapter-recorded manual switch and explicit plugin pin;
3. on a newly constructed/resumed agent, reapply an existing session decision once before prompt restore;
4. bypass a non-empty legacy history with no decision;
5. classify/select only when history is empty and the session has no decision;
6. record a shadow decision without projection;
7. resolve primary, then recorded fallbacks, then the decision's recorded compliant `safe_default_target` before `switch_model`;
8. persist the resolved decision before projection; and
9. preserve the original runtime on any invalid config/store/resolution failure; and
10. on the same live agent, return `already_projected` for the matching stored decision before resolver, `switch_model`, client construction, fallback mutation, classifier, or prompt work.

After a successful projection, stamp the live agent with a plugin-private immutable `ProjectionStamp(decision_id, route_epoch, authority_id, adaptive_revision_id, runtime_id, reasoning_effort)`. Write the stamp only after runtime, reasoning, and authoritative fallback state are all installed. When a later call finds the same persisted decision and an exactly matching stamp/current runtime/reasoning, it returns without touching the resolver, provider client, fallback list, or route epoch. A new agent instance has no stamp and reapplies its durable decision once. A live stamp/current-runtime mismatch is treated as higher-precedence external/manual state and bypassed (the manual-switch wrapper records the pin); Auto never silently switches it back mid-conversation.

Projection failure marks the decision `projection_failed`, restores the runtime snapshot through existing `switch_model` rollback, clears any partial stamp, and continues on baseline. A prewarmed but unused TUI/ACP prompt cache may be cleared only while both persisted user-turn count and provider-call count are zero; a nonzero count causes bypass. No exception from Auto may discard the user's turn.

Build all agent fixtures in this test file with the real `AIAgent`, temporary
session database, local fake provider, and explicit `RuntimeIntent` values.
`routing_store()` reopens the fixture's real profile database, and
`stored_history()` comes from the real session DB; the classifier fake is only
the network completion boundary. `projection_spy` is a counted pass-through
around the real adapter resolver and `AIAgent.switch_model`; it must not replace
their behavior. `primary_work_pool` is a real in-memory Hermes credential pool,
and `fail_first_credential` forces one 401/429 so the resume test proves the
recorded path is rebound before native recovery. The test must include an explicit runtime
intent whose model/provider equals the profile default, proving source—not
value comparison—owns precedence.

- [ ] **Step 4: Run GREEN and cache/session regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_fresh_session_routing.py \
  tests/gateway/test_agent_cache.py \
  tests/gateway/test_session_model_override_routing.py \
  tests/gateway/test_session_model_override_persistence.py \
  tests/tui_gateway/test_reasoning_config_per_model.py
git diff --check
```

Expected: all tests pass; classifier count remains one, the system prompt is byte-identical, and resolver/switch/client identity remain unchanged on later turns of the same live agent.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/auto_routing/service.py plugins/auto_routing/auto_routing/adapters/hermes_0_18.py plugins/auto_routing/auto_routing/config.py tests/plugins/auto_routing/test_fresh_session_routing.py
git diff --cached --check
git commit -m "feat: route fresh hermes sessions once"
```

---

### Task 6: Route Every Delegated Child and Reconcile Durable Background Identity

**Files:**
- Modify: `tools/delegate_tool.py`
- Modify: `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Create: `tests/plugins/auto_routing/test_delegation_routing.py`

**Interfaces:**
- Consumes: `_build_child_agent(task_index, goal, context, ..., parent_agent, override_reasoning_config=None, override_runtime_access_binding=None, ...)`, exact memory-only access bindings, explicit delegation config, child session ID, async delegation durable rows.
- Produces: independent per-child decisions and `link_delegation_operation(delegation_id, child_session_ids)` reconciliation.

- [ ] **Step 1: Write failing mixed-batch/bypass/recovery tests**

```python
def test_batch_children_route_independently_without_schema_change(delegate_parent):
    before_schema = delegate_tool_schema()
    children = build_children(delegate_parent, ["rename one variable", "debug a distributed deadlock"])
    assert [(c.provider, c.model) for c in children] == [
        ("openai-codex", "gpt-5.3-codex"),
        ("openai-codex", "gpt-5.4"),
    ]
    assert [c.reasoning_config for c in children] == [
        {"enabled": True, "effort": "low"},
        {"enabled": True, "effort": "high"},
    ]
    assert [c._session_init_model_config["reasoning_config"] for c in children] == [
        {"enabled": True, "effort": "low"},
        {"enabled": True, "effort": "high"},
    ]
    assert delegate_tool_schema() == before_schema


def test_fixed_delegation_config_bypasses_auto(delegate_parent, config):
    config["delegation"] = {"provider": "openai-codex", "model": "gpt-5.3-codex"}
    child = build_children(delegate_parent, ["hard task"])[0]
    assert child._auto_routing_decision_id is None
    assert child.model == "gpt-5.3-codex"


def test_child_binds_selected_pool_before_client_and_keeps_it_for_recovery(
    delegate_parent, work_pool, fail_first_credential
):
    child = build_child_for_runtime(
        delegate_parent,
        resolved_runtime(
            auth_identity="api-key:work",
            credential_pool_identity="pool:work",
            credential_pool=work_pool,
        ),
    )
    assert child._credential_pool is work_pool
    assert child._runtime_access_identity["auth_identity"] == "api-key:work"
    child.run_conversation("use the selected child runtime")
    assert fail_first_credential.used_pool_identities == ["pool:work", "pool:work"]


def test_reconcile_uses_original_child_decision_after_restart(store, durable_delegation):
    store.close()
    reopened = RoutingStore.open()
    reopened.reconcile_async_delegations()
    linked = reopened.find_operation_decision(durable_delegation.id, task_index=1)
    assert linked.decision_id == durable_delegation.child_decision_ids[1]
    assert linked.adaptive_revision_id == durable_delegation.revision_id
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_delegation_routing.py
```

Expected: all children inherit one existing credential route and durable operation links are absent.

- [ ] **Step 3: Wrap child construction, not the tool schema**

Add the internal-only `_build_child_agent(..., override_reasoning_config: Optional[Dict[str, Any]] = None, override_runtime_access_binding: Optional[RuntimeAccessBinding] = None, ...)` parameters; do not add either to the model-visible `delegate_task` schema. Inside child construction, reasoning precedence is internal Auto override → configured `delegation.reasoning_effort` → parent inheritance, and the resolved value is passed into `AIAgent(reasoning_config=...)` so both `child.reasoning_config` and `_session_init_model_config` persist it. The adapter supplies `{"enabled": False}` for `none`, otherwise `{"enabled": True, "effort": selected_effort}`. When a binding is present, validate its public identity against the requested overrides, pass its private base URL/credential plus `credential_pool=binding.credential_pool` into `AIAgent` at construction time, and bypass the later `_resolve_child_credential_pool` assignment; a bound `None` pool intentionally means no rotation. This ensures the first client and every 401/429 recovery share the selected access path.

Before calling the original `_build_child_agent`, the adapter checks raw `delegation.provider/model`; fixed values bypass Auto. Otherwise it builds a child `RoutingRequest` from goal, context, role, parent/session/turn, and task index, then resolves the full key and passes target values through `model`, `override_provider`, `override_base_url`, `override_api_key`, `override_api_mode`, request overrides, the new internal reasoning override, one `override_runtime_access_binding`, output cap, and ACP command/args. After construction it verifies the child's non-secret access identity, stamps the child, and replaces inherited global fallbacks with the recorded profile chain. A mismatch destroys the unstarted child and fails closed before its first request. Preserve the original child's existing `subagent_start` emission; do not emit a duplicate hook or transcript message.

Persist child decisions first by `(parent_session_id, parent_turn_id, task_index, prompt_hmac)` using the profile-local HMAC helper from Task 3, and then attach `child_session_id`. A reconciliation pass reads Hermes durable async rows' `delegation_id` and `child_session_ids` to link the same rows after return or restart. Never store goal/context in the routing DB.

Define the delegation harness in this test file around the real
`delegate_task` and `_build_child_agent`, with fake provider clients only. Reuse the real in-memory distinct pools and fail-first recovery boundary from Task 4. Read
the schema through Hermes's registered `ToolEntry` before and after adapter
installation. Populate the real durable async rows and reopen the profile DB
for the recovery assertion.

- [ ] **Step 4: Run GREEN and delegate durability regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_delegation_routing.py \
  tests/tools/test_delegate.py \
  tests/tools/test_async_delegation.py
git diff --check
```

Expected: all tests pass; batch children can differ, fixed delegation still wins, and schema snapshots are unchanged.

- [ ] **Step 5: Commit**

```bash
git add tools/delegate_tool.py plugins/auto_routing/auto_routing/adapters/hermes_0_18.py plugins/auto_routing/auto_routing/service.py plugins/auto_routing/auto_routing/storage.py tests/plugins/auto_routing/test_delegation_routing.py
git diff --cached --check
git commit -m "feat: route delegated children independently"
```

---

### Task 7: Enforce Recorded Auto Fallbacks and Cache-Reset Epochs

**Files:**
- Modify: `run_agent.py`
- Modify: `agent/chat_completion_helpers.py`
- Modify: `agent/agent_runtime_helpers.py`
- Modify: `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py`
- Modify: `plugins/auto_routing/auto_routing/decisions.py`
- Modify: `tests/run_agent/test_provider_fallback.py`
- Modify: `tests/agent/test_restore_primary_pool_reselect.py`
- Create: `tests/plugins/auto_routing/test_auto_fallback.py`

**Interfaces:**
- Consumes: recorded fallback targets, `RuntimeAccessBinding`, original `AIAgent._try_activate_fallback(reason, runtime_access_binding=None)`, original `AIAgent._restore_primary_runtime(runtime_access_binding=None)`, and the adapter fallback capability flag.
- Produces: a feature-neutral exact-binding seam for native fallback/primary restoration, adapter-local `project_recorded_fallbacks(agent)` gating, per-target reasoning projection, and append-only `RouteEpoch` records.

- [ ] **Step 1: Write failing authoritative-chain/effort/epoch tests**

```python
def test_auto_chain_does_not_append_global_fallback(auto_agent, global_fallback):
    assert [(f["provider"], f["model"]) for f in auto_agent._fallback_chain] == [
        ("openai-codex", "gpt-5.3-codex")
    ]
    assert global_fallback["model"] not in {f["model"] for f in auto_agent._fallback_chain}


def test_recorded_fallback_projects_reasoning_and_epoch(auto_agent, provider_failure):
    assert auto_agent._try_activate_fallback(reason=provider_failure) is True
    assert auto_agent.reasoning_config == {"effort": "low"}
    epoch = routing_store().latest_epoch(auto_agent.session_id)
    assert epoch.number == 1 and epoch.reason == "recorded_provider_failover"
    assert epoch.decision_id == auto_agent._auto_routing_decision_id


def test_recorded_fallback_rebinds_exact_pool_for_retry_and_rotation(
    auto_agent, fallback_work_pool, provider_failure, fail_first_fallback_credential
):
    assert auto_agent._try_activate_fallback(reason=provider_failure) is True
    assert auto_agent._credential_pool is fallback_work_pool
    auto_agent.run_conversation("retry on the recorded fallback")
    assert fail_first_fallback_credential.used_pool_identities == [
        "fallback-pool:work", "fallback-pool:work"
    ]


def test_next_turn_restores_exact_primary_pool_after_same_provider_fallback(
    auto_agent, primary_work_pool, fallback_work_pool, provider_failure,
    fail_first_primary_credential,
):
    assert auto_agent._try_activate_fallback(reason=provider_failure) is True
    assert auto_agent._credential_pool is fallback_work_pool
    assert auto_agent._restore_primary_runtime() is True
    assert auto_agent._credential_pool is primary_work_pool
    auto_agent.run_conversation("the next primary turn")
    assert fail_first_primary_credential.used_pool_identities == [
        "primary-pool:work", "primary-pool:work"
    ]


def test_direct_credential_fallback_binding_clears_inherited_pool(
    auto_agent, direct_credential_fallback, provider_failure,
):
    auto_agent.set_recorded_fallback(direct_credential_fallback)
    assert auto_agent._try_activate_fallback(reason=provider_failure) is True
    assert auto_agent._credential_pool is None


def test_same_provider_model_distinct_access_path_is_not_deduplicated(
    auto_agent, same_model_metered_fallback, provider_failure,
) -> None:
    assert auto_agent.model == same_model_metered_fallback.model
    assert auto_agent._runtime_access_identity["auth_identity"] == "subscription:default"
    auto_agent.set_recorded_fallback(same_model_metered_fallback)
    assert auto_agent._try_activate_fallback(reason=provider_failure) is True
    assert auto_agent.model == same_model_metered_fallback.model
    assert auto_agent._runtime_access_identity["auth_identity"] == "api-key:work"


def test_fallback_binding_mismatch_restores_complete_runtime_snapshot(
    auto_agent, mismatched_fallback_resolution, provider_failure,
):
    before = capture_runtime_object_identities(auto_agent)
    assert auto_agent._try_activate_fallback(reason=provider_failure) is False
    assert capture_runtime_object_identities(auto_agent) == before
    assert auto_agent._auto_routing_post_call_failover is False


def test_unsupported_fallback_projection_disables_only_post_call_failover(
    auto_agent, adapter,
):
    auto_agent._auto_routing_post_call_failover = False
    adapter.project_recorded_fallbacks(auto_agent)
    assert auto_agent._fallback_chain == []
    assert auto_agent._try_activate_fallback() is False
    assert auto_agent.model == auto_agent._primary_runtime["model"]
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_auto_fallback.py
```

Expected: global fallback leakage or missing effort/epoch assertions fail.

- [ ] **Step 3: Add exact access binding to the direct fallback and restore paths**

Extend the feature-neutral host signatures to `AIAgent._try_activate_fallback(reason=None, runtime_access_binding=None)`, `agent.chat_completion_helpers.try_activate_fallback(agent, reason=None, runtime_access_binding=None)`, `AIAgent._restore_primary_runtime(runtime_access_binding=None)`, and `agent.agent_runtime_helpers.restore_primary_runtime(agent, runtime_access_binding=None)`. Existing callers pass `None` and retain native behavior. A fallback binding applies only to the next chain entry. Immediately after popping that entry—and before the legacy local-skip, provider/model/base-URL dedup, or provider-resolution branches—validate its provider/model/API mode and non-secret access identities against the binding, snapshot the complete runtime/client/Anthropic client/transport cache/credential-pool/access-identity state, and use the binding's private endpoint, credential callable, and exact pool object instead of `resolve_provider_client()` or canonical `load_pool()`. A bound `None` pool deliberately clears rotation. Binding-aware local availability checks use the bound endpoint/runtime, while deduplication and unavailable keys compare the full `public_identity()` against `_runtime_access_identity`; same provider/model/base URL with a different endpoint/auth/pool identity is a real fallback, not a duplicate. The unbound branch retains Hermes's existing local/provider/model/base-URL behavior. If validation or client construction fails, restore the exact object snapshot before attempting native recovery; recursive advancement calls `agent._try_activate_fallback(reason)` without reusing the old binding, allowing the adapter wrapper to resolve the next recorded entry independently.

Primary restoration has the same exact-binding rule. The adapter retains the selected primary's `RuntimeAccessBinding` only on the live agent and wraps `_restore_primary_runtime()` so an Auto-stamped agent passes that binding to the original method. The generic helper requires `binding.public_identity()` to equal `_primary_runtime["runtime_access_identity"]`, binds the exact original pool even when the fallback used the same provider/model, rebuilds from the binding, and performs 401/429 reselection only inside that pool. It never canonical-loads a provider pool when a binding is supplied. A failed restore reinstates the complete pre-restore snapshot. Non-Auto restoration with `None` remains byte-for-byte compatible.

When the final fallback capability group is healthy, an Auto-stamped wrapper first proves `_fallback_chain` is the exact remaining recorded chain and resolves the next fallback's full access path into a `RuntimeAccessBinding`. Call the original `_try_activate_fallback(reason, runtime_access_binding=binding)` directly—the native fallback helper does not call `switch_model()`—inside the adapter-local Auto-projection context so the manual-switch wrapper cannot create a manual pin. Verify the helper's new provider/model/API mode/endpoint/auth/pool/local-backend identities equal the recorded fallback, replace the native per-model reasoning result with the decision target's exact reasoning config, and idempotently call `rewrite_prompt_model_identity()` for that target. Do not clear or null `_cached_system_prompt`: the native helper already rewrites its identity, and every supported conversation-loop retry immediately copies that rewritten prompt into the in-flight messages through `_sync_failover_system_message()`. The provider/model/access change plus the append-only `RouteEpoch` is the cache-reset boundary; append the epoch before returning `True`, then emit a local notice. Do not rewrite the decision's semantic primary or its `_primary_runtime` snapshot. A mismatch restores the pre-fallback snapshot, empties the native chain, disables further Auto failover, and follows the tested native recovery path. When the capability group is disabled or drifted, projection always empties the native chain and stores recorded fallback metadata outside it; the untouched native helper therefore returns `False`. Non-Auto agents call the original method unchanged and retain their configured native chains.

Resume from an unavailable primary may consume only the stored chain and then the decision's recorded compliant `safe_default_target`; it records `resume_route_unavailable` as cache degradation and never invokes classifier/selector or consults the current authority revision.

Construct `auto_agent` with a real recorded decision and a two-entry chain whose same-provider primary/fallback use distinct credential pools;
inject only provider success/failure boundaries. Capture the original non-Auto
helper arguments/results before installing the adapter and assert them again
after installation. Capture the system prompt sent to the fallback retry and
prove the stable-prefix sentinel remains present, the old `Model:`/`Provider:`
identity is absent, the fallback identity is present, and primary/fallback
prompts differ only in the volatile identity lines. In the native regression
files, prove a supplied fallback binding bypasses provider pool loading, a
direct binding clears an inherited pool, mutation failure rolls back every
runtime/client/pool object, and same-provider primary restoration rebinds the
original primary pool before its next 401/429 recovery.

- [ ] **Step 4: Run GREEN and native fallback regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_auto_fallback.py \
  tests/run_agent/test_provider_fallback.py \
  tests/run_agent/test_fallback_reasoning_override.py \
  tests/agent/test_restore_primary_pool_reselect.py \
  tests/agent/test_failover_identity.py
git diff --check
```

Expected: all tests pass; native non-Auto fallback behavior is byte-for-byte unchanged in recorded call arguments.

- [ ] **Step 5: Commit**

```bash
git add run_agent.py agent/chat_completion_helpers.py agent/agent_runtime_helpers.py plugins/auto_routing/auto_routing/adapters/hermes_0_18.py plugins/auto_routing/auto_routing/decisions.py tests/run_agent/test_provider_fallback.py tests/agent/test_restore_primary_pool_reselect.py tests/plugins/auto_routing/test_auto_fallback.py
git diff --cached --check
git commit -m "feat: enforce recorded auto fallback epochs"
```

---

### Task 8: Gate Active Mode with Doctor and Run the Static-Routing E2E Matrix

**Files:**
- Modify: `plugins/auto_routing/auto_routing/cli.py`
- Modify: `plugins/auto_routing/auto_routing/config.py`
- Modify: `plugins/auto_routing/README.md`
- Create: `tests/plugins/auto_routing/test_static_routing_e2e.py`

**Interfaces:**
- Consumes: adapter status, config/inventory/classifier/safe-default/fallback validation, real temporary Hermes sessions.
- Produces: `explain` command and explicit `shadow -> active` approval gate.

- [ ] **Step 1: Write failing activation and matrix tests**

```python
def test_active_apply_requires_healthy_doctor(cli, active_proposal):
    preview = cli.run("edit", "--proposal", active_proposal, "--json")
    assert preview["valid"] is False
    assert "adapter_signature" in preview["blocking_checks"]


def test_healthy_persisted_active_config_restarts_and_routes(hermes_harness) -> None:
    restarted = hermes_harness("cli", persisted_mode="active", adapter="healthy").restart()
    run = restarted.submit("debug the parser")
    assert restarted.service.adapter_status.critical_projection is True
    assert run.route_decision.projected is True


def test_drifted_persisted_active_config_loads_but_bypasses(hermes_harness) -> None:
    restarted = hermes_harness("cli", persisted_mode="active", adapter="drifted").restart()
    run = restarted.submit("debug the parser")
    assert restarted.service.status()["configured_activation"] == "active"
    assert restarted.service.status()["effective_activation"] == "shadow"
    assert run.route_decision.projected is False
    assert run.first_call.runtime == restarted.baseline_runtime


@pytest.mark.parametrize(
    "access_path", ["subscription", "metered", "custom-pool-a", "custom-pool-b"]
)
def test_entire_route_epoch_uses_exact_recorded_access_path(access_path, hermes_harness) -> None:
    run = hermes_harness("api", configured_access_paths="all").submit_for_path(access_path)
    expected = run.route_decision.primary.runtime_key
    assert run.first_call.provider == expected.provider
    assert run.first_call.model == expected.model
    assert run.first_call.api_mode == expected.api_mode
    assert run.first_call.endpoint_identity == expected.endpoint_identity
    assert run.first_call.auth_identity_probe_tag == expected.auth_identity
    assert run.first_call.credential_pool_identity == expected.credential_pool_identity
    assert run.first_call.local_backend == expected.local_backend
    assert run.first_call.secret_material_was_read_from_hermes is True
    assert run.route_decision.serialized_contains_secret is False
    run.force_first_credential_401_or_429_then_retry()
    assert run.retry_call.auth_identity_probe_tag == expected.auth_identity
    assert run.retry_call.credential_pool_identity == expected.credential_pool_identity


def test_installed_local_backend_identity_is_bound_end_to_end(hermes_harness) -> None:
    run = hermes_harness(
        "api",
        local_backend="ollama:default",
        installed_local_model="qwen3:14b",
        hardware_compatible=True,
    ).submit_for_path("local-ollama")
    expected = run.route_decision.primary.runtime_key
    assert expected.local_backend == "ollama:default"
    assert run.first_call.local_backend == expected.local_backend
    assert run.first_call.endpoint_identity == expected.endpoint_identity
    assert run.first_call.model == "qwen3:14b"
    assert run.first_call.secret_material_was_read_from_hermes is False


def test_same_provider_fallback_then_next_turn_restores_primary_access_path(
    hermes_harness,
) -> None:
    run = hermes_harness("api", same_provider_distinct_pools=True).submit(
        "debug the failing parser"
    )
    run.fail_primary_and_activate_recorded_fallback()
    assert run.fallback_call.credential_pool_identity == "pool:fallback-work"
    next_turn = run.submit_next_turn("continue")
    assert next_turn.first_call.credential_pool_identity == "pool:primary-work"
    next_turn.force_first_credential_401_or_429_then_retry()
    assert next_turn.retry_call.credential_pool_identity == "pool:primary-work"


@pytest.mark.parametrize(
    "surface", ["cli", "gateway", "tui", "desktop", "api", "oneshot", "acp"]
)
def test_supported_surface_routes_before_first_provider_call(surface, hermes_harness):
    run = hermes_harness(surface).submit("debug the failing parser")
    assert run.first_call.provider == "openai-codex"
    assert run.first_call.model == "gpt-5.4"
    assert run.first_call.reasoning_effort == "medium"
    assert run.route_decision.persisted_at <= run.first_call.started_at
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_static_routing_e2e.py
```

Expected: active gate and one or more surface contracts fail.

- [ ] **Step 3: Complete doctor, explain, and activation semantics**

Add:

```text
hermes auto-routing explain DECISION_ID [--detailed] [--json]
```

`doctor` blocks active unless: supported adapter; fresh/child signature guards;
runtime-intent provenance on every enabled surface; config/authority valid;
database writable; inventory current; every primary/fallback verified and
resolvable; safe default compliant; classifier provider/model exactly within
plugin-LLM trust allowlists; bounded overhead pricing; and fallback projection
either healthy or explicitly reported disabled. A verified read-only snapshot
may support reports/shadow degradation but cannot support active routing because
the decision must persist before projection. `edit --apply` changing
`activation.mode` to `active` first runs doctor against the prospective normalized bytes and includes all doctor facts plus adapter preflight identity in the preview hash. Apply requires proposal field `activation_approved: true`, takes the config lock, rechecks the input hash, reruns doctor against those exact prospective bytes and current adapter preflight, and writes only when both hashes/facts match. Parsing an already persisted active config is structural and never authorizes projection by itself; effective activation remains shadow unless the process-local adapter status is healthy.

Scheduled/cron and benchmark/batch agents remain pinned in this version. The
advisor may print a read-only recommended provider/model/reasoning tuple for a
job or benchmark, but it is not an executable Auto snapshot and cannot bypass
the native cron provider/model drift guard. Native per-job reasoning and route
revision fields are required before runtime Auto opt-in can be offered.
Internal auxiliary platforms remain excluded.

Build `hermes_harness(surface)` in this test file with real imports and a
temporary `HERMES_HOME`. Each supported surface uses the actual construction
path and a shared local fake provider that records its first request. The TUI
Desktop case uses the real `tui_gateway` JSON-RPC session path rather than
calling the agent directly. The TUI, Desktop, and ACP cases intentionally
prewarm an unused cache and prove it is safely rebuilt before call one. Define
`active_proposal` against a fixture whose critical adapter signature is
intentionally drifted; a second healthy-doctor test applies the same proposal
with its exact config SHA. Add explicit-same-as-default override cases for CLI,
gateway/API, TUI/Desktop, and ACP. Do not patch the surface constructor or the
adapter wrapper under test.

`local-ollama` uses an installed, hardware-compatible fake loopback runtime
with a non-empty backend identity and the fake provider boundary; no model is
installed and no network call is made.

For the access-path test, configure the same provider/model twice through two
independently named credential-pool/custom endpoint paths plus distinct
subscription and metered paths. The fake endpoints expose only non-secret
probe tags after validating their own secret headers, allowing assertions for
auth identity, endpoint identity, API mode, and pool selection without copying
secret material into routing state. An unaddressable or mismatched resolver
result fails before the first call and preserves baseline. Force the initially
selected credential to return 401/429 and assert native recovery remains in
that exact pool for the retry; add the same assertion for a child and a
recorded fallback.

- [ ] **Step 4: Run the full Stage 2 gate**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing
uv run --extra dev python -m pytest -q \
  tests/gateway/test_agent_cache.py \
  tests/gateway/test_session_model_override_routing.py \
  tests/gateway/test_session_model_override_persistence.py \
  tests/tools/test_delegate.py \
  tests/tools/test_async_delegation.py \
  tests/run_agent/test_provider_fallback.py \
  tests/run_agent/test_switch_model_pool_reload_52727.py \
  tests/run_agent/test_fallback_reasoning_override.py \
  tests/tui_gateway/test_reasoning_config_per_model.py \
  tests/hermes_cli/test_runtime_provider_resolution.py
uv run --extra dev ruff check plugins/auto_routing tests/plugins/auto_routing
git diff --check
```

Expected: all tests pass; adapter drift simulations preserve baseline behavior; no prompt/history injection appears in any surface capture.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing tests/plugins/auto_routing/test_static_routing_e2e.py
git diff --cached --check
git commit -m "feat: activate cache safe auto routing"
```
