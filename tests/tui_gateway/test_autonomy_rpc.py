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
import json
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
    assert "profile_home" not in result
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
    assert "profile_home" not in result
    assert result["rules"] == []


def test_autonomy_rpc_binds_session_workspace_for_native_run_argv(
    rpc, server, tmp_path, monkeypatch
):
    import hades_cli.autonomy as autonomy_mod
    from hades_cli.workspace_context import get_workspace_root

    launch = tmp_path / "gateway-launch"
    workspace = tmp_path / "session-workspace"
    launch.mkdir()
    workspace.mkdir()
    server._sessions["sid-autonomy-workspace"] = {
        "session_key": "tui-autonomy-workspace",
        "cwd": str(workspace),
    }
    captured: list[Path] = []

    def run(argv, **kwargs):
        captured.append(get_workspace_root())
        return autonomy_mod.CliResult(
            autonomy_mod.EXIT_OK,
            "safe",
            {"ok": True, "action": "status"},
        )

    monkeypatch.setattr(autonomy_mod, "run_argv", run)
    monkeypatch.chdir(launch)
    result = rpc("autonomy.exec", {
        "session_id": "sid-autonomy-workspace", "argv": ["status"],
    })

    assert result["ok"] is True
    assert captured == [workspace.resolve()]
    assert Path.cwd() == launch


def test_autonomy_relative_action_file_uses_workspace_context(tmp_path, monkeypatch):
    import hades_cli.autonomy as autonomy_mod
    from hades_cli.workspace_context import workspace_context
    from hades_cli.autonomy import run_argv

    launch = tmp_path / "gateway-launch"
    workspace = tmp_path / "session-workspace"
    launch.mkdir()
    workspace.mkdir()
    (workspace / "action.yaml").write_text(
        yaml.safe_dump(ACTION_SEND), encoding="utf-8"
    )
    monkeypatch.chdir(launch)
    with workspace_context(workspace):
        result = run_argv(["evaluate", "--file", "action.yaml"])
    assert result.exit_code == autonomy_mod.EXIT_OK
    assert result.payload["verdict"] in {"allow", "ask"}


# ── Bounded argv validation (4xxx) ──────────────────────────────────────────


def test_argv_must_be_a_list_of_strings(rpc_raw):
    for bad in ("status", None, [1, 2], [["status"]], {"a": 1}):
        resp = rpc_raw("autonomy.exec", {"argv": bad})
        assert resp["error"]["code"] == 4033, resp


def test_invalid_session_id_type_is_rejected_without_traceback(rpc_raw):
    resp = rpc_raw("autonomy.exec", {"session_id": [], "argv": ["status"]})

    assert resp["error"]["code"] == 4004
    assert "Traceback" not in resp["error"]["message"]
    assert "[]" not in resp["error"]["message"]


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


@pytest.mark.parametrize(
    ("exit_name", "error_code", "expected_message"),
    [
        (
            "EXIT_VALIDATION",
            4034,
            "autonomy.exec: validation failed (details withheld; run hades autonomy in terminal)",
        ),
        (
            "EXIT_STORAGE",
            5039,
            "autonomy.exec: storage failure (details withheld; run hades autonomy doctor in terminal)",
        ),
    ],
)
def test_cli_failure_payload_error_is_not_forwarded(
    rpc_raw, monkeypatch, exit_name, error_code, expected_message
):
    import hades_cli.autonomy as autonomy_mod

    secret = "autonomy-wire-secret-token"
    path = "/private/autonomy/secret.yaml"
    details = f"{secret} {path}\nTraceback (most recent call last)"

    def failed(argv, **kwargs):
        return autonomy_mod.CliResult(
            getattr(autonomy_mod, exit_name),
            f"producer output: {details}",
            {"ok": False, "error": details, "code": "producer_failure"},
        )

    monkeypatch.setattr(autonomy_mod, "run_argv", failed)
    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})
    error = resp["error"]
    message = str(error["message"])

    assert error["code"] == error_code
    assert message == expected_message
    assert len(message) <= 256
    assert secret not in str(resp)
    assert path not in str(resp)
    assert "Traceback" not in str(resp)


def test_exit_ok_false_payload_is_a_bounded_internal_error(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    secret = "autonomy-false-success-secret"
    path = "/private/autonomy/false-success.yaml"
    details = f"{secret} {path}\nTraceback (most recent call last)"
    failed = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        f"producer output: {details}",
        {"ok": False, "error": details},
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: failed)

    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})

    assert resp["error"]["code"] == 5038
    assert "result" not in resp
    assert secret not in str(resp)
    assert path not in str(resp)
    assert "Traceback" not in str(resp)


def test_exit_ok_failure_only_payload_error_is_a_bounded_internal_error(
    rpc_raw, monkeypatch
):
    import hades_cli.autonomy as autonomy_mod

    secret = "autonomy-failure-only-secret"
    failed = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        secret,
        {"error": secret},
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: failed)

    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})

    assert resp["error"]["code"] == 5038
    assert secret not in str(resp)


def test_exit_denied_remains_a_structured_result(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    denied = autonomy_mod.CliResult(
        autonomy_mod.EXIT_DENIED,
        "denied by policy",
        {"ok": False, "verdict": "deny", "code": "explicit_deny"},
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: denied)

    resp = rpc_raw("autonomy.exec", {"argv": ["evaluate"]})

    assert "result" in resp
    assert "error" not in resp
    assert resp["result"]["ok"] is False
    assert resp["result"]["exit_code"] == autonomy_mod.EXIT_DENIED


def test_exit_denied_structured_payload_is_allowlisted(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    secret = "autonomy-denied-wire-secret"
    denied = autonomy_mod.CliResult(
        autonomy_mod.EXIT_DENIED,
        "safe denial output",
        {
            "ok": False,
            "verdict": "deny",
            "code": "explicit_deny",
            "reason": "policy denied",
            "error": secret,
            "arbitrary_extra": "producer-only field",
        },
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: denied)

    resp = rpc_raw("autonomy.exec", {"argv": ["evaluate"]})

    assert "result" in resp
    assert "error" not in resp
    assert resp["result"]["ok"] is False
    assert resp["result"]["decision"]["verdict"] == "deny"
    assert resp["result"]["decision"]["code"] == "explicit_deny"
    wire = json.dumps(resp, ensure_ascii=False)
    assert secret not in wire
    assert "arbitrary_extra" not in wire
    assert '"error"' not in wire


def test_exit_ok_allow_decision_is_allowlisted(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    secret = "autonomy-allow-wire-secret"
    allowed = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        "safe allow output",
        {
            "ok": True,
            "verdict": "allow",
            "code": "explicit_allow",
            "matched_rule_ids": ["allow-send"],
            "error": secret,
            "arbitrary_extra": "producer-only field",
        },
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: allowed)

    resp = rpc_raw("autonomy.exec", {"argv": ["evaluate"]})

    assert "result" in resp
    assert resp["result"]["ok"] is True
    assert resp["result"]["decision"]["verdict"] == "allow"
    assert resp["result"]["decision"]["code"] == "explicit_allow"
    wire = json.dumps(resp, ensure_ascii=False)
    assert secret not in wire
    assert "arbitrary_extra" not in wire
    assert '"error"' not in wire


def test_exit_ok_preview_is_allowlisted(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    secret = "autonomy-preview-wire-secret"
    preview = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        "safe preview output",
        {
            "ok": True,
            "applied": False,
            "before_contract_hash": "before-hash",
            "after_contract_hash": "after-hash",
            "added_rule_ids": ["allow-send-2"],
            "error": secret,
            "arbitrary_extra": "producer-only field",
        },
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: preview)

    resp = rpc_raw("autonomy.exec", {"argv": ["rule", "add"]})

    assert "result" in resp
    assert resp["result"]["ok"] is True
    assert resp["result"]["preview"]["applied"] is False
    assert resp["result"]["preview"]["before_contract_hash"] == "before-hash"
    assert resp["result"]["preview"]["after_contract_hash"] == "after-hash"
    wire = json.dumps(resp, ensure_ascii=False)
    assert secret not in wire
    assert "arbitrary_extra" not in wire
    assert '"error"' not in wire


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("contract_version", {"secret": "nested-contract-version"}),
        ("contract_hash", ["nested-contract-hash"]),
        ("profile_id", {"secret": "nested-profile-id"}),
        ("mode", ["nested-mode"]),
    ],
)
def test_contract_document_rejects_non_primitive_fields(
    rpc_raw, monkeypatch, field, malformed
):
    import hades_cli.autonomy as autonomy_mod

    payload = {
        "ok": True,
        "contract_version": 1,
        "contract_hash": "contract-hash",
        field: malformed,
    }
    result = autonomy_mod.CliResult(autonomy_mod.EXIT_OK, "safe", payload)
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})

    assert resp["result"]["contract"] is None
    wire = json.dumps(resp, ensure_ascii=False)
    assert "nested-" not in wire
    assert "secret" not in wire


def test_contract_document_preserves_exact_primitive_fields(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        "safe",
        {
            "ok": True,
            "contract_version": 7,
            "contract_hash": "contract-hash",
            "profile_id": "profile-id",
            "mode": "enforce",
        },
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})

    assert resp["result"]["contract"] == {
        "version": 7,
        "hash": "contract-hash",
        "profile_id": "profile-id",
        "mode": "enforce",
    }


def test_rule_with_nullable_confidence_is_rejected(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        "safe",
        {
            "ok": True,
            "rules": [
                {
                    "rule_id": "allow-send",
                    "source": "stable",
                    "state": "active",
                    "effect": "allow",
                    "confidence_ppm": None,
                    "description": "secret-bearing malformed row",
                }
            ],
        },
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["list"]})

    assert resp["result"]["rules"] == []
    assert "secret-bearing malformed row" not in json.dumps(resp, ensure_ascii=False)


def test_rule_with_integer_confidence_is_preserved(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    row = {
        "rule_id": "allow-send",
        "source": "stable",
        "state": "active",
        "effect": "allow",
        "confidence_ppm": 900_000,
    }
    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK, "safe", {"ok": True, "rules": [row]}
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["list"]})

    assert resp["result"]["rules"] == [row]


def test_oversized_request_id_uses_bounded_null_id_fallback(
    server, monkeypatch
):
    import hades_cli.autonomy as autonomy_mod

    result = autonomy_mod.CliResult(autonomy_mod.EXIT_OK, "safe", {"ok": True})
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)
    rid = "r" * 1_100_000

    resp = server._methods["autonomy.exec"](rid, {"argv": ["status"]})

    assert resp["error"]["code"] == 5038
    assert resp["id"] is None
    assert len((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")) <= 1_048_576


def test_unknown_exit_code_is_a_bounded_internal_error(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    secret = "autonomy-unknown-exit-secret"
    unknown = autonomy_mod.CliResult(
        97,
        f"output {secret} /private/autonomy/unknown",
        {"ok": True, "output": secret},
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: unknown)

    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})

    assert resp["error"]["code"] == 5038
    assert secret not in str(resp)
    assert "-32000" not in str(resp)


@pytest.mark.parametrize("payload", ["not-a-dict", ["not", "a", "dict"]])
def test_malformed_payload_is_a_fixed_error(rpc_raw, monkeypatch, payload):
    import hades_cli.autonomy as autonomy_mod

    result = autonomy_mod.CliResult(autonomy_mod.EXIT_OK, "safe", payload)
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})

    assert resp["error"]["code"] == 5038
    assert "-32000" not in str(resp)


def test_payload_dict_subclass_get_exception_is_not_on_wire(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    secret = "autonomy-malicious-get-secret"

    class MaliciousPayload(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError(f"{secret} /private/autonomy\nTraceback")

    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK, "safe", MaliciousPayload(ok=True)
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})

    assert resp["error"]["code"] == 5038
    assert secret not in str(resp)
    assert "/private/autonomy" not in str(resp)
    assert "Traceback" not in str(resp)


def test_success_output_is_bounded_with_deterministic_suffix(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    suffix = "... [truncated]"
    output = "autonomy output\n" + ("x" * 20_000)
    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK, output, {"ok": True, "action": "status"}
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})

    assert resp["result"]["output"] == output[: 16_384 - len(suffix)] + suffix
    assert len(resp["result"]["output"]) <= 16_384


def test_success_envelope_is_bounded(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        "short",
        {"ok": True, "rules": ["x" * 1_100_000]},
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["status"]})

    assert resp["error"]["code"] == 5038
    assert len(str(resp)) < 2_000


def _assert_native_wire_has_no_sentinels(resp, *sentinels):
    wire = json.dumps(resp, ensure_ascii=False)
    for sentinel in sentinels:
        assert sentinel not in wire
    assert "/Users/private/" not in wire
    assert "profile_home" not in wire


def test_valid_autonomy_success_payload_redacts_all_free_text_fields(
    rpc_raw, monkeypatch
):
    import hades_cli.autonomy as autonomy_mod

    secret = "sk-test-AutonomyValidSuccessSecret"
    private_path = "/Users/private/autonomy/rule.yaml"
    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        f"success output {secret} {private_path}",
        {
            "ok": True,
            "action": "evaluate",
            "rules": [{
                "rule_id": "allow-safe",
                "source": "stable",
                "state": "active",
                "effect": "allow",
                "description": f"description {secret} {private_path}",
                "provenance": f"provenance {secret} {private_path}",
                "edit_command": f"edit {private_path} --token {secret}",
            }],
            "suggestions": [{
                "rule_id": "suggest-safe",
                "source": "learned_suggestion",
                "state": "awaiting_confirmation",
                "effect": "allow",
                "description": f"suggestion {secret} {private_path}",
                "provenance": f"suggestion provenance {secret} {private_path}",
            }],
            "verdict": "allow",
            "code": "explicit_allow",
            "reason": f"decision reason {secret} {private_path}",
            "edit_targets": [f"target {private_path} {secret}"],
            "clarification": {
                "question": f"question {secret} {private_path}",
                "choices": [f"choice {secret} {private_path}"],
                "code": "needs_input",
            },
            "decisions": [{
                "decision_id": "decision-1",
                "operation_key": "operation-1",
                "verdict": "allow",
                "code": "explicit_allow",
                "reason": f"audit reason {secret} {private_path}",
                "edit_targets": [f"audit target {private_path}"],
                "created_at_ms": 1,
            }],
        },
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["evaluate"]})

    assert "result" in resp and "error" not in resp
    _assert_native_wire_has_no_sentinels(resp, secret, private_path)
    assert resp["result"]["output"] != result.output
    assert resp["result"]["decision"]["reason"] != f"decision reason {secret} {private_path}"
    assert resp["result"]["rules"][0]["description"] != f"description {secret} {private_path}"


def test_valid_autonomy_denial_payload_has_fixed_safe_output(
    rpc_raw, monkeypatch
):
    import hades_cli.autonomy as autonomy_mod

    secret = "sk-test-AutonomyValidDenialSecret"
    private_path = "/Users/private/autonomy/deny.yaml"
    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_DENIED,
        f"denied output {secret} {private_path}",
        {
            "ok": False,
            "verdict": "deny",
            "code": "sensitive_data_boundary",
            "reason": f"denial reason {secret} {private_path}",
            "edit_targets": [f"edit {private_path} {secret}"],
            "clarification": {
                "question": f"question {secret} {private_path}",
                "choices": [f"choice {secret} {private_path}"],
            },
        },
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["evaluate"]})

    assert "result" in resp and resp["result"]["ok"] is False
    _assert_native_wire_has_no_sentinels(resp, secret, private_path)
    assert resp["result"]["output"] != result.output
    assert resp["result"]["decision"]["verdict"] == "deny"
    assert resp["result"]["decision"]["code"] == "sensitive_data_boundary"


@pytest.mark.parametrize(
    "path",
    [
        "/Volumes/Untitled/private.txt",
        "/opt/local/secret",
        "/etc/passwd",
        "/root/.ssh/key",
        "/etc",
        "/root",
    ],
)
def test_autonomy_native_redacts_generic_posix_paths_recursively(
    rpc_raw, monkeypatch, path
):
    import hades_cli.autonomy as autonomy_mod

    probe = f"producer free text {path}"
    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        f"producer output {probe}",
        {
            "ok": True,
            "action": "evaluate",
            "verdict": "allow",
            "code": "explicit_allow",
            "reason": probe,
            "edit_targets": [probe],
            "clarification": {
                "question": probe,
                "choices": [probe],
                "code": "clarify",
            },
        },
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["evaluate"]})

    assert "result" in resp, resp
    assert path not in json.dumps(resp, ensure_ascii=False)


@pytest.mark.parametrize("exit_verdict", ["allow", "ask"])
def test_autonomy_exit_denied_cannot_forward_allow_or_ask_decision(
    rpc_raw, monkeypatch, exit_verdict
):
    import hades_cli.autonomy as autonomy_mod

    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_DENIED,
        "safe producer output",
        {"ok": True, "verdict": exit_verdict, "code": "producer_verdict"},
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["evaluate"]})

    assert "error" in resp and resp["error"]["code"] == 5038, resp
    assert "result" not in resp
    assert exit_verdict not in json.dumps(resp, ensure_ascii=False)


def test_autonomy_exit_ok_cannot_forward_deny_decision(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        "safe producer output",
        {"ok": True, "verdict": "deny", "code": "explicit_deny"},
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["evaluate"]})

    assert "error" in resp and resp["error"]["code"] == 5038, resp
    assert "result" not in resp


def test_autonomy_exit_ok_ask_decision_remains_valid(rpc_raw, monkeypatch):
    import hades_cli.autonomy as autonomy_mod

    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        "safe producer output",
        {"ok": True, "verdict": "ask", "code": "needs_confirmation"},
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["evaluate"]})

    assert resp["result"]["ok"] is True
    assert resp["result"]["decision"]["verdict"] == "ask"


@pytest.mark.parametrize(
    "probe",
    [
        "artifact://secret/path",
        "file:///etc/passwd",
        "//etc/passwd",
        "prefix:/etc/passwd",
        "artifact:secret/path",
        "mailto:foo@example.com",
        "urn:secret:item",
        "https://user:pass@example/x?token=secret",
    ],
)
def test_autonomy_native_scrubs_locator_tokens_from_all_free_text_egress(
    rpc_raw, monkeypatch, probe
):
    import hades_cli.autonomy as autonomy_mod

    text = f"producer free text {probe}"
    result = autonomy_mod.CliResult(
        autonomy_mod.EXIT_OK,
        f"producer output {text}",
        {
            "ok": True,
            "action": "evaluate",
            "verdict": "allow",
            "code": "explicit_allow",
            "rules": [{
                "rule_id": "safe-rule",
                "source": "stable",
                "state": "active",
                "effect": "allow",
                "description": text,
                "edit_command": text,
                "provenance": text,
            }],
            "reason": text,
            "edit_targets": [text],
            "clarification": {
                "question": text,
                "choices": [text],
                "code": "clarify",
            },
        },
    )
    monkeypatch.setattr(autonomy_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc_raw("autonomy.exec", {"argv": ["evaluate"]})

    assert "result" in resp and "error" not in resp, resp
    wire = json.dumps(resp, ensure_ascii=False)
    assert probe not in wire
    for fragment in ("//secret", "secret/path", "secret/item", "/etc", "/root"):
        assert fragment not in wire
