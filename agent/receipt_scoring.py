"""Independent end-state scoring and fixed status precedence for receipts.

Task 5 of the Verified Outcome & Artifact Receipts plan. This module
owns:

- The fixed non-verified terminal precedence, applied before any scorer
  is consulted and never overridable by a consumer:

  1. any operation/effect/delivery whose landing is ambiguous →
     ``unknown_effect``;
  2. any known failed required claim/effect or artifact hash mismatch →
     ``failed``;
  3. authority/review/dependency/provider prevention before an ambiguous
     effect → ``blocked``;
  4. completed work with missing, stale, inappropriate, self-authored,
     or inconclusive verification → ``completed_unverified``;
  5. ``verified`` only after every safety check passes and an
     independent, domain-appropriate scorer passes.

- :class:`ScorerRegistry` — registration rejects an empty
  supported-domain set, a duplicate scorer ID/version, and scorers that
  expose mutation methods; a scorer whose identity equals the source
  producer is rejected at decision time (:class:`ScorerIndependenceError`).
- :class:`ReceiptScoringService` — the ONLY holder of the module-private
  capability that mints the sealed
  :class:`~agent.receipt_models.VerifiedReceiptDecision`. Everything
  non-verified is an ordinary immutable
  :class:`~agent.receipt_models.ReceiptDecision`.
- Three narrow built-in scorers: :class:`CodeTurnEndStateScorer`,
  :class:`MissionEndStateScorer`, and :class:`TransactionEndStateScorer`.
  None trusts a model-authored summary, an existing receipt status, a
  legacy signature, or a turn-ledger outcome label.

A scorer receives only the immutable persisted :class:`EvidenceSnapshot`
facts (plus read-only recheck/reload seams) and can never run a mutating
effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from agent.receipt_hashing import canonical_content_hash, normalize_utc_timestamp
from agent.receipt_models import (
    EvidenceSnapshot,
    ReceiptClaim,
    ReceiptDecision,
    VerifiedReceiptDecision,
    _VERIFIED_DECISION_CAPABILITY,
    _build_verified_decision,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.receipt_artifacts import ArtifactCatalog
    from pathlib import Path

__all__ = [
    "CodeTurnEndStateScorer",
    "EndStateScorer",
    "InappropriateScorerError",
    "MissionEndStateScorer",
    "PRECEDENCE_SCORER_ID",
    "PRECEDENCE_SCORER_VERSION",
    "ReceiptScoringError",
    "ReceiptScoringService",
    "ScorerEvaluation",
    "ScorerIndependenceError",
    "ScorerRegistry",
    "ScorerRegistryError",
    "TransactionEndStateScorer",
    "build_default_scoring_service",
]

# Identity stamped on decisions produced by the fixed precedence/safety
# rules themselves (no independent scorer was — or could be — consulted).
PRECEDENCE_SCORER_ID = "hermes.status-precedence"
PRECEDENCE_SCORER_VERSION = "1.0.0"

# The claim kind whose truth only an independent scorer may decide; its
# ingest-time verdict is always "unknown" and never gates completion.
_END_STATE_CLAIM_KIND = "requested-end-state"


class ReceiptScoringError(RuntimeError):
    """Base error for receipt end-state scoring failures."""


class ScorerRegistryError(ReceiptScoringError):
    """A scorer cannot be registered under the frozen registry rules."""


class ScorerIndependenceError(ReceiptScoringError):
    """The resolved scorer is the producer of the facts it would score."""


class InappropriateScorerError(ReceiptScoringError):
    """An explicitly requested scorer does not cover the outcome domain."""


# ---------------------------------------------------------------------------
# Scorer protocol and evaluation value.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScorerEvaluation:
    """Immutable outcome of one scorer pass over one evidence snapshot.

    ``failures`` are hard facts the scorer established (for example an
    artifact whose bytes no longer match its recorded digest); they map
    to ``failed`` under precedence rule 2. ``reasons`` explain a
    non-passing or ambiguous evaluation and map to
    ``completed_unverified``. ``fresh_until`` bounds how long a passing
    verification remains fresh; an already-expired bound can never seal.
    """

    passed: bool
    ambiguous: bool = False
    reasons: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    fresh_until: str | None = None


@runtime_checkable
class EndStateScorer(Protocol):
    """An independent judge for one family of requested end states."""

    scorer_id: str
    scorer_version: str
    supported_outcome_kinds: frozenset[str]

    def evaluate(self, snapshot: EvidenceSnapshot) -> ScorerEvaluation: ...


# ---------------------------------------------------------------------------
# Registry: who may judge which outcome kinds.
# ---------------------------------------------------------------------------

# Method-name prefixes that reveal effectful intent. A scorer judges
# persisted facts; it never lands, retries, or repairs an effect.
_MUTATION_METHOD_PREFIXES = (
    "apply",
    "commit",
    "compensate",
    "create",
    "delete",
    "dispatch",
    "execute",
    "insert",
    "mutate",
    "patch",
    "post_",
    "push",
    "remove",
    "retry",
    "rollback",
    "send",
    "set_",
    "update",
    "write",
)


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ScorerRegistryError(
            f"scorer {name} must be a non-empty string, got {value!r}"
        )
    return value


class ScorerRegistry:
    """Frozen-rule registry of independent end-state scorers."""

    def __init__(self) -> None:
        self._scorers: dict[str, EndStateScorer] = {}
        self._versions: set[tuple[str, str]] = set()

    def register(self, scorer: EndStateScorer) -> EndStateScorer:
        scorer_id = _require_text(getattr(scorer, "scorer_id", None), "scorer_id")
        scorer_version = _require_text(
            getattr(scorer, "scorer_version", None), "scorer_version"
        )
        supported = getattr(scorer, "supported_outcome_kinds", None)
        if supported is None or isinstance(supported, str):
            raise ScorerRegistryError(
                "scorer supported_outcome_kinds must be a set of outcome "
                f"kinds, got {supported!r}"
            )
        kinds = frozenset(supported)
        if not kinds or any(
            not isinstance(kind, str) or not kind for kind in kinds
        ):
            raise ScorerRegistryError(
                f"scorer {scorer_id!r} declares an empty or invalid "
                "supported-domain set; a scorer must name the outcome kinds "
                "it can judge"
            )
        if not callable(getattr(scorer, "evaluate", None)):
            raise ScorerRegistryError(
                f"scorer {scorer_id!r} has no callable evaluate() method"
            )
        if scorer_id in self._scorers or (scorer_id, scorer_version) in (
            self._versions
        ):
            raise ScorerRegistryError(
                f"duplicate scorer ID/version: {scorer_id!r} {scorer_version!r}"
            )
        mutators = sorted(
            name
            for name in dir(scorer)
            if not name.startswith("_")
            and callable(getattr(scorer, name, None))
            and name.startswith(_MUTATION_METHOD_PREFIXES)
        )
        if mutators:
            raise ScorerRegistryError(
                f"scorer {scorer_id!r} exposes mutating methods {mutators!r}; "
                "a scorer judges immutable persisted facts and never runs a "
                "mutating effect"
            )
        self._scorers[scorer_id] = scorer
        self._versions.add((scorer_id, scorer_version))
        return scorer

    def resolve(
        self, outcome_kind: str, scorer_id: str | None = None
    ) -> EndStateScorer | None:
        """Resolve the scorer for one outcome kind.

        An explicitly requested scorer that is unknown or does not cover
        the outcome kind raises :class:`InappropriateScorerError`. With
        no explicit request the first registered scorer covering the
        kind wins; ``None`` means no appropriate scorer exists (the
        service then caps the status at ``completed_unverified``).
        """
        if scorer_id is not None:
            scorer = self._scorers.get(scorer_id)
            if scorer is None:
                raise InappropriateScorerError(
                    f"no scorer {scorer_id!r} is registered"
                )
            if outcome_kind not in scorer.supported_outcome_kinds:
                raise InappropriateScorerError(
                    f"scorer {scorer_id!r} does not judge outcome kind "
                    f"{outcome_kind!r}; an inappropriate scorer can never "
                    "verify"
                )
            return scorer
        for scorer in self._scorers.values():
            if outcome_kind in scorer.supported_outcome_kinds:
                return scorer
        return None


def _require_independent(producer_id: str, scorer_id: str) -> None:
    if producer_id == scorer_id:
        raise ScorerIndependenceError(
            f"scorer {scorer_id!r} is the producer of the facts it would "
            "score; a self-authored scorer can never verify"
        )


# ---------------------------------------------------------------------------
# Fixed non-verified precedence over persisted snapshot facts.
# ---------------------------------------------------------------------------


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(
        normalize_utc_timestamp(value).replace("Z", "+00:00")
    )


def _ordinary_decision(
    status: str,
    snapshot: EvidenceSnapshot,
    *,
    scorer_id: str,
    scorer_version: str,
    uncertainty: Sequence[str],
    decided_at: str,
    fresh_until: str | None = None,
) -> ReceiptDecision:
    body = {
        "scorer_id": scorer_id,
        "scorer_version": scorer_version,
        "subject_kind": snapshot.subject_kind,
        "subject_id": snapshot.subject_id,
        "snapshot_hash": snapshot.content_hash,
        "claim_hashes": tuple(c.content_hash for c in snapshot.claims),
        "decided_at": decided_at,
        "fresh_until": fresh_until,
    }
    return ReceiptDecision(
        status=status,  # type: ignore[arg-type]
        scorer_id=scorer_id,
        scorer_version=scorer_version,
        subject_kind=snapshot.subject_kind,
        subject_id=snapshot.subject_id,
        snapshot_hash=snapshot.content_hash,
        claim_hashes=tuple(c.content_hash for c in snapshot.claims),
        uncertainty=tuple(dict.fromkeys(uncertainty)),
        decided_at=decided_at,
        fresh_until=fresh_until,
        decision_hash=canonical_content_hash(body),
    )


def _ambiguous_operations(snapshot: EvidenceSnapshot) -> list[str]:
    return [
        (
            f"operation {op.operation_id} ({op.operation_kind}) has state "
            f"{op.state!r} and effect disposition "
            f"{op.effect_disposition!r}; its landing is ambiguous"
        )
        for op in snapshot.operation_states
        if op.state == "unknown" or op.effect_disposition == "unknown"
    ]


def _failed_required_claims(snapshot: EvidenceSnapshot) -> list[str]:
    return [
        f"required claim {claim.claim_kind!r} is unsatisfied: {claim.statement}"
        for claim in snapshot.claims
        if claim.required and claim.verdict == "unsatisfied"
    ]


def _nonverified_precedence(
    snapshot: EvidenceSnapshot,
    *,
    decided_at: str,
    extra_failures: Sequence[str] = (),
    extra_uncertainty: Sequence[str] = (),
) -> ReceiptDecision | None:
    """Apply precedence rules 1–3 over immutable persisted facts.

    Returns ``None`` when no rule fires, letting the service continue to
    verification safety checks and scorer evaluation. Consumers cannot
    override this order.
    """
    ambiguous = _ambiguous_operations(snapshot)
    if ambiguous:
        return _ordinary_decision(
            "unknown_effect",
            snapshot,
            scorer_id=PRECEDENCE_SCORER_ID,
            scorer_version=PRECEDENCE_SCORER_VERSION,
            uncertainty=(
                tuple(ambiguous)
                + tuple(snapshot.uncertainty)
                + tuple(extra_uncertainty)
            ),
            decided_at=decided_at,
        )
    failures = (
        list(snapshot.known_failures)
        + _failed_required_claims(snapshot)
        + list(extra_failures)
    )
    if failures:
        return _ordinary_decision(
            "failed",
            snapshot,
            scorer_id=PRECEDENCE_SCORER_ID,
            scorer_version=PRECEDENCE_SCORER_VERSION,
            uncertainty=tuple(failures) + tuple(extra_uncertainty),
            decided_at=decided_at,
        )
    if snapshot.blocked_reasons:
        return _ordinary_decision(
            "blocked",
            snapshot,
            scorer_id=PRECEDENCE_SCORER_ID,
            scorer_version=PRECEDENCE_SCORER_VERSION,
            uncertainty=(
                tuple(snapshot.blocked_reasons) + tuple(extra_uncertainty)
            ),
            decided_at=decided_at,
        )
    return None


def _verification_safety_gaps(
    snapshot: EvidenceSnapshot, *, decided_at: str
) -> list[str]:
    """Safety facts that cap the status at ``completed_unverified``.

    A missing required claim, a required claim citing no evidence, or
    required-claim evidence past its ``fresh_until`` can never be
    verified over — regardless of what any scorer says.
    """
    gaps: list[str] = []
    required = [claim for claim in snapshot.claims if claim.required]
    if not required:
        gaps.append(
            "no required claim binds the requested end state; there is "
            "nothing an independent scorer could verify"
        )
        return gaps
    now = _parse_timestamp(decided_at)
    evidence_by_id = {item.evidence_id: item for item in snapshot.evidence}
    for claim in required:
        if claim.verdict == "not_applicable":
            continue
        if not claim.evidence_ids:
            gaps.append(
                f"required claim {claim.claim_kind!r} cites no evidence; an "
                "unevidenced claim can never be verified"
            )
            continue
        for evidence_id in claim.evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if item is None:
                gaps.append(
                    f"required claim {claim.claim_kind!r} cites evidence "
                    f"{evidence_id!r} that does not exist in the snapshot"
                )
                continue
            if item.fresh_until is not None and (
                _parse_timestamp(item.fresh_until) <= now
            ):
                gaps.append(
                    f"evidence {item.evidence_kind!r} for required claim "
                    f"{claim.claim_kind!r} expired at {item.fresh_until} and "
                    "is no longer fresh"
                )
    return gaps


def _completed_unverified(
    snapshot: EvidenceSnapshot,
    *,
    scorer_id: str,
    scorer_version: str,
    reasons: Sequence[str],
    decided_at: str,
) -> ReceiptDecision:
    return _ordinary_decision(
        "completed_unverified",
        snapshot,
        scorer_id=scorer_id,
        scorer_version=scorer_version,
        uncertainty=tuple(reasons) + tuple(snapshot.uncertainty),
        decided_at=decided_at,
    )


def _seal_verified(
    capability: object,
    snapshot: EvidenceSnapshot,
    scorer: EndStateScorer,
    evaluation: ScorerEvaluation,
    *,
    decided_at: str,
) -> VerifiedReceiptDecision:
    return _build_verified_decision(
        capability,
        scorer_id=scorer.scorer_id,
        scorer_version=scorer.scorer_version,
        subject_kind=snapshot.subject_kind,
        subject_id=snapshot.subject_id,
        snapshot_hash=snapshot.content_hash,
        claim_hashes=tuple(c.content_hash for c in snapshot.claims),
        decided_at=decided_at,
        fresh_until=evaluation.fresh_until,
    )


def _default_now() -> str:
    return normalize_utc_timestamp(datetime.now(timezone.utc))


class ReceiptScoringService:
    """Decide a receipt status from one immutable evidence snapshot.

    The service is the only holder of the module-private capability that
    constructs :class:`VerifiedReceiptDecision`. Every other outcome is
    an ordinary immutable :class:`ReceiptDecision` carrying one of the
    four non-verified statuses under the fixed precedence.
    """

    def __init__(
        self,
        registry: ScorerRegistry | None = None,
        *,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.registry = registry if registry is not None else ScorerRegistry()
        self._capability = _VERIFIED_DECISION_CAPABILITY
        self._now = now if now is not None else _default_now

    def register(self, scorer: EndStateScorer) -> EndStateScorer:
        """Convenience passthrough to :meth:`ScorerRegistry.register`."""
        return self.registry.register(scorer)

    def decide(
        self, snapshot: EvidenceSnapshot, scorer_id: str | None = None
    ) -> ReceiptDecision | VerifiedReceiptDecision:
        if not isinstance(snapshot, EvidenceSnapshot):
            raise TypeError(
                f"decide() takes an EvidenceSnapshot, got "
                f"{type(snapshot).__name__}"
            )
        decided_at = normalize_utc_timestamp(self._now())
        early = _nonverified_precedence(snapshot, decided_at=decided_at)
        if early is not None:
            return early
        scorer = self.registry.resolve(
            snapshot.requested_outcome.outcome_kind, scorer_id
        )
        if scorer is None:
            return _completed_unverified(
                snapshot,
                scorer_id=PRECEDENCE_SCORER_ID,
                scorer_version=PRECEDENCE_SCORER_VERSION,
                reasons=(
                    "no appropriate independent scorer is registered for "
                    f"outcome kind "
                    f"{snapshot.requested_outcome.outcome_kind!r}; completion "
                    "without independent verification stays "
                    "completed_unverified",
                ),
                decided_at=decided_at,
            )
        _require_independent(snapshot.producer_id, scorer.scorer_id)
        _require_independent(
            snapshot.requested_outcome.producer_id, scorer.scorer_id
        )
        safety_gaps = _verification_safety_gaps(snapshot, decided_at=decided_at)
        if safety_gaps:
            return _completed_unverified(
                snapshot,
                scorer_id=scorer.scorer_id,
                scorer_version=scorer.scorer_version,
                reasons=safety_gaps,
                decided_at=decided_at,
            )
        evaluation = scorer.evaluate(snapshot)
        if not isinstance(evaluation, ScorerEvaluation):
            raise ReceiptScoringError(
                f"scorer {scorer.scorer_id!r} must return a ScorerEvaluation, "
                f"got {type(evaluation).__name__}; a scorer can never mint a "
                "decision object itself"
            )
        if evaluation.failures:
            # Facts the scorer established (for example an artifact hash
            # mismatch) land under precedence rule 2, not as mere
            # unverified completion.
            return _ordinary_decision(
                "failed",
                snapshot,
                scorer_id=scorer.scorer_id,
                scorer_version=scorer.scorer_version,
                uncertainty=(
                    tuple(evaluation.failures)
                    + tuple(evaluation.uncertainty)
                    + tuple(snapshot.uncertainty)
                ),
                decided_at=decided_at,
            )
        if not evaluation.passed or evaluation.ambiguous:
            return _completed_unverified(
                snapshot,
                scorer_id=scorer.scorer_id,
                scorer_version=scorer.scorer_version,
                reasons=(
                    tuple(evaluation.reasons)
                    + tuple(evaluation.uncertainty)
                    or (
                        "the independent scorer could not conclusively "
                        "verify the requested end state",
                    )
                ),
                decided_at=decided_at,
            )
        if evaluation.fresh_until is not None and (
            _parse_timestamp(evaluation.fresh_until)
            <= _parse_timestamp(decided_at)
        ):
            return _completed_unverified(
                snapshot,
                scorer_id=scorer.scorer_id,
                scorer_version=scorer.scorer_version,
                reasons=(
                    "the scorer's verification expired at "
                    f"{evaluation.fresh_until} and is no longer fresh",
                ),
                decided_at=decided_at,
            )
        return _seal_verified(
            self._capability, snapshot, scorer, evaluation,
            decided_at=decided_at,
        )


# ---------------------------------------------------------------------------
# Built-in scorers. Narrow domains; truth is re-derived, never trusted.
# ---------------------------------------------------------------------------


def _default_verification_loader(session_id: str) -> tuple[Mapping[str, Any], ...]:
    """Reload verification state for every recorded root of a session."""
    from agent.verification_evidence import (
        session_verification_roots,
        verification_state_for_root,
    )

    return tuple(
        verification_state_for_root(session_id=session_id, root=root)
        for root in session_verification_roots(session_id)
    )


def _completed_claim_gaps(claims: tuple[ReceiptClaim, ...]) -> list[str]:
    """Required source claims (other than the end-state claim) must hold."""
    return [
        f"required claim {claim.claim_kind!r} is {claim.verdict}, not satisfied"
        for claim in claims
        if claim.required
        and claim.claim_kind != _END_STATE_CLAIM_KIND
        and claim.verdict != "satisfied"
    ]


class CodeTurnEndStateScorer:
    """Judge ``code_change`` turns from reloaded verification and artifacts.

    Requires fresh passed verification (reloaded from the verification
    ledger, never taken from the turn label) for every recorded root,
    requested path claims satisfied, every cited artifact present and
    hash-matched through the read-only catalog recheck, and no unknown
    operation. The turn ledger's ``verified`` outcome is never trusted.
    """

    scorer_id = "hermes.code-turn-end-state"
    scorer_version = "1.0.0"
    supported_outcome_kinds = frozenset({"code_change"})

    def __init__(
        self,
        *,
        catalog: "ArtifactCatalog | None" = None,
        allowed_roots: tuple["Path", ...] = (),
        verification_loader: Callable[
            [str], Sequence[Mapping[str, Any]]
        ] | None = None,
    ) -> None:
        self._catalog = catalog
        self._allowed_roots = tuple(allowed_roots)
        self._verification_loader = (
            verification_loader
            if verification_loader is not None
            else _default_verification_loader
        )

    def evaluate(self, snapshot: EvidenceSnapshot) -> ScorerEvaluation:
        reasons: list[str] = []
        failures: list[str] = []
        for op in snapshot.operation_states:
            if op.state == "unknown" or op.effect_disposition == "unknown":
                return ScorerEvaluation(
                    passed=False,
                    ambiguous=True,
                    reasons=(
                        f"operation {op.operation_id} has an unknown effect "
                        "disposition",
                    ),
                )
        reasons.extend(_completed_claim_gaps(snapshot.claims))

        session_id, _, _turn = snapshot.subject_id.partition(":")
        states = tuple(self._verification_loader(session_id))
        if not states:
            reasons.append(
                "no verification evidence exists for this turn; a code "
                "change without a fresh passed check cannot be verified"
            )
        for state in states:
            status = str(state.get("status") or "unverified")
            root = str(state.get("root") or "<unknown root>")
            if status == "stale":
                reasons.append(
                    f"verification is stale after a later edit for root {root}"
                )
            elif status == "failed":
                failures.append(f"verification failed for root {root}")
            elif status != "passed":
                reasons.append(f"verification is {status} for root {root}")

        if snapshot.artifacts:
            if self._catalog is None:
                reasons.append(
                    "cited artifacts cannot be rechecked without the "
                    "artifact catalog"
                )
            else:
                for artifact in snapshot.artifacts:
                    results = self._catalog.recheck(
                        artifact.artifact_id,
                        allowed_roots=self._allowed_roots,
                    )
                    file_results = [
                        result
                        for result in results
                        if result.status != "ambiguous"
                    ]
                    for result in results:
                        if result.status in ("changed", "missing"):
                            failures.append(
                                f"artifact {artifact.display_name} is "
                                f"{result.status}: {result.detail}"
                            )
                        elif result.status in ("inaccessible", "ambiguous"):
                            reasons.append(
                                f"artifact {artifact.display_name} recheck is "
                                f"{result.status}: {result.detail}"
                            )
                    if not file_results:
                        reasons.append(
                            f"artifact {artifact.display_name} has no "
                            "recheckable location"
                        )
        return ScorerEvaluation(
            passed=not reasons and not failures,
            reasons=tuple(reasons),
            failures=tuple(failures),
        )


class MissionEndStateScorer:
    """Judge mission outcomes only through mission-declared checks.

    Supports only mission-declared outcome kinds and check IDs
    (constraints of the form ``check:<check_id>``), reloads every
    required end-state check, requires all required effects settled and
    evidence fresh, and refuses to guess at an unknown check name — an
    unknown check is rejected as unverifiable here and blocked at
    mission creation by the mission layer.
    """

    scorer_id = "hermes.mission-end-state"
    scorer_version = "1.0.0"

    def __init__(
        self,
        *,
        checks: Mapping[str, Callable[[EvidenceSnapshot], bool]] | None = None,
        outcome_kinds: Sequence[str] = ("mission_outcome",),
    ) -> None:
        self._checks = dict(checks or {})
        self.supported_outcome_kinds = frozenset(outcome_kinds)

    def evaluate(self, snapshot: EvidenceSnapshot) -> ScorerEvaluation:
        reasons: list[str] = []
        for op in snapshot.operation_states:
            if op.state == "unknown" or op.effect_disposition == "unknown":
                return ScorerEvaluation(
                    passed=False,
                    ambiguous=True,
                    reasons=(
                        f"operation {op.operation_id} has an unknown effect "
                        "disposition",
                    ),
                )
        reasons.extend(_completed_claim_gaps(snapshot.claims))
        declared = [
            constraint.split(":", 1)[1]
            for constraint in snapshot.requested_outcome.constraints
            if constraint.startswith("check:")
        ]
        if not declared:
            reasons.append(
                "the mission declares no end-state checks; there is nothing "
                "this scorer can independently verify"
            )
        for check_id in declared:
            check = self._checks.get(check_id)
            if check is None:
                reasons.append(
                    f"unknown end-state check {check_id!r}; refusing to guess "
                    "(an unknown check is blocked at mission creation)"
                )
            elif not check(snapshot):
                reasons.append(f"end-state check {check_id!r} did not pass")
        return ScorerEvaluation(passed=not reasons, reasons=tuple(reasons))


class TransactionEndStateScorer:
    """Judge effect-transaction commits/compensations from lineage facts.

    Requires exact revision/graph/preview lineage evidence, settled
    operation dispositions, adapter postcondition evidence, an exact
    compensation record when compensation is claimed, and outbox
    confirmation where required (any unconfirmed dispatch surfaces as
    snapshot uncertainty and refuses verification).
    """

    scorer_id = "hermes.transaction-end-state"
    scorer_version = "1.0.0"
    supported_outcome_kinds = frozenset(
        {"transaction_commit", "transaction_compensation"}
    )

    def evaluate(self, snapshot: EvidenceSnapshot) -> ScorerEvaluation:
        reasons: list[str] = []
        for op in snapshot.operation_states:
            if op.state == "unknown" or op.effect_disposition == "unknown":
                return ScorerEvaluation(
                    passed=False,
                    ambiguous=True,
                    reasons=(
                        f"operation {op.operation_id} has an unknown effect "
                        "disposition",
                    ),
                )
        if not snapshot.operation_states:
            reasons.append(
                "the transaction has no operation journal evidence; its "
                "landing cannot be independently confirmed"
            )
        for op in snapshot.operation_states:
            if op.state != "confirmed":
                reasons.append(
                    f"operation {op.operation_id} is {op.state!r}, not "
                    "confirmed"
                )
        kinds = {item.evidence_kind for item in snapshot.evidence}
        if "transaction_lineage" not in kinds:
            reasons.append(
                "transaction lineage (revision/graph/preview/authority "
                "hashes) is missing from the evidence"
            )
        if "adapter_postcondition" not in kinds:
            reasons.append(
                "adapter postcondition evidence is missing; the declared "
                "effect cannot be independently confirmed"
            )
        if (
            snapshot.requested_outcome.outcome_kind
            == "transaction_compensation"
            and "compensation_record" not in kinds
        ):
            reasons.append(
                "compensation is claimed but no exact compensation record "
                "exists in the evidence"
            )
        reasons.extend(_completed_claim_gaps(snapshot.claims))
        if snapshot.uncertainty:
            reasons.extend(
                f"unresolved uncertainty: {item}"
                for item in snapshot.uncertainty
            )
        return ScorerEvaluation(passed=not reasons, reasons=tuple(reasons))


def build_default_scoring_service(
    *,
    catalog: "ArtifactCatalog | None" = None,
    allowed_roots: tuple["Path", ...] = (),
    now: Callable[[], str] | None = None,
) -> ReceiptScoringService:
    """A scoring service with the three built-in scorers registered."""
    service = ReceiptScoringService(ScorerRegistry(), now=now)
    service.register(
        CodeTurnEndStateScorer(catalog=catalog, allowed_roots=allowed_roots)
    )
    service.register(MissionEndStateScorer())
    service.register(TransactionEndStateScorer())
    return service
