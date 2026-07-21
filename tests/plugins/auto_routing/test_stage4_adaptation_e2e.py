"""Stage 4 adaptation materialization and replay-boundary integration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.auto_routing.auto_routing import service as service_module
from plugins.auto_routing.auto_routing.adaptation import operation_identity_hash
from plugins.auto_routing.auto_routing.config import (
    authority_document,
    authority_revision,
)
from plugins.auto_routing.auto_routing.models import (
    AdaptiveCanaryAssignment,
    AdaptiveExplanation,
    AdaptiveLifecycleEvent,
    DecisionCandidate,
    candidate_id_for,
)
from plugins.auto_routing.auto_routing.selector import SelectionResult
from plugins.auto_routing.auto_routing.service import AutoRoutingService
from plugins.auto_routing.auto_routing.storage import (
    ImmutableRecordConflict,
    RoutingStore,
)
from tests.plugins.auto_routing.test_adaptation_lifecycle import (
    _adaptive_config,
    _revision,
)
from tests.plugins.auto_routing.test_storage import _candidate, _decision


def test_profile_attestation_with_fake_references_is_rejected_before_dispatch(
    tmp_path: Path,
) -> None:
    decision = _decision().model_copy(
        update={
            "profile_adaptive_revision_id": "profile-revision-a",
            "adaptive_assignment_id": "assignment-a",
            "adaptive_profile_snapshot": {"coding": "profile-revision-a"},
        }
    )
    with RoutingStore.open(path=tmp_path / "state.db") as store:
        with pytest.raises(ImmutableRecordConflict, match="authority"):
            store.commit_decision(
                decision,
                candidates=(_candidate(),),
                create_epoch=True,
            )

        assert store.read_decision(decision.decision_id) is None
        assert store.read_session_binding(decision.session_id) is None
        assert store.read_route_epochs(decision.session_id) == ()


def test_canary_assignment_is_provisional_until_final_profile_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _adaptive_config()
    authority_id = authority_revision(config)
    profile = config.profiles["coding"]
    control = _revision("coding", "revision-control").model_copy(
        update={
            "authority_id": authority_id,
            "overlay": AutoRoutingService._profile_overlay(profile),
        }
    )
    ordered = tuple(reversed(control.overlay.ordered_primary_runtime_ids))
    challenger = control.model_copy(
        update={
            "revision_id": "revision-challenger",
            "parent_revision_id": control.revision_id,
            "overlay": control.overlay.model_copy(
                update={"ordered_primary_runtime_ids": ordered}
            ),
            "explanation": AdaptiveExplanation(
                context_bucket_id="b" * 64,
                control_revision_id=control.revision_id,
            ),
        }
    )
    with RoutingStore.open(path=tmp_path / "state.db") as store:
        first = store.publish_profile_revision(
            control, expected_revision_id=None, expected_generation=0
        )
        second = store.insert_inactive_profile_revision(
            challenger,
            expected_active_revision_id=control.revision_id,
            expected_generation=first,
        )
        validated = store.transition_profile_experiment(
            authority_id,
            "coding",
            active_revision_id=control.revision_id,
            control_revision_id=control.revision_id,
            challenger_revision_id=challenger.revision_id,
            experiment_phase="validated",
            cooldown_until=None,
            rejection_count=0,
            expected_generation=second,
            event=AdaptiveLifecycleEvent(
                event_id="event-validated",
                authority_id=authority_id,
                profile_id="coding",
                revision_id=challenger.revision_id,
                event_type="validated",
                reason_code="test",
                created_at="2026-07-18T12:00:01Z",
            ),
        )
        store.transition_profile_experiment(
            authority_id,
            "coding",
            active_revision_id=control.revision_id,
            control_revision_id=control.revision_id,
            challenger_revision_id=challenger.revision_id,
            experiment_phase="canary",
            cooldown_until=None,
            rejection_count=0,
            expected_generation=validated.generation,
            event=AdaptiveLifecycleEvent(
                event_id="event-canary",
                authority_id=authority_id,
                profile_id="coding",
                revision_id=challenger.revision_id,
                event_type="canary",
                reason_code="test",
                created_at="2026-07-18T12:00:02Z",
            ),
        )
        service = AutoRoutingService.__new__(AutoRoutingService)
        service.store = store
        service.hermes_home = tmp_path
        service._pinned_config_path = tmp_path / "config.yaml"
        monkeypatch.setattr(
            service_module,
            "ensure_profile_canary_key",
            lambda *_args, **_kwargs: b"x" * 32,
        )
        request = SimpleNamespace(
            context=SimpleNamespace(
                scope="fresh_session",
                session_id="session-a",
                task_id="task-a",
                operation_id=None,
                task_index=None,
                is_resume=False,
                manual_runtime_pin=False,
                metadata={},
            )
        )
        assessment = SimpleNamespace(risk_class="moderate")
        inventory = SimpleNamespace(
            runtimes=tuple(
                SimpleNamespace(key=target.runtime, state="verified")
                for target in profile.primary_choices()
            )
        )
        snapshot = {"coding": control.revision_id}

        provisional = service._provisional_canary_assignment(
            config=config,
            request=request,
            assessment=assessment,
            inventory=inventory,
            selected_profile_id="coding",
            context_bucket_id="b" * 64,
        )

        assert provisional is not None
        assert store.count_canary_assignments() == 0
        final_snapshot = dict(snapshot)
        final_snapshot["coding"] = (
            provisional.challenger_revision_id
            if provisional.arm == "challenger"
            else provisional.control_revision_id
        )
        selected_revision = final_snapshot["coding"]
        persisted = service._persist_matching_canary_assignment(
            provisional,
            selected_profile_id="coding",
            selected_revision_id=selected_revision,
            selected_runtime_id=(
                challenger.overlay.ordered_primary_runtime_ids[0]
                if provisional.arm == "challenger"
                else control.overlay.ordered_primary_runtime_ids[0]
            ),
            selected_reasoning_effort=(
                challenger.overlay.reasoning_defaults[
                    challenger.overlay.ordered_primary_runtime_ids[0]
                ]
                if provisional.arm == "challenger"
                else control.overlay.reasoning_defaults[
                    control.overlay.ordered_primary_runtime_ids[0]
                ]
            ),
        )
        assert persisted is not None
        assert store.count_canary_assignments() == 1
        chosen = challenger if provisional.arm == "challenger" else control
        chosen_runtime = chosen.overlay.ordered_primary_runtime_ids[0]
        snapshot = {
            profile_id: AutoRoutingService.static_profile_revision_id(
                authority_id,
                profile_id,
            )
            for profile_id in config.profiles
        }
        snapshot["coding"] = chosen.revision_id
        selected_runtime = next(
            target.runtime
            for target in profile.primary_choices()
            if target.runtime.stable_id() == chosen_runtime
        )
        decision = _decision().model_copy(
            update={
                "authority_revision": authority_id,
                "profile_adaptive_revision_id": chosen.revision_id,
                "adaptive_assignment_id": persisted.assignment_id,
                "adaptive_profile_snapshot": snapshot,
                "selected_runtime": selected_runtime,
                "selected_reasoning_effort": chosen.overlay.reasoning_defaults[
                    chosen_runtime
                ],
                "eligible_candidates": (chosen_runtime,),
                "final_scores": ((chosen_runtime, 0.8),),
            }
        )
        store.write_authority_revision(authority_id, authority_document(config))
        committed = store.commit_decision(
            decision,
            candidates=(_candidate(runtime_id=chosen_runtime),),
            create_epoch=False,
        )
        assert committed.decision.adaptive_assignment_id == persisted.assignment_id

        assert service._provisional_canary_assignment(
            config=config,
            request=SimpleNamespace(
                context=SimpleNamespace(
                    **{
                        **request.context.__dict__,
                        "session_id": "session-context-mismatch",
                        "task_id": "task-context-mismatch",
                    }
                )
            ),
            assessment=assessment,
            inventory=inventory,
            selected_profile_id="coding",
            context_bucket_id="c" * 64,
        ) is None
        assert store.count_canary_assignments() == 1

        displaced = provisional.model_copy(
            update={
                "operation_identity_hash": operation_identity_hash(
                    scope="fresh_session",
                    session_id="session-b",
                    task_id="task-b",
                    operation_id=None,
                    task_index=None,
                ),
                "assignment_id": "assignment-displaced",
            }
        )
        assert service._persist_matching_canary_assignment(
            displaced,
            selected_profile_id="research",
            selected_revision_id=selected_revision,
            selected_runtime_id=control.overlay.ordered_primary_runtime_ids[0],
            selected_reasoning_effort="medium",
        ) is None
        assert store.count_canary_assignments() == 1


def test_pre_call_runtime_fallback_cannot_persist_a_displaced_canary_arm() -> None:
    config = _adaptive_config()
    profile = config.profiles["coding"]
    primary = profile.primary
    fallback = profile.primary_challengers[0].model_copy(
        update={"revision_status": "fallback"}
    )
    primary_runtime = SimpleNamespace(key=primary.runtime)
    fallback_runtime = SimpleNamespace(key=fallback.runtime)
    primary_id = primary.runtime.stable_id()
    fallback_id = fallback.runtime.stable_id()

    candidates = (
        DecisionCandidate(
            candidate_id=candidate_id_for("coding", "primary", 0, primary_id),
            profile_id="coding",
            target_role="primary",
            target_ordinal=0,
            runtime_id=primary_id,
            eligible=True,
            reason_codes=(),
            normalized_scoring_inputs=(("quality", 1.0),),
            final_score=1.0,
        ),
        DecisionCandidate(
            candidate_id=candidate_id_for("coding", "fallback", 0, fallback_id),
            profile_id="coding",
            target_role="fallback",
            target_ordinal=0,
            runtime_id=fallback_id,
            eligible=True,
            reason_codes=(),
            normalized_scoring_inputs=(("quality", 0.5),),
            final_score=0.5,
        ),
    )
    selection = SelectionResult(
        assessment=None,
        candidates=candidates,
        eligible_runtime_ids=(primary_id, fallback_id),
        rejections={},
        score_calls=(),
        selected_profile_id="coding",
        selected_runtime=primary_runtime,
        selected_reasoning_effort=primary.reasoning.default,
        fallbacks=(fallback,),
        safe_default_runtime=fallback_runtime,
        safe_default_reasoning_effort=fallback.reasoning.default,
        selection_reason="highest_eligible_score",
    )
    revision = SimpleNamespace(
        overlay=SimpleNamespace(
            ordered_primary_runtime_ids=(primary_id,),
            reasoning_defaults={primary_id: primary.reasoning.default},
        )
    )
    writes: list[object] = []
    service = AutoRoutingService.__new__(AutoRoutingService)
    service.store = SimpleNamespace(
        read_profile_revision=lambda _revision_id: revision,
        get_or_create_canary_assignment=lambda assignment: writes.append(assignment),
    )

    class _Adapter:
        @staticmethod
        def resolve(key):
            if key.stable_id() == primary_id:
                raise RuntimeError("primary cannot resolve")
            return SimpleNamespace(runtime_key=key)

        @staticmethod
        def to_agent_runtime_spec(resolved, *, reasoning_effort, hermes_config):
            return SimpleNamespace(
                model=resolved.runtime_key.model,
                reasoning_effort=reasoning_effort,
                hermes_config=hermes_config,
            )

    service.adapter = _Adapter()
    resolved, spec = service._runtime_resolve_pre_call(
        selection=selection,
        inventory=SimpleNamespace(runtimes=(primary_runtime, fallback_runtime)),
        request=SimpleNamespace(),
        hermes_config={},
    )
    assignment = AdaptiveCanaryAssignment(
        assignment_id="assignment-control",
        authority_id="a" * 64,
        profile_id="coding",
        operation_identity_hash="b" * 64,
        context_bucket_id="c" * 64,
        control_revision_id="control-revision",
        challenger_revision_id="challenger-revision",
        arm="control",
        created_at="2026-07-18T12:00:00Z",
    )

    assert spec is not None
    assert resolved.selection_reason == "pre_call_fallback"
    assert resolved.selected_runtime.key.stable_id() == fallback_id
    assert service._persist_matching_canary_assignment(
        assignment,
        selected_profile_id=resolved.selected_profile_id,
        selected_revision_id="control-revision",
        selected_runtime_id=resolved.selected_runtime.key.stable_id(),
        selected_reasoning_effort=resolved.selected_reasoning_effort,
    ) is None
    assert writes == []


def test_frozen_persistence_race_restores_control_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_config = _adaptive_config()
    config = original_config.model_copy(
        update={
            "activation": original_config.activation.model_copy(
                update={"mode": "active"}
            )
        }
    )
    control_snapshot = {
        profile_id: f"control-{profile_id}"
        for profile_id in config.profiles
    }
    challenger_snapshot = dict(control_snapshot)
    challenger_snapshot["coding"] = "challenger-coding"
    control_runtime = SimpleNamespace(
        key=SimpleNamespace(stable_id=lambda: "control-runtime")
    )
    challenger_runtime = SimpleNamespace(
        key=SimpleNamespace(stable_id=lambda: "challenger-runtime")
    )
    control_selection = SimpleNamespace(
        selected_profile_id="coding",
        selected_runtime=control_runtime,
        selected_reasoning_effort="medium",
        selection_reason="highest_eligible_score",
    )
    challenger_selection = SimpleNamespace(
        selected_profile_id="coding",
        selected_runtime=challenger_runtime,
        selected_reasoning_effort="medium",
        selection_reason="highest_eligible_score",
    )
    receipt = SimpleNamespace(receipt_id="receipt-a", config_sha="c" * 64)
    assignment = AdaptiveCanaryAssignment(
        assignment_id="assignment-challenger",
        authority_id="a" * 64,
        profile_id="coding",
        operation_identity_hash="b" * 64,
        context_bucket_id="c" * 64,
        control_revision_id=control_snapshot["coding"],
        challenger_revision_id=challenger_snapshot["coding"],
        arm="challenger",
        created_at="2026-07-18T12:00:00Z",
    )
    resolver_calls: list[str] = []
    persisted: list[AdaptiveCanaryAssignment] = []
    committed: list[object] = []

    class _Store:
        @staticmethod
        def read_matching_activation_receipt(**_kwargs):
            return receipt

        @staticmethod
        def claim_decision_operation(**_kwargs):
            return SimpleNamespace(status="claimed")

        @staticmethod
        def write_authority_revision(*_args, **_kwargs):
            return None

        @staticmethod
        def read_active_revision(_authority_id):
            return None

        @staticmethod
        def commit_decision(decision, **_kwargs):
            committed.append(decision)
            return SimpleNamespace(
                decision=decision,
                binding=SimpleNamespace(runtime_id="control-runtime"),
            )

    service = AutoRoutingService.__new__(AutoRoutingService)
    service.store = _Store()
    service._authority_is_usable = lambda *_args: True
    service._runtime_fact_metadata = lambda _metadata: {}
    service._runtime_timestamp = lambda: "2026-07-18T12:00:00Z"
    service._new_inventory_service = lambda **_kwargs: SimpleNamespace(
        refresh=lambda **_refresh_kwargs: SimpleNamespace(
            revision="inventory-a",
            runtimes=(),
        )
    )
    service._runtime_rule_evaluation = lambda **_kwargs: SimpleNamespace(
        assessment=SimpleNamespace(risk_class="moderate"),
        safe_default_reason=None,
        profile_id="coding",
        preferred_profile_ids=(),
        applied_rule_ids=(),
        classifier_runtime_id=None,
        classifier_input_tokens=0,
        classifier_output_tokens=0,
        classifier_cost_usd=None,
    )
    service._runtime_safe_default_target = lambda *_args, **_kwargs: object()
    service.maybe_advance_adaptation = lambda **_kwargs: None
    service.resolve_effective_adaptation_snapshot = lambda **_kwargs: dict(
        control_snapshot
    )
    service._runtime_root_config = lambda: {}
    service._profiles_for_adaptation_snapshot = (
        lambda _config, snapshot, _inventory: dict(snapshot)
    )
    service._provisional_canary_assignment = lambda **_kwargs: assignment
    service._canary_assignment_matches_final_route = lambda *_args, **_kwargs: True

    def frozen_persistence(*_args, **_kwargs):
        persisted.append(assignment)
        return None

    service._persist_matching_canary_assignment = frozen_persistence

    def resolve(selection, **_kwargs):
        resolver_calls.append(selection.selected_runtime.key.stable_id())
        return selection, SimpleNamespace(model=selection.selected_runtime.key.stable_id())

    service._runtime_resolve_pre_call = resolve

    class _Selector:
        def __init__(self, **_kwargs):
            pass

        @staticmethod
        def select(*, profiles, **_kwargs):
            return (
                challenger_selection
                if profiles["coding"] == challenger_snapshot["coding"]
                else control_selection
            )

    class _Catalog:
        snapshot = SimpleNamespace(snapshot_id="catalog-a")

        def __init__(self, **_kwargs):
            pass

    class _Builder:
        @staticmethod
        def build(**kwargs):
            decision = SimpleNamespace(
                decision_id="decision-a",
                profile_adaptive_revision_id=kwargs[
                    "profile_adaptive_revision_id"
                ],
                adaptive_assignment_id=kwargs["adaptive_assignment_id"],
            )
            return SimpleNamespace(
                decision=decision,
                candidates=(),
                semantic_checksum="semantic-a",
            )

    monkeypatch.setattr(service_module, "extract_task_facts", lambda **_kwargs: {})
    monkeypatch.setattr(service_module, "task_facts_hash", lambda _facts: "d" * 64)
    monkeypatch.setattr(
        service_module,
        "build_context_bucket",
        lambda *_args, **_kwargs: SimpleNamespace(bucket_id="e" * 64),
    )
    monkeypatch.setattr(service_module, "StaticSelector", _Selector)
    monkeypatch.setattr(service_module, "CatalogService", _Catalog)
    monkeypatch.setattr(service_module, "DecisionBuilder", _Builder)

    request = SimpleNamespace(
        context=SimpleNamespace(
            scope="fresh_session",
            session_id="session-a",
            task_id="task-a",
            operation_id=None,
            task_index=None,
            task="ephemeral",
            metadata={},
        ),
        baseline=SimpleNamespace(reasoning_config=None),
    )
    plan = service.create_runtime_decision(
        request=request,
        config=config,
        activation_receipt=receipt,
        adapter_capability_sha="f" * 64,
    )

    assert persisted == [assignment]
    assert resolver_calls == ["challenger-runtime", "control-runtime"]
    assert plan.action == "project"
    assert plan.runtime.model == "control-runtime"
    assert plan.event["adaptive_assignment_id"] is None
    assert plan.event["profile_adaptive_revision_id"] == control_snapshot["coding"]
    assert committed[0].adaptive_assignment_id is None
    assert committed[0].profile_adaptive_revision_id == control_snapshot["coding"]
