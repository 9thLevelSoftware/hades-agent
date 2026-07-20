---
title: "Outcome Receipts"
description: "Immutable, independently verified receipts for what Hermes actually changed — evidence, artifacts, uncertainty, and rechecks instead of 'done'"
---

# Verified Outcome & Artifact Receipts

Hermes can show **evidence of what changed, whether the requested end state
really holds, what was produced, and what remains uncertain** — instead of
merely saying "done."

A *receipt* is an immutable record attached to a turn, mission, or
transaction. It captures the requested outcome and constraints, every claimed
effect, the evidence and artifact hashes behind each claim, the uncertainty
that remains, and the independent scorer's decision. A model saying "I did
it", a workflow reporting success, a file merely existing, or a cryptographic
signature is **never** treated as proof of truth: at most it yields
`completed_unverified`. Only an independent end-state scorer can mark a
receipt `verified`.

## Quick start

Enable capture in `~/.hades/config.yaml` (per profile):

```yaml
receipts:
  mode: capture   # off (default) | capture | require
```

Then run a turn and inspect what it really did:

```bash
# 1. Issue: run any receipt-enabled turn (capture mode issues silently).
hermes -z "add the maintenance note to README.md"

# 2. List recent receipts.
hermes receipt list --limit 10

# 3. Show one receipt — original decision plus the latest recheck.
hermes receipt show rct_<id> --observation latest

# 4. Re-score current facts; appends a new observation, never edits.
hermes receipt recheck rct_<id>

# 5. Export a redacted, hash-verifiable copy.
hermes receipt export rct_<id> --output receipt.json --redaction public
```

The same grammar is available as the classic `/receipt` slash command, as the
native TUI `/receipt` command, and read-only on the Dashboard `/receipts`
page. `hermes receipts` is an alias of `hermes receipt`.

## The five statuses

`ReceiptStatus` has exactly five values. No feature may add a sixth.

| Status | Meaning |
|--------|---------|
| `verified` | An **independent** scorer confirmed the requested end state holds, from reloaded evidence — never from the producer's own success label. |
| `completed_unverified` | The producer claims completion, but no appropriate independent scorer confirmed it. This is **not** success; it is an unproven claim. |
| `failed` | The requested end state does not hold (a required claim is unsatisfied or a known failure is recorded). |
| `blocked` | Execution was stopped before the end state could hold (approval denied, guardrail, missing dependency). |
| `unknown_effect` | The effect may or may not have happened (e.g. a timeout after dispatch). **Do not retry the effect**; recheck and reconcile evidence first. Hermes never auto-retries an unknown effect. |

Precedence is fixed: ambiguous operations dominate (`unknown_effect`), then
known failures (`failed`), then blocking (`blocked`); only a clean snapshot
can reach scorer evaluation, and only a sealed scorer decision can reach
`verified`. Everything else lands at `completed_unverified`.

## Original decision versus latest observation

Receipts are immutable. `hermes receipt recheck` re-reads the current facts
(artifact bytes, verification evidence, operation certainty) and **appends a
linked observation** — it never updates the original receipt, the subject's
terminal state, or an earlier observation.

- `hermes receipt show <id>` prints the original decision and the latest
  observation side by side, and states drift truthfully (e.g. "originally
  completed_unverified; latest recheck: failed — artifact bytes changed").
- `--observation all` prints the full observation chain;
  `--observation <obs_id>` prints one.
- A later, fresher recheck by an appropriate scorer *can* append a `verified`
  observation — the original stays exactly as decided.

## Claims, evidence, and artifacts

Every claimed effect on a receipt links to evidence that exists:

- **Claim → evidence**: each claim lists the `evd_…` evidence digests it
  rests on (verification checks, operation journal rows, page snapshots,
  delivery records). `hermes receipt claims <id>` renders these edges;
  `--json` emits them machine-readably.
- **Claim → artifact**: produced files are cataloged as `art_…` digests with
  size, media type, and SHA-256 over the bytes. A recheck hashes the same
  open file handle it stats, refuses swapped symlinks where the platform
  permits, and reports `missing`, `changed`, `inaccessible`, or ambiguous
  results truthfully.
- **Freshness**: evidence carries `observed_at` and optional `fresh_until`.
  Stale evidence (an old page snapshot, an expired verification) can never
  support `verified` — it caps the result at `completed_unverified` with the
  staleness named in `uncertainty`.
- **Missing or ambiguous evidence** is stated, not hidden: a claim whose
  requested path does not exist is `unsatisfied`; a delivery with a missing
  acknowledgement is `unknown` and drives `unknown_effect`.

## Export and redaction

`hermes receipt export` writes a self-contained JSON document whose content
hashes can be revalidated anywhere (`verify_export_hashes`).

- `--redaction public` (default): secrets, credentials, message bodies, query
  strings, and absolute profile paths are redacted **before** hashing or
  writing; raw local file locators are excluded entirely.
- `--redaction local`: profile-relative locators are included after boundary
  checks — for your own debugging, not for sharing.
- `--bundle-artifacts` copies hash-verified artifact bytes alongside the
  export; `--sign` attaches a provenance attestation when a signing provider
  is configured.

## Retention

Deletion is explicit and two-step; nothing is pruned implicitly during a live
turn:

```bash
hermes receipt retention-plan            # exact candidates + blockers
hermes receipt prune --confirm-plan <PLAN_HASH>
```

`prune` refuses anything but the exact current plan hash, refuses receipts
under an active hold, and leaves a tombstone for every deletion. Expired
artifact *locators* (default 90 days, `receipts.artifact_locator_retention_days`)
make a later recheck report `completed_unverified` — never an invented
failure. Receipts themselves default to 365 days (`receipts.retention_days`).

## Signing: provenance, never truth

```yaml
receipts:
  signing:
    provider: "my-signer"   # '' disables signing
    required: false
```

Credentials stay in your secret store or `.env` — config names only the
provider ID. Providers load through a service gate or standalone plugin and
are used only if their `check_fn` accepts the config.

A signature proves **who or what produced bytes with a given content hash**.
It never changes a status, claim verdict, uncertainty, freshness, or scorer
result, and never proves that artifact contents or claims are true.
`hermes receipt verify-signature <id>` reports attestation validity separately
from truth status, and imported legacy signatures are kept as untrusted
provenance attestations only.

## Storage and profile isolation

Receipts persist in `state.db` inside your profile home — the directory shown
by `hermes profile` (rendered via `display_hades_home()`, by default
`~/.hades`). Verification evidence stays in its own profile-local
`verification_evidence.db`; raw artifact locators live in a bounded
profile-local table excluded from public export.

Profiles are fully isolated: no lookup, recheck, export, signer, or retention
job crosses `HADES_HOME`. Another profile asking for your receipt or artifact
IDs gets a not-found, never your data. No telemetry leaves the machine.

## The 50-mission proof benchmark

The receipt contract is gated by a preregistered corpus of exactly 50
false-success missions (silent no-op, wrong file, stale page, partial
delivery, reverted change, forged-looking artifact, grader ambiguity):

```bash
uv run python benchmarks/receipts/runner.py \
  --manifest benchmarks/receipts/manifest.yaml \
  --repeats 3 --output-json build/receipt-benchmark.json
```

The report is local-only and states, separately: denominator and exclusions,
correct-classification rate with Wilson 95% intervals overall and per
stratum, false-verified counts for baseline and candidate, traceability and
recheckability ratios, p50/p95 latency, cost, environment facts, the exact
rollout gate it was judged against, and any triggered stop conditions. The
runner exits nonzero on any safety stop or if correct classifications fall
below 45/50. Safety, cost, and accuracy are never combined into one score.

## Staged rollout and failure stops

Receipts roll out in frozen stages. Each stage's gates come from the
preregistered manifest and are reported at runtime by the benchmark runner —
the documentation can never quietly weaken them.

1. **Schema first, `mode: off`.** Land storage, migration, and the read
   viewers with `receipts.mode: off`. Migration is atomic; legacy rows stay
   readable and exportable.
2. **`capture` on designated test profiles only.** Issue unsigned receipts,
   compare against the current Hermes turn-outcome/prose baseline, and run
   all 50 preregistered cases.
3. **Optional signers** may be configured only after forgery, replay, and
   redaction tests pass — and signatures stay visually separated from truth
   status everywhere they render.
4. **`capture` broadly** only after: zero false `verified`, at least 45/50
   correct classifications, 50/50 traceable claims, 50/50 independently
   recheckable receipts, and cache/schema/role invariants green.
5. **`require`** only for explicitly receipt-required mission/transaction
   flows, after crash/projection recovery passes. Generic chat completion
   never depends on receipt storage.
6. **Stop conditions.** Stop the rollout and return affected profiles to
   `off` on any of: a false `verified` receipt; an unsealed `verified`
   insert; an inappropriate or self-authored scorer verifying; an accepted
   hash mismatch; a signature promoting status; a hidden source-replay
   conflict; cross-profile access; a secret or raw locator in a public
   export; mutation during a recheck; a receipt lost after a crash; or any
   tool-schema/cache/role drift.
7. **Rollback** disables new capture and signing but preserves readable
   immutable rows and tombstones. The database is never downgraded by
   deleting canonical tables or restoring legacy hashes.

## What receipts do NOT do

Stated up front so expectations are exact:

- **No new model-visible tool** — the model's tool schema is byte-identical
  with receipts on or off, and receipt state is never injected into prior
  messages.
- **No exactly-once inference.** A receipt records what is known, including
  "unknown"; it does not make delivery or side effects exactly-once.
- **No compensation/reversal claims without evidence.** A receipt never
  asserts that a rollback or refund happened unless there is evidence for it.
- **No automatic retry** of `unknown_effect` operations — reconciliation is
  explicit.
- **No cross-profile receipts**, and **no telemetry upload** — everything is
  profile-local.
- **No messaging-gateway viewer** — receipts are inspected via CLI, native
  TUI, and the read-only Dashboard page.
- **No Desktop dependency or parity** — the Electron app is out of scope.
- **No Dashboard mutation controls** — the `/receipts` page is strictly
  read-only inspection; recheck, export, and prune run from the CLI/TUI.

## See also

- [CLI Commands Reference — `hermes receipt`](../../reference/cli-commands.md#hermes-receipt)
- [Slash Commands Reference](../../reference/slash-commands.md)
- [Receipt Contract (developer guide)](../../developer-guide/receipt-contract.md)
