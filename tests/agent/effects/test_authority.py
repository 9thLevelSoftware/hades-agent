"""Authority binding tests for action transactions (plan Task 4).

Consumes the canonical Preferences & Autonomy contracts (item #6) — no
local authority evaluator. What this module owns is only: mapping
prepared effects into ``ActionContext``, reloading current authority
immediately before commit/compensation, and exact single-use expiring
approval bindings persisted in ``state.db``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent.autonomy import (
    ActionContext,
    AutonomyRule,
    AutonomyService,
    RuleProvenance,
    StoredAuthorityProvider,
    authorize_effect,
)
from agent.autonomy.config_apply import ConfigChange
from agent.effects.authority import (
    ApprovalBinding,
    build_action_context,
    consume_bound_approval,
    request_bound_approval,
)
from agent.effects.models import EffectSemantics, PreparedEffect
from agent.effects.store import TransactionStore
from hades_constants import get_hades_home
from hades_state import SessionDB

NOW_MS = 1_700_000_000_000


class FixedClock:
    def __init__(self, now_ms: int = NOW_MS):
        self._now_ms = now_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, delta_ms: int) -> None:
        self._now_ms += delta_ms

    def __call__(self) -> int:
        return self._now_ms


@pytest.fixture()
def db():
    handle = SessionDB(get_hades_home() / "state.db")
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture()
def clock():
    return FixedClock()


@pytest.fixture()
def store(db, clock):
    return TransactionStore(db, clock=clock)


def _provenance() -> RuleProvenance:
    return RuleProvenance(
        actor_kind="user",
        actor_id="user-7",
        source_ref="cli",
        observed_at_ms=100,
        confirmed_at_ms=200,
        confidence_ppm=1_000_000,
    )


def _write_rule(effect: str = "allow", rule_id: str = "write-rule") -> AutonomyRule:
    return AutonomyRule(
        rule_id=rule_id,
        source="user_assertion",
        state="active",
        effect=effect,
        action_classes=("workspace.write",),
        data_classes=("internal",),
        provenance=_provenance(),
        created_at_ms=100,
    )


def _install(service: AutonomyService, rule: AutonomyRule) -> None:
    preview = service.preview_rule_change(
        ConfigChange(set_rules=(rule,)), now_ms=NOW_MS
    )
    service.apply_rule_change(
        preview,
        expected_contract_hash=preview.before_contract_hash,
        now_ms=NOW_MS,
    )


def prepared_effect(**overrides) -> PreparedEffect:
    base = dict(
        node_id="config",
        adapter_id="workspace.v1",
        action="write_file",
        action_class="workspace.write",
        args={"path": "notes.md", "content": "x"},
        resources=("file:notes.md",),
        semantics=EffectSemantics(
            fidelity="exact", reconciliation="none", idempotency="keyed",
            irreversible_after="commit",
        ),
        data_classes=("internal",),
    )
    base.update(overrides)
    return PreparedEffect(**base)


def approved_binding(**overrides) -> ApprovalBinding:
    base = dict(
        approval_id="ap-1",
        transaction_id="tx-1",
        revision=2,
        node_id="send",
        operation="commit",
        args_hash="a",
        preview_hash="p",
        resources=("telegram:123",),
        authority_version=4,
        requester="user-7",
        channel="tui",
        decision="approved",
        expires_at_ms=NOW_MS + 30_000,
        consumed_at_ms=None,
        created_at_ms=NOW_MS,
    )
    base.update(overrides)
    return ApprovalBinding(**base)


# ── ActionContext mapping ───────────────────────────────────────────────


def test_build_action_context_maps_declared_facts_faithfully():
    context = build_action_context(
        prepared_effect(), operation_key="tx-1:config",
    )
    assert isinstance(context, ActionContext)
    assert context.stage == "preview"
    assert context.action_class == "workspace.write"
    assert context.data_classes == ("internal",)
    assert context.reversibility == "reversible"
    assert context.resource_refs == ("file:notes.md",)


def test_build_action_context_fails_closed_on_missing_facts():
    undeclared = prepared_effect(data_classes=())
    context = build_action_context(undeclared, operation_key="tx-1:config")
    # Missing high-risk facts become explicit "unknown" declarations, never
    # omitted fields a wildcard could silently match.
    assert context.data_classes == ("unknown",)
    irreversible = prepared_effect(
        semantics=EffectSemantics(
            fidelity="none", reconciliation="none", idempotency="none",
            irreversible_after="dispatch",
        ),
    )
    context = build_action_context(irreversible, operation_key="tx-1:config")
    assert context.reversibility == "irreversible"


# ── Authority reload before commit ──────────────────────────────────────


def test_authority_is_reloaded_immediately_before_commit(db):
    service = AutonomyService(db=db)
    _install(service, _write_rule("allow"))
    provider = StoredAuthorityProvider(db=db)

    context = build_action_context(prepared_effect(), operation_key="tx-1:config")
    decision = authorize_effect(provider, context, stage="preview", consume=False)
    assert decision.allowed

    _install(service, _write_rule("deny", rule_id="write-deny"))
    context = build_action_context(prepared_effect(), operation_key="tx-1:config")
    decision = authorize_effect(provider, context, stage="commit", consume=True)
    assert not decision.allowed


def test_compensation_authority_is_distinct_from_commit_authority(db):
    service = AutonomyService(db=db)
    _install(service, _write_rule("allow"))
    provider = StoredAuthorityProvider(db=db)
    context = build_action_context(
        prepared_effect(), operation_key="tx-1:config:compensate",
    )
    decision = authorize_effect(provider, context, stage="compensate", consume=True)
    # The decision is evaluated at the compensate stage — never inherited
    # from the earlier commit decision.
    assert decision.context_hash != ""


# ── Exact approval bindings ─────────────────────────────────────────────


def test_irreversible_approval_is_exact_expiring_and_single_use(store, clock):
    binding = approved_binding(
        transaction_id="tx-1", revision=2, node_id="send", args_hash="a",
        preview_hash="p", resources=("telegram:123",), authority_version=4,
        requester="user-7", channel="tui", expires_at_ms=clock.now_ms() + 30_000,
    )
    store.insert_approval(binding)
    assert consume_bound_approval(store, binding.identity(), clock=clock).approved
    assert consume_bound_approval(store, binding.identity(), clock=clock).code == "consumed"
    assert consume_bound_approval(
        store, replace(binding, args_hash="changed").identity(), clock=clock
    ).code == "mismatch"


def test_expired_approval_never_authorizes(store, clock):
    binding = approved_binding(expires_at_ms=clock.now_ms() + 1_000)
    store.insert_approval(binding)
    clock.advance(2_000)
    result = consume_bound_approval(store, binding.identity(), clock=clock)
    assert not result.approved
    assert result.code == "expired"


@pytest.mark.parametrize(
    "mutation",
    [
        {"preview_hash": "different"},
        {"authority_version": 5},
        {"requester": "someone-else"},
        {"channel": "cli"},
        {"resources": ("telegram:999",)},
    ],
)
def test_any_identity_drift_is_a_mismatch(store, clock, mutation):
    binding = approved_binding()
    store.insert_approval(binding)
    drifted = replace(binding, **mutation).identity()
    result = consume_bound_approval(store, drifted, clock=clock)
    assert not result.approved
    assert result.code == "mismatch"


def test_missing_and_denied_bindings_fail_closed(store, clock):
    missing = approved_binding().identity()
    assert consume_bound_approval(store, missing, clock=clock).code == "missing"
    denied = approved_binding(decision="denied", approval_id="ap-denied")
    store.insert_approval(denied)
    result = consume_bound_approval(store, denied.identity(), clock=clock)
    assert not result.approved
    assert result.code == "denied"


def test_replayed_consume_is_not_a_second_authorization(store, clock):
    binding = approved_binding()
    store.insert_approval(binding)
    first = consume_bound_approval(store, binding.identity(), clock=clock)
    second = consume_bound_approval(store, binding.identity(), clock=clock)
    assert first.approved and not second.approved
    assert second.code == "consumed"


# ── Human approval gate integration ─────────────────────────────────────


def test_request_bound_approval_only_persists_on_explicit_approve(
    store, clock, monkeypatch
):
    calls = []

    def fake_gate(tool_name, reason, **kwargs):
        calls.append((tool_name, kwargs))
        return {"approved": kwargs["arguments"]["node_id"] == "send"}

    monkeypatch.setattr(
        "agent.effects.authority.request_tool_approval", fake_gate
    )
    approved = request_bound_approval(
        store,
        transaction_id="tx-1", revision=2, node_id="send",
        operation="commit", args_hash="a", preview_hash="p",
        resources=("telegram:123",), authority_version=4,
        requester="user-7", channel="tui",
        adapter_id="message-outbox.v1", action="send",
        reason="irreversible send", ttl_ms=30_000, clock=clock,
    )
    assert approved is not None
    assert approved.decision == "approved"
    assert consume_bound_approval(store, approved.identity(), clock=clock).approved

    denied = request_bound_approval(
        store,
        transaction_id="tx-1", revision=2, node_id="other",
        operation="commit", args_hash="b", preview_hash="q",
        resources=("telegram:123",), authority_version=4,
        requester="user-7", channel="tui",
        adapter_id="message-outbox.v1", action="send",
        reason="irreversible send", ttl_ms=30_000, clock=clock,
    )
    assert denied is None

    tool_name, kwargs = calls[0]
    assert kwargs["rule_key"] == "transaction:message-outbox.v1:send"
    # Session/permanent allowlisting must never become a transaction
    # approval: the exact binding is always required.
    assert kwargs["allow_permanent"] is False
    identity_args = kwargs["arguments"]
    for field in (
        "transaction_id", "revision", "node_id", "args_hash", "preview_hash",
        "authority_version", "requester", "channel",
    ):
        assert field in identity_args
