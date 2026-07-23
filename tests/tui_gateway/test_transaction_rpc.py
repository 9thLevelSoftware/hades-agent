"""Native ``transaction.exec`` JSON-RPC tests (plan Task 12).

The RPC runs the shared ``hades_cli.transactions.run_argv`` surface in
the LIVE gateway process — never a slash-worker subprocess — with
bounded argv validation, structured results, and redacted errors.
"""

from __future__ import annotations

import importlib
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


def _seed_transaction(tmp_path, transaction_id="tx-1"):
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.dump({
        "transaction": {"title": "rpc test"},
        "nodes": [{
            "node_id": "write", "adapter_id": "workspace.v1",
            "action": "write_file",
            "args": {"path": "rpc-note.md", "content": "hello\n"},
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
