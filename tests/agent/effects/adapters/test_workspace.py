"""Real-filesystem/Git tests for the workspace effect adapters (plan Task 6).

Everything runs against real temp files, a real shadow-checkpoint store,
and real Git worktrees — reversibility claims are proven on disk, never
mocked.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.checkpoint_manager as checkpoint_manager_module
from agent.effects.adapters.workspace import (
    WorkspaceAdapter,
    WorkspaceGitAdapter,
)
from agent.effects.coordinator import (
    TransactionCoordinator,
    prepared_from_json,
)
from agent.effects.models import CompensationRequest, EffectContext
from agent.effects.registry import EffectAdapterRegistry
from agent.effects.store import TransactionStore
from agent.operation_journal import OperationJournal
from hades_state import SessionDB


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _AllowAll:
    def authorize(self, context, *, consume):
        return SimpleNamespace(
            allowed=True, verdict="allow", code="allow", context_hash="ctx",
        )


class WsHarness:
    def __init__(self, tmp_path: Path):
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db = SessionDB(tmp_path / "state.db")
        self.store = TransactionStore(self.db)
        self.journal = OperationJournal(self.db)
        self.adapter = WorkspaceAdapter(
            workspace_root=self.workspace,
            transaction_lookup=self.store.get_effect_by_operation_id,
        )
        self.git_adapter = WorkspaceGitAdapter(
            transaction_lookup=self.store.get_effect_by_operation_id,
        )
        self.adapters = EffectAdapterRegistry()
        self.adapters.register(self.adapter)
        self.adapters.register(self.git_adapter)
        self.coordinator = TransactionCoordinator(
            store=self.store,
            adapters=self.adapters,
            journal=self.journal,
            authority_provider_factory=_AllowAll,
        )

    def close(self):
        self.db.close()

    def context(self, transaction_id: str, node_id: str) -> EffectContext:
        return EffectContext(
            transaction_id=transaction_id, revision=1, node_id=node_id,
        )

    def create_write(self, transaction_id="tx-1", path="README.md",
                     content="new\n"):
        self.store.create_transaction(
            transaction_id=transaction_id, profile="default",
            title="workspace write",
            authority={"authority_version": 1},
            graph={
                "nodes": [{
                    "node_id": "write", "adapter_id": "workspace.v1",
                    "action": "write_file",
                    "args": {"path": path, "content": content},
                    "resource_keys": [f"file:{path}"],
                }],
                "edges": [],
            },
            failure_policy="stop",
        )

    def write_invoke(self):
        """Terminal handler standing in for the real write_file tool."""

        def _invoke(args):
            target = Path(args["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args["content"], encoding="utf-8", newline="")
            return {"success": True, "path": args["path"]}

        return _invoke


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(
        checkpoint_manager_module, "CHECKPOINT_BASE",
        tmp_path / "checkpoints",
    )
    h = WsHarness(tmp_path)
    try:
        yield h
    finally:
        h.close()


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _init_repo(root: Path, branch: str = "main") -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.check_output(
        ["git", "init", "-b", branch, str(root)], text=True,
    )
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-m", "seed")


# ── workspace.v1 ────────────────────────────────────────────────────────


def test_workspace_preview_commit_and_exact_compensation(harness):
    target = harness.workspace / "README.md"
    target.write_text("old\n", encoding="utf-8", newline="")
    harness.create_write(content="new\n")
    preview = harness.coordinator.preview("tx-1")
    assert preview.status == "ready"
    effect = harness.store.effect_for("tx-1", 1, "write")
    assert effect.preview["before"]["sha256"] == sha256_bytes(b"old\n")
    assert "-old" in effect.preview["summary"]
    assert "+new" in effect.preview["summary"]
    result = harness.coordinator.commit(
        "tx-1", invoke_map={"write": harness.write_invoke()},
    )
    assert result.status == "committed"
    assert target.read_text(encoding="utf-8") == "new\n"

    committed = harness.store.effect_for("tx-1", 1, "write")
    compensation = harness.adapter.compensate(
        CompensationRequest(
            effect_id=committed.effect_id,
            prepared=prepared_from_json(committed.prepared),
            verified_result_hash="",
        ),
        harness.context("tx-1", "write"),
    )
    assert compensation.fidelity == "exact"
    assert compensation.status == "compensated"
    assert target.read_text(encoding="utf-8") == "old\n"


def test_workspace_refuses_escape_and_unsupported_actions(harness):
    outside = harness.workspace.parent / "outside.txt"
    harness.store.create_transaction(
        transaction_id="tx-escape", profile="default", title="escape",
        authority={"authority_version": 1},
        graph={
            "nodes": [{
                "node_id": "write", "adapter_id": "workspace.v1",
                "action": "write_file",
                "args": {"path": str(outside), "content": "x"},
            }],
            "edges": [],
        },
        failure_policy="stop",
    )
    result = harness.coordinator.preview("tx-escape")
    assert result.status == "blocked"
    assert "outside" in (result.error or "")

    harness.store.create_transaction(
        transaction_id="tx-push", profile="default", title="push",
        authority={"authority_version": 1},
        graph={
            "nodes": [{
                "node_id": "push", "adapter_id": "workspace.v1",
                "action": "push", "args": {"remote": "origin"},
            }],
            "edges": [],
        },
        failure_policy="stop",
    )
    result = harness.coordinator.preview("tx-push")
    assert result.status == "blocked"
    assert "unsupported action" in (result.error or "")


def test_workspace_refuses_primary_branch_mutation(harness):
    repo = harness.workspace / "repo"
    _init_repo(repo, branch="main")
    harness.create_write(transaction_id="tx-main", path="repo/seed.txt",
                         content="clobber\n")
    result = harness.coordinator.preview("tx-main")
    assert result.status == "blocked"
    assert "branch" in (result.error or "")


def test_workspace_rewrites_v4a_patch_headers_to_authorized_paths(harness):
    target = harness.workspace / "src.txt"
    target.write_text("line-one\nline-two\n", encoding="utf-8")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src.txt\n"
        "@@\n"
        "-line-one\n"
        "+line-1\n"
        "*** End Patch\n"
    )
    harness.store.create_transaction(
        transaction_id="tx-patch", profile="default", title="patch",
        authority={"authority_version": 1},
        graph={
            "nodes": [{
                "node_id": "patch", "adapter_id": "workspace.v1",
                "action": "patch",
                "args": {"mode": "patch", "patch": patch},
            }],
            "edges": [],
        },
        failure_policy="stop",
    )
    result = harness.coordinator.preview("tx-patch")
    assert result.status == "ready"
    effect = harness.store.effect_for("tx-patch", 1, "patch")
    rewritten = effect.prepared["args"]["patch"]
    assert str(target.resolve()) in rewritten.replace("/", "\\") or (
        str(target.resolve()).replace("\\", "/") in rewritten
    )


def test_workspace_drift_blocks_exact_compensation(harness):
    target = harness.workspace / "README.md"
    target.write_text("old\n", encoding="utf-8", newline="")
    harness.create_write(content="new\n")
    harness.coordinator.preview("tx-1")
    harness.coordinator.commit(
        "tx-1", invoke_map={"write": harness.write_invoke()},
    )
    # A human edits the file after commit — exact undo must refuse.
    target.write_text("human edit\n", encoding="utf-8")
    committed = harness.store.effect_for("tx-1", 1, "write")
    compensation = harness.adapter.compensate(
        CompensationRequest(
            effect_id=committed.effect_id,
            prepared=prepared_from_json(committed.prepared),
            verified_result_hash="",
        ),
        harness.context("tx-1", "write"),
    )
    assert compensation.status == "blocked"
    assert "drift" in (compensation.error or "")
    assert target.read_text(encoding="utf-8") == "human edit\n"


def test_workspace_reconcile_certifies_landed_from_durable_hashes(harness):
    target = harness.workspace / "README.md"
    target.write_text("old\n", encoding="utf-8", newline="")
    harness.create_write(content="new\n")
    harness.coordinator.preview("tx-1")
    harness.coordinator.commit(
        "tx-1", invoke_map={"write": harness.write_invoke()},
    )
    effect = harness.store.effect_for("tx-1", 1, "write")
    result = harness.adapter.reconcile(effect, harness.context("tx-1", "write"))
    assert result.disposition == "landed"


# ── workspace-git.v1 ────────────────────────────────────────────────────


def _make_worktree(tmp_path: Path) -> Path:
    primary = tmp_path / "primary"
    _init_repo(primary, branch="main")
    worktree = tmp_path / "feature-wt"
    _git(primary, "worktree", "add", "-b", "feature", str(worktree))
    _git(worktree, "config", "user.email", "test@example.com")
    _git(worktree, "config", "user.name", "Test")
    return worktree


def test_git_adapter_commits_locally_and_resets_exactly(harness, tmp_path):
    worktree = _make_worktree(tmp_path)
    (worktree / "change.txt").write_text("change\n", encoding="utf-8")
    parent_head = _git(worktree, "rev-parse", "HEAD")

    harness.store.create_transaction(
        transaction_id="tx-git", profile="default", title="local commit",
        authority={"authority_version": 1},
        graph={
            "nodes": [{
                "node_id": "commit", "adapter_id": "workspace-git.v1",
                "action": "commit_local",
                "args": {
                    "worktree": str(worktree),
                    "paths": ["change.txt"],
                    "message": "feat: bounded local commit",
                },
            }],
            "edges": [],
        },
        failure_policy="stop",
    )
    assert harness.coordinator.preview("tx-git").status == "ready"
    result = harness.coordinator.commit("tx-git")
    assert result.status == "committed"
    created = _git(worktree, "rev-parse", "HEAD")
    assert created != parent_head
    assert "bounded local commit" in _git(worktree, "log", "-1", "--format=%s")

    committed = harness.store.effect_for("tx-git", 1, "commit")
    compensation = harness.git_adapter.compensate(
        CompensationRequest(
            effect_id=committed.effect_id,
            prepared=prepared_from_json(committed.prepared),
            verified_result_hash="",
        ),
        harness.context("tx-git", "commit"),
    )
    assert compensation.status == "compensated"
    assert _git(worktree, "rev-parse", "HEAD") == parent_head


def test_git_adapter_refuses_primary_checkout_and_push_args(harness, tmp_path):
    primary = tmp_path / "primary2"
    _init_repo(primary, branch="feature-x")
    harness.store.create_transaction(
        transaction_id="tx-primary", profile="default", title="primary",
        authority={"authority_version": 1},
        graph={
            "nodes": [{
                "node_id": "commit", "adapter_id": "workspace-git.v1",
                "action": "commit_local",
                "args": {
                    "worktree": str(primary), "paths": ["seed.txt"],
                    "message": "nope",
                },
            }],
            "edges": [],
        },
        failure_policy="stop",
    )
    assert harness.coordinator.preview("tx-primary").status == "blocked"

    harness.store.create_transaction(
        transaction_id="tx-pushy", profile="default", title="push",
        authority={"authority_version": 1},
        graph={
            "nodes": [{
                "node_id": "commit", "adapter_id": "workspace-git.v1",
                "action": "commit_local",
                "args": {
                    "worktree": str(primary), "paths": ["seed.txt"],
                    "message": "m", "push": True,
                },
            }],
            "edges": [],
        },
        failure_policy="stop",
    )
    assert harness.coordinator.preview("tx-pushy").status == "blocked"


# ── Tool registry metadata ──────────────────────────────────────────────


def test_file_tools_register_workspace_effect_actions():
    import tools.file_tools  # noqa: F401 — triggers registration
    from model_tools import registry

    for name in ("write_file", "patch"):
        metadata = registry.get_operation_metadata(name)
        assert metadata["effect_adapter"] == "workspace.v1"
        assert metadata["effect_action"] == name


def test_lying_handler_never_verifies(harness):
    """A handler that reports success but writes nothing must not verify."""
    target = harness.workspace / "README.md"
    target.write_text("old\n", encoding="utf-8", newline="")
    harness.create_write(content="new\n")
    harness.coordinator.preview("tx-1")

    def _liar(args):
        return {"success": True}

    result = harness.coordinator.commit("tx-1", invoke_map={"write": _liar})
    # The write never landed: the effect is committed-but-unverified,
    # never `verified`.
    effect = harness.store.effect_for("tx-1", 1, "write")
    assert effect.phase == "committed"
    assert effect.verification["verified"] is False
    assert "expected" in effect.verification["reason"]
    assert target.read_text(encoding="utf-8") == "old\n"
    assert result.status == "committed"


def test_error_reporting_handler_never_verifies(harness):
    target = harness.workspace / "README.md"
    target.write_text("old\n", encoding="utf-8", newline="")
    harness.create_write(content="new\n")
    harness.coordinator.preview("tx-1")

    def _failing(args):
        return {"success": False, "error": "disk full"}

    harness.coordinator.commit("tx-1", invoke_map={"write": _failing})
    effect = harness.store.effect_for("tx-1", 1, "write")
    assert effect.verification["verified"] is False
    assert "disk full" in effect.verification["reason"]
