# Sleep-Time Memory Consolidation (“Dreaming”) Implementation Plan

> For agentic workers: build this as a sibling of the existing skill Curator, not as a second memory engine. The plan assumes A6’s searchable archive/topic contract; if that contract is absent, dreaming remains disabled rather than inventing another archive format.

**Goal:** Use idle time to review recent sessions and memory files, identify duplicates/contradictions/stale facts, archive rather than delete low-confidence entries, and distill unreviewed session episodes into staged memory updates with rollback/reporting.

**Architecture:** Create `agent/memory_curator.py` by reusing Curator idle gates, restricted fork, report, and backup patterns. A `memory_curator` run snapshots the memory directory, gathers bounded memory/session candidates with `session_search`, runs a memory-only auxiliary agent, validates proposed writes through existing `MemoryStore` threat/drift/write-approval gates, and applies one atomic batch per store/topic. Default mode is dry-run/staged; automatic consolidation is opt-in. The builtin provider from A6 receives end-of-session extraction only through this controlled path; external providers remain skipped.

**Tech Stack:** `agent/curator.py`, `agent/curator_backup.py`, `agent/background_review.py`, `tools/memory_tool.py`, `tools/write_approval.py`, `tools/session_search_tool.py`, `hermes_state.SessionDB`, `MemoryStore` topic/archive support, auxiliary model routing, CLI/gateway idle ticks, and existing skill/curator CLI/report conventions.

## Global Constraints

- Depends on A6’s `BuiltinMemoryProvider`, searchable archive/topic format, and stable memory entry ids. Do not duplicate archive parsing or index tables.
- First run after upgrade is deferred; no automatic mutation on deployment/startup.
- Default `memory_curator.consolidate=false` and `dry_run=true`. A user must explicitly enable automatic staged/apply behavior.
- Never hard-delete a memory entry. Duplicate/contradictory/stale entries are archived with provenance and a report; rollback restores the pre-run snapshot.
- Legacy entries with missing provenance/timestamps are flagged, not auto-resolved by “newest wins.”
- Session transcript content is attacker-influenced. The restricted agent may read bounded session/search results, but every proposed write still passes strict threat scanning and background-origin write approval. By default, distilled additions are staged for user review.
- `skip_memory=True` on the fork prevents external provider activity and memory-context leakage. The fork gets only `memory` and read-only `session_search`; no terminal/execute/delegation/skills/cron/network tool.
- A run reads a snapshot and writes one atomic batch per target file/topic under the existing file lock/drift guard. Concurrent live writes cause a safe abort/retry, not partial interleaving.
- Use a single-flight lock/state file per profile. A stuck run cannot block future sessions indefinitely; lease TTL/recovery follows the existing Curator pattern.
- Cost, session count, and output size are bounded. No LLM call runs on every tick or every session.
- Existing per-turn memory nudges remain; dreaming complements them and must not spawn a second review for the same session/run id.

## Current-State Review

- `agent/curator.py` has idle/interval gates, background fork/runtime resolution, skill backup/reporting, and CLI status/run/pause/rollback.
- `tools/memory_tool.MemoryStore` has exact dedupe, archive overflow, file locks, drift guard, threat scan, and write approval, but no consolidation curator or archive review action before A6.
- `agent/memory_manager.commit_session_boundary_async` only extracts through external providers; builtin session-end extraction is the gap A6/A10 close together.
- The `on_session_end` hook exposed from `agent/turn_finalizer.py` runs at the end of each `run_conversation()` call, not only at a true `/new` or session-close boundary. Never enqueue a dreaming record directly from that hook without an idempotent session watermark; use the existing true boundary path for short-session capture.
- `agent/background_review.py` already has the restricted fork and `write_origin="background_review"` pattern, but its interval nudges do not review whole memory stores or short/post-nudge sessions.
- `agent/curator_backup.py` snapshots skills/cron only and must generalize to a supplied memory root without weakening existing skill rollback.
- `hermes_state`/`session_search` expose recent sessions and anchored windows suitable for bounded unreviewed-episode input.
- `learning_mutations` has position-based memory compatibility concerns; stable A6 topic ids must be used and old ids tested.

The plan skips embeddings/semantic contradiction detection until A6’s retrieval layer is available; lexical/candidate matching plus auxiliary review is the minimum useful path.

## Release Order

1. Snapshot/state/idle gate and dry-run report.
2. Restricted memory-only review fork and proposal validation.
3. Archive/merge/apply/rollback with approval staging.
4. Session-end extraction and pending-review queue integration.
5. CLI/gateway surfaces, dedup, and full verification.

## File Map

- Create: `agent/memory_curator.py` — scheduler state, candidate collection, review prompt, apply/report lifecycle.
- Modify: `agent/curator_backup.py` — generic root snapshot/rollback helpers, retain skill wrappers.
- Modify: `tools/memory_tool.py` — curator-only archive/metadata action if A6 does not already provide it.
- Modify: `tools/write_approval.py` — ensure memory-curator origin stages writes and reports `staged` vs `success`.
- Modify: `agent/memory_manager.py` — queue builtin session-end extraction signal without external-provider duplication.
- Modify: `agent/turn_finalizer.py` — add bounded pending-review/session candidate marker if needed.
- Modify: `cli.py` — use the true `/new` boundary snapshot/commit path for the pending-session marker; do not treat every turn finalizer call as a session close.
- Modify: `gateway/run.py` — idle tick alongside skill Curator.
- Modify: `hermes_cli/curator.py` or create `hermes_cli/memory_curator.py` — run/status/pause/rollback/dry-run commands.
- Modify: `gateway/slash_commands.py`, `tui_gateway/server.py` — memory curator status/review actions.
- Modify: `hermes_cli/config.py`, example config/docs.
- Test: new `tests/agent/test_memory_curator.py`, `tests/agent/test_memory_curator_backup.py`, `tests/agent/test_memory_curator_e2e.py`.
- Test: extend `tests/agent/test_curator_activity.py`, `tests/agent/test_curator_backup.py`, `tests/agent/test_memory_boundary_commit.py`, `tests/tools/test_write_approval.py`, and session-search tests.

## Data Contracts

```python
@dataclass(frozen=True)
class MemoryCuratorCandidate:
    entry_id: str | None
    source: Literal["active", "topic", "archive", "session"]
    text: str
    provenance: str | None
    created_at: float | None
    session_id: str | None
    confidence: float | None
```

```python
@dataclass(frozen=True)
class MemoryCuratorAction:
    action: Literal["keep", "merge", "archive", "flag", "add"]
    target_ids: tuple[str, ...]
    text: str | None
    topic: str | None
    reason: str
    confidence: float
    source_ids: tuple[str, ...]
```

```python
@dataclass(frozen=True)
class MemoryCuratorRun:
    run_id: str
    started_at: float
    finished_at: float | None
    status: Literal["dry_run", "staged", "applied", "blocked", "failed", "rolled_back"]
    candidate_count: int
    action_count: int
    staged_count: int
    backup_path: str | None
    report_path: str
    source_session_ids: tuple[str, ...]
```

Config:

```yaml
memory_curator:
  enabled: false
  interval_hours: 168
  min_idle_hours: 2
  dry_run: true
  consolidate: false
  max_sessions: 20
  max_candidates: 100
  archive_stale_after_days: 90
  auxiliary:
    provider: null
    model: null
```

## Task 1: State, Idle Gate, and Memory Backup

**Files:**
- Create: `agent/memory_curator.py`
- Modify: `agent/curator_backup.py`
- Modify: `hermes_cli/config.py`
- Test: `tests/agent/test_memory_curator.py`
- Test: `tests/agent/test_memory_curator_backup.py`

- [ ] Step 1: Add state/idle tests.

```python
def test_memory_curator_defers_first_run_and_requires_idle(tmp_path):
    state = MemoryCuratorState(last_run_at=None, first_seen_at=100.0)
    assert should_run_memory_curator(state, now=101.0, idle_seconds=10000, config=enabled_config()) is False
    state = state.after_first_tick()
    assert should_run_memory_curator(state, now=1000.0, idle_seconds=8000, config=enabled_config()) is True


def test_single_flight_lease_recovers_after_ttl(tmp_path):
    store = MemoryCuratorStateStore(tmp_path / ".curator_state")
    assert store.acquire(run_id="a", now=1.0, ttl=30) is True
    assert store.acquire(run_id="b", now=2.0, ttl=30) is False
    assert store.acquire(run_id="b", now=32.0, ttl=30) is True
```

- [ ] Step 2: Add `memory_curator` config/state under `memories/.curator_state`, preserving profile isolation and deferred first-run behavior. Reuse Curator state serialization/locking, not a second scheduler.

- [ ] Step 3: Generalize `curator_backup` to snapshot a supplied root with tar.gz + manifest + keep-N pruning. Existing skill snapshot/rollback output must remain byte-compatible. Memory backup includes MEMORY/USER/topics/archive/index metadata/state but excludes live secret/provider directories.

- [ ] Step 4: Implement `snapshot_memories(profile_home, run_id)` and `rollback_memories(backup_path)` with “snapshot current before rollback” safety. Make rollback report the restored manifest/digest.

- [ ] Step 5: Run backup/idle tests with real temp files/locks.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest tests/agent/test_memory_curator.py tests/agent/test_memory_curator_backup.py tests/agent/test_curator_backup.py -q
```

- [ ] Step 6: Commit state/backup foundation.

```bash
git add agent/memory_curator.py agent/curator_backup.py hermes_cli/config.py tests/agent/test_memory_curator.py tests/agent/test_memory_curator_backup.py tests/agent/test_curator_backup.py
git diff --cached --check
git commit -m "feat(memory): add dreaming state and backups"
```

## Task 2: Bounded Candidate Collection and Restricted Review Fork

**Files:**
- Modify: `agent/memory_curator.py`
- Modify: `agent/background_review.py` only to expose the restricted-fork recipe if necessary.
- Modify: `tools/session_search_tool.py` only for a bounded curator read shape if necessary.
- Test: `tests/agent/test_memory_curator.py`
- Test: session-search/whitelist tests.

- [ ] Step 1: Add candidate-bound tests.

```python
def test_candidate_collection_caps_sessions_and_chars(temp_profile):
    create_sessions(temp_profile, count=30, with_large_messages=True)
    candidates = collect_memory_curator_candidates(temp_profile, max_sessions=3, max_candidates=5, max_chars=1000)
    assert len(candidates) <= 5
    assert sum(len(candidate.text) for candidate in candidates) <= 1000


def test_curator_fork_has_only_memory_and_session_search():
    spec = build_memory_curator_fork_spec()
    assert spec.tool_whitelist == {"memory", "session_search"}
    assert spec.skip_memory is True
    assert spec.platform == "memory_curator"
```

- [ ] Step 2: Collect active/topic/archive entries through A6 retrieval APIs and recent unreviewed sessions through existing `session_search`/SessionDB anchored views. Exclude subagent/tool/cron sources unless the session is explicitly eligible; record source ids.

- [ ] Step 3: Build a bounded `DREAM_REVIEW_PROMPT` that asks for structured `MemoryCuratorAction` JSON. Include explicit rules: missing provenance is flag-only, never delete, preserve user facts, distinguish contradiction from task-specific temporary state, and write only durable reusable facts.

- [ ] Step 4: Fork a quiet auxiliary agent using `skip_memory=True`, `skip_context_files=True`, `platform="memory_curator"`, no nudges, and a thread whitelist containing only memory/session_search. Bind a freshly loaded `MemoryStore`/builtin retrieval snapshot; do not reuse the parent’s mutable prompt or external provider.

- [ ] Step 5: Validate model output with strict schema, max actions, target-id existence, topic/path safety, confidence range, and source-id membership. Invalid output becomes a failed/dry-run report, not a write.

- [ ] Step 6: Run candidate/fork tests and commit.

```bash
python -m pytest \
  tests/agent/test_memory_curator.py \
  tests/tools/test_session_search_tool.py \
  tests/agent/test_memory_skill_scaffolding.py -q
 git add agent/memory_curator.py agent/background_review.py tools/session_search_tool.py tests/agent/test_memory_curator.py
 git diff --cached --check
git commit -m "feat(memory): add restricted dreaming review"
```

## Task 3: Merge, Contradiction, Archive, and Approval-Staged Apply

**Files:**
- Modify: `agent/memory_curator.py`
- Modify: `tools/memory_tool.py`
- Modify: `tools/write_approval.py`
- Modify: `agent/memory_manager.py`
- Test: `tests/agent/test_memory_curator.py`
- Test: `tests/tools/test_write_approval.py`
- Test: `tests/tools/test_memory_tool.py`

- [ ] Step 1: Add pure action-planning tests.

```python
def test_missing_provenance_is_flagged_not_auto_merged():
    actions = plan_memory_actions([entry("old", provenance=None), entry("new", provenance="user", created_at=2.0)])
    assert actions[0].action == "flag"


def test_duplicate_entries_merge_and_archive_old_ids():
    actions = plan_memory_actions([entry("a", "user", text="uses blue green"), entry("b", "user", text="uses blue green")])
    assert actions[0].action == "merge"
    assert set(actions[0].target_ids) == {"a", "b"}


def test_contradiction_prefers_explicit_user_provenance_but_archives_old():
    actions = plan_memory_actions([entry("old", "background_review", text="uses red"), entry("new", "user", text="uses blue")])
    assert actions[0].action == "merge"
    assert "old" in actions[0].target_ids
```

- [ ] Step 2: Implement deterministic prefilters before LLM actions: exact duplicate/normalized duplicate grouping, metadata/provenance grouping, age candidates, and topic scope. LLM is for semantic merge/contradiction judgment, not file selection from unbounded text.

- [ ] Step 3: Apply actions through one `MemoryStore.apply_batch` per target store/topic. `keep` no-op; `merge` writes a new/updated canonical entry and archives superseded entries; `archive` moves to A6 archive with provenance/reason; `flag` writes report only; `add` uses background-origin write gate.

- [ ] Step 4: Ensure `write_approval` distinguishes `staged` from `success`. When policy requires approval, persist a review item with run/action/source ids; do not let `MemoryManager` mirror an uncommitted staged write to external providers.

- [ ] Step 5: Snapshot before apply, record backup/report path, and on any drift/partial failure stop before the next target. Never continue with a possibly stale snapshot.

- [ ] Step 6: Add archive action/readback tests and threat-pattern tests for distilled prompt-injection text.

```bash
python -m pytest \
  tests/agent/test_memory_curator.py \
  tests/tools/test_write_approval.py \
  tests/tools/test_memory_tool.py \
  tests/agent/test_memory_boundary_commit.py -q
```

- [ ] Step 7: Commit safe apply.

```bash
git add agent/memory_curator.py tools/memory_tool.py tools/write_approval.py agent/memory_manager.py tests/agent/test_memory_curator.py tests/tools/test_write_approval.py tests/tools/test_memory_tool.py tests/agent/test_memory_boundary_commit.py
git diff --cached --check
git commit -m "feat(memory): stage dreaming memory changes safely"
```

## Task 4: Session-End Extraction and Idle Tick Integration

**Files:**
- Modify: `agent/memory_manager.py`
- Modify: `agent/turn_finalizer.py`
- Modify: `cli.py`
- Modify: `gateway/run.py`
- Modify: `hermes_cli/curator.py` or create `hermes_cli/memory_curator.py`
- Test: `tests/agent/test_memory_curator_e2e.py`
- Test: `tests/agent/test_memory_boundary_commit.py`, CLI/gateway curator tests.

- [ ] Step 1: Add an end-of-session test for a short session (< nudge interval) whose durable fact becomes a candidate without triggering an external provider.

```python
def test_short_session_is_queued_for_builtin_dreaming(tmp_path):
    agent = build_agent(memory_enabled=True, external_provider=None, hermes_home=tmp_path)
    finish_turns(agent, count=2, user_text="The deployment uses blue-green")
    finish_session(agent)
    assert memory_curator_pending_sessions(tmp_path)
```

- [ ] Step 2: At the true session boundary (`/new`/handoff/reset path), record an eligible session id/last-reviewed cursor in the memory-curator state/queue. Add a regression assertion that ordinary `run_conversation()` finalization, including its `on_session_end` hook, does not enqueue duplicate “session end” records. Do not run the LLM synchronously in finalization.

- [ ] Step 3: Add `maybe_run_memory_curator()` alongside skill Curator tick sites in CLI/gateway. Reuse idle gates; one run handles bounded pending sessions and marks reviewed cursors only after successful candidate collection/report.

- [ ] Step 4: Ensure external memory providers keep their existing `on_session_end` path and builtin dreaming does not process their provider-specific stores. `skip_memory=True` continues to prevent nested provider calls.

- [ ] Step 5: Add CLI `hermes memory-curator status/run/pause/rollback` with dry-run banner and report/backup paths. Gateway/TUI exposes status and pending staged actions through existing Curator/approval surfaces; no new autonomous write UI.

- [ ] Step 6: Run e2e session-boundary/tick tests and commit.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_memory_curator_e2e.py \
  tests/agent/test_memory_boundary_commit.py \
  tests/agent/test_curator_activity.py \
  tests/hermes_cli/test_curator_run.py \
  tests/hermes_cli/test_curator_status.py -q
 git add agent/memory_manager.py agent/turn_finalizer.py cli.py gateway/run.py hermes_cli/curator.py hermes_cli/memory_curator.py tests/agent/test_memory_curator_e2e.py tests/agent/test_memory_boundary_commit.py tests/agent/test_curator_activity.py tests/hermes_cli/test_curator_run.py tests/hermes_cli/test_curator_status.py
 git diff --cached --check
git commit -m "feat(memory): run dreaming during idle windows"
```

## Task 5: Reports, Review Queue, Rollback, and Final Verification

**Files:**
- Modify: `agent/memory_curator.py`, `agent/curator_backup.py`, CLI/gateway report surfaces.
- Modify: docs/config/example.
- Test: `tests/agent/test_memory_curator_e2e.py`, backup/approval/report tests.

- [ ] Write per-run `run.json`, `REPORT.md`, action counts, candidate/source ids, blocked/staged actions, model/runtime metadata, backup path, and final status. Do not include raw secret/tool payloads.
- [ ] Add a review queue for staged memory actions with accept/reject/rollback; accepted actions use the existing memory write approval contract and are idempotent by run/action id.
- [ ] Add `hermes memory rollback <backup>` and gateway/TUI action. Rollback snapshots current state first and invalidates/rebuilds A6 derived indexes.
- [ ] Run dry-run → staged → approved apply → restart → search/archive readback → rollback scenario under a temp profile.
- [ ] Run contradiction, missing provenance, prompt injection, concurrent live write, external provider, subagent skip-memory, and repeated-run dedup tests.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_memory_curator.py \
  tests/agent/test_memory_curator_backup.py \
  tests/agent/test_memory_curator_e2e.py \
  tests/agent/test_curator_backup.py \
  tests/agent/test_memory_boundary_commit.py \
  tests/tools/test_write_approval.py \
  tests/tools/test_memory_tool.py \
  tests/hermes_cli/test_curator_run.py \
  tests/hermes_cli/test_curator_status.py -q
python3 -m compileall -q agent/memory_curator.py
 git diff --check
```

- [ ] Document enablement, dry-run/staged defaults, backup/rollback, archive-never-delete, provenance limitations, idle/cost controls, and external-provider isolation.
- [ ] Commit final report/docs/evidence.

```bash
git add agent/memory_curator.py agent/curator_backup.py cli.py gateway/run.py hermes_cli/curator.py hermes_cli/memory_curator.py docs website cli-config.yaml.example tests
 git diff --cached --check
git commit -m "docs(memory): document dreaming safety workflow"
```

## Acceptance Checklist

- [ ] Dreaming is idle/interval gated, single-flight, deferred on first run, and bounded.
- [ ] Restricted fork can read only bounded memory/session data and uses no external provider/tools.
- [ ] Duplicate/contradiction/stale decisions are provenance-aware; missing provenance is flagged.
- [ ] No memory is hard-deleted; superseded entries remain archived and searchable.
- [ ] Writes are one atomic batch per target, drift-safe, threat-scanned, approval-staged, and rollbackable.
- [ ] Short/post-nudge sessions can enter builtin extraction without synchronous finalizer work.
- [ ] Reports/review/rollback survive restart and rebuild derived indexes.
- [ ] Existing skill Curator, memory nudges, external providers, and skip-memory isolation remain correct.
- [ ] Default behavior is dry-run/staged; automatic apply requires explicit config.

## Deliberate Simplifications

- Skipped semantic contradiction embeddings in this plan; consume A6 retrieval when available, otherwise use bounded auxiliary review plus explicit provenance.
- Skipped auto-apply of new distilled facts; staged review is safer for prompt-influencing memory.
- Skipped memory file replacement; the existing file/lock/drift path is enough.
- Skipped processing external provider stores; each provider owns its lifecycle and double consolidation risks corruption.
