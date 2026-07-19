"""Pure deterministic authority evaluator for the Autonomy Center.

``evaluate_contract()`` turns one immutable contract snapshot plus one
declared :class:`ActionContext` into an :class:`AuthorityDecisionDraft`.
It performs **no I/O**: persistence, decision-ID binding, mandate
consumption, and budget reservation belong to ``AutonomyService``
(Task 5), which consumes the draft atomically.

Evaluation order (fixed by the plan's authority semantics):

1. validate the context; an unrecognized extension action class that no
   rule describes fails closed;
2. discard inactive, expired, exhausted, wrong-profile, and
   wrong-task/session/mission/transaction rules;
3. exclude every ``learned_suggestion`` regardless of confidence;
4. evaluate hard constraints — credential/financial/health data, unknown
   recipients/data/reversibility, cost caps and windows, time windows,
   uncertainty, missing pre-action evidence;
5. combine matching rules: any deny wins, otherwise any ask wins,
   otherwise allow requires at least one clean allow; no match uses the
   configured conservative default (never ``allow``);
6. union required post-action evidence and return a complete explanation
   naming matches, conflicts, absent facts, and exact edit routes.

Matching is by intersection: every dimension a rule declares must accept
the context. Empty selectors leave a dimension unconstrained, **except**
that unknown values require an explicit ``unknown`` selector and
credential/financial/health data can only be *authorized* by a rule that
explicitly declares the data class plus a recipient selector. Resource
prefixes match on segment boundaries, never raw string prefixes. Cost
compares in integer USD micros, time in minutes within declared local
windows (IANA timezone aware, DST safe), uncertainty in integer ppm.

Specific rules never silently override a matching deny; the explanation
may point at the deny for editing, but the decision stays denied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone as _utc_tz
from typing import Iterable, Mapping, Optional, Sequence

from agent.autonomy.canonical import context_hash
from agent.autonomy.models import (
    ACTION_CLASSES,
    ActionContext,
    AuthorityDecisionDraft,
    AutonomyRule,
    ClarificationRequest,
    EvidenceRequirement,
    TimeConstraint,
)

__all__ = [
    "BudgetUsage",
    "ConflictExplanation",
    "EXACT_APPROVAL_EVIDENCE",
    "MICROS_PER_CENT",
    "OUTBOUND_ACTION_CLASSES",
    "SENSITIVE_DATA_CLASSES",
    "evaluate_contract",
    "explain_conflict",
    "matching_rules",
    "required_pre_action_evidence",
]

#: Snapshot of held-or-settled window spend per rule, in integer USD
#: micros — exactly what ``AutonomyStore.window_spend_micros()`` returns.
BudgetUsage = Mapping[str, int]

SENSITIVE_DATA_CLASSES = frozenset({"credential", "financial", "health"})

#: Action classes whose payload leaves the profile boundary.
OUTBOUND_ACTION_CLASSES = frozenset(
    {
        "data.share",
        "message.send",
        "purchase.prepare",
        "purchase.commit",
        "model.route",
    }
)

#: Evidence kind that satisfies the exact-approval boundary for
#: irreversible effects; its absence is always ``exact_approval_required``.
EXACT_APPROVAL_EVIDENCE = "exact_approval"

MICROS_PER_CENT = 10_000

_MINUTES_PER_DAY = 1_440

#: Constraint-violation codes in deterministic severity order for the
#: ask tier (deny-tier cost codes are extracted separately).
_ASK_VIOLATION_PRIORITY = (
    "exact_approval_required",
    "required_evidence_missing",
    "cost_unknown",
    "outside_time_window",
    "uncertainty_too_high",
)

_PRECEDENCE = "deny > ask > allow"


# ── Conflict explanation ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConflictExplanation:
    """Complete conservative explanation of one rule conflict.

    Confidence is display-only until confirmation and is deliberately
    absent here: no explanation ever claims that a higher-confidence
    suggestion changed the outcome.
    """

    rule_ids: tuple[str, ...]
    sources: tuple[str, ...]
    winning_effect: str
    edit_commands: tuple[str, ...]
    precedence: str = _PRECEDENCE


def explain_conflict(
    rules: Sequence[AutonomyRule], *, winning_effect: str
) -> ConflictExplanation:
    """Explain a conflict between *rules*, naming the winning precedence."""
    if not rules:
        raise ValueError("explain_conflict requires at least one rule")
    ordered = tuple(sorted(rules, key=lambda r: r.rule_id))
    return ConflictExplanation(
        rule_ids=tuple(r.rule_id for r in ordered),
        sources=tuple(f"{r.rule_id}={r.source}" for r in ordered),
        winning_effect=winning_effect,
        edit_commands=_edit_commands(r.rule_id for r in ordered),
    )


def _edit_commands(rule_ids: Iterable[str]) -> tuple[str, ...]:
    commands: list[str] = []
    for rule_id in sorted(set(rule_ids)):
        commands.append(f"hades autonomy rule explain {rule_id}")
        commands.append(f"hades autonomy rule edit {rule_id}")
    return tuple(commands)


# ── Dimension matching ──────────────────────────────────────────────────────


def _segment_prefix_accepts(prefix: str, ref: str) -> bool:
    """Segment-boundary prefix check: never a raw string prefix."""
    prefix = prefix.rstrip("/")
    return ref == prefix or ref.startswith(prefix + "/")


def _dimension_failures(rule: AutonomyRule, ctx: ActionContext) -> frozenset:
    """Which declared dimensions of *rule* reject *ctx* (empty = match)."""
    failures: set[str] = set()

    if rule.action_classes and ctx.action_class not in rule.action_classes:
        failures.add("action")

    data = set(ctx.data_classes)
    sensitive = data & SENSITIVE_DATA_CLASSES
    if rule.effect == "allow":
        if rule.data_classes:
            if not data <= set(rule.data_classes):
                failures.add("data")
        elif "unknown" in data or sensitive:
            # Wildcard data selectors never authorize unknown or
            # credential/financial/health payloads.
            failures.add("data")
        if sensitive and not (rule.recipient_classes or rule.recipient_hashes):
            # Sensitive data additionally requires an explicit recipient
            # selector on the authorizing rule.
            failures.add("data")
    else:
        if rule.data_classes and not data & set(rule.data_classes):
            failures.add("data")

    if rule.recipient_classes or rule.recipient_hashes:
        class_ok = (
            ctx.recipient_class is not None
            and ctx.recipient_class in rule.recipient_classes
        )
        unknown_ok = "unknown" in rule.recipient_classes and ctx.recipient_class in (
            None,
            "unknown",
        )
        hash_ok = (
            ctx.recipient_hash is not None
            and ctx.recipient_hash in rule.recipient_hashes
        )
        if not (class_ok or unknown_ok or hash_ok):
            failures.add("recipient")

    prefixes = rule.scope.resource_prefixes
    if prefixes:
        refs = ctx.resource_refs
        if rule.effect == "allow":
            if not refs or not all(
                any(_segment_prefix_accepts(p, ref) for p in prefixes)
                for ref in refs
            ):
                failures.add("resource")
        elif not refs or not any(
            _segment_prefix_accepts(p, ref) for p in prefixes for ref in refs
        ):
            failures.add("resource")

    if (
        rule.allowed_reversibility
        and ctx.reversibility not in rule.allowed_reversibility
    ):
        failures.add("reversibility")

    for name in ("task_id", "session_id", "mission_id", "transaction_id"):
        wanted = getattr(rule.scope, name)
        if wanted is not None and getattr(ctx, name) != wanted:
            failures.add("scope")

    if rule.scope.profile_id is not None and ctx.profile_id != rule.scope.profile_id:
        failures.add("profile")

    return frozenset(failures)


# ── Per-rule constraint checks (allow rules only) ───────────────────────────


def _local_minute(constraint: TimeConstraint, ctx: ActionContext) -> Optional[int]:
    tz_name = constraint.timezone
    if tz_name and tz_name != "local" and ctx.occurred_at_ms is not None:
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(tz_name)
        except Exception:
            return None  # unresolvable timezone → conservative
        local = datetime.fromtimestamp(
            ctx.occurred_at_ms // 1_000, tz=_utc_tz.utc
        ).astimezone(zone)
        return local.hour * 60 + local.minute
    return ctx.local_time_minute


def _in_window(constraint: TimeConstraint, minute: int) -> bool:
    start, end = constraint.window_start_minute, constraint.window_end_minute
    if start <= end:
        return start <= minute <= end
    return minute >= start or minute <= end  # window crosses midnight


def _allow_constraint_violations(
    rule: AutonomyRule, ctx: ActionContext, budget_usage: Mapping[str, int]
) -> tuple[str, ...]:
    violations: list[str] = []

    if rule.cost is not None and (
        rule.cost.max_per_action_cents is not None
        or rule.cost.max_per_window_cents is not None
    ):
        if ctx.estimated_cost_cents is None:
            violations.append("cost_unknown")
        else:
            estimated_micros = ctx.estimated_cost_cents * MICROS_PER_CENT
            cap = rule.cost.max_per_action_cents
            if cap is not None and estimated_micros > cap * MICROS_PER_CENT:
                violations.append("cost_per_action_exceeded")
            window_cap = rule.cost.max_per_window_cents
            if window_cap is not None:
                spent = budget_usage.get(rule.rule_id, 0)
                if spent + estimated_micros > window_cap * MICROS_PER_CENT:
                    violations.append("cost_budget_exceeded")

    if rule.time is not None:
        minute = _local_minute(rule.time, ctx)
        if minute is None or not _in_window(rule.time, minute):
            violations.append("outside_time_window")

    if rule.max_uncertainty_ppm is not None and (
        ctx.uncertainty_ppm is None
        or ctx.uncertainty_ppm > rule.max_uncertainty_ppm
    ):
        violations.append("uncertainty_too_high")

    missing = [
        req
        for req in rule.evidence_requirements
        if req.stage == "pre_action" and req.kind not in ctx.present_evidence
    ]
    if any(req.kind == EXACT_APPROVAL_EVIDENCE for req in missing):
        violations.append("exact_approval_required")
    elif missing:
        violations.append("required_evidence_missing")

    if ctx.reversibility == "irreversible" and (
        "irreversible" not in rule.allowed_reversibility
        or EXACT_APPROVAL_EVIDENCE not in ctx.present_evidence
    ):
        violations.append("exact_approval_required")

    return tuple(violations)


def _missing_pre_action(
    rules: Iterable[AutonomyRule], ctx: ActionContext
) -> tuple[EvidenceRequirement, ...]:
    missing = {
        (req.kind, req.stage): req
        for rule in rules
        for req in rule.evidence_requirements
        if req.stage == "pre_action" and req.kind not in ctx.present_evidence
    }
    return tuple(missing[key] for key in sorted(missing))


def _evidence_union(
    rules: Iterable[AutonomyRule], stage: str
) -> tuple[EvidenceRequirement, ...]:
    union = {
        (req.kind, req.stage): req
        for rule in rules
        for req in rule.evidence_requirements
        if req.stage == stage
    }
    return tuple(union[key] for key in sorted(union))


# ── Public matching helpers ─────────────────────────────────────────────────


def _partition_rules(contract, ctx: ActionContext, now_ms: int):
    """Split contract rules into active / expired / consumed, discarding
    suggestions and wrong-profile rules outright (evaluation steps 2-3)."""
    active: list[AutonomyRule] = []
    expired: list[AutonomyRule] = []
    consumed: list[AutonomyRule] = []
    for rule in tuple(getattr(contract, "rules", ()) or ()):
        if not isinstance(rule, AutonomyRule):
            continue
        if rule.source == "learned_suggestion":
            continue  # inferred preference is never authorization
        if rule.state != "active":
            continue
        if rule.scope.profile_id is not None and ctx.profile_id != rule.scope.profile_id:
            continue  # profile isolation: silently out of scope
        if rule.remaining_uses is not None and rule.remaining_uses <= 0:
            consumed.append(rule)
        elif rule.expires_at_ms is not None and rule.expires_at_ms <= now_ms:
            expired.append(rule)
        else:
            active.append(rule)
    key = lambda r: r.rule_id  # noqa: E731 — canonical ordering everywhere
    return sorted(active, key=key), sorted(expired, key=key), sorted(consumed, key=key)


def matching_rules(
    contract, context: ActionContext, *, now_ms: int
) -> tuple[AutonomyRule, ...]:
    """Active contract rules whose every declared dimension accepts *context*."""
    active, _, _ = _partition_rules(contract, context, now_ms)
    return tuple(r for r in active if not _dimension_failures(r, context))


def required_pre_action_evidence(
    contract, context: ActionContext, *, now_ms: int
) -> tuple[EvidenceRequirement, ...]:
    """Sorted, deduplicated pre-action evidence demanded by matching allow rules."""
    matched = matching_rules(contract, context, now_ms=now_ms)
    return _evidence_union(
        (r for r in matched if r.effect == "allow"), "pre_action"
    )


# ── Draft assembly ──────────────────────────────────────────────────────────


def _ids(rules: Iterable[AutonomyRule]) -> tuple[str, ...]:
    return tuple(sorted({r.rule_id for r in rules}))


def _edit_targets(rule_ids: Sequence[str], ctx: ActionContext) -> tuple[str, ...]:
    commands = _edit_commands(rule_ids)
    if commands:
        return commands
    return (f"hades autonomy rule add --action-class {ctx.action_class}",)


_CLARIFICATIONS = {
    "unknown_recipient": (
        "Who should receive this action's output?",
        (
            "a designated test recipient",
            "someone in this conversation",
            "a specific external contact",
            "cancel",
        ),
        "The recipient could not be resolved to any class or exact identity "
        "your rules authorize.",
    ),
    "recipient_mismatch": (
        "The recipient does not exactly match any rule. Which identity did you mean?",
        (
            "the exact recipient named in my rule",
            "a new recipient (add a rule first)",
            "cancel",
        ),
        "The resolved recipient hash differs from every exact recipient your "
        "rules name.",
    ),
    "cost_unknown": (
        "What is the maximum cost you authorize for this action?",
        (
            "up to my existing per-action cap",
            "a specific amount I will state",
            "zero spend only",
            "cancel",
        ),
        "A bounded-cost rule matched but no cost estimate is available.",
    ),
    "exact_approval_required": (
        "This action may be irreversible. How should it proceed?",
        (
            "approve exactly this action once",
            "use a reversible method instead",
            "cancel",
        ),
        "Irreversible effects require exact approval evidence before allow.",
    ),
    "conflicting_ask": (
        "Rules conflict for this action. What should happen this time?",
        (
            "proceed this once",
            "keep asking each time",
            "do not proceed",
        ),
        "An allow rule and an ask rule both match; ask wins conservatively.",
    ),
}


def _clarification(code: str) -> Optional[ClarificationRequest]:
    entry = _CLARIFICATIONS.get(code)
    if entry is None:
        return None
    question, choices, why_now = entry
    return ClarificationRequest(
        question=question, choices=choices[:4], code=code, why_now=why_now
    )


def _draft(
    verdict: str,
    code: str,
    reason: str,
    ctx: ActionContext,
    *,
    matched: Sequence[AutonomyRule] = (),
    conflicting: Sequence[AutonomyRule] = (),
    required_evidence: tuple[EvidenceRequirement, ...] = (),
    consume: tuple[str, ...] = (),
    budget_rule: Optional[str] = None,
) -> AuthorityDecisionDraft:
    matched_ids = _ids(matched)
    edit_source = _ids(tuple(matched) + tuple(conflicting))
    return AuthorityDecisionDraft(
        verdict=verdict,
        code=code,
        reason=reason,
        context_hash=context_hash(ctx),
        matched_rule_ids=matched_ids,
        conflicting_rule_ids=_ids(conflicting),
        required_evidence=required_evidence,
        clarification=None if verdict == "allow" else _clarification(code),
        expires_at_ms=None,
        edit_targets=_edit_targets(edit_source, ctx),
        consume_mandate_ids=consume if verdict == "allow" else (),
        budget_rule_id=budget_rule if verdict == "allow" else None,
    )


def _named(rules: Sequence[AutonomyRule]) -> str:
    return ", ".join(_ids(rules)) or "none"


# ── The evaluator ───────────────────────────────────────────────────────────


def evaluate_contract(
    contract,
    context: ActionContext,
    *,
    now_ms: int,
    budget_usage: Optional[BudgetUsage] = None,
    default_known_reversible: str = "ask",
    default_unknown_or_irreversible: str = "deny",
) -> AuthorityDecisionDraft:
    """Deterministically decide allow/ask/deny for *context* under *contract*.

    Pure function: identical inputs (including ``now_ms`` and the
    ``budget_usage`` micros snapshot) always produce an identical draft,
    independent of rule ordering. Conflict order is deny > ask > allow and
    the no-match default is configurable but can never be ``allow``.
    """
    if not isinstance(context, ActionContext):
        raise ValueError("context must be an ActionContext")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")
    for name, value in (
        ("default_known_reversible", default_known_reversible),
        ("default_unknown_or_irreversible", default_unknown_or_irreversible),
    ):
        if value not in ("ask", "deny"):
            raise ValueError(
                f"{name} must be 'ask' or 'deny'; a no-match default can "
                f"never be 'allow' (got {value!r})"
            )
    usage: dict[str, int] = {}
    for key, value in dict(budget_usage or {}).items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"budget_usage[{key!r}] must be a non-negative integer of "
                f"USD micros (got {value!r})"
            )
        usage[key] = value

    active, expired_rules, consumed_rules = _partition_rules(
        contract, context, now_ms
    )
    considered = active + expired_rules + consumed_rules

    # 1. Fail closed on extension action classes no rule describes.
    if context.action_class not in ACTION_CLASSES and not any(
        context.action_class in r.action_classes for r in considered
    ):
        return _draft(
            "deny",
            "unknown_action_class",
            f"action class {context.action_class!r} is not a known action "
            "and no rule describes it; unknown mutations fail closed",
            context,
        )

    failures = {r.rule_id: _dimension_failures(r, context) for r in active}
    matched = [r for r in active if not failures[r.rule_id]]
    matched_allows = [r for r in matched if r.effect == "allow"]
    matched_asks = [r for r in matched if r.effect == "ask"]
    matched_denies = [r for r in matched if r.effect == "deny"]

    violations = {
        r.rule_id: _allow_constraint_violations(r, context, usage)
        for r in matched_allows
    }
    clean_allows = [r for r in matched_allows if not violations[r.rule_id]]

    lapsed_expired = [
        r
        for r in expired_rules
        if r.effect == "allow" and not _dimension_failures(r, context)
    ]
    lapsed_consumed = [
        r
        for r in consumed_rules
        if r.effect == "allow" and not _dimension_failures(r, context)
    ]
    scope_near = [
        r
        for r in active
        if r.effect == "allow" and failures[r.rule_id] == frozenset({"scope"})
    ]
    resource_near = [
        r
        for r in active
        if r.effect == "allow" and failures[r.rule_id] == frozenset({"resource"})
    ]

    sensitive = set(context.data_classes) & SENSITIVE_DATA_CLASSES
    outbound = context.action_class in OUTBOUND_ACTION_CLASSES
    recipient_resolved = context.recipient_class not in (None, "unknown") or (
        context.recipient_hash is not None
        and any(context.recipient_hash in r.recipient_hashes for r in active)
    )
    explicitly_unknown_ok = any(
        "unknown" in r.recipient_classes for r in matched_allows
    )

    # ── Deny tier ──────────────────────────────────────────────────────
    if sensitive and outbound and not recipient_resolved:
        return _draft(
            "deny",
            "sensitive_data_boundary",
            f"{'/'.join(sorted(sensitive))}-labeled data cannot leave the "
            "profile toward an unresolved recipient; declare the exact "
            "recipient in an explicit rule first",
            context,
            matched=matched,
        )

    if matched_denies:
        sensitive_denies = [
            r for r in matched_denies if set(r.data_classes) & sensitive
        ]
        others = matched_allows + matched_asks
        if sensitive_denies:
            code = "sensitive_data_boundary"
            reason = (
                f"deny rule(s) {_named(sensitive_denies)} protect "
                f"{'/'.join(sorted(sensitive))} data"
                + (
                    f"; rule(s) {_named(others)} also matched but a deny "
                    "always wins"
                    if others
                    else ""
                )
            )
        elif others:
            code = "conflicting_deny"
            reason = (
                f"rules conflict: deny rule(s) {_named(matched_denies)} and "
                f"rule(s) {_named(others)} all match; precedence is "
                f"{_PRECEDENCE}, so the decision stays denied until you edit "
                "the deny"
            )
        else:
            code = "explicit_deny"
            reason = f"deny rule(s) {_named(matched_denies)} match this action"
        conflicting = matched if (others or sensitive_denies) else matched_denies
        return _draft(
            "deny", code, reason, context, matched=matched, conflicting=conflicting
        )

    if "unknown" in context.data_classes and outbound and not matched_allows:
        return _draft(
            "deny",
            "unknown_data_class",
            "the payload's data class is unknown and no rule explicitly "
            "authorizes unknown data to leave the profile",
            context,
            matched=matched,
        )

    if outbound and not recipient_resolved and not explicitly_unknown_ok:
        hash_near = context.recipient_hash is not None and any(
            r.recipient_hashes
            and (not r.action_classes or context.action_class in r.action_classes)
            for r in active
        )
        if hash_near:
            return _draft(
                "deny",
                "recipient_mismatch",
                "the resolved recipient does not exactly match any recipient "
                "your rules name (near-matches are never close enough)",
                context,
                matched=matched,
            )
        return _draft(
            "deny",
            "unknown_recipient",
            "the recipient is unresolved (no known class or exact identity); "
            "outbound actions to unknown recipients fail closed",
            context,
            matched=matched,
        )

    if (
        context.reversibility == "unknown"
        and matched_allows
        and not any("unknown" in r.allowed_reversibility for r in matched_allows)
    ):
        return _draft(
            "deny",
            "reversibility_unknown",
            f"rule(s) {_named(matched_allows)} match, but the action's "
            "reversibility is unknown and no rule explicitly accepts "
            "unknown reversibility",
            context,
            matched=matched,
        )

    if not matched_allows and resource_near:
        code = (
            "data_scope_mismatch"
            if context.action_class.startswith("data.")
            else "resource_scope_mismatch"
        )
        return _draft(
            "deny",
            code,
            f"rule(s) {_named(resource_near)} authorize only resources under "
            "their declared prefixes; this resource is outside every "
            "authorized scope (segment-boundary comparison)",
            context,
            matched=resource_near,
        )

    for deny_code in ("cost_per_action_exceeded", "cost_budget_exceeded"):
        offending = [
            r for r in matched_allows if deny_code in violations[r.rule_id]
        ]
        if offending:
            return _draft(
                "deny",
                deny_code,
                f"estimated cost violates the bounded-cost rule(s) "
                f"{_named(offending)} ({deny_code.replace('_', ' ')})",
                context,
                matched=matched,
            )

    if not matched_allows and lapsed_consumed:
        return _draft(
            "deny",
            "mandate_consumed",
            f"mandate(s) {_named(lapsed_consumed)} were already consumed; a "
            "consumed one-use authority never allows a replay",
            context,
            matched=lapsed_consumed,
        )

    expired_now = not matched_allows and lapsed_expired
    if expired_now and context.stage in ("commit", "compensate"):
        return _draft(
            "deny",
            "authority_expired",
            f"authority {_named(lapsed_expired)} expired before "
            f"{context.stage}; expired authority never allows at commit time",
            context,
            matched=lapsed_expired,
        )

    # ── Ask tier ───────────────────────────────────────────────────────
    if expired_now:
        return _draft(
            "ask",
            "authority_expired",
            f"authority {_named(lapsed_expired)} has expired; re-confirm to "
            "proceed",
            context,
            matched=lapsed_expired,
        )

    if not matched_allows and scope_near:
        return _draft(
            "ask",
            "scope_mismatch",
            f"rule(s) {_named(scope_near)} are scoped to a different "
            "task/session/mission/transaction; exact scope is required",
            context,
            matched=scope_near,
        )

    if matched_allows and not clean_allows:
        all_violations = {
            code for codes in violations.values() for code in codes
        }
        for code in _ASK_VIOLATION_PRIORITY:
            if code not in all_violations:
                continue
            offending = [
                r for r in matched_allows if code in violations[r.rule_id]
            ]
            evidence = (
                _missing_pre_action(offending, context)
                if code in ("required_evidence_missing", "exact_approval_required")
                else ()
            )
            return _draft(
                "ask",
                code,
                f"rule(s) {_named(offending)} match but do not authorize yet "
                f"({code.replace('_', ' ')}); the absent fact must be "
                "supplied or confirmed",
                context,
                matched=matched,
                required_evidence=evidence,
            )

    if matched_asks and clean_allows:
        return _draft(
            "ask",
            "conflicting_ask",
            f"rules conflict: allow rule(s) {_named(clean_allows)} and ask "
            f"rule(s) {_named(matched_asks)} all match; precedence is "
            f"{_PRECEDENCE}, so confirmation is required",
            context,
            matched=matched,
            conflicting=matched,
        )

    if matched_asks:
        return _draft(
            "ask",
            "explicit_ask",
            f"rule(s) {_named(matched_asks)} require confirmation for this "
            "action",
            context,
            matched=matched,
        )

    # ── Allow ──────────────────────────────────────────────────────────
    if clean_allows:
        code = (
            "explicit_allow"
            if any(r.source == "user_assertion" for r in clean_allows)
            else "temporary_mandate"
        )
        consume = tuple(
            sorted(
                r.rule_id
                for r in clean_allows
                if r.source == "temporary_mandate" and r.max_uses is not None
            )
        )
        budget_rule = min(
            (r.rule_id for r in clean_allows if r.cost is not None),
            default=None,
        )
        return _draft(
            "allow",
            code,
            f"allow rule(s) {_named(clean_allows)} explicitly authorize this "
            "action; allow is current authority, not proof of completion",
            context,
            matched=matched,
            required_evidence=_evidence_union(clean_allows, "post_action"),
            consume=consume,
            budget_rule=budget_rule,
        )

    # ── Conservative default ───────────────────────────────────────────
    deny_default = (
        context.reversibility in ("unknown", "irreversible")
        or "unknown" in context.data_classes
        or "credential" in context.data_classes
    )
    verdict = (
        default_unknown_or_irreversible if deny_default else default_known_reversible
    )
    return _draft(
        verdict,
        "no_authorizing_rule",
        "no active rule matches this action; the conservative default "
        f"({verdict}) applies — learned suggestions never authorize until "
        "confirmed",
        context,
    )
