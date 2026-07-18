# Tiered Paged Memory and Hybrid Semantic Recall Implementation Plan

> For agentic workers: preserve file-based memory as the source of truth and the frozen system-prompt snapshot. SQLite/FTS/embeddings are derived indexes. Implement the three phases in order; do not make a vector backend a default dependency.

**Goal:** Replace flat write-only overflow with searchable, topic-paged memory and make the builtin memory path prefetch relevant archived memories/session snippets into the existing ephemeral `<memory-context>` channel, with optional BM25+embedding hybrid ranking.

**Architecture:** Add `BuiltinMemoryProvider` around the existing `MemoryStore` and activate it through `MemoryManager` for default installs. Phase 1 makes active/archive entries searchable and wires prefetch without modifying the frozen system prompt. Phase 2 adds topic files and per-entry metadata while retaining atomic file/drift/backup behavior. Phase 3 adds a rebuildable SQLite index and optional embedding sidecar, fused with existing FTS5 by reciprocal-rank fusion. Memory files remain authoritative; skip-memory contexts never prefetch or write.

**Tech Stack:** Python, markdown files/atomic writes/file locks, existing `MemoryProvider`/`MemoryManager`, SQLite/FTS5, existing session FTS, optional configured embedding endpoint or optional sqlite-vec/local model, threat-pattern scanner, daemon pool, learning graph/mutations, and existing memory tool approval gate.

## Global Constraints

- `MEMORY.md`/`USER.md` remain source-of-truth files and retain external-drift guard, atomic writes, backups, and current security limits until migration is complete.
- Never inject recall into the stable/volatile system prompt. All prefetch goes through the current API-time `<memory-context>` user-message block, which is ephemeral and not persisted.
- Default memory prefetch is enabled only when `memory.enabled` is true and `skip_memory` is false. Subagents, background review, cron, and explicitly memory-disabled agents retain current isolation.
- Recalled content is untrusted user-persisted text. Scan active/topic/archive/session snippets with strict threat patterns at recall time, fence it, attach provenance, and omit blocked content.
- Read/search/open actions do not pass through the write approval gate. Add/replace/remove/archive/migration writes keep the existing staged/approval path.
- Archive reads are bounded and paginated. Do not load all archive/topic files into the prompt or tool result.
- Indexes are rebuildable from files/SessionDB. Index failure degrades to current file/FTS behavior; it never blocks a memory write or a turn.
- Embeddings are optional and default-off unless a configured local/approved endpoint is available. No new mandatory dependency or network call from the hot path.
- Prefetch runs on the existing daemon worker and uses a short budget/timeout. A slow provider/index returns no context rather than blocking `run_conversation`.
- Do not conflate builtin memory with an external provider. At most one external provider remains configured; builtin retrieval is the default local provider layer.
- Project-scoped metadata is additive. Global memory remains the default; a project filter must not silently hide existing user memory.
- Tests must use real temp files, SQLite, file locks, and a fake embedding backend with deterministic vectors.

## Current-State Review

The review found the required seams but a dark default path:

- `tools/memory_tool.py` stores `MEMORY.md`/`USER.md` with § entries, atomic batch writes, char caps, drift guard, and optional `memories/archive/{MEMORY,USER}.md`; archive has no readers.
- `agent/memory_manager.py` already provides `prefetch_all`, `queue_prefetch_all`, fenced context assembly, lifecycle hooks, and a single-worker executor, but `agent_init.py` constructs it only when an external provider is configured.
- `MemoryProvider` already defines `prefetch`/`queue_prefetch`, `on_memory_write`, and `on_session_switch`; a builtin implementation is missing.
- `turn_context.py` and `conversation_loop.py` already carry `ext_prefetch_cache` into the API-time user-message copy. This is the cache-safe integration point.
- `hermes_state.py` has versioned migrations and FTS5/trigram message indexes; no memory-entry or embedding tables exist.
- `tools/session_search_tool.py` composes BM25/FTS results but knows nothing about memory archives; `learning_graph` and `learning_mutations` assume simple memory ids.
- `agent/auxiliary_client.py` has per-task provider resolution suitable for optional embedding calls, but the live path must remain local/default-off.

The plan skips replacing markdown with a database, copying external memory providers, and injecting full topic content into every turn.

## Release Order

1. Phase 1: builtin provider, archive/search action, default prefetch.
2. Phase 2: topic files, metadata, paging, index/graph integration.
3. Phase 3: optional embeddings and hybrid ranking.
4. Phase 4: migration, performance, security, and user-facing verification.

## File Map

- Create: `agent/builtin_memory_provider.py` — builtin `MemoryProvider` around `MemoryStore`, retrieval/prefetch lifecycle.
- Create: `agent/memory_retrieval.py` — memory/archived/session candidate retrieval, strict scanning, RRF ranking.
- Create: `agent/memory_topics.py` — topic slug/entry metadata/file paging helpers.
- Modify: `tools/memory_tool.py` — search/open_topic actions, topic-aware writes, archive read helpers.
- Modify: `agent/memory_manager.py` — builtin provider construction, index-write notification, prefetch result limits.
- Modify: `agent/agent_init.py` — always construct builtin manager when memory is enabled; preserve external-provider composition limit.
- Modify: `agent/turn_context.py` and `agent/conversation_loop.py` only for context budget/provenance plumbing.
- Modify: `hermes_state.py` — memory-entry/embedding derived tables and migrations.
- Modify: `tools/session_search_tool.py` — compose memory/archive sources in existing shapes.
- Modify: `agent/auxiliary_client.py` — optional embedding task configuration if needed.
- Modify: `agent/prompt_builder.py` — memory guidance for search/open_topic/paging.
- Modify: `agent/learning_graph.py`, `agent/learning_mutations.py` — topic/metadata node IDs.
- Modify: `hermes_cli/config.py` — memory topics/prefetch/index/embedding defaults.
- Modify: `tools/threat_patterns.py` or recall caller — strict scan reuse only.
- Test: new `tests/agent/test_builtin_memory_provider.py`, `tests/agent/test_memory_retrieval.py`, `tests/agent/test_memory_topics.py`.
- Test: extend `tests/tools/test_memory_tool.py`, `tests/tools/test_memory_tool_schema.py`, `tests/agent/test_memory_provider.py`, `tests/agent/test_memory_boundary_commit.py`, `tests/run_agent/test_commit_memory_session_context_engine.py`.
- Test: new `tests/agent/test_memory_index_e2e.py`, `tests/agent/test_memory_prefetch_e2e.py`.

## Data Contracts

```python
@dataclass(frozen=True)
class MemoryEntry:
    entry_id: str
    store: Literal["MEMORY", "USER"]
    text: str
    topic: str | None
    created_at: float | None
    updated_at: float | None
    provenance: str | None
    confidence: float | None
    project_root: str | None
    archived: bool
```

```python
@dataclass(frozen=True)
class RecallHit:
    source: Literal["active_memory", "topic", "archive", "session"]
    entry_id: str
    text: str
    score: float
    provenance: str
    blocked: bool
```

`memory` tool additions:

```json
{"action":"search","query":"deployment decision","limit":5,"include_archive":true,"project_root":null}
{"action":"open_topic","topic":"deployments","offset":1,"limit":20}
```

Search returns bounded metadata/text previews and a `next_offset`. `open_topic` returns a page and does not mutate prompt/system state.

## Phase 1: Builtin Provider, Archive Search, and Prefetch

### Task 1.1: Implement the builtin provider and activate it safely

**Files:**
- Create: `agent/builtin_memory_provider.py`
- Modify: `agent/memory_manager.py`
- Modify: `agent/agent_init.py`
- Modify: `hermes_cli/config.py`
- Test: `tests/agent/test_builtin_memory_provider.py`
- Test: `tests/agent/test_memory_provider.py`

- [ ] Step 1: Add lifecycle tests that prove default provider creation, skip-memory isolation, and coexistence with one external provider.

```python
def test_builtin_provider_is_created_when_memory_enabled(tmp_path, monkeypatch):
    agent = build_agent(memory_enabled=True, memory_provider=None, hermes_home=tmp_path)
    assert type(agent.memory_manager.providers[0]).__name__ == "BuiltinMemoryProvider"


def test_skip_memory_agent_has_no_builtin_prefetch_or_write(tmp_path):
    agent = build_agent(memory_enabled=True, skip_memory=True, hermes_home=tmp_path)
    assert agent.memory_manager is None or agent.memory_manager.prefetch_all([]) == []
    assert not list((tmp_path / "memories").glob("**/*"))
```

- [ ] Step 2: Run the tests against the current external-provider-only construction.

```bash
python -m pytest tests/agent/test_builtin_memory_provider.py tests/agent/test_memory_provider.py -q
```

- [ ] Step 3: Implement `BuiltinMemoryProvider` with `system_prompt_block` delegating to the existing frozen memory snapshot, `prefetch` delegating to `memory_retrieval`, `queue_prefetch`, `on_memory_write` index notification, and no external network calls.

- [ ] Step 4: Change `agent_init.py` to construct `MemoryManager` with builtin provider whenever memory is enabled. If an external provider is configured, keep builtin as local provider plus the one external provider only if the current manager contract permits that composition; otherwise use builtin retrieval and external lifecycle provider through one manager without adding a second external plugin.

- [ ] Step 5: Add config defaults:

```yaml
memory:
  prefetch:
    enabled: true
    limit: 3
    max_chars: 6000
    timeout_seconds: 0.5
  topics:
    enabled: false
  index:
    enabled: true
  embeddings:
    enabled: false
```

- [ ] Step 6: Run memory lifecycle tests under a temporary home and verify no prompt text changes during an active session write.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_builtin_memory_provider.py \
  tests/agent/test_memory_provider.py \
  tests/agent/test_memory_boundary_commit.py \
  tests/run_agent/test_commit_memory_session_context_engine.py -q
```

- [ ] Step 7: Commit builtin provider activation.

```bash
git add agent/builtin_memory_provider.py agent/memory_manager.py agent/agent_init.py hermes_cli/config.py tests/agent/test_builtin_memory_provider.py tests/agent/test_memory_provider.py
 git diff --cached --check
git commit -m "feat(memory): activate builtin memory provider"
```

### Task 1.2: Add archive/search/open-topic read actions

**Files:**
- Create: `agent/memory_retrieval.py`
- Modify: `tools/memory_tool.py`
- Modify: `toolsets.py` if the schema/toolset description needs the new actions.
- Test: `tests/tools/test_memory_tool.py`
- Test: `tests/tools/test_memory_tool_schema.py`
- Test: `tests/agent/test_memory_retrieval.py`

- [ ] Step 1: Add archive search tests using actual `MemoryStore` writes and `archive_on_overflow`.

```python
def test_archive_search_returns_oldest_overflow_entry(tmp_path, monkeypatch):
    store = make_memory_store(tmp_path, archive_on_overflow=True, max_chars=40)
    store.add("first decision about deploy", target="MEMORY")
    store.add("second decision about deploy", target="MEMORY")
    result = memory_action(store, {"action": "search", "query": "first decision", "include_archive": True})
    assert result["results"][0]["source"] == "archive"
    assert result["results"][0]["text"] == "first decision about deploy"


def test_open_topic_is_bounded_and_read_only(tmp_path):
    store = make_topic_store(tmp_path)
    result = memory_action(store, {"action": "open_topic", "topic": "deployments", "offset": 1, "limit": 1})
    assert len(result["entries"]) == 1
    assert result["next_offset"] == 2
```

- [ ] Step 2: Implement archive readers that parse § entries, preserve source/provenance, and return offset/limit pages. Do not alter archive files during reads.

- [ ] Step 3: Add `search` and `open_topic` schema/actions while keeping add/replace/remove/batch approval behavior unchanged. Reject traversal/absolute paths in topic names through the existing file safety helpers.

- [ ] Step 4: Implement initial retrieval as exact/FTS-style lexical matching over active memory, archive, and (when available) session FTS. Apply strict threat scan and omit blocked hits with a diagnostic count.

- [ ] Step 5: Add tests for empty archive, malformed entry, sensitive topic path, blocked recalled text, pagination, and write-gate preservation.

- [ ] Step 6: Run focused memory tests.

```bash
python -m pytest \
  tests/tools/test_memory_tool.py \
  tests/tools/test_memory_tool_schema.py \
  tests/agent/test_memory_retrieval.py -q
```

- [ ] Step 7: Commit searchable archive/read actions.

```bash
git add agent/memory_retrieval.py tools/memory_tool.py toolsets.py tests/tools/test_memory_tool.py tests/tools/test_memory_tool_schema.py tests/agent/test_memory_retrieval.py
git diff --cached --check
git commit -m "feat(memory): make archive searchable"
```

### Task 1.3: Wire safe automatic prefetch

**Files:**
- Modify: `agent/builtin_memory_provider.py`
- Modify: `agent/memory_manager.py`
- Modify: `agent/turn_context.py`
- Modify: `agent/conversation_loop.py` only for provenance/budget handling.
- Test: `tests/agent/test_memory_prefetch_e2e.py`
- Test: `tests/agent/test_memory_provider.py`

- [ ] Step 1: Add a fake-provider/real-session test that writes a memory, starts a new turn with a matching query, and asserts the context appears only in the API-time user-message copy.

```python
def test_builtin_prefetch_enters_ephemeral_memory_context_only(tmp_path):
    agent = build_agent(hermes_home=tmp_path, memory_enabled=True)
    agent.memory_store.add("deployment uses blue-green", target="MEMORY")
    turn = run_turn_with_fake_provider(agent, "How do we deploy?")
    assert "deployment uses blue-green" in turn.api_user_message
    assert "deployment uses blue-green" not in turn.system_prompt
    assert "deployment uses blue-green" not in turn.persisted_user_message
```

- [ ] Step 2: Use `TurnContext.ext_prefetch_cache` and `build_memory_context_block` exactly as existing external providers do. The builtin provider returns at most configured top-k/char budget, with source/provenance and `[memory-context]` fencing.

- [ ] Step 3: Derive the query from the latest real user message only; do not prefetch on tool-result-only iterations or from synthetic compression/verification messages. Keep an in-turn cache keyed by user-message hash.

- [ ] Step 4: Add timeout/error handling through the existing memory manager daemon worker. A failed index/scan returns an empty list and records a diagnostic, not a turn exception.

- [ ] Step 5: Verify subagents/background review/cron with `skip_memory` do not receive builtin recall.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_memory_prefetch_e2e.py \
  tests/agent/test_memory_provider.py \
  tests/agent/test_memory_skill_scaffolding.py -q
```

- [ ] Step 6: Commit cache-safe prefetch.

```bash
git add agent/builtin_memory_provider.py agent/memory_manager.py agent/turn_context.py agent/conversation_loop.py tests/agent/test_memory_prefetch_e2e.py tests/agent/test_memory_provider.py
git diff --cached --check
git commit -m "feat(memory): prefetch builtin recall safely"
```

## Phase 2: Topic Files, Metadata, and Derived Index

### Task 2.1: Add topic files and entry metadata

**Files:**
- Create: `agent/memory_topics.py`
- Modify: `tools/memory_tool.py`
- Modify: `hermes_cli/config.py`
- Test: `tests/agent/test_memory_topics.py`
- Test: `tests/tools/test_memory_tool.py`

- [ ] Step 1: Add topic write/read tests with drift and atomicity.

```python
def test_topic_write_is_atomic_and_external_drift_is_detected(tmp_path):
    store = make_memory_store(tmp_path, topics_enabled=True)
    store.add("deploy uses blue-green", target="MEMORY", topic="deployments", provenance="user", confidence=0.9)
    assert "deployments" in store.list_topics()
    (tmp_path / "memories/topics/deployments.md").write_text("external edit")
    assert store.add("another", target="MEMORY", topic="deployments") ["status"] == "error"
```

- [ ] Step 2: Implement topic slug validation, per-entry metadata headers, atomic append/replace/remove, and file locks by topic. Reuse `MemoryStore._file_lock`, drift snapshots, and `atomic_write`; do not introduce a new write format outside the memory directory.

- [ ] Step 3: Preserve the existing always-injected `MEMORY.md` index. When a topic is supplied, write a compact index entry with topic/entry id and full text to the topic file; archive overflow remains lossless and searchable.

- [ ] Step 4: Add migration behavior for existing § entries: default topic `general`, null provenance/confidence/timestamps, and no automatic rewrite unless `memory.topics.migrate_on_write` is enabled. A migration command can rewrite files under the normal write gate.

- [ ] Step 5: Extend learning graph/mutations only after stable ids exist. Use `memory:<store>:<topic>:<entry_id>`; old `memory:<store>:<index>` ids continue to resolve.

- [ ] Step 6: Run topic/drift/graph tests.

```bash
python -m pytest tests/agent/test_memory_topics.py tests/tools/test_memory_tool.py tests/agent/test_learning_graph.py -q
```

- [ ] Step 7: Commit topic paging.

```bash
git add agent/memory_topics.py tools/memory_tool.py hermes_cli/config.py agent/learning_graph.py agent/learning_mutations.py tests/agent/test_memory_topics.py tests/tools/test_memory_tool.py tests/agent/test_learning_graph.py
git diff --cached --check
git commit -m "feat(memory): add topic-file paging"
```

### Task 2.2: Build the rebuildable SQLite memory index

**Files:**
- Modify: `hermes_state.py`
- Modify: `agent/memory_manager.py`
- Modify: `agent/memory_retrieval.py`
- Modify: `tools/session_search_tool.py`
- Test: new `tests/agent/test_memory_index_e2e.py`

- [ ] Step 1: Add a temp-home test that writes active/topic/archive entries, rebuilds the index, closes/reopens SessionDB, and retrieves all three sources.

```python
def test_memory_index_rebuilds_from_files_and_archive(tmp_path, monkeypatch):
    db = open_db(tmp_path)
    store = make_memory_store(tmp_path, archive_on_overflow=True, topics_enabled=True)
    write_fixture_entries(store)
    rebuild_memory_index(db, store)
    db.close()
    reopened = open_db(tmp_path)
    hits = search_memory(reopened, "blue-green", include_archive=True)
    assert {hit.source for hit in hits} == {"active_memory", "topic", "archive"}
```

- [ ] Step 2: Add versioned `memory_entries` and `memory_fts` tables with entry id/store/topic/provenance/project/archived/text and FTS triggers. Add `memory_index_meta` with source file digest/mtime and schema version.

- [ ] Step 3: Mirror writes through `MemoryManager.notify_memory_tool_write`; queue indexing on the existing daemon pool and coalesce multiple writes. A failed index write marks dirty and is recoverable by rebuild.

- [ ] Step 4: Extend `session_search` DISCOVER composition to include memory hits only when the query requests memory/archive or the builtin prefetch path calls the retrieval API. Preserve current output shapes/bookends for message hits.

- [ ] Step 5: Add per-project filter and archive/topic source filters with fail-open-to-more-results behavior only for missing metadata; never cross a profile boundary.

- [ ] Step 6: Run index/rebuild/FTS corruption tests with real SQLite.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_memory_index_e2e.py \
  tests/tools/test_session_search_tool.py \
  tests/agent/test_memory_retrieval.py -q
```

- [ ] Step 7: Commit the derived index.

```bash
git add hermes_state.py agent/memory_manager.py agent/memory_retrieval.py tools/session_search_tool.py tests/agent/test_memory_index_e2e.py tests/tools/test_session_search_tool.py tests/agent/test_memory_retrieval.py
git diff --cached --check
git commit -m "feat(memory): add rebuildable memory index"
```

## Phase 3: Optional Hybrid Embedding Recall

### Task 3.1: Add optional embedding sidecar and RRF ranking

**Files:**
- Create: `agent/memory_embeddings.py`
- Modify: `hermes_state.py`
- Modify: `agent/memory_retrieval.py`
- Modify: `agent/auxiliary_client.py` only for configured task resolution.
- Modify: `hermes_cli/config.py`
- Test: new `tests/agent/test_memory_embeddings.py`
- Test: `tests/agent/test_memory_retrieval.py`

- [ ] Step 1: Add deterministic fake-embedding tests.

```python
def test_rrf_fuses_bm25_and_embedding_rankings():
    hits = hybrid_rank(
        bm25=["a", "b"],
        semantic=["b", "c"],
        k=60,
    )
    assert [hit.entry_id for hit in hits][:2] == ["b", "a"]


def test_embedding_failure_returns_bm25_only():
    backend = FailingEmbeddingBackend()
    assert search_with_backend("deploy", backend).mode == "lexical"
```

- [ ] Step 2: Implement a backend protocol with deterministic test implementation, optional configured local endpoint/auxiliary task, and no default network. Store vectors as rebuildable blobs/sidecar rows keyed by entry id/source digest.

- [ ] Step 3: Queue embedding generation after writes on the existing daemon pool; cap batch size, retry once, and mark failed rows. Never embed blocked/redacted content.

- [ ] Step 4: Implement BM25+semantic reciprocal-rank fusion with a stable tie-breaker by updated_at/entry id. Expose retrieval mode/diagnostics without leaking backend credentials.

- [ ] Step 5: Add config opt-in validation for backend/provider/model and context dimensions. If unavailable, startup remains successful and lexical retrieval remains active.

- [ ] Step 6: Run embedding/index tests with the fake backend and a disabled-backend test.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest tests/agent/test_memory_embeddings.py tests/agent/test_memory_retrieval.py -q
```

- [ ] Step 7: Commit optional hybrid retrieval.

```bash
git add agent/memory_embeddings.py hermes_state.py agent/memory_retrieval.py agent/auxiliary_client.py hermes_cli/config.py tests/agent/test_memory_embeddings.py tests/agent/test_memory_retrieval.py
git diff --cached --check
git commit -m "feat(memory): add optional hybrid semantic recall"
```

## Phase 4: Guidance, Migration, and Full Verification

### Task 4.1: Update guidance and user-facing configuration

**Files:**
- Modify: `agent/prompt_builder.py`
- Modify: `tools/memory_tool.py` descriptions/schema.
- Modify: `hermes_cli/config.py`, example config, and memory docs.
- Test: `tests/tools/test_memory_tool_schema.py`, `tests/agent/test_prompt_builder.py`.

- [ ] Step 1: Replace consolidate-or-die guidance with explicit search/open_topic/paging guidance, including when to write a topic and when not to trust recalled content.

- [ ] Step 2: Keep the frozen memory index concise. Topic names/ids may appear in the stable snapshot only if they are part of the persisted index; retrieved text remains ephemeral.

- [ ] Step 3: Document archive retrieval, topic migration, prefetch limits, skip-memory isolation, optional embeddings, rebuild/diagnostic commands, and data trust boundaries.

- [ ] Step 4: Run schema/prompt-cache tests.

```bash
python -m pytest tests/tools/test_memory_tool_schema.py tests/agent/test_prompt_builder.py -q
```

- [ ] Step 5: Commit guidance/configuration.

```bash
git add agent/prompt_builder.py tools/memory_tool.py hermes_cli/config.py cli-config.yaml.example docs website tests/tools/test_memory_tool_schema.py tests/agent/test_prompt_builder.py
git diff --cached --check
git commit -m "docs(memory): document paged recall"
```

### Task 4.2: End-to-end performance/security gate

**Files:**
- Test: `tests/agent/test_memory_prefetch_e2e.py`
- Test: `tests/agent/test_memory_index_e2e.py`
- Test: `tests/agent/test_memory_embeddings.py`
- Test: memory lifecycle/skip-memory suites.

- [ ] Run a real temporary profile scenario: write active memory, overflow to archive, add a topic, close/reopen, rebuild index, run a matching turn, and verify top-k ephemeral context plus `next_offset` tool paging.
- [ ] Modify an archive/topic file externally and verify drift handling refuses a conflicting write but still permits safe read/search.
- [ ] Store prompt-injection text in archive/session memory and verify strict recall scanning omits it from prefetch and tool results.
- [ ] Run subagent/background-review/cron turns with skip-memory and verify no recall/index writes.
- [ ] Run disabled embeddings, fake embeddings, and failed embeddings; verify lexical fallback and no turn latency/blocking regression.
- [ ] Measure prefetch wall time and output budget against configured limits.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/tools/test_memory_tool.py \
  tests/tools/test_memory_tool_schema.py \
  tests/agent/test_builtin_memory_provider.py \
  tests/agent/test_memory_provider.py \
  tests/agent/test_memory_prefetch_e2e.py \
  tests/agent/test_memory_topics.py \
  tests/agent/test_memory_index_e2e.py \
  tests/agent/test_memory_embeddings.py \
  tests/agent/test_memory_boundary_commit.py \
  tests/agent/test_memory_skill_scaffolding.py \
  tests/agent/test_prompt_builder.py -q
python3 -m compileall -q agent/builtin_memory_provider.py agent/memory_retrieval.py agent/memory_topics.py agent/memory_embeddings.py
 git diff --check
```

- [ ] Commit the evidence tests and migration documentation.

```bash
git add tests/agent/test_memory_prefetch_e2e.py tests/agent/test_memory_index_e2e.py tests/agent/test_memory_embeddings.py docs
 git diff --cached --check
git commit -m "test(memory): verify tiered recall end to end"
```

## Acceptance Checklist

- [ ] Default memory-enabled agents construct a builtin provider; skip-memory contexts remain isolated.
- [ ] Archive overflow is searchable and paginated without weakening write approvals or drift protection.
- [ ] Topic files preserve source-of-truth/atomic/lock/backup semantics and have stable ids.
- [ ] Derived SQLite/FTS indexes rebuild from files and degrade safely when unavailable.
- [ ] Prefetch enters only ephemeral API-time memory context, with strict scanning, provenance, and budgets.
- [ ] Session search can compose memory/archive hits without breaking message result shapes.
- [ ] Optional embeddings are disabled by default, local/approved when enabled, incrementally maintained, and fall back to BM25.
- [ ] Learning graph/mutations and prompt guidance handle topic ids and paging.
- [ ] Memory writes, external edits, subagent isolation, and cross-profile boundaries are tested with real state.

## Deliberate Simplifications

- Skipped replacing markdown files with a database; SQLite is a rebuildable index, not authority.
- Skipped mandatory vectors; lexical archive/prefetch delivers the core value without a new dependency or provider outage.
- Skipped automatic full-session embedding backfill; index new/changed entries first, add a bounded offline rebuild command only when retrieval evaluation justifies it.
- Skipped injecting topics/full memories into the system prompt; cache stability is more valuable than a larger always-on index.
