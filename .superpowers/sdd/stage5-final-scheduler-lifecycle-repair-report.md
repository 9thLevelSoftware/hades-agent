# Stage 5 Final Scheduler and Lifecycle Repair Report

## Outcome

This repair closes the four final Stage 5 scheduler/lifecycle gaps without
adding another management receipt phase and without changing the stage ledger.

## Implemented contracts

### Scheduler launch authority

- Script launch capabilities are issued only when the current cron job still
  matches the immutable snapshot dispatched by the scheduler.
- The bound projection includes user-controlled launch authority (including
  schedule, script, workdir, and `repeat.times`) while excluding scheduler-only
  execution bookkeeping (`next_run_at`, run/fire claims, prior run/delivery
  outcomes, and `repeat.completed`).
- User-facing job updates revoke outstanding capabilities, including edit/revert
  ABA sequences.
- The real recurring `tick()` and finite one-shot `run_one_job()` paths are
  covered, not only direct helper mocks.

### Scheduled management authority

- A scheduled invocation binds the full routing `authority_revision`, the
  management authority revision, management control generation/job ID, and the
  exact managed-job fingerprint.
- Revalidation occurs before planning, per-profile, and at precommit.
- The precommit path acquires a strict cross-process cron-store lock after the
  profile config lock and holds it through the config/SQLite mutation boundary.
  Failure or contention is fail-closed.
- Windows strict locking uses bounded nonblocking `msvcrt` polling rather than
  an unbounded blocking call.

### Post-commit lifecycle finalization

- A schema-v9 `management_lifecycle_finalizations` journal records the exact
  pending promote/rollback settlement in the same SQLite transaction that
  commits the existing four-phase config receipt.
- The journal binds receipt, transition revision, challenger revision,
  management authority, profile, expected state generation, action/event,
  rejection count, reason, and immutable intended settlement time.
- Settlement and journal finalization occur atomically under config and SQLite
  locks. Replays are idempotent and do not duplicate lifecycle events.
- If settlement fails after config commit, the journal stays pending. A
  best-effort global freeze does not destroy the recoverable canary state; even
  if freezing also fails, automatic recovery can replay the exact journal.
- Pending journals gate reconciliation, canary advancement, control mutation,
  and runtime management overlays.
- Ambiguous authority, receipt, revision lineage, profile state generation, or
  persisted journal content fails closed.

## Deterministic regressions added

- Changed job before capability issuance.
- Schedule/script/workdir mutation and edit/revert ABA rejection.
- Real recurring and finite one-shot scheduler bookkeeping acceptance.
- Strict cron lock unavailable and Windows contention failure.
- Full profile-only authority edit after scheduled assertion.
- Same-ID job replacement after planning and at the config mutation boundary,
  with zero config/revision/receipt/admission mutation.
- Committed-receipt settlement recovery with immutable cooldown timestamp.
- Duplicate journal replay without duplicate lifecycle events.
- Settlement failure plus freeze failure, pending-work gates, and automatic
  recovery.
- Runtime overlay suppression while a lifecycle journal is pending.
- v8-to-v9 finalization-journal schema migration and complete current schema.
- Multi-profile global-lineage settlement.

## Verification evidence

- Initial RED: the three primary regressions failed for unsupported dispatched
  snapshots/full authority binding and absent lifecycle journal storage.
- Focused scheduler regressions: `8 passed in 40.98s`.
- Focused scheduled-management/lifecycle regressions: `4 passed`, then the
  corrected freeze-failure regression `1 passed`.
- Full Stage 5 management group: `238 passed`; its sole failure exposed and led
  to correction of the multi-profile journal-link invariant. The corrected
  regression passed, and the affected assignment/reconciler modules were then
  rerun: `93 passed in 164.54s`.
- Full touched cron module: `31 passed in 43.51s`.
- Adaptation/config/evidence/foundation/inventory split: `539 passed, 10
  skipped in 228.36s`.
- Resolver/selector/stages 2-4/storage split: `674 passed`; its sole failure was
  a prohibited literal `schema_version == 8` change-detector assertion. That
  assertion now relates to `SCHEMA_VERSION`, and the exact migration regression
  passed after correction.
- Final journal replay + v8 migration checks: `2 passed in 2.14s`.
- Ruff on every touched Python file: `All checks passed!`.
- `py_compile` on every touched production Python module: exit 0.
- `git diff --check`: exit 0.

The monolithic `tests/plugins/auto_routing` invocation remained CPU-active but
hit the command's 10-minute process limit without returning a result, so the
suite was split into the independently reported groups above.

The repository-required `scripts/run_tests.sh` probe cannot execute in this
native Windows worktree because it only recognizes Unix-style `.venv/bin`
layouts. Its exact result was:

```text
error: no virtualenv found in /c/Users/dasbl/hermes-agent/.worktrees/auto-routing/.venv or /c/Users/dasbl/hermes-agent/.worktrees/auto-routing/venv
```

Substantive verification used `.venv\Scripts\python.exe` (created/resolved by
`uv run --with pytest`) and is recorded above.
