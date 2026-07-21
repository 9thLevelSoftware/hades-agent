"""Real-SQLite contracts for the auto-routing profile store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from plugins.auto_routing.auto_routing import storage as storage_module
from plugins.auto_routing.auto_routing.evidence import (
    build_feedback_event,
    feedback_evidence_id,
    turn_evidence_id,
)
from plugins.auto_routing.auto_routing.models import (
    MAX_TASK_INDEX,
    AccessEconomics,
    AdaptiveRevision,
    CatalogApplicability,
    CatalogEvidence,
    RoutingDecision,
    RuntimeKey,
    RuntimeObservation,
    StoredCatalogRecord,
    TaskAssessment,
)
from plugins.auto_routing.auto_routing.storage import (
    SCHEMA_VERSION,
    ActivationReceipt,
    DecisionCandidate,
    DecisionCommit,
    DecisionOperationClaim,
    ImmutableRecordConflict,
    RevisionChecksumError,
    RevisionConflict,
    RouteEpoch,
    RoutingStore,
    SessionRouteBinding,
    UnsafeStoredContent,
    UnsupportedSchemaVersion,
    candidate_id_for,
    connect,
    init_db,
)

EXPECTED_TABLES = {
    "schema_meta",
    "authority_revisions",
    "inventory_snapshots",
    "inventory_observations",
    "catalog_snapshots",
    "catalog_evidence",
    "adaptive_revisions",
    "active_adaptive_revisions",
    "routing_decisions",
    "decision_candidates",
    "route_epochs",
    "decision_operations",
    "session_route_bindings",
    "activation_receipts",
    "budget_ledger",
    "evidence_events",
    "adaptive_profile_revisions",
    "adaptive_profile_states",
    "adaptive_lifecycle_events",
    "adaptive_canary_assignments",
    "adaptive_optimizer_leases",
}


def test_task2_public_durable_types_and_helpers_are_exported() -> None:
    required = {
        "ActivationReceipt",
        "DecisionCandidate",
        "DecisionCommit",
        "DecisionOperationClaim",
        "RouteEpoch",
        "SessionRouteBinding",
        "candidate_id_for",
        "current_process_start_token",
    }

    assert required <= set(storage_module.__all__)


def _runtime(*, model: str = "model-a", revision: str = "inventory-1") -> RuntimeKey:
    return RuntimeKey(
        provider="provider-a",
        model=model,
        auth_identity="subscription:default",
        credential_pool_identity="pool-a",
        endpoint_identity="endpoint-a",
        api_mode="chat_completions",
        inventory_revision=revision,
    )


def _assessment() -> TaskAssessment:
    return TaskAssessment(
        complexity=0.7,
        domains=("coding",),
        required_capabilities=("tools",),
        required_modalities=("text",),
        expected_context_tokens=1000,
        expected_output_tokens=100,
        quality_sensitivity=0.8,
        reliability_sensitivity=0.7,
        latency_sensitivity=0.2,
        cost_sensitivity=0.2,
        risk_class="moderate",
        confidence=0.9,
    )


def _decision(
    *,
    decision_id: str = "decision-1",
    session_id: str = "session-1",
    scope: str = "fresh_session",
    operation_id: str | None = None,
    task_index: int | None = None,
    projection_mode: str = "shadow",
    activation_receipt_id: str | None = None,
    activation_config_sha: str | None = None,
    adapter_capability_sha: str | None = None,
    profile_id: str = "coding",
) -> RoutingDecision:
    runtime = _runtime()
    return RoutingDecision(
        decision_id=decision_id,
        scope=scope,
        session_id=session_id,
        task_id=f"task-{session_id}",
        operation_id=operation_id,
        task_index=task_index,
        created_at="2026-01-01T00:00:00Z",
        applied_rule_ids=("coding",),
        assessment=_assessment(),
        task_facts_hash="a" * 64,
        inventory_revision="inventory-1",
        catalog_revision="catalog-1",
        authority_revision="authority-1",
        policy_revision="policy-1",
        adaptive_revision="adaptive-1",
        activation_receipt_id=activation_receipt_id,
        activation_config_sha=activation_config_sha,
        adapter_capability_sha=adapter_capability_sha,
        eligible_candidates=(runtime.stable_id(),),
        rejected_candidates=(),
        normalized_scoring_inputs=(("quality", 0.8),),
        final_scores=((runtime.stable_id(), 0.8),),
        selected_profile_id=profile_id,
        selected_runtime=runtime,
        selected_reasoning_effort="medium",
        projection_mode=projection_mode,
        selection_reason="highest_eligible_score",
        projected_fallback_chain=(),
        safe_default_runtime=runtime,
        safe_default_reasoning_effort="medium",
        classifier_runtime_id=runtime.stable_id(),
        classifier_input_tokens=10,
        classifier_output_tokens=5,
        classifier_cost_usd=0.001,
        routing_latency_seconds=0.05,
    )


def _candidate(
    *,
    profile_id: str = "coding",
    target_role: str = "primary",
    target_ordinal: int = 0,
    runtime_id: str | None = None,
    score: float = 0.8,
    scoring_input: float = 0.8,
) -> DecisionCandidate:
    stable_runtime = runtime_id or _runtime().stable_id()
    return DecisionCandidate(
        candidate_id=candidate_id_for(
            profile_id,
            target_role,
            target_ordinal,
            stable_runtime,
        ),
        profile_id=profile_id,
        target_role=target_role,
        target_ordinal=target_ordinal,
        runtime_id=stable_runtime,
        eligible=True,
        reason_codes=(),
        normalized_scoring_inputs=(("quality", scoring_input),),
        final_score=score,
    )


def _binding_for(
    decision: RoutingDecision,
    *,
    current_epoch: int = -1,
) -> SessionRouteBinding:
    return SessionRouteBinding(
        session_id=decision.session_id,
        binding_kind="routed",
        projection_mode=decision.projection_mode,
        decision_id=decision.decision_id,
        runtime_id=decision.selected_runtime.stable_id(),
        manual_pin_source=None,
        current_epoch=current_epoch,
        continuation_root=None,
        parent_session_id=None,
        continuation_reason=None,
        created_at=decision.created_at,
    )


def _evidence_parent(store: RoutingStore, valid_turn_event):
    receipt = ActivationReceipt(
        receipt_id="evidence-receipt-a",
        authority_id="authority-1",
        config_sha="b" * 64,
        inventory_contract_sha="c" * 64,
        inventory_revision="inventory-1",
        adapter_capability_sha="d" * 64,
        created_at="2026-01-01T00:00:00Z",
    )
    store.write_activation_receipt(receipt)
    decision = _decision(
        decision_id="decision-a",
        session_id="session-a",
        projection_mode="active",
        activation_receipt_id=receipt.receipt_id,
        activation_config_sha=receipt.config_sha,
        adapter_capability_sha=receipt.adapter_capability_sha,
    )
    committed = store.commit_decision(
        decision,
        candidates=(_candidate(),),
        create_epoch=True,
    )
    assert committed.epoch is not None
    marked_epoch = store.mark_route_epoch_provider_started(
        decision.session_id,
        decision_id=decision.decision_id,
        runtime_id=committed.epoch.runtime_id,
        api_request_id="evidence-request-a",
        started_at="2026-01-01T00:00:01Z",
    )
    event = valid_turn_event.model_copy(update={
        "decision_id": decision.decision_id,
        "session_id": decision.session_id,
        "task_id": decision.task_id,
        "route_epoch_id": marked_epoch.route_epoch_id,
        "runtime_id": marked_epoch.runtime_id,
        "profile_id": decision.selected_profile_id,
        "reasoning_effort": decision.selected_reasoning_effort,
    })
    return event.model_copy(update={
        "evidence_id": turn_evidence_id(event.session_id, event.turn_id),
    })


def _table_counts_and_checksums(
    connection: sqlite3.Connection,
    tables: tuple[str, ...],
) -> dict[str, tuple[int, str]]:
    fingerprint: dict[str, tuple[int, str]] = {}
    for table in tables:
        columns = tuple(
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        order = ", ".join(f'"{column}"' for column in columns)
        rows = [
            [row[column] for column in columns]
            for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
        ]
        document = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        fingerprint[table] = (
            len(rows),
            hashlib.sha256(document.encode()).hexdigest(),
        )
    return fingerprint


def test_profile_identity_is_representable_in_candidate_commit_contract(
    isolated_home: Path,
) -> None:
    del isolated_home
    profile_id = "Coding Profile"
    decision = _decision(profile_id=profile_id)
    candidate = _candidate(profile_id=profile_id)
    commit = DecisionCommit(
        decision=decision,
        candidates=(candidate,),
        binding=_binding_for(decision),
        epoch=None,
        status="computed",
    )

    assert commit.decision.selected_profile_id == profile_id
    assert commit.candidates[0].profile_id == profile_id

    with RoutingStore.open() as store:
        persisted = store.commit_decision(
            decision,
            candidates=(candidate,),
            create_epoch=False,
        )
        assert persisted.decision.selected_profile_id == profile_id
        assert store.read_decision(decision.decision_id) == decision


def test_candidate_profile_identity_matches_config_length_boundary(
    isolated_home: Path,
) -> None:
    del isolated_home
    profile_id = "p" * 256
    candidate = _candidate(profile_id=profile_id)
    decision = _decision(profile_id=profile_id)
    assert len(candidate.profile_id) == 256

    with RoutingStore.open() as store:
        commit = store.commit_decision(
            decision,
            candidates=(candidate,),
            create_epoch=False,
        )
        assert commit.candidates[0].profile_id == profile_id
        assert store.read_decision(decision.decision_id) == decision

    with pytest.raises(ValidationError):
        _candidate(profile_id="p" * 257)


def test_public_decision_commit_rejects_incoherent_candidate_bundle() -> None:
    decision = _decision()

    with pytest.raises(ValueError, match="eligible candidate references"):
        DecisionCommit(
            decision=decision,
            candidates=(),
            binding=_binding_for(decision),
            epoch=None,
            status="computed",
        )


def _observation(snapshot_id: str = "inventory-1") -> RuntimeObservation:
    return RuntimeObservation(
        key=RuntimeKey(
            provider="provider-a",
            model="model-a",
            auth_identity="auth-a",
            credential_pool_identity="pool-a",
            endpoint_identity="endpoint-fingerprint-a",
            api_mode="chat_completions",
            inventory_revision=snapshot_id,
        ),
        state="verified",
        reasons=(),
        economics=AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=1.0,
            metered_output_usd_per_million_tokens=2.0,
            source_id="operator-config",
            provenance="configured access path",
            observed_at="2026-01-01T00:00:00Z",
        ),
        verification_source="hermes-runtime-resolver",
        verified_at="2026-01-01T00:00:00Z",
        verification_expires_at="2026-01-02T00:00:00Z",
        provenance=("configured-provider",),
        observed_at="2026-01-01T00:00:00Z",
    )


def _evidence() -> CatalogEvidence:
    return CatalogEvidence(
        source_id="benchmark-a",
        source_url="https://catalog.example/benchmark-a",
        retrieved_at="2026-01-01T00:00:00Z",
        published_at="2025-12-15T00:00:00Z",
        model="model-a",
        model_version="2025-12",
        domain="coding",
        task_definition="repository repair benchmark",
        metric_name="pass_rate",
        metric_direction="higher_is_better",
        metric_scale="0..1",
        value=0.8,
        sample_size=100,
        confidence=0.9,
        normalization_method="identity",
    )


def _revision(
    revision_id: str,
    *,
    authority_id: str = "authority-1",
    parent_revision_id: str | None = None,
) -> AdaptiveRevision:
    return AdaptiveRevision(
        revision_id=revision_id,
        authority_id=authority_id,
        parent_revision_id=parent_revision_id,
        overlay={"profiles": {"coding": {"base_rank": 1.0}}},
        explanation={"reason": "baseline" if parent_revision_id is None else "update"},
        created_at="2026-01-01T00:00:00Z",
        is_baseline=parent_revision_id is None,
    )


def test_commit_fresh_decision_is_atomic_and_idempotent(isolated_home: Path) -> None:
    del isolated_home
    decision = _decision()
    candidates = (_candidate(),)
    with RoutingStore.open() as store:
        first = store.commit_decision(
            decision,
            candidates=candidates,
            create_epoch=True,
        )
        replay = store.commit_decision(
            decision,
            candidates=candidates,
            create_epoch=True,
        )

        assert replay == first
        assert replay.status == "replayed"
        assert first.status == "computed"
        assert store.read_session_decision(decision.session_id) == decision
        assert store.read_decision(decision.decision_id) == decision
        assert store.read_route_epochs(decision.session_id)[0].epoch_number == 0
        assert store.read_session_binding(decision.session_id).decision_id == (
            decision.decision_id
        )
        assert store.count_decisions() == 1


def test_same_operation_key_with_different_content_conflicts(
    isolated_home: Path,
) -> None:
    del isolated_home
    decision = _decision()
    with RoutingStore.open() as store:
        store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        changed = RoutingDecision.model_validate({
            **decision.model_dump(mode="json"),
            "selection_reason": "pinned_profile",
        })
        with pytest.raises(ImmutableRecordConflict):
            store.commit_decision(
                changed,
                candidates=(_candidate(),),
                create_epoch=True,
            )

            with pytest.raises(ImmutableRecordConflict):
                store.commit_decision(
                    decision,
                    candidates=(_candidate(scoring_input=0.7),),
                    create_epoch=True,
                )


def test_completed_operation_rejects_cross_decision_splice(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        first = _decision()
        second = _decision(
            decision_id="decision-2",
            session_id="session-2",
        )
        store.commit_decision(
            first,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        store.commit_decision(
            second,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        replay_claim = store.claim_decision_operation(
            scope="fresh_session",
            session_id=first.session_id,
            operation_id=None,
            task_index=None,
            facts_hash=first.task_facts_hash,
            lease_seconds=1.0,
        )
        assert replay_claim.status == "replayed"

        operation_key = f"fresh:{first.session_id}"
        row = store.connection.execute(
            "SELECT document_json FROM decision_operations WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        assert row is not None
        document = json.loads(str(row["document_json"]))
        document["decision_id"] = second.decision_id
        document_json = json.dumps(document, sort_keys=True, separators=(",", ":"))
        store.connection.execute(
            "UPDATE decision_operations SET decision_id = ?, document_json = ?, "
            "checksum = ? WHERE operation_key = ?",
            (
                second.decision_id,
                document_json,
                hashlib.sha256(document_json.encode()).hexdigest(),
                operation_key,
            ),
        )

        with pytest.raises(RevisionChecksumError, match=operation_key):
            store.wait_for_decision_operation(
                replay_claim,
                timeout_seconds=0.0,
            )
        with pytest.raises(RevisionChecksumError, match=operation_key):
            store.claim_decision_operation(
                scope="fresh_session",
                session_id=first.session_id,
                operation_id=None,
                task_index=None,
                facts_hash=first.task_facts_hash,
                lease_seconds=1.0,
            )


def test_operation_row_key_must_match_its_boundary_fields(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        operation_key = f"fresh:{decision.session_id}"
        row = store.connection.execute(
            "SELECT document_json FROM decision_operations WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        assert row is not None
        document = json.loads(str(row["document_json"]))
        document["session_id"] = "different-session"
        document_json = json.dumps(document, sort_keys=True, separators=(",", ":"))
        store.connection.execute(
            "UPDATE decision_operations SET session_id = ?, document_json = ?, "
            "checksum = ? WHERE operation_key = ?",
            (
                "different-session",
                document_json,
                hashlib.sha256(document_json.encode()).hexdigest(),
                operation_key,
            ),
        )

        with pytest.raises(RevisionChecksumError, match=operation_key):
            store.claim_decision_operation(
                scope="fresh_session",
                session_id=decision.session_id,
                operation_id=None,
                task_index=None,
                facts_hash=decision.task_facts_hash,
                lease_seconds=1.0,
            )


def test_operation_audit_timestamps_are_covered_by_integrity_document(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        operation_key = f"fresh:{decision.session_id}"
        store.connection.execute(
            "UPDATE decision_operations SET claimed_at = 'raw prose', "
            "updated_at = 'other raw prose' WHERE operation_key = ?",
            (operation_key,),
        )

        with pytest.raises(RevisionChecksumError, match=operation_key):
            store.claim_decision_operation(
                scope=decision.scope,
                session_id=decision.session_id,
                operation_id=decision.operation_id,
                task_index=decision.task_index,
                facts_hash=decision.task_facts_hash,
                lease_seconds=1.0,
            )


@pytest.mark.parametrize(
    "task_index",
    [False, 1.0, -1, MAX_TASK_INDEX + 1],
    ids=["bool", "float", "negative", "too-large"],
)
def test_claim_rejects_noncanonical_delegation_task_index_without_writes(
    isolated_home: Path,
    task_index: object,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        with pytest.raises(ValueError, match="task_index"):
            store.claim_decision_operation(
                scope="delegation",
                session_id="child-session",
                operation_id="operation-1",
                task_index=task_index,  # type: ignore[arg-type]
                facts_hash="a" * 64,
                lease_seconds=1.0,
            )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM decision_operations"
            ).fetchone()[0]
            == 0
        )


def test_commit_requires_strict_create_epoch_bool_without_writes(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        with pytest.raises(ValueError, match="create_epoch"):
            store.commit_decision(
                _decision(),
                candidates=(_candidate(),),
                create_epoch="false",  # type: ignore[arg-type]
            )
        assert store.count_decisions() == 0


@pytest.mark.parametrize("expected_epoch", [False, 0.0], ids=["bool", "float"])
def test_start_route_epoch_requires_strict_expected_epoch_int(
    isolated_home: Path,
    expected_epoch: object,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        with pytest.raises(ValueError, match="expected_epoch"):
            store.start_route_epoch(
                session_id=decision.session_id,
                decision_id=decision.decision_id,
                runtime_id="e" * 64,
                reason_code="pre_call_fallback",
                started_at="2026-01-01T00:00:01Z",
                expected_epoch=expected_epoch,  # type: ignore[arg-type]
            )
        assert len(store.read_route_epochs(decision.session_id)) == 1


@pytest.mark.parametrize("lease_seconds", [True, "1"], ids=["bool", "string"])
def test_claim_requires_strict_numeric_lease_without_writes(
    isolated_home: Path,
    lease_seconds: object,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        with pytest.raises(ValueError, match="lease_seconds"):
            store.claim_decision_operation(
                scope="fresh_session",
                session_id="strict-lease-session",
                operation_id=None,
                task_index=None,
                facts_hash="a" * 64,
                lease_seconds=lease_seconds,  # type: ignore[arg-type]
            )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM decision_operations"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("timeout_seconds", [True, "1"], ids=["bool", "string"])
def test_wait_requires_strict_numeric_timeout(
    isolated_home: Path,
    timeout_seconds: object,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        claim = store.claim_decision_operation(
            scope="fresh_session",
            session_id="strict-timeout-session",
            operation_id=None,
            task_index=None,
            facts_hash="a" * 64,
            lease_seconds=1.0,
        )
        with pytest.raises(ValueError, match="timeout_seconds"):
            store.wait_for_decision_operation(
                claim,
                timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
            )


def test_same_runtime_can_be_a_candidate_for_multiple_profiles(
    isolated_home: Path,
) -> None:
    del isolated_home
    candidates = (
        _candidate(profile_id="coding"),
        _candidate(profile_id="quality", score=0.9),
    )
    with RoutingStore.open() as store:
        commit = store.commit_decision(
            _decision(),
            candidates=candidates,
            create_epoch=True,
        )

        assert commit.candidates == candidates
        assert len({candidate.candidate_id for candidate in candidates}) == 2
        assert len({candidate.runtime_id for candidate in candidates}) == 1


def test_candidate_bundle_references_and_size_are_bounded(
    isolated_home: Path,
) -> None:
    del isolated_home
    decision = _decision()
    selected_runtime_id = decision.selected_runtime.stable_id()
    missing = RoutingDecision.model_validate({
        **decision.model_dump(mode="json"),
        "eligible_candidates": [selected_runtime_id, "f" * 64],
        "final_scores": [
            [selected_runtime_id, 0.8],
            ["f" * 64, 0.7],
        ],
    })
    with RoutingStore.open() as store:
        with pytest.raises(ValueError, match="eligible candidate references"):
            store.commit_decision(
                missing,
                candidates=(_candidate(),),
                create_epoch=True,
            )

        oversized = tuple(
            _candidate(profile_id=f"profile_{index}") for index in range(1_025)
        )
        with pytest.raises(ValueError, match="cannot exceed"):
            store.commit_decision(
                decision,
                candidates=oversized,
                create_epoch=True,
            )


def test_ranked_selection_requires_selected_runtime_profile_and_score_candidate(
    isolated_home: Path,
) -> None:
    del isolated_home
    selected_elsewhere = _runtime(model="model-b")
    incoherent_runtime = _decision().model_copy(
        update={"selected_runtime": selected_elsewhere}
    )
    with RoutingStore.open() as store:
        with pytest.raises(ValueError, match="selected runtime"):
            store.commit_decision(
                incoherent_runtime,
                candidates=(_candidate(),),
                create_epoch=True,
            )
        assert store.count_decisions() == 0

        with pytest.raises(ValueError, match="selected profile"):
            store.commit_decision(
                _decision(),
                candidates=(_candidate(profile_id="quality"),),
                create_epoch=True,
            )
        assert store.count_decisions() == 0


def test_assessment_free_safe_default_can_commit_without_ranked_candidates(
    isolated_home: Path,
) -> None:
    del isolated_home
    document = _decision().model_dump(mode="json")
    document.update({
        "applied_rule_ids": [],
        "assessment": None,
        "eligible_candidates": [],
        "rejected_candidates": [],
        "final_scores": [],
        "selected_profile_id": None,
        "selection_reason": "classifier_failed",
        "classifier_runtime_id": None,
        "classifier_input_tokens": 0,
        "classifier_output_tokens": 0,
        "classifier_cost_usd": None,
        "safe_default_reason": "classifier_failed",
    })
    decision = RoutingDecision.model_validate(document)

    with RoutingStore.open() as store:
        commit = store.commit_decision(
            decision,
            candidates=(),
            create_epoch=True,
        )
        assert commit.binding.runtime_id == decision.safe_default_runtime.stable_id()
        assert commit.candidates == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("eligible", 1),
        ("final_score", True),
        ("final_score", float("nan")),
        ("final_score", float("inf")),
        ("normalized_scoring_inputs", (("quality", True),)),
        ("reason_codes", ("raw_task_prose",)),
    ],
)
def test_decision_candidate_rejects_coercion_and_untyped_reasons(
    field: str,
    value: object,
) -> None:
    document = _candidate().model_dump(mode="json")
    document[field] = value
    with pytest.raises(Exception):
        DecisionCandidate.model_validate(document)


def test_public_durable_records_enforce_runtime_types_and_coherence(
    isolated_home: Path,
) -> None:
    del isolated_home
    epoch = RouteEpoch(
        route_epoch_id="a" * 64,
        session_id="session-1",
        decision_id="decision-1",
        epoch_number=0,
        runtime_id="b" * 64,
        reason_code="initial_route",
        started_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(ValueError):
        replace(epoch, epoch_number=True)
    with pytest.raises(ValueError):
        replace(epoch, provider_started=1)
    with pytest.raises(ValueError):
        replace(epoch, started_at="raw task prose")

    binding = SessionRouteBinding(
        session_id="manual-session",
        binding_kind="manual",
        projection_mode="manual",
        decision_id=None,
        runtime_id="b" * 64,
        manual_pin_source="cli:model",
        current_epoch=-1,
        continuation_root=None,
        parent_session_id=None,
        continuation_reason=None,
        created_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(ValueError):
        replace(binding, binding_kind="invalid")
    with pytest.raises(ValueError):
        replace(binding, projection_mode="shadow")
    with pytest.raises(ValueError):
        replace(binding, current_epoch=True)
    with pytest.raises(ValueError):
        replace(binding, current_epoch=0)

    receipt = ActivationReceipt(
        receipt_id="receipt-typed",
        authority_id="authority-1",
        config_sha="c" * 64,
        inventory_contract_sha="d" * 64,
        inventory_revision="inventory-1",
        adapter_capability_sha="e" * 64,
        created_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(ValueError):
        replace(receipt, created_at="raw task prose")

    claim = DecisionOperationClaim(
        operation_key="fresh:session-1",
        claim_id="f" * 32,
        scope="fresh_session",
        session_id="session-1",
        operation_id=None,
        task_index=None,
        facts_hash="a" * 64,
        owner_pid=1,
        owner_start_token="1.0",
        lease_expires_at=1.0,
        status="claimed",
    )
    with pytest.raises(ValueError):
        replace(claim, status="complete")
    with pytest.raises(ValueError):
        replace(claim, owner_pid=True)
    with pytest.raises(ValueError):
        replace(claim, lease_expires_at=float("inf"))
    with pytest.raises(ValueError):
        replace(claim, task_index=0)

    with RoutingStore.open() as store:
        commit = store.commit_decision(
            _decision(),
            candidates=(_candidate(),),
            create_epoch=True,
        )
        with pytest.raises(ValueError):
            replace(commit, status="invalid")
        with pytest.raises(ValueError):
            DecisionCommit(
                decision=commit.decision,
                candidates=commit.candidates,
                binding=binding,
                epoch=commit.epoch,
                status="computed",
            )
        with pytest.raises(ValueError):
            replace(commit, epoch=None)
        with pytest.raises(ValueError):
            replace(
                commit,
                epoch=replace(commit.epoch, epoch_number=1),
            )
        with pytest.raises(ValueError):
            replace(
                commit,
                binding=replace(commit.binding, current_epoch=-1),
            )


def test_durable_ids_match_task1_256_character_plus_contract(
    isolated_home: Path,
) -> None:
    del isolated_home
    identifier = "+" * 256
    decision = RoutingDecision.model_validate({
        **_decision().model_dump(mode="json"),
        "decision_id": identifier,
        "session_id": identifier,
        "task_id": identifier,
    })
    with RoutingStore.open() as store:
        committed = store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        assert committed.decision.session_id == identifier

    with pytest.raises(Exception):
        RoutingDecision.model_validate({
            **_decision().model_dump(mode="json"),
            "session_id": "+" * 257,
        })


@pytest.mark.parametrize(
    "identifier",
    ["../private/session", "ghp_0123456789ABCDEF"],
    ids=["path", "secret"],
)
def test_durable_ids_preserve_task1_path_and_secret_guards(
    isolated_home: Path,
    identifier: str,
) -> None:
    del isolated_home
    decision = RoutingDecision.model_validate({
        **_decision().model_dump(mode="json"),
        "session_id": identifier,
    })

    with RoutingStore.open() as store:
        with pytest.raises(UnsafeStoredContent, match="identity"):
            store.commit_decision(
                decision,
                candidates=(_candidate(),),
                create_epoch=True,
            )


@pytest.mark.parametrize(
    "failing_table",
    ["decision_candidates", "session_route_bindings", "route_epochs"],
)
def test_commit_failure_rolls_back_complete_decision_bundle(
    isolated_home: Path,
    failing_table: str,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        store.connection.execute(
            f"CREATE TRIGGER fail_bundle BEFORE INSERT ON {failing_table} "
            "BEGIN SELECT RAISE(ABORT, 'fault injection'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="fault injection"):
            store.commit_decision(
                _decision(),
                candidates=(_candidate(),),
                create_epoch=True,
            )

        assert store.count_decisions() == 0
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM decision_candidates"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM session_route_bindings"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.connection.execute("SELECT COUNT(*) FROM route_epochs").fetchone()[0]
            == 0
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM decision_operations"
            ).fetchone()[0]
            == 0
        )


def test_later_candidate_insert_failure_rolls_back_earlier_bundle_rows(
    isolated_home: Path,
) -> None:
    del isolated_home
    candidates = (
        _candidate(profile_id="coding"),
        _candidate(profile_id="quality", score=0.9),
    )
    with RoutingStore.open() as store:
        store.connection.execute(
            "CREATE TRIGGER fail_second_candidate BEFORE INSERT ON decision_candidates "
            "WHEN NEW.ordinal = 1 BEGIN SELECT RAISE(ABORT, 'later candidate'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="later candidate"):
            store.commit_decision(
                _decision(),
                candidates=candidates,
                create_epoch=True,
            )

        for table in (
            "routing_decisions",
            "decision_candidates",
            "session_route_bindings",
            "route_epochs",
            "decision_operations",
        ):
            assert (
                store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                == 0
            )


def test_operation_completion_failure_preserves_claim_and_retry_commits(
    isolated_home: Path,
) -> None:
    del isolated_home
    decision = _decision()
    candidates = (_candidate(),)
    with RoutingStore.open() as store:
        claim = store.claim_decision_operation(
            scope=decision.scope,
            session_id=decision.session_id,
            operation_id=decision.operation_id,
            task_index=decision.task_index,
            facts_hash=decision.task_facts_hash,
            lease_seconds=5.0,
        )
        store.connection.execute(
            "CREATE TRIGGER fail_operation_completion "
            "BEFORE UPDATE OF status ON decision_operations "
            "WHEN NEW.status = 'complete' "
            "BEGIN SELECT RAISE(ABORT, 'operation completion'); END"
        )

        with pytest.raises(sqlite3.IntegrityError, match="operation completion"):
            store.commit_decision(
                decision,
                candidates=candidates,
                create_epoch=True,
                claim=claim,
            )

        for table in (
            "routing_decisions",
            "decision_candidates",
            "session_route_bindings",
            "route_epochs",
        ):
            assert (
                store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                == 0
            )
        operation = store.connection.execute(
            "SELECT * FROM decision_operations WHERE operation_key = ?",
            (claim.operation_key,),
        ).fetchone()
        assert operation is not None
        assert operation["status"] == "claimed"
        assert operation["decision_id"] is None

        store.connection.execute("DROP TRIGGER fail_operation_completion")
        committed = store.commit_decision(
            decision,
            candidates=candidates,
            create_epoch=True,
            claim=claim,
        )
        assert committed.status == "computed"
        assert store.count_decisions() == 1


def test_decision_and_candidate_checksums_are_verified_on_read(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        store.connection.execute(
            "UPDATE routing_decisions SET document_json = '{}' WHERE decision_id = ?",
            (decision.decision_id,),
        )
        with pytest.raises(RevisionChecksumError, match=decision.decision_id):
            store.read_decision(decision.decision_id)

    with RoutingStore.open() as store:
        decision = _decision(
            decision_id="decision-candidate", session_id="session-candidate"
        )
        candidate = _candidate()
        store.commit_decision(decision, candidates=(candidate,), create_epoch=True)
        store.connection.execute(
            "UPDATE decision_candidates SET document_json = '{}' "
            "WHERE decision_id = ? AND candidate_id = ?",
            (decision.decision_id, candidate.candidate_id),
        )
        with pytest.raises(RevisionChecksumError, match=candidate.candidate_id):
            store.commit_decision(
                decision,
                candidates=(candidate,),
                create_epoch=True,
            )


def test_decision_and_candidate_row_identity_must_match_document(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        candidate = _candidate()
        store.commit_decision(
            decision,
            candidates=(candidate,),
            create_epoch=True,
        )
        tampered_candidate_id = "0" * 64
        assert tampered_candidate_id != candidate.candidate_id
        store.connection.execute(
            "UPDATE decision_candidates SET candidate_id = ? "
            "WHERE decision_id = ? AND candidate_id = ?",
            (tampered_candidate_id, decision.decision_id, candidate.candidate_id),
        )
        with pytest.raises(RevisionChecksumError, match=tampered_candidate_id):
            store.read_decision(decision.decision_id)

    with RoutingStore.open() as store:
        decision = _decision(
            decision_id="decision-primary-identity",
            session_id="session-primary-identity",
        )
        store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        tampered_decision_id = "decision-primary-tampered"
        store.connection.execute("PRAGMA foreign_keys = OFF")
        store.connection.execute(
            "UPDATE routing_decisions SET decision_id = ? WHERE decision_id = ?",
            (tampered_decision_id, decision.decision_id),
        )
        with pytest.raises(RevisionChecksumError, match=tampered_decision_id):
            store.read_decision(tampered_decision_id)


def test_read_rejects_canonical_candidate_profile_splice(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        original = _candidate()
        store.commit_decision(
            decision,
            candidates=(original,),
            create_epoch=True,
        )

        spliced = _candidate(profile_id="quality")
        document_json = json.dumps(
            spliced.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        bundle_json = json.dumps(
            [spliced.model_dump(mode="json")],
            sort_keys=True,
            separators=(",", ":"),
        )
        store.connection.execute(
            "UPDATE decision_candidates SET candidate_id = ?, profile_id = ?, "
            "document_json = ?, checksum = ? "
            "WHERE decision_id = ? AND candidate_id = ?",
            (
                spliced.candidate_id,
                spliced.profile_id,
                document_json,
                hashlib.sha256(document_json.encode()).hexdigest(),
                decision.decision_id,
                original.candidate_id,
            ),
        )
        store.connection.execute(
            "UPDATE routing_decisions SET candidate_bundle_checksum = ? "
            "WHERE decision_id = ?",
            (
                hashlib.sha256(bundle_json.encode()).hexdigest(),
                decision.decision_id,
            ),
        )

        with pytest.raises(RevisionChecksumError, match=decision.decision_id):
            store.read_decision(decision.decision_id)


def test_null_candidate_bundle_checksum_cannot_hide_deleted_candidates(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        store.connection.execute(
            "DELETE FROM decision_candidates WHERE decision_id = ?",
            (decision.decision_id,),
        )
        store.connection.execute(
            "UPDATE routing_decisions SET candidate_bundle_checksum = NULL "
            "WHERE decision_id = ?",
            (decision.decision_id,),
        )

        with pytest.raises(RevisionChecksumError, match=decision.decision_id):
            store.read_decision(decision.decision_id)


def test_decision_writer_rejects_secret_material_but_accepts_exact_token_counts(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        secret = RoutingDecision.model_validate({
            **_decision().model_dump(mode="json"),
            "decision_id": "sk-proj-abcdefghijklmnop",
        })
        with pytest.raises(UnsafeStoredContent):
            store.commit_decision(
                secret,
                candidates=(_candidate(),),
                create_epoch=True,
            )

        valid = _decision(
            decision_id="decision-counts",
            session_id="session-counts",
        )
        assert (
            store.commit_decision(
                valid,
                candidates=(_candidate(),),
                create_epoch=True,
            ).decision.classifier_output_tokens
            == 5
        )


def test_classifier_token_aggregates_reject_bool_and_arbitrary_tokens_field() -> None:
    document = _decision().model_dump(mode="json")
    document["classifier_input_tokens"] = True
    with pytest.raises(Exception):
        RoutingDecision.model_validate(document)

    document = _candidate().model_dump(mode="json")
    document["tokens"] = 4
    with pytest.raises(Exception):
        DecisionCandidate.model_validate(document)


def test_activation_receipt_round_trip_and_active_decision_requires_exact_match(
    isolated_home: Path,
) -> None:
    del isolated_home
    receipt = ActivationReceipt(
        receipt_id="receipt-1",
        authority_id="authority-1",
        config_sha="b" * 64,
        inventory_contract_sha="c" * 64,
        inventory_revision="inventory-1",
        adapter_capability_sha="d" * 64,
        created_at="2026-01-01T00:00:00Z",
    )
    with RoutingStore.open() as store:
        assert store.write_activation_receipt(receipt) == receipt
        assert store.write_activation_receipt(receipt) == receipt
        assert (
            store.read_matching_activation_receipt(
                authority_id="authority-1",
                config_sha="b" * 64,
                adapter_capability_sha="d" * 64,
            )
            == receipt
        )
        assert (
            store.read_matching_activation_receipt(
                authority_id="authority-1",
                config_sha="e" * 64,
                adapter_capability_sha="d" * 64,
            )
            is None
        )

        active = _decision(
            decision_id="decision-active",
            session_id="session-active",
            projection_mode="active",
            activation_receipt_id=receipt.receipt_id,
            activation_config_sha=receipt.config_sha,
            adapter_capability_sha=receipt.adapter_capability_sha,
        )
        assert (
            store.commit_decision(
                active,
                candidates=(_candidate(),),
                create_epoch=True,
            ).decision.activation_receipt_id
            == receipt.receipt_id
        )

        invalid = _decision(
            decision_id="decision-invalid-active",
            session_id="session-invalid-active",
            projection_mode="active",
            activation_receipt_id=receipt.receipt_id,
            activation_config_sha="e" * 64,
            adapter_capability_sha=receipt.adapter_capability_sha,
        )
        with pytest.raises(ImmutableRecordConflict, match="activation receipt"):
            store.commit_decision(
                invalid,
                candidates=(_candidate(),),
                create_epoch=True,
            )


def test_activation_receipt_is_immutable_checksum_verified_and_secret_free(
    isolated_home: Path,
) -> None:
    del isolated_home
    receipt = ActivationReceipt(
        receipt_id="receipt-safe",
        authority_id="authority-1",
        config_sha="b" * 64,
        inventory_contract_sha="c" * 64,
        inventory_revision="inventory-1",
        adapter_capability_sha="d" * 64,
        created_at="2026-01-01T00:00:00Z",
    )
    with RoutingStore.open() as store:
        store.write_activation_receipt(receipt)
        changed = replace(receipt, inventory_revision="inventory-2")
        with pytest.raises(ImmutableRecordConflict):
            store.write_activation_receipt(changed)

        store.connection.execute(
            "UPDATE activation_receipts SET document_json = '{}' WHERE receipt_id = ?",
            (receipt.receipt_id,),
        )
        with pytest.raises(RevisionChecksumError, match=receipt.receipt_id):
            store.read_matching_activation_receipt(
                authority_id=receipt.authority_id,
                config_sha=receipt.config_sha,
                adapter_capability_sha=receipt.adapter_capability_sha,
            )


def test_activation_receipt_rejects_checksummed_extra_document_fields(
    isolated_home: Path,
) -> None:
    del isolated_home
    receipt = ActivationReceipt(
        receipt_id="receipt-extra",
        authority_id="authority-1",
        config_sha="b" * 64,
        inventory_contract_sha="c" * 64,
        inventory_revision="inventory-1",
        adapter_capability_sha="d" * 64,
        created_at="2026-01-01T00:00:00Z",
    )
    with RoutingStore.open() as store:
        store.write_activation_receipt(receipt)
        row = store.connection.execute(
            "SELECT document_json FROM activation_receipts WHERE receipt_id = ?",
            (receipt.receipt_id,),
        ).fetchone()
        document = json.loads(str(row["document_json"]))
        document["note"] = "raw task prose must not survive"
        document_json = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
        )
        store.connection.execute(
            "UPDATE activation_receipts SET document_json = ?, checksum = ? "
            "WHERE receipt_id = ?",
            (
                document_json,
                hashlib.sha256(document_json.encode()).hexdigest(),
                receipt.receipt_id,
            ),
        )

        with pytest.raises(RevisionChecksumError, match=receipt.receipt_id):
            store.read_matching_activation_receipt(
                authority_id=receipt.authority_id,
                config_sha=receipt.config_sha,
                adapter_capability_sha=receipt.adapter_capability_sha,
            )


def test_manual_and_compression_bindings_preserve_authoritative_intent(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        routed = store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        ).binding
        first = store.bind_session_continuation(
            routed.session_id,
            "compressed-routed",
            reason="compression",
            created_at="2026-01-01T00:01:00Z",
        )
        second = store.bind_session_continuation(
            routed.session_id,
            "compressed-routed",
            reason="compression",
            created_at="2026-01-01T00:02:00Z",
        )
        assert first == second
        assert second.created_at == "2026-01-01T00:01:00Z"
        assert first.authoritative_intent == routed.authoritative_intent
        assert first.current_epoch == routed.current_epoch
        assert first.continuation_root == routed.session_id
        child_epochs = store.read_route_epochs(first.session_id)
        assert len(child_epochs) == 1
        assert child_epochs[0].epoch_number == routed.current_epoch
        assert child_epochs[0].reason_code == "compression_continuation"
        assert store.count_decisions() == 1

        manual = store.record_manual_pin(
            "manual-parent",
            _runtime().stable_id(),
            "cli:model",
            "2026-01-01T00:00:00Z",
        )
        child = store.bind_session_continuation(
            manual.session_id,
            "compressed-manual",
            reason="compression",
            created_at="2026-01-01T00:01:00Z",
        )
        assert child.authoritative_intent == manual.authoritative_intent
        assert child.manual_pin_source == "cli:model"
        assert store.read_route_epochs(child.session_id) == ()

        with pytest.raises(ImmutableRecordConflict):
            store.bind_session_continuation(
                routed.session_id,
                "compressed-manual",
                reason="compression",
                created_at="2026-01-01T00:01:00Z",
            )
        with pytest.raises(ValueError, match="itself"):
            store.bind_session_continuation(
                routed.session_id,
                routed.session_id,
                reason="compression",
                created_at="2026-01-01T00:01:00Z",
            )


def test_routed_continuation_marks_transitions_repairs_and_chains(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        parent = store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        ).binding
        child = store.bind_session_continuation(
            parent.session_id,
            "compressed-child",
            reason="compression",
            created_at="2026-01-01T00:01:00Z",
        )

        marked = store.mark_route_epoch_provider_started(
            child.session_id,
            decision_id=decision.decision_id,
            runtime_id=child.runtime_id,
            api_request_id="child-request-1",
            started_at="2026-01-01T00:01:01Z",
        )
        assert marked.provider_started is True
        assert (
            store.mark_route_epoch_provider_started(
                child.session_id,
                decision_id=decision.decision_id,
                runtime_id=child.runtime_id,
                api_request_id="child-later-turn-request",
                started_at="2026-01-01T00:01:02Z",
            )
            == marked
        )

        fallback_runtime_id = "e" * 64
        transitioned = store.start_route_epoch(
            session_id=child.session_id,
            decision_id=decision.decision_id,
            runtime_id=fallback_runtime_id,
            reason_code="pre_call_fallback",
            started_at="2026-01-01T00:01:03Z",
            expected_epoch=child.current_epoch,
        )
        assert transitioned.epoch_number == child.current_epoch + 1
        repaired = store.bind_session_continuation(
            parent.session_id,
            child.session_id,
            reason="compression",
            created_at="2026-01-01T00:02:00Z",
        )
        assert repaired.current_epoch == transitioned.epoch_number
        assert repaired.runtime_id == fallback_runtime_id

        grandchild = store.bind_session_continuation(
            child.session_id,
            "compressed-grandchild",
            reason="compression",
            created_at="2026-01-01T00:03:00Z",
        )
        assert grandchild.continuation_root == parent.session_id
        assert grandchild.parent_session_id == child.session_id
        assert grandchild.current_epoch == transitioned.epoch_number
        grandchild_epochs = store.read_route_epochs(grandchild.session_id)
        assert tuple(epoch.epoch_number for epoch in grandchild_epochs) == (
            transitioned.epoch_number,
        )
        assert grandchild_epochs[0].runtime_id == fallback_runtime_id
        assert grandchild_epochs[0].reason_code == "compression_continuation"
        assert store.count_decisions() == 1


def test_route_epoch_compare_and_swap_and_provider_marker_are_idempotent(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        epoch = store.start_route_epoch(
            session_id=decision.session_id,
            decision_id=decision.decision_id,
            runtime_id=decision.selected_runtime.stable_id(),
            reason_code="pre_call_fallback",
            started_at="2026-01-01T00:00:01Z",
            expected_epoch=0,
        )
        assert epoch.epoch_number == 1
        with pytest.raises(ImmutableRecordConflict, match="expected epoch"):
            store.start_route_epoch(
                session_id=decision.session_id,
                decision_id=decision.decision_id,
                runtime_id=decision.selected_runtime.stable_id(),
                reason_code="another",
                started_at="2026-01-01T00:00:02Z",
                expected_epoch=0,
            )

        marked = store.mark_route_epoch_provider_started(
            decision.session_id,
            decision_id=decision.decision_id,
            runtime_id=decision.selected_runtime.stable_id(),
            api_request_id="request-1",
            started_at="2026-01-01T00:00:02Z",
        )
        assert marked.provider_started is True
        assert (
            store.mark_route_epoch_provider_started(
                decision.session_id,
                decision_id=decision.decision_id,
                runtime_id=decision.selected_runtime.stable_id(),
                api_request_id="request-1",
                started_at="2026-01-01T00:00:02Z",
            )
            == marked
        )
        assert (
            store.mark_route_epoch_provider_started(
                decision.session_id,
                decision_id=decision.decision_id,
                runtime_id=decision.selected_runtime.stable_id(),
                api_request_id="request-2",
                started_at="2026-01-01T00:00:03Z",
            )
            == marked
        )


def test_exact_decision_replay_returns_current_epoch_after_route_transition(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        candidates = (_candidate(),)
        first = store.commit_decision(
            decision,
            candidates=candidates,
            create_epoch=True,
        )
        fallback_runtime_id = "e" * 64
        assert fallback_runtime_id != first.binding.runtime_id
        transitioned = store.start_route_epoch(
            session_id=decision.session_id,
            decision_id=decision.decision_id,
            runtime_id=fallback_runtime_id,
            reason_code="pre_call_fallback",
            started_at="2026-01-01T00:00:01Z",
            expected_epoch=0,
        )

        replayed = store.commit_decision(
            decision,
            candidates=candidates,
            create_epoch=True,
        )

        assert replayed.status == "replayed"
        assert replayed.binding.current_epoch == transitioned.epoch_number
        assert replayed.binding.runtime_id == transitioned.runtime_id
        assert replayed.epoch == transitioned


def test_store_is_profile_local_wal_and_revision_atomic(isolated_home: Path) -> None:
    store = RoutingStore.open()
    try:
        assert store.path == isolated_home / "auto-routing" / "state.db"
        assert store.connection.isolation_level is None
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[
            0
        ].lower() in {
            "wal",
            "delete",
        }

        revision = store.build_baseline_revision(
            authority_id="a1",
            overlay={"profiles": {}},
        )
        store.publish_revision(revision, expected_active_id=None)

        assert store.read_active_revision("a1") == revision
    finally:
        store.close()


def test_connect_and_init_db_create_the_declarative_schema(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.db"
    connection = connect(path)
    try:
        init_db(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert EXPECTED_TABLES <= tables
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    finally:
        connection.close()


def test_schema_contains_no_raw_content_or_credential_columns(
    isolated_home: Path,
) -> None:
    store = RoutingStore.open()
    try:
        columns = {
            row[1].lower()
            for table in EXPECTED_TABLES
            for row in store.connection.execute(f"PRAGMA table_info({table})")
        }
        forbidden = {
            "prompt",
            "response",
            "secret",
            "api_key",
            "access_token",
            "refresh_token",
            "raw_endpoint",
        }
        assert columns.isdisjoint(forbidden)
    finally:
        store.close()


@pytest.mark.parametrize(
    "unsafe_document",
    [
        {"prompt": "private task text"},
        {"nested": {"response": "private model output"}},
        {"api_key": "not-a-real-key"},
        {"access_token": "not-a-real-token"},
        {"token": "not-a-real-token"},
        {"key": "not-a-real-key"},
        {"credentials": {"provider": "provider-a"}},
        {"credential_pool": "pool-a"},
        {"nested": {"token": "not-a-real-token"}},
        {"nested": {"credentials": {"provider": "provider-a"}}},
        {"nested": {"key": "not-a-real-key"}},
        {"nested": {"credential_pool": "pool-a"}},
        {"base_url": "https://private.invalid/v1"},
        {"endpoint": "https://private.invalid/v1"},
        {"metadata": {"location": "https://private.invalid/v1"}},
        {"metadata": {"value": "sk-proj-not-a-real-api-key"}},
        {
            "metadata": {
                "value": ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature123")
            }
        },
        {"metadata": {"value": "Bearer header.payload.signature"}},
        {"apiKey": "not-a-real-key"},
        {"access-token": "not-a-real-token"},
        {"credentialPool": "pool-a"},
        {"baseUrl": "https://private.invalid/v1"},
        {"raw_endpoint": "https://private.invalid/v1"},
        {"secret": "not-a-real-secret"},
    ],
)
def test_generic_revision_writer_rejects_raw_content_and_credentials(
    isolated_home: Path,
    unsafe_document: dict[str, object],
) -> None:
    store = RoutingStore.open()
    try:
        with pytest.raises(UnsafeStoredContent):
            store.write_authority_revision("authority-unsafe", unsafe_document)
        assert store.read_authority_revision("authority-unsafe") is None
    finally:
        store.close()


def test_authority_writer_accepts_named_and_fingerprinted_identities(
    isolated_home: Path,
) -> None:
    store = RoutingStore.open()
    document = {
        "runtime": {
            "auth_identity": "configured:work",
            "credential_pool_identity": "pool:work",
            "endpoint_identity": "endpoint:local",
            "runtime_id": "a" * 64,
        },
        "description": "Bearer token handling benchmark",
        "minimum_context_tokens": 4096,
    }
    try:
        written = store.write_authority_revision("authority-safe", document)

        assert json.loads(written.document_json) == document
        assert store.read_authority_revision("authority-safe") == written
    finally:
        store.close()


def test_authority_revision_is_canonical_immutable_and_checksum_verified(
    isolated_home: Path,
) -> None:
    store = RoutingStore.open()
    try:
        first = store.write_authority_revision(
            "authority-1",
            {"z": [2, 1], "profiles": {"coding": {"quality": 1.0}}},
            created_at="2026-01-01T00:00:00Z",
        )
        duplicate = store.write_authority_revision(
            "authority-1",
            {"profiles": {"coding": {"quality": 1.0}}, "z": [2, 1]},
            created_at="2026-01-01T00:00:00Z",
        )

        assert duplicate == first
        assert first.document_json == (
            '{"profiles":{"coding":{"quality":1.0}},"z":[2,1]}'
        )
        assert (
            first.checksum == hashlib.sha256(first.document_json.encode()).hexdigest()
        )
        assert store.read_authority_revision("authority-1") == first

        with pytest.raises(ImmutableRecordConflict):
            store.write_authority_revision(
                "authority-1",
                {"profiles": {"coding": {"quality": 0.5}}},
                created_at="2026-01-01T00:00:00Z",
            )

        store.connection.execute(
            "UPDATE authority_revisions SET document_json = ? WHERE authority_id = ?",
            ('{"tampered":true}', "authority-1"),
        )
        with pytest.raises(RevisionChecksumError, match="authority-1"):
            store.read_authority_revision("authority-1")
    finally:
        store.close()


def test_inventory_and_catalog_snapshots_round_trip_as_immutable_records(
    isolated_home: Path,
) -> None:
    store = RoutingStore.open()
    try:
        inventory = store.write_inventory_snapshot(
            "inventory-1",
            [_observation()],
            created_at="2026-01-01T00:00:00Z",
        )
        catalog = store.write_catalog_snapshot(
            "catalog-1",
            [_evidence()],
            created_at="2026-01-01T00:00:00Z",
        )

        assert store.read_inventory_snapshot("inventory-1") == inventory
        assert store.read_catalog_snapshot("catalog-1") == catalog
        assert inventory.observations == (_observation(),)
        assert catalog.evidence == (_evidence(),)
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM inventory_observations WHERE snapshot_id = ?",
                ("inventory-1",),
            ).fetchone()[0]
            == 1
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM catalog_evidence WHERE snapshot_id = ?",
                ("catalog-1",),
            ).fetchone()[0]
            == 1
        )

        assert (
            store.write_inventory_snapshot(
                "inventory-1",
                [_observation()],
                created_at="2026-01-01T00:00:00Z",
            )
            == inventory
        )
        with pytest.raises(ImmutableRecordConflict):
            store.write_inventory_snapshot(
                "inventory-1",
                [],
                created_at="2026-01-01T00:00:00Z",
            )
    finally:
        store.close()


def test_catalog_snapshot_round_trips_duplicate_evidence_with_exact_scopes(
    isolated_home: Path,
) -> None:
    evidence = _evidence().model_copy(update={"model_version": "model-a"})
    records = tuple(
        StoredCatalogRecord(
            evidence=evidence,
            applicability=CatalogApplicability(
                canonical_provider="provider-a",
                canonical_model="model-a",
                canonical_version="model-a",
                runtime_id=runtime_id,
            ),
        )
        for runtime_id in ("a" * 64, "b" * 64)
    )
    with RoutingStore.open() as store:
        written = store.write_catalog_snapshot(
            "catalog-scoped",
            records,
            created_at="2026-01-01T00:00:00Z",
        )

        assert {record.applicability.runtime_id for record in written.records} == {
            "a" * 64,
            "b" * 64,
        }
        assert written.evidence == (evidence, evidence)
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM catalog_evidence WHERE snapshot_id = ?",
                ("catalog-scoped",),
            ).fetchone()[0]
            == 2
        )

    with RoutingStore.open() as reopened:
        restored = reopened.read_catalog_snapshot("catalog-scoped")

        assert restored is not None
        assert restored.records == written.records


def test_inventory_writer_rejects_raw_endpoint_values(isolated_home: Path) -> None:
    store = RoutingStore.open()
    try:
        observation = _observation()
        raw_endpoint = observation.model_copy(
            update={
                "key": observation.key.model_copy(
                    update={"endpoint_identity": "https://private.invalid/v1"}
                )
            }
        )

        with pytest.raises(UnsafeStoredContent, match="endpoint"):
            store.write_inventory_snapshot("inventory-1", [raw_endpoint])

        assert store.read_inventory_snapshot("inventory-1") is None
    finally:
        store.close()


@pytest.mark.parametrize(
    ("field", "secret_value"),
    [
        ("auth_identity", "Bearer header.payload.signature"),
        (
            "auth_identity",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature123",
        ),
        ("auth_identity", "sk-proj-not-a-real-api-key"),
        (
            "auth_identity",
            "named:sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        ),
        ("auth_identity", "not-a-real-token"),
        ("credential_pool_identity", "ghp_notarealgithubtoken"),
        ("credential_pool_identity", "xoxb-not-a-real-slack-token"),
        ("endpoint_identity", "api.private.invalid/v1"),
        ("auth_identity", "user@example.com"),
    ],
)
def test_inventory_writer_rejects_secret_shaped_identity_values(
    isolated_home: Path,
    field: str,
    secret_value: str,
) -> None:
    store = RoutingStore.open()
    try:
        observation = _observation()
        unsafe = observation.model_copy(
            update={"key": observation.key.model_copy(update={field: secret_value})}
        )

        with pytest.raises(UnsafeStoredContent, match="identity"):
            store.write_inventory_snapshot("inventory-1", [unsafe])

        assert store.read_inventory_snapshot("inventory-1") is None
    finally:
        store.close()


@pytest.mark.parametrize("writer", ["authority", "inventory", "catalog", "adaptive"])
@pytest.mark.parametrize(
    "secret",
    [
        "named:sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        ("named:eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature123"),
        "named:-----BEGIN PRIVATE KEY-----",
        f"named:AKIA{'A' * 16}",
        f"named:ASIA{'0A' * 8}",
        f"named:AIza{'A' * 35}",
    ],
    ids=("api-key", "jwt", "pem", "aws-akia", "aws-asia", "google-aiza"),
)
def test_every_json_writer_rejects_embedded_secret_material(
    isolated_home: Path,
    writer: str,
    secret: str,
) -> None:
    store = RoutingStore.open()
    try:
        with pytest.raises(UnsafeStoredContent, match="secret-shaped"):
            if writer == "authority":
                store.write_authority_revision(
                    "authority-embedded-secret",
                    {"metadata": {"label": secret}},
                )
            elif writer == "inventory":
                observation = _observation("inventory-embedded-value").model_copy(
                    update={"provenance": (secret,)}
                )
                store.write_inventory_snapshot(
                    "inventory-embedded-value",
                    [observation],
                )
            elif writer == "catalog":
                evidence = _evidence().model_copy(
                    update={"normalization_method": secret}
                )
                store.write_catalog_snapshot("catalog-embedded-secret", [evidence])
            else:
                revision = _revision("revision-embedded-value").model_copy(
                    update={"overlay": {"metadata": {"label": secret}}}
                )
                store.publish_revision(revision, expected_active_id=None)

        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM authority_revisions"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM inventory_snapshots"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM catalog_snapshots"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM adaptive_revisions"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.close()


def test_json_writers_accept_harmless_natural_language_plan_and_provenance(
    isolated_home: Path,
) -> None:
    store = RoutingStore.open()
    try:
        authority = store.write_authority_revision(
            "authority-natural-language",
            {
                "subscription_plan": "Team annual plan",
                "provenance": "Operator described the token budget in prose",
            },
        )
        observation = _observation("inventory-natural-language").model_copy(
            update={
                "provenance": ("Benchmark provenance described in natural language",)
            }
        )
        inventory = store.write_inventory_snapshot(
            "inventory-natural-language",
            [observation],
        )
        evidence = _evidence().model_copy(
            update={
                "normalization_method": (
                    "Benchmark normalization described in natural language"
                )
            }
        )
        catalog = store.write_catalog_snapshot(
            "catalog-natural-language",
            [evidence],
        )
        revision = _revision("revision-natural-language").model_copy(
            update={
                "overlay": {"note": "Natural language provenance for an annual plan"},
                "explanation": {"reason": "Plan changed after a token budget review"},
            }
        )
        store.publish_revision(revision, expected_active_id=None)

        assert store.read_authority_revision(authority.authority_id) == authority
        assert store.read_inventory_snapshot(inventory.snapshot_id) == inventory
        assert store.read_catalog_snapshot(catalog.snapshot_id) == catalog
        assert store.read_revision(revision.revision_id) == revision
    finally:
        store.close()


@pytest.mark.parametrize("writer", ["authority", "inventory", "catalog", "adaptive"])
def test_every_json_writer_accepts_harmless_asia_prose(
    isolated_home: Path,
    writer: str,
) -> None:
    store = RoutingStore.open()
    prose = "Benchmark data from Asia"
    try:
        if writer == "authority":
            written = store.write_authority_revision(
                "authority-asia-prose",
                {"provenance": prose},
            )
            assert store.read_authority_revision(written.authority_id) == written
        elif writer == "inventory":
            observation = _observation("inventory-asia-prose").model_copy(
                update={"provenance": (prose,)}
            )
            written = store.write_inventory_snapshot(
                "inventory-asia-prose",
                [observation],
            )
            assert store.read_inventory_snapshot(written.snapshot_id) == written
        elif writer == "catalog":
            evidence = _evidence().model_copy(update={"normalization_method": prose})
            written = store.write_catalog_snapshot("catalog-asia-prose", [evidence])
            assert store.read_catalog_snapshot(written.snapshot_id) == written
        else:
            revision = _revision("revision-asia-prose").model_copy(
                update={"overlay": {"provenance": prose}}
            )
            store.publish_revision(revision, expected_active_id=None)
            assert store.read_revision(revision.revision_id) == revision
    finally:
        store.close()


def test_catalog_writer_accepts_public_asia_source_url(isolated_home: Path) -> None:
    store = RoutingStore.open()
    try:
        evidence = _evidence().model_copy(
            update={"source_url": "https://catalog.example/asia/benchmark-a"}
        )

        written = store.write_catalog_snapshot("catalog-asia-url", [evidence])

        assert store.read_catalog_snapshot(written.snapshot_id) == written
    finally:
        store.close()


@pytest.mark.parametrize(
    "private_url",
    [
        "http://127.0.0.1/catalog",
        "http://[::1]/catalog",
        "http://2130706433/catalog",
        "http://0177.0.0.1/catalog",
        "http://0x7f000001/catalog",
    ],
)
def test_catalog_writer_rejects_private_numeric_source_urls(
    isolated_home: Path,
    private_url: str,
) -> None:
    with RoutingStore.open() as store:
        unsafe = _evidence().model_copy(update={"source_url": private_url})

        with pytest.raises(UnsafeStoredContent, match="public"):
            store.write_catalog_snapshot("catalog-private-url", [unsafe])

        assert store.read_catalog_snapshot("catalog-private-url") is None


@pytest.mark.parametrize("writer", ["inventory", "catalog"])
def test_structured_writers_accept_dotted_non_endpoint_identifiers(
    isolated_home: Path,
    writer: str,
) -> None:
    store = RoutingStore.open()
    try:
        if writer == "inventory":
            observation = _observation("inventory-dotted").model_copy(
                update={
                    "key": _observation("inventory-dotted").key.model_copy(
                        update={"model": "gpt-4.1"}
                    ),
                    "provenance": ("benchmark.v1",),
                }
            )
            written = store.write_inventory_snapshot(
                "inventory-dotted",
                [observation],
            )
            assert store.read_inventory_snapshot("inventory-dotted") == written
        else:
            evidence = _evidence().model_copy(
                update={
                    "source_id": "benchmark.v1",
                    "model": "gpt-4.1",
                    "normalization_method": "benchmark.v1",
                }
            )
            written = store.write_catalog_snapshot("catalog-dotted", [evidence])
            assert store.read_catalog_snapshot("catalog-dotted") == written
    finally:
        store.close()


def test_catalog_writer_rejects_credentials_embedded_in_source_url(
    isolated_home: Path,
) -> None:
    store = RoutingStore.open()
    try:
        unsafe = _evidence().model_copy(
            update={
                "source_url": (
                    "https://catalog.example/benchmark-a?access_token=not-a-real-token"
                )
            }
        )

        with pytest.raises(UnsafeStoredContent, match="source_url"):
            store.write_catalog_snapshot("catalog-unsafe", [unsafe])

        assert store.read_catalog_snapshot("catalog-unsafe") is None
    finally:
        store.close()


@pytest.mark.parametrize(
    "unsafe_overlay",
    [
        {"nested": {"token": "not-a-real-token"}},
        {"nested": {"credentials": {"provider": "provider-a"}}},
        {"nested": {"key": "not-a-real-key"}},
        {"nested": {"credential_pool": "pool-a"}},
        {"endpoint": "https://private.invalid/v1"},
    ],
)
def test_adaptive_writer_rejects_nested_credentials_before_publication(
    isolated_home: Path,
    unsafe_overlay: dict[str, object],
) -> None:
    store = RoutingStore.open()
    try:
        unsafe = _revision("revision-unsafe").model_copy(
            update={"overlay": unsafe_overlay}
        )

        with pytest.raises(UnsafeStoredContent):
            store.publish_revision(unsafe, expected_active_id=None)

        assert store.read_revision("revision-unsafe") is None
        assert store.read_active_revision("authority-1") is None
    finally:
        store.close()


def test_snapshot_child_checksum_corruption_is_rejected(isolated_home: Path) -> None:
    store = RoutingStore.open()
    try:
        store.write_catalog_snapshot(
            "catalog-1",
            [_evidence()],
            created_at="2026-01-01T00:00:00Z",
        )
        store.connection.execute(
            "UPDATE catalog_evidence SET document_json = ? WHERE snapshot_id = ?",
            ('{"tampered":true}', "catalog-1"),
        )

        with pytest.raises(RevisionChecksumError, match="catalog-1"):
            store.read_catalog_snapshot("catalog-1")
    finally:
        store.close()


def test_publish_revision_is_compare_and_swap_and_loser_leaves_no_row(
    isolated_home: Path,
) -> None:
    first = RoutingStore.open()
    second = RoutingStore.open()
    try:
        winner = _revision("revision-winner")
        loser = _revision("revision-loser")

        first.publish_revision(winner, expected_active_id=None)
        with pytest.raises(RevisionConflict) as exc_info:
            second.publish_revision(loser, expected_active_id=None)

        assert exc_info.value.expected_active_id is None
        assert exc_info.value.actual_active_id == winner.revision_id
        assert first.read_revision(loser.revision_id) is None
        assert second.read_active_revision(winner.authority_id) == winner
    finally:
        first.close()
        second.close()


def test_publish_revalidates_model_copy_updates_before_any_write(
    isolated_home: Path,
) -> None:
    store = RoutingStore.open()
    try:
        invalid = _revision("revision-invalid").model_copy(update={"authority_id": ""})

        with pytest.raises(ValueError, match="at least 1 character"):
            store.publish_revision(invalid, expected_active_id=None)

        assert store.read_revision("revision-invalid") is None
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM active_adaptive_revisions"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.close()


def test_incomplete_revision_is_never_returned_or_made_active(
    isolated_home: Path,
) -> None:
    store = RoutingStore.open()
    try:
        complete = _revision("revision-complete")
        incomplete = _revision(
            "revision-incomplete",
            parent_revision_id=complete.revision_id,
        )
        store.publish_revision(complete, expected_active_id=None)
        raw = json.dumps(
            incomplete.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        store.connection.execute(
            "INSERT INTO adaptive_revisions "
            "(revision_id, authority_id, parent_revision_id, document_json, "
            "checksum, explanation_json, created_at, complete) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                incomplete.revision_id,
                incomplete.authority_id,
                incomplete.parent_revision_id,
                raw,
                hashlib.sha256(raw.encode()).hexdigest(),
                json.dumps(dict(incomplete.explanation), separators=(",", ":")),
                incomplete.created_at,
            ),
        )

        assert store.read_revision(incomplete.revision_id) is None
        assert store.read_active_revision(complete.authority_id) == complete
    finally:
        store.close()


def test_active_revision_checksum_is_verified_on_every_read(
    isolated_home: Path,
) -> None:
    store = RoutingStore.open()
    try:
        revision = _revision("revision-1")
        store.publish_revision(revision, expected_active_id=None)
        store.connection.execute(
            "UPDATE adaptive_revisions SET document_json = ? WHERE revision_id = ?",
            ('{"tampered":true}', revision.revision_id),
        )

        with pytest.raises(RevisionChecksumError, match=revision.revision_id):
            store.read_active_revision(revision.authority_id)
    finally:
        store.close()


def test_close_reopen_preserves_rows_and_init_is_idempotent(
    isolated_home: Path,
) -> None:
    first = RoutingStore.open()
    revision = _revision("revision-reopen")
    authority = first.write_authority_revision(
        "authority-reopen",
        {"profiles": {}},
        created_at="2026-01-01T00:00:00Z",
    )
    first.publish_revision(revision, expected_active_id=None)
    path = first.path
    first.close()

    second = RoutingStore.open(path=path)
    try:
        init_db(second.connection)
        assert second.read_authority_revision(authority.authority_id) == authority
        assert second.read_active_revision(revision.authority_id) == revision
    finally:
        second.close()


def test_reopen_recreates_missing_additive_index(isolated_home: Path) -> None:
    first = RoutingStore.open()
    path = first.path
    first.connection.execute("DROP INDEX idx_budget_bucket_day")
    first.close()

    second = RoutingStore.open(path=path)
    try:
        indexes = {
            row["name"]
            for row in second.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_budget_bucket_day" in indexes
    finally:
        second.close()


def _replace_empty_table_ddl(
    path: Path,
    table: str,
    old: str,
    new: str,
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        assert row is not None and row[0] is not None
        original = str(row[0])
        replacement = original.replace(old, new)
        assert replacement != original, (table, old)
        connection.execute(f'DROP TABLE "{table}"')
        connection.execute(replacement)
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        (
            "routing_decisions",
            "selected_profile_id TEXT",
            "selected_profile_id INTEGER",
        ),
        (
            "decision_candidates",
            "eligible INTEGER NOT NULL CHECK (eligible IN (0, 1))",
            "eligible TEXT NOT NULL",
        ),
        ("route_epochs", "api_request_id TEXT", "api_request_id INTEGER"),
        ("decision_operations", "updated_at TEXT NOT NULL", "updated_at INTEGER"),
        (
            "session_route_bindings",
            "projection_mode TEXT NOT NULL",
            "projection_mode INTEGER NOT NULL",
        ),
        (
            "activation_receipts",
            "inventory_revision TEXT NOT NULL",
            "inventory_revision INTEGER",
        ),
    ],
)
def test_current_schema_rejects_malformed_task2_table_signatures(
    tmp_path: Path,
    table: str,
    old: str,
    new: str,
) -> None:
    path = tmp_path / f"malformed-{table}" / "state.db"
    with RoutingStore.open(path=path):
        pass
    _replace_empty_table_ddl(path, table, old, new)

    with pytest.raises(
        UnsupportedSchemaVersion,
        match=rf"schema version {SCHEMA_VERSION}",
    ):
        RoutingStore.open(path=path)


def test_current_schema_rejects_malformed_legacy_table_signature(
    tmp_path: Path,
) -> None:
    path = tmp_path / "malformed-budget" / "state.db"
    with RoutingStore.open(path=path):
        pass
    _replace_empty_table_ddl(
        path,
        "budget_ledger",
        "reserved_usd REAL NOT NULL",
        "reserved_usd TEXT NOT NULL",
    )

    with pytest.raises(
        UnsupportedSchemaVersion,
        match=rf"schema version {SCHEMA_VERSION}",
    ):
        RoutingStore.open(path=path)


def test_current_schema_rejects_wrong_same_named_index(tmp_path: Path) -> None:
    path = tmp_path / "wrong-index" / "state.db"
    with RoutingStore.open(path=path):
        pass
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX idx_decisions_session")
        connection.execute(
            "CREATE INDEX idx_decisions_session ON routing_decisions(task_id)"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        UnsupportedSchemaVersion,
        match=rf"schema version {SCHEMA_VERSION}",
    ):
        RoutingStore.open(path=path)


def test_current_schema_rejects_case_changed_partial_index_literal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wrong-index-literal" / "state.db"
    with RoutingStore.open(path=path):
        pass
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX uq_decisions_fresh_session")
        connection.execute(
            "CREATE UNIQUE INDEX uq_decisions_fresh_session "
            "ON routing_decisions(session_id) WHERE scope = 'FRESH_SESSION'"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        UnsupportedSchemaVersion,
        match=rf"schema version {SCHEMA_VERSION}",
    ):
        RoutingStore.open(path=path)


def test_canonical_partial_index_rejects_duplicate_lowercase_fresh_rows(
    isolated_home: Path,
) -> None:
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision()
        store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        row = store.connection.execute(
            "SELECT * FROM routing_decisions WHERE decision_id = ?",
            (decision.decision_id,),
        ).fetchone()
        columns = tuple(row.keys())
        values = [row[column] for column in columns]
        values[columns.index("decision_id")] = "duplicate-decision"
        values[columns.index("task_id")] = "duplicate-task"
        placeholders = ", ".join("?" for _column in columns)
        with pytest.raises(sqlite3.IntegrityError, match="session_id"):
            store.connection.execute(
                f"INSERT INTO routing_decisions ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                values,
            )


_FULL_LEGACY_V3_SCHEMA_SQL = """
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE authority_revisions (
    authority_id TEXT PRIMARY KEY,
    document_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE inventory_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    document_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)) DEFAULT 0
);
CREATE TABLE inventory_observations (
    snapshot_id TEXT NOT NULL
        REFERENCES inventory_snapshots(snapshot_id) ON DELETE CASCADE,
    runtime_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL,
    document_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, runtime_id)
);
CREATE TABLE catalog_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    document_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)) DEFAULT 0
);
CREATE TABLE catalog_evidence (
    snapshot_id TEXT NOT NULL
        REFERENCES catalog_snapshots(snapshot_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    model TEXT NOT NULL,
    model_version TEXT NOT NULL,
    domain TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    canonical_provider TEXT NOT NULL DEFAULT '',
    canonical_model TEXT NOT NULL DEFAULT '',
    canonical_version TEXT NOT NULL DEFAULT '',
    runtime_id TEXT,
    document_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, evidence_id)
);
CREATE TABLE adaptive_revisions (
    revision_id TEXT PRIMARY KEY,
    authority_id TEXT NOT NULL,
    parent_revision_id TEXT,
    document_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)) DEFAULT 0
);
CREATE TABLE active_adaptive_revisions (
    authority_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES adaptive_revisions(revision_id),
    updated_at TEXT NOT NULL
);
CREATE TABLE routing_decisions (
    decision_id TEXT PRIMARY KEY,
    authority_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    inventory_revision_id TEXT NOT NULL,
    catalog_revision_id TEXT NOT NULL,
    policy_revision_id TEXT NOT NULL,
    adaptive_revision_id TEXT NOT NULL,
    selected_runtime_id TEXT NOT NULL,
    document_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE decision_candidates (
    decision_id TEXT NOT NULL
        REFERENCES routing_decisions(decision_id) ON DELETE CASCADE,
    runtime_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
    reason_codes_json TEXT NOT NULL,
    scoring_json TEXT NOT NULL,
    PRIMARY KEY (decision_id, runtime_id)
);
CREATE TABLE route_epochs (
    route_epoch_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES routing_decisions(decision_id),
    epoch_number INTEGER NOT NULL CHECK (epoch_number >= 0),
    runtime_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    UNIQUE (session_id, epoch_number)
);
CREATE TABLE budget_ledger (
    reservation_id TEXT PRIMARY KEY,
    bucket TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    reserved_usd REAL NOT NULL CHECK (reserved_usd >= 0),
    daily_limit_usd REAL NOT NULL CHECK (daily_limit_usd >= 0),
    actual_usd REAL CHECK (actual_usd IS NULL OR actual_usd >= 0),
    status TEXT NOT NULL CHECK (status IN ('reserved', 'reconciled')),
    created_at TEXT NOT NULL,
    reconciled_at TEXT
);
CREATE TABLE runtime_verification_attempts (
    precondition_hash TEXT PRIMARY KEY,
    runtime_id TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    inventory_revision TEXT NOT NULL,
    budget_reservation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reserved', 'succeeded', 'failed')),
    reason_code TEXT,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    actual_cost_usd REAL CHECK (actual_cost_usd IS NULL OR actual_cost_usd >= 0),
    response_hash TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE runtime_verification_previews (
    precondition_hash TEXT PRIMARY KEY,
    document_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_authority_created ON authority_revisions(created_at);
CREATE INDEX idx_inventory_created ON inventory_snapshots(created_at);
CREATE INDEX idx_inventory_runtime ON inventory_observations(runtime_id, state);
CREATE INDEX idx_catalog_created ON catalog_snapshots(created_at);
CREATE INDEX idx_catalog_model ON catalog_evidence(model, model_version, metric_name);
CREATE INDEX idx_adaptive_authority
    ON adaptive_revisions(authority_id, complete, created_at);
CREATE INDEX idx_decisions_session ON routing_decisions(session_id, created_at);
CREATE INDEX idx_decisions_task ON routing_decisions(task_id, created_at);
CREATE INDEX idx_route_epochs_session ON route_epochs(session_id, epoch_number);
CREATE INDEX idx_budget_bucket_day ON budget_ledger(bucket, budget_day, status);
CREATE INDEX idx_verification_runtime
    ON runtime_verification_attempts(runtime_id, created_at);
CREATE INDEX idx_verification_preview_expiry
    ON runtime_verification_previews(expires_at);
"""


def _create_legacy_v3_routing_db(
    path: Path,
    *,
    duplicate_fresh: bool = False,
) -> None:
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta VALUES ('schema_version', '3');"
        "CREATE TABLE routing_decisions ("
        "decision_id TEXT PRIMARY KEY, authority_id TEXT NOT NULL, "
        "scope TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL, "
        "inventory_revision_id TEXT NOT NULL, catalog_revision_id TEXT NOT NULL, "
        "policy_revision_id TEXT NOT NULL, adaptive_revision_id TEXT NOT NULL, "
        "selected_runtime_id TEXT NOT NULL, document_json TEXT NOT NULL, "
        "checksum TEXT NOT NULL, created_at TEXT NOT NULL);"
        "CREATE TABLE decision_candidates ("
        "decision_id TEXT NOT NULL REFERENCES routing_decisions(decision_id) "
        "ON DELETE CASCADE, runtime_id TEXT NOT NULL, ordinal INTEGER NOT NULL, "
        "eligible INTEGER NOT NULL, reason_codes_json TEXT NOT NULL, "
        "scoring_json TEXT NOT NULL, PRIMARY KEY (decision_id, runtime_id));"
    )
    runtime_id = "f" * 64
    rows = [
        (
            "legacy-1",
            "authority-1",
            "fresh_session",
            "legacy-session",
            "task-1",
            "inventory-1",
            "catalog-1",
            "policy-1",
            "adaptive-1",
            runtime_id,
            "{}",
            hashlib.sha256(b"{}").hexdigest(),
            "2026-01-01T00:00:00Z",
        )
    ]
    if duplicate_fresh:
        rows.append((
            "legacy-2",
            "authority-1",
            "fresh_session",
            "legacy-session",
            "task-2",
            "inventory-1",
            "catalog-1",
            "policy-1",
            "adaptive-1",
            runtime_id,
            "{}",
            hashlib.sha256(b"{}").hexdigest(),
            "2026-01-01T00:00:01Z",
        ))
    connection.executemany(
        "INSERT INTO routing_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.execute(
        "INSERT INTO decision_candidates VALUES (?, ?, ?, ?, ?, ?)",
        (
            "legacy-1",
            runtime_id,
            7,
            0,
            '["missing_tools"]',
            '{"quality":0.5}',
        ),
    )
    connection.commit()
    connection.close()


def _full_legacy_schema_sql(version: int) -> str:
    if version not in {1, 2, 3}:
        raise ValueError("legacy schema version must be 1, 2, or 3")
    schema = _FULL_LEGACY_V3_SCHEMA_SQL
    if version < 3:
        schema = schema.replace(
            "CREATE TABLE runtime_verification_previews (\n"
            "    precondition_hash TEXT PRIMARY KEY,\n"
            "    document_json TEXT NOT NULL,\n"
            "    checksum TEXT NOT NULL,\n"
            "    expires_at TEXT NOT NULL,\n"
            "    created_at TEXT NOT NULL\n"
            ");\n",
            "",
        ).replace(
            "CREATE INDEX idx_verification_preview_expiry\n"
            "    ON runtime_verification_previews(expires_at);\n",
            "",
        )
    if version == 1:
        schema = schema.replace(
            "    metric_name TEXT NOT NULL,\n"
            "    canonical_provider TEXT NOT NULL DEFAULT '',\n"
            "    canonical_model TEXT NOT NULL DEFAULT '',\n"
            "    canonical_version TEXT NOT NULL DEFAULT '',\n"
            "    runtime_id TEXT,\n",
            "    metric_name TEXT NOT NULL,\n",
        )
    return schema


def _earliest_legacy_v1_schema_sql() -> str:
    return (
        _full_legacy_schema_sql(1)
        .replace(
            "CREATE TABLE runtime_verification_attempts (\n"
            "    precondition_hash TEXT PRIMARY KEY,\n"
            "    runtime_id TEXT NOT NULL,\n"
            "    authority_id TEXT NOT NULL,\n"
            "    inventory_revision TEXT NOT NULL,\n"
            "    budget_reservation_id TEXT NOT NULL,\n"
            "    status TEXT NOT NULL CHECK (status IN ('reserved', 'succeeded', 'failed')),\n"
            "    reason_code TEXT,\n"
            "    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),\n"
            "    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),\n"
            "    actual_cost_usd REAL CHECK (actual_cost_usd IS NULL OR actual_cost_usd >= 0),\n"
            "    response_hash TEXT,\n"
            "    created_at TEXT NOT NULL,\n"
            "    completed_at TEXT\n"
            ");\n",
            "",
        )
        .replace(
            "CREATE INDEX idx_verification_runtime\n"
            "    ON runtime_verification_attempts(runtime_id, created_at);\n",
            "",
        )
    )


def _create_full_legacy_db(
    path: Path,
    *,
    version: int,
    earliest_v1: bool = False,
) -> dict[str, str]:
    if earliest_v1 and version != 1:
        raise ValueError("earliest_v1 is valid only for schema version 1")
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        _earliest_legacy_v1_schema_sql()
        if earliest_v1
        else _full_legacy_schema_sql(version)
    )
    connection.execute(
        "INSERT INTO schema_meta VALUES ('schema_version', ?)",
        (str(version),),
    )
    runtime_id = "f" * 64
    decision_id = "legacy-full-decision"
    session_id = "legacy-full-session"
    decision_json = "{}"
    connection.execute(
        "INSERT INTO routing_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            "authority-1",
            "fresh_session",
            session_id,
            "task-legacy-full",
            "inventory-1",
            "catalog-legacy-full",
            "policy-1",
            "adaptive-1",
            runtime_id,
            decision_json,
            hashlib.sha256(decision_json.encode()).hexdigest(),
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.execute(
        "INSERT INTO decision_candidates VALUES (?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            runtime_id,
            3,
            0,
            '["missing_tools"]',
            '{"quality":0.5}',
        ),
    )
    connection.execute(
        "INSERT INTO route_epochs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-epoch",
            session_id,
            decision_id,
            0,
            runtime_id,
            "initial_route",
            "2026-01-01T00:00:00Z",
            None,
        ),
    )
    connection.execute(
        "INSERT INTO budget_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-budget",
            "classifier",
            "2026-01-01",
            0.25,
            1.0,
            None,
            "reserved",
            "2026-01-01T00:00:00Z",
            None,
        ),
    )

    evidence = _evidence()
    evidence_json = json.dumps(
        evidence.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_id = hashlib.sha256(evidence_json.encode()).hexdigest()
    snapshot_json = json.dumps(
        [json.loads(evidence_json)],
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        "INSERT INTO catalog_snapshots VALUES (?, ?, ?, ?, 1)",
        (
            "catalog-legacy-full",
            snapshot_json,
            hashlib.sha256(snapshot_json.encode()).hexdigest(),
            "2026-01-01T00:00:00Z",
        ),
    )
    catalog_values = (
        "catalog-legacy-full",
        evidence_id,
        0,
        evidence.source_id,
        evidence.model,
        evidence.model_version,
        evidence.domain,
        evidence.metric_name,
    )
    if version == 1:
        connection.execute(
            "INSERT INTO catalog_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                *catalog_values,
                evidence_json,
                evidence_id,
                evidence.retrieved_at,
            ),
        )
    else:
        connection.execute(
            "INSERT INTO catalog_evidence VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                *catalog_values,
                "",
                evidence.model,
                evidence.model_version,
                None,
                evidence_json,
                evidence_id,
                evidence.retrieved_at,
            ),
        )

    preview_document = {
        "expires_at": "2026-01-02T00:00:00Z",
        "runtime_id": runtime_id,
    }
    preview_json = json.dumps(
        preview_document,
        sort_keys=True,
        separators=(",", ":"),
    )
    precondition_hash = hashlib.sha256(preview_json.encode()).hexdigest()
    if version == 3:
        connection.execute(
            "INSERT INTO runtime_verification_previews VALUES (?, ?, ?, ?, ?)",
            (
                precondition_hash,
                preview_json,
                precondition_hash,
                preview_document["expires_at"],
                "2026-01-01T00:00:00Z",
            ),
        )
    if not earliest_v1:
        connection.execute(
            "INSERT INTO runtime_verification_attempts VALUES "
            "(?, ?, ?, ?, ?, 'reserved', NULL, NULL, NULL, NULL, NULL, ?, NULL)",
            (
                precondition_hash,
                runtime_id,
                "authority-1",
                "inventory-1",
                "legacy-budget",
                "2026-01-01T00:00:00Z",
            ),
        )
    connection.commit()
    connection.close()
    return {
        "decision_id": decision_id,
        "session_id": session_id,
        "runtime_id": runtime_id,
        "precondition_hash": precondition_hash,
    }


def test_exact_earliest_v1_schema_migrates_populated_rows_and_reopens(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-earliest-v1" / "state.db"
    ids = _create_full_legacy_db(path, version=1, earliest_v1=True)

    connection = sqlite3.connect(path)
    try:
        objects = {
            (object_type, name)
            for object_type, name in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()
    assert len(objects) == 22
    assert ("table", "runtime_verification_attempts") not in objects
    assert ("index", "idx_verification_runtime") not in objects

    with RoutingStore.open(path=path) as store:
        assert store.read_route_epochs(ids["session_id"])[0].route_epoch_id == (
            "legacy-epoch"
        )
        assert store.daily_budget(
            "classifier",
            datetime(2026, 1, 1, tzinfo=UTC).date(),
        ).reserved_usd == pytest.approx(0.25)
        assert store.read_catalog_snapshot("catalog-legacy-full") is not None
        assert store.read_verification_attempt(ids["precondition_hash"]) is None
        candidate = store.connection.execute(
            "SELECT * FROM decision_candidates WHERE decision_id = ?",
            (ids["decision_id"],),
        ).fetchone()
        assert candidate is not None
        assert candidate["candidate_id"] == f"legacy:{ids['runtime_id']}"
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with RoutingStore.open(path=path) as reopened:
        assert reopened.read_route_epochs(ids["session_id"])[0].route_epoch_id == (
            "legacy-epoch"
        )
        assert reopened.read_catalog_snapshot("catalog-legacy-full") is not None
        assert reopened.connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("version", [1, 2])
def test_full_v1_v2_schemas_migrate_representative_rows_and_reopen(
    tmp_path: Path,
    version: int,
) -> None:
    path = tmp_path / f"legacy-full-v{version}" / "state.db"
    ids = _create_full_legacy_db(path, version=version)

    with RoutingStore.open(path=path) as store:
        assert store.read_route_epochs(ids["session_id"])[0].route_epoch_id == (
            "legacy-epoch"
        )
        assert store.daily_budget(
            "classifier",
            datetime(2026, 1, 1, tzinfo=UTC).date(),
        ).reserved_usd == pytest.approx(0.25)
        assert store.read_catalog_snapshot("catalog-legacy-full") is not None
        attempt = store.read_verification_attempt(ids["precondition_hash"])
        assert attempt is not None
        assert attempt.status == "reserved"
        assert store.read_verification_preview(ids["precondition_hash"]) is None
        candidate = store.connection.execute(
            "SELECT * FROM decision_candidates WHERE decision_id = ?",
            (ids["decision_id"],),
        ).fetchone()
        assert candidate is not None
        assert candidate["candidate_id"] == f"legacy:{ids['runtime_id']}"
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with RoutingStore.open(path=path) as reopened:
        assert reopened.read_route_epochs(ids["session_id"])[0].route_epoch_id == (
            "legacy-epoch"
        )
        assert reopened.read_catalog_snapshot("catalog-legacy-full") is not None
        assert reopened.read_verification_attempt(ids["precondition_hash"]) is not None
        assert reopened.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_full_v3_schema_migrates_nonhash_epoch_and_representative_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-full-v3" / "state.db"
    ids = _create_full_legacy_db(path, version=3)

    with RoutingStore.open(path=path) as store:
        epochs = store.read_route_epochs(ids["session_id"])
        assert len(epochs) == 1
        assert epochs[0].route_epoch_id == "legacy-epoch"
        assert epochs[0].runtime_id == ids["runtime_id"]
        assert store.daily_budget(
            "classifier",
            datetime(2026, 1, 1, tzinfo=UTC).date(),
        ).reserved_usd == pytest.approx(0.25)
        assert store.read_catalog_snapshot("catalog-legacy-full") is not None
        attempt = store.read_verification_attempt(ids["precondition_hash"])
        assert attempt is not None
        assert attempt.status == "reserved"
        assert store.read_verification_preview(ids["precondition_hash"]) is not None
        candidate = store.connection.execute(
            "SELECT * FROM decision_candidates WHERE decision_id = ?",
            (ids["decision_id"],),
        ).fetchone()
        assert candidate is not None
        assert candidate["candidate_id"] == f"legacy:{ids['runtime_id']}"
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with RoutingStore.open(path=path) as reopened:
        assert reopened.read_route_epochs(ids["session_id"])[0].route_epoch_id == (
            "legacy-epoch"
        )
        assert reopened.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v3_routing_migration_preserves_candidates_and_adds_atomic_contract(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-routing" / "state.db"
    _create_legacy_v3_routing_db(path)

    with RoutingStore.open(path=path) as store:
        columns = {
            row["name"]
            for row in store.connection.execute("PRAGMA table_info(routing_decisions)")
        }
        assert {
            "operation_id",
            "task_index",
            "selected_profile_id",
            "projection_mode",
            "activation_receipt_id",
            "authority_revision_id",
        } <= columns
        candidate = store.connection.execute(
            "SELECT * FROM decision_candidates WHERE decision_id = 'legacy-1'"
        ).fetchone()
        assert candidate["candidate_id"] == f"legacy:{'f' * 64}"
        assert candidate["runtime_id"] == "f" * 64
        assert candidate["ordinal"] == 7
        assert candidate["eligible"] == 0
        assert json.loads(candidate["reason_codes_json"]) == ["missing_tools"]
        assert json.loads(candidate["scoring_json"]) == {"quality": 0.5}
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            store.connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            == SCHEMA_VERSION
        )

    with RoutingStore.open(path=path) as reopened:
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM decision_candidates"
            ).fetchone()[0]
            == 1
        )


def test_conflicting_legacy_fresh_decisions_abort_entire_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-conflict" / "state.db"
    _create_legacy_v3_routing_db(path, duplicate_fresh=True)

    with pytest.raises(UnsupportedSchemaVersion, match="multiple fresh decisions"):
        RoutingStore.open(path=path)

    verifier = sqlite3.connect(path)
    try:
        assert (
            verifier.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            == "3"
        )
        columns = {
            row[1] for row in verifier.execute("PRAGMA table_info(routing_decisions)")
        }
        assert "operation_id" not in columns
        tables = {
            row[0]
            for row in verifier.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "activation_receipts" not in tables
    finally:
        verifier.close()


@pytest.mark.parametrize("legacy_version", ["1", "2", "3"])
def test_direct_legacy_schema_versions_upgrade_and_reopen(
    tmp_path: Path,
    legacy_version: str,
) -> None:
    path = tmp_path / f"legacy-{legacy_version}" / "state.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_meta VALUES ('schema_version', ?)",
        (legacy_version,),
    )
    connection.commit()
    connection.close()

    with RoutingStore.open(path=path) as store:
        assert (
            store.connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            == SCHEMA_VERSION
        )
    with RoutingStore.open(path=path) as reopened:
        assert reopened.count_decisions() == 0


def test_legacy_budget_ledger_is_migrated_additively(tmp_path: Path) -> None:
    path = tmp_path / "legacy" / "state.db"
    path.parent.mkdir(parents=True)
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE budget_ledger ("
        "reservation_id TEXT PRIMARY KEY, "
        "bucket TEXT NOT NULL, "
        "budget_day TEXT NOT NULL, "
        "reserved_usd REAL NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    legacy.execute(
        "INSERT INTO budget_ledger "
        "(reservation_id, bucket, budget_day, reserved_usd, created_at) "
        "VALUES ('legacy-1', 'classifier', '2026-01-01', 0.25, "
        "'2026-01-01T00:00:00Z')"
    )
    legacy.commit()
    legacy.close()

    store = RoutingStore.open(path=path)
    try:
        columns = {
            row[1]
            for row in store.connection.execute("PRAGMA table_info(budget_ledger)")
        }
        assert {"daily_limit_usd", "actual_usd", "status", "reconciled_at"} <= columns
        assert store.daily_budget(
            "classifier", datetime(2026, 1, 1, tzinfo=UTC).date()
        ).reserved_usd == pytest.approx(0.25)
    finally:
        store.close()


def test_legacy_catalog_rows_gain_global_applicability_without_rewrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-catalog" / "state.db"
    path.parent.mkdir(parents=True)
    evidence = _evidence()
    evidence_json = json.dumps(
        evidence.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_id = hashlib.sha256(evidence_json.encode()).hexdigest()
    snapshot_json = json.dumps(
        [json.loads(evidence_json)],
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot_checksum = hashlib.sha256(snapshot_json.encode()).hexdigest()
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1');"
        "CREATE TABLE catalog_snapshots ("
        "snapshot_id TEXT PRIMARY KEY, document_json TEXT NOT NULL, "
        "checksum TEXT NOT NULL, created_at TEXT NOT NULL, complete INTEGER NOT NULL);"
        "CREATE TABLE catalog_evidence ("
        "snapshot_id TEXT NOT NULL, evidence_id TEXT NOT NULL, ordinal INTEGER NOT NULL, "
        "source_id TEXT NOT NULL, model TEXT NOT NULL, model_version TEXT NOT NULL, "
        "domain TEXT NOT NULL, metric_name TEXT NOT NULL, document_json TEXT NOT NULL, "
        "checksum TEXT NOT NULL, retrieved_at TEXT NOT NULL, "
        "PRIMARY KEY (snapshot_id, evidence_id));"
    )
    legacy.execute(
        "INSERT INTO catalog_snapshots VALUES (?, ?, ?, ?, 1)",
        (
            "legacy-catalog",
            snapshot_json,
            snapshot_checksum,
            "2026-01-01T00:00:00Z",
        ),
    )
    legacy.execute(
        "INSERT INTO catalog_evidence VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-catalog",
            evidence_id,
            evidence.source_id,
            evidence.model,
            evidence.model_version,
            evidence.domain,
            evidence.metric_name,
            evidence_json,
            evidence_id,
            evidence.retrieved_at,
        ),
    )
    legacy.commit()
    legacy.close()

    with RoutingStore.open(path=path) as store:
        restored = store.read_catalog_snapshot("legacy-catalog")

        assert restored is not None
        assert restored.document_json == snapshot_json
        assert restored.records == (
            StoredCatalogRecord(
                evidence=evidence,
                applicability=CatalogApplicability(
                    canonical_model=evidence.model,
                    canonical_version=evidence.model_version,
                ),
            ),
        )
        assert (
            store.connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            == SCHEMA_VERSION
        )
        assert (
            store.write_catalog_snapshot(
                "legacy-catalog",
                [evidence],
            )
            == restored
        )


def test_future_schema_version_is_not_silently_downgraded(tmp_path: Path) -> None:
    path = tmp_path / "future" / "state.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '999')"
    )
    connection.commit()
    connection.close()

    sidecars = [Path(f"{path}-wal"), Path(f"{path}-shm")]
    before_bytes = path.read_bytes()
    before_mtime_ns = path.stat().st_mtime_ns
    before_sidecars = {
        sidecar: (
            sidecar.exists(),
            sidecar.read_bytes() if sidecar.exists() else None,
            sidecar.stat().st_mtime_ns if sidecar.exists() else None,
        )
        for sidecar in sidecars
    }

    with pytest.raises(UnsupportedSchemaVersion, match="999"):
        RoutingStore.open(path=path)

    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime_ns
    assert {
        sidecar: (
            sidecar.exists(),
            sidecar.read_bytes() if sidecar.exists() else None,
            sidecar.stat().st_mtime_ns if sidecar.exists() else None,
        )
        for sidecar in sidecars
    } == before_sidecars

    verifier = sqlite3.connect(path)
    try:
        assert (
            verifier.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            == "999"
        )
        tables = {
            row[0]
            for row in verifier.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert tables == {"schema_meta"}
    finally:
        verifier.close()


def test_future_schema_in_uncheckpointed_wal_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "future-wal" / "state.db"
    path.parent.mkdir(parents=True)
    writer = sqlite3.connect(path, isolation_level=None)

    def file_state() -> dict[Path, tuple[bool, bytes | None, int | None]]:
        files = (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        return {
            file: (
                file.exists(),
                file.read_bytes() if file.exists() else None,
                file.stat().st_mtime_ns if file.exists() else None,
            )
            for file in files
        }

    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        writer.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')"
        )
        assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

        immutable = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            assert (
                immutable.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
                == "1"
            )
        finally:
            immutable.close()

        writer.execute(
            "UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'"
        )

        before = file_state()
        with pytest.raises(UnsupportedSchemaVersion, match="999"):
            RoutingStore.open(path=path)
        assert file_state() == before
    finally:
        writer.close()


def test_evidence_round_trip_and_exact_replay(isolated_home, valid_turn_event):
    del isolated_home
    with RoutingStore.open() as store:
        event = _evidence_parent(store, valid_turn_event)
        first = store.write_evidence_event(event)
        replay = store.write_evidence_event(event)

        assert first.status == "inserted"
        assert replay.status == "replayed"
        assert replay.event == event
        assert store.read_evidence_event(event.evidence_id) == event
        assert store.list_evidence_events(
            decision_id=event.decision_id,
            profile_id=event.profile_id,
            runtime_id=event.runtime_id,
            reasoning_effort=event.reasoning_effort,
            observed_at_or_after=event.observed_at,
        ) == (event,)


def test_same_evidence_id_with_changed_content_conflicts(
    isolated_home,
    valid_turn_event,
):
    del isolated_home
    with RoutingStore.open() as store:
        event = _evidence_parent(store, valid_turn_event)
        store.write_evidence_event(event)
        with pytest.raises(ImmutableRecordConflict):
            store.write_evidence_event(event.model_copy(update={"api_calls": 99}))


def test_evidence_checksum_corruption_is_detected(isolated_home, valid_turn_event):
    del isolated_home
    with RoutingStore.open() as store:
        event = _evidence_parent(store, valid_turn_event)
        store.write_evidence_event(event)
        store.connection.execute(
            "UPDATE evidence_events SET checksum = ? WHERE evidence_id = ?",
            ("0" * 64, event.evidence_id),
        )
        with pytest.raises(RevisionChecksumError):
            store.read_evidence_event(event.evidence_id)


def test_populated_v5_migrates_to_current_schema_without_changing_decisions(tmp_path):
    path = tmp_path / "state.db"
    with RoutingStore.open(path=path) as store:
        decision = _decision(
            decision_id="migration-decision",
            session_id="migration-session",
        )
        stored = store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        expected = store.read_decision(stored.decision.decision_id)
        old_tables = tuple(
            str(row[0])
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT IN ('evidence_events', 'schema_meta') ORDER BY name"
            )
        )
        old_fingerprint = _table_counts_and_checksums(store.connection, old_tables)
        store.connection.execute("DROP INDEX uq_evidence_turn_outcome")
        store.connection.execute("DROP INDEX uq_evidence_feedback")
        store.connection.execute("DROP INDEX idx_evidence_report")
        store.connection.execute("DROP TABLE evidence_events")
        store.connection.execute(
            "UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'"
        )
        assert store.connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()["value"] == "5"
        assert store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_events'"
        ).fetchone() is None
    with RoutingStore.open(path=path) as migrated:
        assert migrated.read_decision(expected.decision_id) == expected
        assert _table_counts_and_checksums(
            migrated.connection,
            old_tables,
        ) == old_fingerprint
        assert migrated.count_evidence_events() == 0
        assert migrated.connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()["value"] == SCHEMA_VERSION


def test_populated_v6_migrates_to_v7_with_legacy_decision_replay_unchanged(
    tmp_path,
    valid_turn_event,
):
    path = tmp_path / "v6-state.db"
    with RoutingStore.open(path=path) as store:
        decision = _decision(
            decision_id="v6-decision",
            session_id="v6-session",
        )
        expected = store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        ).decision
        event = _evidence_parent(store, valid_turn_event)
        store.write_evidence_event(event)
        for table in (
            "adaptive_optimizer_leases",
            "adaptive_canary_assignments",
            "adaptive_lifecycle_events",
            "adaptive_profile_states",
            "adaptive_profile_revisions",
        ):
            store.connection.execute(f'DROP TABLE "{table}"')
        for column in (
            "profile_adaptive_revision_id",
            "adaptive_assignment_id",
            "adaptive_profile_snapshot_json",
        ):
            store.connection.execute(
                f'ALTER TABLE routing_decisions DROP COLUMN "{column}"'
            )
        store.connection.execute(
            "UPDATE schema_meta SET value='6' WHERE key='schema_version'"
        )

    with RoutingStore.open(path=path) as migrated:
        assert migrated.schema_version == int(storage_module.SCHEMA_VERSION)
        assert migrated.count_evidence_events() == 1
        replayed = migrated.read_decision(expected.decision_id)
        assert replayed == expected
        assert replayed.adaptive_revision == expected.adaptive_revision
        row = migrated.connection.execute(
            "SELECT profile_adaptive_revision_id, adaptive_assignment_id, "
            "adaptive_profile_snapshot_json, adaptive_revision_id "
            "FROM routing_decisions WHERE decision_id=?",
            (expected.decision_id,),
        ).fetchone()
        assert tuple(row[:3]) == (None, None, None)
        assert row[3] == expected.adaptive_revision


def test_preintegration_decision_rejects_unattested_profile_adaptive_columns(
    isolated_home,
):
    del isolated_home
    with RoutingStore.open() as store:
        decision = store.commit_decision(
            _decision(),
            candidates=(_candidate(),),
            create_epoch=True,
        ).decision
        store.connection.execute(
            "UPDATE routing_decisions SET profile_adaptive_revision_id=? "
            "WHERE decision_id=?",
            ("forged-revision", decision.decision_id),
        )
        with pytest.raises(RevisionChecksumError):
            store.read_decision(decision.decision_id)


def test_partial_pre_v6_evidence_schema_is_rejected(tmp_path):
    path = tmp_path / "partial-evidence.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta VALUES ('schema_version', '5');"
        "CREATE TABLE evidence_events (evidence_id TEXT PRIMARY KEY);"
    )
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedSchemaVersion, match="evidence_events"):
        RoutingStore.open(path=path)


def _assert_unversioned_partial_evidence_rejected_without_mutation(
    path: Path,
    *,
    create_markerless_schema_meta: bool,
) -> None:
    connection = sqlite3.connect(path)
    if create_markerless_schema_meta:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
    connection.execute(
        "CREATE TABLE evidence_events (evidence_id TEXT PRIMARY KEY)"
    )
    connection.commit()
    connection.close()
    sidecars = (Path(f"{path}-wal"), Path(f"{path}-shm"))
    before = {
        file: (
            file.exists(),
            file.read_bytes() if file.exists() else None,
            file.stat().st_mtime_ns if file.exists() else None,
        )
        for file in (path, *sidecars)
    }

    with pytest.raises(UnsupportedSchemaVersion, match="evidence_events"):
        RoutingStore.open(path=path)

    assert {
        file: (
            file.exists(),
            file.read_bytes() if file.exists() else None,
            file.stat().st_mtime_ns if file.exists() else None,
        )
        for file in (path, *sidecars)
    } == before


def test_partial_evidence_without_schema_meta_is_rejected_before_mutation(tmp_path):
    _assert_unversioned_partial_evidence_rejected_without_mutation(
        tmp_path / "partial-without-schema-meta.db",
        create_markerless_schema_meta=False,
    )


def test_partial_evidence_with_markerless_schema_meta_is_rejected_before_mutation(
    tmp_path,
):
    _assert_unversioned_partial_evidence_rejected_without_mutation(
        tmp_path / "partial-with-markerless-schema-meta.db",
        create_markerless_schema_meta=True,
    )


def test_evidence_rejects_missing_or_crossed_route_records(
    isolated_home,
    valid_turn_event,
):
    del isolated_home
    with RoutingStore.open() as store:
        event = _evidence_parent(store, valid_turn_event)
        with pytest.raises(ImmutableRecordConflict, match="decision"):
            store.write_evidence_event(
                event.model_copy(update={"decision_id": "missing-decision"})
            )
        with pytest.raises(ImmutableRecordConflict, match="epoch"):
            store.write_evidence_event(
                event.model_copy(update={"route_epoch_id": "missing-epoch"})
            )

        receipt = ActivationReceipt(
            receipt_id="evidence-receipt-b",
            authority_id="authority-1",
            config_sha="e" * 64,
            inventory_contract_sha="c" * 64,
            inventory_revision="inventory-1",
            adapter_capability_sha="d" * 64,
            created_at="2026-01-01T00:00:00Z",
        )
        store.write_activation_receipt(receipt)
        other = _decision(
            decision_id="decision-b",
            session_id="session-b",
            projection_mode="active",
            activation_receipt_id=receipt.receipt_id,
            activation_config_sha=receipt.config_sha,
            adapter_capability_sha=receipt.adapter_capability_sha,
        )
        committed = store.commit_decision(
            other,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        assert committed.epoch is not None
        other_epoch = store.mark_route_epoch_provider_started(
            other.session_id,
            decision_id=other.decision_id,
            runtime_id=committed.epoch.runtime_id,
            api_request_id="evidence-request-b",
            started_at="2026-01-01T00:00:01Z",
        )
        with pytest.raises(ImmutableRecordConflict, match="crosses route records"):
            store.write_evidence_event(
                event.model_copy(update={"route_epoch_id": other_epoch.route_epoch_id})
            )


@pytest.mark.parametrize("projection_mode", ["shadow", "inherit"])
def test_evidence_rejects_nonactive_decisions(
    isolated_home,
    valid_turn_event,
    projection_mode,
):
    del isolated_home
    with RoutingStore.open() as store:
        decision = _decision(
            decision_id=f"decision-{projection_mode}",
            session_id=f"session-{projection_mode}",
            projection_mode=projection_mode,
        )
        committed = store.commit_decision(
            decision,
            candidates=(_candidate(),),
            create_epoch=True,
        )
        assert committed.epoch is not None
        epoch = store.mark_route_epoch_provider_started(
            decision.session_id,
            decision_id=decision.decision_id,
            runtime_id=committed.epoch.runtime_id,
            api_request_id=f"request-{projection_mode}",
            started_at="2026-01-01T00:00:01Z",
        )
        event = valid_turn_event.model_copy(update={
            "evidence_id": turn_evidence_id(decision.session_id, valid_turn_event.turn_id),
            "decision_id": decision.decision_id,
            "session_id": decision.session_id,
            "task_id": decision.task_id,
            "route_epoch_id": epoch.route_epoch_id,
            "runtime_id": epoch.runtime_id,
            "profile_id": decision.selected_profile_id,
            "reasoning_effort": decision.selected_reasoning_effort,
        })
        with pytest.raises(ImmutableRecordConflict, match="only active"):
            store.write_evidence_event(event)


def test_first_turn_insert_requires_current_active_binding(
    isolated_home,
    valid_turn_event,
):
    del isolated_home
    with RoutingStore.open() as store:
        event = _evidence_parent(store, valid_turn_event)
        store.record_manual_pin(
            event.session_id,
            event.runtime_id,
            "cli:model",
            "2026-01-01T00:00:02Z",
        )
        with pytest.raises(ImmutableRecordConflict, match="current active"):
            store.write_evidence_event(event)


def test_historical_turn_replay_survives_later_epoch_transition(
    isolated_home,
    valid_turn_event,
):
    del isolated_home
    with RoutingStore.open() as store:
        event = _evidence_parent(store, valid_turn_event)
        store.write_evidence_event(event)
        store.start_route_epoch(
            session_id=event.session_id,
            decision_id=event.decision_id,
            runtime_id=event.runtime_id,
            reason_code="pre_call_fallback",
            started_at="2026-01-01T00:00:02Z",
            expected_epoch=0,
        )

        assert store.write_evidence_event(event).status == "replayed"
        assert store.read_evidence_event(event.evidence_id) == event


def test_first_turn_insert_rejects_a_stale_current_epoch(
    isolated_home,
    valid_turn_event,
):
    del isolated_home
    with RoutingStore.open() as store:
        event = _evidence_parent(store, valid_turn_event)
        store.start_route_epoch(
            session_id=event.session_id,
            decision_id=event.decision_id,
            runtime_id=event.runtime_id,
            reason_code="pre_call_fallback",
            started_at="2026-01-01T00:00:02Z",
            expected_epoch=0,
        )

        with pytest.raises(ImmutableRecordConflict, match="current active"):
            store.write_evidence_event(event)


def test_initial_task_flag_is_validated_in_both_directions(
    isolated_home,
    valid_turn_event,
):
    del isolated_home
    with RoutingStore.open() as store:
        event = _evidence_parent(store, valid_turn_event)
        false_origin = event.model_copy(update={
            "context_bucket": None,
            "is_initial_routing_task": False,
        })
        with pytest.raises(ImmutableRecordConflict, match="initial-task"):
            store.write_evidence_event(false_origin)

        child = store.bind_session_continuation(
            event.session_id,
            "session-child",
            reason="compression",
            created_at="2026-01-01T00:01:00Z",
        )
        child_epoch = store.read_route_epochs(child.session_id)[0]
        child_epoch = store.mark_route_epoch_provider_started(
            child.session_id,
            decision_id=event.decision_id,
            runtime_id=event.runtime_id,
            api_request_id="child-evidence-request",
            started_at="2026-01-01T00:01:01Z",
        )
        child_turn = "c" * 64
        true_child = event.model_copy(update={
            "evidence_id": turn_evidence_id(child.session_id, child_turn),
            "session_id": child.session_id,
            "turn_id": child_turn,
            "task_id": "task-child",
            "route_epoch_id": child_epoch.route_epoch_id,
            "context_bucket": None,
            "is_initial_routing_task": True,
        })
        with pytest.raises(ImmutableRecordConflict, match="initial-task"):
            store.write_evidence_event(true_child)


def test_feedback_requires_turn_parent_and_exact_copied_attribution(
    isolated_home,
    valid_turn_event,
):
    del isolated_home
    with RoutingStore.open() as store:
        parent = _evidence_parent(store, valid_turn_event)
        store.write_evidence_event(parent)
        feedback = build_feedback_event(
            parent,
            "rating-5",
            observed_at="2026-07-17T12:01:00Z",
        )
        stored = store.write_evidence_event(feedback).event

        second = build_feedback_event(
            parent,
            "rating-4",
            observed_at="2026-07-17T12:02:00Z",
        )
        crossed = second.model_copy(update={"task_id": "other-task"})
        with pytest.raises(ImmutableRecordConflict):
            store.write_evidence_event(crossed)

        feedback_on_feedback = second.model_copy(update={
            "parent_evidence_id": stored.evidence_id,
            "evidence_id": feedback_evidence_id(stored.evidence_id, "rating-4"),
        })
        with pytest.raises(ImmutableRecordConflict, match="turn evidence"):
            store.write_evidence_event(feedback_on_feedback)


def test_contradictory_feedback_values_are_distinct_immutable_rows(
    isolated_home,
    valid_turn_event,
):
    del isolated_home
    with RoutingStore.open() as store:
        parent = _evidence_parent(store, valid_turn_event)
        store.write_evidence_event(parent)
        rejected = build_feedback_event(
            parent,
            "rejected",
            observed_at="2026-07-17T12:01:00Z",
        )
        corrected = build_feedback_event(
            parent,
            "corrected",
            observed_at="2026-07-17T12:02:00Z",
        )
        assert store.write_evidence_event(rejected).status == "inserted"
        assert store.write_evidence_event(corrected).status == "inserted"
        assert store.list_evidence_events(
            parent_evidence_id=parent.evidence_id,
        ) == (rejected, corrected)


def test_evidence_secret_shaped_turn_id_is_rejected_before_sql(
    isolated_home,
    valid_turn_event,
):
    del isolated_home
    with RoutingStore.open() as store:
        event = _evidence_parent(store, valid_turn_event)
        secret_turn = "sk-proj-abcdefghijklmnopqrstuvwxyz012345"
        unsafe = event.model_copy(update={
            "evidence_id": turn_evidence_id(event.session_id, secret_turn),
            "turn_id": secret_turn,
        })
        with pytest.raises(UnsafeStoredContent):
            store.write_evidence_event(unsafe)
        assert store.count_evidence_events() == 0


def test_evidence_scalar_document_disagreement_is_detected(
    isolated_home,
    valid_turn_event,
):
    del isolated_home
    with RoutingStore.open() as store:
        event = _evidence_parent(store, valid_turn_event)
        store.write_evidence_event(event)
        store.connection.execute(
            "UPDATE evidence_events SET api_calls = 99 WHERE evidence_id = ?",
            (event.evidence_id,),
        )
        with pytest.raises(RevisionChecksumError):
            store.read_evidence_event(event.evidence_id)


def test_evidence_adjacent_large_integer_scalar_corruption_is_detected(
    isolated_home,
    valid_turn_event,
):
    del isolated_home
    with RoutingStore.open() as store:
        exact = 2**53
        event = _evidence_parent(store, valid_turn_event).model_copy(
            update={"api_calls": exact}
        )
        store.write_evidence_event(event)
        store.connection.execute(
            "UPDATE evidence_events SET api_calls = ? WHERE evidence_id = ?",
            (exact + 1, event.evidence_id),
        )

        with pytest.raises(RevisionChecksumError):
            store.read_evidence_event(event.evidence_id)
