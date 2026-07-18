# One-time setup

Walk these steps interactively with the user. Each step ends with a check;
do not continue past a failing check.

## Prerequisites

- **Gateway installed and running.** The cron ticker and the kanban
  dispatcher both run inside the gateway process
  (`kanban.dispatch_in_gateway: true` is the default).

  ```bash
  hermes gateway install     # user service; or run `hermes gateway` in foreground
  hermes cron status         # check: ticker heartbeat is recent
  ```

- **Unattended-friendly model auth.** The loop runs while the user is away;
  an OAuth flow that needs a browser mid-run will strand it. Nous Portal
  (`hermes setup --portal`) refreshes automatically; long-lived API keys also
  work.

## Step 1 — Create the board

A dedicated board gives the loop hard isolation: its own SQLite DB,
workspaces, and logs, and dispatcher-spawned workers physically cannot see
other boards.

```bash
hermes kanban boards create self-improvement \
    --name "Self-Improvement" \
    --description "Autonomous improvement loop — filed by the cron reviewer"
```

Do **not** pass `--switch` — the user's current-board pointer should stay
wherever they had it. The loop always passes `board="self-improvement"`
explicitly.

Check: `hermes kanban boards list` shows the slug.

## Step 2 — Create the worker profile

```bash
hermes profile create improver
```

The dispatcher spawns this profile per task (`hermes -p improver …`) with the
kanban lifecycle guidance and task-scoped kanban tools injected automatically
(`HERMES_KANBAN_TASK` env). Its default toolsets already include the
terminal/file/code tools implementation needs — no toolset config required on
the worker side.

If the improvements target a specific repo or domain, seed the profile: set
its default model, and install any specialist skills that tasks will pin via
the `skills` array on `kanban_create`.

Check: `hermes profile list` shows `improver`.

## Step 3 — Open the kanban gate for the reviewer

The kanban tools are schema-gated twice for a cron session: the toolset must
be in the job's `enabled_toolsets` (step 4), **and** the runtime check
requires `kanban` in the top-level `toolsets:` list of the profile that owns
the cron job (there is no `HERMES_KANBAN_TASK` env in a cron session to
satisfy it otherwise).

Edit that profile's `config.yaml` (default profile: `~/.hermes/config.yaml`):

```yaml
toolsets: [hermes-cli, kanban]
```

Keep whatever was already in the list (default is `[hermes-cli]`); append
`kanban`.

Check: in a fresh session, `kanban_list` is callable (orchestrator mode).

## Step 4 — Create the cron job

Create the job from chat (the `cronjob` tool) or the CLI. "Every few hours"
defaults to `every 4h`; any [cron schedule format](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron#schedule-formats)
works.

```python
cronjob(
    action="create",
    name="self-improvement-review",
    schedule="every 4h",
    skills=["self-improvement-loop"],
    enabled_toolsets=["kanban", "file", "web"],   # deliberately NO terminal
    deliver="telegram",                            # or discord/slack/email/local/…
    prompt=REVIEW_PROMPT,                          # see below
)
```

There is no CLI flag for `enabled_toolsets` — `hermes cron create` only
exposes name/deliver/repeat/skill/script/workdir. Create the job from chat
via the `cronjob` tool as above; if the job was created another way, apply
the gate from chat before the first run:

```python
cronjob(action="update", job_id="self-improvement-review",
        enabled_toolsets=["kanban", "file", "web"])
```

Without that gate the reviewer runs with the cron platform's default
toolset — including `terminal` — which violates critical rule #1.

Then **pin the model** so the unattended job never inherits a surprise
provider switch (unpinned jobs fail closed when the global default changes).
`model` is an object in the tool schema; a flat `model="<name>"` string is
silently dropped:

```python
cronjob(action="update", job_id="self-improvement-review",
        model={"provider": "<provider>", "model": "<model>"})
```

### The review prompt

The prompt carries the loop's parameters; the attached skill carries the
procedure. Fill the slots with the user before creating the job:

```text
Run the self-improvement review pass per the self-improvement-loop skill.

Parameters:
- board: self-improvement
- assignee: improver
- open-task cap: 3
- workspace: worktree at /abs/path/to/target/repo   # or: scratch / dir:<path>
- improvement sources, in priority order:
  1. <e.g. failing or flaky areas in /abs/path/to/target/repo>
  2. <e.g. TODO/FIXME markers under src/>
  3. <e.g. recent failures in ~/.hermes/cron/output/>
  4. <e.g. docs that contradict current behavior>

Constraints: <anything off-limits — dirs, files, kinds of change>
If nothing was filed, nothing completed, and nothing is blocked, respond
with only [SILENT].
```

Check: `hermes cron list` shows the job with the right schedule, skill, and
pinned model.

## Step 5 — First pass, watched

Trigger one pass manually and watch it end-to-end:

```bash
hermes cron run self-improvement-review        # claims + runs in this process; blocks until done
hermes kanban --board self-improvement list    # tasks appear once filed
hermes kanban runs <task_id>                   # worker attempt history
hermes dashboard                               # visual board, live
```

Success criteria: the reviewer delivered a report (or `[SILENT]`), any filed
task moved `ready → running` within a dispatcher tick (~60s), and the
worker's run terminated in `done` or a `review-required:` block — not
`crashed`/`gave_up`.

## Tuning

| Knob | Where | Default | Notes |
|---|---|---|---|
| Cadence | job `schedule` | `every 4h` | Slower is usually better; the cap makes faster cadences mostly no-ops. |
| Open-task cap | review prompt | 3 | Raise only after the loop demonstrably drains. |
| Per-task runtime | `max_runtime_seconds` on `kanban_create` | unset | Set for known-bounded work; dispatcher SIGTERMs and re-queues on breach. |
| Worker persistence | `goal_mode=True` on `kanban_create` | off | Judge-driven keep-going until acceptance criteria pass; blocks for review on budget exhaustion. Requires an `auxiliary.goal_judge` model in `config.yaml` — without one the gate is a silent no-op. |
| Delivery | job `deliver` | — | `local` saves to `~/.hermes/cron/output/` only; any connected platform name delivers there. |
| Reply-to-report (global) | `cron.mirror_delivery` in `config.yaml` | off | Makes every cron delivery a continuable conversation. |
| Reply-to-report (this job) | `attach_to_session=True` on the `cronjob` tool | off | Per-job override of the global setting. |

## Teardown / pause

```bash
hermes cron pause self-improvement-review    # stop filing; workers drain
hermes cron remove self-improvement-review   # delete the job
hermes kanban boards rm self-improvement     # archive the board (recoverable)
```
