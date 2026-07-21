"""Profile-local Stage 4 adaptation operator controls."""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from plugins.auto_routing.auto_routing.cli import (
    CommandWriteClass,
    build_parser,
    command_metadata,
    execute,
)
from plugins.auto_routing.auto_routing.config import (
    authority_revision,
    config_document,
)
from plugins.auto_routing.auto_routing.models import (
    AdaptiveExplanation,
    AdaptiveOverlay,
    AdaptiveProfileRevision,
)
from plugins.auto_routing.auto_routing.service import AutoRoutingService
from plugins.auto_routing.auto_routing.storage import RoutingStore
from tests.plugins.auto_routing.test_adaptation_lifecycle import _adaptive_config


@pytest.fixture
def adaptation_service(tmp_path: Path):
    config = _adaptive_config()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(config)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    store = RoutingStore.open(path=tmp_path / "auto-routing" / "state.db")
    service = AutoRoutingService(
        plugin_context=None,
        hermes_home=tmp_path,
        store=store,
        adapter=None,
        _pinned_config_path=config_path,
    )
    try:
        yield service
    finally:
        store.close()


def _run(service: AutoRoutingService, *arguments: str):
    parser = argparse.ArgumentParser(prog="hermes auto-routing")
    build_parser(parser)
    return execute(parser.parse_args(list(arguments)), service=service)


def _publish_revisions(service: AutoRoutingService) -> tuple[str, str]:
    config = service._configured_authority()
    authority_id = authority_revision(config)
    profile = config.profiles["coding"]
    first = service._static_profile_revision(
        authority_id,
        profile,
        "2026-07-18T12:00:00Z",
    )
    generation = service.store.publish_profile_revision(
        first,
        expected_revision_id=None,
        expected_generation=0,
    )
    ordered = tuple(reversed(first.overlay.ordered_primary_runtime_ids))
    second = AdaptiveProfileRevision(
        revision_id="revision-challenger",
        authority_id=authority_id,
        profile_id="coding",
        parent_revision_id=first.revision_id,
        overlay=AdaptiveOverlay(
            profile_id="coding",
            ordered_primary_runtime_ids=ordered,
            reasoning_defaults={
                runtime_id: first.overlay.reasoning_defaults[runtime_id]
                for runtime_id in ordered
            },
        ),
        explanation=AdaptiveExplanation(
            reason_codes=("test_revision",),
            control_revision_id=first.revision_id,
        ),
        lifecycle="validated",
        created_at="2026-07-18T12:00:01Z",
    )
    service.store.publish_profile_revision(
        second,
        expected_revision_id=first.revision_id,
        expected_generation=generation,
    )
    return first.revision_id, second.revision_id


@pytest.mark.parametrize(
    ("name", "write_class"),
    (
        ("adapt status", CommandWriteClass.READ_ONLY),
        ("adapt history", CommandWriteClass.READ_ONLY),
        ("adapt freeze", CommandWriteClass.GUARDED_CONTROL_PLANE),
        ("adapt unfreeze", CommandWriteClass.GUARDED_CONTROL_PLANE),
        ("adapt rollback", CommandWriteClass.GUARDED_CONTROL_PLANE),
    ),
)
def test_adapt_leaf_metadata(name: str, write_class: CommandWriteClass) -> None:
    assert command_metadata(name).write_class is write_class


def test_adapt_status_and_history_are_read_only(adaptation_service) -> None:
    revision_a, _revision_b = _publish_revisions(adaptation_service)
    before = adaptation_service.store.connection.total_changes

    status = _run(
        adaptation_service,
        "adapt",
        "status",
        "--profile-id",
        "coding",
        "--json",
    )
    history = _run(
        adaptation_service,
        "adapt",
        "history",
        "--profile-id",
        "coding",
        "--json",
    )

    assert status.exit_code == history.exit_code == 0
    assert status.payload["profile_id"] == "coding"
    assert status.payload["enabled"] is True
    assert status.payload["active_revision_id"] == "revision-challenger"
    assert [item["revision_id"] for item in history.payload["revisions"]] == [
        revision_a,
        "revision-challenger",
    ]
    assert adaptation_service.store.connection.total_changes == before


def test_freeze_preview_requires_exact_hash(adaptation_service) -> None:
    _publish_revisions(adaptation_service)
    preview = _run(
        adaptation_service,
        "adapt",
        "freeze",
        "--profile-id",
        "coding",
        "--json",
    )

    missing = _run(
        adaptation_service,
        "adapt",
        "freeze",
        "--profile-id",
        "coding",
        "--apply",
        "--json",
    )
    applied = _run(
        adaptation_service,
        "adapt",
        "freeze",
        "--profile-id",
        "coding",
        "--apply",
        "--expect-hash",
        preview.payload["precondition_hash"],
        "--json",
    )

    assert preview.exit_code == 0 and preview.payload["apply"] is False
    assert missing.exit_code != 0
    assert applied.exit_code == 0 and applied.payload["frozen"] is True


def test_control_hash_is_stale_after_profile_generation_changes(
    adaptation_service,
) -> None:
    _publish_revisions(adaptation_service)
    stale = _run(
        adaptation_service,
        "adapt",
        "freeze",
        "--profile-id",
        "coding",
        "--json",
    ).payload["precondition_hash"]
    control = adaptation_service.adaptation_status("coding")
    adaptation_service.store.set_profile_freeze(
        control["authority_id"],
        "coding",
        frozen=True,
        expected_generation=control["generation"],
    )

    result = _run(
        adaptation_service,
        "adapt",
        "freeze",
        "--profile-id",
        "coding",
        "--apply",
        "--expect-hash",
        stale,
        "--json",
    )

    assert result.exit_code != 0
    assert "precondition" in result.payload["error"].casefold()


def test_rollback_hash_is_bound_to_requested_revision(adaptation_service) -> None:
    revision_a, revision_b = _publish_revisions(adaptation_service)
    freeze = adaptation_service.preview_adaptation_control(
        action="freeze",
        profile_id="coding",
    )
    adaptation_service.apply_adaptation_control(
        action="freeze",
        profile_id="coding",
        expected_hash=freeze["precondition_hash"],
    )
    preview = _run(
        adaptation_service,
        "adapt",
        "rollback",
        "--profile-id",
        "coding",
        "--revision",
        revision_a,
        "--json",
    )

    result = _run(
        adaptation_service,
        "adapt",
        "rollback",
        "--profile-id",
        "coding",
        "--revision",
        revision_b,
        "--apply",
        "--expect-hash",
        preview.payload["precondition_hash"],
        "--json",
    )

    assert result.exit_code != 0
    assert adaptation_service.adaptation_status("coding")["active_revision_id"] == revision_b


def test_rollback_requires_frozen_profile_and_restores_exact_revision(
    adaptation_service,
) -> None:
    revision_a, revision_b = _publish_revisions(adaptation_service)
    unfrozen_preview = _run(
        adaptation_service,
        "adapt",
        "rollback",
        "--profile-id",
        "coding",
        "--revision",
        revision_a,
        "--json",
    )
    assert unfrozen_preview.exit_code != 0

    freeze = adaptation_service.preview_adaptation_control(
        action="freeze",
        profile_id="coding",
    )
    adaptation_service.apply_adaptation_control(
        action="freeze",
        profile_id="coding",
        expected_hash=freeze["precondition_hash"],
    )
    preview = adaptation_service.preview_adaptation_control(
        action="rollback",
        profile_id="coding",
        revision_id=revision_a,
    )
    result = adaptation_service.apply_adaptation_control(
        action="rollback",
        profile_id="coding",
        revision_id=revision_a,
        expected_hash=preview["precondition_hash"],
    )

    assert result["active_revision_id"] == revision_a
    assert result["frozen"] is True
    assert result["experiment_phase"] == "rolled_back"
    assert revision_b in result["previous_active_revision_id"]


def test_unfreeze_has_its_own_desired_state_hash(adaptation_service) -> None:
    _publish_revisions(adaptation_service)
    freeze = adaptation_service.preview_adaptation_control(
        action="freeze",
        profile_id="coding",
    )
    adaptation_service.apply_adaptation_control(
        action="freeze",
        profile_id="coding",
        expected_hash=freeze["precondition_hash"],
    )
    unfreeze = adaptation_service.preview_adaptation_control(
        action="unfreeze",
        profile_id="coding",
    )

    wrong_action = _run(
        adaptation_service,
        "adapt",
        "unfreeze",
        "--profile-id",
        "coding",
        "--apply",
        "--expect-hash",
        freeze["precondition_hash"],
        "--json",
    )
    result = adaptation_service.apply_adaptation_control(
        action="unfreeze",
        profile_id="coding",
        expected_hash=unfreeze["precondition_hash"],
    )

    assert unfreeze["precondition_hash"] != freeze["precondition_hash"]
    assert wrong_action.exit_code != 0
    assert result["frozen"] is False


def test_adapt_rejects_unknown_profile(adaptation_service) -> None:
    result = _run(
        adaptation_service,
        "adapt",
        "status",
        "--profile-id",
        "other",
        "--json",
    )

    assert result.exit_code != 0
    assert "profile" in result.payload["error"].casefold()


def test_apply_holds_profile_config_lock_through_preview_mutation_and_status(
    adaptation_service,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing import service as service_module

    inside_lock = False

    @contextmanager
    def deterministic_lock(path):
        nonlocal inside_lock
        assert path == adaptation_service.config_path
        inside_lock = True
        try:
            yield
        finally:
            inside_lock = False

    def preview(**_kwargs):
        assert inside_lock
        return {
            "precondition_hash": "f" * 64,
            "precondition": {
                "authority_id": "a" * 64,
                "active_revision_id": None,
                "generation": 0,
                "requested": {"frozen": True},
            },
        }

    def mutate(*_args, **_kwargs):
        assert inside_lock

    def status(_profile_id):
        assert inside_lock
        return {"profile_id": "coding", "frozen": True}

    monkeypatch.setattr(service_module, "profile_config_lock", deterministic_lock)
    monkeypatch.setattr(adaptation_service, "preview_adaptation_control", preview)
    monkeypatch.setattr(adaptation_service.store, "set_profile_freeze", mutate)
    monkeypatch.setattr(adaptation_service, "adaptation_status", status)

    result = adaptation_service.apply_adaptation_control(
        action="freeze",
        profile_id="coding",
        expected_hash="f" * 64,
    )

    assert result["frozen"] is True
    assert inside_lock is False
