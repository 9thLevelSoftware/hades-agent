# Hermes Auto Routing Evidence and Local Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute objective, explicit, behavioral, evaluator, and operational outcome signals to prior routing decisions and expose useful local reports without allowing evidence to mutate any route.

**Architecture:** Existing Hermes observer hooks feed a plugin-owned evidence normalizer keyed by decision/session/turn/API-request identities. A small generic hook-payload enhancement exposes the already-computed canonical turn outcome and active runtime fields. Evidence is append-only, weighted by source strength, content-free by default, and queryable through CLI reports; the adaptive revision pointer remains unchanged throughout this stage.

**Tech Stack:** Python/Pydantic, existing Hermes `post_api_request`, `api_request_error`, `post_llm_call`, `subagent_stop`, and session-switch paths, SQLite/WAL, `PluginLlm` for optional bounded evaluation, pytest, and standard-library statistics.

## Global Constraints

- Complete the Foundation and Cache-Safe Static Runtime plans first and preserve their exact decision/session/child IDs.
- Evidence strength order is objective outcome, explicit user feedback, behavioral proxy, independent evaluator, then operational measurements.
- Silence is not success. A normal answer without verification is `completed_unverified`, never objective success.
- Evaluator evidence runs only when stronger quality evidence is absent and can never upgrade an unverified outcome to objective verification.
- Raw prompts, responses, conversation histories, tool arguments/results, credentials, and free-form feedback notes are not stored in the routing database.
- Full task/response content may be used transiently by the configured evaluator because the approved disclosure mode is `full`; dispose of it after the call.
- Classifier/evaluator provider and model overrides remain inside the immutable plugin-LLM trust allowlists.
- Before an evaluator call, reserve worst-case evaluator overhead; unknown/unbounded price or exhausted limits skips evaluation.
- Operational usage/cost/latency/reliability data is evidence, not a quality verdict.
- Evidence attribution must include decision, route epoch, runtime, reasoning effort, task-context bucket, source, normalized value, confidence, and timestamp.
- Fallback results attribute to the route epoch/runtime that actually produced them, not automatically to the original primary.
- Retried API requests are individual reliability/latency observations but one turn-level quality outcome.
- Stage 3 cannot create or activate an adaptive revision, challenger, experiment, canary, promotion, cooldown, or rollback.
- All reports are local; add no telemetry, upload, attribution tag, or analytics identifier.
- `feedback` is an `append_only_observation`, not a control-plane mutation: it writes one deduplicated finite-vocabulary event transactionally without preview and cannot change authority, mode, the active revision, or an already projected route. Config, mode, pointer, import/restore, and billable operations remain preview/hash guarded.
- Each task ends with focused tests, relevant regressions, `git diff --check`, and one conventional commit.

---

## File Map

### Generic Hermes files modified

- `agent/turn_finalizer.py:427-446` — add canonical outcome, provider, reasoning, API/tool counts, and transformed flag to `post_llm_call`; existing content fields remain unchanged.
- Create `tests/agent/test_turn_finalizer_hooks.py` — assert the generic enriched payload.

### Plugin files created/modified

- Create: `plugins/auto_routing/auto_routing/evidence.py` — signal models, normalization, attribution, evaluator gating, reports.
- Modify: `plugins/auto_routing/auto_routing/models.py` — finalize `EvidenceEvent`, `EvidenceSource`, `TaskContextBucket`, `EvidenceSummary`.
- Modify: `plugins/auto_routing/auto_routing/storage.py` — evidence/event deduplication and report queries.
- Modify: `plugins/auto_routing/auto_routing/service.py` — hook callbacks, explicit feedback, read-only report composition.
- Modify: `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py` — emit manual-reroute/fallback behavioral signals and correlate active route epoch.
- Modify: `plugins/auto_routing/__init__.py` — register evidence observer hooks.
- Modify: `plugins/auto_routing/auto_routing/cli.py` — add `feedback` and `report` commands.
- Modify: `plugins/auto_routing/README.md` — evidence hierarchy, retention, evaluator, privacy, and no-adaptation statement.

### New tests

- `tests/plugins/auto_routing/test_evidence_normalization.py`
- `tests/plugins/auto_routing/test_evidence_hooks.py`
- `tests/plugins/auto_routing/test_evaluator.py`
- `tests/plugins/auto_routing/test_evidence_reporting.py`
- `tests/plugins/auto_routing/test_evidence_privacy.py`
- `tests/plugins/auto_routing/test_evidence_e2e.py`

---

### Task 1: Enrich the Existing Post-Turn Hook with Canonical Outcome Metadata

**Files:**
- Modify: `agent/turn_finalizer.py:427-446`
- Create: `tests/agent/test_turn_finalizer_hooks.py`

**Interfaces:**
- Consumes: `_turn_outcome`, `_turn_outcome_record`, active agent runtime/reasoning, `api_call_count`, and `_turn_tool_count` already computed in `finalize_turn`.
- Produces: versioned backward-compatible `post_llm_call` keyword additions: `hook_payload_version`, `provider`, `reasoning_config`, `outcome`, `outcome_reason`, `api_calls`, `tool_iterations`, and `response_transformed`, plus content-free module contract constants.

- [ ] **Step 1: Write the failing generic hook test**

```python
def test_post_llm_call_includes_canonical_outcome_and_runtime(monkeypatch, finalizer_agent):
    calls = []
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda name, **kwargs: calls.append((name, kwargs)) or [],
    )
    finalize_verified_turn(finalizer_agent, api_calls=2, tool_iterations=1)
    captured = next(kwargs for name, kwargs in calls if name == "post_llm_call")
    assert captured["outcome"] == "verified"
    assert captured["provider"] == "openai-codex"
    assert captured["hook_payload_version"] == POST_LLM_CALL_PAYLOAD_VERSION == 2
    assert POST_LLM_CALL_PAYLOAD_FIELDS <= captured.keys()
    assert captured["reasoning_config"] == {"effort": "medium"}
    assert captured["api_calls"] == 2
    assert captured["tool_iterations"] == 1
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/agent/test_turn_finalizer_hooks.py
```

Expected: the captured payload lacks one or more new fields.

- [ ] **Step 3: Add fields to the existing observer invocation**

Define `POST_LLM_CALL_PAYLOAD_VERSION = 2` and an immutable
`POST_LLM_CALL_PAYLOAD_FIELDS` containing every canonical key below. Extend
the call without changing hook timing or removing old fields:

```python
_invoke_hook(
    "post_llm_call",
    hook_payload_version=POST_LLM_CALL_PAYLOAD_VERSION,
    session_id=agent.session_id,
    task_id=effective_task_id,
    turn_id=turn_id,
    user_message=original_user_message,
    assistant_response=final_response,
    conversation_history=list(messages),
    model=agent.model,
    provider=agent.provider,
    reasoning_config=dict(agent.reasoning_config or {}),
    platform=getattr(agent, "platform", None) or "",
    outcome=_turn_outcome["outcome"],
    outcome_reason=_turn_outcome["reason"],
    api_calls=api_call_count,
    tool_iterations=_turn_tool_count,
    response_transformed=_response_transformed,
)
```

Keep the existing best-effort exception boundary so a plugin cannot discard the user response.

In the new test file, define `finalizer_agent` with the same minimal agent
protocol required by `finalize_turn`; set provider/model/reasoning/session and
an iteration budget, and make cleanup/persistence methods no-ops.
`finalize_verified_turn()` calls the real `finalize_turn` with
`final_response="verified response"`, `interrupted=False`, `failed=False`,
`_turn_exit_reason="completed"`, a message list containing the requested
number of assistant tool-call turns, and a pre-set verified turn status. Do not
stub `finalize_turn` or `classify_turn_outcome`.

- [ ] **Step 4: Run GREEN and finalizer regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/agent/test_turn_finalizer_hooks.py \
  tests/agent/test_turn_outcome.py \
  tests/agent/test_turn_ledger.py
git diff --check
```

Expected: all tests pass and old hook subscribers still receive their original fields.

- [ ] **Step 5: Commit**

```bash
git add agent/turn_finalizer.py tests/agent/test_turn_finalizer_hooks.py
git diff --cached --check
git commit -m "feat: expose canonical outcome to plugin hooks"
```

---

### Task 2: Define Evidence Records, Weights, Deduplication, and Context Buckets

**Files:**
- Create: `plugins/auto_routing/auto_routing/evidence.py`
- Modify: `plugins/auto_routing/auto_routing/models.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Create: `tests/plugins/auto_routing/test_evidence_normalization.py`

**Interfaces:**
- Consumes: decision/epoch IDs, canonical outcomes, API observations, explicit feedback, behavioral facts.
- Produces: `normalize_signal(signal) -> EvidenceEvent`, `RoutingStore.record_evidence(event)`, and `context_bucket(decision) -> TaskContextBucket`.

- [ ] **Step 1: Write failing hierarchy/dedup/context tests**

```python
def test_objective_verification_outweighs_behavioral_proxy(normalizer):
    verified = normalizer.turn_outcome(outcome="verified", reason="tests passed")
    retry = normalizer.behavior(kind="repeated_prompt")
    assert verified.signal_type == "objective_quality"
    assert verified.confidence_weight == pytest.approx(1.0)
    assert retry.confidence_weight == pytest.approx(0.20)


def test_operational_success_is_not_quality_success(normalizer):
    event = normalizer.api_success(latency_seconds=1.2, cost_usd=0.01)
    assert event.signal_type == "operational"
    assert event.quality_value is None
    assert event.reliability_value == 1.0


def test_event_idempotency_and_contextual_bucket(store, decision):
    event = evidence_event(source_event_id="turn-1:objective", decision=decision)
    store.record_evidence(event)
    store.record_evidence(event)
    assert store.count_evidence(decision.decision_id) == 1
    assert event.context_bucket.domains == ("coding",)
    assert event.context_bucket.reasoning_effort == "medium"


def test_transformed_response_keeps_operations_but_suppresses_model_quality(normalizer):
    events = normalizer.events_for_turn(
        outcome="verified",
        reason="tests passed",
        response_transformed=True,
    )
    assert not any(event.quality_value is not None for event in events)
    assert any(
        event.signal_type == "operational"
        and event.metadata["quality_suppressed_reason"] == "response_transformed"
        for event in events
    )
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_evidence_normalization.py
```

Expected: missing evidence models/schema.

- [ ] **Step 3: Add append-only schema and exact source weights**

Add `evidence_events` with unique `(source, source_event_id, decision_id, route_epoch)`, JSON context bucket, normalized quality/reliability values, latency/cost/tokens, confidence weight, verifier/evaluator identity, provenance timestamp, and no content columns. Use these default weights, configurable downward but not upward past the source cap:

```python
SOURCE_CAPS = {
    "objective": 1.00,
    "explicit": 0.90,
    "behavioral": 0.30,
    "evaluator": 0.25,
    "operational": 0.20,
}
```

Map `verified -> objective quality 1.0`. Map `failed -> objective quality 0.0`
only when the canonical reason identifies a verifier/test/structured-workflow
failure attributable to the produced result. `cancelled`, `partial`,
`unresolved`, `blocked`, provider/budget failures, and `completed_unverified`
have no objective quality value; normalize their operational or weak
behavioral facts separately. Operational observations never carry a quality
value, so their cap applies only to reliability evidence; measured
latency/cost remain numeric observations rather than quality votes. Context
buckets include authority ID, normalized domains, complexity band
(`trivial/simple/moderate/hard/extreme`), sorted capabilities, profile ID and lineage,
runtime stable ID, and reasoning effort.

When `response_transformed=True`, retain provider-call latency/cost/token and
operational reliability evidence, but emit no model quality value from the
canonical outcome, behavioral inference, explicit feedback, or evaluator for
that final response. Record only finite provenance
`quality_suppressed_reason=response_transformed`; do not store transformer
content or identity. The current hook cannot prove an identity-preserving
transform, so fail closed instead of crediting/blaming the routed model for
another plugin's output.

Use these stable `EvidenceEvent` field names in every later plan:
`source_event_id`, `decision_id`, `route_epoch`, `runtime_key`, `signal_type`, `source`,
`context_bucket`, `quality_value`, `reliability_value`, `latency_seconds`,
`effective_cost_usd`, `input_tokens`, `output_tokens`, `confidence_weight`,
`verifier_identity`, `occurred_at`, and finite enum/numeric `metadata`.

- [ ] **Step 4: Run GREEN and reopen tests**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_evidence_normalization.py \
  tests/plugins/auto_routing/test_storage.py
git diff --check
```

Expected: all tests pass; duplicate callbacks cannot double-count an event.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/auto_routing/evidence.py plugins/auto_routing/auto_routing/models.py plugins/auto_routing/auto_routing/storage.py tests/plugins/auto_routing/test_evidence_normalization.py
git diff --cached --check
git commit -m "feat: normalize routing evidence"
```

---

### Task 3: Capture Objective and Operational Signals from Existing Hooks

**Files:**
- Modify: `plugins/auto_routing/__init__.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Modify: `plugins/auto_routing/auto_routing/evidence.py`
- Create: `tests/plugins/auto_routing/test_evidence_hooks.py`

**Interfaces:**
- Consumes: `post_api_request`, `api_request_error`, `post_llm_call`, and `subagent_stop` kwargs.
- Produces: `on_api_success`, `on_api_error`, `on_turn_complete`, and `on_subagent_stop` observer callbacks.

- [ ] **Step 1: Write failing hook-attribution tests**

```python
def test_post_api_request_attributes_actual_fallback_epoch(service, fallback_decision):
    service.on_api_success(
        session_id=fallback_decision.session_id,
        turn_id="turn-2",
        api_request_id="req-9",
        model="gpt-5.3-codex",
        provider="openai-codex",
        api_duration=2.4,
        response={
            "model": "gpt-5.3-codex",
            "finish_reason": "stop",
            "assistant_message": {
                "role": "assistant",
                "content": "discard me",
                "tool_calls": [],
            },
            "usage": {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.0},
        },
    )
    event = service.store.list_evidence(fallback_decision.decision_id)[0]
    assert event.route_epoch == 1
    assert event.runtime_key.model == "gpt-5.3-codex"
    verification = service.store.read_inventory(event.runtime_key)
    assert verification.verification_source == "routed_success"
    assert verification.verified_until > service.clock.now()


def test_unrouted_or_unknown_session_is_ignored(service):
    service.on_api_error(session_id="manual-session", api_request_id="req-x", model="m", provider="p")
    assert service.store.count_all_evidence() == 0
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_evidence_hooks.py
```

Expected: callbacks are unregistered or events are not attributed.

- [ ] **Step 3: Register read-only observer hooks**

In root registration:

```python
ctx.register_hook("post_api_request", service.on_api_success)
ctx.register_hook("api_request_error", service.on_api_error)
ctx.register_hook("post_llm_call", service.on_turn_complete)
ctx.register_hook("subagent_stop", service.on_subagent_stop)
```

Callbacks accept the exact existing hook payloads, look up the persisted
decision/epoch from session/child identity, verify actual provider/model matches
an allowed decision target, extract only numeric usage from
`response["usage"]`, redact error classes/reasons to finite categories, and
append evidence best-effort. Do not persist `response`, `assistant_message`,
`request`, `user_message`, `assistant_response`, `conversation_history`, base
URLs, or error message text. Usage stores numeric fields only.

`on_turn_complete` passes the canonical `response_transformed` flag into the
normalizer before creating any quality signal, so Task 2's suppression rule is
enforced on every hook path and optional evaluator scheduling.

After a successful call is attributed to exactly one persisted decision epoch,
refresh that exact `RuntimeKey` verification TTL with source `routed_success`.
Provider/model equality alone is insufficient: auth identity, endpoint identity,
and API mode come from the recorded decision target. Unknown/manual sessions and
ambiguous configured paths cannot create or refresh verification. This is the
ordinary-success verification source promised by the inventory design; the
Stage 1 explicit probe remains the only way to bill solely for access checking.

Define `service` with a real temporary store and register callbacks through a
real `PluginContext`. Construct `fallback_decision` as a persisted decision
with primary epoch zero and fallback epoch one; invoke callbacks directly only
after registration has been asserted. Provider/network objects remain fakes,
but hook dispatch and storage are real.

- [ ] **Step 4: Run GREEN and hook regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_evidence_hooks.py \
  tests/hermes_cli/test_plugins.py \
  tests/tools/test_delegate.py
git diff --check
```

Expected: all tests pass; a hook/storage failure does not alter the agent result.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/__init__.py plugins/auto_routing/auto_routing/service.py plugins/auto_routing/auto_routing/evidence.py tests/plugins/auto_routing/test_evidence_hooks.py
git diff --cached --check
git commit -m "feat: collect routed outcome evidence"
```

---

### Task 4: Record Explicit Feedback and Conservative Behavioral Signals

**Files:**
- Modify: `plugins/auto_routing/auto_routing/evidence.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Modify: `plugins/auto_routing/auto_routing/adapters/hermes_0_18.py`
- Modify: `plugins/auto_routing/auto_routing/cli.py`
- Create: `tests/plugins/auto_routing/test_evidence_reporting.py`

**Interfaces:**
- Consumes: explicit CLI feedback, manual model switch after Auto, fallback epoch, retry/error counts, transient follow-up text.
- Produces: `record_feedback`, `record_behavior`, and CLI `feedback`.

- [ ] **Step 1: Write failing feedback/proxy tests**

```python
def test_explicit_feedback_maps_rating_and_signal(cli, decision_id):
    result = cli.run(
        "feedback", decision_id, "--rating", "2", "--signal", "corrected", "--json"
    )
    event = routing_store().list_evidence(decision_id)[0]
    assert result["recorded"] is True
    assert event.quality_value == pytest.approx(0.25)
    assert event.confidence_weight == pytest.approx(0.90)
    assert event.metadata == {"feedback_signal": "corrected", "rating": 2}


def test_manual_reroute_is_negative_behavior_not_objective_failure(auto_agent):
    auto_agent.switch_model("gpt-5.3-codex", "openai-codex", api_key="redacted", base_url="https://api.test/v1")
    event = routing_store().latest_evidence(auto_agent._auto_routing_decision_id)
    assert event.signal_type == "behavioral_quality"
    assert event.quality_value == 0.0
    assert event.confidence_weight <= 0.30


def test_feedback_for_transformed_response_is_not_model_evidence(cli, transformed_decision_id):
    result = cli.run(
        "feedback", transformed_decision_id,
        "--rating", "1", "--signal", "rejected", "--json",
    )
    assert result == {"recorded": False, "reason": "response_transformed"}
    assert routing_store().list_evidence(transformed_decision_id) == []


def test_feedback_is_idempotent_append_only_and_cannot_move_control_plane(cli, decision_id):
    store = routing_store()
    before = store.control_plane_fingerprint()
    args = ("feedback", decision_id, "--rating", "5", "--signal", "accepted", "--json")
    first = cli.run(*args)
    second = cli.run(*args)

    assert cli.command_metadata("feedback").write_class == "append_only_observation"
    assert first["event_id"] == second["event_id"]
    assert len(store.list_evidence(decision_id)) == 1
    assert store.control_plane_fingerprint() == before
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_evidence_reporting.py
```

Expected: command/signals do not exist.

- [ ] **Step 3: Add finite feedback vocabulary and weak proxies**

Register:

```text
hermes auto-routing feedback DECISION_ID --rating {1,2,3,4,5} \
  --signal {accepted,rejected,corrected,rerouted} [--json]
```

Map ratings to `(rating - 1) / 4`. Do not accept a free-form note argument. Look up the immutable turn-attribution record first and refuse model feedback when `response_transformed` is true. Behavioral events are finite: `manual_reroute=0.30`, `recorded_fallback=0.25`, `repeated_api_failure=0.25`, `explicit_correction_phrase=0.20`, `rapid_retry=0.15`, and `abandonment_after_unresolved=0.15`. Correction detection may inspect the live follow-up transiently using a fixed phrase list, but stores only the enum and confidence. A positive behavioral proxy never exceeds `0.30` and cannot turn silence into success.

Declare this command's write class as `append_only_observation` in CLI help and JSON output. Derive a unique source-event key from `(decision_id, rating, signal, current attributed turn_id)` and insert-or-return it transactionally so retries are idempotent. The handler never invokes adaptation, advances a revision pointer, edits YAML, or changes a live route; assert all four invariants in this task's tests.

Define `cli`, `decision_id`, `transformed_decision_id`, and `auto_agent` in this test file around the
real registered command, persisted decision, and manual-switch wrapper. The
runtime switch receives fake credential values at the provider boundary; scan
the evidence row/log capture to prove neither value is retained.

- [ ] **Step 4: Run GREEN and manual-switch regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_evidence_reporting.py \
  tests/run_agent/test_switch_model_reasoning_override.py \
  tests/gateway/test_session_model_override_routing.py
git diff --check
```

Expected: all tests pass and manual switching retains its original runtime/cache behavior.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/auto_routing/evidence.py plugins/auto_routing/auto_routing/service.py plugins/auto_routing/auto_routing/adapters/hermes_0_18.py plugins/auto_routing/auto_routing/cli.py tests/plugins/auto_routing/test_evidence_reporting.py
git diff --cached --check
git commit -m "feat: record routing feedback signals"
```

---

### Task 5: Add Optional Last-Resort Independent Evaluation

**Files:**
- Modify: `plugins/auto_routing/auto_routing/evidence.py`
- Modify: `plugins/auto_routing/auto_routing/config.py`
- Create: `tests/plugins/auto_routing/test_evaluator.py`

**Interfaces:**
- Consumes: transient prompt/response, profile domain/rubric, configured evaluator trust/budget, absence of stronger evidence.
- Produces: `Evaluator.evaluate(context) -> EvidenceEvent | None`.

- [ ] **Step 1: Write failing gate/schema/budget tests**

```python
@pytest.mark.parametrize("event_factory", [objective_event, explicit_event, behavioral_event])
def test_evaluator_skips_when_stronger_evidence_exists(evaluator, context, event_factory):
    stronger = context.model_copy(update={"stronger_evidence": (event_factory(),)})
    assert evaluator.evaluate(stronger) is None
    assert evaluator.llm.call_count == 0


def test_evaluator_is_bounded_and_revalidated(evaluator, context):
    evaluator.llm.parsed = {
        "quality": 0.72,
        "rubric": {"correctness": 0.8},
        "confidence": 0.6,
    }
    event = evaluator.evaluate(context)
    assert event.signal_type == "evaluator_quality"
    assert event.confidence_weight == pytest.approx(0.15)
    assert event.verifier_identity == "openai-codex/gpt-5.4"


def test_unknown_evaluator_price_skips_call(evaluator, context):
    evaluator.worst_case_cost = None
    assert evaluator.evaluate(context) is None
    assert evaluator.last_reason == "unbounded_evaluator_cost"


def test_evaluator_cannot_judge_its_own_routed_runtime(evaluator, context):
    same_runtime = context.model_copy(
        update={"routed_runtime_id": evaluator.runtime_key.stable_id()}
    )
    assert evaluator.evaluate(same_runtime) is None
    assert evaluator.last_reason == "evaluator_not_independent"


def test_same_model_different_auth_path_is_still_not_independent(evaluator, context):
    same_model_other_auth = context.model_copy(
        update={
            "routed_runtime_key": evaluator.runtime_key.model_copy(
                update={"auth_identity": "api-key:other"}
            )
        }
    )
    assert same_model_other_auth.routed_runtime_key.stable_id() != evaluator.runtime_key.stable_id()
    assert evaluator.evaluate(same_model_other_auth) is None
    assert evaluator.last_reason == "evaluator_not_independent"


def test_transformed_response_is_never_sent_to_evaluator(evaluator, context):
    transformed = context.model_copy(update={"response_transformed": True})
    assert evaluator.evaluate(transformed) is None
    assert evaluator.llm.call_count == 0
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing/test_evaluator.py
```

Expected: evaluator is absent.

- [ ] **Step 3: Implement rubric-only structured evaluation**

Call `PluginLlm.complete_structured` with a domain rubric, prompt/response, no
candidate scores or expected target, temperature zero, strict
`EvaluationResult`, configured reasoning effort, timeout, and max output. Skip
when the response was transformed, when any objective, explicit, or behavioral
quality event already exists, or when the evaluator is not independent of the
producer. Independence requires different canonical model-family identity—not
merely a different `RuntimeKey`; subscription/API/custom access to the same
model cannot self-evaluate. Use catalog/provider alias metadata to canonicalize
family identity, fall back to normalized provider/model equality, and skip on
unknown/ambiguous aliases. Reserve `max_evaluator_calls_per_day` and worst-case
USD first. Final confidence is `min(model_confidence, 1.0) * 0.25`. Store only
normalized rubric component numbers and evaluator provider/model; discard
content and prose.

Malformed/refused/timed-out/denied evaluation records no evidence event and never invents a score.

Define `context` as a frozen transient evaluation record carrying the full routed runtime key, canonical model-family identity, and transformation flag, and `evaluator` with
a counted fake `PluginLlm` completion plus real budget ledger. The fake returns
only at the provider boundary. `objective_event()`, `explicit_event()`, and
`behavioral_event()` construct the stable event schema from Task 2; tests must assert the transient prompt/response never
appears in the store.

- [ ] **Step 4: Run GREEN and trust/budget regressions**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_evaluator.py \
  tests/agent/test_plugin_llm.py \
  tests/plugins/auto_routing/test_storage.py
git diff --check
```

Expected: all tests pass and reservation reconciliation is exact.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing/auto_routing/evidence.py plugins/auto_routing/auto_routing/config.py tests/plugins/auto_routing/test_evaluator.py
git diff --cached --check
git commit -m "feat: evaluate routed outcomes conservatively"
```

---

### Task 6: Ship Local Reports, Privacy Scan, and the No-Adaptation E2E Gate

**Files:**
- Modify: `plugins/auto_routing/auto_routing/evidence.py`
- Modify: `plugins/auto_routing/auto_routing/storage.py`
- Modify: `plugins/auto_routing/auto_routing/service.py`
- Modify: `plugins/auto_routing/auto_routing/cli.py`
- Modify: `plugins/auto_routing/README.md`
- Create: `tests/plugins/auto_routing/test_evidence_privacy.py`
- Create: `tests/plugins/auto_routing/test_evidence_e2e.py`

**Interfaces:**
- Consumes: append-only evidence and decisions.
- Produces: `report`, evidence section in `explain --detailed`, and proof that active adaptive revision never changes.

- [ ] **Step 1: Write failing report/privacy/no-mutation tests**

```python
def test_report_separates_quality_reliability_latency_cost_and_confidence(cli, evidence_fixture):
    report = cli.run("report", "--profile", "coding", "--days", "30", "--json")
    row = report["runtimes"][0]
    assert set(row) >= {"quality", "reliability", "latency", "cost", "sample_weight", "confidence"}
    assert report["warnings"] == ["completed_unverified events are not counted as quality success"]


def test_database_and_logs_exclude_content_and_credentials(evidence_fixture, state_db, log_files):
    forbidden = ["unique raw prompt", "unique raw response", "sk-secret-value", "oauth-secret-value"]
    artifacts = sorted({state_db, *state_db.parent.glob(f"{state_db.name}*"), *log_files})
    haystack = b"".join(path.read_bytes() for path in artifacts if path.exists())
    assert not any(value.encode("utf-8") in haystack for value in forbidden)


def test_evidence_collection_never_advances_revision(service, evidence_fixture):
    before = service.store.read_active_revision(service.authority_id).revision_id
    service.process_pending_evidence()
    assert service.store.read_active_revision(service.authority_id).revision_id == before
    assert service.store.count_adaptive_mutations() == 0
```

- [ ] **Step 2: Run RED**

```bash
uv run --extra dev python -m pytest -q \
  tests/plugins/auto_routing/test_evidence_privacy.py \
  tests/plugins/auto_routing/test_evidence_e2e.py
```

Expected: report/privacy/no-adaptation contracts are incomplete.

- [ ] **Step 3: Add exact read-only report surface**

Register:

```text
hermes auto-routing report [--profile ID] [--runtime ID] [--days N] [--json]
```

Reports group by profile/runtime/reasoning/context, show raw source counts/weights, posterior-free weighted means, p50/p95 latency, observed cost/tokens, reliability/error categories, evidence dates, and confidence caveats. Do not aggregate unrelated domains into a universal winner. `explain --detailed` lists evidence IDs and source metadata without content.

Document that Stage 3 is observation-only and the operator should inspect reports before enabling Plan 4.

In the privacy/E2E files, build `evidence_fixture` through the real hook
callbacks with distinct raw prompt/response/credential sentinels. `state_db` is
the actual profile database and `log_files` contains every file under the
profile's logs directory. Check the SQLite database, WAL/SHM siblings, and logs
as bytes so deleted/free pages cannot hide a previously persisted sentinel;
flush log handlers and close the store before reading artifacts. The E2E service starts from a published
baseline revision and records the active authority ID explicitly.

- [ ] **Step 4: Run the Stage 3 gate**

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing
uv run --extra dev python -m pytest -q \
  tests/agent/test_turn_finalizer_hooks.py \
  tests/agent/test_turn_outcome.py \
  tests/agent/test_turn_ledger.py \
  tests/hermes_cli/test_plugins.py \
  tests/tools/test_delegate.py
uv run --extra dev ruff check plugins/auto_routing tests/plugins/auto_routing
git diff --check
```

Expected: all tests pass, privacy scans find none of the sentinels, and the adaptive revision ID is unchanged.

- [ ] **Step 5: Commit**

```bash
git add plugins/auto_routing tests/plugins/auto_routing agent/turn_finalizer.py tests/agent/test_turn_finalizer_hooks.py
git diff --cached --check
git commit -m "feat: report local auto routing evidence"
```
