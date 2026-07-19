"""Task 4 decision tests for ``agent.autonomy.evaluator``.

The evaluator is a pure function over an immutable contract snapshot and a
declared action context. Invariants proven here:

- deterministic ``allow``/``ask``/``deny`` with deny > ask > allow;
- learned suggestions never participate, regardless of confidence;
- unknown high-risk facts (recipient, data class, reversibility, cost)
  never become wildcard matches — they resolve conservatively;
- expired/consumed/scope-mismatched authority never allows;
- integer fixed-point cost/time/uncertainty comparisons;
- identical inputs give identical decisions across shuffled rule orders.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace

import pytest

from agent.autonomy import (
    ActionContext,
    AutonomyContract,
    AutonomyRule,
    ClarificationRequest,
    CostConstraint,
    EvidenceRequirement,
    RuleProvenance,
    RuleScope,
    TimeConstraint,
)
from agent.autonomy.canonical import content_hash, rule_to_dict
from agent.autonomy.evaluator import (
    ConflictExplanation,
    evaluate_contract,
    explain_conflict,
    matching_rules,
    required_pre_action_evidence,
)

NOW_MS = 50_000
STAGE = "execute"


# ── Fixture builders ────────────────────────────────────────────────────────


def provenance(**overrides) -> RuleProvenance:
    base = dict(
        actor_kind="user",
        actor_id="user-1",
        source_ref="cli",
        observed_at_ms=100,
        confirmed_at_ms=200,
        confidence_ppm=1_000_000,
    )
    base.update(overrides)
    return RuleProvenance(**base)


def rule(rule_id: str, effect: str, **overrides) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="user_assertion",
        state="active",
        effect=effect,
        action_classes=("message.send",),
        provenance=provenance(),
        created_at_ms=1_000,
    )
    base.update(overrides)
    return AutonomyRule(**base)


def allow_rule(rule_id: str = "r-allow", **overrides) -> AutonomyRule:
    return rule(rule_id, "allow", **overrides)


def ask_rule(rule_id: str = "r-ask", **overrides) -> AutonomyRule:
    return rule(rule_id, "ask", **overrides)


def deny_rule(rule_id: str = "r-deny", **overrides) -> AutonomyRule:
    return rule(rule_id, "deny", **overrides)


def mandate(rule_id: str = "m-1", **overrides) -> AutonomyRule:
    base = dict(
        source="temporary_mandate",
        max_uses=1,
        remaining_uses=1,
    )
    base.update(overrides)
    return rule(rule_id, "allow", **base)


def contract_of(*rules: AutonomyRule, shuffle_order: tuple | None = None) -> AutonomyContract:
    ordered = shuffle_order if shuffle_order is not None else tuple(
        sorted(rules, key=lambda r: r.rule_id)
    )
    return AutonomyContract(
        version=1,
        contract_hash=content_hash(
            [rule_to_dict(r) for r in sorted(ordered, key=lambda r: r.rule_id)]
        ),
        profile_id="work",
        compiled_at_ms=1_000,
        rules=tuple(ordered),
    )


def ctx(**overrides) -> ActionContext:
    base = dict(
        operation_key="op-1",
        stage=STAGE,
        action_class="message.send",
        data_classes=("internal",),
        reversibility="reversible",
        recipient_class="designated_test",
        profile_id="work",
    )
    base.update(overrides)
    return ActionContext(**base)


class Case(SimpleNamespace):
    def __init__(self, contract, context, now=NOW_MS, usage=None):
        super().__init__(contract=contract, context=context, now=now, usage=usage or {})


# ── Frozen decision matrix cases ────────────────────────────────────────────


def explicit_allow_context() -> Case:
    return Case(
        contract_of(allow_rule(recipient_classes=("designated_test",))),
        ctx(),
    )


def deny_and_allow_conflict() -> Case:
    rules = (
        allow_rule("r-allow-del", action_classes=("workspace.delete",)),
        deny_rule("r-deny-del", action_classes=("workspace.delete",)),
    )
    return Case(
        contract_of(*rules),
        ctx(action_class="workspace.delete", recipient_class=None),
    )


def ask_and_allow_conflict() -> Case:
    return Case(
        contract_of(
            allow_rule(recipient_classes=("designated_test",)),
            ask_rule("r-ask-send"),
        ),
        ctx(),
    )


def high_confidence_unconfirmed_suggestion() -> Case:
    # The 990k-confidence suggestion lives in state.db awaiting
    # confirmation; it can never enter the compiled contract, so the
    # candidate's effective contract is empty for this action.
    return Case(contract_of(), ctx())


def expired_mandate() -> Case:
    return Case(
        contract_of(
            mandate(
                "m-expired",
                max_uses=None,
                remaining_uses=None,
                expires_at_ms=NOW_MS - 1,
            )
        ),
        ctx(),
    )


def unknown_external_recipient() -> Case:
    return Case(
        contract_of(allow_rule()),
        ctx(recipient_class=None),
    )


def credential_to_external_recipient() -> Case:
    rules = (
        allow_rule("r-allow-exact", recipient_classes=("external",)),
        deny_rule("r-deny-credential", data_classes=("credential",)),
    )
    return Case(
        contract_of(*rules),
        ctx(data_classes=("credential",), recipient_class="external"),
    )


def irreversible_without_exact_evidence() -> Case:
    return Case(
        contract_of(
            allow_rule(
                "r-allow-irrev",
                action_classes=("workspace.delete",),
                allowed_reversibility=("reversible", "irreversible"),
            )
        ),
        ctx(
            action_class="workspace.delete",
            reversibility="irreversible",
            recipient_class=None,
        ),
    )


def unknown_cost_under_bounded_rule() -> Case:
    return Case(
        contract_of(
            allow_rule(
                "r-buy",
                action_classes=("purchase.prepare",),
                recipient_classes=("designated_test",),
                cost=CostConstraint(max_per_action_cents=500),
            )
        ),
        ctx(action_class="purchase.prepare", estimated_cost_cents=None),
    )


def window_budget_exceeded() -> Case:
    return Case(
        contract_of(
            allow_rule(
                "r-buy",
                action_classes=("purchase.prepare",),
                recipient_classes=("designated_test",),
                cost=CostConstraint(
                    max_per_window_cents=1_000, window_ms=86_400_000
                ),
            )
        ),
        ctx(action_class="purchase.prepare", estimated_cost_cents=400),
        usage={"r-buy": 8_000_000},  # $8 already held/settled, in micros
    )


def outside_time_window() -> Case:
    return Case(
        contract_of(
            allow_rule(
                "r-buy",
                action_classes=("purchase.prepare",),
                recipient_classes=("designated_test",),
                time=TimeConstraint(window_start_minute=540, window_end_minute=1020),
            )
        ),
        ctx(
            action_class="purchase.prepare",
            estimated_cost_cents=200,
            local_time_minute=1_300,
        ),
    )


def uncertainty_above_rule_max() -> Case:
    return Case(
        contract_of(
            allow_rule(
                recipient_classes=("designated_test",),
                max_uncertainty_ppm=100_000,
            )
        ),
        ctx(uncertainty_ppm=250_000),
    )


@pytest.mark.parametrize(
    ("case_fn", "verdict", "code"),
    [
        (explicit_allow_context, "allow", "explicit_allow"),
        (deny_and_allow_conflict, "deny", "conflicting_deny"),
        (ask_and_allow_conflict, "ask", "conflicting_ask"),
        (high_confidence_unconfirmed_suggestion, "ask", "no_authorizing_rule"),
        (expired_mandate, "ask", "authority_expired"),
        (unknown_external_recipient, "deny", "unknown_recipient"),
        (credential_to_external_recipient, "deny", "sensitive_data_boundary"),
        (irreversible_without_exact_evidence, "ask", "exact_approval_required"),
        (unknown_cost_under_bounded_rule, "ask", "cost_unknown"),
        (window_budget_exceeded, "deny", "cost_budget_exceeded"),
        (outside_time_window, "ask", "outside_time_window"),
        (uncertainty_above_rule_max, "ask", "uncertainty_too_high"),
    ],
    ids=lambda value: value.__name__ if callable(value) else value,
)
def test_decision_matrix(case_fn, verdict, code):
    case = case_fn()
    decision = evaluate_contract(
        case.contract, case.context, now_ms=case.now, budget_usage=case.usage
    )
    assert (decision.verdict, decision.code) == (verdict, code)
    assert decision.reason
    assert decision.edit_targets


# ── Suggestions can never participate ───────────────────────────────────────


def test_smuggled_suggestion_is_excluded_defensively():
    suggestion = AutonomyRule(
        rule_id="s-1",
        source="learned_suggestion",
        state="awaiting_confirmation",
        effect="allow",
        action_classes=("message.send",),
        provenance=provenance(confirmed_at_ms=None, confidence_ppm=1_000_000),
    )
    fake_contract = SimpleNamespace(rules=(suggestion,))
    decision = evaluate_contract(fake_contract, ctx(), now_ms=NOW_MS)
    assert decision.verdict == "ask"
    assert decision.code == "no_authorizing_rule"
    assert "s-1" not in decision.matched_rule_ids


# ── Recipient selector kinds ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "recipient_class",
    ["self", "same_conversation", "profile_local", "external", "designated_test"],
)
def test_recipient_class_selectors_match_exactly(recipient_class):
    contract = contract_of(allow_rule(recipient_classes=(recipient_class,)))
    decision = evaluate_contract(
        contract, ctx(recipient_class=recipient_class), now_ms=NOW_MS
    )
    assert (decision.verdict, decision.code) == ("allow", "explicit_allow")


def test_exact_recipient_hash_resolves_and_matches():
    contract = contract_of(allow_rule(recipient_hashes=("h-alice",)))
    decision = evaluate_contract(
        contract,
        ctx(recipient_class=None, recipient_hash="h-alice"),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("allow", "explicit_allow")


def test_exact_domain_hash_selector_matches():
    contract = contract_of(allow_rule(recipient_hashes=("h-domain-example.test",)))
    decision = evaluate_contract(
        contract,
        ctx(recipient_class=None, recipient_hash="h-domain-example.test"),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("allow", "explicit_allow")


def test_confusable_recipient_hash_is_a_mismatch_not_unknown():
    contract = contract_of(allow_rule(recipient_hashes=("h-alice",)))
    decision = evaluate_contract(
        contract,
        ctx(recipient_class=None, recipient_hash="h-imposter"),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("deny", "recipient_mismatch")


def test_explicit_unknown_recipient_selector_is_required_to_allow():
    explicit = contract_of(allow_rule(recipient_classes=("unknown",)))
    decision = evaluate_contract(
        explicit, ctx(recipient_class=None), now_ms=NOW_MS
    )
    assert (decision.verdict, decision.code) == ("allow", "explicit_allow")

    wildcard = contract_of(allow_rule())  # no recipient selector at all
    decision = evaluate_contract(
        wildcard, ctx(recipient_class="unknown"), now_ms=NOW_MS
    )
    assert (decision.verdict, decision.code) == ("deny", "unknown_recipient")


# ── Data classes and sensitive boundaries ───────────────────────────────────


def test_unknown_data_class_requires_explicit_unknown_selector():
    wildcard = contract_of(allow_rule(data_classes=("public", "internal")))
    decision = evaluate_contract(
        wildcard,
        ctx(action_class="data.share", data_classes=("unknown",)),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("deny", "unknown_data_class")

    explicit = contract_of(
        allow_rule(
            action_classes=("data.share",),
            data_classes=("unknown",),
            recipient_classes=("designated_test",),
        )
    )
    decision = evaluate_contract(
        explicit,
        ctx(action_class="data.share", data_classes=("unknown",)),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("allow", "explicit_allow")


def test_sensitive_data_to_unresolved_recipient_is_a_boundary_deny():
    contract = contract_of(
        allow_rule(
            action_classes=("purchase.commit",),
            data_classes=("financial",),
            recipient_classes=("designated_test",),
        )
    )
    decision = evaluate_contract(
        contract,
        ctx(
            action_class="purchase.commit",
            data_classes=("financial",),
            recipient_class=None,
        ),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("deny", "sensitive_data_boundary")


def test_sensitive_allow_requires_explicit_data_class_and_recipient():
    # A generic allow (no data/recipient selectors) never authorizes
    # health data; with no other rule, the conservative default applies.
    contract = contract_of(allow_rule())
    decision = evaluate_contract(
        contract, ctx(data_classes=("health",)), now_ms=NOW_MS
    )
    assert (decision.verdict, decision.code) == ("ask", "no_authorizing_rule")

    explicit = contract_of(
        allow_rule(
            data_classes=("health",),
            recipient_classes=("designated_test",),
        )
    )
    decision = evaluate_contract(
        explicit, ctx(data_classes=("health",)), now_ms=NOW_MS
    )
    assert (decision.verdict, decision.code) == ("allow", "explicit_allow")


# ── Resource prefix boundary safety ─────────────────────────────────────────


def test_resource_prefix_uses_segment_boundaries_not_raw_prefix():
    contract = contract_of(
        allow_rule(
            "r-share",
            action_classes=("data.share",),
            recipient_classes=("designated_test",),
            scope=RuleScope(resource_prefixes=("workspace:/allowed",)),
        )
    )
    inside = evaluate_contract(
        contract,
        ctx(
            action_class="data.share",
            resource_refs=("workspace:/allowed/report.txt",),
        ),
        now_ms=NOW_MS,
    )
    assert (inside.verdict, inside.code) == ("allow", "explicit_allow")

    sibling = evaluate_contract(
        contract,
        ctx(
            action_class="data.share",
            resource_refs=("workspace:/allowed-other/report.txt",),
        ),
        now_ms=NOW_MS,
    )
    assert (sibling.verdict, sibling.code) == ("deny", "data_scope_mismatch")


def test_delete_outside_declared_resource_scope_is_denied():
    contract = contract_of(
        allow_rule(
            "r-del",
            action_classes=("workspace.delete",),
            scope=RuleScope(resource_prefixes=("workspace:/tmp",)),
        )
    )
    decision = evaluate_contract(
        contract,
        ctx(
            action_class="workspace.delete",
            recipient_class=None,
            resource_refs=("workspace:/outside/canary.txt",),
        ),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("deny", "resource_scope_mismatch")


# ── Time windows (timezone / DST) ───────────────────────────────────────────


def _utc_ms(year, month, day, hour, minute) -> int:
    stamp = datetime(year, month, day, hour, minute, tzinfo=dt_timezone.utc)
    return int(stamp.timestamp()) * 1_000


def test_iana_timezone_window_handles_dst():
    contract = contract_of(
        allow_rule(
            "r-buy",
            action_classes=("purchase.prepare",),
            recipient_classes=("designated_test",),
            time=TimeConstraint(
                window_start_minute=540,  # 09:00 local
                window_end_minute=1020,  # 17:00 local
                timezone="America/New_York",
            ),
        )
    )
    # 13:30 UTC is 08:30 EST in January (outside) …
    winter = evaluate_contract(
        contract,
        ctx(
            action_class="purchase.prepare",
            estimated_cost_cents=100,
            occurred_at_ms=_utc_ms(2026, 1, 15, 13, 30),
        ),
        now_ms=NOW_MS,
    )
    assert (winter.verdict, winter.code) == ("ask", "outside_time_window")
    # … but 09:30 EDT in July (inside).
    summer = evaluate_contract(
        contract,
        ctx(
            action_class="purchase.prepare",
            estimated_cost_cents=100,
            occurred_at_ms=_utc_ms(2026, 7, 15, 13, 30),
        ),
        now_ms=NOW_MS,
    )
    assert (summer.verdict, summer.code) == ("allow", "explicit_allow")


def test_wraparound_window_crosses_midnight():
    contract = contract_of(
        allow_rule(
            recipient_classes=("designated_test",),
            time=TimeConstraint(window_start_minute=1_380, window_end_minute=120),
        )
    )
    inside = evaluate_contract(
        contract, ctx(local_time_minute=30), now_ms=NOW_MS
    )
    assert inside.verdict == "allow"
    outside = evaluate_contract(
        contract, ctx(local_time_minute=720), now_ms=NOW_MS
    )
    assert (outside.verdict, outside.code) == ("ask", "outside_time_window")


def test_unknown_local_time_under_window_rule_asks():
    contract = contract_of(
        allow_rule(
            recipient_classes=("designated_test",),
            time=TimeConstraint(window_start_minute=540, window_end_minute=1020),
        )
    )
    decision = evaluate_contract(
        contract, ctx(local_time_minute=None), now_ms=NOW_MS
    )
    assert (decision.verdict, decision.code) == ("ask", "outside_time_window")


# ── Cost fixed-point comparisons ────────────────────────────────────────────


def _bounded_buy_contract(**cost_overrides) -> AutonomyContract:
    cost = dict(max_per_action_cents=500)
    cost.update(cost_overrides)
    return contract_of(
        allow_rule(
            "r-buy",
            action_classes=("purchase.prepare",),
            recipient_classes=("designated_test",),
            cost=CostConstraint(**cost),
        )
    )


def test_zero_cost_is_within_every_cap():
    decision = evaluate_contract(
        _bounded_buy_contract(),
        ctx(action_class="purchase.prepare", estimated_cost_cents=0),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("allow", "explicit_allow")


def test_cost_cap_comparison_is_exact_integer_fixed_point():
    at_cap = evaluate_contract(
        _bounded_buy_contract(),
        ctx(action_class="purchase.prepare", estimated_cost_cents=500),
        now_ms=NOW_MS,
    )
    assert at_cap.verdict == "allow"
    over_cap = evaluate_contract(
        _bounded_buy_contract(),
        ctx(action_class="purchase.prepare", estimated_cost_cents=501),
        now_ms=NOW_MS,
    )
    assert (over_cap.verdict, over_cap.code) == ("deny", "cost_per_action_exceeded")


def test_window_budget_counts_prior_usage_snapshot():
    contract = contract_of(
        allow_rule(
            "r-buy",
            action_classes=("purchase.prepare",),
            recipient_classes=("designated_test",),
            cost=CostConstraint(max_per_window_cents=1_000, window_ms=86_400_000),
        )
    )
    context = ctx(action_class="purchase.prepare", estimated_cost_cents=400)
    fresh = evaluate_contract(contract, context, now_ms=NOW_MS, budget_usage={})
    assert fresh.verdict == "allow"
    assert fresh.budget_rule_id == "r-buy"
    exhausted = evaluate_contract(
        contract, context, now_ms=NOW_MS, budget_usage={"r-buy": 8_000_000}
    )
    assert (exhausted.verdict, exhausted.code) == ("deny", "cost_budget_exceeded")


def test_budget_usage_rejects_invalid_snapshot_values():
    with pytest.raises(ValueError, match="budget_usage"):
        evaluate_contract(
            _bounded_buy_contract(),
            ctx(action_class="purchase.prepare", estimated_cost_cents=100),
            now_ms=NOW_MS,
            budget_usage={"r-buy": -1},
        )


# ── Evidence requirements ───────────────────────────────────────────────────


def test_missing_pre_action_evidence_asks_and_names_the_requirement():
    contract = contract_of(
        allow_rule(
            recipient_classes=("designated_test",),
            evidence_requirements=(
                EvidenceRequirement(kind="recipient_verified", stage="pre_action"),
            ),
        )
    )
    missing = evaluate_contract(contract, ctx(), now_ms=NOW_MS)
    assert (missing.verdict, missing.code) == ("ask", "required_evidence_missing")
    assert any(
        req.kind == "recipient_verified" for req in missing.required_evidence
    )
    present = evaluate_contract(
        contract, ctx(present_evidence=("recipient_verified",)), now_ms=NOW_MS
    )
    assert present.verdict == "allow"


def test_missing_exact_approval_evidence_uses_exact_approval_code():
    contract = contract_of(
        allow_rule(
            recipient_classes=("designated_test",),
            evidence_requirements=(
                EvidenceRequirement(kind="exact_approval", stage="pre_action"),
            ),
        )
    )
    decision = evaluate_contract(contract, ctx(), now_ms=NOW_MS)
    assert (decision.verdict, decision.code) == ("ask", "exact_approval_required")


def test_post_action_evidence_unions_sorted_and_deduplicated():
    contract = contract_of(
        allow_rule(
            "r-a",
            recipient_classes=("designated_test",),
            evidence_requirements=(
                EvidenceRequirement(kind="receipt", stage="post_action"),
            ),
        ),
        allow_rule(
            "r-b",
            recipient_classes=("designated_test",),
            evidence_requirements=(
                EvidenceRequirement(kind="receipt", stage="post_action"),
                EvidenceRequirement(kind="audit_log", stage="post_action"),
            ),
        ),
    )
    decision = evaluate_contract(contract, ctx(), now_ms=NOW_MS)
    assert decision.verdict == "allow"
    assert decision.required_evidence == (
        EvidenceRequirement(kind="audit_log", stage="post_action"),
        EvidenceRequirement(kind="receipt", stage="post_action"),
    )


def test_required_pre_action_evidence_helper_unions_matches():
    contract = contract_of(
        allow_rule(
            recipient_classes=("designated_test",),
            evidence_requirements=(
                EvidenceRequirement(kind="recipient_verified", stage="pre_action"),
                EvidenceRequirement(kind="receipt", stage="post_action"),
            ),
        )
    )
    pre = required_pre_action_evidence(contract, ctx(), now_ms=NOW_MS)
    assert pre == (
        EvidenceRequirement(kind="recipient_verified", stage="pre_action"),
    )


# ── Reversibility ───────────────────────────────────────────────────────────


def test_unknown_reversibility_needs_explicit_unknown_declaration():
    implicit = contract_of(
        allow_rule("r-del", action_classes=("workspace.delete",))
    )
    decision = evaluate_contract(
        implicit,
        ctx(
            action_class="workspace.delete",
            reversibility="unknown",
            recipient_class=None,
        ),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("deny", "reversibility_unknown")


def test_irreversible_with_declared_rule_and_exact_approval_allows():
    contract = contract_of(
        allow_rule(
            "r-del",
            action_classes=("workspace.delete",),
            allowed_reversibility=("irreversible",),
        )
    )
    decision = evaluate_contract(
        contract,
        ctx(
            action_class="workspace.delete",
            reversibility="irreversible",
            recipient_class=None,
            present_evidence=("exact_approval",),
        ),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("allow", "explicit_allow")


# ── Mandates: consumption, expiry, scope ────────────────────────────────────


def test_allow_via_mandate_plans_consumption():
    contract = contract_of(
        mandate("m-del", action_classes=("workspace.delete",))
    )
    decision = evaluate_contract(
        contract,
        ctx(action_class="workspace.delete", recipient_class=None),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("allow", "temporary_mandate")
    assert decision.consume_mandate_ids == ("m-del",)


def test_exhausted_mandate_is_denied_as_consumed():
    contract = contract_of(
        mandate(
            "m-del", action_classes=("workspace.delete",), remaining_uses=0
        )
    )
    decision = evaluate_contract(
        contract,
        ctx(action_class="workspace.delete", recipient_class=None),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("deny", "mandate_consumed")


def test_expired_authority_at_commit_stage_denies():
    contract = contract_of(
        mandate(
            "m-expired",
            max_uses=None,
            remaining_uses=None,
            expires_at_ms=NOW_MS - 1,
        )
    )
    decision = evaluate_contract(
        contract, ctx(stage="commit"), now_ms=NOW_MS
    )
    assert (decision.verdict, decision.code) == ("deny", "authority_expired")


def test_mandate_scoped_to_another_task_is_a_scope_mismatch():
    contract = contract_of(
        mandate("m-t", scope=RuleScope(task_id="task-1"))
    )
    decision = evaluate_contract(
        contract, ctx(task_id="task-2"), now_ms=NOW_MS
    )
    assert (decision.verdict, decision.code) == ("ask", "scope_mismatch")


def test_exact_session_and_transaction_scope_matches():
    contract = contract_of(
        mandate(
            "m-s",
            scope=RuleScope(session_id="sess-1", transaction_id="txn-1"),
        )
    )
    decision = evaluate_contract(
        contract,
        ctx(session_id="sess-1", transaction_id="txn-1"),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("allow", "temporary_mandate")


def test_wrong_profile_rule_is_silently_discarded():
    contract = contract_of(
        mandate("m-p", scope=RuleScope(profile_id="other"))
    )
    decision = evaluate_contract(contract, ctx(), now_ms=NOW_MS)
    assert (decision.verdict, decision.code) == ("ask", "no_authorizing_rule")
    assert "m-p" not in decision.matched_rule_ids


def test_stable_deny_wins_over_active_temporary_mandate():
    contract = contract_of(
        deny_rule("r-deny-share", action_classes=("data.share",)),
        mandate(
            "m-share",
            action_classes=("data.share",),
            data_classes=("confidential",),
            recipient_classes=("designated_test",),
        ),
    )
    decision = evaluate_contract(
        contract,
        ctx(action_class="data.share", data_classes=("confidential",)),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("deny", "conflicting_deny")
    assert set(decision.conflicting_rule_ids) == {"r-deny-share", "m-share"}


# ── Explicit ask, defaults, unknown actions ─────────────────────────────────


def test_lone_matching_ask_rule_is_explicit_ask():
    contract = contract_of(ask_rule())
    decision = evaluate_contract(contract, ctx(), now_ms=NOW_MS)
    assert (decision.verdict, decision.code) == ("ask", "explicit_ask")


def test_default_is_deny_for_unknown_or_irreversible_context():
    for overrides in (
        dict(reversibility="unknown"),
        dict(reversibility="irreversible"),
        dict(data_classes=("credential",)),
    ):
        decision = evaluate_contract(
            contract_of(),
            ctx(action_class="workspace.write", recipient_class=None, **overrides),
            now_ms=NOW_MS,
        )
        assert (decision.verdict, decision.code) == ("deny", "no_authorizing_rule")


def test_conservative_default_is_configurable_but_never_allow():
    decision = evaluate_contract(
        contract_of(), ctx(), now_ms=NOW_MS, default_known_reversible="deny"
    )
    assert decision.verdict == "deny"
    with pytest.raises(ValueError, match="allow"):
        evaluate_contract(
            contract_of(), ctx(), now_ms=NOW_MS, default_known_reversible="allow"
        )


def test_unrecognized_extension_action_class_fails_closed():
    decision = evaluate_contract(
        contract_of(allow_rule()),
        ctx(action_class="custom.effect", recipient_class=None),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("deny", "unknown_action_class")

    described = contract_of(
        allow_rule("r-custom", action_classes=("custom.effect",))
    )
    decision = evaluate_contract(
        described,
        ctx(action_class="custom.effect", recipient_class=None),
        now_ms=NOW_MS,
    )
    assert (decision.verdict, decision.code) == ("allow", "explicit_allow")


# ── Conflict explanations ───────────────────────────────────────────────────


def test_conflicting_deny_explains_both_rules_and_edit_routes():
    case = deny_and_allow_conflict()
    decision = evaluate_contract(case.contract, case.context, now_ms=case.now)
    assert set(decision.conflicting_rule_ids) == {"r-allow-del", "r-deny-del"}
    assert "r-allow-del" in decision.reason and "r-deny-del" in decision.reason
    assert "hades autonomy rule explain r-deny-del" in decision.edit_targets
    assert "hades autonomy rule edit r-deny-del" in decision.edit_targets


def test_conflict_language_never_mentions_confidence_or_override():
    for case_fn in (deny_and_allow_conflict, ask_and_allow_conflict):
        case = case_fn()
        decision = evaluate_contract(case.contract, case.context, now_ms=case.now)
        lowered = decision.reason.lower()
        assert "confidence" not in lowered
        assert "overr" not in lowered  # no "override"/"overrode" claims


def test_explain_conflict_names_sources_precedence_and_commands():
    allow = allow_rule("r-a", action_classes=("workspace.delete",))
    deny = deny_rule("r-d", action_classes=("workspace.delete",))
    explanation = explain_conflict((deny, allow), winning_effect="deny")
    assert isinstance(explanation, ConflictExplanation)
    assert explanation.rule_ids == ("r-a", "r-d")
    assert explanation.winning_effect == "deny"
    assert "deny > ask > allow" in explanation.precedence
    assert any("user_assertion" in label for label in explanation.sources)
    assert "hades autonomy rule explain r-a" in explanation.edit_commands
    assert "hades autonomy rule edit r-d" in explanation.edit_commands


# ── Clarification requests ──────────────────────────────────────────────────


def test_clarification_only_for_high_value_bounded_questions():
    unknown_rec = unknown_external_recipient()
    decision = evaluate_contract(
        unknown_rec.contract, unknown_rec.context, now_ms=NOW_MS
    )
    assert isinstance(decision.clarification, ClarificationRequest)
    assert decision.clarification.question
    assert 0 < len(decision.clarification.choices) <= 4

    cost = unknown_cost_under_bounded_rule()
    decision = evaluate_contract(cost.contract, cost.context, now_ms=NOW_MS)
    assert decision.clarification is not None

    conflict = ask_and_allow_conflict()
    decision = evaluate_contract(conflict.contract, conflict.context, now_ms=NOW_MS)
    assert decision.clarification is not None


def test_low_stakes_defaults_do_not_interrupt():
    allow = explicit_allow_context()
    decision = evaluate_contract(allow.contract, allow.context, now_ms=NOW_MS)
    assert decision.clarification is None

    default = high_confidence_unconfirmed_suggestion()
    decision = evaluate_contract(default.contract, default.context, now_ms=NOW_MS)
    assert decision.clarification is None


# ── matching_rules and canonical ordering ───────────────────────────────────


def test_matching_rules_returns_sorted_active_matches_only():
    contract = contract_of(
        allow_rule("r-b", recipient_classes=("designated_test",)),
        allow_rule("r-a", recipient_classes=("designated_test",)),
        allow_rule("r-x", action_classes=("workspace.delete",)),
        mandate(
            "m-old",
            max_uses=None,
            remaining_uses=None,
            expires_at_ms=NOW_MS - 1,
        ),
    )
    matched = matching_rules(contract, ctx(), now_ms=NOW_MS)
    assert [r.rule_id for r in matched] == ["r-a", "r-b"]


def test_matched_and_conflicting_ids_are_canonically_ordered():
    contract = contract_of(
        deny_rule("r-z", action_classes=("workspace.delete",)),
        deny_rule("r-a", action_classes=("workspace.delete",)),
        allow_rule("r-m", action_classes=("workspace.delete",)),
    )
    decision = evaluate_contract(
        contract,
        ctx(action_class="workspace.delete", recipient_class=None),
        now_ms=NOW_MS,
    )
    assert decision.matched_rule_ids == tuple(sorted(decision.matched_rule_ids))
    assert decision.conflicting_rule_ids == tuple(
        sorted(decision.conflicting_rule_ids)
    )


def test_decisions_are_deterministic_across_shuffled_rule_orders():
    rules = [
        allow_rule("r-allow", recipient_classes=("designated_test",)),
        ask_rule("r-ask"),
        deny_rule("r-deny-cred", data_classes=("credential",)),
        allow_rule(
            "r-buy",
            action_classes=("purchase.prepare",),
            recipient_classes=("designated_test",),
            cost=CostConstraint(max_per_action_cents=500),
        ),
        mandate("m-del", action_classes=("workspace.delete",)),
        deny_rule("r-deny-del", action_classes=("workspace.delete",)),
    ]
    context = ctx()
    baseline = evaluate_contract(
        contract_of(*rules), context, now_ms=NOW_MS, budget_usage={}
    )
    rng = random.Random(93)
    for _ in range(1_000):
        shuffled = list(rules)
        rng.shuffle(shuffled)
        contract = contract_of(*rules, shuffle_order=tuple(shuffled))
        decision = evaluate_contract(
            contract, context, now_ms=NOW_MS, budget_usage={}
        )
        assert decision == baseline


# ── New model fields backing the evaluator ──────────────────────────────────


def test_uncertainty_fields_are_bounded_integer_ppm():
    with pytest.raises(ValueError, match="max_uncertainty_ppm"):
        allow_rule(max_uncertainty_ppm=1_000_001)
    with pytest.raises(ValueError, match="uncertainty_ppm"):
        ctx(uncertainty_ppm=-1)
    with pytest.raises(ValueError, match="uncertainty_ppm"):
        ctx(uncertainty_ppm=0.5)


def test_unbounded_uncertainty_under_bounding_rule_asks():
    contract = contract_of(
        allow_rule(
            recipient_classes=("designated_test",), max_uncertainty_ppm=100_000
        )
    )
    decision = evaluate_contract(
        contract, ctx(uncertainty_ppm=None), now_ms=NOW_MS
    )
    assert (decision.verdict, decision.code) == ("ask", "uncertainty_too_high")
