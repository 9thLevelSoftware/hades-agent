from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from plugins.auto_routing.auto_routing.learner import (
    QualityAggregate,
    beta_quantile,
    promotion_decision,
    regularized_beta,
    rollback_decision,
    summarize_quality,
)


@dataclass(frozen=True)
class _Event:
    evidence_id: str
    parent_evidence_id: str | None
    decision_id: str
    assignment_id: str
    source: str
    signal_type: str
    observed_at: str
    is_initial_routing_task: bool = True
    outcome: str | None = None
    feedback_value: str | None = None
    normalized_value: float | None = None


def _parent(index: int, *, assignment_id: str = "a", outcome: str = "verified") -> _Event:
    return _Event(
        evidence_id=f"parent-{index:03d}",
        parent_evidence_id=None,
        decision_id=f"decision-{index:03d}",
        assignment_id=assignment_id,
        source="hermes_turn_outcome",
        signal_type="objective_outcome" if outcome == "verified" else "operational",
        observed_at=f"2026-07-18T12:{index % 60:02d}:00Z",
        outcome=outcome,
        normalized_value=1.0 if outcome == "verified" else None,
    )


def _feedback(
    parent: _Event,
    value: str,
    *,
    sequence: int = 1,
    assignment_id: str | None = None,
) -> _Event:
    ratings = {
        "rating-1": 0.0,
        "rating-2": 0.25,
        "rating-3": 0.5,
        "rating-4": 0.75,
        "rating-5": 1.0,
    }
    return _Event(
        evidence_id=f"{parent.evidence_id}-{value}-{sequence}",
        parent_evidence_id=parent.evidence_id,
        decision_id=parent.decision_id,
        assignment_id=assignment_id or parent.assignment_id,
        source="user_feedback",
        signal_type="explicit_feedback",
        observed_at=f"2026-07-18T13:00:{sequence:02d}Z",
        feedback_value=value,
        normalized_value=ratings.get(value),
    )


def _aggregate(successes: float, samples: int) -> QualityAggregate:
    return QualityAggregate(
        rating_successes=successes,
        rating_failures=samples - successes,
        comparable_samples=samples,
    )


def test_operational_events_do_not_change_quality() -> None:
    verified = _parent(1)
    rating = _feedback(verified, "rating-4")
    failed = _parent(2, outcome="failed")

    assert summarize_quality([verified, rating]) == summarize_quality(
        [verified, rating, failed]
    )


def test_manual_reroute_in_any_child_excludes_exact_parent_turn() -> None:
    parent = _parent(1)
    other = _parent(2)
    aggregate = summarize_quality(
        [
            parent,
            _feedback(parent, "rating-5"),
            _feedback(parent, "manual-reroute", sequence=2),
            other,
        ]
    )

    assert aggregate.comparable_samples == 1
    assert aggregate.success_sum == 1.0


def test_latest_canonical_rating_combines_with_verified_as_one_sample() -> None:
    parent = _parent(1)
    older = _feedback(parent, "rating-1", sequence=1)
    newer = _feedback(parent, "rating-4", sequence=2)

    aggregate = summarize_quality([newer, parent, older])

    assert aggregate.comparable_samples == 1
    assert aggregate.verified_successes == 0.5
    assert aggregate.rating_successes == pytest.approx(0.375)
    assert aggregate.rating_failures == pytest.approx(0.125)
    assert aggregate.success_sum == pytest.approx(0.875)
    assert aggregate.alpha == pytest.approx(1.875)
    assert aggregate.beta == pytest.approx(1.125)


def test_rating_only_and_verified_only_each_count_one_parent_turn() -> None:
    verified = _parent(1)
    rating_parent = _parent(2, outcome="failed")
    rating = _feedback(rating_parent, "rating-2")

    aggregate = summarize_quality([verified, rating])

    assert aggregate.comparable_samples == 2
    assert aggregate.success_sum == pytest.approx(1.25)
    assert aggregate.rating_failures == pytest.approx(0.75)


def test_grouping_uses_parent_decision_and_assignment_exactly() -> None:
    first = _parent(1, assignment_id="a")
    second = replace(
        first,
        decision_id="decision-other",
        assignment_id="b",
    )

    aggregate = summarize_quality([first, second])

    assert aggregate.comparable_samples == 2
    assert summarize_quality([first, second], assignment_id="a").comparable_samples == 1


def test_continuation_rows_never_become_quality_samples() -> None:
    continuation = replace(_parent(1), is_initial_routing_task=False)
    assert summarize_quality([continuation]).comparable_samples == 0


def test_twenty_rows_from_fewer_than_twenty_parents_cannot_pass_floor() -> None:
    events: list[_Event] = []
    for index in range(10):
        parent = _parent(index)
        events.extend((parent, _feedback(parent, "rating-5")))
    challenger = summarize_quality(events)
    control = _aggregate(10.0, 20)

    assert len(events) == 20
    assert challenger.comparable_samples == 10
    assert promotion_decision(
        control,
        challenger,
        minimum_samples=20,
        confidence_level=0.90,
    ).action == "hold"


def test_rejection_and_correction_are_assignment_exact() -> None:
    parent = _parent(1, assignment_id="a")
    rejected = _feedback(parent, "rejected")
    corrected = _feedback(parent, "corrected", sequence=2)

    assert rollback_decision(
        [rejected], assignment_id="a", threshold=0.10
    ).action == "reject"
    assert rollback_decision(
        [rejected], assignment_id="b", threshold=0.10
    ).action == "hold"
    assert rollback_decision(
        [corrected], assignment_id="a", threshold=0.10
    ).action == "reject"


def test_manual_reroute_is_not_categorical_rejection() -> None:
    parent = _parent(1)
    reroute = _feedback(parent, "manual-reroute")
    assert rollback_decision(
        [reroute], assignment_id="a", threshold=0.10
    ).action == "hold"


def test_regularized_beta_and_quantile_cover_known_uniform_distribution() -> None:
    assert regularized_beta(0.5, 1.0, 1.0) == pytest.approx(0.5)
    assert regularized_beta(0.0, 2.0, 3.0) == 0.0
    assert regularized_beta(1.0, 2.0, 3.0) == 1.0
    assert beta_quantile(0.9, 1.0, 1.0) == pytest.approx(0.9)


def test_promotion_requires_both_floors_and_separated_posterior_bounds() -> None:
    clearly_better = promotion_decision(
        _aggregate(50.0, 100),
        _aggregate(100.0, 100),
        minimum_samples=20,
        confidence_level=0.90,
    )
    overlapping = promotion_decision(
        _aggregate(80.0, 100),
        _aggregate(81.0, 100),
        minimum_samples=20,
        confidence_level=0.90,
    )

    assert clearly_better.action == "promote"
    assert clearly_better.challenger_lower_bound > clearly_better.control_upper_bound
    assert overlapping.action == "hold"


def test_promotion_uses_one_sided_configured_confidence_bounds() -> None:
    control = _aggregate(0.0, 20)
    challenger = _aggregate(4.0, 20)
    one_sided_lower = beta_quantile(0.10, challenger.alpha, challenger.beta)
    one_sided_upper = beta_quantile(0.90, control.alpha, control.beta)
    central_lower = beta_quantile(0.05, challenger.alpha, challenger.beta)
    central_upper = beta_quantile(0.95, control.alpha, control.beta)

    decision = promotion_decision(
        control,
        challenger,
        minimum_samples=20,
        confidence_level=0.90,
    )

    assert one_sided_lower > one_sided_upper
    assert central_lower <= central_upper
    assert decision.action == "promote"
    assert decision.challenger_lower_bound == pytest.approx(one_sided_lower)
    assert decision.control_upper_bound == pytest.approx(one_sided_upper)


def test_observed_regression_requires_both_floors_and_includes_threshold_boundary() -> None:
    no_control_floor = rollback_decision(
        [],
        assignment_id="a",
        threshold=0.10,
        control=_aggregate(9.0, 10),
        challenger=_aggregate(16.0, 20),
        minimum_samples=20,
    )
    boundary = rollback_decision(
        [],
        assignment_id="a",
        threshold=0.10,
        control=_aggregate(18.0, 20),
        challenger=_aggregate(16.0, 20),
        minimum_samples=20,
    )
    above_boundary = rollback_decision(
        [],
        assignment_id="a",
        threshold=0.10,
        control=_aggregate(18.0, 20),
        challenger=_aggregate(16.01, 20),
        minimum_samples=20,
    )

    assert no_control_floor.action == "hold"
    assert boundary.action == "rollback"
    assert above_boundary.action == "hold"
