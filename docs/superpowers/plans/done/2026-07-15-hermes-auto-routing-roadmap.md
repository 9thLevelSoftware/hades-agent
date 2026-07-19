# Hermes Auto Model Routing Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved Hermes Auto model-selection design as six independently useful, reviewable stages while keeping semantic routing in an extraction-ready fork-local plugin.

**Architecture:** The implementation is a self-contained `plugins/auto_routing` package with a pure decision core and one versioned Hermes compatibility adapter. User YAML remains the immutable authority; profile-local SQLite stores observations, decisions, evidence, experiments, and adaptive revisions. Each linked plan ends at a usable stopping point and must pass its own gate before the next plan starts.

**Tech Stack:** Python `>=3.11,<3.14`, Pydantic `2.13.4`, SQLite/WAL, `ruamel.yaml 0.18.17`, Hermes `PluginContext` and `PluginLlm`, pytest `9.0.2`, standard-library hashing/locking/statistics, and existing Hermes runtime/inventory/provider APIs.

## Approved Portfolio Contract

- **Layman outcome:** Hermes chooses the cheapest, fastest, most private eligible model that is likely to succeed, and escalates auxiliary or newly created work when partial evidence shows the first choice is insufficient.
- **Design boundary:** Hard privacy, residency, modality, capability, authority, budget, and explicit provider/model constraints filter candidates before ranking. The primary provider/model stays pinned for the lifetime of a conversation cache lineage; an explicit primary transition starts a new lineage. Per-step adaptation is limited to auxiliary calls, evaluators, delegated children, and newly created executions, and may reuse safe partial artifacts without rewriting conversation history.
- **90-day proof:** Freeze 500 representative tasks, end-state scorers, strata, and irreversible/high-risk safety slices before evaluation. Against strongest-model-only, static cheap-first, and frontier-only-auxiliary baselines, require verified success no more than two absolute percentage points below strongest-model-only over the full corpus, no high-risk regression, at least 30% lower model cost, lower or equal cost per verified success, zero privacy/residency violations, stable primary-conversation cache identity, and materially better calibrated escalation than both static routing baselines.
- **Dependencies:** Item #6 supplies hard user authority and item #12 supplies independently scored outcomes. Until those contracts land, stages may collect compatible shadow evidence but cannot claim the portfolio proof complete.
- **Failure conditions:** Cost savings that hide quality loss, any policy escape, any implicit primary-model cache-lineage swap, uncalibrated escalation, or self-graded success fails the program even when unit and integration tests pass.
- **Delivery rung:** Footprint Ladder rung 1. Extend provider/model routing and observability through the extraction-ready plugin and existing runtime seams; providers remain plugins and no model-visible core tool is added.

## Global Constraints

- Preserve per-conversation prompt caching: one semantic route per fresh session, one route per delegated child, and no later-turn reclassification.
- Preserve strict message-role alternation and a byte-stable system prompt inside a route epoch; routing emits local activity/log records, never transcript messages.
- Keep MoA out of the v1 candidate inventory, profiles, recommendations, canaries, and fallback chains.
- Never add a model-visible core tool or fields to the `delegate_task` schema. Configuration is a native plugin CLI plus the explicit-load `auto-routing:auto-routing` skill.
- Keep user-facing behavioral settings under `plugins.entries.auto-routing` in profile-local `config.yaml`; `.env` remains credentials-only.
- Route, recommend, and canary only `verified` runtimes. `configured_unverified`, `temporarily_unavailable`, and `ineligible` runtimes remain visible with reasons but cannot be selected.
- Treat runtime identity as the full addressable access path. Main projection, delegated construction, recorded fallback, primary restoration, and later 401/429 recovery bind the selected credential-pool and local-backend identities; same-provider/model routes cannot inherit another pool or backend.
- Never probe access as an advisor/routing/optimizer side effect. The sole paid verification path is the Stage 1 user-run, previewed, cost-bounded, policy-enabled `verify-runtime --apply --expect-hash ... --ack-billable` command; later exact-attributed successful routed calls may refresh that same `RuntimeKey` TTL.
- Never copy API keys, OAuth tokens, credential-pool contents, or raw endpoint credentials into plugin YAML, SQLite, logs, exports, or decision explanations.
- Do not store raw prompts or responses in the routing database by default. Classifier/evaluator content is transient and allowed only by the approved full-disclosure policy.
- Hard policy filtering precedes ranking. Autonomous code cannot loosen provider/model denies, spend ceilings, latency ceilings, privacy, license, hardware, or plugin-LLM trust.
- Automatic local-model installation is forbidden. The advisor may explain why an uninstalled local model is excluded, but it does not rank, recommend, route, or canary that model; installation remains a separate user-directed workflow outside routing.
- Profile primary/fallback chains are authoritative for Auto routes. Hermes global fallbacks remain unchanged when Auto is bypassed and are not implicitly appended to Auto decisions.
- Post-call Auto failover may consume only a recorded fallback, must project that target's reasoning policy, and must record a cache-reset epoch; disable that capability when the adapter cannot prove the contract.
- Full autonomy uses conservative deterministic canaries and complete immutable revisions; it never ships enabled merely because static routing works.
- Every guarded control-plane CLI command—authority/config, mode, freeze state, active-revision pointer, experiment control, import/restore, or billable access verification—defaults to preview, requires `--apply` plus an exact precondition hash, takes a cross-process lock, preserves unrelated YAML, writes atomically, and creates a recoverable backup where replacement is possible. Content-free, deduplicated append-only observations (`inventory --refresh`, catalog refresh, outcome receipts, and finite-vocabulary feedback) are explicit transactional exceptions and cannot change authority, activation, the active revision, or an already projected route; command metadata labels all operations `read_only`, `append_only_observation`, or `guarded_control_plane`.
- Migration, restore, and downgrade require process quiescence: they refuse to replace SQLite while another live PID/process-start-token lease can retain an open connection, and new opens acknowledge the published maintenance generation.
- Use real imports and a temporary `HERMES_HOME` for integration tests. Mock only provider/network completion boundaries, clock/randomness, and process interruption.
- Preserve contributor credit and license notices for any code salvaged from `b3nw/hermes-delegate-routing` or the unmerged Hermes smart-routing prototype.
- Each implementation task begins with a focused failing test, records RED, implements the smallest complete behavior, records GREEN plus relevant regressions, runs `git diff --check`, and ends in one conventional commit.

---

## Plan Set and Delivery Gates

| Order | Plan | Working stopping point | Gate before continuing |
|---:|---|---|---|
| 1 | [Foundation and Read-Only Advisor](2026-07-15-hermes-auto-routing-foundation-advisor.md) | Enabled plugin stays in `shadow`, inventories only executable runtimes, offers an explicit bounded access-verification flow, imports provenance-bearing evidence, proposes/validates profiles, and applies approved YAML atomically without changing a model choice. | `doctor` passes; no route projection code is installed; advisor never recommends an unverified runtime or performs an unapproved probe. |
| 2 | [Cache-Safe Static Runtime](2026-07-15-hermes-auto-routing-cache-safe-runtime.md) | Rules + structured classifier + deterministic selector route fresh sessions and each delegated child, with resume reuse, manual-pin precedence, authoritative fallbacks, and `off`/`shadow`/`active`. | Cache-contract and real-path integration suites pass on the reviewed Hermes `0.18.2` contract; adapter drift disables Auto without changing baseline behavior. |
| 3 | [Evidence and Local Reporting](2026-07-15-hermes-auto-routing-evidence-reporting.md) | Objective, explicit, behavioral, evaluator, and operational signals are normalized and attributed locally; reports work; adaptive writes are still disabled. | Raw-content/secret scans pass; stronger evidence outranks weak proxies; silence is never success. |
| 4 | [Conservative Adaptation and Canaries](2026-07-15-hermes-auto-routing-adaptive-canaries.md) | After an explicit hash-guarded `mode conservative` transition, approved target/effort combinations learn contextually; a coalescing background scheduler advances deterministic canaries that promote, reject, cool down, roll back, freeze, and restore immutable revisions. | Property tests prove installation never enables adaptation, automatic progress needs no manual `optimize`, and no policy escape, allocation breach, partial revision read, or inexact rollback occurs. |
| 5 | [Full Autonomy](2026-07-15-hermes-auto-routing-full-autonomy.md) | Newly verified runtimes become challengers; classifier/evaluator changes run in shadow first; profiles, fallbacks, weights, and reasoning adapt inside authority; split/merge preserves aliases and tombstones. | Synthetic traces prove contextual rather than global promotion, zero manual optimizer calls, and deterministic rule semantics across topology changes. |
| 6 | [Hardening and Extraction](2026-07-15-hermes-auto-routing-hardening-extraction.md) | Multi-process recovery, migrations, backup/restore, export/import, compatibility tooling, live opt-in smoke tests, and standalone-repository extraction documentation are complete. | Supported-version matrix, stress/fault/security suites, packaging tests, and clean-install smoke test pass. |

The file names above are execution order, not optional feature flags. A team may stop after any stage and retain useful software, but a later plan assumes the preceding plan's public interfaces and migrations exist exactly as documented.

## Shared Package Ownership

```text
plugins/auto_routing/
├── plugin.yaml                         # opt-in standalone manifest
├── __init__.py                         # registration only
├── README.md                           # local operator and extraction notes
├── skills/auto-routing/SKILL.md        # explicit-load advisor; moved under auto_routing/assets in Plan 6
└── auto_routing/
    ├── __init__.py                     # stable package exports/version
    ├── models.py                       # immutable domain/config records
    ├── config.py                       # validation and authority hashing
    ├── config_io.py                    # preview/lock/precondition/backup/apply
    ├── storage.py                      # profile-local SQLite and migrations
    ├── inventory.py                    # four-state executable inventory
    ├── catalog.py                      # provenance-preserving evidence catalog
    ├── scoring.py                      # shared objective/access-economics utility
    ├── advisor.py                      # profile proposal and dry-run service
    ├── cli.py                          # `hermes auto-routing ...`
    ├── service.py                      # process-local composition root
    ├── host.py                         # extraction-time injected host protocols (Plan 6)
    ├── rules.py                        # deterministic task facts and user rules
    ├── classifier.py                   # structured plugin-LLM assessment
    ├── selector.py                     # hard filters, scores, effort, fallbacks
    ├── decisions.py                    # decision persistence/explanation
    ├── evidence.py                     # signal normalization/attribution
    ├── learner.py                      # contextual estimates and revisions
    ├── experiments.py                  # shadow/canary/promotion/rollback
    ├── optimizer.py                    # coalescing automatic trigger scheduler/runner
    └── adapters/
        ├── __init__.py
        ├── base.py                     # Hermes-independent adapter protocols
        └── hermes_0_18.py              # guarded, exact Hermes 0.18.2 projections

tests/plugins/auto_routing/             # focused unit, integration, fault tests
```

Only `adapters/hermes_0_18.py` imports private Hermes runtime/delegation/session symbols. The decision core accepts protocols and immutable records, so extracting the plugin later does not require pulling Hermes internals into its core.

Seven generic, feature-neutral host capabilities are introduced in execution
order. Plan 1 forwards a trust-gated `reasoning_config` through `PluginLlm`,
preserves authenticated-live/contract/static provenance in provider model
discovery, and exposes content-free exact per-model reasoning support. Plan 2
adds content-free `RuntimeIntent` provenance, one exact memory-only runtime-
access binding shared by main switches, child construction, direct fallback,
and primary restoration, and an internal-only delegated-child reasoning
override. Plan 3 versions the canonical post-turn finalizer
payload. They contain no Auto Routing policy and do not change any model-
visible tool schema. Plan 6 records and validates all seven as standalone host
prerequisites.

## Shared Stable Interfaces

Every plan must preserve these names once introduced:

```text
RuntimeKey
AccessEconomics
RoutingTarget
RouteProfile
TaskAssessment
RoutingDecision
EvidenceEvent
AutoRoutingConfig
PolicyEnvelope
AdaptiveRevision
ReasoningSupport

AutoRoutingService.decide(request: RoutingRequest) -> RoutingDecision
AutoRoutingService.explain(decision_id: str, *, detailed: bool = False) -> dict
HermesAdapter.inventory(*, refresh: bool = False) -> list[RuntimeObservation]
HermesAdapter.resolve(runtime_key: RuntimeKey) -> ResolvedRuntime
HermesAdapter.install(service: AutoRoutingService) -> AdapterStatus
resolve_reasoning_support(*, provider, model, api_mode, metadata=None) -> ReasoningSupport
InventoryService.preview_verification(runtime_id: str) -> VerificationPreview
InventoryService.apply_verification(precondition_hash: str, acknowledge_billable: bool) -> VerificationResult
RoutingStore.read_active_revision(authority_id: str) -> AdaptiveRevision
RoutingStore.record_decision(decision: RoutingDecision) -> None
```

Schema evolution is additive and idempotent. `SCHEMA_SQL` is the declarative source of truth; column reconciliation repairs additive drift; ordered migration versions are reserved for data transforms that cannot be expressed with `CREATE ... IF NOT EXISTS`.

## Supported-Surface Policy

| Surface | Initial policy | Reason |
|---|---|---|
| Classic CLI, gateway, TUI/Desktop, API sessions | Fresh-session Auto eligible | They expose stable session IDs and persistent conversations. |
| Delegated child, including mixed batches | Auto eligible per child | Each child is a new prompt-cache namespace. |
| One-shot and ACP | Auto eligible after adapter contract tests | They can route before their first provider call and persist a decision ID for the run. |
| Cron/scheduled jobs | Pinned; advisor recommendations are read-only | Native per-job reasoning and immutable route-revision support do not yet exist, so execution remains reproducible and explicit. |
| Batch runner/benchmarks | Pinned; advisor recommendations are read-only | Benchmarks remain reproducible until a native per-run route-revision contract exists. |
| Curator, compression, title generation, hygiene, prompt-size probes, and other auxiliary agents | Excluded | They already have task-specific auxiliary routing and must not be silently retargeted. |

## Execution Checklist

- [ ] Execute Plan 1 and confirm the plugin can be installed/enabled while all ordinary sessions retain their original runtime.
- [ ] Execute Plan 2 and keep activation at `shadow` until `hermes auto-routing doctor` reports the adapter, inventory, safe default, classifier trust, and fallback projection contracts healthy.
- [ ] Execute Plan 3 and inspect at least one local report before enabling any adaptive mutation.
- [ ] Execute Plan 4 with a configured canary fraction at or below the immutable `max_canary_fraction`; explicitly apply `mode conservative`, prove post-evidence optimization progresses automatically, then verify `freeze` and exact rollback before leaving it enabled.
- [ ] Execute Plan 5 only after the operator explicitly changes adaptation mode from conservative approved-target learning to autonomous mutation.
- [ ] Execute Plan 6 before extracting or publishing the plugin outside this fork.

## Final Cross-Plan Verification

Run after every plan has landed:

```bash
uv run --extra dev python -m pytest -q tests/plugins/auto_routing
uv run --extra dev python -m pytest -q \
  tests/agent/test_plugin_llm.py \
  tests/agent/test_runtime_access.py \
  tests/agent/test_restore_primary_pool_reselect.py \
  tests/test_plugin_skills.py \
  tests/test_packaging_metadata.py \
  tests/hermes_cli/test_provider_live_curated_merge.py \
  tests/hermes_cli/test_runtime_provider_resolution.py \
  tests/gateway/test_agent_cache.py \
  tests/tools/test_delegate.py \
  tests/tools/test_async_delegation.py \
  tests/run_agent/test_provider_fallback.py \
  tests/run_agent/test_fallback_reasoning_override.py
uv run --extra dev ruff check plugins/auto_routing tests/plugins/auto_routing
git diff --check
```

Expected: all selected tests pass, Ruff reports no errors, and `git diff --check` emits no output.
