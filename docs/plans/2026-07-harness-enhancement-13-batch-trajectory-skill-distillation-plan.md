# Batch Trajectory-to-Skill Distillation Implementation Plan

> For agentic workers: implement this after the turn-outcome ledger and verified-skill gate. Distill batches into existing umbrella skills, not one-off skill sprawl; every autonomous patch is staged and verified by the plan-12 pipeline.

**Goal:** Add a bounded offline pass over stored session trajectories that groups related settled sessions, contrasts successes/failures using persisted outcomes, and emits conflict-free patches to existing umbrella `SKILL.md` files. Close the blind spot of one-turn nudges and fire-and-forget review without bypassing skill write safety.

**Architecture:** Persist final turn outcomes/watermarks through A2. `agent/skill_distiller.py` selects sessions older than a settle window with `distilled_at IS NULL`, excludes fork/curator/parent-superseded sessions, groups them by tool/task fingerprint and existing skill descriptions, and creates bounded per-session digests. A restricted auxiliary distiller sees the batch plus relevant umbrella skills and outputs structured patch proposals only. Proposals enter `write_approval` as `background_review` writes; plan 12’s verifier reviews/applies them. Run state/report/watermarks make the pass restartable and idempotent.

**Tech Stack:** `hermes_state.SessionDB`/FTS5/trigram/anchored views, `agent/turn_outcome.py`, `agent/turn_finalizer.py`, `agent/background_review._digest_history`, `agent/agent_runtime_helpers.convert_to_trajectory_format`, `agent/curator.py` scheduling/report patterns, `tools/skill_manager_tool.py`/provenance/write approval, `tools/skill_usage.py`, `agent/skill_verifier.py`, auxiliary runtime resolution.

## Global Constraints

- A2 outcome ledger is a hard dependency. Legacy sessions without an outcome are eligible only through explicit fallback labels (`unknown`) and never treated as successful evidence.
- A12 verification/staging is a hard dependency for autonomous writes. The distiller never calls live skill apply directly.
- Candidate sessions are settled/old enough to avoid live writes and bounded by max sessions, chars, batches, and model iterations. Mark watermarks only after the run has a durable report and successful staging.
- Exclude `background_review`, `curator`, `distiller`, `skill_verifier`, compression/helper forks, and parent sessions superseded by child/compaction lineage. Never distill the distiller’s own transcript.
- Group by task/tool fingerprints and existing umbrella descriptions; do not invent a new skill file. A proposal targeting no existing umbrella is `unverifiable/staged for review`, not an automatic new skill.
- Success patterns and failure anti-patterns are both evidence. A single failed session cannot become a universal rule; require repeated/contrasting support or flag as low confidence.
- Distiller fork uses `skip_context_files=True`, `skip_memory=True`, no network/terminal/delegation/MCP, bounded `session_search`/skills reads, and background origin. Its output is untrusted model text.
- Skill writes preserve read-before-write, fail-closed deletes, ownership/consolidation guards, snapshot/rollback, and threat scanning. Never auto-delete or rewrite unrelated skill sections.
- Conflict-free means patch applies to the current base digest and does not contradict protected sections/metadata. If base changed, rebase/review instead of overwriting.
- Reports contain counts/ids/digests/verdicts, not raw secrets/full transcripts.

## Current-State Review

- Per-turn `background_review` only fires on verified outcomes plus a nudge interval; failed/interrupted/short/post-nudge sessions are missed.
- `agent/learn_prompt.py` explicitly says no separate distillation engine.
- SessionDB already has FTS5/trigram search, anchored views, rich session listing, parent/child metadata, and full stored messages.
- Curator already supplies idle/interval state, auxiliary runtime selection, restricted fork/report/snapshot patterns, but reviews only skills, not transcripts.
- Outcome classification exists in `agent/turn_outcome.py` but was not persisted before A2; this plan must not use the old return dict as if it were a ledger.
- Skill manager/provenance/write approval/verifier infrastructure can safely receive distiller patches.

The plan skips cross-model reinforcement learning, full transcript replay, and new-skill generation.

## Release Order

1. Outcome/watermark schema handoff and candidate selector.
2. Batch grouping/digest/report dry-run.
3. Restricted distiller fork and structured patch validation.
4. Stage/verify/apply through A12.
5. Idle scheduling, CLI/report, metrics, and full verification.

## File Map

- Create: `agent/skill_distiller.py` — selector/grouping/digests/fork/proposal/report/state.
- Modify: `hermes_state.py`, `agent/turn_finalizer.py` — A2 outcome/watermark integration if not already present.
- Modify: `agent/background_review.py` — reuse/export bounded digest helper only.
- Modify: `agent/curator.py`, `cli.py`, `gateway/run.py` — idle tick/state gate.
- Modify: `tools/skill_manager_tool.py`, `tools/write_approval.py` — distiller origin/source metadata handoff.
- Modify: `agent/skill_verifier.py` — accept source/run evidence and target patch.
- Modify: `tools/skill_usage.py`, `agent/insights.py` — batch/distillation metrics.
- Modify: `hermes_cli/config.py`, `hermes_cli/curator.py` or create `hermes_cli/skill_distiller.py` — config/CLI.
- Test: new `tests/agent/test_skill_distiller.py`, `tests/agent/test_skill_distiller_e2e.py`.
- Test: extend `tests/agent/test_turn_outcome.py`, `tests/agent/test_background_review.py`, `tests/agent/test_skill_verifier_e2e.py`, state/skill usage/CLI suites.

## Data Contracts

```python
@dataclass(frozen=True)
class DistillSession:
    session_id: str
    ended_at: float
    outcome: str
    outcome_reason: str | None
    task_fingerprint: str
    tool_fingerprint: tuple[str, ...]
    parent_session_id: str | None
    digest: str
```

```python
@dataclass(frozen=True)
class SkillPatchProposal:
    proposal_id: str
    target_skill: str
    base_digest: str
    patch: str
    supporting_session_ids: tuple[str, ...]
    success_session_ids: tuple[str, ...]
    failure_session_ids: tuple[str, ...]
    confidence: float
    rationale: str
    safety_notes: tuple[str, ...]
```

```python
@dataclass(frozen=True)
class DistillRun:
    run_id: str
    started_at: float
    finished_at: float | None
    status: Literal["dry_run", "staged", "applied", "blocked", "failed"]
    session_count: int
    batch_count: int
    proposal_count: int
    staged_count: int
    reviewed_until: float | None
    report_path: str
```

## Task 1: Persisted Outcome/Watermark Handoff and Candidate Selector

**Files:**
- Modify: `hermes_state.py`, `agent/turn_finalizer.py` only if A2 has not completed these fields.
- Create: `agent/skill_distiller.py`
- Modify: `hermes_cli/config.py`
- Test: `tests/agent/test_skill_distiller.py`, outcome/state tests.

- [ ] Step 1: Add selector tests against a real temp SessionDB.

```python
def test_selector_excludes_forks_parents_and_already_distilled(tmp_path):
    db = open_temp_state_db(tmp_path)
    add_session(db, "good", ended_at=100, outcome="verified", platform="cli", distilled_at=None)
    add_session(db, "fork", ended_at=100, outcome="verified", platform="distiller", distilled_at=None)
    add_session(db, "done", ended_at=100, outcome="verified", platform="cli", distilled_at=90)
    add_session(db, "parent", ended_at=100, outcome="verified", platform="cli", parent_session_id="child", distilled_at=None)
    result = select_distill_sessions(db, now=1000, settle_seconds=100)
    assert [item.session_id for item in result] == ["good"]


def test_legacy_unknown_outcome_is_not_success():
    db = open_temp_state_db(tmp_path)
    add_session(db, "legacy", ended_at=100, outcome=None, distilled_at=None)
    item = select_distill_sessions(db, now=1000, settle_seconds=100)[0]
    assert item.outcome == "unknown"
```

- [ ] Step 2: Confirm A2 adds a turn-outcome table/columns and `distilled_at`/`distill_run_id` watermark. Persist at finalizer where `_turn_outcome` is already available; write outcome/reason atomically with session boundary. Add migration for legacy rows without rewriting content.

- [ ] Step 3: Implement selector query: ended before settle window, watermark null, eligible profile/session, bounded outcome set, no excluded platform/origin, no superseded parent lineage. Add configurable max sessions and max age.

- [ ] Step 4: Compute stable task/tool fingerprints from first user message keywords, tool names/operation metadata, and existing umbrella skill descriptions. Hash normalized values; do not store user message text in the fingerprint.

- [ ] Step 5: Add `distill_run_id` claim/watermark state with a lease. Do not mark `distilled_at` during selection; claim records can be released/retried after crash.

- [ ] Step 6: Run selector/outcome/state tests and commit the handoff if A2 did not already include it.

```bash
python -m pytest tests/agent/test_skill_distiller.py tests/agent/test_turn_outcome.py tests/hermes_state/test_state_migrations.py -q
```

- [ ] Commit exact changed files with `docs(plan)` excluded; if A2 already delivered the schema, commit only distiller selector files.

## Task 2: Batch Grouping, Digest, and Dry-Run Report

**Files:**
- Modify: `agent/skill_distiller.py`
- Reuse/modify: `agent/background_review.py` digest helper.
- Modify: `hermes_state.py` only for bounded FTS query helper.
- Test: `tests/agent/test_skill_distiller.py`, background-digest tests.

- [ ] Step 1: Add grouping/digest tests.

```python
def test_batch_groups_related_tools_and_skill_umbrella():
    sessions = [
        session("a", tools=("terminal", "git"), first_message="deploy service", outcome="verified"),
        session("b", tools=("terminal", "git"), first_message="deploy service", outcome="failed"),
        session("c", tools=("calendar",), first_message="schedule meeting", outcome="verified"),
    ]
    batches = group_into_batches(sessions, skills=[skill("deployment"), skill("calendar")], max_batch_size=8)
    assert [item.skill_name for item in batches] == ["deployment", "calendar"]


def test_digest_is_bounded_and_redacted():
    digest = digest_session(long_session_with_secret(), max_chars=2000)
    assert len(digest.text) <= 2000
    assert "secret-value" not in digest.text
```

- [ ] Step 2: Select existing umbrellas by tool fingerprint/FTS/description; cap 8–15 sessions per batch (configurable), max batches/run, and require a minimum evidence count for auto-proposal. Keep success/failure/unknown labels visible.

- [ ] Step 3: Reuse `_digest_history`/trajectory formatter for per-session compact digests: first user intent, relevant tool sequence, bounded errors, outcome/reason, final response head. Redact values and omit unrelated turns.

- [ ] Step 4: Generate dry-run `run.json`/`REPORT.md` with candidate/batch counts, target skill, outcome mix, digest sizes, skipped reasons, watermark range, and estimated auxiliary cost. Dry-run never calls skill writes.

- [ ] Step 5: Add idle/size/time/cost gates and a first-run defer rule copied from Curator. Run selector/grouping/digest tests.

```bash
python -m pytest tests/agent/test_skill_distiller.py tests/agent/test_background_review.py -q
```

- [ ] Step 6: Commit batch/dry-run implementation.

```bash
git add agent/skill_distiller.py agent/background_review.py hermes_state.py tests/agent/test_skill_distiller.py tests/agent/test_background_review.py
 git diff --cached --check
git commit -m "feat(skills): add batch distillation dry run"
```

## Task 3: Restricted Distiller Fork and Structured Proposals

**Files:**
- Modify: `agent/skill_distiller.py`
- Modify: `hermes_cli/config.py`
- Modify: `tools/skill_manager_tool.py`, `tools/skill_provenance.py` for origin/source metadata.
- Test: `tests/agent/test_skill_distiller.py`, `tests/agent/test_skill_distiller_e2e.py`.

- [ ] Step 1: Add fork/schema tests.

```python
def test_distiller_fork_is_read_only_except_staged_skill_write():
    spec = build_distiller_fork_spec()
    assert spec.skip_memory is True
    assert "session_search" in spec.tool_names
    assert "skill_view" in spec.tool_names
    assert "terminal" not in spec.tool_names
    assert spec.write_origin == "background_review"


def test_proposal_requires_existing_umbrella_and_base_digest():
    assert validate_proposal({"target_skill": "new-skill", "patch": "..."}, skills=["deploy"]).status == "rejected"
    assert validate_proposal(valid_proposal(), skills=["deploy"]).status == "accepted"
```

- [ ] Step 2: Add `auxiliary.skill_distiller` runtime slot with Curator precedence/config. Fork `AIAgent` quiet, bounded, no context files/memory/network, skills/session-search read only; use structured JSON output with max proposals per batch.

- [ ] Step 3: Prompt the model to contrast repeated successful patterns against repeated failure/anti-patterns, preserve existing skill semantics, patch only the relevant section, include evidence/session ids, and avoid credentials/user-specific transient facts.

- [ ] Step 4: Parse/validate proposals: target path under skills root, existing umbrella, base digest/current read, patch size/section constraints, no new top-level skill, no deletes, no secret/threat pattern, supporting ids belong to current batch, confidence/rationale present.

- [ ] Step 5: Create a staged skill write through A12’s `write_approval` origin/run metadata. Do not apply in distiller fork. Pass the proposal to the separate verifier with batch evidence.

- [ ] Step 6: Add invalid JSON/path/contradiction/prompt-injection and no-network tests; commit.

```bash
python -m pytest tests/agent/test_skill_distiller.py tests/agent/test_skill_distiller_e2e.py tests/tools/test_skill_manager_tool.py -q
 git add agent/skill_distiller.py hermes_cli/config.py tools/skill_manager_tool.py tools/skill_provenance.py tests/agent/test_skill_distiller.py tests/agent/test_skill_distiller_e2e.py tests/tools/test_skill_manager_tool.py
 git diff --cached --check
git commit -m "feat(skills): emit guarded batch distillation proposals"
```

## Task 4: Verification/Apply, Watermark, and Metrics Integration

**Files:**
- Modify: `agent/skill_distiller.py`
- Modify: `agent/skill_verifier.py`, `tools/write_approval.py`, `tools/skill_manager_tool.py`
- Modify: `agent/curator_backup.py`, `tools/skill_usage.py`, `agent/insights.py`
- Test: `tests/agent/test_skill_distiller_e2e.py`, verifier/usage tests.

- [ ] Step 1: Add end-to-end state tests.

```python
def test_distiller_stages_then_verifier_applies_existing_skill_patch(tmp_path):
    run = run_distiller_once(tmp_path, sessions=[success_session(), failure_session()])
    assert run.staged_count == 1
    verdict = drain_verification(run.run_id, deterministic_pass=True)
    assert verdict.status == "pass"
    assert live_skill_contains_patch("deployment") is True


def test_base_digest_race_does_not_overwrite_manual_skill_edit(tmp_path):
    run = run_distiller_once(tmp_path, sessions=[success_session()])
    manually_edit_skill("deployment")
    assert drain_verification(run.run_id).status in {"fail", "unverifiable"}
    assert manual_edit_remains() is True
```

- [ ] Step 2: Route staged proposals through plan-12 verifier tiers. Tier 1 script checks apply where declared; Tier 2 rubric uses the batch success/failure evidence; unverifiable remains pending. No special distiller bypass.

- [ ] Step 3: Snapshot skills before verified apply. On pass/user approval, apply patch idempotently via `apply_skill_pending`; on failure/race, retain pending/report and restore snapshot if needed.

- [ ] Step 4: Mark session watermarks only after report plus staging/verification outcome is durable. A failed model run can mark sessions as attempted with retry metadata, but never silently mark them distilled. Make a successful no-proposal dry run idempotent.

- [ ] Step 5: Record metrics by target skill/batch/outcome: sessions selected, success/failure evidence, proposals, staged/passed/failed/unverifiable/applied/rejected, patch size, runtime/cost. Reuse skill usage locking/insights schema.

- [ ] Step 6: Run e2e/race/restart/rollback tests and commit.

```bash
python -m pytest \
  tests/agent/test_skill_distiller_e2e.py \
  tests/agent/test_skill_verifier_e2e.py \
  tests/tools/test_write_approval.py \
  tests/tools/test_skill_manager_tool.py \
  tests/agent/test_curator_backup.py \
  tests/tools/test_skill_usage.py \
  tests/agent/test_insights.py -q
 git add agent/skill_distiller.py agent/skill_verifier.py tools/write_approval.py tools/skill_manager_tool.py agent/curator_backup.py tools/skill_usage.py agent/insights.py tests/agent/test_skill_distiller_e2e.py tests/agent/test_skill_verifier_e2e.py tests/tools/test_write_approval.py tests/tools/test_skill_manager_tool.py tests/agent/test_curator_backup.py tests/tools/test_skill_usage.py tests/agent/test_insights.py
 git diff --cached --check
git commit -m "feat(skills): verify and apply batch distillation"
```

## Task 5: Idle Scheduling, CLI, and Full Verification

**Files:**
- Modify: `cli.py`, `gateway/run.py`
- Modify: `hermes_cli/curator.py` or create `hermes_cli/skill_distiller.py`, gateway/TUI status.
- Modify: config/example/docs.
- Test: `tests/agent/test_skill_distiller_e2e.py`, scheduling/CLI/gateway tests.

- [ ] Add `maybe_run_skill_distiller()` next to Curator ticks with one shared idle/interval guard but independent state/lease. It must not launch while a memory/skill Curator or verifier holds the profile mutation lock.
- [ ] Add `hermes skill-distill status/run/pause/report` with dry-run default, report path, selected/skipped counts, pending/verdict summary, and watermark range. Gateway/TUI exposes status only; approval uses existing pending surfaces.
- [ ] Add config:

```yaml
skill_distiller:
  enabled: false
  interval_hours: 168
  min_idle_hours: 2
  dry_run: true
  max_sessions: 30
  max_batches: 3
  batch_size: 10
  settle_hours: 24
```

- [ ] Run a real temp profile scenario with 12 sessions (verified/failed/interrupted/unknown), parent/fork exclusions, matching/nonmatching tools, batch digest, staged umbrella patch, verifier pass/fail, restart/watermark, and manual skill edit race.
- [ ] Run prompt-injection/secret-redaction tests over transcripts and model proposals.
- [ ] Run no-live-network/no-terminal/no-memory fork tests and cost/time cap tests.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_skill_distiller.py \
  tests/agent/test_skill_distiller_e2e.py \
  tests/agent/test_skill_verifier_e2e.py \
  tests/agent/test_turn_outcome.py \
  tests/agent/test_background_review.py \
  tests/tools/test_write_approval.py \
  tests/tools/test_skill_manager_tool.py \
  tests/tools/test_skill_usage.py \
  tests/agent/test_insights.py \
  tests/agent/test_curator_activity.py \
  tests/hermes_cli/test_write_approval_commands.py -q
python3 -m compileall -q agent/skill_distiller.py
 git diff --check
```

- [ ] Document batch evidence, settled/watermark semantics, target-umbrella-only policy, staging/verifier dependency, dry-run/cost controls, and why trajectory replay is not claimed.
- [ ] Commit scheduling/docs/evidence.

```bash
git add cli.py gateway/run.py hermes_cli/curator.py hermes_cli/skill_distiller.py docs website cli-config.yaml.example tests
 git diff --cached --check
git commit -m "docs(skills): document batch distillation workflow"
```

## Acceptance Checklist

- [ ] Outcomes/watermarks are persisted and legacy unknowns are handled conservatively.
- [ ] Candidate selection excludes forks/superseded parents and is bounded/settled/idempotent.
- [ ] Batches contrast successes/failures and target existing umbrella skills only.
- [ ] Distiller fork has no terminal/network/memory/delegation capability and produces validated structured proposals.
- [ ] Every autonomous patch flows through write approval and plan-12 verification.
- [ ] Base-digest races cannot overwrite manual edits; snapshots/rollback protect apply.
- [ ] Reports/watermarks/metrics survive restart and distinguish staged/applied/rejected/unverifiable.
- [ ] Idle scheduling does not compete with Curator/verifier mutation locks.
- [ ] No transcript secrets or prompt-injection text enter skill files without existing scans/approval.

## Deliberate Simplifications

- Skipped generating new skill files; existing umbrella targeting reduces noise and conflict risk.
- Skipped full trajectory replay; evidence is bounded digest/rubric input, not external side-effect reproduction.
- Skipped online per-turn distillation; existing nudge review remains immediate, this pass is offline catch-up.
- Skipped external provider memory/skill stores; distillation operates on local SessionDB and skill files only.
