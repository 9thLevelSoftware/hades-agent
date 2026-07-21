"""Frozen contracts for autonomous profile-management authority."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plugins.auto_routing.auto_routing import models as models_module
from plugins.auto_routing.auto_routing.config import management_authority_revision
from plugins.auto_routing.auto_routing.models import (
    AutoRoutingConfig,
    ManagementCanaryAssignment,
    ManagementConfigReceipt,
    ManagementControl,
    ManagementDecisionSnapshot,
    ManagementLifecycleEvent,
    ManagementPatch,
    ManagementProfileState,
    ManagementRevision,
    RankingPackMetadata,
)


TEST_PUBLIC_KEY_B64 = "MCowBQYDK2VwAyEA7Qmps3rMcxRhc2Y7Qpdn1i8eo1vvS9A0Yrs7mKMbVhc="


@pytest.fixture
def valid_config() -> dict[str, object]:
    return {
        "llm": {
            "allow_provider_override": True,
            "allowed_providers": ["openai-codex"],
            "allow_model_override": True,
            "allowed_models": ["gpt-5.4-mini"],
        },
        "activation": {"mode": "shadow"},
        "scopes": {"fresh_sessions": True, "delegation": True},
        "classifier": {
            "provider": "openai-codex",
            "model": "gpt-5.4-mini",
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
        "profiles": {
            "coding": {
                "profile_id": "coding",
                "description": "Tool-using software development tasks",
                "base_rank": 70.0,
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
                "primary": {
                    "runtime": {
                        "provider": "openai-codex",
                        "model": "gpt-5.4",
                        "auth_identity": "subscription:default",
                        "api_mode": "codex_responses",
                        "inventory_revision": "inventory-1",
                    },
                    "reasoning": {
                        "default": "medium",
                        "min": "low",
                        "max": "high",
                    },
                    "supported_reasoning_efforts": [],
                    "revision_status": "active",
                },
                "fallbacks": [],
                "provenance": [],
            }
        },
    }


def test_management_is_disabled_by_default(valid_config: dict[str, object]) -> None:
    config = AutoRoutingConfig.model_validate(valid_config)

    assert config.autonomous_profile_management.enabled is False
    assert config.autonomous_profile_management.daily_change_limit == 1
    assert config.autonomous_profile_management.canary_fraction == 0.05
    assert config.autonomous_profile_management.minimum_comparable_samples == 20
    assert config.autonomous_profile_management.observed_regression_threshold == 0.10
    assert config.autonomous_profile_management.cooldown_base_seconds == 3_600
    assert config.autonomous_profile_management.cooldown_max_seconds == 86_400
    assert config.autonomous_profile_management.confidence_level == 0.90


@pytest.mark.parametrize(
    "management_update",
    [
        {"canary_fraction": 0.0},
        {"minimum_comparable_samples": 19},
        {"observed_regression_threshold": 1.1},
        {"cooldown_base_seconds": 59},
        {"cooldown_max_seconds": 59},
        {"confidence_level": 0.79},
        {"cooldown_base_seconds": 7_200, "cooldown_max_seconds": 3_600},
    ],
)
def test_management_policy_rejects_invalid_or_unordered_bounds(
    valid_config: dict[str, object],
    management_update: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AutoRoutingConfig.model_validate({
            **valid_config,
            "autonomous_profile_management": management_update,
        })


def test_management_records_reject_raw_or_secret_content() -> None:
    with pytest.raises(ValidationError, match="content-free"):
        ManagementPatch.model_validate({
            "profile_id": "coding",
            "before_runtime_ids": ("a" * 64,),
            "after_runtime_ids": ("b" * 64,),
            "reason_codes": ("ranking_upgrade",),
            "forbidden_payload": "sk-secret-sentinel",
        })


def test_management_config_changes_canonical_authority(
    valid_config: dict[str, object],
) -> None:
    base = AutoRoutingConfig.model_validate(valid_config)
    enabled = AutoRoutingConfig.model_validate({
        **base.model_dump(mode="json", by_alias=True),
        "autonomous_profile_management": {
            "enabled": True,
            "ranking_pack": {
                "ranking_pack_path": "auto-routing/ranking-packs/current.json",
                "trusted_ed25519_public_keys": (TEST_PUBLIC_KEY_B64,),
            },
            "daily_change_limit": 2,
            "schedule": "17 */6 * * *",
        },
    })

    assert management_authority_revision(base) != management_authority_revision(enabled)


def test_management_contracts_are_exported() -> None:
    required = {
        "AutonomousProfileManagementSettings",
        "RankingPackTrust",
        "RankingPackMetadata",
        "ManagementPatch",
        "ManagementRevision",
        "ManagementProfileState",
        "ManagementControl",
        "ManagementCanaryAssignment",
        "ManagementConfigReceipt",
        "ManagementLifecycleEvent",
        "ManagementDecisionSnapshot",
    }

    assert required <= set(models_module.__all__)
    assert all(
        model.model_config["frozen"]
        for model in (
            RankingPackMetadata,
            ManagementPatch,
            ManagementRevision,
            ManagementProfileState,
            ManagementControl,
            ManagementCanaryAssignment,
            ManagementConfigReceipt,
            ManagementLifecycleEvent,
            ManagementDecisionSnapshot,
        )
    )


def test_management_revision_carries_complete_content_free_authority_lineage() -> None:
    revision = ManagementRevision(
        revision_id="management-revision-a",
        preceding_authority_id="a" * 64,
        resulting_authority_id="b" * 64,
        management_authority_id="c" * 64,
        parent_revision_id=None,
        ranking_pack=RankingPackMetadata(
            ranking_pack_id="ranking-pack-a",
            ranking_pack_sha256="d" * 64,
            schema_version="1",
            verified_at="2026-07-19T12:00:00Z",
        ),
        inventory_revision="inventory-a",
        inventory_fingerprint="e" * 64,
        management_epoch=1,
        action="propose_canary",
        patches=(
            ManagementPatch(
                profile_id="coding",
                before_runtime_ids=("f" * 64,),
                after_runtime_ids=("0" * 64, "f" * 64),
                reason_codes=("ranking_upgrade",),
            ),
        ),
        runtime_scores=(("0" * 64, 0.75),),
        created_at="2026-07-19T12:00:01Z",
    )

    assert revision.preceding_authority_id == "a" * 64
    assert revision.resulting_authority_id == "b" * 64
    assert revision.inventory_fingerprint == "e" * 64
    assert revision.management_epoch == 1
    assert revision.action == "propose_canary"

    with pytest.raises(ValidationError, match="must differ"):
        ManagementRevision.model_validate({
            **revision.model_dump(mode="json"),
            "resulting_authority_id": revision.preceding_authority_id,
        })
    with pytest.raises(ValidationError, match="management_epoch"):
        ManagementRevision.model_validate({
            **revision.model_dump(mode="json"),
            "management_epoch": -1,
        })


@pytest.mark.parametrize(
    ("phase", "runtime_id", "reasoning_effort", "valid"),
    [
        ("reserved", None, None, True),
        ("reserved", "a" * 64, "medium", False),
        ("finalized", "a" * 64, "medium", True),
        ("finalized", None, None, False),
        ("terminal", "a" * 64, "medium", True),
        ("terminal", None, None, False),
    ],
)
def test_management_assignment_phase_requires_final_resolution(
    phase: str,
    runtime_id: str | None,
    reasoning_effort: str | None,
    valid: bool,
) -> None:
    document = {
        "assignment_id": "assignment-a",
        "management_authority_id": "b" * 64,
        "profile_id": "coding",
        "operation_identity_hash": "c" * 64,
        "control_revision_id": "revision-control",
        "challenger_revision_id": "revision-challenger",
        "arm": "challenger",
        "phase": phase,
        "runtime_id": runtime_id,
        "reasoning_effort": reasoning_effort,
        "created_at": "2026-07-19T12:00:02Z",
    }

    if valid:
        assert ManagementCanaryAssignment.model_validate(document).phase == phase
    else:
        with pytest.raises(ValidationError, match="reserved|finalized|terminal"):
            ManagementCanaryAssignment.model_validate(document)


def test_management_profile_state_requires_phase_coherent_revision_pair() -> None:
    state = ManagementProfileState(
        management_authority_id="a" * 64,
        profile_id="coding",
        authority_id="b" * 64,
        active_revision_id="revision-control",
        management_epoch=2,
        control_revision_id="revision-control",
        challenger_revision_id="revision-challenger",
        experiment_phase="canary",
        cooldown_until=None,
        rejection_count=0,
        generation=3,
        updated_at="2026-07-19T12:00:03Z",
    )

    assert state.active_revision_id == state.control_revision_id
    with pytest.raises(ValidationError, match="control and challenger"):
        ManagementProfileState.model_validate({
            **state.model_dump(mode="json"),
            "challenger_revision_id": None,
        })
    with pytest.raises(ValidationError, match="keep control active"):
        ManagementProfileState.model_validate({
            **state.model_dump(mode="json"),
            "active_revision_id": "revision-challenger",
        })
    with pytest.raises(ValidationError, match="cooldown_until"):
        ManagementProfileState.model_validate({
            **state.model_dump(mode="json"),
            "experiment_phase": "cooldown",
        })


def test_management_receipt_has_closed_content_free_recovery_phases() -> None:
    receipt = ManagementConfigReceipt(
        receipt_id="receipt-a",
        revision_id="revision-a",
        phase="prepared",
        preceding_authority_id="a" * 64,
        resulting_authority_id="b" * 64,
        backup_checksum="c" * 64,
        created_at="2026-07-19T12:00:04Z",
        updated_at="2026-07-19T12:00:04Z",
    )

    assert receipt.phase == "prepared"
    with pytest.raises(ValidationError):
        ManagementConfigReceipt.model_validate({
            **receipt.model_dump(mode="json"),
            "phase": "restored",
        })
    with pytest.raises(ValidationError, match="content-free"):
        ManagementConfigReceipt.model_validate({
            **receipt.model_dump(mode="json"),
            "backup_path": "C:/private/config.yaml",
        })
    with pytest.raises(ValidationError, match="must differ"):
        ManagementConfigReceipt.model_validate({
            **receipt.model_dump(mode="json"),
            "resulting_authority_id": receipt.preceding_authority_id,
        })


def test_management_patch_requires_unique_runtime_ids_and_bounded_reason_codes() -> None:
    document = {
        "profile_id": "coding",
        "before_runtime_ids": ["a" * 64, "a" * 64],
        "after_runtime_ids": ["b" * 64],
        "reason_codes": ["ranking_upgrade"],
    }

    with pytest.raises(ValidationError, match="unique"):
        ManagementPatch.model_validate(document)

    with pytest.raises(ValidationError, match="reason_codes"):
        ManagementPatch.model_validate({
            **document,
            "before_runtime_ids": ["a" * 64],
            "reason_codes": [],
        })
