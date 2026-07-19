---
sidebar_position: 30
title: "Preferences & Autonomy Center"
description: "One understandable, editable, versioned place to control what Hades may do, spend, share, remember, interrupt about, or require approval for"
---

# Preferences & Autonomy Center

The Autonomy Center gives each profile **one understandable place to
control what Hades may do, spend, share, remember, interrupt about, or
require approval for** — and it records *why* every decision happened.

It does not execute actions, score results, or guess permissions from
your behaviour. It compiles your authority into an immutable, versioned
contract and deterministically decides `allow`, `ask`, or `deny` for
each candidate action. `allow` is **current authority, not proof of
completion** — a tool can still fail after being allowed.

## The three kinds of rules

| Source kind | Where it lives | Can it authorize? | Lifecycle |
|---|---|---|---|
| `user_assertion` | `config.yaml` (`autonomy.stable_rules`) | Yes | You add/edit/remove it explicitly; durable until changed |
| `learned_suggestion` | profile `state.db` | **Never** | Proposed with provenance and confidence; waits for your confirmation, or is rejected |
| `temporary_mandate` | profile `state.db` | Yes, within its exact scope | Explicitly confirmed; expires and/or is consumed; revocable |

Inferred preference is never authorization: a learned suggestion —
whatever its confidence — is shown *beside* the contract but is excluded
from its rule set and hash until you explicitly confirm it, which
creates a **new** assertion or mandate (the suggestion itself never
becomes authority).

## How decisions are made

Decisions are deterministic `allow` / `ask` / `deny`. The evaluator:

1. validates the declared action context; unknown high-risk facts
   (unclassified data, unresolved recipients, unknown reversibility)
   never match wildcards — they fail closed;
2. discards inactive, expired, exhausted, wrong-profile, and
   wrong-task/session/mission/transaction rules;
3. excludes every learned suggestion regardless of confidence;
4. enforces hard boundaries — credential/financial/health data needs an
   explicitly matching rule with a named recipient, unknown recipients
   deny outbound sends, irreversible actions need exact approval
   evidence, cost caps and time windows bind;
5. combines matching rules with **deny > ask > allow** — a matching deny
   always wins, a specific allow never silently overrides it. The UI may
   offer to edit the deny; the current decision stays denied;
6. with no match, applies the conservative default: `ask` for known
   reversible actions, `deny` for unknown/irreversible/credential/
   external-send actions. The default can never be `allow`.

Every decision records the exact contract version and hash, a redacted
context hash, matched rules, conflicts, required evidence, and the exact
command to edit each rule involved. Audit rows never contain prompt
text, tool output, secrets, message bodies, file contents, or raw
recipient identifiers.

Rules can constrain: action class (`message.send`, `data.share`,
`workspace.delete`, `purchase.prepare`/`commit`, `model.route`, …), data
classes (`public` … `credential`/`financial`/`health`/`unknown`),
recipients (classes and exact salted hashes — near-matches such as
Unicode confusables never match), resources (segment-boundary path
prefixes), cost (integer cents per action and per window), local-time
windows, uncertainty, reversibility, and required pre/post-action
evidence.

## Modes: off, shadow, enforce

Set `autonomy.mode` in the profile's `config.yaml`:

- `off` (default) — no evaluation, no behaviour change; you can still
  author rules, inspect suggestions, and run the benchmark;
- `shadow` — every mutating tool call is evaluated and the candidate
  verdict is recorded, but current approval behaviour is preserved
  exactly;
- `enforce` — deny blocks the tool with a structured explanation, ask
  escalates through the existing approval gate (your once/session answer
  becomes an exact bounded mandate and the action is re-evaluated), and
  allow passes an exact one-use grant downstream so the generic prompt
  is not asked twice.

Hardline command blocks, managed configuration, secret-scope isolation,
information-flow enforcement, and exact irreversible approvals remain
**stronger** boundaries — an autonomy allow can never bypass them. An
invalid `autonomy:` section disables enforce by failing closed
(`invalid_stable_authority`), never by using a partial rule set.

## Command surface

The CLI (`hades autonomy …`), classic slash (`/autonomy`, alias
`/authority`), and the native TUI all share one grammar:

```text
hades autonomy status                        # contract identity, mode, rule counts
hades autonomy list [--effective] [--json]   # rules across both layers / compiled contract
hades autonomy rule show|explain <id>        # full explanation + exact edit route
hades autonomy rule add    --file RULE.yaml [--apply --expected-contract-hash H]
hades autonomy rule edit <id> --file RULE.yaml [--apply --expected-contract-hash H]
hades autonomy rule remove <id> [--apply --expected-contract-hash H]
hades autonomy evaluate --file ACTION.yaml [--stage explain|preview]   # never executes
hades autonomy suggestion list|show|accept|reject
hades autonomy mandate add --file RULE.yaml --expires-in 1h [--uses N]
hades autonomy mandate revoke <id> --reason TEXT
hades autonomy audit [--since T] [--verdict allow|ask|deny] [--json]
hades autonomy export --output PATH [--include-audit]
hades autonomy purge-audit --before ISO8601 --apply
hades autonomy doctor
```

Stable changes are two-phase: `rule add/edit/remove` first prints a
preview with the current contract hash; re-running with `--apply
--expected-contract-hash <hash>` commits it atomically and materializes
a new immutable contract version. If the config changed in between, the
hash no longer matches and the apply is refused.

The Dashboard has a secondary **Autonomy** page for reading the
contract, reviewing suggestions, and editing rules; the CLI/TUI remain
the primary authoring and explanation surfaces.

### Example: a recipient-sharing rule

```yaml
# share-with-alex.yaml
rule_id: allow-share-internal-alex
effect: allow
action_classes: [data.share]
data_classes: [internal]
recipient_hashes: [rh-2f8a91c4d0b7e6a5]   # from `hades autonomy evaluate` output
description: Share internal work docs with Alex after verification
evidence_requirements:
  - {kind: recipient_verified, stage: pre_action}
```

```bash
hades autonomy rule add --file share-with-alex.yaml          # preview + hash
hades autonomy rule add --file share-with-alex.yaml \
    --apply --expected-contract-hash sha256:...              # commit
```

### Example: a one-use transaction mandate

```yaml
# delete-report-once.yaml
rule_id: mandate-delete-report-once
effect: allow
action_classes: [workspace.delete]
scope:
  task_id: task-cleanup-42
  resource_prefixes: ["workspace:/tmp/old-report.txt"]
```

```bash
hades autonomy mandate add --file delete-report-once.yaml --expires-in 1h --uses 1
```

The mandate is consumed atomically the first time it authorizes the
delete; a replay of the same operation is denied (`mandate_consumed`)
with no second effect. After expiry the answer becomes `ask`
(`authority_expired`) — never a silent allow.

### Example: a bounded cost/time purchase rule

```yaml
# sandbox-purchases.yaml
rule_id: allow-sandbox-daytime-purchases
effect: allow
action_classes: [purchase.prepare]
data_classes: [financial]
recipient_classes: [sandbox_merchant]
cost:
  max_per_action_cents: 500      # $5 per action
  max_per_window_cents: 1000     # $10/day
  window_ms: 86400000
time:
  window_start_minute: 540       # 09:00 local
  window_end_minute: 1020        # 17:00 local
```

Unknown estimated cost asks (`cost_unknown`); exceeding a cap denies
(`cost_per_action_exceeded` / `cost_budget_exceeded`); outside the
window asks (`outside_time_window`). Allowed spend is reserved before
the allow returns and settled afterwards.

## Profiles, expiry, audit, recovery

- **Profiles are islands.** Every rule, contract version, mandate,
  suggestion, and audit row lives under that profile's own home
  (`get_hades_home()`). There is no live default-profile inheritance;
  copying rules happens only through explicit profile clone/export/
  import.
- **Commit-time recheck.** Long-running transactions reload authority
  immediately before commit or compensation. Expired, edited, consumed,
  or re-bound authority denies with zero adapter calls
  (`authority_expired`, `authority_changed`, `approval_mismatch`,
  `approval_consumed`).
- **Audit, export, purge.** `hades autonomy audit` shows redacted
  decisions; `export` writes a redacted portable snapshot; `purge-audit
  --before … --apply` deletes settled history explicitly — nothing is
  purged implicitly.
- **Recovery.** A crashed config apply leaves a journal that
  `hades autonomy doctor` reports; mutating enforce-mode calls fail
  closed until it is resolved.

## The 50-case proof benchmark

The preregistered corpus in `benchmarks/autonomy/` (see its README) is
the local gate: 50 synthetic ambiguous/conflicting cases across
recipients, sharing, deletion, purchases, outbound messages, model/
privacy routing, and expired approval. Run it any time:

```bash
python benchmarks/autonomy/run.py --manifest benchmarks/autonomy/manifest.yaml \
    --cases benchmarks/autonomy/cases.yaml --mode baseline  --output benchmarks/autonomy/results/baseline
python benchmarks/autonomy/run.py --manifest benchmarks/autonomy/manifest.yaml \
    --cases benchmarks/autonomy/cases.yaml --mode candidate --output benchmarks/autonomy/results/candidate
python benchmarks/autonomy/score.py --baseline benchmarks/autonomy/results/baseline/results.json \
    --candidate benchmarks/autonomy/results/candidate/results.json \
    --output benchmarks/autonomy/results/report.md
```

## Rollout and rollback

1. Ship with `autonomy.mode: off`. Operators may run CLI/TUI explain and
   the benchmark, and inspect suggestions.
2. Enable `shadow` for the full 50-case preregistered corpus and at
   least two real CLI/TUI workflows from each applicable archetype,
   using only user-authorized data and designated test accounts.
3. Advance to `enforce` only after the benchmark passes (zero contract
   violations, 100% conservative conflict handling, ≥20% redundant
   prompt reduction, 100% explain/edit coverage) **and** manual review
   confirms every explanation and edit route.
4. **Stop rollout** on any contract violation, inferred authorization,
   approval replay, cross-profile access, unredacted sensitive audit
   value, incorrect conflict resolution, commit without a fresh recheck,
   prompt/tool/provider/model drift, role-alternation violation,
   audit-unavailable fail-open, or false completion/verification claim.
5. **Roll back** by setting `autonomy.mode: off` through the guarded
   config apply and starting no new authority-gated effects. Preserve
   state and audit for diagnosis; optionally `hades autonomy export`
   stable rules, then purge runtime audit with the explicit command. Do
   not delete `state.db` or alter past conversations.
