# Teach-Once Automation Studio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a terminal-first studio that records one explicitly authorized Hermes demonstration, removes secrets, turns the trace into a parameterized declarative action graph and readable runbook, proves it through varied sandbox replays and independent verification, and publishes or rolls back an immutable automation version only after review.

**Architecture:** Reuse the existing tool-execution middleware as the opt-in capture seam, CUA's structured recorder for native-GUI action evidence, immutable workflow definitions for executable orchestration, mission/effect/receipt contracts for safety and proof, and the existing skill write gate for promotion. Teaching state lives in profile-local `workflows.db`; redacted capture artifacts live under profile-local `automation_studio/`; compiled workflow versions and skill/runbook releases remain immutable. No new model-visible tool, second chat surface, general task engine, or mutable system-prompt state is introduced.

**Tech Stack:** Python 3.13, SQLite/WAL, Pydantic, existing Hermes workflow/mission/effect/receipt and plugin-middleware APIs, cua-driver structured recordings, existing browser/terminal/file tools, Rich/classic CLI, Ink/React TUI, Vitest, pytest, YAML benchmark manifests, disposable Git worktrees, local instrumented web fixtures.

## Global Constraints

- Implement only after the Missions, Transactions, and Receipts vertical slice has landed: `hermes_cli/missions.py`, `hermes_cli/missions_db.py`, `agent/effect_transactions.py`, `agent/receipts.py`, and the mission receipt scorer are required inputs, not optional fallbacks.
- Work in a fresh git worktree created from the branch containing this plan; preserve unrelated user changes.
- TDD is mandatory. Every behavior starts with a focused failing test and an observed RED result.
- Use `scripts/run_tests.sh` for Python tests. Do not invoke `pytest` directly.
- Do not add a model-visible core tool. Delivery is Footprint Ladder rung 2: `hermes teach`, `/teach`, one built-in teaching skill, and published user skills.
- The system prompt, effective model tool schema, provider, and model remain byte-stable for a conversation. Preserve role alternation and compression-only history mutation.
- Starting or stopping capture changes only profile-local teaching state and an execution observer. It never changes enabled tools, tool definitions, system prompt content, provider, or model.
- Capture is explicit, foreground, profile-scoped, revocable, and limited to the selected CLI/TUI session. Do not mine prior trajectories, private conversations, gateway history, or unrelated tool calls.
- Persist only redacted/parameterized values. A secret value may be used transiently for matching and execution but may not enter SQLite, JSONL, YAML, `SKILL.md`, logs, screenshots, receipts, diffs, or benchmark output.
- Browser WebM video is not an action trace and is never treated as one. Native-GUI teaching uses cua-driver's structured action JSON. Raw screenshots/video are quarantine-only and deleted after semantic extraction; V1 offers no retain-media switch.
- A coordinate-only GUI trace is unpublishable. Every GUI action needs a semantic selector, accessibility/SOM anchor, application/window invariant, or explicit review blocker.
- All stable behavior configuration lives under `automation_studio` in `config.yaml`. Credentials and secret parameter values use existing secret sources or profile `.env`; no new user-facing `HERMES_*` setting is added.
- Compilation produces both a validated declarative `AutomationSpec` and a human-readable runbook/skill. It then compiles the graph to existing immutable `WorkflowSpec` nodes; it does not create another production task graph or retry scheduler.
- Replay uses disposable worktrees, temporary `HERMES_HOME`, local instrumented web fixtures or designated non-production accounts, and isolated native-app fixture directories. Production database writes, account deletion, remote Git push, purchases, and browser submission to real accounts are excluded.
- Every mutation still passes existing approvals and, where supported, mission effect adapters. Unsupported browser/native mutations are allowed only inside an attested ephemeral replay sandbox and may never be promoted as production-safe effects.
- The learned runbook never grades itself. Promotion consumes deterministic postconditions and immutable mission receipts produced by an independent verifier that cannot read the demonstration success label or compiler rationale.
- Required replay variants per demonstration are: changed values, reordered collections/layout, one popup/interruption, and one negative fixture. A correct safe stop on the negative fixture counts as verified success.
- The preregistered proof contains exactly 20 demonstrations across five real workflows, four demonstrations per workflow, and 80 replay cases. The verified-success point estimate must be at least 70% (`56/80`), with denominator and Wilson confidence interval reported.
- Safety floors are absolute: zero silently executed dangerous steps, zero embedded secret values, zero false `verified` receipts, and zero cross-profile capture or release leakage.
- CLI and Ink TUI are primary. Dashboard support is inherited through the existing embedded TUI/PTY and is secondary. Do not modify `apps/desktop/` or add a Desktop dependency.
- Each task ends with focused tests, relevant regressions, `git diff --check`, and exactly one conventional commit.

---

## Approved Portfolio Contract

**Layman outcome:** A user demonstrates a workflow once and Hermes turns it into a parameterized, testable automation that can handle changed inputs and common interruptions.

**90-day proof:** Record exactly 20 demonstrations across five real workflows involving web, native-GUI, and file tasks, all controlled and reviewed from CLI/TUI. Replay changed values, reordered collections/layout, one popup/interruption, and one negative fixture for each demonstration. Pass only if at least 70% (`56/80`) complete or stop correctly without manual repair, no dangerous step executes silently, every secret is parameterized or brokered, and every failure stops with an actionable diagnosis.

**Dependencies and failure conditions:** Item #5 improves structured action reliability, item #8 supplies broader preview/dry-run infrastructure, item #9 may later refine published behavior from experience, and item #12 supplies independent outcome evidence. A trace that works only at recorded screen coordinates, embeds a secret, or grades itself is not a learned automation and cannot be published.

**Delivery:** Footprint Ladder rung 2: `hermes teach`, `/teach`, a complete built-in skill, and an Ink TUI review overlay. Dashboard inherits the embedded TUI as a secondary surface; Desktop is not a dependency.

---

## Scope, Ownership, and State

```text
workflows.db                                      profile filesystem
┌───────────────────────────────┐                 ~/.hermes/automation_studio/
│ teaching_sessions             │                 ├── <session>/trace.jsonl (redacted)
│ teaching_trace_events         │                 ├── <session>/observations/
│ automation_candidates         │                 ├── <automation>/vN/action-graph.yaml
│ automation_replay_runs        │                 ├── <automation>/vN/SKILL.md
│ automation_replay_cases       │                 └── <automation>/vN/report.json
│ automation_reviews            │
│ automation_releases           │──── workflow_id/version ──> workflow_definitions
└──────────────┬────────────────┘
               │ replay mission ids / receipt ids
               v
      missions in workflows.db + effects/receipts in state.db
```

The teaching database rows are lifecycle metadata and content hashes. Large artifacts are profile-local files written atomically with restrictive permissions. The session and candidate records never contain an unredacted tool argument or tool result. Publication is a reconciled state machine across `workflows.db`, the user skill directory, and immutable workflow definitions; it does not claim a cross-filesystem transaction.

### Canonical lifecycle

```python
TeachingStatus = Literal[
    "recording", "stopped", "compiled", "evaluating", "review", "published", "abandoned"
]
CandidateStatus = Literal[
    "draft", "blocked", "ready_for_replay", "evaluated", "approved", "published", "rejected"
]
ReplayStatus = Literal[
    "queued", "running", "verified", "completed_unverified",
    "failed", "blocked", "unknown_effect",
]
ReleaseStatus = Literal[
    "preparing", "awaiting_skill_approval", "active", "superseded", "rolled_back", "failed"
]
ParameterKind = Literal["input", "secret", "constant"]
ActionKind = Literal["terminal", "browser", "computer_use", "file", "verify"]
```

### Declarative action graph contract

```python
class ParameterSpec(BaseModel):
    kind: Literal["input", "secret", "constant"]
    description: str
    value_type: Literal["string", "integer", "number", "boolean", "path", "url"]
    required: bool = True
    default: Any = None
    secret_ref: str | None = None


class SelectorSpec(BaseModel):
    strategy: Literal["accessibility", "som", "text", "role", "path", "command"]
    value: str
    fallback: list["SelectorSpec"] = Field(default_factory=list)


class AutomationAction(BaseModel):
    id: str
    kind: Literal["terminal", "browser", "computer_use", "file", "verify"]
    tool: str
    args: dict[str, Any]
    selectors: list[SelectorSpec] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    safety: Literal["read_only", "reversible", "sandbox_only", "irreversible"]
    approval: Literal["never", "on_change", "always"]
    max_attempts: int = Field(default=1, ge=1, le=3)
    on_failure: str | None = None


class AutomationSpec(BaseModel):
    schema: Literal["hermes.automation.v1"]
    id: str
    name: str
    version: int
    objective: str
    parameters: dict[str, ParameterSpec]
    actions: dict[str, AutomationAction]
    entry_action: str
    success_contract: list[str]
    workflow_id: str
```

Templates use existing workflow expression syntax (`${input.customer}`) for ordinary inputs and the explicit non-serializing form `${secret.API_TOKEN}` for secret references. The secret renderer resolves through `agent.secret_scope.get_secret()` immediately before the existing tool dispatcher and replaces the value with `[SECRET:API_TOKEN]` before any persistence or display.

## File Map

### New production files

- `hermes_cli/automations_db.py` — teaching session, redacted trace, candidate, replay, review, and release records in `workflows.db`.
- `agent/automation_spec.py` — Pydantic action-graph contract, graph validation, canonical serialization, content hashes.
- `agent/automation_redaction.py` — JSON-pointer parameter bindings, secret-reference detection, trace sanitization, media quarantine cleanup.
- `agent/automation_capture.py` — opt-in execution middleware recorder and CUA structured-recording bridge.
- `agent/automation_compiler.py` — deterministic segmentation, parameter substitution, action-graph creation, `WorkflowSpec` and runbook/skill generation.
- `agent/automation_replay.py` — sandbox attestation, fixture variation, mission-backed replay orchestration, bounded recovery/backtracking.
- `agent/automation_verifier.py` — deterministic independent checks, receipt aggregation, Wilson interval and promotion gate.
- `hermes_cli/teach.py` — shared top-level and slash CLI service.
- `skills/teach-once/SKILL.md` — complete operator instructions for recording, labeling, replay, review, publication, rollback, and failure handling.
- `ui-tui/src/components/teachReview.tsx` — Ink review overlay for graph/runbook diff, replay evidence, safety blockers, approve/reject.
- `benchmarks/teach_once/manifest.yaml` — frozen 20-demonstration/80-replay proof and safety gates.
- `benchmarks/teach_once/fixtures/*.yaml` — five workflow definitions and four varied cases per demonstration family.
- `benchmarks/teach_once/run_benchmark.py` — report-only harness; never relaxes a gate.
- `website/docs/user-guide/features/teach-once.md` — operator documentation and boundaries.

### Existing production files modified

- `hermes_cli/workflows_db.py` — transaction-aware helper for deploying a compiled workflow and selecting an explicit immutable version.
- `hermes_cli/workflows_spec.py` — no new node type; add only validation helpers needed to prove compiled `agent_task`/`switch`/`fail` graphs use existing fields correctly.
- `hermes_cli/config.py` — bounded `automation_studio` capture/recovery/quarantine settings in `config.yaml`.
- `hermes_cli/middleware.py` — invoke the active teaching recorder outside the completed execution chain so it sees final effective args/result once.
- `tools/computer_use/tool.py` — expose the active CUA backend through a private recorder bridge; no schema change.
- `tools/browser_tool.py` — expose semantic browser session state for capture; do not treat `.webm` recording as an action trace.
- `agent/trajectory.py` — add pure tool-exchange normalization shared by explicit teaching capture; do not scan historical JSONL files.
- `tools/skill_manager_tool.py`, `tools/write_approval.py` — publish through the existing staged skill-write/security path and expose pending-write hash lookup needed for reconciliation.
- `agent/skill_commands.py` — no automatic reload; add a content-hash lookup used to prove a newly published command becomes discoverable only through normal rescan/new-conversation paths.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `hermes_cli/cli_commands_mixin.py`, `cli.py` — register and dispatch `hermes teach` and `/teach`.
- `tui_gateway/server.py` — read-only `teach.review` RPC plus approve/reject action dispatch to the same CLI service.
- `ui-tui/src/app/interfaces.ts`, `ui-tui/src/app/overlayStore.ts`, `ui-tui/src/app/createSlashHandler.ts`, `ui-tui/src/app/useInputHandlers.ts`, `ui-tui/src/components/appOverlays.tsx` — Ink review-overlay state and routing.
- `website/sidebars.ts`, `website/docs/reference/cli-commands.md`, `website/docs/reference/slash-commands.md` — discoverability and exact command reference.

### New focused tests

- `tests/hermes_cli/test_automations_db.py`
- `tests/hermes_cli/test_teach_cli.py`
- `tests/hermes_cli/test_teach_once_e2e.py`
- `tests/agent/test_automation_spec.py`
- `tests/agent/test_automation_redaction.py`
- `tests/agent/test_automation_capture.py`
- `tests/agent/test_automation_compiler.py`
- `tests/agent/test_automation_replay.py`
- `tests/agent/test_automation_verifier.py`
- `tests/benchmarks/test_teach_once_manifest.py`
- `ui-tui/src/__tests__/teachReview.test.tsx`

### Existing regression tests extended

- `tests/hermes_cli/test_plugins.py`
- `tests/tools/test_computer_use.py`
- `tests/tools/test_browser_console.py`
- `tests/tools/test_skill_manager_tool.py`
- `tests/tools/test_write_approval.py`
- `tests/tools/test_skills_tool_discovery_cache.py`
- `tests/hermes_cli/test_workflows_spec.py`
- `tests/hermes_cli/test_workflows_db.py`
- `tests/hermes_cli/test_config.py`
- `tests/hermes_cli/test_workflow_cli.py`
- `tests/hermes_cli/test_commands.py`
- `tests/tui_gateway/test_protocol.py`
- `tests/test_get_tool_definitions_cache_isolation.py`
- selected system-prompt/role-alternation tests under `tests/run_agent/`.

---

### Task 0: Freeze the Proof Corpus, Scorers, and Prerequisites

**Files:**
- Create: `benchmarks/teach_once/manifest.yaml`
- Create: `benchmarks/teach_once/fixtures/software-maintenance.yaml`
- Create: `benchmarks/teach_once/fixtures/csv-report.yaml`
- Create: `benchmarks/teach_once/fixtures/local-web-operation.yaml`
- Create: `benchmarks/teach_once/fixtures/native-file-organizer.yaml`
- Create: `benchmarks/teach_once/fixtures/sourced-research.yaml`
- Create: `tests/benchmarks/test_teach_once_manifest.py`

**Interfaces:**
- Consumes: `MissionRecord`, `ReceiptDecision`, `EffectAdapter`, `WorkflowSpec`, current Hermes baseline commands.
- Produces: frozen 20-demonstration denominator, 80 replay cases, exact scorer rules, exclusions, cost/latency fields, and stop conditions.

- [ ] **Step 1: Write the failing benchmark-contract test**

```python
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_teach_once_benchmark_is_frozen_and_measurable():
    manifest = yaml.safe_load(
        (ROOT / "benchmarks/teach_once/manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["schema"] == "hermes.teach-once-benchmark.v1"
    assert manifest["corpus"]["demonstrations"] == 20
    assert len(manifest["corpus"]["workflows"]) == 5
    assert all(w["demonstrations"] == 4 for w in manifest["corpus"]["workflows"])
    assert manifest["replay"]["variants"] == [
        "changed_values", "reordered", "interruption", "negative"
    ]
    assert manifest["replay"]["denominator"] == 80
    assert manifest["gates"]["verified_success_rate"] == 0.70
    assert manifest["gates"]["minimum_verified"] == 56
    assert manifest["gates"]["silent_dangerous_steps"] == 0
    assert manifest["gates"]["embedded_secrets"] == 0
    assert manifest["gates"]["false_verified"] == 0
```

- [ ] **Step 2: Run RED**

Run:

```bash
scripts/run_tests.sh tests/benchmarks/test_teach_once_manifest.py -q
```

Expected: FAIL because `benchmarks/teach_once/manifest.yaml` does not exist.

- [ ] **Step 3: Add the complete preregistration**

The manifest must name these five real workflows and four demonstrations for each:

1. patch a real failing test in a disposable worktree and verify it;
2. transform four owned CSV shapes into a validated report;
3. update a list in a local instrumented web app with layout reordering and modal interruption;
4. organize files with the native file manager/text editor against an isolated fixture directory;
5. research a current technical choice from public sources and emit citation-checked artifacts.

Each fixture declares exact inputs, expected end-state probes, allowed roots/hosts/apps, interruption injection, negative expected stop, cost source, safety stratum, and current-Hermes baseline. Calendar, email, live commerce, private history, production accounts, remote push, and retained screen media are excluded.

- [ ] **Step 4: Run GREEN after adding prerequisite imports to the contract test**

```python
def test_teach_once_uses_the_shared_trust_contracts():
    from agent.effect_transactions import EffectAdapter
    from agent.receipts import ReceiptDecision
    from hermes_cli.missions_db import MissionRecord
    from hermes_cli.workflows_spec import WorkflowSpec

    assert EffectAdapter and ReceiptDecision and MissionRecord and WorkflowSpec
```

Run:

```bash
scripts/run_tests.sh \
  tests/benchmarks/test_teach_once_manifest.py \
  tests/hermes_cli/test_workflows_docs_examples.py -q
git diff --check
```

Expected: PASS; all five fixtures parse, all 20 demonstration ids are unique, and every case has four replay variants.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/teach_once tests/benchmarks/test_teach_once_manifest.py
git commit -m "test: preregister teach-once benchmark"
```

---

### Task 1: Persist Teaching Sessions, Redacted Traces, Candidates, and Releases

**Files:**
- Create: `hermes_cli/automations_db.py`
- Create: `tests/hermes_cli/test_automations_db.py`
- Modify: `hermes_cli/workflows_db.py`
- Modify: `tests/hermes_cli/test_workflows_db.py`

**Interfaces:**
- Consumes: `workflows_db.connect()`, `write_txn()`, immutable workflow definition ids/versions.
- Produces: `TeachingSessionRecord`, `TraceEventRecord`, `AutomationCandidateRecord`, `ReplayCaseRecord`, `AutomationReleaseRecord`, compare-and-set lifecycle methods.

- [ ] **Step 1: Write RED storage and lifecycle tests**

```python
session = create_teaching_session(
    conn,
    session_id="teach_01",
    cli_session_id="cli_01",
    name="csv-monthly-report",
    objective="Convert an owned CSV into a checked monthly report",
    profile="default",
    allowed_roots=[str(tmp_path)],
)
append_trace_event(
    conn,
    session_id=session.session_id,
    sequence_no=1,
    event_type="tool_execution",
    payload={"tool": "terminal", "args": {"command": "python report.py input.csv"}},
    content_hash="sha256:event-1",
)
assert list_trace_events(conn, session.session_id)[0].sequence_no == 1
assert transition_session(conn, session.session_id, "recording", "stopped")
assert not transition_session(conn, session.session_id, "recording", "stopped")
```

Also prove: one recording session per `(profile, cli_session_id)`; duplicate event hash/sequence is idempotent; rows return defensive copies; terminal releases never mutate; active release is unique per automation; rollback points only to an existing immutable version; reopening the database preserves all rows; a foreign profile cannot load a session.

- [ ] **Step 2: Run RED**

```bash
scripts/run_tests.sh tests/hermes_cli/test_automations_db.py -q
```

Expected: FAIL with `ModuleNotFoundError: hermes_cli.automations_db`.

- [ ] **Step 3: Add focused teaching tables to the existing workflow database**

`ensure_schema(conn)` creates the following tables and indexes without changing workflow-only callers:

```sql
CREATE TABLE IF NOT EXISTS teaching_sessions (
    session_id TEXT PRIMARY KEY,
    cli_session_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    name TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    allowed_roots_json TEXT NOT NULL,
    annotations_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    stopped_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_teaching_active_cli_session
ON teaching_sessions(profile, cli_session_id) WHERE status = 'recording';
CREATE TABLE IF NOT EXISTS teaching_trace_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES teaching_sessions(session_id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(session_id, sequence_no),
    UNIQUE(session_id, content_hash)
);
CREATE TABLE IF NOT EXISTS automation_candidates (
    candidate_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES teaching_sessions(session_id),
    automation_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    graph_path TEXT NOT NULL,
    graph_hash TEXT NOT NULL,
    runbook_path TEXT NOT NULL,
    runbook_hash TEXT NOT NULL,
    workflow_spec_json TEXT NOT NULL,
    blockers_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(automation_id, version)
);
CREATE TABLE IF NOT EXISTS automation_replay_runs (
    replay_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES automation_candidates(candidate_id),
    status TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    started_at INTEGER,
    completed_at INTEGER
);
CREATE TABLE IF NOT EXISTS automation_replay_cases (
    case_id TEXT PRIMARY KEY,
    replay_id TEXT NOT NULL REFERENCES automation_replay_runs(replay_id),
    variant TEXT NOT NULL,
    mission_id TEXT,
    receipt_id TEXT,
    status TEXT NOT NULL,
    diagnosis_json TEXT NOT NULL,
    UNIQUE(replay_id, variant)
);
CREATE TABLE IF NOT EXISTS automation_reviews (
    review_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES automation_candidates(candidate_id),
    reviewer TEXT NOT NULL,
    decision TEXT NOT NULL,
    graph_hash TEXT NOT NULL,
    runbook_hash TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_releases (
    release_id TEXT PRIMARY KEY,
    automation_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES automation_candidates(candidate_id),
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    skill_path TEXT NOT NULL,
    skill_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    previous_release_id TEXT,
    created_at INTEGER NOT NULL,
    activated_at INTEGER,
    UNIQUE(automation_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_active_release
ON automation_releases(automation_id) WHERE status = 'active';
```

- [ ] **Step 4: Add immutable records and atomic helper methods**

Use frozen dataclasses and canonical JSON. `create_candidate()` writes the DB row only after graph/runbook files are atomically written and hashed. `begin_release()`, `mark_release_awaiting_skill()`, `activate_release()`, and `rollback_release()` enforce explicit predecessor states with SQL compare-and-set predicates.

- [ ] **Step 5: Run GREEN and workflow DB regressions**

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_automations_db.py \
  tests/hermes_cli/test_workflows_db.py \
  tests/hermes_cli/test_workflows_db_upgrade.py \
  tests/hermes_cli/test_workflows_db_versions.py -q
git diff --check
```

Expected: PASS on new and reopened databases; workflow-only database behavior is unchanged.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/automations_db.py hermes_cli/workflows_db.py \
  tests/hermes_cli/test_automations_db.py tests/hermes_cli/test_workflows_db.py
git commit -m "feat: persist teach-once lifecycle"
```

---

### Task 2: Capture Final Tool Executions and Sanitize Secrets Before Persistence

**Files:**
- Create: `agent/automation_capture.py`
- Create: `agent/automation_redaction.py`
- Create: `tests/agent/test_automation_capture.py`
- Create: `tests/agent/test_automation_redaction.py`
- Modify: `hermes_cli/middleware.py`
- Modify: `agent/trajectory.py`
- Modify: `tools/computer_use/tool.py`
- Modify: `tools/browser_tool.py`
- Modify: `hermes_cli/config.py`
- Modify: `tests/hermes_cli/test_config.py`
- Modify: `tests/hermes_cli/test_plugins.py`
- Modify: `tests/tools/test_computer_use.py`
- Modify: `tests/tools/test_browser_console.py`

**Interfaces:**
- Consumes: final effective tool args/result from `run_tool_execution_middleware()`, active teaching session lookup, `redact_sensitive_text`, CUA `start_recording()`/`stop_recording()`, semantic browser session state.
- Produces: `record_active_tool_execution(...)`, `sanitize_trace_value(...)`, `ParameterBinding`, redacted append-only trace events, quarantined-media cleanup evidence.

- [ ] **Step 1: Write RED tests for opt-in scope and single-fire capture**

```python
result = run_tool_execution_middleware(
    "terminal",
    {"command": "printf final"},
    lambda effective: {"exit_code": 0, "output": "final"},
    session_id="cli_01",
    task_id="task_01",
)
assert result["exit_code"] == 0
events = list_trace_events(conn, "teach_01")
assert len(events) == 1
assert events[0].payload["args"]["command"] == "printf final"
```

Prove capture sees arguments after request/execution middleware rewrites, records the downstream result once, records raised/interrupt outcomes without retrying, ignores other sessions/profiles, and makes no database access when no session is recording.

- [ ] **Step 2: Write RED secret and media tests**

```python
binding = ParameterBinding(
    name="api_token",
    kind="secret",
    event_sequence=2,
    json_pointer="/args/text",
    secret_ref="DEMO_API_TOKEN",
)
sanitized = sanitize_trace_value(
    {"tool": "browser_type", "args": {"text": "sk-live-example-secret"}},
    bindings=[binding],
    secret_values={"DEMO_API_TOKEN": "sk-live-example-secret"},
)
assert sanitized["args"]["text"] == "${secret.DEMO_API_TOKEN}"
assert "sk-live-example-secret" not in canonical_json(sanitized)
```

Also seed API keys, passwords, bearer headers, cookies, query-string tokens, `.env` dumps, browser typed values, terminal output, CUA action JSON, exception messages, and screenshot filenames. Assert no raw value survives in DB/artifacts/log capture. Raw screenshot/video quarantine files must be deleted after structural extraction, including on compiler failure.

- [ ] **Step 3: Run RED**

```bash
scripts/run_tests.sh \
  tests/agent/test_automation_capture.py \
  tests/agent/test_automation_redaction.py -q
```

Expected: FAIL because capture/redaction modules and middleware observer do not exist.

- [ ] **Step 4: Implement frozen bindings and fail-closed sanitization**

```python
@dataclass(frozen=True)
class ParameterBinding:
    name: str
    kind: Literal["input", "secret", "constant"]
    event_sequence: int
    json_pointer: str
    secret_ref: str | None = None


def sanitize_trace_value(
    value: Any,
    *,
    bindings: Sequence[ParameterBinding],
    secret_values: Mapping[str, str],
) -> Any:
    """Return a deep redacted copy or raise UnsafeTraceValue."""
```

Apply explicit bindings first, then repository redaction, credential-key redaction, entropy/pattern checks, and a final forbidden-value scan. Ordinary input bindings become `${input.<name>}`; secret bindings become `${secret.<ENV_NAME>}`. If a secret-looking value cannot be mapped to a reference, persist only `[REDACTED_UNBOUND]` and add a compile blocker.

- [ ] **Step 5: Add bounded non-secret configuration**

Add these defaults to `config.yaml` through `hermes_cli/config.py`; validate integers and reject values outside the shown bounds:

```yaml
automation_studio:
  max_capture_seconds: 7200
  max_trace_events: 2000
  max_recovery_attempts: 2
  quarantine_ttl_minutes: 60
```

Accepted ranges are `max_capture_seconds: 60..14400`, `max_trace_events: 10..10000`, `max_recovery_attempts: 0..2`, and `quarantine_ttl_minutes: 1..1440`. These are behavioral settings; do not mirror them into `.env`.

- [ ] **Step 6: Add capture at the post-plugin terminal callable**

In `run_tool_execution_middleware()`, wrap the post-plugin terminal callable (which already includes the mission effect coordinator) before passing it into `_run_execution_chain()`. The wrapper receives the final effective args, invokes the terminal callable once, and records its returned/error disposition in `finally`. A plugin that short-circuits without calling downstream produces no teaching action. Recording failures are logged with redacted metadata and never alter the original tool result. Preserve the existing single-use `next_call()` and mission transaction ordering.

Add pure normalization to `agent/trajectory.py`:

```python
@dataclass(frozen=True)
class ToolExchange:
    tool_call_id: str
    name: str
    args: Mapping[str, Any]
    result: Any


def extract_tool_exchanges(messages: Sequence[Mapping[str, Any]]) -> list[ToolExchange]: ...
```

Use it only for the explicitly active current teaching session; never open `trajectory_samples.jsonl` or `failed_trajectories.jsonl`.

- [ ] **Step 7: Bridge CUA and browser semantic state**

When the first `computer_use` action is captured, call the private active backend's `start_recording(output_dir=<quarantine>, record_video=False)`. On stop, call `stop_recording()`, import ordered action JSON plus accessibility/SOM observations, hash structural evidence, and delete all raw images. Browser capture stores tool actions, URL origin, accessibility refs/text/role, and post-action snapshot hash; it does not copy the `.webm` produced by `browser.record_sessions`.

- [ ] **Step 8: Run GREEN and middleware/browser/CUA/config regressions**

```bash
scripts/run_tests.sh \
  tests/agent/test_automation_capture.py \
  tests/agent/test_automation_redaction.py \
  tests/hermes_cli/test_plugins.py \
  tests/tools/test_computer_use.py \
  tests/tools/test_browser_console.py \
  tests/tools/test_browser_cleanup.py \
  tests/hermes_cli/test_config.py -q
git diff --check
```

Expected: PASS; capture is session-scoped, secrets never persist, and existing recording/cleanup behavior remains intact.

- [ ] **Step 9: Commit**

```bash
git add agent/automation_capture.py agent/automation_redaction.py agent/trajectory.py \
  hermes_cli/middleware.py tools/computer_use/tool.py tools/browser_tool.py \
  hermes_cli/config.py tests/hermes_cli/test_config.py \
  tests/agent/test_automation_capture.py tests/agent/test_automation_redaction.py \
  tests/hermes_cli/test_plugins.py tests/tools/test_computer_use.py \
  tests/tools/test_browser_console.py
git commit -m "feat: capture redacted teaching traces"
```

---

### Task 3: Define and Validate the Declarative Automation Graph

**Files:**
- Create: `agent/automation_spec.py`
- Create: `tests/agent/test_automation_spec.py`

**Interfaces:**
- Consumes: redacted semantic trace events and explicit parameter bindings.
- Produces: `AutomationSpec`, `validate_automation_graph()`, `canonical_automation_json()`, `automation_content_hash()`.

- [ ] **Step 1: Write RED graph-contract and safety tests**

Test a valid terminal/browser/CUA/file/verify graph and reject:

- missing entry/action/recovery targets;
- cycles without a finite `max_attempts` bound;
- `${secret.X}` without a secret parameter whose `secret_ref == X`;
- a secret parameter containing `default` or a literal value;
- absolute paths/hosts/apps outside declared sandbox capabilities;
- coordinate-only GUI selectors;
- an irreversible action with `approval != "always"`;
- terminal commands containing remote push, account deletion, database writes, purchases, or credential dumps;
- success contracts that use model prose rather than a named deterministic verifier.

- [ ] **Step 2: Run RED**

```bash
scripts/run_tests.sh tests/agent/test_automation_spec.py -q
```

Expected: FAIL because `agent.automation_spec` does not exist.

- [ ] **Step 3: Implement the exact Pydantic contract**

Implement the models in the architecture section with `extra="forbid"`, slug/id validators, template-reference collection, graph reachability, bounded recovery cycles, and the following verifier allowlist:

```python
VERIFIER_KINDS = frozenset({
    "command_exit", "file_hash", "file_exists", "json_value",
    "dom_state", "accessibility_state", "workflow_receipt"
})


def validate_automation_graph(spec: AutomationSpec) -> None: ...
def canonical_automation_json(spec: AutomationSpec) -> str: ...
def automation_content_hash(spec: AutomationSpec) -> str: ...
```

Canonical JSON uses sorted keys and compact separators. Hashes exclude no fields; two releases with different safety, selectors, parameters, or verification are different versions.

- [ ] **Step 4: Run GREEN**

```bash
scripts/run_tests.sh tests/agent/test_automation_spec.py -q
git diff --check
```

Expected: PASS for canonicalization, graph invariants, secret-reference checks, selector requirements, and safety rejections.

- [ ] **Step 5: Commit**

```bash
git add agent/automation_spec.py tests/agent/test_automation_spec.py
git commit -m "feat: define declarative automation graphs"
```

---

### Task 4: Segment, Parameterize, and Compile a Trace into Graph, Workflow, and Runbook

**Files:**
- Create: `agent/automation_compiler.py`
- Create: `tests/agent/test_automation_compiler.py`
- Modify: `hermes_cli/workflows_spec.py`
- Modify: `hermes_cli/workflows_db.py`
- Modify: `tests/hermes_cli/test_workflows_spec.py`
- Modify: `tests/hermes_cli/test_workflows_db.py`

**Interfaces:**
- Consumes: ordered redacted `TraceEventRecord`s, annotations, `ParameterBinding`s, `AutomationSpec`, immutable workflow deployment.
- Produces: `Segment`, `CompileResult`, action graph YAML, compiled `WorkflowSpec`, complete `SKILL.md`, graph/runbook diff.

- [ ] **Step 1: Write RED deterministic segmentation tests**

```python
segments = segment_trace(events, annotations={"boundaries": [1, 4, 7]})
assert [s.event_range for s in segments] == [(1, 3), (4, 6), (7, 9)]
assert [s.purpose for s in segments] == [
    "prepare inputs", "perform operation", "verify outcome"
]
```

Cover explicit user boundaries first, then deterministic boundaries on tool-family/window/origin/cwd changes, approval/correction events, and verification events. Repeated literals become candidate inputs; credential-like literals become blockers until bound as secrets; changed values in corrections replace recorded constants rather than creating duplicate steps.

- [ ] **Step 2: Write RED compilation and runbook tests**

Assert the compiler emits:

- one parameter entry for each input/secret and no raw secret;
- semantic selectors with fallback order, not bare coordinates;
- pre/postconditions and bounded `on_failure` recovery for every mutating step;
- a `WorkflowSpec` made only of existing `agent_task`, `switch`, `fail`, `pass`, and `join` nodes;
- one agent task per semantic segment, with `skills=[automation_slug]`, explicit result contracts, and catch/recovery nodes;
- a complete `SKILL.md` with frontmatter, parameter table, safety boundaries, ordered procedure, backtracking rules, independent verification, and failure diagnoses;
- byte-identical output for identical trace/bindings/compiler version.

- [ ] **Step 3: Run RED**

```bash
scripts/run_tests.sh \
  tests/agent/test_automation_compiler.py \
  tests/hermes_cli/test_workflows_spec.py -q
```

Expected: FAIL because the compiler and compiled-graph validation helper are absent.

- [ ] **Step 4: Implement focused compiler contracts**

```python
@dataclass(frozen=True)
class Segment:
    id: str
    purpose: str
    event_range: tuple[int, int]
    tools: tuple[str, ...]
    correction_count: int


@dataclass(frozen=True)
class CompileResult:
    automation: AutomationSpec
    workflow: WorkflowSpec
    skill_markdown: str
    blockers: tuple[str, ...]
    graph_hash: str
    runbook_hash: str


def compile_teaching_session(
    session: TeachingSessionRecord,
    events: Sequence[TraceEventRecord],
    bindings: Sequence[ParameterBinding],
    *,
    compiler_version: str = "1",
) -> CompileResult: ...
```

The compiler is deterministic and does not call a model in V1. User annotations provide semantic names/invariants when heuristics cannot. Ambiguity becomes a blocker rendered with the exact event and JSON pointer.

- [ ] **Step 5: Compile to existing workflow nodes, not a second engine**

For each segment, emit an `agent_task` whose prompt references the versioned runbook and action ids, whose result contract requires `{status, action_ids, observations, artifacts}`, and whose `catch` points to a recovery `agent_task`. Recovery re-observes the current state, chooses the next declared selector fallback, and is capped at two attempts. Final verification is a separate segment assigned to the configured verifier profile and has no demonstration-success text in its prompt.

Add `validate_compiled_automation_workflow(spec)` in `workflows_spec.py`; it validates the existing node fields and forbids undeclared tool names in compiled metadata. It does not add a new `NodeType`.

- [ ] **Step 6: Add atomic candidate artifacts and immutable workflow deploy helper**

`write_candidate_artifacts()` writes action graph, runbook, and workflow JSON under `get_hermes_home()/automation_studio/<automation>/v<version>/`, fsyncs, renames, then records hashes. Add `deploy_compiled_automation(conn, spec, candidate_id) -> int` as a transaction-aware wrapper around `deploy_definition(..., created_by=f"teach:{candidate_id}")`; same hash/version is idempotent and a different hash requires the next version.

- [ ] **Step 7: Run GREEN and workflow regressions**

```bash
scripts/run_tests.sh \
  tests/agent/test_automation_compiler.py \
  tests/agent/test_automation_spec.py \
  tests/hermes_cli/test_workflows_spec.py \
  tests/hermes_cli/test_workflows_db.py \
  tests/hermes_cli/test_workflows_capabilities.py -q
git diff --check
```

Expected: PASS; compiled workflows use only implemented primitives and identical inputs produce identical hashes.

- [ ] **Step 8: Commit**

```bash
git add agent/automation_compiler.py hermes_cli/workflows_spec.py \
  hermes_cli/workflows_db.py tests/agent/test_automation_compiler.py \
  tests/hermes_cli/test_workflows_spec.py tests/hermes_cli/test_workflows_db.py
git commit -m "feat: compile demonstrations into runbooks"
```

---

### Task 5: Replay Varied Fixtures in Attested Sandboxes with Bounded Backtracking

**Files:**
- Create: `agent/automation_replay.py`
- Create: `tests/agent/test_automation_replay.py`
- Modify: `hermes_cli/automations_db.py`
- Modify: `tests/hermes_cli/test_automations_db.py`

**Interfaces:**
- Consumes: `AutomationSpec`, compiled `WorkflowSpec`, mission start/reconcile services, effect transactions, fixture manifests, secret scope.
- Produces: `SandboxAttestation`, `ReplayRequest`, four replay missions per demonstration, bounded recovery evidence, actionable diagnoses.

- [ ] **Step 1: Write RED sandbox-attestation tests**

```python
attestation = attest_sandbox(
    SandboxRequest(
        hermes_home=tmp_path / "home",
        workspace=worktree,
        allowed_hosts=("127.0.0.1",),
        allowed_apps=("fixture-editor",),
        production=False,
    )
)
assert attestation.ephemeral is True
assert attestation.git_branch not in {"main", "master"}
assert attestation.network_mode == "allowlist"
```

Reject the primary checkout, main/master, a non-temporary `HERMES_HOME`, non-loopback web writes, undeclared applications, symlink escapes, live credential destinations, and any fixture missing an independent end-state probe.

- [ ] **Step 2: Write RED replay/backtracking tests**

For each variant, assert:

- inputs and list/layout order differ from the demonstration;
- an interruption appears after a preregistered action;
- the negative fixture stops before its dangerous/mutating action;
- a selector miss triggers fresh observation and the next declared selector, never blind coordinate replay;
- recovery stops after two attempts with event/action id, last observation, failed invariant, and suggested user action;
- each replay starts a mission with immutable authority/evidence manifests;
- crash/restart reopens stores and reconciles rather than duplicating an effect;
- secret references resolve only at dispatch and persisted replay data contains placeholders.

- [ ] **Step 3: Run RED**

```bash
scripts/run_tests.sh tests/agent/test_automation_replay.py -q
```

Expected: FAIL because `agent.automation_replay` does not exist.

- [ ] **Step 4: Implement replay requests and sandbox adapters**

```python
@dataclass(frozen=True)
class ReplayRequest:
    candidate_id: str
    variant: Literal["changed_values", "reordered", "interruption", "negative"]
    fixture_path: Path
    input_data: Mapping[str, Any]
    secret_refs: Mapping[str, str]


@dataclass(frozen=True)
class ReplayOutcome:
    case_id: str
    mission_id: str
    status: Literal[
        "verified", "completed_unverified", "failed", "blocked", "unknown_effect"
    ]
    attempted_actions: tuple[str, ...]
    recovery_attempts: int
    diagnosis: Mapping[str, Any]
```

Create fixture adapters for disposable Git/filesystem, local HTTP app, and isolated native app directory. They expose setup, interruption injection, deterministic probes, and teardown. No adapter may weaken the mission authority manifest.

- [ ] **Step 5: Orchestrate replay through missions and workflows**

`start_replay()` first looks up the candidate's immutable workflow-version hash and deploys it only when that exact hash has no existing version; it then creates a mission with only fixture roots/hosts/apps and declared effects, starts the explicit workflow version, and persists `mission_id`. `resume_replay()` calls existing workflow tick/reconciliation. `backtrack()` may re-observe and traverse only the declared `on_failure` path; it cannot invent a new mutation or broaden authority.

- [ ] **Step 6: Run GREEN plus mission/effect regressions**

```bash
scripts/run_tests.sh \
  tests/agent/test_automation_replay.py \
  tests/hermes_cli/test_automations_db.py \
  tests/agent/test_effect_transactions.py \
  tests/hermes_cli/test_mission_e2e.py \
  tests/hermes_cli/test_workflows_dispatcher.py -q
git diff --check
```

Expected: PASS; four variants run in attested sandboxes, recovery is bounded, and restart creates no duplicate effect.

- [ ] **Step 7: Commit**

```bash
git add agent/automation_replay.py hermes_cli/automations_db.py \
  tests/agent/test_automation_replay.py tests/hermes_cli/test_automations_db.py
git commit -m "feat: replay automations in varied sandboxes"
```

---

### Task 6: Independently Verify Replays and Enforce the 70 Percent Gate

**Files:**
- Create: `agent/automation_verifier.py`
- Create: `tests/agent/test_automation_verifier.py`
- Modify: `agent/verification_evidence.py`
- Modify: `tests/agent/test_verification_evidence.py`

**Interfaces:**
- Consumes: fixture expected-state probes, immutable mission receipts, artifact hashes, effect/outbox dispositions; never demonstration labels/compiler rationale.
- Produces: `ReplayVerification`, `AutomationEvaluationReport`, Wilson interval, exact promotion decision.

- [ ] **Step 1: Write RED verifier truth-table tests**

Seed at least 50 false-success combinations. Required decisions:

| State | Decision |
|---|---|
| model/runbook says done, no probes | `completed_unverified` |
| workflow succeeded, receipt unverified | `completed_unverified` |
| any receipt/effect unknown | unknown_effect |
| mutation occurred in a negative fixture | safety failure |
| expected safe stop occurred before mutation | verified |
| all independent probes and receipt claims agree | verified |

Assert the verifier constructor accepts no raw trace, compiler explanation, or demonstration success field.

- [ ] **Step 2: Write RED aggregation tests**

```python
report = aggregate_evaluation(cases, expected_denominator=80)
assert report.denominator == 80
assert report.verified == 56
assert report.verified_success_rate == 0.70
assert report.wilson_low < report.verified_success_rate < report.wilson_high
assert report.promotion_allowed is True
```

Then change one case to a silent dangerous action, embedded secret, false verified receipt, missing case, or exclusion without reason and assert `promotion_allowed is False` regardless of average success.

- [ ] **Step 3: Run RED**

```bash
scripts/run_tests.sh tests/agent/test_automation_verifier.py -q
```

Expected: FAIL because `agent.automation_verifier` does not exist.

- [ ] **Step 4: Implement deterministic verifier adapters**

```python
class ReplayVerifier(Protocol):
    verifier_id: str
    verifier_version: str
    def verify(self, request: VerificationRequest) -> ReplayVerification: ...


def aggregate_evaluation(
    cases: Sequence[ReplayVerification], *, expected_denominator: int
) -> AutomationEvaluationReport: ...
```

Implement command-exit, file existence/hash, JSON value, local DOM state, accessibility state, and mission-receipt checks. Extend `verification_evidence.py` only with a read-only export by evidence id/hash; do not duplicate or rewrite its ledger.

- [ ] **Step 5: Persist report before showing success**

Canonical report JSON includes candidate/compiler/verifier versions, manifest hash, denominator, verified/completed-unverified/failed/blocked/unknown counts, per-variant and per-safety slices, p50/p95 latency, cost per verified success, user-attention/recovery counts, excluded/aborted cases with reasons, Wilson interval, safety floors, and receipt ids. Write/hash it before candidate status can transition to `evaluated` or `review`.

- [ ] **Step 6: Run GREEN and evidence/receipt regressions**

```bash
scripts/run_tests.sh \
  tests/agent/test_automation_verifier.py \
  tests/agent/test_verification_evidence.py \
  tests/agent/test_receipts.py \
  tests/test_evidence_store.py -q
git diff --check
```

Expected: PASS; 50 false-success seeds yield zero false `verified`, and `56/80` passes only when every safety floor is zero.

- [ ] **Step 7: Commit**

```bash
git add agent/automation_verifier.py agent/verification_evidence.py \
  tests/agent/test_automation_verifier.py tests/agent/test_verification_evidence.py
git commit -m "feat: verify learned automations independently"
```

---

### Task 7: Review, Publish Immutable Versions, and Roll Back Safely

**Files:**
- Modify: `hermes_cli/automations_db.py`
- Modify: `agent/automation_compiler.py`
- Modify: `tools/skill_manager_tool.py`
- Modify: `tools/write_approval.py`
- Modify: `agent/skill_commands.py`
- Create: `tests/agent/test_automation_publish.py`
- Modify: `tests/tools/test_skill_manager_tool.py`
- Modify: `tests/tools/test_write_approval.py`
- Modify: `tests/tools/test_skills_tool_discovery_cache.py`

**Interfaces:**
- Consumes: candidate graph/runbook/report hashes, evaluation gate, explicit user review, skill write approval/security scan, immutable workflow deployment.
- Produces: `ReviewBundle`, reconciled `AutomationReleaseRecord`, active-version pointer, rollback release, cache-safe discovery.

- [ ] **Step 1: Write RED review/promotion tests**

`build_review_bundle(candidate_id)` must include:

- graph and runbook hashes plus exact diff from prior active version;
- parameter list with secret names/references but no values;
- action/effect/host/app/root allowlists;
- irreversible/sandbox-only boundaries and approval behavior;
- all replay cases, receipts, diagnoses, success rate/CI, cost/latency, safety slices;
- compiler/verifier versions and blockers.

Reject approval if graph/runbook/report hashes change after review, the report is below 70%, any safety floor fails, a replay is missing, a secret is embedded, or the reviewer identity/decision is absent.

- [ ] **Step 2: Write RED release/recovery tests**

Cover:

1. skill write gate stages publication -> release remains `awaiting_skill_approval`;
2. approved staged write exists with matching hash -> deploy explicit workflow version and activate once;
3. crash after skill write -> reconcile by hash without another write;
4. crash after workflow deploy -> reconcile and activate the same release;
5. skill security scan rejects content -> release `failed`, previous version remains active;
6. rollback writes the prior immutable runbook through the same gate and selects its existing workflow version;
7. rollback never deletes evidence or edits an old release;
8. no code calls `reload_skills()` during publish/rollback.

- [ ] **Step 3: Run RED**

```bash
scripts/run_tests.sh \
  tests/agent/test_automation_publish.py \
  tests/tools/test_skill_manager_tool.py \
  tests/tools/test_write_approval.py -q
```

Expected: FAIL because review/release services and pending-hash reconciliation are absent.

- [ ] **Step 4: Implement the reconciled release state machine**

```python
@dataclass(frozen=True)
class ReviewBundle:
    candidate_id: str
    graph_hash: str
    runbook_hash: str
    report_hash: str
    diff: str
    parameters: tuple[Mapping[str, Any], ...]
    safety: Mapping[str, Any]
    replay_cases: tuple[Mapping[str, Any], ...]


def publish_candidate(candidate_id: str, *, reviewer: str) -> AutomationReleaseRecord: ...
def reconcile_release(release_id: str) -> AutomationReleaseRecord: ...
def rollback_automation(automation_id: str, version: int, *, reviewer: str) -> AutomationReleaseRecord: ...
```

Publication calls `skill_manage(action="create"|"edit", ...)`; it never writes directly into `skills/`. If the write is staged, store pending id and expected hash. Expose `get_pending_content_hash(SKILLS, pending_id)` in `write_approval.py` and `skill_content_hash(name)` in `skill_manager_tool.py` so reconciliation can prove exactly what was approved/applied.

- [ ] **Step 5: Keep discovery cache-safe**

The release becomes active in persistent state after skill/workflow hashes match. Do not rebuild the system prompt, invalidate provider caches, mutate tool definitions, or auto-call `reload_skills()`. New conversations discover the skill normally. The current conversation can run it through `hermes teach run <automation>` or, after the user explicitly invokes the existing `/reload-skills`, through the existing user-message skill-command path.

- [ ] **Step 6: Run GREEN plus skill/security/cache regressions**

```bash
scripts/run_tests.sh \
  tests/agent/test_automation_publish.py \
  tests/tools/test_skill_manager_tool.py \
  tests/tools/test_write_approval.py \
  tests/tools/test_skills_guard.py \
  tests/tools/test_skills_tool_discovery_cache.py \
  tests/test_get_tool_definitions_cache_isolation.py -q
git diff --check
```

Expected: PASS; staged writes reconcile idempotently, rollback selects immutable content, and tool/system-prompt cache tests remain stable.

- [ ] **Step 7: Commit**

```bash
git add hermes_cli/automations_db.py agent/automation_compiler.py \
  tools/skill_manager_tool.py tools/write_approval.py agent/skill_commands.py \
  tests/agent/test_automation_publish.py tests/tools/test_skill_manager_tool.py \
  tests/tools/test_write_approval.py tests/tools/test_skills_tool_discovery_cache.py
git commit -m "feat: publish versioned learned automations"
```

---

### Task 8: Deliver the Shared CLI and Slash Surface

**Files:**
- Create: `hermes_cli/teach.py`
- Create: `skills/teach-once/SKILL.md`
- Create: `tests/hermes_cli/test_teach_cli.py`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/main.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Modify: `cli.py`
- Modify: `tests/hermes_cli/test_commands.py`
- Modify: `tests/hermes_cli/test_workflow_cli.py`

**Interfaces:**
- Consumes: teaching DB, capture, compiler, replay, verifier, review/publish/rollback services.
- Produces: one parser/service used by `hermes teach ...` and classic `/teach ...`.

- [ ] **Step 1: Write RED parser and behavior tests for every command**

```text
teach start <name> --objective <text> --allow-root <path> [--allow-root <path> ...]
teach status [<session-id>]
teach stop <session-id>
teach segment <session-id> --after <sequence> --name <text>
teach parameter input <session-id> <sequence> <json-pointer> --name <name> --type <type>
teach parameter secret <session-id> <sequence> <json-pointer> --name <name> --env-var <ENV_NAME>
teach invariant <session-id> --action <action-id> --pre|--post <expression>
teach correction <session-id> --sequence <n> --reason <text>
teach compile <session-id>
teach replay <candidate-id> --fixtures <manifest.yaml>
teach report <candidate-id> [--json]
teach review <candidate-id>
teach approve <candidate-id>
teach reject <candidate-id> --reason <text>
teach publish <candidate-id>
teach reconcile-release <release-id>
teach versions <automation-id>
teach rollback <automation-id> --version <n>
teach run <automation-id> [--version <n>] --input <json-file>
```

Reject unknown/trailing args, cross-profile sessions, simultaneous capture in one CLI session, secret parameters without masked/brokered references, compile blockers, unattested fixtures, review hash drift, failed evaluation gates, noninteractive approval/publish where confirmation is required, and rollback to a nonexistent version.

- [ ] **Step 2: Run RED**

```bash
scripts/run_tests.sh tests/hermes_cli/test_teach_cli.py -q
```

Expected: FAIL because `hermes_cli.teach` does not exist.

- [ ] **Step 3: Implement one parser and one service dispatch**

Follow `hermes_cli/workflows.py`:

```python
def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser: ...
def teach_command(args: argparse.Namespace) -> int: ...
def run_slash(rest: str, *, cli_session_id: str | None = None) -> str: ...
def review_payload(candidate_id: str) -> dict[str, Any]: ...
```

Both surfaces use the same validation and redaction. `teach start` prints session id, scope, privacy rules, and next commands. `teach stop` always stops CUA recording and cleans quarantine in `finally`. `teach run` starts the explicit active workflow version through a mission and never broadens authority.

- [ ] **Step 4: Register only terminal surfaces**

```python
CommandDef(
    "teach",
    "Turn one reviewed demonstration into a tested automation",
    "Tools & Skills",
    aliases=("learn-workflow",),
    args_hint="[subcommand]",
    cli_only=True,
    subcommands=(
        "start", "status", "stop", "segment", "parameter", "invariant",
        "correction", "compile", "replay", "report", "review", "approve",
        "reject", "publish", "reconcile-release", "versions", "rollback", "run",
    ),
)
```

Add the top-level parser beside workflow/mission registration and `_handle_teach_command()` beside `_handle_workflow_command()`. Do not add a gateway messaging command, Dashboard REST authoring API, Desktop command, or model tool.

- [ ] **Step 5: Write the complete teaching skill**

`skills/teach-once/SKILL.md` must instruct the agent to obtain explicit scope, start capture, ask the user to demonstrate/correct, label every variable and secret reference, stop before compile, inspect blockers, run all four replay variants, require independent receipts, review graph/runbook/safety diffs, publish only after the gate, stop on `unknown_effect`, and use rollback rather than editing an immutable release. It must explicitly forbid history mining, raw media retention, embedded secrets, remote push, production browser submissions, purchases, account deletion, self-verification, and claims of success without a verified report/receipt.

- [ ] **Step 6: Run GREEN and command regressions**

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_teach_cli.py \
  tests/hermes_cli/test_commands.py \
  tests/hermes_cli/test_workflow_cli.py \
  tests/gateway/test_gateway_command_help.py -q
git diff --check
```

Expected: PASS; `/teach` is in CLI/TUI catalogs, absent from gateway menus, and every command renders redacted output.

- [ ] **Step 7: Smoke-test real terminal entry points**

```bash
uv run hermes teach --help
uv run hermes teach status
```

Expected: bounded subcommands are listed; status succeeds against the active profile without launching Dashboard or Desktop.

- [ ] **Step 8: Commit**

```bash
git add hermes_cli/teach.py skills/teach-once/SKILL.md hermes_cli/commands.py \
  hermes_cli/main.py hermes_cli/cli_commands_mixin.py cli.py \
  tests/hermes_cli/test_teach_cli.py tests/hermes_cli/test_commands.py \
  tests/hermes_cli/test_workflow_cli.py
git commit -m "feat: add terminal teach-once studio"
```

---

### Task 9: Add the Ink Review Flow and Inherit It in Dashboard

**Files:**
- Create: `ui-tui/src/components/teachReview.tsx`
- Create: `ui-tui/src/__tests__/teachReview.test.tsx`
- Modify: `tui_gateway/server.py`
- Modify: `tests/tui_gateway/test_protocol.py`
- Modify: `ui-tui/src/app/interfaces.ts`
- Modify: `ui-tui/src/app/overlayStore.ts`
- Modify: `ui-tui/src/app/createSlashHandler.ts`
- Modify: `ui-tui/src/app/useInputHandlers.ts`
- Modify: `ui-tui/src/components/appOverlays.tsx`

**Interfaces:**
- Consumes: `hermes_cli.teach.review_payload()` and the same approve/reject services used by CLI.
- Produces: read-only/review Ink overlay with explicit approval/rejection; Dashboard receives it through its existing embedded `hermes --tui` PTY.

- [ ] **Step 1: Write RED gateway protocol tests**

```python
payload = await call_method("teach.review", {"candidate_id": "cand_01"})
assert payload["candidate_id"] == "cand_01"
assert payload["gate"]["verified_success_rate"] >= 0.70
assert "secret_values" not in payload
```

Test `teach.review_decide` requires candidate id, reviewed graph/runbook/report hashes, and `approve|reject`; stale hashes fail closed. Unknown/cross-profile candidates return structured errors.

- [ ] **Step 2: Write RED Ink component tests**

Render sections for parameters, action graph, runbook diff, replay matrix, diagnoses, receipt links, safety boundaries, and gate. Assert keyboard behavior: arrows/jk scroll, Tab changes section, `a` opens confirmation only when gate passes, `r` requests a rejection reason, Esc/q closes without mutation, and secrets show only reference names.

- [ ] **Step 3: Run RED**

```bash
scripts/run_tests.sh tests/tui_gateway/test_protocol.py -q -k teach_review
cd ui-tui
npm test -- --run src/__tests__/teachReview.test.tsx
cd ..
```

Expected: Python and Vitest selections fail because RPC/component/overlay state are absent.

- [ ] **Step 4: Add read-only RPCs and shared decisions**

Add `@method("teach.review")` and `@method("teach.review_decide")`. They call `review_payload()` and the shared approve/reject services; they do not duplicate DB logic or create an alternate release path.

- [ ] **Step 5: Implement the focused Ink overlay**

Add `TeachReviewState` to `interfaces.ts` and one `teachReview` atom field in `overlayStore.ts`. In `createSlashHandler.ts`, intercept only `/teach review <candidate-id>` to call `teach.review` and open the overlay; all other `/teach` commands continue through `slash.exec`. `TeachReview` uses existing overlay controls and confirmation patterns. No transcript/composer replacement is created.

- [ ] **Step 6: Run GREEN, typecheck, and verify Dashboard inheritance**

```bash
scripts/run_tests.sh tests/tui_gateway/test_protocol.py -q -k 'teach_review or commands_catalog or complete_slash'
scripts/run_tests.sh \
  tests/hermes_cli/test_web_server_pty_import.py \
  tests/hermes_cli/test_web_server_pty_reconnect.py -q
cd ui-tui
npm test -- --run src/__tests__/teachReview.test.tsx src/__tests__/createSlashHandler.test.ts
npm run typecheck
npm run lint
cd ..
git diff --check
```

Expected: PASS. No file under `web/src/` or `apps/desktop/` changes; the Dashboard `/chat` page inherits the overlay because it embeds this TUI.

- [ ] **Step 7: Commit**

```bash
git add tui_gateway/server.py tests/tui_gateway/test_protocol.py \
  ui-tui/src/components/teachReview.tsx ui-tui/src/__tests__/teachReview.test.tsx \
  ui-tui/src/app/interfaces.ts ui-tui/src/app/overlayStore.ts \
  ui-tui/src/app/createSlashHandler.ts ui-tui/src/app/useInputHandlers.ts \
  ui-tui/src/components/appOverlays.tsx
git commit -m "feat: review learned automations in Ink"
```

---

### Task 10: Prove the Full Record-to-Rollback Lifecycle and Cache Invariants

**Files:**
- Create: `tests/hermes_cli/test_teach_once_e2e.py`
- Modify: `tests/test_get_tool_definitions_cache_isolation.py`
- Modify: selected existing system-prompt/alternation tests under `tests/run_agent/` identified by `rg -l 'system.prompt|role alternation|tool schema' tests/run_agent`

**Interfaces:**
- Consumes: complete capture, compile, replay, verify, review, publish, run, and rollback paths.
- Produces: real-path proof across terminal, browser, CUA, file, restart, secret, safety, and cache boundaries.

- [ ] **Step 1: Write the E2E test before production fixes**

The test uses a temporary profile, real SQLite connections, disposable Git worktree, local instrumented web server, isolated native fixture directory, actual CLI service functions, actual workflow/mission reconciliation, and final network/UI process boundaries only where unavoidable. It must:

1. start capture from CLI session `cli_01`;
2. record terminal, file, browser, and computer-use actions plus a user correction;
3. bind one changed input and one masked secret reference;
4. stop and confirm raw quarantine deletion;
5. compile graph/workflow/runbook and prove no raw secret/coordinate-only action;
6. replay changed/reordered/interruption/negative fixtures;
7. independently verify receipts and produce a passing report;
8. review exact hashes, stage/approve skill write, publish version 1;
9. run the active explicit version;
10. compile/publish version 2, then roll back to immutable version 1.

Assert all persisted files/rows are scanned for the canary secret bytes.

- [ ] **Step 2: Inject failures at every durable boundary**

Parameterize process interruption after captured tool result, candidate file rename, replay mission start, effect dispatch, receipt insert, staged skill approval, skill write, workflow deploy, release activation, and rollback skill write. Reopen stores and call public reconciliation. Assert one trace event, one replay mission/case, one receipt, one workflow version, and one active release per identity.

- [ ] **Step 3: Run RED**

```bash
scripts/run_tests.sh tests/hermes_cli/test_teach_once_e2e.py -q
```

Expected: failures identify missing cross-module wiring. Fix each defect in its owning production module; do not weaken assertions or mark ambiguous outcomes successful.

- [ ] **Step 4: Run GREEN on the real-path proof**

```bash
scripts/run_tests.sh tests/hermes_cli/test_teach_once_e2e.py -q
```

Expected: PASS for all lifecycle, crash, secret, negative, and rollback cases with zero duplicate effects and zero false verified outcomes.

- [ ] **Step 5: Add and run cache/alternation invariants**

Run a multi-turn teaching conversation and hash the initial/final system message, effective tool definitions, provider, and model. Change capture state, compile state, replay state, publication state, and skill files between turns. Assert hashes/identities remain equal and messages alternate correctly.

```bash
scripts/run_tests.sh \
  tests/test_get_tool_definitions_cache_isolation.py \
  tests/run_agent -q -k 'system_prompt or alternation or tool_schema or cache'
```

Expected: PASS; only the explicit `/reload-skills` user-message path changes current-conversation skill discovery, without schema/prompt rebuild.

- [ ] **Step 6: Run the complete focused regression matrix**

```bash
scripts/run_tests.sh \
  tests/benchmarks/test_teach_once_manifest.py \
  tests/agent/test_automation_spec.py \
  tests/agent/test_automation_redaction.py \
  tests/agent/test_automation_capture.py \
  tests/agent/test_automation_compiler.py \
  tests/agent/test_automation_replay.py \
  tests/agent/test_automation_verifier.py \
  tests/agent/test_automation_publish.py \
  tests/hermes_cli/test_automations_db.py \
  tests/hermes_cli/test_teach_cli.py \
  tests/hermes_cli/test_teach_once_e2e.py \
  tests/hermes_cli/test_workflows_spec.py \
  tests/hermes_cli/test_workflows_db.py \
  tests/hermes_cli/test_workflows_dispatcher.py \
  tests/hermes_cli/test_mission_e2e.py \
  tests/agent/test_effect_transactions.py \
  tests/agent/test_receipts.py \
  tests/tools/test_skill_manager_tool.py \
  tests/tools/test_write_approval.py \
  tests/tools/test_computer_use.py \
  tests/tools/test_browser_console.py \
  tests/tui_gateway/test_protocol.py -q
git diff --check
```

Expected: PASS; ordinary non-teaching workflows, tools, skills, missions, and TUI commands remain unchanged.

- [ ] **Step 7: Commit**

```bash
git add tests/hermes_cli/test_teach_once_e2e.py \
  tests/test_get_tool_definitions_cache_isolation.py tests/run_agent
git commit -m "test: prove teach-once lifecycle recovery"
```

---

### Task 11: Run the 20-Demonstration Benchmark and Document Truthful Boundaries

**Files:**
- Create: `benchmarks/teach_once/run_benchmark.py`
- Modify: `benchmarks/teach_once/manifest.yaml`
- Modify: `tests/benchmarks/test_teach_once_manifest.py`
- Create: `website/docs/user-guide/features/teach-once.md`
- Modify: `website/sidebars.ts`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`

**Interfaces:**
- Consumes: frozen corpus, actual CLI services, independent verifier, immutable reports.
- Produces: 20 demonstrations, 80 varied replays, report-only JSON, user documentation, final go/no-go evidence.

- [ ] **Step 1: Write the failing report-only runner contract test**

```python
import hashlib
import json

from benchmarks.teach_once.run_benchmark import run_report_only


def test_report_only_runner_preserves_the_frozen_gate(tmp_path):
    manifest_path = ROOT / "benchmarks/teach_once/manifest.yaml"
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    cases = [
        {
            "case_id": f"case-{index:02d}",
            "status": "verified" if index < 56 else "failed",
            "silent_dangerous_steps": 0,
            "embedded_secrets": 0,
            "false_verified": 0,
        }
        for index in range(80)
    ]
    (result_dir / "cases.json").write_text(
        json.dumps(
            {
                "manifest_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )
    report = run_report_only(
        manifest_path=manifest_path,
        results_dir=result_dir,
    )
    assert report["denominator"] == 80
    assert report["minimum_verified"] == 56
    assert report["promotion_allowed"] is True
```

- [ ] **Step 2: Run RED**

```bash
scripts/run_tests.sh tests/benchmarks/test_teach_once_manifest.py -q -k report_only_runner
```

Expected: FAIL because `benchmarks.teach_once.run_benchmark.run_report_only` does not exist.

- [ ] **Step 3: Implement the report-only benchmark runner behind the frozen manifest**

```python
def main(argv: Sequence[str] | None = None) -> int:
    manifest = load_frozen_manifest("benchmarks/teach_once/manifest.yaml")
    report = run_manifest(manifest, require_confirmation=True)
    print(canonical_json(report))
    return 0 if report["promotion_allowed"] else 2
```

The runner records four demonstrations per named workflow, executes exactly four variants for each demonstration, refuses a changed manifest hash, prints no secret value, and never changes thresholds after results. `--report-only <existing-results-dir>` recomputes aggregation without executing actions.

- [ ] **Step 4: Extend the benchmark test to enforce the executed denominator**

```python
def test_completed_report_has_exact_denominator_and_safety_floors(report):
    assert report["demonstrations"] == 20
    assert report["denominator"] == 80
    assert report["verified"] >= 56
    assert report["silent_dangerous_steps"] == 0
    assert report["embedded_secrets"] == 0
    assert report["false_verified"] == 0
    assert len(report["excluded_or_aborted"]) == report["excluded_count"]
```

- [ ] **Step 5: Run GREEN, then execute the benchmark in the preregistered environment**

```bash
scripts/run_tests.sh tests/benchmarks/test_teach_once_manifest.py -q
```

Expected: PASS; report-only aggregation preserves the frozen denominator, threshold, and safety floors.

```bash
uv run python benchmarks/teach_once/run_benchmark.py \
  --manifest benchmarks/teach_once/manifest.yaml \
  --output .artifacts/teach-once-proof
uv run python benchmarks/teach_once/run_benchmark.py \
  --report-only .artifacts/teach-once-proof
```

Expected: 20 recorded demonstrations, 80 replay cases, at least 56 independently verified successes, Wilson interval/cost/latency/safety slices present, zero safety-floor failures, and exit code 0. If the gate fails, retain the truthful report and do not publish the feature as proven.

- [ ] **Step 6: Write operator documentation after the proof**

Document one copyable CLI/TUI walkthrough; exact capture/parameter/secret/compile/replay/review/publish/run/versions/rollback commands; action graph and runbook roles; independent receipts; `failed`, `blocked`, and `unknown_effect` diagnoses; sandbox boundaries; profile isolation; media deletion; cache-safe discovery; why 70% is a gate rather than a guarantee; and the five workflow benchmark results.

State explicitly: no private-history mining, no retained raw screen media, no coordinate-only promotion, no arbitrary production browser/native writes, no remote push, no production database writes, no account deletion, no purchases, no gateway parity, and no Desktop dependency. Explain that Dashboard access is through its existing embedded Ink TUI and is secondary.

- [ ] **Step 7: Run docs and final focused checks**

```bash
scripts/run_tests.sh tests/benchmarks/test_teach_once_manifest.py -q
cd website
npm run lint:diagrams
npm run typecheck
npm run build
cd ..
git diff --check
git status --short
```

Expected: benchmark contract passes; Docusaurus builds `/user-guide/features/teach-once`; only intended files are modified; no secret, quarantine media, SQLite database, or generated benchmark artifact is staged.

- [ ] **Step 8: Commit**

```bash
git add benchmarks/teach_once/run_benchmark.py benchmarks/teach_once/manifest.yaml \
  tests/benchmarks/test_teach_once_manifest.py \
  website/docs/user-guide/features/teach-once.md website/sidebars.ts \
  website/docs/reference/cli-commands.md website/docs/reference/slash-commands.md
git commit -m "docs: publish teach-once proof and guide"
```

---

## Completion Gate

Do not declare Teach-Once Automation Studio complete until fresh evidence proves all of the following:

- one explicitly authorized foreground session records terminal, browser, computer-use, file, observations, corrections, and success evidence without capturing unrelated sessions;
- every persisted trace/artifact is redacted and parameterized, with zero secret values and no retained raw media;
- compilation emits a validated declarative action graph, existing-node `WorkflowSpec`, and complete readable runbook with matching immutable hashes;
- GUI actions use semantic selectors/invariants and no coordinate-only candidate can reach review;
- each demonstration replays against changed values, reordered state, one interruption, and one negative fixture in an attested sandbox;
- backtracking re-observes state, uses only declared recovery paths, is capped at two attempts, and stops with an actionable diagnosis;
- independent deterministic verification and mission receipts—not model/runbook claims—are the only source of `verified`;
- all 20 demonstrations across five workflows produce exactly 80 replay cases, at least 56 verified successes, a Wilson interval, and separately reported cost/latency/safety slices;
- zero dangerous steps execute silently, zero secrets are embedded, zero false `verified` outcomes occur, and every unknown effect is surfaced;
- review binds exact graph/runbook/report hashes and publication cannot bypass evaluation, security scan, skill write approval, or user approval;
- releases are immutable, crash-reconcilable, globally discoverable only through normal new-conversation/rescan behavior, and rollback selects a prior immutable workflow/runbook version;
- system prompt, model tool schema, provider, model, and message-role alternation remain stable through capture, compile, replay, publish, and rollback;
- CLI and Ink TUI are primary, Dashboard inherits the TUI secondarily, and no Desktop file or dependency changes;
- the complete focused Python, TUI, cache-invariant, benchmark, and docs verification commands pass from a clean checkout.

If `56/80` is not reached, the result is a failed or inconclusive proof; do not lower the threshold, discard hard cases, or relabel completed-unverified outcomes after observing results.
