"""Task 8 native ``autonomy.exec`` JSON-RPC tests for ``tui_gateway.server``.

Real-path invariants against a temporary ``HADES_HOME``:

- ``autonomy.exec`` is a bounded live-process RPC over the shared
  ``hades_cli.autonomy.run_argv`` surface (no shell, no subprocess);
- the response is structured (`ok`, `action`, `output`, `contract`,
  `rules`, `suggestions`, `decision`, `audit`, `preview`,
  ``approval_pending``, `profile_home`) and profile-local;
- argv is validated (list[str], at most 64 entries, at most 64 KiB);
- validation/conflict failures map to JSON-RPC 4xxx and
  storage/recovery failures to 5xxx, never leaking tracebacks or
  exception internals;
- a deny decision is a structured result (``ok`` false, verdict/code,
  edit routes), not an RPC error;
- a session carrying ``profile_home`` evaluates against THAT profile,
  not the launch profile.
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

RULE_ALLOW_SEND = {
    "rule_id": "allow-send",
    "effect": "allow",
    "action_classes": ["message.send"],
    "data_classes": ["public"],
    "recipient_classes": ["designated_test"],
    "description": "allow public sends to the designated test recipient",
}

ACTION_SEND = {
    "action_class": "message.send",
    "data_classes": ["public"],
    "reversibility": "reversible",
    "recipient_class": "designated_test",
}

ACTION_CREDENTIAL = {
    "action_class": "message.send",
    "data_classes": ["credential"],
    "reversibility": "irreversible",
}


def _write_autonomy_config(home: Path, stable_rules: list[dict]) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "autonomy": {
                    "schema_version": 1,
                    "mode": "enforce",
                    "stable_rules": stable_rules,
                }
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def profile_home(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HADES_HOME", str(home))
    _write_autonomy_config(home, [RULE_ALLOW_SEND])
    yield home


@pytest.fixture()
def server(profile_home):
    with patch.dict(
        "sys.modules",
        {
            "hades_cli.env_loader": MagicMock(),
            "hades_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        # See test_goal_command.py: never importlib.reload here — clear the
        # per-session dicts instead so atexit hooks stay single-registered.
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()


@pytest.fixture()
def rpc_raw(server):
    def call(method: str, params: dict) -> dict:
        return server._methods[method](1, params)

    return call


@pytest.fixture()
def rpc(rpc_raw):
    def call(method: str, params: dict) -> dict:
        resp = rpc_raw(method, params)
        assert "error" not in resp, resp
        return resp["result"]

    return call


# ── Structured, profile-local success paths ─────────────────────────────────


def test_autonomy_exec_is_profile_local_and_structured(rpc, profile_home):
    result = rpc("autonomy.exec", {"session_id": "sid-1", "argv": ["list", "--effective"]})
    assert result["ok"] is True
    assert result["profile_home"] == str(profile_home)
    assert {"contract", "rules", "suggestions", "output"} <= set(result)
    assert result["contract"]["hash"]
    assert [r["rule_id"] for r in result["rules"]] == ["allow-send"]


def test_status_reports_contract_identity(rpc):
    result = rpc("autonomy.exec", {"argv": ["status"]})
    assert result["ok"] is True
    assert result["action"] == "status"
    assert result["contract"]["version"] >= 1
    assert result["approval_pending"] is False
    assert "contract" in result["output"]


def test_allow_evaluation_is_structured(rpc, profile_home, tmp_path):
    action = tmp_path / "action-send.yaml"
    action.write_text(yaml.safe_dump(ACTION_SEND), encoding="utf-8")
    result = rpc("autonomy.exec", {"argv": ["evaluate", "--file", str(action)]})
    assert result["ok"] is True
    assert result["decision"]["verdict"] == "allow"
    assert result["decision"]["code"] == "explicit_allow"
    assert result["decision"]["matched_rule_ids"] == ["allow-send"]


def test_deny_evaluation_is_structured_result_not_rpc_error(rpc, tmp_path):
    action = tmp_path / "action-cred.yaml"
    action.write_text(yaml.safe_dump(ACTION_CREDENTIAL), encoding="utf-8")
    result = rpc("autonomy.exec", {"argv": ["evaluate", "--file", str(action)]})
    assert result["ok"] is False
    assert result["exit_code"] == 3
    assert result["decision"]["verdict"] == "deny"
    assert result["decision"]["edit_targets"], "deny must name its edit routes"


def test_mutation_previews_with_exact_hash_and_pending_flag(rpc, tmp_path):
    rule = tmp_path / "rule.yaml"
    rule.write_text(
        yaml.safe_dump({**RULE_ALLOW_SEND, "rule_id": "allow-send-2"}),
        encoding="utf-8",
    )
    result = rpc("autonomy.exec", {"argv": ["rule", "add", "--file", str(rule)]})
    assert result["ok"] is True
    assert result["preview"]["applied"] is False
    assert result["preview"]["before_contract_hash"]
    assert result["preview"]["after_contract_hash"]
    assert result["approval_pending"] is True
    # Nothing was written: the stable layer still has no allow-send-2.
    listed = rpc("autonomy.exec", {"argv": ["list", "--source", "user_assertion"]})
    assert "allow-send-2" not in [r["rule_id"] for r in listed["rules"]]


def test_suggestions_are_labeled_not_authorization(rpc):
    result = rpc("autonomy.exec", {"argv": ["suggestion", "list"]})
    assert result["ok"] is True
    assert result["suggestions"] == []
    assert "never authorize" in result["output"]


def test_session_profile_home_override_is_honored(rpc, server, tmp_path):
    other_home = tmp_path / "other-profile"
    other_home.mkdir()
    _write_autonomy_config(other_home, [])
    server._sessions["sid-other"] = {
        "session_key": "tui-autonomy-other",
        "profile_home": str(other_home),
    }
    result = rpc("autonomy.exec", {"session_id": "sid-other", "argv": ["list", "--effective"]})
    assert result["profile_home"] == str(other_home)
    assert result["rules"] == []


# ── Bounded argv validation (4xxx) ──────────────────────────────────────────


def test_argv_must_be_a_list_of_strings(rpc_raw):
    for bad in ("status", None, [1, 2], [["status"]], {"a": 1}):
        resp = rpc_raw("autonomy.exec", {"argv": bad})
        assert resp["error"]["code"] == 4033, resp


def test_argv_entry_count_is_bounded_to_64(rpc_raw):
    resp = rpc_raw("autonomy.exec", {"argv": ["list"] + ["--json"] * 64})
    assert resp["error"]["code"] == 4033


def test_argv_total_bytes_are_bounded_to_64kib(rpc_raw):
    resp = rpc_raw("autonomy.exec", {"argv": ["evaluate", "--file", "x" * 65_536]})
    assert resp["error"]["code"] == 4033


# ── Error mapping and redaction ─────────────────────────────────────────────


def test_validation_errors_map_to_4xxx(rpc_raw):
    resp = rpc_raw("autonomy.exec", {"argv": ["rule", "remove", "no-such-rule"]})
    assert resp["error"]["code"] == 4034
    assert "Traceback" not in resp["error"]["message"]


def test_stale_apply_hash_is_a_4xxx_conflict(rpc_raw, tmp_path):
    rule = tmp_path / "rule.yaml"
    rule.write_text(
        yaml.safe_dump({**RULE_ALLOW_SEND, "rule_id": "allow-send-3"}),
        encoding="utf-8",
    )
    resp = rpc_raw(
        "autonomy.exec",
        {
            "argv": [
                "rule", "add", "--file", str(rule),
                "--apply", "--expected-contract-hash", "wrong",
            ]
        },
    )
    assert resp["error"]["code"] == 4034


def test_unexpected_failures_map_to_5xxx_and_are_redacted(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    def boom(*_a, **_k):
        raise RuntimeError("secret-token-xyz leaked path C:/private")

    monkeypatch.setattr(autonomy_mod, "run_argv", boom)
    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})
    assert resp["error"]["code"] == 5038
    assert "secret-token-xyz" not in resp["error"]["message"]
    assert "Traceback" not in resp["error"]["message"]


def test_storage_failures_map_to_5xxx(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    def storage_failure(argv, **kwargs):
        return autonomy_mod.CliResult(
            autonomy_mod.EXIT_STORAGE,
            "error: storage failure",
            {"error": "storage failure", "code": "storage_failure"},
        )

    monkeypatch.setattr(autonomy_mod, "run_argv", storage_failure)
    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})
    assert resp["error"]["code"] == 5039
