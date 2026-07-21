"""Locked profile-management config reconciliation and recovery contracts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from plugins.auto_routing.auto_routing import config_io as config_io_module
from plugins.auto_routing.auto_routing.config import (
    authority_revision,
    config_document,
    management_authority_revision,
    parse_config,
)
from plugins.auto_routing.auto_routing.config_io import (
    ConfigRollbackError,
    LockedConfigUpdate,
    ManagementRevisionResult,
    apply_management_config_revision,
    locked_update,
    recover_management_config_revision,
)
from plugins.auto_routing.auto_routing.learner import LearnerDecision
from plugins.auto_routing.auto_routing.models import (
    AccessEconomics,
    AutoRoutingConfig,
    ManagementConfigReceipt,
    ManagementCanaryAssignment,
    ManagementControl,
    ManagementLifecycleEvent,
    ManagementPatch,
    ManagementRevision,
    RankingPackMetadata,
    ReasoningBounds,
    RoutingTarget,
    RuntimeKey,
    RuntimeObservation,
)
from plugins.auto_routing.auto_routing.ranking_pack import (
    RankingPackRow,
    VerifiedRankingPack,
    VerifiedRankingPackMetadata,
)
from plugins.auto_routing.auto_routing.service import (
    AutoRoutingService,
    AutoRoutingServiceError,
)
from plugins.auto_routing.auto_routing.storage import RoutingStore
from utils import fast_safe_load


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
NOW_TEXT = "2026-07-19T12:00:00Z"
PUBLIC_KEY = "MCowBQYDK2VwAyEA7Qmps3rMcxRhc2Y7Qpdn1i8eo1vvS9A0Yrs7mKMbVhc="


def _runtime_key(model: str, *, revision: str = "inventory-current") -> RuntimeKey:
    return RuntimeKey(
        provider="openai",
        model=model,
        auth_identity="api-key:work",
        credential_pool_identity="pool:work",
        endpoint_identity="endpoint:work",
        api_mode="chat_completions",
        inventory_revision=revision,
    )


def _target(model: str, *, status: str) -> dict[str, object]:
    return {
        "runtime": _runtime_key(model).model_dump(mode="json"),
        "reasoning": {"default": "medium", "min": "low", "max": "high"},
        "supported_reasoning_efforts": ["low", "medium", "high"],
        "max_estimated_task_cost_usd": 1.0,
        "max_estimated_latency_seconds": 10.0,
        "revision_status": status,
    }


def _config(*, enabled: bool = True, second_profile: bool = False) -> AutoRoutingConfig:
    profiles: dict[str, object] = {
        "coding": {
            "profile_id": "coding",
            "description": "Code-oriented routes.",
            "match": {
                "domains": ["coding"],
                "complexity": ["moderate"],
                "modalities": ["text"],
                "capabilities": ["tools"],
            },
            "objectives": {
                "quality": 0.55,
                "reliability": 0.25,
                "latency": 0.10,
                "cost": 0.10,
            },
            "limits": {
                "max_estimated_task_cost_usd": 1.0,
                "max_estimated_latency_seconds": 10.0,
            },
            "primary": _target("current", status="active"),
            "fallbacks": [],
            "provenance": ["user-approved"],
        }
    }
    if second_profile:
        profiles["research"] = {
            **profiles["coding"],  # type: ignore[dict-item]
            "profile_id": "research",
            "description": "Research routes.",
            "match": {
                "domains": ["research"],
                "complexity": ["moderate"],
                "modalities": ["text"],
                "capabilities": ["tools"],
            },
        }
    management: dict[str, object] = {"enabled": False}
    if enabled:
        management = {
            "enabled": True,
            "ranking_pack": {
                "ranking_pack_path": "auto-routing/ranking-packs/current.json",
                "trusted_ed25519_public_keys": [PUBLIC_KEY],
            },
            "daily_change_limit": 2,
            "schedule": "17 */6 * * *",
        }
    return parse_config({
        "plugins": {
            "entries": {
                "auto-routing": {
                    "llm": {
                        "allow_provider_override": True,
                        "allowed_providers": ["openai"],
                        "allow_model_override": True,
                        "allowed_models": ["classifier"],
                    },
                    "activation": {"mode": "shadow"},
                    "scopes": {"fresh_sessions": True, "delegation": True},
                    "classifier": {
                        "provider": "openai",
                        "model": "classifier",
                        "reasoning_effort": "low",
                        "timeout_seconds": 15.0,
                        "disclosure": "full",
                    },
                    "safe_default": "inherit",
                    "policy": {
                        "eligible_sources": ["configured_providers"],
                        "uninstalled_local_models": "deny",
                        "local_models": {
                            "require_open_weights": True,
                            "require_compatible_hardware": True,
                        },
                        "denied_providers": [],
                        "denied_models": [],
                        "max_estimated_task_cost_usd": 2.0,
                        "max_estimated_latency_seconds": 120.0,
                        "max_routing_overhead_usd_per_day": 1.0,
                        "max_experiment_cost_usd_per_day": 2.0,
                        "max_evaluator_calls_per_day": 20,
                        "max_canary_fraction": 0.05,
                        "max_reasoning_effort": "high",
                        "allow_subscription": True,
                        "allow_paid_access_probes": False,
                        "allowed_licenses": [],
                        "minimum_context_tokens": 0,
                        "canary_high_risk_tasks": False,
                    },
                    "adaptation": {
                        "enabled": True,
                        "mode": "autonomous",
                        "canary_fraction": 0.05,
                        "minimum_canary_samples": 20,
                        "rollback_threshold": 0.10,
                    },
                    "autonomous_profile_management": management,
                    "profiles": profiles,
                }
            }
        }
    })


def _write_config(
    path: Path, config: AutoRoutingConfig, *, marker: str = "before"
) -> None:
    root = {
        "display": {"skin": marker},
        "plugins": {"entries": {"auto-routing": config_document(config)}},
    }
    path.write_text(json.dumps(root, indent=2) + "\n", encoding="utf-8")


def _proposal(
    config: AutoRoutingConfig, model: str = "challenger"
) -> AutoRoutingConfig:
    profile = config.profiles["coding"]
    target = RoutingTarget(
        runtime=_runtime_key(model),
        reasoning=ReasoningBounds(default="medium", min="low", max="high"),
        supported_reasoning_efforts=("low", "medium", "high"),
        max_estimated_task_cost_usd=1.0,
        max_estimated_latency_seconds=10.0,
        revision_status="challenger",
    )
    changed = profile.model_copy(update={"primary_challengers": (target,)})
    return config.model_copy(
        update={"profiles": {**config.profiles, "coding": changed}}
    )


def _revision(
    before: AutoRoutingConfig,
    after: AutoRoutingConfig,
    *,
    revision_id: str = "management-revision-a",
) -> ManagementRevision:
    before_ids = tuple(
        target.runtime.stable_id()
        for target in (
            *before.profiles["coding"].primary_choices(),
            *before.profiles["coding"].fallbacks,
        )
    )
    after_ids = tuple(
        target.runtime.stable_id()
        for target in (
            *after.profiles["coding"].primary_choices(),
            *after.profiles["coding"].fallbacks,
        )
    )
    return ManagementRevision(
        revision_id=revision_id,
        preceding_authority_id=authority_revision(before),
        resulting_authority_id=authority_revision(after),
        management_authority_id=management_authority_revision(before),
        parent_revision_id=None,
        ranking_pack=RankingPackMetadata(
            ranking_pack_id="ranking-pack-1",
            ranking_pack_sha256="f" * 64,
            schema_version="1",
            verified_at=NOW_TEXT,
        ),
        inventory_revision="inventory-current",
        inventory_fingerprint="e" * 64,
        management_epoch=1,
        action="propose_canary",
        patches=(
            ManagementPatch(
                profile_id="coding",
                before_runtime_ids=before_ids,
                after_runtime_ids=after_ids,
                reason_codes=("new_primary_challenger",),
            ),
        ),
        runtime_scores=((after_ids[-1], 0.9),),
        created_at=NOW_TEXT,
    )


@pytest.fixture
def store(tmp_path: Path) -> RoutingStore:
    value = RoutingStore.open(home=tmp_path)
    try:
        yield value
    finally:
        value.close()


def _admit(store: RoutingStore, revision: ManagementRevision) -> None:
    assert store.try_admit_management_revision(
        profile_id="coding",
        utc_day="2026-07-19",
        daily_limit=2,
        revision=revision,
    )


def _lifecycle_service(
    tmp_path: Path,
) -> tuple[
    AutoRoutingService,
    AutoRoutingConfig,
    AutoRoutingConfig,
    ManagementRevision,
    ManagementRevision,
]:
    before = _config()
    proposal = _proposal(before)
    service = _service(tmp_path, before)
    profile = before.profiles["coding"]
    primary_id = profile.primary.runtime.stable_id()
    management_authority_id = management_authority_revision(before)
    control = ManagementRevision(
        revision_id="management-control-lifecycle",
        preceding_authority_id="0" * 64,
        resulting_authority_id=authority_revision(before),
        management_authority_id=management_authority_id,
        ranking_pack=RankingPackMetadata(
            ranking_pack_id="ranking-pack-1",
            ranking_pack_sha256="f" * 64,
            schema_version="1",
            verified_at=NOW_TEXT,
        ),
        inventory_revision="inventory-current",
        inventory_fingerprint="e" * 64,
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
    challenger = _revision(
        before,
        proposal,
        revision_id="management-challenger-lifecycle",
    ).model_copy(
        update={
            "parent_revision_id": control.revision_id,
            "management_epoch": 2,
        }
    )
    service.store.publish_management_revision(control)
    applied = apply_management_config_revision(
        proposal=proposal,
        revision=challenger,
        expected_authority_id=authority_revision(before),
        admission_utc_day="2026-07-19",
        store=service.store,
        config_path=service.config_path,
    )
    assert applied.changed is True
    return service, before, proposal, control, challenger


def _quality_observations(
    *,
    assignment_id: str,
    successful: bool,
    count: int = 20,
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "evidence_id": f"{index + (1000 if successful else 0):064x}",
            "parent_evidence_id": None,
            "decision_id": f"decision-{'challenger' if successful else 'control'}-{index}",
            "assignment_id": assignment_id,
            "is_initial_routing_task": True,
            "source": "hermes_turn_outcome" if successful else "user_feedback",
            "outcome": "verified" if successful else None,
            "feedback_value": None if successful else "rating-1",
            "retry_count": 0,
            "cost_usd": 0.0,
            "latency_seconds": 0.0,
            "observed_at": f"2026-07-19T12:{index:02d}:00Z",
        }
        for index in range(count)
    )


def test_management_promotion_uses_receipt_saga_without_stage4_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _before, proposal, control, challenger = _lifecycle_service(tmp_path)
    try:
        stage4_authority = authority_revision(proposal)
        stage4_before = service.store.read_profile_control(stage4_authority, "coding")

        started = service.maybe_advance_management(profile_id="coding", now=NOW)
        assert started.action == "canary"
        state = service.store.read_management_profile_state(
            management_authority_revision(proposal), "coding"
        )
        assert state.control_revision_id == control.revision_id
        assert state.challenger_revision_id == challenger.revision_id

        monkeypatch.setattr(
            service,
            "list_management_observations",
            lambda **kwargs: _quality_observations(
                assignment_id=(
                    "management-challenger-sample"
                    if kwargs["revision_id"] == challenger.revision_id
                    else "management-control-sample"
                ),
                successful=kwargs["revision_id"] == challenger.revision_id,
            ),
            raising=False,
        )
        promoted = service.maybe_advance_management(
            profile_id="coding",
            now=NOW + timedelta(minutes=30),
        )

        assert promoted.action == "promote"
        assert service._configured_authority().profiles[
            "coding"
        ].primary.runtime.model == ("challenger")
        transition = service.store.read_management_revision(promoted.revision_id or "")
        assert transition is not None and transition.action == "promote"
        assert service.store.management_daily_admissions("coding", "2026-07-19") == 1
        assert (
            service.store.read_profile_control(stage4_authority, "coding")
            == stage4_before
        )
    finally:
        service.close()


def test_management_rejection_restores_exact_receipt_authority_and_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, before, proposal, control, challenger = _lifecycle_service(tmp_path)
    try:
        service.maybe_advance_management(profile_id="coding", now=NOW)
        authority_id = management_authority_revision(proposal)
        state = service.store.read_management_profile_state(authority_id, "coding")
        challenger_runtime_id = next(
            runtime_id
            for runtime_id in challenger.patches[0].after_runtime_ids
            if runtime_id not in challenger.patches[0].before_runtime_ids
        )
        assignment = ManagementCanaryAssignment(
            assignment_id="management-rejected-assignment",
            management_authority_id=authority_id,
            profile_id="coding",
            operation_identity_hash="a" * 64,
            control_revision_id=control.revision_id,
            challenger_revision_id=challenger.revision_id,
            arm="challenger",
            created_at="2026-07-19T12:01:00Z",
        )
        service.store.reserve_management_assignment(
            assignment,
            expected_generation=state.generation,
        )
        service.store.finalize_management_assignment(
            assignment_id=assignment.assignment_id,
            runtime_id=challenger_runtime_id,
            reasoning_effort="medium",
            expected_generation=state.generation,
        )
        adverse = (
            {
                "evidence_id": "b" * 64,
                "parent_evidence_id": None,
                "decision_id": "decision-rejected",
                "assignment_id": assignment.assignment_id,
                "is_initial_routing_task": True,
                "source": "user_feedback",
                "outcome": None,
                "feedback_value": "rejected",
                "retry_count": 0,
                "cost_usd": 0.0,
                "latency_seconds": 0.0,
                "observed_at": "2026-07-19T12:05:00Z",
            },
        )
        monkeypatch.setattr(
            service,
            "list_management_observations",
            lambda **kwargs: (
                adverse if kwargs["revision_id"] == challenger.revision_id else ()
            ),
            raising=False,
        )

        rejected = service.maybe_advance_management(
            profile_id="coding",
            now=NOW + timedelta(minutes=10),
        )

        assert rejected.action == "rollback"
        assert authority_revision(
            service._configured_authority()
        ) == authority_revision(before)
        rolled_back = service.store.read_management_profile_state(
            authority_id, "coding"
        )
        assert rolled_back.experiment_phase == "cooldown"
        assert rolled_back.rejection_count == 1
        assert rolled_back.cooldown_until is not None
        terminal = service.store.read_management_assignment(assignment.assignment_id)
        assert terminal is not None and terminal.phase == "terminal"
        transition = service.store.read_management_revision(rejected.revision_id or "")
        assert transition is not None and transition.action == "rollback"
        assert service.store.management_daily_admissions("coding", "2026-07-19") == 1
    finally:
        service.close()


@pytest.mark.parametrize("saga_failure", ["returned_hold", "raised_error"])
def test_management_rollback_saga_failure_freezes_without_settling_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    saga_failure: str,
) -> None:
    service, _before, proposal, control, challenger = _lifecycle_service(tmp_path)
    try:
        service.maybe_advance_management(profile_id="coding", now=NOW)
        authority_id = management_authority_revision(proposal)
        state = service.store.read_management_profile_state(authority_id, "coding")
        challenger_runtime_id = next(
            runtime_id
            for runtime_id in challenger.patches[0].after_runtime_ids
            if runtime_id not in challenger.patches[0].before_runtime_ids
        )
        assignment = ManagementCanaryAssignment(
            assignment_id=f"management-failed-saga-{saga_failure}",
            management_authority_id=authority_id,
            profile_id="coding",
            operation_identity_hash=("6" if saga_failure == "returned_hold" else "7")
            * 64,
            control_revision_id=control.revision_id,
            challenger_revision_id=challenger.revision_id,
            arm="challenger",
            created_at="2026-07-19T12:01:00Z",
        )
        service.store.reserve_management_assignment(
            assignment, expected_generation=state.generation
        )
        service.store.finalize_management_assignment(
            assignment_id=assignment.assignment_id,
            runtime_id=challenger_runtime_id,
            reasoning_effort="medium",
            expected_generation=state.generation,
        )
        adverse = (
            {
                "evidence_id": "d" * 64,
                "parent_evidence_id": None,
                "decision_id": "decision-failed-saga",
                "assignment_id": assignment.assignment_id,
                "is_initial_routing_task": True,
                "source": "user_feedback",
                "outcome": None,
                "feedback_value": "rejected",
                "retry_count": 0,
                "cost_usd": 0.0,
                "latency_seconds": 0.0,
                "observed_at": "2026-07-19T12:05:00Z",
            },
        )
        monkeypatch.setattr(
            service,
            "list_management_observations",
            lambda **kwargs: (
                adverse if kwargs["revision_id"] == challenger.revision_id else ()
            ),
        )
        if saga_failure == "returned_hold":
            monkeypatch.setattr(
                service,
                "_apply_management_lifecycle_revision",
                lambda **_kwargs: ManagementRevisionResult(
                    False, "config_restored_after_replace_failure", None
                ),
            )
        else:
            monkeypatch.setattr(
                service,
                "_apply_management_lifecycle_revision",
                lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("rollback saga failed")
                ),
            )

        result = service.maybe_advance_management(
            profile_id="coding", now=NOW + timedelta(minutes=10)
        )

        assert result.action == "frozen"
        assert result.reason == "management_rollback_failed"
        assert authority_revision(
            service._configured_authority()
        ) == authority_revision(proposal)
        recovery = service.store.read_management_profile_state(authority_id, "coding")
        assert recovery.experiment_phase == "recovery_required"
        assert service.store.read_management_control(authority_id).frozen is True
        persisted = service.store.read_management_assignment(assignment.assignment_id)
        assert persisted is not None and persisted.phase == "finalized"
    finally:
        service.close()


def test_management_lifecycle_ignores_global_and_profile_adaptation_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _before, proposal, _control, _challenger = _lifecycle_service(tmp_path)
    try:
        service.maybe_advance_management(profile_id="coding", now=NOW)
        observed: list[tuple[float, int, float]] = []

        def capture_rollback(*_args, **kwargs):
            observed.append((
                kwargs["threshold"],
                kwargs["minimum_samples"],
                -1.0,
            ))
            return LearnerDecision("hold", "no_observed_regression")

        def capture_promotion(*_args, **kwargs):
            previous = observed.pop()
            observed.append((
                previous[0],
                previous[1],
                kwargs.get("confidence_level", -1.0),
            ))
            return LearnerDecision("hold", "minimum_samples")

        monkeypatch.setattr(service, "list_management_observations", lambda **_kw: ())
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.rollback_decision",
            capture_rollback,
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.promotion_decision",
            capture_promotion,
        )
        changed_stage4 = proposal.model_copy(
            update={
                "adaptation": proposal.adaptation.model_copy(
                    update={
                        "minimum_canary_samples": 10_000,
                        "rollback_threshold": 0.99,
                    }
                ),
                "profiles": {
                    **proposal.profiles,
                    "coding": proposal.profiles["coding"].model_copy(
                        update={
                            "adaptation": proposal.profiles[
                                "coding"
                            ].adaptation.model_copy(
                                update={
                                    "minimum_comparable_samples": 10_000,
                                    "observed_regression_threshold": 0.99,
                                    "confidence_level": 0.99,
                                }
                            )
                        }
                    ),
                },
            }
        )
        state = service.store.read_management_profile_state(
            management_authority_revision(proposal), "coding"
        )

        first = service._evaluate_management_canary(
            config=proposal,
            profile=proposal.profiles["coding"],
            state=state,
            moment=NOW + timedelta(minutes=5),
        )
        second = service._evaluate_management_canary(
            config=changed_stage4,
            profile=changed_stage4.profiles["coding"],
            state=state,
            moment=NOW + timedelta(minutes=5),
        )

        assert first == second
        assert observed == [(0.10, 20, 0.90), (0.10, 20, 0.90)]
    finally:
        service.close()


@pytest.mark.parametrize(
    ("failure_kind", "expected_reason"),
    [
        ("provider_failure", "failure_guardrail"),
        ("retry", "retry_guardrail"),
        ("latency", "latency_guardrail"),
        ("cost", "cost_guardrail"),
        ("budget", "budget_guardrail"),
        ("regression", "observed_regression"),
    ],
)
def test_management_guardrails_use_exact_rollback_without_new_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected_reason: str,
) -> None:
    service, before, proposal, control, challenger = _lifecycle_service(tmp_path)
    try:
        service.maybe_advance_management(profile_id="coding", now=NOW)
        base = {
            "evidence_id": "c" * 64,
            "parent_evidence_id": None,
            "decision_id": "decision-guardrail",
            "assignment_id": "management-guardrail-assignment",
            "is_initial_routing_task": True,
            "source": "hermes_turn_outcome",
            "outcome": "verified",
            "feedback_value": None,
            "retry_count": 0,
            "cost_usd": 0.0,
            "latency_seconds": 0.0,
            "observed_at": "2026-07-19T12:05:00Z",
        }
        overrides = {
            "provider_failure": {"outcome": "failed"},
            "retry": {"retry_count": 3},
            "latency": {"latency_seconds": 11.0},
            "cost": {"cost_usd": 1.1},
        }.get(failure_kind, {})
        challenger_observations = ({**base, **overrides},)
        control_observations: tuple[dict[str, object], ...] = ()
        if failure_kind == "regression":
            control_observations = _quality_observations(
                assignment_id="management-control-regression",
                successful=True,
            )
            challenger_observations = _quality_observations(
                assignment_id="management-challenger-regression",
                successful=False,
            )
        monkeypatch.setattr(
            service,
            "list_management_observations",
            lambda **kwargs: (
                challenger_observations
                if kwargs["revision_id"] == challenger.revision_id
                else control_observations
            ),
        )
        if failure_kind == "budget":
            monkeypatch.setattr(
                service,
                "_daily_management_experiment_spend_usd",
                lambda *_args: 3.0,
            )

        result = service.maybe_advance_management(
            profile_id="coding",
            now=NOW + timedelta(minutes=10),
        )

        assert result.action == "rollback"
        assert result.reason == expected_reason
        assert authority_revision(
            service._configured_authority()
        ) == authority_revision(before)
        state = service.store.read_management_profile_state(
            management_authority_revision(proposal), "coding"
        )
        assert state.experiment_phase == "cooldown"
        assert state.rejection_count == 1
        transition = service.store.read_management_revision(result.revision_id or "")
        assert transition is not None and transition.action == "rollback"
        assert service.store.management_daily_admissions("coding", "2026-07-19") == 1
        assert control.revision_id == challenger.parent_revision_id
    finally:
        service.close()


def _verified_pack(
    *runtime_ids: str,
    sha256: str = "f" * 64,
) -> VerifiedRankingPack:
    return VerifiedRankingPack(
        metadata=VerifiedRankingPackMetadata(
            ranking_pack_id="ranking-pack-1",
            ranking_pack_sha256=sha256,
            schema_version="1",
            verified_at=NOW_TEXT,
        ),
        rankings=tuple(
            RankingPackRow(
                runtime_id=runtime_id,
                quality=0.95 - index * 0.2,
                reliability=0.95 - index * 0.2,
                latency=0.1 + index * 0.2,
                cost=0.1 + index * 0.2,
            )
            for index, runtime_id in enumerate(runtime_ids)
        ),
    )


def _observation(
    model: str,
    *,
    revision: str = "inventory-current",
    verification_expires_at: datetime | None = None,
) -> RuntimeObservation:
    key = _runtime_key(model, revision=revision)
    expires_at = verification_expires_at or NOW + timedelta(hours=1)
    return RuntimeObservation(
        key=key,
        state="verified",
        reasons=(),
        economics=AccessEconomics(
            billing_kind="metered",
            effective_marginal_cost_usd_per_task=0.2,
            source_id="verified-economics",
            provenance="inventory",
            observed_at=NOW_TEXT,
        ),
        verification_source="authenticated_live",
        verified_at=NOW_TEXT,
        verification_expires_at=expires_at.isoformat().replace("+00:00", "Z"),
        provenance=("configured", "authenticated_live"),
        observed_at=NOW_TEXT,
        capabilities={
            "supports_tools": True,
            "reasoning_options": ["low", "medium", "high"],
            "context_window": 128_000,
            "estimated_latency_seconds": 1.0,
        },
    )


def _service(tmp_path: Path, config: AutoRoutingConfig) -> AutoRoutingService:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)
    return AutoRoutingService(
        plugin_context=None,
        hermes_home=tmp_path,
        store=RoutingStore.open(home=tmp_path),
        adapter=None,
        _pinned_config_path=config_path,
    )


def _seed_replaced_prepared_receipt(
    *,
    config_path: Path,
    store: RoutingStore,
    before: AutoRoutingConfig,
    proposal: AutoRoutingConfig,
    revision: ManagementRevision,
) -> tuple[ManagementConfigReceipt, bytes]:
    _write_config(config_path, before)
    exact_before = config_path.read_bytes()
    _admit(store, revision)
    receipt_id = config_io_module._management_receipt_id(revision.revision_id)
    backup_path = config_io_module._management_backup_path(config_path, receipt_id)
    with locked_update(proposal, config_path, allow_active=True) as update:
        update.create_backup(backup_path)
        receipt = ManagementConfigReceipt(
            receipt_id=receipt_id,
            revision_id=revision.revision_id,
            phase="prepared",
            preceding_authority_id=revision.preceding_authority_id,
            resulting_authority_id=revision.resulting_authority_id,
            backup_checksum=config_io_module.hashlib.sha256(exact_before).hexdigest(),
            created_at=revision.created_at,
            updated_at=revision.created_at,
        )
        store.record_management_receipt(receipt)
        update.replace()
    return receipt, exact_before


def test_apply_reloads_authority_under_lock_and_manual_edit_wins(
    tmp_path: Path,
    store: RoutingStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    _write_config(config_path, before)
    manual_profile = before.profiles["coding"].model_copy(
        update={"description": "Direct user edit wins."}
    )
    manual = before.model_copy(
        update={"profiles": {**before.profiles, "coding": manual_profile}}
    )
    _write_config(config_path, manual, marker="manual")
    exact_manual_bytes = config_path.read_bytes()

    monkeypatch.setattr(
        LockedConfigUpdate,
        "replace",
        lambda _self: pytest.fail("replace must not run after an authority change"),
    )

    result = apply_management_config_revision(
        proposal=proposal,
        revision=revision,
        expected_authority_id=revision.preceding_authority_id,
        admission_utc_day="2026-07-19",
        store=store,
        config_path=config_path,
    )

    assert result.changed is False
    assert result.reason_code == "authority_changed"
    assert result.revision_id is None
    assert config_path.read_bytes() == exact_manual_bytes
    assert not store.connection.execute(
        "SELECT 1 FROM management_config_receipts"
    ).fetchall()
    assert not store.connection.execute("SELECT 1 FROM management_revisions").fetchall()
    assert store.management_daily_admissions("coding", "2026-07-19") == 0
    state = store.read_management_profile_state(
        revision.management_authority_id,
        "coding",
        current_authority_id=authority_revision(manual),
    )
    assert state.management_epoch == 0
    assert state.experiment_phase == "eligible"


@pytest.mark.parametrize("mismatch", ["before", "after"])
def test_revision_authority_divergence_has_no_side_effects(
    tmp_path: Path,
    store: RoutingStore,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    approved = _proposal(before)
    proposal = approved
    revision = _revision(before, approved)
    if mismatch == "before":
        revision = revision.model_copy(update={"preceding_authority_id": "a" * 64})
    else:
        proposal = _proposal(before, "different-challenger")
    _write_config(config_path, before)
    exact_before = config_path.read_bytes()
    monkeypatch.setattr(
        LockedConfigUpdate,
        "replace",
        lambda _self: pytest.fail("divergent revision must not replace config"),
    )

    result = apply_management_config_revision(
        proposal=proposal,
        revision=revision,
        expected_authority_id=authority_revision(before),
        admission_utc_day="2026-07-19",
        store=store,
        config_path=config_path,
    )

    assert result.changed is False
    assert result.reason_code == "revision_authority_mismatch"
    assert result.revision_id is None
    assert config_path.read_bytes() == exact_before
    assert not list(tmp_path.glob("config.yaml.auto-routing.management.*.bak"))
    assert not store.connection.execute(
        "SELECT 1 FROM management_config_receipts"
    ).fetchall()
    assert not store.connection.execute("SELECT 1 FROM management_revisions").fetchall()
    assert store.management_daily_admissions("coding", "2026-07-19") == 0


def test_stale_management_control_hold_leaves_no_unbound_backup(
    tmp_path: Path,
    store: RoutingStore,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    _write_config(config_path, before)
    control = store.read_management_control(revision.management_authority_id)
    store.transition_management_control(
        control=control.model_copy(update={"updated_at": "2026-07-19T12:00:01Z"}),
        expected_generation=control.generation,
        event=ManagementLifecycleEvent(
            event_id="management-control-generation-changed",
            management_authority_id=revision.management_authority_id,
            profile_id="coding",
            revision_id=None,
            event_type="hold",
            reason_code="operator_hold",
            created_at="2026-07-19T12:00:01Z",
        ),
    )

    result = apply_management_config_revision(
        proposal=proposal,
        revision=revision,
        expected_authority_id=revision.preceding_authority_id,
        admission_utc_day="2026-07-19",
        expected_control_generation=control.generation,
        store=store,
        config_path=config_path,
    )

    assert result.changed is False
    assert result.reason_code == "management_control_changed"
    assert not list(tmp_path.glob("config.yaml.auto-routing.management.*.bak"))
    assert not store.connection.execute(
        "SELECT 1 FROM management_config_receipts"
    ).fetchall()
    assert not store.connection.execute("SELECT 1 FROM management_revisions").fetchall()
    assert store.management_daily_admissions("coding", "2026-07-19") == 0


def test_daily_cap_hold_leaves_no_unbound_backup(
    tmp_path: Path,
    store: RoutingStore,
) -> None:
    config_path = tmp_path / "config.yaml"
    initial = _config()
    before = initial.model_copy(
        update={
            "autonomous_profile_management": (
                initial.autonomous_profile_management.model_copy(
                    update={"daily_change_limit": 1}
                )
            )
        }
    )
    proposal = _proposal(before)
    occupied = _revision(before, proposal, revision_id="management-revision-occupied")
    revision = _revision(before, proposal, revision_id="management-revision-capped")
    _write_config(config_path, before)
    assert store.try_admit_management_revision(
        profile_id="coding",
        utc_day="2026-07-19",
        daily_limit=1,
        revision=occupied,
    )

    result = apply_management_config_revision(
        proposal=proposal,
        revision=revision,
        expected_authority_id=revision.preceding_authority_id,
        admission_utc_day="2026-07-19",
        store=store,
        config_path=config_path,
    )

    assert result.changed is False
    assert result.reason_code == "daily_cap_reached"
    assert not list(tmp_path.glob("config.yaml.auto-routing.management.*.bak"))
    assert store.read_management_revision(revision.revision_id) is None
    assert not store.connection.execute(
        "SELECT 1 FROM management_config_receipts"
    ).fetchall()
    assert store.management_daily_admissions("coding", "2026-07-19") == 1


def test_receipt_store_failure_does_not_change_exact_prior_bytes(
    tmp_path: Path,
    store: RoutingStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    _write_config(config_path, before)
    exact_before = config_path.read_bytes()

    def fail_receipt(_receipt) -> None:
        raise sqlite3.OperationalError("injected receipt write failure")

    monkeypatch.setattr(store, "record_management_receipt", fail_receipt)

    result = apply_management_config_revision(
        proposal=proposal,
        revision=revision,
        expected_authority_id=revision.preceding_authority_id,
        admission_utc_day="2026-07-19",
        store=store,
        config_path=config_path,
    )

    assert result.changed is False
    assert result.reason_code == "config_restored_after_store_failure"
    assert config_path.read_bytes() == exact_before


def test_duplicate_apply_preserves_prepared_receipt_backup_and_recoverability(
    tmp_path: Path,
    store: RoutingStore,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    _write_config(config_path, before)
    exact_before = config_path.read_bytes()
    _admit(store, revision)
    receipt_id = config_io_module._management_receipt_id(revision.revision_id)
    backup_path = config_io_module._management_backup_path(config_path, receipt_id)
    with locked_update(proposal, config_path, allow_active=True) as update:
        update.create_backup(backup_path)
        receipt = ManagementConfigReceipt(
            receipt_id=receipt_id,
            revision_id=revision.revision_id,
            phase="prepared",
            preceding_authority_id=revision.preceding_authority_id,
            resulting_authority_id=revision.resulting_authority_id,
            backup_checksum=config_io_module.hashlib.sha256(exact_before).hexdigest(),
            created_at=revision.created_at,
            updated_at=revision.created_at,
        )
        store.record_management_receipt(receipt)
    exact_backup = backup_path.read_bytes()

    result = apply_management_config_revision(
        proposal=proposal,
        revision=revision,
        expected_authority_id=revision.preceding_authority_id,
        admission_utc_day="2026-07-19",
        store=store,
        config_path=config_path,
    )

    assert result.changed is False
    assert result.reason_code == "management_recovery_required"
    assert backup_path.exists()
    assert backup_path.read_bytes() == exact_backup == exact_before
    stored = store.read_management_receipt(receipt_id)
    assert stored == receipt
    assert stored is not None and stored.phase == "prepared"

    recovered = recover_management_config_revision(
        receipt=stored,
        store=store,
        config_path=config_path,
    )

    assert recovered.reason_code == "recovered"
    assert config_path.read_bytes() == exact_before
    assert backup_path.read_bytes() == exact_backup
    recovered_receipt = store.read_management_receipt(receipt_id)
    assert recovered_receipt is not None
    assert recovered_receipt.receipt_id == receipt.receipt_id
    assert recovered_receipt.revision_id == receipt.revision_id
    assert recovered_receipt.phase == "recovery_required"


def test_db_failure_after_replace_restores_exact_prior_bytes_and_receipt(
    tmp_path: Path,
    store: RoutingStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    _write_config(config_path, before)
    exact_before = config_path.read_bytes()

    monkeypatch.setattr(
        store,
        "transition_management_profile_state",
        lambda **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("injected state commit failure")
        ),
    )

    result = apply_management_config_revision(
        proposal=proposal,
        revision=revision,
        expected_authority_id=revision.preceding_authority_id,
        admission_utc_day="2026-07-19",
        store=store,
        config_path=config_path,
    )

    assert result.changed is False
    assert result.reason_code == "config_restored_after_store_failure"
    assert config_path.read_bytes() == exact_before
    row = store.connection.execute(
        "SELECT receipt_id FROM management_config_receipts"
    ).fetchone()
    assert row is not None
    receipt = store.read_management_receipt(str(row["receipt_id"]))
    assert receipt is not None and receipt.phase == "recovery_required"


def test_failed_restore_freezes_management_and_propagates_error(
    tmp_path: Path,
    store: RoutingStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    _write_config(config_path, before)
    monkeypatch.setattr(
        store,
        "transition_management_profile_state",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("db failed")),
    )
    monkeypatch.setattr(
        LockedConfigUpdate,
        "restore",
        lambda _self, _backup: (_ for _ in ()).throw(OSError("restore failed")),
    )

    with pytest.raises(ConfigRollbackError, match="rollback could not restore"):
        apply_management_config_revision(
            proposal=proposal,
            revision=revision,
            expected_authority_id=revision.preceding_authority_id,
            admission_utc_day="2026-07-19",
            store=store,
            config_path=config_path,
        )

    assert (
        store.read_management_control(revision.management_authority_id).frozen is True
    )


def test_success_commits_receipt_profile_state_and_content_free_result(
    tmp_path: Path,
    store: RoutingStore,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    _write_config(config_path, before)

    result = apply_management_config_revision(
        proposal=proposal,
        revision=revision,
        expected_authority_id=revision.preceding_authority_id,
        admission_utc_day="2026-07-19",
        store=store,
        config_path=config_path,
    )

    assert result.changed is True
    assert result.reason_code == "revision_applied"
    assert result.revision_id == revision.revision_id
    assert set(asdict(result)) == {"changed", "reason_code", "revision_id"}
    assert authority_revision(
        parse_config(fast_safe_load(config_path.read_bytes()))
    ) == (revision.resulting_authority_id)
    row = store.connection.execute(
        "SELECT receipt_id FROM management_config_receipts"
    ).fetchone()
    assert row is not None
    receipt = store.read_management_receipt(str(row["receipt_id"]))
    assert receipt is not None and receipt.phase == "committed"
    state = store.read_management_profile_state(
        revision.management_authority_id, "coding"
    )
    assert state.active_revision_id == revision.revision_id
    assert state.authority_id == revision.resulting_authority_id


def test_prepared_crash_receipt_restores_exact_backup_under_lock(
    tmp_path: Path,
    store: RoutingStore,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    receipt, exact_before = _seed_replaced_prepared_receipt(
        config_path=config_path,
        store=store,
        before=before,
        proposal=proposal,
        revision=revision,
    )

    result = recover_management_config_revision(
        receipt=receipt,
        store=store,
        config_path=config_path,
    )

    assert result.changed is True
    assert result.reason_code == "recovered"
    assert config_path.read_bytes() == exact_before
    recovered = store.read_management_receipt(receipt.receipt_id)
    assert recovered is not None and recovered.phase == "recovery_required"


def test_stale_queued_recovery_replay_is_idempotent_after_terminal_success(
    tmp_path: Path,
    store: RoutingStore,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    stale_receipt, exact_before = _seed_replaced_prepared_receipt(
        config_path=config_path,
        store=store,
        before=before,
        proposal=proposal,
        revision=revision,
    )
    first = recover_management_config_revision(
        receipt=stale_receipt,
        store=store,
        config_path=config_path,
    )
    state = store.read_management_profile_state(
        revision.management_authority_id,
        "coding",
    )
    events_before = store.list_management_lifecycle_events(
        revision.management_authority_id,
        "coding",
    )

    replay = recover_management_config_revision(
        receipt=stale_receipt,
        store=store,
        config_path=config_path,
    )

    assert first.reason_code == "recovered"
    assert replay == ManagementRevisionResult(
        False,
        "already_recovered",
        revision.revision_id,
    )
    assert config_path.read_bytes() == exact_before
    assert (
        store.read_management_profile_state(
            revision.management_authority_id,
            "coding",
        )
        == state
    )
    assert (
        store.list_management_lifecycle_events(
            revision.management_authority_id,
            "coding",
        )
        == events_before
    )
    assert (
        store.read_management_control(revision.management_authority_id).frozen is False
    )


def test_recovery_retry_at_preceding_authority_records_no_fictitious_reversal(
    tmp_path: Path,
    store: RoutingStore,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    receipt, exact_before = _seed_replaced_prepared_receipt(
        config_path=config_path,
        store=store,
        before=before,
        proposal=proposal,
        revision=revision,
    )
    config_path.write_bytes(exact_before)

    result = recover_management_config_revision(
        receipt=receipt,
        store=store,
        config_path=config_path,
    )

    assert result.changed is False
    assert result.reason_code == "recovered"
    assert config_path.read_bytes() == exact_before
    actions = tuple(
        str(row["action"])
        for row in store.connection.execute(
            "SELECT action FROM management_revisions ORDER BY management_epoch"
        ).fetchall()
    )
    assert actions == (revision.action,)
    state = store.read_management_profile_state(
        revision.management_authority_id,
        "coding",
        current_authority_id=revision.preceding_authority_id,
    )
    assert state.experiment_phase == "eligible"
    assert state.active_revision_id is None
    assert any(
        event.event_type == "recovered" and event.revision_id is None
        for event in store.list_management_lifecycle_events(
            revision.management_authority_id,
            "coding",
        )
    )


def test_crash_recovery_never_overwrites_newer_manual_edit(
    tmp_path: Path,
    store: RoutingStore,
) -> None:
    config_path = tmp_path / "config.yaml"
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    receipt, _exact_before = _seed_replaced_prepared_receipt(
        config_path=config_path,
        store=store,
        before=before,
        proposal=proposal,
        revision=revision,
    )
    manual_profile = before.profiles["coding"].model_copy(
        update={"description": "Manual edit after interrupted replace."}
    )
    manual = before.model_copy(
        update={"profiles": {**before.profiles, "coding": manual_profile}}
    )
    _write_config(config_path, manual, marker="manual-after-crash")
    exact_manual = config_path.read_bytes()

    result = recover_management_config_revision(
        receipt=receipt,
        store=store,
        config_path=config_path,
    )

    assert result.changed is False
    assert result.reason_code == "authority_changed"
    assert config_path.read_bytes() == exact_manual
    recovered = store.read_management_receipt(receipt.receipt_id)
    assert recovered is not None and recovered.phase == "recovery_required"


def test_service_recovers_open_receipt_without_refreshing_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    service = _service(tmp_path, before)
    try:
        _receipt, exact_before = _seed_replaced_prepared_receipt(
            config_path=service.config_path,
            store=service.store,
            before=before,
            proposal=proposal,
            revision=revision,
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.inventory.InventoryService.refresh",
            lambda *_args, **_kwargs: pytest.fail("inventory refresh must not run"),
        )

        assert service.recover_management() == "recovered"
        assert service.config_path.read_bytes() == exact_before
    finally:
        service.close()


def test_guarded_recovery_binds_exact_receipt_config_backup_and_freeze(
    tmp_path: Path,
) -> None:
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    service = _service(tmp_path, before)
    try:
        receipt, exact_before = _seed_replaced_prepared_receipt(
            config_path=service.config_path,
            store=service.store,
            before=before,
            proposal=proposal,
            revision=revision,
        )
        config_io_module._freeze_management_recovery(
            revision=revision,
            store=service.store,
        )
        held = service.store.read_management_profile_state(
            revision.management_authority_id,
            "coding",
        )
        assert held.experiment_phase == "recovery_required"

        preview = service.preview_management_recovery(receipt.receipt_id)

        assert preview["precondition"]["frozen"] is True
        assert preview["precondition"]["receipt"] == receipt.model_dump(
            mode="json",
            by_alias=True,
            warnings=False,
        )
        assert preview["precondition"]["config_sha256"]
        assert preview["precondition"]["backup_sha256"] == receipt.backup_checksum

        service.config_path.write_bytes(service.config_path.read_bytes() + b"\n")
        drifted = service.config_path.read_bytes()
        with pytest.raises(
            AutoRoutingServiceError,
            match="precondition changed",
        ):
            service.apply_management_recovery(
                receipt.receipt_id,
                expected_hash=preview["precondition_hash"],
            )
        assert service.config_path.read_bytes() == drifted

        refreshed = service.preview_management_recovery(receipt.receipt_id)
        recovered = service.apply_management_recovery(
            receipt.receipt_id,
            expected_hash=refreshed["precondition_hash"],
        )

        assert recovered["applied_precondition_hash"] == refreshed["precondition_hash"]
        assert recovered["reason_code"] == "recovered"
        assert recovered["changed"] is True
        assert service.config_path.read_bytes() == exact_before
        state = service.store.read_management_profile_state(
            revision.management_authority_id,
            "coding",
        )
        assert state.experiment_phase == "eligible"
        assert state.authority_id == revision.preceding_authority_id
        assert state.active_revision_id != revision.revision_id
        recovery_revision = service.store.read_management_revision(
            state.active_revision_id or ""
        )
        assert recovery_revision is not None
        assert recovery_revision.action == "recovery"
        assert recovery_revision.parent_revision_id == revision.revision_id
        assert (
            recovery_revision.resulting_authority_id == revision.preceding_authority_id
        )
        assert any(
            event.event_type == "recovered"
            and event.revision_id == recovery_revision.revision_id
            for event in service.store.list_management_lifecycle_events(
                revision.management_authority_id,
                "coding",
            )
        )
        assert service.recover_management() == "no_recovery_required"
        with pytest.raises(AutoRoutingServiceError, match="already recovered"):
            service.preview_management_recovery(receipt.receipt_id)

        unfreeze = service.preview_management_control(action="unfreeze")
        service.apply_management_control(
            action="unfreeze",
            expected_hash=unfreeze["precondition_hash"],
        )
        assert (
            service.store.read_management_control(
                revision.management_authority_id
            ).frozen
            is False
        )
    finally:
        service.close()


def test_guarded_recovery_claim_cas_blocks_a_racing_commit_before_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    service = _service(tmp_path, before)
    try:
        receipt, _exact_before = _seed_replaced_prepared_receipt(
            config_path=service.config_path,
            store=service.store,
            before=before,
            proposal=proposal,
            revision=revision,
        )
        freeze = service.preview_management_control(action="freeze")
        service.apply_management_control(
            action="freeze",
            expected_hash=freeze["precondition_hash"],
        )
        preview = service.preview_management_recovery(receipt.receipt_id)
        proposal_bytes = service.config_path.read_bytes()
        real_transition = config_io_module._transition_management_receipt

        def commit_before_recovery_claim(store, current, phase):
            if phase == "recovery_required" and current.phase == "prepared":
                replaced = real_transition(store, current, "config_replaced")
                real_transition(store, replaced, "committed")
            return real_transition(store, current, phase)

        monkeypatch.setattr(
            config_io_module,
            "_transition_management_receipt",
            commit_before_recovery_claim,
        )
        monkeypatch.setattr(
            LockedConfigUpdate,
            "restore_exact_backup",
            lambda *_args, **_kwargs: pytest.fail(
                "recovery must not restore after losing the receipt claim"
            ),
        )

        with pytest.raises(AutoRoutingServiceError, match="precondition changed"):
            service.apply_management_recovery(
                receipt.receipt_id,
                expected_hash=preview["precondition_hash"],
            )

        assert service.config_path.read_bytes() == proposal_bytes
        raced = service.store.read_management_receipt(receipt.receipt_id)
        assert raced is not None and raced.phase == "committed"
    finally:
        service.close()


def test_recovery_retries_after_bytes_restore_before_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    service = _service(tmp_path, before)
    try:
        receipt, exact_before = _seed_replaced_prepared_receipt(
            config_path=service.config_path,
            store=service.store,
            before=before,
            proposal=proposal,
            revision=revision,
        )
        freeze = service.preview_management_control(action="freeze")
        service.apply_management_control(
            action="freeze",
            expected_hash=freeze["precondition_hash"],
        )
        preview = service.preview_management_recovery(receipt.receipt_id)
        real_finalize = config_io_module._finalize_management_recovery
        attempts = 0

        def fail_after_restore_once(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("injected crash after exact byte restore")
            return real_finalize(**kwargs)

        monkeypatch.setattr(
            config_io_module,
            "_finalize_management_recovery",
            fail_after_restore_once,
        )

        with pytest.raises(
            AutoRoutingServiceError,
            match="finalization failed",
        ):
            service.apply_management_recovery(
                receipt.receipt_id,
                expected_hash=preview["precondition_hash"],
            )

        assert service.config_path.read_bytes() == exact_before
        pending = service.store.read_management_receipt(receipt.receipt_id)
        assert pending is not None and pending.phase == "recovery_required"
        assert any(
            event.reason_code == "config_restore_started"
            for event in service.store.list_management_lifecycle_events(
                revision.management_authority_id,
                "coding",
            )
        )

        retry = service.preview_management_recovery(receipt.receipt_id)
        recovered = service.apply_management_recovery(
            receipt.receipt_id,
            expected_hash=retry["precondition_hash"],
        )

        assert recovered["reason_code"] == "recovered"
        assert recovered["changed"] is False
        state = service.store.read_management_profile_state(
            revision.management_authority_id,
            "coding",
        )
        assert state.experiment_phase == "eligible"
        assert state.authority_id == revision.preceding_authority_id
        assert service.recover_management() == "no_recovery_required"
        unfreeze = service.preview_management_control(action="unfreeze")
        service.apply_management_control(
            action="unfreeze",
            expected_hash=unfreeze["precondition_hash"],
        )
        assert (
            service.store.read_management_control(
                revision.management_authority_id
            ).frozen
            is False
        )
    finally:
        service.close()


def test_audit_only_recovery_keeps_ambiguous_profile_frozen(
    tmp_path: Path,
) -> None:
    before = _config()
    proposal = _proposal(before)
    revision = _revision(before, proposal)
    service = _service(tmp_path, before)
    try:
        receipt, exact_before = _seed_replaced_prepared_receipt(
            config_path=service.config_path,
            store=service.store,
            before=before,
            proposal=proposal,
            revision=revision,
        )
        config_io_module._freeze_management_recovery(
            revision=revision,
            store=service.store,
        )
        service.config_path.write_bytes(exact_before)
        preview = service.preview_management_recovery(receipt.receipt_id)

        recovered = service.apply_management_recovery(
            receipt.receipt_id,
            expected_hash=preview["precondition_hash"],
        )

        assert recovered["reason_code"] == "recovered"
        assert recovered["changed"] is False
        state = service.store.read_management_profile_state(
            revision.management_authority_id,
            "coding",
        )
        assert state.experiment_phase == "recovery_required"
        assert service.recover_management() == "no_recovery_required"
        with pytest.raises(
            AutoRoutingServiceError,
            match="recovery-required profiles",
        ):
            service.preview_management_control(action="unfreeze")
    finally:
        service.close()


def test_recovery_does_not_flatten_an_overwritten_canary_state(
    tmp_path: Path,
) -> None:
    service, _before, proposal, _control, challenger = _lifecycle_service(tmp_path)
    try:
        started = service.maybe_advance_management(profile_id="coding", now=NOW)
        assert started.action == "canary"
        prior = service.store.read_management_profile_state(
            challenger.management_authority_id,
            "coding",
        )
        assert prior.experiment_phase == "canary"

        failed_proposal = _proposal(proposal, model="later-challenger")
        failed = _revision(
            proposal,
            failed_proposal,
            revision_id="management-revision-after-canary",
        ).model_copy(
            update={
                "parent_revision_id": challenger.revision_id,
                "management_epoch": challenger.management_epoch + 1,
            }
        )
        receipt, _exact_before = _seed_replaced_prepared_receipt(
            config_path=service.config_path,
            store=service.store,
            before=proposal,
            proposal=failed_proposal,
            revision=failed,
        )
        config_io_module._freeze_management_recovery(
            revision=failed,
            store=service.store,
        )
        preview = service.preview_management_recovery(receipt.receipt_id)

        recovered = service.apply_management_recovery(
            receipt.receipt_id,
            expected_hash=preview["precondition_hash"],
        )

        assert recovered["reason_code"] == "recovered"
        held = service.store.read_management_profile_state(
            failed.management_authority_id,
            "coding",
        )
        assert held.experiment_phase == "recovery_required"
        assert (
            service.store.read_management_control(failed.management_authority_id).frozen
            is True
        )
        assert service.recover_management() == "no_recovery_required"
    finally:
        service.close()


def test_reconcile_disabled_holds_without_inventory_pack_or_provider_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path, _config(enabled=False))
    try:
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pytest.fail("ranking pack must not load"),
            raising=False,
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.inventory.InventoryService.refresh",
            lambda *_args, **_kwargs: pytest.fail("inventory refresh must not run"),
        )

        report = service.reconcile_management(now=NOW)

        assert report.changed is False
        assert report.reason_code == "management_disabled"
    finally:
        service.close()


def test_reconcile_missing_persisted_snapshot_is_a_no_change_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path, _config())
    try:
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pytest.fail("pack must not load without inventory"),
            raising=False,
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.inventory.InventoryService.refresh",
            lambda *_args, **_kwargs: pytest.fail("inventory refresh must not run"),
        )

        report = service.reconcile_management(now=NOW)

        assert report.changed is False
        assert report.reason_code == "inventory_snapshot_missing"
    finally:
        service.close()


def test_reconcile_reads_only_persisted_snapshot_and_applies_verified_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    service = _service(tmp_path, config)
    try:
        observations = (_observation("challenger"), _observation("current"))
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        challenger_id = observations[0].key.stable_id()
        current_id = observations[1].key.stable_id()
        pack = _verified_pack(challenger_id, current_id)
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pack,
            raising=False,
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.inventory.InventoryService.refresh",
            lambda *_args, **_kwargs: pytest.fail("inventory refresh must not run"),
        )

        report = service.reconcile_management(now=NOW)

        assert report.changed is True
        assert report.reason_code == "revision_applied"
        current = service._configured_authority()
        assert current.profiles["coding"].primary_challengers[0].runtime.model == (
            "challenger"
        )
        revisions = tuple(
            service.store.read_management_revision(str(row["revision_id"]))
            for row in service.store.connection.execute(
                "SELECT revision_id FROM management_revisions ORDER BY management_epoch"
            )
        )
        assert len(revisions) == 2
        control, challenger = revisions
        assert control is not None and control.action == "fallback_reorder"
        assert challenger is not None and challenger.action == "propose_canary"
        assert control.patches[0].profile_id == "coding"
        assert control.patches[0].before_runtime_ids == (
            control.patches[0].after_runtime_ids
        )
        assert challenger.parent_revision_id == control.revision_id
        state = service.store.read_management_profile_state(
            management_authority_revision(current), "coding"
        )
        assert state.experiment_phase == "eligible"
        started = service.maybe_advance_management(profile_id="coding", now=NOW)
        assert started.action == "canary"
        state = service.store.read_management_profile_state(
            management_authority_revision(current), "coding"
        )
        assert state.experiment_phase == "canary"
        assert state.control_revision_id == control.revision_id
        assert state.challenger_revision_id == challenger.revision_id
    finally:
        service.close()


def test_direct_profile_edit_rebases_management_and_allows_later_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    service = _service(tmp_path, config)
    try:
        observations = (_observation("challenger"), _observation("current"))
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        first_pack = _verified_pack(
            observations[0].key.stable_id(), observations[1].key.stable_id()
        )
        packs = [first_pack]
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: packs[0],
            raising=False,
        )

        first = service.reconcile_management(now=NOW)
        assert first.changed is True
        managed = service._configured_authority()
        manual_profile = managed.profiles["coding"].model_copy(
            update={"description": "Direct user authority wins."}
        )
        manual = managed.model_copy(
            update={"profiles": {**managed.profiles, "coding": manual_profile}}
        )
        _write_config(service.config_path, manual, marker="manual")

        later = _observation(
            "later-challenger",
            revision="inventory-later",
            verification_expires_at=NOW + timedelta(days=1),
        )
        carried = tuple(
            item.model_copy(
                update={
                    "key": item.key.model_copy(
                        update={"inventory_revision": "inventory-later"}
                    ),
                    "verification_expires_at": "2026-07-20T12:00:00Z",
                }
            )
            for item in observations
        )
        service.store.write_inventory_snapshot(
            "inventory-later", (*carried, later), created_at="2026-07-19T12:05:00Z"
        )
        packs[0] = _verified_pack(
            later.key.stable_id(),
            *(item.key.stable_id() for item in carried),
        )

        resumed = service.reconcile_management(now=NOW + timedelta(minutes=5))

        assert resumed.changed is True
        assert resumed.reason_code == "revision_applied"
        current = service._configured_authority()
        assert current.profiles["coding"].description == "Direct user authority wins."
        assert current.profiles["coding"].primary_challengers[0].runtime.model == (
            "later-challenger"
        )
        actions = tuple(
            str(row["action"])
            for row in service.store.connection.execute(
                "SELECT action FROM management_revisions ORDER BY management_epoch"
            )
        )
        assert actions == (
            "fallback_reorder",
            "propose_canary",
            "fallback_reorder",
            "propose_canary",
        )
        assert all(
            result.reason_code != "authority_changed" for result in resumed.profiles
        )
    finally:
        service.close()


def test_planned_reconciliation_is_cancelled_when_user_edits_before_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    service = _service(tmp_path, config)
    try:
        observations = (_observation("challenger"), _observation("current"))
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        pack = _verified_pack(
            observations[0].key.stable_id(), observations[1].key.stable_id()
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pack,
            raising=False,
        )

        preview = service.plan_management_reconciliation(now=NOW)
        manual_profile = config.profiles["coding"].model_copy(
            update={"description": "Manual edit after preview."}
        )
        manual = config.model_copy(
            update={"profiles": {**config.profiles, "coding": manual_profile}}
        )
        _write_config(service.config_path, manual, marker="manual")
        exact_manual_bytes = service.config_path.read_bytes()

        report = service.apply_management_plan(preview.plan_id, now=NOW)

        assert report.changed is False
        assert report.reason_code == "authority_changed"
        assert service.config_path.read_bytes() == exact_manual_bytes
        state = service.store.read_management_profile_state(
            management_authority_revision(config),
            "coding",
            current_authority_id=authority_revision(manual),
        )
        assert state.experiment_phase == "eligible"
        assert state.management_epoch == 0
        assert not service.store.connection.execute(
            "SELECT 1 FROM management_revisions"
        ).fetchall()
        assert service.store.management_daily_admissions("coding", "2026-07-19") == 0
    finally:
        service.close()


def test_delayed_management_plan_uses_apply_day_for_revision_and_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    service = _service(tmp_path, config)
    try:
        observations = (
            _observation(
                "challenger",
                verification_expires_at=NOW + timedelta(days=2),
            ),
            _observation(
                "current",
                verification_expires_at=NOW + timedelta(days=2),
            ),
        )
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        pack = _verified_pack(
            observations[0].key.stable_id(), observations[1].key.stable_id()
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pack,
            raising=False,
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.inventory.InventoryService.refresh",
            lambda *_args, **_kwargs: pytest.fail("inventory refresh must not run"),
        )
        preview = service.plan_management_reconciliation(now=NOW)
        apply_moment = NOW + timedelta(days=1)

        report = service.apply_management_plan(
            preview.plan_id,
            now=apply_moment,
        )

        assert report.changed is True
        assert report.reason_code == "revision_applied"
        assert service.store.management_daily_admissions("coding", "2026-07-19") == 0
        assert service.store.management_daily_admissions("coding", "2026-07-20") == 1
        revision = service.store.read_management_revision(report.revision_id or "")
        assert revision is not None
        assert revision.created_at == "2026-07-20T12:00:00Z"
    finally:
        service.close()


@pytest.mark.parametrize(
    "stale_kind",
    ["candidate_expired", "inventory_changed", "ranking_pack_changed"],
)
def test_delayed_management_plan_stale_inputs_have_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale_kind: str,
) -> None:
    config = _config()
    service = _service(tmp_path, config)
    try:
        expires_at = (
            NOW + timedelta(hours=1)
            if stale_kind == "candidate_expired"
            else NOW + timedelta(days=2)
        )
        observations = (
            _observation("challenger", verification_expires_at=expires_at),
            _observation("current", verification_expires_at=expires_at),
        )
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        current_pack = [
            _verified_pack(
                observations[0].key.stable_id(), observations[1].key.stable_id()
            )
        ]
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: current_pack[0],
            raising=False,
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.inventory.InventoryService.refresh",
            lambda *_args, **_kwargs: pytest.fail("inventory refresh must not run"),
        )
        preview = service.plan_management_reconciliation(now=NOW)
        exact_before = service.config_path.read_bytes()
        if stale_kind == "inventory_changed":
            later_revision = "inventory-later"
            later_observations = (
                _observation(
                    "challenger",
                    revision=later_revision,
                    verification_expires_at=NOW + timedelta(days=2),
                ),
                _observation(
                    "current",
                    revision=later_revision,
                    verification_expires_at=NOW + timedelta(days=2),
                ),
            )
            service.store.write_inventory_snapshot(
                later_revision,
                later_observations,
                created_at="2026-07-19T12:30:00Z",
            )
        elif stale_kind == "ranking_pack_changed":
            current_pack[0] = _verified_pack(
                observations[0].key.stable_id(),
                observations[1].key.stable_id(),
                sha256="a" * 64,
            )

        report = service.apply_management_plan(
            preview.plan_id,
            now=NOW + timedelta(hours=2),
        )

        assert report.changed is False
        assert report.reason_code == "management_plan_stale"
        assert service.config_path.read_bytes() == exact_before
        assert not list(tmp_path.glob("config.yaml.auto-routing.management.*.bak"))
        assert not service.store.connection.execute(
            "SELECT 1 FROM management_config_receipts"
        ).fetchall()
        assert not service.store.connection.execute(
            "SELECT 1 FROM management_revisions"
        ).fetchall()
        assert service.store.management_daily_admissions("coding", "2026-07-19") == 0
    finally:
        service.close()


def test_preview_then_global_freeze_holds_before_admission_or_config_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    service = _service(tmp_path, config)
    try:
        observations = (_observation("challenger"), _observation("current"))
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        pack = _verified_pack(
            observations[0].key.stable_id(), observations[1].key.stable_id()
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pack,
            raising=False,
        )
        preview = service.plan_management_reconciliation(now=NOW)
        exact_before = service.config_path.read_bytes()
        authority_id = management_authority_revision(config)
        control = service.store.read_management_control(authority_id)
        frozen_at = "2026-07-19T12:00:01Z"
        service.store.transition_management_control(
            control=ManagementControl(
                management_authority_id=authority_id,
                frozen=True,
                changes_today=control.changes_today,
                generation=control.generation,
                updated_at=frozen_at,
            ),
            expected_generation=control.generation,
            event=ManagementLifecycleEvent(
                event_id="freeze-after-management-preview",
                management_authority_id=authority_id,
                profile_id="coding",
                revision_id=None,
                event_type="frozen",
                reason_code="manual_freeze",
                created_at=frozen_at,
            ),
        )

        report = service.apply_management_plan(
            preview.plan_id,
            now=NOW + timedelta(seconds=2),
        )

        assert report.changed is False
        assert report.reason_code == "management_frozen"
        assert service.config_path.read_bytes() == exact_before
        assert not service.store.connection.execute(
            "SELECT 1 FROM management_revisions"
        ).fetchall()
        assert service.store.management_daily_admissions("coding", "2026-07-19") == 0
        assert not list(tmp_path.glob("config.yaml.auto-routing.management.*.bak"))
    finally:
        service.close()


def test_one_profile_lease_problem_does_not_block_an_independent_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(second_profile=True)
    service = _service(tmp_path, config)
    try:
        observations = (_observation("challenger"), _observation("current"))
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        pack = _verified_pack(
            observations[0].key.stable_id(), observations[1].key.stable_id()
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pack,
            raising=False,
        )
        real_acquire = service.store.acquire_management_lease

        def acquire(authority_id, profile_id, owner_id, now, lease_seconds):
            if profile_id == "coding":
                return None
            return real_acquire(authority_id, profile_id, owner_id, now, lease_seconds)

        monkeypatch.setattr(service.store, "acquire_management_lease", acquire)

        report = service.reconcile_management(now=NOW)

        assert report.changed is True
        assert tuple(
            (item.profile_id, item.changed, item.reason_code)
            for item in report.profiles
        ) == (
            ("coding", False, "management_lease_unavailable"),
            ("research", True, "revision_applied"),
        )
        assert not service.store.connection.execute(
            "SELECT 1 FROM management_leases"
        ).fetchall()
    finally:
        service.close()


def test_lease_release_failure_is_reported_and_later_canary_is_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(second_profile=True)
    service = _service(tmp_path, config)
    try:
        observations = (_observation("challenger"), _observation("current"))
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        pack = _verified_pack(
            observations[0].key.stable_id(), observations[1].key.stable_id()
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pack,
            raising=False,
        )
        real_release = service.store.release_management_lease

        def release(lease):
            if lease.profile_id == "coding":
                raise sqlite3.OperationalError("injected lease release failure")
            return real_release(lease)

        monkeypatch.setattr(service.store, "release_management_lease", release)

        report = service.reconcile_management(now=NOW)

        assert report.changed is True
        assert tuple(
            (item.profile_id, item.changed, item.reason_code)
            for item in report.profiles
        ) == (
            ("coding", True, "management_lease_release_failed"),
            ("research", False, "management_canary_pending"),
        )
        current = service._configured_authority()
        assert not current.profiles["research"].primary_challengers
    finally:
        service.close()


def test_reconcile_does_not_replace_a_pending_canary_or_start_another_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(second_profile=True)
    service = _service(tmp_path, config)
    try:
        observations = (_observation("challenger"), _observation("current"))
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        pack = _verified_pack(
            observations[0].key.stable_id(), observations[1].key.stable_id()
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pack,
            raising=False,
        )

        first = service.reconcile_management(now=NOW)
        revision_count = service.store.connection.execute(
            "SELECT COUNT(*) FROM management_revisions"
        ).fetchone()[0]
        second = service.reconcile_management(now=NOW + timedelta(seconds=1))

        assert tuple(item.reason_code for item in first.profiles) == (
            "revision_applied",
            "management_canary_pending",
        )
        assert second.changed is False
        assert tuple(item.reason_code for item in second.profiles) == (
            "management_canary_pending",
            "management_canary_pending",
        )
        assert (
            service.store.connection.execute(
                "SELECT COUNT(*) FROM management_revisions"
            ).fetchone()[0]
            == revision_count
        )
    finally:
        service.close()


def test_settled_first_profile_allows_second_profile_on_one_global_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(second_profile=True)
    service = _service(tmp_path, config)
    try:
        observations = (_observation("challenger"), _observation("current"))
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        pack = _verified_pack(
            observations[0].key.stable_id(), observations[1].key.stable_id()
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pack,
            raising=False,
        )

        first = service.reconcile_management(now=NOW)
        assert first.profiles[0].profile_id == "coding"
        assert first.profiles[0].changed is True
        assert (
            service.maybe_advance_management(
                profile_id="coding", now=NOW + timedelta(seconds=1)
            ).action
            == "canary"
        )
        during_canary = service.reconcile_management(
            now=NOW + timedelta(seconds=1, milliseconds=500)
        )
        assert during_canary.changed is False
        assert tuple(item.reason_code for item in during_canary.profiles) == (
            "management_canary_active",
            "management_canary_active",
        )
        current = service._configured_authority()
        management_authority_id = management_authority_revision(current)
        state = service.store.read_management_profile_state(
            management_authority_id, "coding"
        )
        challenger = service.store.read_management_revision(
            state.challenger_revision_id or ""
        )
        assert challenger is not None
        challenger_runtime_id = service._management_target_id(
            state=state,
            arm="challenger",
        )
        assert challenger_runtime_id is not None

        settled = service._apply_management_canary_transition(
            profile=current.profiles["coding"],
            state=state,
            challenger_revision=challenger,
            challenger_runtime_id=challenger_runtime_id,
            action="rollback",
            reason_code="test_cross_profile_settlement",
            moment=NOW + timedelta(seconds=2),
        )
        second = service.reconcile_management(now=NOW + timedelta(seconds=3))

        assert settled.action == "rollback"
        assert tuple(
            (item.profile_id, item.changed, item.reason_code)
            for item in second.profiles
        ) == (
            ("coding", False, "management_state_not_eligible"),
            ("research", True, "revision_applied"),
        )
        final = service._configured_authority()
        assert not final.profiles["coding"].primary_challengers
        assert final.profiles["research"].primary_challengers
        revisions = tuple(
            service.store.read_management_revision(str(row["revision_id"]))
            for row in service.store.connection.execute(
                "SELECT revision_id FROM management_revisions ORDER BY management_epoch"
            )
        )
        assert all(revision is not None for revision in revisions)
        assert all(
            child.parent_revision_id == parent.revision_id
            for parent, child in zip(revisions, revisions[1:])
        )
    finally:
        service.close()


def test_committed_lifecycle_receipt_recovers_post_commit_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed config revision keeps a separate recoverable settlement journal."""
    service, _before, proposal, _control, challenger = _lifecycle_service(tmp_path)
    try:
        assert service.maybe_advance_management(
            profile_id="coding",
            now=NOW + timedelta(seconds=1),
        ).action == "canary"
        state = service.store.read_management_profile_state(
            management_authority_revision(proposal),
            "coding",
        )
        challenger_runtime_id = service._management_target_id(
            state=state,
            arm="challenger",
        )
        assert challenger_runtime_id is not None
        # Simulate a process crash after the exact canary reservation commit
        # but before final runtime resolution/decision persistence.  The later
        # receipt-bound settlement must be able to discard this unused row and
        # complete recovery; otherwise one crash poisons the profile forever.
        crash_reservation = service.store.reserve_management_assignment(
            ManagementCanaryAssignment(
                assignment_id="crash-left-lifecycle-reservation",
                management_authority_id=management_authority_revision(proposal),
                profile_id="coding",
                operation_identity_hash="c" * 64,
                control_revision_id=state.control_revision_id or "missing",
                challenger_revision_id=state.challenger_revision_id or "missing",
                arm="challenger",
                created_at="2026-07-19T12:00:01.500000Z",
            ),
            expected_generation=state.generation,
        )
        assert crash_reservation.phase == "reserved"
        original_settle = service._settle_management_canary

        def fail_after_config_commit(**_kwargs):
            raise RuntimeError("injected post-commit settlement failure")

        monkeypatch.setattr(
            service,
            "_settle_management_canary",
            fail_after_config_commit,
        )
        failed = service._apply_management_canary_transition(
            profile=proposal.profiles["coding"],
            state=state,
            challenger_revision=challenger,
            challenger_runtime_id=challenger_runtime_id,
            action="rollback",
            reason_code="injected_settlement_failure",
            moment=NOW + timedelta(seconds=2),
        )
        assert failed.action == "frozen"
        receipt_row = service.store.connection.execute(
            "SELECT receipt_id, phase FROM management_config_receipts "
            "WHERE revision_id=?",
            (failed.revision_id,),
        ).fetchone()
        assert receipt_row is not None
        assert receipt_row["phase"] == "committed"
        journal = service.store.connection.execute(
            "SELECT finalization_id, phase FROM management_lifecycle_finalizations "
            "WHERE receipt_id=?",
            (receipt_row["receipt_id"],),
        ).fetchone()
        assert journal is not None
        assert journal["phase"] == "pending"
        pending_finalization = (
            service.store.read_management_lifecycle_finalization(
                str(journal["finalization_id"])
            )
        )
        assert pending_finalization is not None

        monkeypatch.setattr(
            service,
            "_settle_management_canary",
            original_settle,
        )
        preview = service.preview_management_recovery(str(receipt_row["receipt_id"]))
        recovered = service.apply_management_recovery(
            str(receipt_row["receipt_id"]),
            expected_hash=preview["precondition_hash"],
        )

        assert recovered["reason_code"] == "lifecycle_finalized"
        assert (
            service.store.read_management_assignment(
                crash_reservation.assignment_id
            )
            is None
        )
        finalized = service.store.connection.execute(
            "SELECT phase FROM management_lifecycle_finalizations "
            "WHERE finalization_id=?",
            (journal["finalization_id"],),
        ).fetchone()
        assert finalized is not None and finalized["phase"] == "finalized"
        settled = service.store.read_management_profile_state(
            management_authority_revision(proposal),
            "coding",
        )
        assert settled.experiment_phase == "cooldown"
        assert settled.active_revision_id == failed.revision_id
        event_count = service.store.connection.execute(
            "SELECT COUNT(*) FROM management_lifecycle_events"
        ).fetchone()[0]
        replayed = service._finalize_management_lifecycle(
            pending_finalization,
            moment=NOW + timedelta(days=30),
        )
        assert replayed == settled
        assert service.store.connection.execute(
            "SELECT COUNT(*) FROM management_lifecycle_events"
        ).fetchone()[0] == event_count
        cooldown_seconds = min(
            proposal.autonomous_profile_management.cooldown_max_seconds,
            proposal.autonomous_profile_management.cooldown_base_seconds,
        )
        assert settled.cooldown_until == (
            NOW + timedelta(seconds=2 + cooldown_seconds)
        ).isoformat().replace("+00:00", "Z")
        unfreeze = service.preview_management_control(action="unfreeze")
        assert unfreeze["action"] == "unfreeze"
        assert service.recover_management() == "no_recovery_required"
    finally:
        service.close()


def test_pending_lifecycle_journal_survives_freeze_failure_and_gates_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _before, proposal, _control, challenger = _lifecycle_service(tmp_path)
    try:
        assert service.maybe_advance_management(
            profile_id="coding",
            now=NOW + timedelta(seconds=1),
        ).action == "canary"
        authority_id = management_authority_revision(proposal)
        state = service.store.read_management_profile_state(authority_id, "coding")
        challenger_runtime_id = service._management_target_id(
            state=state,
            arm="challenger",
        )
        assert challenger_runtime_id is not None
        original_settle = service._settle_management_canary
        original_freeze = service._freeze_management_lifecycle_finalization

        def fail_settlement(**_kwargs):
            raise RuntimeError("injected settlement failure")

        def fail_freeze(*_args, **_kwargs):
            raise RuntimeError("injected freeze failure")

        monkeypatch.setattr(service, "_settle_management_canary", fail_settlement)
        monkeypatch.setattr(
            service,
            "_freeze_management_lifecycle_finalization",
            fail_freeze,
        )
        failed = service._apply_management_canary_transition(
            profile=proposal.profiles["coding"],
            state=state,
            challenger_revision=challenger,
            challenger_runtime_id=challenger_runtime_id,
            action="rollback",
            reason_code="injected_double_failure",
            moment=NOW + timedelta(seconds=2),
        )

        assert failed.action == "frozen"
        pending = service.store.list_pending_management_lifecycle_finalizations()
        assert len(pending) == 1
        unchanged = service.store.read_management_profile_state(authority_id, "coding")
        assert unchanged.experiment_phase == "canary"
        assert unchanged.generation == state.generation
        report = service.reconcile_management(now=NOW + timedelta(seconds=3))
        assert report.changed is False
        assert report.reason_code == "management_recovery_required"
        advance = service.maybe_advance_management(
            profile_id="coding",
            now=NOW + timedelta(seconds=3),
        )
        assert advance.reason == "management_recovery_required"
        with pytest.raises(
            AutoRoutingServiceError,
            match="lifecycle finalization requires recovery",
        ):
            service.preview_management_control(action="daily-cap", daily_limit=3)

        monkeypatch.setattr(service, "_settle_management_canary", original_settle)
        monkeypatch.setattr(
            service,
            "_freeze_management_lifecycle_finalization",
            original_freeze,
        )
        assert service.recover_management() == "recovered"
        assert not service.store.list_pending_management_lifecycle_finalizations()
        recovered = service.store.read_management_profile_state(authority_id, "coding")
        assert recovered.experiment_phase == "cooldown"
        assert recovered.active_revision_id == failed.revision_id
    finally:
        service.close()


def test_direct_user_edit_cancels_stale_active_canary_before_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    service = _service(tmp_path, config)
    try:
        observations = (_observation("challenger"), _observation("current"))
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        pack = _verified_pack(
            observations[0].key.stable_id(), observations[1].key.stable_id()
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pack,
            raising=False,
        )

        proposed = service.reconcile_management(now=NOW)
        assert proposed.changed is True
        assert (
            service.maybe_advance_management(
                profile_id="coding", now=NOW + timedelta(seconds=1)
            ).action
            == "canary"
        )
        current = service._configured_authority()
        management_authority_id = management_authority_revision(current)
        canary_state = service.store.read_management_profile_state(
            management_authority_id,
            "coding",
        )
        reserved = service.store.reserve_management_assignment(
            ManagementCanaryAssignment(
                assignment_id="direct-edit-reserved-assignment",
                management_authority_id=management_authority_id,
                profile_id="coding",
                operation_identity_hash="a" * 64,
                control_revision_id=canary_state.control_revision_id or "missing",
                challenger_revision_id=(
                    canary_state.challenger_revision_id or "missing"
                ),
                arm="control",
                created_at="2026-07-19T12:00:01.500000Z",
            ),
            expected_generation=canary_state.generation,
        )
        assert reserved.phase == "reserved"
        edited_profile = current.profiles["coding"].model_copy(
            update={"description": "User-edited coding routes."}
        )
        edited = current.model_copy(
            update={
                "profiles": {
                    **current.profiles,
                    "coding": edited_profile,
                }
            }
        )
        _write_config(service.config_path, edited, marker="direct-user-edit")

        report = service.reconcile_management(
            now=NOW + timedelta(seconds=2)
        )

        assert report.reason_code != "management_canary_active"
        final = service._configured_authority()
        assert final.profiles["coding"].description == "User-edited coding routes."
        state = service.store.read_management_profile_state(
            management_authority_revision(final), "coding"
        )
        assert state.authority_id == authority_revision(final)
        assert state.experiment_phase == "eligible"
        assert state.control_revision_id is None
        assert state.challenger_revision_id is None
        assert service.store.list_open_management_assignments(
            management_authority_revision(final), "coding"
        ) == ()
        assert (
            service.store.read_management_assignment(reserved.assignment_id) is None
        )
        assert any(
            event.event_type == "recovered"
            and event.reason_code == "manual_authority_changed"
            for event in service.store.list_management_lifecycle_events(
                management_authority_revision(final), "coding"
            )
        )
    finally:
        service.close()


@pytest.mark.parametrize(
    "action",
    ["daily-cap", "ranking-trust", "schedule", "enable", "disable"],
)
def test_hash_changing_guarded_control_retires_current_canary_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    config = _config()
    service = _service(tmp_path, config)
    try:
        observations = (_observation("challenger"), _observation("current"))
        service.store.write_inventory_snapshot(
            "inventory-current", observations, created_at=NOW_TEXT
        )
        pack = _verified_pack(
            observations[0].key.stable_id(), observations[1].key.stable_id()
        )
        monkeypatch.setattr(
            "plugins.auto_routing.auto_routing.service.load_verified_ranking_pack",
            lambda **_kwargs: pack,
        )
        assert service.reconcile_management(now=NOW).changed is True
        assert service.maybe_advance_management(
            profile_id="coding", now=NOW + timedelta(seconds=1)
        ).action == "canary"

        enabled_config = service._configured_authority()
        canary_authority_id = management_authority_revision(enabled_config)
        state = service.store.read_management_profile_state(
            canary_authority_id,
            "coding",
        )
        runtime_id = service._management_target_id(state=state, arm="control")
        assert runtime_id is not None
        reserved = service.store.reserve_management_assignment(
            ManagementCanaryAssignment(
                assignment_id=f"guarded-{action}-assignment",
                management_authority_id=canary_authority_id,
                profile_id="coding",
                operation_identity_hash="b" * 64,
                control_revision_id=state.control_revision_id or "missing",
                challenger_revision_id=state.challenger_revision_id or "missing",
                arm="control",
                created_at="2026-07-19T12:00:01.500000Z",
            ),
            expected_generation=state.generation,
        )
        finalized = service.store.finalize_management_assignment(
            assignment_id=reserved.assignment_id,
            runtime_id=runtime_id,
            reasoning_effort="medium",
            expected_generation=state.generation,
        )
        assert finalized.phase == "finalized"

        arguments: dict[str, object] = {}
        if action == "daily-cap":
            arguments["daily_limit"] = 4
        elif action == "ranking-trust":
            arguments.update({
                "ranking_pack_path": "auto-routing/ranking-packs/next.json",
                "trusted_public_keys": (PUBLIC_KEY,),
            })
        elif action == "schedule":
            arguments["schedule"] = "23 */4 * * *"
        elif action == "enable":
            disabled = enabled_config.model_copy(
                update={
                    "autonomous_profile_management": (
                        enabled_config.autonomous_profile_management.model_copy(
                            update={"enabled": False}
                        )
                    )
                }
            )
            _write_config(service.config_path, disabled, marker="disabled-directly")

        preview = service.preview_management_control(action=action, **arguments)
        applied = service.apply_management_control(
            action=action,
            expected_hash=preview["precondition_hash"],
            **arguments,
        )

        assert isinstance(applied, dict)
        retired = service.store.read_management_profile_state(
            canary_authority_id,
            "coding",
        )
        assert retired.experiment_phase == "eligible"
        assert retired.control_revision_id is None
        assert retired.challenger_revision_id is None
        assignment = service.store.read_management_assignment(
            finalized.assignment_id
        )
        assert assignment is not None and assignment.phase == "terminal"
        current = service._configured_authority()
        new_control = service.store.read_management_control(
            management_authority_revision(current)
        )
        assert new_control.generation >= 1
    finally:
        service.close()
