# Sovereign Personal Knowledge & Evidence Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a profile-local, user-governed temporal knowledge provider that keeps immutable source evidence separate from revisable claims, explains every claim, and cascades correction or erasure through every Hermes-controlled derivative.

**Architecture:** Ship the graph and all product-specific state as the active `knowledge` memory-provider plugin (Footprint Ladder rung 4). Widen the existing `MemoryProvider`/`MemoryManager` edge only with a generic, non-model-visible management API so the CLI, Ink TUI, and secondary Dashboard inspector can call the active provider without importing its concrete implementation. Store retained evidence, temporal claim assertions, source edges, entities, contradictions, derived artifacts, and lineage in a profile-local SQLite database; use an idempotent purge journal to remove raw copies, summaries, embeddings, graph indexes, caches, and managed exports while preserving content-free deletion audit rows.

**Tech Stack:** Python 3, stdlib `sqlite3`/FTS5, local `sentence-transformers` embeddings installed with the plugin, existing Hermes memory-provider/plugin discovery, `SessionDB`, argparse/Rich, TUI JSON-RPC, React/TypeScript/Ink/nanostores, React Dashboard sidecar, pytest through `scripts/run_tests.sh`, Vitest, and the existing temporary-`HERMES_HOME` test harness.

## Global Constraints

- The layman outcome is: Hermes maintains a user-controlled history of people, projects, preferences, facts, sources, changes, and contradictions that the user can inspect, correct, export, or erase.
- Immutable evidence is never edited in place. Corrections append higher-authority claim assertions and retain the original evidence until the user explicitly erases it.
- Every displayed claim labels its authority as exactly one of `source_says`, `hermes_inferred`, or `user_confirmed`; provenance never implies correctness.
- Claims include entity links, supporting or contradicting source edges, valid-time interval, recorded-time interval, confidence, freshness, visibility/consent scope, and supersession/contradiction links.
- Only explicit CLI or Ink confirmation may create `user_confirmed` authority. Extraction, retrieval, and the model may create only `source_says` or `hermes_inferred` assertions.
- Erasure is lineage-aware and covers all Hermes-controlled raw evidence, claim rows, source/graph edges, summaries, embeddings, FTS indexes, caches, managed exports, and staged import files. Content-free tombstones may retain opaque IDs, counts, timestamps, and operation status, but no deleted text, locator, hash, embedding, entity label, or canary.
- The original user-owned export passed to `ingest` and user-copied unmanaged exports are outside Hermes control. Every preview and completion report must say so; the zero-canary gate covers only state controlled by Hermes.
- Session-origin erasure must either leave the source session intact and report `residual_origin`, or, after explicit `--delete-source-session` authority and an active-session check, use existing `SessionDB.delete_session()` to remove that entire source session. It must never rewrite or splice live conversation history.
- The proof ingests exactly two authorized offline exports—Google Takeout Contacts (`.vcf`) and a Slack standard export (`.zip`)—plus explicitly selected Hermes sessions. It does not mine private history implicitly and makes no connector/network request.
- The frozen evaluation corpus contains at least 100 ground-truthed temporal questions/claims with expected valid intervals and evidence IDs. Freeze corpus version, strata, scorer definitions, current-Hermes baseline, sample size, hardware/network class, cost source, exclusions, and thresholds before evaluation.
- Pass only with at least `+10.0` percentage points evidence-backed answer accuracy versus current Hermes retrieval, evidence precision `>= 0.90`, evidence recall `>= 0.80`, stale-conflict answer rate no higher than baseline, explicit origin on `100%` of displayed claims, and zero recovered deletion canaries from every Hermes-controlled layer.
- Report denominators and Wilson 95% confidence intervals for rates. Excluded or aborted questions remain listed with reasons and are not silently removed from denominators.
- Stop rollout on any recovered canary, unlabeled displayed claim, user-confirmed assertion created without explicit user action, cross-profile read, stale-conflict regression, prompt/tool-schema drift within a conversation, role-alternation violation, or evidence persisted after an ingest transaction reports failure.
- CLI and terminal/Ink TUI are the primary authoring and governance surfaces. Dashboard is a secondary read-only visualization/status surface in this slice. Desktop has no dependency or parity requirement.
- Add no model-visible core tool. `KnowledgeMemoryProvider.get_tool_schemas()` returns `[]`; recall enters only through the existing provider prefetch path and user controls enter through CLI/RPC.
- The system prompt, cached prefix, effective tool-definition snapshot, provider, and model remain byte-stable for a conversation. Enabling the provider, changing embedding identity, or changing published recall configuration takes effect only in a new conversation.
- Preserve strict role alternation and compression-only history mutation. Do not inject a synthetic user message mid-loop.
- Stable non-secret settings live under `memory.knowledge` in `config.yaml`. No credential is required; do not add an environment variable. Runtime authority, evidence, and audit state live in profile-local SQLite/files under `get_hermes_home()`.
- Profiles remain independent islands. Resolve every database, raw-copy, cache, managed-export, and corpus path from the active `HERMES_HOME`; never inherit live state from the default profile.
- Use `scripts/run_tests.sh` for every Python RED/GREEN command. Use real imports and a temporary `HERMES_HOME`; mock only external model execution and OS process boundaries.
- Truthful status vocabulary is `verified`, `completed_unverified`, `unknown_effect`, `blocked`, and `failed`. A cascade is `verified` only after a full controlled-layer canary scan; interrupted or unscannable deletion is never presented as complete.

---

## Approved Portfolio Contract

**Layman outcome:** Hermes maintains a user-controlled history of people, projects, preferences, facts, sources, changes, and contradictions that the user can inspect, correct, export, or erase.

**90-day proof:** Ingest two authorized cross-platform exports plus explicitly selected Hermes session memory, seed temporal changes, contradictions, and deletion canaries, and freeze at least 100 ground-truthed temporal questions/claims with validity intervals and expected evidence. Pass only with at least a 10-percentage-point improvement in evidence-backed answer accuracy over current retrieval, at least 90% evidence precision, at least 80% evidence recall, no stale-conflict regression, explicit origin for every displayed claim, and zero recovered canaries across every Hermes-controlled raw and derived layer.

**Dependencies and failure conditions:** The temporal graph ships as a memory-provider plugin with only generic management-contract widening in core. Provenance never makes extraction correct: the product must distinguish `source_says`, `hermes_inferred`, and `user_confirmed`, preserve contradictions, and stop rollout on any incomplete lineage purge or hidden disagreement.

**Delivery:** Footprint Ladder rung 4 for the knowledge provider plus minimal rung-1 generic provider-management extensions. CLI and Ink TUI are the primary governance surfaces; Dashboard is secondary and read-only in this slice; Desktop has no dependency.

---

## Current-Code Audit and File Map

### Existing surfaces to extend

- `agent/memory_provider.py:43-315` — `MemoryProvider` lifecycle and optional hooks. Add the generic management capability/action contract here; do not add knowledge-specific types.
- `agent/memory_manager.py:353-1138` — provider fan-out, one-external-provider enforcement, tool-schema aggregation, prefetch, write bridge, and shutdown. Add active-provider management dispatch that is never included in `get_all_tool_schemas()`.
- `plugins/memory/__init__.py:64-458` — bundled/user provider discovery and active memory-plugin CLI discovery. Reuse it unchanged except for behavior tests proving `knowledge` is found and its CLI loads without importing embedding dependencies.
- `agent/agent_init.py:1439-1488` and `agent/turn_context.py:629-652` — existing provider initialization and fenced prefetch injection. Reuse these paths; the implementation must not rebuild the system prompt or tool schema after initialization.
- `hermes_state.py:711-1018, 4342-4528, 5410-5730, 5971-6459` — durable sessions/messages/FTS, `search_messages()`, imports/exports, and `delete_session()`. The plugin reads selected sessions through public methods and uses `delete_session()` only after explicit whole-session erasure authority; no knowledge tables go into `state.db`.
- `agent/turn_ledger.py:28-319` — turn outcome provenance. Read it only to attach a selected session/turn outcome to evidence metadata; do not redefine task receipts as knowledge evidence.
- `plugins/memory/holographic/store.py:17-619` and `plugins/memory/holographic/retrieval.py` — useful local SQLite/FTS/entity/vector precedent, but its mutable facts and heuristic contradiction score do not meet this design. Do not retrofit or import its store.
- `plugins/web/tavily/provider.py:64-124`, `plugins/web/exa/provider.py:175-189`, and `plugins/web/parallel/provider.py:243-270` — current normalized URL/title metadata. The knowledge plugin accepts such metadata when a selected session contains it, without changing web providers.
- `hermes_cli/subcommands/memory.py:12-61`, `hermes_cli/memory_setup.py:159-500`, and `hermes_cli/main.py:12768-12831` — external-provider setup/status/off/reset. Keep built-in reset semantics; the knowledge plugin gets its own active-plugin command `hermes knowledge` through the existing discovery path.
- `hermes_cli/session_export.py:21-314` — stable export-rendering precedent. Knowledge export has a separate evidence/claim schema and must not be bolted onto session export.
- `tui_gateway/server.py:14309-14363`, `ui-tui/src/components/journey.tsx`, `ui-tui/src/app/overlayStore.ts`, and `ui-tui/src/components/appLayout.tsx` — existing RPC-backed Ink timeline/inspect/edit/delete overlay pattern. Add a separate knowledge overlay; do not make Journey the knowledge source of truth.
- `hermes_cli/web_server.py:2998-3013` and `web/src/components/ChatSidebar.tsx` — secondary Dashboard sidecar pattern. Add read-only knowledge summary/timeline APIs and a collapsible inspector; never replace the embedded TUI transcript/composer.
- `tests/agent/test_memory_provider.py`, `tests/plugins/memory/test_holographic_store.py`, `tests/hermes_cli/test_plugin_cli_registration.py`, `tests/test_hermes_state.py`, `tests/tui_gateway/`, `ui-tui/src/__tests__/journeyCommand.test.ts`, and `web/src/lib/session-import.test.ts` — nearest behavior-test patterns.

### New knowledge-provider files

- `plugins/memory/knowledge/plugin.yaml` — bundled provider metadata and pinned optional install dependency.
- `plugins/memory/knowledge/__init__.py` — lightweight provider registration only.
- `plugins/memory/knowledge/models.py` — frozen enums/dataclasses and JSON boundary validation.
- `plugins/memory/knowledge/schema.py` — versioned SQLite DDL/migrations.
- `plugins/memory/knowledge/store.py` — transaction boundary and typed persistence API.
- `plugins/memory/knowledge/lineage.py` — lineage closure, invalidation, purge journal, resume, and verification scan.
- `plugins/memory/knowledge/importers.py` — Google VCF, Slack ZIP, and selected-SessionDB adapters.
- `plugins/memory/knowledge/extraction.py` — deterministic candidate parser plus injected extractor interface; cannot promote authority.
- `plugins/memory/knowledge/embeddings.py` — local embedding backend, identity, cache, rebuild, and deletion.
- `plugins/memory/knowledge/retrieval.py` — bitemporal/filter-aware hybrid FTS/vector/entity retrieval and contradiction-safe answer selection.
- `plugins/memory/knowledge/provider.py` — `MemoryProvider` adapter, bounded prefetch, management actions, lifecycle, and cache identity.
- `plugins/memory/knowledge/cli.py` — `hermes knowledge` parser/handlers for ingest, timeline, query, inspect, confirm, correct, export, erase, purge status/resume, and benchmark.
- `plugins/memory/knowledge/render.py` — shared plain-data render model for CLI, RPC, and Dashboard.
- `plugins/memory/knowledge/benchmark.py` — frozen-corpus loader, current-Hermes baseline, evaluator, Wilson intervals, and gate report.
- `plugins/memory/knowledge/README.md` — setup, governance semantics, schemas, privacy boundary, recovery, rollback, and evaluation runbook.

### New tests and proof assets

- `tests/agent/test_memory_management_contract.py`
- `tests/plugins/memory/knowledge/conftest.py`
- `tests/plugins/memory/knowledge/test_models_store.py`
- `tests/plugins/memory/knowledge/test_importers.py`
- `tests/plugins/memory/knowledge/test_claim_workflow.py`
- `tests/plugins/memory/knowledge/test_retrieval.py`
- `tests/plugins/memory/knowledge/test_lineage_purge.py`
- `tests/plugins/memory/knowledge/test_provider.py`
- `tests/plugins/memory/knowledge/test_cli.py`
- `tests/plugins/memory/knowledge/test_e2e.py`
- `tests/plugins/memory/knowledge/test_benchmark.py`
- `tests/plugins/memory/knowledge/fixtures/google_contacts.vcf`
- `tests/plugins/memory/knowledge/fixtures/slack_export.zip`
- `tests/plugins/memory/knowledge/fixtures/corpus-v1.jsonl`
- `tests/tui_gateway/test_knowledge_rpc.py`
- `ui-tui/src/components/knowledgeTimeline.tsx`
- `ui-tui/src/__tests__/knowledgeTimeline.test.tsx`
- `ui-tui/src/__tests__/knowledgeCommand.test.ts`
- `web/src/components/KnowledgeTimelineCard.tsx`
- `web/src/lib/knowledge.ts`
- `web/src/lib/knowledge.test.ts`
- `tests/hermes_cli/test_web_server_knowledge.py`
- `docs/features/knowledge-timeline.md`
- `docs/security/knowledge-erasure-threat-model.md`
- `scripts/knowledge_benchmark.py`

## Public Contracts and Invariants

The following names are fixed for all tasks in this plan.

```python
# agent/memory_provider.py — generic core edge, not model-visible
from dataclasses import dataclass, field
from typing import Any, Mapping

@dataclass(frozen=True)
class MemoryManagementResponse:
    ok: bool
    status: str
    data: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""

class MemoryProvider(ABC):
    def get_management_capabilities(self) -> frozenset[str]:
        return frozenset()

    def handle_management_action(
        self, action: str, args: Mapping[str, Any]
    ) -> MemoryManagementResponse:
        return MemoryManagementResponse(
            ok=False,
            status="unsupported",
            error=f"Provider {self.name} does not support management action {action}",
        )

# agent/memory_manager.py
class MemoryManager:
    def get_management_capabilities(self) -> frozenset[str]:
        return frozenset().union(*(
            provider.get_management_capabilities()
            for provider in self._providers
            if provider.name != "builtin"
        ))
    def handle_management_action(
        self, action: str, args: Mapping[str, Any]
    ) -> MemoryManagementResponse:
        matches = [
            provider for provider in self._providers
            if provider.name != "builtin"
            and action in provider.get_management_capabilities()
        ]
        if len(matches) != 1:
            return MemoryManagementResponse(
                ok=False,
                status="unsupported",
                error=f"No unique provider for management action {action}",
            )
        return matches[0].handle_management_action(action, dict(args))
```

```python
# plugins/memory/knowledge/models.py
class ClaimAuthority(StrEnum):
    SOURCE_SAYS = "source_says"
    HERMES_INFERRED = "hermes_inferred"
    USER_CONFIRMED = "user_confirmed"

class EdgeRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"

class ArtifactKind(StrEnum):
    RAW_COPY = "raw_copy"
    SUMMARY = "summary"
    EMBEDDING = "embedding"
    GRAPH_EDGE = "graph_edge"
    CACHE = "cache"
    MANAGED_EXPORT = "managed_export"

@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    source_id: str
    source_kind: str
    source_locator: str
    captured_at: float
    content_sha256: str
    media_type: str
    consent_scope: str
    visibility: str

@dataclass(frozen=True)
class ClaimAssertion:
    assertion_id: str
    claim_id: str
    subject_entity_id: str
    predicate: str
    object_json: str
    valid_from: float | None
    valid_to: float | None
    recorded_from: float
    recorded_to: float | None
    confidence: float
    freshness_at: float
    authority: ClaimAuthority
    supersedes_assertion_id: str | None

@dataclass(frozen=True)
class TemporalQuery:
    text: str
    at: float | None = None
    as_of: float | None = None
    entity_ids: tuple[str, ...] = ()
    include_conflicts: bool = True
    min_confidence: float = 0.0
    limit: int = 10

@dataclass(frozen=True)
class PurgeRequest:
    root_kind: str
    root_id: str
    delete_source_session: bool
    requested_by: str
    confirmation_token: str
```

Authority ordering is `user_confirmed > source_says > hermes_inferred`; it is a selection preference, not permission to hide disagreement. Valid-time filtering occurs before authority ordering. A contradiction remains visible whenever two retained assertions for the same subject/predicate have overlapping valid intervals and unequal canonical objects. `recorded_from/recorded_to` provides audit history; `valid_from/valid_to` describes the world.

---

### Task 1: Add the Generic, Non-Model-Visible Memory Management Edge

**Files:**
- Modify: `agent/memory_provider.py:36-315`
- Modify: `agent/memory_manager.py:353-760`
- Create: `tests/agent/test_memory_management_contract.py`
- Modify: `tests/agent/test_memory_provider.py`

**Interfaces:**
- Consumes: existing `MemoryProvider.name`, one-external-provider enforcement, and `MemoryManager._providers`.
- Produces: `MemoryManagementResponse`, `MemoryProvider.get_management_capabilities()`, `MemoryProvider.handle_management_action()`, `MemoryManager.get_management_capabilities()`, and `MemoryManager.handle_management_action()` exactly as declared above.

- [ ] **Step 1: Write the failing provider/manager contract tests**

```python
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryManagementResponse, MemoryProvider

class GovernedProvider(MemoryProvider):
    name = "governed"
    def is_available(self): return True
    def initialize(self, session_id, **kwargs): pass
    def get_tool_schemas(self): return []
    def get_management_capabilities(self):
        return frozenset({"inspect", "erase"})
    def handle_management_action(self, action, args):
        return MemoryManagementResponse(True, "verified", {"action": action, **args})

def test_management_actions_dispatch_only_to_capable_external_provider():
    manager = MemoryManager()
    manager.add_provider(GovernedProvider())
    assert manager.get_management_capabilities() == frozenset({"inspect", "erase"})
    result = manager.handle_management_action("inspect", {"id": "c-1"})
    assert result == MemoryManagementResponse(True, "verified", {"action": "inspect", "id": "c-1"})

def test_management_contract_never_enters_model_tool_schemas():
    manager = MemoryManager()
    manager.add_provider(GovernedProvider())
    assert manager.get_all_tool_schemas() == []

def test_unsupported_management_action_fails_closed():
    manager = MemoryManager()
    manager.add_provider(GovernedProvider())
    result = manager.handle_management_action("export", {})
    assert result.ok is False
    assert result.status == "unsupported"
```

- [ ] **Step 2: Run the focused RED test**

Run: `scripts/run_tests.sh tests/agent/test_memory_management_contract.py -v`

Expected: FAIL during collection because `MemoryManagementResponse` and the management methods do not exist.

- [ ] **Step 3: Implement the minimal generic contract and fail-closed dispatch**

Add the dataclass and default methods shown in **Public Contracts**. In `MemoryManager`, select only non-`builtin` providers advertising the requested action, reject zero or multiple matches, copy incoming mappings before dispatch, validate the return type, and convert provider exceptions into `MemoryManagementResponse(ok=False, status="failed", error=str(exc))`. Do not touch `get_all_tool_schemas()`.

```python
def handle_management_action(self, action, args):
    matches = [
        p for p in self._providers
        if p.name != "builtin" and action in p.get_management_capabilities()
    ]
    if len(matches) != 1:
        return MemoryManagementResponse(False, "unsupported", error=f"No unique provider for {action}")
    try:
        response = matches[0].handle_management_action(action, dict(args))
    except Exception as exc:
        return MemoryManagementResponse(False, "failed", error=str(exc))
    if not isinstance(response, MemoryManagementResponse):
        return MemoryManagementResponse(False, "failed", error="Invalid provider response")
    return response
```

- [ ] **Step 4: Run the GREEN contract tests and existing provider suite**

Run: `scripts/run_tests.sh tests/agent/test_memory_management_contract.py tests/agent/test_memory_provider.py -v`

Expected: PASS; existing provider discovery, lifecycle, write bridge, and tool injection tests remain green.

- [ ] **Step 5: Commit**

```bash
git add agent/memory_provider.py agent/memory_manager.py tests/agent/test_memory_management_contract.py tests/agent/test_memory_provider.py
git commit -m "feat(memory): add provider management contract"
```

### Task 2: Create the Immutable Evidence and Bitemporal Claim Store

**Files:**
- Create: `plugins/memory/knowledge/plugin.yaml`
- Create: `plugins/memory/knowledge/__init__.py`
- Create: `plugins/memory/knowledge/models.py`
- Create: `plugins/memory/knowledge/schema.py`
- Create: `plugins/memory/knowledge/store.py`
- Create: `tests/plugins/memory/knowledge/conftest.py`
- Create: `tests/plugins/memory/knowledge/test_models_store.py`

**Interfaces:**
- Consumes: `get_hermes_home()` and stdlib SQLite WAL/FTS5.
- Produces: `KnowledgeStore`, `EvidenceRecord`, `ClaimAssertion`, `TemporalQuery`, `PurgeRequest`, `insert_evidence()`, `append_assertion()`, `link_assertion_evidence()`, `upsert_entity()`, `add_contradiction()`, and `query_assertions()`.

- [ ] **Step 1: Write failing schema and invariants tests**

```python
def test_evidence_is_content_addressed_and_cannot_be_updated(store):
    first = store.insert_evidence(source_id="src-1", source_kind="slack", source_locator="c/1",
        captured_at=10.0, content=b"alpha", media_type="text/plain",
        consent_scope="import:src-1", visibility="private")
    second = store.insert_evidence(source_id="src-1", source_kind="slack", source_locator="c/1",
        captured_at=10.0, content=b"alpha", media_type="text/plain",
        consent_scope="import:src-1", visibility="private")
    assert first == second
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("UPDATE evidence_records SET source_locator='changed' WHERE evidence_id=?", (first.evidence_id,))

def test_assertion_preserves_valid_and_recorded_time(store):
    assertion = store.append_assertion(claim_id="claim-role", subject_entity_id="person-1",
        predicate="works_on", object_value={"entity_id": "project-a"}, valid_from=100.0,
        valid_to=200.0, confidence=0.8, freshness_at=150.0,
        authority=ClaimAuthority.SOURCE_SAYS, supersedes_assertion_id=None)
    assert assertion.valid_from == 100.0
    assert assertion.valid_to == 200.0
    assert assertion.recorded_to is None

def test_claim_requires_at_least_one_source_edge_unless_user_confirmed(store):
    with pytest.raises(ValueError, match="source edge"):
        store.finalize_assertion("assertion-without-edge")
```

- [ ] **Step 2: Run the focused RED test**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_models_store.py -v`

Expected: FAIL because the `plugins.memory.knowledge` package and `KnowledgeStore` do not exist.

- [ ] **Step 3: Implement models, schema, migration guard, and transaction API**

Create `knowledge.db` with foreign keys enabled, WAL, `busy_timeout`, schema version `1`, and these tables: `sources`, `evidence_records`, `evidence_payloads`, `entities`, `entity_aliases`, `claims`, `claim_assertions`, `assertion_evidence`, `contradictions`, `derived_artifacts`, `lineage_edges`, `retrieval_cache`, `managed_exports`, `purge_operations`, `purge_items`, and external-content FTS5 tables for evidence and canonical claim text. Add triggers that abort `UPDATE` on `evidence_records`/`evidence_payloads`, maintain FTS rows, and enforce confidence `[0,1]` plus non-inverted validity intervals.

Use canonical JSON (`sort_keys=True`, separators `(',', ':')`) and UUIDv7-compatible sortable IDs generated by a local helper. Put payload bytes only in `evidence_payloads`; metadata rows never duplicate content. `finalize_assertion()` checks either at least one edge or `USER_CONFIRMED` authority within the same transaction.

- [ ] **Step 4: Run the GREEN store tests and SQLite integrity check**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_models_store.py -v`

Expected: PASS, including `PRAGMA foreign_key_check`, duplicate idempotency, immutability triggers, interval validation, and profile-local path assertions.

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/knowledge tests/plugins/memory/knowledge/conftest.py tests/plugins/memory/knowledge/test_models_store.py
git commit -m "feat(knowledge): add evidence and temporal claim store"
```

### Task 3: Ingest Two Authorized Exports and Explicitly Selected Hermes Sessions

**Files:**
- Create: `plugins/memory/knowledge/importers.py`
- Modify: `plugins/memory/knowledge/store.py`
- Create: `tests/plugins/memory/knowledge/test_importers.py`
- Create: `tests/plugins/memory/knowledge/fixtures/google_contacts.vcf`
- Create: `tests/plugins/memory/knowledge/fixtures/slack_export.zip`

**Interfaces:**
- Consumes: `KnowledgeStore.insert_evidence()`, `SessionDB.export_session()`, and source records containing `consent_scope`, `ownership`, and `import_state`.
- Produces: `GoogleContactsImporter`, `SlackExportImporter`, `HermesSessionImporter`, `ImportPlan`, `ImportResult`, `plan_import()`, and `execute_import()`.

- [ ] **Step 1: Write failing authorization, parsing, dedupe, and rollback tests**

```python
def test_import_requires_matching_confirmation_token(store, fixtures):
    plan = plan_import(store, kind="google_contacts", path=fixtures / "google_contacts.vcf")
    with pytest.raises(PermissionError, match="confirmation token"):
        execute_import(store, plan, confirmation_token="wrong")

def test_three_importers_preserve_origin_and_are_idempotent(store, session_db, fixtures):
    session_db.create_session("s-1", source="cli")
    session_db.append_message("s-1", "user", "CANARY_SESSION works on Phoenix")
    results = [
        run_confirmed_import(store, "google_contacts", fixtures / "google_contacts.vcf"),
        run_confirmed_import(store, "slack", fixtures / "slack_export.zip"),
        run_confirmed_session_import(store, session_db, ["s-1"]),
    ]
    assert [r.source_kind for r in results] == ["google_contacts", "slack", "hermes_session"]
    assert all(r.inserted_evidence > 0 for r in results)
    assert run_confirmed_session_import(store, session_db, ["s-1"]).inserted_evidence == 0

def test_failed_archive_member_rolls_back_source_and_staging_files(store, corrupt_slack_zip):
    plan = plan_import(store, kind="slack", path=corrupt_slack_zip)
    with pytest.raises(ImportError):
        execute_import(store, plan, plan.confirmation_token)
    assert store.count("evidence_records") == 0
    assert not any(store.staging_dir.iterdir())
```

- [ ] **Step 2: Run the focused RED test**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_importers.py -v`

Expected: FAIL because importer classes and `plan_import()` are undefined.

- [ ] **Step 3: Implement bounded offline adapters and consent manifests**

`plan_import()` performs no write and returns counts, byte size, source fingerprint, managed-copy destination, external-original warning, selected session IDs, and a random one-use confirmation token. `execute_import()` streams with limits: maximum archive `2 GiB`, member `64 MiB`, expanded ratio `20x`, path containment, UTF-8 replacement accounting, no symlinks, and no network URLs.

Map Google `FN`, `N`, `EMAIL`, `TEL`, `ORG`, and `NOTE` fields into separate immutable evidence records with VCF property locators. Map Slack `channels.json`, `users.json`, and `channel/*.json` messages with channel/timestamp locators; preserve thread timestamps and edited timestamps. Import only session IDs named in the plan, using `SessionDB.export_session()` and one evidence record per user/assistant/tool message with `session_id`, message ordinal, role, tool name, and available URL/title metadata. Never import system prompts, reasoning fields, credentials, attachments outside the archive, or all sessions by default.

- [ ] **Step 4: Run the GREEN importer tests**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_importers.py -v`

Expected: PASS for valid sources, duplicate replay, malformed VCF, ZIP slip/bomb rejection, selected-session-only behavior, full transaction rollback, and cleanup of staging state.

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/knowledge/importers.py plugins/memory/knowledge/store.py tests/plugins/memory/knowledge/test_importers.py tests/plugins/memory/knowledge/fixtures
git commit -m "feat(knowledge): ingest authorized exports and sessions"
```

### Task 4: Extract Entities and Revisable Claims Without Self-Promotion

**Files:**
- Create: `plugins/memory/knowledge/extraction.py`
- Modify: `plugins/memory/knowledge/store.py`
- Create: `tests/plugins/memory/knowledge/test_claim_workflow.py`

**Interfaces:**
- Consumes: immutable evidence payloads, source locators, and store assertion APIs.
- Produces: `ClaimCandidate`, `ClaimExtractor` protocol, `DeterministicClaimExtractor`, `apply_candidates()`, `confirm_assertion()`, `correct_assertion()`, and contradiction creation.

- [ ] **Step 1: Write failing authority, correction, and contradiction tests**

```python
def test_extraction_cannot_create_user_confirmed_authority(store, evidence):
    candidate = ClaimCandidate(subject="Ari", predicate="works_on", object_value="Phoenix",
        valid_from=10.0, valid_to=None, confidence=0.8, evidence_ids=(evidence.evidence_id,))
    assertion = apply_candidates(store, [candidate], extraction_method="deterministic:v1")[0]
    assert assertion.authority is ClaimAuthority.SOURCE_SAYS

def test_user_correction_appends_and_does_not_mutate_evidence(store, sourced_assertion):
    before = store.get_evidence(sourced_assertion.evidence_ids[0])
    corrected = correct_assertion(store, sourced_assertion.assertion_id,
        object_value="Atlas", valid_from=20.0, valid_to=None,
        actor="cli:user", confirmation_token=store.issue_confirmation("correct", sourced_assertion.assertion_id))
    after = store.get_evidence(sourced_assertion.evidence_ids[0])
    assert corrected.authority is ClaimAuthority.USER_CONFIRMED
    assert corrected.supersedes_assertion_id == sourced_assertion.assertion_id
    assert before == after

def test_overlapping_unequal_assertions_create_visible_contradiction(store, conflicting_assertions):
    conflict = store.get_contradictions(claim_id=conflicting_assertions[0].claim_id)
    assert len(conflict) == 1
    assert set(conflict[0].assertion_ids) == {a.assertion_id for a in conflicting_assertions}
```

- [ ] **Step 2: Run the focused RED test**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_claim_workflow.py -v`

Expected: FAIL because extraction and correction workflows do not exist.

- [ ] **Step 3: Implement deterministic extraction and explicit user assertion workflows**

Extract contact identity/organization and Slack/session statements only with deterministic, versioned rules in this slice. Each candidate records `extraction_method`, source span/JSON pointer, confidence, freshness, and edge relation. Canonicalize entity aliases case-insensitively without merging two entities solely on display name; require an explicit merge confirmation for ambiguous aliases.

`confirm_assertion()` and `correct_assertion()` consume one-use tokens scoped to action/target/current revision, append a `USER_CONFIRMED` assertion, close the prior assertion's recorded interval, and create supersession plus lineage edges atomically. Recompute contradictions by overlapping validity ranges and canonical unequal objects. Never delete evidence during correction and never silently resolve a conflict by confidence alone.

- [ ] **Step 4: Run the GREEN claim workflow tests**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_claim_workflow.py -v`

Expected: PASS for authority fencing, source spans, ambiguous entities, interval edges, correction replay rejection, retained evidence, and visible contradictions.

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/knowledge/extraction.py plugins/memory/knowledge/store.py tests/plugins/memory/knowledge/test_claim_workflow.py
git commit -m "feat(knowledge): add governed temporal claim workflow"
```

### Task 5: Add Lineage-Tracked Local Embeddings and Temporal Retrieval

**Files:**
- Create: `plugins/memory/knowledge/embeddings.py`
- Create: `plugins/memory/knowledge/retrieval.py`
- Modify: `plugins/memory/knowledge/schema.py`
- Modify: `plugins/memory/knowledge/store.py`
- Modify: `plugins/memory/knowledge/plugin.yaml`
- Create: `tests/plugins/memory/knowledge/test_retrieval.py`

**Interfaces:**
- Consumes: `TemporalQuery`, retained assertions/evidence, entity aliases, contradiction rows, and lineage registration.
- Produces: `EmbeddingBackend`, `SentenceTransformerBackend`, `KnowledgeRetriever.query()`, `KnowledgeHit`, `KnowledgeAnswer`, `embedding_identity()`, and `rebuild_embeddings()`.

- [ ] **Step 1: Write failing valid-time, evidence, conflict, and cache tests**

```python
def test_query_answers_at_time_and_returns_explicit_evidence(retriever, temporal_fixture):
    old = retriever.query(TemporalQuery("Where did Ari work?", at=150.0))
    new = retriever.query(TemporalQuery("Where did Ari work?", at=250.0))
    assert old.hits[0].object_value == "Phoenix"
    assert new.hits[0].object_value == "Atlas"
    assert old.hits[0].evidence[0].source_locator
    assert old.hits[0].authority in {"source_says", "hermes_inferred", "user_confirmed"}

def test_unresolved_overlap_returns_conflict_not_stale_winner(retriever, conflict_fixture):
    answer = retriever.query(TemporalQuery("Ari's editor?", at=300.0))
    assert answer.status == "conflict"
    assert len(answer.hits) == 2

def test_cache_key_includes_store_revision_query_time_and_embedding_identity(retriever):
    first = retriever.query(TemporalQuery("Phoenix", at=100.0))
    retriever.store.append_assertion(
        claim_id="claim-project-status",
        subject_entity_id="project-phoenix",
        predicate="status",
        object_value={"value": "paused"},
        valid_from=120.0,
        valid_to=None,
        confidence=1.0,
        freshness_at=120.0,
        authority=ClaimAuthority.USER_CONFIRMED,
        supersedes_assertion_id=None,
    )
    second = retriever.query(TemporalQuery("Phoenix", at=100.0))
    assert first.cache_revision != second.cache_revision
```

- [ ] **Step 2: Run the focused RED test**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_retrieval.py -v`

Expected: FAIL because the embedding and retrieval modules do not exist.

- [ ] **Step 3: Implement local hybrid retrieval and lineage registration**

Pin `sentence-transformers>=3.4,<4` in `plugin.yaml`; default model is `sentence-transformers/all-MiniLM-L6-v2` with a recorded model name and resolved revision in `config.yaml`. Model inference is local and batched; first download is an explicit setup action and never occurs during import/query. If the model is unavailable, return FTS/entity results with `embedding_status="unavailable"` instead of downloading or calling a remote service.

Candidate generation unions claim FTS, evidence FTS, entity aliases, and cosine top-k. Filter by `valid_from <= at < valid_to` and `recorded_from <= as_of < recorded_to` before ranking. Score relevance, authority, confidence, and freshness separately in the result; do not collapse them into an unexplained trust number. Every embedding row and cache row gets a `derived_artifacts` record plus lineage edges to its assertion/evidence parents. Cache keys include normalized query, temporal filters, store revision, retrieval version, and embedding identity.

- [ ] **Step 4: Run the GREEN retrieval tests**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_retrieval.py -v`

Expected: PASS for historic/current answers, as-recorded queries, stale filtering, conflict surfaces, explicit evidence, local-only behavior, deterministic cache invalidation, and FTS-only degradation.

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/knowledge/embeddings.py plugins/memory/knowledge/retrieval.py plugins/memory/knowledge/schema.py plugins/memory/knowledge/store.py plugins/memory/knowledge/plugin.yaml tests/plugins/memory/knowledge/test_retrieval.py
git commit -m "feat(knowledge): add temporal evidence retrieval"
```

### Task 6: Implement Lineage-Aware Correction Invalidation and Cascading Erasure

**Files:**
- Create: `plugins/memory/knowledge/lineage.py`
- Modify: `plugins/memory/knowledge/store.py`
- Modify: `plugins/memory/knowledge/retrieval.py`
- Create: `tests/plugins/memory/knowledge/test_lineage_purge.py`

**Interfaces:**
- Consumes: `PurgeRequest`, `derived_artifacts`, `lineage_edges`, managed source/export paths, embedding/cache stores, and optional `SessionDB.delete_session()`.
- Produces: `LineageService.preview_purge()`, `start_purge()`, `resume_purge()`, `verify_purge()`, `invalidate_descendants()`, `PurgePreview`, and `PurgeReport`.

- [ ] **Step 1: Write failing closure, crash replay, residual, and canary tests**

```python
def test_correction_invalidates_all_descendants_but_retains_evidence(lineage, derived_graph):
    report = lineage.invalidate_descendants("assertion", derived_graph.assertion_id)
    assert set(report.kinds) == {"summary", "embedding", "graph_edge", "cache", "managed_export"}
    assert lineage.store.get_evidence(derived_graph.evidence_id) is not None

@pytest.mark.parametrize("crash_after", ["planned", "raw_copy", "embedding", "cache", "managed_export"])
def test_purge_resumes_idempotently_after_each_stage(lineage, canary_graph, crash_after):
    op = lineage.start_purge(canary_graph.request, fault_after=crash_after)
    assert op.status == "unknown_effect"
    report = lineage.resume_purge(op.operation_id)
    assert report.status == "verified"
    assert report.recovered_canaries == []

def test_session_origin_requires_explicit_whole_session_authority(lineage, session_canary):
    preview = lineage.preview_purge(PurgeRequest("evidence", session_canary.evidence_id, False, "cli:user", ""))
    assert preview.residual_origins == [f"session:{session_canary.session_id}"]
    assert preview.can_verify_complete is False
```

- [ ] **Step 2: Run the focused RED test**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_lineage_purge.py -v`

Expected: FAIL because `LineageService` is undefined.

- [ ] **Step 3: Implement the purge journal and verified controlled-layer scan**

Compute a cycle-safe recursive closure before mutation and persist every planned item. State sequence is `planned -> deleting -> deleted -> verified`; operation status is `blocked`, `unknown_effect`, `failed`, or `verified`. Delete descendants before parents, fsync file-parent directories after unlink, let SQLite FTS triggers remove index content, rebuild affected vector indexes, and commit one bounded stage at a time so restart can resume.

`verify_purge()` searches SQLite tables/FTS vocabularies, embedding blobs/metadata, cache payloads, managed export files, raw/staging directories, and configured backup snapshots controlled by the provider for both plaintext canaries and their exact byte encodings. It also verifies zero live lineage paths from deleted roots. Content-free `purge_operations`/`purge_items` retain opaque IDs, kinds, counts, stage/status, and timestamps only. If a session origin is selected with `delete_source_session=True`, refuse active sessions and call `SessionDB.delete_session(session_id, sessions_dir=store.sessions_dir)`; never redact individual messages.

- [ ] **Step 4: Run the GREEN purge tests**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_lineage_purge.py -v`

Expected: PASS across correction invalidation, each injected crash point, duplicate resume, permission-denied files, locked database retry, external-original residual reporting, whole-session deletion authority, and zero canary recovery.

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/knowledge/lineage.py plugins/memory/knowledge/store.py plugins/memory/knowledge/retrieval.py tests/plugins/memory/knowledge/test_lineage_purge.py
git commit -m "feat(knowledge): cascade lineage correction and erasure"
```

### Task 7: Wire the Provider Lifecycle Without Adding Model Tools

**Files:**
- Modify: `plugins/memory/knowledge/__init__.py`
- Create: `plugins/memory/knowledge/provider.py`
- Create: `plugins/memory/knowledge/render.py`
- Modify: `hermes_cli/config.py:2225-2248`
- Modify: `hermes_cli/memory_providers.py`
- Create: `tests/plugins/memory/knowledge/test_provider.py`
- Modify: `tests/agent/test_memory_provider.py`

**Interfaces:**
- Consumes: generic management contract, `KnowledgeStore`, import/extraction/retrieval/lineage services, provider lifecycle, and profile-local config.
- Produces: `KnowledgeMemoryProvider` with capabilities `status`, `ingest.plan`, `ingest.execute`, `timeline`, `query`, `inspect`, `confirm`, `correct`, `export`, `erase.preview`, `erase.execute`, `purge.status`, and `purge.resume`.

- [ ] **Step 1: Write failing lifecycle, schema-stability, and prefetch tests**

```python
def test_provider_is_local_profile_scoped_and_has_no_model_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = KnowledgeMemoryProvider({"enabled": True, "prefetch_limit": 5})
    provider.initialize("s-1", hermes_home=str(tmp_path), platform="cli", agent_context="primary")
    assert provider.get_tool_schemas() == []
    assert provider.store.db_path == tmp_path / "knowledge" / "knowledge.db"

def test_prefetch_labels_origin_authority_time_and_conflict(provider, seeded_claims):
    text = provider.prefetch("Where does Ari work?", session_id="s-1")
    assert "source_says" in text
    assert "valid:" in text
    assert "source:" in text
    assert "CONFLICT" in text

def test_management_does_not_change_tool_schema_or_static_prompt(provider):
    before = (provider.system_prompt_block(), provider.get_tool_schemas())
    provider.handle_management_action("query", {"text": "Ari"})
    assert (provider.system_prompt_block(), provider.get_tool_schemas()) == before
```

- [ ] **Step 2: Run the focused RED test**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_provider.py -v`

Expected: FAIL because `KnowledgeMemoryProvider` is undefined.

- [ ] **Step 3: Implement provider registration, bounded prefetch, and config**

`system_prompt_block()` is a constant string based only on provider/retrieval version—not record counts or changing state. `prefetch()` returns at most configured `prefetch_limit` hits and `prefetch_char_limit`, fenced by existing `build_memory_context_block()`, with claim ID, authority, confidence, valid interval, freshness, source locator, and conflict marker. `sync_turn()` does not ingest automatically; explicitly selected sessions are imported by management action. `on_session_end()` is a no-op. `backup_paths()` returns `[]` because all state stays under `HERMES_HOME`.

Add default `memory.knowledge` keys: `enabled: true`, `prefetch_limit: 5`, `prefetch_char_limit: 4000`, `embedding_model`, `embedding_revision`, and `managed_export_retention_days: 30`. Register the provider as `name == "knowledge"`. Do not expose extraction/write tools.

- [ ] **Step 4: Run the GREEN provider/discovery tests**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_provider.py tests/agent/test_memory_provider.py -v`

Expected: PASS; provider discovery is lightweight, prompt/tool outputs remain byte-identical across management calls, and subagent/non-primary contexts cannot import or write.

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/knowledge/__init__.py plugins/memory/knowledge/provider.py plugins/memory/knowledge/render.py hermes_cli/config.py hermes_cli/memory_providers.py tests/plugins/memory/knowledge/test_provider.py tests/agent/test_memory_provider.py
git commit -m "feat(knowledge): register governed memory provider"
```

### Task 8: Add the Primary `hermes knowledge` Inspect/Edit/Export/Erase CLI

**Files:**
- Create: `plugins/memory/knowledge/cli.py`
- Modify: `plugins/memory/knowledge/provider.py`
- Modify: `plugins/memory/knowledge/render.py`
- Create: `tests/plugins/memory/knowledge/test_cli.py`
- Modify: `tests/hermes_cli/test_plugin_cli_registration.py`

**Interfaces:**
- Consumes: active memory-plugin CLI discovery and every provider management action from Task 7.
- Produces: `register_cli(subparser)`, `knowledge_command(args)`, and these commands: `status`, `ingest google-contacts|slack|sessions`, `timeline`, `query`, `inspect`, `confirm`, `correct`, `export`, `erase`, `purge status`, `purge resume`, and `benchmark`.

- [ ] **Step 1: Write failing parser and end-to-end command tests**

```python
def test_cli_exposes_governance_commands(active_knowledge_config):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    knowledge_parser = subparsers.add_parser("knowledge")
    register_cli(knowledge_parser)
    for argv in (["knowledge", "timeline"], ["knowledge", "inspect", "claim-1"],
                 ["knowledge", "export", "--format", "jsonl"],
                 ["knowledge", "erase", "evidence", "e-1", "--preview"]):
        parsed = parser.parse_args(argv)
        assert parsed.func is knowledge_command

def test_correct_requires_preview_token_and_prints_new_authority(cli, seeded_claim):
    preview = cli("knowledge", "correct", seeded_claim.assertion_id, "--value", "Atlas", "--preview")
    token = extract_token(preview.stdout)
    result = cli("knowledge", "correct", seeded_claim.assertion_id, "--value", "Atlas", "--confirm", token)
    assert result.exit_code == 0
    assert "user_confirmed" in result.stdout

def test_erase_never_claims_complete_when_origin_remains(cli, session_evidence):
    result = cli("knowledge", "erase", "evidence", session_evidence.evidence_id, "--confirm", confirmed_token)
    assert result.exit_code == 2
    assert "residual_origin" in result.stdout
    assert "verified" not in result.stdout
```

- [ ] **Step 2: Run the focused RED test**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_cli.py tests/hermes_cli/test_plugin_cli_registration.py -v`

Expected: FAIL because the knowledge CLI registration module does not exist.

- [ ] **Step 3: Implement complete CLI parsing and truthful output**

All mutations use two phases: preview prints affected claim/evidence/artifact counts, managed paths, external residuals, source-session consequence, and one-use token; execute requires `--confirm TOKEN`. `--yes` is not accepted for correction or erasure. `export` defaults to a managed file under `knowledge/exports/` with manifest/schema version and lineage; `--copy-to PATH` creates an explicitly unmanaged copy and prints that later Hermes erasure cannot guarantee its removal. Formats are canonical JSONL and a human-readable Markdown evidence bundle.

`inspect` always shows claim authority, extraction method, valid/recorded intervals, confidence, freshness, each support/contradiction edge, source locator, disagreement, supersession, and downstream artifact counts. Exit codes are `0` verified/success, `2` blocked or residual, `3` unknown effect, and `1` failed/invalid.

- [ ] **Step 4: Run the GREEN CLI tests**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_cli.py tests/hermes_cli/test_plugin_cli_registration.py -v`

Expected: PASS for all subcommands, offline imports, tokens, authority labels, managed/unmanaged export wording, erase residuals, purge resume, and ANSI-free noninteractive output.

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/knowledge/cli.py plugins/memory/knowledge/provider.py plugins/memory/knowledge/render.py tests/plugins/memory/knowledge/test_cli.py tests/hermes_cli/test_plugin_cli_registration.py
git commit -m "feat(knowledge): add governed knowledge CLI"
```

### Task 9: Add the Ink Timeline Inspector and Governance RPC

**Files:**
- Modify: `tui_gateway/server.py:180-220, 14309-14366`
- Create: `tests/tui_gateway/test_knowledge_rpc.py`
- Create: `ui-tui/src/components/knowledgeTimeline.tsx`
- Modify: `ui-tui/src/app/interfaces.ts`
- Modify: `ui-tui/src/app/overlayStore.ts`
- Modify: `ui-tui/src/app/useInputHandlers.ts`
- Modify: `ui-tui/src/components/appLayout.tsx`
- Modify: `ui-tui/src/app/slash/commands/ops.ts`
- Create: `ui-tui/src/__tests__/knowledgeTimeline.test.tsx`
- Create: `ui-tui/src/__tests__/knowledgeCommand.test.ts`

**Interfaces:**
- Consumes: `MemoryManager.handle_management_action()` and plain render payloads.
- Produces: RPC methods `knowledge.timeline`, `knowledge.inspect`, `knowledge.query`, `knowledge.correct.preview`, `knowledge.correct.execute`, `knowledge.export`, `knowledge.erase.preview`, `knowledge.erase.execute`, and `knowledge.purge`; plus `/knowledge` overlay.

- [ ] **Step 1: Write failing RPC authorization and Ink interaction tests**

```python
def test_rpc_timeline_and_inspect_return_explicit_origin(call_rpc, active_provider):
    timeline = call_rpc("knowledge.timeline", {"limit": 20})
    assert timeline["claims"][0]["authority"] == "source_says"
    detail = call_rpc("knowledge.inspect", {"id": timeline["claims"][0]["claim_id"]})
    assert detail["evidence"][0]["source_locator"]

def test_rpc_rejects_execute_without_matching_preview_token(call_rpc, active_provider):
    result = call_rpc("knowledge.erase.execute", {"kind": "evidence", "id": "e-1", "token": "bad"})
    assert result["error"]["code"] == 4018
```

```tsx
it('shows authority, validity, freshness, evidence, and conflicts', async () => {
  render(<KnowledgeTimeline gw={gatewayFixture} onClose={vi.fn()} t={theme} />)
  expect(await screen.findByText(/source says/i)).toBeTruthy()
  expect(screen.getByText(/valid/i)).toBeTruthy()
  expect(screen.getByText(/freshness/i)).toBeTruthy()
  expect(screen.getByText(/conflict/i)).toBeTruthy()
})
```

- [ ] **Step 2: Run the focused RED tests**

Run: `scripts/run_tests.sh tests/tui_gateway/test_knowledge_rpc.py -v`

Run: `cd ui-tui && npm test -- --run src/__tests__/knowledgeTimeline.test.tsx src/__tests__/knowledgeCommand.test.ts`

Expected: Python RPC tests fail with method-not-found; Vitest fails because the component, command, and overlay state do not exist.

- [ ] **Step 3: Implement RPC validation and the keyboard-first overlay**

RPC obtains the live agent's `MemoryManager`, checks capability, validates bounded string/limit/interval fields, maps management statuses to existing JSON-RPC errors, and never returns evidence payload bytes unless `inspect` is explicitly called. Mutation execution accepts only tokens issued by the same provider/profile/action/current revision.

The Ink overlay has chronological and entity views; query/filter; detail with why/when/disagreement/downstream copies; `c` confirm, `e` correct through editor, `x` export, and `d` erase with preview then typed token. It reuses `ScrollBox`, `OverlayScrollbar`, `openInEditor`, and RPC error helpers. Add `knowledge` to overlay reset/blocking logic and `/knowledge` to the local ops command. No prompt is submitted and no conversation message is appended.

- [ ] **Step 4: Run the GREEN RPC/UI tests, typecheck, and lint**

Run: `scripts/run_tests.sh tests/tui_gateway/test_knowledge_rpc.py -v`

Run: `cd ui-tui && npm test -- --run src/__tests__/knowledgeTimeline.test.tsx src/__tests__/knowledgeCommand.test.ts && npm run typecheck && npm run lint`

Expected: PASS; the overlay works without a model call, mutation previews are mandatory, and TypeScript/lint report no errors.

- [ ] **Step 5: Commit**

```bash
git add tui_gateway/server.py tests/tui_gateway/test_knowledge_rpc.py ui-tui/src/components/knowledgeTimeline.tsx ui-tui/src/app/interfaces.ts ui-tui/src/app/overlayStore.ts ui-tui/src/app/useInputHandlers.ts ui-tui/src/components/appLayout.tsx ui-tui/src/app/slash/commands/ops.ts ui-tui/src/__tests__/knowledgeTimeline.test.tsx ui-tui/src/__tests__/knowledgeCommand.test.ts
git commit -m "feat(tui): add knowledge evidence timeline"
```

### Task 10: Add the Secondary Read-Only Dashboard Timeline

**Files:**
- Modify: `hermes_cli/web_server.py:2990-3020`
- Create: `tests/hermes_cli/test_web_server_knowledge.py`
- Create: `web/src/lib/knowledge.ts`
- Create: `web/src/lib/knowledge.test.ts`
- Create: `web/src/components/KnowledgeTimelineCard.tsx`
- Modify: `web/src/components/ChatSidebar.tsx`

**Interfaces:**
- Consumes: the same timeline/query/inspect render payload and active profile resolution.
- Produces: `GET /api/knowledge/summary`, `GET /api/knowledge/timeline`, `GET /api/knowledge/claims/{claim_id}`, and a collapsible `KnowledgeTimelineCard`.

- [ ] **Step 1: Write failing profile-isolation and read-only UI tests**

```python
def test_dashboard_knowledge_endpoints_are_profile_scoped(client, two_profile_stores):
    default = client.get("/api/knowledge/timeline", params={"profile": "default"}).json()
    work = client.get("/api/knowledge/timeline", params={"profile": "work"}).json()
    assert {row["claim_id"] for row in default["claims"]}.isdisjoint(
        {row["claim_id"] for row in work["claims"]}
    )

def test_dashboard_exposes_no_knowledge_mutation_route(app):
    paths = {route.path for route in app.routes}
    assert "/api/knowledge/erase" not in paths
    assert "/api/knowledge/correct" not in paths
```

```ts
it('renders origins and directs governance to the terminal', () => {
  const view = renderKnowledgeSummary(fixture)
  expect(view.rows[0].origin).toBe('Slack #phoenix at 2026-04-01')
  expect(view.governanceHint).toContain('Open /knowledge in the terminal')
})
```

- [ ] **Step 2: Run the focused RED tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_web_server_knowledge.py -v`

Run: `cd web && npm test -- --run src/lib/knowledge.test.ts`

Expected: FAIL because the endpoints, mapper, and card do not exist.

- [ ] **Step 3: Implement bounded read-only APIs and sidecar**

Resolve the requested profile with existing dashboard profile guards, open the provider store read-only, cap timeline/query results at `100`, and return no raw payload by default. The sidebar card shows current claim/conflict/source counts, recent changes, authority/freshness chips, and claim detail. Its governance link focuses the existing embedded TUI and instructs `/knowledge`; Dashboard cannot correct, export, or erase in this slice.

- [ ] **Step 4: Run the GREEN Dashboard tests and frontend checks**

Run: `scripts/run_tests.sh tests/hermes_cli/test_web_server_knowledge.py -v`

Run: `cd web && npm test -- --run src/lib/knowledge.test.ts && npm run typecheck && npm run lint`

Expected: PASS; profiles are isolated, raw evidence is opt-in detail only, no mutation route exists, and frontend checks are clean.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/web_server.py tests/hermes_cli/test_web_server_knowledge.py web/src/lib/knowledge.ts web/src/lib/knowledge.test.ts web/src/components/KnowledgeTimelineCard.tsx web/src/components/ChatSidebar.tsx
git commit -m "feat(dashboard): visualize knowledge timeline read only"
```

### Task 11: Freeze the 100-Question Corpus and Enforce the 90-Day Gates

**Files:**
- Create: `plugins/memory/knowledge/benchmark.py`
- Create: `scripts/knowledge_benchmark.py`
- Create: `tests/plugins/memory/knowledge/fixtures/corpus-v1.jsonl`
- Create: `tests/plugins/memory/knowledge/test_benchmark.py`

**Interfaces:**
- Consumes: two fixture exports, selected fixture sessions, `KnowledgeRetriever`, `SessionDB.search_messages()`, and immutable corpus metadata.
- Produces: `CorpusQuestion`, `EvaluationRun`, `CurrentHermesBaseline`, `score_run()`, `wilson_interval()`, `evaluate_gates()`, and canonical `benchmark-report.json`.

- [ ] **Step 1: Write failing corpus-freeze and metric tests**

```python
def test_corpus_v1_is_frozen_complete_and_stratified(corpus):
    assert corpus.version == "knowledge-temporal-v1"
    assert len(corpus.questions) == 100
    assert {q.stratum for q in corpus.questions} == {
        "current", "historic", "changed", "contradicted", "user_corrected", "deleted"
    }
    assert all(q.expected_evidence_ids for q in corpus.questions if q.stratum != "deleted")
    assert all(q.valid_from is not None for q in corpus.questions)

def test_gate_math_uses_fixed_denominators_and_exact_thresholds():
    report = evaluate_gates(candidate=fixture_run(accuracy=0.82, precision=0.90, recall=0.80,
        stale_conflicts=2, recovered_canaries=0, origins=100),
        baseline=fixture_run(accuracy=0.72, precision=0.61, recall=0.55,
        stale_conflicts=2, recovered_canaries=0, origins=33))
    assert report.passed is True
    assert report.accuracy_delta_pp == 10.0
```

- [ ] **Step 2: Run the focused RED test**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_benchmark.py -v`

Expected: FAIL because the corpus and benchmark module do not exist.

- [ ] **Step 3: Create and freeze the corpus and evaluator**

Create exactly 100 JSONL records: 20 current-state, 15 historic-at-time, 20 changed-over-time, 20 explicit contradiction, 15 user-corrected, and 10 deleted-canary questions. Every record includes stable ID, question, normalized accepted answer set, valid interval, expected evidence IDs, expected authority, expected conflict flag, source stratum, safety stratum, and exclusion policy. Put corpus SHA-256 and all preregistration fields in a header record and reject evaluation if the hash differs.

The baseline uses current `SessionDB.search_messages()` plus active built-in memory text, identical query text, result limit, and selected session/export material; it cannot call the new graph. Score answer correctness, evidence precision `TP/(TP+FP)`, evidence recall `TP/(TP+FN)`, stale-conflict answers, origin coverage, and canary recovery separately. Emit raw per-question outcomes, denominators, Wilson 95% intervals, latency p50/p95, user-attention events, cost source/value, exclusions, and gate verdict. Do not combine safety floors into an average.

- [ ] **Step 4: Run the GREEN benchmark tests and a deterministic fixture evaluation**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_benchmark.py -v`

Run: `python scripts/knowledge_benchmark.py --corpus tests/plugins/memory/knowledge/fixtures/corpus-v1.jsonl --fixture-mode --output .tmp/knowledge-benchmark-report.json`

Expected: tests PASS; fixture command exits `0`, reports denominator `100`, `+10.0pp`, precision `0.900`, recall `0.800`, no stale-conflict increase, origin coverage `1.000`, and recovered canaries `0`.

- [ ] **Step 5: Commit**

```bash
git add plugins/memory/knowledge/benchmark.py scripts/knowledge_benchmark.py tests/plugins/memory/knowledge/fixtures/corpus-v1.jsonl tests/plugins/memory/knowledge/test_benchmark.py
git commit -m "test(knowledge): freeze temporal evidence benchmark"
```

### Task 12: Prove Real-Path E2E, Fault Recovery, Privacy, Cache Safety, and Rollback

**Files:**
- Create: `tests/plugins/memory/knowledge/test_e2e.py`
- Modify: `tests/plugins/memory/knowledge/test_lineage_purge.py`
- Modify: `tests/plugins/memory/knowledge/test_provider.py`
- Create: `docs/features/knowledge-timeline.md`
- Create: `docs/security/knowledge-erasure-threat-model.md`
- Modify: `plugins/memory/knowledge/README.md`

**Interfaces:**
- Consumes: the complete provider/CLI/RPC/store/import/retrieval/purge/benchmark vertical slice.
- Produces: production-readiness evidence, migration/rollback procedure, operator documentation, threat model, and final verification matrix.

- [ ] **Step 1: Write the failing real-path E2E and invariant tests**

```python
def test_real_path_import_correct_query_export_erase(
    tmp_path, monkeypatch, fixture_exports, seeded_session_db
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = load_memory_provider("knowledge")
    provider.initialize("e2e-session", hermes_home=str(tmp_path), platform="cli", agent_context="primary")
    ingest_all_three_authorized_sources(provider, fixture_exports, seeded_session_db)
    before = provider.handle_management_action("query", {"text": "Where did Ari work?", "at": 150.0})
    assert before.data["hits"][0]["object_value"] == "Phoenix"
    corrected = explicitly_correct(provider, before.data["hits"][0]["assertion_id"], "Atlas")
    assert corrected.status == "verified"
    export_path = create_managed_export(provider)
    erased = explicitly_erase(provider, root_kind="source", root_id="canary-source")
    assert erased.status == "verified"
    assert erased.data["recovered_canaries"] == []
    assert not export_path.exists()

def test_conversation_cache_and_role_invariants_hold_across_management(agent_fixture):
    baseline = snapshot_prompt_tools_provider_model(agent_fixture)
    agent_fixture._memory_manager.handle_management_action("query", {"text": "Ari"})
    agent_fixture._memory_manager.handle_management_action("timeline", {"limit": 10})
    assert snapshot_prompt_tools_provider_model(agent_fixture) == baseline
    assert_roles_alternate(agent_fixture.messages)
```

- [ ] **Step 2: Run the focused RED E2E test**

Run: `scripts/run_tests.sh tests/plugins/memory/knowledge/test_e2e.py -v`

Expected: FAIL until the complete real-path wiring, rollback metadata, and documentation assertions are present.

- [ ] **Step 3: Complete recovery, migration, privacy, and operator documentation**

Cover these fault/security cases with real SQLite/files and injected boundaries: crash/restart at every purge stage; duplicate import and execute replay; stale correction/erasure token; active source-session refusal; partial managed-export unlink; unavailable embedding model; SQLite busy/locked and corrupt index; malformed/archive-bomb inputs; cross-profile ID guessing; path traversal; malicious Slack/VCF text and prompt injection treated as untrusted evidence; SSRF impossible because import is offline; derived-memory leakage through FTS/vector/cache/export; compromised provider response validation; and backup/restore followed by erase verification.

Document migration as an additive schema version with pre-migration backup under profile-local knowledge state, migration transaction, integrity check, and old-version refusal on downgrade. Rollback disables `memory.provider: knowledge`, starts a new conversation, preserves the database for re-enable, and supports `hermes knowledge export` then verified `erase source --all` before uninstall. A failed migration restores the pre-migration database atomically; it never silently opens a partially migrated store.

Document the origin/residual boundary, user-confirmed authority, correction-vs-erasure distinction, managed/unmanaged exports, whole-session deletion consequence, embedding model/revision, no telemetry, no connector access, new-conversation cache boundary, purge resume/status, and benchmark reproduction.

- [ ] **Step 4: Run the GREEN focused suites and final static checks**

Run: `scripts/run_tests.sh tests/agent/test_memory_management_contract.py tests/plugins/memory/knowledge tests/tui_gateway/test_knowledge_rpc.py tests/hermes_cli/test_web_server_knowledge.py tests/agent/test_memory_provider.py tests/hermes_cli/test_plugin_cli_registration.py -v`

Expected: PASS with real imports and temporary profile-local state; no test writes to the user's actual Hermes home.

Run: `cd ui-tui && npm test -- --run src/__tests__/knowledgeTimeline.test.tsx src/__tests__/knowledgeCommand.test.ts && npm run typecheck && npm run lint`

Expected: PASS with no TypeScript or lint errors.

Run: `cd web && npm test -- --run src/lib/knowledge.test.ts && npm run typecheck && npm run lint`

Expected: PASS with no frontend errors.

Run: `git diff --check`

Expected: no output and exit `0`.

- [ ] **Step 5: Run the preregistered 100-question proof and apply stop conditions**

Run: `python scripts/knowledge_benchmark.py --corpus tests/plugins/memory/knowledge/fixtures/corpus-v1.jsonl --baseline current-hermes --candidate knowledge --output .artifacts/knowledge-temporal-v1/report.json`

Expected: exit `0` only if accuracy improves by at least `+10.0pp`, evidence precision is at least `0.90`, evidence recall is at least `0.80`, stale-conflict answers do not increase, all displayed claims have origins, and controlled-layer recovered canaries equal `0`. Any safety-floor failure exits `2` and blocks rollout; an underpowered result exits `3` as inconclusive rather than relaxing a gate.

- [ ] **Step 6: Commit**

```bash
git add tests/plugins/memory/knowledge/test_e2e.py tests/plugins/memory/knowledge/test_lineage_purge.py tests/plugins/memory/knowledge/test_provider.py docs/features/knowledge-timeline.md docs/security/knowledge-erasure-threat-model.md plugins/memory/knowledge/README.md
git commit -m "docs(knowledge): verify privacy recovery and rollout"
```

## Rollout and Recovery Gates

1. **Developer-only:** run fixture imports and the complete focused suite with FTS-only and local-embedding modes. No existing profile data is migrated automatically.
2. **Opt-in canary profiles:** user selects `knowledge` through `hermes memory setup knowledge`, confirms each import, starts a new conversation, and sees `hermes knowledge status` report schema/retrieval/embedding identities.
3. **Read/query:** enable bounded prefetch only after origin coverage is `100%` and cross-profile tests pass. Corrections and erasure remain explicit foreground operations.
4. **Governance:** enable managed export and whole-session source deletion only after crash-replay and zero-canary gates pass on all supported platforms.
5. **90-day decision:** advance beyond the proof only when the frozen 100-question report clears every quality and privacy gate. Recovered canaries, authority drift, stale-conflict regression, or cache/schema drift immediately disable prefetch and block rollout.

Recovery commands are:

```bash
hermes knowledge purge status
hermes knowledge purge resume <operation-id>
hermes knowledge status
hermes memory off
```

`hermes memory off` is a safe runtime rollback but does not claim erasure. Verified removal requires the preview/confirm erase flow and a successful controlled-layer scan.

## Final Verification Matrix

| Requirement | Primary proof |
|---|---|
| Immutable evidence vs revisable temporal claims | `test_models_store.py`, `test_claim_workflow.py` |
| Entities, source edges, validity, confidence, freshness, contradictions | `test_claim_workflow.py`, `test_retrieval.py`, CLI/Ink detail tests |
| User-confirmed authority only by explicit action | claim, CLI, RPC stale-token tests |
| Cascading correction/deletion across raw/derived/embedding/cache/export | `test_lineage_purge.py`, `test_e2e.py` |
| Inspect/edit/export/erase in CLI and Ink | `test_cli.py`, `test_knowledge_rpc.py`, Ink Vitest |
| Two authorized exports plus selected session memory | `test_importers.py`, E2E fixture |
| Plugin delivery and minimal generic ABC widening | management contract/provider discovery tests |
| No new model-visible tool | management contract and provider schema-stability tests |
| Frozen 100-question corpus and exact metrics | `test_benchmark.py`, canonical report |
| `+10pp`, `>=90%` precision, `>=80%` recall, zero canaries | benchmark gate exit status |
| Real-path crash/replay/stale-authority/partial-failure | purge and E2E parameterized faults |
| Privacy/security and profile isolation | importer, purge, Dashboard, threat-model tests |
| Byte-stable cache/tool/provider/model and role alternation | provider/E2E invariant snapshots |
| CLI/Ink primary, Dashboard secondary, no Desktop dependency | command/overlay tests and read-only Dashboard route test |
| Migration, rollback, operator docs, truthful statuses | Task 12 docs and migration/failure tests |

Plan execution is complete only after every task's RED test was observed failing for the stated reason, every GREEN command passes, the preregistered proof clears all gates, and the working tree contains only the intentional implementation/documentation changes.
