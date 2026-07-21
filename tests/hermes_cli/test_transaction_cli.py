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
    # The CLI wires the registered write_file handler into workspace
    # commits: the write actually lands.
    assert result.payload.get("error") != "mode_gate"
    assert result.payload.get("status") == "committed", result.output
    written = tmp_path / "cli-note.md"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "hello\n"

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


def test_outbox_release_requires_durable_exact_approval(tmp_path, monkeypatch):
    """Release fails closed without a consumed approval binding: there is
    no flag that bypasses the gate, and no human present means no
    release."""
    config_path = get_hades_home() / "config.yaml"
    existing = {}
    if config_path.exists():
        existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    existing["transactions"] = {"mode": "commit"}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(existing), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.dump({
        "transaction": {"title": "release test"},
        "nodes": [{
            "node_id": "send", "adapter_id": "message-outbox.v1",
            "action": "send",
            "args": {
                "platform": "faketest", "target": "faketest:chan",
                "message": "hello", "not_before_seconds": 3600,
            },
        }],
        "edges": [],
    }), encoding="utf-8")
    authority = tmp_path / "authority.yaml"
    authority.write_text(yaml.dump({
        "authority_version": 1, "irreversible_policy": "ask",
    }), encoding="utf-8")
    run_argv([
        "create", "--plan", str(plan), "--authority", str(authority),
        "--transaction-id", "tx-release",
    ])
    assert run_argv(["preview", "tx-release"]).exit_code == EXIT_OK
    assert run_argv(["commit", "tx-release"]).payload.get("status") == "committed"

    listed = run_argv(["outbox", "list", "tx-release"], output="json")
    rows = listed.payload.get("rows") or []
    assert rows and rows[0]["status"] == "pending_approval"
    outbox_id = rows[0]["outbox_id"]

    # No approval binding + no interactive human → refused, fail closed.
    result = run_argv(["outbox", "release", outbox_id])
    assert result.exit_code != EXIT_OK
    assert "approval" in result.output.lower()

    # The parser no longer accepts any bypass flag.
    bypass = run_argv(["outbox", "release", outbox_id, "--approved"])
    assert bypass.exit_code == EXIT_VALIDATION

    # A durable exact approval binding makes the same command succeed.
    from agent.effects.authority import ApprovalBinding
    from agent.effects.models import content_hash
    from agent.effects.store import TransactionStore
    from gateway.mission_outbox import MissionOutboxStore
    from hades_state import SessionDB
    import os as _os
    import time as _time

    db = SessionDB(get_hades_home() / "state.db")
    try:
        store = TransactionStore(db)
        outbox = MissionOutboxStore(db)
        row = outbox.get_by_id(outbox_id)
        effect = store.latest_effects_by_node("tx-release")["send"]
        transaction = store.get_transaction("tx-release")
        requester = (
            _os.environ.get("USERNAME") or _os.environ.get("USER") or "user"
        )
        store.insert_approval(ApprovalBinding(
            approval_id="ap-release-test",
            transaction_id="tx-release",
            revision=effect.revision,
            node_id="send",
            operation="release",
            args_hash=content_hash(dict(row.content or {})),
            preview_hash=row.content_hash,
            resources=(f"message:{row.platform}:{row.target}",),
            authority_version=transaction.authority_version,
            requester=requester,
            channel="cli",
            decision="approved",
            expires_at_ms=int(_time.time() * 1000) + 300_000,
            consumed_at_ms=None,
            created_at_ms=int(_time.time() * 1000),
        ))
    finally:
        db.close()

    released = run_argv(["outbox", "release", outbox_id])
    assert released.exit_code == EXIT_OK, released.output
    confirm = run_argv(["outbox", "list", "tx-release"], output="json")
    assert (confirm.payload.get("rows") or [])[0]["status"] == "scheduled"


def test_workflow_family_is_available_to_cli_plans(tmp_path, monkeypatch):
    """The documented hermes-workflow.v1 family must be registered in the
    shared CLI service without any caller-owned connection."""
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.dump({
        "transaction": {"title": "workflow deploy"},
        "nodes": [{
            "node_id": "deploy", "adapter_id": "hermes-workflow.v1",
            "action": "deploy",
            "args": {"spec": {
                "id": "cli_wf_demo", "name": "CLI Demo", "version": 1,
                "triggers": [{"type": "manual", "id": "manual"}],
                "nodes": {"start": {"type": "pass"}},
            }},
        }],
        "edges": [],
    }), encoding="utf-8")
    authority = tmp_path / "authority.yaml"
    authority.write_text(yaml.dump({
        "authority_version": 1, "irreversible_policy": "ask",
    }), encoding="utf-8")
    created = run_argv([
        "create", "--plan", str(plan), "--authority", str(authority),
        "--transaction-id", "tx-wf",
    ])
    assert created.exit_code == EXIT_OK, created.output
    previewed = run_argv(["preview", "tx-wf"])
    assert previewed.exit_code == EXIT_OK, previewed.output


def test_preview_and_show_render_carried_frozen_nodes(tmp_path, monkeypatch):
    """After a partial commit + revision, the carried frozen node has no
    attempt at the current revision — rendering must not crash."""
    config_path = get_hades_home() / "config.yaml"
    existing = {}
    if config_path.exists():
        existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    existing["transactions"] = {"mode": "commit"}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(existing), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    plan, authority = _write_plan(tmp_path, nodes=[
        {"node_id": "first", "adapter_id": "workspace.v1",
         "action": "write_file",
         "args": {"path": "first.md", "content": "one\n"}},
        {"node_id": "second", "adapter_id": "workspace.v1",
         "action": "write_file",
         "args": {"path": "second.md", "content": "two\n"}},
    ], edges=[{"parent": "first", "child": "second"}])
    run_argv([
        "create", "--plan", str(plan), "--authority", str(authority),
        "--transaction-id", "tx-carried",
    ])
    assert run_argv(["preview", "tx-carried"]).exit_code == EXIT_OK
    committed = run_argv(["commit", "tx-carried", "--through-node", "first"])
    assert committed.payload.get("status") == "ready", committed.output

    revised_plan = tmp_path / "plan2.yaml"
    revised_plan.write_text(yaml.dump({
        "transaction": {"title": "cli test", "failure_policy": "stop"},
        "nodes": [
            {"node_id": "first", "adapter_id": "workspace.v1",
             "action": "write_file",
             "args": {"path": "first.md", "content": "one\n"}},
            {"node_id": "second", "adapter_id": "workspace.v1",
             "action": "write_file",
             "args": {"path": "second.md", "content": "two-revised\n"}},
        ],
        "edges": [{"parent": "first", "child": "second"}],
    }), encoding="utf-8")
    revised = run_argv([
        "revise", "tx-carried", "--plan", str(revised_plan),
        "--expected-revision", "1", "--reason", "edit pending",
    ])
    assert revised.exit_code == EXIT_OK, revised.output

    previewed = run_argv(["preview", "tx-carried"])
    assert previewed.exit_code == EXIT_OK, previewed.output
    assert "first" in previewed.output and "second" in previewed.output

    shown = run_argv(["show", "tx-carried"], output="json")
    assert shown.exit_code == EXIT_OK, shown.output
    payload = json.loads(shown.output)
    phases = {row["node_id"]: row["phase"] for row in payload["nodes"]}
    assert phases["first"] in {"committed", "verified"}
    assert phases["second"] == "previewed"

    final = run_argv(["commit", "tx-carried"])
    assert final.payload.get("status") == "committed", final.output
    assert (tmp_path / "second.md").read_text(encoding="utf-8") == "two-revised\n"
    # The frozen node executed exactly once: its file holds the original.
    assert (tmp_path / "first.md").read_text(encoding="utf-8") == "one\n"
