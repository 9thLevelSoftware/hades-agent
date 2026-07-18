# Cache-Aware Context Compiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sustain long Hermes conversations and missions with equivalent or better outcomes while reducing uncached input cost by at least 30%, improving cache hits, preserving fresh critical state, and never changing cache identity outside an explicit conversation transition.

**Architecture:** Extend the model-invisible `ContextEngine` boundary with a declarative four-lane segment graph: immutable cached prefix, versioned user/memory snapshots, hot mission/evidence state, and ephemeral tool payloads. A cache-aware context-engine plugin compiles those segments into the existing OpenAI-format conversation without inventing messages or owning source data; it independently fingerprints the pinned system prompt, pinned effective tool schema, provider, and model. OpenAI compaction, Google state/cache, and Anthropic context editing are optional adapter optimizations invoked only at Hermes's existing compression boundary; a downgrade always falls back to authoritative Hermes compilation.

**Tech Stack:** Python 3.13, frozen dataclasses/enums, canonical JSON/SHA-256, SQLite/WAL through `SessionDB`, existing `ContextEngine`/`ContextCompressor`/conversation compression, prompt builder and prompt caching, tool registry, mission/knowledge/autonomy/receipt contracts, provider adapters and canonical usage telemetry, Rich/classic CLI, Ink/TypeScript JSON-RPC TUI, pytest through `scripts/run_tests.sh`, Vitest, and versioned YAML/JSONL replay fixtures.

## Global Constraints

- Work from a branch containing item #1's canonical mission records/events/review state, item #4's `KnowledgeMemoryProvider` and versioned knowledge retrieval, item #6's canonical `AuthorityProvider`/`ActionContext` privacy decisions, and item #12's immutable `ReceiptStore`. Missing prerequisites fail their prerequisite tests; this plan creates no local substitutes.
- The system prompt, effective model tool-definition snapshot, provider, and model are independently canonicalized and SHA-256 fingerprinted once at conversation creation. All four remain pinned until an explicit new-conversation or cache-identity transition.
- No compression, memory refresh, mission update, provider optimization, plugin refresh, dynamic schema override, tool-search refresh, or crash recovery may rebuild the cached prefix or mutate the effective model tool schema mid-conversation.
- Compression-only history mutation remains the sole exception to append-only message history. Compiled context preserves strict role alternation, assistant/tool-call pairing, tool-call IDs, content-part order, and the real latest user turn; it never injects a synthetic user message.
- The four lanes are logical ownership/budget classes, not four new chat roles. Versioned snapshots and hot state attach only through existing real-turn context seams; ephemeral payload reduction operates only on existing messages; the immutable prefix remains the pinned system message plus the separately pinned effective tool definitions.
- Knowledge owns long-lived claims/evidence, missions own durable objective/execution state, and receipts own proof. The compiler stores references, hashes, token accounting, and recomputation metadata; it never becomes their source of truth or silently promotes a summary into memory/evidence.
- Stable non-secret settings live under `context.compiler` in profile-local `config.yaml`. Credentials remain in `.env` or secret providers. Runtime graph manifests, transitions, and redacted telemetry live in profile-local SQLite; provider-native retained state requires item #6 disclosure and authority.
- Provider storage/retention, region, deletion, training use, TTL, and cache semantics are displayed before enabling a native optimization. Unknown disclosure or current authority `ask|deny` disables that optimization and uses local Hermes semantics.
- Provider-native compaction/editing/caching is invoked only from `agent.conversation_compression.compress_context()` or the existing provider-owned equivalent. It may optimize transport, never redefine Hermes segment identity, freshness, role semantics, or outcome truth.
- Provider-native state is a disposable cache, not authoritative session state. Crash, cache eviction, unavailable feature, capability downgrade, or provider error reconstructs the same Hermes compile from local sources without losing hot state or replaying stale provider content.
- Dynamic registry metadata and `dynamic_schema_overrides` may continue to update the next conversation's schema. The current conversation uses a deep-frozen effective schema snapshot; a requested toolset/schema/provider/model change opens the existing explicit transition rather than mutating it in place.
- No new model-visible core tool is introduced. Inspection/explanation uses top-level CLI, classic slash, and native Ink JSON-RPC; provider compilers remain plugins/adapters. Delivery is Footprint Ladder rung 1.
- Real-path state, privacy, compression, provider-boundary, and recovery tests use a temporary `HERMES_HOME`, real config/SQLite/import/plugin discovery/message compilation, and actual local segment sources. Mock only provider network/process boundaries and clocks.
- Profiles remain independent islands. Segment/source/cache IDs include the active profile identity, and no compiler or recovery path reads live default-profile state.
- No outbound telemetry is enabled. Provider usage is normalized into local session accounting; proof rows and reports remain local and redact source content, prompts, tool payloads, personal claims, and credentials.
- User-visible status distinguishes input, cache-read, cache-write, uncached input, output, provider-retained state, compaction count, freshness, and estimated/actual cost. A cache hit is never called verification and a provider compaction acknowledgement is never called exactly-once.
- CLI/terminal and native Ink are primary. Dashboard adds no second compiler editor; its embedded Ink view is sufficient. Desktop is not a dependency or parity target.
- The frozen proof replays exactly 100 long sessions/missions against current Hermes behavior and reports outcome, tokens, uncached cost, cache-hit rate, latency, compression, freshness, identity, roles, exclusions, and confidence intervals before any default-on rollout.

---

## Approved Portfolio Contract

**Layman outcome:** Hermes can sustain very long relationships and missions without repeatedly paying to resend everything or forgetting critical state, while preserving its cached prompt prefix.

**90-day proof:** Replay exactly 100 preregistered long sessions and missions against current behavior. Measure behavioral evaluation, total/cached/uncached input tokens and cost, latency, compression frequency, freshness, and independent hashes for system prompt, effective tool schema, provider, and model. Pass only with equivalent or better outcome scores, at least 30% lower uncached input cost, a strictly improved cache-hit rate, no identity change outside an explicit transition, and valid role alternation in every request and persisted transcript.

**Dependencies and failure conditions:** Item #1 supplies structured hot mission state, item #4 supplies versioned knowledge snapshots, item #6 governs provider storage/retention/privacy, and item #12 supplies outcome/evidence truth. Stop or narrow the affected provider lane on any outcome regression, stale required segment, privacy disclosure/authority failure, role/tool-pair corruption, prefix/schema/provider/model drift, cross-profile segment, unrecoverable crash state, provider downgrade that changes semantics, false cache telemetry, or failure to meet the cost/cache target without worsening latency/recovery.

**Delivery:** Footprint Ladder rung 1—extend `ContextEngine`, prompt construction, existing compression/session rotation, and usage accounting. Cross-provider Hermes semantics are authoritative; OpenAI, Google, and Anthropic compiler optimizations remain service-gated plugins/adapters. No new model tool, Dashboard editor, or Desktop dependency.

---

## Product Boundary and Frozen 100-Replay Proof

The compiler answers one question: “Given pinned conversation identity and current versioned sources, which exact existing message content should be sent now, and which content may be recomputed or reduced at the next compression boundary?” It does not choose goals, revise knowledge, score receipts, authorize remote retention, route providers/models, or execute tools.

`benchmarks/context_compiler/manifest.yaml` freezes version `cache-aware-context-100-v1`, baseline `current_hermes_context_compressor`, candidate `cache_aware_context_compiler`, provider/model fixtures, tokenizer/version, pricing snapshot, percentile method, and exactly 100 cases:

- 20 software-maintenance replays: 10 sessions and 10 missions;
- 20 sourced-research replays: 10 sessions and 10 missions;
- 15 data/artifact-pipeline replays: 8 sessions and 7 missions;
- 15 repeated-web-operation replays: 8 sessions and 7 missions;
- 15 personal-knowledge lifecycle replays: 7 sessions and 8 missions;
- 15 proactive/recovery mission replays: 7 sessions and 8 missions.

Every replay begins from an immutable JSONL transcript/source fixture, reaches at least 75% of its configured context window or three baseline compression boundaries, declares required outcome assertions, freshness-sensitive facts, expected mission/knowledge/receipt versions, provider/model/cache identity, tool schema fixture, hardware/network class, pricing, faults, and retention disclosure. Baseline and candidate replay the same actual content and provider response fixture; no private user history is mined.

Exact metrics and gates:

| Metric | Definition | Gate |
|---|---|---|
| Outcome | Independent required-assertion score from task artifact/mission receipt fixture, never model self-report | Candidate aggregate `>=` baseline and no case loses a required/safety assertion |
| Uncached input cost | Sum over calls of provider-priced input tokens excluding cache reads, including cache writes where charged | Candidate `<= 70%` of baseline overall and in each 20+ case safety-relevant stratum |
| Cache-hit rate | `cache_read_tokens / input_tokens`, reported per provider/workflow and overall | Candidate strictly greater than baseline overall and nondecreasing in every supported provider lane |
| Identity | Independent system/tool/provider/model hashes on every request | Zero changes except rows naming an explicit transition ID and new conversation/cache lineage |
| Roles | Validator over API requests and persisted transcript, including tool pairs | 100% valid; zero synthetic mid-loop user messages |
| Freshness | Required source version/hash in compiled graph equals declared current mission/knowledge/receipt version | 100%; one stale required segment is a hard stop |
| Tokens | Total/input/cache-read/cache-write/uncached/output/reasoning by call and lane estimate | Report p50/p95/sum; no hidden/negative/unattributed bucket |
| Latency | Compile, provider, first-token, total-turn, and compression-boundary monotonic durations | Candidate p95 total-turn no more than 10% above baseline and compile p95 `< 100 ms` excluding provider calls |
| Compression | Count, trigger, input/output tokens, provider-native/local path, reclaimed tokens, failures | No compression loop; candidate count no more than baseline unless outcome/freshness requires it |
| Recovery | Crash/replay/downgrade convergence to identical local compiled hash | 100% for injected fault matrix; provider state never required |

Missing/aborted cases remain in the denominator with reasons. An underpowered provider slice is inconclusive, not merged into an average or granted a relaxed post-hoc threshold.

## Current Code Map and Ownership

### Existing seams this plan extends

- `agent/context_engine.py` — current `ContextEngine` lifecycle, token usage, preflight, `compress()`, session lifecycle, optional tools, status, and model updates.
- `agent/context_compressor.py` — built-in cross-provider compressor, head/tail protection, tool-result/media pruning, summary continuity, threshold/cooldown, and canonical OpenAI-message output.
- `agent/conversation_compression.py::compress_context()` — authoritative host compression boundary, lock, session rotation/in-place rewrite, prompt handling, persistence, and Codex app-server native compaction.
- `agent/conversation_loop.py` — preflight/post-response/overflow compression triggers, API message copy, cache-control decoration, canonical usage accumulation, and role-sensitive dispatch.
- `agent/agent_init.py` — context-engine selection, model/context configuration, tool definitions, engine tools, cached-system-prompt initialization, and session lifecycle.
- `agent/system_prompt.py` — prompt parts/build/cache invalidation; compiler pins the one built result and removes normal compression-time rebuilds.
- `agent/prompt_builder.py` — context files, skills snapshot, and stable instruction construction; these are prefix source inputs only at conversation creation.
- `agent/turn_context.py` — memory prefetch/turn context injection and provider-owned Codex compaction decisions; versioned/hot segments attach through this real-user-turn seam.
- `agent/prompt_caching.py` and `agent/agent_runtime_helpers.py` — Anthropic-style cache-control layout and provider cache policy.
- `tools/registry.py` and `model_tools.py::get_tool_definitions()` — registration, dynamic schema overrides, sanitization, tool search, and final effective schemas; compiler pins the final deep-frozen result per conversation.
- `agent/chat_completion_helpers.py`, `agent/codex_responses_adapter.py`, `agent/gemini_native_adapter.py`, and `agent/anthropic_adapter.py` — provider transport and canonical usage/cache telemetry boundaries.
- `agent/transports/codex.py`, `agent/transports/codex_app_server_session.py`, and `agent/codex_runtime.py` — prompt cache key, native compaction notification, and normalized cached-input accounting.
- `plugins/context_engine/__init__.py` and `hermes_cli/plugins.py::PluginContext.register_context_engine()` — current config-selected context-engine plugin discovery.
- `hermes_state.py` and `gateway/session.py` — profile-local session transcript/prompt/compression lineage and recovery.
- `hermes_cli/missions_db.py` (item #1) — mission objective/events/review/version source; remains mission truth.
- `plugins/memory/knowledge/provider.py` and retrieval/store modules (item #4) — versioned bounded knowledge prefetch with provenance/freshness/conflicts; remain knowledge truth.
- `agent/autonomy` (item #6) — provider routing/storage/retention authority and audit.
- `agent.receipts` (item #12) — immutable evidence/outcome claims; compiler references digest/observation state and never scores it.
- `hermes_cli/commands.py`, `cli.py`, `tui_gateway/server.py`, `ui-tui/src/gatewayTypes.ts`, and `ui-tui/src/app/slash/commands/session.ts` — command catalog, compression/status, live JSON-RPC, and native Ink session controls.

### New focused production files

- `agent/context_segments.py` — frozen segment graph, cache identity, compiled request/transition/telemetry contracts, canonical hashes, and validators.
- `agent/context_sources.py` — adapters over conversation, pinned prefix/tools, item #1 mission, item #4 knowledge, and item #12 receipt sources.
- `agent/context_identity.py` — independent system/tool/provider/model fingerprints, pinning, comparison, and explicit transition validation.
- `plugins/context_engine/cache_aware/__init__.py` — registers the cache-aware engine.
- `plugins/context_engine/cache_aware/plugin.yaml` — discovery/config description.
- `plugins/context_engine/cache_aware/compiler.py` — four-lane graph builder, budgets, dependency/freshness/recomputation, materialization, and compatibility `compress()`.
- `plugins/context_engine/cache_aware/store.py` — profile-local graph/transition/telemetry metadata with no duplicated sensitive payloads.
- `plugins/context_engine/cache_aware/provider.py` — provider optimization ABC, registry, disclosure, service gating, and Hermes fallback.
- `plugins/context_engine/cache_aware/providers/openai.py` — OpenAI Responses/native compaction optimization.
- `plugins/context_engine/cache_aware/providers/google.py` — Google cached-content/state/Interactions optimization.
- `plugins/context_engine/cache_aware/providers/anthropic.py` — Anthropic cache-control/context-editing optimization.
- `hermes_cli/context_compiler.py` — one inspect/explain/doctor/transition/benchmark command service.

### Existing production files modified

- `agent/context_engine.py`, `agent/context_compressor.py`, `agent/conversation_compression.py`, `agent/conversation_loop.py`, `agent/agent_init.py`, `agent/system_prompt.py`, `agent/turn_context.py`
- `tools/registry.py`, `model_tools.py`
- `agent/chat_completion_helpers.py`, `agent/codex_responses_adapter.py`, `agent/gemini_native_adapter.py`, `agent/anthropic_adapter.py`
- `hermes_state.py`, `hermes_cli/plugins.py`, `hermes_cli/commands.py`, `hermes_cli/main.py`, `cli.py`
- `tui_gateway/server.py`, `ui-tui/src/gatewayTypes.ts`, `ui-tui/src/app/slash/commands/session.ts`

### Focused tests and proof files

- `tests/agent/test_context_segments.py`, `test_context_identity.py`, `test_context_sources.py`, `test_cache_aware_context_engine.py`, `test_context_provider_optimizers.py`, `test_context_compiler_security.py`
- `tests/integration/test_cache_aware_context_e2e.py`
- `tests/hermes_cli/test_context_compiler.py`, `tests/tui_gateway/test_context_compiler_rpc.py`
- `ui-tui/src/__tests__/contextCompilerCommand.test.ts`, `ui-tui/src/__tests__/slashParity.test.ts`
- `tests/benchmarks/test_context_compiler_benchmark.py`
- `benchmarks/context_compiler/manifest.yaml`, `cases.yaml`, `fixtures/*.jsonl`, `runner.py`, `score.py`
- `website/docs/user-guide/features/cache-aware-context.md`, `website/docs/development/context-compiler-providers.md`, reference command docs, and `website/sidebars.ts`

## Canonical Public Interfaces — Frozen for All Tasks

`agent.context_segments` exposes these exact model-invisible names. Canonical payload hashes use UTF-8 JSON with sorted keys, compact separators, NFC strings, finite integers, tuple-to-array conversion, and explicit schema version.

```python
ContextLane = Literal[
    "immutable_prefix", "versioned_snapshots", "hot_state", "ephemeral_payloads"
]
SegmentStability = Literal[
    "conversation_immutable", "versioned", "turn_hot", "ephemeral"
]
SegmentStatus = Literal["fresh", "stale", "missing", "recomputing", "invalid"]
TransitionReason = Literal[
    "new_conversation", "compression_rotation", "manual_new", "model_change",
    "provider_change", "tool_schema_change", "system_prompt_change",
]

@dataclass(frozen=True)
class SegmentProvenance:
    owner: Literal["prompt", "tools", "conversation", "mission", "knowledge", "receipt", "tool"]
    source_id: str
    source_version: str
    content_hash: str
    observed_at_ms: int
    authority_hash: str | None

@dataclass(frozen=True)
class SegmentDependency:
    segment_id: str
    required_content_hash: str
    required_source_version: str

@dataclass(frozen=True)
class RecomputeSpec:
    adapter_id: str
    method: str
    input_hashes: tuple[str, ...]
    deterministic: bool

@dataclass(frozen=True)
class ContextSegment:
    segment_id: str
    lane: ContextLane
    kind: str
    stability: SegmentStability
    status: SegmentStatus
    provenance: SegmentProvenance
    token_cost: int
    token_counter_id: str
    fresh_until_ms: int | None
    dependencies: tuple[SegmentDependency, ...]
    recompute: RecomputeSpec
    role_anchor: Literal["system", "user", "assistant", "tool", "tools_array"]
    message_indexes: tuple[int, ...]
    required: bool
    priority: int
    payload_ref: str
    content_hash: str

@dataclass(frozen=True)
class ContextGraph:
    schema_version: Literal["hermes.context-graph.v1"]
    profile_id: str
    conversation_session_id: str
    revision: int
    segments: tuple[ContextSegment, ...]
    graph_hash: str

@dataclass(frozen=True)
class CacheIdentity:
    system_prompt_sha256: str
    effective_tool_schema_sha256: str
    provider_sha256: str
    model_sha256: str
    identity_sha256: str

@dataclass(frozen=True)
class CompiledContext:
    graph_hash: str
    cache_identity: CacheIdentity
    messages: tuple[dict[str, object], ...]
    effective_tool_definitions: tuple[dict[str, object], ...]
    included_segment_ids: tuple[str, ...]
    recomputed_segment_ids: tuple[str, ...]
    omitted_segment_ids: tuple[str, ...]
    token_budget: int
    estimated_input_tokens: int
    compile_duration_ms: int
    compiled_hash: str

@dataclass(frozen=True)
class ContextTransition:
    transition_id: str
    reason: TransitionReason
    old_session_id: str | None
    new_session_id: str
    old_identity: CacheIdentity | None
    new_identity: CacheIdentity
    old_graph_hash: str | None
    new_graph_hash: str
    created_at_ms: int

@dataclass(frozen=True)
class ProviderRetentionDisclosure:
    provider_id: str
    optimization_id: str
    stores_content: bool
    storage_scope: str
    retention_ttl_seconds: int | None
    deletion_supported: bool
    training_use: Literal["no", "yes", "unknown"]
    region: str
    summary: str
    disclosure_hash: str

@dataclass(frozen=True)
class ProviderContextPlan:
    optimization_id: str
    boundary: Literal["compression"]
    compiled_hash: str
    retention: ProviderRetentionDisclosure
    provider_state_ref: str | None
    expected_semantic_hash: str

class ProviderContextOptimizer(Protocol):
    optimizer_id: str
    def is_available(self, runtime: ProviderRuntime) -> bool: ...
    def disclosure(self, runtime: ProviderRuntime) -> ProviderRetentionDisclosure: ...
    def plan(self, compiled: CompiledContext, runtime: ProviderRuntime) -> ProviderContextPlan: ...
    def apply(self, plan: ProviderContextPlan, runtime: ProviderRuntime) -> ProviderContextResult: ...
```

`ContextEngine` gains optional, backward-compatible methods:

```python
def build_graph(self, request: ContextCompileRequest) -> ContextGraph: ...
def compile(self, request: ContextCompileRequest) -> CompiledContext: ...
def plan_compression(self, request: ContextCompressionRequest) -> ContextCompressionPlan: ...
def record_context_telemetry(self, telemetry: ContextTelemetry) -> None: ...
```

The default implementations wrap the current message list into immutable-prefix/ephemeral segments and delegate compression to existing `compress()`. Third-party engines that implement only the old ABC continue working. Provider optimizers receive only an already-valid `CompiledContext`; result validation compares `expected_semantic_hash`, roles/tool pairs, required segment hashes, and cache identity before accepting transport state.

---

### Task 0: Freeze the Exact 100-Replay Contract

**Files:**
- Create: `benchmarks/context_compiler/manifest.yaml`
- Create: `benchmarks/context_compiler/cases.yaml`
- Create: `benchmarks/context_compiler/fixtures/session_software.jsonl`
- Create: `benchmarks/context_compiler/fixtures/mission_research.jsonl`
- Create: `benchmarks/context_compiler/fixtures/knowledge_lifecycle.jsonl`
- Create: `tests/benchmarks/test_context_compiler_benchmark.py`

**Interfaces:**
- Produces version `cache-aware-context-100-v1`, exact 100-case IDs/strata, source/message fixtures, provider/model/tool identity fixtures, pricing/tokenizer snapshots, outcomes, faults, and all gates consumed by Task 10.
- Consumes the Approved Portfolio Contract and portfolio §8.5 archetypes; all fixtures use public/synthetic/disposable content.

- [ ] **RED: write exact denominator and gate tests**

```python
def test_manifest_freezes_100_replays_and_all_gates(load_context_manifest):
    manifest, cases = load_context_manifest()
    assert manifest["version"] == "cache-aware-context-100-v1"
    assert len(cases) == 100
    assert {row["kind"] for row in cases} == {"session", "mission"}
    assert sum(row["kind"] == "session" for row in cases) == 50
    assert sum(row["kind"] == "mission" for row in cases) == 50
    assert manifest["gates"]["uncached_cost_ratio_max"] == 0.70
    assert manifest["gates"]["compile_p95_ms_max"] == 100
    assert manifest["hard_floors"] == {
        "required_assertion_regressions": 0, "identity_drift": 0,
        "role_violations": 0, "stale_required_segments": 0,
        "privacy_authority_violations": 0, "cross_profile_segments": 0,
    }
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_context_compiler_benchmark.py -q`

Expected: FAIL because the manifest, cases, and replay fixtures do not exist.

- [ ] **Implement the frozen fixtures and validation**

Create exactly the stated 20/20/15/15/15/15 archetype split and 50/50 session/mission split. Each case declares transcript fixture/digest, current source versions/hashes, required assertions, freshness canaries, compression points, identity, provider feature lane, retention disclosure/authority, pricing/tokenizer IDs, injected fault, expected baseline, and exclusion policy. Expand the three base JSONL fixtures deterministically by case parameters; validation rejects missing turns, broken roles/tool pairs, under-75%-window cases, mutable paths, private history, missing prices, or unfrozen provider capability.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_context_compiler_benchmark.py -q`

Expected: PASS with exactly 100 digest-valid cases, exact strata, valid roles/tool pairs, complete source freshness/identity facts, and no mutable/unpriced case.

- [ ] **Commit**

```bash
git add benchmarks/context_compiler/manifest.yaml benchmarks/context_compiler/cases.yaml benchmarks/context_compiler/fixtures tests/benchmarks/test_context_compiler_benchmark.py
git commit -m "test: freeze context compiler replay corpus"
```

---

### Task 1: Define Segment Graph, Cache Identity, and Transition Contracts

**Files:**
- Create: `agent/context_segments.py`
- Create: `agent/context_identity.py`
- Create: `tests/agent/test_context_segments.py`
- Create: `tests/agent/test_context_identity.py`

**Interfaces:**
- Produces every exact public dataclass/type above, `canonical_context_hash()`, `build_cache_identity()`, `pin_cache_identity()`, `compare_cache_identity()`, and `validate_transition()`.
- Consumes no source/provider SDK and never serializes credentials or raw payloads into identity.

- [ ] **RED: write graph and independent-identity invariants**

```python
def test_cache_identity_fingerprints_four_parts_independently(identity_factory):
    base = identity_factory()
    changed = identity_factory(tool_description="changed")
    assert base.system_prompt_sha256 == changed.system_prompt_sha256
    assert base.effective_tool_schema_sha256 != changed.effective_tool_schema_sha256
    assert base.provider_sha256 == changed.provider_sha256
    assert base.model_sha256 == changed.model_sha256
    assert base.identity_sha256 != changed.identity_sha256


def test_graph_rejects_dependency_cycle_and_wrong_lane_stability(graph_factory):
    with pytest.raises(ValueError, match="dependency cycle"):
        graph_factory(cycle=True)
    with pytest.raises(ValueError, match="lane/stability"):
        graph_factory(lane="immutable_prefix", stability="turn_hot")
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/test_context_segments.py tests/agent/test_context_identity.py -q`

Expected: FAIL importing the new modules.

- [ ] **Implement immutable records and fail-closed validation**

Implement the public contracts exactly. Segment IDs derive from owner/source/version/kind; content hashes bind rendered content; graph validation enforces four lane/stability mappings, unique IDs, acyclic dependencies, valid source versions, nonnegative measured token cost, freshness, required role anchors, deterministic recomputation for immutable/versioned required segments, and no raw secrets in metadata. `build_cache_identity()` hashes the byte-exact system string, canonical deep-frozen final tool array, normalized provider identity, and exact model ID independently.

`compare_cache_identity()` returns equal or the exact changed components. `validate_transition()` permits an identity change only when reason matches changed components and `new_session_id != old_session_id`; compression rotation may preserve identity and link graph lineage, but cannot disguise a provider/model/schema/prompt change.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_context_segments.py tests/agent/test_context_identity.py -q`

Expected: PASS; deterministic hashes survive key order/process restart, cycles/wrong roles/nonfinite values fail, and no identity component can hide behind the aggregate hash.

- [ ] **Commit**

```bash
git add agent/context_segments.py agent/context_identity.py tests/agent/test_context_segments.py tests/agent/test_context_identity.py
git commit -m "feat: define context segment contracts"
```

---

### Task 2: Extend ContextEngine Compatibly and Pin Prefix/Tools at Session Start

**Files:**
- Modify: `agent/context_engine.py`
- Modify: `agent/agent_init.py`
- Modify: `agent/system_prompt.py`
- Modify: `tools/registry.py`
- Modify: `model_tools.py`
- Create: `tests/agent/test_context_engine_compiler_contract.py`
- Modify: `tests/agent/test_context_engine.py`
- Modify: `tests/agent/test_system_prompt.py`
- Modify: `tests/tools/test_registry.py`
- Modify: `tests/test_get_tool_definitions_cache_isolation.py`

**Interfaces:**
- Produces backward-compatible optional `ContextEngine.build_graph/compile/plan_compression/record_context_telemetry`, per-agent `agent._pinned_cache_identity`, `agent._pinned_system_prompt`, and `agent._pinned_effective_tools`.
- Consumes Task 1 identity/contracts and the already-final `get_tool_definitions()` result after sanitization/tool-search assembly.

- [ ] **RED: write legacy compatibility and schema-freeze tests**

```python
def test_legacy_context_engine_needs_no_new_abstract_methods(legacy_engine_class):
    engine = legacy_engine_class()
    compiled = engine.compile(compile_request(messages=valid_messages()))
    assert compiled.messages == tuple(valid_messages())


def test_dynamic_override_changes_next_conversation_not_current(agent_factory, registry):
    agent = agent_factory()
    before = agent._pinned_effective_tools
    registry.change_dynamic_override("delegate_task", {"description": "new"})
    assert agent._pinned_effective_tools == before
    assert agent_factory()._pinned_effective_tools != before
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/test_context_engine_compiler_contract.py tests/agent/test_context_engine.py tests/agent/test_system_prompt.py tests/tools/test_registry.py tests/test_get_tool_definitions_cache_isolation.py -q`

Expected: FAIL because compiler hooks and pinned identity fields are absent.

- [ ] **Implement compatibility defaults and one-time pinning**

Add concrete optional methods to `ContextEngine`, not abstract methods. Their default graph wraps the pinned system/tools and current messages, default compile returns a deep immutable-equivalent copy, and default compression plan delegates existing `compress()`. After all registry filtering/sanitization/tool-search/context-engine schemas complete in `agent_init`, deep-copy/canonicalize tool definitions, build the system prompt exactly once, compute all four identity fingerprints, and pin them on the agent before the first API request.

Make registry generation metadata observable without mutating pinned agents. Any normal call that would invalidate system prompt or rebuild current tools records `cache_identity_change_required` and routes through explicit session transition; compression calls reuse `_pinned_system_prompt`. Keep legacy tests/imports and third-party context engines operational.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_context_engine_compiler_contract.py tests/agent/test_context_engine.py tests/agent/test_system_prompt.py tests/tools/test_registry.py tests/test_get_tool_definitions_cache_isolation.py tests/run_agent/test_plugin_context_engine_init.py -q`

Expected: PASS; legacy engines compile, current prompt/tools remain byte-identical across registry/plugin/config change, and a new conversation sees the new snapshot.

- [ ] **Commit**

```bash
git add agent/context_engine.py agent/agent_init.py agent/system_prompt.py tools/registry.py model_tools.py tests/agent/test_context_engine_compiler_contract.py tests/agent/test_context_engine.py tests/agent/test_system_prompt.py tests/tools/test_registry.py tests/test_get_tool_definitions_cache_isolation.py
git commit -m "feat: pin conversation cache identity"
```

---

### Task 3: Adapt Conversation, Mission, Knowledge, and Receipt Sources

**Files:**
- Create: `agent/context_sources.py`
- Create: `tests/agent/test_context_sources.py`
- Modify: `agent/turn_context.py`

**Interfaces:**
- Produces `ContextSourceAdapter`, `PrefixSource`, `ConversationSource`, `MissionSource`, `KnowledgeSource`, `ReceiptSource`, `ContextSourceSnapshot`, and `collect_context_sources(request) -> tuple[ContextSourceSnapshot, ...]`.
- Consumes pinned prompt/tools, current messages, item #1 `MissionRecord`/events/review projection, item #4 `KnowledgeMemoryProvider.prefetch()` plus store revision/query time/embedding identity, item #12 `ReceiptStore`/latest observation, and item #6 authority hash.

- [ ] **RED: write ownership, version, and stale-source tests**

```python
def test_sources_reference_owners_without_copying_truth(source_harness):
    snapshots = collect_context_sources(source_harness.request())
    mission = next(s for s in snapshots if s.owner == "mission")
    knowledge = next(s for s in snapshots if s.owner == "knowledge")
    assert mission.source_version == source_harness.mission.updated_at_version
    assert knowledge.source_version == source_harness.knowledge.revision
    assert source_harness.compiler_db_contains_raw_claims is False


def test_changed_required_source_is_recomputed_not_reused(source_harness):
    first = source_harness.collect()
    source_harness.correct_knowledge_canary()
    second = source_harness.collect()
    assert second.knowledge.content_hash != first.knowledge.content_hash
    assert second.knowledge.recompute_required
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/test_context_sources.py -q`

Expected: FAIL because source adapters are absent.

- [ ] **Implement bounded, provenance-preserving adapters**

PrefixSource returns pinned prompt/tool snapshots only. ConversationSource maps existing messages/content parts/tool pairs to anchored ephemeral segments without rewriting. MissionSource renders objective, current status/verdict, active constraints, decisions/blockers/next action, open review items, linked execution versions, and receipt IDs from item #1, bounded and sorted. KnowledgeSource calls item #4's bounded prefetch and preserves claim ID/authority/confidence/valid interval/freshness/source/conflict plus store/query/embedding version. ReceiptSource includes only requested outcome, status, current claim/evidence/artifact digests, uncertainty, and latest observation ID; it never treats receipt content as mission state.

`turn_context.py` attaches rendered snapshot/hot blocks only to the current real user turn through its existing memory/session context construction. No adapter inserts a message, mutates prior content, copies raw owner tables, or writes back to an owner.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_context_sources.py tests/agent/test_memory_provider.py tests/plugins/memory/knowledge/test_provider.py tests/hermes_cli/test_missions_db.py tests/agent/test_receipts.py -q`

Expected: PASS; source correction/review/observation changes only the affected segment, conflicts stay visible, and roles/history/owner stores are untouched.

- [ ] **Commit**

```bash
git add agent/context_sources.py agent/turn_context.py tests/agent/test_context_sources.py
git commit -m "feat: adapt versioned context sources"
```

---

### Task 4: Build and Persist the Four-Lane Compiler Graph

**Files:**
- Create: `plugins/context_engine/cache_aware/__init__.py`
- Create: `plugins/context_engine/cache_aware/plugin.yaml`
- Create: `plugins/context_engine/cache_aware/compiler.py`
- Create: `plugins/context_engine/cache_aware/store.py`
- Create: `tests/agent/test_cache_aware_context_engine.py`
- Modify: `hermes_state.py`

**Interfaces:**
- Produces `CacheAwareContextCompiler(ContextEngine)`, `ContextGraphStore`, `build_graph()`, `compile()`, `plan_compression()`, `recompute_stale_segments()`, `validate_compiled_context()`, and provider-neutral `ContextCompressionPlan`.
- Consumes Tasks 1–3 identities/contracts/sources, existing token estimation/`ContextCompressor`, and profile-local `SessionDB`; provider optimizations are not used yet.

- [ ] **RED: write lane, dependency, budget, and persistence tests**

```python
def test_compile_has_exact_four_lanes_and_never_evicts_required_hot_state(engine, request):
    compiled = engine.compile(request)
    graph = engine.store.get_graph(compiled.graph_hash)
    assert {s.lane for s in graph.segments} == {
        "immutable_prefix", "versioned_snapshots", "hot_state", "ephemeral_payloads",
    }
    required = {s.segment_id for s in graph.segments if s.required}
    assert required <= set(compiled.included_segment_ids)


def test_reopen_recomputes_changed_dependency_without_storing_payload(engine, request):
    first = engine.compile(request)
    engine.crash_close()
    request.knowledge.correct_canary()
    reopened = engine.reopen().compile(request)
    assert first.compiled_hash != reopened.compiled_hash
    assert request.knowledge.segment_id in reopened.recomputed_segment_ids
    assert engine.store.raw_payload_row_count() == 0
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/test_cache_aware_context_engine.py -q`

Expected: FAIL because the cache-aware engine/plugin/store do not exist.

- [ ] **Implement graph construction, budgeting, and recomputation**

Register `CacheAwareContextCompiler` as config-selected engine `cache_aware`. Graph order is deterministic: pinned system, pinned tool array, versioned user/profile snapshot, knowledge snapshots, mission hot state, receipt evidence, then existing conversation/tool segments in original order. Dependencies point from rendered blocks to owner snapshots and from tool results to their assistant tool call. Measure exact tokens with the resolved tokenizer when available and a named conservative estimator otherwise.

Budget in this order: immutable prefix always retained; required fresh versioned/hot segments retained; latest real user turn and unresolved assistant/tool pairs retained; stale/missing required segments recomputed or compilation blocks; optional old tool/media payloads are reduced using existing `ContextCompressor` summarizers; optional snapshot/history segments are ranked by required flag, freshness, priority, dependency fan-out, recency, and token cost. Compilation never omits a dependency while retaining its child and never moves content across role anchors.

Persist additive graph/segment/dependency/transition/telemetry tables containing IDs, hashes, versions, token cost, status, source references, recomputation metadata, and timestamps only. Payload resolution remains with the source/message owner. Graph insertion is content-addressed/idempotent; crash before manifest commit leaves no active graph; crash after commit reopens and revalidates every required source version.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_cache_aware_context_engine.py tests/agent/test_context_compressor.py tests/agent/test_context_compressor_summary_continuity.py -q`

Expected: PASS; all four lanes compile deterministically, budget pressure preserves required fresh state/roles/tool pairs, restart recomputes changed sources, and SQLite contains no raw content.

- [ ] **Commit**

```bash
git add plugins/context_engine/cache_aware/__init__.py plugins/context_engine/cache_aware/plugin.yaml plugins/context_engine/cache_aware/compiler.py plugins/context_engine/cache_aware/store.py hermes_state.py tests/agent/test_cache_aware_context_engine.py
git commit -m "feat: compile four-lane context graphs"
```

---

### Task 5: Integrate Compilation at the Existing Turn and Compression Boundaries

**Files:**
- Modify: `agent/conversation_loop.py`
- Modify: `agent/conversation_compression.py`
- Modify: `agent/context_compressor.py`
- Modify: `run_agent.py`
- Modify: `gateway/session.py`
- Create: `tests/agent/test_context_compiler_compression.py`
- Modify: `tests/agent/test_context_compressor_session_end_clears_state.py`
- Modify: `tests/agent/test_turn_finalizer_interrupt_alternation.py`

**Interfaces:**
- Produces `compile_api_context(agent, messages, *, turn_id) -> CompiledContext`, compression transition persistence, and same-identity session lineage.
- Consumes Task 4 compiler, existing preflight/post-response/overflow/manual compression triggers, locks, session rotation/in-place persistence, and pinned Task 2 prefix/tools.

- [ ] **RED: write boundary, role, and no-prefix-rebuild tests**

```python
def test_every_api_request_uses_compiled_messages_and_pinned_prefix(harness):
    harness.run_three_turns_with_tool_and_compression()
    assert all(req.messages[0]["content"] == harness.pinned_system for req in harness.requests)
    assert all(req.tools == harness.pinned_tools for req in harness.requests)
    assert harness.system_prompt_build_count == 1
    assert all(valid_role_and_tool_alternation(req.messages) for req in harness.requests)


def test_compression_rotation_preserves_identity_and_records_transition(harness):
    before = harness.cache_identity
    transition = harness.force_compression()
    assert transition.reason == "compression_rotation"
    assert transition.old_identity == transition.new_identity == before
    assert transition.old_session_id != transition.new_session_id
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/test_context_compiler_compression.py tests/agent/test_turn_finalizer_interrupt_alternation.py -q`

Expected: FAIL because the loop sends raw messages and compression may rebuild `_cached_system_prompt`.

- [ ] **Wire compilation once per API attempt and preserve host semantics**

Immediately before provider cache decoration/transport conversion, call active engine `compile()` with current real messages, pinned identity/tools, source snapshots, context limit, and monotonic deadline. Send `CompiledContext.messages` plus pinned tools; keep the authoritative in-memory/persisted transcript unchanged except when existing compression succeeds. Repair/retry compiles again only if source/message revision changed.

At `compress_context()`, acquire the existing lock, ask `plan_compression()`, and perform local/provider optimization through later tasks. Remove successful same-conversation prompt rebuilds: reuse `_pinned_system_prompt`, validate `_cached_system_prompt` equality, and record a `compression_rotation` transition whose old/new identity matches. Preserve current session rotation/in-place choice, summary markers, transcript links, compressor cooldown, flush cursors, plugin lifecycle, memory commit, interrupts, and Codex owned-thread boundary.

After compression, validate roles/tool pairs and required segment hashes before persisting/rotating. Failure leaves original transcript/active graph intact and releases the lock. Session end/reset closes graph state only for that session; explicit model/provider/tool/prompt change uses a new session/identity transition, never compression.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_context_compiler_compression.py tests/agent/test_context_compressor.py tests/agent/test_context_compressor_session_end_clears_state.py tests/agent/test_turn_finalizer_interrupt_alternation.py tests/run_agent/test_stream_interrupt_retry.py tests/gateway/test_session.py -q`

Expected: PASS; every request compiles through one boundary, prefix/tools remain pinned, rotations preserve identity lineage, and existing compression/retry/session behavior remains valid.

- [ ] **Commit**

```bash
git add agent/conversation_loop.py agent/conversation_compression.py agent/context_compressor.py run_agent.py gateway/session.py tests/agent/test_context_compiler_compression.py tests/agent/test_context_compressor_session_end_clears_state.py tests/agent/test_turn_finalizer_interrupt_alternation.py
git commit -m "feat: integrate cache-aware context compilation"
```

---

### Task 6: Add Provider Optimizer Registry, Retention Disclosure, and Privacy Gate

**Files:**
- Create: `plugins/context_engine/cache_aware/provider.py`
- Modify: `hermes_cli/plugins.py`
- Create: `tests/agent/test_context_provider_optimizers.py`
- Modify: `tests/hermes_cli/test_plugins.py`

**Interfaces:**
- Produces `register_context_optimizer(optimizer, *, owner, check_fn=None)`, `resolve_context_optimizer(runtime)`, `apply_provider_optimization()`, `ProviderContextOptimizer`, and `PluginContext.register_context_optimizer()`.
- Consumes Task 1 provider contracts, Task 4/5 compression plan, item #6 `StoredAuthorityProvider.authorize(ActionContext, consume=False)`, existing plugin ownership/override checks, and config `context.compiler.provider_optimizations`.

- [ ] **RED: write disclosure, availability, authority, and fallback tests**

```python
def test_unknown_retention_disables_native_optimization(optimizer_harness):
    optimizer_harness.disclosure.training_use = "unknown"
    result = optimizer_harness.apply_at_compression_boundary()
    assert result.path == "hermes_local"
    assert optimizer_harness.provider_calls == 0


def test_unavailable_or_denied_optimizer_falls_back_semantically(optimizer_harness):
    expected = optimizer_harness.local_compiled_hash()
    optimizer_harness.check_fn = lambda: False
    assert optimizer_harness.apply().compiled_hash == expected
    optimizer_harness.check_fn = lambda: True
    optimizer_harness.authority_verdict = "deny"
    assert optimizer_harness.apply().compiled_hash == expected
    assert optimizer_harness.provider_calls == 0
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/test_context_provider_optimizers.py tests/hermes_cli/test_plugins.py -q`

Expected: FAIL because optimizer registration/privacy gates do not exist.

- [ ] **Implement bounded, service-gated optimizers**

Register by normalized provider/API-mode/capability key and plugin owner. Reject invalid protocol, mutable/nondeterministic disclosures, protected override without operator opt-in, optimizer IDs that claim another provider, or capabilities derived from remote prompt text. `check_fn=False` or provider feature loss removes eligibility immediately.

Before native use, render exact disclosure and build item #6 contexts for `model.route` and, when content is provider-stored, `data.share`/`data.remember`, including provider/destination, data class, region, TTL, deletion/training facts, estimated cost, profile/session, and compiled hash. `allow` permits the exact boundary operation without consumption; `ask|deny`, unknown retention/training/region, stale authority, changed disclosure, or audit failure uses local compilation. Stable consent lives in item #6/config, not optimizer state.

Apply only at compression boundary. Validate result semantic hash, required segment/source hashes, cache identity, roles/tool pairs, and provider state scope. Any mismatch/error/downgrade discards provider state and returns the local Hermes result; the optimizer cannot alter the transcript or mark completion.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_context_provider_optimizers.py tests/hermes_cli/test_plugins.py tests/agent/autonomy/test_service.py tests/test_get_tool_definitions_cache_isolation.py -q`

Expected: PASS; disclosure/authority/ownership/service failures all use identical local semantics, valid synthetic optimizers run only at compression, and model tool schemas remain unchanged.

- [ ] **Commit**

```bash
git add plugins/context_engine/cache_aware/provider.py hermes_cli/plugins.py tests/agent/test_context_provider_optimizers.py tests/hermes_cli/test_plugins.py
git commit -m "feat: gate provider context optimizers"
```

---

### Task 7: Implement OpenAI, Google, and Anthropic Adapter Optimizations

**Files:**
- Create: `plugins/context_engine/cache_aware/providers/__init__.py`
- Create: `plugins/context_engine/cache_aware/providers/openai.py`
- Create: `plugins/context_engine/cache_aware/providers/google.py`
- Create: `plugins/context_engine/cache_aware/providers/anthropic.py`
- Modify: `agent/codex_responses_adapter.py`
- Modify: `agent/gemini_native_adapter.py`
- Modify: `agent/anthropic_adapter.py`
- Modify: `agent/chat_completion_helpers.py`
- Modify: `tests/agent/test_context_provider_optimizers.py`

**Interfaces:**
- Produces optimizer IDs `openai.compaction.v1`, `google.state-cache.v1`, and `anthropic.context-editing.v1`; adapter request/result hooks expose capability, opaque state reference, usage, and retention facts.
- Consumes Task 6 registry/privacy/fallback, existing provider clients/transports, and no source-owner API.

- [ ] **RED: write table-driven provider semantic-parity tests**

```python
@pytest.mark.parametrize("provider", ["openai", "google", "anthropic"])
def test_native_optimization_runs_only_at_boundary_and_matches_local(provider, provider_harness):
    before = provider_harness.compile_local(provider)
    assert provider_harness.at_normal_turn(provider).native_calls == 0
    native = provider_harness.at_compression_boundary(provider)
    assert native.semantic_hash == before.semantic_hash
    assert native.cache_identity == before.cache_identity
    assert valid_role_and_tool_alternation(native.messages)


@pytest.mark.parametrize("failure", [
    "feature_unavailable", "state_evicted", "timeout", "malformed_result",
    "required_segment_missing", "retention_changed", "usage_missing",
])
def test_provider_failure_downgrades_to_hermes_without_state_loss(failure, provider_harness):
    result = provider_harness.fail_native(failure)
    assert result.path == "hermes_local"
    assert result.compiled_hash == provider_harness.expected_local_hash
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/test_context_provider_optimizers.py -q`

Expected: FAIL because the three optimizers and adapter hooks are absent.

- [ ] **Implement OpenAI optimization**

For Responses/Codex modes that advertise documented compaction, send the already-compiled boundary input with exact prompt-cache key/identity and request compaction. Treat the returned compacted/state reference as opaque transport state; validate acknowledgement/usage/semantic manifest and retain the local graph/transcript. `store=False` or unsupported endpoints use local Hermes compression. Codex app-server `compact_thread()` remains its existing provider-owned equivalent but records the same plan/result/transition/telemetry contract.

- [ ] **Implement Google optimization**

For Gemini native modes that explicitly report cached-content/Interactions capability, materialize only eligible immutable/versioned segments into provider cached content or state, bind the cache to exact provider/model/system/tool/source hashes and TTL, and keep hot/ephemeral content in the live request. Record `cachedContentTokenCount` through canonical usage. Eviction, model mismatch, state expiry, unsupported endpoint, or missing usage falls back locally; no Google interaction ID becomes source truth.

- [ ] **Implement Anthropic optimization**

Preserve existing `system_and_3` cache-control semantics and, when context editing is explicitly supported, clear/reduce only compiler-selected stale optional tool/context payload segments at the compression boundary. Never edit pinned system/tools, latest real user turn, unresolved tool pairs, or required fresh mission/knowledge/receipt segments. Validate returned/request-side edit manifest before transport. Beta unavailable/changed or retention not authorized uses local Hermes compression plus existing cache markers.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_context_provider_optimizers.py tests/run_agent/test_provider_parity.py tests/agent/test_prompt_builder.py tests/run_agent/test_streaming.py -q`

Expected: PASS across native success, every downgrade, semantic/role/identity validation, and existing provider transport parity.

- [ ] **Commit**

```bash
git add plugins/context_engine/cache_aware/providers agent/codex_responses_adapter.py agent/gemini_native_adapter.py agent/anthropic_adapter.py agent/chat_completion_helpers.py tests/agent/test_context_provider_optimizers.py
git commit -m "feat: optimize provider context boundaries"
```

---

### Task 8: Normalize Cache, Lane, Freshness, Compression, Cost, and Latency Telemetry

**Files:**
- Modify: `plugins/context_engine/cache_aware/store.py`
- Modify: `plugins/context_engine/cache_aware/compiler.py`
- Modify: `agent/conversation_loop.py`
- Modify: `agent/codex_runtime.py`
- Modify: `agent/gemini_native_adapter.py`
- Modify: `agent/anthropic_adapter.py`
- Create: `tests/agent/test_context_compiler_telemetry.py`

**Interfaces:**
- Produces `ContextTelemetry`, `ContextTelemetryStore.append/list`, `normalize_context_usage()`, and `ContextCompilerStatus` consumed by CLI/TUI/benchmark.
- Consumes canonical provider usage (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`), compiled segment estimates, session usage/pricing, compression and provider-plan results.

- [ ] **RED: write canonical accounting and redaction tests**

```python
def test_usage_buckets_are_complete_nonnegative_and_reconcile(telemetry_harness):
    row = telemetry_harness.record(input=1000, cache_read=600, cache_write=100, output=50)
    assert row.uncached_input_tokens == 400
    assert row.input_tokens == row.cache_read_tokens + row.uncached_input_tokens
    assert min(row.input_tokens, row.cache_read_tokens, row.cache_write_tokens) >= 0


def test_telemetry_contains_hashes_not_context_content(telemetry_harness):
    telemetry_harness.record_canary("PRIVATE-CONTEXT-CANARY")
    assert b"PRIVATE-CONTEXT-CANARY" not in telemetry_harness.raw_db_bytes()
    assert telemetry_harness.latest().graph_hash
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/agent/test_context_compiler_telemetry.py -q`

Expected: FAIL because normalized compiler telemetry does not exist.

- [ ] **Implement exact local accounting**

Normalize every provider's reported input/output/cache-read/cache-write/reasoning buckets without guessing absent cache hits. Derive uncached input as `max(0, input-cache_read)` and mark unknown/unreconciled fields explicitly. Join call telemetry to graph/compiled/cache identity/transition/provider plan; record lane token estimates, included/omitted/recomputed counts, freshness age/status, compile/provider/first-token/total latency, compression trigger/path/reclaimed tokens/failure, and session price/cost.

Persist only hashes, counts, enum IDs, opaque source/provider references, timestamps, and uncertainty; no messages, summaries, tool content, claim text, credentials, or provider state payload. Provider usage omission never becomes zero cache cost in the benchmark. Keep existing session usage totals/insights backward compatible.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_context_compiler_telemetry.py tests/agent/test_insights_usage_cost.py tests/run_agent/test_provider_parity.py -q`

Expected: PASS; every known bucket reconciles, unknowns remain explicit, provider lanes normalize consistently, and raw telemetry is content-free.

- [ ] **Commit**

```bash
git add plugins/context_engine/cache_aware/store.py plugins/context_engine/cache_aware/compiler.py agent/conversation_loop.py agent/codex_runtime.py agent/gemini_native_adapter.py agent/anthropic_adapter.py tests/agent/test_context_compiler_telemetry.py
git commit -m "feat: record context compiler telemetry"
```

---

### Task 9: Add Primary CLI and Native Ink Inspect/Explain Controls

**Files:**
- Create: `hermes_cli/context_compiler.py`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/main.py`
- Modify: `cli.py`
- Modify: `tui_gateway/server.py`
- Modify: `ui-tui/src/gatewayTypes.ts`
- Modify: `ui-tui/src/app/slash/commands/session.ts`
- Create: `tests/hermes_cli/test_context_compiler.py`
- Create: `tests/tui_gateway/test_context_compiler_rpc.py`
- Create: `ui-tui/src/__tests__/contextCompilerCommand.test.ts`
- Modify: `ui-tui/src/__tests__/slashParity.test.ts`

**Interfaces:**
- Produces `build_parser()`, `context_command(args) -> int`, `run_argv(argv, output_mode) -> CommandResult`, `run_slash(rest) -> str`, top-level `hermes context`, classic/native `/context`, and JSON-RPC `context.exec`.
- Consumes engine `get_status()`, graph/identity/transition/telemetry stores, provider disclosures, and existing manual `/compress`; controls do not mutate current identity.

- [ ] **RED: write complete command and read-only-current-session tests**

```python
@pytest.mark.parametrize("argv", [
    ["status"], ["identity"], ["lanes"], ["graph", "--latest"],
    ["segment", "seg-1"], ["explain", "--omitted", "seg-2"],
    ["freshness"], ["providers"], ["telemetry", "--last", "20"],
    ["transitions"], ["doctor"], ["benchmark", "--check"],
])
def test_context_cli_surface(parser, argv):
    assert parser.parse_args(["context", *argv]).context_action


def test_inspection_never_rebuilds_prompt_or_tools(cli, live_agent):
    before = live_agent.cache_identity
    cli.run("context graph --latest")
    cli.run("context providers")
    assert live_agent.cache_identity == before
    assert live_agent.prompt_build_count == 1
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_context_compiler.py tests/tui_gateway/test_context_compiler_rpc.py -q`

Expected: FAIL because context compiler command/RPC services are absent.

- [ ] **Implement one shared inspect/explain service**

`status` shows engine/mode/current graph/identity, lane token counts, freshness, cache ratios, last compression, provider optimization/disclosure, cost and uncertainty. `identity` prints the four fingerprints independently and transition reason. `graph/segment/explain/freshness` show opaque source/version/hash/dependency/recompute/budget facts with bounded redacted previews disabled by default. `providers` shows availability plus retention/training/region/TTL/delete disclosure and item #6 verdict. `doctor` validates graph/store/audit/roles/tool pairs/source versions/pinned identity/optimizer capability without compiling or contacting a provider.

Register top-level/classic `/context`; preserve `/compress` as the action boundary. Any configuration/provider/model/tool change reports “new conversation required” and invokes existing explicit new/model/tool session transition only through its owner command. Inspection never refreshes registry, prompt, memory, mission, or provider state.

- [ ] **Add native Ink routing**

`context.exec` runs in the live gateway process and returns bounded structured rows/panels so it sees the current engine/store. Route `/context` natively in `session.ts`; keep mutating `/compress` on its existing native/live path. Dashboard inherits this through embedded Ink; no separate Dashboard editor/status page is needed, and Desktop remains out of scope.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/hermes_cli/test_context_compiler.py tests/tui_gateway/test_context_compiler_rpc.py tests/hermes_cli/test_commands.py -q`

Expected: PASS with profile-local redacted status/explanations and zero current-session identity mutation.

Run: `cd ui-tui && npm test -- --run src/__tests__/contextCompilerCommand.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS; native Ink exposes every inspection route and preserves live engine state.

- [ ] **Commit**

```bash
git add hermes_cli/context_compiler.py hermes_cli/commands.py hermes_cli/main.py cli.py tui_gateway/server.py ui-tui/src/gatewayTypes.ts ui-tui/src/app/slash/commands/session.ts tests/hermes_cli/test_context_compiler.py tests/tui_gateway/test_context_compiler_rpc.py ui-tui/src/__tests__/contextCompilerCommand.test.ts ui-tui/src/__tests__/slashParity.test.ts
git commit -m "feat: add context compiler controls"
```

---

### Task 10: Prove Real-Path Crash, Replay, Freshness, Privacy, and Identity Safety

**Files:**
- Create: `tests/integration/test_cache_aware_context_e2e.py`
- Create: `tests/agent/test_context_compiler_security.py`
- Modify: `tests/agent/test_system_prompt.py`
- Modify: `tests/test_get_tool_definitions_cache_isolation.py`
- Modify: `tests/agent/test_turn_finalizer_interrupt_alternation.py`
- Modify: `tests/hermes_cli/test_profiles.py`

**Interfaces:**
- Produces no production API; this is the release gate for Tasks 1–9.
- Consumes real temp-`HERMES_HOME` config/SQLite/plugin/context-engine discovery, real mission/knowledge/receipt fixtures, real compilation/compression/session rotation/CLI/RPC imports, and fake provider network/process boundaries only.

- [ ] **RED: write the real-path fault and attack matrices**

```python
@pytest.mark.parametrize("fault", [
    "crash_before_graph_commit", "crash_after_graph_before_request",
    "crash_during_local_compression", "crash_after_provider_compaction",
    "duplicate_compression_resume", "stale_mission_version",
    "knowledge_corrected_mid_turn", "receipt_observation_advanced",
    "provider_cache_evicted", "provider_feature_downgraded",
    "provider_usage_missing", "session_rotation_projection_failed",
])
def test_fault_reopens_to_authoritative_fresh_compile(context_e2e, fault):
    result = context_e2e.run_fault(fault)
    assert result.compiled_hash == result.clean_local_replay_hash
    assert result.required_stale_segments == 0
    assert result.identity_drift == 0
    assert result.roles_valid
```

```python
@pytest.mark.parametrize("attack", [
    "prompt_claims_new_identity", "tool_result_changes_schema",
    "plugin_mutates_pinned_tools", "provider_returns_missing_hot_state",
    "provider_state_cross_profile", "forged_cache_hit_usage",
    "knowledge_segment_drops_conflict", "mission_segment_hides_review",
    "receipt_text_claims_verified", "recompute_reads_default_profile",
    "retention_disclosure_changed", "telemetry_contains_secret",
])
def test_attack_never_changes_identity_truth_or_privacy(context_e2e, attack):
    result = context_e2e.attempt_attack(attack)
    assert result.accepted_malicious_provider_state is False
    assert result.cross_profile_reads == 0
    assert result.raw_private_audit_values == 0
    assert result.false_verified_claims == 0
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/integration/test_cache_aware_context_e2e.py tests/agent/test_context_compiler_security.py -q`

Expected: FAIL at each unwired real boundary; correct only the owning Task 1–9 module and preserve all assertions.

- [ ] **Implement the complete real-path E2E harness and invariants**

Create a temporary profile home and real config/state/workflows/knowledge databases; seed a real mission, knowledge correction/conflict, receipt/observation, valid long transcript/tool pairs, and dynamic registry override. Load the cache-aware engine through actual plugin discovery, build an `AIAgent`, run turns/compressions/rotations/restarts, and invoke CLI/TUI services. Provider doubles accept exact adapter payloads and return deterministic native/usage/failure fixtures; they do not replace Hermes stores/compiler.

Independently hash system prompt, effective tool definitions, provider, model, source versions, graph, and compiled request before/after each turn, memory correction, mission update, receipt observation, plugin/schema refresh, compression, native optimization, cache eviction, crash, replay, and session rotation. Assert identity only changes with an explicit new-session transition naming the component; same-identity compression never rebuilds prefix. Validate every API/persisted message sequence and tool pair; no sidecar fields reach model messages. Assert profile isolation, authority-bound retention, redacted metadata, no stale required source, local semantic recovery, and truthful receipt/telemetry language.

- [ ] **Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_context_segments.py tests/agent/test_context_identity.py tests/agent/test_context_sources.py tests/agent/test_cache_aware_context_engine.py tests/agent/test_context_compiler_compression.py tests/agent/test_context_provider_optimizers.py tests/agent/test_context_compiler_telemetry.py tests/agent/test_context_compiler_security.py tests/integration/test_cache_aware_context_e2e.py tests/hermes_cli/test_profiles.py -q`

Expected: PASS; every fault/attack converges to fresh local semantics with stable identity/roles/privacy and no provider-state dependency.

Run: `scripts/run_tests.sh tests/agent/test_system_prompt.py tests/test_get_tool_definitions_cache_isolation.py tests/agent/test_turn_finalizer_interrupt_alternation.py -q`

Expected: PASS with byte-stable prefix/schema/provider/model and strict roles.

- [ ] **Commit**

```bash
git add tests/integration/test_cache_aware_context_e2e.py tests/agent/test_context_compiler_security.py tests/agent/test_system_prompt.py tests/test_get_tool_definitions_cache_isolation.py tests/agent/test_turn_finalizer_interrupt_alternation.py tests/hermes_cli/test_profiles.py
git commit -m "test: prove context compiler invariants"
```

---

### Task 11: Run the 100-Replay Benchmark, Document Rollout, and Define Stops

**Files:**
- Create: `benchmarks/context_compiler/runner.py`
- Create: `benchmarks/context_compiler/score.py`
- Modify: `tests/benchmarks/test_context_compiler_benchmark.py`
- Create: `website/docs/user-guide/features/cache-aware-context.md`
- Create: `website/docs/development/context-compiler-providers.md`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`
- Modify: `website/sidebars.ts`

**Interfaces:**
- Produces `run_replays(manifest_path, cases_path, mode, output_dir)`, `score_replays(baseline_path, candidate_path) -> ContextCompilerReport`, local `results.jsonl`, `report.json`, and `report.md`.
- Consumes Task 0 corpus, Tasks 1–10 implementation, frozen provider response/usage/pricing fixtures, and no outbound telemetry.

- [ ] **RED: write exact scorer and hard-stop tests**

```python
def test_score_requires_all_100_cases_cost_cache_outcomes_and_identity(report_factory):
    report = score_replays(*report_factory(
        cases=100, baseline_uncached_cost=100_000, candidate_uncached_cost=69_000,
        baseline_cache_hit=0.40, candidate_cache_hit=0.61,
    ))
    assert report.candidate_outcome_score >= report.baseline_outcome_score
    assert report.uncached_cost_ratio <= 0.70
    assert report.candidate_cache_hit_rate > report.baseline_cache_hit_rate
    assert report.identity_drift == report.role_violations == report.stale_required_segments == 0


def test_one_outcome_identity_privacy_or_stale_failure_stops(report_factory):
    for failure in (
        {"required_assertion_regressions": 1}, {"identity_drift": 1},
        {"privacy_authority_violations": 1}, {"stale_required_segments": 1},
    ):
        report = score_replays(*report_factory(**failure))
        assert not report.passed
        assert len(report.stop_reasons) == 1
```

- [ ] **Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_context_compiler_benchmark.py -q`

Expected: FAIL because replay runner/scorer/report types are absent.

- [ ] **Implement deterministic baseline/candidate replay and scoring**

Runner validates all frozen hashes, starts each case in a fresh temp profile, replays baseline with current `ContextCompressor` and candidate with `cache_aware`, applies identical source updates/faults/provider fixtures, and records one case row plus per-call telemetry. Capture required assertion results, total/input/cache-read/cache-write/uncached/output/reasoning tokens, provider-priced costs, compile/provider/first-token/total latency, compression triggers/counts/reclaimed tokens/failures, freshness/source versions, graph/compiled hashes, four identity hashes, transition IDs, roles/tool pairs, recovery path, retention disclosure/authority, exclusion/abort, and hardware/network class.

Scorer requires exactly 100 paired rows and all declared calls. It reports per-provider/workflow/archetype/safety slices, p50/p95, sums, paired differences, Wilson intervals for case rates, uncached cost ratio, cache-hit rate, outcome noninferiority, freshness, transition/role/recovery/privacy floors, and unknown usage separately. Missing usage or cases are inconclusive/non-passing; no metric is imputed or threshold relaxed after observation.

- [ ] **Run GREEN on the exact proof**

Run: `python benchmarks/context_compiler/runner.py --manifest benchmarks/context_compiler/manifest.yaml --cases benchmarks/context_compiler/cases.yaml --mode baseline --output .local-proof/context-baseline`

Expected: exits 0 after exactly 100 current-Hermes baseline rows and complete per-call telemetry.

Run: `python benchmarks/context_compiler/runner.py --manifest benchmarks/context_compiler/manifest.yaml --cases benchmarks/context_compiler/cases.yaml --mode candidate --output .local-proof/context-candidate`

Expected: exits 0 after exactly 100 cache-aware rows with identical inputs/source events/provider fixtures.

Run: `python benchmarks/context_compiler/score.py --baseline .local-proof/context-baseline/results.jsonl --candidate .local-proof/context-candidate/results.jsonl --output .local-proof/context-report.md`

Expected: exits 0 only with equivalent/better outcomes, uncached cost ratio `<= 0.70`, strictly higher cache-hit rate overall/nondecreasing supported lanes, p95 latency ceiling, 100% freshness/recovery/roles, and zero identity/privacy/profile hard-floor failures.

- [ ] **Write user, operator, and provider documentation**

The user guide documents the layman outcome, four lanes, source ownership, cache identity, `/context` inspection/explanation, `/compress`, freshness/stale blocks, token/cache/cost/latency fields, provider retention disclosures, explicit new-conversation transitions, crash/downgrade recovery, profile isolation, no Dashboard editor/Desktop dependency, and return to the built-in compressor.

The provider guide defines every public contract, canonical hash, segment/dependency/recompute/freshness rule, role-anchor constraints, token accounting, semantic validation, optimizer ownership/service gating, item #6 disclosure/action contexts, OpenAI/Google/Anthropic boundary mappings, local fallback, telemetry redaction, and temp-`HERMES_HOME` real-path tests. Its complete example is a synthetic standalone optimizer plugin; vendor extensions remain outside core.

Rollout and stop conditions are exact:

1. Land contracts/pinning/store with `context.engine: compressor`; run identity/role/security proof.
2. Enable `cache_aware` in shadow mode for opt-in test profiles; compile/score locally but send baseline context.
3. Enable Hermes-local candidate sending only after all 100 shadow replays match outcomes/freshness/roles/identity.
4. Enable each provider optimizer separately after disclosure/authority and its supported-lane replay gates; unknown/unsupported stays local.
5. Advance toward default only after the full 100-case proof passes on Linux, macOS, and Windows with configured provider lanes and no hard-floor failure.
6. Stop immediately on one required assertion regression, identity drift outside transition, role/tool-pair violation, stale required segment, cross-profile read, privacy/retention authority violation, unredacted telemetry, false verified claim, provider semantic mismatch, or local recovery failure.
7. Keep candidate opt-in rather than default if uncached cost reduction is below 30%, cache-hit rate does not improve, p95 total latency exceeds baseline by more than 10%, compile p95 reaches 100 ms, or compression loops/count regression remain unexplained.
8. Roll back with `context.engine: compressor` and provider optimizations off in `config.yaml`; preserve graph/transition/telemetry metadata for diagnosis, discard provider state refs, keep owner stores/transcripts/receipts intact, and never rewrite prior conversations or delete `state.db`.

- [ ] **Run final verification**

Run: `scripts/run_tests.sh tests/agent/test_context_segments.py tests/agent/test_context_identity.py tests/agent/test_context_sources.py tests/agent/test_cache_aware_context_engine.py tests/agent/test_context_compiler_compression.py tests/agent/test_context_provider_optimizers.py tests/agent/test_context_compiler_telemetry.py tests/agent/test_context_compiler_security.py tests/integration/test_cache_aware_context_e2e.py tests/benchmarks/test_context_compiler_benchmark.py tests/hermes_cli/test_context_compiler.py tests/tui_gateway/test_context_compiler_rpc.py -q`

Expected: PASS.

Run: `cd ui-tui && npm test -- --run src/__tests__/contextCompilerCommand.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS.

Run: `cd website && npm run lint:diagrams && npm run typecheck && npm run build`

Expected: PASS with resolved user/provider/reference links.

Run: `scripts/run_tests.sh`

Expected: full Python suite PASS under CI-parity isolation.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Commit**

```bash
git add benchmarks/context_compiler/runner.py benchmarks/context_compiler/score.py tests/benchmarks/test_context_compiler_benchmark.py website/docs/user-guide/features/cache-aware-context.md website/docs/development/context-compiler-providers.md website/docs/reference/cli-commands.md website/docs/reference/slash-commands.md website/sidebars.ts
git commit -m "docs: roll out cache-aware context"
```

---

## Final Verification Matrix

| Contract | Required proof |
|---|---|
| Four logical lanes | Graph contains immutable prefix, versioned snapshots, hot state, and ephemeral payloads with exact identity/provenance/token/stability/freshness/dependency/recompute metadata |
| Source ownership | Mission, knowledge, receipts, transcript, prompt, and tool registry remain authoritative; compiler stores references/hashes/metrics, not copied truth |
| Cache identity | System prompt/tool schema/provider/model independently fingerprinted and pinned; zero drift outside explicit new-session transition |
| Prefix/schema safety | One prompt build/final schema snapshot per conversation; compression/registry/plugin/memory changes cannot mutate either |
| Conversation validity | Every API/persisted request has strict roles, valid assistant/tool pairs, real latest user turn, and no synthetic mid-loop user message |
| Freshness | Required segment versions/hashes match current mission/knowledge/receipt owners or compilation blocks/recomputes; zero stale required segment |
| Provider semantics | OpenAI/Google/Anthropic run only at compression boundary; local Hermes compile is authoritative and semantically identical after downgrade/eviction/error |
| Privacy | Native retention disclosure is exact and item #6-authorized; unknown/denied uses local mode; no private content in compiler DB/telemetry |
| Recovery | Crash/replay/session rotation/provider state loss reconstructs identical fresh local compiled hash without provider state |
| Telemetry | Tokens/cache/cost/latency/compression/freshness are complete or explicitly unknown, locally stored, redacted, and reconciled |
| 100-replay proof | Exact 100 paired cases; equivalent/better outcomes; `>=30%` uncached cost reduction; improved cache-hit rate; p95 latency gates; zero hard-floor failures |
| Primary UX | Top-level/classic CLI and native Ink inspect/explain/doctor controls; Dashboard relies on embedded Ink; no Desktop dependency |
| Footprint | Rung 1 ContextEngine/prompt/compression extension; optimizers are service-gated plugins/adapters; no new model tool |
| Rollback | `context.engine: compressor` plus optimizer disable preserves owner state/history/receipts and discards only disposable provider state refs |

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-cache-aware-context-compiler.md`. Two execution options:

1. **Subagent-Driven (recommended)** — use `superpowers:subagent-driven-development`, dispatch a fresh worker per task, and review contract then code quality between tasks.
2. **Inline Execution** — use `superpowers:executing-plans`, implement in batches with checkpoints after Tasks 3, 5, 8, and 11.
