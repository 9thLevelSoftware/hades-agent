# Harness Review & Enhancement Roadmap — July 2026

A combined **codebase review** of the 9thLevelSoftware Hermes Agent fork and a **technology scan** of the mid-2026 LLM-harness landscape, cross-referenced into a ranked list of ten high-value additions. The focus is deliberately on *adding missing capabilities and strongly enhancing existing ones*, not on bug-fixing.

**Companion documents (full fidelity):**

- [Appendix A — Ranked candidates & grounded proposals](2026-07-harness-enhancement-roadmap-appendix-a-proposals.md): the full 22-candidate ranked list and the complete grounded write-up of the top 13 (existing state, design, verified integration points, effort, risks, judge notes).
- [Appendix B — Subsystem review & technology scan](2026-07-harness-enhancement-roadmap-appendix-b-review-research.md): the complete 10-subsystem codebase review (capabilities, gaps, extension points, key files) and the complete 72-finding technology scan with sources.

## Methodology

Produced by a 39-agent orchestrated workflow:

1. **Map** — 10 agents each read one subsystem of the codebase (core loop, context engine, learning loop, tools, gateway, providers, execution/security, delegation/automation, UI/observability, fork-vs-upstream delta) and produced capability/gap/extension-point maps.
2. **Research** — 8 agents did live web research on mid-2026 harness technology (context engineering, memory systems, multi-agent orchestration, tool use/MCP, security/sandboxing, self-improvement/evals, harness UX, inference/routing).
3. **Synthesize** — 3 agents cross-referenced maps × research through different lenses (capability, self-improvement, platform/trust) producing 27 raw candidates, merged/deduplicated to 22.
4. **Judge** — 4 agents scored every candidate 0–10 from four lenses: user value, feasibility in this codebase, cutting-edge differentiation, and leverage/compounding.
5. **Ground** — the top 13 candidates each got a dedicated agent that read the actual source, verified the proposal was not already implemented, mapped real integration points (file/line), and produced a design sketch, effort estimate, and risk list.

All 13 finalists survived the already-implemented check. Scores below are 4-judge means.

---

## Part 1 — State of the fork (condensed)

The full subsystem review is in Appendix B. The short version:

**What is genuinely strong.** A pluggable `ContextEngine` with a battle-hardened lossy compressor and prompt-cache-stable prompt assembly; a five-layer learning loop (curated memory, background review, curator, learning graph, FTS5 session search) that no other open harness matches in ambition; a ~30-platform gateway behind one adapter contract; six terminal backends behind one `BaseEnvironment` abstraction; a two-layer provider system (declarative profiles + four transports) with credential pooling and fallback chains; heavyweight delegation observability (spawn trees, cost rollups, progress relay); and a hardened webhook platform, cron scheduler, and kanban/worktree substrate.

**What the fork itself added** (~199 commits over upstream): a Visual Workflows subsystem; a reliability spine (canonical 8-state turn outcomes, identity-bound approvals, unresolved-side-effect semantics); and the cap-review security series (approval integrity, symlink/profile bypass closure, delegation lifecycle recovery, operation metadata on tools — `read_only`/`destructive`/`idempotent` flags and `operation_key` idempotency keys plumbed through every dispatch path).

**Cross-cutting gap themes** the review surfaced, each echoed in multiple subsystems:

1. **Plumbed-but-unconsumed instrumentation.** Turn outcomes are classified but never persisted; operation metadata flows through middleware that nothing reads; skill telemetry counts uses but never outcomes. The fork keeps building excellent sensors and not wiring them to anything.
2. **Blocklist security, no policy engine.** Regex patterns for terminal, path lists for file tools, nothing at all for the other ~84 builtin + all MCP/plugin tools. No default-deny, no read-only mode, no per-project rules, no OS-level sandbox on the default local backend.
3. **Nothing survives process death.** Subagents are threads; mid-turn loop state is Python locals; interrupted mutating tools resolve to "effect UNKNOWN". Detection (fork-built) exists; recovery does not.
4. **Retrieval is keyword-or-nothing.** FTS5/BM25 everywhere, embeddings nowhere; memory is two flat char-capped files; the archive is write-only; nothing prefetches relevant history.
5. **Time-only automation, output-blind delivery.** Cron fires on clocks, not events (reactions, edits, webhooks→jobs, email, PR/CI); results deliver fire-and-forget with no review step.
6. **Measured-but-unenforced cost.** Per-call USD is computed and persisted; the only brake anywhere is iteration count.

---

## Part 2 — What changed in the harness world (condensed)

The full 72-finding scan with sources is in Appendix B. The developments that most shaped the top 10:

- **Code-mode tool calling went mainstream.** Anthropic Programmatic Tool Calling (37% token reduction), Cloudflare Code Mode (~99.9% context reduction over 2,500 endpoints via `search()`+`execute()`), Microsoft Agent Framework CodeAct (52% faster, 64% fewer tokens). The pattern is provider-agnostic: expose tools as a generated code API, keep intermediate results out of context.
- **Context management moved server-side.** Anthropic `context_management` edits + `compact_20260112` (84% token savings, +39% on 100-turn agentic benchmarks, cache-preserving because edits run after cache lookup) and OpenAI `/responses/compact`. Client-side stripping that destroys prompt caches is now legacy.
- **Memory converged on tiered files + hybrid retrieval + idle-time consolidation.** Claude Code auto-memory (index + topic files), Anthropic memory tool, Letta MemFS/sleep-time compute (~5× token reduction, +13–18% accuracy), Honcho dreaming, Mem0/Zep-Graphiti hybrid retrieval.
- **Durable execution became the orchestration standard.** Vercel Workflow DevKit GA (100M+ runs), Temporal agent patterns, LangGraph checkpointing, Claude Code background-agent supervisor daemons with resume-from-saved-state, git worktrees as the converged isolation primitive.
- **Security consensus: OS jails + deterministic policy outside the model.** Anthropic sandbox-runtime (bubblewrap/seccomp/Seatbelt + egress proxy, Apache-2.0), Codex CLI's Landlock/Seatbelt trio, credential-broker proxies (secrets never enter the agent), CaMeL-family injection defenses, NSA MCP security guidance, real-world MCP supply-chain attacks.
- **Self-improvement got measurable — and humbled.** SkillsBench: curated skills +16.2pp but *self-generated skills yield negligible-to-negative gains without verification*. Trace2Skill: batch trajectory distillation (+57pp transfer) beats one-shot reflection. Terminal-bench/Harbor standardized harness-agnostic evals.
- **Harness UX table stakes rose.** Event channels pushing into running sessions, review-queue automations (Codex app), checkpoint/rewind, 12-event hooks, plugin marketplaces, background-by-default subagents, fleet views, phone remote control.
- **Reasoning-effort control replaced token budgets** (Anthropic adaptive thinking, OpenAI `reasoning_effort`) as the sanctioned cost/latency degrade knob; OTel GenAI semconv v1.41 became the telemetry standard.

---

## Part 3 — The Top 10

Ordered by 4-judge mean score. Every item was grounded against the source tree by a dedicated agent; file references below are verified. Full designs in Appendix A.

### 1. Code-Mode Tool Orchestration — full-registry programmatic tool calling with a persistent kernel — 7.88

**What.** Upgrade `execute_code` from 7 hardcoded stubs into a generated, typed Python API over the *entire* tool surface (builtin + plugin + MCP), plus an opt-in persistent interpreter session (notebook-style variable reuse across calls) and a structured artifact return channel (files/images, not just 50KB stdout). "Process these 2,000 rows across three MCP servers" becomes one turn instead of an unaffordable 100-iteration loop.

**Why now.** Anthropic PTC, Cloudflare Code Mode, and Microsoft CodeAct all shipped the same lesson: tools-as-code-API with intermediate results kept out of context is the single biggest token/latency lever in modern harnesses — and the pattern runs locally against any model, which fits Hermes' multi-provider identity perfectly.

**What exists.** A solid foundation: two RPC transports (UDS `_rpc_server_loop`, file-based `_rpc_poll_loop` for remote backends) dispatching through the real `handle_function_call` path — but only 7 stubs (`_TOOL_STUBS`, `tools/code_execution_tool.py:227`), a fresh process per call, and stdout-only returns.

**Design in brief.** (a) Generate typed stubs from `ToolEntry.schema` for session-enabled tools, plus Cloudflare-style `search_tools()`/`describe_tool()`/`call_tool()` generics reusing `tools/tool_search.py`'s catalog for the long tail — constant schema-token cost. (b) Widen the allowlist from 7 curated names to "session tools minus a recursion/interactivity denylist", forwarding full session context so approvals, guardrails, and middleware fire on the real tool name; auto-allow `read_only` tools, route `destructive` ones through the existing approval context. (c) Persistent kernel keyed on `(task_id, session)` mirroring `terminal_tool`'s `_active_environments` registry. (d) Artifact channel via the `tool_result_storage` spill pattern with multimodal envelope unwrap.

**Integration points.** `tools/code_execution_tool.py`, `tools/registry.py` (`get_operation_metadata`), `model_tools.py:handle_function_call`, `tools/mcp_tool.py`, `tools/tool_search.py`, `hermes_cli/middleware.py`, `tools/approval.py`.

**Effort / risks.** L. Dominant risk is security: full-registry scripting means generated code can loop destructive ops at machine speed — gate on operation metadata (start conservative; MCP defaults `destructive=True`), and batch approvals per script to avoid prompt storms. Feature 4 (policy engine) is the natural companion.

### 2. Turn-Outcome Ledger — signal-driven reflection triggers + outcome-weighted skill utility — 7.50

**What.** Persist the fork's already-computed 8-state turn outcome (plus cost, retries, guardrail halts, skills loaded) as a per-turn row in the session DB, then close two loops with it: (a) replace blind every-N-turns memory/skill nudges with *event-driven reflection* — fire background review on failed/blocked/unresolved outcomes, detected user corrections, tool-failure streaks, and inbound emoji reactions; (b) attribute outcomes to the skills loaded that turn, giving the Curator measured helped/hurt utility instead of staleness heuristics, and ranking the system-prompt skill index by utility × relevance instead of alphabetically.

**Why now.** This is the fork's cheapest big win — mostly wiring, not building. `agent/turn_outcome.py` classifies every turn at two finalizer sites and the result is *thrown away* (`hermes_state.py` has zero `outcome` references). Today background review structurally requires `outcome == "verified"` (`agent/turn_finalizer.py:506`) — the harness literally only studies its successes, while SkillsBench/Reflexion-era practice says failures carry the signal. The curator's own prompt admits its evidence is weak ("usage counters are not evidence").

**Design in brief.** New `turn_outcomes` table (pure `SCHEMA_SQL` edit — the declarative `_reconcile_columns` path handles migration); a shared `agent/turn_ledger.py` helper called from *both* finalizer paths (`turn_finalizer.py` and `codex_runtime.py` — the second is easy to miss); skill attribution reusing the extraction logic `insights.py:325-344` already implements; a lexical correction detector patterned on `agent/reactions.py`; reaction ingestion (Telegram `MessageReactionHandler`, Discord `on_raw_reaction_add`, Slack's existing no-op `handle_reaction_added`); a new failure-review prompt inheriting the existing "do NOT capture environment-dependent failures" guardrails as the self-poisoning firewall; Laplace-smoothed utility scores feeding `curator.py`'s candidate table and `prompt_builder.py`'s skill index.

**Integration points.** `agent/turn_outcome.py`, `agent/turn_finalizer.py`, `agent/codex_runtime.py`, `hermes_state.py`, `tools/skill_usage.py`, `agent/background_review.py`, `agent/curator.py`, `agent/prompt_builder.py`.

**Effort / risks.** L, cleanly stageable (ledger → triggers → utility ranking). Risks: dual finalizer paths must both write; failure-triggered review needs cooldowns and the guardrail prompt to avoid mass-producing negative-claim skills. This ledger is also the prerequisite for #10-adjacent batch distillation (near-miss #13).

### 3. Durable Agent Execution — checkpointed turns, exactly-once tool effects, crash-surviving subagent fleet — 7.13

**What.** Make in-flight work survive process death, end to end: delegate children become OS-isolated processes (optional git-worktree per code task) whose transcripts checkpoint to the SessionDB and auto-resume after crash/restart, surfaced through a fleet view (working / needs-input / failed, with peek/reply/stop/respawn) over Telegram/Discord/TUI; the turn loop persists a compact `TurnCheckpoint` at its two natural barriers; and the fork's `operation_key` finally becomes an idempotency *journal* so provider retries and resumed turns return recorded results instead of re-sending the email.

**Why now.** Hermes' pitch is a long-lived agent on a VPS steered from your phone — exactly the deployment where process death is routine. The fork already built crash *detection* (PID-liveness on delegation records, "interrupted" marking); Claude Code background agents, Vercel/Temporal durable execution, and worktree-isolated parallel agents defined the recovery pattern. Today an interrupted mutating tool becomes a literal instruction to the model to guess ("effect UNKNOWN").

**Design in brief.** Four phased workstreams: **W1** `tool_effect_journal` table + built-in middleware wrapping the three dispatch sites (they already pass `operation_key`) — ship first, independently valuable; **W2** `delegation.isolation: process` spawning detached `hermes --resume <child_session>` children (extracting `kanban_db.py`'s `_default_spawn`/worktree machinery), completion riding the existing async-delegation event rail; **W3** auto-redispatch with budget/backoff where `_reserve_record_locked` currently terminal-marks; **W4** `turn_checkpoints` upserted at the pre-API-call and post-tool-batch barriers that already flush messages, plus the fleet view.

**Integration points.** `tools/delegate_tool.py`, `tools/async_delegation.py`, `hermes_cli/kanban_db.py`, `hermes_state.py`, `run_agent.py:_flush_messages_to_session_db`, `agent/conversation_loop.py`, `agent/tool_executor.py` ([UNRESOLVED] branches at lines 854/892), `tools/registry.py:operation_key`.

**Effort / risks.** XL overall — phase it; W1 alone is an M with outsized payoff. Risks: checkpoint/transcript consistency across compaction rebaselines; journaled results for non-deterministic tools must be marked; unattended re-execution must share the restart-loop breaker.

### 4. Declarative Permission-Policy Engine — allow/ask/deny rules over the fork's operation metadata — 7.13

**What.** A default-deny-capable, layered permission engine: global + per-project rule files with tool/argument matchers (`terminal(git:*)=allow`, `write_file(/etc/**)=deny`, `mcp_*=ask`), permission modes (default / structural read-only "plan" / acceptEdits / yolo), TTL/scope-bound grants, and a `/permissions` surface. Rules key off `read_only`/`destructive`/`idempotent` — so read-only mode structurally blocks every mutating tool, including MCP and plugin tools.

**Why now.** This is the fork's own thesis completed: PRs #18/#22 plumbed operation metadata through every dispatch path and *nothing consumes it*. Claude Code permission rules and Codex approval policies made declarative tool policy table stakes; MCP 2025-11 annotations supply the vocabulary. Today `/plan` is prompt-level theater, "always allow" grows one global flat list, and 84 builtin + all MCP/plugin tools have no policy at all.

**Design in brief.** New `tools/policy.py` (~500 LOC) with compiled rule layers (matcher → allow/ask/deny + provenance), precedence hardline-floor > deny > mode > allow > ask; registered as a non-removable position-0 callback in the existing `TOOL_EXECUTION_MIDDLEWARE` chain — with the critical detail that the chain fails *open* on exceptions (`hermes_cli/middleware.py:292-303`), so the policy callback must catch its own errors and return deny itself. `ask` routes through the existing `request_tool_approval()` — inheriting CLI prompts, gateway approval buttons, ACP bridging, and fail-closed-when-no-human for free; its "always" answer writes a scoped grant instead of the flat allowlist. Project-level `allow` rules require a one-time trust grant (a prompt-injected agent can write files in cwd) and the rules file joins the sensitive-path denylist. MCP `readOnlyHint` ingestion gated on server trust tier.

**Integration points.** `hermes_cli/middleware.py`, `tools/registry.py`, `tools/approval.py`, `tools/mcp_tool.py:_register_server_tools`, `hermes_cli/config.py`, plus the three dispatch sites.

**Effort / risks.** L (ship name+prefix+path+metadata matchers first; full argument-DSL parity later). Risks: mis-tagged `read_only` flags structurally bypass plan mode (the fork already fixed one such case — `e1b9b5f`); annotation sweep needs review discipline. Multiplies the value of #1 (script-level policy) and #3 (per-child policy profiles).

### 5. Event-Driven Automations with a Review Queue — 7.00

**What.** Extend the time-only cron stack into an event engine: message reactions, user message edits, generic webhooks, IMAP/email push, and GitHub PR/CI events fire jobs whose results land in a per-user *review queue* (accept / retry / discard from chat) instead of fire-and-forget delivery. "Watch this PR and fix CI", "triage important mail as it arrives" become one-sentence automations.

**Why now.** Codex app Automations (results-to-review-queue) and Claude Code Channels (events pushed into running sessions) defined the 2026 automation UX. Hermes already owns both halves — a hardened webhook platform (HMAC, idempotency, rate limits, GitHub event parsing) and a mature job stack (`run_one_job`, DeliveryRouter, continuable threads) — but an inbound event can only start a throwaway chat session, and a job can only start from a clock. The `CronScheduler` ABC even reserves the exact seam (`fire_due`, `scheduler_provider.py:85`).

**Design in brief.** `trigger={source, filter}` job records with `schedule.kind="event"` (ticker skips them structurally; they fire only through `fire_due`'s existing CAS claim); a `plugins/event_sources/` registry cloned from the cron-provider loader; webhook routes gain `trigger_job` (reusing HMAC/idempotency/templating verbatim); reactions/edits become `MessageType.REACTION`/`EDIT` on the base adapter with per-platform handlers; review queue at `~/.hermes/cron/review.json` with jobs.json's atomic-write discipline — accept delivers via the existing router, retry re-fires with verdict feedback appended, and the verdict stream doubles as labeled outcomes for #2's ledger.

**Integration points.** `cron/scheduler_provider.py`, `cron/jobs.py`, `cron/scheduler.py`, `gateway/platforms/webhook.py`, `gateway/platforms/base.py`, `plugins/platforms/{telegram,discord,slack}/adapter.py`, `hermes_cli/webhook.py`.

**Effort / risks.** L phased (webhook→job + review queue first). Risks: event payloads are attacker-controlled prompt-injection at scale (route through the existing cron prompt scanning; keep HMAC/auth per source); reactions bypass the gateway auth gate today and need per-adapter allowed-user checks; self-loop guards for the bot's own lifecycle reactions.

### 6. Tiered Paged Memory with Hybrid Semantic Recall — 7.00

**What.** Replace the flat 2,200-char `MEMORY.md` with a two-layer store — a small always-injected index (stable, cache-friendly) pointing at topic files loaded on demand, per-entry metadata (timestamp, provenance, confidence), and a *readable* archive (`memory_search` action) so overflow becomes paging instead of destructive consolidation. Retrieval half: a zero-dependency embedding sidecar (sqlite-vec or a llama.cpp embedding endpoint) over sessions, memory entries, and skill descriptions, fused with the existing FTS5 BM25 via reciprocal-rank fusion, plus automatic prefetch of relevant past-session context at turn start (injected through the API-call-time user-message channel — never the system prompt, preserving the frozen-snapshot cache invariant).

**Why now.** Claude Code auto-memory shipped exactly this index+topics shape; Letta MemFS endorses file-tiered memory; Mem0/Zep made hybrid retrieval standard. On a default Hermes install, cross-session continuity is a ~3.5KB frozen snapshot plus a search tool the model must remember to call, and overflow triggers the destructive-consolidation path fragile enough to have needed a failure-cap guard. The fork's archive is verifiably write-only — grep finds zero readers.

**Design in brief.** A `BuiltinMemoryProvider` implementing the existing `MemoryProvider` ABC (the "builtin" provider the manager's docstrings already describe); store v2 with topic files reusing the existing file-lock/drift-detection/atomic-write machinery; `memory_entries` + FTS5 tables via the existing version-gated migration chain; hybrid recall = BM25 ∪ vector with RRF; files remain source of truth, SQLite is a rebuildable index — preserving the fork's drift-guard, backup, and journey-graph stories.

**Integration points.** `tools/memory_tool.py`, `agent/memory_manager.py`, `agent/memory_provider.py`, `agent/agent_init.py`, `agent/conversation_loop.py:796-812` (recall injection channel), `agent/system_prompt.py`, `hermes_state.py`.

**Effort / risks.** XL full scope; Phase 1 (builtin provider + searchable archive + prefetch) is an L that delivers most user-visible value. Top risk is prefix-cache regression — recall must use the user-message channel exclusively; injected archived memories are attacker-persistable and must pass threat scanning.

### 7. Provider-Native Context Management — server-side editing/compaction behind the ContextEngine — 6.88

**What.** A provider-capability layer that offloads context eviction to the API where available: Anthropic `clear_tool_uses`/`clear_thinking`/`compact_20260112` (with `pause_after_compaction` hooked to memory flush) and OpenAI `/responses/compact`, falling back to the existing local `ContextCompressor` elsewhere. Effectively unbounded sessions on frontier providers with zero aux-model summarization cost — and eviction that happens *after* cache lookup, so prompt caches stay warm.

**Why now.** Anthropic reports 84% token savings and +39% performance on 100-turn agentic benchmarks; local compaction today costs an aux-model call, a visible pause, a DB lock + lease-refresher thread, and — critically — a cold prompt cache. Upstream issue #526 asks for exactly this. The repo already has a full provider-native compaction precedent for one runtime (the Codex app-server `compact_thread()` path with a `native|hermes|off` config knob) to pattern-match.

**Design in brief.** Phase 1: stateless request decoration — a `provider_native_context_policy()` mirroring `anthropic_prompt_cache_policy`'s provider table (must exclude Anthropic-compatible gateways exactly like `_CONTEXT_1M_BETA` does, since unknown params 400); `build_api_kwargs` computes the `context_management` dict from the compressor's existing thresholds; post-edit `input_tokens` flow back through the existing usage path so `should_compress()` naturally stops firing. Phase 2: compaction-block capture/persistence and `/responses/compact` opaque-item replay, keeping the local transcript canonical.

**Integration points.** `agent/agent_runtime_helpers.py`, `agent/chat_completion_helpers.py:build_api_kwargs`, `agent/transports/anthropic.py`, `agent/anthropic_adapter.py`, `agent/conversation_compression.py`, `agent/context_engine.py`.

**Effort / risks.** L (Phase 1 is an M). Main risk is gateway blast radius — this harness routes `anthropic_messages` to many non-Anthropic endpoints; gating must be conservative, and double-eviction (server edits + local estimator both firing) needs the estimator suppressed on capable providers.

### 8. MCP Capability Acquisition with a Supply-Chain Trust Lifecycle — 6.88

**What.** Let Hermes grow its own toolset on demand while treating every acquired capability as a supply-chain artifact: query the official MCP Registry (~9.6k servers) + skills.sh/SkillsMP, present vetted candidates, and — after chat-button approval — install with version pinning, hash verification, namespace-auth trust tiers, and the existing OSV/injection scanning pipeline. Plus: ingest MCP `readOnlyHint`/`destructiveHint` annotations (trust-gated), snapshot tool descriptions to detect drift (rug-pulls), and pin servers in a lockfile.

**Why now.** Hermes markets a closed learning loop, but capability acquisition is the one arc still human-only: a 3-entry PR-gated catalog vs a 9.6k-server registry. Meanwhile MCP supply-chain attacks went from theoretical to empirical (malicious Postmark server, registry poisoning), the NSA published MCP security guidance, and the registry shipped namespace authentication — the trust primitives now exist. Every security primitive the flow needs (approval gate, OSV preflight, skills-guard trust tiers, hub lockfile) already exists in the repo; the skills half is mostly built.

**Design in brief.** `hermes_cli/mcp_registry.py` mirroring the `skills_hub.py` adapter pattern, mapping registry entries onto the existing `CatalogEntry` with trust tiers (curated / authenticated-namespace / unverified); a `capability_acquire` tool (search/inspect/install for servers and hub skills) routing installs through `request_tool_approval` → `mcp_catalog.install_entry` → `refresh_agent_mcp_tools` (mid-session tool landing, with `tool_search` deferral absorbing context bloat); `~/.hermes/.mcp/lock.json` modeled on `HubLockFile`; description snapshots hashed per server with drift → re-approval.

**Integration points.** `hermes_cli/mcp_catalog.py`, `hermes_cli/mcp_config.py`, `hermes_cli/mcp_security.py`, `tools/mcp_tool.py`, `tools/skills_guard.py`, `tools/skills_hub.py`, `tools/approval.py`, `tools/osv_check.py`.

**Effort / risks.** XL total but decomposes: annotation ingestion S, drift detection M, lockfile M, registry+acquire tool L, per-server sandbox deferred. Risks: annotations from a malicious server are themselves an attack channel (relaxation must be trust-gated); real npm pinning needs more than `pkg@x.y.z` in argv.

### 9. OS-Sandboxed Local Execution + Credential-Brokering Egress Proxy — 6.63

**What.** Structurally jail the default local execution path without Docker: a new `TERMINAL_ENV=local-sandboxed` backend using bubblewrap + Landlock + seccomp on Linux (all unprivileged-capable on a bare VPS) and Seatbelt on macOS — writes confined to workspace + declared dirs, network namespace removed, all egress forced through a host-side proxy over a Unix socket enforcing a per-session domain allowlist with approval-button prompts on new domains. The proxy doubles as a credential broker: secrets are injected into outbound requests host-side and *never enter the sandbox*.

**Why now.** Anthropic open-sourced sandbox-runtime (exactly this architecture, Apache-2.0); Codex CLI ships the Landlock/Seatbelt trio; credential-broker proxies (Infisical Agent Vault, Pipelock) codified "secrets never in agent context." Hermes' own SECURITY.md declares the OS the only real boundary and every in-process guard a heuristic — then ships a default backend with no OS boundary. Three hand-maintained env blocklists are the current defense.

**Design in brief.** `tools/environments/local_sandboxed.py` subclassing `LocalEnvironment` — only `_run_bash` changes (wrap argv in a platform jail prefix; the session snapshot/cwd files must stay writable inside the jail); a strictness ladder (bwrap → landlock-only → refuse/fallback) with honest degradation errors; host-side CONNECT proxy unifying the existing `website_policy` blocklist + `url_safety` private-IP rules (enforced at connect time, killing DNS rebinding) + `allowed_domains` + session grants, unknown domains routed through the existing gateway approval buttons; same wrapper reusable to jail individual MCP servers (composing with #8).

**Integration points.** `tools/environments/{base,local}.py`, `tools/terminal_tool.py:_create_environment`, `hermes_cli/config.py`, `tools/code_execution_tool.py:_scrub_child_env`, `tools/mcp_tool.py:_build_safe_env`, `tools/approval.py`.

**Effort / risks.** XL full scope; phaseable (value-based redaction S, sandbox-only L, raw-tunnel proxy +M, TLS-terminating broker +L). Risks: bwrap availability varies (Ubuntu 24.04 AppArmor restricts unprivileged userns; nested-in-Docker can't bwrap) — needs a detection ladder; proxy env vars don't cover raw TCP/ssh/DNS, which fail confusingly under `unshare-net`.

### 10. Sleep-Time Memory Consolidation ("Dreaming") — 6.50

**What.** A memory counterpart to the skill Curator: during detected idle windows, a restricted background agent reviews recent sessions plus `MEMORY.md`/`USER.md`, deduplicates and merges entries, detects contradictions (preferring newer provenance), expires stale facts to the archive, and distills unreviewed session episodes into durable entries — end-of-session extraction the builtin path currently lacks, at near-zero marginal cost on an idle box.

**Why now.** Letta sleep-time compute (~5× test-time token reduction, +13–18% accuracy), Honcho dreaming, and Claude Code's background memory-writer independently converged on idle-time consolidation. Every stale or contradictory entry in Hermes' tiny always-injected files is both a per-turn token tax and a standing behavior bug; today's only relief valves are off-by-default blind rotation or forcing the *main* model into mid-turn consolidation (the path that needed a failure cap).

**Design in brief.** `agent/memory_curator.py` mirroring `agent/curator.py` wholesale: same idle/interval gates and deferred-first-run seeding, same restricted-fork recipe (`quiet_mode`, `skip_memory` for external providers, thread tool whitelist `{memory, session_search}`, `background_review` write-origin), same tar.gz snapshot + rollback via a generalized `curator_backup.py`, same per-run reports, tick hooks beside the existing `maybe_run_curator` call sites in `cli.py` and `gateway/run.py`, aux-model slot `auxiliary.memory_curator`. Consolidation defaults OFF (archive-never-delete until provenance data accumulates).

**Integration points.** `agent/curator.py` (pattern), `agent/curator_backup.py`, `agent/background_review.py`, `tools/memory_tool.py`, `tools/write_approval.py`, `cli.py`, `gateway/run.py`.

**Effort / risks.** L — heavy pattern reuse; curator-grade safety/report/rollback parity is most of the work. Top risk: autonomous mutation of user-curated memory — mitigated by snapshot/rollback, dry-run seeding, staged writes, and archive-never-delete. Compounds with #6 (provenance metadata, readable archive) and #2 (outcome signals for what to distill).

---

## Near-misses (ranked 11–13, fully grounded in Appendix A)

- **11. Cost & wall-clock budget enforcement (6.50 — tied with #10, lost the tiebreak).** Extend `IterationBudget` into a multi-dimensional `TurnBudget` (USD/tokens/deadline; per-turn, per-day, per-delegation-tree) charging at the loop's existing accounting block, degrading gracefully (drop reasoning effort → cheaper fallback → summarize) before hard-stopping. The review found cost measured everywhere and enforced nowhere; if your usage patterns include delegation fan-out, consider swapping this into the ten.
- **12. Verified Skill Edits (6.38).** A separate verifier subagent replays evidence before autonomous skill writes commit — the direct answer to SkillsBench's "self-generated skills don't help without verification." All staging/pending/replay scaffolding already exists in `tools/write_approval.py`.
- **13. Batch Trajectory-to-Skill Distillation (6.25).** Periodic offline pass distilling *batches* of stored transcripts (grouped via FTS5 + the #2 ledger) into patches on existing skill umbrellas — the Trace2Skill result. Depends on #2.

The remaining nine candidates (agent-team mailboxes, tamper-evident security ledger, taint-tracked context quarantine, OTel GenAI exporter, realtime voice, computer-use escalation, A2A interop, RL-from-experience) are listed with scores in Appendix A.

## Suggested sequencing

Dependencies suggest three tracks that can proceed in parallel:

1. **Instrumentation track (fastest wins):** #2 Turn-Outcome Ledger → #10 Dreaming → (near-miss #13 Distillation, #12 Verified Skill Edits). The ledger is small, unblocks the whole learning-loop agenda, and its schema is needed by three other items.
2. **Policy/execution track:** #4 Policy Engine (consumes the fork's existing metadata investment; unlocks safe versions of everything else) → #1 Code-Mode Orchestration (its approval/batching story depends on #4) → #3 W1 idempotency journal → rest of #3 → #9 sandbox.
3. **Context/capability track:** #7 Provider-Native Context (Phase 1 is small and immediately felt) → #6 Paged Memory Phase 1 → #5 Event Automations → #8 MCP Acquisition.

A reasonable "first 90 days": #2, #4, #7-Phase-1, #3-W1 — four M-or-smaller slices, each independently shippable, that together convert the fork's existing plumbing investments (turn outcomes, operation metadata, operation keys, cache policy tables) into user-visible capability.
