"""Real Stage 5 management revision, replay, and content-hygiene proofs."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource
from hermes_cli.plugins import PluginContext, PluginManager
from plugins.auto_routing.auto_routing.catalog import CatalogService
from plugins.auto_routing.auto_routing.config import (
    authority_document,
    authority_revision,
    config_revision,
    management_authority_revision,
    parse_config,
)
from plugins.auto_routing.auto_routing.runtime_resolver import (
    AutoRoutingRuntimeResolver,
)
from plugins.auto_routing.auto_routing.service import AutoRoutingService
from plugins.auto_routing.auto_routing.storage import EvidenceCommit, RoutingStore
from tui_gateway import server as tui_server
from _stage2_test_support import install_runtime_resolver, plugin_manifest
from _stage3_test_support import (
    PROJECT_ROOT,
    _CatalogSource,
    _Stage3Adapter,
    _request,
)
from test_stage2_gateway_tui import _gateway_runner
from test_management_assignment import _management_authority
from test_ranking_pack import (
    TEST_PRIVATE_KEY,
    _public_key_bytes,
    _write_signed_pack,
)


SECRET_SENTINEL = "sk-secret-sentinel"
ENDPOINT_SENTINEL = "https://endpoint.example"


@pytest.fixture
def stage5_active_service(tmp_path):
    home = tmp_path / "stage5-active-profile"
    home.mkdir()
    manager = PluginManager()
    context = PluginContext(plugin_manifest(PROJECT_ROOT), manager)
    adapter = _Stage3Adapter(api_key=SECRET_SENTINEL)
    adapter.base_url = ENDPOINT_SENTINEL
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
    coding = authority["profiles"]["coding"]
    available_challenger = coding["primary_challengers"].pop(0)
    available_challenger["revision_status"] = "fallback"
    coding["fallbacks"] = [available_challenger]
    authority["autonomous_profile_management"]["ranking_pack"][
        "trusted_ed25519_public_keys"
    ] = [base64.b64encode(_public_key_bytes(TEST_PRIVATE_KEY)).decode("ascii")]
    service.config_path.write_text(
        json.dumps({
            "agent": {"reasoning_effort": "low"},
            "plugins": {"entries": {"auto-routing": authority}},
        }),
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
    CatalogService(store=service.store).refresh([
        _CatalogSource(adapter.now, runtime_ids)
    ])
    preview = service.preview_activation("active")
    assert preview["doctor"]["healthy"] is True, preview["doctor"]
    applied = service.apply_activation(
        "active",
        expected_config_sha256=preview["expected_config_sha256"],
    )
    assert applied["applied"] is True
    config = service._configured_authority()
    assert resolver.service_for_current_profile() is service
    try:
        yield service, resolver, config, inventory
    finally:
        resolver.close()


def _install_local_management_inputs(
    service: AutoRoutingService,
    inventory,
) -> datetime:
    previous = service.store.read_inventory_snapshot(inventory.revision)
    assert previous is not None
    moment = datetime.now(UTC) + timedelta(hours=2)
    moment_text = moment.isoformat().replace("+00:00", "Z")
    expires_text = (moment + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    revision = "inventory-stage5-management-e2e"

    observations = []
    for observation in previous.observations:
        observations.append(
            observation.model_copy(
                update={
                    "key": observation.key.model_copy(
                        update={"inventory_revision": revision}
                    ),
                    "verified_at": moment_text,
                    "verification_expires_at": expires_text,
                    "observed_at": moment_text,
                }
            )
        )
    service.store.write_inventory_snapshot(
        revision,
        observations,
        created_at=moment_text,
    )
    rankings: dict[str, dict[str, float]] = {
        observation.key.stable_id(): {
            "quality": 0.70,
            "reliability": 0.70,
            "latency": 0.30,
            "cost": 0.30,
        }
        for observation in observations
    }
    available = next(
        observation
        for observation in observations
        if observation.key.model == "fallback-model"
    )
    rankings[available.key.stable_id()] = {
        "quality": 0.99,
        "reliability": 0.99,
        "latency": 0.01,
        "cost": 0.01,
    }
    _write_signed_pack(
        service.hermes_home,
        expires_at=moment + timedelta(days=1),
        rankings=rankings,
    )
    return moment


def _apply_local_management_change(
    service: AutoRoutingService,
    inventory,
):
    moment = _install_local_management_inputs(service, inventory)

    report = service.reconcile_management(now=moment)

    assert report.changed is True
    assert report.reason_code == "revision_applied"
    return report


def _dump_management_tables(store: RoutingStore) -> str:
    tables = [
        str(row["name"])
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'management_%' ORDER BY name"
        )
    ]
    payload: dict[str, list[dict[str, Any]]] = {}
    for table in tables:
        payload[table] = [
            {key: row[key] for key in row.keys()}
            for row in store.connection.execute(f'SELECT * FROM "{table}"')
        ]
    return json.dumps(payload, sort_keys=True, default=str)


def test_gateway_and_tui_replay_original_snapshot_after_management_change(
    stage5_active_service,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, resolver, config, inventory = stage5_active_service
    install_runtime_resolver(monkeypatch, resolver)
    original_authority = authority_revision(config)

    runner = _gateway_runner()
    monkeypatch.setattr(
        gateway_run,
        "_resolve_gateway_model",
        lambda _config=None: "unavailable-baseline",
    )
    monkeypatch.setattr(runner, "_refresh_fallback_model", lambda: [])
    monkeypatch.setattr(
        runner,
        "_resolve_session_reasoning_config",
        lambda **_kwargs: None,
    )
    source = SessionSource(
        platform=Platform.API_SERVER,
        chat_id="stage5-gateway-chat",
        chat_type="dm",
        user_id="stage5-user",
    )
    original_gateway = runner._prepare_gateway_agent_runtime(
        task="route this gateway task",
        source=source,
        session_id="stage5-gateway-session",
        session_key="stage5-gateway-key",
        user_config={},
        is_resume=False,
    )["prepared"].plan

    constructed: list[dict[str, Any]] = []

    class FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.constructor = kwargs
            constructed.append(kwargs)

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr(tui_server, "_load_cfg", lambda: {"agent": {}})
    monkeypatch.setattr(tui_server, "_parse_tui_skills_env", lambda: [])
    monkeypatch.setattr(tui_server, "_load_provider_routing", lambda: {})
    monkeypatch.setattr(
        tui_server,
        "_load_reasoning_config",
        lambda _model: None,
    )
    monkeypatch.setattr(tui_server, "_load_service_tier", lambda: None)
    monkeypatch.setattr(tui_server, "_load_enabled_toolsets", lambda: None)
    monkeypatch.setattr(tui_server, "_load_fallback_model", lambda: [])
    monkeypatch.setattr(tui_server, "_agent_cbs", lambda _sid: {})
    monkeypatch.setattr(tui_server, "_get_db", lambda: None)
    monkeypatch.setattr(
        tui_server,
        "_resolve_startup_runtime",
        lambda: ("unavailable-baseline", "unavailable-provider"),
    )
    monkeypatch.setattr(
        tui_server,
        "_tui_launch_runtime_pin_fields",
        lambda: frozenset(),
    )
    original_tui_agent = tui_server._make_agent(
        "stage5-tui",
        "stage5-tui-key",
        session_id="stage5-tui-session",
        initial_task="route this TUI task",
        platform_override="tui",
    )
    original_tui = original_tui_agent.constructor["prepared_agent_runtime"].plan

    for plan in (original_gateway, original_tui):
        assert plan.action == "project"
        assert plan.decision_id is not None

    _apply_local_management_change(service, inventory)
    assert authority_revision(service._configured_authority()) != original_authority

    replay_gateway = runner._prepare_gateway_agent_runtime(
        task=None,
        source=source,
        session_id="stage5-gateway-session",
        session_key="stage5-gateway-key",
        user_config={},
        is_resume=True,
    )["prepared"].plan
    replay_tui_agent = tui_server._make_agent(
        "stage5-tui",
        "stage5-tui-key",
        session_id="stage5-tui-session",
        initial_task=None,
        platform_override="tui",
        is_resume=True,
    )
    replay_tui = replay_tui_agent.constructor["prepared_agent_runtime"].plan

    assert len(constructed) == 2
    for original, replay in (
        (original_gateway, replay_gateway),
        (original_tui, replay_tui),
    ):
        assert replay.action == "project"
        assert replay.decision_id == original.decision_id
        assert replay.runtime.public_record() == original.runtime.public_record()
        assert replay.runtime.reasoning_config == original.runtime.reasoning_config
        assert replay.runtime.fallback_model == original.runtime.fallback_model


def test_management_reports_and_sql_records_are_content_free(
    stage5_active_service,
) -> None:
    service, _resolver, _config, inventory = stage5_active_service
    reconcile_report = _apply_local_management_change(service, inventory)

    rendered = json.dumps(
        {
            "reconcile": asdict(reconcile_report),
            "status": service.management_status(),
            "history": service.management_history(),
        },
        sort_keys=True,
        default=str,
    )
    stored = _dump_management_tables(service.store)

    for sentinel in (SECRET_SENTINEL, ENDPOINT_SENTINEL):
        assert sentinel not in rendered
        assert sentinel not in stored


def test_reconcile_rolls_active_receipt_into_fresh_assignment_and_post_turn(
    stage5_active_service,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, resolver, _config, inventory = stage5_active_service
    receipts_before = service.store.connection.execute(
        "SELECT COUNT(*) FROM activation_receipts"
    ).fetchone()[0]
    _apply_local_management_change(service, inventory)
    current = service._configured_authority()
    assert current.activation.mode == "active"
    assert (
        service.store.connection.execute(
            "SELECT COUNT(*) FROM activation_receipts"
        ).fetchone()[0]
        == receipts_before + 1
    )

    monkeypatch.setattr(
        "plugins.auto_routing.auto_routing.service.deterministic_canary_arm",
        lambda *_args, **_kwargs: "challenger",
    )
    request = _request(
        session_id="stage5-managed-fresh",
        task_id="stage5-managed-task",
    )
    plan = resolver.resolve(request)

    assert plan.action == "project"
    assert plan.reason_code == "active_projected"
    assert plan.decision_id is not None
    decision = service.store.read_decision(plan.decision_id)
    assert decision is not None
    state = service.store.read_management_profile_state(
        management_authority_revision(current), "coding"
    )
    assert state.experiment_phase == "canary", state
    managed_id = current.profiles["coding"].primary_challengers[0].runtime.stable_id()
    assert managed_id in decision.eligible_candidates, decision.rejected_candidates
    assert decision.management_assignment_id is not None
    assignment = service.store.read_management_assignment(
        decision.management_assignment_id
    )
    assert assignment is not None and assignment.phase == "finalized"

    resolver.on_pre_api_request(
        session_id=request.context.session_id,
        task_id=request.context.task_id,
        api_request_id="stage5-managed-request",
        decision_id=decision.decision_id,
        runtime_id=decision.selected_runtime.stable_id(),
        model=decision.selected_runtime.model,
        provider=decision.selected_runtime.provider,
    )
    committed: list[EvidenceCommit] = []
    real_record = service.record_management_outcome

    def record(outcome):
        committed.append(outcome)
        return real_record(outcome)

    monkeypatch.setattr(service, "record_management_outcome", record)
    resolver.on_post_turn_outcome(
        telemetry_schema_version="hermes.observer.v1",
        session_id=request.context.session_id,
        turn_id="stage5-managed-turn",
        task_id=request.context.task_id,
        observed_at_unix=datetime.now(UTC).timestamp(),
        outcome="verified",
        api_calls=1,
        tool_iterations=0,
        retry_count=0,
        cost_usd=0.001,
        input_tokens=5,
        output_tokens=1,
        cache_read_tokens=0,
        reasoning_effort=decision.selected_reasoning_effort,
        runtime_binding={
            "scope": decision.scope,
            "session_id": decision.session_id,
            "task_id": decision.task_id,
            "action": "project",
            "model": decision.selected_runtime.model,
            "provider": decision.selected_runtime.provider,
            "decision_id": decision.decision_id,
        },
    )

    assert len(committed) == 1
    assert committed[0].event.decision_id == decision.decision_id
    assert (
        service.store.connection.execute(
            "SELECT COUNT(*) FROM evidence_events WHERE decision_id=?",
            (decision.decision_id,),
        ).fetchone()[0]
        == 1
    )


def test_active_rollover_store_failure_restores_config_without_orphan_records(
    stage5_active_service,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, _config, inventory = stage5_active_service
    moment = _install_local_management_inputs(service, inventory)
    exact_before = service.config_path.read_bytes()
    counts_before = {
        table: service.store.connection.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]
        for table in (
            "activation_receipts",
            "authority_revisions",
            "adaptive_revisions",
            "active_adaptive_revisions",
        )
    }
    transitions = 0
    real_transition = service.store.transition_management_profile_state

    def transition(**kwargs):
        nonlocal transitions
        transitions += 1
        if transitions == 2:
            raise RuntimeError("injected post-replace state failure")
        return real_transition(**kwargs)

    monkeypatch.setattr(
        service.store,
        "transition_management_profile_state",
        transition,
    )

    report = service.reconcile_management(now=moment)

    assert report.changed is False
    assert report.reason_code == "config_restored_after_store_failure"
    assert service.config_path.read_bytes() == exact_before
    assert {
        table: service.store.connection.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]
        for table in counts_before
    } == counts_before


def test_active_daily_cap_control_rolls_receipt_and_keeps_fresh_routing(
    stage5_active_service,
) -> None:
    service, resolver, _config, _inventory = stage5_active_service
    receipts_before = service.store.connection.execute(
        "SELECT COUNT(*) FROM activation_receipts"
    ).fetchone()[0]
    preview = service.preview_management_control(
        action="daily-cap",
        daily_limit=4,
    )

    applied = service.apply_management_control(
        action="daily-cap",
        daily_limit=4,
        expected_hash=preview["precondition_hash"],
    )
    plan = resolver.resolve(
        _request(
            session_id="stage5-daily-cap",
            task_id="stage5-daily-cap-task",
        )
    )

    assert isinstance(applied, dict)
    assert (
        service.store.connection.execute(
            "SELECT COUNT(*) FROM activation_receipts"
        ).fetchone()[0]
        == receipts_before + 1
    )
    assert plan.action == "project"
    assert plan.reason_code == "active_projected"


def test_active_disable_enable_exact_return_reuses_activation_receipt(
    stage5_active_service,
) -> None:
    service, resolver, config, _inventory = stage5_active_service
    original = service.store.read_matching_activation_receipt(
        authority_id=authority_revision(config),
        config_sha=config_revision(config),
        adapter_capability_sha=service._adapter_contract(service.adapter)[1],
    )
    assert original is not None

    disable_preview = service.preview_management_control(action="disable")
    disabled = service.apply_management_control(
        action="disable",
        expected_hash=disable_preview["precondition_hash"],
    )
    assert isinstance(disabled, dict) and disabled["enabled"] is False
    receipts_after_disable = service.store.connection.execute(
        "SELECT COUNT(*) FROM activation_receipts"
    ).fetchone()[0]

    enable_preview = service.preview_management_control(action="enable")
    enabled = service.apply_management_control(
        action="enable",
        expected_hash=enable_preview["precondition_hash"],
    )
    assert isinstance(enabled, dict) and enabled["enabled"] is True
    restored = service._configured_authority()
    matching = service.store.read_matching_activation_receipt(
        authority_id=authority_revision(restored),
        config_sha=config_revision(restored),
        adapter_capability_sha=service._adapter_contract(service.adapter)[1],
    )

    assert matching == original
    assert service.store.connection.execute(
        "SELECT COUNT(*) FROM activation_receipts"
    ).fetchone()[0] == receipts_after_disable
    plan = resolver.resolve(
        _request(
            session_id="stage5-disable-enable-return",
            task_id="stage5-disable-enable-return-task",
        )
    )
    assert plan.action == "project"


def test_active_daily_cap_failure_restores_config_and_rollover_records(
    stage5_active_service,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, _config, _inventory = stage5_active_service
    exact_before = service.config_path.read_bytes()
    counts_before = {
        table: service.store.connection.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]
        for table in (
            "activation_receipts",
            "authority_revisions",
            "adaptive_revisions",
            "active_adaptive_revisions",
        )
    }
    preview = service.preview_management_control(
        action="daily-cap",
        daily_limit=4,
    )
    monkeypatch.setattr(
        service,
        "_transition_global_management_control",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )

    with pytest.raises(Exception, match="management control apply failed"):
        service.apply_management_control(
            action="daily-cap",
            daily_limit=4,
            expected_hash=preview["precondition_hash"],
        )

    assert service.config_path.read_bytes() == exact_before
    assert {
        table: service.store.connection.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]
        for table in counts_before
    } == counts_before


def test_active_daily_cap_receipt_failure_removes_partial_rollover_publication(
    stage5_active_service,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _resolver, _config, _inventory = stage5_active_service
    exact_before = service.config_path.read_bytes()
    counts_before = {
        table: service.store.connection.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]
        for table in (
            "activation_receipts",
            "authority_revisions",
            "adaptive_revisions",
            "active_adaptive_revisions",
        )
    }
    preview = service.preview_management_control(
        action="daily-cap",
        daily_limit=4,
    )
    real_write = service.store.write_activation_receipt

    def write_then_fail(receipt) -> None:
        real_write(receipt)
        raise RuntimeError("receipt unavailable")

    monkeypatch.setattr(service.store, "write_activation_receipt", write_then_fail)
    monkeypatch.setattr(
        service.store,
        "rollback_activation_receipt",
        lambda _receipt: (_ for _ in ()).throw(
            RuntimeError("receipt compensation unavailable")
        ),
    )
    monkeypatch.setattr(
        service.store,
        "rollback_authority_and_baseline",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("authority compensation unavailable")
        ),
    )

    with pytest.raises(Exception, match="management control apply failed"):
        service.apply_management_control(
            action="daily-cap",
            daily_limit=4,
            expected_hash=preview["precondition_hash"],
        )

    assert service.config_path.read_bytes() == exact_before
    assert {
        table: service.store.connection.execute(
            f'SELECT COUNT(*) FROM "{table}"'
        ).fetchone()[0]
        for table in counts_before
    } == counts_before
