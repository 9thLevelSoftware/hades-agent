# Hermes Auto Routing — Stage 5 Autonomous Profile Management Design

## Status

User-approved design. This document authorizes implementation planning only;
implementation requires review and approval of the corresponding plan.

## Goal

Allow a user who explicitly enables global autonomous profile management to
keep existing route profiles current from trusted local runtime inventory and
signed ranking data. The system may automatically add, remove, and reorder
primary candidates and fallbacks within existing profiles, while preserving
all active and replayed routing decisions exactly as they were recorded.

Stage 5 extends the Stage 4 conservative-adaptation boundary. It does not
create, split, merge, or delete profiles, download models, enable providers,
or make paid verification requests.

## Authority and activation

- A global `autonomous_profile_management` control plane is disabled by
  default. When disabled, Stage 5 is read-only.
- When enabled, it applies to every existing profile. A guarded global freeze
  stops new automatic changes without interrupting routing, Stage 3 evidence,
  Stage 4 adaptation, status/history, or guarded recovery.
- Automation writes canonical profile-management revisions under the existing
  profile configuration lock. Each revision records the preceding authority,
  exact canonical patch, resulting authority, ranking-pack version, inventory
  fingerprints, reason codes, and timestamps. Records contain no prompt,
  response, task text, credential, endpoint, or provider payload.
- Direct user configuration changes always win. A changed authority cancels
  pending management activity and starts a new management epoch rather than
  being overwritten by a stale reconciler operation.
- Active, resumed, compressed, fixed, manual, recovered, and replayed
  decisions use their recorded snapshots. Only future fresh and eligible
  delegated decisions can observe a new configuration revision.

## Candidate sources and ranking data

The reconciler may consider only:

1. Configured runtimes with a current successful verification record.
2. Already-installed local models that Hermes can run and whose local
   capability record is current.
3. Signed, versioned ranking packs already available on disk.

It never downloads a model, enables or installs a provider, refreshes a
catalog, performs a paid probe, or queries live web/ranking sources. A
runtime without current verification is ineligible until an explicit
verification operation succeeds.

Each profile score is deterministic from the trusted ranking-pack metadata,
the profile's existing objective weights and hard limits, local capability,
and verified access. An invalid, expired, unsigned, or untrusted ranking pack
is rejected without changing any profile.

## Reconciliation and rollout

On a scheduled local trigger or explicit local command, the reconciler builds
a candidate inventory and computes the desired primary and fallback ordering
for each existing profile.

- A newly qualified top candidate first enters as an approved primary
  challenger. Stage 5 owns a separate profile-management experiment state that
  reuses Stage 4's deterministic canary, learner, and guardrail math to decide
  whether it becomes primary. It never changes a profile's existing
  `adaptation.enabled` setting or Stage 4 adaptation control state.
- Lower-ranked qualified candidates may be inserted, removed, or reordered in
  the fallback chain. Automation never removes the final viable route or a
  runtime referenced by an unfinished assignment.
- A profile-management revision is bounded by a configurable per-profile daily
  change cap. A cap hit creates a content-free hold record and leaves the
  authority untouched.
- Any material primary/fallback change starts a new management epoch. Stage 4
  adaptation remains limited to its own complete profile authority and never
  compares observations across a changed candidate set.
- A rejected canary, policy breach, budget exhaustion, retry/latency/cost
  guardrail, or other configured operational failure rolls the affected
  management revision back to its exact preceding authority and begins
  cooldown.

Existing Stage 4 assignment invariants still apply: a canary must be bound to
the exact final resolved profile, revision, runtime, and reasoning effort
before provider dispatch. A resolution or persistence failure falls back to a
recorded valid control route and never dispatches an unrecorded challenger.

## Operations and recovery

Read-only CLI and skill surfaces expose local inventory, ranking-pack status,
eligibility and rejection reasons, management status/history, configuration
revision lineage, current canary state, cooldown, and remaining daily change
budget. Outputs remain content-free and use only redacted IDs, fingerprints,
reason codes, and canonical version metadata.

Global enablement, freeze/unfreeze, ranking-pack trust configuration, and
daily-cap changes are guarded preview-then-apply controls. The preview hash
binds all authority, configuration, control generation, action, and requested
arguments. Once enabled, individual eligible reconciliation changes occur
automatically without separate approval.

The reconciler fails closed: inventory uncertainty, missing viable routes,
concurrent configuration updates, invalid state, tampered receipts, or
corrupted management storage produce a no-change hold. Corruption freezes
reconciliation until an explicit guarded repair or rollback succeeds.

## Explicit exclusions

Stage 5 does not introduce provider or model discovery beyond configured and
already-installed runtime inventory, candidate downloads, provider enablement,
live web ranking requests, paid automated probes, outbound telemetry,
model-visible tools, classifier/evaluator learning, MoA, fallback execution
outside the recorded user-owned chain, or profile topology mutation.

## Validation contract

Implementation must prove that:

- only configured-and-verified or already-installed runnable local models can
  enter the candidate set;
- signed-pack, verification, capability, and hard-limit validation fail closed
  before any configuration write;
- every automatic change has a canonical reversible management revision and
  obeys a per-profile daily cap;
- newly introduced primaries use Stage 4 canaries before promotion, while
  fallback changes preserve at least one viable route;
- manual, active, resumed, compressed, fixed, recovered, and replayed work
  preserves existing routing and fallback snapshots;
- config-lock, CAS, freeze, cooldown, rollback, and concurrent user-edit
  paths cannot overwrite a newer authority or dispatch an unrecorded route;
- ranking, inventory, receipts, and reports remain content-free; and
- no provider, network, telemetry, paid-probe, download, evaluator, or MoA
  path is reachable from reconciliation.

Focused unit, migration, concurrency, security, fresh-session, delegation,
gateway, TUI, and Windows/local-capability tests must cover these properties.
