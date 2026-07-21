"""Independent management canary assignment and decision-snapshot contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent.runtime_routing import AgentRuntimeRequest
from hermes_cli.plugins import PluginContext, PluginManager
from plugins.auto_routing.auto_routing.config import (
    authority_document,
    authority_revision,
    config_document,
    config_revision,
    management_authority_revision,
    parse_config,
)
from plugins.auto_routing.auto_routing.config_io import (
    _management_backup_path,
    _management_receipt_id,
)
from plugins.auto_routing.auto_routing.models import (
    ManagementCanaryAssignment,
    ManagementConfigReceipt,
    ManagementDecisionSnapshot,
    ManagementLifecycleEvent,
    ManagementPatch,
    ManagementProfileState,
    ManagementRevision,
    RankingPackMetadata,
)
from plugins.auto_routing.auto_routing.runtime_resolver import (
    AutoRoutingRuntimeResolver,
    _adapter_capability_sha,
)
from plugins.auto_routing.auto_routing.service import (
    AutoRoutingService,
    ManagementAdvance,
)
from plugins.auto_routing.auto_routing.storage import (
    ImmutableRecordConflict,
    RoutingStore,
)

from _stage2_test_support import plugin_manifest
from _stage3_test_support import (
    PROJECT_ROOT,
    _CatalogSource,
    _Stage3Adapter,
    _authority,
    _request,
)


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
NOW_TEXT = "2026-07-19T12:00:00Z"
PUBLIC_KEY = "MCowBQYDK2VwAyEA7Qmps3rMcxRhc2Y7Qpdn1i8eo1vvS9A0Yrs7mKMbVhc="


def _management_authority() -> dict[str, Any]:
    document = _authority()
    document["adaptation"]["canary_fraction"] = 0.04
    document["profiles"]["coding"]["adaptation"] = {"canary_fraction": 0.03}
    supported_efforts = ["low", "medium", "high"]
    document["profiles"]["coding"]["primary"][
        "supported_reasoning_efforts"
    ] = supported_efforts
    document["profiles"]["coding"]["fallbacks"][0][
        "supported_reasoning_efforts"
    ] = supported_efforts
    document["safe_default"]["supported_reasoning_efforts"] = supported_efforts
    challenger = json.loads(json.dumps(document["profiles"]["coding"]["fallbacks"][0]))
    challenger["revision_status"] = "challenger"
    document["profiles"]["coding"]["primary_challengers"] = [challenger]
    document["profiles"]["coding"]["fallbacks"] = []
    document["autonomous_profile_management"] = {
        "enabled": True,
        "ranking_pack": {
            "ranking_pack_path": "auto-routing/ranking-packs/current.json",
            "trusted_ed25519_public_keys": [PUBLIC_KEY],
        },
        "daily_change_limit": 2,
        "schedule": "17 */6 * * *",
    }
    return document


def _event(
    *,
    authority_id: str,
    profile_id: str,
    revision_id: str,
    event_type: str,
    reason_code: str,
    created_at: str,
) -> ManagementLifecycleEvent:
    return ManagementLifecycleEvent(
        event_id=f"management-{event_type}-{created_at.replace(':', '-')}",
        management_authority_id=authority_id,
        profile_id=profile_id,
        revision_id=revision_id,
        event_type=event_type,
        reason_code=reason_code,
        created_at=created_at,
    )


def _seed_management_canary(
    service: AutoRoutingService,
    config: Any,
    *,
    inventory_revision: str,
    inventory_fingerprint: str,
) -> tuple[ManagementRevision, ManagementRevision]:
    profile = config.profiles["coding"]
    primary_id = profile.primary.runtime.stable_id()
    challenger_id = profile.primary_challengers[0].runtime.stable_id()
    control_profile = profile.model_copy(update={"primary_challengers": ()})
    control_config = config.model_copy(
        update={"profiles": {**config.profiles, "coding": control_profile}}
    )
    control_authority_id = authority_revision(control_config)
    current_authority_id = authority_revision(config)
    management_authority_id = management_authority_revision(config)
    ranking_pack = RankingPackMetadata(
        ranking_pack_id="management-pack",
        ranking_pack_sha256="f" * 64,
        schema_version="1",
        verified_at=NOW_TEXT,
    )
    control = ManagementRevision(
        revision_id="management-control",
        preceding_authority_id="0" * 64,
        resulting_authority_id=control_authority_id,
        management_authority_id=management_authority_id,
        ranking_pack=ranking_pack,
        inventory_revision=inventory_revision,
        inventory_fingerprint=inventory_fingerprint,
        management_epoch=1,
        action="fallback_reorder",
        patches=(
            ManagementPatch(
                profile_id="coding",
                before_runtime_ids=(primary_id,),
                after_runtime_ids=(primary_id,),
                reason_codes=("control_authority",),
            ),
        ),
        created_at=NOW_TEXT,
    )
    challenger = ManagementRevision(
        revision_id="management-challenger",
        preceding_authority_id=control_authority_id,
        resulting_authority_id=current_authority_id,
        management_authority_id=management_authority_id,
        parent_revision_id=control.revision_id,
        ranking_pack=ranking_pack,
        inventory_revision=inventory_revision,
        inventory_fingerprint=inventory_fingerprint,
        management_epoch=2,
        action="propose_canary",
        patches=(
            ManagementPatch(
                profile_id="coding",
                before_runtime_ids=(primary_id,),
                after_runtime_ids=(primary_id, challenger_id),
                reason_codes=("new_primary_challenger",),
            ),
        ),
        runtime_scores=((challenger_id, 0.9),),
        created_at="2026-07-19T12:00:01Z",
    )
    service.store.publish_management_revision(control)
    service.store.publish_management_revision(challenger)
    receipt_id = _management_receipt_id(challenger.revision_id)
    backup_bytes = (
        json.dumps(
            {
                "plugins": {
                    "entries": {
                        "auto-routing": config_document(control_config)
                    }
                }
            },
            indent=2,
        )
        + "\n"
    ).encode()
    _management_backup_path(service.config_path, receipt_id).write_bytes(
        backup_bytes
    )
    prepared_receipt = service.store.record_management_receipt(
        ManagementConfigReceipt(
            receipt_id=receipt_id,
            revision_id=challenger.revision_id,
            phase="prepared",
            preceding_authority_id=control_authority_id,
            resulting_authority_id=current_authority_id,
            backup_checksum=hashlib.sha256(backup_bytes).hexdigest(),
            created_at="2026-07-19T12:00:01Z",
            updated_at="2026-07-19T12:00:01Z",
        )
    )
    replaced_receipt = service.store.recover_management_receipt(
        prepared_receipt.model_copy(update={"phase": "config_replaced"}),
        expected_phase="prepared",
    )
    service.store.recover_management_receipt(
        replaced_receipt.model_copy(update={"phase": "committed"}),
        expected_phase="config_replaced",
    )
    proposed = service.store.transition_management_profile_state(
        profile_id="coding",
        authority_id=management_authority_id,
        expected_generation=0,
        state=ManagementProfileState(
            management_authority_id=management_authority_id,
            profile_id="coding",
            authority_id=current_authority_id,
            active_revision_id=challenger.revision_id,
            management_epoch=challenger.management_epoch,
            experiment_phase="eligible",
            updated_at="2026-07-19T12:00:01Z",
        ),
        event=_event(
            authority_id=management_authority_id,
            profile_id="coding",
            revision_id=challenger.revision_id,
            event_type="proposed",
            reason_code="new_primary_challenger",
            created_at="2026-07-19T12:00:01Z",
        ),
    )
    validated = service.store.transition_management_profile_state(
        profile_id="coding",
        authority_id=management_authority_id,
        expected_generation=proposed.generation,
        state=ManagementProfileState(
            management_authority_id=management_authority_id,
            profile_id="coding",
            authority_id=control_authority_id,
            active_revision_id=control.revision_id,
            management_epoch=challenger.management_epoch,
            control_revision_id=control.revision_id,
            challenger_revision_id=challenger.revision_id,
            experiment_phase="validated",
            updated_at="2026-07-19T12:00:02Z",
        ),
        event=_event(
            authority_id=management_authority_id,
            profile_id="coding",
            revision_id=challenger.revision_id,
            event_type="validated",
            reason_code="management_pair_validated",
            created_at="2026-07-19T12:00:02Z",
        ),
    )
    service.store.transition_management_profile_state(
        profile_id="coding",
        authority_id=management_authority_id,
        expected_generation=validated.generation,
        state=validated.model_copy(
            update={
                "experiment_phase": "canary",
                "updated_at": "2026-07-19T12:00:03Z",
            }
        ),
        event=_event(
            authority_id=management_authority_id,
            profile_id="coding",
            revision_id=challenger.revision_id,
            event_type="canary",
            reason_code="management_canary_started",
            created_at="2026-07-19T12:00:03Z",
        ),
    )
    return control, challenger


@pytest.fixture
def active_service(tmp_path: Path):
    home = tmp_path / "management-profile"
    home.mkdir()
    manager = PluginManager()
    context = PluginContext(plugin_manifest(PROJECT_ROOT), manager)
    adapter = _Stage3Adapter(api_key="MANAGEMENT_TEST_KEY")
    service = AutoRoutingService(
        plugin_context=context,
        hermes_home=home,
        store=RoutingStore.open(home=home),
        adapter=adapter,
        _pinned_config_path=home / "config.yaml",
    )
    resolver = AutoRoutingRuntimeResolver(
        plugin_context=context,
        home_resolver=lambda: home,
        service_factory=lambda: service,
    )
    context.register_agent_runtime_resolver(resolver)
    authority = _management_authority()
    service.config_path.write_text(
        json.dumps(
            {
                "agent": {"reasoning_effort": "low"},
                "plugins": {"entries": {"auto-routing": authority}},
            }
        ),
        encoding="utf-8",
    )
    config = parse_config({"plugins": {"entries": {"auto-routing": authority}}})
    authority_id = authority_revision(config)
    service.store.publish_authority_and_baseline(
        authority_id=authority_id,
        document=authority_document(config),
        baseline=service._baseline_revision(config, authority_id=authority_id),
    )
    inventory = service._new_inventory_service().refresh(refresh=False, persist=True)
    runtime_ids = {
        runtime.key.model: runtime.key.stable_id()
        for runtime in inventory.runtimes
        if runtime.key.model in {"primary-model", "fallback-model"}
    }
    assert set(runtime_ids) == {"primary-model", "fallback-model"}
    from plugins.auto_routing.auto_routing.catalog import CatalogService

    CatalogService(store=service.store).refresh(
        [_CatalogSource(adapter.now, runtime_ids)]
    )
    preview = service.preview_activation("active")
    assert preview["doctor"]["healthy"] is True, preview["doctor"]
    applied = service.apply_activation(
        "active",
        expected_config_sha256=preview["expected_config_sha256"],
    )
    assert applied["applied"] is True
    config = service._configured_authority()
    stored_inventory = service.store.read_inventory_snapshot(inventory.revision)
    assert stored_inventory is not None
    control, challenger = _seed_management_canary(
        service,
        config,
        inventory_revision=inventory.revision,
        inventory_fingerprint=stored_inventory.checksum,
    )
    try:
        yield service, resolver, config, adapter, control, challenger
    finally:
        resolver.close()


def _receipt_inputs(service: AutoRoutingService, config: Any, adapter: Any):
    capability_sha = _adapter_capability_sha(adapter)
    receipt = service.store.read_matching_activation_receipt(
        authority_id=authority_revision(config),
        config_sha=config_revision(config),
        adapter_capability_sha=capability_sha,
    )
    assert receipt is not None
    return receipt, capability_sha


def _decision_for(
    service: AutoRoutingService,
    config: Any,
    adapter: Any,
    request: AgentRuntimeRequest,
):
    receipt, capability_sha = _receipt_inputs(service, config, adapter)
    plan = service.create_runtime_decision(
        request=request,
        config=config,
        activation_receipt=receipt,
        adapter_capability_sha=capability_sha,
    )
    assert plan.decision_id is not None
    decision = service.store.read_decision(plan.decision_id)
    assert decision is not None
    return plan, decision


def test_fresh_management_canary_persists_final_runtime_before_dispatch(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, config, adapter, _control, challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )

    plan, decision = _decision_for(
        service,
        config,
        adapter,
        _request(session_id="management-fresh", task_id="management-task"),
    )

    assert plan.runtime.model == "fallback-model"
    assert decision.management_revision_id == challenger.revision_id
    assert decision.management_assignment_id is not None
    assert decision.management_profile_snapshot == {
        "coding": challenger.revision_id
    }
    assignment = service.store.read_management_assignment(
        decision.management_assignment_id
    )
    assert assignment is not None
    assert assignment.phase == "finalized"
    assert assignment.runtime_id == decision.selected_runtime.stable_id()
    assert assignment.reasoning_effort == decision.selected_reasoning_effort


@pytest.mark.parametrize("race", ["settlement", "cancellation"])
def test_fresh_management_decision_commit_rejects_closed_canary_race(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    service, _resolver, config, adapter, control, challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    real_commit = service.store.commit_decision

    def close_canary_before_commit(decision, **kwargs):
        management_authority_id = management_authority_revision(config)
        state = service.store.read_management_profile_state(
            management_authority_id,
            "coding",
        )
        closed_at = "2026-07-19T12:00:04Z"
        if race == "settlement":
            service.store.transition_management_profile_state(
                profile_id="coding",
                authority_id=management_authority_id,
                expected_generation=state.generation,
                state=state.model_copy(
                    update={
                        "experiment_phase": "cooldown",
                        "cooldown_until": "2026-07-19T13:00:04Z",
                        "updated_at": closed_at,
                    }
                ),
                event=_event(
                    authority_id=management_authority_id,
                    profile_id="coding",
                    revision_id=challenger.revision_id,
                    event_type="rejected",
                    reason_code="concurrent_settlement",
                    created_at=closed_at,
                ),
            )
        else:
            service.store.cancel_stale_management_experiment(
                profile_id="coding",
                authority_id=management_authority_id,
                expected_generation=state.generation,
                state=state.model_copy(
                    update={
                        "authority_id": control.resulting_authority_id,
                        "active_revision_id": control.revision_id,
                        "management_epoch": control.management_epoch,
                        "control_revision_id": None,
                        "challenger_revision_id": None,
                        "experiment_phase": "eligible",
                        "cooldown_until": None,
                        "updated_at": closed_at,
                    }
                ),
                event=_event(
                    authority_id=management_authority_id,
                    profile_id="coding",
                    revision_id=control.revision_id,
                    event_type="recovered",
                    reason_code="manual_authority_changed",
                    created_at=closed_at,
                ),
            )
        return real_commit(decision, **kwargs)

    monkeypatch.setattr(service.store, "commit_decision", close_canary_before_commit)

    with pytest.raises(ImmutableRecordConflict, match="management"):
        _decision_for(
            service,
            config,
            adapter,
            _request(
                session_id=f"management-commit-{race}",
                task_id=f"management-commit-{race}-task",
            ),
        )

    assert service.store.connection.execute(
        "SELECT COUNT(*) FROM routing_decisions WHERE session_id=?",
        (f"management-commit-{race}",),
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault",
    ["assignment", "revision", "operation", "runtime", "reasoning"],
)
def test_fresh_management_decision_commit_rejects_nonexact_attestation(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    service, _resolver, config, adapter, control, _challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    real_commit = service.store.commit_decision

    def corrupt_attestation(decision, **kwargs):
        updates: dict[str, Any]
        if fault == "assignment":
            updates = {"management_assignment_id": "missing-assignment"}
        elif fault == "revision":
            updates = {
                "management_revision_id": control.revision_id,
                "management_profile_snapshot": {"coding": control.revision_id},
            }
        elif fault == "operation":
            updates = {"task_id": f"{decision.task_id}-changed"}
        elif fault == "runtime":
            updates = {
                "selected_runtime": config.profiles["coding"].primary.runtime
            }
        else:
            updates = {"selected_reasoning_effort": "high"}
        return real_commit(decision.model_copy(update=updates), **kwargs)

    monkeypatch.setattr(service.store, "commit_decision", corrupt_attestation)

    with pytest.raises(ImmutableRecordConflict, match="management"):
        _decision_for(
            service,
            config,
            adapter,
            _request(
                session_id=f"management-attestation-{fault}",
                task_id=f"management-attestation-{fault}-task",
            ),
        )


@pytest.mark.parametrize(
    ("arm", "expected_model"),
    [("control", "primary-model"), ("challenger", "fallback-model")],
)
def test_management_arms_preserve_ordinary_low_effort_before_dispatch(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    arm: str,
    expected_model: str,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    resolved_models: list[str] = []
    real_resolve = adapter.resolve

    def resolve(runtime_key):
        resolved_models.append(runtime_key.model)
        return real_resolve(runtime_key)

    monkeypatch.setattr(adapter, "resolve", resolve)
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: arm,
    )

    plan, decision = _decision_for(
        service,
        config,
        adapter,
        _request(
            session_id=f"management-low-{arm}",
            task_id=f"management-low-{arm}-task",
        ),
    )

    assert config.profiles["coding"].primary.reasoning.default == "high"
    assert config.profiles["coding"].primary_challengers[0].reasoning.default == "high"
    assert resolved_models == [expected_model]
    assert plan.runtime.model == expected_model
    assert plan.runtime.reasoning_config["effort"] == "low"
    assert decision.selected_reasoning_effort == "low"
    assert decision.management_assignment_id is not None
    assignment = service.store.read_management_assignment(
        decision.management_assignment_id
    )
    assert assignment is not None and assignment.reasoning_effort == "low"


def test_management_arm_uses_only_management_owned_canary_fraction(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    observed_fractions: list[float] = []

    def choose_control(_key, _profile_id, _operation_hash, fraction):
        observed_fractions.append(fraction)
        return "control"

    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        choose_control,
    )

    _decision_for(
        service,
        config,
        adapter,
        _request(
            session_id="management-policy-independent",
            task_id="management-policy-independent-task",
        ),
    )

    assert config.adaptation.canary_fraction == 0.04
    assert config.profiles["coding"].adaptation.canary_fraction == 0.03
    assert config.autonomous_profile_management.canary_fraction == 0.05
    assert observed_fractions == [0.05]


@pytest.mark.parametrize(
    ("context_updates", "metadata"),
    [
        ({"manual_runtime_pin": True, "manual_pin_source": "test"}, {}),
        ({"is_resume": True}, {}),
        ({}, {"is_compression": True}),
        ({}, {"fixed_delegation_provider": True}),
        ({}, {"recorded_replay": True}),
        ({}, {"recovered": True}),
        ({}, {"active_session": True}),
    ],
)
def test_existing_or_user_owned_boundaries_never_receive_management_overlay(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    context_updates: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    suffix = str(abs(hash(json.dumps([context_updates, metadata], sort_keys=True))))
    base = _request(session_id=f"boundary-{suffix}", task_id=f"task-{suffix}")
    request = replace(
        base,
        context=replace(
            base.context,
            metadata={"platform": "cli", **metadata},
            **context_updates,
        ),
    )

    plan, decision = _decision_for(service, config, adapter, request)

    assert plan.runtime.model == "primary-model"
    assert decision.management_revision_id is None
    assert decision.management_assignment_id is None
    assert decision.management_profile_snapshot == {}


def test_pending_lifecycle_finalization_disables_management_overlay(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    monkeypatch.setattr(
        service.store,
        "list_pending_management_lifecycle_finalizations",
        lambda: (object(),),
    )

    plan, decision = _decision_for(
        service,
        config,
        adapter,
        _request(
            session_id="management-pending-finalization",
            task_id="management-pending-finalization-task",
        ),
    )

    assert plan.runtime.model == "primary-model"
    assert decision.management_revision_id is None
    assert decision.management_assignment_id is None
    assert decision.management_profile_snapshot == {}


@pytest.mark.parametrize("failure", ["resolve", "reserve", "finalize"])
def test_management_failure_uses_recorded_control_and_never_dispatches_challenger(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    if failure == "resolve":
        real_resolve = adapter.resolve

        def resolve(runtime_key):
            if runtime_key.model == "fallback-model":
                raise RuntimeError("challenger unavailable")
            return real_resolve(runtime_key)

        monkeypatch.setattr(adapter, "resolve", resolve)
    elif failure == "reserve":
        monkeypatch.setattr(
            service.store,
            "reserve_management_assignment",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("reservation unavailable")
            ),
        )
    else:
        monkeypatch.setattr(
            service.store,
            "finalize_management_assignment",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("finalization unavailable")
            ),
        )

    plan, decision = _decision_for(
        service,
        config,
        adapter,
        _request(
            session_id=f"management-failure-{failure}",
            task_id=f"management-failure-task-{failure}",
        ),
    )

    assert plan.runtime.model == "primary-model"
    assert plan.runtime.reasoning_config["effort"] == "low"
    assert decision.selected_runtime.model == "primary-model"
    assert decision.selected_reasoning_effort == "low"
    assert decision.management_assignment_id is None
    assert decision.management_profile_snapshot == {}
    if failure == "resolve":
        rolled_back = service.store.read_management_profile_state(
            management_authority_revision(config), "coding"
        )
        assert rolled_back.experiment_phase == "cooldown"
        assert rolled_back.rejection_count == 1
        assert service._configured_authority().profiles["coding"].primary_challengers == ()


def test_unsupported_challenger_low_effort_falls_back_to_exact_control_low(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    captured: dict[str, Any] = {}
    real_overlay = service._apply_management_runtime_overlay

    def capture_overlay(**kwargs):
        captured.update(kwargs)
        return (
            kwargs["selection"],
            kwargs["projected_spec"],
            ManagementDecisionSnapshot(),
        )

    monkeypatch.setattr(service, "_apply_management_runtime_overlay", capture_overlay)
    _decision_for(
        service,
        config,
        adapter,
        _request(
            session_id="management-capture-selection",
            task_id="management-capture-selection-task",
        ),
    )
    monkeypatch.setattr(service, "_apply_management_runtime_overlay", real_overlay)
    profile = config.profiles["coding"]
    unsupported_challenger = profile.primary_challengers[0].model_copy(
        update={"supported_reasoning_efforts": ("high",)}
    )
    unsupported_profile = profile.model_copy(
        update={"primary_challengers": (unsupported_challenger,)}
    )
    unsupported_config = config.model_copy(
        update={
            "profiles": {**config.profiles, "coding": unsupported_profile}
        }
    )
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )

    selected, projected, snapshot = real_overlay(
        request=_request(
            session_id="management-unsupported-low",
            task_id="management-unsupported-low-task",
        ),
        config=unsupported_config,
        selection=captured["selection"],
        inventory=captured["inventory"],
        projected_spec=None,
        adaptive_assignment_id=None,
        now=NOW,
    )

    assert selected.selected_runtime.key.model == "primary-model"
    assert selected.selected_reasoning_effort == "low"
    assert projected.model == "primary-model"
    assert projected.reasoning_config["effort"] == "low"
    assert snapshot == ManagementDecisionSnapshot()


def test_management_assignment_does_not_change_stage4_adaptation_state(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    stage4_authority = authority_revision(config)
    before = service.store.read_profile_control(stage4_authority, "coding")

    _decision_for(
        service,
        config,
        adapter,
        _request(
            session_id="management-adaptation-independent",
            task_id="management-adaptation-task",
        ),
    )

    after = service.store.read_profile_control(stage4_authority, "coding")
    assert after == before


def test_finalize_failure_discards_reservation_before_management_rollback(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, config, adapter, _control, challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    monkeypatch.setattr(
        service.store,
        "finalize_management_assignment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("finalization unavailable")
        ),
    )

    plan, decision = _decision_for(
        service,
        config,
        adapter,
        _request(
            session_id="management-finalize-poison",
            task_id="management-finalize-poison-task",
        ),
    )

    assert plan.runtime.model == "primary-model"
    assert decision.management_assignment_id is None
    assert service.store.list_open_management_assignments(
        management_authority_revision(config), "coding"
    ) == ()
    adverse = ({
        "evidence_id": "e" * 64,
        "parent_evidence_id": None,
        "decision_id": "decision-after-finalize-failure",
        "assignment_id": "management-never-dispatched",
        "is_initial_routing_task": True,
        "source": "user_feedback",
        "outcome": None,
        "feedback_value": "rejected",
        "retry_count": 0,
        "cost_usd": 0.0,
        "latency_seconds": 0.0,
        "observed_at": "2026-07-19T12:05:00Z",
    },)
    monkeypatch.setattr(
        service,
        "list_management_observations",
        lambda **kwargs: (
            adverse if kwargs["revision_id"] == challenger.revision_id else ()
        ),
    )

    result = service.maybe_advance_management(
        profile_id="coding", now=NOW.replace(minute=10)
    )

    assert result.action == "rollback"
    state = service.store.read_management_profile_state(
        management_authority_revision(config), "coding"
    )
    assert state.experiment_phase == "cooldown"


def test_resolver_rollback_saga_failure_freezes_management_without_dispatch(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    real_resolve = adapter.resolve

    def fail_challenger(runtime_key):
        if runtime_key.model == "fallback-model":
            raise RuntimeError("challenger unavailable")
        return real_resolve(runtime_key)

    monkeypatch.setattr(adapter, "resolve", fail_challenger)
    monkeypatch.setattr(
        service,
        "_apply_management_lifecycle_revision",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("rollback saga unavailable")
        ),
    )

    plan, decision = _decision_for(
        service,
        config,
        adapter,
        _request(
            session_id="management-resolver-saga-failure",
            task_id="management-resolver-saga-failure-task",
        ),
    )

    assert plan.runtime.model == "primary-model"
    assert decision.management_assignment_id is None
    authority_id = management_authority_revision(config)
    assert service.store.read_management_control(authority_id).frozen is True
    state = service.store.read_management_profile_state(authority_id, "coding")
    assert state.experiment_phase == "recovery_required"


@pytest.mark.parametrize("discard_failure", ["changed", "raised"])
def test_unprovable_finalize_cleanup_freezes_management_without_dispatch(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    discard_failure: str,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    monkeypatch.setattr(
        service.store,
        "finalize_management_assignment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("finalization unavailable")
        ),
    )
    if discard_failure == "changed":
        monkeypatch.setattr(
            service.store,
            "discard_management_reservation",
            lambda *_args, **_kwargs: False,
        )
    else:
        monkeypatch.setattr(
            service.store,
            "discard_management_reservation",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("discard unavailable")
            ),
        )

    plan, decision = _decision_for(
        service,
        config,
        adapter,
        _request(
            session_id=f"management-discard-{discard_failure}",
            task_id=f"management-discard-{discard_failure}-task",
        ),
    )

    assert plan.runtime.model == "primary-model"
    assert decision.management_assignment_id is None
    authority_id = management_authority_revision(config)
    assert service.store.read_management_control(authority_id).frozen is True
    assert service.store.read_management_profile_state(
        authority_id, "coding"
    ).experiment_phase == "recovery_required"


def test_recorded_management_replay_requires_its_finalized_assignment(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    request = _request(
        session_id="management-replay",
        task_id="management-replay-task",
    )
    _plan, decision = _decision_for(service, config, adapter, request)
    assert decision.management_assignment_id is not None
    binding = service.store.read_session_binding(request.context.session_id)
    assert binding is not None
    service.store.connection.execute(
        "DELETE FROM management_canary_assignments WHERE assignment_id=?",
        (decision.management_assignment_id,),
    )

    from plugins.auto_routing.auto_routing.service import AutoRoutingServiceError

    with pytest.raises(AutoRoutingServiceError, match="management assignment"):
        service.replay_runtime_decision(
            request=replace(
                request,
                context=replace(request.context, is_resume=True),
            ),
            binding=binding,
        )


def test_management_outcome_derives_profile_only_from_exact_persisted_decision(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    _plan, decision = _decision_for(
        service,
        config,
        adapter,
        _request(
            session_id="management-outcome-exact",
            task_id="management-outcome-exact-task",
        ),
    )
    observed_profiles: list[str] = []

    def capture_advance(*, profile_id: str, now=None):
        observed_profiles.append(profile_id)
        return ManagementAdvance("hold", "captured")

    monkeypatch.setattr(service, "maybe_advance_management", capture_advance)

    result = service.record_management_outcome(
        {"decision_id": decision.decision_id, "profile_id": "caller-controlled"},
        now=NOW,
    )

    assert result == ManagementAdvance("hold", "captured")
    assert observed_profiles == ["coding"]


def test_management_outcome_rejects_profile_only_unknown_and_stage4_decisions(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    manual = _request(
        session_id="management-outcome-stage4-only",
        task_id="management-outcome-stage4-only-task",
    )
    manual = replace(
        manual,
        context=replace(
            manual.context,
            manual_runtime_pin=True,
            manual_pin_source="test",
        ),
    )
    _plan, stage4_only = _decision_for(service, config, adapter, manual)
    before = service.store.read_management_profile_state(
        management_authority_revision(config), "coding"
    )
    monkeypatch.setattr(
        service,
        "maybe_advance_management",
        lambda **_kwargs: pytest.fail("invalid outcome advanced management"),
    )

    outcomes = (
        {"profile_id": "coding"},
        {"decision_id": "missing-decision", "profile_id": "coding"},
        {"decision_id": stage4_only.decision_id, "profile_id": "coding"},
    )
    results = tuple(service.record_management_outcome(item, now=NOW) for item in outcomes)

    assert all(
        result == ManagementAdvance("hold", "management_outcome_unattributed")
        for result in results
    )
    assert service.store.read_management_profile_state(
        management_authority_revision(config), "coding"
    ) == before


@pytest.mark.parametrize(
    "assignment_fault",
    [
        "missing",
        "reserved",
        "profile",
        "authority",
        "arm",
        "runtime",
        "reasoning",
        "operation",
        "tampered",
    ],
)
def test_management_outcome_rejects_nonexact_assignment_without_mutation(
    active_service: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    assignment_fault: str,
) -> None:
    service, _resolver, config, adapter, _control, _challenger = active_service
    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    _plan, decision = _decision_for(
        service,
        config,
        adapter,
        _request(
            session_id=f"management-outcome-{assignment_fault}",
            task_id=f"management-outcome-{assignment_fault}-task",
        ),
    )
    assert decision.management_assignment_id is not None
    assignment = service.store.read_management_assignment(
        decision.management_assignment_id
    )
    assert assignment is not None
    before = service.store.read_management_profile_state(
        management_authority_revision(config), "coding"
    )
    replacements: dict[str, ManagementCanaryAssignment | None] = {
        "missing": None,
        "reserved": assignment.model_copy(
            update={"phase": "reserved", "runtime_id": None, "reasoning_effort": None}
        ),
        "profile": assignment.model_copy(update={"profile_id": "research"}),
        "authority": assignment.model_copy(
            update={"management_authority_id": "f" * 64}
        ),
        "arm": assignment.model_copy(update={"arm": "control"}),
        "runtime": assignment.model_copy(update={"runtime_id": "f" * 64}),
        "reasoning": assignment.model_copy(update={"reasoning_effort": "high"}),
        "operation": assignment.model_copy(
            update={"operation_identity_hash": "1" * 64}
        ),
        "tampered": assignment,
    }
    if assignment_fault == "tampered":
        monkeypatch.setattr(
            service.store,
            "read_management_assignment",
            lambda _assignment_id: (_ for _ in ()).throw(
                RuntimeError("assignment checksum mismatch")
            ),
        )
    else:
        monkeypatch.setattr(
            service.store,
            "read_management_assignment",
            lambda _assignment_id: replacements[assignment_fault],
        )
    monkeypatch.setattr(
        service,
        "maybe_advance_management",
        lambda **_kwargs: pytest.fail("invalid outcome advanced management"),
    )

    result = service.record_management_outcome(
        {"decision_id": decision.decision_id}, now=NOW
    )

    assert result == ManagementAdvance("hold", "management_outcome_unattributed")
    assert service.store.read_management_profile_state(
        management_authority_revision(config), "coding"
    ) == before
