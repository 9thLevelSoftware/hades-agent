"""Task 6 execution-gate tests for ``agent.autonomy.runtime``.

Real-path invariants against the temporary ``HADES_HOME`` set by the
autouse conftest fixture:

- the tool-execution middleware gates the FINAL post-plugin arguments;
  a plugin rewrite is the authorized identity, never the original args;
- mode ``off`` performs no autonomy DB write; ``shadow`` records the
  candidate verdict but preserves execution; ``enforce`` blocks before
  the handler;
- a call with no middleware plugins still passes through the gate, and a
  plugin short-circuit (no ``next_call``) creates no autonomy decision;
- ``next_call()`` stays single-use and exceptions preserve the original
  result;
- audit failure blocks mutating enforce-mode calls (fail closed);
- read-only calls bypass the gate and never gain mutation authority;
- ``ask`` returns a bounded structured clarification as tool output
  without injecting a message;
- an interactive once-approval creates an exact one-use temporary
  mandate and re-evaluates under the new contract; denial records deny;
- an ``allow`` installs an exact one-use :class:`AuthorityGrant` that
  satisfies only the matching recoverable approval prompt.
"""

from __future__ import annotations

import json
import types

import pytest
import yaml

from hades_cli.middleware import run_tool_execution_middleware
from hades_constants import get_hades_home
from hades_state import SessionDB
from tools.registry import registry

TOOL_SEND = "autonomy_rt_send"
TOOL_MUTATE = "autonomy_rt_mutate"
TOOL_READ = "autonomy_rt_read"

IDS = {"task_id": "task-1", "session_id": "sess-1", "tool_call_id": "call-1"}


# ── Helpers ─────────────────────────────────────────────────────────────────


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


def send_allow_rule(**overrides) -> dict:
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


def send_ask_rule(**overrides) -> dict:
    base = {
        "rule_id": "send-ask",
        "effect": "ask",
        "action_classes": ["message.send"],
        "data_classes": ["internal"],
        "recipient_classes": ["colleague"],
        "allowed_reversibility": ["reversible"],
    }
    base.update(overrides)
    return base


def list_decisions():
    db = SessionDB(get_hades_home() / "state.db")
    try:
        return db.autonomy.list_decisions(limit=200)
    finally:
        db.close()


def list_runtime_rules(**kwargs):
    db = SessionDB(get_hades_home() / "state.db")
    try:
        return db.autonomy.list_runtime_rules(**kwargs)
    finally:
        db.close()


def _send_resolver(args: dict) -> dict:
    ctx = {
        "action_class": "message.send",
        "data_classes": ("internal",),
        "reversibility": "reversible",
    }
    if args.get("recipient") == "safe":
        ctx["recipient_class"] = "colleague"
    return ctx


def _schema(name: str) -> dict:
    return {
        "name": name,
        "description": f"test tool {name}",
        "parameters": {"type": "object", "properties": {}},
    }


@pytest.fixture(autouse=True)
def no_plugin_middleware(monkeypatch):
    """Empty plugin-middleware set by default; tests install their own."""
    manager = types.SimpleNamespace(_middleware={})
    monkeypatch.setattr("hades_cli.plugins.get_plugin_manager", lambda: manager)
    return manager


@pytest.fixture(autouse=True)
def test_tools():
    registry.register(
        name=TOOL_SEND,
        toolset="autonomy-rt-test",
        schema=_schema(TOOL_SEND),
        handler=lambda args, **kw: json.dumps({"ok": True}),
        authority_context_fn=_send_resolver,
    )
    registry.register(
        name=TOOL_MUTATE,
        toolset="autonomy-rt-test",
        schema=_schema(TOOL_MUTATE),
        handler=lambda args, **kw: json.dumps({"ok": True}),
    )
    registry.register(
        name=TOOL_READ,
        toolset="autonomy-rt-test",
        schema=_schema(TOOL_READ),
        handler=lambda args, **kw: json.dumps({"ok": True}),
        read_only=True,
    )
    yield
    for name in (TOOL_SEND, TOOL_MUTATE, TOOL_READ):
        registry.deregister(name)


@pytest.fixture
def terminal():
    """Counting terminal callback standing in for the true tool handler."""

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


# ── Mode semantics ──────────────────────────────────────────────────────────


class TestModes:
    def test_mode_off_executes_and_performs_no_autonomy_db_write(self, terminal):
        write_autonomy_config("off")
        result = run_gate(TOOL_MUTATE, {"x": 1}, terminal)
        assert result == "terminal-result"
        assert terminal.calls == [{"x": 1}]
        assert not (get_hades_home() / "state.db").exists()

    def test_missing_config_defaults_to_off_passthrough(self, terminal):
        args = {"x": 1}
        result = run_gate(TOOL_MUTATE, args, terminal)
        assert result == "terminal-result"
        assert not (get_hades_home() / "state.db").exists()

    def test_shadow_records_candidate_verdict_and_preserves_execution(self, terminal):
        write_autonomy_config("shadow")
        result = run_gate(TOOL_MUTATE, {"x": 1}, terminal)
        assert result == "terminal-result"
        assert terminal.calls == [{"x": 1}]
        records = list_decisions()
        assert len(records) == 1
        assert records[0].stage == "execute"
        # unknown.mutation with unknown data/reversibility → candidate deny
        assert records[0].decision.verdict == "deny"

    def test_enforce_blocks_before_handler_on_deny(self, terminal):
        write_autonomy_config("enforce")
        result = run_gate(TOOL_MUTATE, {"x": 1}, terminal)
        payload = json.loads(result)
        assert payload["autonomy"]["verdict"] == "deny"
        assert terminal.calls == []
        records = list_decisions()
        assert len(records) == 1
        assert records[0].decision.verdict == "deny"

    def test_enforce_allow_executes_with_explicit_rule(self, terminal):
        write_autonomy_config("enforce", (send_allow_rule(),))
        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        assert result == "terminal-result"
        assert terminal.calls == [{"recipient": "safe"}]
        records = list_decisions()
        assert any(
            r.decision.verdict == "allow" and r.decision.code == "explicit_allow"
            for r in records
        )


# ── Final-argument identity ────────────────────────────────────────────────


class TestFinalArgumentIdentity:
    def test_plugin_modified_args_are_the_authorized_identity(
        self, monkeypatch, terminal
    ):
        write_autonomy_config("enforce", (send_allow_rule(),))

        def rewriting(**kwargs):
            return kwargs["next_call"]({**kwargs["args"], "recipient": "external"})

        manager = types.SimpleNamespace(_middleware={"tool_execution": [rewriting]})
        monkeypatch.setattr("hades_cli.plugins.get_plugin_manager", lambda: manager)

        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        payload = json.loads(result)
        assert payload["autonomy"]["verdict"] == "deny"
        assert payload["autonomy"]["code"] == "unknown_recipient"
        assert terminal.calls == []

        final_key = registry.operation_key(
            TOOL_SEND,
            {"recipient": "external"},
            task_id=IDS["task_id"],
            tool_call_id=IDS["tool_call_id"],
        )
        records = list_decisions()
        assert len(records) == 1
        assert records[0].operation_key == final_key

    def test_no_middleware_still_invokes_gate(self, terminal):
        write_autonomy_config("enforce")
        result = run_gate(TOOL_MUTATE, {"x": 1}, terminal)
        assert json.loads(result)["autonomy"]["verdict"] == "deny"
        assert terminal.calls == []

    def test_plugin_short_circuit_creates_no_autonomy_decision(
        self, monkeypatch, terminal
    ):
        write_autonomy_config("enforce")

        def short_circuit(**kwargs):
            return "short-circuited"

        manager = types.SimpleNamespace(
            _middleware={"tool_execution": [short_circuit]}
        )
        monkeypatch.setattr("hades_cli.plugins.get_plugin_manager", lambda: manager)

        result = run_gate(TOOL_MUTATE, {"x": 1}, terminal)
        assert result == "short-circuited"
        assert terminal.calls == []
        assert not (get_hades_home() / "state.db").exists()

    def test_next_call_remains_single_use(self, monkeypatch, terminal):
        write_autonomy_config("enforce", (send_allow_rule(),))

        def double_next(**kwargs):
            first = kwargs["next_call"](kwargs["args"])
            kwargs["next_call"](kwargs["args"])
            return first

        manager = types.SimpleNamespace(_middleware={"tool_execution": [double_next]})
        monkeypatch.setattr("hades_cli.plugins.get_plugin_manager", lambda: manager)

        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        assert result == "terminal-result"
        assert terminal.calls == [{"recipient": "safe"}]
        allow_records = [
            r for r in list_decisions() if r.decision.verdict == "allow"
        ]
        assert len(allow_records) == 1

    def test_exceptions_preserve_original_result_and_clear_grant(self, terminal):
        from agent.autonomy.runtime import consume_exact_authority_grant

        write_autonomy_config("enforce", (send_allow_rule(),))

        def boom(args):
            raise RuntimeError("tool failed")

        with pytest.raises(RuntimeError, match="tool failed"):
            run_gate(TOOL_SEND, {"recipient": "safe"}, boom)

        assert (
            consume_exact_authority_grant(
                tool_name=TOOL_SEND, arguments={"recipient": "safe"}
            )
            is None
        )


# ── Fail-closed and read-only behaviour ────────────────────────────────────


class TestFailClosed:
    def test_audit_failure_blocks_mutating_enforce_calls(self, monkeypatch, terminal):
        write_autonomy_config("enforce", (send_allow_rule(),))

        class BoomProvider:
            def authorize(self, context, *, consume):
                raise RuntimeError("state.db is corrupt")

            def current_contract(self):  # pragma: no cover - interface parity
                raise RuntimeError("state.db is corrupt")

        import agent.autonomy.runtime as runtime_module

        monkeypatch.setattr(
            runtime_module, "StoredAuthorityProvider", BoomProvider
        )
        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        payload = json.loads(result)
        assert payload["autonomy"]["verdict"] == "deny"
        assert terminal.calls == []

    def test_invalid_stable_authority_blocks_mutating_calls(self, terminal):
        home = get_hades_home()
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(
            yaml.safe_dump({"autonomy": {"mode": "yolo"}}), encoding="utf-8"
        )
        result = run_gate(TOOL_MUTATE, {"x": 1}, terminal)
        payload = json.loads(result)
        assert payload["autonomy"]["verdict"] == "deny"
        assert terminal.calls == []

    def test_sqlite_busy_audit_error_fails_closed_in_enforce_mode(
        self, monkeypatch, terminal
    ):
        """Task 10: a busy/locked audit DB never becomes an allow."""
        import sqlite3

        from agent.autonomy.store import AutonomyStore

        write_autonomy_config("enforce", (send_allow_rule(),))

        def busy(self, record, rule_ids):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(
            AutonomyStore, "consume_rules_and_record_decision", busy
        )
        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        payload = json.loads(result)
        assert payload["autonomy"]["verdict"] == "deny"
        assert terminal.calls == []

    def test_sqlite_busy_in_shadow_mode_preserves_current_behavior(
        self, monkeypatch, terminal
    ):
        """Task 10: shadow mode never changes execution on audit failure."""
        import sqlite3

        from agent.autonomy.store import AutonomyStore

        write_autonomy_config("shadow", (send_allow_rule(),))

        def busy(self, record, rule_ids):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(
            AutonomyStore, "consume_rules_and_record_decision", busy
        )
        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        assert result == "terminal-result"
        assert terminal.calls == [{"recipient": "safe"}]

    def test_read_only_calls_bypass_gate_and_gain_no_authority(self, terminal):
        from agent.autonomy.runtime import consume_exact_authority_grant

        write_autonomy_config("enforce")
        result = run_gate(TOOL_READ, {"q": "x"}, terminal)
        assert result == "terminal-result"
        assert terminal.calls == [{"q": "x"}]
        assert not (get_hades_home() / "state.db").exists()
        assert (
            consume_exact_authority_grant(tool_name=TOOL_READ, arguments={"q": "x"})
            is None
        )


# ── Ask: structured clarification, approval, mandates ──────────────────────


class TestAskFlow:
    def test_ask_returns_bounded_clarification_without_injecting_message(
        self, terminal
    ):
        write_autonomy_config("enforce", (send_allow_rule(), send_ask_rule()))
        result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        payload = json.loads(result)
        assert payload["autonomy"]["verdict"] == "ask"
        assert payload["autonomy"]["code"] == "conflicting_ask"
        clarification = payload["autonomy"]["clarification"]
        assert clarification["question"]
        assert 0 < len(clarification["choices"]) <= 4
        assert terminal.calls == []

    def test_once_approval_creates_one_use_mandate_and_reevaluates(
        self, monkeypatch, terminal
    ):
        from tools.terminal_tool import set_approval_callback

        write_autonomy_config("enforce")
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        prompts = []

        def approve_once(command, description, **kwargs):
            prompts.append((command, kwargs))
            return "once"

        set_approval_callback(approve_once)
        try:
            result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        finally:
            set_approval_callback(None)

        assert result == "terminal-result"
        assert terminal.calls == [{"recipient": "safe"}]
        assert len(prompts) == 1
        # the [a]lways option is hidden for autonomy asks
        assert prompts[0][1].get("allow_permanent") is False

        mandates = list_runtime_rules(source="temporary_mandate")
        assert len(mandates) == 1
        assert mandates[0].rule.max_uses == 1
        assert mandates[0].rule.state == "consumed"

        records = list_decisions()
        assert any(
            r.decision.verdict == "allow"
            and r.decision.code == "temporary_mandate"
            for r in records
        )
        assert any(r.decision.verdict == "ask" for r in records)

    def test_user_denial_records_deny_and_blocks(self, monkeypatch, terminal):
        from tools.terminal_tool import set_approval_callback

        write_autonomy_config("enforce")
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")
        set_approval_callback(lambda command, description, **kwargs: "deny")
        try:
            result = run_gate(TOOL_SEND, {"recipient": "safe"}, terminal)
        finally:
            set_approval_callback(None)

        payload = json.loads(result)
        assert payload["autonomy"]["verdict"] == "deny"
        assert terminal.calls == []
        records = list_decisions()
        assert any(
            r.decision.verdict == "deny" and r.decision.code == "approval_denied"
            for r in records
        )
        assert not list_runtime_rules(source="temporary_mandate")


# ── AuthorityGrant → generic approval dedupe ───────────────────────────────


class TestAuthorityGrant:
    def test_allow_grant_satisfies_only_matching_recoverable_prompt(self, terminal):
        from tools.approval import request_tool_approval

        write_autonomy_config("enforce", (send_allow_rule(),))
        outcomes = {}

        def handler(args):
            outcomes["match"] = request_tool_approval(
                TOOL_SEND,
                "plugin flagged this send",
                arguments=dict(args),
            )
            outcomes["mismatch"] = request_tool_approval(
                TOOL_SEND,
                "plugin flagged a different send",
                arguments={"recipient": "other"},
            )
            return "terminal-result"

        result = run_gate(TOOL_SEND, {"recipient": "safe"}, handler)
        assert result == "terminal-result"
        # exact match consumed the grant with no prompt (non-interactive
        # context would otherwise fail closed)
        assert outcomes["match"]["approved"] is True
        # a mismatched call is never satisfied by the grant; with no human
        # present the plugin escalation fails closed
        assert outcomes["mismatch"]["approved"] is False

    def test_grant_is_single_use(self, terminal):
        from tools.approval import request_tool_approval

        write_autonomy_config("enforce", (send_allow_rule(),))
        outcomes = []

        def handler(args):
            for _ in range(2):
                outcomes.append(
                    request_tool_approval(
                        TOOL_SEND,
                        "repeat escalation",
                        arguments=dict(args),
                    )["approved"]
                )
            return "terminal-result"

        run_gate(TOOL_SEND, {"recipient": "safe"}, handler)
        assert outcomes == [True, False]
