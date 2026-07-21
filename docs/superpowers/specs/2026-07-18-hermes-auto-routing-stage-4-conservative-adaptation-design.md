# Hermes Auto Routing — Stage 4 Conservative Adaptation Design

## Status

Proposed Stage 4 design. This document authorizes planning only after user review;
it does not authorize implementation until the corresponding plan is approved.

## Goal

Allow an opted-in route profile to automatically, conservatively improve future
route choice among its already-approved primary targets and allowed reasoning
efforts. Every learned change is an immutable, reversible overlay; automatic
promotion and rollback are bounded by deterministic canaries and do not change
the user-owned routing authority.

## Decisions Made

- Adaptation is opt-in per profile. Existing profiles remain static after an
  upgrade until the operator enables it.
- It may automatically publish and roll back revisions within the Stage 4
  envelope.
- Quality evidence is limited to verified outcomes and explicit feedback.
  Latency, retries, provider failures, token counts, and cost are operational
  guardrails and report data, not inferred negative-quality labels.
- Default policy is a deterministic 5% canary fraction, 20 comparable samples
  before promotion, a 10% observed-regression rollback threshold, and
  exponential cooldown after rejection.
- `freeze` halts new adaptive proposals, canary assignments, and publication;
  it does not stop routing, evidence collection, feedback, reporting, or an
  explicitly guarded rollback.

## Stage 4 Envelope

Stage 4 may only materialize an overlay that:

- reorders already-approved **primary** target choices for one opted-in profile;
- selects an already-authorized reasoning effort within the selected target,
  profile, global, runtime-support, and Hermes explicit-override bounds; and
- refers only to the active authority revision's exact `RuntimeKey` stable
  identities.

It must not add or remove runtimes, profiles, candidates, fallback members, or
policy constraints. User-owned fallback chains remain unchanged. It must not
change classifier/evaluator behavior, profile topology, provider discovery,
MoA, model tools, telemetry, or YAML authority. Those belong to later stages.

## Invariants

- Manual provider/model/reasoning intent wins over adaptation.
- Hard policy, verified-access, capability, budget, and eligibility gates run
  before any learned ordering.
- Adaptation applies only when a new decision is created at a future fresh or
  eligible delegated boundary. It never alters an active, resumed, compressed,
  or recovered decision.
- A decision snapshots its complete adaptive revision and recorded fallback
  chain. Replay uses that snapshot only.
- All revisions, lifecycle events, assignments, and explanations are
  profile-local, canonical, checksummed, immutable, and content-free.
- An authority edit invalidates incompatible overlays; an overlay is never
  partially rebased across a changed authority envelope.

## Evidence and Comparability

The learner reads only local immutable evidence events:

- `verified` turn evidence is objective positive quality evidence.
- Ratings `rating-1` through `rating-5` contribute their Stage 3 normalized
  value only when attached to an exactly attributed routed turn.
- `rejected` and `corrected` are explicit adverse feedback. They can reject a
  canary only for the exact attributed assignment; they are not generalized
  from operational failures.
- `manual-reroute` is explicit preference/override evidence and excludes that
  observation from automatic quality scoring and promotion.
- Other terminal outcomes remain quality-unknown. Silence, latency, retry
  count, cost, and provider failure never become negative quality labels.

Evidence compares only initial routed tasks in the same profile and the same
content-free context bucket. A context with insufficient samples has no
promotion authority. There is no cross-profile pooling and no global winner.

## Learner and Revision Lifecycle

The learner uses a deterministic, conservative beta-binomial comparison of
verified outcomes, augmented only by bounded explicit numeric feedback. It
records every input event ID and derived aggregate in a content-free proposal
explanation. A proposal is eligible only when its challenger and control are
both existing approved primary choices and all policy/availability gates pass.

Lifecycle is durable and explicit:

`eligible → validated → canary → promoted | rejected → cooldown → eligible`

An optimizer lease allows one profile-local publisher at a time. It creates a
complete immutable revision, validates the typed overlay against its authority,
and atomically compares-and-swaps the active revision pointer. Runtime reads
one complete revision or the baseline; it never reads a partial overlay.

## Canary, Promotion, and Rollback

Canaries apply only to suitable future initial tasks with no manual pin,
fixed-delegation target, high-risk classification, or policy exclusion. The
assignment is deterministic from a profile-keyed HMAC over stable operation
identity, not a random decision ID. It is persisted before provider dispatch,
and durable recovery reuses the recorded assignment.

Default policy is configurable per profile:

| Control | Default |
| --- | --- |
| Canary fraction | 5% |
| Comparable samples before promotion | 20 |
| Observed regression rollback threshold | 10% |
| Cooldown | exponential, with new comparable evidence required |

Promotion requires the configured sample floor, conservative confidence over
the incumbent in the same context, and no policy/operational guardrail breach.
Repeated explicit adverse feedback, a policy failure, budget exhaustion, or a
configured observed regression rolls back to the exact prior complete revision
and begins cooldown. An unavailable canary runtime falls back through the
recorded user-owned chain but does not count as a successful canary result.

## Operator Controls

All controls are profile-local and use the existing guarded-control-plane
transaction pattern (dry run, precondition hash, lock, atomic write,
recoverable history):

- `hermes auto-routing adapt status`
- `hermes auto-routing adapt history`
- `hermes auto-routing adapt freeze`
- `hermes auto-routing adapt unfreeze`
- `hermes auto-routing adapt rollback --revision ID`

Status and history are read-only. Freeze blocks new mutation and canary
assignment while static routing and evidence continue. Rollback remains
available while frozen and restores an exact complete prior revision; it never
changes YAML authority.

## Architecture Boundaries

New plugin-local components are required:

1. A typed adaptive-overlay model and pure authority-bound validator/materializer.
2. Immutable revision, lifecycle, assignment, and optimizer-lease storage.
3. A learner that aggregates only allowed evidence and emits content-free
   proposals; it does not call providers, selector internals, or the network.
4. A service/CLI lifecycle layer for history, freeze, publication, and rollback.
5. A selector input seam that materializes exactly one validated active overlay
   before static selection and records that revision in every new decision.

The static selector remains deterministic. It receives effective profiles, not
raw evidence or learner logic. Existing manual precedence, prompt caching,
role alternation, decision replay, and fallback recovery paths remain intact.

## Testing and Completion Criteria

Implementation must prove:

- overlays cannot escape the authority envelope or override manual intent;
- freeze/rollback/history are atomic, profile-local, and recoverable;
- canary assignment is deterministic, persisted before dispatch, and reused on
  recovery;
- promotion, rejection, cooldown, and rollback use only comparable allowed
  evidence and never infer negative quality from operational data;
- authority changes invalidate incompatible overlays;
- active/resumed/recovered decisions keep their recorded revision and chain;
- no adaptive state contains prompt, response, endpoint, or credential data;
- no outbound network, telemetry, MoA, classifier/evaluator adaptation, runtime
  discovery, fallback mutation, or profile topology change is introduced; and
- cache, role-alternation, manual-precedence, TUI, gateway, CLI, and delegation
  regressions remain green.

## Deferred to Stage 5+

New runtime challengers, candidate/fallback mutation, classifier/evaluator
changes, profile split/merge, topology mutation, broader autonomy, and MoA are
explicitly out of scope.
