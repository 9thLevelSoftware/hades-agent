"""Immutable schema-v8 storage contracts for autonomous profile management."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from plugins.auto_routing.auto_routing import storage as storage_module
from plugins.auto_routing.auto_routing.models import (
    ManagementCanaryAssignment,
    ManagementConfigReceipt,
    ManagementControl,
    ManagementLifecycleEvent,
    ManagementPatch,
    ManagementProfileState,
    ManagementRevision,
    RankingPackMetadata,
)
from plugins.auto_routing.auto_routing.storage import (
    ImmutableRecordConflict,
    InvalidLifecycleTransition,
    RevisionChecksumError,
    RevisionConflict,
    RoutingStore,
    UnsupportedSchemaVersion,
)


MANAGEMENT_AUTHORITY_ID = "a" * 64
AUTHORITY_A = "b" * 64
AUTHORITY_B = "c" * 64
AUTHORITY_C = "d" * 64
PROFILE_ID = "coding"
DAY = "2026-07-19"
EPOCH = "1970-01-01T00:00:00.000000Z"


@pytest.fixture
def store(tmp_path: Path):
    with RoutingStore.open(path=tmp_path / "state.db") as opened:
        yield opened


def management_revision(
    revision_id: str = "management-control",
    *,
    parent_revision_id: str | None = None,
    preceding_authority_id: str = AUTHORITY_A,
    resulting_authority_id: str = AUTHORITY_B,
    management_epoch: int = 1,
    action: str = "fallback_reorder",
    created_at: str = "2026-07-19T12:00:00Z",
    after_runtime_ids: tuple[str, ...] = ("1" * 64, "2" * 64),
) -> ManagementRevision:
    return ManagementRevision(
        revision_id=revision_id,
        preceding_authority_id=preceding_authority_id,
        resulting_authority_id=resulting_authority_id,
        management_authority_id=MANAGEMENT_AUTHORITY_ID,
        parent_revision_id=parent_revision_id,
        ranking_pack=RankingPackMetadata(
            ranking_pack_id="ranking-pack-a",
            ranking_pack_sha256="e" * 64,
            schema_version="1",
            verified_at="2026-07-19T11:59:00Z",
        ),
        inventory_revision="inventory-a",
        inventory_fingerprint="f" * 64,
        management_epoch=management_epoch,
        action=action,
        patches=(
            ManagementPatch(
                profile_id=PROFILE_ID,
                before_runtime_ids=("1" * 64,),
                after_runtime_ids=after_runtime_ids,
                reason_codes=("ranking_upgrade",),
            ),
        ),
        runtime_scores=(("1" * 64, 0.7), ("2" * 64, 0.8)),
        created_at=created_at,
    )


def lifecycle_event(
    event_type: str,
    revision_id: str | None,
    *,
    suffix: str | None = None,
    created_at: str = "2026-07-19T12:00:10Z",
) -> ManagementLifecycleEvent:
    return ManagementLifecycleEvent(
        event_id=f"management-event-{suffix or event_type}",
        management_authority_id=MANAGEMENT_AUTHORITY_ID,
        profile_id=PROFILE_ID,
        revision_id=revision_id,
        event_type=event_type,
        reason_code="storage_test",
        created_at=created_at,
    )


def assignment(
    *,
    assignment_id: str = "management-assignment-a",
    operation_identity_hash: str = "9" * 64,
    arm: str = "challenger",
) -> ManagementCanaryAssignment:
    return ManagementCanaryAssignment(
        assignment_id=assignment_id,
        management_authority_id=MANAGEMENT_AUTHORITY_ID,
        profile_id=PROFILE_ID,
        operation_identity_hash=operation_identity_hash,
        control_revision_id="management-control",
        challenger_revision_id="management-challenger",
        arm=arm,
        phase="reserved",
        created_at="2026-07-19T12:00:20Z",
    )


def publish_pair(store: RoutingStore) -> tuple[ManagementRevision, ManagementRevision]:
    control = management_revision()
    challenger = management_revision(
        "management-challenger",
        parent_revision_id=control.revision_id,
        preceding_authority_id=control.resulting_authority_id,
        resulting_authority_id=AUTHORITY_C,
        management_epoch=2,
        action="propose_canary",
        created_at="2026-07-19T12:00:01Z",
        after_runtime_ids=("2" * 64, "1" * 64),
    )
    store.publish_management_revision(control)
    store.publish_management_revision(challenger)
    return control, challenger


def prepare_canary(store: RoutingStore) -> ManagementProfileState:
    control, challenger = publish_pair(store)
    eligible = store.transition_management_profile_state(
        profile_id=PROFILE_ID,
        authority_id=MANAGEMENT_AUTHORITY_ID,
        expected_generation=0,
        state=ManagementProfileState(
            management_authority_id=MANAGEMENT_AUTHORITY_ID,
            profile_id=PROFILE_ID,
            authority_id=control.resulting_authority_id,
            active_revision_id=control.revision_id,
            management_epoch=control.management_epoch,
            updated_at="2026-07-19T12:00:02Z",
        ),
        event=lifecycle_event(
            "proposed", control.revision_id, suffix="control", created_at="2026-07-19T12:00:02Z"
        ),
    )
    validated = store.transition_management_profile_state(
        profile_id=PROFILE_ID,
        authority_id=MANAGEMENT_AUTHORITY_ID,
        expected_generation=eligible.generation,
        state=ManagementProfileState(
            management_authority_id=MANAGEMENT_AUTHORITY_ID,
            profile_id=PROFILE_ID,
            authority_id=control.resulting_authority_id,
            active_revision_id=control.revision_id,
            management_epoch=challenger.management_epoch,
            control_revision_id=control.revision_id,
            challenger_revision_id=challenger.revision_id,
            experiment_phase="validated",
            updated_at="2026-07-19T12:00:03Z",
        ),
        event=lifecycle_event(
            "validated", challenger.revision_id, created_at="2026-07-19T12:00:03Z"
        ),
    )
    return store.transition_management_profile_state(
        profile_id=PROFILE_ID,
        authority_id=MANAGEMENT_AUTHORITY_ID,
        expected_generation=validated.generation,
        state=validated.model_copy(
            update={
                "experiment_phase": "canary",
                "updated_at": "2026-07-19T12:00:11Z",
            }
        ),
        event=lifecycle_event(
            "canary", challenger.revision_id, created_at="2026-07-19T12:00:11Z"
        ),
    )


def test_current_schema_creates_complete_management_surface(store: RoutingStore) -> None:
    tables = {
        str(row["name"])
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "management_controls",
        "management_profile_states",
        "management_revisions",
        "management_lifecycle_events",
        "management_canary_assignments",
        "management_leases",
        "management_config_receipts",
        "management_lifecycle_finalizations",
    } <= tables
    assert store.schema_version == int(storage_module.SCHEMA_VERSION)


def test_v7_database_migrates_without_changing_adaptation_rows(tmp_path: Path) -> None:
    path = tmp_path / "v7.db"
    with RoutingStore.open(path=path) as initial:
        initial.connection.execute(
            "INSERT INTO adaptive_optimizer_leases "
            "(authority_id, profile_id, owner_id, lease_expires_at, generation, "
            "updated_at, document_json, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "1" * 64,
                PROFILE_ID,
                "owner-a",
                "2026-07-19T13:00:00Z",
                1,
                "2026-07-19T12:00:00Z",
                '{"authority_id":"1111111111111111111111111111111111111111111111111111111111111111","generation":1,"lease_expires_at":"2026-07-19T13:00:00Z","owner_id":"owner-a","profile_id":"coding","updated_at":"2026-07-19T12:00:00Z"}',
                "unused",
            ),
        )
        document = initial.connection.execute(
            "SELECT document_json FROM adaptive_optimizer_leases"
        ).fetchone()[0]
        initial.connection.execute(
            "UPDATE adaptive_optimizer_leases SET checksum=?",
            (hashlib.sha256(str(document).encode()).hexdigest(),),
        )
        for table in storage_module._MANAGEMENT_TABLES:
            initial.connection.execute(f'DROP TABLE "{table}"')
        initial.connection.execute(
            "UPDATE schema_meta SET value='7' WHERE key='schema_version'"
        )

    with RoutingStore.open(path=path) as migrated:
        assert migrated.schema_version == int(storage_module.SCHEMA_VERSION)
        assert migrated.connection.execute(
            "SELECT owner_id FROM adaptive_optimizer_leases"
        ).fetchone()[0] == "owner-a"


def test_v8_database_adds_lifecycle_finalization_journal(tmp_path: Path) -> None:
    path = tmp_path / "v8.db"
    with RoutingStore.open(path=path) as initial:
        initial.connection.execute("DROP TABLE management_lifecycle_finalizations")
        initial.connection.execute(
            "UPDATE schema_meta SET value='8' WHERE key='schema_version'"
        )

    with RoutingStore.open(path=path) as migrated:
        assert migrated.connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='management_lifecycle_finalizations'"
        ).fetchone() is not None
        assert migrated.schema_version == int(storage_module.SCHEMA_VERSION)


def test_partial_pre_v8_management_schema_is_rejected_before_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "partial-v8.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta VALUES ('schema_version', '7');"
        "CREATE TABLE management_controls (management_authority_id TEXT PRIMARY KEY);"
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(UnsupportedSchemaVersion, match="management.*missing"):
        RoutingStore.open(path=path)

    assert path.read_bytes() == before


def test_v8_missing_management_table_is_rejected_before_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-v8.db"
    with RoutingStore.open(path=path) as initial:
        initial.connection.execute("DROP TABLE management_leases")

    with pytest.raises(UnsupportedSchemaVersion, match="v8 database.*missing"):
        RoutingStore.open(path=path)


def test_publish_read_and_same_revision_id_is_immutable(store: RoutingStore) -> None:
    revision = management_revision()

    assert store.publish_management_revision(revision) == revision
    assert store.publish_management_revision(revision) == revision
    assert store.read_management_revision(revision.revision_id) == revision

    changed = revision.model_copy(update={"resulting_authority_id": AUTHORITY_C})
    with pytest.raises(ImmutableRecordConflict):
        store.publish_management_revision(changed)


def test_revision_parent_authority_and_epoch_are_validated_atomically(
    store: RoutingStore,
) -> None:
    control = management_revision()
    store.publish_management_revision(control)
    invalid = management_revision(
        "management-invalid-child",
        parent_revision_id=control.revision_id,
        preceding_authority_id=AUTHORITY_A,
        resulting_authority_id=AUTHORITY_C,
        management_epoch=3,
    )

    with pytest.raises(ImmutableRecordConflict, match="parent|epoch|authority"):
        store.publish_management_revision(invalid)

    assert store.read_management_revision(invalid.revision_id) is None


def test_second_root_revision_cannot_bypass_management_parent_lineage(
    store: RoutingStore,
) -> None:
    root = management_revision()
    store.publish_management_revision(root)
    unlinked = management_revision(
        "management-unlinked-root",
        preceding_authority_id=root.resulting_authority_id,
        resulting_authority_id=AUTHORITY_C,
        management_epoch=2,
        created_at="2026-07-19T12:00:01Z",
    )

    with pytest.raises(ImmutableRecordConflict, match="parent|root"):
        store.publish_management_revision(unlinked)
    assert store.read_management_revision(unlinked.revision_id) is None


def test_canonical_forged_second_root_is_rejected_on_read(
    store: RoutingStore,
) -> None:
    _control, challenger = publish_pair(store)
    forged = challenger.model_copy(update={"parent_revision_id": None})
    document_json = storage_module._canonical_json(forged)
    store.connection.execute(
        "UPDATE management_revisions SET parent_revision_id=NULL, "
        "document_json=?, checksum=? WHERE revision_id=?",
        (
            document_json,
            storage_module._checksum(document_json),
            challenger.revision_id,
        ),
    )

    with pytest.raises(RevisionChecksumError):
        store.read_management_revision(challenger.revision_id)


def test_management_revision_storage_is_content_free_and_tamper_evident(
    store: RoutingStore,
) -> None:
    revision = management_revision()
    store.publish_management_revision(revision)
    columns = {
        str(row["name"])
        for row in store.connection.execute("PRAGMA table_info(management_revisions)")
    }
    assert not ({"config", "config_json", "raw_config", "provider_payload"} & columns)
    assert b"PROMPT_SENTINEL" not in store.path.read_bytes()

    row = store.connection.execute(
        "SELECT document_json FROM management_revisions WHERE revision_id=?",
        (revision.revision_id,),
    ).fetchone()
    pretty = json.dumps(json.loads(str(row[0])), indent=2)
    store.connection.execute(
        "UPDATE management_revisions SET document_json=?, checksum=? WHERE revision_id=?",
        (pretty, hashlib.sha256(pretty.encode()).hexdigest(), revision.revision_id),
    )
    with pytest.raises(RevisionChecksumError):
        store.read_management_revision(revision.revision_id)


def test_control_and_profile_transitions_use_generation_cas_and_atomic_events(
    store: RoutingStore,
) -> None:
    initial = store.read_management_control(MANAGEMENT_AUTHORITY_ID)
    assert (initial.frozen, initial.generation, initial.updated_at) == (False, 0, EPOCH)
    frozen = store.transition_management_control(
        control=initial.model_copy(
            update={"frozen": True, "updated_at": "2026-07-19T12:00:01Z"}
        ),
        expected_generation=0,
        event=lifecycle_event(
            "frozen",
            None,
            suffix="global-freeze",
            created_at="2026-07-19T12:00:01Z",
        ),
    )
    assert (frozen.frozen, frozen.generation) == (True, 1)

    with pytest.raises(RevisionConflict):
        store.transition_management_control(
            control=frozen.model_copy(
                update={"frozen": False, "updated_at": "2026-07-19T12:00:02Z"}
            ),
            expected_generation=0,
            event=lifecycle_event(
                "unfrozen",
                None,
                suffix="stale-unfreeze",
                created_at="2026-07-19T12:00:02Z",
            ),
        )
    assert "management-event-stale-unfreeze" not in {
        item.event_id
        for item in store.list_management_lifecycle_events(
            MANAGEMENT_AUTHORITY_ID, PROFILE_ID
        )
    }

    control_revision = management_revision()
    store.publish_management_revision(control_revision)
    state = store.transition_management_profile_state(
        profile_id=PROFILE_ID,
        authority_id=MANAGEMENT_AUTHORITY_ID,
        expected_generation=0,
        state=ManagementProfileState(
            management_authority_id=MANAGEMENT_AUTHORITY_ID,
            profile_id=PROFILE_ID,
            authority_id=control_revision.resulting_authority_id,
            active_revision_id=control_revision.revision_id,
            management_epoch=1,
            updated_at="2026-07-19T12:00:02Z",
        ),
        event=lifecycle_event(
            "proposed",
            control_revision.revision_id,
            created_at="2026-07-19T12:00:02Z",
        ),
    )
    assert state.generation == 1
    assert store.read_management_profile_state(
        MANAGEMENT_AUTHORITY_ID, PROFILE_ID
    ) == state


def test_daily_cap_counts_only_admitted_revisions_and_records_hold(
    store: RoutingStore,
) -> None:
    first = management_revision("management-admitted")
    held = management_revision(
        "management-held",
        resulting_authority_id=AUTHORITY_C,
        created_at="2026-07-19T12:00:01Z",
    )

    assert store.try_admit_management_revision(
        profile_id=PROFILE_ID, utc_day=DAY, daily_limit=1, revision=first
    )
    assert not store.try_admit_management_revision(
        profile_id=PROFILE_ID, utc_day=DAY, daily_limit=1, revision=held
    )
    assert store.management_daily_admissions(PROFILE_ID, DAY) == 1
    assert store.read_management_revision(first.revision_id) == first
    assert store.read_management_revision(held.revision_id) is None
    assert any(
        event.event_type == "hold" and event.reason_code == "daily_cap_reached"
        for event in store.list_management_lifecycle_events(
            MANAGEMENT_AUTHORITY_ID, PROFILE_ID
        )
    )


def test_transition_and_rollback_revisions_do_not_consume_daily_admission(
    store: RoutingStore,
) -> None:
    admitted = management_revision("management-admitted")
    rollback = management_revision(
        "management-rollback",
        parent_revision_id=admitted.revision_id,
        preceding_authority_id=admitted.resulting_authority_id,
        resulting_authority_id=AUTHORITY_A,
        management_epoch=2,
        action="rollback",
        created_at="2026-07-19T12:00:01Z",
    )

    assert store.try_admit_management_revision(
        profile_id=PROFILE_ID,
        utc_day=DAY,
        daily_limit=1,
        revision=admitted,
    )
    assert store.try_admit_management_revision(
        profile_id=PROFILE_ID,
        utc_day=DAY,
        daily_limit=1,
        revision=rollback,
    )
    assert store.management_daily_admissions(PROFILE_ID, DAY) == 1
    assert store.read_management_revision(rollback.revision_id) == rollback


def test_batch_admission_counts_each_patched_profile_atomically(
    store: RoutingStore,
) -> None:
    base = management_revision("management-batch")
    batch = base.model_copy(
        update={
            "patches": (
                *base.patches,
                ManagementPatch(
                    profile_id="research",
                    before_runtime_ids=("3" * 64,),
                    after_runtime_ids=("4" * 64, "3" * 64),
                    reason_codes=("ranking_upgrade",),
                ),
            )
        }
    )

    assert store.try_admit_management_revision(
        profile_id=PROFILE_ID,
        utc_day=DAY,
        daily_limit=1,
        revision=batch,
    )
    assert store.management_daily_admissions(PROFILE_ID, DAY) == 1
    assert store.management_daily_admissions("research", DAY) == 1


def test_one_lifecycle_event_cannot_drive_two_profile_cas_transitions(
    store: RoutingStore,
) -> None:
    revision = management_revision()
    store.publish_management_revision(revision)
    event = lifecycle_event(
        "proposed",
        revision.revision_id,
        suffix="one-shot",
        created_at="2026-07-19T12:00:02Z",
    )
    first = store.transition_management_profile_state(
        profile_id=PROFILE_ID,
        authority_id=MANAGEMENT_AUTHORITY_ID,
        expected_generation=0,
        state=ManagementProfileState(
            management_authority_id=MANAGEMENT_AUTHORITY_ID,
            profile_id=PROFILE_ID,
            authority_id=revision.resulting_authority_id,
            active_revision_id=revision.revision_id,
            management_epoch=revision.management_epoch,
            updated_at=event.created_at,
        ),
        event=event,
    )

    with pytest.raises(ImmutableRecordConflict, match="event"):
        store.transition_management_profile_state(
            profile_id=PROFILE_ID,
            authority_id=MANAGEMENT_AUTHORITY_ID,
            expected_generation=first.generation,
            state=first,
            event=event,
        )
    assert store.read_management_profile_state(
        MANAGEMENT_AUTHORITY_ID, PROFILE_ID
    ) == first


def test_profile_transition_rejects_event_timestamp_and_revision_mismatch(
    store: RoutingStore,
) -> None:
    revision = management_revision()
    store.publish_management_revision(revision)
    state = ManagementProfileState(
        management_authority_id=MANAGEMENT_AUTHORITY_ID,
        profile_id=PROFILE_ID,
        authority_id=revision.resulting_authority_id,
        active_revision_id=revision.revision_id,
        management_epoch=revision.management_epoch,
        updated_at="2026-07-19T12:00:02Z",
    )

    with pytest.raises(ImmutableRecordConflict, match="timestamp"):
        store.transition_management_profile_state(
            profile_id=PROFILE_ID,
            authority_id=MANAGEMENT_AUTHORITY_ID,
            expected_generation=0,
            state=state,
            event=lifecycle_event(
                "proposed",
                revision.revision_id,
                suffix="wrong-time",
                created_at="2026-07-19T12:00:03Z",
            ),
        )
    with pytest.raises(ImmutableRecordConflict, match="revision"):
        store.transition_management_profile_state(
            profile_id=PROFILE_ID,
            authority_id=MANAGEMENT_AUTHORITY_ID,
            expected_generation=0,
            state=state,
            event=lifecycle_event(
                "proposed",
                None,
                suffix="wrong-revision",
                created_at=state.updated_at,
            ),
        )
    assert store.read_management_profile_state(
        MANAGEMENT_AUTHORITY_ID, PROFILE_ID
    ).generation == 0


def test_profile_state_epoch_must_equal_active_revision_when_pair_is_clear(
    store: RoutingStore,
) -> None:
    revision = management_revision()
    store.publish_management_revision(revision)
    first = store.transition_management_profile_state(
        profile_id=PROFILE_ID,
        authority_id=MANAGEMENT_AUTHORITY_ID,
        expected_generation=0,
        state=ManagementProfileState(
            management_authority_id=MANAGEMENT_AUTHORITY_ID,
            profile_id=PROFILE_ID,
            authority_id=revision.resulting_authority_id,
            active_revision_id=revision.revision_id,
            management_epoch=revision.management_epoch,
            updated_at="2026-07-19T12:00:02Z",
        ),
        event=lifecycle_event(
            "proposed",
            revision.revision_id,
            suffix="initial-epoch",
            created_at="2026-07-19T12:00:02Z",
        ),
    )

    with pytest.raises(RevisionChecksumError):
        store.transition_management_profile_state(
            profile_id=PROFILE_ID,
            authority_id=MANAGEMENT_AUTHORITY_ID,
            expected_generation=first.generation,
            state=first.model_copy(
                update={
                    "management_epoch": first.management_epoch + 1,
                    "updated_at": "2026-07-19T12:00:03Z",
                }
            ),
            event=lifecycle_event(
                "proposed",
                revision.revision_id,
                suffix="inflated-epoch",
                created_at="2026-07-19T12:00:03Z",
            ),
        )

def test_assignment_reservation_finalization_and_event_terminalization(
    store: RoutingStore,
) -> None:
    canary = prepare_canary(store)
    reserved = store.reserve_management_assignment(
        assignment(), expected_generation=canary.generation
    )
    assert reserved.phase == "reserved"
    finalized = store.finalize_management_assignment(
        assignment_id=reserved.assignment_id,
        runtime_id="2" * 64,
        reasoning_effort="medium",
        expected_generation=canary.generation,
    )
    crash_left = store.reserve_management_assignment(
        assignment(
            assignment_id="management-assignment-crash-left",
            operation_identity_hash="8" * 64,
            arm="control",
        ),
        expected_generation=canary.generation,
    )
    assert (finalized.phase, finalized.runtime_id, finalized.reasoning_effort) == (
        "finalized",
        "2" * 64,
        "medium",
    )
    assert store.list_open_management_assignments(
        MANAGEMENT_AUTHORITY_ID, PROFILE_ID
    ) == (finalized, crash_left)

    cooldown = store.transition_management_profile_state(
        profile_id=PROFILE_ID,
        authority_id=MANAGEMENT_AUTHORITY_ID,
        expected_generation=canary.generation,
        state=canary.model_copy(
            update={
                "authority_id": AUTHORITY_C,
                "active_revision_id": "management-challenger",
                "experiment_phase": "cooldown",
                "cooldown_until": "2026-07-19T13:00:00Z",
                "updated_at": "2026-07-19T12:00:30Z",
            }
        ),
        event=lifecycle_event(
            "promoted",
            "management-challenger",
            created_at="2026-07-19T12:00:30Z",
        ),
    )
    assert cooldown.generation == canary.generation + 1
    assert store.list_open_management_assignments(
        MANAGEMENT_AUTHORITY_ID, PROFILE_ID
    ) == ()
    terminal = store.read_management_assignment(finalized.assignment_id)
    assert terminal is not None and terminal.phase == "terminal"
    assert store.read_management_assignment(crash_left.assignment_id) is None


def test_speculative_management_reservation_can_be_discarded_by_exact_identity(
    store: RoutingStore,
) -> None:
    canary = prepare_canary(store)
    reserved = store.reserve_management_assignment(
        assignment(), expected_generation=canary.generation
    )

    discarded = store.discard_management_reservation(
        reserved.assignment_id,
        expected_generation=canary.generation,
        expected_management_authority_id=reserved.management_authority_id,
        expected_profile_id=reserved.profile_id,
        expected_operation_identity_hash=reserved.operation_identity_hash,
    )

    assert discarded is True
    assert store.read_management_assignment(reserved.assignment_id) is None
    assert store.list_open_management_assignments(
        MANAGEMENT_AUTHORITY_ID, PROFILE_ID
    ) == ()


def test_finalized_or_identity_changed_management_assignment_cannot_be_discarded(
    store: RoutingStore,
) -> None:
    canary = prepare_canary(store)
    reserved = store.reserve_management_assignment(
        assignment(), expected_generation=canary.generation
    )
    finalized = store.finalize_management_assignment(
        assignment_id=reserved.assignment_id,
        runtime_id="2" * 64,
        reasoning_effort="medium",
        expected_generation=canary.generation,
    )

    assert not store.discard_management_reservation(
        finalized.assignment_id,
        expected_generation=canary.generation,
        expected_management_authority_id=finalized.management_authority_id,
        expected_profile_id=finalized.profile_id,
        expected_operation_identity_hash=finalized.operation_identity_hash,
    )
    assert not store.discard_management_reservation(
        finalized.assignment_id,
        expected_generation=canary.generation,
        expected_management_authority_id=finalized.management_authority_id,
        expected_profile_id=finalized.profile_id,
        expected_operation_identity_hash="8" * 64,
    )
    assert store.read_management_assignment(finalized.assignment_id) == finalized


def test_lease_is_expiring_and_owner_generation_guarded(store: RoutingStore) -> None:
    publish_pair(store)
    lease = store.acquire_management_lease(
        MANAGEMENT_AUTHORITY_ID,
        PROFILE_ID,
        "owner-a",
        "2026-07-19T12:00:00Z",
        10.0,
    )
    assert lease is not None
    assert store.acquire_management_lease(
        MANAGEMENT_AUTHORITY_ID,
        PROFILE_ID,
        "owner-b",
        "2026-07-19T12:00:05Z",
        10.0,
    ) is None
    replacement = store.acquire_management_lease(
        MANAGEMENT_AUTHORITY_ID,
        PROFILE_ID,
        "owner-b",
        "2026-07-19T12:00:11Z",
        10.0,
    )
    assert replacement is not None and replacement.generation == lease.generation + 1
    assert not store.release_management_lease(lease)
    assert store.release_management_lease(replacement)


def test_receipt_phases_are_checksum_validated_and_cas_recovered(
    store: RoutingStore,
) -> None:
    revision = management_revision()
    store.publish_management_revision(revision)
    prepared = ManagementConfigReceipt(
        receipt_id="management-receipt-a",
        revision_id=revision.revision_id,
        phase="prepared",
        preceding_authority_id=revision.preceding_authority_id,
        resulting_authority_id=revision.resulting_authority_id,
        backup_checksum="8" * 64,
        created_at="2026-07-19T12:00:40Z",
        updated_at="2026-07-19T12:00:40Z",
    )
    assert store.record_management_receipt(prepared) == prepared
    replaced = store.recover_management_receipt(
        prepared.model_copy(
            update={
                "phase": "config_replaced",
                "updated_at": "2026-07-19T12:00:41Z",
            }
        ),
        expected_phase="prepared",
    )
    assert replaced.phase == "config_replaced"
    with pytest.raises(RevisionConflict):
        store.recover_management_receipt(
            replaced.model_copy(
                update={"phase": "committed", "updated_at": "2026-07-19T12:00:42Z"}
            ),
            expected_phase="prepared",
        )
    assert store.read_management_receipt(prepared.receipt_id) == replaced


def test_receipt_rejects_direct_transitions_and_records_restoration_evidence(
    store: RoutingStore,
) -> None:
    revision = management_revision()
    store.publish_management_revision(revision)
    prepared = ManagementConfigReceipt(
        receipt_id="management-receipt-restoration",
        revision_id=revision.revision_id,
        phase="prepared",
        preceding_authority_id=revision.preceding_authority_id,
        resulting_authority_id=revision.resulting_authority_id,
        backup_checksum="7" * 64,
        created_at="2026-07-19T12:00:40Z",
        updated_at="2026-07-19T12:00:40Z",
    )
    store.record_management_receipt(prepared)

    with pytest.raises(InvalidLifecycleTransition):
        store.recover_management_receipt(
            prepared.model_copy(
                update={"phase": "committed", "updated_at": "2026-07-19T12:00:41Z"}
            ),
            expected_phase="prepared",
        )

    recovery_required = store.recover_management_receipt(
        prepared.model_copy(
            update={
                "phase": "recovery_required",
                "updated_at": "2026-07-19T12:00:42Z",
            }
        ),
        expected_phase="prepared",
    )
    assert (
        recovery_required.preceding_authority_id,
        recovery_required.resulting_authority_id,
        recovery_required.backup_checksum,
    ) == (
        prepared.preceding_authority_id,
        prepared.resulting_authority_id,
        prepared.backup_checksum,
    )
    with pytest.raises(InvalidLifecycleTransition):
        store.recover_management_receipt(
            recovery_required.model_copy(
                update={
                    "phase": "config_replaced",
                    "updated_at": "2026-07-19T12:00:43Z",
                }
            ),
            expected_phase="recovery_required",
        )

    restore_started = ManagementLifecycleEvent(
        event_id=storage_module.management_restore_started_event_id(
            receipt_id=recovery_required.receipt_id,
            failed_revision_id=revision.revision_id,
            profile_id=PROFILE_ID,
            restored_authority_id=recovery_required.preceding_authority_id,
            backup_checksum=recovery_required.backup_checksum,
        ),
        management_authority_id=revision.management_authority_id,
        profile_id=PROFILE_ID,
        revision_id=revision.revision_id,
        event_type="hold",
        reason_code="config_restore_started",
        created_at=recovery_required.updated_at,
    )
    assert store.record_management_restore_started_event(
        receipt_id=recovery_required.receipt_id,
        failed_revision_id=revision.revision_id,
        restored_authority_id=recovery_required.preceding_authority_id,
        backup_checksum=recovery_required.backup_checksum,
        event=restore_started,
    ) == restore_started
    assert store.record_management_restore_started_event(
        receipt_id=recovery_required.receipt_id,
        failed_revision_id=revision.revision_id,
        restored_authority_id=recovery_required.preceding_authority_id,
        backup_checksum=recovery_required.backup_checksum,
        event=restore_started,
    ) == restore_started
    with pytest.raises(ImmutableRecordConflict):
        store.record_management_restore_started_event(
            receipt_id=recovery_required.receipt_id,
            failed_revision_id=revision.revision_id,
            restored_authority_id=recovery_required.preceding_authority_id,
            backup_checksum="6" * 64,
            event=restore_started,
        )
    with pytest.raises(InvalidLifecycleTransition):
        store.recover_management_receipt(
            recovery_required.model_copy(
                update={
                    "phase": "committed",
                    "updated_at": "2026-07-19T12:00:44Z",
                }
            ),
            expected_phase="recovery_required",
        )
