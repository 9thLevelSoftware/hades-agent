# Turn-Outcome Ledger Implementation Plan

> For agentic workers: implement this plan task-by-task. The ledger is telemetry and learning plumbing, not a new model tool. Keep all writes best-effort so a broken ledger can never fail a user turn.

**Goal:** Persist Hermes' existing eight-state turn outcome with per-turn cost/tool/skill context, then use the recorded signal to trigger targeted reflection and rank skills by measured utility.

**Architecture:** Add one append-only `turn_outcomes` table and a small `agent/turn_ledger.py` adapter around `SessionDB`. Call the adapter from both finalizer paths (`turn_finalizer.py` and `codex_runtime.py`) after classification, with an exception boundary. Derive correction, failure-streak, and reaction signals as updates to the previous ledger row, not as a second event store. Feed utility as evidence to the existing Curator and skill index, never as an irreversible automatic archive rule.

**Tech Stack:** Python, SQLite/FTS5 `SessionDB`, existing `classify_turn_outcome`, `skill_usage` sidecar, background-review fork, gateway platform adapter events, CLI insights formatter, and existing prompt-cache-stable system-prompt snapshot.

## Global Constraints

- Preserve the existing `TurnOutcome` vocabulary: `verified`, `completed_unverified`, `partial`, `blocked`, `failed`, `interrupted`, `unresolved`, and `cancelled`.
- Write from both finalizer paths. A ledger that only covers chat completions is incomplete; Codex app-server turns must be represented too.
- Ledger writes are exception-guarded and never change the turn result, response, or finalizer exit path.
- Do not place outcome-driven skill ordering into a live system prompt that can change mid-conversation. Utility ranking is computed at session start and remains stable for that session; cache invalidation affects only later sessions.
- Failure-triggered review must retain the existing environment-dependent-failure and negative-tool-claim exclusions. A failed turn is a signal to inspect, not proof that a skill is bad.
- Use Laplace smoothing and minimum sample thresholds. Correlation is evidence for the Curator, not an auto-delete predicate.
- Reaction and correction ingestion must be authenticated by the existing platform/session identity and must not synthesize an extra user turn for the same feedback event.
- Use YAML/config for intervals, cooldowns, and thresholds. Do not introduce non-secret `HERMES_*` configuration.
- Validate persistence and gateway events with temporary `HERMES_HOME` and real SQLite/file-lock paths; do not rely on MagicMock-only tests.

## Current-State Review

The source review confirms a disconnected-but-usable foundation:

- `agent/turn_outcome.py` already classifies eight outcomes and is called at `agent/turn_finalizer.py` and `agent/codex_runtime.py`; `hermes_state.py` currently has no outcome table or outcome references.
- Background review is forked from `agent/background_review.py`, but normal triggers are blind interval counters and currently require `verified` outcome.
- `tools/skill_usage.py` persists open-ended per-skill records with locking; it counts views/uses/patches but not helped/hurt outcomes.
- `agent/insights.py` already extracts `skill_view` calls from stored messages and supplies CLI/gateway report surfaces.
- `agent/prompt_builder.py` renders an alphabetical skill index with disk snapshot caching; changing it per turn would violate the repository's prompt-cache invariant.
- Reactions are not uniformly normalized: Matrix resolves approval reactions, Feishu can inject synthetic text events, Slack subscribes but discards `reaction_added`, and Telegram/Discord have platform-specific reaction APIs. The plan normalizes feedback without rewriting the whole gateway.

The plan skips a new analytics backend, a new event bus, and an LLM correction classifier. Lexical correction detection plus existing platform events is the ponytail minimum.

## Dependency and Release Order

1. Ledger schema, accessors, and dual-finalizer write adapter.
2. Per-turn deltas, skill attribution, and sidecar utility counters.
3. Failure/correction/reaction signal normalization and cooldowned review triggers.
4. Utility evidence in Curator, prompt index, and Insights.
5. End-to-end persistence and platform verification.

## File Map

- Create: `agent/turn_ledger.py` — immutable record construction, serialization, exception-guarded write helper.
- Create: `agent/reflection_triggers.py` — lexical correction, tool-failure streak, outcome trigger, cooldown/dedup state.
- Modify: `hermes_state.py` — `turn_outcomes` DDL, migration reconciliation, record/query/update methods.
- Modify: `agent/turn_finalizer.py` — record the classified result and invoke reflection trigger evaluation.
- Modify: `agent/codex_runtime.py` — record the same fields for Codex app-server/runtime turns.
- Modify: `agent/turn_context.py` — capture turn-start token/cost counters and incoming-message identity.
- Modify: `agent/background_review.py` — add a failure-signal prompt variant and single-flight/cooldown guard.
- Modify: `tools/skill_usage.py` — outcome counters and smoothed utility query.
- Modify: `agent/insights.py` — outcome trends and per-skill utility report sections.
- Modify: `hermes_cli/subcommands/insights.py` — render the new report without changing existing output defaults.
- Modify: `agent/curator.py` — include utility evidence in candidate prompts; never hard-delete on utility alone.
- Modify: `agent/prompt_builder.py` — rank/filter the next-session skill index using frozen utility snapshot data.
- Modify: `agent/reactions.py` — add narrowly scoped correction/feedback classification helpers.
- Modify: `gateway/platforms/base.py` — normalize reaction feedback callback/message type without forcing every adapter to implement it.
- Modify: `plugins/platforms/telegram/adapter.py`, `plugins/platforms/discord/adapter.py`, `plugins/platforms/slack/adapter.py` — publish authenticated feedback events where platform support exists.
- Modify: `plugins/platforms/feishu/adapter.py`, `plugins/platforms/photon/adapter.py` — mark existing synthetic-event paths so feedback is not double-counted.
- Test: `tests/agent/test_turn_outcome.py`, `tests/agent/test_turn_finalizer_memory_gating.py`, `tests/agent/test_codex_runtime*.py`.
- Test: new `tests/agent/test_turn_ledger.py`, `tests/agent/test_reflection_triggers.py`.
- Test: `tests/tools/test_skill_usage.py`, `tests/agent/test_insights.py`, `tests/agent/test_prompt_builder.py`.
- Test: platform reaction tests alongside existing Telegram/Discord/Slack/Feishu/Matrix gateway suites.

## Data Contracts

The database row is the source of truth for one completed turn:

```python
@dataclass(frozen=True)
class TurnOutcomeRecord:
    session_id: str
    turn_id: str
    created_at: float
    outcome: str
    outcome_reason: str | None
    turn_exit_reason: str | None
    api_calls: int
    tool_iterations: int
    retry_count: int
    guardrail_halt: str | None
    cost_usd_delta: float
    input_tokens_delta: int
    output_tokens_delta: int
    cache_read_tokens_delta: int
    skills_loaded: tuple[str, ...]
    model: str | None
```

Persist `skills_loaded` and guardrail details as JSON text. Add nullable `feedback_kind`, `feedback_value`, `feedback_source`, and `feedback_at` fields for a later correction/reaction annotation; one row may receive at most one latest correction marker per source event id.

Required `SessionDB` methods:

```python
record_turn_outcome(record: TurnOutcomeRecord) -> None
annotate_turn_feedback(session_id: str, turn_id: str, *, kind: str, value: str, source: str, event_id: str) -> bool
get_outcome_trends(*, session_id: str | None = None, days: int = 30) -> list[dict[str, object]]
get_skill_outcome_counts(*, days: int = 30) -> list[dict[str, object]]
```

Utility is computed as:

```python
utility = (helped + 1.0) / (helped + hurt + 2.0)
```

where `verified` is helped, `failed`/`blocked`/`unresolved` are hurt evidence, other outcomes are neutral, and a skill is not demoted until it has at least five attributed turns.

## Task 1: Persist the Ledger and Cover Both Finalizers

**Files:**
- Modify: `hermes_state.py`
- Create: `agent/turn_ledger.py`
- Modify: `agent/turn_finalizer.py`
- Modify: `agent/codex_runtime.py`
- Modify: `agent/turn_context.py`
- Test: `tests/agent/test_turn_ledger.py`
- Test: `tests/agent/test_turn_outcome.py`

**Interfaces:**
- Consumes: `classify_turn_outcome`, existing session token/cost counters, finalizer exit fields, and `SessionDB` migration reconciliation.
- Produces: `record_turn_outcome_safely(agent, outcome, turn_context) -> None` and the methods in the data contract.

- [ ] Step 1: Add a temp-home SQLite test that proves the new table accepts all eight outcomes and round-trips JSON fields.

```python
def test_turn_outcome_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB.open_for_profile("default")
    record = TurnOutcomeRecord(
        session_id="s1", turn_id="t1", created_at=1.0,
        outcome="unresolved", outcome_reason="tool timeout",
        turn_exit_reason="tool_timeout", api_calls=2, tool_iterations=1,
        retry_count=1, guardrail_halt=None, cost_usd_delta=0.12,
        input_tokens_delta=20, output_tokens_delta=4,
        cache_read_tokens_delta=10, skills_loaded=("plan", "web"), model="test-model",
    )
    db.record_turn_outcome(record)
    assert db.get_outcome_trends(session_id="s1", days=30)[0]["outcome"] == "unresolved"
    assert json.loads(db.get_outcome_trends(session_id="s1", days=30)[0]["skills_loaded"]) == ["plan", "web"]
```

- [ ] Step 2: Run the focused test and observe the missing-table failure.

```bash
python -m pytest tests/agent/test_turn_ledger.py -q
```

- [ ] Step 3: Add `turn_outcomes` to the declarative schema/migration path. Use a unique `(session_id, turn_id)` key, indexes on `(created_at, outcome)` and `(session_id, created_at)`, and WAL-safe parameterized writes. Do not rewrite or duplicate the sessions table.

- [ ] Step 4: Implement `TurnOutcomeRecord`, `record_turn_outcome_safely`, feedback columns, and query methods. The safe wrapper logs a warning with session/turn identifiers and returns `None` on database errors; it never raises through finalization.

- [ ] Step 5: Capture turn-start token/cost snapshots in `turn_context.py` and diff them at finalization. Missing counters produce zero deltas rather than `None` arithmetic.

- [ ] Step 6: Call the safe adapter immediately after `classify_turn_outcome` in both finalizer paths. Add a test that monkeypatches the safe writer to capture calls from `turn_finalizer` and `codex_runtime`, then assert both produce the same outcome vocabulary.

```python
def test_both_finalizers_record_outcomes(monkeypatch):
    records = []
    monkeypatch.setattr(turn_ledger, "record_turn_outcome_safely", records.append)
    finalize_chat_turn(agent_fixture)
    finalize_codex_turn(agent_fixture)
    assert {record.outcome for record in records} <= set(TURN_OUTCOMES)
    assert len(records) == 2
```

- [ ] Step 7: Run ledger, outcome, finalizer-gating, and Codex focused tests.

```bash
python -m pytest \
  tests/agent/test_turn_ledger.py \
  tests/agent/test_turn_outcome.py \
  tests/agent/test_turn_finalizer_memory_gating.py \
  tests/agent/test_codex_runtime.py -q
```

- [ ] Step 8: Commit the durable foundation.

```bash
git add hermes_state.py agent/turn_ledger.py agent/turn_finalizer.py agent/codex_runtime.py agent/turn_context.py tests/agent/test_turn_ledger.py tests/agent/test_turn_outcome.py
git diff --cached --check
git commit -m "feat(learning): persist per-turn outcomes"
```

## Task 2: Attribute Skills and Record Utility Evidence

**Files:**
- Modify: `agent/turn_ledger.py`
- Modify: `tools/skill_usage.py`
- Modify: `agent/insights.py`
- Test: `tests/tools/test_skill_usage.py`
- Test: `tests/agent/test_turn_ledger.py`
- Test: `tests/agent/test_insights.py`

**Interfaces:**
- Consumes: ledger rows and the existing skill-view extraction in `insights.py`.
- Produces: `extract_loaded_skills(messages) -> tuple[str, ...]`, `bump_outcome(skill, outcome, cost_delta) -> None`, and `get_skill_utility(skill) -> dict[str, float | int]`.

- [ ] Step 1: Add tests for exact attribution and neutral outcome handling.

```python
def test_skill_attribution_uses_skill_view_calls_only():
    messages = [
        {"role": "assistant", "tool_calls": [{"name": "skill_view", "arguments": {"name": "web"}}]},
        {"role": "assistant", "tool_calls": [{"name": "read_file", "arguments": {"path": "x"}}]},
    ]
    assert extract_loaded_skills(messages) == ("web",)


def test_skill_utility_is_smoothed_and_requires_minimum_sample():
    for _ in range(2):
        bump_outcome("web", "verified", 0.01)
    assert get_skill_utility("web")["eligible"] is False
    for _ in range(3):
        bump_outcome("web", "failed", 0.01)
    assert get_skill_utility("web")["utility"] == 3 / 7
```

- [ ] Step 2: Run the tests against the current activity-only sidecar.

```bash
python -m pytest tests/tools/test_skill_usage.py tests/agent/test_turn_ledger.py -k 'attribution or utility' -q
```

- [ ] Step 3: Reuse the existing `insights.py` extraction logic rather than adding a second parser. At finalization, extract unique skill names from the turn's stored/in-memory messages, persist them in `skills_loaded`, and update sidecar outcome counters under the existing file lock.

- [ ] Step 4: Extend the open record shape with `outcome_counts`, `helped`, `hurt`, `neutral`, `outcome_cost_usd`, and `last_outcome_at`. Unknown fields must survive older readers and older records must be seeded lazily.

- [ ] Step 5: Add `get_skill_utility` with the formula and minimum sample threshold from the data contract. Return counts and cost as well as the score so Curator and Insights can show evidence.

- [ ] Step 6: Add Insights sections `turn_outcomes`, `reflection_triggers`, and `skill_utility` behind the existing report generation path. Existing callers that do not request learning details retain their current output shape.

- [ ] Step 7: Run sidecar, ledger, and Insights tests with a temporary home to exercise file locking and unknown-field preservation.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/tools/test_skill_usage.py tests/agent/test_turn_ledger.py tests/agent/test_insights.py -q
```

- [ ] Step 8: Commit attribution and utility evidence.

```bash
git add agent/turn_ledger.py tools/skill_usage.py agent/insights.py tests/tools/test_skill_usage.py tests/agent/test_turn_ledger.py tests/agent/test_insights.py
 git diff --cached --check
git commit -m "feat(learning): attribute turn outcomes to skills"
```

## Task 3: Add Failure, Correction, and Reaction Triggers

**Files:**
- Create: `agent/reflection_triggers.py`
- Modify: `agent/reactions.py`
- Modify: `agent/turn_finalizer.py`
- Modify: `agent/codex_runtime.py`
- Modify: `agent/background_review.py`
- Modify: `gateway/platforms/base.py`
- Modify: `plugins/platforms/telegram/adapter.py`
- Modify: `plugins/platforms/discord/adapter.py`
- Modify: `plugins/platforms/slack/adapter.py`
- Modify: `plugins/platforms/feishu/adapter.py`
- Modify: `plugins/platforms/photon/adapter.py`
- Test: `tests/agent/test_reflection_triggers.py`
- Test: existing platform reaction tests

**Interfaces:**
- Consumes: `TurnOutcomeRecord`, existing background-review spawn, adapter identity/auth checks, and existing reaction callbacks.
- Produces: `ReflectionTrigger` values `failure`, `correction`, `tool_failure_streak`, and `reaction`; `should_trigger_review(context) -> bool`; `record_feedback_event(...) -> bool`.

- [ ] Step 1: Add pure trigger tests before wiring adapters.

```python
def test_failed_turn_triggers_review_once():
    trigger = evaluate_reflection_triggers(outcome="failed", user_text="", tool_results=[])
    assert trigger.kind == "failure"
    assert evaluate_reflection_triggers(outcome="failed", user_text="", tool_results=[]).dedupe_key == trigger.dedupe_key


def test_correction_detector_requires_correction_language():
    assert detect_user_correction("No, I meant the staging branch") is True
    assert detect_user_correction("No results yet") is False


def test_three_consecutive_tool_failures_trigger_review():
    results = [{"error": "x"}, {"error": "y"}, {"error": "z"}]
    assert evaluate_reflection_triggers(outcome="partial", user_text="", tool_results=results).kind == "tool_failure_streak"
```

- [ ] Step 2: Run the pure tests and confirm the new module is absent.

```bash
python -m pytest tests/agent/test_reflection_triggers.py -q
```

- [ ] Step 3: Implement lexical correction detection with a small, case-insensitive pattern set (`no,`, `i meant`, `that is wrong`, `wrong file`, `please undo`, `not what i asked`). Exclude ordinary negation and short acknowledgements. Keep the detector pure and return the matched kind for later telemetry.

- [ ] Step 4: Implement per-session cooldown and single-flight deduplication using the existing background-review runtime state. A failed turn can trigger at most one review in the configured cooldown window; a correction/reaction may annotate the row even when a review is already running.

- [ ] Step 5: Replace the verified-only trigger gate in both finalizer paths with `should_trigger_review`. Keep interval nudges as a fallback when no signal fires; signals must not cause more than one concurrent review.

- [ ] Step 6: Add a failure-specific review prompt that explicitly says to distinguish environment-dependent failures, transient provider errors, negative tool claims, and actual reusable lessons. The prompt may inspect but must not write a skill that says a tool is unavailable based on one failure.

- [ ] Step 7: Normalize inbound feedback.
  - Add a non-abstract base callback that accepts `(platform, conversation_id, message_id, actor_id, reaction, event_id)`.
  - Telegram and Discord publish supported reaction events after allowed-user checks and self-event filtering.
  - Slack turns its existing `reaction_added` no-op into the callback.
  - Feishu and Photon mark synthetic text feedback as already annotated so it is not recorded twice.
  - Matrix approval reactions remain approval-only unless they are explicitly outside a pending approval identity.

- [ ] Step 8: Add duplicate/auth tests.

```python
def test_reaction_feedback_requires_allowed_actor_and_dedupes_event(tmp_path):
    first = record_feedback_event("telegram", "chat", "m1", "user", "thumbs_down", "evt-1")
    second = record_feedback_event("telegram", "chat", "m1", "user", "thumbs_down", "evt-1")
    assert first is True
    assert second is False


def test_bot_reaction_does_not_trigger_reflection():
    assert normalize_reaction_event(actor_is_bot=True, reaction="thumbs_down") is None
```

- [ ] Step 9: Run trigger and platform-focused tests.

```bash
python -m pytest \
  tests/agent/test_reflection_triggers.py \
  tests/gateway/test_matrix_approval_reaction_fail_closed.py \
  tests/gateway/test_telegram_approval_buttons.py \
  tests/gateway/test_slack_approval_buttons.py -q
```

- [ ] Step 10: Commit signal-driven reflection.

```bash
git add agent/reflection_triggers.py agent/reactions.py agent/turn_finalizer.py agent/codex_runtime.py agent/background_review.py gateway/platforms/base.py plugins/platforms/telegram/adapter.py plugins/platforms/discord/adapter.py plugins/platforms/slack/adapter.py plugins/platforms/feishu/adapter.py plugins/platforms/photon/adapter.py tests/agent/test_reflection_triggers.py tests/gateway
git diff --cached --check
git commit -m "feat(learning): trigger reflection from turn signals"
```

## Task 4: Feed Utility Into Curator and Next-Session Skill Ranking

**Files:**
- Modify: `agent/curator.py`
- Modify: `agent/prompt_builder.py`
- Modify: `agent/insights.py`
- Modify: `hermes_cli/subcommands/insights.py`
- Modify: `hermes_cli/config.py`
- Test: `tests/agent/test_prompt_builder.py`
- Test: `tests/agent/test_curator_classification.py`
- Test: `tests/agent/test_insights.py`

**Interfaces:**
- Consumes: sidecar utility records and ledger query methods.
- Produces: session-stable utility-ranked skill index and Curator candidate evidence.

- [ ] Step 1: Add a prompt-builder test that proves ordering is deterministic within a session and changes only after a new snapshot is built.

```python
def test_skill_index_uses_utility_then_relevance_and_is_snapshot_stable(tmp_path):
    records = {"rare": {"utility": 0.95, "samples": 8}, "common": {"utility": 0.55, "samples": 8}}
    first = build_skills_system_prompt(skills, utility_records=records, session_snapshot=True)
    second = build_skills_system_prompt(skills, utility_records={"rare": {"utility": 0.1, "samples": 8}, "common": {"utility": 0.9, "samples": 8}}, session_snapshot=True)
    assert first == second
```

- [ ] Step 2: Run the prompt-builder and Curator tests to establish the alphabetical/current-evidence baseline.

```bash
python -m pytest tests/agent/test_prompt_builder.py tests/agent/test_curator_classification.py -q
```

- [ ] Step 3: Add `skills.utility_ranking.enabled`, `min_samples`, and `utility_weight` config defaults. Load the utility sidecar once when the system-prompt snapshot is built; do not stat/read it on every API call.

- [ ] Step 4: Rank eligible skills by `utility_weight * utility + (1 - utility_weight) * lexical_relevance`. Keep pinned/protected and mandatory skills present. Skills below the minimum sample count retain their existing alphabetical/relevance order.

- [ ] Step 5: Add utility counts and caveats to the Curator candidate table/prompt. Curator decisions must still use staleness, provenance, pinned state, package integrity, and explicit `absorbed_into`; utility can recommend review but cannot authorize deletion by itself.

- [ ] Step 6: Extend Insights and CLI formatting with per-skill sample/helped/hurt/utility and trigger counts. Keep a compact default report and a verbose learning section, matching existing output options.

- [ ] Step 7: Run the prompt/cache, Curator, and Insights tests with a temp home.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_prompt_builder.py \
  tests/agent/test_curator_classification.py \
  tests/agent/test_insights.py -q
```

- [ ] Step 8: Commit utility consumption.

```bash
git add agent/curator.py agent/prompt_builder.py agent/insights.py hermes_cli/subcommands/insights.py hermes_cli/config.py tests/agent/test_prompt_builder.py tests/agent/test_curator_classification.py tests/agent/test_insights.py
git diff --cached --check
git commit -m "feat(learning): consume skill utility evidence"
```

## Task 5: End-to-End Ledger and Feedback Verification

**Files:**
- Test: new `tests/agent/test_turn_ledger_e2e.py`
- Test: new `tests/gateway/test_reflection_feedback_e2e.py`
- Modify: `docs/` or website documentation for the user-facing insights command only if a current command reference exists.

- [ ] Step 1: Exercise a real temporary profile through a successful turn and a failed/blocked turn using the existing fake provider fixtures. Assert two rows exist, deltas are non-negative, the correct skill names are stored, and the final response is unaffected by a forced ledger write exception.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_turn_ledger_e2e.py \
  tests/gateway/test_reflection_feedback_e2e.py -q
```

- [ ] Step 2: Exercise duplicate reaction delivery from a real adapter event fixture. Assert one ledger annotation, one reflection trigger, and no second synthetic user message.

- [ ] Step 3: Exercise a session restart. Open the same temporary profile in a new `SessionDB` instance and assert trends/utility are available without rebuilding the ledger.

- [ ] Step 4: Run the full focused gate and static checks.

```bash
python -m pytest \
  tests/agent/test_turn_outcome.py \
  tests/agent/test_turn_ledger.py \
  tests/agent/test_turn_ledger_e2e.py \
  tests/agent/test_reflection_triggers.py \
  tests/agent/test_turn_finalizer_memory_gating.py \
  tests/tools/test_skill_usage.py \
  tests/agent/test_prompt_builder.py \
  tests/agent/test_curator_classification.py \
  tests/agent/test_insights.py \
  tests/gateway/test_reflection_feedback_e2e.py -q
python3 -m compileall -q agent/turn_ledger.py agent/reflection_triggers.py
 git diff --check
```

- [ ] Step 5: Commit the evidence tests.

```bash
git add tests/agent/test_turn_ledger_e2e.py tests/gateway/test_reflection_feedback_e2e.py
git diff --cached --check
git commit -m "test(learning): verify outcome ledger feedback loop"
```

## Acceptance Checklist

- [ ] Every classified turn writes one row through the chat and Codex finalizer paths.
- [ ] Ledger failures are logged and invisible to the user turn result.
- [ ] Cost/token/tool/skill context round-trips through SQLite under a temporary profile.
- [ ] Failed, blocked, unresolved, correction, streak, and reaction signals trigger at most one cooldowned review.
- [ ] Reaction events are authenticated, self-filtered, and deduplicated.
- [ ] Skill utility uses smoothing/minimum samples and remains evidence rather than deletion policy.
- [ ] Curator and Insights consume the evidence without changing existing default output unexpectedly.
- [ ] Skill ordering is session-stable and does not mutate the frozen system prompt mid-turn.
- [ ] Existing interval nudges remain as a fallback; the plan does not delete a working learning path.

## Deliberate Simplifications

- Skipped a new event bus; ledger annotations and existing adapter callbacks are enough for the first consumer.
- Skipped an LLM correction classifier; lexical signals are cheap, inspectable, and less likely to poison the loop.
- Skipped automatic skill archival on low utility; add only after controlled evaluation shows the correlation is predictive rather than task-difficulty bias.
- Skipped a provider-neutral telemetry export; first make the local learning loop consume the signal it already computes.
