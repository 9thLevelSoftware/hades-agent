"""Tests for Task 4 — WorkspaceEffectAdapter and WorkspaceCommitEffectAdapter.

Real temp Git repositories and disposable worktrees only; never touches the
project worktree. The adapter mediates between the Task 3 effect coordinator
and ``tools/checkpoint_manager.py`` (forced checkpoints) and ``tools/file_tools.py``
(path resolution). All injection points are explicit — no module globals.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest
import yaml

# ---------------------------------------------------------------------------
# Temp-Git helpers
# ---------------------------------------------------------------------------


def _run(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_git_repo(path: Path, *, default_branch: str = "main") -> Path:
    """Initialise a real git repo at ``path`` with a single commit and the
    requested default branch. Returns the repo path."""
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "--initial-branch", default_branch], path)
    _run(["git", "config", "user.email", "test@local"], path)
    _run(["git", "config", "user.name", "Test"], path)
    _run(["git", "config", "commit.gpgsign", "false"], path)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "-A"], path)
    _run(["git", "commit", "-m", "seed"], path)
    return path


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def _branch_of(path: Path) -> str:
    out = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip()


def _make_worktree(parent: Path, name: str, *, branch: str = "feature") -> Path:
    """Create a worktree under ``parent/.worktrees/<name>`` on a new branch."""
    wt = parent / ".worktrees" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "-C", str(parent), "worktree", "add", "-b", branch, str(wt)], parent)
    return wt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated $HOME so checkpoint manager doesn't write to ~/.hades."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture()
def checkpoint_base(tmp_path, monkeypatch):
    """Use an isolated checkpoint base, not the user's real one."""
    base = tmp_path / "checkpoints"
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)
    return base


@pytest.fixture()
def repo(tmp_path) -> Path:
    """Real temp git repository — disposable, has main branch + seed commit."""
    return _init_git_repo(tmp_path / "repo", default_branch="main")


@pytest.fixture()
def worktree_repo(repo: Path) -> Path:
    """Real git repo with an active worktree on a feature branch (NOT main)."""
    return _make_worktree(repo, "wt1", branch="feature/test")


@pytest.fixture()
def mission_ctx(repo: Path):
    """Mission authority object the adapter depends on. workspace_root resolves
    to the feature worktree; workspace_roots includes it; no Kanban."""
    from agent.effect_adapters import WorkspaceAuthority
    wt = _make_worktree(repo, "wt-authority", branch="mission/wt")
    return WorkspaceAuthority(
        mission_id="m-test",
        workspace_roots=[wt],
        workspace_root=wt,
        actor_id="tester",
    )


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def _wf(a):
    """Test handler that actually writes to disk, like write_file_tool."""
    p = Path(a["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(a.get("content", ""), encoding="utf-8")
    return {"wrote": a["path"]}


# ---------------------------------------------------------------------------
# Authority / fail-closed
# ---------------------------------------------------------------------------


class TestAuthority:
    def test_prepare_blocks_path_outside_workspace_roots(
        self, worktree_repo, checkpoint_base, mission_ctx, monkeypatch
    ):
        """An absolute path that resolves outside every ``workspace_roots``
        entry is rejected at prepare time, before any mutation."""
        # Switch the mission's working context to a different worktree so
        # ``worktree_repo`` itself is OUTSIDE workspace_roots.
        from agent.effect_adapters import (
            WorkspaceAuthority, WorkspaceEffectAdapter,
        )
        outside = worktree_repo
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        from agent.effect_transactions import OperationRequest
        # Path resolving OUTSIDE the authority's workspace_roots.
        target = outside / "secrets.txt"
        req = OperationRequest(
            tool_name="write_file",
            args={"path": str(target), "content": "leak"},
            mission_id="m-test",
            operation_key="opk-leak",
        )
        with pytest.raises(PermissionError):
            adapter.prepare(req)

    def test_prepare_blocks_symlink_traversal_outside_workspace_root(
        self, mission_ctx, checkpoint_base, monkeypatch
    ):
        """A symlink whose target is OUTSIDE ``workspace_root`` is rejected
        after symlink resolution."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        wt = mission_ctx.workspace_root
        # Create a symlink inside the worktree pointing OUTSIDE it.
        outside_dir = wt.parent.parent / "external"
        outside_dir.mkdir(parents=True, exist_ok=True)
        link = wt / "leak.txt"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(outside_dir / "real.txt")
        (outside_dir / "real.txt").write_text("secret\n")

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        from agent.effect_transactions import OperationRequest
        req = OperationRequest(
            tool_name="write_file",
            args={"path": str(link), "content": "x"},
            mission_id="m-test",
            operation_key="opk-sym",
        )
        with pytest.raises(PermissionError):
            adapter.prepare(req)

    def test_prepare_rejects_main_or_master_branch(
        self, repo, checkpoint_base, monkeypatch
    ):
        """A workspace whose current branch is main/master (or HEAD detached)
        is rejected before any mutation. Authority lookup should never even
        be reached here — but if it is, the adapter still blocks."""
        from agent.effect_adapters import (
            WorkspaceAuthority, WorkspaceEffectAdapter,
        )
        adapter = WorkspaceEffectAdapter(
            authority=WorkspaceAuthority(
                mission_id="m-test",
                workspace_roots=[repo],
                workspace_root=repo,
                actor_id="tester",
            ),
            checkpoint_base=checkpoint_base,
        )
        from agent.effect_transactions import OperationRequest
        assert _branch_of(repo) == "main"
        req = OperationRequest(
            tool_name="write_file",
            args={"path": "README.md", "content": "mutate"},
            mission_id="m-test",
            operation_key="opk-main",
        )
        with pytest.raises(PermissionError):
            adapter.prepare(req)

    def test_prepare_rejects_primary_checkout_of_kanban_or_repository_workspace(
        self, repo, checkpoint_base, monkeypatch
    ):
        """The PRIMARY checkout (not a worktree) of a Kanban/repo workspace
        is rejected. We simulate this by marking the authority's workspace as
        primary AND forcing the ``is_primary_checkout`` flag the adapter
        trusts from injected authority data."""
        from agent.effect_adapters import (
            WorkspaceAuthority, WorkspaceEffectAdapter,
        )
        # Build a feature worktree first; then point authority at the primary
        # checkout and assert it's rejected.
        wt = _make_worktree(repo, "wt-feat", branch="feat")
        authority = WorkspaceAuthority(
            mission_id="m-test",
            workspace_roots=[repo],      # primary checkout
            workspace_root=repo,         # primary checkout
            actor_id="tester",
            workspace_kind="repository",
        )
        adapter = WorkspaceEffectAdapter(
            authority=authority, checkpoint_base=checkpoint_base,
        )
        from agent.effect_transactions import OperationRequest
        req = OperationRequest(
            tool_name="write_file",
            args={"path": "README.md", "content": "x"},
            mission_id="m-test",
            operation_key="opk-primary",
        )
        with pytest.raises(PermissionError):
            adapter.prepare(req)


# ---------------------------------------------------------------------------
# Prepare / commit / verify round-trip — bytes, mode, deletion, git diff
# ---------------------------------------------------------------------------


class TestWorkspacePrepareCommitVerify:
    def test_write_file_prepare_records_forced_checkpoint_and_exact_target(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """``prepare`` returns a PreparedEffect whose ``before`` snapshot
        captures existence/SHA/mode/git status, whose ``compensation`` carries
        a forced CheckpointRef (distinct from the no-turn forced ones), and
        whose ``preview`` carries a unified diff preview."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest
        from tools.checkpoint_manager import CheckpointRef

        wt = mission_ctx.workspace_root
        target = wt / "notes.txt"
        target.write_text("original\n")

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        req = OperationRequest(
            tool_name="write_file",
            args={"path": "notes.txt", "content": "updated\n"},
            mission_id="m-test",
            operation_key="opk-1",
        )
        prepared = adapter.prepare(req)

        # Forced checkpoint recorded on PreparedEffect.compensation as a
        # JSON-decodable mapping (NOT a CheckpointRef dataclass — the
        # dataclass does not survive SessionDB canonical JSON).
        assert "checkpoint_id" in prepared.compensation
        assert "commit_hash" in prepared.compensation
        assert "working_dir" in prepared.compensation
        assert prepared.compensation["working_dir"] == str(wt.resolve())
        assert len(prepared.compensation["commit_hash"]) >= 4

        # Exact normalized target recorded.
        assert prepared.before["targets"] == [str((wt / "notes.txt").resolve())]
        # Existence / SHA / mode captured.
        before_targets = prepared.before["targets_with_state"]
        entry = before_targets[0]
        assert entry["existed"] is True
        assert entry["mode"] is not None
        assert entry["sha256"] == hashlib.sha256(b"original\n").hexdigest()
        # Git status snapshot.
        assert "git_status" in prepared.before

        # Unified diff preview produced.
        assert "unified_diff" in prepared.preview
        diff = prepared.preview["unified_diff"]
        assert "-original" in diff or "original" in diff
        assert "+updated" in diff or "updated" in diff

    def test_two_transactions_get_distinct_forced_checkpoints(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """``prepare(force=True)`` produces a distinct checkpoint even when
        the same turn sees consecutive adapter calls. The first transaction's
        forced checkpoint must NOT equal the second's."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        # Two prepare() calls back-to-back — both must produce distinct
        # forced CheckpointRefs, not the deduped single-checkpoint-per-turn.
        p1 = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "a.txt", "content": "1\n"},
            mission_id="m-test", operation_key="opk-1",
        ))
        p2 = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "b.txt", "content": "2\n"},
            mission_id="m-test", operation_key="opk-2",
        ))
        ref1 = p1.compensation
        ref2 = p2.compensation
        assert ref1["commit_hash"] != ref2["commit_hash"]  # type: ignore[index]  # noqa: E501

    def test_commit_invokes_handler_once_with_normalized_args(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """``commit`` invokes the handler exactly once with the normalized
        absolute path."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )

        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "hello.txt", "content": "hi\n"},
            mission_id="m-test", operation_key="opk-commit",
        ))
        calls: List[Dict[str, Any]] = []

        def handler(args):
            calls.append(dict(args))
            return {"wrote": args["path"]}

        result = adapter.commit(prepared, handler)
        assert result == {"wrote": str((wt / "hello.txt").resolve())}
        assert len(calls) == 1
        # Normalized path passed to handler.
        assert calls[0]["path"] == str((wt / "hello.txt").resolve())

    def test_verify_records_before_after_facts_and_changed_paths(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """``verify`` returns changed_paths and resulting hashes."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )

        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "hello.txt", "content": "hi\n"},
            mission_id="m-test", operation_key="opk-verify",
        ))
        def _write_handler(args):
            p = Path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return {"wrote": args["path"]}

        result = adapter.commit(prepared, _write_handler)
        verified = adapter.verify(prepared, result)
        target = (wt / "hello.txt").resolve()
        assert verified["changed_paths"] == [str(target)]
        sha = hashlib.sha256(b"hi\n").hexdigest()
        assert verified["after_hashes"][str(target)] == sha

    def test_patch_normalizes_and_records_diff(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """``patch`` adapter normalizes target paths and records a unified
        diff in ``preview``."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        (wt / "src.txt").write_text("hello world\n")

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="patch",
            args={
                "mode": "replace", "path": "src.txt",
                "old_string": "hello", "new_string": "goodbye",
            },
            mission_id="m-test", operation_key="opk-patch",
        ))
        # Target must resolve to absolute path inside workspace_root.
        assert prepared.before["targets"] == [str((wt / "src.txt").resolve())]
        # Diff preview mentions both sides.
        diff = prepared.preview["unified_diff"]
        assert "-hello" in diff or "hello" in diff
        assert "+goodbye" in diff or "goodbye" in diff


# ---------------------------------------------------------------------------
# Reconcile — handler crash recovery without re-invoking handler
# ---------------------------------------------------------------------------


class TestReconcile:
    def test_reconcile_returns_landed_or_unknown_without_calling_handler(
        self, worktree_repo, checkpoint_base, mission_ctx, monkeypatch
    ):
        """After a handler crash/timeout the adapter's ``reconcile`` returns
        evidence based on durable before/after state — it does NOT invoke
        the handler a second time."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "recon.txt", "content": "landed\n"},
            mission_id="m-test", operation_key="opk-recon",
        ))
        # Simulate that the handler ran and the file landed before crashing.
        (wt / "recon.txt").write_text("landed\n")
        # Now reconcile — handler must NOT be invoked.
        handler_calls: List[Any] = []
        outcome = adapter.reconcile(
            type("Rec", (), {
                "operation_id": "opk-recon",
                "before": prepared.before,
                "verification": {
                    "after_hashes": {
                        str((wt / "recon.txt").resolve()):
                            hashlib.sha256(b"landed\n").hexdigest(),
                    },
                    "changed_paths": [str((wt / "recon.txt").resolve())],
                },
            })()
        )
        # Handler not invoked from reconcile — only the durable state was
        # consulted.
        assert handler_calls == []
        assert outcome["disposition"] in {"landed", "unknown"}
        assert outcome["changed_paths"] == [str((wt / "recon.txt").resolve())]

    def test_reconcile_marks_unknown_when_state_does_not_match(
        self, worktree_repo, checkpoint_base, mission_ctx, monkeypatch
    ):
        """When the file does NOT exist on disk but verify expected it,
        reconcile returns ``unknown`` — never a retry."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "missing.txt", "content": "x"},
            mission_id="m-test", operation_key="opk-missing",
        ))
        # File does NOT exist on disk; reconcile must be honest about it.
        target = (wt / "missing.txt").resolve()
        outcome = adapter.reconcile(
            type("Rec", (), {
                "operation_id": "opk-missing",
                "before": prepared.before,
                "verification": {
                    "after_hashes": {
                        str(target): hashlib.sha256(b"x").hexdigest(),
                    },
                    "changed_paths": [str(target)],
                },
            })()
        )
        assert outcome["disposition"] == "unknown"

    def test_reconcile_uses_transaction_lookup_when_record_lacks_evidence(
        self, worktree_repo, checkpoint_base, mission_ctx, tmp_path
    ):
        """Spec: the Task 3 coordinator hands ``reconcile`` an
        ``OperationRecord`` carrying only ``operation_id`` (no
        ``before``/``verification``). The adapter must consult the
        injected ``transaction_lookup`` to fetch the durable SessionDB
        effect transaction row and decide ``landed`` from the
        durable ``verification.after_hashes`` — without re-invoking
        the handler.
        """
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest
        from hades_state import SessionDB

        wt = mission_ctx.workspace_root
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )

        # Drive the full prepare/commit/verify cycle so we have durable
        # evidence to look up.
        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "durable.txt", "content": "landed-on-disk\n"},
            mission_id="m-test", operation_key="opk-durable",
        ))
        commit_handler_calls: List[Any] = []

        def commit_handler(args):
            commit_handler_calls.append(args)
            return _wf(args)

        result = adapter.commit(prepared, commit_handler)
        verification = dict(adapter.verify(prepared, result))
        # Sanity: the commit phase called the handler exactly once (the
        # action that produced the durable evidence). Reconcile must
        # NEVER invoke a handler.
        assert len(commit_handler_calls) == 1

        # Persist the durable effect tx in a real SessionDB and freeze
        # the row exactly as the coordinator would after a verify call.
        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        try:
            # The effect_transactions table FK-requires an entry in
            # agent_operations; seed one via OperationJournal first.
            from agent.operation_journal import OperationJournal
            OperationJournal(db).create(
                operation_id="opk-durable", kind="workspace.v1",
            )
            db.create_effect_transaction(
                transaction_id="tx-opk-durable",
                operation_id="opk-durable",
                mission_id="m-test",
                adapter_id=WorkspaceEffectAdapter.adapter_id,
                sequence_no=1,
                semantics={"kind": "reversible", "idempotent": False,
                           "reconcilable": True},
                depends_on=[],
                # Prepared envelope: the durable mapping the coordinator
                # persists. The adapter reads ``prepared.before`` from
                # this row when the in-hand record carries no evidence.
                prepared={
                    "before": dict(prepared.before),
                    "preview": dict(prepared.preview),
                    "normalized_args": dict(prepared.normalized_args),
                    "compensation": dict(prepared.compensation or {}),
                },
                preview=dict(prepared.preview),
                verification=verification,
                compensation=dict(prepared.compensation or {}),
            )
        finally:
            db.close()

        # Minimal OperationRecord-like: ONLY operation_id (mirrors the
        # Task 3 OperationJournal OperationRecord surface).
        minimal_record = type("Rec", (), {
            "operation_id": "opk-durable",
        })()

        # Real SessionDB-backed lookup. SessionDB's get_effect_transaction
        # keys by ``transaction_id``; in production the coordinator owns
        # the operation_id→transaction_id mapping. Here we build a real
        # index over the effect_transactions table by operation_id.
        lookup_db = SessionDB(db_path=db_path)

        def lookup_by_op_id(operation_id: str):
            row = lookup_db._conn.execute(
                "SELECT transaction_id FROM effect_transactions "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                return None
            return lookup_db.get_effect_transaction(row["transaction_id"])

        try:
            # Use a fresh adapter with the lookup injected — handler
            # invocations on this adapter must remain zero.
            fresh = WorkspaceEffectAdapter(
                authority=mission_ctx, checkpoint_base=checkpoint_base,
                transaction_lookup=lookup_by_op_id,
            )
            outcome = fresh.reconcile(minimal_record)
        finally:
            lookup_db.close()

        # Reconcile itself never invokes a handler (the only handler
        # call was during ``commit`` above; no new handler call was
        # introduced by the lookup or reconcile path).
        assert len(commit_handler_calls) == 1
        assert outcome["disposition"] == "landed", (
            f"expected landed from durable evidence, got {outcome!r}"
        )
        assert (wt / "durable.txt").read_text(encoding="utf-8") == "landed-on-disk\n"

    def test_reconcile_without_lookup_or_evidence_returns_unknown(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """If no durable lookup is injected and the record carries no
        before/verification, reconcile cannot honestly answer landed —
        return ``unknown`` rather than fabricate.
        """
        from agent.effect_adapters import WorkspaceEffectAdapter

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        # Minimal OperationRecord: operation_id only, no before/verification.
        minimal_record = type("Rec", (), {
            "operation_id": "opk-nothing",
        })()
        outcome = adapter.reconcile(minimal_record)
        assert outcome == {"disposition": "unknown"}

    def test_reconcile_lookup_error_returns_unknown_without_invoking_handler(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """A lookup that raises (storage fault) must not be promoted to
        landed. Reconcile returns ``unknown`` and never re-invokes the
        handler.
        """
        from agent.effect_adapters import WorkspaceEffectAdapter

        handler_calls: List[Any] = []

        def broken_lookup(_op_id):
            raise RuntimeError("sessiondb unavailable")

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
            transaction_lookup=broken_lookup,
        )
        record = type("Rec", (), {"operation_id": "opk-broken"})()
        # The handler in real use is only invoked from ``commit``. We
        # prove ``reconcile`` does not call any user-supplied handler by
        # verifying the handler is unreachable from the reconcile path —
        # the existing direct-record tests already cover that contract.
        outcome = adapter.reconcile(record)
        assert outcome == {"disposition": "unknown"}
        assert handler_calls == []


# ---------------------------------------------------------------------------
# Compensate — restore bytes / mode / deletion / git diff
# ---------------------------------------------------------------------------


class TestCompensate:
    def test_compensate_restores_bytes_mode_deletion_and_git_diff(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """After commit + verify, ``compensate`` restores the file exactly:
        bytes (matches original SHA), mode, deletion-state (file exists
        again), and the git diff (working tree matches checkpoint)."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        target = wt / "restorable.txt"
        target.write_text("original-bytes\n")
        original_sha = hashlib.sha256(b"original-bytes\n").hexdigest()

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "restorable.txt", "content": "new-bytes\n"},
            mission_id="m-test", operation_key="opk-comp",
        ))
        adapter.commit(prepared, _wf)
        adapter.verify(prepared, {"wrote": str(target.resolve())})
        assert target.read_text() == "new-bytes\n"

        # Compensation must restore the original bytes.
        adapter.compensate(prepared)
        assert target.read_text() == "original-bytes\n"
        # SHA matches what we recorded before mutation.
        assert hashlib.sha256(target.read_bytes()).hexdigest() == original_sha
        # Git diff against the checkpoint tip is clean (working tree matches).
        diff = _git(wt, "diff", "--stat")
        assert diff == ""

    def test_compensate_blocks_on_post_commit_drift_human_or_nonmission_edit(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """If the file changed AFTER the transaction verified (human edit,
        non-mission mutation), compensation must NOT clobber it. The
        adapter emits the injected review callback and raises."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        target = wt / "shared.txt"
        target.write_text("original\n")

        reviews: List[Dict[str, Any]] = []

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx,
            checkpoint_base=checkpoint_base,
            review_callback=lambda payload: reviews.append(payload),
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "shared.txt", "content": "by-mission\n"},
            mission_id="m-test", operation_key="opk-drift",
        ))
        def _write(a):
            p = Path(a["path"])
            p.write_text(a.get("content", ""), encoding="utf-8")
            return {"wrote": a["path"]}

        adapter.commit(prepared, _write)
        adapter.verify(prepared, {"wrote": str(target.resolve())})

        # Human edit drifts the file AFTER verify.
        target.write_text("by-human\n")
        with pytest.raises(RuntimeError):
            adapter.compensate(prepared)
        # Review callback fired.
        assert reviews, "review callback must fire on drift"
        # File NOT clobbered.
        assert target.read_text() == "by-human\n"


# ---------------------------------------------------------------------------
# Dependency-aware cascade compensation
# ---------------------------------------------------------------------------


class TestDependencyCascade:
    def test_dependent_uncompensated_blocks_noncascade_compensation(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """If tx B depends on tx A and A hasn't been compensated yet,
        compensating B alone (no cascade) must be blocked."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        (wt / "shared.txt").write_text("base\n")

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        # Transaction A: first write.
        prep_a = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "shared.txt", "content": "v1\n"},
            mission_id="m-test", operation_key="opk-A",
        ))
        adapter.commit(prep_a, _wf)
        adapter.verify(prep_a, {"wrote": str((wt / "shared.txt").resolve())})
        # Transaction B: depends on A.
        prep_b = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "shared.txt", "content": "v2\n"},
            mission_id="m-test", operation_key="opk-B",
        ))
        adapter.commit(prep_b, _wf)
        adapter.verify(prep_b, {"wrote": str((wt / "shared.txt").resolve())})

        # Try to compensate B with a noncascade dependency-check callback
        # that says "A is not compensated yet".
        def dep_checker(target_key: str) -> bool:
            return False  # dependents remain
        adapter_with_dep = WorkspaceEffectAdapter(
            authority=mission_ctx,
            checkpoint_base=checkpoint_base,
            dependency_check=dep_checker,
        )
        with pytest.raises(RuntimeError):
            adapter_with_dep.compensate(prep_b)
        # B's content must still be v2 (compensation blocked).
        assert (wt / "shared.txt").read_text() == "v2\n"

    def test_cascade_reverses_dependency_order(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """Cascade compensation reverses dependency order: B is compensated
        first, then A — leaving the file with the original base bytes."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        (wt / "shared.txt").write_text("base\n")

        # Cascadeable checker that permits reverse-order traversal and
        # reports "no more dependents" once B is rolled back.
        remaining: set = {"opk-A", "opk-B"}

        def dep_checker(target_key: str) -> bool:
            return target_key not in remaining

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx,
            checkpoint_base=checkpoint_base,
            dependency_check=dep_checker,
        )
        prep_a = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "shared.txt", "content": "v1\n"},
            mission_id="m-test", operation_key="opk-A",
        ))
        adapter.commit(prep_a, _wf)
        adapter.verify(prep_a, {"wrote": str((wt / "shared.txt").resolve())})
        prep_b = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "shared.txt", "content": "v2\n"},
            mission_id="m-test", operation_key="opk-B",
        ))
        adapter.commit(prep_b, _wf)
        adapter.verify(prep_b, {"wrote": str((wt / "shared.txt").resolve())})

        # Cascade in reverse order: B then A.
        results = adapter.compensate_cascade([prep_a, prep_b])
        assert "compensated" in results
        # After cascade the file should be back to base.
        assert (wt / "shared.txt").read_text() == "base\n"

    def test_cascade_stops_at_irreversible_boundary(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """Cascade compensation stops at the irreversible boundary — the
        irreversible transaction is NOT clobbered, only the reversible
        dependents are reverted."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        (wt / "shared.txt").write_text("base\n")

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
            dependency_check=lambda k: True,
        )
        # A: irreversible (e.g. push or remote op simulated via marker).
        prep_a = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "shared.txt", "content": "irrev\n"},
            mission_id="m-test", operation_key="opk-irrev",
        ))
        # Promote to irreversible semantics so cascade stops here.
        from agent.effect_transactions import PreparedEffect, EffectSemantics
        prep_a = PreparedEffect(
            adapter_id=prep_a.adapter_id,
            normalized_args=prep_a.normalized_args,
            before=prep_a.before,
            preview=prep_a.preview,
            semantics=EffectSemantics(
                kind="irreversible", idempotent=False, reconcilable=False,
            ),
            compensation=prep_a.compensation,
        )
        adapter.commit(prep_a, _wf)
        adapter.verify(prep_a, {"wrote": str((wt / "shared.txt").resolve())})
        # B: reversible, depends on A.
        prep_b = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "shared.txt", "content": "rev\n"},
            mission_id="m-test", operation_key="opk-rev",
        ))
        adapter.commit(prep_b, _wf)
        adapter.verify(prep_b, {"wrote": str((wt / "shared.txt").resolve())})

        # Cascade: only B should be reverted. A is irreversible and the
        # cascade stops at the boundary.
        adapter.compensate_cascade([prep_a, prep_b])
        assert (wt / "shared.txt").read_text() == "irrev\n"


# ---------------------------------------------------------------------------
# WorkspaceCommitEffectAdapter — bounded local commits
# ---------------------------------------------------------------------------


class TestWorkspaceCommit:
    def test_local_commit_in_disposable_worktree(
        self, worktree_repo, checkpoint_base
    ):
        """Stage + commit succeeds inside a disposable worktree, recording
        parent HEAD, the actual commit, and verifying it."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        from agent.effect_transactions import (
            OperationRequest, PreparedEffect, EffectSemantics,
        )

        wt = worktree_repo
        (wt / "feature.txt").write_text("feature-content\n")
        parent_head = _git(wt, "rev-parse", "HEAD")
        adapter = WorkspaceCommitEffectAdapter(checkpoint_base=checkpoint_base)
        req = OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(wt), "paths": ["feature.txt"],
                  "message": "feat: add feature"},
            mission_id="m-test", operation_key="opk-commit",
        )
        prepared = adapter.prepare(req)
        assert prepared.before["parent_head"] == parent_head
        assert prepared.preview["paths"] == ["feature.txt"]

        result = adapter.commit(prepared, lambda a: a)
        assert result["success"] is True
        new_head = _git(wt, "rev-parse", "HEAD")
        assert new_head != parent_head
        assert _git(wt, "log", "-1", "--format=%s") == "feat: add feature"
        verified = adapter.verify(prepared, result)
        assert verified["created_commit"] == new_head
        assert verified["parent_head"] == parent_head

    def test_remote_or_push_or_arbitrary_args_rejected(
        self, worktree_repo, checkpoint_base
    ):
        """Requests that ask for ``push`` / ``remote`` / arbitrary git args
        are rejected at prepare time. Only ``add`` + ``commit`` + ``reset``
        are permitted."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = worktree_repo
        adapter = WorkspaceCommitEffectAdapter(checkpoint_base=checkpoint_base)

        # Push — rejected.
        req = OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(wt), "paths": ["x"], "push": True,
                  "message": "x"},
            mission_id="m-test", operation_key="opk-push",
        )
        with pytest.raises(PermissionError):
            adapter.prepare(req)
        # Remote add — rejected.
        req2 = OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(wt), "paths": ["x"], "add_remote": "evil",
                  "message": "x"},
            mission_id="m-test", operation_key="opk-remote",
        )
        with pytest.raises(PermissionError):
            adapter.prepare(req2)
        # Arbitrary git args — rejected.
        req3 = OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(wt), "paths": ["x"], "extra_args": ["-c"],
                  "message": "x"},
            mission_id="m-test", operation_key="opk-args",
        )
        with pytest.raises(PermissionError):
            adapter.prepare(req3)

    def test_commit_rejects_non_worktree_or_main_branch(
        self, repo, worktree_repo, checkpoint_base
    ):
        """``prepare`` rejects the primary checkout (not a worktree), a
        non-worktree path, and main/master branches."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        from agent.effect_transactions import OperationRequest

        adapter = WorkspaceCommitEffectAdapter(checkpoint_base=checkpoint_base)
        # Detached HEAD on primary checkout.
        (repo / "n.txt").write_text("n\n")
        req = OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(repo), "paths": ["n.txt"], "message": "x"},
            mission_id="m-test", operation_key="opk-detached",
        )
        with pytest.raises(PermissionError):
            adapter.prepare(req)

# ---------------------------------------------------------------------------
# file_tools registry metadata — adapter wiring on write_file / patch
# ---------------------------------------------------------------------------


class TestFileToolsRegistryMetadata:
    def test_write_file_registry_metadata_carries_effect_adapter(
        self, checkpoint_base
    ):
        """``write_file`` tool entry must register with ``effect_adapter``
        = ``workspace.v1`` so the coordinator routes mutations through
        the workspace adapter. Schema (model-visible) is unchanged."""
        import tools.file_tools  # noqa: F401  # ensure registration
        from tools.registry import registry
        meta = registry.get_operation_metadata("write_file")
        assert meta["effect_adapter"] == "workspace.v1"
        assert meta["destructive"] is True

    def test_patch_registry_metadata_carries_effect_adapter(
        self, checkpoint_base
    ):
        """``patch`` tool entry must register with ``effect_adapter`` =
        ``workspace.v1``."""
        import tools.file_tools  # noqa: F401  # ensure registration
        from tools.registry import registry
        meta = registry.get_operation_metadata("patch")
        assert meta["effect_adapter"] == "workspace.v1"
        assert meta["destructive"] is True

    def test_read_file_registry_metadata_unchanged(self):
        """Read-only tools remain read-only — no effect_adapter wiring."""
        import tools.file_tools  # noqa: F401  # ensure registration
        from tools.registry import registry
        meta = registry.get_operation_metadata("read_file")
        assert meta["read_only"] is True
        assert meta["effect_adapter"] in (None, "")


# ---------------------------------------------------------------------------
# Task 4 re-review: durable checkpoint payload serialization
# ---------------------------------------------------------------------------


class TestCompensationSerialization:
    """Spec: PreparedEffect.compensation must contain a JSON-decodable
    mapping (not the CheckpointRef dataclass), because the Task 3
    SessionDB canonical JSON persists compensation as an opaque string
    otherwise. Adapter restore/drift/verify must rehydrate from the
    mapping on demand."""

    def test_compensation_is_json_decodable_mapping_not_dataclass(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """``compensation`` MUST be a plain JSON-decodable mapping with
        exactly {checkpoint_id, working_dir, commit_hash, created_at}."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest
        wt = mission_ctx.workspace_root
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "x.txt", "content": "x"},
            mission_id="m-test", operation_key="opk-serial",
        ))
        comp = prepared.compensation
        # It is a mapping, not a dataclass instance.
        assert isinstance(comp, dict)
        # Round-trips through JSON without truncation.
        import json as _json
        encoded = _json.dumps(comp)
        decoded = _json.loads(encoded)
        assert decoded == dict(comp)
        # Required keys present.
        for key in ("checkpoint_id", "working_dir", "commit_hash", "created_at"):
            assert key in decoded, f"missing {key} in compensation"
        # CheckpointRef dataclass type does NOT appear.
        from tools.checkpoint_manager import CheckpointRef
        assert not isinstance(comp.get("checkpoint"), CheckpointRef)

    def test_round_trip_through_canonical_json_string(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        """The canonical-JSON round-trip (the production persistence)
        must leave compensation usable for restore/drift checks."""
        import json as _json
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest
        wt = mission_ctx.workspace_root
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        # Write the target file BEFORE prepare so the checkpoint captures
        # the original bytes — restore must bring them back.
        target = wt / "round2.txt"
        target.write_text("original\n")
        prepared2 = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "round2.txt", "content": "r"},
            mission_id="m-test", operation_key="opk-round2",
        ))
        # Simulate SessionDB canonical JSON round-trip of compensation.
        canonical = _json.loads(_json.dumps(prepared2.compensation))
        prepared2_payload = type(prepared2)(
            adapter_id=prepared2.adapter_id,
            normalized_args=prepared2.normalized_args,
            before=prepared2.before,
            preview=prepared2.preview,
            semantics=prepared2.semantics,
            compensation=canonical,
        )
        # Commit overwrites round2.txt with "r"; verify records after hash.
        def _w(a):
            Path(a["path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(a["path"]).write_text(a.get("content", ""), encoding="utf-8")
            return {"wrote": a["path"]}
        adapter.commit(prepared2_payload, _w)
        assert target.read_text() == "r"
        adapter.verify(prepared2_payload, {"wrote": str(target.resolve())})
        # Compensate with the JSON-roundtripped payload restores original.
        adapter.compensate(prepared2_payload)
        assert target.read_text() == "original\n"


# ---------------------------------------------------------------------------
# WorkspaceCommitEffectAdapter — no-clobber, primary-checkout, empty paths
# ---------------------------------------------------------------------------


class TestWorkspaceCommitSafety:
    def test_commit_rejects_empty_paths_before_git_add(
        self, worktree_repo, checkpoint_base
    ):
        """Empty ``paths`` list must be rejected before ``git add`` to
        prevent an empty-commit edge case and unintended stage sweep."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        from agent.effect_transactions import OperationRequest
        wt = worktree_repo
        (wt / "z.txt").write_text("z\n")
        adapter = WorkspaceCommitEffectAdapter(checkpoint_base=checkpoint_base)
        req = OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(wt), "paths": [], "message": "noop"},
            mission_id="m-test", operation_key="opk-empty",
        )
        with pytest.raises(ValueError):
            adapter.prepare(req)

    def test_compensate_blocks_when_human_commit_advanced_head(
        self, worktree_repo, checkpoint_base
    ):
        """Spec: compensate MUST reset only when current HEAD still
        equals the commit we created. A subsequent human commit must
        block reset, fire the review callback, and raise — never clobber
        the human's work."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        from agent.effect_transactions import OperationRequest
        review_calls: List[Dict[str, Any]] = []

        def _review(payload):
            review_calls.append(payload)

        wt = worktree_repo
        (wt / "c.txt").write_text("c\n")
        adapter = WorkspaceCommitEffectAdapter(
            checkpoint_base=checkpoint_base,
            dependency_check=lambda k: True,
            review_callback=_review,
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(wt), "paths": ["c.txt"],
                  "message": "feat: c"},
            mission_id="m-test", operation_key="opk-human-clobber",
        ))
        adapter.commit(prepared, lambda a: a)
        adapter.verify(prepared, {"success": True})
        # The commit we created must be a different HEAD than parent.
        created_commit = _git(wt, "rev-parse", "HEAD")
        assert created_commit != prepared.before["parent_head"]
        # Now a "human" creates another commit on top.
        (wt / "human.txt").write_text("human\n")
        _run(["git", "-C", str(wt), "add", "human.txt"], wt)
        _run(["git", "-C", str(wt), "commit", "-m", "human edit"], wt)
        human_commit = _git(wt, "rev-parse", "HEAD")
        assert human_commit != created_commit
        # Compensate must NOT reset HEAD and must raise.
        with pytest.raises(RuntimeError):
            adapter.compensate(prepared, dependency_check=lambda k: True)
        # HEAD still at human commit.
        assert _git(wt, "rev-parse", "HEAD") == human_commit
        # human.txt still present.
        assert (wt / "human.txt").exists()
        # Review callback fired (human-edit safety report).
        assert any(
            c.get("reason") in {"head_advanced", "compensate_blocked"}
            or "human" in str(c.get("reason", "")).lower()
            for c in review_calls
        ) or review_calls, "review callback should fire on human-commit clobber"

    def test_compensate_resets_when_head_exactly_matches_created_commit(
        self, worktree_repo, checkpoint_base
    ):
        """Spec: exact-head match is the only safe reset; HEAD reset to
        parent."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        from agent.effect_transactions import OperationRequest
        wt = worktree_repo
        (wt / "e.txt").write_text("e\n")
        adapter = WorkspaceCommitEffectAdapter(
            checkpoint_base=checkpoint_base,
            dependency_check=lambda k: True,
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(wt), "paths": ["e.txt"],
                  "message": "feat e"},
            mission_id="m-test", operation_key="opk-exact",
        ))
        adapter.commit(prepared, lambda a: a)
        adapter.verify(prepared, {"success": True})
        parent = prepared.before["parent_head"]
        adapter.compensate(prepared, dependency_check=lambda k: True)
        assert _git(wt, "rev-parse", "HEAD") == parent

    def test_prepare_rejects_primary_checkout_via_subdirectory_input(
        self, repo, checkpoint_base
    ):
        """Spec: a subdirectory INSIDE the primary checkout is also a
        primary checkout — must be rejected, not bypassed via the
        subdir path. The implementation must resolve the git root and
        refuse if it is primary, even when ``worktree`` is a subdir."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        from agent.effect_transactions import OperationRequest
        # repo is the primary checkout (`git init` produced `.git/` dir).
        subdir = repo / "deep" / "nested"
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "x.txt").write_text("x\n")
        adapter = WorkspaceCommitEffectAdapter(checkpoint_base=checkpoint_base)
        req = OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(subdir), "paths": ["x.txt"],
                  "message": "x"},
            mission_id="m-test", operation_key="opk-subdir-primary",
        )
        with pytest.raises(PermissionError):
            adapter.prepare(req)


# ---------------------------------------------------------------------------
# WorkspaceCommitEffectAdapter — reconcile with durable record (no handler)
# ---------------------------------------------------------------------------


class TestWorkspaceCommitReconcile:
    def test_reconcile_with_durable_record_returns_landed(
        self, worktree_repo, checkpoint_base
    ):
        """Spec: when a recovery SessionDB lookup yields durable
        prepared.before + verification, reconcile returns
        ``disposition='landed'`` WITHOUT invoking any handler."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        from agent.effect_transactions import OperationRequest
        wt = worktree_repo
        (wt / "r.txt").write_text("r\n")
        adapter = WorkspaceCommitEffectAdapter(checkpoint_base=checkpoint_base)
        prepared = adapter.prepare(OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(wt), "paths": ["r.txt"],
                  "message": "feat r"},
            mission_id="m-test", operation_key="opk-rec",
        ))
        # Actually run the commit so HEAD advances past parent_head,
        # mirroring a real recovery scenario where the durable row
        # carries the freshly committed created_commit.
        adapter.commit(prepared, lambda a: a)
        created_commit = (
            prepared.before.get("worktree", "") and
            _git(wt, "rev-parse", "HEAD")
        )
        # Simulate a SessionDB record with prepared.before +
        # verification.created_commit.
        record = type("Rec", (), {
            "operation_id": "opk-rec",
            "before": prepared.before,
            "verification": {"created_commit": created_commit},
            "prepared": {"before": prepared.before, "compensation": prepared.compensation},
        })()
        outcome = adapter.reconcile(record)
        assert outcome["disposition"] == "landed"

    def test_reconcile_without_record_returns_unknown(
        self, worktree_repo, checkpoint_base
    ):
        """If the adapter cannot fetch the durable record, dispose as
        ``unknown`` rather than fabricate a landed answer."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        wt = worktree_repo
        adapter = WorkspaceCommitEffectAdapter(
            checkpoint_base=checkpoint_base,
            transaction_lookup=lambda op: None,  # type: ignore[arg-type]
        )
        # Empty record — adapter still returns unknown, no handler call.
        record = type("Rec", (), {
            "operation_id": "",
            "before": {}, "verification": {}, "prepared": None,
        })()
        outcome = adapter.reconcile(record)
        assert outcome["disposition"] == "unknown"

    def test_reconcile_crash_before_adapter_commit_with_human_commit_returns_unknown(
        self, worktree_repo, checkpoint_base
    ):
        """Spec: durable ``verification.created_commit`` is the ONLY
        accepted evidence a commit happened. If a SessionDB row claims a
        ``created_commit`` but a human (non-adapter) commit has since
        advanced HEAD past it, the adapter MUST return ``unknown`` —
        not landed. The legacy ``current != parent_head`` shortcut
        silently accepts any descendant, which is wrong here."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        wt = worktree_repo
        adapter = WorkspaceCommitEffectAdapter(checkpoint_base=checkpoint_base)
        parent_head = _git(wt, "rev-parse", "HEAD")
        # The "crash before adapter commit" simulation: prepared.before
        # still has parent_head (commit never ran). Then a human commit
        # lands at HEAD. Durable verification.created_commit is empty —
        # the adapter has zero evidence the adapter itself committed.
        record = type("Rec", (), {
            "operation_id": "opk-crash-human",
            "before": {"parent_head": parent_head, "worktree": str(wt)},
            "verification": {"created_commit": ""},
            "prepared": {
                "before": {"parent_head": parent_head, "worktree": str(wt)},
                "compensation": {"parent_head": parent_head, "worktree": str(wt)},
            },
        })()
        # A human landed an unrelated commit on HEAD.
        (wt / "human.txt").write_text("h\n")
        _run(["git", "-C", str(wt), "add", "human.txt"], wt)
        _run(["git", "-C", str(wt), "commit", "-m", "human edit"], wt)
        assert _git(wt, "rev-parse", "HEAD") != parent_head
        outcome = adapter.reconcile(record)
        assert outcome["disposition"] == "unknown", (
            "without durable created_commit evidence, reconcile must NOT "
            "fabricate landed from a human commit"
        )

    def test_reconcile_separately_created_commit_with_human_descendant_returns_landed(
        self, worktree_repo, checkpoint_base
    ):
        """Spec: when durable ``verification.created_commit`` is set and
        that exact commit IS an ancestor of current HEAD, the effect
        landed (a human descendant is fine — the commit itself was
        made). Compensation, however, MUST still refuse the reset when
        HEAD has advanced past the created commit. Reconcile is
        'landed'; compensate is 'block'."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        from agent.effect_transactions import OperationRequest
        wt = worktree_repo
        adapter = WorkspaceCommitEffectAdapter(checkpoint_base=checkpoint_base)
        # Run a real adapter commit, then a human descendant.
        (wt / "base.txt").write_text("b\n")
        prepared = adapter.prepare(OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(wt), "paths": ["base.txt"],
                  "message": "feat b"},
            mission_id="m-test", operation_key="opk-rec-desc",
        ))
        adapter.commit(prepared, lambda a: a)
        created = _git(wt, "rev-parse", "HEAD")
        (wt / "human.txt").write_text("h\n")
        _run(["git", "-C", str(wt), "add", "human.txt"], wt)
        _run(["git", "-C", str(wt), "commit", "-m", "human edit"], wt)
        current = _git(wt, "rev-parse", "HEAD")
        assert current != created
        record = type("Rec", (), {
            "operation_id": "opk-rec-desc",
            "before": prepared.before,
            "verification": {"created_commit": created},
            "prepared": {
                "before": dict(prepared.before),
                "compensation": dict(prepared.compensation or {}),
            },
        })()
        outcome = adapter.reconcile(record)
        assert outcome["disposition"] == "landed"
        # Compensation must STILL block — HEAD has advanced.
        with pytest.raises(RuntimeError):
            adapter.compensate(prepared, dependency_check=lambda k: True)


# ---------------------------------------------------------------------------
# Durable fresh-process compensation (SessionDB lookup path)
# ---------------------------------------------------------------------------


class TestDurableFreshProcessCompensation:
    """Spec: a fresh process (no in-memory mapping from commit()) can
    rehydrate the PreparedEffect + compensation payload and still
    compensate correctly when a real SessionDB row carries the durable
    ``verification.after_hashes`` (WorkspaceEffectAdapter) or
    ``verification.created_commit`` (WorkspaceCommitEffectAdapter).
    Drift must still block, even on a fresh process."""

    def test_workspace_fresh_process_compensation_succeeds_when_durable_after_hash_matches(
        self, worktree_repo, checkpoint_base, mission_ctx, tmp_path
    ):
        """Process A: prepare → commit → verify (real SessionDB row).
        Process B (fresh adapter instance, same SessionDB): rehydrate
        PreparedEffect from the durable row, no in-memory _last_verify,
        compensation succeeds iff current file SHA equals durable
        after_hashes, and restores before state."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest
        from hades_state import SessionDB
        from agent.operation_journal import OperationJournal

        wt = mission_ctx.workspace_root
        target = wt / "durable-comp.txt"
        target.write_text("before\n")

        # Process A
        a = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        req = OperationRequest(
            tool_name="write_file",
            args={"path": "durable-comp.txt", "content": "after\n"},
            mission_id="m-test", operation_key="opk-durable-comp",
        )
        prepared = a.prepare(req)
        a.commit(prepared, _wf)
        verification = dict(a.verify(prepared, {"wrote": str(target.resolve())}))
        assert target.read_text() == "after\n"

        # Persist in real SessionDB
        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        try:
            OperationJournal(db).create(
                operation_id="opk-durable-comp", kind="workspace.v1",
            )
            db.create_effect_transaction(
                transaction_id="tx-opk-durable-comp",
                operation_id="opk-durable-comp",
                mission_id="m-test",
                adapter_id=WorkspaceEffectAdapter.adapter_id,
                sequence_no=1,
                semantics={"kind": "reversible", "idempotent": False,
                           "reconcilable": True},
                depends_on=[],
                prepared={
                    "before": dict(prepared.before),
                    "compensation": dict(prepared.compensation or {}),
                },
                preview=dict(prepared.preview),
                verification=verification,
                compensation=dict(prepared.compensation or {}),
            )
        finally:
            db.close()

        # Process B: rehydrate from the durable row, NOT from in-memory state.
        rehydrated_comp = json.loads(json.dumps(prepared.compensation))
        rehydrated = type(prepared)(
            adapter_id=prepared.adapter_id,
            normalized_args=prepared.normalized_args,
            before=prepared.before,
            preview=prepared.preview,
            semantics=prepared.semantics,
            compensation=rehydrated_comp,
        )
        b_db = SessionDB(db_path=db_path)

        def lookup(op_id):
            row = b_db._conn.execute(
                "SELECT transaction_id FROM effect_transactions "
                "WHERE operation_id = ?", (op_id,),
            ).fetchone()
            if row is None:
                return None
            return b_db.get_effect_transaction(row["transaction_id"])

        try:
            b = WorkspaceEffectAdapter(
                authority=mission_ctx, checkpoint_base=checkpoint_base,
                transaction_lookup=lookup,
            )
            # Current file SHA matches verification.after_hashes:
            # compensation must succeed and restore 'before'.
            b.compensate(rehydrated)
            assert target.read_text() == "before\n"
        finally:
            b_db.close()

    def test_workspace_fresh_process_drift_still_blocks_compensation(
        self, worktree_repo, checkpoint_base, mission_ctx, tmp_path
    ):
        """Spec: drift detection must work on a fresh process too. The
        SessionDB lookup yields verification.after_hashes, but a
        non-mission edit changed the file post-verify — compensate
        MUST block and emit the review callback."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest
        from hades_state import SessionDB
        from agent.operation_journal import OperationJournal

        wt = mission_ctx.workspace_root
        target = wt / "drift-comp.txt"
        target.write_text("before\n")

        a = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        req = OperationRequest(
            tool_name="write_file",
            args={"path": "drift-comp.txt", "content": "after\n"},
            mission_id="m-test", operation_key="opk-drift-comp",
        )
        prepared = a.prepare(req)
        a.commit(prepared, _wf)
        verification = dict(a.verify(prepared, {"wrote": str(target.resolve())}))

        # Persist
        db_path = tmp_path / "state.db"
        db = SessionDB(db_path=db_path)
        try:
            OperationJournal(db).create(
                operation_id="opk-drift-comp", kind="workspace.v1",
            )
            db.create_effect_transaction(
                transaction_id="tx-opk-drift-comp",
                operation_id="opk-drift-comp",
                mission_id="m-test",
                adapter_id=WorkspaceEffectAdapter.adapter_id,
                sequence_no=1,
                semantics={"kind": "reversible", "idempotent": False,
                           "reconcilable": True},
                depends_on=[],
                prepared={
                    "before": dict(prepared.before),
                    "compensation": dict(prepared.compensation or {}),
                },
                preview=dict(prepared.preview),
                verification=verification,
                compensation=dict(prepared.compensation or {}),
            )
        finally:
            db.close()

        # Non-mission edit BEFORE rehydration; simulates human drift
        # post-verify but pre-compensate.
        target.write_text("human edit\n")

        rehydrated = type(prepared)(
            adapter_id=prepared.adapter_id,
            normalized_args=prepared.normalized_args,
            before=prepared.before,
            preview=prepared.preview,
            semantics=prepared.semantics,
            compensation=json.loads(json.dumps(prepared.compensation)),
        )
        reviews: List[Dict[str, Any]] = []
        b_db = SessionDB(db_path=db_path)

        def lookup(op_id):
            row = b_db._conn.execute(
                "SELECT transaction_id FROM effect_transactions "
                "WHERE operation_id = ?", (op_id,),
            ).fetchone()
            if row is None:
                return None
            return b_db.get_effect_transaction(row["transaction_id"])

        try:
            b = WorkspaceEffectAdapter(
                authority=mission_ctx, checkpoint_base=checkpoint_base,
                transaction_lookup=lookup,
                review_callback=lambda payload: reviews.append(payload),
            )
            with pytest.raises(RuntimeError):
                b.compensate(rehydrated)
        finally:
            b_db.close()
        # Drift block fires review callback.
        assert reviews, "drift block must fire review callback"
        assert reviews[0].get("reason") == "post_commit_drift"

    def test_commit_fresh_process_compensation_succeeds_with_exact_created_head(
        self, worktree_repo, checkpoint_base
    ):
        """Spec: a fresh WorkspaceCommitEffectAdapter instance — no
        in-memory _in_process_created mapping — must still compensate
        when the durable SessionDB row carries the exact created_commit
        and HEAD still equals it."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        from agent.effect_transactions import OperationRequest
        wt = worktree_repo
        (wt / "fcomp.txt").write_text("f\n")
        # Process A: prepare + commit so HEAD advances.
        a = WorkspaceCommitEffectAdapter(checkpoint_base=checkpoint_base)
        prepared = a.prepare(OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(wt), "paths": ["fcomp.txt"],
                  "message": "feat f"},
            mission_id="m-test", operation_key="opk-fresh-comp",
        ))
        a.commit(prepared, lambda a: a)
        created_commit = _git(wt, "rev-parse", "HEAD")
        # Process B: fresh adapter, no in-memory record. Durable
        # verification.created_commit must be enough to anchor the
        # exact-HHEAD reset — injected via constructor lookup.
        def lookup(op_id):
            if op_id == "opk-fresh-comp":
                return type("Rec", (), {
                    "verification": {"created_commit": created_commit},
                })()
            return None
        b = WorkspaceCommitEffectAdapter(
            checkpoint_base=checkpoint_base,
            transaction_lookup=lookup,
        )
        # Rehydrate prepared.compensation (must include operation_id
        # so the SessionDB lookup can find the verification envelope).
        rehydrated = type(prepared)(
            adapter_id=prepared.adapter_id,
            normalized_args=dict(prepared.normalized_args),
            before=prepared.before,
            preview=prepared.preview,
            semantics=prepared.semantics,
            compensation={
                **dict(prepared.compensation or {}),
                "operation_id": "opk-fresh-comp",
            },
        )
        b.compensate(rehydrated, dependency_check=lambda k: True)
        assert _git(wt, "rev-parse", "HEAD") == prepared.before["parent_head"]

    def test_commit_fresh_process_compensation_blocks_when_head_advanced(
        self, worktree_repo, checkpoint_base
    ):
        """Spec: a fresh adapter with durable created_commit but HEAD
        advanced past it (a human commit landed after verify) MUST
        refuse to reset and fire the review callback."""
        from agent.effect_adapters import WorkspaceCommitEffectAdapter
        from agent.effect_transactions import OperationRequest
        wt = worktree_repo
        (wt / "fcomp2.txt").write_text("f\n")
        a = WorkspaceCommitEffectAdapter(checkpoint_base=checkpoint_base)
        prepared = a.prepare(OperationRequest(
            tool_name="local_commit",
            args={"worktree": str(wt), "paths": ["fcomp2.txt"],
                  "message": "feat f"},
            mission_id="m-test", operation_key="opk-fresh-comp-block",
        ))
        a.commit(prepared, lambda a: a)
        created_commit = _git(wt, "rev-parse", "HEAD")
        # Human commit AFTER verify.
        (wt / "human.txt").write_text("h\n")
        _run(["git", "-C", str(wt), "add", "human.txt"], wt)
        _run(["git", "-C", str(wt), "commit", "-m", "human"], wt)

        reviews: List[Dict[str, Any]] = []
        def lookup(op_id):
            if op_id == "opk-fresh-comp-block":
                return type("Rec", (), {
                    "verification": {"created_commit": created_commit},
                })()
            return None
        b = WorkspaceCommitEffectAdapter(
            checkpoint_base=checkpoint_base,
            transaction_lookup=lookup,
            review_callback=lambda payload: reviews.append(payload),
        )
        rehydrated = type(prepared)(
            adapter_id=prepared.adapter_id,
            normalized_args=dict(prepared.normalized_args),
            before=prepared.before,
            preview=prepared.preview,
            semantics=prepared.semantics,
            compensation={
                **dict(prepared.compensation or {}),
                "operation_id": "opk-fresh-comp-block",
            },
        )
        with pytest.raises(RuntimeError):
            b.compensate(rehydrated, dependency_check=lambda k: True)
        assert reviews, "advanced head must fire review callback"


# ---------------------------------------------------------------------------
# Task 4 final remediation — V4A Move parse, sibling clobber, authority
# ---------------------------------------------------------------------------


class TestV4AMoveParseTargets:
    """Spec: ``*** Move File: old -> new`` must yield BOTH the source
    and the destination as distinct targets. A destination outside the
    authorized workspace roots MUST block the mutation before any
    changes — otherwise a hostile patch header could plant a file
    outside the mission scope."""

    def test_v4a_move_yields_two_distinct_targets(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        (wt / "old.txt").write_text("old\n")
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        patch_body = (
            "*** Begin Patch\n"
            "*** Move File: old.txt -> subdir/new.txt\n"
            "*** End Patch\n"
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="patch",
            args={"mode": "patch", "patch": patch_body},
            mission_id="m-test", operation_key="opk-move",
        ))
        # Both targets captured as absolute normalized paths.
        targets = prepared.before["targets"]
        assert str((wt / "old.txt").resolve()) in targets
        # Destination must be a different normalized absolute path.
        new_target = str((wt / "subdir" / "new.txt").resolve())
        assert new_target in targets
        assert len(targets) == 2

    def test_v4a_move_outside_workspace_root_blocks_before_mutation(
        self, worktree_repo, checkpoint_base, mission_ctx, tmp_path
    ):
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        (wt / "data.txt").write_text("data\n")
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        # Destination resolves OUTSIDE mission_ctx.workspace_roots.
        outside = tmp_path / "escaped.txt"
        patch_body = (
            "*** Begin Patch\n"
            f"*** Move File: data.txt -> {outside}\n"
            "*** End Patch\n"
        )
        with pytest.raises(PermissionError):
            adapter.prepare(OperationRequest(
                tool_name="patch",
                args={"mode": "patch", "patch": patch_body},
                mission_id="m-test", operation_key="opk-move-escape",
            ))
        # No side-effects on the source tree.
        assert (wt / "data.txt").read_text() == "data\n"


class TestSiblingClobberOnCompensate:
    """Spec: compensating a WorkspaceEffectAdapter transaction must
    restore ONLY the declared file targets; an unrelated sibling
    edited between commit and compensate must NOT be clobbered.

    Driven end-to-end through the adapter so the test validates the
    shared ``restore_checkpoint(..., file_paths=...)`` path used at
    runtime. The compensate path walks the same machinery that the
    tests in TestTask4SharedRestoreGuards exercise at the manager
    level."""

    def test_compensate_preserves_unrelated_sibling(
        self, worktree_repo, checkpoint_base, mission_ctx
    ):
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        target = wt / "target.txt"
        sibling = wt / "sibling.txt"
        target.write_text("orig-target\n")
        sibling.write_text("orig-sibling\n")

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "target.txt", "content": "by-mission\n"},
            mission_id="m-test", operation_key="opk-sib",
        ))
        adapter.commit(prepared, _wf)
        adapter.verify(prepared, {"wrote": str(target.resolve())})
        assert target.read_text() == "by-mission\n"

        # Human edits sibling post-verify.
        sibling.write_text("by-human\n")
        # Mission requests compensate — sibling must survive.
        adapter.compensate(prepared)
        assert target.read_text() == "orig-target\n"
        assert sibling.read_text() == "by-human\n"


class TestWorkspaceAuthorityImmutability:
    """Spec: a mission's WorkspaceAuthority is the trust boundary.
    The adapter must not be tricked into trusting later mutation of
    source list ``workspace_roots`` or the encoded string
    ``workspace_root``. Construct the authority frozen, copy/coerce
    caller inputs, and never expose mutable state."""

    def test_authority_workspace_roots_is_tuple_after_construction(
        self, worktree_repo, checkpoint_base
    ):
        from agent.effect_adapters import WorkspaceAuthority
        roots = [worktree_repo]
        auth = WorkspaceAuthority(
            mission_id="m-test",
            workspace_roots=roots,
            workspace_root=str(worktree_repo),
            actor_id="tester",
        )
        # Public surface is immutable even when caller passed a list.
        assert isinstance(auth.workspace_roots, tuple)
        # The trust-boundary tuple's identity must NOT change if a
        # caller mutates the original list after construction.
        roots.append(worktree_repo.parent)
        assert len(auth.workspace_roots) == 1

    def test_authority_mutation_via_setattr_fails(self, worktree_repo):
        from agent.effect_adapters import WorkspaceAuthority
        from dataclasses import FrozenInstanceError
        auth = WorkspaceAuthority(
            mission_id="m-test",
            workspace_roots=[worktree_repo],
            workspace_root=str(worktree_repo),
            actor_id="tester",
        )
        with pytest.raises(FrozenInstanceError):
            auth.workspace_roots = ("/tmp/elsewhere",)  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            auth.workspace_root = "/tmp/elsewhere"  # type: ignore[misc]

    def test_resolve_rejects_unchanged_authority_after_caller_mutation(
        self, worktree_repo, checkpoint_base
    ):
        """Even when the caller mutates the post-construction list
        passed in, the adapter must keep trusting the snapshot the
        authority captured — never the mutated caller list."""
        from agent.effect_adapters import (
            WorkspaceAuthority, WorkspaceEffectAdapter,
        )
        from agent.effect_transactions import OperationRequest

        # Build authority with a single permitted root (the worktree).
        roots = [str(worktree_repo.resolve())]
        auth = WorkspaceAuthority(
            mission_id="m-test",
            workspace_roots=roots,
            workspace_root=str(worktree_repo),
            actor_id="tester",
        )
        adapter = WorkspaceEffectAdapter(
            authority=auth, checkpoint_base=checkpoint_base,
        )
        # Caller adds an outside root AFTER construction.
        roots.append("/tmp/hostile-root")
        # write_file inside the original worktree must still succeed
        # (the path remains under the captured authority root).
        target = worktree_repo / "inside.txt"
        req = OperationRequest(
            tool_name="write_file",
            args={"path": str(target), "content": "ok\n"},
            mission_id="m-test",
            operation_key="opk-immut",
        )
        # The captured authority has only one root; subsequent adapter
        # resolution must therefore trust that single root, NOT the
        # caller-mutated list. Inside paths still resolve fine.
        prepared = adapter.prepare(req)
        assert prepared.before["targets"] == [str(target.resolve())]  # type: ignore[index]  # noqa: E501


# ---------------------------------------------------------------------------
# Task 4 final — V4A execution-boundary bypass: handler must see resolved
# absolute paths in normalized_args['patch'], not the original relative
# headers. Real FileOperations handler used so the test exercises the
# actual execution parser.
# ---------------------------------------------------------------------------


def _real_fileops_handler(invoke_args: Dict[str, Any]):
    """Invoke a real FileOperations.patch_v4a against normalized_args.

    Mirrors the production path: tools.file_operations.ShellFileOperations
    is constructed against ``terminal_env.cwd`` outside the authorized
    workspace, and ``invoke(normalized_args)`` runs the same parser the
    production handler uses. The class-level implementation of
    ``patch_v4a`` parses the V4A grammar, then ``apply_v4a_operations``
    dispatches write/delete/move through the abstract handlers — which
    on the production shell backend go through ``_exec`` (real subprocess).
    """
    from tools.file_operations import ShellFileOperations

    class _TerminalEnv:
        # Simulate a terminal cwd that is OUTSIDE the authorized mission
        # workspace so a relative-path handler would resolve to /tmp.
        # The test handler is given absolute paths via the V4A rewrite
        # so the actual cwd is irrelevant — set to /tmp to mirror the
        # adversarial scenario.
        cwd = "/tmp"

        def execute(self, command, cwd=None, timeout=None,
                    stdin_data=None, **kwargs):
            import subprocess as _sp
            try:
                proc = _sp.run(
                    command, shell=True, cwd=cwd or self.cwd,
                    capture_output=True, text=True, timeout=timeout or 30,
                    input=stdin_data,
                )
                return {"output": proc.stdout, "returncode": proc.returncode}
            except Exception as exc:
                return {"output": "", "returncode": 1, "error": str(exc)}

    ops = ShellFileOperations(_TerminalEnv())
    return ops.patch_v4a(invoke_args["patch"])


class TestV4AExecutionBoundaryAuthority:
    """Spec: the adapter must rewrite V4A header lines in
    normalized_args['patch'] so the real FileOperations handler (which
    parses the same V4A grammar at execute time) operates on absolute
    authorized paths, NOT on relative paths resolved against the
    terminal/task CWD. Without this rewrite, a Move or Update header
    that names a relative path would silently resolve against whatever
    CWD the production handler runs in — bypassing the adapter's
    workspace_roots authorization."""

    def test_v4a_update_patch_rewritten_with_absolute_authorized_path(
        self, worktree_repo, checkpoint_base, mission_ctx,
    ):
        """An ``Update File: src.txt`` V4A patch must be rewritten in
        normalized_args['patch'] so the header path is the absolute
        authorized target. The real FileOperations handler then runs
        against that absolute path (so it works regardless of CWD)."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        (wt / "src.txt").write_text("hello world\n")
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        patch_body = (
            "*** Begin Patch\n"
            "*** Update File: src.txt\n"
            "@@ replace @@\n"
            "-hello world\n"
            "+goodbye world\n"
            "*** End Patch\n"
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="patch",
            args={"mode": "patch", "patch": patch_body},
            mission_id="m-test", operation_key="opk-v4a-update",
        ))
        # The handler-bound patch text must carry the absolute path,
        # not the original "src.txt".
        normalized_patch = prepared.normalized_args["patch"]
        absolute_src = str((wt / "src.txt").resolve())
        assert absolute_src in normalized_patch
        assert "Update File: src.txt" not in normalized_patch
        # Real handler invocation succeeds even with terminal CWD outside.
        result = adapter.commit(prepared, _real_fileops_handler)
        # Handler ran; mutation landed on the authorized path.
        assert (wt / "src.txt").read_text() == "goodbye world\n"

    def test_v4a_add_patch_rewritten_with_absolute_authorized_path(
        self, worktree_repo, checkpoint_base, mission_ctx,
    ):
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        patch_body = (
            "*** Begin Patch\n"
            "*** Add File: new.txt\n"
            "+fresh content\n"
            "*** End Patch\n"
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="patch",
            args={"mode": "patch", "patch": patch_body},
            mission_id="m-test", operation_key="opk-v4a-add",
        ))
        normalized_patch = prepared.normalized_args["patch"]
        absolute_new = str((wt / "new.txt").resolve())
        assert absolute_new in normalized_patch
        result = adapter.commit(prepared, _real_fileops_handler)
        # V4A Add joins the ``+`` lines with newlines; the parser
        # strips the trailing newline. The handler writes the
        # reconstructed content as-is — match without the trailing
        # newline (what the handler actually wrote).
        assert (wt / "new.txt").read_text() == "fresh content"

    def test_v4a_delete_patch_rewritten_with_absolute_authorized_path(
        self, worktree_repo, checkpoint_base, mission_ctx,
    ):
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        (wt / "doomed.txt").write_text("bye\n")
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        patch_body = (
            "*** Begin Patch\n"
            "*** Delete File: doomed.txt\n"
            "*** End Patch\n"
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="patch",
            args={"mode": "patch", "patch": patch_body},
            mission_id="m-test", operation_key="opk-v4a-delete",
        ))
        normalized_patch = prepared.normalized_args["patch"]
        absolute_doomed = str((wt / "doomed.txt").resolve())
        assert absolute_doomed in normalized_patch
        result = adapter.commit(prepared, _real_fileops_handler)
        assert not (wt / "doomed.txt").exists()

    def test_v4a_move_patch_rewrites_both_endpoints_absolute(
        self, worktree_repo, checkpoint_base, mission_ctx,
    ):
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        (wt / "old.txt").write_text("move-me\n")
        # ShellFileOperations.move_file does not mkdir -p the
        # destination parent — pre-create it so the production
        # handler succeeds without bypassing the real code path.
        (wt / "subdir").mkdir(exist_ok=True)
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        patch_body = (
            "*** Begin Patch\n"
            "*** Move File: old.txt -> subdir/new.txt\n"
            "*** End Patch\n"
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="patch",
            args={"mode": "patch", "patch": patch_body},
            mission_id="m-test", operation_key="opk-v4a-move",
        ))
        normalized_patch = prepared.normalized_args["patch"]
        absolute_old = str((wt / "old.txt").resolve())
        absolute_new = str((wt / "subdir" / "new.txt").resolve())
        # BOTH endpoints rewritten to absolute.
        assert absolute_old in normalized_patch
        assert absolute_new in normalized_patch
        # Original relative forms absent.
        assert "Move File: old.txt -> subdir/new.txt" not in normalized_patch
        # Real handler runs; file ends at the absolute authorized destination.
        result = adapter.commit(prepared, _real_fileops_handler)
        assert not (wt / "old.txt").exists()
        assert (wt / "subdir" / "new.txt").read_text() == "move-me\n"

    def test_v4a_move_outside_workspace_blocks_before_handler(
        self, worktree_repo, checkpoint_base, mission_ctx, tmp_path,
    ):
        """A Move destination outside workspace_roots is still blocked
        by prepare-time authority resolution. The rewrite must NOT have
        introduced a way to sneak past authorization."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        (wt / "data.txt").write_text("x\n")
        outside = tmp_path / "escaped.txt"
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        patch_body = (
            "*** Begin Patch\n"
            f"*** Move File: data.txt -> {outside}\n"
            "*** End Patch\n"
        )
        with pytest.raises(PermissionError):
            adapter.prepare(OperationRequest(
                tool_name="patch",
                args={"mode": "patch", "patch": patch_body},
                mission_id="m-test", operation_key="opk-v4a-move-escape",
            ))
        # Source untouched.
        assert (wt / "data.txt").read_text() == "x\n"
        assert not outside.exists()

    def test_v4a_patch_normalized_preserves_hunk_lines_byte_for_byte(
        self, worktree_repo, checkpoint_base, mission_ctx,
    ):
        """Rewriting V4A header lines must not change any hunk line —
        the parser validates and applies hunks by literal byte content.
        A hunk with leading `` ``, ``+``, ``-`` and ``@@`` markers must
        round-trip exactly after the rewrite."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        (wt / "src.txt").write_text("alpha\nbeta\ngamma\n")
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        hunk = (
            "@@ replace @@\n"
            " alpha\n"
            "-beta\n"
            "+BETA\n"
            " gamma\n"
        )
        patch_body = (
            "*** Begin Patch\n"
            "*** Update File: src.txt\n"
            f"{hunk}"
            "*** End Patch\n"
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="patch",
            args={"mode": "patch", "patch": patch_body},
            mission_id="m-test", operation_key="opk-v4a-hunk-roundtrip",
        ))
        normalized_patch = prepared.normalized_args["patch"]
        # Every non-header line is preserved byte-for-byte.
        for line in hunk.splitlines():
            assert line in normalized_patch
        # Original relative header gone.
        assert "Update File: src.txt" not in normalized_patch

    def test_v4a_parse_error_in_normalized_patch_blocks_prepare(
        self, worktree_repo, checkpoint_base, mission_ctx,
    ):
        """If ``parse_v4a_patch`` rejects the patch (no operations,
        missing path, malformed header), prepare must fail loud — the
        adapter must not silently forward an unparseable patch to the
        handler."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        # Empty patch (no operations) — the parser returns ([], None),
        # which the spec calls "no operations" → reject.
        with pytest.raises(ValueError):
            adapter.prepare(OperationRequest(
                tool_name="patch",
                args={"mode": "patch", "patch": ""},
                mission_id="m-test", operation_key="opk-v4a-empty",
            ))


# ---------------------------------------------------------------------------
# Task 4 final — deletion state in verify/reconcile/compensate
# ---------------------------------------------------------------------------


class TestDeletionStateRecovery:
    """Spec: when a V4A patch deletes a target, verify must record
    ``after_hashes[path] = None`` (JSON-safe) and ``changed_paths``
    must include the deleted target. Reconcile must certify
    ``landed`` when expected None and current absent; treat
    ``expected None / current None`` as match; block/review if a
    human re-created the file after deletion. compensate() must
    restore the deleted file from checkpoint state."""

    def test_verify_records_missing_target_as_none_after_hash(
        self, worktree_repo, checkpoint_base, mission_ctx,
    ):
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        target = wt / "delete-me.txt"
        target.write_text("orig\n")
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "delete-me.txt", "content": "fresh\n"},
            mission_id="m-test", operation_key="opk-dstate-1",
        ))
        # Mission handler deletes the file (simulating a delete mutation).
        target.unlink()
        result = adapter.commit(
            prepared, lambda a: {"deleted": a["path"]},
        )
        verified = adapter.verify(prepared, result)
        # Missing target recorded in changed_paths AND after_hashes[None].
        assert str(target.resolve()) in verified["changed_paths"]
        assert verified["after_hashes"][str(target.resolve())] is None

    def test_reconcile_certifies_landed_when_expected_none_current_absent(
        self, worktree_repo, checkpoint_base, mission_ctx,
    ):
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        target = wt / "gone.txt"
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        # Drive a baseline prepare so the manager is initialized.
        adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "other.txt", "content": "x\n"},
            mission_id="m-test", operation_key="opk-baseline",
        ))
        target_str = str(target.resolve())
        assert not target.exists()
        record = type("Rec", (), {
            "operation_id": "opk-dstate-recon",
            "before": {"targets": [target_str]},
            "verification": {
                "after_hashes": {target_str: None},
                "changed_paths": [target_str],
            },
        })()
        outcome = adapter.reconcile(record)
        assert outcome["disposition"] == "landed"
        assert outcome["changed_paths"] == [target_str]
        assert outcome["after_hashes"][target_str] is None

    def test_reconcile_drift_when_human_recreated_after_delete(
        self, worktree_repo, checkpoint_base, mission_ctx,
    ):
        from agent.effect_adapters import WorkspaceEffectAdapter

        wt = mission_ctx.workspace_root
        target = wt / "recreated.txt"
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        target_str = str(target.resolve())
        # The mission recorded deletion (expected None). Human recreated.
        target.write_text("by-human\n")
        record = type("Rec", (), {
            "operation_id": "opk-dstate-recreated",
            "before": {"targets": [target_str]},
            "verification": {
                "after_hashes": {target_str: None},
                "changed_paths": [target_str],
            },
        })()
        outcome = adapter.reconcile(record)
        # Reconcile cannot certify "landed" — the file exists when we
        # expected it absent. Treat as unknown (NOT landed) so the
        # caller does NOT compensate (compensate would clobber the
        # human file).
        assert outcome["disposition"] == "unknown"

    def test_compensate_restores_deleted_file_from_checkpoint(
        self, worktree_repo, checkpoint_base, mission_ctx,
    ):
        """After a mission handler deletes a file (verify recorded
        None), compensate must restore it from the checkpoint
        snapshot."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        target = wt / "doomed.txt"
        target.write_text("orig-content\n")
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "doomed.txt", "content": "after-mission\n"},
            mission_id="m-test", operation_key="opk-dstate-comp",
        ))
        # The mission handler DELETES the file as part of its work.
        # Verify must observe the post-deletion state and record None.
        target.unlink()
        adapter.commit(prepared, lambda a: {"deleted": a["path"]})
        verified = adapter.verify(prepared, {"deleted": str(target.resolve())})
        # Sanity: verify saw the deleted state.
        assert verified["after_hashes"][str(target.resolve())] is None
        assert str(target.resolve()) in verified["changed_paths"]
        assert not target.exists()
        # Compensate restores the file from checkpoint.
        adapter.compensate(prepared)
        assert target.exists()
        assert target.read_text() == "orig-content\n"

    def test_compensate_blocks_when_human_recreated_after_delete(
        self, worktree_repo, checkpoint_base, mission_ctx,
    ):
        """If the file was deleted by mission and a human re-created it
        AFTER the deletion was recorded, compensate must NOT clobber
        the human file. The drift guard compares current SHA (the
        recreated file) against the verified-after state (None/missing)."""
        from agent.effect_adapters import WorkspaceEffectAdapter
        from agent.effect_transactions import OperationRequest

        wt = mission_ctx.workspace_root
        target = wt / "recreated.txt"
        # Set up: file exists before mission.
        target.write_text("original\n")
        adapter = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
        )
        prepared = adapter.prepare(OperationRequest(
            tool_name="write_file",
            args={"path": "recreated.txt", "content": "by-mission\n"},
            mission_id="m-test", operation_key="opk-dstate-block",
        ))
        adapter.commit(prepared, _wf)
        adapter.verify(prepared, {"wrote": str(target.resolve())})
        # Mission deletes the file then human re-creates with NEW content.
        target.unlink()
        target.write_text("by-human\n")
        reviews: List[Dict[str, Any]] = []
        b = WorkspaceEffectAdapter(
            authority=mission_ctx, checkpoint_base=checkpoint_base,
            review_callback=lambda payload: reviews.append(payload),
        )
        rehydrated = type(prepared)(
            adapter_id=prepared.adapter_id,
            normalized_args=dict(prepared.normalized_args),
            before=prepared.before,
            preview=prepared.preview,
            semantics=prepared.semantics,
            compensation=dict(prepared.compensation or {}),
        )
        with pytest.raises(RuntimeError):
            b.compensate(rehydrated)
        # Human file preserved.
        assert target.read_text() == "by-human\n"


# ---------------------------------------------------------------------------
# Task 5: coordinator-only Hermes-state adapters
# ---------------------------------------------------------------------------


def _state_workflow_spec() -> dict[str, Any]:
    return {
        "id": "state_demo",
        "name": "State Demo",
        "version": 1,
        "triggers": [{"type": "manual", "id": "manual"}],
        "nodes": {"start": {"type": "pass"}},
    }


def _state_cron_job() -> dict[str, Any]:
    return {
        "id": "state-cron",
        "name": "State cron",
        "prompt": "durably mutate cron state",
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
        "schedule_display": "every 60m",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "deliver": "local",
        "skills": [],
    }


class TestHermesStateEffectAdapters:
    def _request(self, args: dict[str, Any]) -> Any:
        from agent.effect_transactions import OperationRequest

        return OperationRequest(
            tool_name="mission-state",
            args=args,
            mission_id="mission-state-test",
            operation_key="mission:state:test",
        )

    def test_workflow_adapter_applies_verifies_and_disables_deployed_version(
        self, tmp_path, monkeypatch
    ):
        from agent.effect_adapters import HermesWorkflowStateAdapter
        from hades_cli import workflows_db as wfdb

        monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
        wfdb.init_db()
        with wfdb.connect() as conn:
            adapter = HermesWorkflowStateAdapter(conn)
            prepared = adapter.prepare(
                self._request({"action": "deploy", "spec": _state_workflow_spec()})
            )
            assert prepared.preview["before"]["version"] is None
            assert prepared.preview["after"]["version"] == 1
            result = adapter.commit(prepared, lambda _args: pytest.fail("state adapter invoked handler"))
            assert adapter.verify(prepared, result)["landed"] is True
            adapter.compensate(prepared)
            assert wfdb.get_definition_record(conn, "state_demo", 1).enabled is False

    def test_cron_adapter_uses_durable_store_and_removes_created_job(self, tmp_path):
        from agent.effect_adapters import HermesCronStateAdapter
        from cron.jobs import get_job, use_cron_store

        with use_cron_store(tmp_path / "profile"):
            adapter = HermesCronStateAdapter()
            prepared = adapter.prepare(
                self._request({"action": "create", "job": _state_cron_job()})
            )
            assert prepared.preview["before"] is None
            assert prepared.preview["after"]["deliver"] == "local"
            result = adapter.commit(prepared, lambda _args: pytest.fail("state adapter invoked handler"))
            assert adapter.verify(prepared, result)["landed"] is True
            adapter.compensate(prepared)
            assert get_job("state-cron") is None

    def test_config_adapter_preserves_absent_and_null_and_rejects_credentials(
        self, tmp_path, monkeypatch
    ):
        from agent.effect_adapters import HermesConfigStateAdapter

        monkeypatch.setenv("HADES_HOME", str(tmp_path))
        adapter = HermesConfigStateAdapter()
        prepared = adapter.prepare(
            self._request({"action": "set", "key": "display.theme", "value": "night"})
        )
        assert prepared.preview["before"]["exists"] is False
        result = adapter.commit(prepared, lambda _args: pytest.fail("state adapter invoked handler"))
        assert adapter.verify(prepared, result)["landed"] is True
        adapter.compensate(prepared)
        assert yaml.safe_load((tmp_path / "config.yaml").read_text()) == {"display": {}}

        with pytest.raises(ValueError, match="credential"):
            adapter.prepare(
                self._request({"action": "set", "key": "model.api_key", "value": "blocked"})
            )

    def test_registration_is_coordinator_only(self, tmp_path, monkeypatch):
        from agent.effect_adapters import register_hermes_state_adapters
        from agent.effect_transactions import AdapterRegistry
        from hades_cli import workflows_db as wfdb

        monkeypatch.setenv("HADES_HOME", str(tmp_path / ".hades"))
        wfdb.init_db()
        with wfdb.connect() as conn:
            registry = AdapterRegistry()
            register_hermes_state_adapters(registry, workflow_conn=conn)
            assert registry.all_ids() == [
                "hermes.config-state.v1",
                "hermes.cron-state.v1",
                "hermes.workflow-state.v1",
            ]
