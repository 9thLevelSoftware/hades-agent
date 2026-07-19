"""Authority provider and lifecycle service for the Autonomy Center.

:class:`AutonomyService` is the one runtime owner of authority: it keeps
the profile-local compiled contract head in sync with its sources
(``config.yaml`` stable assertions plus active ``state.db`` mandates),
evaluates declared action contexts through the pure evaluator, and binds
every consuming decision atomically to the audit trail, mandate
consumption, and bounded budget reservations.

:class:`StoredAuthorityProvider` is the canonical ``AuthorityProvider``
implementation consumed by action transactions (portfolio item #2) and
the execution middleware. Its contract:

- resolves only the active ``get_hades_home()`` profile — never another
  profile's config or database;
- recompiles the contract head whenever the config fingerprint or the
  active mandate set changed, on **every** call — commit/compensate never
  reuse a stale snapshot;
- fails closed (:class:`~agent.autonomy.config_apply.IncompleteAuthorityApply`)
  while a crashed config apply awaits recovery;
- ``evaluate(..., consume=True)`` performs replay check, decision
  append, mandate consumption, and cost reservation in short store
  transactions and never holds one across a prompt, handler, model, or
  network call.

Lifecycle invariants:

- ``propose_suggestion()`` always stores ``learned_suggestion`` /
  ``awaiting_confirmation`` regardless of what the caller passed;
  inferred preference is never authorization.
- ``confirm_suggestion()`` requires an explicit ``user`` actor and
  creates a *new* rule (stable config preview or bounded mandate) with
  fresh provenance; the suggestion row is resolved, never mutated into
  authority in place.
- Learned-suggestion rows have exactly two storable states
  (``awaiting_confirmation`` / ``rejected``); a converted suggestion is
  therefore stored terminally as ``rejected`` with the conversion lineage
  carried by the new rule's ``provenance.source_ref``
  (``suggestion:<id>``).
"""

from __future__ import annotations

import dataclasses
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Optional, Protocol, runtime_checkable

from agent.autonomy.compiler import parse_stable_rules, validate_autonomy_section
from agent.autonomy.config_apply import (
    INCOMPLETE_AUTHORITY_APPLY,
    AppliedConfigChange,
    ConfigChange,
    ConfigChangePreview,
    IncompleteAuthorityApply,
    apply_config_change,
    journal_path,
    pending_apply,
    preview_config_change,
    _profile_id_for,
    _read_raw_config,
    _sync_head,
)
from agent.autonomy.evaluator import MICROS_PER_CENT, evaluate_contract
from agent.autonomy.models import (
    ActionContext,
    AuthorityDecision,
    AuthorityDecisionDraft,
    AutonomyContract,
    AutonomyRule,
    BudgetReservation,
    DecisionStage,
    RuleProvenance,
    RuleScope,
)
from agent.autonomy.store import (
    AutonomyBudgetError,
    AutonomyStoreConflictError,
    DecisionRecord,
    StoredContractVersion,
    StoredRuntimeRule,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from hades_state import SessionDB

__all__ = [
    "AuthorityProvider",
    "AutonomyService",
    "AutonomyServiceError",
    "RuleExplanation",
    "StoredAuthorityProvider",
    "SuggestionConfirmation",
    "UnknownRuleError",
    "authorize_effect",
]

_CONSUMING_STAGES = frozenset({"execute", "commit", "compensate"})
_RUNTIME_SCOPE_FIELDS = (
    "profile_id", "task_id", "session_id", "mission_id", "transaction_id",
)
_PENDING_STATES = ("active", "awaiting_confirmation")


class AutonomyServiceError(RuntimeError):
    """A lifecycle request violates the authority contract — fail closed."""


class UnknownRuleError(AutonomyServiceError):
    """No stable or runtime rule exists with the requested ID."""


# ── Provider protocol and effect binding ────────────────────────────────────


@runtime_checkable
class AuthorityProvider(Protocol):
    """Canonical authority interface consumed by action transactions."""

    def current_contract(self) -> AutonomyContract: ...

    def authorize(
        self, context: ActionContext, *, consume: bool
    ) -> AuthorityDecision: ...


def authorize_effect(
    provider: AuthorityProvider,
    context: ActionContext,
    *,
    stage: DecisionStage,
    consume: Optional[bool] = None,
) -> AuthorityDecision:
    """Authorize *context* at *stage*; effectful stages consume by default."""
    return provider.authorize(
        dataclasses.replace(context, stage=stage),
        consume=(stage in _CONSUMING_STAGES if consume is None else consume),
    )


# ── Records ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class SuggestionConfirmation:
    """Outcome of an explicit user confirmation of one suggestion.

    ``destination="stable"`` carries a config-saga preview that the user
    must still apply; ``destination="mandate"`` already created the
    bounded mandate. Either way ``new_rule_id`` names the NEW rule — the
    suggestion itself never becomes authority.
    """

    suggestion_id: str
    destination: str
    new_rule_id: str
    actor_id: str
    preview: Optional[ConfigChangePreview] = None
    mandate: Optional[StoredRuntimeRule] = None

    @property
    def requires_apply(self) -> bool:
        return self.preview is not None

    @property
    def before_contract_hash(self) -> str:
        if self.preview is None:
            raise AutonomyServiceError(
                "a mandate confirmation is already committed; only a stable "
                "confirmation carries an apply preview"
            )
        return self.preview.before_contract_hash


@dataclass(frozen=True, kw_only=True)
class RuleExplanation:
    """Complete explanation of one rule: what it is, why, and how to edit.

    The full selectors/constraints/evidence live on ``rule``; the
    surrounding fields add layer, provenance label, current matchability,
    conflicts, and the exact command routes to edit or revoke it.
    """

    rule: AutonomyRule
    layer: str  # "stable_config" | "runtime"
    revision: Optional[int]
    provenance_label: str
    confidence_ppm: int
    in_current_contract: bool
    conflicts_with: tuple[str, ...]
    edit_route: tuple[str, ...]
    revoke_route: tuple[str, ...]

    @property
    def rule_id(self) -> str:
        return self.rule.rule_id

    @property
    def source(self) -> str:
        return self.rule.source

    @property
    def state(self) -> str:
        return self.rule.state

    @property
    def effect(self) -> str:
        return self.rule.effect

    @property
    def expires_at_ms(self) -> Optional[int]:
        return self.rule.expires_at_ms

    @property
    def max_uses(self) -> Optional[int]:
        return self.rule.max_uses

    @property
    def remaining_uses(self) -> Optional[int]:
        return self.rule.remaining_uses


def _routes(rule: AutonomyRule) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rid = rule.rule_id
    if rule.source == "user_assertion":
        return (
            (f"hades autonomy rule edit {rid}",),
            (f"hades autonomy rule remove {rid}",),
        )
    if rule.source == "learned_suggestion":
        return (
            (
                f"hades autonomy suggestion confirm {rid}",
                f"hades autonomy suggestion reject {rid}",
            ),
            (f"hades autonomy suggestion reject {rid}",),
        )
    return (
        (f"hades autonomy mandate revoke {rid}",),
        (f"hades autonomy mandate revoke {rid}",),
    )


# ── Service ─────────────────────────────────────────────────────────────────


class AutonomyService:
    """Profile-local authority lifecycle, evaluation, and audit service."""

    def __init__(self, db: Optional["SessionDB"] = None) -> None:
        self._db = db

    # ── Session / clock plumbing ───────────────────────────────────────

    @contextmanager
    def _session(self) -> Iterator["SessionDB"]:
        if self._db is not None:
            yield self._db
            return
        from hades_constants import get_hades_home
        from hades_state import SessionDB

        handle = SessionDB(get_hades_home() / "state.db")
        try:
            yield handle
        finally:
            handle.close()

    @staticmethod
    def _now(now_ms: Optional[int]) -> int:
        if now_ms is None:
            return int(time.time() * 1000)
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("now_ms must be a non-negative integer")
        return now_ms

    @staticmethod
    def _new_decision_id() -> str:
        return f"dec-{secrets.token_hex(12)}"

    @staticmethod
    def _require_recovered() -> None:
        if pending_apply():
            raise IncompleteAuthorityApply(
                f"{INCOMPLETE_AUTHORITY_APPLY}: a previous authority apply "
                f"did not complete ({journal_path()}); run recovery before "
                "evaluating or mutating authority"
            )

    @staticmethod
    def _require_user_actor(actor_kind: str, action: str) -> None:
        if actor_kind != "user":
            raise AutonomyServiceError(
                f"only the user may {action} (got actor_kind={actor_kind!r}); "
                "a suggestion can never confirm itself into authority"
            )

    # ── Contract sync ──────────────────────────────────────────────────

    def _sync(
        self, db: "SessionDB", now: int
    ) -> tuple[StoredContractVersion, Mapping[str, Any]]:
        """Fail-closed source read + head recompile-on-change."""
        self._require_recovered()
        from hades_constants import get_hades_home

        home = get_hades_home()
        raw = _read_raw_config(home / "config.yaml")
        section = validate_autonomy_section(raw.get("autonomy"))
        stored = _sync_head(db, raw, _profile_id_for(home), now)
        return stored, section

    def current_contract(self, *, now_ms: Optional[int] = None) -> StoredContractVersion:
        """The hash-verified current contract head (recompiled if stale)."""
        now = self._now(now_ms)
        with self._session() as db:
            stored, _ = self._sync(db, now)
            return stored

    # ── Evaluation ─────────────────────────────────────────────────────

    def _budget_snapshot(
        self, db: "SessionDB", contract: AutonomyContract, now: int
    ) -> dict[str, int]:
        usage: dict[str, int] = {}
        for rule in contract.rules:
            if rule.cost is not None and rule.cost.max_per_window_cents is not None:
                usage[rule.rule_id] = db.autonomy.window_spend_micros(
                    rule.rule_id, self._window_start(rule, now)
                )
        return usage

    @staticmethod
    def _window_start(rule: AutonomyRule, now: int) -> int:
        window_ms = rule.cost.window_ms if rule.cost is not None else None
        if window_ms:
            return (now // window_ms) * window_ms
        return 0

    def _decide(
        self, db: "SessionDB", context: ActionContext, now: int
    ) -> tuple[StoredContractVersion, Mapping[str, Any], AuthorityDecisionDraft]:
        stored, section = self._sync(db, now)
        draft = evaluate_contract(
            stored.contract,
            context,
            now_ms=now,
            budget_usage=self._budget_snapshot(db, stored.contract, now),
            default_known_reversible=section.get("default_known_reversible", "ask"),
            default_unknown_or_irreversible=section.get(
                "default_unknown_or_irreversible", "deny"
            ),
        )
        return stored, section, draft

    def _bind(
        self,
        draft: AuthorityDecisionDraft,
        stored: StoredContractVersion,
        *,
        now: int,
        ttl_ms: int,
        reservation: Optional[BudgetReservation],
    ) -> AuthorityDecision:
        return AuthorityDecision(
            decision_id=self._new_decision_id(),
            verdict=draft.verdict,
            code=draft.code,
            reason=draft.reason,
            authority_version=stored.version,
            authority_hash=stored.content_hash,
            context_hash=draft.context_hash,
            matched_rule_ids=draft.matched_rule_ids,
            conflicting_rule_ids=draft.conflicting_rule_ids,
            required_evidence=draft.required_evidence,
            clarification=draft.clarification,
            expires_at_ms=(now + ttl_ms) if draft.verdict == "allow" else draft.expires_at_ms,
            edit_targets=draft.edit_targets,
            budget_reservation=reservation,
        )

    def _fail_closed_decision(
        self,
        draft: AuthorityDecisionDraft,
        stored: StoredContractVersion,
        *,
        code: str,
        reason: str,
        matched: tuple[str, ...],
    ) -> AuthorityDecision:
        return AuthorityDecision(
            decision_id=self._new_decision_id(),
            verdict="deny",
            code=code,
            reason=reason,
            authority_version=stored.version,
            authority_hash=stored.content_hash,
            context_hash=draft.context_hash,
            matched_rule_ids=matched,
            conflicting_rule_ids=(),
            required_evidence=(),
            clarification=None,
            expires_at_ms=None,
            edit_targets=draft.edit_targets,
            budget_reservation=None,
        )

    @staticmethod
    def _existing_decision_id(
        db: "SessionDB",
        context: ActionContext,
        stored: StoredContractVersion,
        draft_context_hash: str,
    ) -> Optional[str]:
        """Replay identity pre-check (the store re-checks in-transaction)."""

        def _get(conn: sqlite3.Connection) -> Optional[str]:
            row = conn.execute(
                "SELECT decision_id FROM autonomy_decisions "
                "WHERE operation_key = ? AND stage = ? "
                "AND contract_version = ? AND context_hash = ?",
                (
                    context.operation_key,
                    context.stage,
                    stored.version,
                    draft_context_hash,
                ),
            ).fetchone()
            return row[0] if row else None

        return db._execute_read(_get)

    def _record_fail_closed(
        self, db: "SessionDB", decision: AuthorityDecision, context: ActionContext, now: int
    ) -> None:
        record = DecisionRecord(
            decision=decision,
            operation_key=context.operation_key,
            stage=context.stage,
            created_at_ms=now,
        )
        try:
            db.autonomy.consume_rules_and_record_decision(record, ())
        except AutonomyStoreConflictError:
            # A concurrent writer already decided this exact operation;
            # keep the fail-closed verdict for this caller regardless.
            pass

    def _commit(
        self,
        db: "SessionDB",
        stored: StoredContractVersion,
        context: ActionContext,
        draft: AuthorityDecisionDraft,
        *,
        now: int,
        ttl_ms: int,
    ) -> AuthorityDecision:
        replayed = self._existing_decision_id(db, context, stored, draft.context_hash)
        if replayed is not None:
            original = db.autonomy.get_decision(replayed)
            if original is not None:
                return original.decision

        # 1. Reserve bounded cost BEFORE the allow can return. A lost
        #    budget race fails closed to deny; the evaluator already
        #    denied over-cap requests against the snapshot, so this only
        #    trips under true concurrency.
        reservation: Optional[BudgetReservation] = None
        budget_rule: Optional[AutonomyRule] = None
        decision_id_for_ledger = self._new_decision_id()
        if (
            draft.verdict == "allow"
            and draft.budget_rule_id is not None
            and context.estimated_cost_cents is not None
        ):
            budget_rule = next(
                (r for r in stored.contract.rules if r.rule_id == draft.budget_rule_id),
                None,
            )
        if budget_rule is not None and budget_rule.cost is not None:
            window_start = self._window_start(budget_rule, now)
            cap = budget_rule.cost.max_per_window_cents
            try:
                entry_id = db.autonomy.reserve_budget(
                    rule_id=budget_rule.rule_id,
                    operation_key=context.operation_key,
                    decision_id=decision_id_for_ledger,
                    amount_micros=context.estimated_cost_cents * MICROS_PER_CENT,
                    window_started_at_ms=window_start,
                    max_window_micros=cap * MICROS_PER_CENT if cap is not None else None,
                    now_ms=now,
                )
            except AutonomyBudgetError as exc:
                denied = self._fail_closed_decision(
                    draft,
                    stored,
                    code="cost_budget_exceeded",
                    reason=(
                        "the budget reservation was refused at commit time "
                        f"({exc}); a lost budget race never becomes an allow"
                    ),
                    matched=(budget_rule.rule_id,),
                )
                self._record_fail_closed(db, denied, context, now)
                return denied
            reservation = BudgetReservation(
                reservation_id=entry_id,
                rule_id=budget_rule.rule_id,
                amount_cents=context.estimated_cost_cents,
                window_key=str(window_start),
                created_at_ms=now,
            )

        # 2. One short store transaction: replay re-check, decision
        #    append, and atomic mandate consumption.
        decision = self._bind(draft, stored, now=now, ttl_ms=ttl_ms, reservation=reservation)
        decision = dataclasses.replace(decision, decision_id=decision_id_for_ledger)
        record = DecisionRecord(
            decision=decision,
            operation_key=context.operation_key,
            stage=context.stage,
            created_at_ms=now,
        )
        try:
            result = db.autonomy.consume_rules_and_record_decision(
                record, draft.consume_mandate_ids
            )
        except AutonomyStoreConflictError as exc:
            # A mandate was consumed/revoked/expired by a concurrent
            # operation between snapshot and commit: release the cost
            # hold and fail closed with an audited deny.
            if reservation is not None and budget_rule is not None:
                db.autonomy.release_budget(
                    rule_id=budget_rule.rule_id,
                    operation_key=context.operation_key,
                    decision_id=decision_id_for_ledger,
                    window_started_at_ms=int(reservation.window_key),
                    now_ms=now,
                )
            denied = self._fail_closed_decision(
                draft,
                stored,
                code="mandate_consumed",
                reason=(
                    "the authorizing mandate was consumed by a concurrent "
                    f"operation before this one committed ({exc}); a "
                    "one-use authority never allows twice"
                ),
                matched=draft.consume_mandate_ids,
            )
            self._record_fail_closed(db, denied, context, now)
            return denied
        if result.replayed:
            if reservation is not None and budget_rule is not None:
                db.autonomy.release_budget(
                    rule_id=budget_rule.rule_id,
                    operation_key=context.operation_key,
                    decision_id=decision_id_for_ledger,
                    window_started_at_ms=int(reservation.window_key),
                    now_ms=now,
                )
            original = db.autonomy.get_decision(result.decision_id)
            return original.decision if original is not None else decision
        return decision

    def evaluate(
        self,
        context: ActionContext,
        *,
        consume: bool,
        now_ms: Optional[int] = None,
    ) -> AuthorityDecision:
        """Deterministically decide allow/ask/deny for *context*.

        ``consume=False`` (explain/preview/shadow) computes and returns
        the decision without persisting anything. ``consume=True``
        atomically appends the decision, consumes eligible mandates, and
        reserves bounded cost before an allow returns; replays of the
        same operation identity return the original decision.
        """
        if not isinstance(context, ActionContext):
            raise ValueError("context must be an ActionContext")
        now = self._now(now_ms)
        with self._session() as db:
            stored, section, draft = self._decide(db, context, now)
            ttl_ms = int(section.get("decision_ttl_seconds", 300)) * 1000
            if not consume:
                return self._bind(
                    draft, stored, now=now, ttl_ms=ttl_ms, reservation=None
                )
            return self._commit(db, stored, context, draft, now=now, ttl_ms=ttl_ms)

    # ── Stable rule authoring (config saga passthrough) ────────────────

    def preview_rule_change(
        self, change: ConfigChange, *, now_ms: Optional[int] = None
    ) -> ConfigChangePreview:
        """Preview a stable-rule change without writing anything."""
        with self._session() as db:
            return preview_config_change(change, db=db, now_ms=now_ms)

    def apply_rule_change(
        self,
        preview: ConfigChangePreview | SuggestionConfirmation,
        *,
        expected_contract_hash: str,
        now_ms: Optional[int] = None,
    ) -> AppliedConfigChange:
        """Apply a previewed change under exact-hash compare-and-set.

        Accepts either a raw :class:`ConfigChangePreview` or a stable
        :class:`SuggestionConfirmation`; the latter also resolves the
        source suggestion once the apply commits.
        """
        confirmation = preview if isinstance(preview, SuggestionConfirmation) else None
        cfg_preview = confirmation.preview if confirmation is not None else preview
        if not isinstance(cfg_preview, ConfigChangePreview):
            raise ValueError(
                "preview must be a ConfigChangePreview or a stable "
                "SuggestionConfirmation carrying one"
            )
        now = self._now(now_ms)
        with self._session() as db:
            applied = apply_config_change(
                cfg_preview,
                expected_contract_hash=expected_contract_hash,
                db=db,
                now_ms=now,
            )
            if confirmation is not None:
                self._resolve_suggestion(db, confirmation.suggestion_id, now)
            return applied

    # ── Suggestions ────────────────────────────────────────────────────

    def propose_suggestion(
        self, rule: AutonomyRule, *, now_ms: Optional[int] = None
    ) -> StoredRuntimeRule:
        """Store a learned suggestion; it can never authorize as stored."""
        self._require_recovered()
        if not isinstance(rule, AutonomyRule):
            raise ValueError("rule must be an AutonomyRule")
        if rule.provenance.actor_kind == "user":
            raise AutonomyServiceError(
                "a user's explicit statement is a durable assertion or "
                "mandate, never a learned suggestion; use "
                "preview_rule_change/create_mandate instead"
            )
        forced = dataclasses.replace(
            rule,
            source="learned_suggestion",
            state="awaiting_confirmation",
            max_uses=None,
            remaining_uses=None,
            provenance=dataclasses.replace(rule.provenance, confirmed_at_ms=None),
        )
        with self._session() as db:
            return db.autonomy.put_runtime_rule(
                forced, expected_revision=0, now_ms=self._now(now_ms)
            )

    def _load_suggestion(
        self, db: "SessionDB", suggestion_id: str
    ) -> StoredRuntimeRule:
        stored = db.autonomy.get_runtime_rule(suggestion_id)
        if stored is None or stored.rule.source != "learned_suggestion":
            raise UnknownRuleError(
                f"no learned suggestion {suggestion_id!r} exists"
            )
        return stored

    def _resolve_suggestion(
        self, db: "SessionDB", suggestion_id: str, now: int
    ) -> None:
        """Terminal-state the suggestion after its conversion committed."""
        stored = db.autonomy.get_runtime_rule(suggestion_id)
        if stored is None or stored.rule.state != "awaiting_confirmation":
            return
        db.autonomy.put_runtime_rule(
            dataclasses.replace(stored.rule, state="rejected"),
            expected_revision=stored.revision,
            now_ms=now,
        )

    def confirm_suggestion(
        self,
        suggestion_id: str,
        *,
        destination: str = "stable",
        actor_kind: str = "user",
        actor_id: str,
        new_rule_id: Optional[str] = None,
        expires_at_ms: Optional[int] = None,
        max_uses: Optional[int] = None,
        scope: Optional[RuleScope] = None,
        now_ms: Optional[int] = None,
    ) -> SuggestionConfirmation:
        """Explicit user confirmation: create NEW authority, never in place.

        ``destination="stable"`` returns a config-saga preview the caller
        must still :meth:`apply_rule_change`; ``destination="mandate"``
        creates a bounded (expiring and/or consumable) mandate now and
        resolves the suggestion.
        """
        self._require_recovered()
        self._require_user_actor(actor_kind, "confirm a suggestion")
        if destination not in ("stable", "mandate"):
            raise AutonomyServiceError(
                f"destination must be 'stable' or 'mandate' (got {destination!r})"
            )
        now = self._now(now_ms)
        rule_id = new_rule_id or f"{suggestion_id}-confirmed"
        with self._session() as db:
            stored = self._load_suggestion(db, suggestion_id)
            if stored.rule.state != "awaiting_confirmation":
                raise AutonomyServiceError(
                    f"suggestion {suggestion_id!r} is {stored.rule.state!r}, "
                    "not awaiting confirmation"
                )
            origin = stored.rule
            provenance = RuleProvenance(
                actor_kind="user",
                actor_id=actor_id,
                source_ref=f"suggestion:{suggestion_id}",
                observed_at_ms=origin.provenance.observed_at_ms,
                confirmed_at_ms=now,
                confidence_ppm=1_000_000,
            )
            if destination == "stable":
                if any(
                    getattr(origin.scope, name) is not None
                    for name in _RUNTIME_SCOPE_FIELDS
                ):
                    raise AutonomyServiceError(
                        f"suggestion {suggestion_id!r} carries runtime scope; "
                        "confirm it as a bounded mandate instead of a "
                        "durable assertion"
                    )
                new_rule = dataclasses.replace(
                    origin,
                    rule_id=rule_id,
                    source="user_assertion",
                    state="active",
                    provenance=provenance,
                    scope=RuleScope(
                        resource_prefixes=origin.scope.resource_prefixes
                    ),
                    max_uses=None,
                    remaining_uses=None,
                    created_at_ms=now,
                    expires_at_ms=None,
                )
                preview = preview_config_change(
                    ConfigChange(set_rules=(new_rule,)), db=db, now_ms=now
                )
                return SuggestionConfirmation(
                    suggestion_id=suggestion_id,
                    destination="stable",
                    new_rule_id=rule_id,
                    actor_id=actor_id,
                    preview=preview,
                )
            if expires_at_ms is None and max_uses is None:
                raise AutonomyServiceError(
                    "a temporary mandate must be bounded by expires_at_ms "
                    "and/or max_uses"
                )
            new_rule = dataclasses.replace(
                origin,
                rule_id=rule_id,
                source="temporary_mandate",
                state="active",
                provenance=provenance,
                scope=origin.scope if scope is None else scope,
                created_at_ms=now,
                expires_at_ms=expires_at_ms,
                max_uses=max_uses,
                remaining_uses=max_uses,
            )
            mandate = db.autonomy.put_runtime_rule(
                new_rule, expected_revision=0, now_ms=now
            )
            self._resolve_suggestion(db, suggestion_id, now)
            return SuggestionConfirmation(
                suggestion_id=suggestion_id,
                destination="mandate",
                new_rule_id=rule_id,
                actor_id=actor_id,
                mandate=mandate,
            )

    def reject_suggestion(
        self,
        suggestion_id: str,
        *,
        actor_kind: str = "user",
        actor_id: str,
        now_ms: Optional[int] = None,
    ) -> StoredRuntimeRule:
        """Explicitly reject a pending suggestion (terminal)."""
        self._require_recovered()
        self._require_user_actor(actor_kind, "reject a suggestion")
        now = self._now(now_ms)
        with self._session() as db:
            stored = self._load_suggestion(db, suggestion_id)
            if stored.rule.state == "rejected":
                return stored
            return db.autonomy.put_runtime_rule(
                dataclasses.replace(stored.rule, state="rejected"),
                expected_revision=stored.revision,
                now_ms=now,
            )

    # ── Mandates ───────────────────────────────────────────────────────

    def create_mandate(
        self, rule: AutonomyRule, *, now_ms: Optional[int] = None
    ) -> StoredRuntimeRule:
        """Store an explicitly-confirmed, bounded temporary mandate."""
        self._require_recovered()
        if not isinstance(rule, AutonomyRule):
            raise ValueError("rule must be an AutonomyRule")
        if rule.source != "temporary_mandate" or rule.state != "active":
            raise AutonomyServiceError(
                "create_mandate requires an active temporary_mandate rule "
                f"(got {rule.source!r}/{rule.state!r})"
            )
        if rule.provenance.actor_kind != "user":
            raise AutonomyServiceError(
                "a temporary mandate exists only through explicit user "
                f"confirmation (got actor_kind={rule.provenance.actor_kind!r})"
            )
        with self._session() as db:
            return db.autonomy.put_runtime_rule(
                rule, expected_revision=0, now_ms=self._now(now_ms)
            )

    def revoke_mandate(
        self,
        rule_id: str,
        *,
        actor_kind: str = "user",
        actor_id: str,
        now_ms: Optional[int] = None,
    ) -> StoredRuntimeRule:
        """Revoke an active mandate; terminal states are idempotent."""
        self._require_recovered()
        self._require_user_actor(actor_kind, "revoke a mandate")
        now = self._now(now_ms)
        with self._session() as db:
            stored = db.autonomy.get_runtime_rule(rule_id)
            if stored is None or stored.rule.source != "temporary_mandate":
                raise UnknownRuleError(f"no temporary mandate {rule_id!r} exists")
            if stored.rule.state != "active":
                return stored
            return db.autonomy.put_runtime_rule(
                dataclasses.replace(stored.rule, state="revoked"),
                expected_revision=stored.revision,
                now_ms=now,
            )

    # ── Listing and explanation ────────────────────────────────────────

    def _stable_rules(self) -> tuple[AutonomyRule, ...]:
        from hades_constants import get_hades_home

        raw = _read_raw_config(get_hades_home() / "config.yaml")
        return parse_stable_rules(raw.get("autonomy"))

    def list_rules(
        self,
        *,
        source: Optional[str] = None,
        states: Optional[tuple[str, ...]] = None,
    ) -> tuple[AutonomyRule, ...]:
        """Every rule across both layers (stable config + runtime state)."""
        rules: list[AutonomyRule] = []
        if source is None or source == "user_assertion":
            rules.extend(self._stable_rules())
        with self._session() as db:
            runtime_source = source if source != "user_assertion" else None
            if source is None or runtime_source is not None:
                rules.extend(
                    stored.rule
                    for stored in db.autonomy.list_runtime_rules(
                        source=runtime_source, states=states
                    )
                )
        if states is not None:
            rules = [r for r in rules if r.state in states]
        return tuple(sorted(rules, key=lambda r: r.rule_id))

    @staticmethod
    def _action_overlap(a: AutonomyRule, b: AutonomyRule) -> bool:
        if not a.action_classes or not b.action_classes:
            return True
        return bool(set(a.action_classes) & set(b.action_classes))

    def explain_rule(
        self, rule_id: str, *, now_ms: Optional[int] = None
    ) -> RuleExplanation:
        """Complete explanation of one rule, whatever layer it lives in."""
        now = self._now(now_ms)
        with self._session() as db:
            stored_contract, _ = self._sync(db, now)
            revision: Optional[int] = None
            layer = "stable_config"
            rule: Optional[AutonomyRule] = next(
                (r for r in self._stable_rules() if r.rule_id == rule_id), None
            )
            if rule is None:
                runtime = db.autonomy.get_runtime_rule(rule_id)
                if runtime is None:
                    raise UnknownRuleError(f"no rule {rule_id!r} exists")
                rule = runtime.rule
                revision = runtime.revision
                layer = "runtime"
            contract_ids = {r.rule_id for r in stored_contract.contract.rules}
            conflicts = tuple(
                sorted(
                    other.rule_id
                    for other in stored_contract.contract.rules
                    if other.rule_id != rule.rule_id
                    and other.effect != rule.effect
                    and self._action_overlap(other, rule)
                )
            )
            edit_route, revoke_route = _routes(rule)
            provenance = rule.provenance
            return RuleExplanation(
                rule=rule,
                layer=layer,
                revision=revision,
                provenance_label=(
                    f"{provenance.actor_kind}:{provenance.actor_id}"
                    + (f" ({provenance.source_ref})" if provenance.source_ref else "")
                ),
                confidence_ppm=provenance.confidence_ppm,
                in_current_contract=rule.rule_id in contract_ids,
                conflicts_with=conflicts,
                edit_route=edit_route,
                revoke_route=revoke_route,
            )

    # ── Budgets ────────────────────────────────────────────────────────

    def settle_cost(
        self,
        decision_id: str,
        *,
        actual_micros: int,
        now_ms: Optional[int] = None,
    ) -> bool:
        """Settle a reserved decision's actual cost exactly once.

        Returns ``True`` on first settlement, ``False`` on an idempotent
        replay. Raises for unknown/unreserved decisions or bad amounts.
        """
        if isinstance(actual_micros, bool) or not isinstance(actual_micros, int):
            raise ValueError("actual_micros must be an integer")
        if actual_micros < 0:
            raise ValueError("actual_micros must be >= 0")
        now = self._now(now_ms)
        with self._session() as db:
            record = db.autonomy.get_decision(decision_id)
            if record is None:
                raise AutonomyServiceError(f"no decision {decision_id!r} exists")
            reservation = record.decision.budget_reservation
            if reservation is None:
                raise AutonomyServiceError(
                    f"decision {decision_id!r} carries no budget reservation "
                    "to settle"
                )
            try:
                db.autonomy.settle_budget(
                    rule_id=reservation.rule_id,
                    operation_key=record.operation_key,
                    decision_id=decision_id,
                    amount_micros=actual_micros,
                    window_started_at_ms=int(reservation.window_key),
                    now_ms=now,
                )
            except AutonomyBudgetError:
                return False  # already settled — idempotent
            return True

    def budget_usage(
        self, *, rule_id: str, now_ms: Optional[int] = None
    ) -> int:
        """Held-or-settled window spend (integer USD micros) for one rule."""
        now = self._now(now_ms)
        with self._session() as db:
            stored, _ = self._sync(db, now)
            rule = next(
                (r for r in stored.contract.rules if r.rule_id == rule_id), None
            )
            if rule is None:
                runtime = db.autonomy.get_runtime_rule(rule_id)
                rule = runtime.rule if runtime is not None else None
            window_start = self._window_start(rule, now) if rule is not None else 0
            return db.autonomy.window_spend_micros(rule_id, window_start)

    # ── Audit access ───────────────────────────────────────────────────

    def list_decisions(self, *, limit: int = 100) -> tuple[DecisionRecord, ...]:
        with self._session() as db:
            return db.autonomy.list_decisions(limit=limit)

    def export_redacted(
        self,
        *,
        include_decisions: bool = False,
        now_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Portable redacted authority export.

        Includes stable rule documents and runtime lifecycle metadata.
        Recipient/resource hashes become opaque local labels; the profile
        hash key, raw audit context, and (by default) decisions are
        excluded.
        """
        now = self._now(now_ms)
        labels: dict[str, str] = {}

        def redact(hashes: tuple[str, ...]) -> list[str]:
            out = []
            for value in hashes:
                if value not in labels:
                    labels[value] = f"local:recipient:{len(labels) + 1}"
                out.append(labels[value])
            return out

        def rule_doc(rule: AutonomyRule) -> dict[str, Any]:
            return {
                "rule_id": rule.rule_id,
                "source": rule.source,
                "state": rule.state,
                "effect": rule.effect,
                "action_classes": list(rule.action_classes),
                "data_classes": list(rule.data_classes),
                "recipient_classes": list(rule.recipient_classes),
                "recipient_labels": redact(rule.recipient_hashes),
                "resource_prefixes": list(rule.scope.resource_prefixes),
                "allowed_reversibility": list(rule.allowed_reversibility),
                "confidence_ppm": rule.provenance.confidence_ppm,
                "created_at_ms": rule.created_at_ms,
                "expires_at_ms": rule.expires_at_ms,
                "max_uses": rule.max_uses,
                "remaining_uses": rule.remaining_uses,
                "description": rule.description,
            }

        with self._session() as db:
            stored, _ = self._sync(db, now)
            export: dict[str, Any] = {
                "schema": stored.schema_id,
                "profile_id": stored.contract.profile_id,
                "exported_at_ms": now,
                "contract": {
                    "version": stored.version,
                    "content_hash": stored.content_hash,
                    "compiled_at_ms": stored.contract.compiled_at_ms,
                },
                "stable_rules": [rule_doc(r) for r in self._stable_rules()],
                "runtime_rules": [
                    rule_doc(item.rule)
                    for item in db.autonomy.list_runtime_rules()
                ],
            }
            if include_decisions:
                export["decisions"] = [
                    {
                        "decision_id": record.decision.decision_id,
                        "stage": record.stage,
                        "verdict": record.decision.verdict,
                        "code": record.decision.code,
                        "contract_version": record.decision.authority_version,
                        "created_at_ms": record.created_at_ms,
                    }
                    for record in db.autonomy.list_decisions(limit=1000)
                ]
            return export

    def purge_runtime_history(
        self, *, before_ms: int, now_ms: Optional[int] = None
    ) -> dict[str, int]:
        """Delete settled runtime history older than *before_ms*.

        Never deletes rules, stable config, contract versions, open
        (unsettled/unreleased) budget reservations, or the decisions that
        hold them.
        """
        if isinstance(before_ms, bool) or not isinstance(before_ms, int) or before_ms < 0:
            raise ValueError("before_ms must be a non-negative integer")
        self._require_recovered()

        def _do(conn: sqlite3.Connection) -> dict[str, int]:
            counts: dict[str, int] = {}
            # Cost entries: only operations that finished (settle/release)
            # and are entirely older than the boundary. An open reserve
            # keeps holding its window budget.
            cursor = conn.execute(
                "DELETE FROM autonomy_cost_ledger WHERE operation_key IN ("
                " SELECT operation_key FROM autonomy_cost_ledger"
                " GROUP BY operation_key"
                " HAVING SUM(CASE WHEN kind IN ('settle','release')"
                "            THEN 1 ELSE 0 END) > 0"
                "    AND MAX(created_at_ms) < ?)",
                (before_ms,),
            )
            counts["cost_entries"] = cursor.rowcount
            cursor = conn.execute(
                "DELETE FROM autonomy_consumptions WHERE decision_id IN ("
                " SELECT decision_id FROM autonomy_decisions"
                " WHERE created_at_ms < ?"
                "   AND operation_key NOT IN"
                "       (SELECT operation_key FROM autonomy_cost_ledger))",
                (before_ms,),
            )
            counts["consumptions"] = cursor.rowcount
            cursor = conn.execute(
                "DELETE FROM autonomy_decisions WHERE created_at_ms < ?"
                " AND operation_key NOT IN"
                "     (SELECT operation_key FROM autonomy_cost_ledger)",
                (before_ms,),
            )
            counts["decisions"] = cursor.rowcount
            # Lifecycle events: keep the full history of every rule that
            # is still pending or active; terminal/absent rules shed
            # events past the retention boundary.
            cursor = conn.execute(
                "DELETE FROM autonomy_rule_events WHERE created_at_ms < ?"
                " AND rule_id NOT IN"
                "     (SELECT rule_id FROM autonomy_runtime_rules"
                "      WHERE state IN (?, ?))",
                (before_ms, *_PENDING_STATES),
            )
            counts["rule_events"] = cursor.rowcount
            return counts

        with self._session() as db:
            return db._execute_write(_do)


# ── Stored provider ─────────────────────────────────────────────────────────


class StoredAuthorityProvider:
    """Canonical :class:`AuthorityProvider` over the active profile home.

    Every call resolves the current ``get_hades_home()`` sources fresh —
    a commit/compensate-time ``authorize`` therefore always sees the
    latest published contract, and a pending crashed apply fails closed.
    """

    def __init__(self, db: Optional["SessionDB"] = None) -> None:
        self._service = AutonomyService(db=db)

    @property
    def service(self) -> AutonomyService:
        return self._service

    def current_contract(self) -> AutonomyContract:
        return self._service.current_contract().contract

    def authorize(
        self, context: ActionContext, *, consume: bool
    ) -> AuthorityDecision:
        return self._service.evaluate(context, consume=consume)
