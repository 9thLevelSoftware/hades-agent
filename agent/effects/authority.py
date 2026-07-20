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
    "enforce_transaction_authority",
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


# ── Transaction-stored authority contract ───────────────────────────────


def _resource_allowed(resource: str, allowed: str) -> bool:
    """Order-insensitive, separator-safe match of one declared resource.

    Exact match always passes. Otherwise both sides must share a scheme
    (the text before the first ``:``) and the allowed entry's path must
    be a whole-segment suffix of the concrete resource path — plans
    declare workspace-relative paths while adapters resolve absolutes.
    """
    if resource == allowed:
        return True
    scheme, _, resource_path = resource.partition(":")
    allowed_scheme, _, allowed_path = allowed.partition(":")
    if not allowed_path or scheme != allowed_scheme:
        return False
    normalized = resource_path.replace("\\", "/")
    tail = allowed_path.replace("\\", "/")
    if normalized == tail:
        return True
    return normalized.endswith("/" + tail) or normalized.endswith(":" + tail)


def enforce_transaction_authority(
    transaction, prepared: PreparedEffect, *, now_ms: int
) -> tuple[bool, str]:
    """Enforce the DURABLE per-transaction authority contract.

    The profile-wide Autonomy Center still evaluates afterwards; this
    check only narrows: an effect outside the transaction's declared
    ``allowed_actions``/``allowed_resources`` or past its expiry is
    denied even when profile authority would allow it. Expiry semantics:
    with ``issued_at_ms`` present the lifetime ``expires - issued`` is
    anchored at the transaction's creation time (fixture-controlled
    clocks stay meaningful); without it ``expires_at_ms`` is an absolute
    wall-clock deadline. Falsy/absent fields impose no constraint.
    """
    authority = dict(transaction.authority or {})

    allowed_actions = authority.get("allowed_actions")
    if allowed_actions is not None and prepared.action not in set(
        str(action) for action in allowed_actions
    ):
        return False, (
            f"action {prepared.action!r} is outside the transaction "
            f"authority's allowed_actions"
        )

    allowed_resources = authority.get("allowed_resources")
    if allowed_resources is not None:
        allowed_list = [str(entry) for entry in allowed_resources]
        for resource in prepared.resources:
            if not any(
                _resource_allowed(str(resource), entry)
                for entry in allowed_list
            ):
                return False, (
                    f"resource {resource!r} is outside the transaction "
                    f"authority's allowed_resources"
                )

    expires_at_ms = authority.get("expires_at_ms")
    if expires_at_ms:
        issued_at_ms = authority.get("issued_at_ms")
        if issued_at_ms:
            lifetime = max(0, int(expires_at_ms) - int(issued_at_ms))
            deadline = int(transaction.created_at_ms) + lifetime
        else:
            deadline = int(expires_at_ms)
        if now_ms >= deadline:
            return False, "the transaction authority has expired"

    return True, ""


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
