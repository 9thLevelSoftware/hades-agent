"""Content-free serializers for persisted routing explanations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .models import RoutingDecision

EXPLANATION_SCHEMA = "auto-routing-decision-explanation-v1"

_CONCISE_DECISION_FIELDS = (
    "decision_id",
    "scope",
    "session_id",
    "task_id",
    "operation_id",
    "task_index",
    "created_at",
    "applied_rule_ids",
    "assessment",
    "selected_profile_id",
    "selected_runtime",
    "selected_reasoning_effort",
    "projection_mode",
    "selection_reason",
    "safe_default_runtime",
    "safe_default_reasoning_effort",
    "safe_default_reason",
    "degradation_reason",
    "routing_latency_seconds",
)


def serialize_decision_explanation(
    decision: RoutingDecision,
    *,
    candidates: Sequence[Any] = (),
    detailed: bool = False,
) -> dict[str, Any]:
    """Serialize only the immutable, content-free routing decision bundle."""
    if not isinstance(decision, RoutingDecision):
        raise TypeError("a validated routing decision is required")
    full = decision.model_dump(mode="json")
    if detailed:
        decision_record = full
    else:
        decision_record = {name: full[name] for name in _CONCISE_DECISION_FIELDS}
        decision_record["candidate_summary"] = {
            "eligible": len(decision.eligible_candidates),
            "rejected": len(decision.rejected_candidates),
            "scored": len(decision.final_scores),
            "fallbacks": len(decision.projected_fallback_chain),
        }
        decision_record["provenance"] = {
            "inventory_revision": decision.inventory_revision,
            "catalog_revision": decision.catalog_revision,
            "authority_revision": decision.authority_revision,
            "policy_revision": decision.policy_revision,
            "adaptive_revision": decision.adaptive_revision,
            "profile_adaptive_revision_id": decision.profile_adaptive_revision_id,
            "adaptive_assignment_id": decision.adaptive_assignment_id,
            "adaptive_profile_snapshot": dict(decision.adaptive_profile_snapshot),
            "activation_receipt_id": decision.activation_receipt_id,
        }
        decision_record["classifier_accounting"] = {
            "runtime_id": decision.classifier_runtime_id,
            "input_tokens": decision.classifier_input_tokens,
            "output_tokens": decision.classifier_output_tokens,
            "cost_usd": decision.classifier_cost_usd,
        }
    payload: dict[str, Any] = {
        "schema": EXPLANATION_SCHEMA,
        "redacted": True,
        "detail": "detailed" if detailed else "concise",
        "decision": decision_record,
    }
    if detailed:
        payload["candidates"] = [
            candidate.model_dump(mode="json") for candidate in candidates
        ]
    return payload


__all__ = ["EXPLANATION_SCHEMA", "serialize_decision_explanation"]
