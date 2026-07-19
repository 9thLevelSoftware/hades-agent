---
name: autonomy-center
description: "Explain and edit what Hades may do: inspect, preview, and apply allow/ask/deny authority rules, bounded mandates, and learned suggestions from the terminal."
version: 1.0.0
author: Hades Agent
license: MIT
metadata:
  hermes:
    tags: [Autonomy, Authority, Preferences, Safety, Configuration]
---

# Preferences & Autonomy Center

One place to control what Hades may do, spend, share, remember, interrupt
about, or require approval for. Every control below runs through the same
deterministic surface: `hades autonomy ...` at the shell, `/autonomy`
(alias `/authority`) in a chat session. Decisions are always exactly
`allow`, `ask`, or `deny`, deny wins over ask wins over allow, and a
missing or unknown high-risk fact never becomes a wildcard match.

**`allow` is authorization, not completion proof.** An allow verdict means
the action is currently authorized; whether it then completed, and whether
that completion is verified, is reported by the action/transaction layer
with its own vocabulary (`verified`, `completed_unverified`,
`unknown_effect`).

## Operating rules (follow in order)

1. **Inspect and explain before changing anything.**
   - `hades autonomy status` — contract version/hash, mode, rule counts.
   - `hades autonomy list --effective --json` — the compiled contract.
   - `hades autonomy rule explain <rule-id>` — why a rule exists, its
     provenance and confidence, what it conflicts with, and the exact
     command to edit or revoke it.
2. **Preview every stable edit first.** `rule add|edit|remove` without
   `--apply` writes nothing and prints the before/after contract hashes.
3. **Apply only with the exact hash.** Re-run with
   `--apply --expected-contract-hash <before_contract_hash>` from the
   preview you just read. A stale hash exits 2 and writes nothing —
   re-inspect, re-preview, and re-decide; never retry blindly.
4. **Use temporary mandates for task-bound authority.** If authority is
   needed only for one task/session/transaction or a bounded time/use
   count, create a mandate (`mandate add --file RULE.yaml --expires-in 2h
   --uses 1`) instead of a durable stable rule. Mandates cannot bypass a
   stable deny.
5. **Never accept a learned suggestion without the user's explicit
   confirmation.** Suggestions are shown beside the contract but never
   authorize anything. `suggestion accept <id>` requires the user to pick
   a destination: `--stable` (previewed, exact-hash applied) or
   `--temporary --expires-in DURATION [--uses N]`. If the user has not
   explicitly confirmed, do nothing or `suggestion reject <id> --reason`.
6. **Re-evaluate after any conflict or change.** When a decision reports
   conflicting rules, or after any apply/revoke, run
   `hades autonomy evaluate --file ACTION.yaml` again. Specific rules
   never silently override a matching deny; the current decision stays
   denied until the user edits/removes that deny.
7. **Stop on deny, unknown, or audit failure.** A `deny` verdict (exit 3),
   an `unknown_*` code, a pending-apply/doctor failure (exit 4), or any
   storage error means: stop, report, and do not look for another route
   around the decision.
8. **Never edit another profile implicitly.** All commands operate on the
   active profile's own `config.yaml` and `state.db`. To change another
   profile's authority, the user must switch profiles explicitly; copying
   rules happens only through explicit profile clone/export/import.
9. **Authority changes never require a new conversation.** They affect
   deterministic execution only. Start a new conversation only when a
   separate change also alters system-prompt, tool, provider, or model
   identity.

## Command surface

```text
hades autonomy status [--json]
hades autonomy list [--source SOURCE] [--state STATE] [--effective] [--json]
hades autonomy rule show|explain <rule-id> [--json]
hades autonomy rule add --file RULE.yaml [--apply --expected-contract-hash HASH]
hades autonomy rule edit <rule-id> --file RULE.yaml [--apply --expected-contract-hash HASH]
hades autonomy rule remove <rule-id> [--apply --expected-contract-hash HASH]
hades autonomy evaluate --file ACTION.yaml [--stage explain|preview] [--json]
hades autonomy suggestion list|show <id> [--json]
hades autonomy suggestion accept <id> (--stable | --temporary --expires-in DURATION [--uses N]) [--apply --expected-contract-hash HASH]
hades autonomy suggestion reject <id> --reason TEXT
hades autonomy mandate add --file RULE.yaml --expires-in DURATION [--uses N]
hades autonomy mandate revoke <id> --reason TEXT
hades autonomy audit [--since ISO8601] [--verdict VERDICT] [--limit 200] [--json]
hades autonomy export --output PATH [--include-audit]
hades autonomy purge-audit --before ISO8601 --apply
hades autonomy doctor [--json]
```

Bounds: input files are UTF-8 YAML/JSON up to 1 MiB; durations are
`<int><s|m|h|d>` between 1 minute and 365 days; `--uses` is 1–10,000;
audit `--limit` is 1–500. Exit codes: 0 success/preview, 2 validation or
stale authority, 3 denied evaluation, 4 storage/recovery failure.

## Copyable rule files (`RULE.yaml`)

Rules select on normalized dotted action classes (`message.send`,
`data.share`, `workspace.delete`, `purchase.prepare`, `purchase.commit`,
`model.route`, ...), finite data classes (`public`, `internal`,
`personal`, `confidential`, `credential`, `financial`, `health`,
`unknown`), recipient classes/exact hashes, resource prefixes, cost/time
windows, reversibility, uncertainty, and required evidence.

### Recipients — allow sends to one designated recipient class

```yaml
rule_id: allow-send-designated
effect: allow
action_classes: [message.send]
data_classes: [public, internal]
recipient_classes: [designated_test]
description: public/internal sends to the designated test recipient
```

### Data sharing — always ask before sharing personal data

```yaml
rule_id: ask-share-personal
effect: ask
action_classes: [data.share]
data_classes: [personal]
description: confirm every share of personal data, any recipient
```

### Credential boundary — hard deny (deny always wins)

```yaml
rule_id: deny-credential-outbound
effect: deny
action_classes: [message.send, data.share]
data_classes: [credential]
description: credential-labeled content never leaves this profile
```

### Workspace deletion — reversible deletes under one prefix only

```yaml
rule_id: allow-delete-tmp
effect: allow
action_classes: [workspace.delete]
data_classes: [internal]
scope:
  resource_prefixes: ["workspace:/tmp"]
allowed_reversibility: [reversible]
description: checkpoint-backed deletes under workspace:/tmp only
```

### Outbound messages — generic ask for anything not matched above

```yaml
rule_id: ask-outbound-default
effect: ask
action_classes: [message.send]
data_classes: [public, internal, personal]
description: confirm outbound messages that no specific rule allows
```

### Model privacy routing — local-only for confidential context

```yaml
rule_id: deny-remote-confidential
effect: deny
action_classes: [model.route]
data_classes: [confidential, health]
recipient_classes: [remote_provider]
description: confidential/health context never routes to remote inference
```

### Cost / time / uncertainty / reversibility bounds on purchases

```yaml
rule_id: allow-sandbox-purchase
effect: allow
action_classes: [purchase.prepare]
data_classes: [financial]
recipient_classes: [sandbox_merchant]
cost:
  currency: USD
  max_per_action_cents: 500
  max_per_window_cents: 1000
  window_ms: 86400000
time:
  window_start_minute: 480   # 08:00 local
  window_end_minute: 1200    # 20:00 local
allowed_reversibility: [reversible, compensatable]
description: bounded sandbox carts, $5/action, $10/day, daytime only
```

### Required evidence — allow only after verification exists

```yaml
rule_id: allow-share-verified-recipient
effect: allow
action_classes: [data.share]
data_classes: [personal]
recipient_classes: [designated_test]
evidence_requirements:
  - kind: recipient_verified
    stage: pre_action
  - kind: delivery_receipt
    stage: post_action
description: personal shares require verified recipient before, receipt after
```

## Copyable action files (`ACTION.yaml` for `evaluate`)

Declare every high-risk fact explicitly. Unclassified content must be
`data_classes: [unknown]` and an unresolved recipient stays undeclared —
both resolve conservatively (fail closed), never as a wildcard match.

```yaml
# Outbound message to a designated recipient
action_class: message.send
data_classes: [public]
reversibility: reversible
recipient_class: designated_test
```

```yaml
# Reversible workspace delete under an allowed prefix
action_class: workspace.delete
data_classes: [internal]
reversibility: reversible
resource_refs: ["workspace:/tmp/canary.txt"]
```

```yaml
# Sandbox purchase preparation with declared cost
action_class: purchase.prepare
data_classes: [financial]
reversibility: compensatable
recipient_class: sandbox_merchant
estimated_cost_cents: 200
local_time_minute: 600
```

## Suggestions and mandates (worked flow)

```bash
# 1. See what was learned (never authorizes on its own)
hades autonomy suggestion list --json

# 2a. User confirms it durably: preview, then apply with the exact hash
hades autonomy suggestion accept suggest-1 --stable
hades autonomy suggestion accept suggest-1 --stable --apply \
  --expected-contract-hash <before_contract_hash-from-preview>

# 2b. ...or bounded to the task at hand
hades autonomy suggestion accept suggest-1 --temporary --expires-in 2h --uses 1

# 2c. ...or reject it
hades autonomy suggestion reject suggest-1 --reason "not wanted"

# 3. Verify the effect on the compiled contract
hades autonomy list --effective
hades autonomy evaluate --file ACTION.yaml
```

## Health and audit

- `hades autonomy audit --json` — recorded decisions (redacted: hashes and
  labels only, never message bodies, secrets, or raw recipients).
- `hades autonomy doctor` — config validity, contract head, pending crashed
  apply. While an apply journal is pending, every evaluation and mutation
  fails closed (exit 4); resolve recovery before anything else.
- `hades autonomy export --output authority.json` — portable redacted
  export for user review or explicit profile import.
