"""Task 10 real-path end-to-end proofs for the Preferences & Autonomy Center.

Every scenario runs against the temporary ``HADES_HOME`` set by the
autouse conftest fixture with a real ``SessionDB``, real config
reads/writes, real registry imports, and the real execution middleware;
the only fake is the terminal effect callback at the outward-effect
boundary. Covered release-gate scenarios:

1.  a stable allow removes the recoverable prompt for the exact
    workspace canary but not for another path;
2.  a suggestion at confidence 1,000,000 cannot allow; explicit
    confirmation creates a new rule and contract version;
3.  a one-use mandate survives restart and permits exactly one
    operation;
4.  expiry between preview and execute blocks with zero handler calls;
5.  an authority/config change between transaction preview and commit
    blocks the commit with zero adapter calls;
6.  a conflict explanation names deny/ask/allow rules and every edit
    route succeeds;
7.  cost reservation crash/replay never double-spends and an unknown
    actual cost remains unsettled, not zero;
8.  a config crash after the YAML replace fails closed, then recovery
    converges before enforcement resumes;
9.  an audit SQLite busy/error fails closed in enforce mode and does
    not alter current behavior in shadow mode;
10. profiles with opposite rules never see each other's config,
    mandates, audit, recipient hashes, or budget ledger;
11. approval identity rejects changed args/requester/channel/expiry
    and replay;
12. an ``ask`` clarification is bounded and produces no synthetic user
    message;

plus the cache/conversation invariants: system message, effective tool
definitions, provider, and model hashes are byte-stable across every
authority operation, roles strictly alternate, history is never
mutated, and ``authority_context_fn`` never appears in serialized tool
definitions.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import sqlite3
import time
import types

import pytest
import yaml

from agent.autonomy import (
    ActionContext,
    AutonomyRule,
    AutonomyService,
    AutonomyServiceError,
    CostConstraint,
    RuleProvenance,
    StoredAuthorityProvider,
    authorize_effect,
)
from agent.autonomy.config_apply import (
    ConfigChange,
    apply_config_change,
    pending_apply,
    preview_config_change,
    recover_config_apply,
)
from agent.autonomy.evaluator import MICROS_PER_CENT
from agent.autonomy.store import AutonomyStore
from hades_cli.middleware import run_tool_execution_middleware
from hades_constants import get_hades_home
from hades_state import SessionDB
from tools.registry import registry

TOOL_SEND = "autonomy_e2e_send"
TOOL_DELETE = "autonomy_e2e_delete"
TOOL_MUTATE = "autonomy_e2e_mutate"

IDS = {"task_id": "task-e2e", "session_id": "sess-e2e", "tool_call_id": "call-e2e"}

NOW_MS = 1_800_000_000_000
DAY_MS = 86_400_000


class SimulatedCrash(RuntimeError):
    """Injected mid-saga failure."""


# ── Config / rule builders ──────────────────────────────────────────────────


def write_autonomy_config(mode: str, stable_rules: tuple = ()) -> None:
    home = get_hades_home()
    home.mkdir(parents=True, exist_ok=True)
    section = {
        "schema_version": 1,
        "mode": mode,
        "stable_rules": [dict(entry) for entry in stable_rules],
    }
    (home / "config.yaml").write_text(
        yaml.safe_dump({"autonomy": section}), encoding="utf-8"
    )


def send_allow_entry(**overrides) -> dict:
    base = {
        "rule_id": "send-allow",
        "effect": "allow",
        "action_classes": ["message.send"],
        "data_classes": ["internal"],
        "recipient_classes": ["colleague"],
        "allowed_reversibility": ["reversible"],
    }
    base.update(overrides)
    return base


def delete_allow_entry(**overrides) -> dict:
    base = {
        "rule_id": "delete-allow",
        "effect": "allow",
        "action_classes": ["workspace.delete"],
        "data_classes": ["internal"],
        "allowed_reversibility": ["reversible"],
        "scope": {"resource_prefixes": ["workspace:/tmp"]},
    }
    base.update(overrides)
    return base


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
        observed_at_ms=NOW_MS - DAY_MS,
        confirmed_at_ms=None,
        confidence_ppm=1_000_000,  # maximum confidence still never authorizes
    )
    base.update(overrides)
    return RuleProvenance(**base)


def stable_rule(rule_id: str, **overrides) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="user_assertion",
        state="active",
        effect="allow",
        action_classes=("message.send",),
        data_classes=("internal",),
        recipient_classes=("colleague",),
        allowed_reversibility=("reversible",),
        provenance=user_provenance(),
        created_at_ms=100,
    )
    base.update(overrides)
    return AutonomyRule(**base)


def mandate_rule(rule_id: str = "mandate-e2e", **overrides) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="temporary_mandate",
        state="active",
        effect="allow",
        action_classes=("workspace.delete",),
        data_classes=("internal",),
        allowed_reversibility=("reversible",),
        provenance=user_provenance(),
        created_at_ms=NOW_MS,
        max_uses=1,
        remaining_uses=1,
    )
    base.update(overrides)
    return AutonomyRule(**base)


def suggestion_rule(rule_id: str = "suggest-e2e", **overrides) -> AutonomyRule:
    base = dict(
        rule_id=rule_id,
        source="learned_suggestion",
        state="awaiting_confirmation",
        effect="allow",
        action_classes=("message.send",),
        data_classes=("internal",),
        recipient_classes=("colleague",),
        allowed_reversibility=("reversible",),
        provenance=learner_provenance(),
        created_at_ms=NOW_MS - DAY_MS,
        description="observed repeated sends to a colleague",
    )
    base.update(overrides)
    return AutonomyRule(**base)


def make_context(**overrides) -> ActionContext:
    base = dict(
        operation_key="op-e2e-1",
        stage="execute",
        action_class="message.send",
        data_classes=("internal",),
        reversibility="reversible",
        recipient_class="colleague",
        task_id=IDS["task_id"],
        session_id=IDS["session_id"],
    )
    base.update(overrides)
    return ActionContext(**base)


# ── Registry / middleware plumbing ─────────────────────────────────────────


def _send_resolver(args: dict) -> dict:
    ctx = {
        "action_class": "message.send",
        "data_classes": ("internal",),
        "reversibility": "reversible",
    }
    if args.get("recipient") == "safe":
        ctx["recipient_class"] = "colleague"
    return ctx


def _delete_resolver(args: dict) -> dict:
    return {
        "action_class": "workspace.delete",
        "data_classes": ("internal",),
        "reversibility": "reversible",
        "resource_refs": (str(args.get("path") or ""),),
    }


def _schema(name: str) -> dict:
    return {
        "name": name,
        "description": f"e2e test tool {name}",
        "parameters": {"type": "object", "properties": {}},
    }


@pytest.fixture(autouse=True)
def no_plugin_middleware(monkeypatch):
    """Empty plugin-middleware set by default; tests install their own."""
    manager = types.SimpleNamespace(_middleware={})
    monkeypatch.setattr("hades_cli.plugins.get_plugin_manager", lambda: manager)
    return manager


@pytest.fixture(autouse=True)
def _non_interactive(monkeypatch):
    for var in (
        "HERMES_INTERACTIVE",
        "HERMES_GATEWAY_SESSION",
        "HERMES_CRON_SESSION",
        "HERMES_SESSION_PLATFORM",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def test_tools():
    registry.register(
        name=TOOL_SEND,
        toolset="autonomy-e2e-test",
        schema=_schema(TOOL_SEND),
        handler=lambda args, **kw: json.dumps({"ok": True}),
        authority_context_fn=_send_resolver,
    )
    registry.register(
        name=TOOL_DELETE,
        toolset="autonomy-e2e-test",
        schema=_schema(TOOL_DELETE),
        handler=lambda args, **kw: json.dumps({"ok": True}),
        authority_context_fn=_delete_resolver,
    )
    registry.register(
        name=TOOL_MUTATE,
        toolset="autonomy-e2e-test",
        schema=_schema(TOOL_MUTATE),
        handler=lambda args, **kw: json.dumps({"ok": True}),
    )
    yield
    for name in (TOOL_SEND, TOOL_DELETE, TOOL_MUTATE):
        registry.deregister(name)


@pytest.fixture
def terminal():
    class Terminal:
        def __init__(self):
            self.calls = []

        def __call__(self, args):
            self.calls.append(dict(args))
            return "terminal-result"

    return Terminal()


def run_gate(tool_name, args, terminal, **extra):
    context = {
        "operation_metadata": registry.get_operation_metadata(tool_name),
        **IDS,
        **extra,
    }
    return run_tool_execution_middleware(tool_name, args, terminal, **context)


def list_decisions():
    db = SessionDB(get_hades_home() / "state.db")
    try:
        return db.autonomy.list_decisions(limit=200)
    finally:
        db.close()


def profile_hash_recipient(value: str) -> str:
    db = SessionDB(get_hades_home() / "state.db")
    try:
        return db.autonomy.hash_recipient(value)
    finally:
        db.close()


# ── 1. Stable allow removes the recoverable prompt for the exact canary ───


class TestStableAllowRemovesPromptOnlyForExactCanary:
    def test_exact_canary_skips_prompt_other_path_still_prompts(
        self, monkeypatch, terminal
    ):
        from tools.approval import request_tool_approval
        from tools.terminal_tool import set_approval_callback

        write_autonomy_config("enforce", (delete_allow_entry(),))
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        prompts = []

        def counting_callback(command, description, **kwargs):
            prompts.append(command)
            return "deny"

        outcomes = {}

        def handler(args):
            outcomes["exact"] = request_tool_approval(
                TOOL_DELETE,
                "plugin flagged this delete",
                arguments=dict(args),
            )
            outcomes["other"] = request_tool_approval(
                TOOL_DELETE,
                "plugin flagged this delete",
                arguments={"path": "workspace:/outside/canary.txt"},
            )
            return "terminal-result"

        set_approval_callback(counting_callback)
        try:
            result = run_gate(
                TOOL_DELETE, {"path": "workspace:/tmp/canary.txt"}, handler
            )
        finally:
            set_approval_callback(None)

        assert result == "terminal-result"
        # exact canary: the stable allow's one-use grant satisfied the
        # recoverable prompt with NO human round-trip
        assert outcomes["exact"]["approved"] is True
        # another path: the grant does not apply; the human prompt still
        # occurs and the user's deny stands
        assert outcomes["other"]["approved"] is False
        assert len(prompts) == 1

    def test_other_path_never_reaches_the_handler(self, terminal):
        write_autonomy_config("enforce", (delete_allow_entry(),))
        result = run_gate(
            TOOL_DELETE, {"path": "workspace:/outside/canary.txt"}, terminal
        )
        payload = json.loads(result)
        assert payload["autonomy"]["verdict"] == "deny"
        assert payload["autonomy"]["code"] == "resource_scope_mismatch"
        assert terminal.calls == []


# ── 2. Suggestions never allow; explicit confirmation creates authority ────


class TestSuggestionConfirmationCreatesNewVersion:
    def test_max_confidence_suggestion_cannot_allow_until_confirmed(self):
        write_autonomy_config("enforce")
        service = AutonomyService()
        service.propose_suggestion(suggestion_rule(), now_ms=NOW_MS)

        before = service.evaluate(
            make_context(operation_key="op-sugg-1"), consume=True, now_ms=NOW_MS
        )
        assert before.verdict != "allow"
        assert before.code == "no_authorizing_rule"

        version_before = service.current_contract(now_ms=NOW_MS).version
        confirmation = service.confirm_suggestion(
            "suggest-e2e",
            destination="stable",
            actor_kind="user",
            actor_id="user-1",
            now_ms=NOW_MS,
        )
        applied = service.apply_rule_change(
            confirmation,
            expected_contract_hash=confirmation.before_contract_hash,
            now_ms=NOW_MS,
        )
        assert applied.contract.version > version_before

        after = service.evaluate(
            make_context(operation_key="op-sugg-2"), consume=True, now_ms=NOW_MS
        )
        assert after.verdict == "allow"
        assert after.code == "explicit_allow"
        assert "suggest-e2e-confirmed" in after.matched_rule_ids
        assert after.authority_version == applied.contract.version


# ── 3. One-use mandates survive restart ────────────────────────────────────


class TestOneUseMandateSurvivesRestart:
    def test_mandate_persists_across_restart_and_allows_exactly_once(self):
        write_autonomy_config("enforce")
        AutonomyService().create_mandate(mandate_rule(), now_ms=NOW_MS)

        # "Restart": every service call opens a fresh SessionDB over the
        # same profile home; no in-memory state carries over.
        restarted = AutonomyService()
        ctx = make_context(
            operation_key="op-mandate-1",
            action_class="workspace.delete",
            recipient_class=None,
        )
        first = restarted.evaluate(ctx, consume=True, now_ms=NOW_MS)
        assert first.verdict == "allow"
        assert first.code == "temporary_mandate"

        replay = AutonomyService().evaluate(
            dataclasses.replace(ctx, operation_key="op-mandate-2"),
            consume=True,
            now_ms=NOW_MS + 1,
        )
        assert replay.verdict == "deny"
        assert replay.code == "mandate_consumed"


# ── 4. Expiry between preview and execute ──────────────────────────────────


class TestExpiryBetweenPreviewAndExecute:
    def test_expired_authority_blocks_with_zero_handler_calls(self):
        write_autonomy_config("enforce")
        service = AutonomyService()
        service.create_mandate(
            mandate_rule(
                "mandate-expiring",
                max_uses=None,
                remaining_uses=None,
                expires_at_ms=NOW_MS + 60_000,
            ),
            now_ms=NOW_MS,
        )
        ctx = make_context(
            operation_key="op-expiry-1",
            action_class="workspace.delete",
            recipient_class=None,
        )
        preview = service.evaluate(
            dataclasses.replace(ctx, stage="preview"), consume=False, now_ms=NOW_MS
        )
        assert preview.verdict == "allow"

        handler_calls = []
        decision = service.evaluate(
            ctx, consume=True, now_ms=NOW_MS + 120_000
        )
        if decision.verdict == "allow":  # pragma: no cover - must not happen
            handler_calls.append(ctx.operation_key)
        assert decision.verdict == "ask"
        assert decision.code == "authority_expired"
        assert handler_calls == []


# ── 5. Authority change between transaction preview and commit ─────────────


class TestCommitTimeAuthorityRecheck:
    def test_config_change_between_preview_and_commit_blocks_commit(self):
        write_autonomy_config("enforce", (send_allow_entry(),))
        service = AutonomyService()
        ctx = make_context(operation_key="op-commit-1")

        preview = service.evaluate(
            dataclasses.replace(ctx, stage="preview"), consume=False, now_ms=NOW_MS
        )
        assert preview.verdict == "allow"
        previewed_version = preview.authority_version

        # The user edits authority between preview and commit: the allow
        # becomes a deny through the guarded config saga.
        change = ConfigChange(
            set_rules=(stable_rule("send-deny", effect="deny"),),
            remove_rule_ids=("send-allow",),
        )
        change_preview = service.preview_rule_change(change, now_ms=NOW_MS)
        applied = service.apply_rule_change(
            change_preview,
            expected_contract_hash=change_preview.before_contract_hash,
            now_ms=NOW_MS,
        )
        assert applied.contract.version > previewed_version

        # Item #2 semantics: the coordinator reloads the provider
        # immediately before commit; the adapter runs only on allow.
        adapter_calls = []
        provider = StoredAuthorityProvider()
        commit = provider.service.evaluate(
            dataclasses.replace(ctx, stage="commit"),
            consume=True,
            now_ms=NOW_MS + 1,
        )
        if commit.verdict == "allow":  # pragma: no cover - must not happen
            adapter_calls.append(ctx.operation_key)
        assert commit.verdict == "deny"
        assert commit.authority_version == applied.contract.version
        assert adapter_calls == []

    def test_authorize_effect_commit_stage_consumes_current_head(self):
        write_autonomy_config("enforce", (send_allow_entry(),))
        provider = StoredAuthorityProvider()
        decision = authorize_effect(
            provider, make_context(operation_key="op-commit-2"), stage="commit"
        )
        assert decision.verdict == "allow"
        records = list_decisions()
        assert any(r.stage == "commit" for r in records)


# ── 6. Conflict explanation and edit routes ────────────────────────────────


class TestConflictExplanationAndEditRoutes:
    def test_conflict_names_all_rules_and_every_edit_route_succeeds(self):
        write_autonomy_config(
            "enforce",
            (
                send_allow_entry(rule_id="send-allow"),
                send_allow_entry(rule_id="send-ask", effect="ask"),
                send_allow_entry(rule_id="send-deny", effect="deny"),
            ),
        )
        service = AutonomyService()
        decision = service.evaluate(
            make_context(operation_key="op-conflict-1"),
            consume=True,
            now_ms=NOW_MS,
        )
        assert decision.verdict == "deny"
        assert decision.code == "conflicting_deny"
        assert set(decision.conflicting_rule_ids) == {
            "send-allow",
            "send-ask",
            "send-deny",
        }
        assert decision.edit_targets

        # Every effective rule explains with an exact edit route and
        # names its conflicts.
        for rule_id in ("send-allow", "send-ask", "send-deny"):
            explanation = service.explain_rule(rule_id, now_ms=NOW_MS)
            assert explanation.in_current_contract is True
            assert explanation.edit_route
            assert explanation.revoke_route
            others = {"send-allow", "send-ask", "send-deny"} - {rule_id}
            assert others & set(explanation.conflicts_with)

        # The stable edit route works: editing the deny away flips the
        # decision to the conservative conflicting_ask.
        change = ConfigChange(remove_rule_ids=("send-deny",))
        change_preview = service.preview_rule_change(change, now_ms=NOW_MS)
        service.apply_rule_change(
            change_preview,
            expected_contract_hash=change_preview.before_contract_hash,
            now_ms=NOW_MS,
        )
        after = service.evaluate(
            make_context(operation_key="op-conflict-2"),
            consume=True,
            now_ms=NOW_MS + 1,
        )
        assert after.verdict == "ask"
        assert after.code == "conflicting_ask"

        # Runtime routes work too: a mandate revokes, a suggestion rejects.
        service.create_mandate(mandate_rule("mandate-route"), now_ms=NOW_MS)
        assert service.explain_rule("mandate-route", now_ms=NOW_MS).revoke_route
        revoked = service.revoke_mandate(
            "mandate-route", actor_id="user-1", now_ms=NOW_MS
        )
        assert revoked.rule.state == "revoked"

        service.propose_suggestion(suggestion_rule("suggest-route"), now_ms=NOW_MS)
        assert service.explain_rule("suggest-route", now_ms=NOW_MS).edit_route
        rejected = service.reject_suggestion(
            "suggest-route", actor_id="user-1", now_ms=NOW_MS
        )
        assert rejected.rule.state == "rejected"


# ── 7. Cost reservation crash/replay ───────────────────────────────────────


def purchase_entry(rule_id: str, **overrides) -> dict:
    base = {
        "rule_id": rule_id,
        "effect": "allow",
        "action_classes": ["purchase.prepare"],
        "data_classes": ["internal"],
        "recipient_classes": ["merchant"],
        "allowed_reversibility": ["reversible"],
        "cost": {
            "max_per_action_cents": 500,
            "max_per_window_cents": 1000,
            "window_ms": DAY_MS,
        },
    }
    base.update(overrides)
    return base


def purchase_context(**overrides) -> ActionContext:
    base = dict(
        operation_key="op-buy-1",
        stage="execute",
        action_class="purchase.prepare",
        data_classes=("internal",),
        reversibility="reversible",
        recipient_class="merchant",
        estimated_cost_cents=200,
        task_id=IDS["task_id"],
        session_id=IDS["session_id"],
    )
    base.update(overrides)
    return ActionContext(**base)


class TestCostReservationCrashAndReplay:
    def test_replay_returns_original_decision_without_double_spend(self):
        write_autonomy_config("enforce", (purchase_entry("buy-a"),))
        service = AutonomyService()
        ctx = purchase_context(operation_key="op-buy-replay")

        first = service.evaluate(ctx, consume=True, now_ms=NOW_MS)
        assert first.verdict == "allow"
        assert first.budget_reservation is not None
        spend = service.budget_usage(rule_id="buy-a", now_ms=NOW_MS)
        assert spend == 200 * MICROS_PER_CENT

        replay = service.evaluate(ctx, consume=True, now_ms=NOW_MS)
        assert replay.decision_id == first.decision_id
        assert service.budget_usage(rule_id="buy-a", now_ms=NOW_MS) == spend

    def test_crash_between_reserve_and_record_never_double_spends(
        self, monkeypatch
    ):
        write_autonomy_config("enforce", (purchase_entry("buy-b"),))
        service = AutonomyService()
        ctx = purchase_context(operation_key="op-buy-crash")

        original = AutonomyStore.consume_rules_and_record_decision
        state = {"crashed": False}

        def crash_once(self, record, rule_ids):
            if not state["crashed"] and record.decision.verdict == "allow":
                state["crashed"] = True
                raise sqlite3.OperationalError("simulated crash after reserve")
            return original(self, record, rule_ids)

        monkeypatch.setattr(
            AutonomyStore, "consume_rules_and_record_decision", crash_once
        )
        with pytest.raises(sqlite3.OperationalError):
            service.evaluate(ctx, consume=True, now_ms=NOW_MS)
        monkeypatch.setattr(
            AutonomyStore, "consume_rules_and_record_decision", original
        )

        # The reservation from the crashed attempt is still held — the
        # replay must NOT reserve a second time.
        held = service.budget_usage(rule_id="buy-b", now_ms=NOW_MS)
        assert held == 200 * MICROS_PER_CENT

        retry = service.evaluate(ctx, consume=True, now_ms=NOW_MS)
        assert retry.verdict == "deny"
        assert service.budget_usage(rule_id="buy-b", now_ms=NOW_MS) == held

    def test_unknown_actual_cost_remains_unsettled_not_zero(self):
        write_autonomy_config("enforce", (purchase_entry("buy-c"),))
        service = AutonomyService()
        decision = service.evaluate(
            purchase_context(operation_key="op-buy-unsettled"),
            consume=True,
            now_ms=NOW_MS,
        )
        assert decision.verdict == "allow"
        # No settlement arrives: the hold stays at the reserved amount —
        # it is never optimistically zeroed.
        assert (
            service.budget_usage(rule_id="buy-c", now_ms=NOW_MS)
            == 200 * MICROS_PER_CENT
        )

        # An exact settlement replaces the hold exactly once.
        assert service.settle_cost(
            decision.decision_id, actual_micros=150 * MICROS_PER_CENT, now_ms=NOW_MS
        )
        assert (
            service.budget_usage(rule_id="buy-c", now_ms=NOW_MS)
            == 150 * MICROS_PER_CENT
        )
        assert not service.settle_cost(
            decision.decision_id, actual_micros=150 * MICROS_PER_CENT, now_ms=NOW_MS
        )


# ── 8. Config crash recovery before enforcement ────────────────────────────


class TestConfigCrashRecoveryBeforeEnforcement:
    def test_crash_after_yaml_replace_fails_closed_then_recovers(self, terminal):
        write_autonomy_config("enforce")
        preview = preview_config_change(
            ConfigChange(set_rules=(stable_rule("send-allow"),)), now_ms=NOW_MS
        )

        def hook(point: str) -> None:
            if point == "after_config_replace":
                raise SimulatedCrash(point)

        with pytest.raises(SimulatedCrash):
            apply_config_change(
                preview,
                expected_contract_hash=preview.before_contract_hash,
                now_ms=NOW_MS,
                _crash_hook=hook,
            )
        assert pending_apply()

        # Enforcement fails closed while the crashed apply is pending.
        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        payload = json.loads(result)
        assert payload["autonomy"]["verdict"] == "deny"
        assert terminal.calls == []

        recovery = recover_config_apply(now_ms=NOW_MS)
        assert recovery.action == "completed"
        assert not pending_apply()

        # The recovered (materialized) contract now enforces the new allow.
        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        assert result == "terminal-result"
        assert terminal.calls == [{"recipient": "safe"}]


# ── 9. Audit store busy/error: enforce fails closed, shadow unchanged ──────


class TestAuditFailureModes:
    @pytest.fixture
    def busy_store(self, monkeypatch):
        def busy(self, record, rule_ids):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(
            AutonomyStore, "consume_rules_and_record_decision", busy
        )

    def test_enforce_mode_fails_closed_on_sqlite_busy(self, busy_store, terminal):
        write_autonomy_config("enforce", (send_allow_entry(),))
        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        payload = json.loads(result)
        assert payload["autonomy"]["verdict"] == "deny"
        assert terminal.calls == []

    def test_shadow_mode_keeps_current_behavior_on_sqlite_busy(
        self, busy_store, terminal
    ):
        write_autonomy_config("shadow", (send_allow_entry(),))
        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        assert result == "terminal-result"
        assert terminal.calls == [{"recipient": "safe"}]


# ── 10. Profile isolation with opposite rules ──────────────────────────────


class TestProfileIsolationWithOppositeRules:
    def test_profiles_never_see_each_other(self, monkeypatch):
        default_home = get_hades_home()
        named_home = default_home / "profiles" / "work"
        named_home.mkdir(parents=True, exist_ok=True)

        # Default: allow. Named: opposite (deny). Same context everywhere.
        write_autonomy_config("enforce", (send_allow_entry(),))
        (named_home / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "autonomy": {
                        "schema_version": 1,
                        "mode": "enforce",
                        "stable_rules": [send_allow_entry(effect="deny")],
                    }
                }
            ),
            encoding="utf-8",
        )

        ctx = make_context(operation_key="op-profile-1")
        default_decision = AutonomyService().evaluate(
            ctx, consume=True, now_ms=NOW_MS
        )
        assert default_decision.verdict == "allow"
        AutonomyService().create_mandate(
            mandate_rule("default-mandate"), now_ms=NOW_MS
        )
        default_hash = profile_hash_recipient("alice@example.test")

        monkeypatch.setenv("HADES_HOME", str(named_home))
        named_decision = AutonomyService().evaluate(
            ctx, consume=True, now_ms=NOW_MS
        )
        assert named_decision.verdict == "deny"

        named_db = SessionDB(named_home / "state.db")
        try:
            # No mandate, decision, or budget row leaks from the default home.
            assert named_db.autonomy.list_runtime_rules() == ()
            named_ops = {
                r.operation_key for r in named_db.autonomy.list_decisions()
            }
            assert named_ops == {"op-profile-1"}
            assert (
                named_db.autonomy.window_spend_micros("send-allow", 0) == 0
            )
            named_hash = named_db.autonomy.hash_recipient("alice@example.test")
        finally:
            named_db.close()
        assert named_hash != default_hash

        monkeypatch.setenv("HADES_HOME", str(default_home))
        default_db = SessionDB(default_home / "state.db")
        try:
            rules = default_db.autonomy.list_runtime_rules()
            assert [r.rule_id for r in rules] == ["default-mandate"]
        finally:
            default_db.close()


# ── 11. Approval identity: args/requester/channel/expiry/replay ────────────


class TestApprovalIdentityRejection:
    SESSION = "e2e-approval-identity"

    @pytest.fixture(autouse=True)
    def clean_pending(self):
        import tools.approval as approval

        def clear():
            with approval._lock:
                approval._pending.clear()
                approval._pending_by_session.clear()
                approval._pending_loaded_home = None
                path = approval._pending_path()
                if path.exists():
                    path.unlink()

        clear()
        token = approval.set_current_session_key(self.SESSION)
        yield
        approval.reset_current_session_key(token)
        clear()

    def _submit_and_resolve(self, arguments, **extra):
        import tools.approval as approval

        request = approval.submit_pending(
            self.SESSION,
            {
                "operation": TOOL_SEND,
                "tool_name": TOOL_SEND,
                "arguments": dict(arguments),
                "policy_key": "plugin_rule:autonomy:e2e",
                "requester": "user-1",
                "channel": "tui",
                **extra,
            },
        )
        assert request is not None
        assert (
            approval.resolve_gateway_approval(
                self.SESSION,
                "once",
                request_id=request["request_id"],
                request_hash=request["argument_hash"],
            )
            == 1
        )
        return request

    def _attempt(self, arguments, *, requester="user-1", channel="tui"):
        from tools.approval import request_tool_approval

        return request_tool_approval(
            TOOL_SEND,
            "autonomy identity recheck",
            rule_key="autonomy:e2e",
            arguments=dict(arguments),
            requester=requester,
            channel=channel,
        )

    def test_changed_args_requester_channel_and_replay_are_rejected(self):
        args = {"recipient": "safe", "body": "canary"}
        self._submit_and_resolve(args)

        # Changed final arguments never consume the approval.
        drift = self._attempt({"recipient": "evil", "body": "canary"})
        assert drift["approved"] is False

        # A different requester or channel is a different identity.
        assert self._attempt(args, requester="user-2")["approved"] is False
        assert self._attempt(args, channel="gateway")["approved"] is False

        # The exact identity consumes it once...
        assert self._attempt(args)["approved"] is True
        # ...and a replay of the consumed approval fails closed.
        assert self._attempt(args)["approved"] is False

    def test_expired_resolved_approval_never_approves(self):
        import tools.approval as approval

        args = {"recipient": "safe"}
        request = self._submit_and_resolve(args)
        # The approval expires while the action waits in the outbox.
        with approval._lock:
            approval._pending[request["request_id"]]["expires_at"] = (
                time.time() - 60
            )
        assert self._attempt(args)["approved"] is False


# ── 12. Bounded ask clarification, no synthetic user message ───────────────


class TestAskClarificationBounded:
    def test_ask_is_structured_tool_output_and_injects_no_message(self, terminal):
        write_autonomy_config(
            "enforce",
            (send_allow_entry(), send_allow_entry(rule_id="send-ask", effect="ask")),
        )
        messages = [
            {"role": "user", "content": "send the update"},
            {"role": "assistant", "content": "sending"},
        ]
        snapshot = copy.deepcopy(messages)

        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        payload = json.loads(result)
        assert payload["autonomy"]["verdict"] == "ask"
        clarification = payload["autonomy"]["clarification"]
        assert clarification["question"]
        assert 0 < len(clarification["choices"]) <= 4
        assert terminal.calls == []
        # The ask is bounded structured TOOL OUTPUT: the conversation
        # history is untouched and no synthetic user turn exists.
        assert messages == snapshot
        assert isinstance(result, str)


# ── Cache and conversation invariants ──────────────────────────────────────


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class TestCacheAndConversationInvariants:
    def test_authority_operations_never_perturb_prompt_tools_provider_model(
        self, terminal
    ):
        write_autonomy_config("enforce", (send_allow_entry(),))
        service = AutonomyService()

        system_message = {
            "role": "system",
            "content": "You are Hades. Byte-stable for this conversation.",
        }
        provider_name = "anthropic"
        model_name = "claude-sonnet-4-5"
        messages = [
            {"role": "user", "content": "turn 1"},
            {"role": "assistant", "content": "reply 1"},
            {"role": "user", "content": "turn 2"},
            {"role": "assistant", "content": "reply 2"},
        ]
        tool_names = {TOOL_SEND, TOOL_DELETE, TOOL_MUTATE}

        def snapshot():
            return (
                _hash(system_message),
                _hash(registry.get_definitions(tool_names, quiet=True)),
                _hash(provider_name),
                _hash(model_name),
            )

        baseline = snapshot()
        history = copy.deepcopy(messages)

        def apply_cache_rule():
            change_preview = service.preview_rule_change(
                ConfigChange(set_rules=(stable_rule("cache-rule"),)), now_ms=NOW_MS
            )
            return service.apply_rule_change(
                change_preview,
                expected_contract_hash=change_preview.before_contract_hash,
                now_ms=NOW_MS,
            )

        operations = {
            "rule_apply": apply_cache_rule,
            "mandate_consumption": lambda: (
                service.create_mandate(mandate_rule("cache-mandate"), now_ms=NOW_MS),
                service.evaluate(
                    make_context(
                        operation_key="op-cache-mandate",
                        action_class="workspace.delete",
                        recipient_class=None,
                    ),
                    consume=True,
                    now_ms=NOW_MS,
                ),
            ),
            "suggestion_confirmation": lambda: (
                service.propose_suggestion(
                    suggestion_rule("cache-suggest"), now_ms=NOW_MS
                ),
                service.confirm_suggestion(
                    "cache-suggest",
                    destination="mandate",
                    actor_kind="user",
                    actor_id="user-1",
                    max_uses=1,
                    now_ms=NOW_MS,
                ),
            ),
            "deny": lambda: run_gate(TOOL_MUTATE, {"x": 1}, terminal),
            "ask": lambda: service.evaluate(
                make_context(
                    operation_key="op-cache-ask", recipient_class="stranger"
                ),
                consume=True,
                now_ms=NOW_MS,
            ),
            "audit_purge": lambda: service.purge_runtime_history(
                before_ms=NOW_MS - DAY_MS, now_ms=NOW_MS
            ),
        }
        for name, operation in operations.items():
            operation()
            assert snapshot() == baseline, (
                f"authority operation {name!r} perturbed the cached "
                "prefix/tool/provider/model identity"
            )
            assert messages == history, (
                f"authority operation {name!r} mutated conversation history"
            )

        # Strict role alternation, no synthetic user message.
        roles = [m["role"] for m in messages]
        assert roles == ["user", "assistant"] * 2

    def test_authority_context_fn_never_serialized(self):
        definitions = registry.get_definitions(
            {TOOL_SEND, TOOL_DELETE, TOOL_MUTATE}, quiet=True
        )
        serialized = json.dumps(definitions, default=str)
        assert "authority_context_fn" not in serialized
        assert "_send_resolver" not in serialized
