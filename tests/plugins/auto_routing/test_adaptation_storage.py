"""Profile-local immutable adaptation storage contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from plugins.auto_routing.auto_routing import storage as storage_module
from plugins.auto_routing.auto_routing.models import (
    AdaptiveCanaryAssignment,
    AdaptiveExplanation,
    AdaptiveLifecycleEvent,
    AdaptiveOverlay,
    AdaptiveProfileRevision,
)
from plugins.auto_routing.auto_routing.storage import (
    ImmutableRecordConflict,
    RevisionChecksumError,
    RevisionConflict,
    RoutingStore,
    UnsupportedSchemaVersion,
)


AUTHORITY_ID = "a" * 64
PROFILE_ID = "coding"
EPOCH = "1970-01-01T00:00:00.000000Z"


@pytest.fixture
def store(tmp_path: Path):
    with RoutingStore.open(path=tmp_path / "state.db") as opened:
        yield opened


def revision(
    revision_id: str,
    *,
    authority_id: str = AUTHORITY_ID,
    profile_id: str = PROFILE_ID,
    parent_revision_id: str | None = None,
    lifecycle: str = "validated",
    created_at: str = "2026-07-18T12:00:00Z",
) -> AdaptiveProfileRevision:
    return AdaptiveProfileRevision(
        revision_id=revision_id,
        authority_id=authority_id,
        profile_id=profile_id,
        parent_revision_id=parent_revision_id,
        overlay=AdaptiveOverlay(
            profile_id=profile_id,
            ordered_primary_runtime_ids=("b" * 64, "c" * 64),
            reasoning_defaults={"b" * 64: "medium"},
        ),
        explanation={"reason_codes": ("enough_evidence",)},
        lifecycle=lifecycle,
        created_at=created_at,
        complete=True,
    )


def revision_checksum(value: AdaptiveProfileRevision) -> str:
    return storage_module._checksum(storage_module._canonical_json(value))


def stored_revision_checksum(store: RoutingStore, revision_id: str) -> str:
    value = store.read_profile_revision(revision_id)
    assert value is not None
    return revision_checksum(value)


def lifecycle_event(
    event_type: str,
    revision_id: str | None,
    *,
    suffix: str | None = None,
    authority_id: str = AUTHORITY_ID,
    profile_id: str = PROFILE_ID,
    created_at: str = "2026-07-18T12:00:10Z",
) -> AdaptiveLifecycleEvent:
    return AdaptiveLifecycleEvent(
        event_id=f"event-{suffix or event_type}",
        authority_id=authority_id,
        profile_id=profile_id,
        revision_id=revision_id,
        event_type=event_type,
        reason_code="lifecycle_test",
        explanation={},
        created_at=created_at,
    )


def assignment(
    *,
    assignment_id: str = "assignment-a",
    operation_identity_hash: str = "d" * 64,
    arm: str = "challenger",
) -> AdaptiveCanaryAssignment:
    return AdaptiveCanaryAssignment(
        assignment_id=assignment_id,
        authority_id=AUTHORITY_ID,
        profile_id=PROFILE_ID,
        operation_identity_hash=operation_identity_hash,
        context_bucket_id="e" * 64,
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        arm=arm,
        created_at="2026-07-18T12:00:20Z",
    )


def _publish_pair(store: RoutingStore) -> tuple[int, int]:
    first = store.publish_profile_revision(
        revision("revision-control"),
        expected_revision_id=None,
        expected_generation=0,
    )
    second = store.publish_profile_revision(
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
        expected_generation=first,
    )
    return first, second


def _promote_pair(store: RoutingStore) -> storage_module.AdaptiveProfileControl:
    _, generation = _publish_pair(store)
    validated = store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-control",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="validated",
        cooldown_until=None,
        rejection_count=0,
        expected_generation=generation,
        event=lifecycle_event("validated", "revision-challenger"),
    )
    canary = store.transition_profile_experiment(
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
            "canary", "revision-challenger", created_at="2026-07-18T12:00:11Z"
        ),
    )
    return store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-challenger",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="promoted",
        cooldown_until=None,
        rejection_count=0,
        expected_generation=canary.generation,
        event=lifecycle_event(
            "promoted", "revision-challenger", created_at="2026-07-18T12:00:12Z"
        ),
    )


def test_current_schema_preserves_adaptation_and_legacy_tables(
    store: RoutingStore,
) -> None:
    tables = {
        str(row[0])
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "adaptive_revisions",
        "active_adaptive_revisions",
        "adaptive_profile_revisions",
        "adaptive_profile_states",
        "adaptive_lifecycle_events",
        "adaptive_canary_assignments",
        "adaptive_optimizer_leases",
    } <= tables
    assert store.schema_version == int(storage_module.SCHEMA_VERSION)


def test_v7_database_missing_an_adaptation_table_is_rejected_before_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-v7-adaptation-table.db"
    with RoutingStore.open(path=path) as store:
        store.connection.execute("DROP TABLE adaptive_canary_assignments")

    with pytest.raises(UnsupportedSchemaVersion, match="v7 database.*missing"):
        RoutingStore.open(path=path)


def test_v7_database_missing_every_adaptation_table_is_rejected_before_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "empty-v7-adaptation-surface.db"
    with RoutingStore.open(path=path) as store:
        for table in storage_module._ADAPTATION_TABLES:
            store.connection.execute(f'DROP TABLE "{table}"')

    with pytest.raises(UnsupportedSchemaVersion, match="v7 database.*missing"):
        RoutingStore.open(path=path)


def test_publish_read_list_and_pointer_generation_cas(store: RoutingStore) -> None:
    control = revision("revision-control")
    challenger = revision(
        "revision-challenger",
        parent_revision_id=control.revision_id,
        created_at="2026-07-18T12:00:01Z",
    )

    assert store.read_profile_control(AUTHORITY_ID, PROFILE_ID).model_dump() == {
        "authority_id": AUTHORITY_ID,
        "profile_id": PROFILE_ID,
        "active_revision_id": None,
        "control_revision_id": None,
        "challenger_revision_id": None,
        "experiment_phase": "eligible",
        "frozen": False,
        "cooldown_until": None,
        "rejection_count": 0,
        "generation": 0,
        "updated_at": EPOCH,
    }
    assert (
        store.publish_profile_revision(
            control,
            expected_revision_id=None,
            expected_generation=0,
        )
        == 1
    )
    assert store.read_active_profile_revision(AUTHORITY_ID, PROFILE_ID) == (control, 1)
    assert store.read_profile_revision(control.revision_id) == control
    assert store.list_profile_revisions(AUTHORITY_ID, PROFILE_ID) == (control,)

    with pytest.raises(RevisionConflict):
        store.publish_profile_revision(
            challenger,
            expected_revision_id=None,
            expected_generation=0,
        )
    assert store.read_profile_revision(challenger.revision_id) is None


def test_publish_rejects_dangling_parent_without_mutating_pointer(
    store: RoutingStore,
) -> None:
    child = revision("revision-child", parent_revision_id="missing-parent")

    with pytest.raises(ImmutableRecordConflict, match="parent"):
        store.publish_profile_revision(
            child,
            expected_revision_id=None,
            expected_generation=0,
        )

    assert store.read_profile_revision(child.revision_id) is None
    assert store.read_active_profile_revision(AUTHORITY_ID, PROFILE_ID) == (None, 0)


def test_profile_revision_read_rejects_deleted_parent(store: RoutingStore) -> None:
    _publish_pair(store)
    store.connection.execute(
        "DELETE FROM adaptive_profile_revisions WHERE revision_id=?",
        ("revision-control",),
    )

    with pytest.raises(RevisionChecksumError, match="revision-challenger"):
        store.read_profile_revision("revision-challenger")


def test_publish_generation_is_shared_with_freeze_and_rejects_stale_state(
    store: RoutingStore,
) -> None:
    generation = store.publish_profile_revision(
        revision("revision-control"),
        expected_revision_id=None,
        expected_generation=0,
    )
    frozen = store.set_profile_freeze(
        AUTHORITY_ID,
        PROFILE_ID,
        frozen=True,
        expected_generation=generation,
    )
    assert frozen.generation == generation + 1

    with pytest.raises(storage_module.ProfileStateConflict):
        store.set_profile_freeze(
            AUTHORITY_ID,
            PROFILE_ID,
            frozen=False,
            expected_generation=generation,
        )
    with pytest.raises(storage_module.ProfileFrozen):
        store.publish_profile_revision(
            revision("revision-challenger", parent_revision_id="revision-control"),
            expected_revision_id="revision-control",
            expected_generation=frozen.generation,
        )


def test_transition_profile_experiment_enforces_exact_finite_graph(
    store: RoutingStore,
) -> None:
    _, generation = _publish_pair(store)

    validated = store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-control",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="validated",
        cooldown_until=None,
        rejection_count=0,
        expected_generation=generation,
        event=lifecycle_event("validated", "revision-challenger"),
    )
    assert validated.experiment_phase == "validated"
    assert validated.control_revision_id == "revision-control"
    assert validated.challenger_revision_id == "revision-challenger"

    canary = store.transition_profile_experiment(
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
            "canary", "revision-challenger", created_at="2026-07-18T12:00:11Z"
        ),
    )
    promoted = store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-challenger",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="promoted",
        cooldown_until=None,
        rejection_count=0,
        expected_generation=canary.generation,
        event=lifecycle_event(
            "promoted", "revision-challenger", created_at="2026-07-18T12:00:12Z"
        ),
    )
    cooldown = store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-challenger",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="cooldown",
        cooldown_until="2026-07-18T13:00:00Z",
        rejection_count=0,
        expected_generation=promoted.generation,
        event=lifecycle_event(
            "cooldown", "revision-challenger", created_at="2026-07-18T12:00:13Z"
        ),
    )
    eligible = store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-challenger",
        control_revision_id=None,
        challenger_revision_id=None,
        experiment_phase="eligible",
        cooldown_until=None,
        rejection_count=0,
        expected_generation=cooldown.generation,
        event=lifecycle_event(
            "eligible", "revision-challenger", created_at="2026-07-18T12:00:14Z"
        ),
    )
    assert eligible.experiment_phase == "eligible"
    assert eligible.control_revision_id is None
    assert [
        event.event_type
        for event in store.list_adaptive_lifecycle_events(AUTHORITY_ID, PROFILE_ID)
        if event.event_type
        in {"validated", "canary", "promoted", "cooldown", "eligible"}
    ] == ["validated", "canary", "promoted", "cooldown", "eligible"]


def test_transition_rejects_illegal_phase_stale_generation_and_wrong_event(
    store: RoutingStore,
) -> None:
    _, generation = _publish_pair(store)
    kwargs = {
        "active_revision_id": "revision-control",
        "control_revision_id": "revision-control",
        "challenger_revision_id": "revision-challenger",
        "cooldown_until": None,
        "rejection_count": 0,
    }
    with pytest.raises(storage_module.InvalidLifecycleTransition):
        store.transition_profile_experiment(
            AUTHORITY_ID,
            PROFILE_ID,
            **kwargs,
            experiment_phase="canary",
            expected_generation=generation,
            event=lifecycle_event("canary", "revision-challenger"),
        )
    with pytest.raises(ImmutableRecordConflict, match="event type"):
        store.transition_profile_experiment(
            AUTHORITY_ID,
            PROFILE_ID,
            **kwargs,
            experiment_phase="validated",
            expected_generation=generation,
            event=lifecycle_event("canary", "revision-challenger"),
        )

    current = store.read_profile_control(AUTHORITY_ID, PROFILE_ID)
    with pytest.raises(storage_module.ProfileStateConflict):
        store.transition_profile_experiment(
            AUTHORITY_ID,
            PROFILE_ID,
            **kwargs,
            experiment_phase="validated",
            expected_generation=current.generation - 1,
            event=lifecycle_event(
                "validated", "revision-challenger", suffix="stale"
            ),
        )


def test_rejected_branch_is_legal_and_keeps_exact_control_active(
    store: RoutingStore,
) -> None:
    _, generation = _publish_pair(store)
    validated = store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-control",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="validated",
        cooldown_until=None,
        rejection_count=0,
        expected_generation=generation,
        event=lifecycle_event("validated", "revision-challenger"),
    )
    canary = store.transition_profile_experiment(
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
            suffix="rejected-canary",
            created_at="2026-07-18T12:00:11Z",
        ),
    )
    rejected = store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-control",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="rejected",
        cooldown_until=None,
        rejection_count=1,
        expected_generation=canary.generation,
        event=lifecycle_event(
            "rejected",
            "revision-challenger",
            created_at="2026-07-18T12:00:12Z",
        ),
    )
    assert rejected.active_revision_id == "revision-control"
    assert rejected.rejection_count == 1
    cooldown = store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-control",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="cooldown",
        cooldown_until="2026-07-18T13:00:00Z",
        rejection_count=1,
        expected_generation=rejected.generation,
        event=lifecycle_event(
            "cooldown",
            "revision-control",
            suffix="rejected-cooldown",
            created_at="2026-07-18T12:00:13Z",
        ),
    )
    assert cooldown.active_revision_id == "revision-control"


def test_transition_requires_complete_same_profile_revisions(store: RoutingStore) -> None:
    _, generation = _publish_pair(store)
    other = revision(
        "revision-other",
        profile_id="research",
        created_at="2026-07-18T12:00:02Z",
    )
    store.publish_profile_revision(
        other,
        expected_revision_id=None,
        expected_generation=0,
    )

    with pytest.raises(ImmutableRecordConflict, match="profile"):
        store.transition_profile_experiment(
            AUTHORITY_ID,
            PROFILE_ID,
            active_revision_id="revision-control",
            control_revision_id="revision-control",
            challenger_revision_id="revision-other",
            experiment_phase="validated",
            cooldown_until=None,
            rejection_count=0,
            expected_generation=generation,
            event=lifecycle_event("validated", "revision-challenger"),
        )


def test_arbitrary_lifecycle_append_is_not_a_public_storage_api(
    store: RoutingStore,
) -> None:
    assert not hasattr(store, "append_adaptive_lifecycle_event")


def test_transition_rejects_replacement_pair_and_unrelated_event_revision(
    store: RoutingStore,
) -> None:
    _, generation = _publish_pair(store)
    generation = store.publish_profile_revision(
        revision(
            "revision-unrelated",
            parent_revision_id="revision-challenger",
            created_at="2026-07-18T12:00:02Z",
        ),
        expected_revision_id="revision-challenger",
        expected_generation=generation,
    )
    with pytest.raises(ImmutableRecordConflict, match="event revision"):
        store.transition_profile_experiment(
            AUTHORITY_ID,
            PROFILE_ID,
            active_revision_id="revision-control",
            control_revision_id="revision-control",
            challenger_revision_id="revision-challenger",
            experiment_phase="validated",
            cooldown_until=None,
            rejection_count=0,
            expected_generation=generation,
            event=lifecycle_event(
                "validated", "revision-control", suffix="wrong-validated-event"
            ),
        )
    validated = store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-control",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="validated",
        cooldown_until=None,
        rejection_count=0,
        expected_generation=generation,
        event=lifecycle_event("validated", "revision-challenger"),
    )

    with pytest.raises(ImmutableRecordConflict, match="preserve.*pair"):
        store.transition_profile_experiment(
            AUTHORITY_ID,
            PROFILE_ID,
            active_revision_id="revision-control",
            control_revision_id="revision-control",
            challenger_revision_id="revision-unrelated",
            experiment_phase="canary",
            cooldown_until=None,
            rejection_count=0,
            expected_generation=validated.generation,
            event=lifecycle_event(
                "canary",
                "revision-unrelated",
                suffix="replacement-pair",
                created_at="2026-07-18T12:00:11Z",
            ),
        )
    with pytest.raises(ImmutableRecordConflict, match="event revision"):
        store.transition_profile_experiment(
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
                "revision-unrelated",
                suffix="unrelated-event",
                created_at="2026-07-18T12:00:11Z",
            ),
        )


def test_canary_assignment_is_first_writer_wins_and_profile_linked(
    store: RoutingStore,
) -> None:
    _, generation = _publish_pair(store)
    validated = store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-control",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="validated",
        cooldown_until=None,
        rejection_count=0,
        expected_generation=generation,
        event=lifecycle_event("validated", "revision-challenger"),
    )
    store.transition_profile_experiment(
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
    first = assignment()
    assert store.get_or_create_canary_assignment(first) == first
    assert store.get_or_create_canary_assignment(
        first.model_copy(update={"assignment_id": "assignment-racer"})
    ) == first
    with pytest.raises(storage_module.InvalidLifecycleTransition, match="context"):
        store.get_or_create_canary_assignment(
            first.model_copy(
                update={
                    "assignment_id": "assignment-wrong-context",
                    "operation_identity_hash": "8" * 64,
                    "context_bucket_id": "f" * 64,
                }
            )
        )
    with pytest.raises(ImmutableRecordConflict):
        store.get_or_create_canary_assignment(
            first.model_copy(update={"arm": "control"})
        )
    with pytest.raises(storage_module.InvalidLifecycleTransition, match="experiment"):
        store.get_or_create_canary_assignment(
            first.model_copy(
                update={
                    "assignment_id": "assignment-other-profile",
                    "operation_identity_hash": "f" * 64,
                    "profile_id": "research",
                }
            )
        )
    current = store.read_profile_control(AUTHORITY_ID, PROFILE_ID)
    store.set_profile_freeze(
        AUTHORITY_ID,
        PROFILE_ID,
        frozen=True,
        expected_generation=current.generation,
    )
    with pytest.raises(storage_module.ProfileFrozen):
        store.get_or_create_canary_assignment(
            first.model_copy(
                update={
                    "assignment_id": "assignment-frozen",
                    "operation_identity_hash": "9" * 64,
                }
            )
        )


def test_optimizer_lease_expiration_takeover_and_owner_guard(store: RoutingStore) -> None:
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    lease_a = store.acquire_optimizer_lease(
        AUTHORITY_ID, PROFILE_ID, "owner-a", now, 10
    )
    assert lease_a is not None
    assert (
        store.acquire_optimizer_lease(
            AUTHORITY_ID, PROFILE_ID, "owner-b", now, 10
        )
        is None
    )
    lease_b = store.acquire_optimizer_lease(
        AUTHORITY_ID,
        PROFILE_ID,
        "owner-b",
        datetime(2026, 7, 18, 12, 0, 11, tzinfo=UTC),
        10,
    )
    assert lease_b is not None and lease_b.generation == lease_a.generation + 1
    assert store.release_optimizer_lease(lease_a) is False
    assert store.release_optimizer_lease(lease_b) is True


def test_optimizer_lease_scalar_tamper_is_detected_on_acquire_and_release(
    store: RoutingStore,
) -> None:
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    lease = store.acquire_optimizer_lease(
        AUTHORITY_ID, PROFILE_ID, "owner-a", now, 10
    )
    assert lease is not None
    columns = {
        str(row["name"])
        for row in store.connection.execute(
            "PRAGMA table_info(adaptive_optimizer_leases)"
        )
    }
    assert {"document_json", "checksum"} <= columns
    store.connection.execute(
        "UPDATE adaptive_optimizer_leases SET owner_id='owner-tampered' "
        "WHERE authority_id=? AND profile_id=?",
        (AUTHORITY_ID, PROFILE_ID),
    )
    with pytest.raises(RevisionChecksumError):
        store.acquire_optimizer_lease(
            AUTHORITY_ID,
            PROFILE_ID,
            "owner-b",
            datetime(2026, 7, 18, 12, 0, 11, tzinfo=UTC),
            10,
        )
    with pytest.raises(RevisionChecksumError):
        store.release_optimizer_lease(lease)
    store.connection.execute(
        "UPDATE adaptive_optimizer_leases SET owner_id='owner-a', checksum=? "
        "WHERE authority_id=? AND profile_id=?",
        ("0" * 64, AUTHORITY_ID, PROFILE_ID),
    )
    with pytest.raises(RevisionChecksumError):
        store.release_optimizer_lease(lease)


def test_existing_scalar_schema_v7_optimizer_lease_is_rejected_as_corrupt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v7-lease.db"
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    with RoutingStore.open(path=path) as store:
        lease = store.acquire_optimizer_lease(
            AUTHORITY_ID, PROFILE_ID, "owner-a", now, 10
        )
        assert lease is not None
        columns = {
            str(row["name"])
            for row in store.connection.execute(
                "PRAGMA table_info(adaptive_optimizer_leases)"
            )
        }
        if "document_json" in columns:
            store.connection.executescript(
                "ALTER TABLE adaptive_optimizer_leases "
                "RENAME TO adaptive_optimizer_leases_attested;"
                "CREATE TABLE adaptive_optimizer_leases ("
                "authority_id TEXT NOT NULL, profile_id TEXT NOT NULL, "
                "owner_id TEXT NOT NULL, lease_expires_at TEXT NOT NULL, "
                "generation INTEGER NOT NULL CHECK (generation > 0), "
                "updated_at TEXT NOT NULL, PRIMARY KEY (authority_id, profile_id));"
                "INSERT INTO adaptive_optimizer_leases "
                "SELECT authority_id, profile_id, owner_id, lease_expires_at, "
                "generation, updated_at FROM adaptive_optimizer_leases_attested;"
                "DROP TABLE adaptive_optimizer_leases_attested;"
            )

    with pytest.raises(UnsupportedSchemaVersion, match="v7 database"):
        RoutingStore.open(path=path)


def test_existing_initial_v7_profile_state_constraint_is_rejected_as_corrupt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v7-profile-state.db"
    with RoutingStore.open(path=path) as store:
        _, generation = _publish_pair(store)
        expected = store.read_profile_control(AUTHORITY_ID, PROFILE_ID)
        assert expected.generation == generation
        sql = str(
            store.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='adaptive_profile_states'"
            ).fetchone()[0]
        )
        old_sql = sql.replace(",\n                'rolled_back'", "")
        assert old_sql != sql
        store.connection.execute(
            "ALTER TABLE adaptive_profile_states "
            "RENAME TO adaptive_profile_states_with_rollback"
        )
        store.connection.execute(old_sql)
        store.connection.execute(
            "INSERT INTO adaptive_profile_states SELECT * "
            "FROM adaptive_profile_states_with_rollback"
        )
        store.connection.execute("DROP TABLE adaptive_profile_states_with_rollback")

    with pytest.raises(UnsupportedSchemaVersion, match="v7 database"):
        RoutingStore.open(path=path)


def test_exact_same_profile_rollback_works_while_frozen_and_is_audited(
    store: RoutingStore,
) -> None:
    first, second = _publish_pair(store)
    assert second == first + 1
    frozen = store.set_profile_freeze(
        AUTHORITY_ID,
        PROFILE_ID,
        frozen=True,
        expected_generation=second,
    )

    rolled_back = store.rollback_profile_revision(
        authority_id=AUTHORITY_ID,
        profile_id=PROFILE_ID,
        revision_id="revision-control",
        expected_target_checksum=stored_revision_checksum(store, "revision-control"),
        expected_generation=frozen.generation,
    )
    assert rolled_back.revision_id == "revision-control"
    control = store.read_profile_control(AUTHORITY_ID, PROFILE_ID)
    assert control.active_revision_id == "revision-control"
    assert control.frozen is True
    assert control.generation == frozen.generation + 1
    assert store.list_adaptive_lifecycle_events(AUTHORITY_ID, PROFILE_ID)[-1].event_type == (
        "rolled_back"
    )

    research_frozen = store.set_profile_freeze(
        AUTHORITY_ID,
        "research",
        frozen=True,
        expected_generation=0,
    )
    with pytest.raises(ImmutableRecordConflict, match="profile"):
        store.rollback_profile_revision(
            authority_id=AUTHORITY_ID,
            profile_id="research",
            revision_id="revision-control",
            expected_target_checksum=stored_revision_checksum(
                store, "revision-control"
            ),
            expected_generation=research_frozen.generation,
        )


def test_storage_rollback_requires_frozen_state_and_exact_target_checksum(
    store: RoutingStore,
) -> None:
    _first, generation = _publish_pair(store)
    target = store.read_profile_revision("revision-control")
    assert target is not None
    checksum = revision_checksum(target)

    with pytest.raises(storage_module.ProfileFrozen):
        store.rollback_profile_revision(
            authority_id=AUTHORITY_ID,
            profile_id=PROFILE_ID,
            revision_id=target.revision_id,
            expected_target_checksum=checksum,
            expected_generation=generation,
        )

    frozen = store.set_profile_freeze(
        AUTHORITY_ID,
        PROFILE_ID,
        frozen=True,
        expected_generation=generation,
    )
    with pytest.raises(RevisionChecksumError):
        store.rollback_profile_revision(
            authority_id=AUTHORITY_ID,
            profile_id=PROFILE_ID,
            revision_id=target.revision_id,
            expected_target_checksum="f" * 64,
            expected_generation=frozen.generation,
        )

    assert store.read_profile_control(AUTHORITY_ID, PROFILE_ID) == frozen
    state_before_tamper = tuple(
        store.connection.execute(
            "SELECT document_json, checksum FROM adaptive_profile_states "
            "WHERE authority_id=? AND profile_id=?",
            (AUTHORITY_ID, PROFILE_ID),
        ).fetchone()
    )
    store.connection.execute(
        "UPDATE adaptive_profile_revisions SET checksum=? WHERE revision_id=?",
        ("0" * 64, target.revision_id),
    )
    with pytest.raises(RevisionChecksumError):
        store.rollback_profile_revision(
            authority_id=AUTHORITY_ID,
            profile_id=PROFILE_ID,
            revision_id=target.revision_id,
            expected_target_checksum=checksum,
            expected_generation=frozen.generation,
        )
    assert tuple(
        store.connection.execute(
            "SELECT document_json, checksum FROM adaptive_profile_states "
            "WHERE authority_id=? AND profile_id=?",
            (AUTHORITY_ID, PROFILE_ID),
        ).fetchone()
    ) == state_before_tamper


def test_frozen_promoted_challenger_rolls_back_to_control_and_clears_pair(
    store: RoutingStore,
) -> None:
    promoted = _promote_pair(store)
    frozen = store.set_profile_freeze(
        AUTHORITY_ID,
        PROFILE_ID,
        frozen=True,
        expected_generation=promoted.generation,
    )

    target = store.rollback_profile_revision(
        authority_id=AUTHORITY_ID,
        profile_id=PROFILE_ID,
        revision_id="revision-control",
        expected_target_checksum=stored_revision_checksum(store, "revision-control"),
        expected_generation=frozen.generation,
    )

    assert target.revision_id == "revision-control"
    rolled_back = store.read_profile_control(AUTHORITY_ID, PROFILE_ID)
    assert rolled_back.active_revision_id == "revision-control"
    assert rolled_back.control_revision_id is None
    assert rolled_back.challenger_revision_id is None
    assert rolled_back.experiment_phase == "rolled_back"
    assert rolled_back.frozen is True
    assert rolled_back.generation == frozen.generation + 1
    event = store.list_adaptive_lifecycle_events(AUTHORITY_ID, PROFILE_ID)[-1]
    assert (event.event_type, event.revision_id) == (
        "rolled_back",
        "revision-control",
    )

    unfrozen = store.set_profile_freeze(
        AUTHORITY_ID,
        PROFILE_ID,
        frozen=False,
        expected_generation=rolled_back.generation,
    )
    eligible = store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-control",
        control_revision_id=None,
        challenger_revision_id=None,
        experiment_phase="eligible",
        cooldown_until=None,
        rejection_count=0,
        expected_generation=unfrozen.generation,
        event=lifecycle_event(
            "eligible",
            "revision-control",
            suffix="rollback-cleanup",
            created_at="2026-07-18T12:00:14Z",
        ),
    )
    assert eligible.experiment_phase == "eligible"


def test_frozen_rollback_accepts_exact_older_same_profile_revision(
    store: RoutingStore,
) -> None:
    first = store.publish_profile_revision(
        revision("revision-old"), expected_revision_id=None, expected_generation=0
    )
    second = store.publish_profile_revision(
        revision(
            "revision-control",
            parent_revision_id="revision-old",
            created_at="2026-07-18T12:00:01Z",
        ),
        expected_revision_id="revision-old",
        expected_generation=first,
    )
    third = store.publish_profile_revision(
        revision(
            "revision-challenger",
            parent_revision_id="revision-control",
            created_at="2026-07-18T12:00:02Z",
        ),
        expected_revision_id="revision-control",
        expected_generation=second,
    )
    frozen = store.set_profile_freeze(
        AUTHORITY_ID, PROFILE_ID, frozen=True, expected_generation=third
    )

    target = store.rollback_profile_revision(
        authority_id=AUTHORITY_ID,
        profile_id=PROFILE_ID,
        revision_id="revision-old",
        expected_target_checksum=stored_revision_checksum(store, "revision-old"),
        expected_generation=frozen.generation,
    )

    assert target.revision_id == "revision-old"
    state = store.read_profile_control(AUTHORITY_ID, PROFILE_ID)
    assert state.active_revision_id == "revision-old"
    assert state.experiment_phase == "rolled_back"
    assert state.frozen is True


def test_rollback_stale_generation_and_invalid_target_are_atomic(
    store: RoutingStore,
) -> None:
    _, generation = _publish_pair(store)
    store.publish_profile_revision(
        revision("revision-other-profile", profile_id="research"),
        expected_revision_id=None,
        expected_generation=0,
    )
    frozen = store.set_profile_freeze(
        AUTHORITY_ID,
        PROFILE_ID,
        frozen=True,
        expected_generation=generation,
    )
    events_before = store.list_adaptive_lifecycle_events(AUTHORITY_ID, PROFILE_ID)

    with pytest.raises(storage_module.ProfileStateConflict):
        store.rollback_profile_revision(
            authority_id=AUTHORITY_ID,
            profile_id=PROFILE_ID,
            revision_id="revision-control",
            expected_target_checksum=stored_revision_checksum(
                store, "revision-control"
            ),
            expected_generation=generation - 1,
        )
    with pytest.raises(ImmutableRecordConflict, match="does not exist"):
        store.rollback_profile_revision(
            authority_id=AUTHORITY_ID,
            profile_id=PROFILE_ID,
            revision_id="revision-missing",
            expected_target_checksum="f" * 64,
            expected_generation=frozen.generation,
        )
    with pytest.raises(ImmutableRecordConflict, match="profile"):
        store.rollback_profile_revision(
            authority_id=AUTHORITY_ID,
            profile_id=PROFILE_ID,
            revision_id="revision-other-profile",
            expected_target_checksum=stored_revision_checksum(
                store, "revision-other-profile"
            ),
            expected_generation=frozen.generation,
        )

    assert store.read_profile_control(AUTHORITY_ID, PROFILE_ID) == frozen
    assert store.list_adaptive_lifecycle_events(AUTHORITY_ID, PROFILE_ID) == events_before


def test_revision_and_state_tampering_or_noncanonical_json_is_detected(
    store: RoutingStore,
) -> None:
    store.publish_profile_revision(
        revision("revision-control"),
        expected_revision_id=None,
        expected_generation=0,
    )
    store.connection.execute(
        "UPDATE adaptive_profile_revisions SET overlay_json = ? WHERE revision_id = ?",
        ('{ "profile_id": "coding" }', "revision-control"),
    )
    with pytest.raises(RevisionChecksumError):
        store.read_profile_revision("revision-control")

    store.connection.execute(
        "UPDATE adaptive_profile_states SET checksum = ? "
        "WHERE authority_id = ? AND profile_id = ?",
        ("0" * 64, AUTHORITY_ID, PROFILE_ID),
    )
    with pytest.raises(RevisionChecksumError):
        store.read_profile_control(AUTHORITY_ID, PROFILE_ID)


def test_event_noncanonical_document_with_matching_checksum_is_rejected(
    store: RoutingStore,
) -> None:
    _, generation = _publish_pair(store)
    store.transition_profile_experiment(
        AUTHORITY_ID,
        PROFILE_ID,
        active_revision_id="revision-control",
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="validated",
        cooldown_until=None,
        rejection_count=0,
        expected_generation=generation,
        event=lifecycle_event("validated", "revision-challenger"),
    )
    event = store.list_adaptive_lifecycle_events(AUTHORITY_ID, PROFILE_ID)[-1]
    pretty = json.dumps(event.model_dump(mode="json"), indent=2)
    store.connection.execute(
        "UPDATE adaptive_lifecycle_events SET document_json = ?, checksum = ? "
        "WHERE event_id = ?",
        (pretty, hashlib.sha256(pretty.encode()).hexdigest(), event.event_id),
    )
    with pytest.raises(RevisionChecksumError):
        store.list_adaptive_lifecycle_events(AUTHORITY_ID, PROFILE_ID)


def test_storage_revalidates_constructed_content_sentinel(store: RoutingStore) -> None:
    unsafe = revision("revision-unsafe").model_dump(mode="json")
    unsafe["explanation"]["prompt"] = "PROMPT_SENTINEL raw user content"
    with pytest.raises((ValueError, TypeError)):
        store.publish_profile_revision(
            unsafe,  # type: ignore[arg-type]
            expected_revision_id=None,
            expected_generation=0,
        )


def test_partial_pre_v7_adaptation_schema_is_rejected_before_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "partial-v7.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta VALUES ('schema_version', '6');"
        "CREATE TABLE adaptive_profile_states (authority_id TEXT PRIMARY KEY);"
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(UnsupportedSchemaVersion, match="adaptive_profile_states"):
        RoutingStore.open(path=path)

    assert path.read_bytes() == before
