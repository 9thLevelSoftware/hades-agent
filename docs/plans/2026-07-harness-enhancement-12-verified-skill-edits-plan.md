# Verified Skill Edits and Regression-Gate Implementation Plan

> For agentic workers: the acting review/curator fork must never grade its own skill write. Keep existing foreground behavior unchanged, stage all background-origin edits when verification is enabled, and give the verifier execution rights only inside a scratch sandbox with skill-write rights removed.

**Goal:** Verify autonomous background/curator skill edits before committing them, using deterministic script checks where possible and a separate verifier agent/rubric for prose/trajectory skills. Failed or unverifiable edits remain in the existing pending-write queue with verdict evidence; accepted/rejected outcomes are logged for learning-loop metrics.

**Architecture:** Extend `write_approval` records with verification/session evidence. At background review/curator completion and periodic orphan drains, claim pending skill writes lacking verdict. Materialize the staged post-edit skill into a temp sandbox. Tier 1 runs declared skill scripts/tests; Tier 2 compares a bounded stored trajectory/rubric without replaying unsafe external side effects; Tier 3 marks unverifiable. A distinct `agent/skill_verifier.py` fork has terminal/execute/files read/run access but no `skill_manage`/memory writes and a `skill_verifier` origin rejected by skill-write guards. Passes can auto-apply only when user write-approval policy permits; foreground/user approval always wins. Fail/unverifiable returns to the existing CLI/gateway pending surface.

**Tech Stack:** `tools/write_approval.py`, `tools/skill_manager_tool.py`, `tools/skill_provenance.py`, `agent/background_review.py`, `agent/curator.py`, `agent/curator_backup.py`, `hermes_state.SessionDB`, `tools/skill_usage.py`, `agent/insights.py`, `hermes_cli/write_approval_commands.py`, sandboxed terminal/docker/local environment, auxiliary runtime resolution.

## Global Constraints

- Foreground skill writes are unchanged unless the user explicitly requests verification; `skills.verify_background_writes` affects only background_review/curator origins.
- User `write_approval=on` always wins: a passing verifier does not auto-apply a write that still requires user approval.
- The acting fork cannot invoke verifier tools or grade its own output. The verifier cannot call `skill_manage`, write skills, mutate memory, delegate, access credentials, or use network by default.
- Verification occurs against a materialized scratch copy, never the live skills directory. A failed test cannot alter production files.
- Script execution is only for declared, bounded skill scripts/tests and uses A9/local/docker sandbox when configured; arbitrary `SKILL.md` commands are not executed.
- Stored trajectory “verification” is rubric/evidence grading, not side-effectful replay. The verdict must say which tier ran.
- Auto-apply only on a deterministic pass and a clean pending-record claim. Races with user apply/reject resolve by claim/idempotency; never apply twice.
- Every verdict includes verifier model/runtime, tier, commands/evidence, score/reason, duration, and policy decision; redact secrets/raw output before persistence.
- Per-skill/total verification cost and runtime are bounded; failure/timeout becomes unverifiable/pending, not an infinite retry.
- Curator snapshot/rollback runs before any auto-apply, and the existing pending write can still be discarded/approved manually.
- Tests must use real staged files/temp sandboxes and a fake verifier model, not only mocks of the gate.

## Current-State Review

- `write_approval` already has generic memory/skills subsystem records, staged payloads, list/get/discard, and CLI/gateway approval.
- `skill_manager_tool._apply_skill_write_gate`/`apply_skill_pending` are the enforcement/replay seams; `_skill_gate_bypass` is only for trusted replay.
- Skill provenance tracks foreground/background origin and read-before-write/consolidation guards already reject unsafe background behavior.
- `background_review` intentionally has a skills/memory whitelist with no terminal/execute; the proposed verifier must be a separate fork, not a relaxed acting fork.
- `curator` has a different fork that may access terminal for archive work but has no behavioral verification; it needs the same verifier drain and origin restrictions.
- `curator_backup` snapshots skills/cron; `skill_usage`/`insights` can hold accept/reject metrics.
- SessionDB stores full conversations for bounded trajectory evidence, but replaying arbitrary side effects is unsafe and must not be the default.

The plan skips automatic networked trajectory replay and full formal skill correctness; it verifies what can be proven and queues the rest.

## Release Order

1. Verification record schema/claiming and background gate.
2. Scratch materialization and deterministic script tier.
3. Separate verifier fork/rubric tier and verdict policy.
4. Drain hooks, apply/rollback, metrics, and review surfaces.
5. Adversarial/full verification.

## File Map

- Create: `agent/skill_verifier.py` — verifier fork, tier selection, verdict normalization.
- Modify: `tools/write_approval.py` — verification block, claim/complete/idempotency fields.
- Modify: `tools/skill_manager_tool.py` — mandatory background staging, verifier-origin denial, safe apply.
- Modify: `tools/skill_provenance.py` — `skill_verifier` origin.
- Modify: `agent/background_review.py`, `agent/curator.py` — verification drain triggers.
- Modify: `agent/curator_backup.py` — pre-apply snapshot integration if not already generic.
- Modify: `tools/skill_usage.py`, `agent/insights.py` — pass/fail/unverifiable/accept-rate metrics.
- Modify: `hermes_cli/write_approval_commands.py`, `gateway/slash_commands.py`, `hermes_cli/cli_commands_mixin.py` — verdict display/review.
- Modify: `hermes_cli/config.py`, `cli-config.yaml.example` — verifier config/auxiliary slot.
- Test: new `tests/agent/test_skill_verifier.py`, `tests/agent/test_skill_verifier_e2e.py`.
- Test: extend `tests/tools/test_write_approval.py`, `tests/tools/test_skill_manager_tool.py`, `tests/agent/test_background_review.py`, curator/usage/CLI/gateway suites.

## Data Contracts

```python
@dataclass(frozen=True)
class SkillVerificationVerdict:
    pending_id: str
    tier: Literal[1, 2, 3]
    status: Literal["pass", "fail", "unverifiable", "timeout", "error"]
    score: float | None
    evidence: tuple[str, ...]
    reason: str
    verifier_runtime: str
    elapsed_seconds: float
    created_at: float
```

```python
@dataclass(frozen=True)
class SkillVerificationPolicy:
    enabled: bool
    auto_apply_pass: bool
    max_seconds: float
    max_output_chars: int
    tier1_commands: tuple[str, ...]
    tier2_min_score: float
    require_user_approval_for_new_skills: bool
```

Pending records gain:

```json
{
  "verification": {
    "status": "pass|fail|unverifiable|pending",
    "verdict_id": "uuid",
    "tier": 1,
    "session_id": "...",
    "source_run_id": "...",
    "claimed_by": "...",
    "applied_at": null
  }
}
```

## Task 1: Stage Background Writes and Add Verification Records

**Files:**
- Modify: `tools/write_approval.py`
- Modify: `tools/skill_manager_tool.py`, `tools/skill_provenance.py`
- Modify: `hermes_cli/config.py`
- Test: `tests/tools/test_write_approval.py`, `tests/tools/test_skill_manager_tool.py`

- [ ] Step 1: Add gate/record tests.

```python
def test_background_skill_write_is_staged_when_verification_enabled(tmp_path):
    result = apply_skill_write(origin="background_review", config=verify_config(), payload=skill_payload())
    assert result["status"] == "staged"
    record = load_pending(result["pending_id"])
    assert record["verification"]["status"] == "pending"


def test_foreground_write_behavior_is_unchanged():
    result = apply_skill_write(origin="foreground", config=verify_config(), payload=skill_payload())
    assert result["status"] in {"success", "staged"}


def test_skill_verifier_origin_cannot_write_skills():
    result = apply_skill_write(origin="skill_verifier", config=verify_config(), payload=skill_payload())
    assert result["status"] == "error"
```

- [ ] Step 2: Add `skills.verify_background_writes` default `false` for the initial rollout so existing installations do not change behavior; when explicitly enabled it stages background writes even if `skills.write_approval=false`, while `auto_apply_pass=false` remains the safe default. Define a later rollout gate that can flip the staging default only after pass/fail/unverifiable metrics exist.

- [ ] Step 3: Extend pending records with source session/parent pointer, origin, skill path/base digest, verification state, claim token/lease, and idempotent apply/verdict ids. Keep old records readable with `verification.status=not_required`.

- [ ] Step 4: Make `_apply_skill_write_gate` stage background changes before any live mutation; make `_background_review_write_guard` and new `skill_verifier` guard reject verifier writes even under gate bypass unless the trusted apply path sets the existing bypass context.

- [ ] Step 5: Add claim/complete/discard transitions under the existing pending directory lock. A claimed record with expired lease can be reclaimed; an applied/rejected record cannot be claimed.

- [ ] Step 6: Run focused tests and commit.

```bash
python -m pytest tests/tools/test_write_approval.py tests/tools/test_skill_manager_tool.py tests/tools/test_skill_provenance.py -q
 git add tools/write_approval.py tools/skill_manager_tool.py tools/skill_provenance.py hermes_cli/config.py tests/tools/test_write_approval.py tests/tools/test_skill_manager_tool.py tests/tools/test_skill_provenance.py
 git diff --cached --check
git commit -m "feat(skills): stage background writes for verification"
```

## Task 2: Materialize Scratch Skills and Deterministic Script Tier

**Files:**
- Create/modify: `agent/skill_verifier.py`
- Modify: `tools/skill_manager_tool.py` only to expose parameterized pending materialization/apply.
- Modify: `tools/environments` selection only through existing sandbox API.
- Test: `tests/agent/test_skill_verifier.py`, `tests/agent/test_skill_verifier_e2e.py`

- [ ] Step 1: Add real filesystem/scratch tests.

```python
def test_materialized_skill_does_not_modify_live_skill_dir(tmp_path):
    live = create_skill(tmp_path / "skills", "deploy", script="print('ok')")
    pending = stage_skill_patch(live, "print('changed')")
    scratch = materialize_pending_skill(pending, tmp_path / "scratch")
    scratch.write_text("print('scratch')")
    assert live.read_text() == "print('ok')"


def test_tier_one_runs_only_declared_bounded_script(tmp_path):
    pending = stage_skill_with_declared_check(tmp_path, command="python check.py")
    verdict = verify_skill(pending, policy=policy(tier1_commands=("python check.py",)))
    assert verdict.tier == 1
    assert verdict.status == "pass"
```

- [ ] Step 2: Materialize base skill plus staged payload into a new temp directory; preserve relative paths/permissions needed for scripts but reject symlinks escaping scratch.

- [ ] Step 3: Discover Tier 1 checks from explicit skill metadata/config only. Allow Python/module/shell command through existing local/docker/A9 sandbox, with cwd scratch, no network/credentials, timeout, output cap, and process-group cleanup. Do not execute arbitrary prose fenced blocks.

- [ ] Step 4: Normalize exit code/output into verdict evidence. A missing declared check is `unverifiable`, nonzero/timeout is `fail`/`timeout`; never convert a skipped check to pass.

- [ ] Step 5: Add tests for path traversal, symlink escape, command timeout, output cap, no credentials/network, and cleanup.

- [ ] Step 6: Run verifier/scratch tests and commit.

```bash
python -m pytest tests/agent/test_skill_verifier.py tests/agent/test_skill_verifier_e2e.py tests/tools/test_skill_manager_tool.py -q
 git add agent/skill_verifier.py tools/skill_manager_tool.py tests/agent/test_skill_verifier.py tests/agent/test_skill_verifier_e2e.py
 git diff --cached --check
git commit -m "feat(skills): verify executable edits in scratch"
```

## Task 3: Separate Verifier Fork and Trajectory-Rubric Tier

**Files:**
- Modify: `agent/skill_verifier.py`
- Modify: `agent/background_review.py`, `agent/curator.py`
- Modify: `hermes_cli/config.py`
- Test: verifier/fork/background/curator suites.

- [ ] Step 1: Add fake-runtime tests proving tool isolation.

```python
def test_verifier_fork_can_execute_but_cannot_write_skills():
    spec = build_skill_verifier_fork_spec()
    assert {"terminal", "execute", "files"}.issubset(spec.toolsets)
    assert "skill_manage" not in spec.tool_names
    assert "memory" not in spec.toolsets
    assert spec.write_origin == "skill_verifier"


def test_trajectory_tier_is_honest_about_non_replay():
    verdict = grade_trajectory_rubric(skill=skill_payload(), transcript=stored_transcript(), runtime=stub_runtime())
    assert verdict.tier == 2
    assert "rubric" in verdict.evidence[0]
```

- [ ] Step 2: Add auxiliary `skill_verifier` slot and reuse `_resolve_review_runtime` precedence. Set `skip_context_files=True`, `skip_memory=True`, `_persist_disabled=True`, bounded iterations, quiet output, and no network by default.

- [ ] Step 3: Build verifier tool definition from skills/terminal/execute/files but remove `skill_manage`, memory, delegation, MCP, network, and any write path outside scratch. Add a hard origin guard as defense in depth.

- [ ] Step 4: Implement Tier 2 rubric input: skill diff, skill metadata/usage, bounded stored trajectory evidence from SessionDB (or supplied source session), expected outcome/tool sequence, and failure excerpts. The verifier judges coverage/clarity/consistency/regression risk; it does not replay side effects.

- [ ] Step 5: Use score threshold plus hard-fail rules. A pass requires no hard fail and score ≥ configured threshold; a fail requires clear contradiction/broken behavior; ambiguous/insufficient evidence is unverifiable.

- [ ] Step 6: Add separate verifier origin to provenance and ensure any attempted skill write is denied. Run fork/rubric tests.

```bash
python -m pytest \
  tests/agent/test_skill_verifier.py \
  tests/agent/test_background_review.py \
  tests/agent/test_curator_activity.py \
  tests/tools/test_skill_provenance.py -q
```

- [ ] Step 7: Commit verifier fork/rubric.

```bash
git add agent/skill_verifier.py agent/background_review.py agent/curator.py hermes_cli/config.py tests/agent/test_skill_verifier.py tests/agent/test_background_review.py tests/agent/test_curator_activity.py tests/tools/test_skill_provenance.py
git diff --cached --check
git commit -m "feat(skills): add isolated verifier fork"
```

## Task 4: Queue Drain, Verdict Policy, Apply, and Rollback

**Files:**
- Modify: `agent/skill_verifier.py`
- Modify: `agent/background_review.py`, `agent/curator.py`
- Modify: `tools/skill_manager_tool.py`, `tools/write_approval.py`
- Modify: `agent/curator_backup.py`
- Test: `tests/agent/test_skill_verifier_e2e.py`, approval/curator tests.

- [ ] Step 1: Add e2e state-machine tests.

```python
def test_passing_background_skill_auto_applies_only_when_policy_allows(tmp_path):
    pending_id = stage_background_skill(tmp_path)
    verdict = complete_verification(pending_id, pass_verdict(tier=1))
    assert maybe_apply_verified(pending_id, config=auto_apply_config()) == "applied"
    assert load_pending(pending_id)["verification"]["applied_at"] is not None


def test_user_approval_policy_overrides_verifier_pass(tmp_path):
    pending_id = stage_background_skill(tmp_path)
    complete_verification(pending_id, pass_verdict(tier=1))
    assert maybe_apply_verified(pending_id, config=user_approval_required_config()) == "staged"
    assert skill_is_live(pending_id) is False


def test_failed_or_unverifiable_verdict_stays_pending():
    pending_id = stage_background_skill(tmp_path)
    complete_verification(pending_id, verdict(status="unverifiable", tier=2))
    assert maybe_apply_verified(pending_id, config=auto_apply_config()) == "staged"
```

- [ ] Step 2: Add `drain_skill_verification_queue(origin_filter=...)` that claims records, materializes/verifies, writes verdict, then applies only according to policy. Drain after background review/curator returns and from periodic tick for crash orphans.

- [ ] Step 3: Before auto-apply, create/record Curator skill snapshot. Invoke existing `apply_skill_pending` under trusted gate bypass only after clean claim/verdict. On apply failure, keep pending and restore snapshot if partial mutation occurred.

- [ ] Step 4: Make manual CLI/gateway pending list show verification status/tier/score/reason/evidence summary and allow user approve/reject/discard independent of verifier. Manual approve can apply a failed/unverifiable record only with explicit user action and existing gate semantics.

- [ ] Step 5: Update background-review notification text to distinguish committed/verified, queued-pending, rejected, and error. No “success” for a staged write.

- [ ] Step 6: Run e2e queue/apply/race/rollback tests and commit.

```bash
python -m pytest \
  tests/agent/test_skill_verifier_e2e.py \
  tests/tools/test_write_approval.py \
  tests/tools/test_skill_manager_tool.py \
  tests/agent/test_curator_backup.py \
  tests/agent/test_background_review.py \
  tests/hermes_cli/test_write_approval_commands.py -q
 git add agent/skill_verifier.py agent/background_review.py agent/curator.py tools/skill_manager_tool.py tools/write_approval.py agent/curator_backup.py tests/agent/test_skill_verifier_e2e.py tests/tools/test_write_approval.py tests/tools/test_skill_manager_tool.py tests/agent/test_curator_backup.py tests/agent/test_background_review.py tests/hermes_cli/test_write_approval_commands.py
 git diff --cached --check
git commit -m "feat(skills): gate background edits on verdicts"
```

## Task 5: Metrics, Review Surfaces, and Full Verification

**Files:**
- Modify: `tools/skill_usage.py`, `agent/insights.py`
- Modify: CLI/gateway pending surfaces/config/docs.
- Test: skill usage/insights/CLI/gateway/e2e suites.

- [ ] Add counters by origin/skill/tier/status: verifier runs, pass/fail/unverifiable/timeout, auto-applied, user-approved, rejected, rollback, and median duration. Preserve existing usage schema and locking.
- [ ] Surface accept-rate and pending verification breakdown in insights/status without exposing raw transcript/secret evidence.
- [ ] Document deterministic tier, rubric tier, unverifiable semantics, sandbox/aux-model controls, auto-apply/user-approval precedence, and rollback.
- [ ] Run adversarial tests: verifier attempts skill write, path/symlink escape, prompt injection in skill/trajectory, malicious declared command, command timeout, race with manual approve/reject, crash/orphan reclaim, and provider/credential isolation.
- [ ] Run a real temp profile scenario: background review stages a skill, verifier pass applies in scratch/live safely, restart preserves metrics/pending, failing edit remains reviewable, rollback restores prior skill.

```bash
HERMES_HOME="$(mktemp -d)" python -m pytest \
  tests/agent/test_skill_verifier.py \
  tests/agent/test_skill_verifier_e2e.py \
  tests/tools/test_write_approval.py \
  tests/tools/test_skill_manager_tool.py \
  tests/tools/test_skill_provenance.py \
  tests/agent/test_background_review.py \
  tests/agent/test_curator_activity.py \
  tests/agent/test_curator_backup.py \
  tests/tools/test_skill_usage.py \
  tests/agent/test_insights.py \
  tests/hermes_cli/test_write_approval_commands.py -q
python3 -m compileall -q agent/skill_verifier.py
 git diff --check
```

- [ ] Commit docs/evidence.

```bash
git add tools/skill_usage.py agent/insights.py docs website cli-config.yaml.example tests
 git diff --cached --check
git commit -m "docs(skills): document verified autonomous edits"
```

## Acceptance Checklist

- [ ] Background skill writes stage when verification is enabled; foreground behavior is unchanged.
- [ ] Verifier is a separate fork with execution only in scratch and no skill/memory write capability.
- [ ] Tier 1 deterministic checks, Tier 2 transcript rubric, and Tier 3 unverifiable outcomes are explicit.
- [ ] No side-effectful arbitrary trajectory replay is claimed.
- [ ] Pass auto-apply obeys user approval policy; fail/unverifiable remain pending.
- [ ] Pending claims/races/crash recovery are idempotent and safe.
- [ ] Curator snapshots/rollback protect live skill state.
- [ ] CLI/gateway review shows machine verdict evidence and correct staged status.
- [ ] Metrics provide origin/tier/status accept-rate without raw sensitive data.
- [ ] Initial default keeps verification opt-in; any future default-on rollout is tied to observed verifier metrics and an explicit migration/config change.

## Deliberate Simplifications

- Skipped automatic full trajectory replay; stored transcripts support rubric evidence, not safe reproduction of side effects.
- Skipped networked/browser verification; the verifier sandbox is offline by default.
- Skipped verifier auto-apply for new skills unless deterministic Tier 1 passes and policy explicitly allows it.
- Skipped changing foreground write approvals; this plan is for autonomous background/curator edits.
