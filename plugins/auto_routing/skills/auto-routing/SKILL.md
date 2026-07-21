---
name: auto-routing
description: Configure validated automatic model routing profiles.
version: 0.3.0
author: Hermes fork contributors
license: MIT
metadata:
  hermes:
    tags: [models, routing, configuration]
    category: productivity
---

# Auto Routing Skill

Use this skill for an explicit setup, edit, or autonomous-management interview
for the active Hermes profile. It never routes an ordinary task, probes access
autonomously, or adds model-visible tools.

## When to Use

Use this skill only when the user asks to set up, inspect, validate, diagnose,
or edit Auto Routing. Do not start the interview during normal chat, session
resume, delegation, planning for an unrelated task, or runtime routing.

## Prerequisites

- The `auto-routing` plugin is enabled for the active profile.
- The `terminal` tool can run that profile's `hermes` executable.
- Provider credentials and local backends are already managed by Hermes.
- Never request credentials or place secrets, endpoints, prompt bodies, or
  provider responses in an advisor request.

## How to Run

Start setup or edit with a content-free executable inventory:

```text
hermes auto-routing inventory --json
```

Use `--refresh` only when the user asks for a fresh observation:

```text
hermes auto-routing inventory --refresh --json
```

Inventory and catalog refreshes do not verify paid access. The only billable
Auto Routing command is the separately approved `verify-runtime` flow below.

## Quick Reference

| Purpose | Command |
|---|---|
| Inventory | `hermes auto-routing inventory [--refresh] [--include-ineligible] --json` |
| Catalog | `hermes auto-routing refresh-catalog [--models-dev] [--hermes] [--file FILE] --json` |
| Plan | `hermes auto-routing plan --request FILE [--prompt-file FILE ...] --json` |
| Validate | `hermes auto-routing validate [--proposal FILE] --json` |
| Setup preview | `hermes auto-routing setup --proposal FILE --json` |
| Setup apply | `hermes auto-routing setup --proposal FILE --apply --expected-config-sha SHA256 --json` |
| Edit preview | `hermes auto-routing edit --proposal FILE --json` |
| Edit apply | `hermes auto-routing edit --proposal FILE --apply --expected-config-sha SHA256 --json` |
| Activation preview | `hermes auto-routing activate --mode active --json` |
| Activation apply | `hermes auto-routing activate --mode active --apply --expected-config-sha SHA256 --json` |
| Return to shadow | `hermes auto-routing activate --mode shadow [--apply --expected-config-sha SHA256] --json` |
| Explain session | `hermes auto-routing explain --session-id ID [--detailed] --json` |
| Explain delegation | `hermes auto-routing explain --operation-id ID --task-index N [--detailed] --json` |
| Explicit feedback | `hermes auto-routing feedback --evidence-id EVIDENCE_ID --value rating-1\|rating-2\|rating-3\|rating-4\|rating-5\|rejected\|corrected\|manual-reroute [--json]` |
| Evidence report | `hermes auto-routing report [--days 1..3650] [--decision-id ID] [--profile-id ID] [--runtime-id SHA256] [--reasoning-effort EFFORT] [--json]` |
| Adaptation status | `hermes auto-routing adapt status --profile-id ID --json` |
| Adaptation history | `hermes auto-routing adapt history --profile-id ID --json` |
| Freeze preview/apply | `hermes auto-routing adapt freeze --profile-id ID [--apply --expect-hash SHA256] --json` |
| Unfreeze preview/apply | `hermes auto-routing adapt unfreeze --profile-id ID [--apply --expect-hash SHA256] --json` |
| Rollback preview/apply | `hermes auto-routing adapt rollback --profile-id ID --revision ID [--apply --expect-hash SHA256] --json` |
| Management inventory | `hermes auto-routing manage inventory --json` |
| Ranking-pack status | `hermes auto-routing manage ranking --json` |
| Management status | `hermes auto-routing manage status --json` |
| Management history | `hermes auto-routing manage history [--profile-id ID] --json` |
| Management reconcile | `hermes auto-routing manage reconcile [--apply --expect-hash SHA256] --json` |
| Management enable/disable | `hermes auto-routing manage enable\|disable [--apply --expect-hash SHA256] --json` |
| Management freeze/unfreeze | `hermes auto-routing manage freeze\|unfreeze [--apply --expect-hash SHA256] --json` |
| Management recovery | `hermes auto-routing manage recover --receipt-id ID [--apply --expect-hash SHA256] --json` |
| Management schedule | `hermes auto-routing manage schedule --schedule CRON [--apply --expect-hash SHA256] --json` |
| Verify preview | `hermes auto-routing verify-runtime RUNTIME_STABLE_ID --json` |
| Verify apply | `hermes auto-routing verify-runtime RUNTIME_STABLE_ID --apply --expect-hash HASH --ack-billable --json` |
| Health | `hermes auto-routing doctor --json` |
| State | `hermes auto-routing status --json` |

## Procedure

1. Run `inventory --json`. Treat stable runtime IDs and states as the active
   profile's executable truth. Catalog presence is not access proof.
2. Keep `configured_unverified`, `temporarily_unavailable`, and `ineligible`
   runtimes out of primary, fallback, and safe-default targets.
3. Define one or more profiles. For each profile, collect its purpose and full
   profile match: domains, named complexity bands, modalities, and
   capabilities. An empty match dimension is neutral; it is not permission to
   invent a restriction.
4. Collect every advisor request field. Do not invent defaults:
   - workload domains, examples, and bounded input/output token estimates;
   - required modalities and required capabilities, including an explicit empty list
     when no custom capability is required;
   - risk classes and the separate tool-use requirement;
   - provider, model, license, local, subscription, cost, and latency limits;
   - for every profile, all four objective weights: quality, reliability,
     latency, and cost;
   - for every profile, explicit hard limits or an explicit `null` inheritance
     choice; profile limits may only tighten the global limits;
   - classifier and evaluator provider/model identities plus approval for full
     task disclosure;
   - named profiles and base ranks (base rank selects between profiles and
     never biases models within a profile);
   - configurable complexity bands and the complete routing vocabulary;
   - deterministic rules that only pin or prefer a defined profile; when the
     user wants no rules, record the explicit complete choice `"rules": []`;
   - representative prompt files; and
   - explicit approval to produce the final plan. This is not apply approval.
5. Before asking the user to select targets, write the profile intents to a
   partial JSON request and run `plan`. The returned `profile_rankings` compare
   every currently verified eligible runtime independently for each profile.
   Exclude configured-unverified, unavailable, incompatible local, and MoA
   candidates. Present provenance, source dates, confidence, uncertainty, and
   rejection reasons. Answer comparison questions and explain tradeoffs; never
   auto-select a primary or fallback merely because it is ranked first.
6. After the user chooses, add exact primary and ordered fallback stable
   runtime IDs plus default, minimum, and maximum reasoning for every selected
   target in each profile. A deliberate verified non-top choice is valid and
   must be preserved. Run `plan` again. Exit 2 with ordered `missing_facts`
   means the interview is incomplete; ask only for those facts.
7. For a ready plan, check its JSON fields before presenting it:
   - every target has `resolution_status: verified`;
   - ranking rows include sources, dates, confidence, and uncertainty;
   - inaccessible candidates and reasons are present;
   - economics are separated by exact access path;
   - `dry_run.results` contains derived prompt-index assessments only;
   - every `resolver_validation` entry is an exact match;
   - `yaml_diff`, `proposal`, and `initial_revision.canonical_json` exist; and
   - `next_command` does not contain `--apply`.
8. Save the unchanged `proposal` object to a proposal file. Run `validate` and
   then the matching `setup` or `edit` preview command.
9. Show the exact YAML diff, authority/baseline checksums, shadow activation,
   warnings, and `expected_config_sha256`. Ask the user to approve that exact
   preview. A request to "set up routing" is not approval to write it.
10. Only after approval, run the matching apply command with the exact preview
   hash. If the hash, proposal, inventory, economics, or policy changes, stop,
   preview again, and obtain new approval.
11. Run `validate`, `status`, and `doctor`. Setup/edit must remain shadow. Do
   not activate unless the user separately asks to enable active routing.

### Optional Active Transition

Offer this only after setup/edit is applied in shadow and the user explicitly
asks to activate routing.

1. Run `hermes auto-routing activate --mode active --json`. This is read-only.
2. Confirm `doctor.healthy` is true. Explain the exact config precondition,
   proposed-config hash, authority/inventory/adapter fingerprints, and the
   `post_call_model_failover: disabled` warning.
3. Explain that only new sessions and delegated children route; live and
   resumed sessions keep their recorded runtime and are not reclassified.
4. Obtain approval for that exact preview. Any config, authority, persisted
   inventory, or adapter-contract change requires a new preview.
5. Apply with `--apply --expected-config-sha SHA256`, then run `status` and
   `doctor`. Confirm a matching `activation_receipt_id` is present.

When diagnosing a recorded choice, use `explain` with exactly one lookup.
Start with the concise form and use `--detailed` only when candidate scores,
rejections, revisions, or accounting are needed. The output is deliberately
redacted; never supplement it with raw task text or provider payloads.

For a completed active routed turn, use `explain` to obtain its content-free
evidence ID. Record `feedback` only when the user explicitly supplies exactly
one of the finite command values. Never infer feedback from silence, tone,
follow-up, or an assistant's self-evaluation, and never translate free text
without confirming the finite value with the user. Feedback is append-only;
contradictory observations remain visible.

Use `report` only as a read-only descriptive summary of observed events. It
has no decision-population denominator or attribution-coverage percentage,
and its deterministic group order is not a ranking or recommendation. Do not
change, recommend, or rewrite routes from Stage 3 evidence. Continuation
context is unavailable, missing latency remains missing, and only `verified`
is objective positive quality evidence; every other turn outcome is
quality-unknown.

### Optional Conservative Adaptation Controls

Adaptation is opt-in per profile through `profiles.<id>.adaptation.enabled`.
Before enabling it, confirm that every `primary_challengers` entry is an exact
runtime the user already approved and can execute locally or through a Hermes
provider they have configured. Never propose an unavailable model, install or
download a model, or broaden provider/model discovery to create a challenger.

Quality adaptation may use only exactly attributed initial-task verified
outcomes and explicit feedback. `manual-reroute` excludes the observation;
silence, latency, retries, cost, provider failure, and quality-unknown outcomes
never infer feedback or negative quality. The learner and selector remain
separate, and no classifier or evaluator learning, outbound telemetry, or MoA
is part of this stage.

Use `adapt status --profile-id ID` and `adapt history --profile-id ID` for
read-only diagnosis. Freeze, unfreeze, and rollback are guarded operations:
run the command without `--apply`, explain the exact action and profile-local
state, obtain approval, then apply with the unchanged `precondition_hash`.
Never reuse a hash after any argument or state change. Rollback additionally
binds the requested immutable revision and checksum, requires the profile to
be frozen, and restores that exact same-authority, same-profile revision.

Freeze halts new proposals, canary assignments, and automatic publication;
routing, evidence, feedback, reporting, and an explicitly approved rollback
continue. Recovery is deterministic from immutable profile-local history and
the single control generation. These controls never mutate YAML, primary or
fallback authority, policy, profile topology, a live/resumed decision, or the
recorded fallback chain.

### Optional Autonomous Profile Management

Autonomous profile management is a separate, disabled-by-default global opt-in;
it does not enable or reconfigure per-profile adaptation. Offer it only when the
user explicitly asks Hermes to maintain existing Auto Routing profiles.

1. Run `manage inventory --json`, `manage ranking --json`, and `manage status
   --json` first. Treat the newest persisted inventory as the complete candidate
   universe. Never refresh it automatically for this workflow.
2. If ranking status is unconfigured or invalid, ask the user to configure a
   profile-local path below `auto-routing/ranking-packs/`, copy a signed pack
   there, and configure one or more trusted Ed25519 public keys. Do not fetch,
   generate, copy, or sign a pack unless the user separately asks for that work.
3. Verify that the envelope has exactly `schema_version`, `pack_id`, `issued_at`,
   `expires_at`, `key_id`, `rankings`, and `signature`. Each ranking must name an
   exact persisted stable runtime ID and contain finite `[0, 1]` quality,
   reliability, latency, and cost metrics. Do not expose signature bytes or
   ranking rows in management reports.
4. Never offer a remote model or provider that is not already configured and
   verified in the active Hermes profile. Offer a local model only when it is
   already installed, compatible with the current hardware, open under the
   configured license policy, and verified in persisted inventory. Never
   download a model, enable a provider, or invoke billable verification.
5. Show the `manage enable --json` preview and explain its exact pack
   fingerprint, inventory state, daily cap, schedule, cron identity, and
   authority/control generations. Ask the user to approve that exact preview.
   Apply only with its unchanged `precondition_hash`.
6. Treat `manage reconcile`, `manage schedule`, `manage ranking-trust`,
   `manage daily-cap`, `manage freeze`, `manage unfreeze`, and `manage disable`
   the same way: preview, explain, obtain explicit approval, then apply the
   unchanged hash. `manage ranking-trust` replaces the complete trust set and
   requires the local pack path plus every trusted Ed25519 public key; reports
   may show only key-set fingerprints and count, never raw keys. `manage
   daily-cap --limit N` accepts only 1 through 10 and preserves admissions
   already consumed that UTC day. Never infer approval from a general request
   to "manage models."
7. Explain an automatic result with `manage status`, profile-scoped `manage
   history`, and the exact hold or lifecycle `reason_code`. State whether it was
   a no-change hold, proposed canary, promotion, rejection/rollback, cooldown,
   cap, lease, stale-authority, inventory, pack, or recovery condition. Do not
   turn status ordering into a new ranking or recommendation.

Enabling management creates one profile-local no-agent cron job for the
configured schedule. The job runs local reconciliation only; it does not start
an agent, call an LLM, refresh a catalog, probe a provider, use MoA or an
evaluator, or send telemetry. Existing decisions and live/resumed sessions keep
their recorded runtime, reasoning, and fallback snapshots.

For recovery, follow this exact sequence:

1. Preview `manage freeze --json`, explain it, obtain approval, and apply it
   with the unchanged hash.
2. Run `manage status --json` and `manage history --profile-id PROFILE_ID
   --json`. Identify the exact revision, receipt, backup checksum, phase, and
   hold code. Never inspect or repeat raw task, response, credential, endpoint,
   or provider payload content.
3. Run `manage recover --receipt-id RECEIPT_ID --json` for that exact
   incomplete receipt. Confirm that the preview binds the frozen control
   generation, receipt identity and phase, current config checksum, and exact
   backup checksum. Explain it, obtain explicit approval, and apply only with
   `manage recover --receipt-id RECEIPT_ID --apply --expect-hash SHA256 --json`.
   The command may restore only that receipt's checksum-matched pre-change
   bytes. If preview or apply reports changed authority/config/backup evidence
   or otherwise cannot prove exact recovery, keep management frozen and ask an
   operator to repair that receipt and backup. Do not edit SQLite, swap backups,
   or force a new plan.
   A `config_restore_started` history event is content-free evidence that the
   same receipt began restoring from its failed resulting authority. If apply
   stopped after restoring bytes but before lifecycle finalization, obtain a
   new preview and retry that exact receipt; never substitute a receipt or infer
   this marker from already-restored bytes.
4. Re-run status and history. Confirm receipt-bound `recovered` lifecycle
   evidence for every affected profile. Preview any required reconcile or
   control repair, explain the updated precondition, and obtain approval before
   applying it. If any affected profile remains in `recovery_required` because
   its exact prior canary or cooldown state cannot be proven, keep management
   frozen and ask an operator to repair it; never flatten ambiguous state to
   `eligible`.
5. Preview `manage unfreeze --json` only after recovery is proven and no
   affected profile remains in `recovery_required`. Obtain a new explicit
   approval and apply only that unchanged new hash.

This workflow never changes profile topology, existing adaptation controls, or
manual provider/model/reasoning intent. It uses only signed local rankings and
configured/verified or installed-compatible runtimes.

If activation returns to shadow and a later activation preview approves a new
inventory fingerprint, treat that preview as a new approval. It receives a
new receipt; an older session remains bound to its historical receipt ID.

Never hand-edit `mode: active`. Such an edit has no matching receipt and must
not project. To deactivate, preview and apply `activate --mode shadow` with the
same guarded hash flow.

### Optional Billable Verification

Offer this only during setup/edit when a desired exact runtime is
`configured_unverified` and policy explicitly allows paid access probes.

1. Run the `verify-runtime` preview. It must make zero provider requests.
2. Explain the exact runtime, billing kind, economics source, fixed probe
   shape, maximum monetary cost, maximum quota unit, budget reservation class,
   and expiration time shown in the preview.
3. Confirm `policy.allow_paid_access_probes` is true and obtain separate,
   explicit approval for that billable/quota-consuming call.
4. Apply with the unchanged `precondition_hash` and `--ack-billable`.
5. Run `inventory --refresh --json` after success before planning again.

Never offer or invoke billable verification in ordinary chat, `plan`,
inventory refresh, catalog refresh, autonomous adaptation, or runtime routing.

## Pitfalls

- Do not infer executable access from public model lists or credentials alone.
- Do not install or download models during inventory, planning, validation, or
  verification.
- Do not copy representative prompt bodies into CLI output, SQLite, journals,
  logs, or proposals. Only prompt indexes and derived requirements may persist.
- Do not hand-edit around a failed validator or resolver check.
- Do not reuse an apply hash after any preview input changes.
- Do not imply post-call model-changing failover is supported for Auto-owned
  routes; Stage 2 resolves the recorded chain only before the first request.
- Do not mutate a live conversation's model, tools, prompt, or history.

## Verification

After an approved write, confirm:

1. objective weights are explicit and normalized;
2. authority and the complete initial baseline share the approved checksums;
3. primary, fallback, and explicit safe-default targets are exactly verified;
4. reasoning defaults stay inside approved per-target bounds;
5. no pending apply journal remains;
6. `doctor` has no fatal authority or isolation error;
7. setup/edit status reports `shadow`; an explicitly approved activation
   reports `active` plus a matching receipt;
8. doctor reports the post-call failover warning; and
9. ordinary live/resumed agent provider/model identity did not change.
