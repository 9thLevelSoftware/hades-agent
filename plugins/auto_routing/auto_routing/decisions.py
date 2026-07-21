"""Content-free routing-decision construction and semantic checksums."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Mapping

from .models import RoutingDecision
from .selector import SelectionResult
from .storage import DecisionCandidate, _validate_decision_candidate_coherence


@dataclass(frozen=True)
class BuiltDecision:
    """One event-specific decision and its stable semantic identity."""

    decision: RoutingDecision
    candidates: tuple[DecisionCandidate, ...]
    semantic_checksum: str


class DecisionBuilder:
    """Attach event identity to a content-free deterministic route decision."""

    def __init__(
        self,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._id_factory = id_factory or _new_decision_id
        self._clock = clock or _canonical_now

    def build(
        self,
        *,
        scope: Literal["fresh_session", "delegation"],
        session_id: str,
        task_id: str,
        operation_id: str | None,
        task_index: int | None,
        selection: SelectionResult,
        task_facts_hash: str,
        inventory_revision: str,
        catalog_revision: str,
        authority_revision: str,
        policy_revision: str,
        adaptive_revision: str,
        profile_adaptive_revision_id: str | None = None,
        adaptive_assignment_id: str | None = None,
        adaptive_profile_snapshot: Mapping[str, str] | None = None,
        management_revision_id: str | None = None,
        management_assignment_id: str | None = None,
        management_profile_snapshot: Mapping[str, str] | None = None,
        projection_mode: Literal["shadow", "active", "inherit"],
        routing_latency_seconds: float,
        applied_rule_ids: tuple[str, ...] = (),
        activation_receipt_id: str | None = None,
        activation_config_sha: str | None = None,
        adapter_capability_sha: str | None = None,
        classifier_runtime_id: str | None = None,
        classifier_input_tokens: int = 0,
        classifier_output_tokens: int = 0,
        classifier_cost_usd: float | None = None,
    ) -> BuiltDecision:
        """Build a validated event without accepting prompt or secret objects."""
        selected_id = selection.selected_runtime.key.stable_id()
        selected_candidate = next(
            (
                candidate
                for candidate in selection.candidates
                if candidate.profile_id == selection.selected_profile_id
                and candidate.runtime_id == selected_id
                and candidate.eligible
            ),
            None,
        )
        normalized_inputs = (
            selected_candidate.normalized_scoring_inputs
            if selected_candidate is not None
            else ()
        )
        scores: dict[str, float] = {}
        for candidate in selection.candidates:
            if candidate.final_score is None or not candidate.eligible:
                continue
            previous = scores.get(candidate.runtime_id)
            if previous is None or candidate.final_score > previous:
                scores[candidate.runtime_id] = candidate.final_score
        if selected_candidate is not None and selected_candidate.final_score is not None:
            scores[selected_id] = selected_candidate.final_score

        eligible_ids = set(selection.eligible_runtime_ids)
        rejected_by_runtime: dict[str, tuple[str, ...]] = {}
        for candidate in selection.candidates:
            if (
                candidate.eligible
                or candidate.runtime_id in eligible_ids
                or candidate.runtime_id in rejected_by_runtime
            ):
                continue
            rejected_by_runtime[candidate.runtime_id] = candidate.reason_codes
        rejected = tuple(sorted(rejected_by_runtime.items()))
        degradation_reason = None
        if selection.selection_reason == "safe_default":
            degradation_reason = "safe_default_selected"
        elif selection.selection_reason == "pre_call_fallback":
            degradation_reason = "pre_call_fallback"
        elif selection.selection_reason == "baseline_inherit":
            degradation_reason = "baseline_inherit"

        decision = RoutingDecision(
            decision_id=self._id_factory(),
            scope=scope,
            session_id=session_id,
            task_id=task_id,
            operation_id=operation_id,
            task_index=task_index,
            created_at=self._clock(),
            applied_rule_ids=applied_rule_ids,
            assessment=selection.assessment,
            task_facts_hash=task_facts_hash,
            inventory_revision=inventory_revision,
            catalog_revision=catalog_revision,
            authority_revision=authority_revision,
            policy_revision=policy_revision,
            adaptive_revision=adaptive_revision,
            profile_adaptive_revision_id=profile_adaptive_revision_id,
            adaptive_assignment_id=adaptive_assignment_id,
            adaptive_profile_snapshot=adaptive_profile_snapshot or {},
            management_revision_id=management_revision_id,
            management_assignment_id=management_assignment_id,
            management_profile_snapshot=management_profile_snapshot or {},
            activation_receipt_id=activation_receipt_id,
            activation_config_sha=activation_config_sha,
            adapter_capability_sha=adapter_capability_sha,
            eligible_candidates=selection.eligible_runtime_ids,
            rejected_candidates=rejected,
            normalized_scoring_inputs=normalized_inputs,
            final_scores=tuple(sorted(scores.items())),
            selected_profile_id=selection.selected_profile_id,
            selected_runtime=selection.selected_runtime.key,
            selected_reasoning_effort=selection.selected_reasoning_effort,
            projection_mode=projection_mode,
            selection_reason=selection.selection_reason,
            projected_fallback_chain=selection.fallbacks,
            safe_default_runtime=selection.safe_default_runtime.key,
            safe_default_reasoning_effort=selection.safe_default_reasoning_effort,
            classifier_runtime_id=classifier_runtime_id,
            classifier_input_tokens=classifier_input_tokens,
            classifier_output_tokens=classifier_output_tokens,
            classifier_cost_usd=classifier_cost_usd,
            routing_latency_seconds=routing_latency_seconds,
            safe_default_reason=selection.safe_default_reason,
            degradation_reason=degradation_reason,
        )
        semantic = {
            "scope": scope,
            "task_facts_hash": task_facts_hash,
            "inventory_revision": inventory_revision,
            "catalog_revision": catalog_revision,
            "authority_revision": authority_revision,
            "policy_revision": policy_revision,
            "adaptive_revision": adaptive_revision,
            "profile_adaptive_revision_id": profile_adaptive_revision_id,
            "adaptive_assignment_id": adaptive_assignment_id,
            "adaptive_profile_snapshot": dict(
                sorted((adaptive_profile_snapshot or {}).items())
            ),
            "applied_rule_ids": list(applied_rule_ids),
            "projection_mode": projection_mode,
            "activation_config_sha": activation_config_sha,
            "adapter_capability_sha": adapter_capability_sha,
            "selection": selection.semantic_record(),
        }
        if (
            management_revision_id is not None
            or management_assignment_id is not None
            or management_profile_snapshot
        ):
            semantic["management_revision_id"] = management_revision_id
            semantic["management_assignment_id"] = management_assignment_id
            semantic["management_profile_snapshot"] = dict(
                sorted((management_profile_snapshot or {}).items())
            )
        checksum = hashlib.sha256(
            json.dumps(
                semantic,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        _validate_decision_candidate_coherence(decision, selection.candidates)
        return BuiltDecision(
            decision=decision,
            candidates=selection.candidates,
            semantic_checksum=checksum,
        )


def _new_decision_id() -> str:
    return f"decision-{uuid.uuid4().hex}"


def _canonical_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["BuiltDecision", "DecisionBuilder"]
