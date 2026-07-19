"""Local report-only runner for the 50-mission false-success proof.

Task 11 of the Verified Outcome & Artifact Receipts plan. This harness
builds every preregistered case from turn/external truth sources plus
the fixture-backed evidence/recheck adapters declared in the manifest —
it never invents a second missions or effects implementation — issues a
real persisted receipt through the public ``agent.receipts`` services
(the candidate arm), classifies the same facts the way today's Hermes
turn-outcome/prose baseline would, and independently rechecks each
receipt after reopening storage.

The report is local JSON/Markdown-style text only (no telemetry, no
upload) and states denominators, exclusions, Wilson 95% intervals,
p50/p95 latency, baseline/candidate costs, safety slices, and stop
conditions separately — safety, cost, and accuracy are never combined
into one score.

Subprocess seams for the E2E proof (``--run-case`` / ``--recheck``) run
one case in a genuinely fresh process over its own case home so that
issuance and recheck are proven across real process restarts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac as hmac_module
import json
import math
import os
import platform
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

if __package__ in (None, ""):  # executed as a plain script file
    _repo_root = str(Path(__file__).resolve().parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

from agent.receipt_artifacts import ArtifactCatalog
from agent.receipt_hashing import canonical_content_hash
from agent.receipt_ingest import (
    ReceiptIngestor,
    ReceiptIssuer,
    ReceiptSourceResolver,
    TurnEvidenceSource,
    build_evidence_snapshot,
)
from agent.receipt_models import (
    ReceiptStatus,
    build_claim,
    build_evidence_digest,
    build_operation_evidence,
    build_receipt,
    build_requested_outcome,
)
from agent.receipt_scoring import (
    CodeTurnEndStateScorer,
    ReceiptScoringService,
    ScorerEvaluation,
    ScorerRegistry,
)
from agent.receipt_security import ReceiptSigningService, SignatureMaterial
from agent.receipt_store import ReceiptStore
from agent.receipts import ReceiptSourceKey
from agent.turn_ledger import TurnOutcomeRecord, fetch_turn_outcome
from benchmarks.receipts.cases import (
    ReceiptBenchmarkManifest,
    ReceiptCase,
    load_receipt_cases,
)
from hades_state import SessionDB

__all__ = [
    "ReceiptBenchmarkReport",
    "ReceiptCaseResult",
    "ReceiptRolloutGate",
    "StratumAccuracy",
    "main",
    "recheck_case",
    "render_receipt_report",
    "rollout_gate",
    "run_receipt_benchmark",
    "run_single_case",
    "wilson_interval",
]

# Durable fixture timestamps — fixed so re-reads of the same seeded case
# are hash-identical across processes.
_T0 = 1752660000
_OBSERVED_AT = "2026-07-16T10:00:00Z"
_STALE_OBSERVED_AT = "2026-07-01T00:00:00Z"
_STALE_FRESH_UNTIL = "2026-07-02T00:00:00Z"

_ROOT_NAMES = ("workspace", "snapshots", "deliveries")
_STATE_FILE = "receipt-case.json"


class ReceiptBenchmarkError(RuntimeError):
    """The benchmark harness hit an unrecoverable setup problem."""


# ---------------------------------------------------------------------------
# Report dataclasses.
# ---------------------------------------------------------------------------


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


@dataclass(frozen=True)
class ReceiptRolloutGate:
    """Runtime-reported staged-rollout gate.

    The exact floors the rollout is judged against — the 50-case
    denominator, the zero false-verified floor, the 45/50 correct
    classification floor, the 50/50 traceability and recheckability
    floors, and the named stop conditions.  Derived only from the
    preregistered manifest and embedded verbatim in every emitted
    report so documentation can never drift from what the runner
    actually enforces.
    """

    denominator: int
    max_false_verified: int
    min_correct_classifications: int
    require_full_traceability: bool
    require_full_recheckability: bool
    stop_conditions: tuple[str, ...]

    def to_json(self) -> dict:
        return {
            "denominator": self.denominator,
            "max_false_verified": self.max_false_verified,
            "min_correct_classifications": self.min_correct_classifications,
            "require_full_traceability": self.require_full_traceability,
            "require_full_recheckability": self.require_full_recheckability,
            "stop_conditions": list(self.stop_conditions),
        }


def rollout_gate(manifest: ReceiptBenchmarkManifest) -> ReceiptRolloutGate:
    """The rollout gate the runner enforces for *manifest* at runtime."""
    return ReceiptRolloutGate(
        denominator=manifest.denominator,
        max_false_verified=manifest.gates.max_false_verified,
        min_correct_classifications=(
            manifest.gates.min_correct_classifications
        ),
        require_full_traceability=(
            manifest.gates.min_traceable_claims_ratio >= 1.0
        ),
        require_full_recheckability=(
            manifest.gates.min_recheckable_receipts_ratio >= 1.0
        ),
        stop_conditions=manifest.stop_conditions,
    )


@dataclass(frozen=True)
class StratumAccuracy:
    stratum: str
    denominator: int
    correct: int
    rate: float
    wilson_low: float
    wilson_high: float


@dataclass(frozen=True)
class ReceiptBenchmarkReport:
    """Separately-stated accuracy, safety, performance, and cost facts."""

    corpus_version: str
    random_seed: int
    baseline: str
    candidate: str
    repeats: int
    denominator: int
    excluded: tuple[str, ...]
    results: tuple[ReceiptCaseResult, ...]
    # ── Accuracy (never combined with safety or cost) ──
    correct_classifications: int
    accuracy_rate: float
    accuracy_wilson_low: float
    accuracy_wilson_high: float
    per_stratum: tuple[StratumAccuracy, ...]
    # ── Safety slices ──
    false_verified_count: int
    baseline_false_verified_count: int
    traceable_claims_ratio: float
    recheckable_receipts_ratio: float
    stop_conditions: tuple[str, ...]
    triggered_stops: tuple[str, ...]
    # ── Performance ──
    baseline_latency_ms_p50: float
    baseline_latency_ms_p95: float
    candidate_latency_ms_p50: float
    candidate_latency_ms_p95: float
    # ── Cost (local compute only; no API spend) ──
    baseline_cost_usd_total: float
    candidate_cost_usd_total: float
    baseline_cost_per_verified_success: float | None
    candidate_cost_per_verified_success: float | None
    # ── Environment ──
    environment: dict
    # ── Rollout gate the report was judged against (runtime-reported) ──
    rollout: ReceiptRolloutGate
    gates_passed: bool
    generated_at: str

    def to_json(self) -> dict:
        return {
            "corpus_version": self.corpus_version,
            "random_seed": self.random_seed,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "repeats": self.repeats,
            "denominator": self.denominator,
            "excluded": list(self.excluded),
            "accuracy": {
                "correct_classifications": self.correct_classifications,
                "rate": self.accuracy_rate,
                "wilson_95": [
                    self.accuracy_wilson_low,
                    self.accuracy_wilson_high,
                ],
                "per_stratum": [
                    {
                        "stratum": s.stratum,
                        "denominator": s.denominator,
                        "correct": s.correct,
                        "rate": s.rate,
                        "wilson_95": [s.wilson_low, s.wilson_high],
                    }
                    for s in self.per_stratum
                ],
            },
            "safety": {
                "false_verified_count": self.false_verified_count,
                "baseline_false_verified_count": (
                    self.baseline_false_verified_count
                ),
                "traceable_claims_ratio": self.traceable_claims_ratio,
                "recheckable_receipts_ratio": self.recheckable_receipts_ratio,
                "stop_conditions": list(self.stop_conditions),
                "triggered_stops": list(self.triggered_stops),
            },
            "performance": {
                "baseline_latency_ms": {
                    "p50": self.baseline_latency_ms_p50,
                    "p95": self.baseline_latency_ms_p95,
                },
                "candidate_latency_ms": {
                    "p50": self.candidate_latency_ms_p50,
                    "p95": self.candidate_latency_ms_p95,
                },
            },
            "cost": {
                "baseline_cost_usd_total": self.baseline_cost_usd_total,
                "candidate_cost_usd_total": self.candidate_cost_usd_total,
                "baseline_cost_per_verified_success": (
                    self.baseline_cost_per_verified_success
                ),
                "candidate_cost_per_verified_success": (
                    self.candidate_cost_per_verified_success
                ),
                "note": (
                    "local SQLite/filesystem compute only; no model or "
                    "provider spend is incurred by either arm"
                ),
            },
            "environment": dict(self.environment),
            "rollout": self.rollout.to_json(),
            "gates_passed": self.gates_passed,
            "generated_at": self.generated_at,
            "results": [
                {
                    "case_id": r.case_id,
                    "stratum": r.stratum,
                    "expected_status": r.expected_status,
                    "actual_status": r.actual_status,
                    "false_verified": r.false_verified,
                    "claim_count": r.claim_count,
                    "traceable_claim_count": r.traceable_claim_count,
                    "independently_recheckable": r.independently_recheckable,
                    "baseline_latency_ms": r.baseline_latency_ms,
                    "candidate_latency_ms": r.candidate_latency_ms,
                    "baseline_cost_usd": r.baseline_cost_usd,
                    "candidate_cost_usd": r.candidate_cost_usd,
                    "excluded_reason": r.excluded_reason,
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# Statistics helpers.
# ---------------------------------------------------------------------------


def wilson_interval(
    successes: int, total: int, z: float = 1.959963984540054
) -> tuple[float, float]:
    """Wilson 95% score interval for a binomial proportion."""
    if total <= 0:
        return (0.0, 0.0)
    phat = successes / total
    denom = 1.0 + z * z / total
    centre = phat + z * z / (2.0 * total)
    margin = z * math.sqrt(
        (phat * (1.0 - phat) + z * z / (4.0 * total)) / total
    )
    return ((centre - margin) / denom, (centre + margin) / denom)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return float(ordered[low])
    return float(
        ordered[low] + (ordered[high] - ordered[low]) * (index - low)
    )


def _median(values: list[float]) -> float:
    return _percentile(values, 0.5)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Case context: one seeded case home with real profile-local stores.
# ---------------------------------------------------------------------------


class ReceiptCaseContext:
    """Open handles over one case home (real SQLite, real files)."""

    def __init__(self, case_home: Path) -> None:
        self.case_home = Path(case_home)
        self.case_home.mkdir(parents=True, exist_ok=True)
        self.db = SessionDB(db_path=self.case_home / "state.db")
        self.catalog = ArtifactCatalog(self.db)
        self.store = ReceiptStore(self.db)
        self.roots: dict[str, Path] = {}
        for name in _ROOT_NAMES:
            root = self.case_home / name
            root.mkdir(exist_ok=True)
            self.roots[name] = root

    def close(self) -> None:
        self.db.close()


class _FixtureEndStateScorer:
    """Fixture-backed independent grader for external outcome kinds.

    Declared by the manifest's recheck adapters; independent of every
    case producer. The grader variant is honestly ambiguous — it can
    never conclusively verify the requested end state.
    """

    scorer_version = "1.0"

    def __init__(
        self, scorer_id: str, outcome_kinds: tuple[str, ...], *,
        ambiguous: bool = False,
    ) -> None:
        self.scorer_id = scorer_id
        self.supported_outcome_kinds = frozenset(outcome_kinds)
        self._ambiguous = ambiguous

    def evaluate(self, snapshot) -> ScorerEvaluation:
        return ScorerEvaluation(
            passed=True,
            ambiguous=self._ambiguous,
            reasons=(
                ("the requested end state is ambiguous for every scorer",)
                if self._ambiguous
                else ()
            ),
        )


def _scoring_service(ctx: ReceiptCaseContext) -> ReceiptScoringService:
    """The candidate arm: the real service plus manifest fixture graders."""
    service = ReceiptScoringService(ScorerRegistry())
    service.register(
        CodeTurnEndStateScorer(
            catalog=ctx.catalog, allowed_roots=(ctx.roots["workspace"],)
        )
    )
    service.register(
        _FixtureEndStateScorer("bench.page-end-state", ("page_publication",))
    )
    service.register(
        _FixtureEndStateScorer(
            "bench.delivery-end-state", ("delivery_confirmation",)
        )
    )
    service.register(
        _FixtureEndStateScorer(
            "bench.prose-grader", ("prose_quality",), ambiguous=True
        )
    )
    return service


# ---------------------------------------------------------------------------
# Seeding: durable mutations happen exactly once per case home.
# ---------------------------------------------------------------------------


def _turn_record(session_id: str) -> TurnOutcomeRecord:
    """A ledger row falsely claiming verified success (untrusted label)."""
    return TurnOutcomeRecord(
        session_id=session_id,
        turn_id="t1",
        created_at=float(_T0),
        outcome="verified",
        outcome_reason="model claims the requested change landed",
        turn_exit_reason="text_response(finish_reason=stop)",
        api_calls=1,
        tool_iterations=1,
        retry_count=0,
        guardrail_halt=None,
        cost_usd_delta=0.0,
        input_tokens_delta=10,
        output_tokens_delta=5,
        cache_read_tokens_delta=0,
        skills_loaded=(),
        model="bench-model",
    )


def _requested_path(ctx: ReceiptCaseContext, case: ReceiptCase) -> Path:
    return ctx.roots[case.allowed_root] / f"{case.case_id}.txt"


def _seed_case(ctx: ReceiptCaseContext, case: ReceiptCase) -> None:
    """Inject the case's fault into real durable state, exactly once."""
    root = ctx.roots[case.allowed_root]
    if case.source_kind == "turn":
        ctx.db.record_turn_outcome(_turn_record(case.case_id))
        requested = _requested_path(ctx, case)
        if case.stratum == "silent_noop":
            pass  # completion claimed but no write ever performed
        elif case.stratum == "wrong_file":
            sibling = root / f"{case.case_id}-sibling.txt"
            sibling.write_text("the edit landed in the wrong file")
        elif case.stratum in ("reverted_change", "forged_artifact"):
            requested.write_text(f"claimed content for {case.case_id}")
            ctx.catalog.register_path(
                requested,
                source_kind="execute_code",
                source_ref=f"{case.case_id}:t1:artifact",
                allowed_roots=(root,),
            )
            if case.stratum == "reverted_change":
                requested.write_text("original content restored")
            else:
                requested.write_text(
                    f"forged bytes that do not match the digest {case.case_id}"
                )
        else:  # pragma: no cover - manifest freezes the strata
            raise ReceiptBenchmarkError(
                f"unexpected turn stratum {case.stratum!r}"
            )
        return
    fixture = root / f"{case.case_id}.json"
    if case.stratum == "stale_page":
        fixture.write_text(
            json.dumps(
                {"url": f"https://example.test/{case.case_id}", "etag": "abc123"}
            )
        )
    elif case.stratum == "partial_delivery":
        recipients = ["alpha", "beta", "gamma"]
        fixture.write_text(
            json.dumps(
                {"recipients": recipients, "acknowledged": recipients[:-1]}
            )
        )
    elif case.stratum == "grader_ambiguity":
        fixture.write_text(
            json.dumps(
                {"draft": f"prose draft for {case.case_id}", "rubric": "unclear"}
            )
        )
    else:  # pragma: no cover - manifest freezes the strata
        raise ReceiptBenchmarkError(
            f"unexpected external stratum {case.stratum!r}"
        )


# ---------------------------------------------------------------------------
# Snapshot builders: read-only, deterministic for the same durable facts.
# ---------------------------------------------------------------------------


def _case_snapshot(ctx: ReceiptCaseContext, case: ReceiptCase):
    if case.source_kind == "turn":
        return _turn_case_snapshot(ctx, case)
    if case.stratum == "stale_page":
        return _stale_page_snapshot(ctx, case)
    if case.stratum == "partial_delivery":
        return _partial_delivery_snapshot(ctx, case)
    if case.stratum == "grader_ambiguity":
        return _grader_ambiguity_snapshot(ctx, case)
    raise ReceiptBenchmarkError(f"no snapshot builder for {case.case_id!r}")


def _turn_case_snapshot(ctx: ReceiptCaseContext, case: ReceiptCase):
    base = TurnEvidenceSource(ctx.db, catalog=ctx.catalog).snapshot(
        case.case_id, "t1"
    )
    root = ctx.roots[case.allowed_root]
    requested = _requested_path(ctx, case)
    evidence = list(base.evidence)
    claims = list(base.claims)
    failures = list(base.known_failures)
    uncertainty = list(base.uncertainty)
    artifacts = list(base.artifacts)

    if case.stratum in ("silent_noop", "wrong_file"):
        if case.stratum == "wrong_file":
            sibling = root / f"{case.case_id}-sibling.txt"
            if sibling.exists():
                uncertainty.append(
                    f"an unrequested sibling path {sibling.name} was written"
                )
        present = requested.exists()
        recheck = build_evidence_digest(
            evidence_kind="artifact_recheck",
            source_ref=f"{case.allowed_root}:{requested.name}",
            producer_id="hermes.receipt-artifacts",
            observed_at=_OBSERVED_AT,
            summary=(
                f"recheck of requested path {requested.name}: "
                + ("present" if present else "missing")
            ),
            payload_hash=canonical_content_hash(
                {
                    "path": requested.name,
                    "status": "present" if present else "missing",
                }
            ),
        )
        evidence.append(recheck)
        claims.append(
            build_claim(
                claim_kind="requested-path",
                statement=(
                    f"the requested path {requested.name} contains the "
                    "claimed change"
                ),
                evidence_ids=(recheck.evidence_id,),
                required=True,
                verdict="satisfied" if present else "unsatisfied",
            )
        )
        if not present:
            failures.append(
                f"claimed write to {requested.name} never landed: the "
                "requested path does not exist"
            )
    elif case.stratum in ("reverted_change", "forged_artifact"):
        if not artifacts:
            raise ReceiptBenchmarkError(
                f"case {case.case_id} has no registered artifact digest"
            )
        digest = artifacts[0]
        results = ctx.catalog.recheck(
            digest.artifact_id, allowed_roots=(root,)
        )
        statuses = sorted(result.status for result in results)
        recheck = build_evidence_digest(
            evidence_kind="artifact_recheck",
            source_ref=f"{case.allowed_root}:{requested.name}",
            producer_id="hermes.receipt-artifacts",
            observed_at=_OBSERVED_AT,
            summary=(
                f"recheck of artifact {digest.artifact_id[:12]}: "
                + ",".join(statuses)
            ),
            payload_hash=canonical_content_hash(
                {"artifact_id": digest.artifact_id, "statuses": statuses}
            ),
        )
        evidence.append(recheck)
        claims.append(
            build_claim(
                claim_kind="requested-path",
                statement=(
                    f"artifact {requested.name} still matches its recorded "
                    "digest"
                ),
                evidence_ids=(recheck.evidence_id,),
                artifact_ids=(digest.artifact_id,),
                required=True,
                verdict=(
                    "satisfied"
                    if statuses and set(statuses) == {"unchanged"}
                    else "unsatisfied"
                ),
            )
        )
        if any(status in ("changed", "missing") for status in statuses):
            failures.append(
                "the claimed change was reverted before scoring"
                if case.stratum == "reverted_change"
                else (
                    "artifact metadata is forged: bytes do not match the "
                    "recorded sha256 digest"
                )
            )
    else:  # pragma: no cover - manifest freezes the strata
        raise ReceiptBenchmarkError(f"unexpected turn stratum {case.stratum!r}")

    unique_artifacts = {item.artifact_id: item for item in artifacts}
    return build_evidence_snapshot(
        source=base.source,
        subject_kind=base.subject_kind,
        subject_id=base.subject_id,
        producer_id=base.producer_id,
        requested_outcome=base.requested_outcome,
        claims=tuple(claims),
        evidence=tuple(evidence),
        artifacts=tuple(unique_artifacts.values()),
        operation_states=base.operation_states,
        blocked_reasons=base.blocked_reasons,
        known_failures=tuple(failures),
        uncertainty=tuple(uncertainty),
        captured_at=_OBSERVED_AT,
    )


def _stale_page_snapshot(ctx: ReceiptCaseContext, case: ReceiptCase):
    fixture = ctx.roots[case.allowed_root] / f"{case.case_id}.json"
    page_evidence = build_evidence_digest(
        evidence_kind="page_snapshot",
        source_ref=f"{case.allowed_root}:{fixture.name}",
        producer_id="external.page-publisher",
        observed_at=_STALE_OBSERVED_AT,
        fresh_until=_STALE_FRESH_UNTIL,  # long past every decision time
        summary=f"captured page snapshot for {case.case_id}",
        payload_hash=canonical_content_hash(json.loads(fixture.read_text())),
    )
    published = build_claim(
        claim_kind="page-published",
        statement="the requested page is live with the captured content",
        evidence_ids=(page_evidence.evidence_id,),
        required=True,
        verdict="satisfied",
    )
    return build_evidence_snapshot(
        source=ReceiptSourceKey("external", case.case_id),
        subject_kind="external",
        subject_id=case.case_id,
        producer_id="external.page-publisher",
        requested_outcome=build_requested_outcome(
            outcome_kind="page_publication",
            description=f"publish the requested page for {case.case_id}",
            producer_id="external.page-publisher",
        ),
        claims=(published,),
        evidence=(page_evidence,),
        captured_at=_OBSERVED_AT,
    )


def _partial_delivery_snapshot(ctx: ReceiptCaseContext, case: ReceiptCase):
    fixture = ctx.roots[case.allowed_root] / f"{case.case_id}.json"
    payload = json.loads(fixture.read_text())
    evidence = []
    operations = []
    for index, recipient in enumerate(payload["recipients"], start=1):
        acknowledged = recipient in payload["acknowledged"]
        digest = build_evidence_digest(
            evidence_kind="delivery_record",
            source_ref=f"{case.allowed_root}:{fixture.name}:{recipient}",
            producer_id="external.delivery-gateway",
            observed_at=_OBSERVED_AT,
            summary=(
                f"delivery to {recipient}: "
                + ("acknowledged" if acknowledged else "no acknowledgement")
            ),
            payload_hash=canonical_content_hash(
                {"recipient": recipient, "acknowledged": acknowledged}
            ),
        )
        evidence.append(digest)
        operations.append(
            build_operation_evidence(
                operation_id=f"{case.case_id}-op-{index}",
                operation_kind="delivery",
                state="confirmed" if acknowledged else "dispatched",
                effect_disposition="landed" if acknowledged else "unknown",
                source_ref=f"{case.allowed_root}:{fixture.name}:{recipient}",
                observed_at=_OBSERVED_AT,
            )
        )
    delivered = build_claim(
        claim_kind="delivery",
        statement="every requested recipient received the delivery",
        evidence_ids=tuple(item.evidence_id for item in evidence),
        required=True,
        verdict="unknown",
    )
    return build_evidence_snapshot(
        source=ReceiptSourceKey("external", case.case_id),
        subject_kind="external",
        subject_id=case.case_id,
        producer_id="external.delivery-gateway",
        requested_outcome=build_requested_outcome(
            outcome_kind="delivery_confirmation",
            description=f"deliver to every recipient for {case.case_id}",
            producer_id="external.delivery-gateway",
        ),
        claims=(delivered,),
        evidence=tuple(evidence),
        operation_states=tuple(operations),
        uncertainty=(
            "delivery acknowledgement is missing for a subset of "
            "recipients; their landing is ambiguous",
        ),
        captured_at=_OBSERVED_AT,
    )


def _grader_ambiguity_snapshot(ctx: ReceiptCaseContext, case: ReceiptCase):
    fixture = ctx.roots[case.allowed_root] / f"{case.case_id}.json"
    transcript = build_evidence_digest(
        evidence_kind="grader_transcript",
        source_ref=f"{case.allowed_root}:{fixture.name}",
        producer_id="external.grader",
        observed_at=_OBSERVED_AT,
        summary=f"grader transcript for {case.case_id}",
        payload_hash=canonical_content_hash(json.loads(fixture.read_text())),
    )
    produced = build_claim(
        claim_kind="draft-produced",
        statement="a draft satisfying the ambiguous rubric was produced",
        evidence_ids=(transcript.evidence_id,),
        required=True,
        verdict="satisfied",
    )
    return build_evidence_snapshot(
        source=ReceiptSourceKey("external", case.case_id),
        subject_kind="external",
        subject_id=case.case_id,
        producer_id="external.prose-author",
        requested_outcome=build_requested_outcome(
            outcome_kind="prose_quality",
            description=(
                f"produce prose meeting the requested quality bar for "
                f"{case.case_id}"
            ),
            producer_id="external.prose-author",
        ),
        claims=(produced,),
        evidence=(transcript,),
        captured_at=_OBSERVED_AT,
    )


class _CaseAdapter:
    """Fixture-backed read-only recheck adapter declared in the manifest."""

    def __init__(self, ctx: ReceiptCaseContext, case: ReceiptCase) -> None:
        self._ctx = ctx
        self._case = case

    def snapshot(self):
        return _case_snapshot(self._ctx, self._case)


# ---------------------------------------------------------------------------
# Baseline arm: today's Hermes turn-outcome label and completion prose.
# ---------------------------------------------------------------------------


def _classify_baseline(
    ctx: ReceiptCaseContext, case: ReceiptCase
) -> tuple[str, float]:
    """What current Hermes would report for the same seeded facts.

    Turn strata: the turn ledger's own outcome label. External strata:
    the producer's completion prose taken at face value — today there is
    no independent end-state check, so a claimed success reads as
    verified success.
    """
    start = time.perf_counter()
    if case.source_kind == "turn":
        record = fetch_turn_outcome(ctx.db, case.case_id, "t1")
        status = record.outcome if record is not None else "unknown_effect"
    else:
        status = "verified"
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return status, elapsed_ms


# ---------------------------------------------------------------------------
# Issue / recheck seams (also driven per-process by the E2E proof).
# ---------------------------------------------------------------------------


def run_single_case(
    case_home: Path, manifest_path: Path, case_id: str
) -> dict:
    """Seed and issue one preregistered case inside its own case home."""
    manifest_path = Path(manifest_path).resolve()
    _manifest, cases = load_receipt_cases(manifest_path)
    case = next((c for c in cases if c.case_id == case_id), None)
    if case is None:
        raise ReceiptBenchmarkError(f"unknown case {case_id!r}")
    ctx = ReceiptCaseContext(Path(case_home))
    try:
        _seed_case(ctx, case)
        baseline_status, baseline_ms = _classify_baseline(ctx, case)
        snapshot = _case_snapshot(ctx, case)
        service = _scoring_service(ctx)
        timings: dict[str, float] = {}

        def _timed_decide(snap, scorer_id=None):
            start = time.perf_counter()
            decision = service.decide(snap, scorer_id=scorer_id)
            timings["candidate_ms"] = (time.perf_counter() - start) * 1000.0
            return decision

        receipt = ReceiptIngestor(ctx.store, decide=_timed_decide).issue(
            snapshot
        )
        evidence_ids = {item.evidence_id for item in receipt.evidence}
        traceable = sum(
            1
            for claim in receipt.claims
            if claim.evidence_ids
            and set(claim.evidence_ids) <= evidence_ids
        )
        state = {
            "case_id": case.case_id,
            "manifest": str(manifest_path),
            "receipt_id": receipt.receipt_id,
        }
        (ctx.case_home / _STATE_FILE).write_text(
            json.dumps(state, sort_keys=True), encoding="utf-8"
        )
        return {
            "case_id": case.case_id,
            "stratum": case.stratum,
            "expected_status": case.expected_status,
            "status": receipt.status,
            "receipt_id": receipt.receipt_id,
            "claim_count": len(receipt.claims),
            "traceable_claim_count": traceable,
            "baseline_status": baseline_status,
            "baseline_latency_ms": baseline_ms,
            "candidate_latency_ms": timings.get("candidate_ms", 0.0),
        }
    finally:
        ctx.close()


def recheck_case(case_home: Path) -> dict:
    """Independently recheck a case receipt after reopening storage.

    Builds a completely fresh object graph over the durable case home
    (fresh SQLite connections, fresh catalog/resolver/scoring), registers
    the manifest-declared fixture recheck adapter, and appends one linked
    observation through the public issuer.
    """
    case_home = Path(case_home)
    state_path = case_home / _STATE_FILE
    if not state_path.exists():
        raise ReceiptBenchmarkError(
            f"case home {case_home} has no issued case state"
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    _manifest, cases = load_receipt_cases(Path(state["manifest"]))
    case = next(
        (c for c in cases if c.case_id == state["case_id"]), None
    )
    if case is None:
        raise ReceiptBenchmarkError(
            f"case {state['case_id']!r} is not in the manifest"
        )
    ctx = ReceiptCaseContext(case_home)
    try:
        service = _scoring_service(ctx)
        resolver = ReceiptSourceResolver(
            ctx.db,
            catalog=ctx.catalog,
            allowed_roots=tuple(ctx.roots.values()),
        )
        resolver.register_adapter(
            case.source_kind, lambda source_id: _CaseAdapter(ctx, case)
        )
        issuer = ReceiptIssuer(ctx.store, scoring=service, sources=resolver)
        observation = issuer.recheck(state["receipt_id"])
        return {
            "receipt_id": observation.receipt_id,
            "observation_id": observation.observation_id,
            "previous_observation_id": observation.previous_observation_id,
            "status": observation.status,
        }
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# Safety probes: signatures and profile isolation.
# ---------------------------------------------------------------------------


class _ProbeHmacSigner:
    """Faked external signing boundary for the signature safety probe."""

    provider_id = "bench-hmac"
    _key = b"bench-probe-signing-key"

    def sign(self, content_hash: str) -> SignatureMaterial:
        digest = hmac_module.new(
            self._key, content_hash.encode("utf-8"), hashlib.sha256
        ).digest()
        return SignatureMaterial(
            key_id="k1",
            algorithm="hmac-sha256",
            signature_b64=base64.b64encode(digest).decode("ascii"),
        )

    def verify(self, content_hash: str, material: SignatureMaterial) -> bool:
        expected = self.sign(content_hash).signature_b64
        return hmac_module.compare_digest(expected, material.signature_b64)


def _probe_receipt():
    evidence = build_evidence_digest(
        evidence_kind="verification_check",
        source_ref="verification_evidence.db:check:probe",
        producer_id="hades.verification",
        observed_at=_OBSERVED_AT,
        summary="probe check",
        payload_hash=canonical_content_hash({"probe": True}),
    )
    claim = build_claim(
        statement="the probe artifact was produced",
        evidence_ids=(evidence.evidence_id,),
        verdict="satisfied",
    )
    return build_receipt(
        source=ReceiptSourceKey("external", "safety-probe"),
        subject_kind="external",
        subject_id="safety-probe",
        requested_outcome=build_requested_outcome(
            outcome_kind="external_state",
            description="safety probe subject",
            producer_id="bench.probe",
        ),
        status="completed_unverified",
        claims=(claim,),
        evidence=(evidence,),
        scorer_id="bench.probe-scorer",
        scorer_version="1.0",
        decided_at=_OBSERVED_AT,
    )


def _run_safety_probes(scratch: Path) -> dict:
    """Directly probe the signature and profile-isolation stops."""
    home_a = scratch / "probe-profile-a"
    home_b = scratch / "probe-profile-b"
    signature_changed_truth = True
    cross_profile_leak = True
    db_a = SessionDB(db_path=home_a.joinpath("state.db"))
    try:
        home_a.mkdir(parents=True, exist_ok=True)
        store_a = ReceiptStore(db_a)
        receipt = store_a.insert(_probe_receipt())
        signing = ReceiptSigningService(
            store_a, provider_id="bench-hmac", signer=_ProbeHmacSigner()
        )
        attestation = signing.sign(receipt)
        verified = (
            attestation is not None and signing.verify(attestation).valid
        )
        after = store_a.get(receipt.receipt_id)
        signature_changed_truth = not (
            verified and after is not None and after.status == receipt.status
        )
        db_b = SessionDB(db_path=home_b.joinpath("state.db"))
        try:
            store_b = ReceiptStore(db_b)
            cross_profile_leak = (
                store_b.get(receipt.receipt_id) is not None
                or store_b.find_by_source(receipt.source) is not None
            )
        finally:
            db_b.close()
    finally:
        db_a.close()
    return {
        "any_signature_changes_truth_status": signature_changed_truth,
        "any_cross_profile_read_or_write": cross_profile_leak,
    }


# ---------------------------------------------------------------------------
# The benchmark loop.
# ---------------------------------------------------------------------------


def _scratch_dir() -> Path:
    """Profile-local scratch: every benchmark path stays inside the home."""
    from hades_constants import get_hades_home

    base = Path(get_hades_home()) / "benchmarks" / "receipts"
    scratch = base / f"run-{os.getpid()}-{int(time.time() * 1000)}"
    scratch.mkdir(parents=True)
    return scratch


def _environment_facts() -> dict:
    return {
        "os": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "filesystem_class": (
            "ntfs-like" if os.name == "nt" else "posix-like"
        ),
        "signer_class": "local-fake-hmac (probe only; no vendor signer)",
        "network_class": "local-only (no outbound requests)",
    }


def run_receipt_benchmark(
    manifest_path: Path, *, repeats: int, output: TextIO
) -> ReceiptBenchmarkReport:
    """Run all 50 preregistered cases locally and report — never upload."""
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    manifest, cases = load_receipt_cases(Path(manifest_path))
    scratch = _scratch_dir()
    results: list[ReceiptCaseResult] = []
    baseline_false_verified = 0
    try:
        for case in cases:
            runs: list[dict] = []
            rechecks_ok: list[bool] = []
            for repeat in range(repeats):
                case_home = scratch / f"repeat-{repeat:02d}" / case.case_id
                run = run_single_case(
                    case_home, Path(manifest_path), case.case_id
                )
                runs.append(run)
                try:
                    recheck = recheck_case(case_home)
                    rechecks_ok.append(
                        recheck["receipt_id"] == run["receipt_id"]
                    )
                except Exception:
                    rechecks_ok.append(False)
            statuses = {run["status"] for run in runs}
            excluded_reason = (
                None
                if len(statuses) == 1
                else "unstable_classification_across_repeats"
            )
            first = runs[0]
            if any(run["baseline_status"] == "verified" for run in runs):
                baseline_false_verified += 1
            results.append(
                ReceiptCaseResult(
                    case_id=case.case_id,
                    stratum=case.stratum,
                    expected_status=case.expected_status,
                    actual_status=first["status"],
                    false_verified=any(
                        run["status"] == "verified" for run in runs
                    ),
                    claim_count=first["claim_count"],
                    traceable_claim_count=first["traceable_claim_count"],
                    independently_recheckable=all(rechecks_ok),
                    baseline_latency_ms=_median(
                        [run["baseline_latency_ms"] for run in runs]
                    ),
                    candidate_latency_ms=_median(
                        [run["candidate_latency_ms"] for run in runs]
                    ),
                    baseline_cost_usd=0.0,
                    candidate_cost_usd=0.0,
                    excluded_reason=excluded_reason,
                )
            )
        probes = _run_safety_probes(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    report = _build_report(
        manifest,
        tuple(results),
        repeats=repeats,
        baseline_false_verified=baseline_false_verified,
        probes=probes,
    )
    output.write(render_receipt_report(report))
    return report


def _build_report(
    manifest: ReceiptBenchmarkManifest,
    results: tuple[ReceiptCaseResult, ...],
    *,
    repeats: int,
    baseline_false_verified: int,
    probes: dict,
) -> ReceiptBenchmarkReport:
    denominator = manifest.denominator
    correct = sum(
        1
        for r in results
        if r.excluded_reason is None and r.actual_status == r.expected_status
    )
    false_verified = sum(1 for r in results if r.false_verified)
    traceable = sum(
        1 for r in results if r.claim_count == r.traceable_claim_count
    )
    recheckable = sum(1 for r in results if r.independently_recheckable)
    traceable_ratio = traceable / denominator if denominator else 0.0
    recheckable_ratio = recheckable / denominator if denominator else 0.0
    wilson_low, wilson_high = wilson_interval(correct, denominator)

    per_stratum: list[StratumAccuracy] = []
    for stratum in dict.fromkeys(r.stratum for r in results):
        stratum_results = [r for r in results if r.stratum == stratum]
        stratum_correct = sum(
            1
            for r in stratum_results
            if r.excluded_reason is None
            and r.actual_status == r.expected_status
        )
        low, high = wilson_interval(stratum_correct, len(stratum_results))
        per_stratum.append(
            StratumAccuracy(
                stratum=stratum,
                denominator=len(stratum_results),
                correct=stratum_correct,
                rate=(
                    stratum_correct / len(stratum_results)
                    if stratum_results
                    else 0.0
                ),
                wilson_low=low,
                wilson_high=high,
            )
        )

    gate = rollout_gate(manifest)
    triggered: list[str] = []
    if false_verified > gate.max_false_verified:
        triggered.append("any_seeded_failure_verified")
    if traceable_ratio < manifest.gates.min_traceable_claims_ratio:
        triggered.append("any_effect_claim_without_existing_evidence")
    if recheckable_ratio < manifest.gates.min_recheckable_receipts_ratio:
        triggered.append("any_receipt_not_recheckable_after_process_restart")
    if probes.get("any_signature_changes_truth_status"):
        triggered.append("any_signature_changes_truth_status")
    if probes.get("any_cross_profile_read_or_write"):
        triggered.append("any_cross_profile_read_or_write")

    baseline_latencies = [r.baseline_latency_ms for r in results]
    candidate_latencies = [r.candidate_latency_ms for r in results]
    gates_passed = (
        not triggered
        and correct >= gate.min_correct_classifications
    )
    excluded = tuple(
        r.case_id for r in results if r.excluded_reason is not None
    )
    return ReceiptBenchmarkReport(
        corpus_version=manifest.corpus_version,
        random_seed=manifest.random_seed,
        baseline=manifest.baseline,
        candidate=manifest.candidate,
        repeats=repeats,
        denominator=denominator,
        excluded=excluded,
        results=results,
        correct_classifications=correct,
        accuracy_rate=correct / denominator if denominator else 0.0,
        accuracy_wilson_low=wilson_low,
        accuracy_wilson_high=wilson_high,
        per_stratum=tuple(per_stratum),
        false_verified_count=false_verified,
        baseline_false_verified_count=baseline_false_verified,
        traceable_claims_ratio=traceable_ratio,
        recheckable_receipts_ratio=recheckable_ratio,
        stop_conditions=manifest.stop_conditions,
        triggered_stops=tuple(triggered),
        baseline_latency_ms_p50=_percentile(baseline_latencies, 0.5),
        baseline_latency_ms_p95=_percentile(baseline_latencies, 0.95),
        candidate_latency_ms_p50=_percentile(candidate_latencies, 0.5),
        candidate_latency_ms_p95=_percentile(candidate_latencies, 0.95),
        baseline_cost_usd_total=0.0,
        candidate_cost_usd_total=0.0,
        # No seeded false-success case may ever verify, so cost per
        # verified success is undefined for this corpus in both arms.
        baseline_cost_per_verified_success=None,
        candidate_cost_per_verified_success=None,
        environment=_environment_facts(),
        rollout=gate,
        gates_passed=gates_passed,
        generated_at=_now_iso(),
    )


def render_receipt_report(report: ReceiptBenchmarkReport) -> str:
    lines = [
        "Receipt false-success benchmark (local report only; no upload)",
        f"corpus: {report.corpus_version} seed={report.random_seed} "
        f"repeats={report.repeats}",
        f"baseline arm: {report.baseline}",
        f"candidate arm: {report.candidate}",
        f"denominator: {report.denominator} "
        f"excluded: {list(report.excluded) or 'none'}",
        "",
        "accuracy:",
        f"  correct classifications: {report.correct_classifications}/"
        f"{report.denominator} "
        f"(rate {report.accuracy_rate:.3f}, Wilson95 "
        f"[{report.accuracy_wilson_low:.3f}, "
        f"{report.accuracy_wilson_high:.3f}])",
    ]
    for stratum in report.per_stratum:
        lines.append(
            f"  {stratum.stratum}: {stratum.correct}/{stratum.denominator} "
            f"(Wilson95 [{stratum.wilson_low:.3f}, {stratum.wilson_high:.3f}])"
        )
    lines += [
        "",
        "safety:",
        f"  candidate false verified: {report.false_verified_count}",
        f"  baseline false verified: {report.baseline_false_verified_count}",
        f"  traceable claims ratio: {report.traceable_claims_ratio:.3f}",
        "  recheckable receipts ratio: "
        f"{report.recheckable_receipts_ratio:.3f}",
        f"  triggered stops: {list(report.triggered_stops) or 'none'}",
        "",
        "performance:",
        "  baseline latency ms p50/p95: "
        f"{report.baseline_latency_ms_p50:.3f}/"
        f"{report.baseline_latency_ms_p95:.3f}",
        "  candidate latency ms p50/p95: "
        f"{report.candidate_latency_ms_p50:.3f}/"
        f"{report.candidate_latency_ms_p95:.3f}",
        "",
        "cost:",
        f"  baseline total USD: {report.baseline_cost_usd_total:.2f} "
        f"(per verified success: "
        f"{report.baseline_cost_per_verified_success})",
        f"  candidate total USD: {report.candidate_cost_usd_total:.2f} "
        f"(per verified success: "
        f"{report.candidate_cost_per_verified_success})",
        "",
        "rollout gate:",
        f"  denominator: {report.rollout.denominator}",
        f"  max false verified: {report.rollout.max_false_verified}",
        "  min correct classifications: "
        f"{report.rollout.min_correct_classifications}",
        "  full traceability required: "
        f"{report.rollout.require_full_traceability}",
        "  full recheckability required: "
        f"{report.rollout.require_full_recheckability}",
        f"  stop conditions: {list(report.rollout.stop_conditions)}",
        "",
        f"environment: {json.dumps(report.environment, sort_keys=True)}",
        f"gates passed: {report.gates_passed}",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmarks.receipts.runner",
        description=(
            "Local report-only 50-mission receipt false-success benchmark"
        ),
    )
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output-json", type=Path, default=None)
    # Fresh-process seams for the E2E proof.
    parser.add_argument("--run-case", default=None, metavar="CASE_ID")
    parser.add_argument("--recheck", action="store_true")
    parser.add_argument("--case-home", type=Path, default=None)
    parser.add_argument("--result-json", type=Path, default=None)
    args = parser.parse_args(argv)

    def _write_result(payload: dict) -> None:
        text = json.dumps(payload, sort_keys=True)
        if args.result_json is not None:
            args.result_json.parent.mkdir(parents=True, exist_ok=True)
            args.result_json.write_text(text, encoding="utf-8")
        else:
            print(text)

    if args.run_case:
        if args.case_home is None or args.manifest is None:
            parser.error("--run-case requires --case-home and --manifest")
        _write_result(
            run_single_case(args.case_home, args.manifest, args.run_case)
        )
        return 0
    if args.recheck:
        if args.case_home is None:
            parser.error("--recheck requires --case-home")
        _write_result(recheck_case(args.case_home))
        return 0

    manifest_path = args.manifest
    if manifest_path is None:
        manifest_path = Path(__file__).resolve().parent / "manifest.yaml"
    report = run_receipt_benchmark(
        manifest_path, repeats=args.repeats, output=sys.stdout
    )
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(report.to_json(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if report.triggered_stops:
        print(
            "SAFETY STOP: " + ", ".join(report.triggered_stops),
            file=sys.stderr,
        )
        return 1
    if not report.gates_passed:
        print("GATES FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
