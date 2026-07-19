---
sidebar_position: 40
title: "Autonomy Contract"
description: "Consumer contract and security rules for agent.autonomy — the canonical AuthorityProvider used by action transactions and the execution middleware"
---

# Autonomy Contract (developer guide)

`agent.autonomy` is the single authority engine for the Preferences &
Autonomy Center. Consumers — action transactions, the execution
middleware, CLI/TUI/Dashboard surfaces — import **only** the stable
package exports (`from agent.autonomy import …`), never submodule
privates.

## Canonical JSON and hashes

- `canonical_json()` accepts only canonical material: strings, ints,
  bools, `None`, lists, and string-keyed dicts. **Floats are rejected**;
  money is integer cents (micros internally), time is integer
  milliseconds/minutes, confidence and uncertainty are integer ppm.
- `content_hash()` is SHA-256 over canonical JSON. A contract's
  `contract_hash` covers exactly its ordered, suggestion-free rule set;
  `context_hash()` covers the declared, redacted `ActionContext`.
- `hash_recipient()` / `hash_resource()` are profile-keyed, domain-
  separated keyed hashes. Raw recipient identifiers must be hashed at
  the resolver boundary and discarded — they never enter contexts,
  decisions, or audit rows. Matching is exact hash equality; Unicode
  confusables and near-matches are mismatches by construction.

## AuthorityProvider

```python
@runtime_checkable
class AuthorityProvider(Protocol):
    def current_contract(self) -> AutonomyContract: ...
    def authorize(self, context: ActionContext, *, consume: bool) -> AuthorityDecision: ...

def authorize_effect(provider, context, *, stage: DecisionStage,
                     consume: bool | None = None) -> AuthorityDecision: ...
```

`StoredAuthorityProvider` is the canonical profile-local implementation
(config + `state.db` through `SessionDB.autonomy`). Item #2's
`agent/effects/authority.py` is an effect-specific **adapter plus exact
transaction-approval binding** over these types — never a second
authority schema or store. Its obligations:

- reload the provider and re-authorize immediately before commit or
  compensation (`authorize_effect(provider, ctx, stage="commit")`);
- bind approvals to the exact contract version/hash, final-argument
  hash, requester, and channel; any drift is `authority_changed` /
  `approval_mismatch`, a consumed one-use approval is
  `approval_consumed` — all deny with **zero** adapter calls.

## ActionContext is declared, trusted input

Resolvers (`tools/registry.py::get_authority_context` metadata plus
`agent/autonomy/runtime.py::resolve_action_context`) build the context
from the FINAL post-plugin arguments. Rules:

- every high-risk dimension is declared explicitly: unclassified
  payloads are `data_classes=("unknown",)`, unresolved recipients are a
  `None` hash/class — never an omitted field a wildcard could match;
- resolver failure degrades to explicit `unknown` labels in shadow mode
  and **fails closed** (`invalid_action_context`) for mutating enforce-
  mode calls; registry-proven read-only operations bypass the gate and
  can never gain or grant mutation authority;
- resource refs are canonicalized (symlinks resolved) before matching;
  prefix checks are segment-boundary, never raw string prefixes.

## Pure evaluation order

`evaluate_contract(contract, context, *, now_ms, budget_usage,
lapsed_rules)` is a pure function — identical inputs give an identical
`AuthorityDecisionDraft`, independent of rule order:

1. validate context; unknown extension action classes fail closed;
2. discard inactive/expired/exhausted/wrong-profile/wrong-scope rules;
3. exclude every `learned_suggestion` regardless of confidence;
4. hard constraints: sensitive-data boundaries, unknown recipient/data/
   reversibility, cost caps and windows (integer micros), local-time
   windows (IANA/DST aware), uncertainty, missing pre-action evidence;
5. combine: any deny wins, else any ask wins, else allow needs one clean
   allow; no match uses the conservative default (never `allow`);
6. union post-action evidence; explanations name matches, conflicts,
   absent facts, and exact edit routes.

`lapsed_rules` carries terminally expired/consumed mandates so replays
explain as `mandate_consumed`/`authority_expired` instead of the generic
default. A matching deny is never silently overridden by a more specific
allow or an active mandate.

## Versions, consumption, budgets — transactions

The store (`agent/autonomy/store.py`, tables under `SessionDB` with
`BEGIN IMMEDIATE` retry) guarantees:

- **contract versions are immutable and monotonic**; each decision binds
  the exact version + hash it was evaluated under;
- **mandate consumption is atomic** with decision append: the decision
  row and the `remaining_uses` decrement (or `consumed` transition)
  commit together, replay-safe under one operation key;
- **budget reservations** are recorded before an allow returns and
  settled afterwards; window spend is queried in integer micros and
  passed to the evaluator as `budget_usage`.

An unavailable or failing audit/store path never becomes an allow
(`authority_audit_failure` — fail closed).

## Decision audit redaction

Audit rows contain verdict/code/reason, contract version + hash, context
hash, matched/conflicting rule IDs, evidence kinds, and edit routes.
They must never contain prompt text, tool output, secrets, message
bodies, file contents, or raw recipient identifiers. Exports follow the
same redaction.

## Approval-grant ordering

In enforce mode `authority_gate()` (installed by
`hades_cli/middleware.py` around the TRUE terminal call, after plugin
argument finalization) resolves:

1. hardline command blocks — always first, no autonomy involvement;
2. autonomy decision (`deny` blocks; `ask` escalates through the
   existing `tools/approval.py::request_tool_approval` — a once/session
   answer creates an exact bounded mandate and re-evaluates);
3. on allow, a context-local one-use `AuthorityGrant` bound to the
   operation key, tool name, and final-argument hash is installed;
   `tools/approval.py` may consume it in place of a redundant generic
   prompt — but never for irreversible actions
   (`satisfies_generic_approval=False`) and never before hardline
   checks.

Clarifications reuse the existing clarify transport as bounded
structured output; no new prompt protocol and no injected messages.

## Ownership boundaries

- **Item #15** owns source-to-sink information-flow propagation.
  Autonomy consumes its data labels (`data_classes`) and never invents a
  second data-flow engine.
- **Item #2** imports `AuthorityProvider`, `StoredAuthorityProvider`,
  `AuthorityDecision`, and `authorize_effect()` from here; its adapter
  binds transactions and rechecks at commit time.
- **Profiles**: every path resolves from `get_hades_home()`; no
  evaluation path may call a default-profile home. Secret scope stays
  fail-closed: autonomy sees only the `credential` label and hashes,
  never scoped secret values.

## Crash recovery and cache invariants

- Stable config changes go through the exact-hash guarded saga in
  `agent/autonomy/config_apply.py` (preview → apply with
  `--expected-contract-hash`). A crash mid-apply leaves a journal;
  mutating enforce-mode calls fail closed until it is recovered
  (`hades autonomy doctor`).
- Authority changes affect deterministic execution only. The system
  prompt, cached prefix, effective tool-definition snapshot, provider,
  and model stay byte-stable for a conversation; no messages are
  rewritten, no tools reloaded, no synthetic user turns injected.

## Required tests for new consumers

Every new consumer of this contract must ship real-path tests (mocks
only for clocks, user prompt callbacks, and external effect/network
boundaries) against a temporary `HADES_HOME` covering at least:

1. an allow that its effect actually observes (grant consumed exactly
   once);
2. a deny/ask with **zero** effect calls;
3. commit-time recheck: authority expiring/changing between preview and
   commit yields zero adapter calls;
4. suggestion exclusion: a high-confidence suggestion alone never
   allows;
5. profile isolation: an opposite rule in a second profile home changes
   nothing;
6. audit redaction of every new field the consumer records.

The preregistered proof harness lives in `benchmarks/autonomy/`
(`run.py`, `score.py`, README) and must keep passing all four gates.
