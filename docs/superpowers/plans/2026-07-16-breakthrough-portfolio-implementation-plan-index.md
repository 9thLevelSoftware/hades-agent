# Hermes Breakthrough Portfolio Implementation Plan Index

**Source design:** [`2026-07-15-hermes-breakthrough-opportunity-portfolio-design.md`](../specs/2026-07-15-hermes-breakthrough-opportunity-portfolio-design.md)

**Purpose:** Map every approved portfolio item to its implementation-ready plan and define the evidence required before the nineteen-plan follow-on program is considered complete.

## Portfolio Map

| # | Portfolio item | Authoritative implementation plan |
|---:|---|---|
| 1 | Durable Goal-to-Outcome Missions | [`2026-07-15-missions-transactions-receipts-vertical-slice.md`](2026-07-15-missions-transactions-receipts-vertical-slice.md) |
| 2 | Reversible & Revisable Action Transactions | [`2026-07-16-reversible-revisable-action-transactions.md`](2026-07-16-reversible-revisable-action-transactions.md) |
| 3 | Teach-Once Automation Studio | [`2026-07-16-teach-once-automation-studio.md`](2026-07-16-teach-once-automation-studio.md) |
| 4 | Sovereign Personal Knowledge & Evidence Timeline | [`2026-07-16-sovereign-personal-knowledge-evidence-timeline.md`](2026-07-16-sovereign-personal-knowledge-evidence-timeline.md) |
| 5 | Universal Action Fabric | [`2026-07-16-universal-action-fabric.md`](2026-07-16-universal-action-fabric.md) |
| 6 | Preferences & Autonomy Center | [`2026-07-16-preferences-autonomy-center.md`](2026-07-16-preferences-autonomy-center.md) |
| 7 | Proactive Attention Broker | [`2026-07-16-proactive-attention-broker.md`](2026-07-16-proactive-attention-broker.md) |
| 8 | Plan Preview & What-If Dry Run | [`2026-07-16-plan-preview-what-if-dry-run.md`](2026-07-16-plan-preview-what-if-dry-run.md) |
| 9 | Verified Experience Compiler | [`2026-07-16-verified-experience-compiler.md`](2026-07-16-verified-experience-compiler.md) |
| 10 | Adaptive Intelligence Router | [`2026-07-15-hermes-auto-routing-roadmap.md`](2026-07-15-hermes-auto-routing-roadmap.md), which orders six independently executable stage plans |
| 11 | Interactive Agent Workspaces | [`2026-07-16-interactive-agent-workspaces.md`](2026-07-16-interactive-agent-workspaces.md) |
| 12 | Verified Outcome & Artifact Receipts | [`2026-07-16-verified-outcome-artifact-receipts.md`](2026-07-16-verified-outcome-artifact-receipts.md) |
| 13 | Sovereign Personal Compute Mesh | [`2026-07-16-sovereign-personal-compute-mesh.md`](2026-07-16-sovereign-personal-compute-mesh.md) |
| 14 | Diversity-Aware Cognitive Team Planner | [`2026-07-16-diversity-aware-cognitive-team-planner.md`](2026-07-16-diversity-aware-cognitive-team-planner.md) |
| 15 | Deterministic Information-Flow Guard | [`2026-07-16-deterministic-information-flow-guard.md`](2026-07-16-deterministic-information-flow-guard.md) |
| 16 | Live Presence | [`2026-07-16-live-presence.md`](2026-07-16-live-presence.md) |
| 17 | Safe Capability Exchange | [`2026-07-16-safe-capability-exchange.md`](2026-07-16-safe-capability-exchange.md) |
| 18 | Cache-Aware Context Compiler | [`2026-07-16-cache-aware-context-compiler.md`](2026-07-16-cache-aware-context-compiler.md) |
| 19 | Delegated Presence & Agent Federation | [`2026-07-16-delegated-presence-agent-federation.md`](2026-07-16-delegated-presence-agent-federation.md) |
| 20 | Bounded Purchase Assistant & Receipt Vault | [`2026-07-16-bounded-purchase-assistant-receipt-vault.md`](2026-07-16-bounded-purchase-assistant-receipt-vault.md) |

Item 1 is the plan completed before this follow-on program. Items 2–20 are the nineteen artifacts this program must verify. Item 10 legitimately uses a roadmap plus six stage plans because each stage is independently useful and reviewable; the roadmap is the single portfolio-level entry point.

Item 10 predates the follow-on plan template. Its roadmap owns the Approved Portfolio Contract and `Shared Stable Interfaces`; each stage task owns an exact Files block, focused failing test, implementation steps, passing verification command, and commit. For structural audit purposes, the roadmap-level shared-interface block is the Interfaces contract for every linked stage task, and a stage task's passing “verify/run focused tests” step is its GREEN step. This is a formatting compatibility rule only—the stage set must still meet the same behavioral, E2E, placeholder, and proof gates, including the added frozen 500-task acceptance task.

## Shared Dependency Order

The plans are independently reviewable, but implementation must respect these shared contracts:

1. Missions (#1), transactions (#2), authority (#6), and receipts (#12) establish durable intent, effects, permissions, and proof.
2. Teach-Once (#3), knowledge timeline (#4), action fabric (#5), attention broker (#7), preview (#8), experience compiler (#9), workspaces (#11), team planner (#14), and context compiler (#18) consume those contracts without creating incompatible local substitutes.
3. Information-flow control (#15) and capability exchange (#17) establish the security boundary required before broad live sensing, remote federation, or commerce.
4. Compute mesh (#13), live presence (#16), federation (#19), and commerce (#20) remain explicitly opt-in and advance only through their proof/incubation gates.
5. Adaptive routing (#10) preserves one primary provider/model per conversation cache lineage and treats privacy, residency, budget, and explicit model choices as hard authority constraints.

## Canonical Shared Contract Registry

Later plans consume or re-export these owner-defined contracts; they must not create local substitutes with different names, fields, status vocabularies, or certainty semantics.

| Owner | Canonical public contract |
|---|---|
| Missions (#1) | `MissionRecord`, `MissionReviewItem`, and the mission lifecycle/projection services defined by the vertical-slice plan |
| Effects and revisions (#2) | `ActionTransaction`, `TransactionRevision`, `RevisionNode`, `RevisionEdge`, `TransactionStore`, `TransactionCoordinator`, `AdapterDescriptor`, and `EffectAdapter` |
| Authority (#6) | `ActionContext`, `AuthorityDecision`, `AuthorityProvider`, `StoredAuthorityProvider`, and `authorize_effect()` |
| Events (#7) | `EventEnvelope` |
| Routing (#10) | `RoutingRequest`, `RoutingDecision`, and `AutoRoutingService.decide()` |
| Receipts (#12) | `Receipt`, `ReceiptObservation`, `ReceiptClaim`, `EvidenceDigest`, `ArtifactDigest`, `ReceiptSourceKey`, `ReceiptStore`, scorer-only `VerifiedReceiptDecision`, and `ReceiptIssuer` |
| Information flow (#15) | `FlowContext`, `FlowDecision`, `InformationFlowGuard`, and `StoredInformationFlowGuard` |
| Capabilities (#17) | `CapabilityManifest`, `CapabilityGrant`, and `CapabilityGrantStore`; grant implementation remains owned by #6 and is re-exported by `agent.capabilities` |
| Context compilation (#18) | `ContextSegment`, `ContextGraph`, `CacheIdentity`, `CompiledContext`, `ContextTransition`, `ContextCompressionPlan`, and `ProviderContextOptimizer` |

The receipt status vocabulary is exactly `verified`, `completed_unverified`, `failed`, `blocked`, and `unknown_effect`. Transaction approval and effect certainty remain owned by #2; general action authority remains owned by #6; receipt truth remains owned by #12; an allow from any one layer cannot override a block from another.

## Required Contents of Every Item Plan

Every mapped implementation plan must contain all of the following:

- the standard `superpowers:writing-plans` header and global constraints;
- a current-code file map with exact existing and proposed paths;
- explicit consumed/produced interfaces whose names remain consistent across tasks;
- TDD tasks with checkboxes, a focused RED command and expected failure, implementation content, a GREEN command and expected result, and one conventional commit;
- a real-path E2E proof using temporary profile-local state and real imports, with mocks limited to external network/provider/process boundaries;
- crash, replay, stale-authority, partial-failure, privacy/security, and rollback/recovery cases appropriate to the item;
- the exact 90-day corpus, denominator, metrics, thresholds, safety floors, stop conditions, and comparison baseline from the approved design;
- CLI/terminal/Ink TUI as the primary control surface when the feature is user-authored or governed, Dashboard only where the design makes it secondary, and no Desktop dependency unless the item is explicitly a realtime/visual surface;
- no new model-visible core tool when an existing tool, CLI + skill, service-gated tool, plugin, or MCP server is sufficient;
- byte-stable system prompt and effective tool-schema invariants, strict role alternation, and explicit new-conversation boundaries for newly published skills or changed cache identity;
- `config.yaml` for non-secret stable settings, secret stores/`.env` only for credentials, and durable SQLite state for runtime authority/audit/evidence;
- truthful language for `verified`, `completed_unverified`, `unknown_effect`, reversible, compensatable, idempotent, private, isolated, or exactly-once claims;
- operator documentation, bounded rollout, migration/compatibility behavior, and a final verification matrix.

## Completion Audit

The follow-on planning program is complete only when an automated and manual audit proves:

1. every item numbered 2 through 20 resolves to an existing non-empty plan entry point;
2. every plan has at least one implementation task and every task contains Files, Interfaces, RED, GREEN, and Commit sections, applying the documented item-10 roadmap-level Interfaces and passing-verification compatibility rule;
3. Markdown fences are balanced and `git diff --check` reports no whitespace errors;
4. no plan contains `TODO`, `TBD`, `FIXME`, “implement later,” “fill in details,” “add appropriate error handling,” “similar to Task,” or unresolved file/test placeholders;
5. each plan explicitly includes its approved layman outcome, design boundary, 90-day proof thresholds, dependencies, failure conditions, and Footprint Ladder delivery rung;
6. cross-plan public names for mission, effect, authority, evidence/receipt, capability, context, and event contracts do not conflict;
7. the Adaptive Intelligence Router roadmap and all six linked stage plans pass the same structural and placeholder audit;
8. the working tree contains only the intended plan/index changes before the final commit.

## Completed Audit — 2026-07-16

- All nineteen follow-on entry points (#2–#20) exist and are non-empty; the full portfolio map (#1–#20) has 21 resolving Markdown links including the source design.
- The follow-on entry points contain 231 granular tasks. Item #10's six linked stages contain another 45 independently executable tasks, for 276 planned tasks in the complete follow-on set.
- Every normal-plan task contains Files, Interfaces, RED, GREEN, and commit instructions. Every legacy routing-stage task contains its roadmap-owned shared interface contract, a focused failing test/RED run, a concrete change step, a passing focused/stage-gate run, and a commit.
- Standard headers, Approved Portfolio Contracts, product/ownership boundaries, exact proof denominators, dependencies, failure/stop gates, delivery rungs, balanced Markdown fences, and index link resolution all passed automated inspection.
- The forbidden-placeholder scan returned zero matches, and `git diff --check` returned no whitespace errors.
- Manual contract review confirmed the canonical names and ownership rules in the registry above, including the exact five receipt statuses and the rule that no permission, effect, flow, evidence, capability, or remote claim can silently substitute for another layer's decision.

This audit verifies implementation readiness and cross-plan consistency. It does not claim that the planned production code or its future test suites have already been implemented or executed.
