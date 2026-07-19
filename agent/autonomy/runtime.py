"""Execution-stage authority gate for the Preferences & Autonomy Center.

``authority_gate()`` wraps the TRUE terminal tool call installed by
``hades_cli.middleware.run_tool_execution_middleware()`` — it therefore
always sees the FINAL post-plugin arguments, and those arguments are the
authorized identity. Mode/config/provider lookup happens at execution
time; the system prompt, cached prefix, and model-visible tool schemas
are never touched.

Mode semantics (``autonomy.mode`` in profile-local ``config.yaml``):

- ``off``     — passthrough; no autonomy evaluation, no DB write;
- ``shadow``  — evaluate and record the candidate verdict (best effort),
  but preserve current execution/approval behaviour exactly;
- ``enforce`` — deterministic allow/ask/deny gates the handler:
  * ``deny`` blocks before the handler with a structured explanation;
  * ``ask`` escalates through the existing recoverable approval gate
    (``tools.approval.request_tool_approval``) with a rule key of
    ``autonomy:<contract-hash>:<context-hash>`` and the [a]lways option
    hidden. An explicit once/session answer creates an exact bounded
    ``temporary_mandate`` and re-evaluates under the new contract; a
    denial records deny. The evaluator's bounded
    :class:`~agent.autonomy.models.ClarificationRequest` is returned as
    structured tool output so the model may use the existing ``clarify``
    tool — no message is ever injected;
  * ``allow`` installs a context-local one-use :class:`AuthorityGrant`
    bound to the exact operation identity, which
    ``tools.approval._run_approval_gate()`` may consume in place of a
    redundant recoverable prompt. Hardline blocks, user deny rules, and
    exact irreversible transaction approvals remain stronger boundaries
    a grant can never satisfy.

Fail-closed invariants:

- registry-proven read-only operations bypass the gate and never gain
  (or grant) mutation authority;
- an invalid stable-authority section, a pending crashed config apply,
  or an audit/store failure blocks mutating enforce-mode calls;
- learned suggestions never participate (enforced by the evaluator);
- exceptions from the wrapped tool always propagate unchanged and the
  grant is cleared on every exit path.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import secrets
import time
from contextvars import ContextVar, Token
from typing import Any, Callable, Dict, Mapping, Optional, get_args

from agent.autonomy.canonical import normalize_action_class
from agent.autonomy.compiler import validate_autonomy_section
from agent.autonomy.config_apply import _read_raw_config
from agent.autonomy.models import (
    ActionContext,
    AuthorityDecision,
    DataClass,
    Reversibility,
)
from agent.autonomy.service import StoredAuthorityProvider, authorize_effect
from agent.autonomy.store import DecisionRecord

logger = logging.getLogger(__name__)

__all__ = [
    "AuthorityGrant",
    "argument_hash",
    "authority_gate",
    "clear_authority_grant",
    "consume_exact_authority_grant",
    "set_authority_grant",
    "structured_authority_block",
]

#: Bound lifetime of a session-scoped approval mandate. Sessions have no
#: portable end signal, so "session" answers become an expiring mandate
#: additionally scoped to the exact session ID when one is present.
SESSION_MANDATE_TTL_MS = 8 * 60 * 60 * 1000

_GATE_STAGE = "execute"

_VALID_DATA_CLASSES = frozenset(get_args(DataClass))
_VALID_REVERSIBILITY = frozenset(get_args(Reversibility))


# ── AuthorityGrant (context-local, one-use) ────────────────────────────────


@dataclasses.dataclass(frozen=True, kw_only=True)
class AuthorityGrant:
    """Exact, one-use downstream witness of a current allow decision.

    Bound to the operation key, tool name, and hash of the FINAL
    arguments. ``satisfies_generic_approval`` is False for irreversible
    actions — a grant can never stand in for an exact irreversible
    transaction approval.
    """

    operation_key: str
    tool_name: str
    argument_hash: str
    decision_id: str
    contract_version: int
    contract_hash: str
    expires_at_ms: Optional[int] = None
    satisfies_generic_approval: bool = True


_CURRENT_GRANT: ContextVar[Optional[AuthorityGrant]] = ContextVar(
    "_autonomy_authority_grant", default=None
)


def argument_hash(arguments: Optional[Mapping[str, Any]]) -> str:
    """Stable hash of one exact argument payload (same dump rules as
    ``ToolRegistry.operation_key``)."""
    canonical = json.dumps(
        dict(arguments or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def set_authority_grant(grant: Optional[AuthorityGrant]) -> Token:
    """Install *grant* as the context-local current grant."""
    if grant is not None and not isinstance(grant, AuthorityGrant):
        raise ValueError("grant must be an AuthorityGrant or None")
    return _CURRENT_GRANT.set(grant)


def clear_authority_grant(token: Optional[Token] = None) -> None:
    """Clear the current grant (restoring *token*'s prior value if given)."""
    if token is not None:
        _CURRENT_GRANT.reset(token)
    else:
        _CURRENT_GRANT.set(None)


def consume_exact_authority_grant(
    *,
    tool_name: str,
    arguments: Optional[Mapping[str, Any]] = None,
    now_ms: Optional[int] = None,
) -> Optional[AuthorityGrant]:
    """Consume the current grant iff it exactly matches this call.

    One-use: a successful match clears the grant. A mismatched tool name
    or argument hash never consumes (and never satisfies); an expired or
    exact-approval-only (``satisfies_generic_approval=False``) grant is
    discarded without satisfying anything.
    """
    grant = _CURRENT_GRANT.get()
    if grant is None:
        return None
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if grant.expires_at_ms is not None and grant.expires_at_ms <= now:
        _CURRENT_GRANT.set(None)
        return None
    if not grant.satisfies_generic_approval:
        return None
    if grant.tool_name != tool_name:
        return None
    if grant.argument_hash != argument_hash(arguments):
        return None
    _CURRENT_GRANT.set(None)
    return grant


# ── Structured block/ask output ─────────────────────────────────────────────


def structured_authority_block(
    decision: AuthorityDecision,
    *,
    tool_name: str,
    stage: str = _GATE_STAGE,
    approval: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Bounded structured tool output for a non-allow authority decision.

    For ``ask`` the evaluator's bounded clarification is included so the
    model may relay it through the existing ``clarify`` tool; nothing is
    ever injected into the message stream, and only the user's explicit
    answer (via approval/CLI) can create new authority.
    """
    autonomy: Dict[str, Any] = {
        "verdict": decision.verdict,
        "code": decision.code,
        "reason": decision.reason,
        "decision_id": decision.decision_id,
        "contract_version": decision.authority_version,
        "contract_hash": decision.authority_hash,
        "context_hash": decision.context_hash,
        "matched_rule_ids": list(decision.matched_rule_ids),
        "conflicting_rule_ids": list(decision.conflicting_rule_ids),
        "edit_targets": list(decision.edit_targets),
        "tool_name": tool_name,
        "stage": stage,
    }
    if decision.clarification is not None:
        autonomy["clarification"] = {
            "question": decision.clarification.question,
            "choices": list(decision.clarification.choices)[:4],
            "code": decision.clarification.code,
            "why_now": decision.clarification.why_now,
        }
    block: Dict[str, Any] = {
        "error": (
            f"Autonomy authority {decision.verdict} ({decision.code}): "
            f"{decision.reason}"
        ),
        "autonomy": autonomy,
    }
    if approval:
        block["approval"] = approval
    return block


def _failure_block(tool_name: str, code: str, message: str) -> Dict[str, Any]:
    """Fail-closed block when no decision could be produced at all."""
    return {
        "error": f"Autonomy authority deny ({code}): {message}",
        "autonomy": {
            "verdict": "deny",
            "code": code,
            "reason": message,
            "tool_name": tool_name,
            "stage": _GATE_STAGE,
        },
    }


def _block_result(block: Dict[str, Any]) -> str:
    return json.dumps(block, ensure_ascii=False)


# ── Context resolution ──────────────────────────────────────────────────────


def _open_db():
    from hades_constants import get_hades_home
    from hades_state import SessionDB

    return SessionDB(get_hades_home() / "state.db")


def _load_autonomy_section() -> Mapping[str, Any]:
    """Fail-closed read of the profile-local ``autonomy:`` section."""
    from hades_constants import get_hades_home

    raw = _read_raw_config(get_hades_home() / "config.yaml")
    return validate_autonomy_section(raw.get("autonomy"))


def _clean_str_tuple(value: Any) -> tuple:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    return tuple(
        dict.fromkeys(
            item for item in value if isinstance(item, str) and item.strip()
        )
    )


def _optional_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _optional_nonneg_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def resolve_action_context(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    operation_key: str,
    task_id: str = "",
    session_id: str = "",
    now_ms: Optional[int] = None,
) -> ActionContext:
    """Build the redacted :class:`ActionContext` for one candidate call.

    Consumes ``ToolRegistry.get_authority_context()`` metadata. A raw
    ``recipient_ref`` (e.g. a normalized delivery-target string) is
    converted to the profile-keyed recipient hash and discarded — raw
    recipient identifiers never enter the context or the audit trail.
    Anything malformed degrades to the explicit ``unknown`` label, never
    to an omitted (wildcard-matchable) field.
    """
    from tools.registry import registry

    meta = dict(registry.get_authority_context(tool_name, dict(args or {})))
    now = int(time.time() * 1000) if now_ms is None else now_ms

    try:
        action_class = normalize_action_class(meta.get("action_class"))
    except Exception:
        action_class = "unknown.mutation"

    data_classes = tuple(
        dc for dc in _clean_str_tuple(meta.get("data_classes"))
        if dc in _VALID_DATA_CLASSES
    ) or ("unknown",)

    reversibility = meta.get("reversibility")
    if reversibility not in _VALID_REVERSIBILITY:
        reversibility = "unknown"

    recipient_hash = _optional_str(meta.get("recipient_hash"))
    recipient_ref = _optional_str(meta.get("recipient_ref"))
    if recipient_hash is None and recipient_ref is not None:
        db = _open_db()
        try:
            recipient_hash = db.autonomy.hash_recipient(recipient_ref)
        finally:
            db.close()

    local_time = time.localtime(now / 1000)
    return ActionContext(
        operation_key=operation_key,
        stage=_GATE_STAGE,
        action_class=action_class,
        data_classes=data_classes,
        reversibility=reversibility,
        recipient_class=_optional_str(meta.get("recipient_class")),
        recipient_hash=recipient_hash,
        resource_refs=_clean_str_tuple(meta.get("resource_refs")),
        estimated_cost_cents=_optional_nonneg_int(meta.get("estimated_cost_cents")),
        local_time_minute=local_time.tm_hour * 60 + local_time.tm_min,
        task_id=_optional_str(task_id),
        session_id=_optional_str(session_id),
        tool_name=tool_name,
        present_evidence=_clean_str_tuple(meta.get("present_evidence")),
        occurred_at_ms=now,
        uncertainty_ppm=_optional_nonneg_int(meta.get("uncertainty_ppm")),
    )


# ── Shadow / denial audit writes ────────────────────────────────────────────


def _record_decision(decision: AuthorityDecision, *, operation_key: str) -> None:
    """Append one decision row (replay-safe; raises on store failure)."""
    record = DecisionRecord(
        decision=decision,
        operation_key=operation_key,
        stage=_GATE_STAGE,
        created_at_ms=int(time.time() * 1000),
    )
    db = _open_db()
    try:
        db.autonomy.consume_rules_and_record_decision(record, ())
    finally:
        db.close()


def _record_user_denial(
    ask_decision: AuthorityDecision, context: ActionContext
) -> AuthorityDecision:
    """Record the user's explicit denial of an autonomy ask (best effort)."""
    denied = AuthorityDecision(
        decision_id=f"dec-{secrets.token_hex(12)}",
        verdict="deny",
        code="approval_denied",
        reason=(
            "the user explicitly denied this action at the approval gate; "
            "do not retry it"
        ),
        authority_version=ask_decision.authority_version,
        authority_hash=ask_decision.authority_hash,
        context_hash=ask_decision.context_hash,
        matched_rule_ids=ask_decision.matched_rule_ids,
        conflicting_rule_ids=ask_decision.conflicting_rule_ids,
        required_evidence=(),
        clarification=None,
        expires_at_ms=None,
        edit_targets=ask_decision.edit_targets,
        budget_reservation=None,
    )
    try:
        # Distinct operation identity: the ask decision already holds this
        # operation's (key, stage, version, context) slot.
        _record_decision(denied, operation_key=f"{context.operation_key}#user-denial")
    except Exception as exc:
        logger.warning("autonomy: could not audit user denial: %s", exc)
    return denied


# ── Approval-answer → exact mandate ────────────────────────────────────────


def _mandate_from_context(
    context: ActionContext, *, choice: str, actor_id: str, now: int
):
    """Exact bounded mandate mirroring every declared context dimension."""
    from agent.autonomy.models import AutonomyRule, RuleProvenance, RuleScope

    scope = RuleScope(
        task_id=context.task_id,
        session_id=context.session_id,
        transaction_id=context.transaction_id,
        resource_prefixes=context.resource_refs,
    )
    if choice == "session":
        expires_at_ms: Optional[int] = now + SESSION_MANDATE_TTL_MS
        max_uses: Optional[int] = None
    else:  # exact one-use
        expires_at_ms = None
        max_uses = 1
    return AutonomyRule(
        rule_id=f"approval-{secrets.token_hex(6)}",
        source="temporary_mandate",
        state="active",
        effect="allow",
        action_classes=(context.action_class,),
        data_classes=context.data_classes,
        recipient_classes=(
            (context.recipient_class,) if context.recipient_class else ()
        ),
        recipient_hashes=(
            (context.recipient_hash,) if context.recipient_hash else ()
        ),
        scope=scope,
        allowed_reversibility=(context.reversibility,),
        provenance=RuleProvenance(
            actor_kind="user",
            actor_id=actor_id or "user",
            source_ref=f"approval:{context.operation_key}",
            observed_at_ms=now,
            confirmed_at_ms=now,
            confidence_ppm=1_000_000,
        ),
        created_at_ms=now,
        expires_at_ms=expires_at_ms,
        max_uses=max_uses,
        remaining_uses=max_uses,
        description=(
            f"explicit {choice or 'once'} approval of {context.action_class} "
            f"via the generic approval gate"
        ),
    )


def _resolve_ask(
    provider,
    context: ActionContext,
    ask_decision: AuthorityDecision,
    *,
    tool_name: str,
    effective_args: Mapping[str, Any],
    requester: str,
    channel: str,
) -> tuple[AuthorityDecision, Optional[Dict[str, Any]]]:
    """Escalate an ask through the existing recoverable approval gate.

    Returns ``(final_decision, approval_info)``. An explicit once/session
    answer creates an exact bounded mandate, then re-evaluates under the
    NEW contract — the answer itself never bypasses the evaluator, and a
    matching deny (or persisting ask rule) still stands.
    """
    from tools.approval import request_tool_approval

    rule_key = (
        f"autonomy:{ask_decision.authority_hash}:{ask_decision.context_hash}"
    )
    outcome = request_tool_approval(
        tool_name,
        f"Autonomy contract requires confirmation ({ask_decision.code}): "
        f"{ask_decision.reason}",
        rule_key=rule_key,
        arguments=dict(effective_args or {}),
        requester=requester,
        channel=channel,
        allow_permanent=False,
    )
    if not outcome.get("approved"):
        if outcome.get("status") == "approval_required":
            approval = {
                "status": "approval_required",
                **{
                    key: outcome[key]
                    for key in ("request_id", "argument_hash", "expires_at")
                    if key in outcome
                },
            }
            return ask_decision, approval
        if outcome.get("user_consent") is False or "denied" in str(
            outcome.get("message") or ""
        ).lower():
            return _record_user_denial(ask_decision, context), None
        return ask_decision, {
            "status": "unavailable",
            "message": str(outcome.get("message") or ""),
        }

    now = int(time.time() * 1000)
    choice = str(outcome.get("choice") or "once")
    try:
        from agent.autonomy.service import AutonomyService

        AutonomyService().create_mandate(
            _mandate_from_context(
                context, choice=choice, actor_id=requester, now=now
            ),
            now_ms=now,
        )
    except Exception as exc:
        logger.warning("autonomy: approval mandate creation failed: %s", exc)
        return ask_decision, {"status": "mandate_failed", "message": str(exc)}
    # Re-evaluate under the new contract; the mandate cannot override a
    # matching deny (or a persisting ask rule) — deny > ask > allow holds.
    return (
        authorize_effect(provider, context, stage=_GATE_STAGE, consume=True),
        None,
    )


# ── The gate ────────────────────────────────────────────────────────────────


def authority_gate(
    tool_name: str,
    effective_args: Any,
    terminal_call: Callable[[Any], Any],
    *,
    operation_metadata: Optional[Mapping[str, Any]] = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    requester: str = "",
    channel: str = "",
    **_ignored: Any,
) -> Any:
    """Gate one terminal tool execution on the current authority contract.

    Invoked exactly once per tool call, AFTER plugin argument
    finalization, with the final effective arguments — those arguments
    are the authorized identity and the operation key is derived from
    them at this boundary.
    """
    from tools.registry import registry

    args: Dict[str, Any] = (
        dict(effective_args) if isinstance(effective_args, dict) else {}
    )
    metadata = dict(operation_metadata or registry.get_operation_metadata(tool_name))

    # Registry-proven read-only operations never mutate and never gain
    # (or grant) mutation authority; they bypass the gate entirely.
    if metadata.get("read_only"):
        return terminal_call(effective_args)

    try:
        section = _load_autonomy_section()
        mode = section.get("mode", "off")
    except Exception as exc:
        # invalid_stable_authority: enforce is disabled by failing closed,
        # never by falling back to a partial rule set.
        return _block_result(
            _failure_block(
                tool_name,
                getattr(exc, "code", "invalid_stable_authority"),
                f"the stable authority configuration is invalid ({exc}); "
                "mutating calls fail closed until it is repaired",
            )
        )
    if mode == "off":
        return terminal_call(effective_args)

    operation_key = registry.operation_key(
        tool_name, args, task_id=task_id or "", tool_call_id=tool_call_id or ""
    )

    try:
        context = resolve_action_context(
            tool_name,
            args,
            operation_key=operation_key,
            task_id=task_id,
            session_id=session_id,
        )
    except Exception as exc:
        if mode == "enforce":
            return _block_result(
                _failure_block(
                    tool_name,
                    "invalid_action_context",
                    f"the action context could not be declared ({exc}); "
                    "an undeclarable mutation fails closed",
                )
            )
        logger.warning("autonomy shadow: context resolution failed: %s", exc)
        return terminal_call(effective_args)

    provider = StoredAuthorityProvider()

    if mode == "shadow":
        try:
            decision = authorize_effect(
                provider, context, stage=_GATE_STAGE, consume=False
            )
            _record_decision(decision, operation_key=operation_key)
        except Exception as exc:
            logger.warning(
                "autonomy shadow evaluation failed for %s: %s", tool_name, exc
            )
        return terminal_call(effective_args)

    # ── enforce ────────────────────────────────────────────────────────
    try:
        decision = authorize_effect(
            provider, context, stage=_GATE_STAGE, consume=True
        )
    except Exception as exc:
        # An unavailable/failing audit path never becomes an allow.
        return _block_result(
            _failure_block(
                tool_name,
                getattr(exc, "code", "authority_audit_failure"),
                f"the authority decision could not be evaluated and "
                f"audited ({exc}); mutating calls fail closed",
            )
        )

    approval_info: Optional[Dict[str, Any]] = None
    if decision.verdict == "ask":
        try:
            decision, approval_info = _resolve_ask(
                provider,
                context,
                decision,
                tool_name=tool_name,
                effective_args=args,
                requester=requester,
                channel=channel,
            )
        except Exception as exc:
            return _block_result(
                _failure_block(
                    tool_name,
                    "authority_audit_failure",
                    f"the approval escalation failed ({exc}); mutating "
                    "calls fail closed",
                )
            )

    if decision.verdict != "allow":
        return _block_result(
            structured_authority_block(
                decision, tool_name=tool_name, approval=approval_info
            )
        )

    grant = AuthorityGrant(
        operation_key=operation_key,
        tool_name=tool_name,
        argument_hash=argument_hash(args),
        decision_id=decision.decision_id,
        contract_version=decision.authority_version,
        contract_hash=decision.authority_hash,
        expires_at_ms=decision.expires_at_ms,
        satisfies_generic_approval=(context.reversibility != "irreversible"),
    )
    token = set_authority_grant(grant)
    try:
        return terminal_call(effective_args)
    finally:
        clear_authority_grant(token)
