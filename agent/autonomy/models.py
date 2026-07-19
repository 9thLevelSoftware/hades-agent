"""Frozen public contract records for the Preferences & Autonomy Center.

This module defines the immutable authority vocabulary and record shapes
shared by the compiler, store, evaluator, service, and consumers such as
action transactions. It deliberately contains **no runtime behaviour** —
only frozen dataclasses with fail-closed validation.

Contract invariants enforced here:

- Decisions are deterministic ``allow``/``ask``/``deny``; conflict order
  is deny > ask > allow (enforced by the evaluator, vocabulary fixed here).
- ``learned_suggestion`` rules can never be ``active`` and never appear in
  a compiled contract; inferred preference is not authorization.
- Canonical authority is float-free: every numeric field is an integer
  (milliseconds, cents, minutes, parts-per-million).
- Missing high-risk facts must be declared as explicit ``unknown`` labels,
  never omitted or empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, get_args

AUTONOMY_CONTRACT_SCHEMA = "hades.autonomy.v1"

# ── Canonical finite vocabularies ───────────────────────────────────────────

DecisionVerdict = Literal["allow", "ask", "deny"]
RuleEffect = Literal["allow", "ask", "deny"]
RuleSource = Literal["user_assertion", "learned_suggestion", "temporary_mandate"]
RuleState = Literal[
    "active", "awaiting_confirmation", "rejected", "revoked", "expired", "consumed"
]
DataClass = Literal[
    "public", "internal", "personal", "confidential", "credential",
    "financial", "health", "unknown",
]
Reversibility = Literal["reversible", "compensatable", "irreversible", "unknown"]
DecisionStage = Literal["explain", "preview", "execute", "commit", "compensate"]
EvidenceStage = Literal["pre_action", "post_action"]

#: Normalized dotted action classes of the first proof set. Extension
#: classes are permitted only after normalization and are fail-closed when
#: no rule or adapter metadata describes them.
ACTION_CLASSES: frozenset[str] = frozenset(
    {
        "data.read",
        "data.share",
        "data.remember",
        "workspace.write",
        "workspace.delete",
        "message.send",
        "purchase.prepare",
        "purchase.commit",
        "model.route",
        "config.change",
        "workflow.change",
        "cron.change",
        "attention.interrupt",
        "unknown.mutation",
    }
)

_ACTION_CLASS_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_CONFIDENCE_PPM_MAX = 1_000_000
_MINUTES_PER_DAY = 1_440


# ── Validation helpers (fail closed, raise ValueError) ──────────────────────


def _check_int(
    name: str,
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    optional: bool = False,
) -> None:
    if value is None:
        if optional:
            return
        raise ValueError(f"{name} is required")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{name} must be an integer; floats and bools are rejected in "
            f"canonical authority (got {value!r})"
        )
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum} (got {value})")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum} (got {value})")


def _check_str(name: str, value: object, *, optional: bool = False) -> None:
    if value is None:
        if optional:
            return
        raise ValueError(f"{name} is required")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string (got {value!r})")


def _check_literal(name: str, value: object, literal_type: object) -> None:
    allowed = get_args(literal_type)
    if value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)} (got {value!r})")


def _check_action_class(name: str, value: object) -> None:
    if not isinstance(value, str) or not _ACTION_CLASS_RE.match(value):
        raise ValueError(
            f"{name} must be a normalized dotted action_class identifier "
            f"such as 'message.send' (got {value!r})"
        )


def _check_unique(name: str, values: tuple) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} contains duplicate selectors: {values!r}")


def _check_str_tuple(name: str, values: object) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{name} must be a tuple (got {type(values).__name__})")
    for item in values:
        _check_str(f"{name} entry", item)
    _check_unique(name, values)


# ── Provenance, scope, and constraints ──────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class RuleProvenance:
    """Who/what created a rule, when, and with what confidence."""

    actor_kind: str
    actor_id: str
    source_ref: str | None = None
    observed_at_ms: int
    confirmed_at_ms: int | None = None
    confidence_ppm: int = _CONFIDENCE_PPM_MAX

    def __post_init__(self) -> None:
        _check_str("actor_kind", self.actor_kind)
        _check_str("actor_id", self.actor_id)
        _check_str("source_ref", self.source_ref, optional=True)
        _check_int("observed_at_ms", self.observed_at_ms, minimum=0)
        _check_int("confirmed_at_ms", self.confirmed_at_ms, minimum=0, optional=True)
        _check_int(
            "confidence_ppm",
            self.confidence_ppm,
            minimum=0,
            maximum=_CONFIDENCE_PPM_MAX,
        )


@dataclass(frozen=True, kw_only=True)
class RuleScope:
    """Exact-match scope restrictions.

    Absence of a field means "no restriction on that dimension" — it never
    means another profile: profile isolation is structural (each profile
    owns its own config/state under ``get_hades_home()``).
    """

    profile_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    mission_id: str | None = None
    transaction_id: str | None = None
    resource_prefixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("profile_id", "task_id", "session_id", "mission_id", "transaction_id"):
            _check_str(name, getattr(self, name), optional=True)
        _check_str_tuple("resource_prefixes", self.resource_prefixes)
        for prefix in self.resource_prefixes:
            if ".." in prefix or "\\" in prefix:
                raise ValueError(
                    f"resource_prefixes must be normalized (no '..' or backslashes): {prefix!r}"
                )


@dataclass(frozen=True, kw_only=True)
class CostConstraint:
    """Integer-cent cost caps; a windowed cap requires an explicit window."""

    currency: str = "USD"
    max_per_action_cents: int | None = None
    max_per_window_cents: int | None = None
    window_ms: int | None = None

    def __post_init__(self) -> None:
        _check_str("currency", self.currency)
        _check_int("max_per_action_cents", self.max_per_action_cents, minimum=0, optional=True)
        _check_int("max_per_window_cents", self.max_per_window_cents, minimum=0, optional=True)
        _check_int("window_ms", self.window_ms, minimum=1, optional=True)
        if self.max_per_window_cents is not None and self.window_ms is None:
            raise ValueError("window_ms is required when max_per_window_cents is set")


@dataclass(frozen=True, kw_only=True)
class TimeConstraint:
    """Inclusive local-time window in minutes since midnight (0..1439)."""

    window_start_minute: int
    window_end_minute: int
    timezone: str = "local"

    def __post_init__(self) -> None:
        _check_int(
            "window_start_minute", self.window_start_minute,
            minimum=0, maximum=_MINUTES_PER_DAY - 1,
        )
        _check_int(
            "window_end_minute", self.window_end_minute,
            minimum=0, maximum=_MINUTES_PER_DAY - 1,
        )
        _check_str("timezone", self.timezone)


@dataclass(frozen=True, kw_only=True)
class EvidenceRequirement:
    """Named evidence that must exist before (or after) the action."""

    kind: str
    stage: EvidenceStage

    def __post_init__(self) -> None:
        _check_str("kind", self.kind)
        _check_literal("stage", self.stage, EvidenceStage)


# ── Rules ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class AutonomyRule:
    """One authority rule with exactly one source kind.

    ``user_assertion`` (config.yaml, may authorize), ``learned_suggestion``
    (state.db, may **never** authorize), or ``temporary_mandate``
    (state.db, may authorize within exact scope while unexpired/unconsumed).
    """

    rule_id: str
    source: RuleSource
    state: RuleState
    effect: RuleEffect
    action_classes: tuple[str, ...] = ()
    data_classes: tuple[str, ...] = ()
    recipient_classes: tuple[str, ...] = ()
    recipient_hashes: tuple[str, ...] = ()
    scope: RuleScope = field(default_factory=RuleScope)
    cost: CostConstraint | None = None
    time: TimeConstraint | None = None
    allowed_reversibility: tuple[str, ...] = ()
    evidence_requirements: tuple[EvidenceRequirement, ...] = ()
    max_uncertainty_ppm: int | None = None
    provenance: RuleProvenance
    created_at_ms: int = 0
    expires_at_ms: int | None = None
    max_uses: int | None = None
    remaining_uses: int | None = None
    description: str = ""

    def __post_init__(self) -> None:
        _check_str("rule_id", self.rule_id)
        _check_literal("source", self.source, RuleSource)
        _check_literal("state", self.state, RuleState)
        _check_literal("effect", self.effect, RuleEffect)

        if self.source == "learned_suggestion":
            if self.state not in ("awaiting_confirmation", "rejected"):
                raise ValueError(
                    "learned suggestions cannot authorize: state must be "
                    f"'awaiting_confirmation' or 'rejected' (got {self.state!r})"
                )
        elif self.source == "user_assertion":
            if self.state != "active":
                raise ValueError(
                    "user_assertion rules are durable and always 'active'; "
                    "remove or edit them instead of changing state "
                    f"(got {self.state!r})"
                )
        else:  # temporary_mandate
            if self.state not in ("active", "revoked", "expired", "consumed"):
                raise ValueError(
                    f"temporary_mandate state must be active/revoked/expired/"
                    f"consumed (got {self.state!r})"
                )
            if self.expires_at_ms is None and self.max_uses is None:
                raise ValueError(
                    "temporary_mandate must be bounded by expires_at_ms "
                    "and/or max_uses"
                )

        _check_str_tuple("action_classes", self.action_classes)
        for ac in self.action_classes:
            _check_action_class("action_classes entry", ac)
        if not isinstance(self.data_classes, tuple):
            raise ValueError("data_classes must be a tuple")
        for dc in self.data_classes:
            _check_literal("data_classes entry", dc, DataClass)
        _check_unique("data_classes", self.data_classes)
        _check_str_tuple("recipient_classes", self.recipient_classes)
        _check_str_tuple("recipient_hashes", self.recipient_hashes)
        for rv in self.allowed_reversibility:
            _check_literal("allowed_reversibility entry", rv, Reversibility)
        _check_unique("allowed_reversibility", self.allowed_reversibility)
        for req in self.evidence_requirements:
            if not isinstance(req, EvidenceRequirement):
                raise ValueError("evidence_requirements entries must be EvidenceRequirement")
        _check_int(
            "max_uncertainty_ppm",
            self.max_uncertainty_ppm,
            minimum=0,
            maximum=_CONFIDENCE_PPM_MAX,
            optional=True,
        )

        if self.effect == "allow" and not self.action_classes:
            raise ValueError(
                "allow rules require an explicit action selector; a wildcard "
                "allow is never valid"
            )

        _check_int("created_at_ms", self.created_at_ms, minimum=0)
        _check_int("expires_at_ms", self.expires_at_ms, minimum=0, optional=True)
        if self.expires_at_ms is not None and self.expires_at_ms < self.created_at_ms:
            raise ValueError(
                f"expires_at_ms ({self.expires_at_ms}) is an expiry before "
                f"creation ({self.created_at_ms})"
            )
        _check_int("max_uses", self.max_uses, minimum=1, optional=True)
        _check_int("remaining_uses", self.remaining_uses, minimum=0, optional=True)
        if self.remaining_uses is not None:
            if self.max_uses is None:
                raise ValueError("remaining_uses requires max_uses")
            if self.remaining_uses > self.max_uses:
                raise ValueError(
                    f"remaining_uses ({self.remaining_uses}) exceeds "
                    f"max_uses ({self.max_uses})"
                )

    @property
    def may_authorize(self) -> bool:
        """Whether this rule's source kind can ever participate in allow."""
        return self.source != "learned_suggestion"


# ── Action context ──────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class ActionContext:
    """Redactable, declared facts about one candidate action.

    High-risk dimensions must be declared explicitly: an unclassified
    payload is ``data_classes=("unknown",)``, an unresolved recipient is a
    ``None`` hash with no recipient_class — never an omitted field that a
    wildcard could silently match.
    """

    operation_key: str
    stage: DecisionStage
    action_class: str
    data_classes: tuple[str, ...] = ()
    reversibility: Reversibility = "unknown"
    recipient_class: str | None = None
    recipient_hash: str | None = None
    resource_refs: tuple[str, ...] = ()
    estimated_cost_cents: int | None = None
    local_time_minute: int | None = None
    profile_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    mission_id: str | None = None
    transaction_id: str | None = None
    tool_name: str | None = None
    present_evidence: tuple[str, ...] = ()
    occurred_at_ms: int | None = None
    uncertainty_ppm: int | None = None

    def __post_init__(self) -> None:
        _check_str("operation_key", self.operation_key)
        _check_literal("stage", self.stage, DecisionStage)
        _check_action_class("action_class", self.action_class)
        if not isinstance(self.data_classes, tuple) or not self.data_classes:
            raise ValueError(
                "data_classes must be declared explicitly; use ('unknown',) "
                "for unclassified content, never an empty high-risk field"
            )
        for dc in self.data_classes:
            _check_literal("data_classes entry", dc, DataClass)
        _check_unique("data_classes", self.data_classes)
        _check_literal("reversibility", self.reversibility, Reversibility)
        _check_str("recipient_class", self.recipient_class, optional=True)
        _check_str("recipient_hash", self.recipient_hash, optional=True)
        _check_str_tuple("resource_refs", self.resource_refs)
        _check_str_tuple("present_evidence", self.present_evidence)
        _check_int("estimated_cost_cents", self.estimated_cost_cents, minimum=0, optional=True)
        _check_int(
            "local_time_minute", self.local_time_minute,
            minimum=0, maximum=_MINUTES_PER_DAY - 1, optional=True,
        )
        for name in ("profile_id", "task_id", "session_id", "mission_id",
                     "transaction_id", "tool_name"):
            _check_str(name, getattr(self, name), optional=True)
        _check_int("occurred_at_ms", self.occurred_at_ms, minimum=0, optional=True)
        _check_int(
            "uncertainty_ppm",
            self.uncertainty_ppm,
            minimum=0,
            maximum=_CONFIDENCE_PPM_MAX,
            optional=True,
        )


# ── Compiled contract ───────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class AutonomyContract:
    """Immutable compiled snapshot of effective authority.

    Contains only active, authorizing rules (confirmed user assertions and
    active temporary mandates). Learned suggestions are shown beside the
    contract but are excluded from its rule set and hash.
    """

    schema: str = AUTONOMY_CONTRACT_SCHEMA
    version: int
    contract_hash: str
    profile_id: str
    compiled_at_ms: int
    rules: tuple[AutonomyRule, ...] = ()

    def __post_init__(self) -> None:
        _check_str("schema", self.schema)
        _check_int("version", self.version, minimum=1)
        _check_str("contract_hash", self.contract_hash)
        _check_str("profile_id", self.profile_id)
        _check_int("compiled_at_ms", self.compiled_at_ms, minimum=0)
        for r in self.rules:
            if not isinstance(r, AutonomyRule):
                raise ValueError("rules entries must be AutonomyRule")
            if r.source == "learned_suggestion":
                raise ValueError(
                    f"rule {r.rule_id!r} has source learned_suggestion; "
                    "suggestions are excluded from the compiled contract"
                )
            if r.state != "active":
                raise ValueError(
                    f"rule {r.rule_id!r} is {r.state!r}; only active rules "
                    "belong in a compiled contract"
                )
        _check_unique("rules (duplicate rule_id)", tuple(r.rule_id for r in self.rules))


# ── Clarification, budget, decisions ────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class ClarificationRequest:
    """Structured question for the existing clarify transport.

    Autonomy never adds another prompt protocol; this record is rendered
    through ``tools/clarify_tool.py`` / approval surfaces.
    """

    question: str
    choices: tuple[str, ...] = ()
    code: str = ""
    why_now: str = ""

    def __post_init__(self) -> None:
        _check_str("question", self.question)
        _check_str_tuple("choices", self.choices)
        if not isinstance(self.why_now, str):
            raise ValueError("why_now must be a string")


@dataclass(frozen=True, kw_only=True)
class BudgetReservation:
    """Bounded integer-cent reservation recorded before an allow returns."""

    reservation_id: str
    rule_id: str
    amount_cents: int
    window_key: str = ""
    created_at_ms: int

    def __post_init__(self) -> None:
        _check_str("reservation_id", self.reservation_id)
        _check_str("rule_id", self.rule_id)
        _check_int("amount_cents", self.amount_cents, minimum=0)
        _check_int("created_at_ms", self.created_at_ms, minimum=0)


def _check_decision_common(
    verdict: object,
    code: object,
    reason: object,
    context_hash: object,
    matched_rule_ids: object,
    conflicting_rule_ids: object,
    required_evidence: tuple,
    clarification: object,
    expires_at_ms: object,
    edit_targets: object,
) -> None:
    _check_literal("verdict", verdict, DecisionVerdict)
    _check_str("code", code)
    _check_str("reason", reason)
    _check_str("context_hash", context_hash)
    _check_str_tuple("matched_rule_ids", matched_rule_ids)
    _check_str_tuple("conflicting_rule_ids", conflicting_rule_ids)
    for req in required_evidence:
        if not isinstance(req, EvidenceRequirement):
            raise ValueError("required_evidence entries must be EvidenceRequirement")
    if clarification is not None and not isinstance(clarification, ClarificationRequest):
        raise ValueError("clarification must be a ClarificationRequest or None")
    if verdict == "allow" and clarification is not None:
        raise ValueError("an allow verdict cannot carry a clarification request")
    _check_int("expires_at_ms", expires_at_ms, minimum=0, optional=True)
    _check_str_tuple("edit_targets", edit_targets)


@dataclass(frozen=True, kw_only=True)
class AuthorityDecisionDraft:
    """Evaluator output before the service binds identity and consumes.

    Carries the mandate IDs and budget rule selected for atomic
    consumption. ``AutonomyService`` assigns the decision ID, binds the
    current contract version/hash, performs consumption/reservation, and
    returns an :class:`AuthorityDecision`; callers never persist a draft
    directly.
    """

    verdict: DecisionVerdict
    code: str
    reason: str
    context_hash: str
    matched_rule_ids: tuple[str, ...] = ()
    conflicting_rule_ids: tuple[str, ...] = ()
    required_evidence: tuple[EvidenceRequirement, ...] = ()
    clarification: ClarificationRequest | None = None
    expires_at_ms: int | None = None
    edit_targets: tuple[str, ...] = ()
    consume_mandate_ids: tuple[str, ...] = ()
    budget_rule_id: str | None = None

    def __post_init__(self) -> None:
        _check_decision_common(
            self.verdict, self.code, self.reason, self.context_hash,
            self.matched_rule_ids, self.conflicting_rule_ids,
            self.required_evidence, self.clarification,
            self.expires_at_ms, self.edit_targets,
        )
        _check_str_tuple("consume_mandate_ids", self.consume_mandate_ids)
        _check_str("budget_rule_id", self.budget_rule_id, optional=True)
        if self.verdict != "allow" and (self.consume_mandate_ids or self.budget_rule_id):
            raise ValueError(
                "only an allow draft may plan mandate consumption or a "
                "budget reservation"
            )


@dataclass(frozen=True)
class AuthorityDecision:
    """One recorded deterministic authority decision.

    ``allow`` is current authority, not proof of completion.
    """

    decision_id: str
    verdict: DecisionVerdict
    code: str
    reason: str
    authority_version: int
    authority_hash: str
    context_hash: str
    matched_rule_ids: tuple[str, ...]
    conflicting_rule_ids: tuple[str, ...]
    required_evidence: tuple[EvidenceRequirement, ...]
    clarification: ClarificationRequest | None
    expires_at_ms: int | None
    edit_targets: tuple[str, ...]
    budget_reservation: BudgetReservation | None

    def __post_init__(self) -> None:
        _check_str("decision_id", self.decision_id)
        _check_int("authority_version", self.authority_version, minimum=1)
        _check_str("authority_hash", self.authority_hash)
        _check_decision_common(
            self.verdict, self.code, self.reason, self.context_hash,
            self.matched_rule_ids, self.conflicting_rule_ids,
            self.required_evidence, self.clarification,
            self.expires_at_ms, self.edit_targets,
        )
        if self.budget_reservation is not None and not isinstance(
            self.budget_reservation, BudgetReservation
        ):
            raise ValueError("budget_reservation must be a BudgetReservation or None")
        if self.verdict != "allow" and self.budget_reservation is not None:
            raise ValueError("only an allow decision may carry a budget reservation")

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"

    @property
    def requires_approval(self) -> bool:
        return self.verdict == "ask"
