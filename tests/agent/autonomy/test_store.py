"""Task 2 persistence, replay, and privacy tests for ``agent.autonomy.store``.

Real-path invariants: profile-local SQLite through ``SessionDB``, immutable
contract versions addressed by content hash, revision-guarded runtime rules,
atomic mandate consumption with replay idempotency, bounded budget
reservations, and audit rows that never contain raw sensitive values.
"""

from __future__ import annotations

import pytest

from agent.autonomy import ActionContext, AuthorityDecision, AutonomyRule, RuleProvenance
from agent.autonomy.canonical import context_hash
from agent.autonomy.store import (
    AutonomyBudgetError,
    AutonomyIntegrityError,
    AutonomyStoreConflictError,
    ContractDraft,
    DecisionRecord,
)
from hades_state import SessionDB

NOW_MS = 50_000


# ── Fixture builders ────────────────────────────────────────────────────────


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


def stable_rule(**overrides) -> AutonomyRule:
    base = dict(
        rule_id="r-allow-send",
        source="user_assertion",
        state="active",
        effect="allow",
        action_classes=("message.send",),
        provenance=provenance(),
        created_at_ms=1_000,
    )
    base.update(overrides)
    return AutonomyRule(**base)


def mandate_fixture(
    rule_id: str = "mandate-1", remaining_uses: int | None = 1, **overrides
) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="temporary_mandate",
        state="active",
        effect="allow",
        action_classes=("workspace.delete",),
        provenance=provenance(),
        created_at_ms=1_000,
        max_uses=None if remaining_uses is None else max(remaining_uses, 1),
        remaining_uses=remaining_uses,
        description="one-use delete mandate",
    )
    base.update(overrides)
    return AutonomyRule(**base)


def suggestion_fixture(rule_id: str = "suggest-1", **overrides) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="learned_suggestion",
        state="awaiting_confirmation",
        effect="allow",
        action_classes=("message.send",),
        provenance=provenance(
            actor_kind="learner", confirmed_at_ms=None, confidence_ppm=990_000
        ),
        created_at_ms=1_000,
    )
    base.update(overrides)
    return AutonomyRule(**base)


def contract_fixture(**overrides) -> ContractDraft:
    base = dict(
        profile_id="default",
        compiled_at_ms=10_000,
        rules=(stable_rule(),),
        source_fingerprint="config:abc123",
    )
    base.update(overrides)
    return ContractDraft(**base)


def context_fixture(
    operation_key: str = "op-default", stage: str = "execute", **overrides
) -> ActionContext:
    base = dict(
        operation_key=operation_key,
        stage=stage,
        action_class="workspace.delete",
        data_classes=("internal",),
        reversibility="reversible",
        resource_refs=("workspace:/tmp/canary.txt",),
    )
    base.update(overrides)
    return ActionContext(**base)


def decision_fixture(
    decision_id: str = "decision-1",
    operation_key: str = "op-default",
    stage: str = "execute",
    authority_version: int = 1,
    authority_hash: str | None = None,
    context: ActionContext | None = None,
    verdict: str = "allow",
    matched_rule_ids: tuple[str, ...] = ("r-allow-send",),
) -> DecisionRecord:
    ctx = context or context_fixture(operation_key=operation_key, stage=stage)
    decision = AuthorityDecision(
        decision_id=decision_id,
        verdict=verdict,
        code="explicit_allow" if verdict == "allow" else "explicit_ask",
        reason="matched explicit stable allow rule",
        authority_version=authority_version,
        authority_hash=authority_hash or contract_fixture().content_hash(),
        context_hash=context_hash(ctx),
        matched_rule_ids=matched_rule_ids,
        conflicting_rule_ids=(),
        required_evidence=(),
        clarification=None,
        expires_at_ms=None,
        edit_targets=("autonomy rule edit r-allow-send",),
        budget_reservation=None,
    )
    return DecisionRecord(
        decision=decision,
        operation_key=operation_key,
        stage=stage,
        created_at_ms=NOW_MS,
    )


@pytest.fixture
def db(tmp_path):
    handle = SessionDB(tmp_path / "state.db")
    yield handle
    handle.close()


@pytest.fixture
def store(db):
    autonomy = db.autonomy
    autonomy.materialize_contract(contract_fixture(), now_ms=NOW_MS)
    autonomy.put_runtime_rule(
        mandate_fixture(remaining_uses=1), expected_revision=0, now_ms=NOW_MS
    )
    return autonomy


# ── Reopen durability ───────────────────────────────────────────────────────


def test_reopen_preserves_immutable_version_runtime_rule_and_audit(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    store = db.autonomy
    version = store.materialize_contract(contract_fixture(), now_ms=NOW_MS)
    store.put_runtime_rule(
        mandate_fixture(remaining_uses=1), expected_revision=0, now_ms=NOW_MS
    )
    store.record_decision(decision_fixture(authority_version=version.version))
    db.close()

    reopened_db = SessionDB(tmp_path / "state.db")
    try:
        reopened = reopened_db.autonomy
        assert (
            reopened.get_contract(version.version).content_hash
            == version.content_hash
        )
        assert reopened.get_runtime_rule("mandate-1").remaining_uses == 1
        assert (
            reopened.get_decision("decision-1").authority_version == version.version
        )
    finally:
        reopened_db.close()


# ── Immutable contract versions ─────────────────────────────────────────────


def test_materialize_contract_is_idempotent_by_content_hash(db):
    store = db.autonomy
    first = store.materialize_contract(contract_fixture(), now_ms=NOW_MS)
    again = store.materialize_contract(contract_fixture(), now_ms=NOW_MS + 1)
    assert again.version == first.version
    assert again.content_hash == first.content_hash
    # compiled_at_ms is excluded from identity — same rules, different timestamp
    # still returns the same version (no spurious version churn).
    same_rules_new_time = store.materialize_contract(
        contract_fixture(compiled_at_ms=20_000), now_ms=NOW_MS + 2
    )
    assert same_rules_new_time.version == first.version
    # Actual rule change creates a new version.
    changed = store.materialize_contract(
        contract_fixture(rules=(stable_rule(rule_id="r-deny-delete", effect="deny"),)),
        now_ms=NOW_MS + 3,
    )
    assert changed.version == first.version + 1
    assert store.get_head().version == changed.version


def test_materialize_contract_compare_and_sets_head(db):
    store = db.autonomy
    first = store.materialize_contract(contract_fixture(), now_ms=NOW_MS)
    with pytest.raises(AutonomyStoreConflictError):
        store.materialize_contract(
            contract_fixture(compiled_at_ms=20_000),
            expected_head_version=first.version + 5,
            now_ms=NOW_MS,
        )
    # Head is unchanged after the rejected compare-and-set.
    assert store.get_head().version == first.version


def test_get_contract_verifies_stored_hash_before_use(db):
    store = db.autonomy
    version = store.materialize_contract(contract_fixture(), now_ms=NOW_MS)

    def _tamper(conn):
        conn.execute(
            "UPDATE autonomy_contract_versions SET contract_json = ? "
            "WHERE contract_version = ?",
            ('{"schema":"tampered"}', version.version),
        )

    db._execute_write(_tamper)
    with pytest.raises(AutonomyIntegrityError):
        store.get_contract(version.version)


# ── Runtime rules and revision guard ────────────────────────────────────────


def test_put_runtime_rule_rejects_user_assertion(db):
    with pytest.raises(ValueError, match="user_assertion"):
        db.autonomy.put_runtime_rule(stable_rule(), expected_revision=0, now_ms=NOW_MS)


def test_put_runtime_rule_requires_exact_revision(db):
    store = db.autonomy
    stored = store.put_runtime_rule(
        mandate_fixture(), expected_revision=0, now_ms=NOW_MS
    )
    assert stored.revision == 1

    with pytest.raises(AutonomyStoreConflictError):
        store.put_runtime_rule(mandate_fixture(), expected_revision=0, now_ms=NOW_MS)
    with pytest.raises(AutonomyStoreConflictError):
        store.put_runtime_rule(
            mandate_fixture(remaining_uses=0), expected_revision=7, now_ms=NOW_MS
        )

    revoked = store.put_runtime_rule(
        mandate_fixture(state="revoked"), expected_revision=1, now_ms=NOW_MS
    )
    assert revoked.revision == 2
    assert store.get_runtime_rule("mandate-1").state == "revoked"

    events = store.list_rule_events("mandate-1")
    assert [e.event_type for e in events] == ["created", "updated"]


def test_runtime_rules_accept_learned_suggestions_but_flag_them(db):
    store = db.autonomy
    store.put_runtime_rule(suggestion_fixture(), expected_revision=0, now_ms=NOW_MS)
    stored = store.get_runtime_rule("suggest-1")
    assert stored.source == "learned_suggestion"
    assert stored.state == "awaiting_confirmation"
    assert stored.rule.may_authorize is False


# ── Decisions, consumption, and replay ──────────────────────────────────────


def test_consume_is_atomic_and_idempotent_under_replay(store):
    first = store.consume_rules_and_record_decision(
        decision_fixture(decision_id="d1", operation_key="op-1"), ("mandate-1",)
    )
    replay = store.consume_rules_and_record_decision(
        decision_fixture(decision_id="d2", operation_key="op-1"), ("mandate-1",)
    )
    assert first.consumed_rule_ids == ("mandate-1",)
    assert first.replayed_decision_id is None
    assert replay.replayed_decision_id == "d1"
    assert replay.consumed_rule_ids == ()
    assert store.get_runtime_rule("mandate-1").remaining_uses == 0
    assert store.get_runtime_rule("mandate-1").state == "consumed"
    # Only the first decision was recorded.
    assert store.get_decision("d1") is not None
    assert store.get_decision("d2") is None


def test_consume_rejects_exhausted_mandate_without_partial_writes(store):
    store.consume_rules_and_record_decision(
        decision_fixture(decision_id="d1", operation_key="op-1"), ("mandate-1",)
    )
    with pytest.raises(AutonomyStoreConflictError, match="mandate-1"):
        store.consume_rules_and_record_decision(
            decision_fixture(decision_id="d3", operation_key="op-2"), ("mandate-1",)
        )
    # Atomicity: the failed consumption recorded no decision.
    assert store.get_decision("d3") is None


def test_consume_rejects_expired_mandate(store):
    store.put_runtime_rule(
        mandate_fixture(
            rule_id="mandate-exp",
            expires_at_ms=NOW_MS - 1,
            max_uses=None,
            remaining_uses=None,
        ),
        expected_revision=0,
        now_ms=NOW_MS,
    )
    with pytest.raises(AutonomyStoreConflictError, match="expired"):
        store.consume_rules_and_record_decision(
            decision_fixture(decision_id="d-exp", operation_key="op-exp"),
            ("mandate-exp",),
        )


def test_consume_requires_allow_verdict(store):
    with pytest.raises(ValueError, match="allow"):
        store.consume_rules_and_record_decision(
            decision_fixture(decision_id="d-ask", operation_key="op-ask", verdict="ask"),
            ("mandate-1",),
        )


def test_record_decision_rejects_stale_contract_hash(store):
    with pytest.raises(AutonomyIntegrityError):
        store.record_decision(
            decision_fixture(decision_id="d-stale", authority_hash="f" * 64)
        )


def test_record_decision_round_trips_full_record(store):
    record = decision_fixture(decision_id="d-full", operation_key="op-full")
    store.record_decision(record)
    loaded = store.get_decision("d-full")
    assert loaded.decision == record.decision
    assert loaded.operation_key == "op-full"
    assert loaded.stage == "execute"


# ── Budget ledger ───────────────────────────────────────────────────────────


def test_reserve_budget_rejects_negative_and_over_limit(store):
    store.record_decision(decision_fixture(decision_id="d-b1", operation_key="op-b1"))
    with pytest.raises(AutonomyBudgetError):
        store.reserve_budget(
            rule_id="r-budget",
            operation_key="op-b1",
            decision_id="d-b1",
            amount_micros=-1,
            window_started_at_ms=0,
            now_ms=NOW_MS,
        )
    store.reserve_budget(
        rule_id="r-budget",
        operation_key="op-b1",
        decision_id="d-b1",
        amount_micros=8_000_000,
        window_started_at_ms=0,
        max_window_micros=10_000_000,
        now_ms=NOW_MS,
    )
    store.record_decision(decision_fixture(decision_id="d-b2", operation_key="op-b2"))
    with pytest.raises(AutonomyBudgetError):
        store.reserve_budget(
            rule_id="r-budget",
            operation_key="op-b2",
            decision_id="d-b2",
            amount_micros=4_000_000,
            window_started_at_ms=0,
            max_window_micros=10_000_000,
            now_ms=NOW_MS,
        )
    assert store.window_spend_micros("r-budget", 0) == 8_000_000


def test_reserve_budget_is_unique_per_operation(store):
    store.record_decision(decision_fixture(decision_id="d-b1", operation_key="op-b1"))
    store.reserve_budget(
        rule_id="r-budget",
        operation_key="op-b1",
        decision_id="d-b1",
        amount_micros=2_000_000,
        window_started_at_ms=0,
        now_ms=NOW_MS,
    )
    with pytest.raises(AutonomyBudgetError):
        store.reserve_budget(
            rule_id="r-budget",
            operation_key="op-b1",
            decision_id="d-b1",
            amount_micros=2_000_000,
            window_started_at_ms=0,
            now_ms=NOW_MS,
        )


def test_window_spend_snapshot_drives_evaluator_budget_decisions(store):
    """The store's micros snapshot is the evaluator's ``budget_usage`` input."""
    from agent.autonomy import AutonomyContract, CostConstraint
    from agent.autonomy.canonical import content_hash, rule_to_dict
    from agent.autonomy.evaluator import evaluate_contract

    buy_rule = stable_rule(
        rule_id="r-buy",
        action_classes=("purchase.prepare",),
        data_classes=("financial",),
        recipient_classes=("designated_test",),
        cost=CostConstraint(max_per_window_cents=1_000, window_ms=86_400_000),
    )
    contract = AutonomyContract(
        version=1,
        contract_hash=content_hash([rule_to_dict(buy_rule)]),
        profile_id="default",
        compiled_at_ms=NOW_MS,
        rules=(buy_rule,),
    )
    context = context_fixture(
        operation_key="op-b3",
        action_class="purchase.prepare",
        data_classes=("financial",),
        recipient_class="designated_test",
        resource_refs=(),
        estimated_cost_cents=400,
    )

    fresh_usage = {"r-buy": store.window_spend_micros("r-buy", 0)}
    fresh = evaluate_contract(
        contract, context, now_ms=NOW_MS, budget_usage=fresh_usage
    )
    assert (fresh.verdict, fresh.code) == ("allow", "explicit_allow")
    assert fresh.budget_rule_id == "r-buy"

    # $8 held in the window; the same $4 request now exceeds the $10 cap.
    store.record_decision(decision_fixture(decision_id="d-b3", operation_key="op-b3"))
    store.reserve_budget(
        rule_id="r-buy",
        operation_key="op-b3",
        decision_id="d-b3",
        amount_micros=8_000_000,
        window_started_at_ms=0,
        now_ms=NOW_MS,
    )
    held_usage = {"r-buy": store.window_spend_micros("r-buy", 0)}
    assert held_usage == {"r-buy": 8_000_000}
    held = evaluate_contract(
        contract, context, now_ms=NOW_MS, budget_usage=held_usage
    )
    assert (held.verdict, held.code) == ("deny", "cost_budget_exceeded")


def test_release_budget_frees_the_window(store):
    store.record_decision(decision_fixture(decision_id="d-b1", operation_key="op-b1"))
    store.reserve_budget(
        rule_id="r-budget",
        operation_key="op-b1",
        decision_id="d-b1",
        amount_micros=8_000_000,
        window_started_at_ms=0,
        now_ms=NOW_MS,
    )
    store.release_budget(
        rule_id="r-budget",
        operation_key="op-b1",
        decision_id="d-b1",
        window_started_at_ms=0,
        now_ms=NOW_MS,
    )
    assert store.window_spend_micros("r-budget", 0) == 0


# ── Privacy of audit rows ───────────────────────────────────────────────────


def test_audit_rows_contain_hashes_not_sensitive_values(store):
    recipient = "alice@example.test"
    secret = "sk-canary"  # never handed to the store; proves no payload path
    recipient_hash = store.hash_recipient(recipient)
    ctx = ActionContext(
        operation_key="op-priv",
        stage="execute",
        action_class="message.send",
        data_classes=("personal",),
        recipient_class="external_contact",
        recipient_hash=recipient_hash,
    )
    store.record_decision(
        decision_fixture(decision_id="d-priv", operation_key="op-priv", context=ctx)
    )
    store.put_runtime_rule(
        mandate_fixture(
            rule_id="mandate-priv",
            action_classes=("message.send",),
            recipient_hashes=(recipient_hash,),
        ),
        expected_revision=0,
        now_ms=NOW_MS,
    )
    raw = store.dump_raw_autonomy_tables()
    assert recipient not in raw
    assert secret not in raw
    assert recipient_hash in raw


def test_hash_recipient_is_keyed_and_profile_local(tmp_path):
    db_a = SessionDB(tmp_path / "a" / "state.db")
    db_b = SessionDB(tmp_path / "b" / "state.db")
    try:
        hash_a = db_a.autonomy.hash_recipient("alice@example.test")
        hash_b = db_b.autonomy.hash_recipient("alice@example.test")
        assert hash_a != "alice@example.test"
        assert hash_a == db_a.autonomy.hash_recipient("alice@example.test")
        assert hash_a != hash_b  # per-profile random key, never shared
    finally:
        db_a.close()
        db_b.close()
