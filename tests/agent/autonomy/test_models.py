"""Task 1/2 model- and canonicalization-contract tests for ``agent.autonomy``.

These tests freeze the public authority contract: frozen dataclasses,
finite vocabularies, fail-closed validation, and the deterministic
canonical JSON / hashing layer. No persistence is exercised here.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from agent.autonomy import (
    ACTION_CLASSES,
    AUTONOMY_CONTRACT_SCHEMA,
    ActionContext,
    AuthorityDecision,
    AuthorityDecisionDraft,
    AutonomyContract,
    AutonomyRule,
    BudgetReservation,
    ClarificationRequest,
    CostConstraint,
    EvidenceRequirement,
    RuleProvenance,
    RuleScope,
    TimeConstraint,
    canonical_json,
    context_hash,
    contract_hash,
    hash_recipient,
    normalize_action_class,
)
from agent.autonomy.canonical import rule_from_dict, rule_to_dict


def provenance(**overrides) -> RuleProvenance:
    base = dict(
        actor_kind="user",
        actor_id="user-1",
        source_ref="cli",
        observed_at_ms=1_000,
        confirmed_at_ms=2_000,
        confidence_ppm=1_000_000,
    )
    base.update(overrides)
    return RuleProvenance(**base)


def rule(**overrides) -> AutonomyRule:
    base = dict(
        rule_id="r-1",
        source="user_assertion",
        state="active",
        effect="allow",
        action_classes=("message.send",),
        provenance=provenance(),
        created_at_ms=1_000,
    )
    base.update(overrides)
    return AutonomyRule(**base)


def decision(**overrides) -> AuthorityDecision:
    base = dict(
        decision_id="d-1",
        verdict="allow",
        code="explicit_allow",
        reason="matched explicit stable allow rule",
        authority_version=1,
        authority_hash="a" * 64,
        context_hash="b" * 64,
        matched_rule_ids=("r-1",),
        conflicting_rule_ids=(),
        required_evidence=(),
        clarification=None,
        expires_at_ms=None,
        edit_targets=("autonomy rule edit r-1",),
        budget_reservation=None,
    )
    base.update(overrides)
    return AuthorityDecision(**base)


# ── Contract identity ───────────────────────────────────────────────────────


def test_contract_schema_constant_is_frozen():
    assert AUTONOMY_CONTRACT_SCHEMA == "hades.autonomy.v1"


def test_first_proof_action_classes_are_in_vocabulary():
    expected = {
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
    assert expected == set(ACTION_CLASSES)


# ── Source/state authority invariants ───────────────────────────────────────


def test_suggestion_cannot_be_active_authority():
    with pytest.raises(ValueError, match="learned suggestions cannot authorize"):
        AutonomyRule(
            rule_id="r-suggest",
            source="learned_suggestion",
            state="active",
            effect="allow",
            action_classes=("message.send",),
            provenance=provenance(),
        )


def test_suggestion_awaiting_confirmation_is_valid_but_never_active():
    suggested = rule(
        rule_id="r-suggest",
        source="learned_suggestion",
        state="awaiting_confirmation",
        provenance=provenance(confirmed_at_ms=None),
    )
    assert suggested.state == "awaiting_confirmation"


def test_user_assertion_must_be_active():
    with pytest.raises(ValueError, match="user_assertion"):
        rule(source="user_assertion", state="awaiting_confirmation")


def test_temporary_mandate_requires_expiry_or_uses():
    with pytest.raises(ValueError, match="temporary_mandate"):
        rule(source="temporary_mandate", state="active")
    bounded = rule(
        source="temporary_mandate",
        state="active",
        expires_at_ms=5_000,
        max_uses=1,
        remaining_uses=1,
    )
    assert bounded.max_uses == 1


# ── Fail-closed field validation ────────────────────────────────────────────


def test_action_context_requires_unknown_labels_instead_of_empty_high_risk_fields():
    with pytest.raises(ValueError, match="data_classes"):
        ActionContext(operation_key="op-1", stage="commit", action_class="message.send")


def test_action_context_accepts_explicit_unknown_label():
    ctx = ActionContext(
        operation_key="op-1",
        stage="commit",
        action_class="message.send",
        data_classes=("unknown",),
    )
    assert ctx.data_classes == ("unknown",)
    assert ctx.reversibility == "unknown"


def test_action_context_rejects_unknown_enum_values():
    with pytest.raises(ValueError, match="stage"):
        ActionContext(
            operation_key="op-1",
            stage="later",
            action_class="message.send",
            data_classes=("public",),
        )
    with pytest.raises(ValueError, match="data_classes"):
        ActionContext(
            operation_key="op-1",
            stage="commit",
            action_class="message.send",
            data_classes=("very_public",),
        )


def test_action_context_rejects_unnormalized_action_class():
    for bad in ("MessageSend", "message send", "message.", ".send", ""):
        with pytest.raises(ValueError, match="action_class"):
            ActionContext(
                operation_key="op-1",
                stage="commit",
                action_class=bad,
                data_classes=("public",),
            )


def test_rule_rejects_unknown_enum_values():
    with pytest.raises(ValueError, match="effect"):
        rule(effect="maybe")
    with pytest.raises(ValueError, match="source"):
        rule(source="vibes")
    with pytest.raises(ValueError, match="state"):
        rule(state="dormant")
    with pytest.raises(ValueError, match="data_classes"):
        rule(data_classes=("classified",))


def test_rule_rejects_floats_in_canonical_authority():
    with pytest.raises(ValueError, match="integer"):
        rule(created_at_ms=1000.5)
    with pytest.raises(ValueError, match="integer"):
        rule(expires_at_ms=2000.0)
    with pytest.raises(ValueError, match="integer"):
        provenance(confidence_ppm=0.99)
    with pytest.raises(ValueError, match="integer"):
        CostConstraint(max_per_action_cents=5.0)


def test_rule_rejects_bools_in_canonical_authority():
    with pytest.raises(ValueError, match="integer"):
        rule(created_at_ms=True)


def test_rule_rejects_negative_cost_and_time():
    with pytest.raises(ValueError, match="max_per_action_cents"):
        CostConstraint(max_per_action_cents=-1)
    with pytest.raises(ValueError, match="window_ms"):
        CostConstraint(max_per_window_cents=100, window_ms=-5)
    with pytest.raises(ValueError, match="window_start_minute"):
        TimeConstraint(window_start_minute=-1, window_end_minute=100)
    with pytest.raises(ValueError, match="window_end_minute"):
        TimeConstraint(window_start_minute=0, window_end_minute=1_440)


def test_cost_window_cap_requires_window():
    with pytest.raises(ValueError, match="window_ms"):
        CostConstraint(max_per_window_cents=1_000)


def test_rule_rejects_expiry_before_creation():
    with pytest.raises(ValueError, match="expir"):
        rule(created_at_ms=5_000, expires_at_ms=4_999)


def test_allow_rule_requires_action_selector():
    with pytest.raises(ValueError, match="action"):
        rule(effect="allow", action_classes=())


def test_deny_rule_may_be_broad():
    broad = rule(rule_id="r-deny", effect="deny", action_classes=())
    assert broad.effect == "deny"


def test_rule_rejects_duplicate_selectors():
    with pytest.raises(ValueError, match="duplicate"):
        rule(action_classes=("message.send", "message.send"))
    with pytest.raises(ValueError, match="duplicate"):
        rule(data_classes=("public", "public"))
    with pytest.raises(ValueError, match="duplicate"):
        rule(recipient_hashes=("rh-1", "rh-1"))


def test_confidence_ppm_bounds():
    with pytest.raises(ValueError, match="confidence_ppm"):
        provenance(confidence_ppm=-1)
    with pytest.raises(ValueError, match="confidence_ppm"):
        provenance(confidence_ppm=1_000_001)
    assert provenance(confidence_ppm=0).confidence_ppm == 0


def test_remaining_uses_bounds():
    with pytest.raises(ValueError, match="remaining_uses"):
        rule(
            source="temporary_mandate",
            state="active",
            max_uses=1,
            remaining_uses=2,
        )
    with pytest.raises(ValueError, match="remaining_uses"):
        rule(
            source="temporary_mandate",
            state="active",
            max_uses=1,
            remaining_uses=-1,
        )
    with pytest.raises(ValueError, match="max_uses"):
        rule(source="temporary_mandate", state="active", remaining_uses=1)


def test_rule_scope_rejects_traversal_and_duplicates():
    with pytest.raises(ValueError, match="resource_prefixes"):
        RuleScope(resource_prefixes=("workspace:/tmp/../etc",))
    with pytest.raises(ValueError, match="duplicate"):
        RuleScope(resource_prefixes=("workspace:/tmp", "workspace:/tmp"))


# ── Contract snapshot invariants ────────────────────────────────────────────


def contract(**overrides) -> AutonomyContract:
    base = dict(
        version=1,
        contract_hash="c" * 64,
        profile_id="default",
        compiled_at_ms=10_000,
        rules=(rule(),),
    )
    base.update(overrides)
    return AutonomyContract(**base)


def test_contract_defaults_to_frozen_schema():
    assert contract().schema == AUTONOMY_CONTRACT_SCHEMA


def test_contract_excludes_learned_suggestions():
    suggested = rule(
        rule_id="r-suggest",
        source="learned_suggestion",
        state="awaiting_confirmation",
        provenance=provenance(confirmed_at_ms=None),
    )
    with pytest.raises(ValueError, match="learned_suggestion"):
        contract(rules=(suggested,))


def test_contract_rejects_inactive_rules():
    revoked = rule(
        rule_id="r-revoked",
        source="temporary_mandate",
        state="revoked",
        expires_at_ms=5_000,
    )
    with pytest.raises(ValueError, match="active"):
        contract(rules=(revoked,))


def test_contract_rejects_duplicate_rule_ids():
    with pytest.raises(ValueError, match="duplicate"):
        contract(rules=(rule(), rule()))


def test_contract_version_must_be_positive():
    with pytest.raises(ValueError, match="version"):
        contract(version=0)


# ── Decisions ───────────────────────────────────────────────────────────────


def test_decision_verdict_properties():
    allow = decision()
    assert allow.allowed and not allow.requires_approval
    ask = decision(
        verdict="ask",
        code="explicit_ask",
        clarification=ClarificationRequest(
            question="Share personal canary data with rh-colleague?",
            choices=("share once", "always allow", "deny"),
        ),
    )
    assert ask.requires_approval and not ask.allowed
    deny = decision(verdict="deny", code="conflicting_deny", matched_rule_ids=())
    assert not deny.allowed and not deny.requires_approval


def test_decision_rejects_unknown_verdict_and_blank_code():
    with pytest.raises(ValueError, match="verdict"):
        decision(verdict="permit")
    with pytest.raises(ValueError, match="code"):
        decision(code="")


def test_allow_decision_cannot_carry_clarification():
    with pytest.raises(ValueError, match="clarification"):
        decision(
            verdict="allow",
            clarification=ClarificationRequest(question="really?"),
        )


def test_draft_carries_consumption_plan_but_no_identity():
    draft = AuthorityDecisionDraft(
        verdict="allow",
        code="temporary_mandate",
        reason="one-use mandate matched",
        context_hash="b" * 64,
        matched_rule_ids=("m-1",),
        consume_mandate_ids=("m-1",),
        budget_rule_id=None,
    )
    assert draft.consume_mandate_ids == ("m-1",)
    field_names = {f.name for f in dataclasses.fields(AuthorityDecisionDraft)}
    assert "decision_id" not in field_names
    assert "authority_version" not in field_names
    assert "authority_hash" not in field_names


def test_budget_reservation_rejects_negative_amounts():
    with pytest.raises(ValueError, match="amount_cents"):
        BudgetReservation(
            reservation_id="res-1",
            rule_id="r-budget",
            amount_cents=-200,
            created_at_ms=1_000,
        )


def test_evidence_requirement_validates_stage():
    ev = EvidenceRequirement(kind="recipient_verified", stage="pre_action")
    assert ev.stage == "pre_action"
    with pytest.raises(ValueError, match="stage"):
        EvidenceRequirement(kind="recipient_verified", stage="mid_action")
    with pytest.raises(ValueError, match="kind"):
        EvidenceRequirement(kind="", stage="pre_action")


# ── Canonicalization and hashing (Task 2) ───────────────────────────────────


def test_canonical_json_is_deterministic_and_compact():
    text = canonical_json({"b": 2, "a": [1, {"z": None, "y": "é"}]})
    assert text == '{"a":[1,{"y":"é","z":null}],"b":2}'
    # Key insertion order never changes the canonical bytes.
    assert canonical_json({"a": [1, {"y": "é", "z": None}], "b": 2}) == text


def test_canonical_json_rejects_floats_and_non_string_keys():
    with pytest.raises(ValueError, match="float"):
        canonical_json({"amount": 1.5})
    with pytest.raises(ValueError, match="float"):
        canonical_json([float("nan")])
    with pytest.raises(ValueError):
        canonical_json({1: "a"})
    with pytest.raises(ValueError):
        canonical_json({"obj": object()})


def test_contract_hash_is_stable_over_key_order():
    body = {"schema": AUTONOMY_CONTRACT_SCHEMA, "profile_id": "default", "rules": []}
    reordered = {"rules": [], "profile_id": "default", "schema": AUTONOMY_CONTRACT_SCHEMA}
    assert contract_hash(body) == contract_hash(reordered)
    assert len(contract_hash(body)) == 64
    assert contract_hash(body) != contract_hash({**body, "profile_id": "other"})


def test_context_hash_is_stable_and_field_sensitive():
    def ctx(**overrides) -> ActionContext:
        base = dict(
            operation_key="op-1",
            stage="execute",
            action_class="message.send",
            data_classes=("public",),
        )
        base.update(overrides)
        return ActionContext(**base)

    assert context_hash(ctx()) == context_hash(ctx())
    assert context_hash(ctx()) != context_hash(ctx(data_classes=("personal",)))
    assert context_hash(ctx()) != context_hash(ctx(stage="commit"))


def test_rule_round_trips_through_canonical_dicts():
    original = rule(
        source="temporary_mandate",
        state="active",
        expires_at_ms=5_000,
        max_uses=2,
        remaining_uses=1,
        data_classes=("public", "internal"),
        recipient_hashes=("rh-1",),
        scope=RuleScope(task_id="task-1", resource_prefixes=("workspace:/tmp",)),
        cost=CostConstraint(max_per_action_cents=500),
        time=TimeConstraint(window_start_minute=540, window_end_minute=1_020),
        evidence_requirements=(
            EvidenceRequirement(kind="workspace_checkpoint", stage="pre_action"),
        ),
    )
    wire = json.loads(canonical_json(rule_to_dict(original)))
    assert rule_from_dict(wire) == original


def test_normalize_action_class_normalizes_and_fails_closed():
    assert normalize_action_class("  Message.Send ") == "message.send"
    assert normalize_action_class("workspace.delete") == "workspace.delete"
    for bad in ("MessageSend", "message send", "message.", ".send", "", None, 7):
        with pytest.raises(ValueError):
            normalize_action_class(bad)


def test_hash_recipient_is_keyed_exact_and_confusable_distinct():
    key = b"k" * 32
    baseline = hash_recipient("alice@example.test", key=key)
    assert baseline == hash_recipient("alice@example.test", key=key)
    # Case-insensitive equivalence, but Unicode confusables stay distinct.
    assert baseline == hash_recipient(" Alice@Example.Test ", key=key)
    assert baseline != hash_recipient("aлice@example.test", key=key)
    assert baseline != hash_recipient("alice@example.test", key=b"x" * 32)
    assert "alice" not in baseline
    with pytest.raises(ValueError, match="key"):
        hash_recipient("alice@example.test", key=b"short")
    with pytest.raises(ValueError):
        hash_recipient("", key=key)


# ── Immutability ────────────────────────────────────────────────────────────


def test_records_are_frozen():
    for record in (
        provenance(),
        rule(),
        contract(),
        decision(),
        RuleScope(),
        CostConstraint(max_per_action_cents=500),
        TimeConstraint(window_start_minute=540, window_end_minute=1_020),
        EvidenceRequirement(kind="workspace_checkpoint", stage="pre_action"),
        ClarificationRequest(question="proceed?"),
    ):
        first_field = dataclasses.fields(record)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(record, first_field, "mutated")
