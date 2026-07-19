---
title: "Receipt Contract"
description: "The frozen agent.receipts consumer, scorer, and signer contract — types, hashing, storage, sealing, and plugin seams"
---

# The Receipt Contract (`agent.receipts`)

`agent.receipts` is the **one public receipt contract**. Missions,
transactions, experience, team, federation, and commerce code consume it and
may add domain claims only — they never own receipt schema, status
resolution, hashing, or observations. Import receipt names only from
`agent.receipts`; the sibling `agent.receipt_*` modules are implementation.

This page freezes the consumer, scorer, and signer surface. Widening the
scorer or evidence-source ABC requires a concrete approved consumer; vendor
signers remain standalone plugins.

## Frozen public names

```python
ReceiptStatus = Literal[
    "verified", "completed_unverified", "failed", "blocked", "unknown_effect"
]
RECEIPT_STATUSES: frozenset[ReceiptStatus]  # exactly those five strings

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
        self, receipt: Receipt, *,
        decision: VerifiedReceiptDecision | None = None,
    ) -> Receipt: ...
    def append_observation(
        self, observation: ReceiptObservation, *,
        decision: VerifiedReceiptDecision | None = None,
    ) -> ReceiptObservation: ...
    def get(self, receipt_id: str) -> Receipt | None: ...
    def find_by_source(self, source: ReceiptSourceKey) -> Receipt | None: ...
    def list(self, query: ReceiptQuery) -> list[ReceiptSummary]: ...

def canonical_content_hash(value: object) -> str: ...
def digest_artifact(path: Path, *, source_kind: str, source_ref: str,
                    allowed_roots: tuple[Path, ...]) -> ArtifactDigest: ...
```

`agent.receipts` additionally exports `ReceiptQuery`, `ReceiptSummary`,
`EndStateScorer`, `ReceiptScoringService`, `ReceiptIssuer`, `ReceiptSigner`,
`ReceiptRedactor`, `ReceiptExporter`, `ReceiptRetentionService`,
`ReceiptSigningService`, `register_receipt_signer`, and
`verify_export_hashes`.

## Canonical hashing

`canonical_content_hash()` is the one byte encoding behind every content
hash:

- UTF-8 JSON, **sorted string keys**, compact separators (`,` and `:`).
- NFC-normalized strings; mapping keys must be strings.
- UTC RFC 3339 timestamps (`...Z`); naive datetimes are rejected.
- Finite decimal rendering only — NaN and infinities are rejected loudly.
- Tuples and lists both render as JSON arrays; frozen dataclasses render as
  their field mapping. Bytes, paths, and sets are rejected, never guessed.

**Protocol vector** (interoperability invariant): the canonical bytes of
`{"answer": 42}` are exactly `{"answer":42}` and hash to
`sha256:ecf59a2696ca44a417e20e2a7eabb1b26e82c779f8546bea354a2cc80e8e1eed`.

Hash inputs **exclude** `receipt_id`, `observation_id`, database
`inserted_at`, `content_hash` itself, local artifact locators, and provenance
attestations. They **include** subject/source keys, requested outcome,
status, all claim/evidence/artifact content hashes, uncertainty, scorer
identity/version, and the `decided_at`/`observed_at` freshness facts.

IDs are deterministic `rct_<64 hex>`, `obs_<64 hex>`, `clm_<64 hex>`,
`evd_<64 hex>`, and `art_<64 hex>` values derived from the corresponding
canonical hash. Migrated legacy receipt/observation IDs are the explicit
compatibility exception; they stay mapped to their recomputed canonical
hashes.

## Storage semantics

- **Source dedupe and conflict.** Ingestion is idempotent by
  `(source_kind, source_id)` and content hash. Re-issuing an identical
  source returns the existing receipt. Reusing a source identity with
  *different* content raises `SnapshotConflictError` — new facts must become
  a recheck observation, never a replacement receipt.
- **Immutable insertion.** `insert()` and `append_observation()` never
  update rows. There is no update or delete API outside the explicit,
  tombstoned retention service.
- **Observation chain CAS.** `append_observation()` validates
  `previous_observation_id` against the current latest observation
  (compare-and-swap). A stale append raises a conflict instead of forking or
  reordering the chain; replaying an identical observation returns the
  stored one.
- **Reference validation.** Every claim must cite at least one existing
  evidence digest ("no evidence" is recorded first as a durable
  `absence_observed` digest); every `evidence_ids`/`artifact_ids` entry must
  resolve within the same receipt or observation. Dangling references are
  rejected at build time.
- **Sealed verified decisions.** `status == "verified"` is rejected unless
  accompanied by a `VerifiedReceiptDecision` whose subject, snapshot hash,
  exact claim hashes, scorer identity, freshness, and decision fields all
  match. `VerifiedReceiptDecision` has `init=False`; only
  `ReceiptScoringService` holds the module-private capability that
  constructs it. Non-verified decisions use the ordinary immutable internal
  `ReceiptDecision` and never receive a seal.
- **Projection recovery.** Mission/transaction consumers link their rows to
  a receipt with compare-and-swap after the receipt is durable. A crash
  between receipt insertion and the consumer's projection is repaired from
  the source key and content hash on the next issue/recovery pass — never by
  creating a duplicate.

## Status precedence

Fixed and not consumer-overridable, evaluated over the immutable snapshot:

1. Any ambiguous operation (`effect_disposition == "unknown"`, missing
   delivery acknowledgement) → `unknown_effect`. Unknown dominates known
   failure and blocking, and is never blind-retried.
2. Any known failure or unsatisfied required claim → `failed`.
3. Any blocked reason → `blocked`.
4. Verification safety gaps (stale evidence, missing independent check,
   post-check edits) cap the result at `completed_unverified`.
5. Only a clean snapshot reaches scorer evaluation, and only a sealed
   scorer decision yields `verified`.

A workflow/turn/transaction success label, model statement, handler return,
operation-journal row, artifact existence, user assertion, or signature alone
yields at most `completed_unverified`.

## Scorer rules

`EndStateScorer` is a read-only protocol:

```python
class EndStateScorer(Protocol):
    scorer_id: str
    scorer_version: str
    supported_outcome_kinds: frozenset[str]

    def evaluate(self, snapshot: EvidenceSnapshot) -> ScorerEvaluation: ...
```

- **Independence:** a scorer whose identity matches the snapshot's
  `producer_id` is rejected — self-scoring can never verify.
- **Appropriateness:** a scorer is resolved by the requested
  `outcome_kind`; an unavailable or inappropriate scorer cannot emit
  `verified` (the result stays `completed_unverified` with the gap named).
- **Freshness:** the scorer must decide from evidence that is current at
  decision time; stale `fresh_until` caps the status.
- **Read-only:** `evaluate()` must not mutate the subject, the store, or
  the filesystem. Rechecks reload facts (open-handle artifact hashing,
  re-read verification/operation rows) and append one linked observation.
- **Ambiguity is honest:** `ScorerEvaluation(ambiguous=True, reasons=...)`
  yields `completed_unverified` with the ambiguity recorded — never a coin
  flip.

### Example: a read-only scorer

```python
from agent.receipts import ReceiptScoringService
from agent.receipt_scoring import ScorerEvaluation

class PagePublicationScorer:
    scorer_id = "acme.page-end-state"
    scorer_version = "1.0"
    supported_outcome_kinds = frozenset({"page_publication"})

    def evaluate(self, snapshot):
        fresh = [e for e in snapshot.evidence
                 if e.evidence_kind == "page_snapshot"]
        if not fresh:
            return ScorerEvaluation(
                passed=False, ambiguous=True,
                reasons=("no page snapshot evidence to grade",),
            )
        return ScorerEvaluation(passed=True, ambiguous=False, reasons=())

service: ReceiptScoringService = ...
service.register(PagePublicationScorer())
```

## Evidence sources and issuance

`ReceiptIssuer` / `ReceiptIngestor.issue(source)` accepts an
`EvidenceSnapshot` or a bound source exposing `snapshot()`. Snapshots are
built only through `build_evidence_snapshot(...)` (module
`agent.receipt_ingest`), which sorts deterministically, collapses identical
content hashes, rejects conflicting duplicates, and enforces claim
traceability. Sources copy no state machines: the turn ledger,
verification ledger, operation journal, mission records, and transaction
records stay the systems of record.

### Example: a mission evidence source

```python
from agent.receipt_ingest import TurnEvidenceSource, build_evidence_snapshot
from agent.receipt_models import (
    build_claim, build_evidence_digest, build_requested_outcome,
)
from agent.receipts import ReceiptSourceKey, canonical_content_hash

class AcmeMissionSource:
    """Read-only projection of one mission row into evidence."""

    producer_id = "acme.missions"

    def __init__(self, missions_db):
        self._db = missions_db

    def bind(self, mission_id):
        return _Bound(self, mission_id)  # object exposing .snapshot()

    def snapshot(self, mission_id):
        mission = self._db.get(mission_id)          # existing truth source
        check = build_evidence_digest(
            evidence_kind="mission_step_check",
            source_ref=f"missions.db:{mission_id}:final-step",
            producer_id=self.producer_id,
            observed_at=mission.finished_at,
            summary="final mission step recorded terminal state",
            payload_hash=canonical_content_hash(mission.final_step_row),
        )
        goal = build_claim(
            claim_kind="mission-goal",
            statement=mission.goal_statement,
            evidence_ids=(check.evidence_id,),
            required=True,
            verdict="unknown",   # the scorer decides, not the mission
        )
        return build_evidence_snapshot(
            source=ReceiptSourceKey("mission", mission_id),
            subject_kind="mission",
            subject_id=mission_id,
            producer_id=self.producer_id,
            requested_outcome=build_requested_outcome(
                outcome_kind="mission_goal",
                description=mission.goal_statement,
                producer_id=self.producer_id,
            ),
            claims=(goal,),
            evidence=(check,),
        )
```

### Example: a transaction claim builder

```python
from agent.receipts import ReceiptStore, ReceiptSourceKey
from agent.receipt_models import build_claim, build_observation

def issue_transaction_receipt(issuer, tx) -> "Receipt":
    # Idempotent by source: an identical replay returns the same receipt;
    # changed content for the same transaction ID raises a conflict.
    return issuer.issue(transaction_source.bind(tx.transaction_id))

def append_settlement_recheck(store: ReceiptStore, receipt, *,
                              claims, evidence, scorer):
    latest = store.latest_observation(receipt.receipt_id)
    observation = build_observation(
        receipt_id=receipt.receipt_id,
        previous_observation_id=(
            latest.observation_id if latest else None   # CAS link
        ),
        status="completed_unverified",
        claims=claims,
        evidence=evidence,
        scorer_id=scorer.scorer_id,
        scorer_version=scorer.scorer_version,
        observed_at="2026-07-16T12:00:00Z",
    )
    return store.append_observation(observation)
```

Domain claims (`claim_kind="settlement"`, expected/observed JSON amounts)
belong to the consumer; statuses, hashing, and sealing never do.

## Redaction, export, and attestations

- `ReceiptRedactor` removes secrets, credentials, message bodies, query
  strings, and sensitive absolute path prefixes **before** canonical content
  is hashed or persisted. Raw local locators live only in the bounded
  artifact-location table and are excluded from public export.
- `ReceiptExporter` writes public (default) or local redaction;
  `verify_export_hashes(path)` revalidates every content hash offline.
- `ReceiptAttestation` binds a signature to a content hash. Verifying an
  attestation answers only "did this provider sign these bytes?" — it never
  changes a status, verdict, uncertainty, or scorer result. Migrated legacy
  signatures import as untrusted provenance attestations.

### Example: standalone plugin signer registration

```python
from agent.receipts import register_receipt_signer
from agent.receipt_security import SignatureMaterial

class AcmeHsmSigner:
    provider_id = "acme-hsm"

    def sign(self, content_hash: str) -> SignatureMaterial: ...
    def verify(self, content_hash: str,
               material: SignatureMaterial) -> bool: ...

def _check(config: dict) -> bool:
    # Approve only when the operator explicitly configured this provider.
    return config.get("signing", {}).get("provider") == "acme-hsm"

register_receipt_signer(
    "acme-hsm",
    factory=lambda config: AcmeHsmSigner(),
    check_fn=_check,
)
```

Credentials come from the provider's own secret store or `.env` — never from
`config.yaml`, which stores only the provider ID and whether signing is
required. A provider loads only when config names it **and** its `check_fn`
accepts the config. Vendor signers ship as standalone plugins, not core
imports.

## Migration behavior

The provisional vertical-slice `receipts`/`receipt_observations` tables are
migration inputs, not a second schema. Migration is atomic and preserves
receipt IDs and lineage, recomputes canonical hashes, imports signatures as
untrusted provenance attestations, and **downgrades a legacy `verified` row
to `completed_unverified`** until a current independent scorer rechecks it.
Rollback never deletes canonical tables or restores legacy hashes.

## Required tests for consumers

Consumer, scorer, and signer integrations must ship real-path tests:

- a temporary `HADES_HOME` per test — no shared or cross-profile state;
- real `SessionDB`/verification SQLite connections, real files, real hashes,
  real CLI parsers — mock only external signing/network/process-kill
  boundaries;
- fresh object graphs or subprocess restarts proving receipts remain
  independently recheckable after reopening storage;
- proof that an identical source replay returns the same receipt and a
  conflicting replay fails;
- proof that the consumer's own success label alone never yields `verified`.

See `tests/agent/test_receipt_*.py`, `tests/hermes_cli/test_receipt_cli.py`,
`tests/hermes_cli/test_receipt_e2e.py`, and
`tests/benchmarks/test_receipt_benchmark.py` for the house patterns, and the
[operator guide](../user-guide/features/outcome-receipts.md) for statuses,
rollout gates, and failure stops.
