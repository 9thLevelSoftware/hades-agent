---
name: action-transactions
description: Preview, revise, commit, reconcile, and compensate bounded action transactions through the hermes transaction CLI. Use when a multi-step change to workspace files, hermes workflow/cron/config state, or delayed outbound messages must be previewable, revisable, and truthfully undoable.
---

# Action Transactions

Terminal-first operation of reversible & revisable action transactions.
Every command below is `hermes transaction ...` (alias `hermes tx ...`;
classic chat: `/transaction ...`).

## Operating rules (non-negotiable)

1. **Create and preview before commit.** Never call commit on a
   transaction you have not previewed in its current revision. Preview
   shows exact classifications per node: `exact` undo, `semantic`
   compensation, or irreversible boundary.
2. **Re-preview after every revision.** A revision supersedes all
   pending prepared work; commit refuses a stale preview.
3. **Use `eligibility` before compensation.** Only `eligible_exact` may
   be described as "exact undo"; `eligible_compensation` is semantic
   compensation — never call it undo. Blocked codes name the reason;
   report them verbatim.
4. **Stop on unknown.** If any node or the transaction reports
   `unknown_effect`, stop all dependent work and run
   `hermes transaction reconcile <tx>` before anything else. Never
   retry an unknown effect.
5. **Reconcile before further dependent work** after any crash,
   interruption, or ambiguous outcome.
6. **Success means a verified receipt.** Only
   `hermes transaction receipt <tx>` showing status `verified` proves
   completion. Handler output, model text, or a committed phase alone
   is `completed_unverified` — say so plainly.

## Exclusions (never attempt through transactions)

- No remote push, no arbitrary shell wrapping, no production databases.
- No browser/service writes, account deletion, or purchases.
- No cross-profile work; every path stays inside the active profile.
- Never claim semantic compensation is exact undo.
- Message sends become irreversible at dispatch unless the platform
  adapter proves edit/delete support; do not promise recall.

## Command flow

```
hermes transaction create --plan plan.yaml --authority authority.yaml
hermes transaction preview <tx>
hermes transaction commit <tx>              # requires transactions.mode: commit
hermes transaction reconcile <tx>           # after any ambiguity
hermes transaction eligibility <tx> [--cascade]
hermes transaction compensate <tx> <node> [--cascade]
hermes transaction receipt <tx> [--recheck]
hermes transaction outbox list <tx>
hermes transaction outbox revise <outbox-id> --message "..." --expected-revision N
hermes transaction outbox cancel <outbox-id>
```

## Plan YAML — three first adapter families (complete, copyable)

```yaml
transaction:
  title: update notes, set timezone, notify channel
  failure_policy: stop

nodes:
  - node_id: write_notes
    adapter_id: workspace.v1
    action: write_file
    args:
      path: notes/status.md
      content: |
        Status update: rollout paused.
  - node_id: set_timezone
    adapter_id: hermes-config.v1
    action: set
    args:
      key: display.theme
      value: night
  - node_id: notify
    adapter_id: message-outbox.v1
    action: send
    args:
      platform: telegram
      target: "telegram:12345"
      message: "Status notes updated; theme switched."
      not_before_seconds: 300

edges:
  - parent: write_notes
    child: set_timezone
  - parent: set_timezone
    child: notify
```

Other built-in adapters: `workspace-git.v1` action `commit_local`
(args: `worktree`, `paths`, `message` — disposable non-main worktrees
only), `hermes-cron.v1` actions `create`/`update`/`disable` (args:
`job` mapping or `job_id` + `updates`), `hermes-workflow.v1` actions
`deploy`/`enable`/`disable` (args: `spec` mapping or `workflow_id` +
`version`).

## Authority YAML (complete, copyable)

```yaml
authority_version: 1
irreversible_policy: ask
issued_at_ms: 0
expires_at_ms: 0        # 0 = fixture/test; set a real epoch-ms deadline
allowed_actions:
  - write_file
  - set
  - send
allowed_resources:
  - "file:notes/status.md"
  - "config:display.theme"
  - "message:telegram:12345"
requester: operator
channel: cli
```

## Truthful vocabulary

- `verified` — independent scorer sealed the outcome.
- `completed_unverified` — it happened, but proof is insufficient.
- `unknown_effect` — Hermes cannot prove whether the effect landed;
  frozen until reconciled.
- `eligible_exact` — byte-exact restoration is currently possible.
- `eligible_compensation` — a declared semantic counter-action exists;
  NOT exact undo.
- Delayed messages: revisable/cancellable until release+dispatch, then
  irreversible.
