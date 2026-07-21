# Action Transactions

Reversible & revisable action transactions let Hermes show what a
bounded multi-step action will do, commit each effect under freshly
rechecked authority, revise the still-pending remainder, and undo or
compensate completed steps — only when the underlying service truthfully
makes that possible.

The CLI and the native Ink TUI are the primary control surfaces. The
Dashboard receives the feature through the existing embedded Ink TUI
only. There is no gateway messaging command, no Desktop parity promise,
and no new model-visible tool.

## What a transaction is

A transaction is an immutable plan: a DAG of effect nodes, each bound to
a registered effect adapter and action. Revisions replace only pending
work — a committed node is a fact that later revisions can depend on but
never remove, change, or rewire.

The first adapter families are exactly:

| Adapter | Actions | Undo class |
|---|---|---|
| `workspace.v1` | `write_file`, `patch` | exact (checkpointed, drift-guarded) |
| `workspace-git.v1` | `commit_local` | exact while HEAD matches (reset --mixed) |
| `hermes-workflow.v1` | `deploy`, `enable`, `disable` | semantic (prior immutable version) |
| `hermes-cron.v1` | `create`, `update`, `disable` | semantic (exact prior job; never hard-delete) |
| `hermes-config.v1` | `set` | semantic (single leaf; revision-guarded) |
| `message-outbox.v1` | `send` | cancellable until dispatch, then irreversible |

Excluded by design: remote push, arbitrary shell wrapping, production
databases, browser/service writes, account deletion, purchases, live
commerce or federation, cross-profile actions, and any exactly-once
delivery claim. A provider idempotency key means dedupe support, not
exactly-once.

## Walkthrough

Create the plan and authority files (complete, copyable):

```yaml
# plan.yaml
transaction:
  title: update notes, set theme, notify channel
  failure_policy: stop
nodes:
  - node_id: write_notes
    adapter_id: workspace.v1
    action: write_file
    args:
      path: notes/status.md
      content: "Status update: rollout paused.\n"
  - node_id: set_theme
    adapter_id: hermes-config.v1
    action: set
    args: { key: display.theme, value: night }
  - node_id: notify
    adapter_id: message-outbox.v1
    action: send
    args:
      platform: telegram
      target: "telegram:12345"
      message: "Status notes updated."
      not_before_seconds: 300
edges:
  - { parent: write_notes, child: set_theme }
  - { parent: set_theme, child: notify }
```

```yaml
# authority.yaml
authority_version: 1
irreversible_policy: ask
expires_at_ms: 1790000000000
allowed_actions: [write_file, set, send]
allowed_resources:
  - "file:notes/status.md"
  - "config:display.theme"
  - "message:telegram:12345"
requester: operator
channel: cli
```

Then drive the lifecycle (`hermes tx` is the alias; `/transaction` in
the TUI and classic CLI chat):

```bash
hermes transaction create --plan plan.yaml --authority authority.yaml
hermes transaction preview <tx>       # prepares; NO outward effect
hermes transaction commit <tx>        # requires transactions.mode: commit
hermes transaction show <tx>
hermes transaction graph <tx> --revision 2
hermes transaction revise <tx> --plan plan2.yaml --expected-revision 1 --reason "new recipient"
hermes transaction reconcile <tx>     # classify in-flight effects after a crash
hermes transaction eligibility <tx> [--cascade]
hermes transaction compensate <tx> <node> [--cascade]
hermes transaction receipt <tx> [--recheck]
hermes transaction outbox list <tx>
hermes transaction outbox revise <outbox-id> --message "final" --expected-revision 1
hermes transaction outbox cancel <outbox-id>
```

## Statuses and eligibility codes

Transaction statuses: `draft`, `previewing`, `ready`, `committing`,
`committed`, `revising`, `compensating`, `compensated`,
`partially_compensated`, `blocked`, `failed`, `unknown_effect`,
`cancelled`.

Effect phases: `planned`, `prepared`, `previewed`, `committing`,
`committed`, `verified`, `superseded`, `compensating`, `compensated`,
`blocked`, `failed`, `unknown_effect`.

Eligibility codes (truthful vocabulary — the UI never rounds these up):

- `eligible_exact` — byte-exact restoration is possible right now.
- `eligible_compensation` — a declared semantic counter-action exists.
  This is **not** exact undo and is never displayed as undo.
- `already_compensated`, `blocked_live_dependents`,
  `blocked_irreversible_boundary`, `blocked_unknown`, `blocked_drift`,
  `blocked_window_expired`, `blocked_authority`, `unsupported`.

`unknown_effect` means Hermes cannot prove whether an outward effect
landed. Nothing retries it automatically; run
`hermes transaction reconcile <tx>` and review.

## Approvals and expiry

An irreversible effect always needs its exact approval binding: the
approval is bound to transaction id, revision, node, argument hash,
preview hash, resource set, authority version, requester, and channel,
and it expires and is consumed exactly once. Session or permanent
allowlisting never releases an irreversible effect.

## Crash and unknown recovery

At CLI/TUI/gateway startup a bounded recovery pass runs after the
owner-fenced operation-journal reconciliation: in-flight effects are
classified through their adapters (`landed`, `not_landed`, `unknown`),
never blindly retried. `not_landed` work resumes only through an
explicit later `commit`.

## Storage and privacy

All state is profile-local under your Hermes home (shown by
`hermes profile`): `state.db` holds the transaction aggregate, approval
bindings, compensation journal, outbox rows, and receipts; the shadow
checkpoint store holds exact workspace before-states. Deleting a
profile removes all of it. Receipts export through the existing
`hermes receipt export` path with the same redaction rules. Nothing is
uploaded; the benchmark reports below are local files.

## Configuration

```yaml
transactions:
  mode: preview            # off | preview | commit
  auto_reconcile_on_start: true
  recovery_batch_size: 100        # 1..1000
  outbox_max_delay_seconds: 86400 # 1..604800
  compensation_default: manual    # manual | compensate_prefix
```

- `off` — no transaction preview/commit; recovery reads still work.
- `preview` (default) — everything except commit/release/compensate.
- `commit` — all three first adapter families, subject to authority.

## Staged rollout

1. Dark schema + recovery read path with `mode: off` on internal builds.
2. Default `mode: preview`; dogfood previews and local receipts.
3. Opt-in `mode: commit` for designated test profiles only after all
   100 preregistered benchmark cases pass.
4. Commit stays opt-in until 30 real CLI/TUI transactions across the
   three adapter families show zero unauthorized irreversible commits,
   zero duplicates, every unknown surfaced, correct compensation order,
   and under 15% median eligible overhead.
5. Rollout stops on any false verified receipt, unclassified
   irreversible effect, approval replay, cross-profile write, duplicate
   instrumented effect, or compensation across a forbidden boundary.
   A preregistered gate is never relaxed after results.

Run the local benchmark:

```bash
uv run python benchmarks/transactions/runner.py \
  --manifest benchmarks/transactions/manifest.yaml \
  --repeats 5 --output-json build/transaction-benchmark.json
```

The report includes the denominator, exclusions, Wilson 95% intervals
per stratum, p50/p95 latency for baseline and candidate, the median
eligible overhead ratio, and separate zero-count safety metrics — never
a composite score.
