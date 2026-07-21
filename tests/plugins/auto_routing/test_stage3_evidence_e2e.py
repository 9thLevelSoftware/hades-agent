"""Exact routed-turn attribution and exclusion contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from _stage3_test_support import _request


def test_active_turn_is_attributed_to_current_epoch_runtime(active_route):
    turn_payload = active_route.payload()
    binding = active_route.service.store.read_session_binding(
        turn_payload["session_id"]
    )
    epoch = active_route.service.store.read_route_epochs(binding.session_id)[
        binding.current_epoch
    ]

    committed = active_route.service.ingest_turn_outcome(turn_payload)

    assert committed is not None
    assert committed.status == "inserted"
    assert committed.event.decision_id == binding.decision_id
    assert committed.event.route_epoch_id == epoch.route_epoch_id
    assert committed.event.runtime_id == binding.runtime_id
    assert committed.event.reasoning_effort == "high"
    assert committed.event.attribution_confidence == 1.0
    assert committed.event.is_initial_routing_task is True
    assert committed.event.context_bucket is not None


def test_exact_replay_is_deduplicated(active_route):
    payload = active_route.payload()

    first = active_route.service.ingest_turn_outcome(payload)
    second = active_route.service.ingest_turn_outcome(payload)

    assert first.status == "inserted"
    assert second.status == "replayed"
    assert first.event == second.event
    assert active_route.service.store.count_evidence_events() == 1


def test_fresh_service_ingests_using_only_durable_route_state(active_route):
    restarted = active_route.fresh_service()
    restarted.adapter.inventory = lambda *_args, **_kwargs: pytest.fail(  # type: ignore[method-assign]
        "evidence ingestion must not refresh inventory"
    )
    restarted.adapter.resolve = lambda *_args, **_kwargs: pytest.fail(  # type: ignore[method-assign]
        "evidence ingestion must not resolve a runtime"
    )
    restarted._configured_authority = lambda: pytest.fail(  # type: ignore[method-assign]
        "evidence ingestion must not parse current YAML authority"
    )

    committed = restarted.ingest_turn_outcome(active_route.payload())

    assert committed is not None
    assert committed.status == "inserted"
    assert committed.event.decision_id == active_route.decision.decision_id


def test_recorded_fallback_credits_fallback_epoch_not_original_primary(
    fallback_route,
):
    fallback_turn_payload = fallback_route.payload()
    binding = fallback_route.service.store.read_session_binding(
        fallback_route.session_id
    )
    decision = fallback_route.service.store.read_decision(binding.decision_id)

    committed = fallback_route.service.ingest_turn_outcome(fallback_turn_payload)

    assert committed.event.runtime_id == binding.runtime_id
    assert committed.event.runtime_id != decision.selected_runtime.stable_id()
    assert committed.event.route_epoch_id == fallback_route.epoch.route_epoch_id


def test_compression_descendant_uses_original_decision_and_child_epoch(
    compressed_route,
):
    compressed_turn_payload = compressed_route.payload()
    committed = compressed_route.service.ingest_turn_outcome(compressed_turn_payload)
    child_binding = compressed_route.service.store.read_session_binding(
        compressed_route.session_id
    )

    assert committed.event.decision_id == child_binding.decision_id
    assert committed.event.session_id == child_binding.session_id
    assert committed.event.route_epoch_id == compressed_route.epoch.route_epoch_id
    assert committed.event.is_initial_routing_task is False
    assert committed.event.context_bucket is None


def test_compression_descendant_reusing_original_task_id_is_not_initial(
    active_route,
):
    child = active_route.compression_child(
        child_session_id="compressed-same-task-child",
        child_task_id=active_route.decision.task_id,
    )

    committed = child.service.ingest_turn_outcome(child.payload())

    assert committed.event.session_id != active_route.decision.session_id
    assert committed.event.task_id == active_route.decision.task_id
    assert committed.event.is_initial_routing_task is False
    assert committed.event.context_bucket is None


@pytest.mark.parametrize("action", ["shadow", "inherit"])
def test_non_projecting_binding_receives_no_target_credit(active_route, action):
    turn_payload = active_route.payload()
    turn_payload["runtime_binding"]["action"] = action

    assert active_route.service.ingest_turn_outcome(turn_payload) is None
    assert active_route.service.store.count_evidence_events() == 0


def test_manual_or_reasoning_mismatch_is_unattributed(active_route):
    turn_payload = active_route.payload()
    turn_payload["reasoning_effort"] = "low"

    assert active_route.service.ingest_turn_outcome(turn_payload) is None
    assert active_route.service.store.count_evidence_events() == 0


def test_zero_api_calls_is_unattributed(active_route):
    assert active_route.service.ingest_turn_outcome(
        active_route.payload(api_calls=0)
    ) is None


def test_wrong_public_decision_id_is_unattributed(active_route):
    payload = active_route.payload()
    payload["runtime_binding"]["decision_id"] = "wrong-decision"

    assert active_route.service.ingest_turn_outcome(payload) is None


def test_public_scope_disagreement_is_unattributed(active_route):
    payload = active_route.payload()
    payload["runtime_binding"]["scope"] = "delegation"

    assert active_route.service.ingest_turn_outcome(payload) is None


def test_observer_and_public_binding_task_mismatch_is_unattributed(active_route):
    payload = active_route.payload(task_id="other-task")

    assert active_route.service.ingest_turn_outcome(payload) is None


def test_forged_origin_task_id_is_unattributed(active_route):
    payload = active_route.payload()
    payload["task_id"] = "forged-origin-task"
    payload["runtime_binding"]["task_id"] = "forged-origin-task"

    assert active_route.service.ingest_turn_outcome(payload) is None
    assert active_route.service.store.count_evidence_events() == 0


def test_store_rejects_forged_origin_task_id_transactionally(active_route):
    from plugins.auto_routing.auto_routing.evidence import turn_evidence_id
    from plugins.auto_routing.auto_routing.storage import ImmutableRecordConflict

    committed = active_route.service.ingest_turn_outcome(active_route.payload())
    forged = committed.event.model_copy(
        update={
            "evidence_id": turn_evidence_id(active_route.session_id, "e" * 64),
            "turn_id": "e" * 64,
            "task_id": "forged-origin-task",
            "context_bucket": None,
            "is_initial_routing_task": False,
        }
    )

    with pytest.raises(ImmutableRecordConflict, match="origin task"):
        active_route.service.store.write_evidence_event(forged)


def test_missing_session_binding_is_unattributed(active_route, monkeypatch):
    monkeypatch.setattr(
        active_route.service.store,
        "read_session_binding",
        lambda _session_id: None,
    )

    assert active_route.service.ingest_turn_outcome(active_route.payload()) is None


def test_missing_decision_is_unattributed(active_route, monkeypatch):
    monkeypatch.setattr(
        active_route.service.store,
        "read_decision",
        lambda _decision_id: None,
    )

    assert active_route.service.ingest_turn_outcome(active_route.payload()) is None


def test_wrong_session_binding_is_unattributed(active_route, monkeypatch):
    wrong = replace(active_route.binding, session_id="other-session")
    monkeypatch.setattr(
        active_route.service.store,
        "read_session_binding",
        lambda _session_id: wrong,
    )

    assert active_route.service.ingest_turn_outcome(active_route.payload()) is None


def test_missing_current_epoch_is_unattributed(active_route, monkeypatch):
    monkeypatch.setattr(
        active_route.service.store,
        "read_route_epochs",
        lambda _session_id: (),
    )

    assert active_route.service.ingest_turn_outcome(active_route.payload()) is None


def test_current_epoch_mismatch_is_unattributed(active_route, monkeypatch):
    wrong = replace(active_route.epoch, epoch_number=active_route.epoch.epoch_number + 1)
    monkeypatch.setattr(
        active_route.service.store,
        "read_route_epochs",
        lambda _session_id: (wrong,),
    )

    assert active_route.service.ingest_turn_outcome(active_route.payload()) is None


def test_provider_not_started_is_unattributed_despite_api_calls(
    active_route,
    monkeypatch,
):
    unstarted = replace(
        active_route.epoch,
        provider_started=False,
        api_request_id=None,
        provider_started_at=None,
    )
    monkeypatch.setattr(
        active_route.service.store,
        "read_route_epochs",
        lambda _session_id: (unstarted,),
    )

    assert active_route.service.ingest_turn_outcome(active_route.payload()) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("provider", "other-provider"), ("model", "other-model")],
)
def test_public_target_disagreement_is_unattributed(active_route, field, value):
    kwargs = {field: value}

    assert active_route.service.ingest_turn_outcome(
        active_route.payload(**kwargs)
    ) is None


def test_epoch_runtime_outside_recorded_chain_is_unattributed(
    active_route,
    monkeypatch,
):
    unknown_runtime_id = "c" * 64
    binding = replace(active_route.binding, runtime_id=unknown_runtime_id)
    epoch = replace(active_route.epoch, runtime_id=unknown_runtime_id)
    monkeypatch.setattr(
        active_route.service.store,
        "read_session_binding",
        lambda _session_id: binding,
    )
    monkeypatch.setattr(
        active_route.service.store,
        "read_route_epochs",
        lambda _session_id: (epoch,),
    )

    assert active_route.service.ingest_turn_outcome(active_route.payload()) is None


def test_historical_authority_identity_mismatch_is_unattributed(
    active_route,
    monkeypatch,
):
    authority = active_route.service.store.read_authority_revision(
        active_route.decision.authority_revision
    )
    assert authority is not None
    changed = replace(
        authority,
        document_json=authority.document_json.replace(
            '"trivial_max":0.2',
            '"trivial_max":0.19',
        ),
    )
    assert changed.document_json != authority.document_json
    monkeypatch.setattr(
        active_route.service.store,
        "read_authority_revision",
        lambda _authority_id: changed,
    )

    assert active_route.service.ingest_turn_outcome(active_route.payload()) is None


def test_bound_route_identity_is_rejected_by_sealed_observer(active_route):
    payload = active_route.payload()
    payload["runtime_binding"]["bound_route_identity"] = "mixed-purpose-id"

    with pytest.raises(ValidationError):
        active_route.service.ingest_turn_outcome(payload)
    assert active_route.service.store.count_evidence_events() == 0


def test_resolver_swallows_bound_route_identity_validation(active_route):
    payload = active_route.payload()
    payload["runtime_binding"]["bound_route_identity"] = "mixed-purpose-id"

    assert active_route.resolver.on_post_turn_outcome(**payload) is None
    assert active_route.service.store.count_evidence_events() == 0


def test_store_exception_is_fail_open_at_plugin_boundary(active_route, monkeypatch):
    monkeypatch.setattr(
        active_route.service,
        "ingest_turn_outcome",
        lambda _payload: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )

    assert active_route.resolver.on_post_turn_outcome(**active_route.payload()) is None


def test_locked_store_is_fail_open_at_plugin_boundary(active_route):
    with active_route.lock_store():
        assert (
            active_route.resolver.on_post_turn_outcome(**active_route.payload()) is None
        )
    assert active_route.service.store.count_evidence_events() == 0


def test_profiles_keep_same_deterministic_evidence_id_isolated(
    stage3_profile_factory,
):
    common = {
        "session_id": "same-session",
        "task_id": "same-task",
        "turn_id": "d" * 64,
    }
    first = stage3_profile_factory(profile_name="first", **common)
    second = stage3_profile_factory(profile_name="second", **common)

    with first.activate_profile():
        first.resolver.on_post_turn_outcome(**first.payload())
    with second.activate_profile():
        second.resolver.on_post_turn_outcome(**second.payload())

    first_event = first.service.store.read_evidence_event(
        first.service.store.connection.execute(
            "SELECT evidence_id FROM evidence_events"
        ).fetchone()[0]
    )
    second_event = second.service.store.read_evidence_event(
        second.service.store.connection.execute(
            "SELECT evidence_id FROM evidence_events"
        ).fetchone()[0]
    )
    assert first_event.evidence_id == second_event.evidence_id
    assert first.service.store.count_evidence_events() == 1
    assert second.service.store.count_evidence_events() == 1
    assert set(first.auto_routing_artifacts()).isdisjoint(second.auto_routing_artifacts())


def test_real_agent_turn_traverses_hook_and_persists_evidence(active_route):
    result = active_route.run_real_turn(prompt="finish the stage3 loopback turn")

    assert result["final_response"].strip() == "ok"
    assert result["decision_id"] == active_route.decision.decision_id
    assert result["evidence"] is not None
    assert result["evidence"].runtime_id == active_route.runtime.stable_id()
    assert active_route.service.store.count_evidence_events() == 1


def test_evidence_collection_preserves_exact_recorded_route_replay(active_route):
    with active_route.activate_profile():
        before = active_route.resolver.resolve(
            _request(
                session_id=active_route.session_id,
                task_id=active_route.task_id,
                is_resume=True,
            )
        )
    event = active_route.service.ingest_turn_outcome(active_route.payload()).event
    active_route.service.record_feedback(
        evidence_id=event.evidence_id,
        value="manual-reroute",
    )
    active_route.service.report(days=30)
    with active_route.activate_profile():
        after = active_route.resolver.resolve(
            _request(
                session_id=active_route.session_id,
                task_id=active_route.task_id,
                is_resume=True,
            )
        )

    assert after.action == before.action == "project"
    assert after.decision_id == before.decision_id == active_route.decision.decision_id
    assert after.runtime.model == before.runtime.model == active_route.runtime.model
    assert after.runtime.provider == before.runtime.provider == active_route.runtime.provider
    assert after.runtime.reasoning_config == before.runtime.reasoning_config == {
        "enabled": True,
        "effort": active_route.reasoning_effort,
    }
