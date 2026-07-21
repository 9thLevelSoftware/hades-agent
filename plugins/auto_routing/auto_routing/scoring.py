"""Pure, deterministic normalization and utility scoring."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import MAX_CATALOG_SAMPLE_SIZE, ObjectiveWeights


MISSING_METRIC_PRIOR = 0.35
MISSING_METRIC_UNCERTAINTY = 0.10
SELECTOR_SCORE_COMPONENTS: tuple[str, ...] = (
    "quality",
    "reliability",
    "normalized_latency",
    "normalized_cost",
    "uncertainty_penalty",
    "staleness_penalty",
    "profile_affinity",
    "profile_affinity_adjustment",
    "normalized_base_rank",
    "base_rank_adjustment",
)


@dataclass(frozen=True)
class ConservativeMetric:
    """A bounded metric estimate plus explicit uncertainty contribution."""

    value: float
    uncertainty: float
    used_prior: bool


def clamp_unit(value: float) -> float:
    """Return a finite value clipped to the closed unit interval."""
    if not math.isfinite(value):
        raise ValueError("normalized scoring values must be finite")
    return min(1.0, max(0.0, float(value)))


def conservative_metric(
    value: float | None,
    *,
    confidence: float | None = None,
    sample_size: int | None = None,
    prior: float = MISSING_METRIC_PRIOR,
) -> ConservativeMetric:
    """Discount observed evidence and represent missing evidence conservatively."""
    if value is None:
        return ConservativeMetric(
            value=clamp_unit(prior),
            uncertainty=MISSING_METRIC_UNCERTAINTY,
            used_prior=True,
        )
    normalized = clamp_unit(value)
    confidence_trust = 0.5 if confidence is None else clamp_unit(confidence)
    if sample_size is None:
        sample_trust = 0.5
    elif (
        isinstance(sample_size, bool)
        or not isinstance(sample_size, int)
        or not 0 <= sample_size <= MAX_CATALOG_SAMPLE_SIZE
    ):
        raise ValueError("sample size must be a bounded non-negative integer")
    else:
        sample_trust = math.sqrt(sample_size / (sample_size + 100.0))
    trusted = confidence_trust * sample_trust
    discounted = normalized * trusted + clamp_unit(prior) * (1.0 - trusted)
    return ConservativeMetric(
        value=min(normalized, discounted),
        uncertainty=(1.0 - trusted) * MISSING_METRIC_UNCERTAINTY,
        used_prior=False,
    )


def normalize_against_limit(value: float, limit: float) -> float:
    """Normalize a non-negative estimate against a fixed positive hard limit."""
    if not math.isfinite(value) or value < 0:
        raise ValueError("estimate must be finite and non-negative")
    if not math.isfinite(limit) or limit <= 0:
        return 0.0 if value == 0 and limit == 0 else 1.0
    return clamp_unit(value / limit)


def normalize_catalog_metric(
    *,
    value: float,
    direction: str,
    scale: str,
    normalization_method: str,
) -> float:
    """Normalize one validated catalog point and apply its direction once."""
    pair = (scale.strip().casefold(), normalization_method.strip().casefold())
    if pair == ("unit_interval", "identity"):
        normalized = clamp_unit(value)
    elif pair == ("percent", "divide_by_100"):
        normalized = clamp_unit(value / 100.0)
    else:
        raise ValueError("unsupported metric scale and normalization")
    if direction == "higher_is_better":
        return normalized
    if direction == "lower_is_better":
        return 1.0 - normalized
    raise ValueError("unsupported metric direction")


def utility_score(
    *,
    objectives: ObjectiveWeights,
    quality: float,
    reliability: float,
    normalized_latency: float,
    normalized_cost: float,
    uncertainty_penalty: float,
    staleness_penalty: float,
) -> float:
    """Apply the single fixed utility formula shared with the Stage 2 selector."""
    values = (
        quality,
        reliability,
        normalized_latency,
        normalized_cost,
        uncertainty_penalty,
        staleness_penalty,
    )
    if any(not math.isfinite(value) for value in values):
        raise ValueError("utility inputs must be finite")
    utility = (
        objectives.quality * clamp_unit(quality)
        + objectives.reliability * clamp_unit(reliability)
        + objectives.latency * (1.0 - clamp_unit(normalized_latency))
        + objectives.cost * (1.0 - clamp_unit(normalized_cost))
        - max(0.0, uncertainty_penalty)
        - max(0.0, staleness_penalty)
    )
    if not math.isfinite(utility):  # pragma: no cover - guarded above
        raise ValueError("utility result must be finite")
    return utility


__all__ = [
    "ConservativeMetric",
    "MISSING_METRIC_PRIOR",
    "MISSING_METRIC_UNCERTAINTY",
    "SELECTOR_SCORE_COMPONENTS",
    "clamp_unit",
    "conservative_metric",
    "normalize_against_limit",
    "normalize_catalog_metric",
    "utility_score",
]
