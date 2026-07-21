"""Independent-connection races for profile-local adaptation state."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from plugins.auto_routing.auto_routing import storage as storage_module
from plugins.auto_routing.auto_routing.models import AdaptiveExplanation
from plugins.auto_routing.auto_routing.storage import RevisionConflict, RoutingStore
from tests.plugins.auto_routing.test_adaptation_storage import (
    AUTHORITY_ID,
    PROFILE_ID,
    assignment,
    lifecycle_event,
    revision,
    stored_revision_checksum,
)


def test_assignment_first_writer_wins_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with RoutingStore.open(path=path) as setup:
        generation = setup.publish_profile_revision(
            revision("revision-control"),
            expected_revision_id=None,
            expected_generation=0,
        )
        challenger_generation = setup.publish_profile_revision(
            revision(
                "revision-challenger",
                parent_revision_id="revision-control",
                created_at="2026-07-18T12:00:01Z",
            ).model_copy(
                update={
                    "explanation": AdaptiveExplanation(
                        context_bucket_id="e" * 64,
                        control_revision_id="revision-control",
                    )
                }
            ),
            expected_revision_id="revision-control",
            expected_generation=generation,
        )
        validated = setup.transition_profile_experiment(
            AUTHORITY_ID,
            PROFILE_ID,
            active_revision_id="revision-control",
            control_revision_id="revision-control",
            challenger_revision_id="revision-challenger",
            experiment_phase="validated",
            cooldown_until=None,
            rejection_count=0,
            expected_generation=challenger_generation,
            event=lifecycle_event("validated", "revision-challenger"),
        )
        setup.transition_profile_experiment(
            AUTHORITY_ID,
            PROFILE_ID,
            active_revision_id="revision-control",
            control_revision_id="revision-control",
            challenger_revision_id="revision-challenger",
            experiment_phase="canary",
            cooldown_until=None,
            rejection_count=0,
            expected_generation=validated.generation,
            event=lifecycle_event(
                "canary",
                "revision-challenger",
                created_at="2026-07-18T12:00:11Z",
            ),
        )

    worker_count = 8
    barrier = threading.Barrier(worker_count)

    def write(_index: int):
        with RoutingStore.open(path=path) as store:
            barrier.wait()
            return store.get_or_create_canary_assignment(assignment())

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        rows = list(pool.map(write, range(worker_count)))
    assert {row.assignment_id for row in rows} == {"assignment-a"}
    with RoutingStore.open(path=path) as verifier:
        assert verifier.count_canary_assignments() == 1


@pytest.mark.parametrize("winner", ["publish", "freeze"])
def test_publish_versus_freeze_has_one_generation_winner(
    tmp_path: Path,
    winner: str,
) -> None:
    path = tmp_path / f"{winner}.db"
    with RoutingStore.open(path=path) as setup:
        generation = setup.publish_profile_revision(
            revision("revision-control"),
            expected_revision_id=None,
            expected_generation=0,
        )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def publish() -> None:
        with RoutingStore.open(path=path) as publish_store:
            barrier.wait()
            try:
                publish_store.publish_profile_revision(
                    revision(
                        "revision-challenger",
                        parent_revision_id="revision-control",
                        created_at="2026-07-18T12:00:01Z",
                    ),
                    expected_revision_id="revision-control",
                    expected_generation=generation,
                )
                outcomes.append("publish")
            except (
                storage_module.ProfileFrozen,
                storage_module.ProfileStateConflict,
                RevisionConflict,
            ):
                outcomes.append("publish_lost")

    def freeze() -> None:
        with RoutingStore.open(path=path) as freeze_store:
            barrier.wait()
            try:
                freeze_store.set_profile_freeze(
                    AUTHORITY_ID,
                    PROFILE_ID,
                    frozen=True,
                    expected_generation=generation,
                )
                outcomes.append("freeze")
            except storage_module.ProfileStateConflict:
                outcomes.append("freeze_lost")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(publish if winner == "publish" else freeze)
        second = pool.submit(freeze if winner == "publish" else publish)
        first.result()
        second.result()
    assert sum(item in {"publish", "freeze"} for item in outcomes) == 1
    with RoutingStore.open(path=path) as verifier:
        control = verifier.read_profile_control(AUTHORITY_ID, PROFILE_ID)
        assert control.generation == generation + 1
        assert (control.frozen, control.active_revision_id) in {
            (True, "revision-control"),
            (False, "revision-challenger"),
        }


def test_rollback_versus_freeze_uses_same_generation(tmp_path: Path) -> None:
    path = tmp_path / "rollback-freeze.db"
    with RoutingStore.open(path=path) as setup:
        first = setup.publish_profile_revision(
            revision("revision-control"),
            expected_revision_id=None,
            expected_generation=0,
        )
        generation = setup.publish_profile_revision(
            revision(
                "revision-challenger",
                parent_revision_id="revision-control",
                created_at="2026-07-18T12:00:01Z",
            ),
            expected_revision_id="revision-control",
            expected_generation=first,
        )
    barrier = threading.Barrier(2)
    successes: list[str] = []

    def rollback() -> None:
        with RoutingStore.open(path=path) as rollback_store:
            barrier.wait()
            try:
                rollback_store.rollback_profile_revision(
                    authority_id=AUTHORITY_ID,
                    profile_id=PROFILE_ID,
                    revision_id="revision-control",
                    expected_target_checksum=stored_revision_checksum(
                        rollback_store, "revision-control"
                    ),
                    expected_generation=generation,
                )
                successes.append("rollback")
            except (
                storage_module.ProfileFrozen,
                storage_module.ProfileStateConflict,
            ):
                pass

    def freeze() -> None:
        with RoutingStore.open(path=path) as freeze_store:
            barrier.wait()
            try:
                freeze_store.set_profile_freeze(
                    AUTHORITY_ID,
                    PROFILE_ID,
                    frozen=True,
                    expected_generation=generation,
                )
                successes.append("freeze")
            except storage_module.ProfileStateConflict:
                pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(pool.map(lambda fn: fn(), (rollback, freeze)))
    assert successes == ["freeze"]
    with RoutingStore.open(path=path) as verifier:
        assert verifier.read_profile_control(
            AUTHORITY_ID, PROFILE_ID
        ).generation == generation + 1


def test_stale_precondition_never_overwrites_newer_control(tmp_path: Path) -> None:
    path = tmp_path / "stale.db"
    first = RoutingStore.open(path=path)
    second = RoutingStore.open(path=path)
    try:
        initial = first.read_profile_control(AUTHORITY_ID, PROFILE_ID)
        frozen = first.set_profile_freeze(
            AUTHORITY_ID,
            PROFILE_ID,
            frozen=True,
            expected_generation=initial.generation,
        )
        with pytest.raises(storage_module.ProfileStateConflict):
            second.set_profile_freeze(
                AUTHORITY_ID,
                PROFILE_ID,
                frozen=False,
                expected_generation=initial.generation,
            )
        assert second.read_profile_control(AUTHORITY_ID, PROFILE_ID) == frozen
    finally:
        first.close()
        second.close()
