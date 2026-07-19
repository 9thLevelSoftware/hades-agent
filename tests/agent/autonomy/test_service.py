"""Task 5 lifecycle and atomic-authorize tests for ``agent.autonomy.service``.

Real-path invariants against the temporary ``HADES_HOME`` set by the
autouse conftest fixture:

- learned suggestions NEVER authorize until an explicit user confirmation
  creates a new stable assertion (via the config saga) or a bounded
  temporary mandate;
- one-use mandates allow exactly one concurrent commit;
- cost reservations are bounded, settled idempotently, and released on a
  lost race;
- evaluation with ``consume=True`` is replay-idempotent;
- every rule is explainable with an exact edit/revoke route;
- redacted export never leaks recipient hashes or the profile hash key;
- runtime history purges never touch rules or stable config;
- a pending (crashed) authority apply fails every evaluation/mutation
  closed until recovery.
"""

from __future__ import annotations

import json
import threading

import pytest

from agent.autonomy import (
    ActionContext,
    AuthorityProvider,
    AutonomyContract,
    AutonomyRule,
    AutonomyService,
    AutonomyServiceError,
    CostConstraint,
    RuleProvenance,
    RuleScope,
    StoredAuthorityProvider,
    UnknownRuleError,
    authorize_effect,
)
from agent.autonomy.config_apply import (
    ConfigChange,
    IncompleteAuthorityApply,
    journal_path,
)
from agent.autonomy.service import AutonomyService as ServiceClass
from hades_constants import get_hades_home
from hades_state import SessionDB

NOW_MS = 1_700_000_000_000
DAY_MS = 86_400_000
OLD_MS = NOW_MS - 10 * DAY_MS


# ── Fixture builders ────────────────────────────────────────────────────────


def user_provenance(**overrides) -> RuleProvenance:
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


def learner_provenance(**overrides) -> RuleProvenance:
    base = dict(
        actor_kind="learner",
        actor_id="pattern-miner",
        source_ref="observed-behavior",
        observed_at_ms=OLD_MS,
        confirmed_at_ms=None,
        confidence_ppm=990_000,
    )
    base.update(overrides)
    return RuleProvenance(**base)


def suggestion_rule(rule_id: str = "suggest-1", **overrides) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="learned_suggestion",
        state="awaiting_confirmation",
        effect="allow",
        action_classes=("message.send",),
        data_classes=("internal",),
        recipient_classes=("colleague",),
        provenance=learner_provenance(),
        created_at_ms=OLD_MS,
        description="observed repeated sends to a colleague",
    )
    base.update(overrides)
    return AutonomyRule(**base)


def mandate_rule(rule_id: str = "mandate-1", **overrides) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="temporary_mandate",
        state="active",
        effect="allow",
        action_classes=("workspace.delete",),
        data_classes=("internal",),
        allowed_reversibility=("reversible",),
        scope=RuleScope(transaction_id="tx-1"),
        provenance=user_provenance(),
        created_at_ms=100,
        max_uses=1,
        remaining_uses=1,
        description="one-use checkpointed delete",
    )
    base.update(overrides)
    return AutonomyRule(**base)


def stable_send_rule(rule_id: str = "send-allow", **overrides) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="user_assertion",
        state="active",
        effect="allow",
        action_classes=("message.send",),
        data_classes=("internal",),
        recipient_classes=("colleague",),
        provenance=user_provenance(),
        created_at_ms=100,
    )
    base.update(overrides)
    return AutonomyRule(**base)


def purchase_cap_rule(rule_id: str = "purchase-cap", **overrides) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="user_assertion",
        state="active",
        effect="allow",
        action_classes=("purchase.prepare",),
        data_classes=("internal",),
        recipient_classes=("sandbox_merchant",),
        cost=CostConstraint(
            max_per_action_cents=500,
            max_per_window_cents=1_000,
            window_ms=DAY_MS,
        ),
        provenance=user_provenance(),
        created_at_ms=100,
    )
    base.update(overrides)
    return AutonomyRule(**base)


def send_context(op: str = "op-send-1", **overrides) -> ActionContext:
    base = dict(
        operation_key=op,
        stage="execute",
        action_class="message.send",
        data_classes=("internal",),
        reversibility="reversible",
        recipient_class="colleague",
    )
    base.update(overrides)
    return ActionContext(**base)


def commit_context(transaction_id: str, op: str, **overrides) -> ActionContext:
    base = dict(
        operation_key=op,
        stage="commit",
        action_class="workspace.delete",
        data_classes=("internal",),
        reversibility="reversible",
        resource_refs=("workspace:/tmp/canary.txt",),
        transaction_id=transaction_id,
    )
    base.update(overrides)
    return ActionContext(**base)


def purchase_context(
    op: str = "op-buy-1", cost_cents: int | None = 200, **overrides
) -> ActionContext:
    base = dict(
        operation_key=op,
        stage="execute",
        action_class="purchase.prepare",
        data_classes=("internal",),
        reversibility="reversible",
        recipient_class="sandbox_merchant",
        estimated_cost_cents=cost_cents,
    )
    base.update(overrides)
    return ActionContext(**base)


@pytest.fixture
def db():
    handle = SessionDB(get_hades_home() / "state.db")
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def service(db):
    return AutonomyService(db=db)


def install_stable(service: AutonomyService, rule: AutonomyRule) -> None:
    preview = service.preview_rule_change(
        ConfigChange(set_rules=(rule,)), now_ms=NOW_MS
    )
    service.apply_rule_change(
        preview,
        expected_contract_hash=preview.before_contract_hash,
        now_ms=NOW_MS,
    )


@pytest.fixture
def race(monkeypatch):
    """Run *workers* service calls that all evaluate the same contract
    snapshot before any of them commits (barrier between decide and
    consume), so mandate consumption truly races."""

    def run(fn, *, workers: int = 2):
        barrier = threading.Barrier(workers)
        original = ServiceClass._decide

        def patched(self, sdb, context, now):
            result = original(self, sdb, context, now)
            barrier.wait(timeout=10)
            return result

        monkeypatch.setattr(ServiceClass, "_decide", patched)
        results: list = [None] * workers
        errors: list = []

        def worker(index: int) -> None:
            try:
                results[index] = fn(index)
            except BaseException as exc:  # noqa: BLE001 — re-raised below
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        if errors:
            raise errors[0]
        return results

    return run


# ── Suggestions never authorize until explicit confirmation ─────────────────


def test_suggestion_never_authorizes_until_explicit_confirmation(service):
    service.propose_suggestion(suggestion_rule(), now_ms=NOW_MS)

    before = service.evaluate(send_context(), consume=False, now_ms=NOW_MS)
    assert before.verdict == "ask"
    assert before.code == "no_authorizing_rule"

    confirmation = service.confirm_suggestion(
        "suggest-1", destination="stable", actor_id="user-1", now_ms=NOW_MS
    )
    assert confirmation.requires_apply
    # Confirmation alone changed nothing: the apply saga must still run.
    assert (
        service.evaluate(send_context(op="op-send-2"), consume=False, now_ms=NOW_MS).verdict
        == "ask"
    )

    service.apply_rule_change(
        confirmation,
        expected_contract_hash=confirmation.before_contract_hash,
        now_ms=NOW_MS,
    )
    after = service.evaluate(send_context(op="op-send-3"), consume=False, now_ms=NOW_MS)
    assert after.verdict == "allow"
    assert after.code == "explicit_allow"
    # The confirmed rule is a NEW user assertion; the suggestion row was
    # resolved, never mutated into authority in place.
    assert confirmation.new_rule_id != "suggest-1"
    resolved = service.explain_rule("suggest-1")
    assert resolved.source == "learned_suggestion"
    assert resolved.state != "awaiting_confirmation"


def test_confirm_suggestion_rejects_non_user_actor(service):
    service.propose_suggestion(suggestion_rule(), now_ms=NOW_MS)
    with pytest.raises(AutonomyServiceError, match="user"):
        service.confirm_suggestion(
            "suggest-1", destination="stable", actor_kind="model",
            actor_id="model-1", now_ms=NOW_MS,
        )
    # Still pending and still not authority.
    assert service.explain_rule("suggest-1").state == "awaiting_confirmation"
    assert service.evaluate(send_context(), consume=False, now_ms=NOW_MS).verdict == "ask"


def test_propose_suggestion_forces_suggestion_lifecycle(service):
    # Even a rule handed in as an active mandate is stored as an
    # awaiting-confirmation suggestion — the proposer cannot self-authorize.
    sneaky = mandate_rule("suggest-sneaky", provenance=learner_provenance())
    stored = service.propose_suggestion(sneaky, now_ms=NOW_MS)
    assert stored.rule.source == "learned_suggestion"
    assert stored.rule.state == "awaiting_confirmation"
    decision = service.evaluate(
        commit_context("tx-1", "op-sneaky"), consume=False, now_ms=NOW_MS
    )
    assert decision.verdict != "allow"


def test_propose_suggestion_rejects_user_provenance(service):
    with pytest.raises(AutonomyServiceError, match="assertion"):
        service.propose_suggestion(
            suggestion_rule(provenance=user_provenance()), now_ms=NOW_MS
        )


def test_reject_suggestion_terminal_and_never_authorizes(service):
    service.propose_suggestion(suggestion_rule(), now_ms=NOW_MS)
    service.reject_suggestion("suggest-1", actor_id="user-1", now_ms=NOW_MS)
    assert service.explain_rule("suggest-1").state == "rejected"
    assert service.evaluate(send_context(), consume=False, now_ms=NOW_MS).verdict == "ask"


def test_confirm_suggestion_as_bounded_mandate(service):
    service.propose_suggestion(
        suggestion_rule(
            "suggest-del",
            action_classes=("workspace.delete",),
            recipient_classes=(),
            allowed_reversibility=("reversible",),
        ),
        now_ms=NOW_MS,
    )
    confirmation = service.confirm_suggestion(
        "suggest-del",
        destination="mandate",
        actor_id="user-1",
        max_uses=1,
        expires_at_ms=NOW_MS + 60_000,
        now_ms=NOW_MS,
    )
    assert not confirmation.requires_apply
    assert confirmation.mandate is not None
    assert confirmation.mandate.rule.source == "temporary_mandate"
    assert confirmation.mandate.rule.max_uses == 1
    # The suggestion is resolved, not mutated into the mandate.
    assert service.explain_rule("suggest-del").state != "awaiting_confirmation"
    mandate = service.explain_rule(confirmation.new_rule_id)
    assert mandate.source == "temporary_mandate"
    assert mandate.rule.provenance.actor_kind == "user"

    decision = service.evaluate(
        ActionContext(
            operation_key="op-del-conf",
            stage="execute",
            action_class="workspace.delete",
            data_classes=("internal",),
            reversibility="reversible",
            resource_refs=("workspace:/tmp/canary.txt",),
        ),
        consume=False,
        now_ms=NOW_MS,
    )
    assert decision.verdict == "allow"
    assert decision.code == "temporary_mandate"

    # An unbounded mandate confirmation is rejected outright.
    service.propose_suggestion(suggestion_rule("suggest-unbounded"), now_ms=NOW_MS)
    with pytest.raises(AutonomyServiceError, match="bounded"):
        service.confirm_suggestion(
            "suggest-unbounded", destination="mandate", actor_id="user-1",
            now_ms=NOW_MS,
        )


# ── Mandates: one-use atomicity, revocation ────────────────────────────────


def test_one_use_mandate_allows_exactly_one_concurrent_commit(service, race):
    service.create_mandate(mandate_rule(), now_ms=NOW_MS)

    decisions = race(
        lambda i: service.evaluate(
            commit_context("tx-1", f"op-del-{i}"), consume=True, now_ms=NOW_MS
        ),
        workers=2,
    )
    assert sorted(d.verdict for d in decisions) == ["allow", "deny"]
    winner = next(d for d in decisions if d.verdict == "allow")
    loser = next(d for d in decisions if d.verdict == "deny")
    assert winner.code == "temporary_mandate"
    assert loser.code == "mandate_consumed"
    assert service.explain_rule("mandate-1").state == "consumed"
    assert service.explain_rule("mandate-1").remaining_uses == 0


def test_create_mandate_requires_user_provenance(service):
    with pytest.raises(AutonomyServiceError, match="user"):
        service.create_mandate(
            mandate_rule(provenance=learner_provenance()), now_ms=NOW_MS
        )


def test_revoke_mandate_removes_authority(service):
    service.create_mandate(mandate_rule(), now_ms=NOW_MS)
    context = commit_context("tx-1", "op-del-rev")
    assert service.evaluate(context, consume=False, now_ms=NOW_MS).verdict == "allow"

    service.revoke_mandate("mandate-1", actor_id="user-1", now_ms=NOW_MS)
    assert service.explain_rule("mandate-1").state == "revoked"
    after = service.evaluate(context, consume=False, now_ms=NOW_MS)
    assert after.verdict != "allow"

    with pytest.raises(AutonomyServiceError, match="user"):
        service.revoke_mandate("mandate-1", actor_kind="model", actor_id="m", now_ms=NOW_MS)
    with pytest.raises(UnknownRuleError):
        service.revoke_mandate("missing-rule", actor_id="user-1", now_ms=NOW_MS)


# ── Replay idempotency ─────────────────────────────────────────────────────


def test_consuming_evaluate_is_replay_idempotent(service):
    install_stable(service, stable_send_rule())
    first = service.evaluate(send_context(op="op-replay"), consume=True, now_ms=NOW_MS)
    second = service.evaluate(send_context(op="op-replay"), consume=True, now_ms=NOW_MS)
    assert first.verdict == "allow"
    assert second.decision_id == first.decision_id
    records = service.list_decisions()
    assert [r.decision.decision_id for r in records] == [first.decision_id]


def test_non_consuming_evaluate_records_nothing(service):
    install_stable(service, stable_send_rule())
    decision = service.evaluate(send_context(op="op-shadow"), consume=False, now_ms=NOW_MS)
    assert decision.verdict == "allow"
    assert service.list_decisions() == ()


# ── Budgets ────────────────────────────────────────────────────────────────


def test_cost_reservation_is_released_or_settled_once(service):
    install_stable(service, purchase_cap_rule())
    decision = service.evaluate(
        purchase_context(cost_cents=200), consume=True, now_ms=NOW_MS
    )
    assert decision.verdict == "allow"
    assert decision.budget_reservation is not None
    assert decision.budget_reservation.amount_cents == 200
    assert decision.budget_reservation.rule_id == "purchase-cap"
    # Reservation holds 200 cents = 2,000,000 micros until settlement.
    assert service.budget_usage(rule_id="purchase-cap", now_ms=NOW_MS) == 2_000_000

    assert service.settle_cost(
        decision.decision_id, actual_micros=1_500_000, now_ms=NOW_MS
    )
    # Second settlement is an idempotent no-op, never double-counted.
    assert not service.settle_cost(
        decision.decision_id, actual_micros=1_500_000, now_ms=NOW_MS
    )
    assert service.budget_usage(rule_id="purchase-cap", now_ms=NOW_MS) == 1_500_000


def test_over_cap_purchase_denies_and_reserves_nothing(service):
    install_stable(service, purchase_cap_rule())
    decision = service.evaluate(
        purchase_context(op="op-buy-over", cost_cents=600), consume=True, now_ms=NOW_MS
    )
    assert decision.verdict == "deny"
    assert decision.code == "cost_per_action_exceeded"
    assert decision.budget_reservation is None
    assert service.budget_usage(rule_id="purchase-cap", now_ms=NOW_MS) == 0
    # The deny was still audited.
    codes = [r.decision.code for r in service.list_decisions()]
    assert "cost_per_action_exceeded" in codes


def test_settle_cost_requires_a_reserved_decision(service):
    install_stable(service, stable_send_rule())
    decision = service.evaluate(send_context(op="op-nores"), consume=True, now_ms=NOW_MS)
    with pytest.raises(AutonomyServiceError, match="reservation"):
        service.settle_cost(decision.decision_id, actual_micros=100, now_ms=NOW_MS)
    with pytest.raises(AutonomyServiceError):
        service.settle_cost("missing-decision", actual_micros=100, now_ms=NOW_MS)


# ── Provider protocol ──────────────────────────────────────────────────────


def test_stored_provider_satisfies_protocol_and_stage_defaults(service, db):
    provider = StoredAuthorityProvider(db=db)
    assert isinstance(provider, AuthorityProvider)
    contract = provider.current_contract()
    assert isinstance(contract, AutonomyContract)

    preview_decision = authorize_effect(
        provider, send_context(op="op-preview"), stage="preview"
    )
    assert preview_decision.verdict == "ask"  # no rules yet, conservative
    assert service.list_decisions() == ()  # preview never consumes/records

    execute_decision = authorize_effect(
        provider, send_context(op="op-exec"), stage="execute"
    )
    recorded = [r.decision.decision_id for r in service.list_decisions()]
    assert execute_decision.decision_id in recorded  # execute consumes by default
    assert (
        authorize_effect(
            provider, send_context(op="op-exec-2"), stage="execute", consume=False
        ).verdict
        == "ask"
    )


# ── Explain / list ─────────────────────────────────────────────────────────


def test_every_rule_is_explainable_with_edit_route(service):
    install_stable(service, stable_send_rule())
    service.create_mandate(mandate_rule(), now_ms=NOW_MS)
    service.propose_suggestion(suggestion_rule(), now_ms=NOW_MS)

    listed = {rule.rule_id for rule in service.list_rules()}
    assert {"send-allow", "mandate-1", "suggest-1"} <= listed

    stable = service.explain_rule("send-allow")
    assert stable.layer == "stable_config"
    assert stable.in_current_contract
    assert stable.confidence_ppm == 1_000_000
    assert any("send-allow" in cmd for cmd in stable.edit_route)

    mandate = service.explain_rule("mandate-1")
    assert mandate.layer == "runtime"
    assert mandate.in_current_contract
    assert mandate.remaining_uses == 1
    assert any("revoke" in cmd for cmd in mandate.revoke_route)

    suggestion = service.explain_rule("suggest-1")
    assert suggestion.layer == "runtime"
    assert not suggestion.in_current_contract  # suggestions never compile in
    assert suggestion.confidence_ppm == 990_000
    assert any("confirm" in cmd for cmd in suggestion.edit_route)

    with pytest.raises(UnknownRuleError):
        service.explain_rule("no-such-rule")


def test_explanations_name_conflicting_rules(service):
    install_stable(service, stable_send_rule())
    install_stable(
        service,
        stable_send_rule(
            "send-deny", effect="deny", recipient_classes=(), data_classes=()
        ),
    )
    explanation = service.explain_rule("send-allow")
    assert "send-deny" in explanation.conflicts_with


# ── Redacted export ────────────────────────────────────────────────────────


def test_export_redacts_hashes_and_excludes_decisions(service):
    secret_hash = "f" * 64
    install_stable(
        service,
        stable_send_rule(recipient_classes=(), recipient_hashes=(secret_hash,)),
    )
    service.propose_suggestion(suggestion_rule(), now_ms=NOW_MS)
    service.evaluate(
        send_context(op="op-exported", recipient_class="colleague"),
        consume=True,
        now_ms=NOW_MS,
    )

    export = service.export_redacted(now_ms=NOW_MS)
    dumped = json.dumps(export)
    assert secret_hash not in dumped
    assert "local:recipient:" in dumped
    assert "recipient_hash_key" not in dumped
    assert "decisions" not in export
    rule_ids = {entry["rule_id"] for entry in export["stable_rules"]}
    assert "send-allow" in rule_ids
    runtime_ids = {entry["rule_id"] for entry in export["runtime_rules"]}
    assert "suggest-1" in runtime_ids


# ── Runtime history purge ──────────────────────────────────────────────────


def test_purge_runtime_history_keeps_rules_and_open_reservations(service, db):
    install_stable(service, stable_send_rule())
    install_stable(service, purchase_cap_rule())

    old_settled = service.evaluate(send_context(op="op-old"), consume=True, now_ms=OLD_MS)
    held = service.evaluate(
        purchase_context(op="op-hold"), consume=True, now_ms=OLD_MS
    )
    assert held.budget_reservation is not None  # reserve never settled
    fresh = service.evaluate(send_context(op="op-new"), consume=True, now_ms=NOW_MS)

    service.create_mandate(mandate_rule("mandate-old"), now_ms=OLD_MS)
    service.revoke_mandate("mandate-old", actor_id="user-1", now_ms=OLD_MS)
    service.propose_suggestion(suggestion_rule(), now_ms=OLD_MS)

    counts = service.purge_runtime_history(before_ms=NOW_MS - DAY_MS)
    assert counts["decisions"] >= 1

    remaining = {r.decision.decision_id for r in service.list_decisions()}
    assert old_settled.decision_id not in remaining
    assert fresh.decision_id in remaining
    # An unsettled reservation still holds budget: its decision survives.
    assert held.decision_id in remaining
    assert service.budget_usage(rule_id="purchase-cap", now_ms=OLD_MS) == 2_000_000

    # Rules are never deleted — only history is.
    assert service.explain_rule("mandate-old").state == "revoked"
    assert service.explain_rule("suggest-1").state == "awaiting_confirmation"
    assert {"send-allow", "purchase-cap"} <= {r.rule_id for r in service.list_rules()}
    # Terminal-rule lifecycle events older than the boundary are gone;
    # pending-suggestion events are retained.
    assert db.autonomy.list_rule_events("mandate-old") == ()
    assert db.autonomy.list_rule_events("suggest-1") != ()


# ── Fail closed on pending apply ───────────────────────────────────────────


def test_pending_apply_fails_all_evaluation_and_mutation_closed(service):
    install_stable(service, stable_send_rule())
    journal_path().write_text(
        json.dumps({"schema": 1, "before_config_hash": "x", "after_config_hash": "y"}),
        encoding="utf-8",
    )
    try:
        with pytest.raises(IncompleteAuthorityApply):
            service.evaluate(send_context(op="op-blocked"), consume=False, now_ms=NOW_MS)
        with pytest.raises(IncompleteAuthorityApply):
            service.propose_suggestion(suggestion_rule(), now_ms=NOW_MS)
        with pytest.raises(IncompleteAuthorityApply):
            service.create_mandate(mandate_rule(), now_ms=NOW_MS)
        with pytest.raises(IncompleteAuthorityApply):
            service.purge_runtime_history(before_ms=NOW_MS)
    finally:
        journal_path().unlink()
    # After the journal is resolved, evaluation works again.
    assert (
        service.evaluate(send_context(op="op-unblocked"), consume=False, now_ms=NOW_MS).verdict
        == "allow"
    )
