# Durable Agent Execution Implementation Plan

> For agentic workers: implement this plan in the four workstreams in order. W1 is independently shippable and must land before any unattended resume behavior. Do not turn the current thread-based delegation default into a subprocess default in this plan.

**Goal:** Make tool effects, delegated children, and in-flight turns survive process death without blindly repeating side effects or asking the model to guess what happened.

**Architecture:** Use `SessionDB` as the durable source for transcripts, effects, and turn checkpoints. Keep the existing `delegations.json` only as compatibility control-plane metadata until a deliberate migration/reconciliation step removes the dual store; do not add a third delegation store. W1 adds a per-session `tool_effect_journal` keyed by the existing `operation_key`; the dispatch middleware returns committed results instead of re-executing them. W2 reuses the kanban detached-process/worktree pattern for an opt-in `delegation.isolation: process` mode and sends completion through the existing completion queue. W3 adds bounded, restart-loop-guarded redispatch. W4 checkpoints the loop at its existing pre-API and post-tool-batch flush barriers and merges durable delegation/checkpoint state into the existing `/agents` and TUI surfaces.

**Tech Stack:** Python threads and subprocesses, SQLite/SessionDB, existing `operation_key` and operation metadata, `delegations.json` file locking, `process_registry.completion_queue`, git worktrees, gateway restart-loop guard, and existing CLI/TUI/gateway delegation controls.

## Global Constraints

- W1 must be safe and useful without W2–W4. A journal hit must be deterministic and scoped to the exact `operation_key`.
- `operation_key` includes `tool_call_id`; never deduplicate two distinct model tool calls that happen to have the same name and arguments.
- Journal only non-read-only effects. Read-only calls may remain repeatable; unknown metadata is destructive by default.
- `started` without `committed` is still unknown. Never convert an incomplete journal row into a successful result.
- Journaled results for nondeterministic tools are replayed as recorded and visibly marked as replayed. No attempt is made to re-run them for freshness.
- Thread-based delegation remains the default until process mode passes restart, approval, worktree, and cleanup tests.
- Auto-redispatch is opt-in, bounded by a retry budget/backoff, and shares `restart_loop_guard` plus the existing auto-resume allowlist re-check. Approval-gated work must stop at `needs-input`.
- A checkpoint is valid only when its message cursor, session id, turn id, and operation keys match the flushed transcript. A stale checkpoint falls back to the current recovery-note path.
- No new cross-process event bus; use `process_registry.completion_queue` and existing polling/heartbeat paths.
- A child process must inherit profile context without copying secret-bearing parent state or arbitrary Python globals.
- Worktrees are optional and code-task-only; every allocated worktree/branch has a cleanup path after completion, failure, or redispatch exhaustion.
- Real subprocess/temp-DB/file-lock tests are mandatory. Mocks may cover pure serialization but cannot prove recovery.

## Current-State Review

The codebase review confirms partial scaffolding but no durable execution:

- `tools/registry.py` computes `operation_key`; `model_tools.py`, `agent/tool_executor.py`, and `agent/agent_runtime_helpers.py` pass it through middleware. No consumer or journal table exists.
- `agent/tool_executor.py` emits `[UNRESOLVED] ... effect is UNKNOWN` on timeout/interrupted tool threads. This is the correct honest fallback until W1 supplies a committed result.
- `tools/delegate_tool.py` builds child `AIAgent` instances inside the parent process and uses daemon thread pools. Child sessions are durable, but child execution is not.
- `tools/async_delegation.py` persists records, owner PID/start time, goal/context, and child session ids; dead records become `interrupted` and are surfaced for manual `/resume`, not automatically redispatched.
- Delegation state is currently dual-persisted in `delegations.json` and `state.db.async_delegations`. W2 must define which fields remain control-plane compatibility metadata, add a reconciliation/read-repair test, and avoid expanding both stores independently.
- `hermes_cli/kanban_db.py` already spawns detached `hermes` subprocesses and creates per-task worktrees. Reuse this path rather than designing another worker launcher.
- `run_agent.py` flushes messages before tool effects and at the pre-API barrier; `conversation_loop.py` has the natural post-tool flush. No `turn_checkpoints` table exists.
- Gateway/CLI/TUI have separate live `/agents` views. Durable records are not merged into the gateway roster.

The plan skips a full event-sourced rewrite, a new workflow engine, and live bidirectional IPC for v1. Polling a child transcript is sufficient for restart-safe peek/reply semantics.

## Release Order and Dependencies

1. W1: journaled tool effects and unresolved-result lookup.
2. W2: opt-in detached process delegation and completion sentinel.
3. W3: restart redispatch with budget/backoff and worktree cleanup.
4. W4: turn checkpoints, resume validation, and durable fleet views.

W2 can ship with W1 but must not enable unattended redispatch until W3. W4 consumes W1 for pending tool batches and W2/W3 for fleet state.

## File Map

- Create: `tools/effect_journal.py` — journal record type, middleware adapter, size/pruning policy.
- Create: `tools/delegation_worker.py` — small shared detached-child spec/launcher wrapper around the kanban spawn pattern.
- Create: `agent/turn_checkpoint.py` — checkpoint dataclass, validation, serialization, resume decision.
- Modify: `hermes_state.py` — `tool_effect_journal` and `turn_checkpoints` tables/accessors.
- Modify: `hermes_cli/middleware.py` — register the built-in effect-journal callback at the correct execution point; keep callback errors fail-closed for journal semantics.
- Modify: `agent/tool_executor.py` — resolve committed journal hits and preserve truthful `started`/unknown behavior.
- Modify: `model_tools.py` and `agent/agent_runtime_helpers.py` — ensure all dispatch sites pass the same journal context already used for operation keys.
- Modify: `tools/async_delegation.py` — process-mode record fields, redispatch state machine, and recovery pass.
- Modify: `tools/delegate_tool.py` — process-mode selection, child-session creation, worktree spec, and completion handling.
- Modify: `hermes_cli/kanban_db.py` — expose or reuse the detached argv/worktree helper without duplicating its implementation.
- Modify: `run_agent.py`, `agent/conversation_loop.py`, `agent/turn_context.py`, `agent/turn_finalizer.py` — checkpoint barriers, rehydration, and clear-on-finalize.
- Modify: `gateway/run.py`, `gateway/slash_commands.py`, `hermes_cli/cli_commands_mixin.py`, `tui_gateway/server.py` — durable fleet state/actions.
- Modify: `gateway/restart_loop_guard.py` only if it needs a narrow public reservation/check method; do not create a second breaker.
- Test: new `tests/tools/test_effect_journal.py`, `tests/tools/test_delegation_process.py`, `tests/agent/test_turn_checkpoint.py`.
- Test: extend `tests/tools/test_async_delegation.py`, `tests/tools/test_delegate.py`, `tests/gateway/test_async_delegation_session_binding.py`, `tests/gateway/test_restart_redelivery_dedup.py`, and TUI delegation tests.

## Data Contracts

Journal rows:

```python
@dataclass(frozen=True)
class ToolEffectRecord:
    operation_key: str
    session_id: str | None
    turn_id: str | None
    tool_name: str
    status: Literal["started", "committed"]
    result_json: str | None
    effect_disposition: Literal["none", "committed", "unknown"]
    created_at: float
    committed_at: float | None
    replay_count: int
```

Required methods:

```python
begin_effect(record: ToolEffectRecord) -> bool
commit_effect(operation_key: str, result_json: str, effect_disposition: str) -> None
get_committed_effect(operation_key: str) -> ToolEffectRecord | None
get_started_effect(operation_key: str) -> ToolEffectRecord | None
prune_effects(session_id: str, *, keep_seconds: int) -> int
```

The database key is `operation_key` and the record is scoped by the key's existing session/task inputs. Result JSON has a configurable byte cap; oversized results are stored as a spill reference, not truncated into an apparently complete success.

Checkpoint rows:

```python
@dataclass(frozen=True)
class TurnCheckpoint:
    session_id: str
    turn_id: str
    transcript_cursor: int
    barrier: Literal["pre_api", "post_tool_batch"]
    api_call_count: int
    iteration_budget_remaining: int
    retry_state_json: str
    continuation_flags_json: str
    pending_tool_batch_json: str
    created_at: float
```

`validate_checkpoint(checkpoint, current_session_id, flushed_cursor, current_turn_id) -> bool` must reject mismatched session, cursor, or turn ids before any tool is run.

## Workstream 1: Exactly-Once Tool-Effect Journal

### Task 1.1: Add the journal schema and pure accessors

**Files:**
- Modify: `hermes_state.py`
- Create: `tools/effect_journal.py`
- Test: `tests/tools/test_effect_journal.py`

- [ ] Step 1: Write temp-home tests for started/committed transitions, duplicate begin, size cap, and restart read-back.

```python
def test_committed_effect_round_trips_across_db_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB.open_for_profile("default")
    assert db.begin_effect(ToolEffectRecord("op-1", "s", "t", "write_file", "started", None, "unknown", 1.0, None, 0)) is True
    db.commit_effect("op-1", '{"ok":true}', "committed")
    db.close()
    reopened = SessionDB.open_for_profile("default")
    assert reopened.get_committed_effect("op-1").result_json == '{"ok":true}'


def test_duplicate_begin_is_not_a_second_execution_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB.open_for_profile("default")
    record = ToolEffectRecord("op-1", "s", "t", "terminal", "started", None, "unknown", 1.0, None, 0)
    assert db.begin_effect(record) is True
    assert db.begin_effect(record) is False
```

- [ ] Step 2: Run the new tests and observe the missing table/method failures.

```bash
python -m pytest tests/tools/test_effect_journal.py -q
```

- [ ] Step 3: Add `tool_effect_journal` to the declarative schema with `operation_key` primary key, status/effect indexes, and a result-size check in Python. Parameterize all SQL and use the existing migration reconciliation path.

- [ ] Step 4: Implement accessors with transaction boundaries: `begin_effect` inserts only once, `commit_effect` updates only a matching started row, and reads return immutable records. A missing row on commit is logged as a journal anomaly rather than silently creating a committed result.

- [ ] Step 5: Add pruning at session end using the existing `SessionDB` cleanup path. Keep committed records for the configured replay window; never prune a started record until it is explicitly marked unknown or the retention window expires.

- [ ] Step 6: Run real temp-home tests and compile the new module.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest tests/tools/test_effect_journal.py -q
python3 -m compileall -q tools/effect_journal.py
```

- [ ] Step 7: Commit the schema/accessor foundation.

```bash
git add hermes_state.py tools/effect_journal.py tests/tools/test_effect_journal.py
git diff --cached --check
git commit -m "feat(durability): add tool effect journal"
```

### Task 1.2: Consume the journal at the dispatch chokepoint

**Files:**
- Modify: `hermes_cli/middleware.py`
- Modify: `agent/tool_executor.py`
- Modify: `model_tools.py`
- Modify: `agent/agent_runtime_helpers.py`
- Test: `tests/tools/test_effect_journal.py`
- Test: `tests/tools/test_delegate.py` for unchanged operation-key behavior

- [ ] Step 1: Add a fake-tool test that dispatches a mutating tool twice with the same `operation_key` and asserts the handler runs once and the second result is marked replayed.

```python
def test_committed_operation_is_replayed_without_handler_call(monkeypatch, temp_session):
    calls = []
    register_mutating_fixture(lambda args: calls.append(args) or {"ok": True})
    first = dispatch_fixture("op-1", {"value": 1}, temp_session)
    second = dispatch_fixture("op-1", {"value": 1}, temp_session)
    assert calls == [{"value": 1}]
    assert second["effect_disposition"] == "committed"
    assert second["replayed"] is True
```

- [ ] Step 2: Add an interrupted-start test. A `started` row must not return success and must preserve the existing `[UNRESOLVED]` behavior.

```python
def test_started_without_commit_remains_unknown(temp_session):
    temp_session.begin_effect(started_record("op-2"))
    result = resolve_effect_before_retry("op-2", temp_session)
    assert result["effect_disposition"] == "unknown"
    assert "Do not retry blindly" in result["text"]
```

- [ ] Step 3: Register a built-in journal callback at the actual tool execution chain. The callback must:
  - read `operation_key`, `operation_metadata`, tool name, session id, and turn id from middleware context;
  - bypass read-only operations;
  - return a committed result before the handler when a row exists;
  - insert `started` before the handler;
  - serialize the normalized result after the handler and commit it;
  - preserve multimodal/spill envelopes rather than converting them to an unbounded string;
  - catch journal failures and fail closed for duplicate replay decisions (a journal read error must not claim a mutation is safe to repeat).

- [ ] Step 4: Update the two unresolved branches in `agent/tool_executor.py` to consult the journal only when an operation key exists. Existing user interrupts remain `effect_disposition="none"`; timeout/interrupted mutation remains unknown unless a committed row is available.

- [ ] Step 5: Run the focused execution, approval, and delegation tests.

```bash
python -m pytest \
  tests/tools/test_effect_journal.py \
  tests/tools/test_execute_code_approval_cluster.py \
  tests/tools/test_delegate.py \
  tests/agent/test_tool_executor.py -q
```

- [ ] Step 6: Commit journal consumption.

```bash
git add hermes_cli/middleware.py agent/tool_executor.py model_tools.py agent/agent_runtime_helpers.py tests/tools/test_effect_journal.py tests/tools/test_delegate.py
git diff --cached --check
git commit -m "feat(durability): replay committed tool effects"
```

## Workstream 2: Opt-In Process-Isolated Delegation

### Task 2.1: Define the detached child spec and process mode

**Files:**
- Create: `tools/delegation_worker.py`
- Modify: `tools/delegate_tool.py`
- Modify: `tools/async_delegation.py`
- Modify: `hermes_cli/kanban_db.py` only to expose a shared spawn helper
- Modify: `hermes_cli/config.py`
- Test: `tests/tools/test_delegation_process.py`

- [ ] Step 1: Add a subprocess test that launches a child with a temporary profile/session, waits for a result sentinel, and confirms the parent process can exit without killing the child.

```python
def test_process_worker_writes_completion_sentinel(tmp_path):
    spec = DetachedChildSpec(
        profile="test", child_session_id="child", goal="print result",
        workdir=str(tmp_path), log_path=str(tmp_path / "child.log"),
    )
    handle = spawn_detached_child(spec)
    assert wait_for_sentinel(handle.result_path, timeout=10)["status"] == "completed"
    assert handle.pid > 0
```

- [ ] Step 2: Run the test against the existing thread-only delegation and observe the missing spec/launcher behavior.

```bash
python -m pytest tests/tools/test_delegation_process.py -q
```

- [ ] Step 3: Implement `DetachedChildSpec` and `DetachedChildHandle`. Build argv by reusing the kanban `_resolve_hermes_argv`/`_default_spawn` behavior, including `start_new_session=True`, profile, quiet CLI mode, child session id, and a task-specific log. Pass only `HERMES_DELEGATION_ID`, profile, board/home routing, and non-secret task metadata needed for the child to reopen its own SessionDB.

- [ ] Step 4: Add `delegation.isolation` with values `thread` and `process`, defaulting to `thread`; reject unknown values during config normalization. Add `worktree` only for process-mode code tasks and reuse `_ensure_git_worktree` rather than copying worktree logic.

- [ ] Step 5: For process mode, pre-create the child SessionDB session row, persist child PID/start time/worktree metadata in `delegations.json`, then spawn. The parent must never construct a second in-process `AIAgent` for that child.

- [ ] Step 6: Add a completion sentinel containing child session id, delegation id, terminal outcome, result message id, exit code, and worktree path. Use an atomic write and do not place tool arguments or secrets in the sentinel.

- [ ] Step 7: Run process-mode tests with real subprocesses and inspect no child remains after completion.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest tests/tools/test_delegation_process.py tests/tools/test_async_delegation.py -q
```

- [ ] Step 8: Commit opt-in process isolation.

```bash
git add tools/delegation_worker.py tools/delegate_tool.py tools/async_delegation.py hermes_cli/kanban_db.py hermes_cli/config.py tests/tools/test_delegation_process.py
git diff --cached --check
git commit -m "feat(delegation): add opt-in process isolation"
```

### Task 2.2: Reuse the completion queue and expose durable child progress

**Files:**
- Modify: `tools/async_delegation.py`
- Modify: `cli.py`
- Modify: `gateway/run.py`
- Modify: `gateway/slash_commands.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Test: `tests/gateway/test_async_delegation_session_binding.py`
- Test: `tests/cli/test_cli_delegate_background_notice.py`

- [ ] Step 1: Add a test that a process child completion is delivered once through `process_registry.completion_queue` to both CLI and gateway drain paths.

```python
def test_process_completion_reenters_existing_queue_once(process_record):
    publish_process_completion(process_record)
    publish_process_completion(process_record)
    events = drain_completion_queue_for(process_record.origin_session_id)
    assert len([event for event in events if event.type == "async_delegation"]) == 1
```

- [ ] Step 2: Extend the existing watcher/poller to detect child exit and sentinel state. Do not stream a second IPC channel in v1; `peek` reads the child's SessionDB messages and the log tail.

- [ ] Step 3: Add durable states `working`, `needs-input`, `completed`, `failed`, `interrupted`, and `exhausted` as a view over the existing record plus PID/sentinel/checkpoint facts. Keep the persisted record's existing statuses backward compatible.

- [ ] Step 4: Add `/agents peek <id>` for process children using the current session DB read path. Add `/agents stop <id>` through the existing interrupt/terminate path. Keep `/resume <child_session_id>` as the manual fallback for thread-mode interrupted records.

- [ ] Step 5: Add `needs-input` handling. A child that encounters a gateway approval writes a pending marker and stops; the parent surfaces the same approval identity, and no automatic redispatch occurs until resolution.

- [ ] Step 6: Run gateway/CLI completion and approval tests.

```bash
python -m pytest \
  tests/tools/test_async_delegation.py \
  tests/gateway/test_async_delegation_session_binding.py \
  tests/cli/test_cli_delegate_background_notice.py \
  tests/gateway/test_approve_deny_commands.py -q
```

- [ ] Step 7: Commit durable child delivery.

```bash
git add tools/async_delegation.py cli.py gateway/run.py gateway/slash_commands.py hermes_cli/cli_commands_mixin.py tests/tools/test_async_delegation.py tests/gateway/test_async_delegation_session_binding.py tests/cli/test_cli_delegate_background_notice.py
git diff --cached --check
git commit -m "feat(delegation): deliver process workers through existing queue"
```

## Workstream 3: Bounded Redispatch and Cleanup

### Task 3.1: Persist redispatch policy and restart recovery

**Files:**
- Modify: `tools/async_delegation.py`
- Modify: `gateway/run.py`
- Modify: `cli.py`
- Modify: `gateway/restart_loop_guard.py` only for a shared reservation call if needed
- Test: `tests/tools/test_async_delegation.py`
- Test: `tests/gateway/test_restart_redelivery_dedup.py`

- [ ] Step 1: Add tests for policy boundaries: budget zero, exponential backoff, approval pending, dead PID/start-time mismatch, and one successful redispatch.

```python
def test_redispatch_stops_at_budget_and_uses_backoff():
    policy = RedispatchPolicy(budget=2, backoff_seconds=5, attempts=2)
    assert next_redispatch_delay(policy) == 5
    policy = policy.after_attempt(now=10)
    assert next_redispatch_delay(policy) == 10
    assert policy.after_attempt(now=20).can_retry is False


def test_pid_generation_mismatch_is_interrupted_not_resumed():
    record = record_with_owner(pid=123, start_time="old")
    assert classify_owner_state(record, live_pid=123, live_start_time="new") == "interrupted"
```

- [ ] Step 2: Add `redispatch{budget, backoff_s, attempts, next_at}` to the persisted record with defaults that preserve detection-only behavior. Configure process mode to require an explicit positive budget; thread mode never auto-redispatches.

- [ ] Step 3: At startup, when `_load_records_locked` detects an orphaned process child, reserve the record atomically, ask `restart_loop_guard` for a boot/recovery slot, re-check the auto-resume allowlist and pending approval state, and schedule after `next_at`. If any check fails, mark interrupted with a durable reason.

- [ ] Step 4: Use the existing file-lock merge-on-persist discipline. A failed spawn releases the reservation and increments attempts exactly once. Two gateway/CLI processes must not both redispatch one record.

- [ ] Step 5: Run restart/dedup tests using separate process invocations against one temporary `HERMES_HOME`.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/tools/test_async_delegation.py \
  tests/gateway/test_restart_redelivery_dedup.py -q
```

- [ ] Step 6: Commit bounded redispatch.

```bash
git add tools/async_delegation.py gateway/run.py cli.py gateway/restart_loop_guard.py tests/tools/test_async_delegation.py tests/gateway/test_restart_redelivery_dedup.py
git diff --cached --check
git commit -m "feat(delegation): add bounded process redispatch"
```

### Task 3.2: Worktree and orphan cleanup

**Files:**
- Modify: `tools/delegation_worker.py`
- Modify: `tools/delegate_tool.py`
- Modify: `hermes_cli/kanban_db.py` only for shared prune helper
- Test: `tests/tools/test_delegation_process.py`

- [ ] Step 1: Add tests for completion cleanup, failed cleanup, redispatch reuse, and parent cancellation.

```python
def test_failed_process_child_prunes_its_worktree(tmp_path):
    handle = launch_code_child_with_worktree(tmp_path)
    terminate_child(handle)
    finalize_process_record(handle.record_id)
    assert not Path(handle.worktree_path).exists()
```

- [ ] Step 2: Reuse the kanban worktree creation/prune implementation. Persist branch/worktree paths before spawn so a crash can clean them later. Never run `git clean` on the parent workspace.

- [ ] Step 3: Add a periodic conservative prune for only worktrees/branches owned by a durable delegation record. Unknown worktrees remain untouched for manual cleanup.

- [ ] Step 4: Verify process-mode child environment cleanup removes kernel/terminal resources and closes log descriptors after exit.

- [ ] Step 5: Run process/worktree tests and commit.

```bash
python -m pytest tests/tools/test_delegation_process.py tests/tools/test_delegate.py -q
git add tools/delegation_worker.py tools/delegate_tool.py hermes_cli/kanban_db.py tests/tools/test_delegation_process.py
git diff --cached --check
git commit -m "fix(delegation): clean process worker resources"
```

## Workstream 4: Turn Checkpoints and Fleet View

### Task 4.1: Persist and validate turn checkpoints

**Files:**
- Create: `agent/turn_checkpoint.py`
- Modify: `hermes_state.py`
- Modify: `run_agent.py`
- Modify: `agent/conversation_loop.py`
- Modify: `agent/turn_context.py`
- Modify: `agent/turn_finalizer.py`
- Test: `tests/agent/test_turn_checkpoint.py`

- [ ] Step 1: Add serialization/validation tests before loop wiring.

```python
def test_checkpoint_rejects_stale_transcript_cursor():
    checkpoint = TurnCheckpoint("s", "t", 10, "pre_api", 2, 4, "{}", "{}", "[]", 1.0)
    assert validate_checkpoint(checkpoint, "s", flushed_cursor=11, current_turn_id="t") is False


def test_checkpoint_accepts_matching_barrier():
    checkpoint = TurnCheckpoint("s", "t", 10, "post_tool_batch", 2, 4, "{}", "{}", "[]", 1.0)
    assert validate_checkpoint(checkpoint, "s", flushed_cursor=10, current_turn_id="t") is True
```

- [ ] Step 2: Add `turn_checkpoints` keyed by session/turn/barrier with JSON columns and a cleanup index. Use upsert semantics and keep only the newest valid checkpoint per session/turn.

- [ ] Step 3: Upsert at the existing pre-API flush barrier and immediately after the post-tool-batch flush. Capture `TurnRetryState`, continuation flags, pending verification response state, remaining iteration budget, and per-tool operation keys/statuses. Do not serialize arbitrary Python objects.

- [ ] Step 4: Clear the checkpoint in `finalize_turn` only after the final transcript/result persistence succeeds. If finalization itself crashes, the checkpoint remains available for the next recovery pass.

- [ ] Step 5: At turn prologue, load and validate a checkpoint against the flushed message cursor and current turn id. If valid, rehydrate counters and resume at the recorded barrier. For a pending tool batch, consult W1: committed result is injected, started/absent is handled according to the existing unknown/retry rule. If invalid, discard it with a diagnostic and use the existing recovery-note path.

- [ ] Step 6: Run checkpoint tests with a real temporary SessionDB and a fake provider that crashes between barriers.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest tests/agent/test_turn_checkpoint.py tests/agent/test_tool_executor.py -q
```

- [ ] Step 7: Commit checkpoint durability.

```bash
git add agent/turn_checkpoint.py hermes_state.py run_agent.py agent/conversation_loop.py agent/turn_context.py agent/turn_finalizer.py tests/agent/test_turn_checkpoint.py
git diff --cached --check
git commit -m "feat(durability): checkpoint in-flight turns"
```

### Task 4.2: Merge durable workers into fleet surfaces

**Files:**
- Modify: `gateway/slash_commands.py`
- Modify: `gateway/run.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Modify: `tui_gateway/server.py`
- Modify: `ui-tui/src/` only if the existing delegation screen cannot render the added durable states
- Test: `tests/gateway/test_async_delegation_session_binding.py`
- Test: `tests/tui_gateway/test_delegation_session_lifecycle.py`

- [ ] Step 1: Add a state aggregation test for live thread, process, needs-input, completed, failed, and interrupted records.

```python
def test_agents_view_merges_live_and_durable_records():
    view = build_agents_view(live_records, durable_records, checkpoints)
    assert {row["state"] for row in view} == {"working", "needs-input", "failed", "interrupted"}
    assert all("delegation_id" in row for row in view)
```

- [ ] Step 2: Extend the existing `/agents` handler to list durable records, preserving current live-agent output. Actions:
  - `peek` reads the child's SessionDB transcript tail and log tail;
  - `stop` uses the existing interrupt function for threads or terminates the owned process for process mode;
  - `respawn` requires a positive remaining redispatch budget and reuses the same bounded path;
  - `reply` appends an authenticated user message only to a `needs-input` child session and clears the pending marker.

- [ ] Step 3: Extend `delegation.status` with durable records/checkpoint state. Keep `delegation.pause` and `subagent.interrupt` behavior unchanged for live threads. If UI changes are required, add the states/actions to the existing delegation store rather than creating a second fleet screen.

- [ ] Step 4: Run gateway/TUI tests, including restart/redelivery dedup and approval routing.

```bash
python -m pytest \
  tests/gateway/test_async_delegation_session_binding.py \
  tests/gateway/test_restart_redelivery_dedup.py \
  tests/tui_gateway/test_delegation_session_lifecycle.py -q
```

- [ ] Step 5: Commit durable fleet surfaces.

```bash
git add gateway/slash_commands.py gateway/run.py hermes_cli/cli_commands_mixin.py tui_gateway/server.py ui-tui/src tests/gateway/test_async_delegation_session_binding.py tests/tui_gateway/test_delegation_session_lifecycle.py
git diff --cached --check
git commit -m "feat(delegation): surface durable worker fleet"
```

## End-to-End Verification

- [ ] Start a process-mode child with a temporary profile, kill the parent process, start the gateway/CLI recovery path, and verify exactly one redispatch/completion event.
- [ ] Crash a fake turn after the pre-API checkpoint and after the post-tool-batch checkpoint. Verify the valid barrier resumes and a stale cursor falls back without tool execution.
- [ ] Execute a mutating fixture once, simulate a provider retry with the same `operation_key`, and verify one handler invocation plus one replayed result.
- [ ] Leave a `started` journal row, restart, and verify the user-facing result remains unresolved rather than falsely successful.
- [ ] Run code-task process mode with a worktree, kill it, redispatch it, and verify branch/worktree ownership and cleanup.
- [ ] Exercise pending approval: child enters `needs-input`, parent surfaces the request, an approval resolves it, and only then does the child continue.

Commands:

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/tools/test_effect_journal.py \
  tests/tools/test_delegation_process.py \
  tests/tools/test_async_delegation.py \
  tests/agent/test_turn_checkpoint.py \
  tests/agent/test_tool_executor.py \
  tests/gateway/test_async_delegation_session_binding.py \
  tests/gateway/test_restart_redelivery_dedup.py \
  tests/tui_gateway/test_delegation_session_lifecycle.py -q
python3 -m compileall -q tools/effect_journal.py tools/delegation_worker.py agent/turn_checkpoint.py
 git diff --check
```

## Acceptance Checklist

- [ ] A committed operation key is never executed twice by provider retry or checkpoint resume.
- [ ] An incomplete operation remains explicitly unknown.
- [ ] Thread-mode delegation behavior is backward compatible.
- [ ] Process-mode children have their own PID, profile/session, log, optional worktree, and completion sentinel.
- [ ] Parent/gateway restarts redispatch only opted-in records within budget and backoff, with the existing loop breaker.
- [ ] Completion delivery is deduplicated through the existing queue.
- [ ] Checkpoints validate transcript identity/cursor before rehydrating control state.
- [ ] `/agents` and TUI show durable states without replacing existing live controls.
- [ ] Process, kernel, log, and worktree cleanup is verified after success and failure.
- [ ] No new event bus, orchestration framework, or default unattended re-execution path was added.

## Deliberate Simplifications

- Skipped live child streaming and sibling mailboxes; SessionDB tail reads and the existing completion queue are enough for v1.
- Skipped cross-machine workers; process isolation on the local host is the smallest step that closes the current crash gap.
- Skipped automatic retry of `started` effects; without a committed result, replay would violate exactly-once semantics.
- Skipped conversation rewind/branching; checkpoint resume and the existing file checkpoint manager address durability without a second history model.
