"""Independent-connection races for schema-v8 management state."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from plugins.auto_routing.auto_routing import storage as storage_module
from plugins.auto_routing.auto_routing.models import ManagementConfigReceipt
from plugins.auto_routing.auto_routing.storage import (
    ImmutableRecordConflict,
    RevisionConflict,
    RoutingStore,
)
from tests.plugins.auto_routing.test_management_storage import (
    DAY,
    MANAGEMENT_AUTHORITY_ID,
    PROFILE_ID,
    assignment,
    management_revision,
    prepare_canary,
    publish_pair,
)


def test_daily_admission_is_atomic_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "daily-cap.db"
    with RoutingStore.open(path=path):
        pass
    barrier = threading.Barrier(2)

    def admit(index: int) -> bool:
        with RoutingStore.open(path=path) as store:
            barrier.wait()
            return store.try_admit_management_revision(
                profile_id=PROFILE_ID,
                utc_day=DAY,
                daily_limit=1,
                revision=management_revision(
                    f"daily-revision-{index}",
                    resulting_authority_id=("c" if index == 0 else "d") * 64,
                    created_at=f"2026-07-19T12:00:0{index}Z",
                ),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(admit, range(2)))
    assert sorted(outcomes) == [False, True]
    with RoutingStore.open(path=path) as verifier:
        assert verifier.management_daily_admissions(PROFILE_ID, DAY) == 1


def test_management_lease_has_one_live_owner_across_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lease.db"
    with RoutingStore.open(path=path) as setup:
        publish_pair(setup)
    barrier = threading.Barrier(2)

    def acquire(owner: str):
        with RoutingStore.open(path=path) as store:
            barrier.wait()
            return store.acquire_management_lease(
                MANAGEMENT_AUTHORITY_ID,
                PROFILE_ID,
                owner,
                "2026-07-19T12:00:00Z",
                10.0,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(acquire, ("owner-a", "owner-b")))
    assert sum(lease is not None for lease in leases) == 1


def test_assignment_reservation_is_first_writer_wins_across_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "assignment.db"
    with RoutingStore.open(path=path) as setup:
        canary = prepare_canary(setup)
    barrier = threading.Barrier(6)

    def reserve(_index: int):
        with RoutingStore.open(path=path) as store:
            barrier.wait()
            return store.reserve_management_assignment(
                assignment(), expected_generation=canary.generation
            )

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(reserve, range(6)))
    assert {row.assignment_id for row in rows} == {"management-assignment-a"}
    with RoutingStore.open(path=path) as verifier:
        assert len(
            verifier.list_open_management_assignments(
                MANAGEMENT_AUTHORITY_ID, PROFILE_ID
            )
        ) == 1


def test_stale_profile_generation_never_wins_across_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state-cas.db"
    with RoutingStore.open(path=path) as setup:
        canary = prepare_canary(setup)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def mutate(index: int) -> None:
        with RoutingStore.open(path=path) as store:
            current = store.read_management_profile_state(
                MANAGEMENT_AUTHORITY_ID, PROFILE_ID
            )
            barrier.wait()
            try:
                store.transition_management_profile_state(
                    profile_id=PROFILE_ID,
                    authority_id=MANAGEMENT_AUTHORITY_ID,
                    expected_generation=canary.generation,
                    state=current.model_copy(
                        update={
                            "experiment_phase": "cooldown",
                            "cooldown_until": "2026-07-19T13:00:00Z",
                            "updated_at": f"2026-07-19T12:01:0{index}Z",
                        }
                    ),
                    event=__import__(
                        "tests.plugins.auto_routing.test_management_storage",
                        fromlist=["lifecycle_event"],
                    ).lifecycle_event(
                        "rejected",
                        "management-challenger",
                        suffix=f"race-{index}",
                        created_at=f"2026-07-19T12:01:0{index}Z",
                    ),
                )
                outcomes.append("won")
            except RevisionConflict:
                outcomes.append("lost")

    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(pool.map(mutate, range(2)))
    assert sorted(outcomes) == ["lost", "won"]
    with RoutingStore.open(path=path) as verifier:
        assert verifier.read_management_profile_state(
            MANAGEMENT_AUTHORITY_ID, PROFILE_ID
        ).generation == canary.generation + 1


def test_assignment_finalization_cannot_change_concurrent_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "finalize.db"
    with RoutingStore.open(path=path) as setup:
        canary = prepare_canary(setup)
        setup.reserve_management_assignment(
            assignment(), expected_generation=canary.generation
        )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def finalize(runtime_digit: str) -> None:
        with RoutingStore.open(path=path) as store:
            barrier.wait()
            try:
                store.finalize_management_assignment(
                    assignment_id="management-assignment-a",
                    runtime_id=runtime_digit * 64,
                    reasoning_effort="medium",
                    expected_generation=canary.generation,
                )
                outcomes.append("won")
            except ImmutableRecordConflict:
                outcomes.append("lost")

    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(pool.map(finalize, ("1", "2")))
    assert sorted(outcomes) == ["lost", "won"]
    with RoutingStore.open(path=path) as verifier:
        stored = verifier.read_management_assignment("management-assignment-a")
        assert stored is not None
        assert stored.phase == "finalized"
        assert stored.runtime_id in {"1" * 64, "2" * 64}


def test_prepared_receipt_transition_is_first_writer_wins_across_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "receipt-cas.db"
    revision = management_revision()
    prepared = ManagementConfigReceipt(
        receipt_id="management-receipt-race",
        revision_id=revision.revision_id,
        phase="prepared",
        preceding_authority_id=revision.preceding_authority_id,
        resulting_authority_id=revision.resulting_authority_id,
        backup_checksum="8" * 64,
        created_at="2026-07-19T12:00:40Z",
        updated_at="2026-07-19T12:00:40Z",
    )
    with RoutingStore.open(path=path) as setup:
        setup.publish_management_revision(revision)
        setup.record_management_receipt(prepared)
    barrier = threading.Barrier(2)

    def transition(index: int) -> tuple[str, str]:
        phase = ("config_replaced", "recovery_required")[index]
        candidate = prepared.model_copy(
            update={
                "phase": phase,
                "updated_at": f"2026-07-19T12:00:4{index + 1}Z",
            }
        )
        with RoutingStore.open(path=path) as store:
            barrier.wait()
            try:
                store.recover_management_receipt(
                    candidate,
                    expected_phase="prepared",
                )
            except RevisionConflict:
                return "lost", phase
        return "won", phase

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(transition, range(2)))
    assert sorted(outcome for outcome, _phase in outcomes) == ["lost", "won"]
    winning_phase = next(phase for outcome, phase in outcomes if outcome == "won")

    with RoutingStore.open(path=path) as verifier:
        stored = verifier.read_management_receipt(prepared.receipt_id)
        assert stored is not None
        assert stored.phase == winning_phase
        assert (
            stored.receipt_id,
            stored.revision_id,
            stored.preceding_authority_id,
            stored.resulting_authority_id,
            stored.backup_checksum,
            stored.created_at,
        ) == (
            prepared.receipt_id,
            prepared.revision_id,
            prepared.preceding_authority_id,
            prepared.resulting_authority_id,
            prepared.backup_checksum,
            prepared.created_at,
        )
        row = verifier.connection.execute(
            "SELECT document_json, checksum FROM management_config_receipts "
            "WHERE receipt_id=?",
            (prepared.receipt_id,),
        ).fetchone()
        assert row is not None
        assert row["checksum"] == storage_module._checksum(row["document_json"])
