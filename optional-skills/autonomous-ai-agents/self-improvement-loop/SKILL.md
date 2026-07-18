---
name: self-improvement-loop
description: Cron reviewer files improvement tasks; kanban workers fix.
version: 1.0.0
author: 9thLevelSoftware, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [self-improvement, kanban, cron, multi-agent, orchestration, automation]
    related_skills: [hermes-agent, plan, kanban-video-orchestrator]
---

# Self-Improvement Loop (Kanban)

## Overview

A standing automation that makes Hermes improve itself — or any project you
point it at — every few hours, without a human driving each step:

```
cron (every N hours)                    kanban dispatcher (gateway tick)
        │                                        │
        ▼                                        ▼
┌──────────────────┐   kanban_create   ┌──────────────────┐
│  REVIEWER        │ ────────────────▶ │  WORKER profile  │
│  (this skill,    │   prioritized     │  (implements ONE │
│  orchestrator:   │   tasks with      │  task, verifies, │
│  survey + file,  │   acceptance      │  then completes  │
│  never implement)│   criteria        │  or blocks for   │
└──────────────────┘                   │  human review)   │
        │                              └──────────────────┘
        ▼                                        │
  delivery report                                ▼
  (or [SILENT])                        review gate: human
                                       unblocks / redirects
```

The split matters: the **reviewer** is a cron-spawned session that only reads
and routes; the **workers** are dispatcher-spawned profiles that only execute
their one assigned task. The kanban board is the durable state between passes
— backlog, audit trail, and human-intervention surface in one place. Both
daemons (cron ticker and kanban dispatcher) already live inside the gateway
process, so one `hermes gateway` powers the whole loop.

This skill runs in three modes. Detect which one applies before acting:

- **Setup mode** — a user interactively asks to set up / install / configure
  the loop. Follow [references/setup.md](references/setup.md).
- **Review mode** — you were spawned by the loop's cron job (non-interactive
  session, prompt says "run the self-improvement review pass"). Run the
  review pass below.
- **Monitor mode** — a user asks how the loop is doing or wants to intervene.
  See [Monitoring and intervention](#monitoring-and-intervention).

## When to Use

- "Have Hermes improve itself every few hours" / "run a self-improvement
  skill on a schedule and implement what it prioritizes"
- Standing unattended improvement of a codebase, skill library, docs tree,
  or ops setup, with a human review gate on the output
- Converting a pile of TODOs / recurring failures into a self-draining queue

Don't use for:

- A one-shot improvement task — just do it, or use `/goal` for iteration
  inside one session.
- Keeping skills/memory tidy — the built-in background review and curator
  already do that; this loop is for goal-directed work beyond library upkeep.
- Sub-hourly reaction to events — use a webhook trigger, not cron.

## The review pass (review mode)

Run these steps in order. The cron prompt supplies the parameters; when one
is missing, use the defaults given in
[references/setup.md](references/setup.md#the-review-prompt) (board
`self-improvement`, cap 3, assignee `improver`).

### 1. Orient

Call `kanban_list` with the loop's `board` — once unfiltered (bounded), and
once per interesting status if the board is busy. Done when you can state:
open count (statuses `triage|todo|ready|running`), blocked count, and what
completed since the previous pass (compare against the previous pass's
delivered report, which lists filed ids).

Pass `board="<loop board>"` explicitly on **every** kanban tool call in this
skill — never rely on the install's current-board pointer, which the user may
switch at any time.

### 2. Backpressure gate

If open count ≥ the cap (default **3**), file nothing this pass. The loop
must drain before it fills. Skip to step 5 and report only status (or
`[SILENT]` if there is also nothing blocked and nothing newly completed).

### 3. Survey and prioritize

Survey the improvement sources named in the cron prompt — read-only, using
the file/web tools you have. You do not have (and must not need) terminal
access: candidates come from reading, not from running things. Typical
sources: repo `TODO`/`FIXME` markers, recent cron output failures under
`~/.hermes/cron/output/`, stale or contradictory docs, gaps the user noted in
memory, previously blocked-then-unblocked tasks whose follow-ups were
deferred.

Score each candidate on impact (how much better does this make the system?),
effort (can one worker session finish it?), and risk (blast radius if wrong).
Pick at most `cap − open` candidates, best first — filing one good task beats
filing three vague ones. Done when each pick has a one-line justification you
will reuse in the report.

### 4. File tasks

For each pick, call `kanban_create` following the conventions in
[references/task-filing.md](references/task-filing.md) — title style, body
template with checkable acceptance criteria and verification commands,
`idempotency_key` (mandatory — it is what makes re-filing across passes
safe), workspace choice, `goal_mode`, `max_runtime_seconds`, and `priority`.
Done when every call returned a task id; if the id belongs to a pre-existing
task (idempotency dedup), say so in the report instead of counting it as new.

Never implement a pick yourself, no matter how small it looks. The reviewer
files; workers execute. A "quick fix" done inline is invisible to the audit
trail and trains the loop to bypass its own review gate.

### 5. Report

Your final response is delivered to the loop's configured channel. Include:
tasks filed this pass (id, title, one-line why), tasks completed since the
last pass (from step 1), and tasks blocked awaiting human review — these are
the user's action items, repeat them every pass until cleared. If all three
lists are empty, respond with only `[SILENT]` so nothing is delivered.

## Monitoring and intervention

- `hermes kanban --board self-improvement list` — board snapshot;
  `hermes dashboard` → Kanban tab for the visual board.
- A task blocked `review-required: …` is waiting on the user: review the
  worker's `kanban_comment` metadata (changed files, tests run, diff/PR),
  then `hermes kanban unblock <id>` to accept follow-ups, or comment feedback
  first — the respawned worker reads the whole thread.
- A stuck task: `kanban_show` it, check heartbeats and run history; comment
  specific redirection rather than deleting and re-filing (the thread is the
  worker's memory).
- Pause the whole loop: `hermes cron pause self-improvement-review` (workers
  finish in-flight tasks; nothing new is filed). Resume with
  `hermes cron resume`.

## Critical rules

1. **The reviewer never implements.** File tasks only. Its cron job gets no
   terminal toolset, so the temptation cannot be acted on.
2. **Cap open work** (default 3). An unattended filer without backpressure
   floods the board and the token budget.
3. **`idempotency_key` on every `kanban_create`.** The same finding
   re-surveyed next pass must collapse into the existing task.
4. **Explicit `board=` on every kanban call.** The current-board pointer is
   user-mutable state; automation must not depend on it.
5. **Code tasks use a `worktree` workspace and never push to the default
   branch.** Workers commit to the task branch and end with a
   `review-required:` block (or a PR link) — a human promotes the change.
6. **Every task body carries checkable acceptance criteria and exact
   verification commands.** A worker that cannot verify must block, not
   complete.
7. **Risky or irreversible actions block for a human.** Deletions,
   config/credential changes, anything outward-facing: file as
   `initial_status="blocked"` or have the worker block before acting.

## Common Pitfalls

1. **Kanban tools missing in the cron session.** Two gates must both pass:
   `kanban` in the job's `enabled_toolsets`, AND `kanban` in the top-level
   `toolsets:` list of the owning profile's `config.yaml` (the tools'
   runtime check reads the profile config; cron sessions have no
   `HERMES_KANBAN_TASK` env to satisfy it otherwise). Setup step 3 covers
   this; if `kanban_list` is absent at review time, report the
   misconfiguration in the delivery instead of silently doing nothing.
2. **Duplicate tasks every pass.** Cause: missing/unstable
   `idempotency_key`. Fix: derive the key from the finding, not the pass
   (see [references/task-filing.md](references/task-filing.md#idempotency-keys)).
3. **Runaway spend.** Cause: no cap, no `max_runtime_seconds`, or an
   unpinned cron model. Pin `provider`/`model` on the job (unpinned jobs
   fail closed if the global default changes — by design).
4. **Worker output vanished.** `scratch` workspaces are deleted on
   completion. Anything worth keeping needs `worktree` or `dir:` — see the
   workspace table in [references/task-filing.md](references/task-filing.md#workspace-selection).
5. **Loop "runs" but nothing happens.** Both the cron ticker and the kanban
   dispatcher live in the gateway process. No gateway → no reviewer runs and
   no worker spawns. Check `hermes cron status` and
   `hermes kanban --board self-improvement list` for tasks stuck in `ready`.
6. **Premature completion by workers.** Vague acceptance criteria let a
   worker declare victory early. Criteria must be checkable commands;
   `goal_mode=True` adds a judge that keeps the worker going until they pass
   (or blocks the task for review when the budget runs out). The judge
   requires an `auxiliary.goal_judge` model in `config.yaml` — without one
   the completion gate is deliberately skipped (fail-open), so `goal_mode`
   is a silent no-op.
7. **Tasks landing in `triage`.** With `kanban.auto_decompose: true`
   (default) the aux-model decomposer rewrites triage cards. This loop files
   fully-specified tasks straight to `todo`/`ready` — never pass
   `triage=True` from the reviewer.

## Verification Checklist

After setup (all commands in [references/setup.md](references/setup.md)):

- [ ] Gateway running; `hermes cron status` shows a live ticker
- [ ] Board exists: `hermes kanban boards list` shows `self-improvement`
- [ ] Worker profile exists: `hermes profile list` shows the assignee
- [ ] Owning profile's `config.yaml` has `kanban` in top-level `toolsets:`
- [ ] Cron job listed with pinned model and the review prompt
- [ ] Manual first pass (`hermes cron run self-improvement-review`) either
      files tasks or delivers/silences correctly
- [ ] Filed task gets claimed by the dispatcher and the worker's run appears
      in `hermes kanban runs <task_id>`
