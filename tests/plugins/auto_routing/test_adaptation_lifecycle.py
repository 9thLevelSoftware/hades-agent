"""Stage 4 lifecycle and immutable decision-boundary contracts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.auto_routing.auto_routing import service as service_module
from plugins.auto_routing.auto_routing import storage as storage_module
from plugins.auto_routing.auto_routing.config import (
    authority_document,
    authority_revision,
    parse_config,
)
from plugins.auto_routing.auto_routing.learner import summarize_quality
from plugins.auto_routing.auto_routing.models import (
    AdaptiveExplanation,
    AdaptiveOverlay,
    AdaptiveProfileRevision,
)
from plugins.auto_routing.auto_routing.service import AutoRoutingService
from plugins.auto_routing.auto_routing.storage import RoutingStore
from tests.plugins.auto_routing.test_storage import _candidate, _decision
from tests.plugins.auto_routing.test_adaptation_storage import (
    _publish_pair,
    assignment,
    lifecycle_event,
)


AUTHORITY_ID = "a" * 64


def _adaptive_config(*, enabled: bool = True):
    document = json.loads(
        (Path(__file__).with_name("fixtures") / "approved_proposal.json").read_text(
            encoding="utf-8"
        )
    )
    profile = document["profiles"]["coding"]
    challenger = json.loads(json.dumps(profile["primary"]))
    challenger["runtime"].update(
        {
            "model": "approved-challenger",
            "auth_identity": "auth:challenger",
        }
    )
    challenger["revision_status"] = "challenger"
    profile["primary_challengers"] = [challenger]
    profile["adaptation"] = {"enabled": enabled}
    document["llm"]["allowed_models"].append("approved-challenger")
    return parse_config({"plugins": {"entries": {"auto-routing": document}}})


def _revision(profile_id: str, revision_id: str) -> AdaptiveProfileRevision:
    return AdaptiveProfileRevision(
        revision_id=revision_id,
        authority_id=AUTHORITY_ID,
        profile_id=profile_id,
        overlay=AdaptiveOverlay(
            profile_id=profile_id,
            ordered_primary_runtime_ids=("b" * 64, "c" * 64),
        ),
        lifecycle="validated",
        created_at="2026-07-18T12:00:00Z",
    )


def test_decision_rejects_dangling_profile_adaptation_attestation(
    tmp_path: Path,
) -> None:
    snapshot = {"coding": "revision-coding", "research": "static-research"}
    decision = _decision().model_copy(
        update={
            "profile_adaptive_revision_id": "revision-coding",
            "adaptive_assignment_id": "assignment-a",
            "adaptive_profile_snapshot": snapshot,
        }
    )
    with RoutingStore.open(path=tmp_path / "state.db") as store:
        with pytest.raises(storage_module.ImmutableRecordConflict, match="authority"):
            store.commit_decision(
                decision,
                candidates=(_candidate(),),
                create_epoch=False,
            )

        assert store.read_decision(decision.decision_id) is None
        assert store.read_session_binding(decision.session_id) is None


def test_v6_decision_document_without_profile_attestation_still_validates() -> None:
    legacy = _decision().model_dump(mode="json")

    restored = type(_decision()).model_validate(legacy)

    assert restored.profile_adaptive_revision_id is None
    assert restored.adaptive_assignment_id is None
    assert dict(restored.adaptive_profile_snapshot) == {}


def test_fresh_decision_rejects_a_dangling_adaptive_revision_before_binding(
    tmp_path: Path,
) -> None:
    config = _adaptive_config()
    authority_id = authority_revision(config)
    snapshot = {
        profile_id: AutoRoutingService.static_profile_revision_id(
            authority_id,
            profile_id,
        )
        for profile_id in config.profiles
    }
    snapshot["coding"] = "missing-profile-revision"
    decision = _decision().model_copy(
        update={
            "authority_revision": authority_id,
            "profile_adaptive_revision_id": "missing-profile-revision",
            "adaptive_profile_snapshot": snapshot,
        }
    )
    with RoutingStore.open(path=tmp_path / "state.db") as store:
        store.write_authority_revision(authority_id, authority_document(config))
        with pytest.raises(storage_module.ImmutableRecordConflict, match="does not exist"):
            store.commit_decision(
                decision,
                candidates=(_candidate(),),
                create_epoch=True,
            )

        assert store.read_decision(decision.decision_id) is None
        assert store.read_session_binding(decision.session_id) is None
        assert store.read_route_epochs(decision.session_id) == ()


def test_effective_snapshot_uses_only_complete_authority_profile_revisions(
    tmp_path: Path,
) -> None:
    with RoutingStore.open(path=tmp_path / "state.db") as store:
        store.publish_profile_revision(
            _revision("coding", "revision-coding"),
            expected_revision_id=None,
            expected_generation=0,
        )
        service = AutoRoutingService.__new__(AutoRoutingService)
        service.store = store

        snapshot = service.resolve_effective_adaptation_snapshot(
            authority_id=AUTHORITY_ID,
            profile_ids=("research", "coding"),
        )

        assert snapshot == {
            "coding": "revision-coding",
            "research": service.static_profile_revision_id(
                AUTHORITY_ID, "research"
            ),
        }


def test_store_reads_profile_pointer_snapshot_in_one_canonical_mapping(
    tmp_path: Path,
) -> None:
    with RoutingStore.open(path=tmp_path / "state.db") as store:
        store.publish_profile_revision(
            _revision("coding", "revision-coding"),
            expected_revision_id=None,
            expected_generation=0,
        )

        snapshot = store.read_active_profile_revision_snapshot(
            AUTHORITY_ID,
            ("research", "coding"),
        )

        assert tuple(snapshot) == ("coding", "research")
        assert snapshot["coding"][0].revision_id == "revision-coding"
        assert snapshot["research"] == (None, 0)


def test_authority_edit_invalidates_overlay_without_deleting_history(
    tmp_path: Path,
) -> None:
    with RoutingStore.open(path=tmp_path / "state.db") as store:
        store.publish_profile_revision(
            _revision("coding", "revision-coding"),
            expected_revision_id=None,
            expected_generation=0,
        )
        service = AutoRoutingService.__new__(AutoRoutingService)
        service.store = store
        edited_authority = "d" * 64
        snapshot = service.resolve_effective_adaptation_snapshot(
            authority_id=edited_authority,
            profile_ids=("coding",),
        )

        assert snapshot == {
            "coding": service.static_profile_revision_id(
                edited_authority, "coding"
            )
        }
        assert store.read_profile_revision("revision-coding") is not None


def test_selector_remains_free_of_evidence_and_learner_dependencies() -> None:
    selector = (
        Path(__file__).parents[3]
        / "plugins"
        / "auto_routing"
        / "auto_routing"
        / "selector.py"
    ).read_text(encoding="utf-8")

    assert "EvidenceEvent" not in selector
    assert "evidence_events" not in selector
    assert "from .learner" not in selector


def test_frozen_or_disabled_profile_never_acquires_optimizer_lease() -> None:
    for enabled, frozen, expected in (
        (False, False, "disabled"),
        (True, True, "frozen"),
    ):
        config = _adaptive_config(enabled=enabled)
        store = SimpleNamespace(
            read_profile_control=lambda _authority, _profile: SimpleNamespace(
                frozen=frozen,
                experiment_phase="eligible",
                cooldown_until=None,
            ),
            acquire_optimizer_lease=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("disabled/frozen lifecycle acquired a lease")
            ),
        )
        service = AutoRoutingService.__new__(AutoRoutingService)
        service.store = store
        service._configured_authority = lambda: config

        result = service.maybe_advance_adaptation(
            profile_id="coding",
            now="2026-07-18T12:00:00Z",
        )

        assert result.action == expected


def test_adaptation_observations_are_exact_content_free_pairs() -> None:
    config = _adaptive_config()
    profile = config.profiles["coding"]
    runtime_id = profile.primary_challengers[0].runtime.stable_id()
    bucket = SimpleNamespace(bucket_id="bucket-a")
    matching = SimpleNamespace(
        evidence_id="evidence-a",
        parent_evidence_id=None,
        decision_id="decision-a",
        profile_id="coding",
        runtime_id=runtime_id,
        reasoning_effort="medium",
        context_bucket=bucket,
        is_initial_routing_task=True,
        source="hermes_turn_outcome",
        outcome="verified",
        feedback_value=None,
        observed_at="2026-07-18T12:00:00Z",
    )
    feedback = SimpleNamespace(
        **{
            **matching.__dict__,
            "evidence_id": "feedback-a",
            "parent_evidence_id": "evidence-a",
            "source": "user_feedback",
            "outcome": None,
            "feedback_value": "rating-5",
        }
    )
    failed = SimpleNamespace(
        **{
            **matching.__dict__,
            "evidence_id": "failed-a",
            "decision_id": "decision-failed",
            "outcome": "failed",
        }
    )
    stale_failed = SimpleNamespace(
        **{
            **failed.__dict__,
            "evidence_id": "failed-stale-authority",
            "decision_id": "decision-stale",
        }
    )
    ignored = SimpleNamespace(**{**matching.__dict__, "runtime_id": "runtime-b"})
    decision = SimpleNamespace(
        authority_revision=AUTHORITY_ID,
        selected_profile_id="coding",
        adaptive_assignment_id="assignment-a",
        profile_adaptive_revision_id="revision-a",
    )
    store = SimpleNamespace(
        list_evidence_events=lambda **_kwargs: (
            matching,
            feedback,
            failed,
            stale_failed,
            ignored,
        ),
        read_decision=lambda decision_id: (
            SimpleNamespace(
                **{
                    **decision.__dict__,
                    "authority_revision": "b" * 64,
                }
            )
            if decision_id == "decision-stale"
            else decision
        ),
    )
    service = AutoRoutingService.__new__(AutoRoutingService)
    service.store = store

    observations = service.list_adaptation_observations(
        authority_id=AUTHORITY_ID,
        profile_id="coding",
        context_bucket_id="bucket-a",
        runtime_id=runtime_id,
        reasoning_effort="medium",
        assignment_id="assignment-a",
    )

    assert [item["evidence_id"] for item in observations] == [
        "evidence-a",
        "failed-a",
        "feedback-a",
    ]
    assert all(item["assignment_id"] == "assignment-a" for item in observations)
    assert all("provider" not in item and "content" not in item for item in observations)
    assert AutoRoutingService._operational_guardrail_reason(
        config=config,
        profile=profile,
        runtime_id=runtime_id,
        observations=observations,
    ) == "failure_guardrail"


def test_first_local_context_starts_complete_canary_and_releases_lease(
    tmp_path: Path,
) -> None:
    config = _adaptive_config()
    context_event = SimpleNamespace(
        evidence_id="e" * 64,
        decision_id="decision-current",
        profile_id="coding",
        observed_at="2026-07-18T11:00:00Z",
        is_initial_routing_task=True,
        context_bucket=SimpleNamespace(bucket_id="f" * 64),
    )
    with RoutingStore.open(path=tmp_path / "state.db") as store:
        class StoreProxy:
            def __getattr__(self, name):
                return getattr(store, name)

            def list_evidence_events(self, **_kwargs):
                return (context_event,)

            def read_decision(self, _decision_id):
                return SimpleNamespace(
                    authority_revision=authority_revision(config),
                    selected_profile_id="coding",
                )

        service = AutoRoutingService.__new__(AutoRoutingService)
        service.store = StoreProxy()
        service._configured_authority = lambda: config

        result = service.maybe_advance_adaptation(
            profile_id="coding",
            now="2026-07-18T12:00:00Z",
        )

        authority_id = authority_revision(config)
        control = store.read_profile_control(authority_id, "coding")
        assert result.action == "canary"
        assert control.experiment_phase == "canary"
        assert control.active_revision_id == control.control_revision_id
        assert len(store.list_profile_revisions(authority_id, "coding")) == 2
        assert store.connection.execute(
            "SELECT COUNT(*) FROM adaptive_optimizer_leases"
        ).fetchone()[0] == 0


def test_challenger_revision_uses_only_captured_authority_config() -> None:
    config = _adaptive_config()
    profile = config.profiles["coding"]
    authority_id = authority_revision(config)
    service = AutoRoutingService.__new__(AutoRoutingService)
    service.store = SimpleNamespace(list_profile_revisions=lambda *_args: ())
    service._configured_authority = lambda: (_ for _ in ()).throw(
        AssertionError("live config must not be reread under the optimizer lease")
    )
    parent = service._static_profile_revision(
        authority_id,
        profile,
        "2026-07-18T12:00:00Z",
    )

    challenger = service._challenger_profile_revision(
        config=config,
        authority_id=authority_id,
        profile=profile,
        parent=parent,
        context_bucket_id="f" * 64,
        evidence_ids=("e" * 64,),
        created_at="2026-07-18T12:00:01Z",
    )

    assert challenger.authority_id == authority_id
    assert challenger.parent_revision_id == parent.revision_id


def test_challenger_revision_tries_each_configured_primary_before_cycling(
    tmp_path: Path,
) -> None:
    document = json.loads(
        (Path(__file__).with_name("fixtures") / "approved_proposal.json").read_text(
            encoding="utf-8"
        )
    )
    profile_document = document["profiles"]["coding"]
    challengers = []
    for suffix in ("one", "two"):
        challenger = json.loads(json.dumps(profile_document["primary"]))
        challenger["runtime"].update(
            {
                "model": f"approved-challenger-{suffix}",
                "auth_identity": f"auth:challenger:{suffix}",
            }
        )
        challenger["revision_status"] = "challenger"
        challengers.append(challenger)
        document["llm"]["allowed_models"].append(
            f"approved-challenger-{suffix}"
        )
    profile_document["primary_challengers"] = challengers
    profile_document["adaptation"] = {"enabled": True}
    config = parse_config({"plugins": {"entries": {"auto-routing": document}}})
    profile = config.profiles["coding"]
    authority_id = authority_revision(config)

    with RoutingStore.open(path=tmp_path / "state.db") as store:
        service = AutoRoutingService.__new__(AutoRoutingService)
        service.store = store
        parent = service._static_profile_revision(
            authority_id,
            profile,
            "2026-07-18T12:00:00Z",
        )
        generation = store.publish_profile_revision(
            parent,
            expected_revision_id=None,
            expected_generation=0,
        )
        first = service._challenger_profile_revision(
            config,
            authority_id,
            profile,
            parent,
            "f" * 64,
            ("e" * 64,),
            "2026-07-18T12:00:01Z",
        )
        generation = store.insert_inactive_profile_revision(
            first,
            expected_active_revision_id=parent.revision_id,
            expected_generation=generation,
        )
        second = service._challenger_profile_revision(
            config,
            authority_id,
            profile,
            parent,
            "f" * 64,
            ("e" * 64,),
            "2026-07-18T12:00:02Z",
        )
        store.insert_inactive_profile_revision(
            second,
            expected_active_revision_id=parent.revision_id,
            expected_generation=generation,
        )
        third = service._challenger_profile_revision(
            config,
            authority_id,
            profile,
            parent,
            "f" * 64,
            ("e" * 64,),
            "2026-07-18T12:00:03Z",
        )

    configured = tuple(
        target.runtime.stable_id() for target in profile.primary_challengers
    )
    assert (
        first.explanation.challenger_runtime_id,
        second.explanation.challenger_runtime_id,
        third.explanation.challenger_runtime_id,
    ) == (configured[0], configured[1], configured[0])
    assert first.explanation.labels["challenger_selection"] == "untried"
    assert second.explanation.labels["challenger_selection"] == "untried"
    assert third.explanation.labels["challenger_selection"] == "cycle"
    assert third.revision_id != first.revision_id


@pytest.mark.parametrize(
    ("observation_update", "expected_reason"),
    [
        (
            {
                "source": "user_feedback",
                "outcome": None,
                "feedback_value": "corrected",
            },
            "promoted_regression_rollback",
        ),
        (
            {
                "source": "hermes_turn_outcome",
                "outcome": "verified",
                "feedback_value": None,
                "cost_usd": 10_000.0,
            },
            "cost_guardrail",
        ),
        (
            {
                "source": "hermes_turn_outcome",
                "outcome": "failed",
                "feedback_value": None,
            },
            "failure_guardrail",
        ),
    ],
)
def test_promoted_adverse_signal_rolls_back_exact_control(
    observation_update: dict[str, object],
    expected_reason: str,
) -> None:
    config = _adaptive_config()
    profile = config.profiles["coding"]
    control_revision = _revision("coding", "revision-control").model_copy(
        update={
            "authority_id": authority_revision(config),
            "overlay": AutoRoutingService._profile_overlay(profile),
        }
    )
    challenger_revision = control_revision.model_copy(
        update={
            "revision_id": "revision-challenger",
            "parent_revision_id": control_revision.revision_id,
            "overlay": control_revision.overlay.model_copy(
                update={
                    "ordered_primary_runtime_ids": tuple(
                        reversed(control_revision.overlay.ordered_primary_runtime_ids)
                    )
                }
            ),
            "explanation": AdaptiveExplanation(
                context_bucket_id="f" * 64,
                control_revision_id=control_revision.revision_id,
            ),
        }
    )
    state = SimpleNamespace(
        control_revision_id=control_revision.revision_id,
        challenger_revision_id=challenger_revision.revision_id,
        generation=7,
        rejection_count=0,
    )
    transitioned = []

    def transition(*_args, **kwargs):
        transitioned.append(kwargs)
        return SimpleNamespace(active_revision_id=kwargs["active_revision_id"])

    service = AutoRoutingService.__new__(AutoRoutingService)
    service.store = SimpleNamespace(
        read_profile_revision=lambda revision_id: {
            control_revision.revision_id: control_revision,
            challenger_revision.revision_id: challenger_revision,
        }[revision_id],
        transition_profile_experiment=transition,
    )
    service.list_adaptation_observations = lambda **kwargs: (
        ({
            "evidence_id": "feedback-a",
            "parent_evidence_id": "parent-a",
            "decision_id": "decision-a",
            "assignment_id": None,
            "profile_adaptive_revision_id": challenger_revision.revision_id,
            "is_initial_routing_task": True,
            "source": "hermes_turn_outcome",
            "outcome": "verified",
            "feedback_value": None,
            "retry_count": 0,
            "cost_usd": 0.0,
            "latency_seconds": 0.0,
            "observed_at": "2026-07-18T12:00:00Z",
            **observation_update,
        },)
        if kwargs["runtime_id"]
        == challenger_revision.overlay.ordered_primary_runtime_ids[0]
        else ()
    )
    service._daily_experiment_spend_usd = lambda *_args: float(
        observation_update.get("cost_usd", 0.0)
    )

    result = service._evaluate_promoted(
        config=config,
        profile=profile,
        control=state,
        authority_id=authority_revision(config),
        moment=datetime.fromisoformat("2026-07-18T12:00:00+00:00"),
    )

    assert result.action == "rollback"
    assert result.reason == expected_reason
    assert transitioned[0]["experiment_phase"] == "cooldown"
    assert transitioned[0]["active_revision_id"] == control_revision.revision_id


@pytest.mark.parametrize(
    ("challenger_update", "experiment_spend", "expected_reason"),
    [
        ({"cost_usd": 10_000.0}, 10_000.0, "cost_guardrail"),
        ({"outcome": "failed"}, 0.0, "failure_guardrail"),
    ],
)
def test_canary_guardrail_blocks_an_otherwise_promotable_challenger(
    monkeypatch,
    challenger_update: dict[str, object],
    experiment_spend: float,
    expected_reason: str,
) -> None:
    config = _adaptive_config()
    profile = config.profiles["coding"]
    authority_id = authority_revision(config)
    control_revision = _revision("coding", "revision-control").model_copy(
        update={
            "authority_id": authority_id,
            "overlay": AutoRoutingService._profile_overlay(profile),
        }
    )
    challenger_revision = control_revision.model_copy(
        update={
            "revision_id": "revision-challenger",
            "parent_revision_id": control_revision.revision_id,
            "overlay": control_revision.overlay.model_copy(
                update={
                    "ordered_primary_runtime_ids": tuple(
                        reversed(control_revision.overlay.ordered_primary_runtime_ids)
                    )
                }
            ),
            "explanation": AdaptiveExplanation(
                context_bucket_id="f" * 64,
                control_revision_id=control_revision.revision_id,
            ),
        }
    )
    state = SimpleNamespace(
        control_revision_id=control_revision.revision_id,
        challenger_revision_id=challenger_revision.revision_id,
        generation=7,
        rejection_count=0,
    )
    transitions = []

    def transition(*_args, **kwargs):
        transitions.append(kwargs)
        return SimpleNamespace(
            active_revision_id=kwargs["active_revision_id"],
            generation=state.generation + len(transitions),
            rejection_count=kwargs["rejection_count"],
        )

    service = AutoRoutingService.__new__(AutoRoutingService)
    service.store = SimpleNamespace(
        read_profile_revision=lambda revision_id: {
            control_revision.revision_id: control_revision,
            challenger_revision.revision_id: challenger_revision,
        }[revision_id],
        transition_profile_experiment=transition,
    )

    def observations(**kwargs):
        revision = (
            challenger_revision
            if kwargs["runtime_id"]
            == challenger_revision.overlay.ordered_primary_runtime_ids[0]
            else control_revision
        )
        return ({
            "evidence_id": f"evidence-{revision.revision_id}",
            "parent_evidence_id": None,
            "decision_id": f"decision-{revision.revision_id}",
            "assignment_id": "assignment-a",
            "profile_adaptive_revision_id": revision.revision_id,
            "is_initial_routing_task": True,
            "source": "hermes_turn_outcome",
            "outcome": "verified",
            "feedback_value": None,
            "retry_count": 0,
            "cost_usd": 0.0,
            "latency_seconds": 0.0,
            "observed_at": "2026-07-18T12:00:00Z",
            **(challenger_update if revision is challenger_revision else {}),
        },)

    service.list_adaptation_observations = observations
    service._daily_experiment_spend_usd = lambda *_args: experiment_spend
    monkeypatch.setattr(
        service_module,
        "promotion_decision",
        lambda *_args, **_kwargs: SimpleNamespace(
            action="promote",
            reason="would_promote",
        ),
    )

    result = service._evaluate_canary(
        config=config,
        profile=profile,
        control=state,
        authority_id=authority_id,
        moment=datetime.fromisoformat("2026-07-18T12:00:00+00:00"),
    )

    assert result.action == "rejected"
    assert result.reason == expected_reason
    assert transitions[0]["experiment_phase"] == "rejected"
    assert all(
        transition["active_revision_id"] == control_revision.revision_id
        for transition in transitions
    )


def test_inactive_challenger_insert_never_changes_active_control(
    tmp_path: Path,
) -> None:
    with RoutingStore.open(path=tmp_path / "state.db") as store:
        control_generation = store.publish_profile_revision(
            _revision("coding", "revision-control"),
            expected_revision_id=None,
            expected_generation=0,
        )
        challenger = _revision("coding", "revision-challenger").model_copy(
            update={"parent_revision_id": "revision-control"}
        )

        unchanged_generation = store.insert_inactive_profile_revision(
            challenger,
            expected_active_revision_id="revision-control",
            expected_generation=control_generation,
        )

        active, generation = store.read_active_profile_revision(
            AUTHORITY_ID, "coding"
        )
        assert active is not None and active.revision_id == "revision-control"
        assert generation == unchanged_generation == control_generation
        assert store.read_profile_revision("revision-challenger") == challenger


def test_assignment_reuse_rejects_freeze_and_stale_experiment(
    tmp_path: Path,
) -> None:
    with RoutingStore.open(path=tmp_path / "state.db") as store:
        _, generation = _publish_pair(store)
        validated = store.transition_profile_experiment(
            AUTHORITY_ID,
            "coding",
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
            "coding",
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
        stored = store.get_or_create_canary_assignment(assignment())
        store.set_profile_freeze(
            AUTHORITY_ID,
            "coding",
            frozen=True,
            expected_generation=canary.generation,
        )

        with pytest.raises(storage_module.ProfileFrozen):
            store.get_or_create_canary_assignment(stored)


def test_proposal_context_filters_old_authority_before_selection() -> None:
    current = SimpleNamespace(
        evidence_id="c" * 64,
        decision_id="decision-current",
        profile_id="coding",
        observed_at="2026-07-18T11:00:00Z",
        is_initial_routing_task=True,
        context_bucket=SimpleNamespace(bucket_id="d" * 64),
    )
    old = SimpleNamespace(
        **{
            **current.__dict__,
            "evidence_id": "e" * 64,
            "decision_id": "decision-old",
            "observed_at": "2026-07-18T12:00:00Z",
            "context_bucket": SimpleNamespace(bucket_id="f" * 64),
        }
    )
    service = AutoRoutingService.__new__(AutoRoutingService)
    service.store = SimpleNamespace(
        list_evidence_events=lambda **_kwargs: (current, old),
        read_decision=lambda decision_id: SimpleNamespace(
            authority_revision=(
                AUTHORITY_ID if decision_id == "decision-current" else "b" * 64
            ),
            selected_profile_id="coding",
        ),
    )

    events = service._proposal_context_events(AUTHORITY_ID, "coding")

    assert events == (current,)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"retry_count": 3}, "retry_guardrail"),
        ({"cost_usd": 10_000.0}, "cost_guardrail"),
        ({"latency_seconds": 10_000.0}, "latency_guardrail"),
    ],
)
def test_operational_guardrails_act_without_becoming_quality(
    overrides: dict[str, object],
    reason: str,
) -> None:
    config = _adaptive_config()
    profile = config.profiles["coding"]
    runtime_id = profile.primary_challengers[0].runtime.stable_id()
    observation = {
        "evidence_id": "a" * 64,
        "is_initial_routing_task": True,
        "source": "hermes_turn_outcome",
        "outcome": "verified",
        "feedback_value": None,
        "retry_count": 0,
        "cost_usd": 0.0,
        "latency_seconds": 0.0,
        **overrides,
    }

    assert summarize_quality((observation,)).success_sum == 1.0
    assert AutoRoutingService._operational_guardrail_reason(
        config=config,
        profile=profile,
        runtime_id=runtime_id,
        observations=(observation,),
    ) == reason


def test_failed_turn_guardrail_cannot_be_washed_out_by_verified_quality() -> None:
    config = _adaptive_config()
    profile = config.profiles["coding"]
    runtime_id = profile.primary_challengers[0].runtime.stable_id()
    observations = tuple(
        {
            "evidence_id": f"{index:064x}",
            "decision_id": f"decision-{index}",
            "assignment_id": "assignment-a",
            "is_initial_routing_task": True,
            "source": "hermes_turn_outcome",
            "outcome": "failed" if index == 25 else "verified",
            "feedback_value": None,
            "retry_count": 0,
            "cost_usd": 0.0,
            "latency_seconds": 0.0,
            "observed_at": "2026-07-18T12:00:00Z",
        }
        for index in range(26)
    )

    quality = summarize_quality(observations)
    assert quality.success_sum == 25.0
    assert quality.comparable_samples == 25
    assert AutoRoutingService._operational_guardrail_reason(
        config=config,
        profile=profile,
        runtime_id=runtime_id,
        observations=observations,
    ) == "failure_guardrail"


def test_operational_guardrail_enforces_persisted_daily_experiment_budget() -> None:
    config = _adaptive_config()
    profile = config.profiles["coding"]
    runtime_id = profile.primary_challengers[0].runtime.stable_id()
    observations = tuple(
        {
            "evidence_id": character * 64,
            "is_initial_routing_task": True,
            "source": "hermes_turn_outcome",
            "outcome": "verified",
            "feedback_value": None,
            "retry_count": 0,
            "cost_usd": 1.1,
            "latency_seconds": 0.0,
            "observed_at": "2026-07-18T12:00:00Z",
        }
        for character in ("a", "b")
    )

    assert summarize_quality(observations).success_sum == 2.0
    assert AutoRoutingService._operational_guardrail_reason(
        config=config,
        profile=profile,
        runtime_id=runtime_id,
        observations=observations,
        now=datetime.fromisoformat("2026-07-18T13:00:00+00:00"),
    ) == "budget_guardrail"


def test_daily_experiment_spend_uses_only_current_assigned_turns() -> None:
    events = (
        SimpleNamespace(
            decision_id="decision-current",
            is_initial_routing_task=True,
            source="hermes_turn_outcome",
            cost_usd=1.25,
        ),
        SimpleNamespace(
            decision_id="decision-unassigned",
            is_initial_routing_task=True,
            source="hermes_turn_outcome",
            cost_usd=9.0,
        ),
        SimpleNamespace(
            decision_id="decision-old-authority",
            is_initial_routing_task=True,
            source="hermes_turn_outcome",
            cost_usd=9.0,
        ),
    )
    decisions = {
        "decision-current": SimpleNamespace(
            authority_revision=AUTHORITY_ID,
            adaptive_assignment_id="assignment-a",
        ),
        "decision-unassigned": SimpleNamespace(
            authority_revision=AUTHORITY_ID,
            adaptive_assignment_id=None,
        ),
        "decision-old-authority": SimpleNamespace(
            authority_revision="b" * 64,
            adaptive_assignment_id="assignment-old",
        ),
    }
    observed_filters = []
    service = AutoRoutingService.__new__(AutoRoutingService)
    service.store = SimpleNamespace(
        list_evidence_events=lambda **kwargs: (
            observed_filters.append(kwargs) or events
        ),
        read_decision=lambda decision_id: decisions[decision_id],
    )

    assert service._daily_experiment_spend_usd(
        AUTHORITY_ID,
        datetime.fromisoformat("2026-07-18T13:00:00+00:00"),
    ) == 1.25
    assert observed_filters == [
        {"observed_at_or_after": "2026-07-18T00:00:00Z"}
    ]
