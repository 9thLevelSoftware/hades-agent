"""Native ``transaction.exec`` JSON-RPC tests (plan Task 12).

The RPC runs the shared ``hades_cli.transactions.run_argv`` surface in
the LIVE gateway process — never a slash-worker subprocess — with
bounded argv validation, structured results, and redacted errors.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


@pytest.fixture()
def home() -> Path:
    return Path(os.environ["HADES_HOME"])


@pytest.fixture()
def server():
    with patch.dict(
        "sys.modules",
        {
            "hades_cli.env_loader": MagicMock(),
            "hades_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()


@pytest.fixture()
def rpc(server):
    def call(method: str, params: dict) -> dict:
        resp = server._methods[method](1, params)
        return resp["result"] if "result" in resp else resp

    return call


def _seed_transaction(
    tmp_path, transaction_id="tx-1", *, target_path=None, content="hello\n"
):
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.dump({
        "transaction": {"title": "rpc test"},
        "nodes": [{
            "node_id": "write", "adapter_id": "workspace.v1",
            "action": "write_file",
            "args": {
                "path": str(target_path or "rpc-note.md"),
                "content": content,
            },
        }],
        "edges": [],
    }), encoding="utf-8")
    authority = tmp_path / "authority.yaml"
    authority.write_text(yaml.dump({
        "authority_version": 1, "irreversible_policy": "ask",
    }), encoding="utf-8")
    from hades_cli.transactions import run_argv

    result = run_argv([
        "create", "--plan", str(plan), "--authority", str(authority),
        "--transaction-id", transaction_id,
    ])
    assert result.exit_code == 0, result.output
    return transaction_id


def test_transaction_rpc_uses_live_profile_and_structured_result(
    rpc, home, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _seed_transaction(tmp_path, "tx-1")
    result = rpc("transaction.exec", {
        "session_id": "sid-1",
        "argv": ["show", "tx-1"],
    })
    assert result.get("ok") is True, result
    assert result["action"] == "show"
    assert result["transaction"]["transaction_id"] == "tx-1"
    assert "output" in result and result["output"]
    # Content never rides the wire; hashes/ids do.
    assert "hello" not in str(result)


def test_transaction_rpc_validates_argv_without_echoing_content(rpc):
    for bad in (
        None, [], "show tx-1", [1, 2], ["x"] * 100,
        ["y" * (70 * 1024)],
    ):
        resp = rpc("transaction.exec", {"argv": bad, "session_id": "sid"})
        error = resp.get("error") or {}
        assert error.get("code") == 4006, resp
        # An oversized argument may be a pasted secret: never echoed.
        if (
            isinstance(bad, list) and bad
            and isinstance(bad[0], str) and len(bad[0]) > 32
        ):
            assert bad[0] not in str(error.get("message", ""))


def test_transaction_rpc_rejects_invalid_session_id_type_without_traceback(rpc):
    resp = rpc("transaction.exec", {
        "session_id": [], "argv": ["list"],
    })
    error = resp.get("error") or {}

    assert error.get("code") == 4006
    assert "Traceback" not in str(error.get("message", ""))
    assert "[]" not in str(error.get("message", ""))


def test_transaction_rpc_maps_validation_and_failure_codes(rpc):
    resp = rpc("transaction.exec", {
        "argv": ["explode"], "session_id": "sid",
    })
    error = resp.get("error") or {}
    assert error.get("code") == 4007

    resp = rpc("transaction.exec", {
        "argv": ["show", "tx-missing"], "session_id": "sid",
    })
    error = resp.get("error") or {}
    assert error.get("code") in {4007, 5044}
    assert "Traceback" not in str(error.get("message", ""))


def test_transaction_rpc_redacts_validation_error_details(rpc, tmp_path):
    secret = "transaction-secret-plan-token"
    missing_plan = tmp_path / f"missing-{secret}.yaml"
    missing_authority = tmp_path / f"authority-{secret}.yaml"

    resp = rpc("transaction.exec", {
        "argv": [
            "create", "--plan", str(missing_plan),
            "--authority", str(missing_authority),
        ],
        "session_id": "sid",
    })
    error = resp.get("error") or {}
    message = str(error.get("message", ""))

    assert error.get("code") == 4007, resp
    assert secret not in message
    assert str(missing_plan) not in message
    assert str(missing_authority) not in message
    assert "file not found" not in message
    assert "Traceback" not in message
    assert len(message) <= 256


def test_transaction_rpc_redacts_nonvalidation_command_failure(rpc, monkeypatch):
    import hades_cli.transactions as transactions_mod

    secret = "transaction-secret-command-output"
    details = f"{secret} /private/authority.yaml\nTraceback (most recent call last)"
    failed = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_ERROR,
        f"error: {details}",
        {"ok": False, "error": details, "code": "RuntimeError"},
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: failed)

    resp = rpc("transaction.exec", {
        "argv": ["show", "tx-failed"], "session_id": "sid",
    })
    error = resp.get("error") or {}
    message = str(error.get("message", ""))

    assert error.get("code") == 5044, resp
    assert secret not in message
    assert "/private/authority.yaml" not in message
    assert "Traceback" not in message
    assert len(message) <= 256


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        ("unknown_effect", {"committed_nodes": ["write"]}),
        ("blocked", {"committed_nodes": ["write"], "blocked_node": "publish"}),
        ("partially_compensated", {"compensated_nodes": ["write"]}),
    ],
)
def test_transaction_rpc_preserves_safety_uncertainty_on_nonzero_exit(
    rpc, monkeypatch, status, extra
):
    import hades_cli.transactions as transactions_mod

    secret = "transaction-secret-partial-compensation"
    details = (
        f"{secret} /private/authority.yaml\\n"
        "Traceback (most recent call last)"
    )
    failed = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        f"error: {details}",
        {
            "ok": False,
            "action": "compensate",
            "status": status,
            "error": details,
            **extra,
        },
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: failed)

    resp = rpc("transaction.exec", {
        "argv": ["compensate", "tx-partial"], "session_id": "sid",
    })
    assert resp.get("ok") is False, resp
    assert resp["status"] == status
    for key, value in extra.items():
        assert resp[key] == value
    assert secret not in str(resp)
    assert "/private/authority.yaml" not in str(resp)
    assert "Traceback" not in str(resp)
    assert resp["output"] != failed.output


def test_transaction_rpc_keeps_unrecognized_nonzero_failure_as_fixed_error(
    rpc, monkeypatch
):
    import hades_cli.transactions as transactions_mod

    failed = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_ERROR,
        "unsafe producer output",
        {"ok": False, "action": "commit", "error": "unsafe"},
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: failed)

    resp = rpc("transaction.exec", {"argv": ["commit", "tx"], "session_id": "sid"})

    assert resp["error"]["code"] == 5044
    assert "unsafe" not in str(resp)


def test_transaction_rpc_preserves_recognized_status_on_success_exit(
    rpc, monkeypatch
):
    import hades_cli.transactions as transactions_mod

    secret = "transaction-success-uncertainty-secret"
    failed = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        f"unsafe {secret} /Users/private/transaction",
        {
            "ok": False,
            "action": "commit",
            "status": "unknown_effect",
            "committed_nodes": ["write"],
            "blocked_node": "publish",
            "error": secret,
        },
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: failed)

    resp = rpc("transaction.exec", {"argv": ["commit", "tx"], "session_id": "sid"})

    assert resp["ok"] is False
    assert resp["status"] == "unknown_effect"
    assert resp["committed_nodes"] == ["write"]
    assert resp["blocked_node"] == "publish"
    assert secret not in str(resp)
    assert "/Users/private/transaction" not in str(resp)


def test_transaction_rpc_bounds_success_output_and_omits_failure_error(
    rpc, monkeypatch
):
    import hades_cli.transactions as transactions_mod

    max_output_chars = 16_384
    truncation_suffix = "... [truncated]"
    secret = "transaction-secret-success-payload-error"
    oversized_output = "transaction output\\n" + ("x" * 20_000)
    successful = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        oversized_output,
        {
            "ok": True,
            "action": "show",
            "error": f"failure-only detail: {secret}",
        },
    )
    monkeypatch.setattr(
        transactions_mod, "run_argv", lambda *_a, **_k: successful
    )

    resp = rpc("transaction.exec", {
        "argv": ["show", "tx-success"], "session_id": "sid",
    })

    assert resp.get("ok") is True, resp
    assert "error" not in resp
    assert secret not in str(resp)
    assert len(resp["output"]) <= max_output_chars
    assert resp["output"] == (
        oversized_output[:max_output_chars - len(truncation_suffix)]
        + truncation_suffix
    )


def test_transaction_rpc_runs_mutations_in_live_process(
    rpc, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _seed_transaction(tmp_path, "tx-live")
    result = rpc("transaction.exec", {
        "session_id": "sid-1", "argv": ["preview", "tx-live"],
    })
    assert result.get("ok") is True, result
    assert result["action"] == "preview"
    assert result.get("preview_hash")
    # The durable phase change proves the mutation ran here, not in a
    # worker: the same profile store observes it immediately.
    from agent.effects.store import TransactionStore
    from hades_state import SessionDB

    db = SessionDB(Path(os.environ["HADES_HOME"]) / "state.db")
    try:
        store = TransactionStore(db)
        assert store.get_transaction("tx-live").status == "ready"
    finally:
        db.close()


def test_transaction_rpc_binds_relative_inputs_and_adapters_to_session_workspace(
    rpc, server, tmp_path, monkeypatch
):
    import hades_cli.transactions as transactions_mod
    from hades_cli.workspace_context import get_workspace_root

    launch = tmp_path / "gateway-launch"
    workspace = tmp_path / "session-workspace"
    launch.mkdir()
    workspace.mkdir()
    (workspace / "plan.yaml").write_text(yaml.safe_dump({
        "transaction": {"title": "session-relative"},
        "nodes": [{
            "node_id": "write",
            "adapter_id": "workspace.v1",
            "action": "write_file",
            "args": {"path": "relative.txt", "content": "hello"},
        }],
        "edges": [],
    }), encoding="utf-8")
    (workspace / "authority.yaml").write_text(
        yaml.safe_dump({"authority_version": 1}), encoding="utf-8"
    )
    server._sessions["sid-workspace"] = {
        "session_key": "tui-workspace",
        "cwd": str(workspace),
    }
    monkeypatch.chdir(launch)
    captured: list[Path] = []
    adapters_mod = __import__(
        "agent.effects.adapters", fromlist=["register_builtin_adapters"]
    )
    original_register = adapters_mod.register_builtin_adapters

    def register(registry, *, workspace_root, **kwargs):
        captured.append(get_workspace_root())
        assert Path(workspace_root) == workspace.resolve()
        return original_register(registry, workspace_root=workspace_root, **kwargs)

    monkeypatch.setattr(adapters_mod, "register_builtin_adapters", register)
    result = rpc("transaction.exec", {
        "session_id": "sid-workspace",
        "argv": [
            "create", "--plan", "plan.yaml", "--authority", "authority.yaml",
            "--transaction-id", "tx-relative",
        ],
    })

    assert result.get("ok") is True, result
    assert captured == [workspace.resolve()]
    assert Path.cwd() == launch


def test_transaction_rpc_preserves_safe_scalar_diagnostics(rpc, monkeypatch):
    import hades_cli.transactions as transactions_mod

    safe = "port:8080 error:EADDRINUSE code:42 name:foo sha256:abc123 id:abc"
    result = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        safe,
        {
            "ok": True,
            "action": "show",
            "eligibility": {
                "node-1": {
                    "can_execute": False,
                    "code": "blocked",
                    "fidelity": "exact",
                    "reason": safe,
                    "blockers": [safe],
                    "required_cascade_node_ids": ["node-2"],
                }
            },
        },
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: result)

    response = rpc("transaction.exec", {"argv": ["show", "tx"]})
    wire = json.dumps(response, ensure_ascii=False)
    for fragment in safe.split():
        assert fragment in wire


def test_transaction_rpc_successful_compensation_is_not_uncertain(rpc, monkeypatch):
    import hades_cli.transactions as transactions_mod

    result = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        "compensation compensated; nodes: write",
        {
            "ok": True,
            "action": "compensate",
            "status": "compensated",
            "compensated_nodes": ["write"],
        },
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: result)

    response = rpc("transaction.exec", {"argv": ["compensate", "tx", "write"]})
    assert response["ok"] is True
    assert response["action"] == "compensate"
    assert response["status"] == "compensated"
    assert "reconcile" not in response["output"]


def test_transaction_preview_wire_result_contains_only_safe_node_metadata(
    rpc, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    secret = "transaction-preview-wire-secret-token"
    target_path = tmp_path / "preview-target.txt"
    _seed_transaction(
        tmp_path,
        "tx-preview-wire",
        target_path=target_path,
        content=secret,
    )

    result = rpc("transaction.exec", {
        "session_id": "sid-preview",
        "argv": ["preview", "tx-preview-wire"],
    })

    assert result.get("ok") is True, result
    assert result.get("preview_hash")
    wire = str(result)
    assert secret not in wire
    assert str(tmp_path) not in wire
    assert result["nodes"]
    for node in result["nodes"]:
        assert set(node) == {"node_id", "fidelity", "requires_approval"}
        assert "summary" not in node
        assert "before" not in node
        assert "after" not in node


def test_transaction_preview_node_count_is_bounded(rpc, monkeypatch):
    import hades_cli.transactions as transactions_mod

    rows = [
        {
            "node_id": f"node-{idx}",
            "fidelity": "exact",
            "requires_approval": False,
        }
        for idx in range(10_000)
    ]
    successful = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        "preview output",
        {"ok": True, "action": "preview", "preview_hash": "hash", "nodes": rows},
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: successful)

    resp = rpc("transaction.exec", {"argv": ["preview", "tx-many"], "session_id": "sid"})

    assert resp["action"] == "preview"
    assert len(resp["nodes"]) <= 256
    assert len(resp["output"]) <= 16_384


@pytest.mark.parametrize("payload", ["not-a-dict", ["not", "a", "dict"]])
def test_malformed_payload_is_a_fixed_error(rpc, monkeypatch, payload):
    import hades_cli.transactions as transactions_mod

    result = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK, "safe", payload
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("transaction.exec", {"argv": ["show", "tx"], "session_id": "sid"})

    assert resp["error"]["code"] == 5045
    assert "-32000" not in str(resp)


def test_payload_dict_subclass_get_exception_is_not_on_wire(rpc, monkeypatch):
    import hades_cli.transactions as transactions_mod

    secret = "transaction-malicious-get-secret"

    class MaliciousPayload(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError(f"{secret} /private/transaction\nTraceback")

    result = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK, "safe", MaliciousPayload(ok=True)
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("transaction.exec", {"argv": ["show", "tx"], "session_id": "sid"})

    assert resp["error"]["code"] == 5045
    assert secret not in str(resp)
    assert "/private/transaction" not in str(resp)
    assert "Traceback" not in str(resp)


def test_success_envelope_is_bounded(rpc, monkeypatch):
    import hades_cli.transactions as transactions_mod

    result = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        "short",
        {"ok": True, "action": "show", "transactions": [{"detail": "x" * 1_100_000}]},
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("transaction.exec", {"argv": ["show", "tx"], "session_id": "sid"})

    assert resp["error"]["code"] == 5045
    assert len(str(resp)) < 2_000


def test_valid_transaction_success_payload_forwards_only_minimal_schemas(
    rpc, monkeypatch
):
    import hades_cli.transactions as transactions_mod

    secret = "sk-test-TransactionValidSuccessSecret"
    private_path = "/Users/private/transactions/plan.yaml"
    result = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        f"transaction output {secret} {private_path}",
        {
            "ok": True,
            "action": "show",
            "transaction": {
                "transaction_id": "tx-1",
                "status": "ready",
                "current_revision": 3,
                "receipt_id": "rct-1",
                "title": f"title {secret}",
                "path": private_path,
            },
            "transactions": [{
                "transaction_id": "tx-2",
                "status": "committed",
                "current_revision": 4,
                "receipt_id": None,
                "title": f"title {secret}",
                "path": private_path,
            }],
            "eligibility": {
                "node-1": {
                    "can_execute": False,
                    "code": "blocked",
                    "fidelity": "exact",
                    "reason": f"reason {secret} {private_path}",
                    "blockers": [f"blocker {secret} {private_path}"],
                    "required_cascade_node_ids": ["node-2"],
                    "title": f"title {secret}",
                }
            },
            "rows": [{"title": f"row {secret}", "path": private_path}],
            "nodes": [{"node_id": "node-1", "title": f"node {secret}", "path": private_path}],
            "receipt": {"receipt_id": "rct-1", "status": "verified", "content_hash": "sha256:deadbeef"},
            "observation": {"observation_id": "obs-1", "status": "verified", "content_hash": "sha256:feedface"},
        },
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("transaction.exec", {"argv": ["show", "tx-1"], "session_id": "sid"})

    wire = str(resp)
    assert secret not in wire
    assert private_path not in wire
    assert "title" not in wire
    assert "path" not in wire
    assert "rows" not in resp
    assert "nodes" not in resp
    assert set(resp["transaction"]) == {
        "transaction_id", "status", "current_revision", "receipt_id"
    }
    assert set(resp["transactions"][0]) == {
        "transaction_id", "status", "current_revision", "receipt_id"
    }
    assert set(resp["eligibility"]["node-1"]) == {
        "can_execute", "code", "fidelity", "reason", "blockers",
        "required_cascade_node_ids"
    }


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
def test_transaction_native_redacts_generic_posix_paths_recursively(
    rpc, monkeypatch, path
):
    import hades_cli.transactions as transactions_mod

    probe = f"producer free text {path}"
    result = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        f"producer output {probe}",
        {
            "ok": True,
            "action": "show",
            "eligibility": {
                "node-1": {
                    "can_execute": False,
                    "code": "blocked",
                    "fidelity": "exact",
                    "reason": probe,
                    "blockers": [probe],
                    "required_cascade_node_ids": ["node-2"],
                }
            },
        },
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("transaction.exec", {"argv": ["show", "tx"], "session_id": "sid"})

    assert path not in str(resp)


@pytest.mark.parametrize(
    "status",
    ["unknown_effect", "blocked", "partially_compensated", "failed"],
)
def test_transaction_rpc_uncertainty_wins_over_exit_ok_and_payload_ok(
    rpc, monkeypatch, status
):
    import hades_cli.transactions as transactions_mod

    result = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        "unsafe producer output /etc/passwd",
        {
            "ok": True,
            "action": "commit",
            "status": status,
            "committed_nodes": ["write"],
        },
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("transaction.exec", {"argv": ["commit", "tx"], "session_id": "sid"})

    assert resp["ok"] is False, resp
    assert resp["status"] == status
    assert resp["output"] == "transaction effect uncertain — do not retry; reconcile first"
    assert resp["action"] == "commit"
    assert "/etc/passwd" not in str(resp)


@pytest.mark.parametrize(
    "producer_action",
    [
        "artifact://secret/path",
        "//etc/passwd",
        "prefix:/etc/passwd",
        "mailto:foo",
        "/root",
    ],
)
def test_transaction_uncertainty_uses_fixed_warning_and_safe_action(
    rpc, monkeypatch, producer_action
):
    import hades_cli.transactions as transactions_mod

    result = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        f"producer output {producer_action}",
        {
            "ok": False,
            "action": producer_action,
            "status": "unknown_effect",
        },
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("transaction.exec", {"argv": ["commit", "tx"], "session_id": "sid"})

    assert resp["output"] == "transaction effect uncertain — do not retry; reconcile first"
    assert resp["action"] == "command"
    wire = json.dumps(resp, ensure_ascii=False)
    assert producer_action not in wire
    for fragment in ("//secret", "secret/path", "/etc", "/root", "mailto:foo"):
        assert fragment not in wire


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
def test_transaction_native_scrubs_locator_tokens_from_output_and_free_text(
    rpc, monkeypatch, probe
):
    import hades_cli.transactions as transactions_mod

    text = f"producer free text {probe}"
    result = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        f"producer output {text}",
        {
            "ok": True,
            "action": "show",
            "eligibility": {
                "node-1": {
                    "can_execute": False,
                    "code": "blocked",
                    "fidelity": "exact",
                    "reason": text,
                    "blockers": [text],
                    "required_cascade_node_ids": ["node-2"],
                }
            },
        },
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: result)

    resp = rpc("transaction.exec", {"argv": ["show", "tx"], "session_id": "sid"})

    wire = json.dumps(resp, ensure_ascii=False)
    assert probe not in wire
    for fragment in ("//secret", "secret/path", "secret/item", "/etc", "/root"):
        assert fragment not in wire


def test_transaction_native_preserves_wire_safe_domain_ids_and_drops_unsafe_rows(
    rpc, monkeypatch
):
    import hades_cli.transactions as transactions_mod

    result = transactions_mod.TransactionCommandResult(
        transactions_mod.EXIT_OK,
        "safe",
        {
            "ok": True,
            "action": "preview",
            "transaction": {
                "transaction_id": "project:item-1",
                "status": "ready",
                "current_revision": 1,
                "receipt_id": "receipt:one",
            },
            "transactions": [
                {
                    "transaction_id": "project:item-1",
                    "status": "ready",
                    "current_revision": 1,
                    "receipt_id": "receipt:one",
                },
                {
                    "transaction_id": "artifact:secret/path",
                    "status": "ready",
                    "current_revision": 1,
                    "receipt_id": "receipt:bad",
                },
            ],
            "eligibility": {
                "node:publish": {
                    "can_execute": True,
                    "code": "ready",
                    "fidelity": "exact",
                    "reason": "safe",
                    "blockers": [],
                    "required_cascade_node_ids": ["node:cleanup"],
                },
                "file:/etc": {
                    "can_execute": False,
                    "code": "blocked",
                    "fidelity": "exact",
                    "reason": "unsafe",
                    "blockers": [],
                    "required_cascade_node_ids": [],
                },
            },
            "receipt": {
                "receipt_id": "receipt:one",
                "status": "verified",
                "content_hash": "sha256:deadbeef",
            },
            "observation": {
                "observation_id": "observation:one",
                "status": "verified",
                "content_hash": "sha256:feedface",
            },
            "preview_hash": "sha256:cafebabe",
            "nodes": [
                {"node_id": "node:publish", "fidelity": "exact", "requires_approval": False},
                {"node_id": "mailto:user", "fidelity": "exact", "requires_approval": False},
            ],
        },
    )
    monkeypatch.setattr(transactions_mod, "run_argv", lambda *_a, **_k: result)

    response = rpc("transaction.exec", {"argv": ["preview", "project:item-1"]})

    assert response["transaction"]["transaction_id"] == "project:item-1"
    assert response["transaction"]["receipt_id"] == "receipt:one"
    assert response["transactions"] == [
        {
            "transaction_id": "project:item-1",
            "status": "ready",
            "current_revision": 1,
            "receipt_id": "receipt:one",
        }
    ]
    assert set(response["eligibility"]) == {"node:publish"}
    assert response["eligibility"]["node:publish"]["required_cascade_node_ids"] == [
        "node:cleanup"
    ]
    assert response["receipt"]["receipt_id"] == "receipt:one"
    assert response["observation"]["observation_id"] == "observation:one"
    assert response["nodes"] == [
        {"node_id": "node:publish", "fidelity": "exact", "requires_approval": False}
    ]
    wire = json.dumps(response, ensure_ascii=False)
    for unsafe in ("artifact:secret/path", "file:/etc", "mailto:user"):
        assert unsafe not in wire

    required = {
        "transaction_id", "receipt_id", "node_id", "observation_id",
        "status", "content_hash", "fidelity", "code",
    }

    def assert_required_values(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in required:
                    assert nested is not None, (key, value)
                assert_required_values(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_required_values(nested)

    assert_required_values(response)
