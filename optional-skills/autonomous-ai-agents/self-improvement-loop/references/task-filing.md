# Task-filing conventions

Every task the reviewer files is a contract with a worker that has **no
other context** — a fresh dispatcher-spawned session that reads the title,
body, and comment thread, and must end its run in exactly one of
`kanban_complete`, `kanban_block`, or a crash. Write the card so that a
stranger could execute it and a skeptic could verify it.

## The create call

```python
kanban_create(
    board="self-improvement",              # always explicit
    title="fix: flaky retry test in cron scheduler",
    assignee="improver",
    body=BODY,                             # template below
    idempotency_key="si:repo-todo:cron-retry-flake",
    workspace_kind="worktree",
    workspace_path="/abs/path/to/target/repo",
    priority=2,
    goal_mode=True,                        # judge-enforced completion (needs auxiliary.goal_judge)
    max_runtime_seconds=3600,              # when the work is bounded
    skills=["test-driven-development"],    # optional specialist context
)
```

## Body template

```markdown
## Goal
One sentence: the end state that must be true when this task is done.

## Context
- Files / paths involved (absolute or repo-relative)
- What the reviewer observed (error text, TODO text, doc excerpt)
- Prior related tasks, if any (ids)

## Acceptance criteria
- [ ] Each criterion is a checkable fact, not an intention
- [ ] "tests in tests/cron/ pass" beats "improve reliability"

## Verification
Exact commands to run and their expected outcome, e.g.:
- `scripts/run_tests.sh tests/cron/` → exit 0
- `grep -c TODO src/foo.py` → 0

## Constraints
- Commit to the task branch only; NEVER push to the default branch.
- Out of scope: <adjacent things the worker must not touch>
- If verification cannot pass, revert your changes and block with the
  failure — do not complete.

## Handoff
When the change needs human review (any code or config change does):
1. `kanban_comment` whose body carries the structured payload as
   markdown: changed files, tests run, diff path or PR url, decisions
   made. (The comment tool has no metadata parameter — structured
   `metadata=` belongs on `kanban_complete` only.)
2. `kanban_block(kind="needs_input", reason="review-required: <one-line summary>")`.
   The `kind` is required on goal_mode tasks — a bare block is rejected
   so workers can't sidestep the completion judge.
Only truly terminal chores (typo fix, docs touch-up with verification
passing) may `kanban_complete` directly — pass the same structured
payload via `metadata=` there.
```

The kanban lifecycle itself (complete/block/heartbeat semantics) is injected
into the worker's system prompt automatically — the body adds only the
loop-specific contract above.

## Priority rubric

`priority` is the dispatcher's tiebreaker among ready tasks sharing an
assignee — higher runs sooner.

| Pick when | priority |
|---|---|
| Fixes something actively failing (test, cron job, broken doc command) | 3 |
| Removes recurring friction (flake, slow path, repeated manual step) | 2 |
| Nice-to-have polish, speculative refactor | 1 |

Impact × effort gate before filing at all: skip candidates a single worker
session can't finish — split them (see [Linking](#linking-multi-step-work))
or leave them for the user.

## Workspace selection

| Kind | Use for | Lifetime |
|---|---|---|
| `worktree` + `workspace_path` | Any change to a git repo. Worker gets an isolated branch; nothing touches the user's checkout. | **Preserved** on completion |
| `dir:` absolute path | Shared non-repo state (docs tree, vault, ops dir) | **Preserved** on completion |
| `scratch` (default) | Research/analysis whose only output is the summary + comments | **Deleted** on completion (managed paths) |

When `workspace_path` points at a git repo root, the worker does not run in
that path — it lands in `<repo>/.worktrees/<task_id>` on a dedicated task
branch. Look for diffs there, not in the checkout you named.

Never put an artifact you want to keep in `scratch` — dispatcher-created
scratch dirs (under the board's managed `workspaces/` root) are removed when
the task completes. Don't rely on the wipe for hygiene either: a scratch
path outside the managed root is preserved with a warning instead of
deleted. Research tasks should paste their findings into the completion
`summary`/`metadata`, not leave files behind.

## Idempotency keys

Mandatory on every create. Derive the key from the **finding**, so the same
candidate re-surveyed on a later pass dedups into the existing card instead
of duplicating it:

```
si:<source>:<stable-slug>
si:repo-todo:cron-retry-flake        # from a TODO marker
si:cron-output:morning-digest-429    # from a recurring cron failure
si:docs-drift:kanban-cli-flags       # from a stale doc
```

Never include a timestamp or pass number — that defeats the dedup. If
`kanban_create` returns an id you can see already existed, report it as
"already filed", not as new work.

## Linking multi-step work

For an improvement that genuinely needs stages, file the stages as separate
tasks and wire dependencies with `parents` (or `kanban_link` after the
fact): children stay in `todo` until every parent is `done`, then
auto-promote to `ready`. Count **all** of them against the open-task cap.
Don't over-decompose — the smallest graph that isolates the human review
gate is the right size; most improvements are one task.

## The review gate

The loop's safety hinges on `review-required:` blocks:

- The worker blocks instead of completing whenever a human should look
  before the change takes effect (code, config, anything outward-facing).
- The reviewer repeats blocked tasks in every delivery report until the
  user acts — they are the loop's inbox.
- The user reviews the comment metadata, then either
  `hermes kanban unblock <id>` (worker respawns with the full thread for
  follow-ups) or comments feedback first and unblocks.
- For work that must not even start without a human go-ahead, file with
  `initial_status="blocked"` and a `review-required: approve before start`
  reason.
