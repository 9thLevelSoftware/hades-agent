# Proactive Attention Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Hermes profile observe only explicitly authorized event sources, normalize and deduplicate their events, make an auditable bounded-attention decision, and—only after a successful no-action shadow proof—surface useful items or execute reversible pre-authorized actions without disturbing conversation caches.

**Architecture:** Add a shared, profile-local `agent.events` contract for canonical envelopes and authorized ingress, then build `agent.attention` as a deterministic policy/store/runtime over that contract. The broker consumes item #6's canonical authority and budget provider, sends item #1 mission updates, and delegates every effectful action to item #2's transaction coordinator; direct delivery is reserved for budget-authorized review/digest/interrupt decisions and never becomes a synthetic chat turn. Existing gateway, webhook, cron, goal, workflow, and reaction seams publish into the broker, while source-specific connectors remain existing adapters, service-gated integrations, or standalone plugins.

**Tech Stack:** Python 3.13, frozen dataclasses/enums, canonical JSON/SHA-256, SQLite/WAL through `SessionDB`, existing `agent.autonomy`, `agent.effects`, mission/workflow/cron/gateway services, `agent.auxiliary_client.call_llm`, Rich/classic CLI, Ink/TypeScript JSON-RPC TUI, React Dashboard, pytest through `scripts/run_tests.sh`, Vitest, and versioned YAML benchmark fixtures.

## Global Constraints

- Portfolio items #1 (missions), #2 (transactions), and #6 (autonomy) are prerequisites. This plan consumes their mission, effect, authority, approval, budget, evidence, and truthful-status contracts; it does not create local substitutes.
- The system prompt, cached prefix, effective model-tool definition snapshot, primary provider, and primary model remain byte-stable for a conversation. Event intake never appends a synthetic user message, mutates history, swaps tools, or wakes an existing agent turn.
- Delivery is Footprint Ladder rung 1 for the shared gateway broker and rung 2 for its CLI + skill. There is no new model-visible core tool. New vendor/source connectors remain service-gated, standalone plugins, or MCP servers.
- Proactivity is explicit opt-in. `attention.mode` defaults to `off`; `shadow` records recommendations but performs no mission update, workflow start, transaction, digest, notification, interrupt, or other outward action.
- Profiles are independent islands. Every config, database, cursor, hash key, audit row, inbox item, label, benchmark artifact, and source subscription resolves from `get_hermes_home()`; no live default-profile inheritance or cross-profile dedupe is allowed.
- Stable, non-secret settings and explicit subscriptions live under `attention:` and `autonomy:` in `config.yaml`. Credentials remain in existing adapter secret fields, secret providers, or `.env`; event/audit/runtime state lives in profile-local SQLite.
- Source identity, native event identity, sensitivity, canonical entity links, dedupe keys, authority context, and causality are produced by trusted local normalizers. Event text, webhook JSON, page content, message text, model output, and remote metadata cannot assert those fields.
- Cheap deterministic/local filters run before any model. Optional model ranking is bounded, off by default, authority-checked as remote/local inference, budgeted, and used only for uncertain cases. Failure, timeout, malformed output, or unavailable provider never becomes interrupt/action.
- The broker's complete disposition vocabulary is `ignore`, `update_mission`, `preauthorized_action`, `review`, `digest`, and `interrupt`. Shadow mode stores the recommendation but forces the effective disposition to `shadow_inbox` or `suppressed`.
- Item #6 owns interruption, action, and ranking budgets and the `allow|ask|deny` decision. Item #2 owns action preview, commit, current-authority recheck, certainty, compensation, and receipt linkage through item #12's canonical evidence store. Item #12 alone owns receipt records and verification vocabulary. Item #1 owns durable mission outcomes/execution state. Attention owns only event grouping and the choice of whether/when to route.
- No pre-authorized action executes unless item #2 reports current `eligible_exact` or an explicitly allowed `eligible_compensation`, the action is authorized at preview and commit, and the adapter proves it is reversible for this exact state. `unknown_effect`, irreversible, stale, unverified, or unsupported actions become review items.
- User-visible truth remains exact: deduplicated is not exactly-once; authorized is not completed; `verified`, `completed_unverified`, `unknown_effect`, `reversible`, `compensatable`, and `irreversible` retain the shared portfolio meanings.
- Audit payloads contain hashes, bounded redacted summaries, reason codes, and evidence references—not secrets, raw webhook bodies, raw message bodies, full file contents, access tokens, cookies, or model prompts. Deleting a subscription can purge derived summaries while retaining non-sensitive accounting hashes.
- Initial proof sources are exactly the approved corpus sources: cron/time, filesystem and local Git state, an inbound generic/GitHub-style webhook, and one already-configured gateway channel feeding the terminal attention inbox. Calendar, email, commerce, and always-on sensors remain excluded without separate opt-in.
- CLI/terminal and native Ink TUI are primary. Dashboard is a secondary inbox/inspection surface. The Electron Desktop app is not modified and is not a delivery dependency.
- Every state, security, config, resolution, and remote-I/O path receives a real-import E2E test against a temporary `HERMES_HOME`. Mocks are limited to clocks, the optional model/network boundary, platform delivery, and explicit crash injection.
- The no-action gate is immutable: at least 10 active eight-hour days, at least 500 normalized events, at least 100 surfaced candidates all labeled, and at least 100 stratified suppressed events audited. Usefulness precision must be at least 85%, premature would-interrupts must average fewer than 1 per active eight-hour day, audited high-value misses must be at most 5%, dedupe must prevent every duplicate surface/action, authority violations must be zero, and the review trail must be complete before reversible-only rollout.

---

## Approved Portfolio Contract and Ownership Boundary

**Layman outcome:** Hermes watches only authorized event sources, acts when safely useful, batches routine noise, and interrupts the user only when the expected benefit justifies the interruption.

**Design boundary:** Missions own outcomes and execution state; attention chooses ignore/batch/review/action/interrupt. Autonomy owns permission and budgets; transactions own effects and reversal; workflows own graph execution; gateway adapters own admission/authentication and platform delivery. The broker does not become a second scheduler, workflow engine, approval manager, receipt store, or chat surface.

**90-day proof:** Run at least two real authorized sources in no-action shadow mode for at least ten preregistered active eight-hour windows. Cover at least 500 normalized events and 100 surfaced candidates; label every surfaced candidate `useful`, `premature`, or `not_useful`; audit at least 100 stratified non-duplicate suppressed events for high-value misses. Pass only with usefulness precision `useful / (useful + premature + not_useful) >= 0.85`, fewer than one premature would-interrupt per active window, high-value misses `<= 0.05`, reliable exact and cross-channel dedupe, zero action outside current authority, and a complete review trail.

**Dependencies and failure conditions:** Items #1, #2, #6, and #12 own mission state, effect certainty, authority/budgets, and receipt truth. Stop or roll back on an unauthorized action or interruption, a high-value miss above the frozen floor, duplicate delivery/action, unverifiable decision history, cross-profile data, prompt/tool-schema drift, or any ambiguous outward effect that is retried instead of remaining `unknown_effect`.

**Delivery:** Footprint Ladder rung 1/2—extend existing event, mission, transaction, gateway, CLI, and Ink seams and add an operator skill; optional source adapters remain service-gated plugins. No new model-visible core tool, scheduler, workflow engine, approval manager, receipt store, or Desktop dependency is introduced.

**Post-proof rollout:** Advance from `shadow` to `notify_only` only by explicit guarded config apply. `notify_only` may populate review/digests and deliver authority/budget-approved interrupts but cannot update missions or execute actions. Advance to `reversible_actions` only after a second explicit apply; this mode permits mission updates and item #2 transactions whose exact live eligibility is reversible. Any irreversible/unknown action remains review-only.

## Current-Code Audit and File Map

### Existing seams retained

- `gateway/platforms/base.py::MessageEvent` and `SessionSource` already normalize platform messages, debounce bursts, merge pending events, and publish authorized reaction feedback. The event contract extends beside them rather than changing chat semantics.
- `gateway/platforms/webhook.py::_handle_webhook()` already authenticates before processing, bounds bodies, filters routes, rate-limits, and deduplicates delivery IDs in memory. Durable event dedupe is added after those gates and before agent/direct delivery.
- `gateway/run.py` already wires adapters, workflow dispatcher ticks, goal continuations, completion delivery, and profile-scoped session identity. It constructs one attention runtime per active profile without injecting events into conversations.
- `cron/scheduler.py::tick()` and `run_one_job()` have due/dispatch/completion boundaries; `hermes_cli/workflows_dispatcher.py::tick()` and workflow events have queued/started/waiting/succeeded/failed boundaries.
- `hermes_cli/workflows_capabilities.py` declares `webhook` but excludes it from `IMPLEMENTED_TRIGGER_TYPES`; `hermes_cli/workflows_db.py` already owns workflow definitions, executions, schedules, input feeds, and idempotent write transactions.
- `hermes_cli/goals.py::GoalContract` and `GoalState`, plus `gateway/run.py::_post_turn_goal_continuation()`, expose goal status and verdict changes. A goal remains context, not authority.
- `agent.auxiliary_client.call_llm(task=...)` is the existing bounded side-model path. Attention adds an auxiliary task config, not a primary model swap.
- `gateway/delivery.py::DeliveryRouter` and `DeliveryTarget` own outbound delivery and acknowledgements. The broker records delivery identity/outcome and does not claim exactly-once.
- Item #6's planned `agent.autonomy` exports `AuthorityProvider`, `StoredAuthorityProvider`, `AuthorityDecision`, `ActionContext`, and `authorize_effect()`. This plan adds generic usage-budget reservation to that same package.
- Item #2's planned `agent.effects` exports `TransactionCoordinator`, transaction eligibility, adapters, and shared receipt integration. This plan adds one Hermes-state workflow-trigger adapter there rather than creating an attention effect engine.
- Item #1's planned `hermes_cli/missions_db.py` owns append-only mission events and review items. Attention stores only the mission link and its routing decision.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `cli.py`, `tui_gateway/server.py`, and `ui-tui/src/app/slash/commands/ops.ts` provide shared CLI/classic/Ink routing.
- `hermes_cli/web_server.py` and `web/src/App.tsx` provide profile-scoped secondary Dashboard pages without depending on Desktop.

### New production files

- `agent/events/__init__.py` — stable public exports and `EVENT_ENVELOPE_SCHEMA_ID`.
- `agent/events/models.py` — frozen source, entity, time, dedupe, sensitivity, causality, content-reference, and `EventEnvelope` records.
- `agent/events/canonical.py` — normalization, bounds, canonical JSON, trusted hashing, and envelope/event identity.
- `agent/events/registry.py` — local `EventNormalizer` and source registration with duplicate/version validation.
- `agent/events/subscriptions.py` — compile explicit config subscriptions and exact source matching.
- `agent/events/sources/filesystem_git.py` — bounded polling normalizer for authorized roots/repos.
- `agent/attention/__init__.py` — stable broker exports and policy version.
- `agent/attention/models.py` — ingestion, decision, score, label, inbox, digest, execution, and proof records.
- `agent/attention/store.py` — typed `SessionDB` persistence, dedupe/grouping, audit, inbox, labels, delivery/action links, and proof queries.
- `agent/attention/policy.py` — deterministic cheap filters, fixed-point scoring, opportunity-window and disposition selection.
- `agent/attention/ranker.py` — optional bounded `AttentionRanker` protocol and auxiliary-model implementation.
- `agent/attention/service.py` — subscription resolution, normalization/ingest, evaluation, label/audit/export/delete services.
- `agent/attention/executor.py` — idempotent mission/action/review/digest/interrupt execution with mode and authority rechecks.
- `agent/attention/runtime.py` — async queue, bounded workers, source health/poll ticks, startup recovery, and shutdown drain.
- `gateway/attention_bridge.py` — gateway `MessageEvent`, reaction, webhook, goal, cron, and workflow event adapters.
- `hermes_cli/attention.py` — shared top-level/classic command parser and bounded renderers.
- `skills/attention-broker/SKILL.md` — terminal-first source, inbox, labeling, proof, and rollout guidance.
- `benchmarks/attention/manifest.yaml` — frozen windows, sources, strata, metrics, floors, baselines, exclusions, and cost source.
- `benchmarks/attention/events.schema.json` — import/export validation for labeled proof events without raw content.
- `benchmarks/attention/run.py` — shadow-window recorder and baseline/candidate runner.
- `benchmarks/attention/score.py` — denominators, Wilson intervals, precision, misses, interruption rate, dedupe, cost, and stop-gate scorer.
- `benchmarks/attention/README.md` — preregistration and exact local operation.
- `website/docs/user-guide/features/proactive-attention-broker.md` — operator/user guide.
- `website/docs/developer-guide/event-envelope-attention.md` — envelope, normalizer, policy, and source SDK contract.
- `web/src/pages/AttentionPage.tsx` — secondary Dashboard inbox/proof/audit page.

### Existing production files modified

- `hermes_state.py` — additive attention tables and a `SessionDB.attention` facade; no schema-version snapshot test.
- `hermes_cli/config.py` — bounded `attention` defaults, `auxiliary.attention_ranker`, and guarded section apply.
- `agent/autonomy/models.py`, `agent/autonomy/store.py`, `agent/autonomy/service.py`, `agent/autonomy/__init__.py` — named usage budgets owned by autonomy.
- `agent/effects/adapters/hermes_state.py` — reversible `hermes.workflow.trigger.v1` adapter over owner-module APIs.
- `hermes_cli/missions_db.py` — idempotent attention event/review append helpers, preserving mission ownership.
- `hermes_cli/workflows_spec.py`, `hermes_cli/workflows_capabilities.py`, `hermes_cli/workflows_db.py`, `hermes_cli/workflows_dispatcher.py` — durable webhook trigger matching/start and event-sink publication.
- `cron/scheduler.py` — optional event sink at due/start/finish boundaries, with unchanged behavior when absent.
- `gateway/platforms/base.py` — additive reaction-listener composition while preserving the built-in reflection handler.
- `gateway/platforms/webhook.py` — publish authenticated/filtered/idempotent webhook candidates and support explicit attention/workflow bindings.
- `gateway/run.py` — create/close profile-local attention runtimes and publish admitted channel, goal, cron, and workflow events.
- `gateway/delivery.py` — accept a stable broker delivery id through the existing journal path; no schema change.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `cli.py` — `attention` command registration and shared dispatch.
- `tui_gateway/server.py`, `ui-tui/src/gatewayTypes.ts`, `ui-tui/src/app/slash/commands/ops.ts`, `ui-tui/src/__tests__/slashParity.test.ts` — native attention RPC/rendering/parity.
- `hermes_cli/web_server.py`, `web/src/lib/api.ts`, `web/src/App.tsx` — secondary profile-scoped APIs/page.

### Tests created or extended

- `tests/agent/events/test_models.py`
- `tests/agent/events/test_subscriptions.py`
- `tests/agent/events/test_filesystem_git_source.py`
- `tests/agent/attention/test_store.py`
- `tests/agent/attention/test_policy.py`
- `tests/agent/attention/test_ranker.py`
- `tests/agent/attention/test_service.py`
- `tests/agent/attention/test_executor.py`
- `tests/agent/attention/test_runtime.py`
- `tests/agent/attention/test_security.py`
- `tests/agent/attention/test_e2e.py`
- `tests/agent/autonomy/test_usage_budgets.py`
- `tests/gateway/test_attention_bridge.py`
- `tests/gateway/test_attention_reactions.py`
- `tests/gateway/test_webhook_attention.py`
- `tests/cron/test_attention_events.py`
- `tests/hermes_cli/test_workflow_webhook_trigger.py`
- `tests/hermes_cli/test_attention.py`
- `tests/tui_gateway/test_attention_rpc.py`
- `tests/benchmarks/test_attention_benchmark.py`
- `ui-tui/src/__tests__/attentionCommand.test.ts`
- `web/src/pages/AttentionPage.test.tsx`

---

### Task 0: Preregister the No-Action Shadow Contract Before Production Code

**Files:**
- Create: `benchmarks/attention/manifest.yaml`
- Create: `benchmarks/attention/events.schema.json`
- Create: `tests/benchmarks/test_attention_benchmark.py`

**Interfaces:**
- Produces benchmark version `attention-shadow-v1`, exact active-window/source/case definitions, immutable denominators, labels, metrics, stop conditions, baseline, and rollout gate.
- Consumes the approved portfolio corpus and no production broker code.

- [ ] **Step 1: Write RED manifest-contract tests**

```python
def test_manifest_freezes_no_action_gate(load_attention_manifest):
    m = load_attention_manifest()
    assert m["version"] == "attention-shadow-v1"
    assert m["mode"] == "shadow"
    assert m["active_window_hours"] == 8
    assert m["minimum_active_windows"] == 10
    assert m["minimum_normalized_events"] == 500
    assert m["minimum_surfaced_candidates"] == 100
    assert m["minimum_suppressed_audit"] == 100
    assert m["gates"] == {
        "usefulness_precision_min": 0.85,
        "premature_interruptions_per_window_max_exclusive": 1.0,
        "high_value_miss_rate_max": 0.05,
        "unauthorized_actions_max": 0,
        "duplicate_surfaces_or_actions_max": 0,
        "incomplete_audit_rows_max": 0,
    }


def test_manifest_requires_two_real_sources_and_all_approved_source_kinds(load_attention_manifest):
    m = load_attention_manifest()
    assert m["minimum_simultaneously_healthy_real_sources"] == 2
    assert set(m["eligible_source_kinds"]) == {
        "cron_time", "filesystem_git", "webhook", "gateway_channel"
    }
    assert set(m["excluded_source_kinds"]) == {
        "calendar", "email", "commerce", "always_on_sensor"
    }
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_attention_benchmark.py -q`

Expected: FAIL because the manifest and event schema do not exist.

- [ ] **Step 3: Write the frozen manifest and event schema**

`manifest.yaml` defines an active window as one preregistered contiguous eight-hour interval in which at least two authorized real sources report health for at least 95% of scheduled health checks. Windows may not overlap. A source outage stays in the report; it disqualifies that window rather than shrinking the denominator. The first proof chooses two or more from the four eligible kinds before collection and records connector IDs as profile-local hashes.

Freeze these measurement definitions:

```yaml
version: attention-shadow-v1
mode: shadow
active_window_hours: 8
minimum_active_windows: 10
minimum_simultaneously_healthy_real_sources: 2
source_health_fraction_min: 0.95
minimum_normalized_events: 500
minimum_surfaced_candidates: 100
minimum_suppressed_audit: 100
surfaced_labels: [useful, premature, not_useful]
suppressed_labels: [high_value_miss, not_high_value]
eligible_source_kinds: [cron_time, filesystem_git, webhook, gateway_channel]
excluded_source_kinds: [calendar, email, commerce, always_on_sensor]
suppressed_strata: [source_kind, event_kind, sensitivity, score_decile, opportunity_window]
gates:
  usefulness_precision_min: 0.85
  premature_interruptions_per_window_max_exclusive: 1.0
  high_value_miss_rate_max: 0.05
  unauthorized_actions_max: 0
  duplicate_surfaces_or_actions_max: 0
  incomplete_audit_rows_max: 0
baseline: current_hermes_webhook_cron_channel_behavior
confidence_interval: wilson_95
cost_source: session_and_auxiliary_request_ledgers
```

The JSON schema requires event/decision hashes, source kind, event kind, occurred/observed times, score components, recommended/effective disposition, budget/authority decision IDs, dedupe disposition, opportunity-window bounds, label/labeler timestamp, and content-free evidence references. It rejects raw payload/body/message/content/token/cookie fields and unknown properties.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_attention_benchmark.py -q`

Expected: PASS; the preregistered gate cannot silently lower samples or broaden sources.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/attention/manifest.yaml benchmarks/attention/events.schema.json tests/benchmarks/test_attention_benchmark.py
git commit -m "test: preregister attention broker shadow proof"
```

---

### Task 1: Define the Canonical Event Envelope and Trusted Normalizer Registry

**Files:**
- Create: `agent/events/__init__.py`
- Create: `agent/events/models.py`
- Create: `agent/events/canonical.py`
- Create: `agent/events/registry.py`
- Create: `tests/agent/events/test_models.py`

**Interfaces:**
- Produces `EventEnvelope`, `SourceIdentity`, `EntityLink`, `EventTime`, `EventDedupe`, `Sensitivity`, `Causality`, `ContentRef`, `EventCandidate`, `EventNormalizer`, `EventNormalizerRegistry`, `canonicalize_envelope()`, and `derive_event_id()`.
- Consumed by every later source, store, policy, mission/workflow bridge, and audit task.

- [ ] **Step 1: Write RED envelope identity, bounds, and trust tests**

```python
def test_event_identity_is_stable_across_receive_time_and_json_order(normalize):
    a = normalize(candidate(native_id="delivery-7", observed_at_ms=100, payload={"b": 2, "a": 1}))
    b = normalize(candidate(native_id="delivery-7", observed_at_ms=200, payload={"a": 1, "b": 2}))
    assert a.event_id == b.event_id
    assert a.dedupe.exact_key_hash == b.dedupe.exact_key_hash
    assert a.observed_at_ms != b.observed_at_ms


def test_untrusted_payload_cannot_assert_identity_sensitivity_or_causality(normalize):
    envelope = normalize(candidate(payload={
        "sensitivity": "public", "dedupe_key": "victim", "entity_id": "admin",
        "causal_predecessor": "evt-trusted", "profile": "other",
    }))
    assert envelope.sensitivity.data_class == "unknown"
    assert envelope.dedupe.exact_key_hash != sha256_text("victim")
    assert envelope.causality.predecessor_event_id is None
    assert envelope.profile_scope_hash != sha256_text("other")


def test_cross_channel_semantic_key_requires_locally_mapped_entity(registry):
    left = registry.normalize(gateway_candidate(channel="slack", remote_entity="INC-7"))
    right = registry.normalize(gateway_candidate(channel="discord", remote_entity="INC-7"))
    assert left.dedupe.semantic_key_hash is None
    registry.install_entity_mapping("incident", {"slack:INC-7": "incident:7", "discord:INC-7": "incident:7"})
    assert registry.normalize(gateway_candidate(channel="slack", remote_entity="INC-7")).dedupe.semantic_key_hash == \
           registry.normalize(gateway_candidate(channel="discord", remote_entity="INC-7")).dedupe.semantic_key_hash
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/events/test_models.py -q`

Expected: FAIL importing `agent.events`.

- [ ] **Step 3: Implement the exact frozen records**

```python
EVENT_ENVELOPE_SCHEMA_ID = "hermes.event.v1"

@dataclass(frozen=True)
class SourceIdentity:
    kind: Literal["cron_time", "filesystem_git", "webhook", "gateway_channel", "goal", "workflow", "mission"]
    connector_id: str
    account_hash: str
    scope_hash: str
    native_event_id_hash: str | None

@dataclass(frozen=True)
class EntityLink:
    kind: str
    entity_hash: str
    relation: Literal["subject", "actor", "container", "target"]

@dataclass(frozen=True)
class EventTime:
    observed_at_ms: int
    occurred_at_ms: int | None
    effective_from_ms: int | None
    expires_at_ms: int | None
    timezone: str
    precision: Literal["millisecond", "second", "minute", "day", "unknown"]

@dataclass(frozen=True)
class EventDedupe:
    exact_key_hash: str
    semantic_key_hash: str | None
    group_key_hash: str
    semantic_window_ms: int

@dataclass(frozen=True)
class Sensitivity:
    data_class: DataClass
    classifier: str
    confidence_ppm: int

@dataclass(frozen=True)
class Causality:
    predecessor_event_id: str | None
    root_event_id: str | None
    correlation_hash: str | None

@dataclass(frozen=True)
class ContentRef:
    owner: Literal["gateway", "webhook", "cron", "filesystem", "git", "workflow", "goal", "mission"]
    opaque_ref: str
    content_hash: str

@dataclass(frozen=True)
class EventEnvelope:
    schema_id: str
    event_id: str
    profile_scope_hash: str
    subscription_id: str
    source: SourceIdentity
    event_kind: str
    entities: tuple[EntityLink, ...]
    time: EventTime
    dedupe: EventDedupe
    sensitivity: Sensitivity
    causality: Causality
    summary: str
    features: tuple[tuple[str, int | str | bool], ...]
    content_ref: ContentRef | None
    envelope_hash: str
```

Use UTF-8 NFC, lowercase dotted kinds, sorted tuples, integer milliseconds/fixed-point ppm, canonical JSON with sorted keys and compact separators, and SHA-256. Bound summary to 2,048 UTF-8 bytes, features to 64 entries/8 KiB, entities to 32, kinds/IDs to 128 bytes, semantic windows to 1 second–30 days, and reject unknown fields. `derive_event_id()` hashes schema/source/native identity/event kind/entity/state identity but excludes receive time. If no native ID exists, the trusted normalizer must provide a stable state fingerprint and occurrence bucket; it may not fall back to random/time-only identity.

- [ ] **Step 4: Implement a non-model registry**

```python
class EventNormalizer(Protocol):
    normalizer_id: str
    version: int
    source_kind: str
    def normalize(self, candidate: EventCandidate, subscription: CompiledSubscription) -> EventEnvelope: ...


class EventNormalizerRegistry:
    def register(self, normalizer: EventNormalizer) -> None: ...
    def normalize(self, candidate: EventCandidate, subscription: CompiledSubscription) -> EventEnvelope: ...
    def snapshot(self) -> tuple[tuple[str, int, str], ...]: ...
```

Registration rejects duplicate `(normalizer_id, version)`, unknown source kinds, remote functions, and invalid bounds. Registry metadata is never added to `tools/registry.py` or model schemas. Entity mappings are explicit local subscription data; remote strings alone cannot create a cross-channel semantic key.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/events/test_models.py tests/test_get_tool_definitions_cache_isolation.py -q`

Expected: PASS; identities are stable, untrusted fields cannot expand trust, and model-tool definitions are unchanged.

- [ ] **Step 6: Commit**

```bash
git add agent/events tests/agent/events/test_models.py
git commit -m "feat: define canonical event envelopes"
```

---

### Task 2: Compile Authorized Source Subscriptions and Extend Autonomy-Owned Usage Budgets

**Files:**
- Create: `agent/events/subscriptions.py`
- Modify: `hermes_cli/config.py`
- Modify: `agent/autonomy/models.py`
- Modify: `agent/autonomy/store.py`
- Modify: `agent/autonomy/service.py`
- Modify: `agent/autonomy/__init__.py`
- Modify: `hermes_state.py`
- Create: `tests/agent/events/test_subscriptions.py`
- Create: `tests/agent/autonomy/test_usage_budgets.py`

**Interfaces:**
- Produces `SourceSubscription`, `CompiledSubscription`, `SubscriptionCompiler`, `resolve_subscription(candidate)`, `UsageRequest`, `UsageReservation`, and `AuthorityProvider.reserve_usage()` / `.settle_usage()` / `.release_usage()`.
- Consumes profile-local config, existing adapter admission facts, item #6 contracts, and profile-keyed hashing; later policy/executor tasks consume compiled subscriptions and reservations.

- [ ] **Step 1: Write RED source authorization and budget tests**

```python
def test_unsubscribed_or_wrong_scope_event_never_normalizes(compiler):
    compiler.load([subscription(source_kind="webhook", connector_id="alerts", scopes=["repo:a"])])
    assert compiler.resolve(webhook_candidate(connector="alerts", scope="repo:b")) is None
    assert compiler.resolve(webhook_candidate(connector="other", scope="repo:a")) is None


def test_subscription_expiry_and_profile_are_fail_closed(two_profiles):
    default, named = two_profiles
    default.add_subscription(subscription(subscription_id="s1", expires_at_ms=100))
    assert default.resolve(candidate_at(99)) is not None
    assert default.resolve(candidate_at(101)) is None
    assert named.resolve(candidate_at(99)) is None


def test_interrupt_budget_reservation_is_atomic_and_replay_safe(authority, race):
    authority.set_usage_budget("attention.interrupt", limit=1, window_seconds=8 * 3600)
    results = race(lambda: authority.reserve_usage(UsageRequest(
        budget_class="attention.interrupt", units=1, operation_key="evt-1"
    )), workers=2)
    assert sum(r.allowed for r in results) == 1
    assert authority.usage("attention.interrupt") == 1
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/events/test_subscriptions.py tests/agent/autonomy/test_usage_budgets.py -q`

Expected: FAIL because subscription compilation and named usage budgets do not exist.

- [ ] **Step 3: Add exact stable config defaults and subscription schema**

Add defaults without materializing them into user config:

```python
"attention": {
    "mode": "off",  # off | shadow | notify_only | reversible_actions
    "subscriptions": [],
    "queue_max": 1000,
    "workers": 2,
    "retention_days": 90,
    "semantic_dedupe_window_seconds": 300,
    "digest_interval_seconds": 14400,
    "quiet_hours": {"timezone": "UTC", "start": "22:00", "end": "07:00"},
    "ranker": {"enabled": False, "uncertain_min_ppm": 400000,
               "uncertain_max_ppm": 800000, "max_batch": 16,
               "max_input_bytes": 32768, "max_output_tokens": 512,
               "timeout_seconds": 15},
},
"auxiliary": {
    # preserve all existing entries
    "attention_ranker": {"provider": "auto", "model": "", "base_url": "",
                         "api_key": "", "timeout": 15, "extra_body": {},
                         "reasoning_effort": "none"},
},
```

Each subscription has `subscription_id`, `source_kind`, `connector_id`, exact `account`, `scopes`, allowed `event_kinds`, trusted entity mappings, maximum event rate, default sensitivity, optional expiry, mode (`mirror` or `broker_only`), optional mission/workflow binding, and delivery targets. Reject wildcard account/scope for webhook/channel, overlapping IDs, unknown keys, credentials, cross-profile paths, relative filesystem roots, primary/main worktree action bindings, expiry in the past, and workflow bindings without an exact trigger ID.

- [ ] **Step 4: Implement generic usage budgets in item #6 authority**

```python
UsageBudgetClass = Literal[
    "attention.surface", "attention.digest", "attention.interrupt",
    "attention.action", "attention.model_rank"
]

@dataclass(frozen=True)
class UsageRequest:
    budget_class: UsageBudgetClass
    units: int
    operation_key: str
    window_started_at_ms: int | None = None

@dataclass(frozen=True)
class UsageReservation:
    reservation_id: str
    budget_class: UsageBudgetClass
    units: int
    decision_id: str
    state: Literal["reserved", "settled", "released"]
```

Append an `autonomy_usage_ledger` table, owned and accessed only through `AutonomyStore`:

```sql
CREATE TABLE IF NOT EXISTS autonomy_usage_ledger (
    reservation_id TEXT PRIMARY KEY,
    budget_class TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('reserve','settle','release')),
    units INTEGER NOT NULL CHECK (units > 0),
    window_started_at_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (budget_class, operation_key, kind)
);
CREATE INDEX IF NOT EXISTS idx_autonomy_usage_window
ON autonomy_usage_ledger(budget_class, window_started_at_ms, created_at_ms);
```

`AuthorityProvider.reserve_usage()` first authorizes the matching action class and then atomically checks/reserves the configured count window. It never changes `allow|ask|deny`; exhausted/unknown budgets return deny. Settlement/release is idempotent. Attention cannot write this table directly.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/events/test_subscriptions.py tests/agent/autonomy/test_usage_budgets.py tests/agent/autonomy -q`

Expected: PASS; only exact active subscriptions resolve, budget races grant at most one reservation, and profiles remain isolated.

- [ ] **Step 6: Commit**

```bash
git add agent/events/subscriptions.py hermes_cli/config.py agent/autonomy hermes_state.py tests/agent/events/test_subscriptions.py tests/agent/autonomy/test_usage_budgets.py
git commit -m "feat: authorize attention sources and budgets"
```

---

### Task 3: Persist Envelopes, Cross-Channel Dedupe, Decisions, Inbox, Labels, and Complete Audit

**Files:**
- Create: `agent/attention/__init__.py`
- Create: `agent/attention/models.py`
- Create: `agent/attention/store.py`
- Modify: `hermes_state.py`
- Create: `tests/agent/attention/test_store.py`

**Interfaces:**
- Produces `AttentionStore.ingest()`, `.record_decision()`, `.upsert_inbox()`, `.label_candidate()`, `.record_execution()`, `.record_delivery()`, `.select_suppressed_audit_sample()`, `.proof_snapshot()`, and immutable records `IngestResult`, `AttentionDecision`, `InboxItem`, `AttentionLabel`, `ExecutionRecord`.
- Consumes canonical envelopes and returns durable idempotent identities used by policy, executor, CLI, and benchmark tasks.

- [ ] **Step 1: Write RED crash/replay/dedupe/audit tests**

```python
def test_exact_retry_records_attempt_but_one_logical_event(store, envelope):
    first = store.ingest(envelope, attempt_id="a1")
    second = store.ingest(envelope, attempt_id="a2")
    assert first.event_id == second.event_id
    assert store.count_events() == 1
    assert store.count_ingest_attempts() == 2
    assert second.dedupe_disposition == "exact_duplicate"


def test_cross_channel_duplicate_groups_once_and_never_surfaces_twice(store):
    slack, discord = same_semantic_event_different_channels()
    assert store.ingest(slack, attempt_id="a1").dedupe_disposition == "new"
    duplicate = store.ingest(discord, attempt_id="a2")
    assert duplicate.dedupe_disposition == "semantic_duplicate"
    assert duplicate.duplicate_of_event_id == slack.event_id
    store.upsert_inbox(decision_for(slack, "interrupt"))
    store.upsert_inbox(decision_for(discord, "interrupt"))
    assert store.count_inbox_items() == 1


def test_every_authorized_event_has_a_terminal_audit_decision_after_recovery(store):
    store.ingest(envelope(), attempt_id="a1")
    assert store.audit_gaps() == [envelope().event_id]
    store.recover_unresolved(default_code="recovered_to_review")
    assert store.audit_gaps() == []
    assert store.get_decision(envelope().event_id).recommended_disposition == "review"
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/attention/test_store.py -q`

Expected: FAIL importing `agent.attention.store`.

- [ ] **Step 3: Add the declarative SQL schema**

Append additive tables to `SCHEMA_SQL` without a literal-version snapshot assertion:

```sql
CREATE TABLE IF NOT EXISTS attention_events (
    event_id TEXT PRIMARY KEY,
    envelope_hash TEXT NOT NULL UNIQUE,
    subscription_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_identity_hash TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    exact_dedupe_key TEXT NOT NULL,
    semantic_dedupe_key TEXT,
    group_key TEXT NOT NULL,
    occurred_at_ms INTEGER,
    observed_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER,
    sensitivity TEXT NOT NULL,
    summary_redacted TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    duplicate_of_event_id TEXT REFERENCES attention_events(event_id),
    dedupe_disposition TEXT NOT NULL CHECK (dedupe_disposition IN ('new','exact_duplicate','semantic_duplicate','causal_update')),
    created_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attention_exact_dedupe
ON attention_events(subscription_id, exact_dedupe_key);
CREATE INDEX IF NOT EXISTS idx_attention_semantic_dedupe
ON attention_events(semantic_dedupe_key, occurred_at_ms);
CREATE INDEX IF NOT EXISTS idx_attention_group
ON attention_events(group_key, observed_at_ms);

CREATE TABLE IF NOT EXISTS attention_ingest_attempts (
    attempt_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES attention_events(event_id),
    connector_id TEXT NOT NULL,
    received_at_ms INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    detail_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attention_decisions (
    decision_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE REFERENCES attention_events(event_id),
    policy_version TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    deterministic_score_ppm INTEGER NOT NULL,
    rank_score_ppm INTEGER,
    score_components_json TEXT NOT NULL,
    recommended_disposition TEXT NOT NULL,
    effective_disposition TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    opportunity_start_ms INTEGER,
    opportunity_end_ms INTEGER,
    authority_decision_id TEXT,
    budget_reservation_ids_json TEXT NOT NULL,
    ranker_request_hash TEXT,
    ranker_response_hash TEXT,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS attention_inbox (
    inbox_id TEXT PRIMARY KEY,
    group_key TEXT NOT NULL UNIQUE,
    representative_event_id TEXT NOT NULL REFERENCES attention_events(event_id),
    decision_id TEXT NOT NULL REFERENCES attention_decisions(decision_id),
    state TEXT NOT NULL CHECK (state IN ('shadow','review','digest','interrupt','dismissed','resolved')),
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at_ms INTEGER NOT NULL,
    last_seen_at_ms INTEGER NOT NULL,
    resolved_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS attention_labels (
    label_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES attention_events(event_id),
    sample_kind TEXT NOT NULL CHECK (sample_kind IN ('surfaced','suppressed_audit')),
    label TEXT NOT NULL CHECK (label IN ('useful','premature','not_useful','high_value_miss','not_high_value')),
    labeler_hash TEXT NOT NULL,
    evidence_ref TEXT,
    created_at_ms INTEGER NOT NULL,
    UNIQUE(event_id, sample_kind)
);

CREATE TABLE IF NOT EXISTS attention_executions (
    execution_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE REFERENCES attention_decisions(decision_id),
    kind TEXT NOT NULL,
    mission_id TEXT,
    transaction_id TEXT,
    workflow_execution_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('not_executed_shadow','pending','completed','completed_unverified','blocked','failed','unknown_effect')),
    result_hash TEXT,
    receipt_id TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS attention_deliveries (
    delivery_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES attention_decisions(decision_id),
    group_key TEXT NOT NULL,
    delivery_kind TEXT NOT NULL CHECK (delivery_kind IN ('review','digest','interrupt')),
    target_set_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    acknowledgement_hash TEXT,
    created_at_ms INTEGER NOT NULL,
    UNIQUE(group_key, delivery_kind, target_set_hash)
);

CREATE TABLE IF NOT EXISTS attention_source_health (
    health_id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    checked_at_ms INTEGER NOT NULL,
    healthy INTEGER NOT NULL CHECK (healthy IN (0,1)),
    detail_hash TEXT NOT NULL
);
```

- [ ] **Step 4: Implement atomic ingestion and conservative dedupe**

Exact retries insert only a new ingest-attempt row pointing to the existing logical event. Semantic dedupe queries the same profile, canonical entity mapping, event kind/state, and bounded occurrence window; it may group events but never merges envelope provenance or discards the second audit attempt. A causal update links to its predecessor and updates the inbox representative only when the trusted normalizer supplied a valid predecessor/root. Higher-sensitivity events never inherit lower sensitivity, lower urgency, or an ignore result from a duplicate.

`record_decision()` is insert-once by event ID and validates that every field hashes correctly. `recover_unresolved()` deterministically records review for any persisted authorized event lacking a decision after a crash. Labels are immutable except an explicit relabel command that appends an audit event and replaces through revision CAS; raw content never enters labels.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/attention/test_store.py tests/test_hermes_state.py -q`

Expected: PASS; reopen/replay preserves one logical event, one decision, one inbox group, complete audit, and no duplicate surface.

- [ ] **Step 6: Commit**

```bash
git add agent/attention/models.py agent/attention/store.py agent/attention/__init__.py hermes_state.py tests/agent/attention/test_store.py
git commit -m "feat: persist attention decisions and audit"
```

---

### Task 4: Implement Deterministic Filters, Opportunity Windows, and Optional Bounded Ranking

**Files:**
- Create: `agent/attention/policy.py`
- Create: `agent/attention/ranker.py`
- Create: `tests/agent/attention/test_policy.py`
- Create: `tests/agent/attention/test_ranker.py`

**Interfaces:**
- Produces `AttentionPolicy.evaluate(envelope, context) -> DecisionDraft`, `ScoreComponents`, `OpportunityWindow`, `AttentionRanker.rank()`, `DisabledRanker`, and `AuxiliaryModelRanker`.
- Consumes envelopes, compiled subscription, mission/goal/workflow summary facts, quiet hours, current authority preview, and budget availability; performs no action.

- [ ] **Step 1: Write RED deterministic-order and ranker-bound tests**

```python
@pytest.mark.parametrize(("case", "disposition", "code"), [
    ("exact_duplicate", "ignore", "duplicate_exact"),
    ("expired", "ignore", "opportunity_expired"),
    ("routine_quiet_hours", "digest", "quiet_hours_batch"),
    ("mission_deadline_risk", "update_mission", "mission_risk"),
    ("authorized_reversible_fix", "preauthorized_action", "safe_action_candidate"),
    ("high_value_time_sensitive", "interrupt", "benefit_exceeds_interruption"),
    ("unknown_sensitive", "review", "sensitive_uncertain"),
])
def test_policy_has_stable_conservative_dispositions(policy, case, disposition, code):
    decision = policy.evaluate(fixture(case), context(case))
    assert decision.recommended_disposition == disposition
    assert code in decision.reason_codes


def test_input_order_does_not_change_score_or_disposition(policy):
    a = policy.evaluate(envelope(entities=(entity("b"), entity("a"))), shuffled_context(1))
    b = policy.evaluate(envelope(entities=(entity("a"), entity("b"))), shuffled_context(2))
    assert (a.score_ppm, a.recommended_disposition, a.policy_hash) == \
           (b.score_ppm, b.recommended_disposition, b.policy_hash)


def test_ranker_is_called_only_for_uncertain_bounded_redacted_batch(ranker_harness):
    ranker_harness.evaluate(scores=[399999, 400000, 800000, 800001], payload_bytes=100000)
    request = ranker_harness.requests.single()
    assert request.event_count == 2
    assert request.event_count <= 16
    assert request.utf8_bytes <= 32768
    assert "raw_body" not in request.serialized
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/attention/test_policy.py tests/agent/attention/test_ranker.py -q`

Expected: FAIL because policy and ranker modules do not exist.

- [ ] **Step 3: Implement fixed-point local decision order**

Evaluate in this exact order:

1. validate subscription/current source health and event expiry;
2. classify exact/semantic duplicate and causal update without trusting remote keys;
3. apply explicit local ignore/group rules and source rate bounds;
4. calculate opportunity window from effective/expiry/deadline facts;
5. evaluate sensitivity, mission relevance, state change magnitude, deadline proximity, novelty, recoverability, user-authored priority, quiet hours, and current attention saturation;
6. preview item #6 authority and named budget availability without consuming;
7. compute integer `benefit_ppm`, `urgency_ppm`, `relevance_ppm`, `confidence_ppm`, `interruption_cost_ppm`, and `action_risk_ppm` with versioned weights;
8. send only uncertain non-sensitive-authorized cases to the optional ranker;
9. select one disposition with deny/review overriding action/interrupt and quiet-hours batching routine items;
10. persist all components/reason codes, including why no ranker ran.

An interrupt requires a live opportunity window, explicit `attention.interrupt` allow, available interruption budget, benefit minus interruption cost at least 200,000 ppm, and confidence at least 800,000 ppm. Action requires exact source/action binding, `attention.action` allow, available action budget, and item #2 preview eligibility; otherwise review. A mission update requires an exact bound mission and active mission; otherwise review. Missing/unknown sensitivity, entity, time, authority, or audit availability never becomes action/interrupt.

- [ ] **Step 4: Implement the optional auxiliary ranker**

`DisabledRanker` returns no score. `AuxiliaryModelRanker` calls:

```python
call_llm(
    task="attention_ranker",
    messages=[{"role": "user", "content": canonical_redacted_batch_json}],
    max_tokens=512,
    temperature=0,
    timeout=15,
)
```

Before remote routing, authorize `model.route` for the exact provider/data classes and reserve `attention.model_rank` usage. The prompt contains only event ID, trusted feature integers, bounded redacted summary, opportunity bounds, and allowed output schema. Require one JSON object per input with `event_id`, `benefit_ppm`, `urgency_ppm`, `confidence_ppm`, and a reason code from a fixed allowlist. Reject extra/missing IDs, prose, floats, out-of-range values, duplicate IDs, and output over 32 KiB. Provider failure releases the reservation and returns deterministic review/digest fallback; it never silently ignores an event.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/attention/test_policy.py tests/agent/attention/test_ranker.py tests/agent/autonomy/test_usage_budgets.py -q`

Expected: PASS; cheap filters precede model use, outputs are deterministic/bounded, and every model failure is conservative and audited.

- [ ] **Step 6: Commit**

```bash
git add agent/attention/policy.py agent/attention/ranker.py tests/agent/attention/test_policy.py tests/agent/attention/test_ranker.py
git commit -m "feat: rank attention with bounded local policy"
```

---

### Task 5: Build the Broker Service and Idempotent Decision Executor

**Files:**
- Create: `agent/attention/service.py`
- Create: `agent/attention/executor.py`
- Modify: `hermes_cli/missions_db.py`
- Modify: `agent/effects/adapters/hermes_state.py`
- Modify: `gateway/delivery.py`
- Create: `tests/agent/attention/test_service.py`
- Create: `tests/agent/attention/test_executor.py`

**Interfaces:**
- Produces `AttentionService.ingest_candidate()`, `.evaluate_event()`, `.label()`, `.audit_suppressed()`, `.proof_snapshot()`, `AttentionExecutor.execute(decision_id)`, and `MissionEventSink.append_attention_event()`.
- Consumes registry/subscriptions/store/policy/ranker, `StoredAuthorityProvider`, autonomy usage budgets, item #1 mission DB, item #2 `TransactionCoordinator`, shared receipt/eligibility states, workflow owner service, and `DeliveryRouter`.

- [ ] **Step 1: Write RED mode/authority/action/delivery tests**

```python
@pytest.mark.parametrize("recommended", [
    "update_mission", "preauthorized_action", "review", "digest", "interrupt"
])
def test_shadow_mode_performs_no_action(recommended, executor):
    result = executor.execute(decision(recommended, mode="shadow"))
    assert result.status == "not_executed_shadow"
    assert executor.mission_calls == executor.transaction_calls == executor.delivery_calls == 0
    assert executor.store.get_inbox_for(result.event_id).state == "shadow"


def test_reversible_action_rechecks_authority_and_transaction_eligibility(executor):
    executor.preview_authority("allow", version=4)
    executor.commit_authority("deny", version=5)
    result = executor.execute(decision("preauthorized_action", mode="reversible_actions"))
    assert result.status == "blocked"
    assert result.code == "authority_changed"
    assert executor.effect_calls == 0


def test_cross_channel_duplicate_has_one_interrupt_delivery(executor):
    first, second = duplicate_decisions_same_group()
    executor.execute(first)
    executor.execute(second)
    assert executor.delivery_calls == 1
    assert executor.store.delivery_count(group_key=first.group_key) == 1
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/attention/test_service.py tests/agent/attention/test_executor.py -q`

Expected: FAIL importing service/executor.

- [ ] **Step 3: Implement ingest/evaluate as one durable pipeline**

`ingest_candidate()` resolves an exact active subscription before normalization. It writes the ingest attempt/event, evaluates local policy, optionally ranks, rechecks preview authority, records one decision, and only then enqueues execution. A crash after event insert is recovered to `review`; a crash after decision insert replays the same decision ID. Queue saturation records `review / broker_queue_full` synchronously instead of dropping the event.

Mode mapping is exact:

| Mode | Effective behavior |
|---|---|
| `off` | Do not normalize/store; increment only a local non-persisted disabled counter. Existing source behavior is unchanged. |
| `shadow` | Persist every authorized event/decision; put recommended review/digest/interrupt/action/mission candidates in the shadow inbox; execute nothing. |
| `notify_only` | Persist all; allow review inbox, digest, and interrupt after fresh authority/budget checks; mission/action recommendations become review. |
| `reversible_actions` | Add mission updates and transaction-owned exact reversible actions; all other effects remain review. |

- [ ] **Step 4: Implement decision execution without chat turns**

- `ignore`: record completed execution with no inbox/delivery.
- `review`: reserve `attention.surface`, upsert one group inbox item, and settle the reservation.
- `digest`: reserve `attention.digest`, add the group to the current digest, and deliver one deterministic bounded digest at the configured interval through `DeliveryRouter`.
- `interrupt`: reload authority for `ActionContext(action_class="attention.interrupt")`, reserve the interrupt budget, render a deterministic message from the redacted summary/opportunity/reason, and call `DeliveryRouter.deliver()` with stable ID `attn:<group-key>:interrupt:<target-set-hash>`. Do not invoke an agent or append to a session.
- `update_mission`: verify exact active mission binding, append an idempotent mission event keyed by decision ID through `missions_db.append_attention_event()`, and let the mission owner decide later execution. Do not change mission verdict directly.
- `preauthorized_action`: reload authority, reserve `attention.action`, create/load the exact item #2 transaction from the subscription's frozen action intent, require dynamic `eligible_exact` (or explicitly permitted `eligible_compensation`), preview, commit, and store transaction/receipt IDs. Any stale authority, changed resource, ambiguity, missing receipt, unknown effect, or non-reversible eligibility creates review and releases/settles budget truthfully.

`append_attention_event()` stores only event/decision IDs, group key, bounded reason codes, and content reference; duplicate decision IDs create one mission event. It cannot change authority or mission terminal state.

- [ ] **Step 5: Add the workflow-trigger transaction adapter**

Register `hermes.workflow.trigger.v1` in item #2's existing Hermes-state adapter module. `prepare()` resolves the exact enabled workflow version/trigger, validates the event input against trigger intake, proves the workflow's mutating nodes all use transaction-supported reversible adapters, and previews execution creation. `commit()` calls the owner-module `start_event_execution()` with event ID as idempotency key. `reconcile()` queries by `(workflow_id, trigger_id, event_id)`. Compensation may cancel only before a mutating node commits; otherwise eligibility becomes blocked. The adapter never treats workflow completion as proof of action success; shared receipts retain effect evidence.

- [ ] **Step 6: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/attention/test_service.py tests/agent/attention/test_executor.py tests/agent/effects tests/hermes_cli/test_missions_db.py tests/gateway/test_delivery.py -q`

Expected: PASS; shadow makes zero calls, duplicate decisions execute once, and only freshly authorized reversible transactions can commit.

- [ ] **Step 7: Commit**

```bash
git add agent/attention/service.py agent/attention/executor.py hermes_cli/missions_db.py agent/effects/adapters/hermes_state.py gateway/delivery.py tests/agent/attention/test_service.py tests/agent/attention/test_executor.py
git commit -m "feat: execute bounded attention decisions"
```

---

### Task 6: Wire Gateway, Cron, Goal, Workflow, Filesystem/Git, and Reaction Ingress

**Files:**
- Create: `agent/events/sources/filesystem_git.py`
- Create: `agent/attention/runtime.py`
- Create: `gateway/attention_bridge.py`
- Modify: `gateway/platforms/base.py`
- Modify: `gateway/run.py`
- Modify: `cron/scheduler.py`
- Modify: `hermes_cli/workflows_dispatcher.py`
- Create: `tests/agent/events/test_filesystem_git_source.py`
- Create: `tests/agent/attention/test_runtime.py`
- Create: `tests/gateway/test_attention_bridge.py`
- Create: `tests/gateway/test_attention_reactions.py`
- Create: `tests/cron/test_attention_events.py`

**Interfaces:**
- Produces `AttentionRuntime.start()/stop()/publish()/poll_once()/recover()`, `GatewayAttentionBridge`, `FilesystemGitSource.poll()`, optional `event_sink(EventCandidate)` parameters for cron/workflow dispatch, and additive `BasePlatformAdapter.add_feedback_handler()`.
- Consumes `AttentionService/Executor`, admitted `MessageEvent`/`SessionSource`, existing scheduler/dispatcher lifecycle facts, and exact profile scope.

- [ ] **Step 1: Write RED real-source and listener-composition tests**

```python
def test_channel_event_is_mirrored_only_after_adapter_admission(gateway_harness):
    gateway_harness.receive(user="unauthorized", text="urgent")
    assert gateway_harness.attention_candidates == []
    gateway_harness.receive(user="authorized", text="build failed")
    assert len(gateway_harness.attention_candidates) == 1
    assert gateway_harness.normal_agent_calls == 1  # mirror does not steal chat


def test_cron_and_workflow_publish_status_without_changing_existing_results(runtime):
    assert runtime.run_cron_job() is True
    assert [e.event_kind for e in runtime.events] == ["cron.started", "cron.completed"]
    assert runtime.tick_workflow() == 1
    assert "workflow.succeeded" in [e.event_kind for e in runtime.events]


def test_reaction_labels_attention_and_preserves_reflection_handler(adapter, broker):
    adapter.add_feedback_handler(broker.on_reaction)
    adapter.publish_feedback("slack", "c1", "m1", "u1", "👍", "reaction-1")
    assert adapter.reflection_feedback_count == 1
    assert broker.labels == [("m1", "useful")]
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/events/test_filesystem_git_source.py tests/agent/attention/test_runtime.py tests/gateway/test_attention_bridge.py tests/gateway/test_attention_reactions.py tests/cron/test_attention_events.py -q`

Expected: FAIL because runtime/bridge/source/listener APIs do not exist.

- [ ] **Step 3: Implement bounded runtime and startup recovery**

One `AttentionRuntime` owns a bounded `asyncio.Queue` and two workers per profile. `publish()` never blocks adapter receive loops; it enqueues or synchronously records `broker_queue_full -> review`. `recover()` fills missing decisions, resumes pending delivery/action records only through idempotent executor calls, and never retries `unknown_effect`. `stop()` stops pollers, drains for at most 10 seconds, converts remaining candidates to review, and closes SQLite handles.

Gateway construction keys runtimes by resolved profile home and closes them on profile/gateway shutdown. Multiplexed messages resolve the source profile before broker lookup. Disabled profiles create no broker runtime and no persistent counters.

- [ ] **Step 4: Publish admitted platform and lifecycle events**

`GatewayAttentionBridge.from_message()` runs after existing adapter authorization/admission and command/control handling. `mirror` subscriptions preserve the normal message path; `broker_only` is allowed only for bot/webhook-style source accounts explicitly marked in config and returns an accepted status without starting an agent. The bridge derives hashes from `SessionSource.platform`, account/user, `scope_id`, chat/thread, message ID, and trusted adapter metadata; raw text becomes a bounded redacted summary/content hash only.

Pass optional sinks into cron/workflow code:

```python
def tick(*, adapters=None, loop=None, event_sink: Callable[[EventCandidate], None] | None = None, **kwargs): ...

def workflow_tick(*, limit: int = 10, event_sink: Callable[[EventCandidate], None] | None = None) -> int: ...
```

Emit cron due/started/completed/failed and workflow queued/started/waiting/succeeded/failed with stable job/execution IDs and causal predecessors. The sink is invoked only after the owner state transition commits; sink failure is logged and does not roll back the owner. Manual CLI ticks construct the broker sink only when attention is enabled.

At goal judge/state transitions, `gateway/run.py` publishes `goal.continue|wait|done|blocked` after the goal state save. Goal text is a content reference, not an authority source. At mission event append, the item #1 owner may publish a causal mission event without recursively routing it back to the same mission; a correlation-root guard stops loops.

- [ ] **Step 5: Implement bounded filesystem/Git polling**

`FilesystemGitSource` accepts only subscription-resolved absolute canonical roots, a maximum 32 roots, 100,000 entries per poll, 1 GiB aggregate metadata scan, and intervals 5 seconds–24 hours. It records file path hashes, stat/hash deltas for explicitly matched files, Git HEAD/index/worktree status hashes using argument-array subprocess calls, and cursor state in attention storage. It never reads file bodies for ranking, follows no symlink outside the root, runs no hooks, fetches no remotes, and makes no Git mutation. First poll establishes a baseline unless `emit_initial=true` was explicitly configured.

- [ ] **Step 6: Compose reaction labeling**

Replace the single replaceable feedback callback internally with an ordered listener list seeded by `_record_feedback`; retain `set_feedback_handler()` as a backward-compatible reset for tests/plugins and add `add_feedback_handler()`. Attention maps configured positive reaction to `useful`, configured negative to `not_useful`, and configured early/annoyed reaction to `premature` only when actor authorization and platform-message-to-inbox identity both resolve. Reaction event IDs make relabel replay idempotent.

- [ ] **Step 7: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/events/test_filesystem_git_source.py tests/agent/attention/test_runtime.py tests/gateway/test_attention_bridge.py tests/gateway/test_attention_reactions.py tests/cron/test_attention_events.py tests/gateway/test_goal_verdict_send.py tests/hermes_cli/test_workflows_dispatcher.py -q`

Expected: PASS; owner behavior is unchanged without a sink, authorized events publish after commit, reactions preserve reflection, and runtime recovery never duplicates effects.

- [ ] **Step 8: Commit**

```bash
git add agent/events/sources/filesystem_git.py agent/attention/runtime.py gateway/attention_bridge.py gateway/platforms/base.py gateway/run.py cron/scheduler.py hermes_cli/workflows_dispatcher.py tests/agent/events/test_filesystem_git_source.py tests/agent/attention/test_runtime.py tests/gateway/test_attention_bridge.py tests/gateway/test_attention_reactions.py tests/cron/test_attention_events.py
git commit -m "feat: ingest authorized attention events"
```

---

### Task 7: Complete Authenticated Webhook Ingress and Durable Workflow Triggers

**Files:**
- Modify: `gateway/platforms/webhook.py`
- Modify: `hermes_cli/workflows_spec.py`
- Modify: `hermes_cli/workflows_capabilities.py`
- Modify: `hermes_cli/workflows_db.py`
- Modify: `hermes_cli/workflows_dispatcher.py`
- Create: `tests/gateway/test_webhook_attention.py`
- Create: `tests/hermes_cli/test_workflow_webhook_trigger.py`

**Interfaces:**
- Produces `workflows_db.start_event_execution(conn, workflow_id, trigger_id, *, envelope, idempotency_key, now)`, durable webhook-trigger idempotency, and explicit route keys `attention_subscription_id`, `workflow_id`, and `workflow_trigger_id`.
- Consumes webhook auth/filter/rate/idempotency gates, `GatewayAttentionBridge`, workflow intake validation, and Task 5's transaction adapter.

- [ ] **Step 1: Write RED auth/order/replay/workflow tests**

```python
async def test_webhook_publishes_only_after_auth_filter_and_delivery_dedupe(webhook):
    assert (await webhook.post(signature="bad", delivery="d1")).status == 401
    assert webhook.attention_calls == 0
    assert (await webhook.post(signature="good", delivery="d1", event="ignored")).json()["status"] == "ignored"
    assert webhook.attention_calls == 0
    assert (await webhook.post(signature="good", delivery="d2", event="push")).status == 202
    assert webhook.attention_calls == 1
    assert (await webhook.post(signature="good", delivery="d2", event="push")).json()["status"] == "duplicate"
    assert webhook.attention_calls == 1


def test_event_execution_is_durable_idempotent_and_intake_validated(workflow_db, envelope):
    first = start_event_execution(workflow_db, "wf-1", "webhook-main", envelope=envelope,
                                  idempotency_key=envelope.event_id, now=100)
    second = start_event_execution(workflow_db, "wf-1", "webhook-main", envelope=envelope,
                                   idempotency_key=envelope.event_id, now=101)
    assert first.execution_id == second.execution_id
    assert first.trigger_type == "webhook"
    assert workflow_db.execution_count() == 1
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/gateway/test_webhook_attention.py tests/hermes_cli/test_workflow_webhook_trigger.py -q`

Expected: FAIL because webhook broker binding and event execution do not exist.

- [ ] **Step 3: Publish webhook candidates at the safe boundary**

After signature validation, allowed-event filter, route filter/script, bounded prompt transformation, and existing delivery-ID acceptance—but before direct delivery or agent dispatch—build a trusted candidate from route name, event kind, delivery ID, request timestamp, local entity mappings, and a content reference/hash. Never allow payload keys to select profile, subscription, workflow, sensitivity, target, dedupe key, or authority.

If no `attention_subscription_id` is configured, existing webhook behavior is byte-for-byte unchanged. If configured in `shadow`, publish and continue existing behavior only when the subscription mode is `mirror`; `broker_only` returns the broker's durable event ID/status. In `notify_only|reversible_actions`, a workflow binding becomes a broker `preauthorized_action` recommendation; it never starts directly inside the HTTP handler. Return 202 after durable event/decision write, 200 for exact duplicate, 403 for disabled/unsubscribed binding, and 503 when audit persistence is unavailable. Do not acknowledge an unaudited accepted event.

- [ ] **Step 4: Complete workflow webhook trigger ownership**

Set `IMPLEMENTED_TRIGGER_TYPES = {"manual", "schedule", "webhook"}` while preserving `kanban_event` as unsupported. Correct `workflow_capabilities()` so `implemented` is derived from `IMPLEMENTED_TRIGGER_TYPES`, not the declared set. Validate webhook trigger `path`, exact trigger ID, intake schema, optional event-kind allowlist, and subscription binding at publish/open time.

Add an idempotency table to `workflows.db` schema:

```sql
CREATE TABLE IF NOT EXISTS workflow_event_triggers (
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    trigger_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    execution_id TEXT NOT NULL UNIQUE REFERENCES workflow_executions(execution_id),
    envelope_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, version, trigger_id, event_id)
);
```

`start_event_execution()` resolves the current immutable workflow version, exact enabled webhook trigger, materializes only declared input fields from trusted envelope features/content reference, evaluates intake criteria, and creates execution + trigger row in one `write_txn`. Replays return the original execution. It cannot be called from the network handler except through Task 5's transaction adapter.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/gateway/test_webhook_attention.py tests/hermes_cli/test_workflow_webhook_trigger.py tests/gateway/test_webhook_adapter.py tests/hermes_cli/test_workflows_capabilities.py tests/hermes_cli/test_workflows_e2e.py -q`

Expected: PASS; unauthenticated/filtered/duplicate events do not dispatch, workflow event starts are idempotent, and unsupported triggers still fail visibly.

- [ ] **Step 6: Commit**

```bash
git add gateway/platforms/webhook.py hermes_cli/workflows_spec.py hermes_cli/workflows_capabilities.py hermes_cli/workflows_db.py hermes_cli/workflows_dispatcher.py tests/gateway/test_webhook_attention.py tests/hermes_cli/test_workflow_webhook_trigger.py
git commit -m "feat: complete webhook workflow triggers"
```

---

### Task 8: Deliver the Terminal-First Attention CLI and Operating Skill

**Files:**
- Create: `hermes_cli/attention.py`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/main.py`
- Modify: `cli.py`
- Create: `skills/attention-broker/SKILL.md`
- Create: `tests/hermes_cli/test_attention.py`

**Interfaces:**
- Produces `build_parser(parent_subparsers)`, `attention_command(args)`, and `run_argv(argv, *, output_mode="text")` shared by top-level/classic/TUI.
- Consumes only `AttentionService`, config guarded-apply, and autonomy explanation routes; it never accesses SQLite/YAML directly.

- [ ] **Step 1: Write RED grammar, label, proof, and apply tests**

```python
def test_source_add_previews_and_requires_exact_hash(cli):
    preview = cli.run("source add --file webhook-source.yaml")
    assert preview.exit_code == 0
    assert preview.json["applied"] is False
    assert preview.json["before_config_hash"]
    stale = cli.run("source add --file webhook-source.yaml --apply --expected-config-hash bad")
    assert stale.exit_code == 2


def test_shadow_candidate_label_and_suppressed_audit_are_explicit(cli):
    assert cli.run("inbox label evt-1 useful --evidence-ref note:1").exit_code == 0
    assert cli.run("audit-suppressed sample --count 100 --seed proof-v1").json["count"] == 100
    assert cli.run("audit-suppressed label evt-2 high_value_miss --evidence-ref note:2").exit_code == 0


def test_reversible_rollout_refuses_incomplete_proof(cli):
    result = cli.run("mode set reversible_actions --apply --expected-config-hash current")
    assert result.exit_code == 3
    assert "shadow proof has not passed" in result.output
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_attention.py -q`

Expected: FAIL importing `hermes_cli.attention`.

- [ ] **Step 3: Implement the exact command grammar**

```text
hermes attention status [--json]
hermes attention mode show|set <off|shadow|notify_only|reversible_actions> [--apply --expected-config-hash HASH]
hermes attention source list|show <id> [--json]
hermes attention source add --file SOURCE.yaml [--apply --expected-config-hash HASH]
hermes attention source edit <id> --file SOURCE.yaml [--apply --expected-config-hash HASH]
hermes attention source remove <id> [--purge-derived] [--apply --expected-config-hash HASH]
hermes attention source test <id> --file EVENT.json [--json]
hermes attention inbox list [--state STATE] [--source SOURCE] [--since ISO8601] [--limit 200] [--json]
hermes attention inbox show <event-or-inbox-id> [--json]
hermes attention inbox label <event-id> <useful|premature|not_useful> --evidence-ref REF
hermes attention inbox dismiss|resolve <inbox-id>
hermes attention audit [--event EVENT] [--decision DISPOSITION] [--since ISO8601] [--limit 200] [--json]
hermes attention audit-suppressed sample --count N --seed TEXT [--json]
hermes attention audit-suppressed label <event-id> <high_value_miss|not_high_value> --evidence-ref REF
hermes attention digest preview|run [--json]
hermes attention budget show [--json]
hermes attention proof status|export --output PATH [--json]
hermes attention replay <event-id> --policy-current [--json]
hermes attention doctor [--json]
```

Source/event files are UTF-8 YAML/JSON capped at 1 MiB with duplicate keys/aliases/unknown fields rejected. Inbox/audit limits are 1–500; suppressed sample count is 1–10,000 and deterministic by seed. `source test` normalizes/evaluates without persisting or acting and labels all payload text untrusted. `replay` writes a linked comparison decision but never changes or executes the original.

Add `CommandDef("attention", "Inspect and govern proactive event attention", "Configuration", aliases=("attn",), args_hint="[status|mode|source|inbox|audit|digest|budget|proof|doctor]")`. Top-level, classic slash, and TUI call the same parser/service. Mode apply checks legal progression and proof state; `off` rollback is always allowed. Exit codes are 0 success/preview, 2 validation/stale hash, 3 authority/proof/rollout block, and 4 storage/runtime failure.

- [ ] **Step 4: Write the complete operating skill**

The skill contains one full webhook source YAML, one filesystem/Git source, one gateway-channel mirror, one cron binding, and item #6 rules/budgets for surface/digest/interrupt/action/rank. It mandates source test, `shadow`, inbox labeling, stratified suppressed audit, proof inspection, then explicit `notify_only` and `reversible_actions` applies. It explains exact dispositions, quiet hours, opportunity windows, dedupe, reaction labels, mission/workflow binding, unknown-effect review, rollback/purge, profile isolation, and why source text/model scores never grant authority. It forbids calendar/email/commerce/sensors in the first proof, raw secrets in source files, direct DB edits, action before proof, irreversible actions, and claims of exactly-once or verification without shared evidence.

- [ ] **Step 5: Run GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_attention.py tests/hermes_cli/test_commands.py -q`

Expected: PASS; all mutation paths preview/apply by hash, labels are explicit, and proof gates cannot be bypassed.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/attention.py hermes_cli/commands.py hermes_cli/main.py cli.py skills/attention-broker/SKILL.md tests/hermes_cli/test_attention.py
git commit -m "feat: add attention broker cli controls"
```

---

### Task 9: Route Attention Controls Natively in the Ink TUI

**Files:**
- Modify: `tui_gateway/server.py`
- Modify: `ui-tui/src/gatewayTypes.ts`
- Modify: `ui-tui/src/app/slash/commands/ops.ts`
- Modify: `ui-tui/src/__tests__/slashParity.test.ts`
- Create: `tests/tui_gateway/test_attention_rpc.py`
- Create: `ui-tui/src/__tests__/attentionCommand.test.ts`

**Interfaces:**
- Produces JSON-RPC `attention.exec` with `{argv: string[], session_id?: string}` and structured `AttentionExecResponse`.
- Consumes `hermes_cli.attention.run_argv(..., output_mode="structured")`, existing pages/panels/system messages, and active profile/session resolution.

- [ ] **Step 1: Write RED native RPC/parity/render tests**

```python
def test_attention_rpc_is_profile_local_and_redacted(rpc, profile_home):
    result = rpc("attention.exec", {"session_id": "sid-1", "argv": ["inbox", "list"]})
    assert result["ok"] is True
    assert result["profile_home"] == str(profile_home)
    assert {"status", "items", "proof", "output"} <= result
    assert "raw_payload" not in json.dumps(result)
```

```typescript
it('routes mutating attention commands through attention.exec', () => {
  findSlashCommand('attention')!.run('inbox label evt-1 useful --evidence-ref note:1', ctx,
    '/attention inbox label evt-1 useful --evidence-ref note:1')
  expect(ctx.gateway.rpc).toHaveBeenCalledWith('attention.exec', {
    argv: ['inbox', 'label', 'evt-1', 'useful', '--evidence-ref', 'note:1'], session_id: 'sid-1'
  })
  expect(ctx.gateway.gw.request).not.toHaveBeenCalledWith('slash.exec', expect.anything())
})
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/tui_gateway/test_attention_rpc.py -q`

Expected: FAIL with unknown RPC method.

Run: `cd ui-tui && npm test -- --run src/__tests__/attentionCommand.test.ts src/__tests__/slashParity.test.ts`

Expected: FAIL because attention has no native handler.

- [ ] **Step 3: Implement bounded RPC and native rendering**

Validate at most 64 UTF-8 argv entries/64 KiB, resolve the active profile without holding profile scope across an await, call the shared runner in a worker thread, and return `{ok, action, output, status, sources, items, decision, audit, digest, budgets, proof, preview, profile_home}`. Never return raw content, recipient/source IDs, exception tracebacks, secrets, or model prompts.

Add native `attention`/`attn` routing. Render status/sources/inbox/audit/proof as panels/pages; render labels/dismiss/resolve/mode applies as persistent system messages; render `premature`, high-value misses, duplicate groups, exhausted budget, stale authority, and `unknown_effect` as warnings with exact CLI routes. Add `attention` to both native-mutating parity sets so catalog discovery cannot fall through to `slash.exec`. No new approval modal is added.

- [ ] **Step 4: Run GREEN and typecheck**

Run: `scripts/run_tests.sh tests/tui_gateway/test_attention_rpc.py -q`

Expected: PASS.

Run: `cd ui-tui && npm test -- --run src/__tests__/attentionCommand.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS; attention mutations are native and bounded with no chat-composer dependency.

- [ ] **Step 5: Commit**

```bash
git add tui_gateway/server.py ui-tui/src/gatewayTypes.ts ui-tui/src/app/slash/commands/ops.ts ui-tui/src/__tests__/slashParity.test.ts tests/tui_gateway/test_attention_rpc.py ui-tui/src/__tests__/attentionCommand.test.ts
git commit -m "feat: add native tui attention controls"
```

---

### Task 10: Add a Secondary Dashboard Inbox and Proof View Without Desktop Work

**Files:**
- Modify: `hermes_cli/web_server.py`
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/App.tsx`
- Create: `web/src/pages/AttentionPage.tsx`
- Create: `web/src/pages/AttentionPage.test.tsx`

**Interfaces:**
- Produces profile-scoped `/api/attention/status`, `/sources`, `/inbox`, `/inbox/{id}`, `/labels`, `/audit`, `/suppressed-sample`, `/digest`, and `/proof` endpoints.
- Consumes the same `AttentionService` and structured renderers; Dashboard never reads tables/config directly and never becomes a second chat surface.

- [ ] **Step 1: Write RED API/page tests**

```typescript
it('shows shadow recommendation, dedupe, reason, opportunity, and label controls', async () => {
  render(<AttentionPage />)
  await screen.findByText('Would interrupt')
  expect(screen.getByText('Shadow — no action taken')).toBeVisible()
  expect(screen.getByText('3 grouped events')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Useful' })).toBeVisible()
  expect(screen.getByRole('button', { name: 'Premature' })).toBeVisible()
  expect(screen.getByRole('button', { name: 'Not useful' })).toBeVisible()
})

it('does not enable rollout when the proof gate fails', async () => {
  render(<AttentionPage />)
  await screen.findByText('High-value misses: 7%')
  expect(screen.getByRole('button', { name: 'Enable reversible actions' })).toBeDisabled()
})
```

Python endpoint tests cover invalid profile, profile-switch race, request/body/limit bounds, auth/CSRF middleware, label idempotency, stale config hash, raw content redaction, proof-gate enforcement, and no await while holding process-global profile scope.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_web_server.py -k attention -q`

Expected: FAIL with 404 attention endpoints.

Run: `cd web && npm test -- --run src/pages/AttentionPage.test.tsx`

Expected: FAIL because the page/API client do not exist.

- [ ] **Step 3: Implement profile-scoped APIs and secondary page**

Resolve/validate profile synchronously, perform service calls in a worker inside a short scope, and return bounded redacted records. POST label/dismiss/resolve/suppressed-sample/digest-preview only; stable source/mode changes remain preview/hash/apply through the shared config service. The page shows mode/source health, grouped inbox, full decision reasons/scores/opportunity, label actions, suppressed audit progress, budgets, dedupe statistics, proof thresholds, and mode rollback. It does not deliver messages, execute actions, or manipulate chat/PTY state.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_web_server.py -k attention -q`

Expected: PASS.

Run: `cd web && npm test -- --run src/pages/AttentionPage.test.tsx && npm run typecheck`

Expected: PASS; Dashboard is a non-destructive secondary inspector and no `apps/desktop/` file changes.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/web_server.py web/src/lib/api.ts web/src/App.tsx web/src/pages/AttentionPage.tsx web/src/pages/AttentionPage.test.tsx
git commit -m "feat: add dashboard attention inbox"
```

---

### Task 11: Prove Real-Path Recovery, Security, Profile Isolation, and Cache Stability

**Files:**
- Create: `tests/agent/attention/test_security.py`
- Create: `tests/agent/attention/test_e2e.py`
- Modify: `tests/agent/attention/test_runtime.py`
- Modify: `tests/gateway/test_webhook_attention.py`
- Modify: `tests/hermes_cli/test_profiles.py`

**Interfaces:**
- Consumes the complete broker, real `SessionDB`, real config/workflow/mission/transaction imports, local webhook server, temp files/Git repo, and delivery/action fakes only at outward boundaries.
- Produces no new public interface; it is the release safety gate.

- [ ] **Step 1: Write real-path crash/replay/partial-failure scenarios**

Use a temporary `HERMES_HOME`, real SQLite/WAL, real guarded config, real local aiohttp webhook, real temp filesystem/Git repository, real workflow and mission stores, real policy/authority imports, and process/object reconstruction. Cover:

1. crash after authenticated receive but before event insert returns 503 and provider retry creates one event;
2. crash after event insert before decision recovers to review with complete audit;
3. crash after decision before inbox creates one inbox item on restart;
4. crash before/after mission append creates one mission event;
5. crash before/after transaction commit intent delegates to item #2 reconciliation and never blindly retries unknown;
6. crash before/after delivery acknowledgement creates at most one Hermes delivery identity and truthfully records unknown acknowledgement;
7. exact webhook retry and semantic Slack/Discord duplicate create one surface/digest/interrupt/action while retaining both provenance records;
8. authority expires/changes between policy preview and execution, producing zero mission/effect/delivery calls;
9. budget reservation races/replays never overspend; failed delivery/action settles or releases once according to actual attempt;
10. workflow trigger event replay creates one execution; compensation is blocked after a mutating node commits;
11. source revocation stops new intake immediately while preserving auditable historical hashes;
12. default and named profiles with identical remote delivery IDs do not share subscriptions, hashes, events, budgets, inbox, labels, or deliveries.

- [ ] **Step 2: Write adversarial source-to-sink security tests**

```python
@pytest.mark.parametrize("attack", [
    "payload_sets_profile", "payload_sets_subscription", "payload_sets_public_sensitivity",
    "payload_sets_dedupe_of_victim", "payload_sets_mission", "payload_sets_workflow",
    "prompt_claims_user_approved", "model_returns_interrupt_for_denied_event",
    "unicode_entity_confusable", "semantic_dedupe_poisoning", "webhook_replay_old_timestamp",
    "invalid_hmac", "oversize_chunked_body", "ssrf_shaped_content_ref",
    "filesystem_symlink_escape", "git_hook_injection", "cross_profile_delivery_replay",
    "stale_authority_replay", "reaction_from_unauthorized_actor", "secret_in_summary",
])
def test_attack_never_expands_attention_or_authority(security_harness, attack):
    result = security_harness.attempt(attack)
    assert result.mission_calls == 0
    assert result.effect_calls == 0
    assert result.delivery_calls == 0
    assert security_harness.no_secret_in_db_logs_output_or_model_request()
```

Threat-model prompt injection, confused delegation, SSRF, replay, privilege drift, derived-memory leakage, compromised normalizer/plugin, malicious webhook peer, duplicate poisoning, sensitive cross-source grouping, and reaction forgery. Registry/plugin normalizers are trusted-code boundaries: invalid output or exceptions become rejected/review, not fallback allow. Content refs are opaque owner lookups, never fetchable URLs.

- [ ] **Step 3: Prove cache, model, and role invariants**

Run a multi-turn real agent harness while events are ingested, deduplicated, ranked, labeled, digested, and transaction-reconciled. Independently hash the system message, effective tool definitions, primary provider, and primary model before/after every broker state change. Assert all unchanged, strict role alternation, compression-only history mutation, and no synthetic user event. The optional ranker uses its auxiliary request and never mutates the primary client/cache lineage. Direct delivery never writes a gateway conversation turn.

- [ ] **Step 4: Run RED at every injected boundary**

Run: `scripts/run_tests.sh tests/agent/attention/test_e2e.py tests/agent/attention/test_security.py tests/agent/attention/test_runtime.py tests/gateway/test_webhook_attention.py tests/hermes_cli/test_profiles.py -q`

Expected: FAIL at the first unhandled crash, replay, redaction, profile, or cache boundary.

- [ ] **Step 5: Make only contract-preserving corrections and run GREEN**

Corrections stay in files owned by Tasks 1–7, preserve public names/state vocabularies, and do not weaken fail-closed behavior. Every ambiguous outward effect remains `unknown_effect`; every audit-store failure blocks action/interrupt and surfaces operator health.

Run: `scripts/run_tests.sh tests/agent/events tests/agent/attention tests/gateway/test_attention_bridge.py tests/gateway/test_attention_reactions.py tests/gateway/test_webhook_attention.py tests/cron/test_attention_events.py tests/hermes_cli/test_workflow_webhook_trigger.py tests/hermes_cli/test_attention.py tests/hermes_cli/test_profiles.py tests/tui_gateway/test_attention_rpc.py -q`

Expected: PASS; all fault/security cases converge without duplicate or unauthorized effects and cache identities remain stable.

- [ ] **Step 6: Commit**

```bash
git add agent/events agent/attention gateway/attention_bridge.py gateway/platforms/base.py gateway/platforms/webhook.py gateway/run.py cron/scheduler.py hermes_cli/workflows_db.py hermes_cli/workflows_dispatcher.py tests/agent/attention tests/gateway/test_webhook_attention.py tests/hermes_cli/test_profiles.py
git commit -m "test: prove attention broker safety"
```

---

### Task 12: Run the Shadow Proof, Gate Reversible Rollout, and Document Operations

**Files:**
- Create: `benchmarks/attention/run.py`
- Create: `benchmarks/attention/score.py`
- Create: `benchmarks/attention/README.md`
- Modify: `tests/benchmarks/test_attention_benchmark.py`
- Create: `website/docs/user-guide/features/proactive-attention-broker.md`
- Create: `website/docs/developer-guide/event-envelope-attention.md`

**Interfaces:**
- Produces `record_window()`, `export_proof()`, `score_proof()`, local `results.json`, `report.md`, a signed content hash used by rollout apply, and complete operator/developer documentation.
- Consumes the frozen Task 0 contract and final service/CLI APIs; generated proof results remain local and are not committed.

- [ ] **Step 1: Write RED denominator/metric/stop-gate tests**

```python
def test_score_requires_all_fixed_denominators(complete_shadow_run):
    report = score_proof(complete_shadow_run)
    assert report.active_windows >= 10
    assert report.normalized_events >= 500
    assert report.surfaced_labeled >= 100
    assert report.suppressed_audited >= 100
    assert report.usefulness_precision >= 0.85
    assert report.premature_interruptions_per_window < 1.0
    assert report.high_value_miss_rate <= 0.05
    assert report.unauthorized_actions == 0
    assert report.duplicate_surfaces_or_actions == 0
    assert report.incomplete_audit_rows == 0
    assert report.passed


@pytest.mark.parametrize("mutation", [
    "nine_windows", "short_window", "source_health_below_95", "unlabeled_surface",
    "unstratified_suppressed_sample", "missing_decision", "one_shadow_action",
    "duplicate_interrupt", "precision_below_85", "premature_rate_equal_one",
    "miss_rate_above_five", "post_hoc_threshold_change",
])
def test_any_gate_failure_blocks_rollout(complete_shadow_run, mutation):
    assert not score_proof(complete_shadow_run.mutate(mutation)).passed
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_attention_benchmark.py -q`

Expected: FAIL because runner/scorer are incomplete.

- [ ] **Step 3: Implement exact metrics and proof artifact**

Use these definitions:

- `normalized_events`: distinct logical `attention_events`; ingest retries remain a separate reported denominator.
- `surfaced_candidates`: non-duplicate events whose shadow recommendation is review, digest, interrupt, mission update, or preauthorized action and which have a shadow inbox row.
- `usefulness_precision = useful / (useful + premature + not_useful)` over all labeled surfaced candidates; every surfaced candidate in the proof denominator must have exactly one label.
- `premature_interruptions_per_window = count(recommended interrupt AND label premature) / qualifying active windows`; equality to 1.0 fails because the threshold is exclusive.
- `high_value_miss_rate = high_value_miss / (high_value_miss + not_high_value)` over the preregistered stratified, non-duplicate suppressed audit sample; Wilson 95% upper bound is also reported and cannot be hidden.
- `reliable_dedupe`: 100% exact provider retries map to one logical event and zero semantic duplicate groups create more than one inbox/delivery/mission/action execution. Report separately; do not claim exactly-once.
- `complete_review_trail`: every authorized normalized event has source/subscription identity, dedupe result, policy version/hash, score components, reason codes, recommended/effective disposition, authority/budget references or explicit not-applicable code, opportunity window, and execution/shadow status.
- `unauthorized_action`: any mission update, workflow start, transaction, delivery, model route, or other effect during shadow; any later action without current allow/budget; or any effect outside its exact subscription binding.

Report current-Hermes baseline and broker candidate separately for user attention minutes, surfaced count, duplicate count, inference calls/tokens/cost, p50/p95 decision latency, source health, recovery burden, and failure clarity. Report every exclusion/abort with reason, all strata, denominators, Wilson intervals, hardware/network class, and local cost source. Underpowered results are inconclusive; never relax a threshold after collection.

The proof artifact contains manifest/corpus/policy/normalizer/config hashes, qualifying window IDs, source-health hashes, all metric inputs by event ID, result hash, scorer version, and `passed`. It contains no raw content or credentials. Guarded mode apply accepts only the exact current proof hash and rechecks that subscriptions/policy/authority budget definitions have not changed; otherwise a new shadow proof is required.

- [ ] **Step 4: Execute the no-action proof commands**

Run before the first window:

`python benchmarks/attention/run.py preregister --manifest benchmarks/attention/manifest.yaml --output-dir benchmarks/attention/results/local/attention-shadow-v1`

Expected: exits 0, freezes manifest/config/policy/source hashes, and confirms `attention.mode=shadow` plus at least two healthy authorized real sources.

Run for each preregistered window:

`python benchmarks/attention/run.py record-window --proof-dir benchmarks/attention/results/local/attention-shadow-v1 --duration-hours 8`

Expected: exits 0 only after an eight-hour window; records source-health and event/audit hashes; performs zero outward action. Repeat until at least ten qualifying windows, 500 events, and 100 surfaced candidates exist.

Run labeling/audit through the primary UI:

`hermes attention inbox list --state shadow --limit 500`

`hermes attention audit-suppressed sample --count 100 --seed attention-shadow-v1`

Expected: every surfaced event receives one surfaced label and every sampled suppressed event receives one miss label with evidence reference.

Score:

`python benchmarks/attention/score.py --proof-dir benchmarks/attention/results/local/attention-shadow-v1 --output benchmarks/attention/results/local/attention-shadow-v1/report.md`

Expected: exits 0 only when every sample, metric, safety floor, and complete-audit gate passes; otherwise exits 3 and names each blocking denominator/threshold.

- [ ] **Step 5: Define staged rollout and rollback**

1. Ship `off` by default. Operators configure/test sources and authority without intake.
2. Enter `shadow` by exact config hash and collect the full proof; no mission/action/delivery occurs.
3. If and only if the proof passes, allow explicit `notify_only` apply bound to the proof hash. Start with review/digest; enable interrupt only after the user sets a positive `attention.interrupt` budget and explicit allow rule.
4. After at least two additional qualifying notify-only windows remain within the same precision/premature/miss floors and produce zero duplicate delivery, allow explicit `reversible_actions` apply. Only exact transaction-eligible reversible actions and idempotent mission updates are enabled.
5. Stop and revert to `shadow` on any unauthorized/irreversible/unknown action attempt, duplicate surface/delivery/action, cross-profile event, unredacted sensitive value, audit gap, source auth bypass, stale authority commit, budget overspend, false verified claim, cache/tool/provider/model drift, role violation, usefulness precision below 85%, premature rate at least 1/window, or high-value miss rate above 5%.
6. Roll back through guarded `attention mode set shadow|off`; stop new executor claims, let item #2 reconcile in-flight effects, preserve unknowns/review/audit, and never delete the shared DB. Source removal can purge derived summaries explicitly while retaining content-free audit hashes.

- [ ] **Step 6: Write operator and developer documentation**

The user guide documents the layman outcome, opt-in modes, complete command grammar, four initial source kinds/exclusions, source YAML, existing webhook credentials, shadow inbox/labels/reactions, suppressed audit, opportunity windows, deterministic/model ranking, quiet hours, autonomy budgets, mission/workflow/action binding, digests/interrupts, proof thresholds, staged apply, rollback, profile isolation, export/purge, failures, and truthful dedupe/verification language.

The developer guide documents every `EventEnvelope` field/bound, trusted normalizer protocol, canonical identity/hash rules, cross-channel entity mappings, source authorization, SQL/state transitions, crash recovery, fixed-point policy, ranker JSON/bounds, authority/budget calls, transaction/mission/workflow ownership, gateway/cron/workflow sinks, reaction composition, content references/redaction/deletion, plugin/service-gated source registration, cache invariants, and required real-path tests. Include one complete standalone source plugin example that registers a normalizer and emits candidates without touching core files.

- [ ] **Step 7: Run GREEN through the final verification matrix**

Run: `scripts/run_tests.sh tests/agent/events tests/agent/attention tests/agent/autonomy/test_usage_budgets.py tests/gateway/test_attention_bridge.py tests/gateway/test_attention_reactions.py tests/gateway/test_webhook_attention.py tests/cron/test_attention_events.py tests/hermes_cli/test_workflow_webhook_trigger.py tests/hermes_cli/test_attention.py tests/tui_gateway/test_attention_rpc.py tests/benchmarks/test_attention_benchmark.py -q`

Expected: PASS.

Run: `cd ui-tui && npm test -- --run src/__tests__/attentionCommand.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS.

Run: `cd web && npm test -- --run src/pages/AttentionPage.test.tsx && npm run typecheck`

Expected: PASS.

Run: `scripts/run_tests.sh`

Expected: full Python suite PASS under CI-parity isolation.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 8: Commit**

```bash
git add benchmarks/attention/run.py benchmarks/attention/score.py benchmarks/attention/README.md tests/benchmarks/test_attention_benchmark.py website/docs/user-guide/features/proactive-attention-broker.md website/docs/developer-guide/event-envelope-attention.md
git commit -m "docs: ship attention broker proof and operations"
```

---

## Final Verification Matrix

| Requirement | Proof |
|---|---|
| Canonical event identity | Frozen `EventEnvelope`, source/native identity, canonical hashes, stable receive-time-independent event ID |
| Entity/time/dedupe/sensitivity/causality | Typed bounded fields, trusted normalizer tests, cross-channel mapping and poisoning cases |
| Authorized source subscriptions | Exact profile/account/scope/event matching, expiry/revocation, existing adapter admission/auth before broker |
| Cheap deterministic filters | Fixed-point policy order and deterministic shuffled-input tests before ranker calls |
| Optional bounded model ranking | Off by default, authority/budget checked, bounded redacted batches, strict JSON, conservative failure |
| Six decisions | Stable ignore/update-mission/preauthorized-action/review/digest/interrupt vocabulary and mode mapping |
| Attention/action budgets | Named item #6 reservations with atomic replay-safe reserve/settle/release and fresh authority |
| No-action shadow | All effectful recommendations forced to shadow inbox/suppressed; zero mission/action/delivery/model route outside configured ranking |
| Cross-channel dedupe | Durable exact attempts plus semantic grouping; one inbox/delivery/mission/action identity per group |
| Complete audit | Recovery closes every authorized event to a decision; no audit gap, with content-free hashes/evidence refs |
| Mission ownership | Idempotent append-only mission event only; no verdict/execution ownership in attention |
| Reversible action ownership | Item #2 transaction/eligibility/recheck/reconcile/receipt; irreversible/unknown becomes review |
| Webhook/workflow completion | Auth/filter/rate/idempotency before broker; durable event-trigger row and transaction-owned workflow start |
| Cron/goal/workflow/source hooks | Optional post-commit sinks and bounded filesystem/Git poller; unchanged owner behavior when absent |
| Reaction feedback | Additive listener composition, actor authorization, idempotent useful/premature/not-useful labels |
| Profile/privacy/security | Temporary homes, separate hash keys/state, no raw content/secrets, hostile source-to-sink tests |
| Cache/conversation safety | Stable prompt/tools/provider/model hashes, strict alternation, no synthetic turn, auxiliary ranker isolation |
| Primary/secondary surfaces | Shared CLI/classic and native Ink primary; Dashboard inspector secondary; no Desktop files |
| 90-day proof | >=10 eight-hour windows, >=500 events, >=100 surfaced/labeled, >=100 suppressed/audited, precision >=85%, premature <1/window, misses <=5% |
| Rollout/stop/rollback | Proof-hash-bound notify-only then reversible-actions, explicit safety stops, guarded rollback preserving evidence |
| Narrow-waist extensibility | Shared event/broker seams, CLI + skill, source connectors service-gated/plugin/MCP, no core model tool |

The Proactive Attention Broker is complete only when the no-action proof passes with all denominators and safety floors, notify-only remains within the same quality gates, and every enabled action is still proven reversible and freshly authorized at item #2's commit boundary. A high-recall broker that annoys the user, misses high-value suppressed events, duplicates delivery, or cannot explain every decision fails this plan.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-proactive-attention-broker.md`. Execute only after portfolio items #1, #2, and #6 expose the consumed contracts named above.

1. **Subagent-Driven (recommended)** — use `superpowers:subagent-driven-development`, one fresh implementation subagent per task with review between tasks.
2. **Inline Execution** — use `superpowers:executing-plans`, execute task batches with explicit checkpoints.
