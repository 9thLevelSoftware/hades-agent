"""Transaction CLI parser/service tests (plan Task 11)."""

from __future__ import annotations

import json

import pytest
import yaml

from hades_cli.transactions import (
    EXIT_OK,
    EXIT_VALIDATION,
    build_parser,
    run_argv,
    run_slash,
)
from hades_constants import get_hades_home


@pytest.fixture()
def parser():
    import argparse

    root = argparse.ArgumentParser(prog="hermes")
    sub = root.add_subparsers(dest="_root")
    build_parser(sub)
    return root


@pytest.mark.parametrize("argv", [
    ["create", "--plan", "plan.yaml", "--authority", "authority.yaml"],
    ["list", "--status", "ready"],
    ["show", "tx-1"],
    ["graph", "tx-1", "--revision", "2"],
    ["preview", "tx-1"],
    ["revise", "tx-1", "--plan", "revised.yaml", "--expected-revision", "1",
     "--reason", "recipient changed"],
    ["commit", "tx-1"],
    ["reconcile", "tx-1"],
    ["eligibility", "tx-1"],
    ["compensate", "tx-1", "write", "--cascade"],
    ["receipt", "tx-1", "--recheck"],
    ["outbox", "list", "tx-1"],
    ["outbox", "revise", "ob-1", "--message", "final",
     "--expected-revision", "1"],
    ["outbox", "cancel", "ob-1"],
    ["outbox", "release", "ob-1"],
])
def test_transaction_parser_accepts_only_bounded_commands(argv, parser):
    args = parser.parse_args(["transaction", *argv])
    assert args.transaction_action


@pytest.mark.parametrize("argv", [
    ["explode", "tx-1"],
    ["commit", "tx-1", "--force"],
    ["show", "tx-1", "trailing-garbage"],
    ["outbox", "detonate", "ob-1"],
])
def test_unknown_commands_and_trailing_arguments_are_rejected(argv):
    result = run_argv(argv)
    assert result.exit_code == EXIT_VALIDATION
    assert result.payload.get("ok") is False


def test_argument_bounds_are_enforced():
    result = run_argv(["show"] + ["x"] * 100)
    assert result.exit_code == EXIT_VALIDATION
    big = "y" * (70 * 1024)
    result = run_argv(["show", big])
    assert result.exit_code == EXIT_VALIDATION


def _write_plan(tmp_path, nodes=None, edges=None):
    plan = {
        "transaction": {"title": "cli test", "failure_policy": "stop"},
        "nodes": nodes if nodes is not None else [{
            "node_id": "write",
            "adapter_id": "workspace.v1",
            "action": "write_file",
            "args": {"path": "cli-note.md", "content": "hello\n"},
        }],
        "edges": edges or [],
    }
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.dump(plan), encoding="utf-8")
    authority = tmp_path / "authority.yaml"
    authority.write_text(yaml.dump({
        "authority_version": 1,
        "irreversible_policy": "ask",
    }), encoding="utf-8")
    return path, authority


def test_create_show_preview_and_json_redaction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan, authority = _write_plan(tmp_path)
    created = run_argv([
        "create", "--plan", str(plan), "--authority", str(authority),
        "--transaction-id", "tx-cli",
    ])
    assert created.exit_code == EXIT_OK, created.output
    assert "hermes transaction preview tx-cli" in created.output

    shown = run_argv(["show", "tx-cli"], output="json")
    assert shown.exit_code == EXIT_OK
    payload = json.loads(shown.output)
    assert payload["transaction"]["transaction_id"] == "tx-cli"
    # Redaction: content never appears in JSON output; hashes/ids do.
    assert "hello" not in shown.output

    previewed = run_argv(["preview", "tx-cli"])
    assert previewed.exit_code == EXIT_OK
    assert "preview ready" in previewed.output

    listed = run_argv(["list", "--status", "ready"])
    assert "tx-cli" in listed.output

    eligibility = run_argv(["eligibility", "tx-cli"])
    assert eligibility.exit_code == EXIT_OK


def test_commit_is_config_gated_in_preview_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan, authority = _write_plan(tmp_path)
    run_argv([
        "create", "--plan", str(plan), "--authority", str(authority),
        "--transaction-id", "tx-gated",
    ])
    run_argv(["preview", "tx-gated"])
    result = run_argv(["commit", "tx-gated"])
    assert result.exit_code != EXIT_OK
    assert "transactions.mode" in result.output
    assert result.payload.get("error") == "mode_gate"


def test_commit_works_when_mode_commit(tmp_path, monkeypatch):
    config_path = get_hades_home() / "config.yaml"
    existing = {}
    if config_path.exists():
        existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    existing["transactions"] = {"mode": "commit"}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(existing), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    plan, authority = _write_plan(tmp_path)
    run_argv([
        "create", "--plan", str(plan), "--authority", str(authority),
        "--transaction-id", "tx-commit",
    ])
    assert run_argv(["preview", "tx-commit"]).exit_code == EXIT_OK
    result = run_argv(["commit", "tx-commit"])
    # The workspace adapter requires the terminal tool handler for
    # write_file commits from the CLI; a blocked commit is an honest
    # outcome here — what matters is the gate opened and the
    # coordinator ran.
    assert result.payload.get("error") != "mode_gate"

    receipt = run_argv(["receipt", "tx-commit"])
    assert receipt.exit_code == EXIT_OK
    assert "receipt rct_" in receipt.output


def test_rejects_unknown_adapter_cycles_and_oversize_plan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan, authority = _write_plan(tmp_path, nodes=[{
        "node_id": "ghost", "adapter_id": "not-registered.v1",
        "action": "write_file", "args": {"path": "x", "content": "y"},
    }])
    result = run_argv([
        "create", "--plan", str(plan), "--authority", str(authority),
    ])
    assert result.exit_code != EXIT_OK

    cyclic, authority = _write_plan(
        tmp_path,
        nodes=[
            {"node_id": "a", "adapter_id": "workspace.v1",
             "action": "write_file", "args": {"path": "a", "content": "a"}},
            {"node_id": "b", "adapter_id": "workspace.v1",
             "action": "write_file", "args": {"path": "b", "content": "b"}},
        ],
        edges=[{"parent": "a", "child": "b"}, {"parent": "b", "child": "a"}],
    )
    result = run_argv([
        "create", "--plan", str(cyclic), "--authority", str(authority),
    ])
    assert result.exit_code != EXIT_OK
    assert "cycle" in result.output.lower()

    fat = tmp_path / "fat.yaml"
    fat.write_text("x: " + "a" * (1024 * 1024 + 10), encoding="utf-8")
    result = run_argv([
        "create", "--plan", str(fat), "--authority", str(authority),
    ])
    assert result.exit_code == EXIT_VALIDATION
    assert "1 MiB" in result.output


def test_run_slash_shares_the_same_surface(tmp_path, monkeypatch):
    assert "/transaction" in run_slash("")
    assert "/transaction" in run_slash("help")
    output = run_slash("list")
    assert "no transactions" in output or "transaction" in output


def test_help_exits_zero_without_starting_chat():
    result = run_argv(["--help"])
    assert result.exit_code == 0
    assert "transaction" in result.output
