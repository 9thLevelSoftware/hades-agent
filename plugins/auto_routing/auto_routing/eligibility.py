"""Shared fail-closed runtime eligibility gates.

These helpers are deliberately independent from selector scoring so both the
Stage 1 advisor and Stage 2 runtime selector apply identical hard policy and
capability semantics before any soft ranking.
"""

from __future__ import annotations

from typing import Any

from .inventory import ExecutableRuntime
from .models import PolicyEnvelope


def runtime_capability_rejection_reasons(
    runtime: ExecutableRuntime,
    *,
    required_capabilities: tuple[str, ...] = (),
    required_modalities: tuple[str, ...] = (),
    minimum_context_tokens: int = 0,
    minimum_output_tokens: int = 0,
) -> tuple[str, ...]:
    """Return fail-closed hard capability reasons before any soft scoring."""
    facts = runtime.capabilities
    reasons: list[str] = []
    for raw_capability in required_capabilities:
        capability = str(raw_capability).strip().casefold()
        if not capability:
            continue
        fact_name = (
            "supports_tools"
            if capability in {"tool", "tools", "tool_use"}
            else f"supports_{capability}"
        )
        supported = facts.get(fact_name)
        canonical = "tools" if fact_name == "supports_tools" else capability
        if supported is False:
            reasons.append(f"required_capability_unsupported:{canonical}")
        elif supported is not True:
            reasons.append(f"required_capability_unproven:{canonical}")

    supported_modalities = facts.get("input_modalities")
    normalized_modalities = (
        {
            str(value).strip().casefold()
            for value in supported_modalities
            if str(value).strip()
        }
        if isinstance(supported_modalities, (list, tuple, set, frozenset))
        else None
    )
    for raw_modality in required_modalities:
        modality = str(raw_modality).strip().casefold()
        if not modality:
            continue
        if normalized_modalities is None:
            reasons.append(f"required_modality_unproven:{modality}")
        elif modality not in normalized_modalities:
            reasons.append(f"required_modality_unsupported:{modality}")

    context_window = facts.get("context_window")
    if minimum_context_tokens:
        if (
            isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or context_window <= 0
        ):
            reasons.append("context_capacity_unproven")
        elif context_window < minimum_context_tokens:
            reasons.append("context_capacity_insufficient")

    max_output_tokens = facts.get("max_output_tokens")
    if minimum_output_tokens:
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens <= 0
        ):
            reasons.append("output_capacity_unproven")
        elif max_output_tokens < minimum_output_tokens:
            reasons.append("output_capacity_insufficient")
    return tuple(dict.fromkeys(reasons))


def runtime_policy_rejection_reasons(
    runtime: ExecutableRuntime,
    *,
    policy: PolicyEnvelope,
    catalog: Any,
) -> tuple[str, ...]:
    """Validate a current runtime against the immutable routing policy."""
    reasons: list[str] = []
    if runtime.key.provider in policy.denied_providers:
        reasons.append("provider_denied_by_policy")
    if runtime.key.model in policy.denied_models:
        reasons.append("model_denied_by_policy")
    if runtime.key.local_backend:
        if "installed_local_models" not in policy.eligible_sources:
            reasons.append("local_source_not_allowed")
        if (
            policy.local_models.require_open_weights
            and runtime.capabilities.get("open_weights") is not True
        ):
            reasons.append("open_weights_unproven")
        if (
            policy.local_models.require_compatible_hardware
            and runtime.capabilities.get("hardware_compatible") is not True
        ):
            reasons.append("hardware_compatibility_unproven")
        license_id = runtime.capabilities.get("license_id")
        if policy.allowed_licenses and license_id not in policy.allowed_licenses:
            reasons.append("license_not_allowed")
    elif "configured_providers" not in policy.eligible_sources:
        reasons.append("configured_provider_source_not_allowed")

    economics = runtime.economics
    throttle = str(economics.throttle_state or "").strip().casefold()
    if throttle in {"cooldown", "exhausted", "depleted", "rate_limited"}:
        reasons.append("runtime_throttled")
    if economics.billing_kind == "subscription":
        if not policy.allow_subscription:
            reasons.append("subscription_access_disallowed")
        subscription_state = str(
            economics.subscription_state or ""
        ).strip().casefold()
        remaining = economics.subscription_quota_remaining
        if subscription_state in {"exhausted", "depleted"} or (
            remaining is not None and remaining <= 0
        ):
            reasons.append("subscription_quota_exhausted")

    if economics.billing_kind in {"metered", "subscription"}:
        effective_costs = tuple(
            value
            for value in (
                economics.effective_marginal_cost_usd_per_task,
                economics.effective_amortized_cost_usd_per_task,
            )
            if value is not None
        )
        estimated_cost = max(effective_costs) if effective_costs else None
    else:
        local_costs = tuple(
            value
            for value in (
                economics.local_compute_cost_usd_per_task,
                economics.local_energy_cost_usd_per_task,
            )
            if value is not None
        )
        estimated_cost = sum(local_costs) if local_costs else None
    if estimated_cost is None:
        reasons.append("estimated_cost_unknown")
    elif estimated_cost > policy.max_estimated_task_cost_usd:
        reasons.append("estimated_cost_exceeds_limit")

    latency_rows = tuple(
        row
        for row in catalog.evidence_for(runtime)
        if row.metric_name == "latency" and not catalog.evidence_is_expired(row)
    )
    if not latency_rows:
        reasons.append("estimated_latency_unknown")
    elif max(row.value for row in latency_rows) > policy.max_estimated_latency_seconds:
        reasons.append("estimated_latency_exceeds_limit")

    reasons.extend(
        runtime_capability_rejection_reasons(
            runtime,
            required_modalities=("text",),
            minimum_context_tokens=policy.minimum_context_tokens,
        )
    )
    return tuple(dict.fromkeys(reasons))


__all__ = [
    "runtime_capability_rejection_reasons",
    "runtime_policy_rejection_reasons",
]
