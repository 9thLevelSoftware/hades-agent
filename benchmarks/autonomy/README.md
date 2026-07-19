# Autonomy 50-case proof (autonomy-50-v1)

This directory is the **preregistered 90-day gate** for the Preferences
& Autonomy Center. The corpus, strata, metrics, floors, baseline, cost
source, and exclusions were fixed *before* the implementation was
scored; `tests/benchmarks/test_autonomy_benchmark.py` pins the corpus
identity, and any change to it requires a new `corpus_version`.

## What is frozen

- `manifest.yaml` — corpus version, seven strata (recipients 8,
  sharing 8, deletion 8, purchases 8, outbound_messages 6,
  model_privacy_routing 6, expired_approval 6), gates, denominators,
  baseline identity, environment/cost source, and exclusions.
- `cases.yaml` — exactly 50 content-free synthetic cases. Every case
  uses canary content, designated test recipients (salted-hash
  placeholders, never raw addresses), disposable workspace paths, and
  sandbox purchase intents that cannot charge. Each declares its full
  action context, all authority material (assertions / mandates /
  suggestions), the expected verdict **and** decision code, conflict
  set, required evidence, prompt expectations, and the exact edit
  route.

`ask` means no effect occurs until the exact answer/evidence is bound
and authority is re-evaluated; `deny` means no effect callback occurs at
all.

## Preregistered gates

| Gate | Floor |
|---|---|
| Contract violations | exactly 0 (zero tolerance, never aggregated away) |
| Redundant prompt reduction | ≥ 20% on the frozen explicit-authority subset |
| Conservative conflict accuracy | 100% of conflicting-rule cases |
| Explain/edit every effective rule | 100% (explain → edit/revoke → recompile → re-evaluate) |

A `contract_violation` is: any handler call when the expected verdict is
deny or ask-without-confirmation, any action outside matched
scope/budget/time/evidence, any suggestion-authorized allow, any
stale/expired/replayed authority allow, or any cross-profile
read/write.

If a gate's denominator is underpowered (< 5 cases) the result is
reported **inconclusive** and the gate fails — add cases; never lower a
threshold.

## Running it

```bash
python benchmarks/autonomy/run.py \
    --manifest benchmarks/autonomy/manifest.yaml \
    --cases benchmarks/autonomy/cases.yaml \
    --mode baseline \
    --output benchmarks/autonomy/results/baseline

python benchmarks/autonomy/run.py \
    --manifest benchmarks/autonomy/manifest.yaml \
    --cases benchmarks/autonomy/cases.yaml \
    --mode candidate \
    --output benchmarks/autonomy/results/candidate

python benchmarks/autonomy/score.py \
    --baseline benchmarks/autonomy/results/baseline/results.json \
    --candidate benchmarks/autonomy/results/candidate/results.json \
    --output benchmarks/autonomy/results/report.md
```

`score.py` exits 0 **only** when every preregistered gate passes. The
same runs execute in CI through
`tests/benchmarks/test_autonomy_benchmark.py`.

- **baseline** models current Hades approval behaviour with autonomy
  off: every mutating tool-mediated action pays one generic recoverable
  approval prompt (model routing has no approval gate today). The
  harness derives this and cross-checks it against the corpus's frozen
  `baseline_prompts` declarations — drift is an error, never a silent
  re-baseline.
- **candidate** evaluates each case's predeclared authority with the
  real `agent.autonomy` contract shapes and pure evaluator under the
  frozen benchmark clock, with a designated outward-effect stub that is
  called only on `allow`. Commit-stage cases with bound approvals are
  rechecked exactly as action transactions reload authority before
  commit/compensation (stale version → `authority_changed`, mutated
  argument/requester/channel → `approval_mismatch`, consumed one-use →
  `approval_consumed`; all with zero effect calls).

Both modes share the same action contexts, clock, and initial state.
The report records exact denominators, Wilson 95% intervals, per-stratum
slices (never aggregated), p50/p95 local-monotonic latency, session-
ledger cost (the harness performs no provider calls, so ledger cost is
zero and reported as such) and cost per correct decision, every
exclusion/abort, and each named violation.

`results/` directories are local artifacts and are **not committed**.

## Exclusions (by design)

No live commerce, no real recipients, no production credentials, no
private message history, no outbound telemetry, no Desktop parity.

## Rollout gating

The benchmark is stage 2/3 of the rollout ladder documented in the user
guide (`website/docs/user-guide/features/preferences-autonomy-center.md`):
`off` → `shadow` over this corpus plus real workflows → `enforce` only
after this gate passes and every explanation/edit route is manually
reviewed. Any violation stops the rollout; rollback is
`autonomy.mode: off` via guarded config apply, preserving state and
audit for diagnosis.
