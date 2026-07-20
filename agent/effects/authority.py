"""Map prepared effects into canonical authority, and bind exact approvals.

Item #6 (Preferences & Autonomy Center) owns rule parsing, expiry,
consumption, conflict resolution, budgets, and allow/ask/deny. This
module supplies only trusted adapter-normalized facts and owns exactly
one thing itself: transaction-specific approval bindings persisted in
``state.db`` so a restart never broadens consent.

An irreversible effect always needs its exact binding — session or
permanent allowlisting never translates into a transaction approval.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from agent.autonomy import ActionContext
from agent.effects.models import PreparedEffect
from tools.approval import request_tool_approval

__all__ = [
    "ApprovalBinding",
    "ApprovalConsumption",
    "ApprovalIdentity",
    "build_action_context",
    "consume_bound_approval",
    "request_bound_approval",
]

# Adapter-declared compensation fidelity -> canonical autonomy vocabulary.
_REVERSIBILITY_BY_FIDELITY = {
    "exact": "reversible",
    "semantic": "compensatable",
    "none": "irreversible",
}


def build_action_context(
    prepared: PreparedEffect,
    *,
    operation_key: str,
    transaction_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    profile_id: Optional[str] = None,
) -> ActionContext:
    """Initialize the immutable context at ``stage="preview"``.

    Only item #6's ``authorize_effect(..., stage=...)`` may replace the
    decision stage. Missing high-risk facts become explicit "unknown"
    declarations — never omitted fields a wildcard could silently match.
    """
    data_classes = tuple(prepared.data_classes) or ("unknown",)
    recipient_class = (
        prepared.recipient_classes[0] if prepared.recipient_classes else None
    )
    recipient_hash = (
        prepared.recipient_hashes[0] if prepared.recipient_hashes else None
    )
    cost_cents = (
        prepared.cost_usd_micros // 10_000 if prepared.cost_usd_micros else None
    )
    return ActionContext(
        operation_key=operation_key,
        stage="preview",
        action_class=prepared.action_class or "unknown.mutation",
        data_classes=data_classes,
        reversibility=_REVERSIBILITY_BY_FIDELITY.get(
            prepared.semantics.fidelity, "unknown"
        ),
        recipient_class=recipient_class,
        recipient_hash=recipient_hash,
        resource_refs=tuple(prepared.resources),
        estimated_cost_cents=cost_cents,
        transaction_id=transaction_id,
        tool_name=tool_name,
        profile_id=profile_id,
        uncertainty_ppm=prepared.uncertainty_ppm or None,
    )


# ── Exact approval bindings ─────────────────────────────────────────────


@dataclass(frozen=True)
class ApprovalIdentity:
    """The exact facts an irreversible approval is bound to.

    Any drift — arguments, preview, resources, authority version,
    requester, or channel — makes the binding unusable.
    """

    transaction_id: str
    revision: int
    node_id: str
    operation: str
    args_hash: str
    preview_hash: str
    resources: tuple[str, ...]
    authority_version: int
    requester: str
    channel: str


@dataclass(frozen=True)
class ApprovalBinding:
    approval_id: str
    transaction_id: str
    revision: int
    node_id: str
    operation: str
    args_hash: str
    preview_hash: str
    resources: tuple[str, ...]
    authority_version: int
    requester: str
    channel: str
    decision: str  # approved | denied
    expires_at_ms: int
    consumed_at_ms: Optional[int]
    created_at_ms: int

    def identity(self) -> ApprovalIdentity:
        return ApprovalIdentity(
            transaction_id=self.transaction_id,
            revision=self.revision,
            node_id=self.node_id,
            operation=self.operation,
            args_hash=self.args_hash,
            preview_hash=self.preview_hash,
            # Sorted so identity comparison is order-insensitive between
            # in-memory bindings and store-loaded rows.
            resources=tuple(sorted(self.resources)),
            authority_version=self.authority_version,
            requester=self.requester,
            channel=self.channel,
        )


@dataclass(frozen=True)
class ApprovalConsumption:
    approved: bool
    code: str  # approved | consumed | mismatch | expired | denied | missing
    binding: Optional[ApprovalBinding] = None


def _now_ms(clock) -> int:
    if clock is None:
        return int(time.time() * 1000)
    if hasattr(clock, "now_ms"):
        return int(clock.now_ms())
    return int(clock())


def consume_bound_approval(
    store, identity: ApprovalIdentity, *, clock=None
) -> ApprovalConsumption:
    """Atomically consume the one binding matching *identity* exactly.

    Fail-closed order: missing < mismatch < denied < expired < consumed.
    Only a live, approved, unconsumed, exactly-matching binding
    authorizes — and it authorizes exactly once.
    """
    candidates = store.find_approvals(
        identity.transaction_id, identity.revision, identity.node_id
    )
    if not candidates:
        return ApprovalConsumption(approved=False, code="missing")
    exact = [b for b in candidates if b.identity() == identity]
    if not exact:
        return ApprovalConsumption(approved=False, code="mismatch")
    binding = exact[0]
    if binding.decision != "approved":
        return ApprovalConsumption(approved=False, code="denied", binding=binding)
    now = _now_ms(clock)
    if now >= binding.expires_at_ms:
        return ApprovalConsumption(approved=False, code="expired", binding=binding)
    if binding.consumed_at_ms is not None:
        return ApprovalConsumption(approved=False, code="consumed", binding=binding)
    if not store.consume_approval(binding.approval_id, now_ms=now):
        # Lost the CAS race: someone else consumed it first.
        return ApprovalConsumption(approved=False, code="consumed", binding=binding)
    return ApprovalConsumption(approved=True, code="approved", binding=binding)


def request_bound_approval(
    store,
    *,
    transaction_id: str,
    revision: int,
    node_id: str,
    operation: str,
    args_hash: str,
    preview_hash: str,
    resources: tuple[str, ...],
    authority_version: int,
    requester: str,
    channel: str,
    adapter_id: str,
    action: str,
    reason: str,
    ttl_ms: int,
    clock=None,
    approval_callback=None,
) -> Optional[ApprovalBinding]:
    """Escalate to the human gate and persist an exact binding on approve.

    Returns the approved binding, or ``None`` when the human denied, the
    gate timed out, or no human was present (the gate fails closed).
    ``allow_permanent=False`` because durable consent for irreversible
    transaction effects requires an explicit authority contract edit,
    never an allowlist entry.
    """
    identity_args = {
        "transaction_id": transaction_id,
        "revision": revision,
        "node_id": node_id,
        "operation": operation,
        "args_hash": args_hash,
        "preview_hash": preview_hash,
        "resources": list(resources),
        "authority_version": authority_version,
        "requester": requester,
        "channel": channel,
    }
    result = request_tool_approval(
        operation,
        reason,
        rule_key=f"transaction:{adapter_id}:{action}",
        approval_callback=approval_callback,
        arguments=identity_args,
        requester=requester,
        channel=channel,
        allow_permanent=False,
    )
    if not result or not result.get("approved"):
        return None
    now = _now_ms(clock)
    binding = ApprovalBinding(
        approval_id=f"ap-{uuid.uuid4().hex}",
        transaction_id=transaction_id,
        revision=revision,
        node_id=node_id,
        operation=operation,
        args_hash=args_hash,
        preview_hash=preview_hash,
        resources=tuple(resources),
        authority_version=authority_version,
        requester=requester,
        channel=channel,
        decision="approved",
        expires_at_ms=now + int(ttl_ms),
        consumed_at_ms=None,
        created_at_ms=now,
    )
    store.insert_approval(binding)
    return binding
