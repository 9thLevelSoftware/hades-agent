"""Guarded, content-free Stage 5 management CLI contracts."""

from __future__ import annotations

import argparse
import base64
import json
from datetime import timedelta
from pathlib import Path

import pytest

from plugins.auto_routing.auto_routing.cli import (
    CommandWriteClass,
    build_parser,
    command_metadata,
    execute,
)
from plugins.auto_routing.auto_routing.config import config_document
from plugins.auto_routing.auto_routing.config import management_authority_revision
from plugins.auto_routing.auto_routing.models import RankingPackTrust
from plugins.auto_routing.auto_routing.service import (
    AutoRoutingService,
    AutoRoutingServiceError,
)
from plugins.auto_routing.auto_routing.storage import RoutingStore
from cron.jobs import (
    claim_dispatch,
    get_job,
    list_jobs,
    mark_job_run,
    update_job,
    use_cron_store,
)
from tests.plugins.auto_routing.test_management_reconciler import (
    _config,
    _observation,
    _verified_pack,
)
from tests.plugins.auto_routing.test_ranking_pack import (
    NOW,
    OTHER_PRIVATE_KEY,
    TEST_PRIVATE_KEY,
    _public_key_bytes,
    _write_signed_pack,
)


def _run(service: object, *arguments: str):
    parser = argparse.ArgumentParser(prog="hermes auto-routing")
    build_parser(parser)
    return execute(parser.parse_args(list(arguments)), service=service)


class _ScheduledService:
    def __init__(self) -> None:
        self.invocation_checked = False
        self.completed_invocations: list[object] = []

    def assert_scheduled_management_invocation(self) -> object:
        self.invocation_checked = True
        return "authorized-scheduled-invocation"

    def complete_scheduled_management_invocation(self, invocation: object) -> None:
        self.completed_invocations.append(invocation)

    def reconcile_management(
        self,
        *,
        scheduled: bool = False,
        scheduled_invocation: object | None = None,
    ):
        assert scheduled is True
        assert scheduled_invocation == "authorized-scheduled-invocation"
        return {
            "changed": False,
            "reason_code": "no_change",
            "revision_id": None,
            "profiles": [],
            "scheduled": True,
            "reconciled_at": "2026-07-19T12:00:00Z",
        }


@pytest.fixture
def management_service(tmp_path: Path):
    trusted_key = base64.b64encode(_public_key_bytes(TEST_PRIVATE_KEY)).decode("ascii")
    enabled = _config(enabled=True)
    settings = enabled.autonomous_profile_management.model_copy(
        update={
            "ranking_pack": RankingPackTrust(
                ranking_pack_path="auto-routing/ranking-packs/current.json",
                trusted_ed25519_public_keys=(trusted_key,),
            )
        }
    )
    enabled = enabled.model_copy(update={"autonomous_profile_management": settings})
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(enabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _write_signed_pack(
        tmp_path,
        expires_at=NOW.replace(year=2027),
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


@pytest.mark.parametrize(
    ("name", "write_class"),
    (
        ("manage inventory", CommandWriteClass.READ_ONLY),
        ("manage ranking", CommandWriteClass.READ_ONLY),
        ("manage status", CommandWriteClass.READ_ONLY),
        ("manage history", CommandWriteClass.READ_ONLY),
        ("manage reconcile", CommandWriteClass.GUARDED_CONTROL_PLANE),
        ("manage enable", CommandWriteClass.GUARDED_CONTROL_PLANE),
        ("manage disable", CommandWriteClass.GUARDED_CONTROL_PLANE),
        ("manage freeze", CommandWriteClass.GUARDED_CONTROL_PLANE),
        ("manage unfreeze", CommandWriteClass.GUARDED_CONTROL_PLANE),
        ("manage recover", CommandWriteClass.GUARDED_CONTROL_PLANE),
        ("manage schedule", CommandWriteClass.GUARDED_CONTROL_PLANE),
        ("manage ranking-trust", CommandWriteClass.GUARDED_CONTROL_PLANE),
        ("manage daily-cap", CommandWriteClass.GUARDED_CONTROL_PLANE),
    ),
)
def test_manage_leaf_metadata(name: str, write_class: CommandWriteClass) -> None:
    assert command_metadata(name).write_class is write_class


def test_manage_control_requires_preview_hash_before_apply(
    management_service: AutoRoutingService,
) -> None:
    result = _run(management_service, "manage", "freeze", "--apply", "--json")

    assert result.exit_code == 2
    assert result.payload["error_code"] == "expected_hash_required"


def test_manage_setting_controls_forward_exact_preview_and_apply_arguments() -> None:
    class SettingService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def preview_management_control(self, **kwargs):
            self.calls.append(("preview", kwargs))
            return {"apply": False, "precondition_hash": "a" * 64}

        def apply_management_control(self, **kwargs):
            self.calls.append(("apply", kwargs))
            return {"apply": True}

    service = SettingService()
    key = base64.b64encode(_public_key_bytes(TEST_PRIVATE_KEY)).decode("ascii")

    daily = _run(service, "manage", "daily-cap", "--limit", "4", "--json")
    trust = _run(
        service,
        "manage",
        "ranking-trust",
        "--ranking-pack-path",
        "auto-routing/ranking-packs/next.json",
        "--trusted-ed25519-public-key",
        key,
        "--apply",
        "--expect-hash",
        "b" * 64,
        "--json",
    )

    assert daily.exit_code == trust.exit_code == 0
    assert service.calls == [
        (
            "preview",
            {
                "action": "daily-cap",
                "schedule": None,
                "ranking_pack_path": None,
                "trusted_public_keys": None,
                "daily_limit": 4,
            },
        ),
        (
            "apply",
            {
                "action": "ranking-trust",
                "expected_hash": "b" * 64,
                "schedule": None,
                "ranking_pack_path": "auto-routing/ranking-packs/next.json",
                "trusted_public_keys": (key,),
                "daily_limit": None,
            },
        ),
    ]


def test_daily_cap_control_preserves_cron_and_updates_only_bounded_setting(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enabled_preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=enabled_preview["precondition_hash"],
    )
    assert isinstance(enabled, dict)
    job_id = enabled["cron_job_id"]
    preview = management_service.preview_management_control(
        action="daily-cap",
        daily_limit=4,
    )
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.install_management_cron",
        lambda **_kwargs: pytest.fail("daily cap must not reinstall cron"),
    )

    applied = management_service.apply_management_control(
        action="daily-cap",
        daily_limit=4,
        expected_hash=preview["precondition_hash"],
    )

    assert isinstance(applied, dict)
    assert applied["cron_job_id"] == job_id
    assert (
        management_service._configured_authority().autonomous_profile_management.daily_change_limit
        == 4
    )
    assert preview["precondition"]["proposed_daily_change_limit"] == 4


def test_ranking_trust_replacement_is_verified_and_preview_hides_raw_keys(
    management_service: AutoRoutingService,
) -> None:
    path = (
        management_service.hermes_home / "auto-routing" / "ranking-packs" / "next.json"
    )
    _write_signed_pack(
        management_service.hermes_home,
        signer=OTHER_PRIVATE_KEY,
        expires_at=NOW.replace(year=2027),
        pack_id="replacement-pack",
        path=path,
    )
    key = base64.b64encode(_public_key_bytes(OTHER_PRIVATE_KEY)).decode("ascii")

    preview = management_service.preview_management_control(
        action="ranking-trust",
        ranking_pack_path="auto-routing/ranking-packs/next.json",
        trusted_public_keys=(key,),
    )
    serialized = json.dumps(preview, sort_keys=True)
    assert key not in serialized
    assert preview["precondition"]["proposed_ranking_pack_id"] == "replacement-pack"
    assert preview["precondition"]["trusted_key_count"] == 1

    applied = management_service.apply_management_control(
        action="ranking-trust",
        ranking_pack_path="auto-routing/ranking-packs/next.json",
        trusted_public_keys=(key,),
        expected_hash=preview["precondition_hash"],
    )

    assert isinstance(applied, dict)
    trust = management_service._configured_authority().autonomous_profile_management.ranking_pack
    assert trust is not None
    assert trust.ranking_pack_path == "auto-routing/ranking-packs/next.json"
    assert trust.trusted_ed25519_public_keys == (key,)


def test_invalid_existing_ranking_trust_cannot_block_emergency_freeze(
    management_service: AutoRoutingService,
) -> None:
    config = management_service._configured_authority()
    invalid_settings = config.autonomous_profile_management.model_copy(
        update={
            "ranking_pack": RankingPackTrust(
                ranking_pack_path="auto-routing/ranking-packs/current.json",
                trusted_ed25519_public_keys=("not-base64",),
            )
        }
    )
    invalid = config.model_copy(
        update={"autonomous_profile_management": invalid_settings}
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(invalid)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    preview = management_service.preview_management_control(action="freeze")
    applied = management_service.apply_management_control(
        action="freeze",
        expected_hash=preview["precondition_hash"],
    )

    assert isinstance(applied, dict)
    assert applied["frozen"] is True
    assert preview["precondition"]["trusted_key_count"] == 1


def test_manage_recover_routes_one_exact_receipt_through_guarded_control() -> None:
    class RecoveryService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []

        def preview_management_recovery(self, receipt_id: str):
            self.calls.append(("preview", receipt_id, None))
            return {
                "apply": False,
                "action": "recover",
                "receipt_id": receipt_id,
                "precondition_hash": "a" * 64,
            }

        def apply_management_recovery(
            self,
            receipt_id: str,
            *,
            expected_hash: str,
        ):
            self.calls.append(("apply", receipt_id, expected_hash))
            return {
                "apply": True,
                "action": "recover",
                "receipt_id": receipt_id,
                "reason_code": "recovered",
            }

    service = RecoveryService()
    preview = _run(
        service,
        "manage",
        "recover",
        "--receipt-id",
        "receipt-1",
        "--json",
    )
    applied = _run(
        service,
        "manage",
        "recover",
        "--receipt-id",
        "receipt-1",
        "--apply",
        "--expect-hash",
        "a" * 64,
        "--json",
    )

    assert preview.exit_code == applied.exit_code == 0
    assert service.calls == [
        ("preview", "receipt-1", None),
        ("apply", "receipt-1", "a" * 64),
    ]


def test_manage_reconcile_scheduled_mode_is_noninteractive_and_content_free() -> None:
    service = _ScheduledService()
    result = _run(service, "manage", "reconcile", "--scheduled", "--json")

    assert result.exit_code == 0
    assert service.invocation_checked is True
    serialized = json.dumps(result.payload).casefold()
    assert "prompt" not in serialized
    assert "response" not in serialized
    assert result.payload["scheduled"] is True
    assert service.completed_invocations == ["authorized-scheduled-invocation"]


def test_manage_reconcile_scheduled_mode_rejects_missing_script_proof() -> None:
    class _UntrustedScheduledService(_ScheduledService):
        def assert_scheduled_management_invocation(self) -> None:
            raise RuntimeError("scheduled invocation proof is invalid")

    result = _run(
        _UntrustedScheduledService(),
        "manage",
        "reconcile",
        "--scheduled",
        "--json",
    )

    assert result.exit_code == 2
    assert "proof is invalid" in result.payload["error"]


def test_scheduled_reconcile_flag_is_hidden_from_public_help() -> None:
    parser = argparse.ArgumentParser(prog="hermes auto-routing")
    build_parser(parser)
    top = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    manage = top.choices["manage"]
    leaves = next(
        action
        for action in manage._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    assert "--scheduled" not in leaves.choices["reconcile"].format_help()


def test_manual_reconcile_requires_preview_then_exact_apply_hash(
    management_service: AutoRoutingService,
) -> None:
    preview = _run(management_service, "manage", "reconcile", "--json")
    missing = _run(
        management_service,
        "manage",
        "reconcile",
        "--apply",
        "--json",
    )

    assert preview.exit_code == 0
    assert preview.payload["apply"] is False
    assert len(preview.payload["precondition_hash"]) == 64
    assert missing.exit_code == 2
    assert missing.payload["error_code"] == "expected_hash_required"


def test_management_preview_binds_all_control_authority(
    management_service: AutoRoutingService,
) -> None:
    preview = management_service.preview_management_control(action="freeze")

    assert preview["precondition_hash"]
    assert set(preview["precondition"]) >= {
        "authority_id",
        "management_authority_id",
        "control_generation",
        "action",
        "schedule",
        "ranking_pack_path",
        "ranking_pack_fingerprint",
        "daily_change_limit",
        "cron_job_id",
    }
    assert preview["precondition"]["action"] == "freeze"


def test_enable_installs_no_agent_python_cron_job(
    management_service: AutoRoutingService,
) -> None:
    config = management_service._configured_authority()
    disabled_settings = config.autonomous_profile_management.model_copy(
        update={"enabled": False}
    )
    disabled = config.model_copy(
        update={"autonomous_profile_management": disabled_settings}
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(disabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    preview = management_service.preview_management_control(action="enable")

    applied = management_service.apply_management_control(
        action="enable",
        expected_hash=preview["precondition_hash"],
    )

    assert isinstance(applied, dict)
    assert applied["enabled"] is True
    assert applied["cron_job_id"]
    with use_cron_store(management_service.hermes_home):
        job = get_job(applied["cron_job_id"])
    assert job is not None
    assert job["no_agent"] is True
    assert job["script"].endswith("auto-routing-management.py")
    current = management_service._configured_authority()
    control = management_service.store.read_management_control(
        management_authority_revision(current)
    )
    assert control.cron_job_id == applied["cron_job_id"]


def test_freeze_apply_rejects_a_stale_control_generation(
    management_service: AutoRoutingService,
) -> None:
    stale = management_service.preview_management_control(action="freeze")
    applied = management_service.apply_management_control(
        action="freeze",
        expected_hash=stale["precondition_hash"],
    )
    assert isinstance(applied, dict) and applied["frozen"] is True

    with pytest.raises(Exception, match="precondition changed"):
        management_service.apply_management_control(
            action="freeze",
            expected_hash=stale["precondition_hash"],
        )


def test_disable_removal_failure_leaves_disabled_frozen_repair_state(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing import service as service_module

    enabled_preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=enabled_preview["precondition_hash"],
    )
    assert isinstance(enabled, dict) and enabled["cron_job_id"]
    monkeypatch.setattr(service_module, "remove_management_cron", lambda **_kw: False)
    preview = management_service.preview_management_control(action="disable")

    with pytest.raises(Exception, match="management_cron_removal_failed"):
        management_service.apply_management_control(
            action="disable",
            expected_hash=preview["precondition_hash"],
        )

    status = management_service.management_status()
    assert status["enabled"] is False
    assert status["frozen"] is True
    assert status["cron_job_id"] == enabled["cron_job_id"]


def test_disable_control_failure_before_job_removal_restores_enabled_job(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=enable_preview["precondition_hash"],
    )
    assert isinstance(enabled, dict)
    job_id = enabled["cron_job_id"]
    original_transition = management_service._transition_global_management_control

    def fail_clear_transition(**kwargs):
        if kwargs["cron_job_id"] is None:
            raise RuntimeError("control store unavailable")
        return original_transition(**kwargs)

    monkeypatch.setattr(
        management_service,
        "_transition_global_management_control",
        fail_clear_transition,
    )
    preview = management_service.preview_management_control(action="disable")

    with pytest.raises(Exception, match="management control apply failed"):
        management_service.apply_management_control(
            action="disable",
            expected_hash=preview["precondition_hash"],
        )

    assert (
        management_service._configured_authority().autonomous_profile_management.enabled
        is True
    )
    with use_cron_store(management_service.hermes_home):
        assert get_job(job_id) is not None


def test_disable_remove_then_interrupt_stays_disabled_and_frozen(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing import service as service_module

    enable_preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=enable_preview["precondition_hash"],
    )
    assert isinstance(enabled, dict)
    job_id = enabled["cron_job_id"]
    real_remove = service_module.remove_management_cron

    def remove_then_interrupt(**kwargs):
        assert real_remove(**kwargs) is True
        raise KeyboardInterrupt

    monkeypatch.setattr(
        service_module,
        "remove_management_cron",
        remove_then_interrupt,
    )
    preview = management_service.preview_management_control(action="disable")

    with pytest.raises(
        AutoRoutingServiceError,
        match="management_cron_removal_failed",
    ):
        management_service.apply_management_control(
            action="disable",
            expected_hash=preview["precondition_hash"],
        )

    status = management_service.management_status()
    assert status["enabled"] is False
    assert status["frozen"] is True
    assert status["cron_job_id"] == job_id
    with use_cron_store(management_service.hermes_home):
        assert get_job(job_id) is None


def test_disable_repair_transition_interrupt_never_restores_enabled_config(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing import service as service_module

    enable_preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=enable_preview["precondition_hash"],
    )
    assert isinstance(enabled, dict)
    job_id = enabled["cron_job_id"]
    real_remove = service_module.remove_management_cron
    real_transition = management_service._transition_global_management_control
    transitions = 0

    def remove_then_report_failure(**kwargs):
        assert real_remove(**kwargs) is True
        return False

    def interrupt_repair_transition(**kwargs):
        nonlocal transitions
        transitions += 1
        if transitions == 2:
            raise KeyboardInterrupt
        return real_transition(**kwargs)

    monkeypatch.setattr(
        service_module,
        "remove_management_cron",
        remove_then_report_failure,
    )
    monkeypatch.setattr(
        management_service,
        "_transition_global_management_control",
        interrupt_repair_transition,
    )
    preview = management_service.preview_management_control(action="disable")

    with pytest.raises(
        AutoRoutingServiceError,
        match="management_cron_removal_failed",
    ):
        management_service.apply_management_control(
            action="disable",
            expected_hash=preview["precondition_hash"],
        )

    assert (
        management_service._configured_authority().autonomous_profile_management.enabled
        is False
    )
    with use_cron_store(management_service.hermes_home):
        assert get_job(job_id) is None


def test_enable_control_failure_restores_config_and_removes_new_cron_job(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = management_service._configured_authority()
    disabled = config.model_copy(
        update={
            "autonomous_profile_management": (
                config.autonomous_profile_management.model_copy(
                    update={"enabled": False}
                )
            )
        }
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(disabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    preview = management_service.preview_management_control(action="enable")
    monkeypatch.setattr(
        management_service,
        "_transition_global_management_control",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )

    with pytest.raises(Exception, match="management control apply failed"):
        management_service.apply_management_control(
            action="enable",
            expected_hash=preview["precondition_hash"],
        )

    assert (
        management_service._configured_authority().autonomous_profile_management.enabled
        is False
    )
    with use_cron_store(management_service.hermes_home):
        assert list_jobs(include_disabled=True) == []


def test_post_create_cron_failure_restores_disabled_config_without_an_orphan(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing import management_cron as cron_module

    config = management_service._configured_authority()
    disabled = config.model_copy(
        update={
            "autonomous_profile_management": (
                config.autonomous_profile_management.model_copy(
                    update={"enabled": False}
                )
            )
        }
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(disabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    preview = management_service.preview_management_control(action="enable")
    real_create = cron_module.create_job

    def create_then_raise(*args, **kwargs):
        real_create(*args, **kwargs)
        raise RuntimeError("post-create failure")

    monkeypatch.setattr(cron_module, "create_job", create_then_raise)

    with pytest.raises(Exception, match="management control apply failed"):
        management_service.apply_management_control(
            action="enable",
            expected_hash=preview["precondition_hash"],
        )

    assert (
        management_service._configured_authority().autonomous_profile_management.enabled
        is False
    )
    with use_cron_store(management_service.hermes_home):
        assert list_jobs(include_disabled=True) == []


def test_post_return_cron_interrupt_restores_disabled_config_without_an_orphan(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.jobs import create_job
    from plugins.auto_routing.auto_routing import service as service_module

    class PostReturnInterrupt(BaseException):
        pass

    config = management_service._configured_authority()
    disabled = config.model_copy(
        update={
            "autonomous_profile_management": (
                config.autonomous_profile_management.model_copy(
                    update={"enabled": False}
                )
            )
        }
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(disabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    preview = management_service.preview_management_control(action="enable")
    with use_cron_store(management_service.hermes_home):
        unrelated = create_job(
            prompt="unrelated scheduled work",
            schedule="every 5m",
            name="unrelated-cron-job",
            deliver="local",
        )
    real_install = service_module.install_management_cron

    def install_then_interrupt(**kwargs):
        real_install(**kwargs)
        raise PostReturnInterrupt("after cron installer returned")

    monkeypatch.setattr(
        service_module, "install_management_cron", install_then_interrupt
    )

    with pytest.raises(PostReturnInterrupt, match="after cron installer returned"):
        management_service.apply_management_control(
            action="enable",
            expected_hash=preview["precondition_hash"],
        )

    assert (
        management_service._configured_authority().autonomous_profile_management.enabled
        is False
    )
    with use_cron_store(management_service.hermes_home):
        assert [job["id"] for job in list_jobs(include_disabled=True)] == [
            unrelated["id"]
        ]


def test_post_return_cron_interrupt_restores_the_existing_job_only(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.jobs import create_job
    from plugins.auto_routing.auto_routing import service as service_module

    class PostReturnInterrupt(BaseException):
        pass

    enable_preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=enable_preview["precondition_hash"],
    )
    job_id = enabled["cron_job_id"]
    original_schedule = enabled["schedule"]
    with use_cron_store(management_service.hermes_home):
        unrelated = create_job(
            prompt="unrelated scheduled work",
            schedule="every 5m",
            name="unrelated-cron-job",
            deliver="local",
        )
    preview = management_service.preview_management_control(
        action="schedule",
        schedule="23 */4 * * *",
    )
    real_install = service_module.install_management_cron
    calls = 0

    def install_then_interrupt_once(**kwargs):
        nonlocal calls
        result = real_install(**kwargs)
        calls += 1
        if calls == 1:
            raise PostReturnInterrupt("after cron installer returned")
        return result

    monkeypatch.setattr(
        service_module,
        "install_management_cron",
        install_then_interrupt_once,
    )

    with pytest.raises(PostReturnInterrupt, match="after cron installer returned"):
        management_service.apply_management_control(
            action="schedule",
            schedule="23 */4 * * *",
            expected_hash=preview["precondition_hash"],
        )

    assert (
        management_service._configured_authority().autonomous_profile_management.schedule
        == original_schedule
    )
    with use_cron_store(management_service.hermes_home):
        restored = get_job(job_id)
        assert restored is not None
        assert restored["schedule"]["expr"] == original_schedule
        assert get_job(unrelated["id"]) == unrelated


def test_enable_failure_restores_an_adopted_cron_job_without_deleting_it(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing import management_cron as cron_module

    config = management_service._configured_authority()
    disabled = config.model_copy(
        update={
            "autonomous_profile_management": (
                config.autonomous_profile_management.model_copy(
                    update={"enabled": False}
                )
            )
        }
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(disabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    adopted = cron_module.install_management_cron(
        home=management_service.hermes_home,
        schedule="23 */4 * * *",
        previous_job_id=None,
    )
    with use_cron_store(management_service.hermes_home):
        before = get_job(adopted.job_id)
    assert before is not None
    preview = management_service.preview_management_control(action="enable")
    monkeypatch.setattr(
        management_service,
        "_transition_global_management_control",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )

    with pytest.raises(Exception, match="management control apply failed"):
        management_service.apply_management_control(
            action="enable",
            expected_hash=preview["precondition_hash"],
        )

    assert (
        management_service._configured_authority().autonomous_profile_management.enabled
        is False
    )
    with use_cron_store(management_service.hermes_home):
        restored = get_job(adopted.job_id)
        assert restored is not None
        assert restored["_launch_revision"] != before["_launch_revision"]
        assert {
            key: value
            for key, value in restored.items()
            if key != "_launch_revision"
        } == {
            key: value
            for key, value in before.items()
            if key != "_launch_revision"
        }


def test_base_exception_restores_an_adopted_cron_job_without_deleting_it(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing import management_cron as cron_module

    class ControlInterrupt(BaseException):
        pass

    config = management_service._configured_authority()
    disabled = config.model_copy(
        update={
            "autonomous_profile_management": (
                config.autonomous_profile_management.model_copy(
                    update={"enabled": False}
                )
            )
        }
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(disabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    adopted = cron_module.install_management_cron(
        home=management_service.hermes_home,
        schedule="23 */4 * * *",
        previous_job_id=None,
    )
    with use_cron_store(management_service.hermes_home):
        before = get_job(adopted.job_id)
    assert before is not None
    preview = management_service.preview_management_control(action="enable")
    monkeypatch.setattr(
        management_service,
        "_transition_global_management_control",
        lambda **_kwargs: (_ for _ in ()).throw(ControlInterrupt("store interrupted")),
    )

    with pytest.raises(ControlInterrupt, match="store interrupted"):
        management_service.apply_management_control(
            action="enable",
            expected_hash=preview["precondition_hash"],
        )

    assert (
        management_service._configured_authority().autonomous_profile_management.enabled
        is False
    )
    with use_cron_store(management_service.hermes_home):
        restored = get_job(adopted.job_id)
        assert restored is not None
        assert restored["_launch_revision"] != before["_launch_revision"]
        assert {
            key: value
            for key, value in restored.items()
            if key != "_launch_revision"
        } == {
            key: value
            for key, value in before.items()
            if key != "_launch_revision"
        }


def test_post_replace_failure_restores_exact_config_before_cron_install(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing import config_io as config_io_module

    config = management_service._configured_authority()
    disabled = config.model_copy(
        update={
            "autonomous_profile_management": (
                config.autonomous_profile_management.model_copy(
                    update={"enabled": False}
                )
            )
        }
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(disabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    before = management_service.config_path.read_bytes()
    preview = management_service.preview_management_control(action="enable")
    original_replace = config_io_module.LockedConfigUpdate.replace

    def replace_then_fail(mutation) -> None:
        original_replace(mutation)
        raise RuntimeError("post-replace verification unavailable")

    monkeypatch.setattr(
        config_io_module.LockedConfigUpdate,
        "replace",
        replace_then_fail,
    )

    with pytest.raises(Exception, match="management control apply failed"):
        management_service.apply_management_control(
            action="enable",
            expected_hash=preview["precondition_hash"],
        )

    assert management_service.config_path.read_bytes() == before
    with use_cron_store(management_service.hermes_home):
        assert list_jobs(include_disabled=True) == []
    assert (
        list(
            management_service.config_path.parent.glob(
                f"{management_service.config_path.name}.auto-routing."
                "management-control.*.bak"
            )
        )
        == []
    )


def test_enable_failure_restores_config_even_when_cron_compensation_fails(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.auto_routing.auto_routing import service as service_module

    config = management_service._configured_authority()
    disabled = config.model_copy(
        update={
            "autonomous_profile_management": (
                config.autonomous_profile_management.model_copy(
                    update={"enabled": False}
                )
            )
        }
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(disabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    preview = management_service.preview_management_control(action="enable")
    monkeypatch.setattr(
        management_service,
        "_transition_global_management_control",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )
    monkeypatch.setattr(
        service_module,
        "rollback_management_cron_install",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("cron unavailable")),
    )

    with pytest.raises(Exception, match="recovery is incomplete"):
        management_service.apply_management_control(
            action="enable",
            expected_hash=preview["precondition_hash"],
        )

    assert (
        management_service._configured_authority().autonomous_profile_management.enabled
        is False
    )


def test_recovered_enable_failure_can_retry_the_same_exact_preview(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = management_service._configured_authority()
    disabled = config.model_copy(
        update={
            "autonomous_profile_management": (
                config.autonomous_profile_management.model_copy(
                    update={"enabled": False}
                )
            )
        }
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(disabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    preview = management_service.preview_management_control(action="enable")
    original_transition = management_service._transition_global_management_control
    calls = 0

    def fail_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("store unavailable")
        return original_transition(**kwargs)

    monkeypatch.setattr(
        management_service,
        "_transition_global_management_control",
        fail_once,
    )

    with pytest.raises(Exception, match="management control apply failed"):
        management_service.apply_management_control(
            action="enable",
            expected_hash=preview["precondition_hash"],
        )
    assert (
        list(
            management_service.config_path.parent.glob(
                f"{management_service.config_path.name}.auto-routing."
                "management-control.*.bak"
            )
        )
        == []
    )
    applied = management_service.apply_management_control(
        action="enable",
        expected_hash=preview["precondition_hash"],
    )

    assert isinstance(applied, dict)
    assert applied["enabled"] is True
    assert applied["cron_job_id"]


def test_enable_and_disable_preserve_an_explicit_global_freeze(
    management_service: AutoRoutingService,
) -> None:
    freeze_preview = management_service.preview_management_control(action="freeze")
    management_service.apply_management_control(
        action="freeze",
        expected_hash=freeze_preview["precondition_hash"],
    )
    enable_preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=enable_preview["precondition_hash"],
    )
    assert isinstance(enabled, dict)
    assert enabled["frozen"] is True

    disable_preview = management_service.preview_management_control(action="disable")
    disabled = management_service.apply_management_control(
        action="disable",
        expected_hash=disable_preview["precondition_hash"],
    )

    assert isinstance(disabled, dict)
    assert disabled["frozen"] is True


def test_schedule_updates_config_and_the_same_recorded_job(
    management_service: AutoRoutingService,
) -> None:
    enable_preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=enable_preview["precondition_hash"],
    )
    assert isinstance(enabled, dict)
    original_job_id = enabled["cron_job_id"]
    preview = management_service.preview_management_control(
        action="schedule",
        schedule="23 */4 * * *",
    )

    applied = management_service.apply_management_control(
        action="schedule",
        schedule="23 */4 * * *",
        expected_hash=preview["precondition_hash"],
    )

    assert isinstance(applied, dict)
    assert applied["cron_job_id"] == original_job_id
    assert applied["schedule"] == "23 */4 * * *"
    with use_cron_store(management_service.hermes_home):
        job = get_job(original_job_id)
    assert job is not None and job["schedule"]["expr"] == "23 */4 * * *"


def test_disable_removes_recorded_job_and_clears_control(
    management_service: AutoRoutingService,
) -> None:
    enable_preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=enable_preview["precondition_hash"],
    )
    assert isinstance(enabled, dict)
    job_id = enabled["cron_job_id"]
    preview = management_service.preview_management_control(action="disable")

    disabled = management_service.apply_management_control(
        action="disable",
        expected_hash=preview["precondition_hash"],
    )

    assert isinstance(disabled, dict)
    assert disabled["enabled"] is False
    assert disabled["frozen"] is False
    assert disabled["cron_job_id"] is None
    with use_cron_store(management_service.hermes_home):
        assert get_job(job_id) is None


def test_schedule_control_failure_restores_previous_job_and_config(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=enable_preview["precondition_hash"],
    )
    assert isinstance(enabled, dict)
    job_id = enabled["cron_job_id"]
    original_schedule = enabled["schedule"]
    preview = management_service.preview_management_control(
        action="schedule",
        schedule="23 */4 * * *",
    )
    monkeypatch.setattr(
        management_service,
        "_transition_global_management_control",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )

    with pytest.raises(Exception, match="management control apply failed"):
        management_service.apply_management_control(
            action="schedule",
            schedule="23 */4 * * *",
            expected_hash=preview["precondition_hash"],
        )

    assert (
        management_service._configured_authority().autonomous_profile_management.schedule
        == original_schedule
    )
    with use_cron_store(management_service.hermes_home):
        job = get_job(job_id)
    assert job is not None and job["schedule"]["expr"] == original_schedule


def test_manual_reconcile_exact_apply_succeeds_for_no_change_hold(
    management_service: AutoRoutingService,
) -> None:
    preview = _run(management_service, "manage", "reconcile", "--json")

    applied = _run(
        management_service,
        "manage",
        "reconcile",
        "--apply",
        "--expect-hash",
        preview.payload["precondition_hash"],
        "--json",
    )

    assert applied.exit_code == 0
    assert applied.payload["changed"] is False
    assert applied.payload["reason_code"] == "inventory_snapshot_missing"


def _enable_one_shot_management_job(
    management_service: AutoRoutingService,
    *,
    schedule: str = "2099-01-01T00:00:00Z",
) -> str:
    config = management_service._configured_authority()
    disabled = config.model_copy(
        update={
            "autonomous_profile_management": (
                config.autonomous_profile_management.model_copy(
                    update={"enabled": False, "schedule": schedule}
                )
            )
        }
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(disabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    preview = management_service.preview_management_control(action="enable")
    applied = management_service.apply_management_control(
        action="enable",
        expected_hash=preview["precondition_hash"],
    )
    assert isinstance(applied, dict)
    assert isinstance(applied["cron_job_id"], str)
    return applied["cron_job_id"]


def _managed_job_snapshot(
    management_service: AutoRoutingService,
    job_id: str,
) -> dict:
    with use_cron_store(management_service.hermes_home):
        job = get_job(job_id)
    assert job is not None
    return job


def test_successful_scheduled_one_shot_reconcile_clears_control_before_cron_removal(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.script_claim import issue_script_launch_claim

    job_id = _enable_one_shot_management_job(management_service)
    with use_cron_store(management_service.hermes_home):
        assert claim_dispatch(job_id) is True
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=_managed_job_snapshot(management_service, job_id),
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)

    result = _run(
        management_service,
        "manage",
        "reconcile",
        "--scheduled",
        "--json",
    )

    assert result.exit_code == 0
    assert result.payload["changed"] is False
    assert result.payload["reason_code"] == "inventory_snapshot_missing"
    # The generic cron lifecycle owns deletion of a completed finite job.
    with use_cron_store(management_service.hermes_home):
        mark_job_run(job_id, success=True)
        assert get_job(job_id) is None
    status = management_service.management_status()
    assert status["enabled"] is True
    assert status["cron_job_id"] is None


def test_successful_scheduled_recurring_reconcile_preserves_control_and_job(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.script_claim import issue_script_launch_claim

    preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=preview["precondition_hash"],
    )
    assert isinstance(enabled, dict)
    job_id = enabled["cron_job_id"]
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=_managed_job_snapshot(management_service, job_id),
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)

    result = _run(
        management_service,
        "manage",
        "reconcile",
        "--scheduled",
        "--json",
    )

    assert result.exit_code == 0
    with use_cron_store(management_service.hermes_home):
        mark_job_run(job_id, success=True)
        current = get_job(job_id)
    assert current is not None
    assert current["repeat"]["times"] is None
    assert management_service.management_status()["cron_job_id"] == job_id


def test_scheduled_invocation_binding_allows_exact_actionable_reconcile(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.script_claim import issue_script_launch_claim

    observations = (
        _observation(
            "challenger",
            verification_expires_at=NOW + timedelta(days=365),
        ),
        _observation(
            "current",
            verification_expires_at=NOW + timedelta(days=365),
        ),
    )
    management_service.store.write_inventory_snapshot(
        "inventory-current",
        observations,
        created_at=(NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
    )
    pack = _verified_pack(*(item.key.stable_id() for item in observations))
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
        lambda **_kwargs: pack,
    )
    preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=preview["precondition_hash"],
    )
    job_id = enabled["cron_job_id"]
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=_managed_job_snapshot(management_service, job_id),
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)

    result = _run(
        management_service,
        "manage",
        "reconcile",
        "--scheduled",
        "--json",
    )

    assert result.exit_code == 0
    assert result.payload["changed"] is True
    assert result.payload["reason_code"] == "revision_applied"
    assert management_service.store.connection.execute(
        "SELECT COUNT(*) FROM management_revisions"
    ).fetchone()[0] == 2
    assert management_service.store.connection.execute(
        "SELECT COUNT(*) FROM management_config_receipts WHERE phase='committed'"
    ).fetchone()[0] == 1


def test_scheduled_one_shot_completion_preserves_a_replaced_recurring_job(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.script_claim import issue_script_launch_claim

    job_id = _enable_one_shot_management_job(management_service)
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=_managed_job_snapshot(management_service, job_id),
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)
    invocation = management_service.assert_scheduled_management_invocation()

    preview = management_service.preview_management_control(
        action="schedule",
        schedule="23 */4 * * *",
    )
    replacement = management_service.apply_management_control(
        action="schedule",
        schedule="23 */4 * * *",
        expected_hash=preview["precondition_hash"],
    )

    assert isinstance(replacement, dict)
    assert replacement["cron_job_id"] == job_id
    assert (
        management_service.complete_scheduled_management_invocation(invocation) is False
    )
    with use_cron_store(management_service.hermes_home):
        current = get_job(job_id)
    assert current is not None
    assert current["schedule"]["kind"] == "cron"
    assert current["repeat"]["times"] is None
    assert management_service.management_status()["cron_job_id"] == job_id


def test_scheduled_one_shot_completion_rejects_a_same_id_job_replacement(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.script_claim import issue_script_launch_claim

    job_id = _enable_one_shot_management_job(management_service)
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=_managed_job_snapshot(management_service, job_id),
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)
    invocation = management_service.assert_scheduled_management_invocation()

    with use_cron_store(management_service.hermes_home):
        replaced = update_job(
            job_id,
            {"schedule": "2099-01-02T00:00:00Z"},
        )
    assert replaced is not None
    assert replaced["id"] == job_id
    assert (
        management_service.complete_scheduled_management_invocation(invocation) is False
    )
    assert management_service.management_status()["cron_job_id"] == job_id


def test_scheduled_claim_rejects_same_id_reschedule_after_issuance(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.script_claim import issue_script_launch_claim

    job_id = _enable_one_shot_management_job(management_service)
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=_managed_job_snapshot(management_service, job_id),
    )
    with use_cron_store(management_service.hermes_home):
        assert update_job(job_id, {"schedule": "2099-01-02T00:00:00Z"})
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="claim is invalid"):
        management_service.assert_scheduled_management_invocation()


def test_scheduled_reconcile_rechecks_same_id_job_before_config_apply(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.script_claim import issue_script_launch_claim

    observations = (_observation("challenger"), _observation("current"))
    management_service.store.write_inventory_snapshot(
        "inventory-current",
        observations,
        created_at=(NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
    )
    pack = _verified_pack(*(item.key.stable_id() for item in observations))
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
        lambda **_kwargs: pack,
    )
    job_id = _enable_one_shot_management_job(management_service)
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=_managed_job_snapshot(management_service, job_id),
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)
    invocation = management_service.assert_scheduled_management_invocation()
    before = management_service.config_path.read_bytes()
    original_prepare = management_service._prepare_management_profile
    replaced = False

    def replace_job_after_planning(**kwargs):
        nonlocal replaced
        prepared = original_prepare(**kwargs)
        if not replaced:
            replaced = True
            with use_cron_store(management_service.hermes_home):
                assert update_job(
                    job_id,
                    {"schedule": "2099-01-02T00:00:00Z"},
                )
        return prepared

    monkeypatch.setattr(
        management_service,
        "_prepare_management_profile",
        replace_job_after_planning,
    )

    report = management_service.reconcile_management(
        now=NOW + timedelta(days=1),
        scheduled=True,
        scheduled_invocation=invocation,
    )

    assert replaced is True
    assert report.changed is False
    assert report.reason_code == "scheduled_invocation_changed"
    assert management_service.config_path.read_bytes() == before
    assert management_service.store.connection.execute(
        "SELECT COUNT(*) FROM management_revisions"
    ).fetchone()[0] == 0
    assert management_service.store.connection.execute(
        "SELECT COUNT(*) FROM management_config_receipts"
    ).fetchone()[0] == 0
    assert management_service.store.management_daily_admissions(
        "coding",
        "2026-07-19",
    ) == 0


def test_scheduled_reconcile_holds_job_binding_across_config_mutation(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-ID replacement after planning still precedes all durable mutation."""
    from cron.script_claim import issue_script_launch_claim

    observations = (_observation("challenger"), _observation("current"))
    management_service.store.write_inventory_snapshot(
        "inventory-current",
        observations,
        created_at=(NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
    )
    pack = _verified_pack(*(item.key.stable_id() for item in observations))
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
        lambda **_kwargs: pack,
    )
    job_id = _enable_one_shot_management_job(management_service)
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=_managed_job_snapshot(management_service, job_id),
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)
    invocation = management_service.assert_scheduled_management_invocation()
    before = management_service.config_path.read_bytes()
    original_rollover = management_service._management_activation_rollover
    replaced = False

    def replace_job_at_mutation_boundary(**kwargs):
        nonlocal replaced
        rollover = original_rollover(**kwargs)
        if not replaced:
            replaced = True
            with use_cron_store(management_service.hermes_home):
                assert update_job(
                    job_id,
                    {"schedule": "2099-01-02T00:00:00Z"},
                )
        return rollover

    monkeypatch.setattr(
        management_service,
        "_management_activation_rollover",
        replace_job_at_mutation_boundary,
    )

    report = management_service.reconcile_management(
        now=NOW + timedelta(days=1),
        scheduled=True,
        scheduled_invocation=invocation,
    )

    assert replaced is True
    assert report.changed is False
    assert report.reason_code == "scheduled_invocation_changed"
    assert management_service.config_path.read_bytes() == before
    assert management_service.store.connection.execute(
        "SELECT COUNT(*) FROM management_revisions"
    ).fetchone()[0] == 0
    assert management_service.store.connection.execute(
        "SELECT COUNT(*) FROM management_config_receipts"
    ).fetchone()[0] == 0
    assert management_service.store.management_daily_admissions(
        "coding",
        "2026-07-19",
    ) == 0


def test_scheduled_reconcile_rejects_authority_change_after_assertion(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.script_claim import issue_script_launch_claim

    job_id = _enable_one_shot_management_job(management_service)
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=_managed_job_snapshot(management_service, job_id),
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)
    invocation = management_service.assert_scheduled_management_invocation()
    current = management_service._configured_authority()
    settings = current.autonomous_profile_management.model_copy(
        update={
            "daily_change_limit": (
                current.autonomous_profile_management.daily_change_limit + 1
            )
        }
    )
    changed = current.model_copy(
        update={"autonomous_profile_management": settings}
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(changed)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    after_edit = management_service.config_path.read_bytes()

    report = management_service.reconcile_management(
        now=NOW,
        scheduled=True,
        scheduled_invocation=invocation,
    )

    assert report.changed is False
    assert report.reason_code == "scheduled_invocation_changed"
    assert management_service.config_path.read_bytes() == after_edit
    assert management_service.store.connection.execute(
        "SELECT COUNT(*) FROM management_revisions"
    ).fetchone()[0] == 0
    assert management_service.store.connection.execute(
        "SELECT COUNT(*) FROM management_config_receipts"
    ).fetchone()[0] == 0
    assert management_service.store.management_daily_admissions(
        "coding",
        "2026-07-18",
    ) == 0


def test_scheduled_reconcile_rejects_profile_only_authority_change_after_assertion(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled claim binds full routing authority, not just management settings."""
    from cron.script_claim import issue_script_launch_claim

    job_id = _enable_one_shot_management_job(management_service)
    with use_cron_store(management_service.hermes_home):
        dispatched = get_job(job_id)
    assert dispatched is not None
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=dispatched,
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)
    invocation = management_service.assert_scheduled_management_invocation()
    current = management_service._configured_authority()
    profile = current.profiles["coding"].model_copy(
        update={"description": "manual profile-only authority edit"}
    )
    changed = current.model_copy(
        update={"profiles": {**current.profiles, "coding": profile}}
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(changed)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    after_edit = management_service.config_path.read_bytes()

    report = management_service.reconcile_management(
        now=NOW,
        scheduled=True,
        scheduled_invocation=invocation,
    )

    assert report.changed is False
    assert report.reason_code == "scheduled_invocation_changed"
    assert management_service.config_path.read_bytes() == after_edit
    assert management_service.store.connection.execute(
        "SELECT COUNT(*) FROM management_revisions"
    ).fetchone()[0] == 0
    assert management_service.store.connection.execute(
        "SELECT COUNT(*) FROM management_config_receipts"
    ).fetchone()[0] == 0


def test_scheduled_one_shot_completion_holds_control_when_job_changes_mid_finalize(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.script_claim import issue_script_launch_claim
    from plugins.auto_routing.auto_routing import service as service_module

    job_id = _enable_one_shot_management_job(management_service)
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=_managed_job_snapshot(management_service, job_id),
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)
    invocation = management_service.assert_scheduled_management_invocation()
    original_get_job = service_module.get_job
    changed = False

    def change_after_first_read(candidate_job_id: str):
        nonlocal changed
        job = original_get_job(candidate_job_id)
        if candidate_job_id == job_id and not changed:
            changed = True
            assert (
                update_job(
                    job_id,
                    {"schedule": "2099-01-02T00:00:00Z"},
                )
                is not None
            )
        return job

    monkeypatch.setattr(service_module, "get_job", change_after_first_read)

    assert (
        management_service.complete_scheduled_management_invocation(invocation) is False
    )
    assert changed is True
    assert management_service.management_status()["cron_job_id"] == job_id


def test_failed_or_untrusted_scheduled_one_shot_does_not_clear_control(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.script_claim import issue_script_launch_claim

    job_id = _enable_one_shot_management_job(management_service)
    untrusted = _run(
        management_service,
        "manage",
        "reconcile",
        "--scheduled",
        "--json",
    )
    assert untrusted.exit_code == 2
    assert management_service.management_status()["cron_job_id"] == job_id

    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=job_id,
        dispatched_job=_managed_job_snapshot(management_service, job_id),
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        management_service,
        "reconcile_management",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("reconcile failed")),
    )

    failed = _run(
        management_service,
        "manage",
        "reconcile",
        "--scheduled",
        "--json",
    )

    assert failed.exit_code == 2
    assert "reconcile failed" in failed.payload["error"]
    assert management_service.management_status()["cron_job_id"] == job_id


def test_manual_reconcile_cannot_run_after_approved_state_changes(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enabled = management_service._configured_authority()
    disabled = enabled.model_copy(
        update={
            "autonomous_profile_management": (
                enabled.autonomous_profile_management.model_copy(
                    update={"enabled": False}
                )
            )
        }
    )
    management_service.config_path.write_text(
        json.dumps(
            {"plugins": {"entries": {"auto-routing": config_document(disabled)}}},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    preview = management_service.preview_management_control(action="reconcile")
    original_reconcile = management_service.reconcile_management

    def enable_after_approval(**kwargs):
        management_service.config_path.write_text(
            json.dumps(
                {"plugins": {"entries": {"auto-routing": config_document(enabled)}}},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return original_reconcile(**kwargs)

    monkeypatch.setattr(
        management_service, "reconcile_management", enable_after_approval
    )

    report = management_service.apply_management_control(
        action="reconcile",
        expected_hash=preview["precondition_hash"],
    )

    assert report.changed is False
    assert report.reason_code == "management_precondition_changed"


def test_scheduled_claim_must_match_the_controlled_management_job(
    management_service: AutoRoutingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cron.jobs import create_job
    from cron.script_claim import issue_script_launch_claim

    enable_preview = management_service.preview_management_control(action="enable")
    enabled = management_service.apply_management_control(
        action="enable",
        expected_hash=enable_preview["precondition_hash"],
    )
    assert enabled["cron_job_id"]
    script = management_service.hermes_home / "scripts" / "unrelated.py"
    script.write_text("print('unrelated')\n", encoding="utf-8")
    with use_cron_store(management_service.hermes_home):
        unrelated = create_job(
            prompt=None,
            schedule="every 5m",
            name="unrelated-cron-job",
            script=script.name,
            no_agent=True,
            script_launch_claim=True,
            deliver="local",
        )
    claim = issue_script_launch_claim(
        home=management_service.hermes_home,
        job_id=unrelated["id"],
        dispatched_job=unrelated,
    )
    for name, value in claim.environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="claim is invalid"):
        management_service.assert_scheduled_management_invocation()


def test_scheduled_reconcile_rejects_manual_approval_flags() -> None:
    result = _run(
        _ScheduledService(),
        "manage",
        "reconcile",
        "--scheduled",
        "--apply",
        "--expect-hash",
        "f" * 64,
        "--json",
    )

    assert result.exit_code == 2
    assert result.payload["error_code"] == "scheduled_approval_forbidden"


def test_manage_status_and_history_are_content_free_and_read_only(
    management_service: AutoRoutingService,
) -> None:
    before = management_service.store.connection.total_changes

    status = _run(management_service, "manage", "status", "--json")
    history = _run(
        management_service,
        "manage",
        "history",
        "--profile-id",
        "coding",
        "--json",
    )

    assert status.exit_code == history.exit_code == 0
    assert management_service.store.connection.total_changes == before
    serialized = json.dumps({
        "status": status.payload,
        "history": history.payload,
    }).casefold()
    for forbidden in ("prompt", "response", "api_key", "endpoint"):
        assert forbidden not in serialized


def test_schedule_requires_a_requested_expression(
    management_service: AutoRoutingService,
) -> None:
    parser = argparse.ArgumentParser(prog="hermes auto-routing")
    build_parser(parser)

    with pytest.raises(SystemExit):
        parser.parse_args(["manage", "schedule", "--json"])
