"""Pure, deterministic quality aggregation and adaptive decision math."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


_RATINGS = {
    "rating-1": 0.0,
    "rating-2": 0.25,
    "rating-3": 0.5,
    "rating-4": 0.75,
    "rating-5": 1.0,
}
_BETA_CONTINUED_FRACTION_CAP = 200
_BETA_EPSILON = 3e-14
_BETA_FPMIN = 1e-300


@dataclass(frozen=True)
class QualityAggregate:
    """Fractional Bernoulli observations grouped by initial parent turn."""

    verified_successes: float = 0.0
    rating_successes: float = 0.0
    rating_failures: float = 0.0
    comparable_samples: int = 0

    @property
    def alpha(self) -> float:
        return 1.0 + self.verified_successes + self.rating_successes

    @property
    def beta(self) -> float:
        return 1.0 + self.rating_failures

    @property
    def success_sum(self) -> float:
        return self.verified_successes + self.rating_successes


@dataclass(frozen=True)
class LearnerDecision:
    """Closed learner action with optional auditable posterior bounds."""

    action: Literal["hold", "promote", "reject", "rollback"]
    reason: str
    challenger_lower_bound: float | None = None
    control_upper_bound: float | None = None


def _field(event: object, name: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def _parent_key(event: object) -> tuple[object, object, object]:
    parent_evidence_id = _field(event, "parent_evidence_id")
    return (
        parent_evidence_id or _field(event, "evidence_id"),
        _field(event, "decision_id"),
        _field(event, "assignment_id"),
    )


def _canonical_observation_key(event: object) -> tuple[datetime, str]:
    observed_at = str(_field(event, "observed_at"))
    instant = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    return instant, str(_field(event, "evidence_id"))


def summarize_quality(
    events: Iterable[object],
    *,
    assignment_id: str | None = None,
) -> QualityAggregate:
    """Summarize quality once per exact initial parent-turn attribution."""
    groups: dict[tuple[object, object, object], list[object]] = {}
    for event in events:
        if not _field(event, "is_initial_routing_task", False):
            continue
        if assignment_id is not None and _field(event, "assignment_id") != assignment_id:
            continue
        groups.setdefault(_parent_key(event), []).append(event)

    verified_successes = 0.0
    rating_successes = 0.0
    rating_failures = 0.0
    comparable_samples = 0
    for observations in groups.values():
        if any(
            _field(event, "feedback_value") == "manual-reroute"
            for event in observations
        ):
            continue
        verified = any(
            _field(event, "source") == "hermes_turn_outcome"
            and _field(event, "outcome") == "verified"
            for event in observations
        )
        ratings = [
            event
            for event in observations
            if _field(event, "source") == "user_feedback"
            and _field(event, "feedback_value") in _RATINGS
        ]
        rating = None
        if ratings:
            latest = max(ratings, key=_canonical_observation_key)
            rating = _RATINGS[str(_field(latest, "feedback_value"))]

        if verified and rating is not None:
            verified_successes += 0.5
            rating_successes += rating / 2.0
            rating_failures += (1.0 - rating) / 2.0
        elif verified:
            verified_successes += 1.0
        elif rating is not None:
            rating_successes += rating
            rating_failures += 1.0 - rating
        else:
            continue
        comparable_samples += 1

    return QualityAggregate(
        verified_successes=verified_successes,
        rating_successes=rating_successes,
        rating_failures=rating_failures,
        comparable_samples=comparable_samples,
    )


def _beta_continued_fraction(x: float, a: float, b: float) -> float:
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _BETA_FPMIN:
        d = _BETA_FPMIN
    d = 1.0 / d
    result = d
    for iteration in range(1, _BETA_CONTINUED_FRACTION_CAP + 1):
        even = 2 * iteration
        coefficient = iteration * (b - iteration) * x / (
            (qam + even) * (a + even)
        )
        d = 1.0 + coefficient * d
        if abs(d) < _BETA_FPMIN:
            d = _BETA_FPMIN
        c = 1.0 + coefficient / c
        if abs(c) < _BETA_FPMIN:
            c = _BETA_FPMIN
        d = 1.0 / d
        result *= d * c

        coefficient = -(a + iteration) * (qab + iteration) * x / (
            (a + even) * (qap + even)
        )
        d = 1.0 + coefficient * d
        if abs(d) < _BETA_FPMIN:
            d = _BETA_FPMIN
        c = 1.0 + coefficient / c
        if abs(c) < _BETA_FPMIN:
            c = _BETA_FPMIN
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < _BETA_EPSILON:
            break
    return result


def regularized_beta(x: float, a: float, b: float) -> float:
    """Return the regularized incomplete beta using standard-library math."""
    if not all(math.isfinite(value) for value in (x, a, b)):
        raise ValueError("beta inputs must be finite")
    if a <= 0.0 or b <= 0.0 or not 0.0 <= x <= 1.0:
        raise ValueError("beta shapes must be positive and x must be in [0, 1]")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    factor = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return factor * _beta_continued_fraction(x, a, b) / a
    return 1.0 - factor * _beta_continued_fraction(1.0 - x, b, a) / b


def beta_quantile(probability: float, a: float, b: float) -> float:
    """Invert the beta CDF with exactly 80 deterministic bisection steps."""
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError("beta probability must be finite and in [0, 1]")
    if probability == 0.0:
        return 0.0
    if probability == 1.0:
        return 1.0
    lower = 0.0
    upper = 1.0
    for _iteration in range(80):
        midpoint = (lower + upper) / 2.0
        if regularized_beta(midpoint, a, b) < probability:
            lower = midpoint
        else:
            upper = midpoint
    return (lower + upper) / 2.0


def _validate_sample_floor(minimum_samples: int) -> None:
    if isinstance(minimum_samples, bool) or not isinstance(minimum_samples, int):
        raise ValueError("minimum_samples must be a non-negative integer")
    if minimum_samples < 0:
        raise ValueError("minimum_samples must be a non-negative integer")


def promotion_decision(
    control: QualityAggregate,
    challenger: QualityAggregate,
    *,
    minimum_samples: int,
    confidence_level: float = 0.90,
) -> LearnerDecision:
    """Promote only after both floors and non-overlapping posterior bounds."""
    _validate_sample_floor(minimum_samples)
    if not math.isfinite(confidence_level) or not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence_level must be finite and between zero and one")
    if (
        control.comparable_samples < minimum_samples
        or challenger.comparable_samples < minimum_samples
    ):
        return LearnerDecision("hold", "minimum_samples")
    tail = 1.0 - confidence_level
    challenger_lower = beta_quantile(tail, challenger.alpha, challenger.beta)
    control_upper = beta_quantile(1.0 - tail, control.alpha, control.beta)
    action: Literal["hold", "promote"] = (
        "promote" if challenger_lower > control_upper else "hold"
    )
    return LearnerDecision(
        action,
        "posterior_separated" if action == "promote" else "posterior_overlap",
        challenger_lower_bound=challenger_lower,
        control_upper_bound=control_upper,
    )


def rollback_decision(
    events: Iterable[object],
    *,
    assignment_id: str,
    threshold: float,
    control: QualityAggregate | None = None,
    challenger: QualityAggregate | None = None,
    minimum_samples: int = 20,
) -> LearnerDecision:
    """Reject exact adverse feedback or roll back a sufficiently sampled regression."""
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or not 0.0 <= threshold <= 1.0
    ):
        raise ValueError("threshold must be finite and between zero and one")
    _validate_sample_floor(minimum_samples)
    for event in events:
        if (
            _field(event, "assignment_id") == assignment_id
            and _field(event, "source") == "user_feedback"
            and _field(event, "feedback_value") in {"rejected", "corrected"}
        ):
            return LearnerDecision("reject", "exact_assignment_feedback")
    if control is None or challenger is None:
        return LearnerDecision("hold", "no_comparable_arms")
    if (
        control.comparable_samples < minimum_samples
        or challenger.comparable_samples < minimum_samples
    ):
        return LearnerDecision("hold", "minimum_samples")
    control_rate = control.success_sum / control.comparable_samples
    challenger_rate = challenger.success_sum / challenger.comparable_samples
    if challenger_rate <= control_rate - threshold:
        return LearnerDecision("rollback", "observed_regression")
    return LearnerDecision("hold", "no_observed_regression")


__all__ = [
    "LearnerDecision",
    "QualityAggregate",
    "beta_quantile",
    "promotion_decision",
    "regularized_beta",
    "rollback_decision",
    "summarize_quality",
]
