"""Atomic reservation and reconciliation contracts for routing overhead."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from plugins.auto_routing.auto_routing.storage import (
    BudgetExceeded,
    ReservationConflict,
    ReservationNotFound,
    RoutingStore,
)


@pytest.fixture
def store(isolated_home: Path) -> RoutingStore:
    result = RoutingStore.open()
    try:
        yield result
    finally:
        result.close()


def test_budget_reservation_is_atomic_and_reconciled(
    store: RoutingStore,
    mutable_clock,
) -> None:
    reservation = store.reserve_budget(
        "classifier",
        worst_case_usd=0.20,
        daily_limit_usd=1.00,
        now=mutable_clock.now(),
    )
    before = store.daily_budget("classifier", mutable_clock.today())
    assert before.spent_usd == 0.0
    assert before.reserved_usd == pytest.approx(0.20)
    assert before.committed_usd == pytest.approx(0.20)

    reconciled = store.reconcile_budget(
        reservation.reservation_id,
        actual_usd=0.07,
        now=mutable_clock.now(),
    )
    after = store.daily_budget("classifier", mutable_clock.today())

    assert reconciled.status == "reconciled"
    assert reconciled.actual_usd == pytest.approx(0.07)
    assert after.spent_usd == pytest.approx(0.07)
    assert after.reserved_usd == 0.0
    assert after.committed_usd == pytest.approx(0.07)


def test_pending_reservations_count_against_the_limit(
    store: RoutingStore,
    mutable_clock,
) -> None:
    store.reserve_budget(
        "classifier",
        worst_case_usd=0.60,
        daily_limit_usd=1.00,
        now=mutable_clock.now(),
    )

    with pytest.raises(BudgetExceeded) as exc_info:
        store.reserve_budget(
            "classifier",
            worst_case_usd=0.41,
            daily_limit_usd=1.00,
            now=mutable_clock.now(),
        )

    assert exc_info.value.bucket == "classifier"
    assert exc_info.value.committed_usd == pytest.approx(0.60)
    assert store.daily_budget(
        "classifier",
        mutable_clock.today(),
    ).committed_usd == pytest.approx(0.60)


def test_reconciliation_releases_unused_reservation_capacity(
    store: RoutingStore,
    mutable_clock,
) -> None:
    first = store.reserve_budget(
        "evaluator",
        worst_case_usd=0.60,
        daily_limit_usd=1.00,
        now=mutable_clock.now(),
    )
    store.reconcile_budget(
        first.reservation_id,
        actual_usd=0.20,
        now=mutable_clock.now(),
    )
    second = store.reserve_budget(
        "evaluator",
        worst_case_usd=0.70,
        daily_limit_usd=1.00,
        now=mutable_clock.now(),
    )

    budget = store.daily_budget("evaluator", mutable_clock.today())
    assert second.status == "reserved"
    assert budget.spent_usd == pytest.approx(0.20)
    assert budget.reserved_usd == pytest.approx(0.70)
    assert budget.committed_usd == pytest.approx(0.90)


def test_reconciled_actual_costs_are_additive(
    store: RoutingStore,
    mutable_clock,
) -> None:
    reservations = [
        store.reserve_budget(
            "classifier",
            worst_case_usd=0.20,
            daily_limit_usd=1.00,
            now=mutable_clock.now(),
        )
        for _ in range(2)
    ]
    store.reconcile_budget(
        reservations[0].reservation_id,
        actual_usd=0.04,
        now=mutable_clock.now(),
    )
    store.reconcile_budget(
        reservations[1].reservation_id,
        actual_usd=0.06,
        now=mutable_clock.now(),
    )

    budget = store.daily_budget("classifier", mutable_clock.today())
    assert budget.spent_usd == pytest.approx(0.10)
    assert budget.reserved_usd == 0.0


def test_same_reconciliation_is_idempotent_but_different_actual_conflicts(
    store: RoutingStore,
    mutable_clock,
) -> None:
    reservation = store.reserve_budget(
        "classifier",
        worst_case_usd=0.20,
        daily_limit_usd=1.00,
        now=mutable_clock.now(),
    )
    first = store.reconcile_budget(
        reservation.reservation_id,
        actual_usd=0.07,
        now=mutable_clock.now(),
    )
    duplicate = store.reconcile_budget(
        reservation.reservation_id,
        actual_usd=0.07,
        now=mutable_clock.now(),
    )

    assert duplicate == first
    with pytest.raises(ReservationConflict):
        store.reconcile_budget(
            reservation.reservation_id,
            actual_usd=0.08,
            now=mutable_clock.now(),
        )
    assert store.daily_budget(
        "classifier",
        mutable_clock.today(),
    ).spent_usd == pytest.approx(0.07)


def test_unknown_reservation_cannot_be_reconciled(
    store: RoutingStore,
    mutable_clock,
) -> None:
    with pytest.raises(ReservationNotFound, match="missing"):
        store.reconcile_budget(
            "missing",
            actual_usd=0.01,
            now=mutable_clock.now(),
        )


def test_actual_above_worst_case_is_recorded_and_blocks_future_spend(
    store: RoutingStore,
    mutable_clock,
) -> None:
    reservation = store.reserve_budget(
        "experiment",
        worst_case_usd=0.25,
        daily_limit_usd=1.00,
        now=mutable_clock.now(),
    )
    store.reconcile_budget(
        reservation.reservation_id,
        actual_usd=1.10,
        now=mutable_clock.now(),
    )

    assert store.daily_budget(
        "experiment",
        mutable_clock.today(),
    ).spent_usd == pytest.approx(1.10)
    with pytest.raises(BudgetExceeded):
        store.reserve_budget(
            "experiment",
            worst_case_usd=0.01,
            daily_limit_usd=1.00,
            now=mutable_clock.now(),
        )


def test_budget_buckets_and_utc_days_are_independent(
    store: RoutingStore,
    mutable_clock,
) -> None:
    store.reserve_budget(
        "classifier",
        worst_case_usd=1.00,
        daily_limit_usd=1.00,
        now=mutable_clock.now(),
    )
    store.reserve_budget(
        "evaluator",
        worst_case_usd=1.00,
        daily_limit_usd=1.00,
        now=mutable_clock.now(),
    )
    mutable_clock.advance(seconds=24 * 60 * 60)
    next_day = store.reserve_budget(
        "classifier",
        worst_case_usd=1.00,
        daily_limit_usd=1.00,
        now=mutable_clock.now(),
    )

    assert next_day.budget_day == mutable_clock.today()
    assert store.daily_budget("classifier", date(2026, 1, 1)).reserved_usd == 1.0
    assert store.daily_budget("classifier", date(2026, 1, 2)).reserved_usd == 1.0
    assert store.daily_budget("evaluator", date(2026, 1, 1)).reserved_usd == 1.0


@pytest.mark.parametrize(
    ("worst_case_usd", "daily_limit_usd"),
    [
        (-0.01, 1.0),
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        (0.1, -1.0),
        (0.1, float("nan")),
    ],
)
def test_reservation_rejects_invalid_money_values_without_writing(
    store: RoutingStore,
    mutable_clock,
    worst_case_usd: float,
    daily_limit_usd: float,
) -> None:
    with pytest.raises(ValueError):
        store.reserve_budget(
            "classifier",
            worst_case_usd=worst_case_usd,
            daily_limit_usd=daily_limit_usd,
            now=mutable_clock.now(),
        )
    assert store.daily_budget("classifier", mutable_clock.today()).committed_usd == 0.0


@pytest.mark.parametrize("actual_usd", [-0.01, float("nan"), float("inf")])
def test_reconciliation_rejects_invalid_actual_without_consuming_reservation(
    store: RoutingStore,
    mutable_clock,
    actual_usd: float,
) -> None:
    reservation = store.reserve_budget(
        "classifier",
        worst_case_usd=0.20,
        daily_limit_usd=1.00,
        now=mutable_clock.now(),
    )

    with pytest.raises(ValueError):
        store.reconcile_budget(
            reservation.reservation_id,
            actual_usd=actual_usd,
            now=mutable_clock.now(),
        )
    budget = store.daily_budget("classifier", mutable_clock.today())
    assert budget.spent_usd == 0.0
    assert budget.reserved_usd == pytest.approx(0.20)
