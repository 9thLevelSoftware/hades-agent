# Verified Outcome & Artifact Receipts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every receipt-enabled Hermes turn, mission, and transaction one immutable, independently scored, independently recheckable record of the requested end state, claimed effects, evidence, artifacts, uncertainty, freshness, and provenance without ever treating completion text or a signature as proof of truth.

**Architecture:** Establish `agent.receipts` as the only public receipt contract and persist its canonical JSON projections in profile-local `state.db`. Source adapters convert the existing turn ledger, verification ledger, operation journal, mission records, transaction records, and artifact catalog into deduplicated evidence snapshots; an independent end-state scorer alone can mint the sealed decision required for `verified`. CLI and native Ink viewers are primary, Dashboard inspection is secondary, and optional signing remains a service-gated provenance layer outside the model tool schema.

**Tech Stack:** Python 3.13, frozen dataclasses and `typing.Literal`, SHA-256 canonical JSON, SQLite/WAL through `SessionDB`, existing turn/verification/operation/mission/transaction seams, Rich/classic CLI, Ink/TypeScript JSON-RPC, React Dashboard, pytest through `scripts/run_tests.sh`, Vitest, YAML benchmark manifests.

## Global Constraints

- Work from the branch containing this plan and preserve unrelated changes. Each task ends in exactly one conventional commit.
- TDD is mandatory. Run Python tests only through `scripts/run_tests.sh`; use package-local npm scripts for Ink, Dashboard, and documentation checks.
- `agent.receipts` owns the one canonical receipt vocabulary, immutable value objects, content hashing, storage, scorer seal, recheck observations, redaction, export, retention, and signer registry. Mission, transaction, experience, team, federation, and commerce code consume it and may add domain claims only.
- `ReceiptStatus` has exactly `verified`, `completed_unverified`, `failed`, `blocked`, and `unknown_effect`. `RECEIPT_STATUSES` is the canonical frozen set of those five strings. No consumer may add a receipt status.
- Only an independent scorer appropriate to the requested end state can produce `VerifiedReceiptDecision`. A workflow/turn/transaction success label, model statement, handler return, operation-journal row, artifact existence, user assertion, or signature alone yields at most `completed_unverified`.
- A signature proves provenance over a content hash. It never changes a status, claim verdict, uncertainty, freshness, or scorer result and never proves that artifact contents or claims are true.
- Receipts, claims, evidence digests, artifact digests, and receipt observations are immutable. Recheck appends one linked observation; it never updates the original receipt, subject terminal state, or an earlier observation.
- Persist evidence and the receipt before a receipt-enabled mission or transaction presents terminal success. A crash between receipt insertion and a consumer's projection is repaired by the source key/content hash and never creates a duplicate.
- Preserve existing `turn_outcomes.outcome == "verified"` for compatibility, but treat it only as an untrusted source claim. The receipt scorer must independently reload verification evidence, operation certainty, artifact digests, and freshness before issuing a verified receipt.
- The v1 vertical-slice `receipts` and `receipt_observations` tables are migration inputs, not a second schema. Preserve receipt IDs and lineage, recompute canonical hashes, import signatures as untrusted provenance attestations, and downgrade a legacy `verified` row to `completed_unverified` until a current scorer rechecks it.
- Source ingestion is idempotent by `(source_kind, source_id)` and content hash. A repeated identical source returns the existing receipt; reusing a source identity with different content is a conflict, not an update.
- Artifact rechecks are read-only and race-safe: open the file without following a swapped symlink where the platform permits, hash the same open handle that is statted, enforce allowed roots, and report missing, changed, inaccessible, or ambiguous evidence truthfully.
- Redact secrets, credentials, message bodies, query strings, and sensitive absolute path prefixes before canonical receipt content is hashed or persisted. Raw local locators live only in the bounded artifact-location table and are excluded from public export.
- Profiles remain independent. Every database, export, artifact locator, signing provider, and benchmark path resolves from `get_hermes_home()`; no lookup, recheck, export, signer, or retention job crosses `HERMES_HOME`.
- Stable behavioral settings live under `receipts:` in `config.yaml`. Signing credentials stay in a secret store or `.env`; config stores only a provider ID and whether signing is required.
- Add no model-visible core tool and do not change any existing tool JSON schema. This is Footprint Ladder rung 1 plus CLI, native TUI RPC, secondary read-only Dashboard APIs, and a service-gated/plugin-backed signing seam.
- The system prompt, effective model tool definitions, provider, and model remain byte-stable for a conversation. Receipt state is never injected into prior messages, never adds a synthetic user message, and never mutates history outside existing compression.
- Real-path tests use a temporary `HERMES_HOME`, real `SessionDB`/verification SQLite connections, real files, real hashes, real CLI parsers, and fresh object graphs or subprocess restarts. Mock only external signing/network/process-kill boundaries.
- No outbound telemetry. Proof reports are local JSON/Markdown and state denominators, exclusions, Wilson intervals, p50/p95 latency, baseline/candidate costs, safety slices, and stop conditions separately.

---


---

## Approved Portfolio Contract

**Layman outcome:** Hermes shows evidence of what changed, whether the requested end state really holds, what was produced, and what remains uncertain instead of merely saying “done.”

**Design boundary:** Receipts own task/effect proof. Knowledge owns long-lived personal claims and evidence; Context may compile receipts into a temporary working set but never becomes their source of truth. A receipt carries immutable requested outcome and constraints, mission/step and transaction identifiers, before/after observations, effect claims, independent verifier results, evidence pointers, artifact hashes, uncertainty, freshness, and optional provenance attestations. Rechecking appends a linked observation.

**90-day proof:** Seed exactly 50 false-success missions across silent no-op, wrong file, stale page, partial delivery, reverted change, forged-looking artifact, and grader ambiguity. Pass only with zero seeded failures labeled `verified`, at least 45/50 correct terminal classifications, 50/50 claimed effects linked to existing evidence, and 50/50 receipts independently recheckable after reopening storage in a new process.

**Dependencies and failure conditions:** This shared evidence contract is a prerequisite for portfolio items #1, #2, #9, #14, #19, and #20. Mission and transaction implementations may be present from the approved vertical slice when migration runs. A signature proves who or what produced bytes, not factual truth; an unavailable, stale, inappropriate, self-authored, or ambiguous scorer cannot emit `verified`.

**Delivery:** Footprint Ladder rung 1—extend canonical `SessionDB` and artifact/evidence seams, expose CLI and native Ink viewers first, add secondary Dashboard inspection, keep Desktop out of scope, and load optional signers only through a configured service gate or standalone plugin.

---

## Current Code Map and Ownership

### Existing seams this plan extends

- `agent/turn_ledger.py:27-347` owns frozen `TurnOutcomeRecord`, builds one per turn, and writes through `SessionDB.record_turn_outcome()`; its `verified` outcome remains compatibility input, not receipt truth.
- `agent/turn_finalizer.py:152-185` and `agent/codex_runtime.py:508-558` classify then persist turn outcomes. The receipt hook runs after raw ledger persistence and before a receipt-enabled terminal result is projected.
- `agent/verification_evidence.py:25-620` owns a separate profile-local `verification_evidence.db`, records terminal checks, marks edits stale, and exposes `verification_status(session_id, cwd)`; receipt code reads it through an immutable adapter and does not duplicate its rows.
- `agent/verification_stop.py:191-308` consumes `verification_status()` for a bounded coding follow-up. Receipt issuance does not add another prompt nudge or alter this behavior.
- `agent/operation_journal.py:70-278` and `hermes_state.py:870-915` own operation certainty. `unknown`/`effect_disposition == "unknown"` maps to `unknown_effect` and is never retried by receipt code.
- `hermes_state.py:134`, `hermes_state.py:760-915`, `hermes_state.py:1024-1460`, and `hermes_state.py:2302-2380` define v21 declarative schema reconciliation, WAL writes, `turn_outcomes`, `agent_operations`, and turn-ledger accessors. Canonical receipt tables and typed store primitives land here.
- `tools/code_execution_tool.py:67-133`, `tools/code_execution_tool.py:699-910`, and `tools/code_execution_tool.py:1028-1061` bound and persist generated artifacts but currently return paths without canonical artifact IDs or SHA-256 digests.
- The approved vertical slice may add `hermes_cli/missions_db.py`, `agent/effect_transactions.py`, `agent/receipts.py`, and provisional `receipts`/`receipt_observations`; this plan migrates those tables and replaces that module's public implementation without changing consumer import path.
- Portfolio item #2 consumes the public names frozen below from `agent.receipts` and adds transaction-specific claim construction in `agent/effects/receipts.py`; it does not own receipt schema, status resolution, hashing, or observations.
- `hermes_cli/commands.py:46-304`, `hermes_cli/main.py:4328-4350`, `hermes_cli/main.py:13252-13256`, and `cli.py:8427-9255` provide registry, top-level subcommand, and classic slash-command patterns.
- `tui_gateway/server.py:11779-11860` and `tui_gateway/server.py:13181-13330` expose registry-backed command discovery and JSON-RPC dispatch. `ui-tui/src/app/slash/commands/ops.ts:64-735` owns native operational commands.
- `hermes_cli/web_server.py:9765-10220` and `web/src/App.tsx:109-206` provide profile-aware REST and Dashboard route/navigation patterns. Dashboard receives a separate read-only receipt inspector; Desktop files are untouched.

### New focused production files

- `agent/receipts.py` — stable public facade and exports for all canonical names.
- `agent/receipt_models.py` — frozen requested-outcome, claim, evidence, artifact, receipt, observation, decision, source-key, and attestation values.
- `agent/receipt_hashing.py` — strict canonical JSON and `sha256:` content hashes.
- `agent/receipt_store.py` — typed `SessionDB` store, idempotent source links, immutable inserts, observations, query/filter methods, and migration conversion helpers.
- `agent/receipt_artifacts.py` — bounded artifact registration, locator isolation, open-handle hashing, and read-only recheck.
- `agent/receipt_scoring.py` — scorer protocol/registry, sealed verified decision, precedence rules, and built-in code-turn/mission/transaction scorers.
- `agent/receipt_ingest.py` — evidence-envelope builders and idempotent turn/mission/transaction issuance/recovery.
- `agent/receipt_security.py` — redaction, public/local export, safe bundles, retention tombstones, and service-gated signer registry.
- `hermes_cli/receipts.py` — shared top-level/classic CLI parser, service wiring, text/JSON renderers.
- `benchmarks/receipts/manifest.yaml` — preregistered exact 50-mission corpus and gates.
- `benchmarks/receipts/cases.py` — deterministic case expansion and manifest validation.
- `benchmarks/receipts/runner.py` — local report-only baseline/candidate harness.
- `website/docs/user-guide/features/outcome-receipts.md` — operator guide, status language, recheck/export/signing/retention, and limitations.
- `website/docs/development/receipt-contract.md` — public consumer/scorer/signer contract for built-ins and standalone plugins.
- `web/src/pages/ReceiptsPage.tsx` — secondary Dashboard list/detail/observation inspector.

### Existing production files modified

- `hermes_state.py` — canonical tables, indexes, atomic v1 migration, and low-level receipt/artifact methods.
- `agent/turn_ledger.py`, `agent/turn_finalizer.py`, `agent/codex_runtime.py` — idempotent turn-source issuance with no new prompt messages.
- `tools/code_execution_tool.py` — attach canonical artifact ID/hash metadata using internal execution context; tool definition remains byte-identical.
- `hermes_cli/config.py` — safe `receipts` config and validation.
- `hermes_cli/commands.py`, `hermes_cli/main.py`, `hermes_cli/cli_commands_mixin.py`, `cli.py` — `receipt`/`receipts` top-level and classic routes.
- `tui_gateway/server.py`, `ui-tui/src/gatewayTypes.ts`, `ui-tui/src/app/slash/commands/ops.ts` — native `receipt.exec` RPC and viewer.
- `hermes_cli/web_server.py`, `web/src/lib/api.ts`, `web/src/App.tsx` — read-only Dashboard endpoints/client/route.
- `website/docs/reference/cli-commands.md`, `website/docs/reference/slash-commands.md`, `website/sidebars.ts` — command and navigation docs.

### Focused tests

- `tests/agent/test_receipt_models.py`
- `tests/agent/test_receipt_store.py`
- `tests/agent/test_receipt_migration.py`
- `tests/agent/test_receipt_artifacts.py`
- `tests/agent/test_receipt_scoring.py`
- `tests/agent/test_receipt_ingest.py`
- `tests/agent/test_receipt_security.py`
- `tests/hermes_cli/test_receipt_cli.py`
- `tests/hermes_cli/test_receipt_e2e.py`
- `tests/tui_gateway/test_receipt_rpc.py`
- `tests/hermes_cli/test_receipt_dashboard.py`
- `tests/benchmarks/test_receipt_benchmark.py`
- `ui-tui/src/__tests__/receiptCommand.test.ts`
- `web/src/pages/ReceiptsPage.test.tsx`

---

## Canonical Public Interface — Frozen for All Portfolio Plans

`agent.receipts` must export exactly these public contract names. Additional private helpers live in sibling modules; consumer plans import only from `agent.receipts`.

```python
ReceiptStatus = Literal[
    "verified", "completed_unverified", "failed", "blocked", "unknown_effect"
]
RECEIPT_STATUSES: frozenset[ReceiptStatus] = frozenset({
    "verified", "completed_unverified", "failed", "blocked", "unknown_effect",
})

@dataclass(frozen=True)
class RequestedOutcome:
    outcome_kind: str
    description: str
    constraints: tuple[str, ...]
    producer_id: str
    content_hash: str

@dataclass(frozen=True)
class ReceiptClaim:
    claim_id: str
    claim_kind: str
    statement: str
    expected_json: str
    observed_json: str
    evidence_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    required: bool
    verdict: Literal["satisfied", "unsatisfied", "unknown", "not_applicable"]
    uncertainty: tuple[str, ...]
    content_hash: str

@dataclass(frozen=True)
class EvidenceDigest:
    evidence_id: str
    evidence_kind: str
    source_ref: str
    producer_id: str
    observed_at: str
    fresh_until: str | None
    summary: str
    payload_hash: str
    artifact_ids: tuple[str, ...]
    content_hash: str

@dataclass(frozen=True)
class ArtifactDigest:
    artifact_id: str
    source_kind: str
    source_ref: str
    display_name: str
    media_type: str | None
    size_bytes: int
    sha256: str
    mtime_ns: int | None
    captured_at: str
    content_hash: str

@dataclass(frozen=True)
class ReceiptSourceKey:
    source_kind: Literal["turn", "mission", "transaction", "legacy", "external"]
    source_id: str

@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    source: ReceiptSourceKey
    subject_kind: Literal["turn", "mission", "transaction", "external"]
    subject_id: str
    session_id: str | None
    turn_id: str | None
    mission_id: str | None
    transaction_id: str | None
    requested_outcome: RequestedOutcome
    status: ReceiptStatus
    claims: tuple[ReceiptClaim, ...]
    evidence: tuple[EvidenceDigest, ...]
    artifacts: tuple[ArtifactDigest, ...]
    uncertainty: tuple[str, ...]
    scorer_id: str
    scorer_version: str
    decided_at: str
    content_hash: str

@dataclass(frozen=True)
class ReceiptObservation:
    observation_id: str
    receipt_id: str
    previous_observation_id: str | None
    status: ReceiptStatus
    claims: tuple[ReceiptClaim, ...]
    evidence: tuple[EvidenceDigest, ...]
    artifacts: tuple[ArtifactDigest, ...]
    uncertainty: tuple[str, ...]
    scorer_id: str
    scorer_version: str
    observed_at: str
    content_hash: str

@dataclass(frozen=True, init=False)
class VerifiedReceiptDecision:
    scorer_id: str
    scorer_version: str
    subject_kind: str
    subject_id: str
    snapshot_hash: str
    claim_hashes: tuple[str, ...]
    decided_at: str
    fresh_until: str | None
    decision_hash: str

class ReceiptStore:
    def insert(
        self,
        receipt: Receipt,
        *,
        decision: VerifiedReceiptDecision | None = None,
    ) -> Receipt: ...
    def append_observation(
        self,
        observation: ReceiptObservation,
        *,
        decision: VerifiedReceiptDecision | None = None,
    ) -> ReceiptObservation: ...
    def get(self, receipt_id: str) -> Receipt | None: ...
    def find_by_source(self, source: ReceiptSourceKey) -> Receipt | None: ...
    def list(self, query: ReceiptQuery) -> list[ReceiptSummary]: ...

def canonical_content_hash(value: object) -> str: ...
def digest_artifact(path: Path, *, source_kind: str, source_ref: str,
                    allowed_roots: tuple[Path, ...]) -> ArtifactDigest: ...
```

`VerifiedReceiptDecision` has `init=False`; only `ReceiptScoringService` holds the module-private capability used to construct it. `ReceiptStore.insert()` and `append_observation()` reject `status == "verified"` unless the sealed decision matches subject, snapshot, exact claim hashes, scorer, freshness, and the receipt/observation decision fields. Non-verified decisions use the ordinary immutable `ReceiptDecision` internal type and never receive a seal.

Canonical hashes use UTF-8 JSON, sorted string keys, compact separators, NFC strings, UTC RFC 3339 timestamps, finite decimal rendering, and tuple-to-array conversion. Hash inputs exclude `receipt_id`, `observation_id`, database `inserted_at`, `content_hash`, local artifact locators, and provenance attestations. They include subject/source keys, requested outcome, status, all claim/evidence/artifact content hashes, uncertainty, scorer identity/version, and `decided_at`/`observed_at` freshness facts. New IDs are deterministic `rct_<64 hex>`, `obs_<64 hex>`, `clm_<64 hex>`, `evd_<64 hex>`, and `art_<64 hex>` values derived from the corresponding canonical hash; migrated legacy receipt/observation IDs are the explicit compatibility exception and remain mapped to their recomputed canonical hashes.

---

### Task 0: Preregister the 50-Mission False-Success Contract

**Files:**
- Create: `benchmarks/receipts/manifest.yaml`
- Create: `benchmarks/receipts/cases.py`
- Create: `tests/benchmarks/test_receipt_benchmark.py`

**Interfaces:**
- Produces `load_receipt_cases(path: Path) -> tuple[ReceiptBenchmarkManifest, tuple[ReceiptCase, ...]]` and an exact 50-case denominator consumed by Tasks 5 and 10.
- Consumes no production receipt implementation, so benchmark definitions are frozen before behavior exists.

- [ ] **Step 1: Write the RED manifest contract test**

```python
def test_receipt_manifest_freezes_exact_proof_contract():
    manifest, cases = load_receipt_cases(MANIFEST)
    assert manifest.corpus_version == "receipts-false-success-v1"
    assert manifest.random_seed == 20260716
    assert len(cases) == 50
    assert Counter(c.stratum for c in cases) == {
        "silent_noop": 8,
        "wrong_file": 7,
        "stale_page": 7,
        "partial_delivery": 7,
        "reverted_change": 7,
        "forged_artifact": 7,
        "grader_ambiguity": 7,
    }
    assert manifest.gates == ReceiptGates(
        max_false_verified=0,
        min_correct_classifications=45,
        min_traceable_claims_ratio=1.0,
        min_recheckable_receipts_ratio=1.0,
    )
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_receipt_benchmark.py -q`

Expected: FAIL importing `benchmarks.receipts.cases` and reading the absent manifest.

- [ ] **Step 3: Freeze exact strata, expectations, baseline, and stop conditions**

Create a manifest whose generator expands IDs `silent-noop-01..08` and `wrong-file|stale-page|partial-delivery|reverted-change|forged-artifact|grader-ambiguity-01..07`. Each row fixes `expected_status`, injected fault, evidence source, recheck adapter, safety stratum, and allowed root. Use these required classifications:

```yaml
corpus_version: receipts-false-success-v1
random_seed: 20260716
denominator: 50
strata:
  silent_noop: {count: 8, expected_status: failed}
  wrong_file: {count: 7, expected_status: failed}
  stale_page: {count: 7, expected_status: completed_unverified}
  partial_delivery: {count: 7, expected_status: unknown_effect}
  reverted_change: {count: 7, expected_status: failed}
  forged_artifact: {count: 7, expected_status: failed}
  grader_ambiguity: {count: 7, expected_status: completed_unverified}
gates:
  max_false_verified: 0
  min_correct_classifications: 45
  min_traceable_claims_ratio: 1.0
  min_recheckable_receipts_ratio: 1.0
baseline: current_hermes_turn_outcome_and_prose
candidate: canonical_receipt_scorer
stop_conditions:
  - any_seeded_failure_verified
  - any_effect_claim_without_existing_evidence
  - any_receipt_not_recheckable_after_process_restart
  - any_signature_changes_truth_status
  - any_cross_profile_read_or_write
```

`cases.py` uses frozen dataclasses, rejects duplicate IDs, count drift, unknown status values, missing recheck adapters, or a threshold weaker than the approved gate. It never derives expectations from scorer output.

- [ ] **Step 4: Run GREEN**

Run: `scripts/run_tests.sh tests/benchmarks/test_receipt_benchmark.py -q`

Expected: PASS with exactly 50 unique frozen cases and all seven strata/gates validated.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/receipts/manifest.yaml benchmarks/receipts/cases.py \
  tests/benchmarks/test_receipt_benchmark.py
git commit -m "test: preregister receipt false-success proof"
```

---

### Task 1: Freeze Immutable Models, Public Names, and Canonical Hashes

**Files:**
- Create: `agent/receipt_models.py`
- Create: `agent/receipt_hashing.py`
- Modify (vertical-slice prerequisite): `agent/receipts.py`
- Create: `tests/agent/test_receipt_models.py`

**Interfaces:**
- Produces every name and exact field/signature in “Canonical Public Interface,” plus private `ReceiptDecision`, `ReceiptEnvelope`, `EvidenceSnapshot`, `ReceiptQuery`, `ReceiptSummary`, and builders used later.
- Consumes only Python standard library types; no store, scorer, mission, transaction, or UI imports.

- [ ] **Step 1: Write RED API, immutability, and hash-vector tests**

```python
def test_public_status_contract_and_immutable_claim():
    assert RECEIPT_STATUSES == frozenset({
        "verified", "completed_unverified", "failed", "blocked", "unknown_effect",
    })
    claim = make_claim(statement="README contains marker", evidence_ids=("evd_a",))
    with pytest.raises(FrozenInstanceError):
        claim.statement = "changed"


def test_hash_is_stable_across_key_order_and_rejects_non_finite_float():
    assert canonical_content_hash({"b": [2], "a": "é"}) == canonical_content_hash(
        {"a": "e\u0301", "b": (2,)}
    )
    assert canonical_content_hash({"answer": 42}) == (
        "sha256:e6db658a6ad458d357eaa9df09a0bdbe425"
        "cbb81eaff54d37754a6c3f73d3539"
    )
    with pytest.raises(ValueError, match="finite"):
        canonical_content_hash({"bad": float("nan")})


def test_verified_decision_has_no_public_constructor():
    with pytest.raises(TypeError):
        VerifiedReceiptDecision(scorer_id="self")
```

The committed digest is the protocol vector for the exact canonical bytes `{"answer":42}`; it is an interoperability invariant, not a changing catalog snapshot.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/test_receipt_models.py -q`

Expected: FAIL importing `agent.receipts` public types and `canonical_content_hash`.

- [ ] **Step 3: Implement strict canonicalization and frozen builders**

```python
def canonical_content_hash(value: object) -> str:
    normalized = _normalize(value)
    payload = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def build_claim(**fields: object) -> ReceiptClaim:
    body = _claim_body(**fields)
    digest = canonical_content_hash(body)
    return ReceiptClaim(claim_id=f"clm_{digest.removeprefix('sha256:')}",
                        content_hash=digest, **body)
```

Normalize dataclasses, mappings, tuples/lists, booleans, `None`, integers, finite `Decimal`/float values, and timezone-aware datetimes. Reject bytes, paths, naive datetimes, non-string mapping keys, sets, unknown objects, duplicate IDs, dangling claim references, and duplicate content hashes. NFC-normalize every string and UTC-normalize timestamps.

- [ ] **Step 4: Export only the canonical facade**

`agent/receipts.py` re-exports the public models, `ReceiptStore` forward import, `canonical_content_hash`, `digest_artifact`, scorer protocol, signer protocol, and issuer service. It defines `__all__` explicitly and contains no second implementation. If the vertical slice already created this file, move its behavior into the new sibling modules and keep imports working.

- [ ] **Step 5: Run GREEN and existing turn vocabulary regressions**

Run: `scripts/run_tests.sh tests/agent/test_receipt_models.py tests/agent/test_turn_ledger.py -q`

Expected: PASS; immutable/hash vectors are stable and the existing turn-outcome vocabulary remains unchanged.

- [ ] **Step 6: Commit**

```bash
git add agent/receipt_models.py agent/receipt_hashing.py agent/receipts.py \
  tests/agent/test_receipt_models.py
git commit -m "feat: define canonical receipt contract"
```

---

### Task 2: Migrate and Persist One Canonical Receipt Store

**Files:**
- Modify: `hermes_state.py`
- Create: `agent/receipt_store.py`
- Create: `tests/agent/test_receipt_store.py`
- Create: `tests/agent/test_receipt_migration.py`
- Modify: `tests/test_hermes_state.py`
- Modify: `tests/test_hermes_state_wal_fallback.py`

**Interfaces:**
- Consumes Task 1 immutable models and canonical hash builders plus `SessionDB._execute_read/_execute_write`.
- Produces the exact `ReceiptStore.insert/append_observation/get/find_by_source/list` API, artifact/source/attestation primitives used by every later task, and atomic clean-v21/v1-vertical-slice upgrades.

- [ ] **Step 1: Write RED clean-schema, immutable-write, replay, and migration tests**

```python
def test_verified_insert_requires_matching_scorer_seal(store, verified_receipt):
    with pytest.raises(PermissionError, match="scorer decision"):
        store.insert(verified_receipt)


def test_source_replay_is_idempotent_but_conflict_is_rejected(store, completed_receipt):
    first = store.insert(completed_receipt)
    assert store.insert(completed_receipt) == first
    with pytest.raises(ReceiptSourceConflict):
        store.insert(replace(completed_receipt, content_hash="sha256:" + "0" * 64))


def test_migrates_v1_verified_as_unverified_until_recheck(v1_state_db):
    db = SessionDB(v1_state_db)
    migrated = ReceiptStore(db).get("legacy-r1")
    assert migrated.status == "completed_unverified"
    assert "legacy verified status requires independent recheck" in migrated.uncertainty
    assert migrated.receipt_id == "legacy-r1"
    assert ReceiptStore(db).find_by_source(ReceiptSourceKey("legacy", "legacy-r1"))
```

Also copy the exact provisional tables from the vertical-slice plan into a fixture. Cover interrupted migration rollback, duplicate old content hashes, legacy signature import, observation ordering, insert-after-reopen, two-process identical replay, source-identity conflict, and attempts to `UPDATE` immutable rows through public methods.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/test_receipt_store.py tests/agent/test_receipt_migration.py -q`

Expected: FAIL because receipt tables, migration, and `ReceiptStore` are absent.

- [ ] **Step 3: Add normalized canonical tables and immutability triggers**

Add declarative tables with exact five-value `CHECK` constraints:

```sql
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    mission_id TEXT,
    transaction_id TEXT,
    requested_outcome_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
      'verified','completed_unverified','failed','blocked','unknown_effect')),
    claims_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    uncertainty_json TEXT NOT NULL,
    scorer_id TEXT NOT NULL,
    scorer_version TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    inserted_at REAL NOT NULL,
    UNIQUE(source_kind, source_id)
);
CREATE TABLE IF NOT EXISTS receipt_observations (
    observation_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL REFERENCES receipts(receipt_id) ON DELETE CASCADE,
    previous_observation_id TEXT REFERENCES receipt_observations(observation_id),
    status TEXT NOT NULL CHECK(status IN (
      'verified','completed_unverified','failed','blocked','unknown_effect')),
    claims_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    uncertainty_json TEXT NOT NULL,
    scorer_id TEXT NOT NULL,
    scorer_version TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    inserted_at REAL NOT NULL,
    UNIQUE(receipt_id, previous_observation_id, content_hash)
);
CREATE TABLE IF NOT EXISTS receipt_attestations (
    attestation_id TEXT PRIMARY KEY,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('receipt','observation')),
    target_id TEXT NOT NULL,
    target_content_hash TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    signature_b64 TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    verification_state TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS receipt_deletion_tombstones (
    receipt_id TEXT PRIMARY KEY,
    receipt_content_hash TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE
);
```

Create indexes for `(subject_kind, subject_id)`, `(status, decided_at)`, `(session_id, turn_id)`, `mission_id`, `transaction_id`, and observation order. SQLite triggers abort `UPDATE` on receipts, observations, and attestations. Deletion is available only to the retention service in one transaction that first appends a tombstone.

- [ ] **Step 4: Implement atomic v1 migration**

Inside `SessionDB._init_schema()`, inspect `PRAGMA table_info(receipts)`. When the old `objective/constraints_json/execution_ids_json/transaction_ids_json/before_after_json/verifier_json/freshness_json/signature_json/created_at` shape exists, run one `BEGIN IMMEDIATE` migration:

1. rename both old tables to `_receipt_v1_*`;
2. create canonical tables;
3. convert each row to immutable values while preserving `receipt_id`, mission/transaction lineage, original fields as evidence, and old hash as `legacy_content_hash` evidence;
4. map old `verified` to `completed_unverified` with explicit uncertainty; preserve the other four statuses;
5. import `signature_json` as `verification_state="unverified_import"` attestation without affecting status;
6. chain observations by old `(receipt_id, created_at, observation_id)` order;
7. validate row counts, foreign keys, source uniqueness, and recomputed hashes;
8. drop the renamed tables only after validation and record migration version/result in `state_meta`.

Any exception rolls back to the original tables. A restarted process detects either the untouched v1 shape or completed canonical shape; there is no half-migrated state.

- [ ] **Step 5: Implement typed immutable store operations**

`ReceiptStore.insert()` recomputes every nested hash and the receipt hash, validates traceability/no dangling IDs, validates the verified seal when required, and inserts receipt plus source identity atomically. Identical source/content replay returns the stored object. `append_observation()` requires the latest observation ID as predecessor (or `None` for first), uses CAS to prevent forks, and applies the same verified-seal rules. All query JSON is decoded through Task 1 constructors.

- [ ] **Step 6: Run GREEN and schema regressions**

Run: `scripts/run_tests.sh tests/agent/test_receipt_store.py tests/agent/test_receipt_migration.py tests/test_hermes_state.py tests/test_hermes_state_wal_fallback.py -q`

Expected: PASS for clean v21 creation, v1 atomic migration, replay/concurrency, immutability, and WAL fallback.

- [ ] **Step 7: Commit**

```bash
git add hermes_state.py agent/receipt_store.py tests/agent/test_receipt_store.py \
  tests/agent/test_receipt_migration.py tests/test_hermes_state.py \
  tests/test_hermes_state_wal_fallback.py
git commit -m "feat: persist and migrate immutable receipts"
```

---

### Task 3: Catalog and Recheck Artifact Digests Without Duplicating Bytes

**Files:**
- Create: `agent/receipt_artifacts.py`
- Modify: `hermes_state.py`
- Modify: `tools/code_execution_tool.py`
- Create: `tests/agent/test_receipt_artifacts.py`
- Modify: `tests/tools/test_code_execution.py`
- Modify: `tests/tools/test_code_execution_modes.py`

**Interfaces:**
- Consumes Task 1 `ArtifactDigest`/`canonical_content_hash` and Task 2 low-level storage.
- Produces `ArtifactCatalog.register_path()`, `ArtifactCatalog.register_bytes()`, `ArtifactCatalog.recheck()`, and public `digest_artifact()`; tool results may add `artifact_id`, `artifact_sha256`, and `artifact_content_hash` but model-visible tool definitions do not change.

- [ ] **Step 1: Write RED content-addressing and race/security tests**

```python
def test_same_bytes_reuse_digest_but_keep_source_links(catalog, tmp_path):
    a = tmp_path / "a.txt"; b = tmp_path / "b.txt"
    a.write_bytes(b"proof"); b.write_bytes(b"proof")
    first = catalog.register_path(a, source_kind="execute_code", source_ref="call-a",
                                  allowed_roots=(tmp_path,))
    second = catalog.register_path(b, source_kind="mission", source_ref="m1:artifact",
                                   allowed_roots=(tmp_path,))
    assert first.artifact_id == second.artifact_id
    assert catalog.location_count(first.artifact_id) == 2


def test_recheck_detects_symlink_swap_and_never_reads_outside_root(catalog, root, secret):
    link = root / "report.txt"
    link.symlink_to(secret)
    with pytest.raises(ArtifactBoundaryError):
        catalog.register_path(link, source_kind="test", source_ref="escape",
                              allowed_roots=(root,))
```

Cover missing artifact, changed bytes with same size/mtime, file replaced between open/stat, Windows no-follow fallback, oversized files, non-regular files, duplicate location replay, public source-ref redaction, and two profiles containing identical bytes without cross-profile lookup.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/test_receipt_artifacts.py -q`

Expected: FAIL importing `ArtifactCatalog` and because artifact tables are absent.

- [ ] **Step 3: Add digest/location tables and safe open-handle hashing**

```sql
CREATE TABLE IF NOT EXISTS artifact_digests (
    artifact_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    media_type TEXT,
    display_name TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS artifact_locations (
    location_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifact_digests(artifact_id),
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    locator_json TEXT NOT NULL,
    locator_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    UNIQUE(source_kind, source_ref, locator_hash)
);
```

Resolve and boundary-check parent directories, open with `O_NOFOLLOW` where available, confirm regular-file identity with `fstat`, stream SHA-256 from that handle, and re-check inode/file-index plus size after hashing. On platforms without a safe identity primitive, return ambiguity rather than claiming a stable digest if the path changes during capture.

- [ ] **Step 4: Wire existing code-execution artifacts through the catalog**

Extend internal `CodeExecutionContext` with optional `turn_id` and `tool_call_id`, propagate them from the handler, and pass context to `_attach_execute_artifacts()`. Register only already-bounded durable destinations after fsync:

```python
digest = ArtifactCatalog.for_profile().register_path(
    Path(artifact_path),
    source_kind="execute_code",
    source_ref=f"{context.session_id or ''}:{context.turn_id or ''}:{context.tool_call_id or ''}",
    allowed_roots=(Path(_artifact_storage_dir()),),
)
result.update({
    "artifact_id": digest.artifact_id,
    "artifact_sha256": digest.sha256,
    "artifact_content_hash": digest.content_hash,
})
```

Registration failure leaves the current artifact path/result usable but adds `artifact_digest_error` and prevents later verified claims from citing it. Do not add or alter tool parameters, descriptions, or definitions.

- [ ] **Step 5: Run GREEN and tool-schema regression**

Run: `scripts/run_tests.sh tests/agent/test_receipt_artifacts.py tests/tools/test_code_execution.py tests/tools/test_code_execution_modes.py tests/test_get_tool_definitions_cache_isolation.py -q`

Expected: PASS; artifact bytes are stored once, source links are deduplicated, tampering is detected, and tool-definition hashes remain unchanged.

- [ ] **Step 6: Commit**

```bash
git add agent/receipt_artifacts.py hermes_state.py tools/code_execution_tool.py \
  tests/agent/test_receipt_artifacts.py tests/tools/test_code_execution.py \
  tests/tools/test_code_execution_modes.py
git commit -m "feat: catalog receipt artifact digests"
```

---

### Task 4: Build Deduplicated Evidence Snapshots from Existing Truth Sources

**Files:**
- Create: `agent/receipt_ingest.py`
- Modify: `agent/turn_ledger.py`
- Modify: `agent/verification_evidence.py`
- Modify: `agent/operation_journal.py`
- Modify (vertical-slice prerequisite): `hermes_cli/missions_db.py`
- Modify (vertical-slice prerequisite): `agent/effect_transactions.py`
- Create: `tests/agent/test_receipt_ingest.py`
- Modify: `tests/agent/test_turn_ledger.py`
- Modify: `tests/agent/test_verification_evidence.py`
- Modify: `tests/agent/test_operation_journal.py`

**Interfaces:**
- Consumes Task 1 immutable `ReceiptEnvelope`/`EvidenceSnapshot`, Task 2 source lookup, Task 3 artifact catalog, `TurnOutcomeRecord`, `verification_status()`, `OperationJournal`, and vertical-slice mission/effect rows.
- Produces `TurnEvidenceSource.snapshot(session_id, turn_id)`, `MissionEvidenceSource.snapshot(mission_id)`, `TransactionEvidenceSource.snapshot(transaction_id)`, and `ReceiptIngestor.issue(source)`/`recover_projection(source)` for Task 5 scoring and Task 10 recovery.

- [ ] **Step 1: Write RED source-lineage, dedupe, and untrusted-turn tests**

```python
def test_turn_verified_label_is_not_receipt_verification(turn_source, stale_evidence):
    turn_source.db.record_turn_outcome(record(outcome="verified"))
    snapshot = turn_source.snapshot("s1", "t1")
    assert snapshot.source == ReceiptSourceKey("turn", "s1:t1")
    assert snapshot.producer_id == "hermes.turn-ledger"
    assert snapshot.claim("turn-completed").verdict == "satisfied"
    assert snapshot.claim("requested-end-state").verdict == "unknown"
    assert stale_evidence.evidence_id in snapshot.claim("requested-end-state").evidence_ids


def test_mission_and_transaction_share_artifact_digest_without_duplicate(
    mission_source, transaction_source, artifact_catalog,
):
    mission = mission_source.snapshot("m1")
    transaction = transaction_source.snapshot("tx1")
    assert mission.artifacts[0].artifact_id == transaction.artifacts[0].artifact_id
    assert artifact_catalog.digest_count() == 1
```

Cover missing mission/transaction rows, cross-profile IDs, unknown operation disposition, absent verification DB, stale verification, outbox ambiguity, before/after evidence, constraints, mission step IDs, transaction/revision/graph hashes, duplicate execution/operation/artifact references, and source re-read after restart.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/test_receipt_ingest.py tests/agent/test_turn_ledger.py tests/agent/test_verification_evidence.py tests/agent/test_operation_journal.py -q`

Expected: FAIL importing the three evidence sources and immutable snapshot builder.

- [ ] **Step 3: Implement a normalized read-only evidence envelope**

```python
@dataclass(frozen=True)
class EvidenceSnapshot:
    source: ReceiptSourceKey
    subject_kind: str
    subject_id: str
    producer_id: str
    requested_outcome: RequestedOutcome
    claims: tuple[ReceiptClaim, ...]
    evidence: tuple[EvidenceDigest, ...]
    artifacts: tuple[ArtifactDigest, ...]
    operation_states: tuple[OperationEvidence, ...]
    blocked_reasons: tuple[str, ...]
    known_failures: tuple[str, ...]
    uncertainty: tuple[str, ...]
    captured_at: str
    content_hash: str
```

The builder sorts by stable IDs, collapses identical evidence/artifact hashes, rejects conflicting duplicates, and validates every claimed effect has at least one existing evidence ID. “No evidence” is itself a durable evidence digest with kind `absence_observed`, source, timestamp, and scope; it is never represented by a dangling reference.

- [ ] **Step 4: Implement exact source adapters**

- `TurnEvidenceSource` reads `turn_outcomes`, matching `messages`/tool-call IDs, `agent_operations`, and each edited root's `verification_status()`. The existing ledger result is recorded as `turn_classification` evidence. It does not automatically satisfy the requested end state.
- `MissionEvidenceSource` reads immutable mission intent/constraints, execution links, step/event history, review/blocked state, associated effect transactions/operations/outbox, and artifact IDs. It never copies workflow retry/node state into receipt tables.
- `TransactionEvidenceSource` reads transaction/revision/preview/authority hashes when item #2 tables exist and falls back to vertical-slice `effect_transactions` during migration. It never creates a second effect journal and treats any unknown journal/dispatch state as uncertainty.
- Every adapter is read-only, profile-bound, and returns the same snapshot hash for the same durable facts regardless of row order.

- [ ] **Step 5: Add consumer projection recovery without duplicate issuance**

`ReceiptIngestor.issue(source)` first computes the snapshot, then checks `ReceiptStore.find_by_source()`. Identical source/snapshot returns the existing receipt after Task 5 scores it; changed content for a terminal source is a conflict and must become a recheck observation, not a replacement receipt. `recover_projection()` looks up the receipt by source and CAS-links its ID into the mission/transaction projection if those columns exist. A crash before projection therefore reuses the inserted receipt.

- [ ] **Step 6: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_receipt_ingest.py tests/agent/test_turn_ledger.py tests/agent/test_verification_evidence.py tests/agent/test_operation_journal.py -q`

Expected: PASS; source snapshots are deterministic, profile-local, traceable, and do not duplicate source rows or artifact bytes.

- [ ] **Step 7: Commit**

```bash
git add agent/receipt_ingest.py agent/turn_ledger.py agent/verification_evidence.py \
  agent/operation_journal.py tests/agent/test_receipt_ingest.py \
  tests/agent/test_turn_ledger.py tests/agent/test_verification_evidence.py \
  tests/agent/test_operation_journal.py
git add hermes_cli/missions_db.py agent/effect_transactions.py
git commit -m "feat: ingest receipt evidence sources"
```

---

### Task 5: Enforce Independent End-State Scoring and Status Precedence

**Files:**
- Create: `agent/receipt_scoring.py`
- Modify: `agent/receipts.py`
- Create: `tests/agent/test_receipt_scoring.py`
- Modify: `tests/benchmarks/test_receipt_benchmark.py`

**Interfaces:**
- Consumes Task 4 `EvidenceSnapshot` and Task 0 case expectations.
- Produces `EndStateScorer`, `ScorerRegistry.register()`, `ReceiptScoringService.decide(snapshot, scorer_id=None) -> ReceiptDecision | VerifiedReceiptDecision`, and built-in `CodeTurnEndStateScorer`, `MissionEndStateScorer`, and `TransactionEndStateScorer`.

- [ ] **Step 1: Write RED independence, appropriateness, precedence, and 50-case tests**

```python
@pytest.mark.parametrize(("facts", "status"), [
    ({"unknown_effect": True, "known_failure": True}, "unknown_effect"),
    ({"known_failure": True, "blocked": True}, "failed"),
    ({"blocked": True}, "blocked"),
    ({"completed": True, "verification": "missing"}, "completed_unverified"),
])
def test_nonverified_precedence(scoring, snapshot_factory, facts, status):
    assert scoring.decide(snapshot_factory(**facts)).status == status


def test_self_scorer_and_wrong_domain_cannot_verify(registry, code_snapshot):
    registry.register(FakePassingScorer(
        scorer_id=code_snapshot.producer_id, supported_outcomes=("code_change",)
    ))
    with pytest.raises(ScorerIndependenceError):
        registry.decide(code_snapshot)
    with pytest.raises(InappropriateScorerError):
        registry.decide(code_snapshot, scorer_id="hermes.delivery-end-state")


@pytest.mark.parametrize("case", RECEIPT_CASES, ids=lambda c: c.case_id)
def test_seeded_false_success_never_verifies(case, receipt_case_harness):
    result = receipt_case_harness.score(case)
    assert result.status != "verified"
    assert result.status == case.expected_status
```

Also assert a fresh independent passing scorer can verify; expired `fresh_until`, missing required claim, empty evidence refs, artifact mismatch, ambiguous grader, unknown operation, and forged attestation cannot. Attempt to construct or pickle/rebuild a `VerifiedReceiptDecision` without the registry capability must fail store validation.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/test_receipt_scoring.py tests/benchmarks/test_receipt_benchmark.py -q`

Expected: FAIL importing scorer registry/service and because false-success cases cannot run.

- [ ] **Step 3: Implement status resolution before scorer selection**

The fixed terminal precedence is:

1. any operation/effect/delivery whose landing is ambiguous → `unknown_effect`;
2. any known failed required claim/effect or artifact hash mismatch → `failed`;
3. authority/review/dependency/provider prevention before an ambiguous effect → `blocked`;
4. completed work missing, stale, inappropriate, self-authored, or inconclusive verification → `completed_unverified`;
5. `verified` only after all safety checks and an independent appropriate scorer passes.

Consumers cannot override this precedence. A scorer receives only immutable persisted facts and cannot run a mutating effect.

- [ ] **Step 4: Seal verified decisions inside the scoring service**

```python
class EndStateScorer(Protocol):
    scorer_id: str
    scorer_version: str
    supported_outcome_kinds: frozenset[str]
    def evaluate(self, snapshot: EvidenceSnapshot) -> ScorerEvaluation: ...


class ReceiptScoringService:
    def decide(self, snapshot: EvidenceSnapshot,
               scorer_id: str | None = None) -> ReceiptDecision | VerifiedReceiptDecision:
        early = _nonverified_precedence(snapshot)
        if early is not None:
            return early
        scorer = self.registry.resolve(snapshot.requested_outcome.outcome_kind, scorer_id)
        _require_independent(snapshot.producer_id, scorer.scorer_id)
        evaluation = scorer.evaluate(snapshot)
        if not evaluation.passed or evaluation.ambiguous:
            return _completed_unverified(evaluation)
        return _seal_verified(self._capability, snapshot, scorer, evaluation)
```

The private seal binds exact subject/source, snapshot hash, required claim hashes, scorer identity/version, decision time, and freshness. Registry registration rejects an empty supported-domain set, duplicate scorer ID/version, scorers with mutation methods, or a scorer whose producer ID equals the source producer.

- [ ] **Step 5: Implement three narrow built-in scorers**

- `CodeTurnEndStateScorer` supports `code_change`; it requires fresh passed verification after the latest edit for the exact workspace root, requested path claims satisfied, every cited artifact present/hash-matched, and no unknown operation.
- `MissionEndStateScorer` supports only mission-declared outcome kinds/check IDs, reloads every required end-state check, requires all required effects settled and evidence fresh, and treats an unknown check name as blocked at mission creation rather than guessing.
- `TransactionEndStateScorer` supports `transaction_commit` and `transaction_compensation`; it requires exact revision/graph/preview lineage, fresh authority facts, settled operation dispositions, adapter postcondition evidence, exact-compensation checks when claimed, and outbox confirmation where required.

None trusts a model-authored summary, existing receipt status, legacy signature, or turn outcome.

- [ ] **Step 6: Run GREEN with the exact 50 seeds**

Run: `scripts/run_tests.sh tests/agent/test_receipt_scoring.py tests/benchmarks/test_receipt_benchmark.py -q`

Expected: PASS for all scorer security tests and 50/50 seeded cases with zero `verified`.

- [ ] **Step 7: Commit**

```bash
git add agent/receipt_scoring.py agent/receipts.py \
  tests/agent/test_receipt_scoring.py tests/benchmarks/test_receipt_benchmark.py
git commit -m "feat: score receipt end states independently"
```

---

### Task 6: Issue Receipts and Append Observation-Only Rechecks

**Files:**
- Modify: `agent/receipt_ingest.py`
- Modify: `agent/turn_ledger.py`
- Modify: `agent/turn_finalizer.py`
- Modify: `agent/codex_runtime.py`
- Modify (vertical-slice prerequisite): `hermes_cli/missions_db.py`
- Modify (vertical-slice prerequisite): `agent/effect_transactions.py`
- Modify: `tests/agent/test_receipt_ingest.py`
- Modify: `tests/agent/test_turn_ledger_e2e.py`
- Modify: `tests/agent/test_turn_finalizer_interrupt_alternation.py`

**Interfaces:**
- Consumes Tasks 2, 4, and 5 stores/sources/scoring.
- Produces `ReceiptIssuer.issue(source: ReceiptSourceKey) -> Receipt`, `ReceiptIssuer.recheck(receipt_id: str) -> ReceiptObservation`, and crash-safe mission/transaction/turn integration. Portfolio item #2's `TransactionReceiptBuilder.issue()`/`.recheck()` delegates to these methods.

- [ ] **Step 1: Write RED issue/recheck/crash-projection tests**

```python
def test_recheck_appends_and_never_rewrites_original(receipt_issuer):
    original = receipt_issuer.issue(ReceiptSourceKey("mission", "m1"))
    receipt_issuer.fixture.revert_artifact()
    observation = receipt_issuer.recheck(original.receipt_id)
    assert observation.receipt_id == original.receipt_id
    assert observation.previous_observation_id is None
    assert observation.status == "failed"
    assert receipt_issuer.store.get(original.receipt_id) == original


def test_crash_after_insert_before_mission_projection_reuses_receipt(harness):
    with pytest.raises(InjectedCrash):
        harness.issue_mission(crash_at="after_receipt_insert")
    recovered = harness.reopen().recover_mission("m1")
    assert recovered.receipt_count == 1
    assert recovered.mission.receipt_id == recovered.receipt.receipt_id
```

Cover second/third rechecks chaining predecessor IDs, concurrent recheck CAS conflict/retry, recheck of missing/retained artifact locator, turn ledger labeled verified with stale evidence, operation ambiguity after original verification, and recheck code attempting a mutating adapter method.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/test_receipt_ingest.py tests/agent/test_turn_ledger_e2e.py tests/agent/test_turn_finalizer_interrupt_alternation.py -q`

Expected: FAIL because issuer, recheck, and finalizer integration are absent.

- [ ] **Step 3: Implement deterministic issue and recheck**

`issue()` builds a fresh persisted snapshot, asks `ReceiptScoringService`, constructs the deterministic receipt, inserts with a verified seal only when returned, then projects its ID. `recheck()` reloads the immutable original requested outcome and source adapter, runs only `inspect/status/reconcile_read_only/hash` methods, scores current facts, and appends a new observation with the current predecessor. It records evidence changes in claims/uncertainty while using only the canonical five status values.

```python
def recheck(self, receipt_id: str) -> ReceiptObservation:
    original = self.store.require(receipt_id)
    snapshot = self.sources.for_key(original.source).snapshot_for_recheck(original)
    decision = self.scoring.decide(snapshot, scorer_id=original.scorer_id or None)
    observation = build_observation(original, self.store.latest_observation(receipt_id),
                                    snapshot, decision)
    return self.store.append_observation(
        observation,
        decision=decision if isinstance(decision, VerifiedReceiptDecision) else None,
    )
```

- [ ] **Step 4: Wire all producers without creating duplicate receipt logic**

- Turn finalizers call one `record_turn_outcome_and_receipt()` seam after building the raw record. With `receipts.mode == "capture"`, ledger persistence remains best-effort, receipt failure is logged, and no verified receipt is exposed. With `mode == "require"` for an explicitly receipt-required turn, a receipt-store failure changes only the receipt projection to `completed_unverified`; it never fabricates a user/system message.
- Mission terminalization inserts the receipt before terminal verdict projection. Restart reconciliation calls `recover_projection()`.
- Vertical-slice effect transactions call `ReceiptIssuer` with `ReceiptSourceKey("transaction", transaction_id)`.
- Item #2's later `agent/effects/receipts.py` `TransactionReceiptBuilder` adds transaction claims to the snapshot, then delegates insertion/recheck to the shared issuer and never accesses receipt SQL directly; this plan freezes that consumed interface but does not edit the later item #2 implementation.

- [ ] **Step 5: Preserve conversation/cache invariants in runtime tests**

Hash the system message, effective tool definitions, provider, model, and normalized role sequence immediately before and after receipt issue/recheck. Assert no new message is appended by the receipt path, no cached prompt field is reset, and only existing compression may change prior history.

- [ ] **Step 6: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_receipt_ingest.py tests/agent/test_turn_ledger_e2e.py tests/agent/test_turn_finalizer_interrupt_alternation.py tests/test_get_tool_definitions_cache_isolation.py -q`

Expected: PASS; issuance/recheck are idempotent, original receipts remain byte-identical, projections recover after crash, and prompt/tool/model/role invariants hold.

- [ ] **Step 7: Commit**

```bash
git add agent/receipt_ingest.py agent/turn_ledger.py agent/turn_finalizer.py \
  agent/codex_runtime.py tests/agent/test_receipt_ingest.py \
  tests/agent/test_turn_ledger_e2e.py \
  tests/agent/test_turn_finalizer_interrupt_alternation.py
git add hermes_cli/missions_db.py agent/effect_transactions.py
git commit -m "feat: issue and recheck outcome receipts"
```

---

### Task 7: Add Redacted Export, Retention Tombstones, and Service-Gated Signing

**Files:**
- Create: `agent/receipt_security.py`
- Modify: `agent/receipt_store.py`
- Modify: `agent/receipts.py`
- Modify: `hermes_cli/config.py`
- Create: `tests/agent/test_receipt_security.py`
- Modify: `tests/hermes_cli/test_config_validation.py`

**Interfaces:**
- Consumes immutable receipt/observation hashes, artifact catalog, and Task 2 attestation/tombstone tables.
- Produces `ReceiptRedactor`, `ReceiptExporter.export()`, `ReceiptRetentionService.plan/prune()`, `ReceiptSigner`, `register_receipt_signer(provider_id, factory, check_fn)`, and `ReceiptSigningService.sign/verify()`.

- [ ] **Step 1: Write RED secret, forgery, traversal, retention, and signer-gate tests**

```python
def test_public_export_contains_no_secret_or_raw_locator(security_harness):
    exported = security_harness.export_public()
    text = exported.read_text("utf-8")
    assert "sk-live-secret" not in text
    assert str(security_harness.home) not in text
    assert "artifact_locations" not in text
    assert verify_export_hashes(exported)


def test_valid_signature_never_promotes_unverified(signing_service, completed_receipt):
    attestation = signing_service.sign(completed_receipt)
    assert signing_service.verify(attestation).valid
    assert signing_service.store.get(completed_receipt.receipt_id).status == "completed_unverified"


def test_unconfigured_signer_is_not_loaded(signer_factory, config):
    config["receipts"]["signing"] = {"provider": "", "required": False}
    ReceiptSigningService.from_config(config)
    assert signer_factory.calls == 0
```

Cover forged signature bytes, swapped target hash, replayed attestation for another receipt, unavailable provider optional/required behavior, plugin `check_fn=False`, malicious archive names, symlink export race, JSON formula-like strings, public/local modes, deletion hold, observation retention, idempotent prune, and profile boundary.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/agent/test_receipt_security.py tests/hermes_cli/test_config_validation.py -q`

Expected: FAIL because security/export/retention/signing services and config are absent.

- [ ] **Step 3: Add exact safe config and validation**

```yaml
receipts:
  mode: off
  retention_days: 365
  artifact_locator_retention_days: 90
  export_redaction: public
  signing:
    provider: ""
    required: false
```

Accept `mode` only as `off|capture|require`, retention days as `1..3650`, locator days as `1..retention_days`, export redaction as `public|local`, provider as a bounded identifier, and required as boolean. No environment-variable bridge is added. Provider credentials are resolved by the provider from existing secret infrastructure.

- [ ] **Step 4: Redact before persist/export and create safe bundles**

Redaction recursively replaces credential-like keys, bearer tokens, URL userinfo/query secrets, message bodies not explicitly declared evidence, home/profile prefixes, and configured sensitive roots. Public export contains canonical receipt/observations/attestations plus hash-verification instructions and no raw locators. Local export may include profile-relative locators after boundary checks. Bundle artifact names are generated from `artifact_id + sanitized extension`; never trust display names as paths. Reopen/hash the artifact while copying and fail the bundle on mismatch.

- [ ] **Step 5: Implement append-only provenance attestations**

```python
class ReceiptSigner(Protocol):
    provider_id: str
    def sign(self, content_hash: str) -> SignatureMaterial: ...
    def verify(self, content_hash: str, material: SignatureMaterial) -> bool: ...


def register_receipt_signer(provider_id: str, factory: SignerFactory,
                            check_fn: Callable[[dict], bool]) -> None: ...
```

The registry loads no provider until config names it and `check_fn(config)` passes. `ReceiptSigningService` signs only the existing canonical target hash and appends an immutable attestation. Optional signing failure leaves the truthful unsigned receipt plus an operator warning; required signing prevents signed export/consumer projection but cannot change receipt status. Imported/external signatures start `unverified_import` and require explicit verification.

- [ ] **Step 6: Implement explicit retention with tombstones and holds**

`plan(now)` returns exact receipt/observation/attestation/artifact-location IDs and blockers. `prune(plan_id, expected_hash)` revalidates the plan, refuses active mission/transaction/legal/user holds, deletes expired raw artifact locators before receipt rows, and atomically inserts deletion tombstones containing source identity and old content hash. It never runs implicitly during a live turn and never deletes artifact bytes outside the configured receipt artifact directory.

- [ ] **Step 7: Run GREEN**

Run: `scripts/run_tests.sh tests/agent/test_receipt_security.py tests/hermes_cli/test_config_validation.py tests/agent/test_receipt_store.py -q`

Expected: PASS; exports redact and verify, signers are gated, valid signatures do not alter truth, and retention is bounded/replay-safe.

- [ ] **Step 8: Commit**

```bash
git add agent/receipt_security.py agent/receipt_store.py agent/receipts.py \
  hermes_cli/config.py tests/agent/test_receipt_security.py \
  tests/hermes_cli/test_config_validation.py
git commit -m "feat: secure receipt export and retention"
```

---

### Task 8: Deliver the Top-Level and Classic CLI Receipt Viewer

**Files:**
- Create: `hermes_cli/receipts.py`
- Modify: `hermes_cli/commands.py`
- Modify: `hermes_cli/main.py`
- Modify: `hermes_cli/cli_commands_mixin.py`
- Modify: `cli.py`
- Create: `tests/hermes_cli/test_receipt_cli.py`
- Modify: `tests/hermes_cli/test_commands.py`

**Interfaces:**
- Consumes `ReceiptStore`, `ReceiptIssuer`, `ReceiptExporter`, `ReceiptRetentionService`, and `ReceiptSigningService`.
- Produces `build_parser(subparsers)`, `run_argv(argv: Sequence[str], *, output: Literal["text", "json"] = "text") -> ReceiptCommandResult`, top-level `hermes receipt`, alias `hermes receipts`, and classic `/receipt`/`/receipts`.

- [ ] **Step 1: Write RED parser, truthful-rendering, and safety tests**

```python
def test_show_distinguishes_original_from_latest_observation(cli, receipt_with_drift):
    result = cli.run(["show", receipt_with_drift.receipt_id])
    assert result.exit_code == 0
    assert "Original: verified" in result.stdout
    assert "Latest recheck: failed" in result.stdout
    assert "Artifact hash changed" in result.stdout


def test_export_defaults_public_and_prune_requires_exact_plan(cli, receipt):
    exported = cli.run(["export", receipt.receipt_id, "--output", "receipt.json"])
    assert exported.exit_code == 0
    assert cli.home.as_posix() not in Path("receipt.json").read_text("utf-8")
    refused = cli.run(["prune", "--confirm-plan", "wrong"])
    assert refused.exit_code == 2
```

Cover `list`, `show`, `claims`, `recheck`, `export`, `verify-signature`, `retention-plan`, `prune`, `--json`, filters, unknown ID, disabled capture with read access, signing unavailable, output-path boundary, no traceback/secret leakage, and classic/top-level parser parity.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_receipt_cli.py -q`

Expected: FAIL importing `hermes_cli.receipts` and resolving the command.

- [ ] **Step 3: Implement the shared command service**

Exact grammar:

```text
hermes receipt list [--status STATUS] [--subject KIND] [--limit N] [--json]
hermes receipt show RECEIPT_ID [--observation latest|all|OBS_ID] [--json]
hermes receipt claims RECEIPT_ID [--json]
hermes receipt recheck RECEIPT_ID [--json]
hermes receipt export RECEIPT_ID --output PATH [--redaction public|local]
                       [--bundle-artifacts] [--sign]
hermes receipt verify-signature RECEIPT_ID [--json]
hermes receipt retention-plan [--at RFC3339] [--json]
hermes receipt prune --confirm-plan PLAN_HASH [--json]
```

`run_argv` validates at most 64 UTF-8 arguments and 64 KiB total, opens the active profile's `SessionDB`, and returns structured records independent of Rich. Text rendering shows requested outcome, original/current status, scorer/freshness, every claim→evidence→artifact edge, uncertainty, attestations under the label “provenance only,” and copyable recheck/export commands. It never renders `completed_unverified` as success or `unknown_effect` as failure/retry-safe.

- [ ] **Step 4: Register top-level and classic routes once**

Add `CommandDef("receipt", ..., aliases=("receipts",), args_hint="[list|show|claims|recheck|export|verify-signature|retention-plan|prune]")`. `hermes_cli/main.py` delegates parser construction to `hermes_cli.receipts.build_parser`; `HermesCLI.process_command()` delegates the parsed tail to the same `run_argv`. No gateway messaging command or model tool is added.

- [ ] **Step 5: Run GREEN and registry regressions**

Run: `scripts/run_tests.sh tests/hermes_cli/test_receipt_cli.py tests/hermes_cli/test_commands.py -q`

Expected: PASS; top-level/classic output agrees and aliases/help/autocomplete derive from `COMMAND_REGISTRY`.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/receipts.py hermes_cli/commands.py hermes_cli/main.py \
  hermes_cli/cli_commands_mixin.py cli.py tests/hermes_cli/test_receipt_cli.py \
  tests/hermes_cli/test_commands.py
git commit -m "feat: add receipt cli viewer"
```

---

### Task 9: Route Receipt Inspection and Recheck Natively in the Ink TUI

**Files:**
- Modify: `tui_gateway/server.py`
- Modify: `ui-tui/src/gatewayTypes.ts`
- Modify: `ui-tui/src/app/slash/commands/ops.ts`
- Create: `tests/tui_gateway/test_receipt_rpc.py`
- Create: `ui-tui/src/__tests__/receiptCommand.test.ts`
- Modify: `ui-tui/src/__tests__/slashParity.test.ts`
- Modify: `ui-tui/src/__tests__/createSlashHandler.test.ts`

**Interfaces:**
- Consumes Task 8 `hermes_cli.receipts.run_argv(..., output="json")`.
- Produces `receipt.exec` JSON-RPC and native Ink `/receipt`/`/receipts` rendering; no shell subprocess and no `slash.exec` fallback for receipt commands.

- [ ] **Step 1: Write RED RPC and Ink behavior tests**

```python
def test_receipt_rpc_returns_traceable_detail(rpc, seeded_receipt):
    result = rpc("receipt.exec", {
        "session_id": "sid", "argv": ["show", seeded_receipt.receipt_id]
    })
    assert result["ok"] is True
    assert result["receipt"]["content_hash"] == seeded_receipt.content_hash
    assert result["claim_edges"][0]["evidence_ids"]


def test_receipt_rpc_rejects_oversized_argv_without_secret_echo(rpc):
    result = rpc("receipt.exec", {"session_id": "sid", "argv": ["x" * 65537]})
    assert result["error"]["code"] == 4004
    assert "x" * 100 not in result["error"]["message"]
```

Vitest asserts native registration, list/detail/claims panels, original/latest status distinction, persistent unknown-effect warning, provenance-only signature label, pager behavior, stale-session guard, and no fallthrough to `slash.exec`.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/tui_gateway/test_receipt_rpc.py -q`

Expected: FAIL with unknown `receipt.exec`.

Run: `cd ui-tui && npm test -- --run src/__tests__/receiptCommand.test.ts src/__tests__/slashParity.test.ts`

Expected: FAIL because receipt is not a native ops command.

- [ ] **Step 3: Implement bounded live-process RPC**

Validate argv count/bytes, resolve the session's active profile without accepting a caller-supplied path, and call the shared command service. Return:

```typescript
interface ReceiptExecResponse {
  ok: boolean
  action: string
  receipts?: ReceiptSummary[]
  receipt?: ReceiptDetail
  observations?: ReceiptObservationDetail[]
  claim_edges?: ReceiptClaimEdge[]
  export_path?: string
  retention_plan_hash?: string
  warning?: string
}
```

Validation/conflict errors use JSON-RPC 4xxx; store/provider failures use 5xxx. No raw locator, secret, traceback, or signer material leaves the RPC.

- [ ] **Step 4: Implement native Ink rendering and mutation parity**

Add `receipt` and `receipts` to `opsCommands`. Parse with the existing slash parser, invoke `receipt.exec`, render list/show/claims as `panel`/`page`, and render recheck/export/prune as concise system results. `unknown_effect` always includes “Do not retry the effect; recheck/reconcile evidence.” Add `receipt` to native command parity because recheck/prune write observations/tombstones; typed/catalog-discovered receipt commands must never fall through to the worker.

- [ ] **Step 5: Run GREEN and typecheck**

Run: `scripts/run_tests.sh tests/tui_gateway/test_receipt_rpc.py -q`

Expected: PASS.

Run: `cd ui-tui && npm test -- --run src/__tests__/receiptCommand.test.ts src/__tests__/createSlashHandler.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS; receipt uses native RPC and TypeScript types are exact.

- [ ] **Step 6: Commit**

```bash
git add tui_gateway/server.py ui-tui/src/gatewayTypes.ts \
  ui-tui/src/app/slash/commands/ops.ts tests/tui_gateway/test_receipt_rpc.py \
  ui-tui/src/__tests__/receiptCommand.test.ts \
  ui-tui/src/__tests__/createSlashHandler.test.ts \
  ui-tui/src/__tests__/slashParity.test.ts
git commit -m "feat: add native tui receipt viewer"
```

---

### Task 10: Add Secondary Read-Only Dashboard Inspection

**Files:**
- Modify: `hermes_cli/web_server.py`
- Modify: `web/src/lib/api.ts`
- Create: `web/src/pages/ReceiptsPage.tsx`
- Create: `web/src/pages/ReceiptsPage.test.tsx`
- Modify: `web/src/App.tsx`
- Create: `tests/hermes_cli/test_receipt_dashboard.py`

**Interfaces:**
- Consumes Task 2 store query/detail and Task 7 public redaction; never calls signer, retention prune, or a mutating effect.
- Produces profile-aware `GET /api/receipts`, `GET /api/receipts/{receipt_id}`, `GET /api/receipts/{receipt_id}/observations`, typed Dashboard API methods, and `/receipts` secondary inspection page.

- [ ] **Step 1: Write RED REST auth/profile/redaction and page tests**

```python
def test_dashboard_receipt_detail_is_profile_scoped_and_redacted(client, profiles):
    response = client.get(
        f"/api/receipts/{profiles.alpha.receipt_id}",
        headers=profiles.alpha.auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["receipt_id"] == profiles.alpha.receipt_id
    assert profiles.alpha.home.as_posix() not in response.text
    denied = client.get(
        f"/api/receipts/{profiles.alpha.receipt_id}",
        headers=profiles.beta.auth_headers,
    )
    assert denied.status_code == 404
```

React tests assert status/filter list, claim→evidence expansion, artifact hashes, original/latest observation, freshness/uncertainty, provenance-only signatures, loading/empty/error states, and no recheck/prune/sign controls.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_receipt_dashboard.py -q`

Expected: FAIL with 404 routes.

Run: `cd web && npm test -- --run src/pages/ReceiptsPage.test.tsx`

Expected: FAIL importing `ReceiptsPage` and API methods.

- [ ] **Step 3: Implement read-only profile-aware endpoints**

Reuse existing dashboard auth/profile resolution and open `SessionDB` for that resolved profile only. `GET /api/receipts` accepts canonical status, subject kind, cursor, and limit `1..200`; detail/observations validate bounded IDs. Responses use `ReceiptRedactor.public_view()` and never expose `artifact_locations.locator_json`, signer secrets, deletion internals, or another profile's existence. Headless `hermes serve` may expose authenticated APIs, but Desktop has no route/client dependency.

- [ ] **Step 4: Implement the secondary inspector page**

Add `ReceiptsPage` to `BUILTIN_ROUTES_CORE` and a `Receipts` nav item with `ShieldCheck` after Sessions. Use existing API/loading/card/table primitives. The page is explicitly inspection-only and links its command hint to `hermes receipt recheck <id>` or `/receipt recheck <id>` for primary control.

- [ ] **Step 5: Run GREEN, frontend tests, and typecheck**

Run: `scripts/run_tests.sh tests/hermes_cli/test_receipt_dashboard.py tests/hermes_cli/test_dashboard_auth_gate.py tests/hermes_cli/test_dashboard_web_dist_validation.py -q`

Expected: PASS; auth/profile/headless behavior remains intact.

Run: `cd web && npm test -- --run src/pages/ReceiptsPage.test.tsx src/lib/api.test.ts && npm run typecheck`

Expected: PASS; Dashboard inspection is typed and contains no mutation controls.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/web_server.py web/src/lib/api.ts web/src/pages/ReceiptsPage.tsx \
  web/src/pages/ReceiptsPage.test.tsx web/src/App.tsx \
  tests/hermes_cli/test_receipt_dashboard.py
git commit -m "feat: inspect receipts in dashboard"
```

---

### Task 11: Prove 50 Missions Across Crash, Replay, Forgery, Staleness, and Ambiguity

**Files:**
- Create: `tests/hermes_cli/test_receipt_e2e.py`
- Create: `benchmarks/receipts/runner.py`
- Modify: `tests/benchmarks/test_receipt_benchmark.py`
- Modify: `tests/agent/test_receipt_ingest.py`
- Modify: `tests/agent/test_receipt_security.py`
- Modify: `tests/test_get_tool_definitions_cache_isolation.py`
- Modify: `tests/agent/test_turn_finalizer_interrupt_alternation.py`

**Interfaces:**
- Consumes only public receipt, mission, transaction, artifact, CLI, and scorer services plus injected `FaultHook(point, context)` and fake external signer/delivery boundaries.
- Produces `run_receipt_benchmark(manifest_path: Path, *, repeats: int, output: TextIO) -> ReceiptBenchmarkReport` and the final real-path proof report.

- [ ] **Step 1: Write the real-path E2E matrix before the runner**

```python
@pytest.mark.parametrize("fault_point", [
    "after_source_snapshot", "after_receipt_insert", "before_subject_projection",
    "after_subject_projection", "during_artifact_hash", "after_observation_insert",
])
def test_receipt_recovery_is_idempotent(receipt_e2e, fault_point):
    receipt_e2e.issue_with_subprocess_exit(fault_point)
    final = receipt_e2e.reopen_all_stores_and_reconcile()
    assert final.receipt_count == 1
    assert final.subject.receipt_id == final.receipt.receipt_id
    assert final.receipt.content_hash == final.recomputed_hash
    assert final.receipt.status != "verified" or final.independent_evidence_passed


@pytest.mark.parametrize("case", RECEIPT_CASES, ids=lambda c: c.case_id)
def test_each_seeded_mission_is_traceable_and_recheckable(receipt_e2e, case):
    result = receipt_e2e.run_case_in_fresh_process(case)
    assert result.status == case.expected_status
    assert result.status != "verified"
    assert result.claim_count == result.traceable_claim_count
    assert receipt_e2e.recheck_in_new_process(result.receipt_id).receipt_id == result.receipt_id
```

Use a temporary `HERMES_HOME`, real `state.db`, real `verification_evidence.db`, real mission/workflow records, real operation rows, real files/hashes, and real CLI service. Use the final fake platform boundary only for partial-delivery ambiguity and a fake signer only for provenance attacks.

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/hermes_cli/test_receipt_e2e.py -q`

Expected: FAIL at missing cross-module wiring or fault recovery; repair the owning module and never weaken expected classifications.

- [ ] **Step 3: Exercise exact crash/replay/security cases**

The E2E harness must prove:

- crash after receipt insert but before mission/transaction projection reuses the one source-linked row;
- identical replay returns the same receipt, conflicting replay fails, observation replay returns the same observation, and signer-attestation replay cannot target another hash;
- artifact byte, same-size byte, mtime, locator, and symlink swaps are detected from an open-handle recheck;
- stale verification/page/authority evidence cannot verify; a fresh later recheck appends a verified observation without changing the original;
- unknown operation or delivery acknowledgement dominates known failure/blocking and remains `unknown_effect` without blind retry;
- forged-looking artifact metadata, forged signature, imported legacy verified status, model “done” text, and self-scoring never verify;
- public export contains no planted secret/path canary and independently validates every content hash;
- retention refuses active holds, emits a tombstone, and makes expired locator recheck `completed_unverified` rather than inventing failure;
- profile B receives 404/no result for profile A receipt/artifact IDs.

- [ ] **Step 4: Implement the local report-only benchmark runner**

```python
@dataclass(frozen=True)
class ReceiptCaseResult:
    case_id: str
    stratum: str
    expected_status: ReceiptStatus
    actual_status: ReceiptStatus
    false_verified: bool
    claim_count: int
    traceable_claim_count: int
    independently_recheckable: bool
    baseline_latency_ms: float
    candidate_latency_ms: float
    baseline_cost_usd: float
    candidate_cost_usd: float
    excluded_reason: str | None
```

`ReceiptBenchmarkReport` reports denominator/exclusions, correct-classification rate and Wilson 95% interval overall/per stratum, false-verified count, traceability/recheckability rates, baseline/candidate p50/p95 latency and cost per verified success, plus OS/Python/SQLite/filesystem/signer/network classes. Exit nonzero on any safety stop or if correct classifications are below 45/50. Never combine safety, cost, and accuracy into one score and never upload output.

- [ ] **Step 5: Run GREEN on all 50 missions and report**

Run: `scripts/run_tests.sh tests/hermes_cli/test_receipt_e2e.py tests/benchmarks/test_receipt_benchmark.py -q`

Expected: PASS with exactly 50 collected seeded mission cases, zero false `verified`, at least 45 correct classifications, 100% claim traceability, and 100% independent recheckability.

Run: `uv run python benchmarks/receipts/runner.py --manifest benchmarks/receipts/manifest.yaml --repeats 3 --output-json build/receipt-benchmark.json`

Expected: exit 0 only when all four gates pass; output declares the current Hermes turn-outcome/prose baseline and all exclusions.

- [ ] **Step 6: Prove cache, model, tool-schema, role, and ordinary-path invariants**

Run a multi-turn receipt-enabled fixture and hash system prompt/effective tool definitions before source capture, after issue, after artifact recheck, and after observation append. Assert provider/model identity and strict role alternation. Also run a receipt-disabled ordinary turn and prove no receipt/artifact-catalog writes.

Run: `scripts/run_tests.sh tests/test_get_tool_definitions_cache_isolation.py tests/run_agent tests/agent/test_turn_finalizer_interrupt_alternation.py -q -k 'system_prompt or tool_schema or cache or alternation or receipt'`

Expected: PASS; all cache identity hashes match and ordinary disabled behavior is unchanged.

- [ ] **Step 7: Run the complete focused regression matrix**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_receipt_models.py \
  tests/agent/test_receipt_store.py \
  tests/agent/test_receipt_migration.py \
  tests/agent/test_receipt_artifacts.py \
  tests/agent/test_receipt_ingest.py \
  tests/agent/test_receipt_scoring.py \
  tests/agent/test_receipt_security.py \
  tests/agent/test_turn_ledger.py \
  tests/agent/test_turn_ledger_e2e.py \
  tests/agent/test_verification_evidence.py \
  tests/agent/test_operation_journal.py \
  tests/tools/test_code_execution.py \
  tests/hermes_cli/test_receipt_cli.py \
  tests/hermes_cli/test_receipt_e2e.py \
  tests/hermes_cli/test_receipt_dashboard.py \
  tests/tui_gateway/test_receipt_rpc.py \
  tests/benchmarks/test_receipt_benchmark.py -q
git diff --check
```

Expected: all pass and whitespace check is clean.

- [ ] **Step 8: Commit**

```bash
git add tests/hermes_cli/test_receipt_e2e.py benchmarks/receipts/runner.py \
  tests/benchmarks/test_receipt_benchmark.py tests/agent/test_receipt_ingest.py \
  tests/agent/test_receipt_security.py tests/test_get_tool_definitions_cache_isolation.py \
  tests/agent/test_turn_finalizer_interrupt_alternation.py
git commit -m "test: prove receipt truth and recovery"
```

---

### Task 12: Document the Contract, Staged Rollout, and Failure Stops

**Files:**
- Create: `website/docs/user-guide/features/outcome-receipts.md`
- Create: `website/docs/development/receipt-contract.md`
- Modify: `website/docs/reference/cli-commands.md`
- Modify: `website/docs/reference/slash-commands.md`
- Modify: `website/sidebars.ts`
- Modify: `tests/benchmarks/test_receipt_benchmark.py`

**Interfaces:**
- Consumes all proven interfaces and reports from Tasks 0-11.
- Produces operator/consumer documentation, exact rollout gates, failure/rollback conditions, and the final completion matrix; no code/API expansion.

- [ ] **Step 1: Write RED documentation-contract behavior checks**

Extend benchmark/CLI tests to assert the runtime-reported rollout metadata contains the exact 50-case denominator, zero false-verified floor, `45/50` classification floor, `50/50` traceability/recheckability floors, and stop conditions. Test `hermes receipt --help` contains all documented subcommands rather than reading source text.

```python
def test_rollout_gate_matches_preregistered_manifest(runtime_rollout, manifest):
    assert runtime_rollout.denominator == manifest.denominator == 50
    assert runtime_rollout.max_false_verified == 0
    assert runtime_rollout.min_correct_classifications == 45
    assert runtime_rollout.require_full_traceability
    assert runtime_rollout.require_full_recheckability
```

- [ ] **Step 2: Run RED**

Run: `scripts/run_tests.sh tests/benchmarks/test_receipt_benchmark.py tests/hermes_cli/test_receipt_cli.py -q`

Expected: FAIL because final rollout metadata/help contract is incomplete.

- [ ] **Step 3: Write the complete operator guide**

Document the layman outcome; one copyable issue/list/show/recheck/export flow; exact five statuses; original versus latest observation; claim→evidence→artifact traceability; freshness; missing and ambiguous evidence; artifact hashes; public/local export; retention plan/confirmation; signing provider configuration; why signature is provenance and never truth; storage paths via `display_hermes_home()`; profile isolation; and the 50-mission benchmark command/report.

State exclusions prominently: no new model tool, no exactly-once inference, no claim that compensation/reversal occurred without evidence, no automatic retry for unknown effects, no cross-profile receipt, no telemetry upload, no messaging-gateway viewer, no Desktop dependency/parity, and no Dashboard mutation controls.

- [ ] **Step 4: Write the complete consumer/scorer/signer guide**

Document every frozen `agent.receipts` public type/signature; canonical hash normalization/exclusions and protocol vector; source dedupe/conflict behavior; immutable insertion; observation chain CAS; claim/evidence/artifact reference validation; status precedence; scorer independence/appropriateness/freshness rules; sealed verified decision; mission/transaction projection recovery; read-only recheck rule; redaction boundaries; attestation semantics; signer `check_fn`; migration downgrade behavior; and required temp-`HERMES_HOME` real-path tests.

Include complete examples of a mission evidence source, a transaction claim builder consuming `ReceiptStore.insert/append_observation`, a read-only scorer, and a standalone plugin signer registration. Vendor signers remain standalone plugins; widening the scorer/source ABC requires a concrete approved consumer.

- [ ] **Step 5: Freeze staged rollout and rollback behavior**

1. Land schema/migration/read viewers with `receipts.mode: off`; migrate atomically and permit reading/exporting legacy rows.
2. Enable `capture` only on designated test profiles; issue unsigned receipts, compare current Hermes baseline, and run all 50 cases.
3. Permit configured optional signer providers after forgery/replay/redaction tests pass; keep signatures visually separated from truth status.
4. Enable `capture` broadly only after zero false verified, at least 45/50 correct classification, and 50/50 traceable/recheckable receipts, with cache/schema/role invariants green.
5. Enable `require` only for explicitly receipt-required mission/transaction flows after crash/projection recovery passes; do not make generic chat completion depend on receipt storage.
6. Stop rollout and return affected profiles to `off` on any false verified receipt, unsealed verified insert, inappropriate/self scorer, hash mismatch accepted, signature-based promotion, source replay conflict hidden, cross-profile access, secret/raw-locator public export, mutation during recheck, lost receipt after crash, or tool-schema/cache/role drift.
7. Rollback disables new capture/signing but preserves readable immutable rows/tombstones. Never downgrade the database by deleting canonical tables or restoring legacy hashes.

- [ ] **Step 6: Run GREEN and all UI/docs checks**

Run: `scripts/run_tests.sh tests/benchmarks/test_receipt_benchmark.py tests/hermes_cli/test_receipt_cli.py -q`

Expected: PASS; runtime/help and manifest gates agree.

Run: `cd ui-tui && npm test -- --run src/__tests__/receiptCommand.test.ts src/__tests__/slashParity.test.ts && npm run typecheck`

Expected: PASS.

Run: `cd web && npm test -- --run src/pages/ReceiptsPage.test.tsx src/lib/api.test.ts && npm run typecheck && npm run build`

Expected: PASS; `/receipts` builds as secondary inspection.

Run: `cd website && npm run lint:diagrams && npm run typecheck && npm run build`

Expected: PASS; operator and developer receipt pages build with resolved links.

- [ ] **Step 7: Final clean-tree verification and commit**

Run the Task 11 focused matrix, then:

```bash
git status --short
git diff --check
```

Expected: only intended receipt implementation/test/benchmark/UI/docs files are changed; no secrets, raw exports, generated databases, benchmark reports, node caches, or build artifacts are staged.

```bash
git add website/docs/user-guide/features/outcome-receipts.md \
  website/docs/development/receipt-contract.md \
  website/docs/reference/cli-commands.md website/docs/reference/slash-commands.md \
  website/sidebars.ts tests/benchmarks/test_receipt_benchmark.py
git commit -m "docs: roll out verified receipts safely"
```

---

## Completion Gate

Do not call Verified Outcome & Artifact Receipts complete until fresh evidence proves every item:

- `agent.receipts` exports the frozen five-value status contract, immutable requested outcome/claim/evidence/artifact/receipt/observation types, `ReceiptStore.insert/append_observation`, scorer-only `VerifiedReceiptDecision`, and canonical content hashes exactly as specified.
- Clean v21 state and provisional vertical-slice receipt tables both migrate atomically; IDs/lineage survive, hashes are recomputed, legacy signatures remain provenance-only, and legacy verified rows require a current independent recheck.
- Turn, mission, transaction, operation, verification, and artifact facts ingest without copying their source state machines or duplicating source/artifact rows.
- Only an independent appropriate fresh end-state scorer can seal `verified`; turn/workflow/model success, artifact existence, and signatures alone cannot.
- Every receipt and observation is immutable; every recheck is linked, read-only, independently runnable after restart, and leaves the original/subject terminal status unchanged.
- Crash between source snapshot, receipt insert, subject projection, artifact hashing, or observation insert recovers without lost or duplicate receipts/effects/observations.
- Replay conflicts, forgeries, stale evidence, symlink/path swaps, ambiguous delivery/effect state, redaction canaries, retention holds, and cross-profile access all fail safely and truthfully.
- Exactly 50 seeded false-success missions run: zero seeded failures are verified, at least 45 are correctly classified, every claimed effect resolves to durable evidence, and every receipt is independently recheckable.
- Local proof reports publish denominator, exclusions, Wilson intervals, p50/p95 latency, cost per verified success, and safety slices separately against current Hermes behavior.
- CLI and native Ink TUI are complete primary viewers/controllers; Dashboard is secondary read-only inspection; Desktop and messaging-gateway parity are not dependencies.
- Optional signing is loaded only through its config/service gate or standalone plugin, appends provenance attestations over content hashes, and never changes truth status.
- Public export contains no raw locator/secret canary, retention is explicit and tombstoned, profiles stay isolated, and no telemetry leaves the machine.
- System prompt, effective tool schema, provider, model, role alternation, compression-only history mutation, and ordinary receipt-disabled behavior remain unchanged.
- Focused Python, Ink, Dashboard, and documentation suites pass from a clean checkout with `git diff --check` clean.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-verified-outcome-artifact-receipts.md`. Two execution options:

1. **Subagent-Driven (recommended)** — use `superpowers:subagent-driven-development`, one fresh implementation subagent per task with specification and quality review between tasks.
2. **Inline Execution** — use `superpowers:executing-plans`, execute task batches with explicit checkpoints.
