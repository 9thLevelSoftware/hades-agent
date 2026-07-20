"""Tests for tools/checkpoint_manager.py — CheckpointManager (v2 single-store)."""

import json
import logging
import os
import subprocess
import threading
import time
import pytest
from pathlib import Path
from unittest.mock import patch

from tools.checkpoint_manager import (
    CheckpointManager,
    _shadow_repo_path,
    _init_shadow_repo,
    _init_store,
    _run_git,
    _git_env,
    _dir_file_count,
    _project_hash,
    _store_path,
    _ref_name,
    _project_meta_path,
    _touch_project,
    format_checkpoint_list,
    prune_checkpoints,
    maybe_auto_prune_checkpoints,
    store_status,
    clear_all,
    clear_legacy,
)


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture()
def work_dir(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    (d / "main.py").write_text("print('hello')\n")
    (d / "README.md").write_text("# Project\n")
    return d


@pytest.fixture()
def checkpoint_base(tmp_path):
    """Isolated checkpoint base — never writes to ~/.hades/."""
    return tmp_path / "checkpoints"


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture()
def mgr(work_dir, checkpoint_base, monkeypatch):
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
    return CheckpointManager(enabled=True, max_snapshots=50)


@pytest.fixture()
def disabled_mgr(checkpoint_base, monkeypatch):
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
    return CheckpointManager(enabled=False)


# =========================================================================
# Store path + project hash
# =========================================================================

class TestStorePath:
    def test_store_is_single_shared_path(self, work_dir, checkpoint_base, monkeypatch):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        # All projects resolve to the same store.
        p1 = _shadow_repo_path(str(work_dir))
        p2 = _shadow_repo_path(str(work_dir.parent / "other"))
        assert p1 == p2 == _store_path(checkpoint_base)

    def test_project_hash_deterministic(self, work_dir):
        assert _project_hash(str(work_dir)) == _project_hash(str(work_dir))

    def test_project_hash_differs_per_dir(self, tmp_path):
        assert _project_hash(str(tmp_path / "a")) != _project_hash(str(tmp_path / "b"))

    def test_tilde_and_expanded_home_share_project_hash(
        self, fake_home, checkpoint_base, monkeypatch,
    ):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        project = fake_home / "project"
        project.mkdir()
        tilde = f"~/{project.name}"
        assert _project_hash(tilde) == _project_hash(str(project))


# =========================================================================
# Store init + legacy migration
# =========================================================================

class TestStoreInit:
    def test_creates_git_store(self, work_dir, checkpoint_base, monkeypatch):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        store = _store_path(checkpoint_base)
        err = _init_store(store, str(work_dir))
        assert err is None
        assert (store / "HEAD").exists()
        assert (store / "objects").exists()
        assert (store / "info" / "exclude").exists()
        assert "node_modules/" in (store / "info" / "exclude").read_text()

    def test_no_git_in_project_dir(self, work_dir, checkpoint_base, monkeypatch):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        store = _store_path(checkpoint_base)
        _init_store(store, str(work_dir))
        assert not (work_dir / ".git").exists()

    def test_init_idempotent(self, work_dir, checkpoint_base, monkeypatch):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        store = _store_path(checkpoint_base)
        assert _init_store(store, str(work_dir)) is None
        assert _init_store(store, str(work_dir)) is None

    def test_bc_init_shadow_repo_shim(self, work_dir, checkpoint_base, monkeypatch):
        """Backward-compatible helper still works for old callers/tests."""
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        store = _shadow_repo_path(str(work_dir))
        err = _init_shadow_repo(store, str(work_dir))
        assert err is None
        assert (store / "HEAD").exists()
        assert (store / "HERMES_WORKDIR").exists()

    def test_legacy_migration_archives_prev2_repos(
        self, checkpoint_base, work_dir,
    ):
        """Pre-v2 per-project shadow repos get moved into legacy-<ts>/."""
        base = checkpoint_base
        base.mkdir(parents=True)
        # Simulate a pre-v2 repo directly under base
        fake_repo = base / "deadbeefcafebabe"
        fake_repo.mkdir()
        (fake_repo / "HEAD").write_text("ref: refs/heads/main\n")
        (fake_repo / "HERMES_WORKDIR").write_text(str(work_dir) + "\n")
        (fake_repo / "objects").mkdir()

        # Init store — should migrate the fake pre-v2 repo
        store = _store_path(base)
        err = _init_store(store, str(work_dir))
        assert err is None

        assert not fake_repo.exists()
        legacies = [p for p in base.iterdir() if p.name.startswith("legacy-")]
        assert len(legacies) == 1
        assert (legacies[0] / fake_repo.name).exists()
        assert (legacies[0] / fake_repo.name / "HEAD").exists()


# =========================================================================
# CheckpointManager — disabled
# =========================================================================

class TestDisabledManager:
    def test_ensure_checkpoint_returns_false(self, disabled_mgr, work_dir):
        assert disabled_mgr.ensure_checkpoint(str(work_dir)) is False

    def test_new_turn_works(self, disabled_mgr):
        disabled_mgr.new_turn()


# =========================================================================
# CheckpointManager — taking checkpoints
# =========================================================================

class TestTakeCheckpoint:
    def test_first_checkpoint(self, mgr, work_dir):
        result = mgr.ensure_checkpoint(str(work_dir), "initial")
        assert result is True

    def test_dedup_same_turn(self, mgr, work_dir):
        r1 = mgr.ensure_checkpoint(str(work_dir), "first")
        r2 = mgr.ensure_checkpoint(str(work_dir), "second")
        assert r1 is True
        assert r2 is False  # dedup'd

    def test_concurrent_same_turn_only_takes_once(self, mgr, work_dir, monkeypatch):
        mgr._git_available = True
        callers_ready = threading.Barrier(2)
        take_started = threading.Event()
        release_take = threading.Event()
        calls = []
        results = []

        def fake_take(directory, reason):
            calls.append((directory, reason))
            take_started.set()
            release_take.wait(timeout=5)
            return True, "deadbeef"

        monkeypatch.setattr(mgr, "_take", fake_take)

        def ensure(reason):
            callers_ready.wait(timeout=5)
            results.append(mgr.ensure_checkpoint(str(work_dir), reason))

        threads = [
            threading.Thread(target=ensure, args=("first",)),
            threading.Thread(target=ensure, args=("second",)),
        ]
        for thread in threads:
            thread.start()
        assert take_started.wait(timeout=5)
        release_take.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()

        assert len(calls) == 1
        assert sorted(results) == [False, True]

    def test_new_turn_waits_for_inflight_checkpoint(self, mgr, work_dir, monkeypatch):
        mgr._git_available = True
        take_started = threading.Event()
        release_take = threading.Event()
        calls = []
        first_result = []

        def fake_take(directory, reason):
            calls.append((directory, reason))
            take_started.set()
            release_take.wait(timeout=5)
            return True, "deadbeef"

        monkeypatch.setattr(mgr, "_take", fake_take)

        first = threading.Thread(
            target=lambda: first_result.append(
                mgr.ensure_checkpoint(str(work_dir), "first")
            )
        )
        first.start()
        assert take_started.wait(timeout=5)

        release_take.set()
        first.join(timeout=5)
        assert not first.is_alive()

        assert first_result == [True]
        mgr.new_turn()
        assert mgr.ensure_checkpoint(str(work_dir), "second") is True
        assert len(calls) == 2

    def test_failed_checkpoint_can_retry_same_turn(self, mgr, work_dir, monkeypatch):
        mgr._git_available = True
        calls = []

        def fake_take(directory, reason):
            calls.append((directory, reason))
            return (len(calls) == 2, "deadbeef" if len(calls) == 2 else None)

        monkeypatch.setattr(mgr, "_take", fake_take)

        assert mgr.ensure_checkpoint(str(work_dir), "first") is False
        assert mgr.ensure_checkpoint(str(work_dir), "retry") is True
        assert len(calls) == 2

    def test_successful_checkpoint_deduplicates_same_turn(self, mgr, work_dir, monkeypatch):
        mgr._git_available = True
        calls = []
        monkeypatch.setattr(
            mgr,
            "_take",
            lambda directory, reason: (calls.append((directory, reason)), True, "deadbeef")[1:],
        )

        assert mgr.ensure_checkpoint(str(work_dir), "first") is True
        assert mgr.ensure_checkpoint(str(work_dir), "duplicate") is False
        assert len(calls) == 1

    def test_new_turn_resets_dedup(self, mgr, work_dir):
        assert mgr.ensure_checkpoint(str(work_dir), "turn 1") is True
        mgr.new_turn()
        (work_dir / "main.py").write_text("print('modified')\n")
        assert mgr.ensure_checkpoint(str(work_dir), "turn 2") is True

    def test_no_changes_skips_commit(self, mgr, work_dir):
        mgr.ensure_checkpoint(str(work_dir), "initial")
        mgr.new_turn()
        assert mgr.ensure_checkpoint(str(work_dir), "no changes") is False

    def test_skip_root_dir(self, mgr):
        assert mgr.ensure_checkpoint("/", "root") is False

    def test_skip_home_dir(self, mgr):
        assert mgr.ensure_checkpoint(str(Path.home()), "home") is False

    def test_multiple_projects_share_store(self, mgr, tmp_path):
        """Two projects commit to the SAME shared store (dedup wins)."""
        a = tmp_path / "proj-a"
        a.mkdir()
        (a / "f.py").write_text("a\n")
        b = tmp_path / "proj-b"
        b.mkdir()
        (b / "g.py").write_text("b\n")

        assert mgr.ensure_checkpoint(str(a), "a") is True
        mgr.new_turn()
        assert mgr.ensure_checkpoint(str(b), "b") is True

        # Only one "store" directory exists.
        bases = list(Path(mgr._checkpointed_dirs).__iter__()) if False else None
        from tools.checkpoint_manager import CHECKPOINT_BASE as BASE
        # Exactly one store dir + two project metas
        assert (BASE / "store" / "HEAD").exists()
        assert (BASE / "store" / "projects" / f"{_project_hash(str(a))}.json").exists()
        assert (BASE / "store" / "projects" / f"{_project_hash(str(b))}.json").exists()


# =========================================================================
# CheckpointManager — listing
# =========================================================================

class TestListCheckpoints:
    def test_empty_when_no_checkpoints(self, mgr, work_dir):
        assert mgr.list_checkpoints(str(work_dir)) == []

    def test_list_after_take(self, mgr, work_dir):
        mgr.ensure_checkpoint(str(work_dir), "test checkpoint")
        result = mgr.list_checkpoints(str(work_dir))
        assert len(result) == 1
        assert result[0]["reason"] == "test checkpoint"
        assert "hash" in result[0]
        assert "short_hash" in result[0]
        assert "timestamp" in result[0]

    def test_multiple_checkpoints_ordered(self, mgr, work_dir):
        mgr.ensure_checkpoint(str(work_dir), "first")
        mgr.new_turn()
        (work_dir / "main.py").write_text("v2\n")
        mgr.ensure_checkpoint(str(work_dir), "second")
        mgr.new_turn()
        (work_dir / "main.py").write_text("v3\n")
        mgr.ensure_checkpoint(str(work_dir), "third")

        result = mgr.list_checkpoints(str(work_dir))
        assert len(result) == 3
        assert result[0]["reason"] == "third"
        assert result[2]["reason"] == "first"

    def test_list_isolated_per_project(self, mgr, tmp_path):
        """Listing one project doesn't leak checkpoints from another."""
        a = tmp_path / "a"
        a.mkdir()
        (a / "f").write_text("A\n")
        b = tmp_path / "b"
        b.mkdir()
        (b / "g").write_text("B\n")

        mgr.ensure_checkpoint(str(a), "A-1")
        mgr.new_turn()
        mgr.ensure_checkpoint(str(b), "B-1")

        assert [c["reason"] for c in mgr.list_checkpoints(str(a))] == ["A-1"]
        assert [c["reason"] for c in mgr.list_checkpoints(str(b))] == ["B-1"]

    def test_tilde_path_lists_same_checkpoints(self, checkpoint_base, fake_home, monkeypatch):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        m = CheckpointManager(enabled=True, max_snapshots=50)
        project = fake_home / "project"
        project.mkdir()
        (project / "main.py").write_text("v1\n")
        assert m.ensure_checkpoint(f"~/{project.name}", "initial") is True
        listed = m.list_checkpoints(str(project))
        assert len(listed) == 1
        assert listed[0]["reason"] == "initial"


# =========================================================================
# Pruning: max_snapshots actually enforced (v2 fix)
# =========================================================================

class TestRealPruning:
    def test_max_snapshots_trims_history(self, work_dir, checkpoint_base, monkeypatch):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        # Tiny cap to test enforcement.
        m = CheckpointManager(enabled=True, max_snapshots=3)

        for i in range(6):
            (work_dir / "main.py").write_text(f"v{i}\n")
            m.new_turn()
            m.ensure_checkpoint(str(work_dir), f"step-{i}")

        cps = m.list_checkpoints(str(work_dir))
        assert len(cps) == 3
        reasons = [c["reason"] for c in cps]
        # Newest first — step-5, step-4, step-3
        assert reasons[0] == "step-5"
        assert reasons[-1] == "step-3"

    def test_max_file_size_mb_skips_large_files(
        self, tmp_path, checkpoint_base, monkeypatch,
    ):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        wd = tmp_path / "proj"
        wd.mkdir()
        (wd / "small.py").write_text("tiny\n")
        big = wd / "weights.bin"
        big.write_bytes(b"\0" * (2 * 1024 * 1024))  # 2 MB

        m = CheckpointManager(enabled=True, max_snapshots=5, max_file_size_mb=1)
        assert m.ensure_checkpoint(str(wd), "initial") is True

        store = _store_path(checkpoint_base)
        ok, files, _ = _run_git(
            ["ls-tree", "-r", "--name-only", _ref_name(_project_hash(str(wd)))],
            store, str(wd),
        )
        assert ok
        names = set(files.splitlines())
        assert "small.py" in names
        assert "weights.bin" not in names  # filtered by size cap


# =========================================================================
# CheckpointManager — restoring
# =========================================================================

class TestRestore:
    def test_restore_to_previous(self, mgr, work_dir):
        (work_dir / "main.py").write_text("original\n")
        mgr.ensure_checkpoint(str(work_dir), "original state")
        mgr.new_turn()

        (work_dir / "main.py").write_text("modified\n")

        cps = mgr.list_checkpoints(str(work_dir))
        assert len(cps) == 1

        result = mgr.restore(str(work_dir), cps[0]["hash"])
        assert result["success"] is True
        assert (work_dir / "main.py").read_text() == "original\n"

    def test_restore_invalid_hash(self, mgr, work_dir):
        mgr.ensure_checkpoint(str(work_dir), "initial")
        result = mgr.restore(str(work_dir), "deadbeef1234")
        assert result["success"] is False

    def test_restore_no_checkpoints(self, mgr, work_dir):
        result = mgr.restore(str(work_dir), "abc123")
        assert result["success"] is False

    def test_restore_creates_pre_rollback_snapshot(self, mgr, work_dir):
        (work_dir / "main.py").write_text("v1\n")
        mgr.ensure_checkpoint(str(work_dir), "v1")
        mgr.new_turn()

        (work_dir / "main.py").write_text("v2\n")
        cps = mgr.list_checkpoints(str(work_dir))
        mgr.restore(str(work_dir), cps[0]["hash"])

        all_cps = mgr.list_checkpoints(str(work_dir))
        assert len(all_cps) >= 2
        assert "pre-rollback" in all_cps[0]["reason"]

    def test_tilde_path_supports_diff_and_restore_flow(
        self, checkpoint_base, fake_home, monkeypatch,
    ):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        m = CheckpointManager(enabled=True, max_snapshots=50)
        project = fake_home / "project"
        project.mkdir()
        file_path = project / "main.py"
        file_path.write_text("original\n")

        tilde = f"~/{project.name}"
        assert m.ensure_checkpoint(tilde, "initial") is True
        m.new_turn()

        file_path.write_text("changed\n")
        cps = m.list_checkpoints(str(project))
        diff_result = m.diff(tilde, cps[0]["hash"])
        assert diff_result["success"] is True
        assert "main.py" in diff_result["diff"]

        restore_result = m.restore(tilde, cps[0]["hash"])
        assert restore_result["success"] is True
        assert file_path.read_text() == "original\n"


# =========================================================================
# CheckpointManager — working dir resolution
# =========================================================================

class TestWorkingDirResolution:
    def test_resolves_git_project_root(self, tmp_path):
        m = CheckpointManager(enabled=True)
        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".git").mkdir()
        subdir = project / "src"
        subdir.mkdir()
        filepath = subdir / "main.py"
        filepath.write_text("x\n")

        assert m.get_working_dir_for_path(str(filepath)) == str(project)

    def test_resolves_pyproject_root(self, tmp_path):
        m = CheckpointManager(enabled=True)
        project = tmp_path / "pyproj"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\n")
        subdir = project / "src"
        subdir.mkdir()
        assert m.get_working_dir_for_path(str(subdir / "file.py")) == str(project)

    def test_falls_back_to_parent(self, tmp_path, monkeypatch):
        m = CheckpointManager(enabled=True)
        filepath = tmp_path / "random" / "file.py"
        filepath.parent.mkdir(parents=True)
        filepath.write_text("x\n")

        import pathlib as _pl
        _real_exists = _pl.Path.exists

        def _guarded_exists(self):
            s = str(self)
            stop = str(tmp_path)
            if not s.startswith(stop) and any(
                s.endswith("/" + m) or s == "/" + m
                for m in (".git", "pyproject.toml", "package.json",
                          "Cargo.toml", "go.mod", "Makefile", "pom.xml",
                          ".hg", "Gemfile")
            ):
                return False
            return _real_exists(self)

        monkeypatch.setattr(_pl.Path, "exists", _guarded_exists)
        assert m.get_working_dir_for_path(str(filepath)) == str(filepath.parent)

    def test_resolves_tilde_path_to_project_root(self, fake_home):
        m = CheckpointManager(enabled=True)
        project = fake_home / "myproject"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\n")
        subdir = project / "src"
        subdir.mkdir()
        filepath = subdir / "main.py"
        filepath.write_text("x\n")

        assert m.get_working_dir_for_path(
            f"~/{project.name}/src/main.py"
        ) == str(project)


# =========================================================================
# Git env isolation
# =========================================================================

class TestGitEnvIsolation:
    def test_sets_git_dir(self, tmp_path):
        store = tmp_path / "store"
        env = _git_env(store, str(tmp_path / "work"))
        assert env["GIT_DIR"] == str(store)

    def test_sets_work_tree(self, tmp_path):
        store = tmp_path / "store"
        work = tmp_path / "work"
        env = _git_env(store, str(work))
        assert env["GIT_WORK_TREE"] == str(work.resolve())

    def test_clears_index_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_INDEX_FILE", "/some/index")
        env = _git_env(tmp_path / "store", str(tmp_path))
        assert "GIT_INDEX_FILE" not in env

    def test_sets_index_file_when_provided(self, tmp_path):
        env = _git_env(
            tmp_path / "store", str(tmp_path),
            index_file=tmp_path / "store" / "indexes" / "abc",
        )
        assert env["GIT_INDEX_FILE"].endswith("indexes/abc")

    def test_expands_tilde_in_work_tree(self, fake_home, tmp_path):
        work = fake_home / "work"
        work.mkdir()
        env = _git_env(tmp_path / "store", f"~/{work.name}")
        assert env["GIT_WORK_TREE"] == str(work.resolve())


# =========================================================================
# format_checkpoint_list
# =========================================================================

class TestFormatCheckpointList:
    def test_empty_list(self):
        assert "No checkpoints" in format_checkpoint_list([], "/some/dir")

    def test_formats_entries(self):
        cps = [
            {"hash": "abc123", "short_hash": "abc1",
             "timestamp": "2026-03-09T21:15:00-07:00",
             "reason": "before write_file"},
            {"hash": "def456", "short_hash": "def4",
             "timestamp": "2026-03-09T21:10:00-07:00",
             "reason": "before patch"},
        ]
        result = format_checkpoint_list(cps, "/home/user/project")
        assert "abc1" in result
        assert "def4" in result
        assert "before write_file" in result
        assert "/rollback" in result


# =========================================================================
# Dir size / file count guards
# =========================================================================

class TestDirFileCount:
    def test_counts_files(self, work_dir):
        assert _dir_file_count(str(work_dir)) >= 2

    def test_nonexistent_dir(self, tmp_path):
        assert _dir_file_count(str(tmp_path / "nonexistent")) == 0


# =========================================================================
# Error resilience
# =========================================================================

class TestErrorResilience:
    def test_no_git_installed(self, work_dir, checkpoint_base, monkeypatch):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        m = CheckpointManager(enabled=True)
        monkeypatch.setattr("shutil.which", lambda x: None)
        m._git_available = None
        assert m.ensure_checkpoint(str(work_dir), "test") is False

    def test_run_git_allows_expected_nonzero_without_error_log(
        self, tmp_path, caplog,
    ):
        work = tmp_path / "work"
        work.mkdir()
        completed = subprocess.CompletedProcess(
            args=["git", "diff", "--cached", "--quiet"],
            returncode=1, stdout="", stderr="",
        )
        with patch("tools.checkpoint_manager.subprocess.run", return_value=completed):
            with caplog.at_level(logging.ERROR, logger="tools.checkpoint_manager"):
                ok, stdout, stderr = _run_git(
                    ["diff", "--cached", "--quiet"],
                    tmp_path / "store", str(work),
                    allowed_returncodes={1},
                )
        assert ok is False
        assert stdout == ""
        assert not caplog.records

    def test_run_git_invalid_working_dir_reports_path_error(self, tmp_path, caplog):
        missing = tmp_path / "missing"
        with caplog.at_level(logging.ERROR, logger="tools.checkpoint_manager"):
            ok, _, stderr = _run_git(
                ["status"], tmp_path / "store", str(missing),
            )
        assert ok is False
        assert "working directory not found" in stderr
        assert not any(
            "Git executable not found" in r.getMessage() for r in caplog.records
        )

    def test_run_git_missing_git_reports_git_not_found(
        self, tmp_path, monkeypatch, caplog,
    ):
        work = tmp_path / "work"
        work.mkdir()

        def raise_missing_git(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "git")

        monkeypatch.setattr("tools.checkpoint_manager.subprocess.run", raise_missing_git)
        with caplog.at_level(logging.ERROR, logger="tools.checkpoint_manager"):
            ok, _, stderr = _run_git(
                ["status"], tmp_path / "store", str(work),
            )
        assert ok is False
        assert stderr == "git not found"
        assert any(
            "Git executable not found" in r.getMessage() for r in caplog.records
        )

    def test_checkpoint_failure_does_not_raise(self, mgr, work_dir, monkeypatch):
        def broken_run_git(*args, **kwargs):
            raise OSError("git exploded")
        monkeypatch.setattr("tools.checkpoint_manager._run_git", broken_run_git)
        assert mgr.ensure_checkpoint(str(work_dir), "test") is False


class TestTouchProjectMalformedMeta:
    """_touch_project must not raise when the project metadata file is corrupted.

    The try/except in _touch_project only catches ``(OSError, ValueError)``.
    When ``json.load`` succeeds but returns a non-dict (e.g. a list ``[]``,
    ``null``, or a scalar), the subsequent ``meta["workdir"] = ...`` raises
    ``TypeError: list indices must be integers…``.  This TypeError propagates
    uncaught out of ``_touch_project`` and up through ``_take`` into
    ``ensure_checkpoint``, where it is swallowed by the broad ``except
    Exception`` safety net — but the effect is that the checkpoint is silently
    skipped for the entire session.

    Fix: add ``if not isinstance(meta, dict): meta = {}`` after parsing,
    mirroring the same guard already present in ``_list_projects``.
    """

    @pytest.mark.parametrize("payload", ["[]", "null", "42", '"oops"'])
    def test_non_dict_meta_does_not_raise(self, tmp_path, payload):
        store = tmp_path / "store"
        workdir = str(tmp_path / "project")
        _init_store(store, workdir)

        dir_hash = _project_hash(workdir)
        meta_path = _project_meta_path(store, dir_hash)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(payload, encoding="utf-8")

        # Must not raise TypeError
        _touch_project(store, workdir)

        # Metadata file should now be a valid dict with last_touch updated
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "last_touch" in data
        assert "workdir" in data


# =========================================================================
# Security / input validation
# =========================================================================

class TestSecurity:
    def test_restore_rejects_argument_injection(self, mgr, work_dir):
        mgr.ensure_checkpoint(str(work_dir), "initial")
        result = mgr.restore(str(work_dir), "--patch")
        assert result["success"] is False
        assert "Invalid commit hash" in result["error"]
        assert "must not start with '-'" in result["error"]

        result = mgr.restore(str(work_dir), "-p")
        assert result["success"] is False
        assert "Invalid commit hash" in result["error"]

    def test_restore_rejects_invalid_hex_chars(self, mgr, work_dir):
        mgr.ensure_checkpoint(str(work_dir), "initial")
        result = mgr.restore(str(work_dir), "abc; rm -rf /")
        assert result["success"] is False
        assert "expected 4-64 hex characters" in result["error"]

        result = mgr.diff(str(work_dir), "abc&def")
        assert result["success"] is False
        assert "expected 4-64 hex characters" in result["error"]

    def test_restore_rejects_path_traversal(self, mgr, work_dir):
        mgr.ensure_checkpoint(str(work_dir), "initial")
        cps = mgr.list_checkpoints(str(work_dir))
        target_hash = cps[0]["hash"]

        result = mgr.restore(str(work_dir), target_hash, file_path="/etc/passwd")
        assert result["success"] is False
        assert "got absolute path" in result["error"]

        result = mgr.restore(str(work_dir), target_hash, file_path="../outside_file.txt")
        assert result["success"] is False
        assert "escapes the working directory" in result["error"]

    def test_restore_accepts_valid_file_path(self, mgr, work_dir):
        mgr.ensure_checkpoint(str(work_dir), "initial")
        cps = mgr.list_checkpoints(str(work_dir))
        target_hash = cps[0]["hash"]

        result = mgr.restore(str(work_dir), target_hash, file_path="main.py")
        assert result["success"] is True

        (work_dir / "subdir").mkdir()
        (work_dir / "subdir" / "test.txt").write_text("hello")
        mgr.new_turn()
        mgr.ensure_checkpoint(str(work_dir), "second")
        cps = mgr.list_checkpoints(str(work_dir))
        result = mgr.restore(str(work_dir), cps[0]["hash"], file_path="subdir/test.txt")
        assert result["success"] is True


# =========================================================================
# GPG / global git config isolation
# =========================================================================

class TestGpgAndGlobalConfigIsolation:
    def test_git_env_isolates_global_and_system_config(self, tmp_path):
        env = _git_env(tmp_path / "store", str(tmp_path))
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_CONFIG_SYSTEM"] == os.devnull
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"

    def test_init_sets_commit_gpgsign_false(self, work_dir, checkpoint_base, monkeypatch):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        store = _store_path(checkpoint_base)
        _init_store(store, str(work_dir))
        result = subprocess.run(
            ["git", "config", "--file", str(store / "config"),
             "--get", "commit.gpgsign"],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == "false"

    def test_init_sets_tag_gpgsign_false(self, work_dir, checkpoint_base, monkeypatch):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        store = _store_path(checkpoint_base)
        _init_store(store, str(work_dir))
        result = subprocess.run(
            ["git", "config", "--file", str(store / "config"),
             "--get", "tag.gpgSign"],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == "false"

    def test_checkpoint_works_with_global_gpgsign_and_broken_gpg(
        self, work_dir, checkpoint_base, monkeypatch, tmp_path,
    ):
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        (fake_home / ".gitconfig").write_text(
            "[user]\n    email = real@user.com\n    name = Real User\n"
            "[commit]\n    gpgsign = true\n"
            "[tag]\n    gpgSign = true\n"
            "[gpg]\n    program = /nonexistent/fake-gpg-binary\n"
        )
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.delenv("GPG_TTY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)

        m = CheckpointManager(enabled=True)
        assert m.ensure_checkpoint(str(work_dir), reason="with-global-gpgsign") is True
        assert len(m.list_checkpoints(str(work_dir))) == 1


# =========================================================================
# prune_checkpoints + maybe_auto_prune_checkpoints
# =========================================================================

def _seed_legacy_repo(base: Path, name: str, workdir: Path, mtime: float = None) -> Path:
    """Create a minimal pre-v2 shadow repo directly under base."""
    shadow = base / name
    shadow.mkdir(parents=True)
    (shadow / "HEAD").write_text("ref: refs/heads/main\n")
    (shadow / "HERMES_WORKDIR").write_text(str(workdir) + "\n")
    (shadow / "info").mkdir()
    (shadow / "info" / "exclude").write_text("node_modules/\n")
    if mtime is not None:
        for p in shadow.rglob("*"):
            os.utime(p, (mtime, mtime))
        os.utime(shadow, (mtime, mtime))
    return shadow


def _seed_v2_project(base: Path, workdir: Path, last_touch: float = None) -> str:
    """Register a v2 project in the shared store (no commits, just metadata)."""
    store = _store_path(base)
    _init_store(store, str(workdir if workdir.exists() else base))
    dir_hash = _project_hash(str(workdir))
    meta = {
        "workdir": str(workdir.resolve()) if workdir.exists() else str(workdir),
        "created_at": (last_touch or time.time()),
        "last_touch": (last_touch or time.time()),
    }
    mp = _project_meta_path(store, dir_hash)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(meta))
    return dir_hash


class TestPruneCheckpointsLegacy:
    """Backwards-compat: prune still handles pre-v2 per-project shadow repos."""

    def test_deletes_orphan_when_workdir_missing(self, tmp_path):
        base = tmp_path / "checkpoints"
        alive_work = tmp_path / "alive"
        alive_work.mkdir()
        alive_repo = _seed_legacy_repo(base, "aaaa" * 4, alive_work)
        orphan_repo = _seed_legacy_repo(base, "bbbb" * 4, tmp_path / "was-deleted")

        result = prune_checkpoints(retention_days=0, checkpoint_base=base)

        assert result["scanned"] == 2
        assert result["deleted_orphan"] == 1
        assert result["deleted_stale"] == 0
        assert alive_repo.exists()
        assert not orphan_repo.exists()

    def test_deletes_stale_by_mtime(self, tmp_path):
        base = tmp_path / "checkpoints"
        work = tmp_path / "work"
        work.mkdir()
        fresh_repo = _seed_legacy_repo(base, "cccc" * 4, work)
        stale_work = tmp_path / "stale_work"
        stale_work.mkdir()
        old = time.time() - 60 * 86400
        stale_repo = _seed_legacy_repo(base, "dddd" * 4, stale_work, mtime=old)

        result = prune_checkpoints(
            retention_days=30, delete_orphans=False, checkpoint_base=base,
        )
        assert result["deleted_stale"] == 1
        assert fresh_repo.exists()
        assert not stale_repo.exists()

    def test_delete_orphans_disabled_keeps_orphans(self, tmp_path):
        base = tmp_path / "checkpoints"
        orphan = _seed_legacy_repo(base, "ffff" * 4, tmp_path / "gone")

        result = prune_checkpoints(
            retention_days=0, delete_orphans=False, checkpoint_base=base,
        )
        assert result["deleted_orphan"] == 0
        assert orphan.exists()

    def test_skips_non_shadow_dirs(self, tmp_path):
        base = tmp_path / "checkpoints"
        base.mkdir()
        (base / "garbage-dir").mkdir()
        (base / "garbage-dir" / "random.txt").write_text("hi")

        result = prune_checkpoints(retention_days=0, checkpoint_base=base)
        assert result["scanned"] == 0
        assert (base / "garbage-dir").exists()

    def test_base_missing_returns_empty_counts(self, tmp_path):
        result = prune_checkpoints(checkpoint_base=tmp_path / "does-not-exist")
        assert result["scanned"] == 0
        assert result["deleted_orphan"] == 0


class TestPruneCheckpointsV2:
    """v2 pruning walks the shared store's projects/ metadata."""

    def test_deletes_orphan_project_entry(self, tmp_path, monkeypatch):
        base = tmp_path / "checkpoints"
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)

        alive = tmp_path / "alive"
        alive.mkdir()
        (alive / "f").write_text("a")
        gone = tmp_path / "was-gone"
        gone.mkdir()
        (gone / "g").write_text("b")

        m = CheckpointManager(enabled=True)
        assert m.ensure_checkpoint(str(alive), "alive") is True
        m.new_turn()
        assert m.ensure_checkpoint(str(gone), "gone") is True

        # Simulate deletion of "gone"
        import shutil as _shutil
        _shutil.rmtree(gone)

        result = prune_checkpoints(retention_days=0, checkpoint_base=base)

        assert result["deleted_orphan"] >= 1
        # Alive project survives
        alive_hash = _project_hash(str(alive))
        assert (base / "store" / "projects" / f"{alive_hash}.json").exists()
        # Gone project metadata wiped
        gone_hash = _project_hash(str(gone))
        assert not (base / "store" / "projects" / f"{gone_hash}.json").exists()

    def test_deletes_stale_project_by_last_touch(self, tmp_path, monkeypatch):
        base = tmp_path / "checkpoints"
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)

        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "f").write_text("f")
        stale = tmp_path / "stale"
        stale.mkdir()
        (stale / "s").write_text("s")

        m = CheckpointManager(enabled=True)
        m.ensure_checkpoint(str(fresh), "fresh")
        m.new_turn()
        m.ensure_checkpoint(str(stale), "stale")

        # Backdate stale's last_touch to 60 days ago
        stale_hash = _project_hash(str(stale))
        meta_path = base / "store" / "projects" / f"{stale_hash}.json"
        meta = json.loads(meta_path.read_text())
        meta["last_touch"] = time.time() - 60 * 86400
        meta_path.write_text(json.dumps(meta))

        result = prune_checkpoints(
            retention_days=30, delete_orphans=False, checkpoint_base=base,
        )

        assert result["deleted_stale"] >= 1
        fresh_hash = _project_hash(str(fresh))
        assert (base / "store" / "projects" / f"{fresh_hash}.json").exists()
        assert not meta_path.exists()

    def test_legacy_archive_dirs_also_pruned(self, tmp_path, monkeypatch):
        """legacy-<ts>/ dirs older than retention_days get wiped."""
        base = tmp_path / "checkpoints"
        base.mkdir()
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)

        old_legacy = base / "legacy-20200101-000000"
        old_legacy.mkdir()
        (old_legacy / "junk").write_bytes(b"x" * 1000)
        old = time.time() - 60 * 86400
        for p in old_legacy.rglob("*"):
            os.utime(p, (old, old))
        os.utime(old_legacy, (old, old))

        result = prune_checkpoints(retention_days=7, checkpoint_base=base)
        assert result["deleted_stale"] >= 1
        assert not old_legacy.exists()


class TestMaybeAutoPruneCheckpoints:
    def test_first_call_prunes_and_writes_marker(self, tmp_path):
        base = tmp_path / "checkpoints"
        _seed_legacy_repo(base, "0000" * 4, tmp_path / "gone")

        out = maybe_auto_prune_checkpoints(checkpoint_base=base)
        assert out["skipped"] is False
        assert out["result"]["deleted_orphan"] == 1
        assert (base / ".last_prune").exists()

    def test_second_call_within_interval_skips(self, tmp_path):
        base = tmp_path / "checkpoints"
        _seed_legacy_repo(base, "1111" * 4, tmp_path / "gone")

        first = maybe_auto_prune_checkpoints(
            checkpoint_base=base, min_interval_hours=24,
        )
        assert first["skipped"] is False

        _seed_legacy_repo(base, "2222" * 4, tmp_path / "also-gone")
        second = maybe_auto_prune_checkpoints(
            checkpoint_base=base, min_interval_hours=24,
        )
        assert second["skipped"] is True
        assert (base / ("2222" * 4)).exists()

    def test_corrupt_marker_treated_as_no_prior_run(self, tmp_path):
        base = tmp_path / "checkpoints"
        base.mkdir()
        (base / ".last_prune").write_text("not-a-timestamp")
        _seed_legacy_repo(base, "3333" * 4, tmp_path / "gone")

        out = maybe_auto_prune_checkpoints(checkpoint_base=base)
        assert out["skipped"] is False
        assert out["result"]["deleted_orphan"] == 1

    def test_missing_base_no_raise(self, tmp_path):
        out = maybe_auto_prune_checkpoints(
            checkpoint_base=tmp_path / "does-not-exist",
        )
        assert out["skipped"] is False
        assert out["result"]["scanned"] == 0


# =========================================================================
# store_status / clear_all / clear_legacy
# =========================================================================

class TestStoreStatus:
    def test_empty_base(self, tmp_path, monkeypatch):
        base = tmp_path / "checkpoints"
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)
        info = store_status()
        assert info["project_count"] == 0
        assert info["total_size_bytes"] == 0

    def test_reports_projects_and_legacy(self, tmp_path, monkeypatch, work_dir):
        base = tmp_path / "checkpoints"
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)

        m = CheckpointManager(enabled=True)
        m.ensure_checkpoint(str(work_dir), "initial")

        # Add a legacy archive dir manually
        legacy = base / "legacy-20200101-000000"
        legacy.mkdir()
        (legacy / "junk").write_bytes(b"x" * 100)

        info = store_status()
        assert info["project_count"] == 1
        assert info["projects"][0]["workdir"] == str(work_dir.resolve())
        assert info["projects"][0]["commits"] >= 1
        assert info["projects"][0]["exists"] is True
        assert len(info["legacy_archives"]) == 1
        assert info["legacy_archives"][0]["size_bytes"] >= 100


class TestClearFunctions:
    def test_clear_all_wipes_base(self, tmp_path, monkeypatch, work_dir):
        base = tmp_path / "checkpoints"
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)
        m = CheckpointManager(enabled=True)
        m.ensure_checkpoint(str(work_dir), "initial")
        assert base.exists()

        result = clear_all()
        assert result["deleted"] is True
        assert result["bytes_freed"] > 0
        assert not base.exists()

    def test_clear_legacy_only_removes_legacy_dirs(
        self, tmp_path, monkeypatch, work_dir,
    ):
        base = tmp_path / "checkpoints"
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)
        m = CheckpointManager(enabled=True)
        m.ensure_checkpoint(str(work_dir), "initial")

        legacy = base / "legacy-20200101-000000"
        legacy.mkdir()
        (legacy / "junk").write_bytes(b"x" * 1000)

        result = clear_legacy()
        assert result["deleted"] == 1
        assert result["bytes_freed"] >= 1000
        assert not legacy.exists()
        # Store preserved
        assert (base / "store" / "HEAD").exists()

    def test_clear_all_on_missing_base_is_noop(self, tmp_path, monkeypatch):
        base = tmp_path / "does-not-exist"
        monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", base)
        result = clear_all()
        assert result["deleted"] is False


# =========================================================================
# Task 4 — CheckpointRef + create_checkpoint/restore_checkpoint + force=True
# =========================================================================


class TestCheckpointRefAndForced:
    """Task 4 surface: distinct, forced checkpoints per transaction. The
    legacy one-per-turn path (``ensure_checkpoint``) is preserved unchanged
    for callers outside missions."""

    def test_checkpoint_ref_is_frozen(self):
        from tools.checkpoint_manager import CheckpointRef
        from dataclasses import FrozenInstanceError
        ref = CheckpointRef(
            checkpoint_id="ck-1", working_dir="/tmp/x",
            commit_hash="abcd1234", created_at=0,
        )
        with pytest.raises(FrozenInstanceError):
            ref.commit_hash = "other"  # type: ignore[misc]

    def test_create_checkpoint_returns_checkpoint_ref(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        from tools.checkpoint_manager import CheckpointManager, CheckpointRef
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        ref = mgr.create_checkpoint(str(work_dir), reason="t1")
        assert isinstance(ref, CheckpointRef)
        assert ref.working_dir == str(work_dir.resolve())
        assert len(ref.commit_hash) >= 4
        assert ref.created_at > 0

    def test_create_checkpoint_force_true_yields_distinct_refs(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        from tools.checkpoint_manager import CheckpointManager
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        # Force=True bypasses the dedup set so each call produces a distinct
        # ref even when invoked twice in the same turn.
        (work_dir / "a.txt").write_text("1\n")
        ref1 = mgr.create_checkpoint(str(work_dir), reason="t1", force=True)
        (work_dir / "b.txt").write_text("2\n")
        ref2 = mgr.create_checkpoint(str(work_dir), reason="t2", force=True)
        assert ref1.commit_hash != ref2.commit_hash

    def test_create_checkpoint_force_true_distinct_for_identical_tree(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """Spec: forced checkpoints must produce distinct commit hashes even
        when the tree is byte-identical between calls — a forced checkpoint
        is per-transaction, not dedup-eligible. The legacy
        ``.hades-checkpoint-marker`` workaround must NOT be used
        (it leaks into the restored tree)."""
        from tools.checkpoint_manager import CheckpointManager
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        # No file changes between calls — tree is identical.
        ref1 = mgr.create_checkpoint(str(work_dir), reason="t1", force=True)
        ref2 = mgr.create_checkpoint(str(work_dir), reason="t2", force=True)
        assert ref1.commit_hash != ref2.commit_hash
        assert ref1.checkpoint_id != ref2.checkpoint_id

    def test_force_checkpoint_does_not_create_marker_file(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """Spec: the forced-checkpoint path must not touch
        ``.hades-checkpoint-marker`` on disk at all (the prior
        workaround would leak the marker into the restored tree)."""
        from tools.checkpoint_manager import CheckpointManager
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        mgr.create_checkpoint(str(work_dir), reason="t1", force=True)
        assert not (work_dir / ".hades-checkpoint-marker").exists()

    def test_ensure_checkpoint_outside_missions_still_dedups(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """Legacy one-per-turn dedup behavior is preserved for callers that
        route through ``ensure_checkpoint`` (non-mission callers)."""
        from tools.checkpoint_manager import CheckpointManager
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        assert mgr.ensure_checkpoint(str(work_dir), "first") is True
        # Same turn: dedup → False.
        assert mgr.ensure_checkpoint(str(work_dir), "second") is False
        # New turn: True again.
        mgr.new_turn()
        (work_dir / "main.py").write_text("changed\n")
        assert mgr.ensure_checkpoint(str(work_dir), "third") is True

    def test_restore_checkpoint_rejects_root_mismatch(
        self, work_dir, checkpoint_base, monkeypatch, tmp_path
    ):
        """A CheckpointRef whose resolved root does NOT match the current
        workspace root must fail closed — no silent cross-checkout restore."""
        from tools.checkpoint_manager import CheckpointManager, CheckpointRef
        # Make a sibling dir that we'll pretend is the ref's working_dir.
        sibling = tmp_path / "other-workdir"
        sibling.mkdir()
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        # Real checkpoint in work_dir.
        mgr.ensure_checkpoint(str(work_dir), "before")
        cps = mgr.list_checkpoints(str(work_dir))
        assert cps
        commit = cps[0]["hash"]
        # Build a ref whose working_dir is the SIBLING — restore must fail.
        wrong_ref = CheckpointRef(
            checkpoint_id="ck-fake",
            working_dir=str(sibling.resolve()),
            commit_hash=commit,
            created_at=int(time.time()),
        )
        with pytest.raises(PermissionError):
            mgr.restore_checkpoint(wrong_ref, current_root=str(work_dir))

    def test_restore_checkpoint_round_trips(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """A CheckpointRef matching the current root restores bytes/mode/
        deletion state."""
        from tools.checkpoint_manager import CheckpointManager
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        # Create a file, checkpoint, mutate it, restore.
        target = work_dir / "data.txt"
        target.write_text("original\n")
        ref = mgr.create_checkpoint(str(work_dir), reason="before-mutation")
        target.write_text("mutated\n")
        mgr.restore_checkpoint(ref, current_root=str(work_dir))
        assert target.read_text() == "original\n"

    def test_restore_checkpoint_does_not_leave_marker_file(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """Spec: even after a checkpoint + restore round-trip the working
        tree must NOT contain ``.hades-checkpoint-marker`` — the legacy
        forced-checkpoint workaround used that file to create distinct
        trees, but it leaked into restored snapshots. The new path uses
        ``commit-tree`` against an empty tree bypass."""
        from tools.checkpoint_manager import CheckpointManager
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        # Make real content; otherwise forced checkpoint may be a no-op
        # via empty-tree and could legitimately report nothing.
        (work_dir / "f.txt").write_text("f\n")
        ref = mgr.create_checkpoint(str(work_dir), reason="t", force=True)
        assert not (work_dir / ".hades-checkpoint-marker").exists()
        # Mutate, then restore.
        (work_dir / "f.txt").write_text("mutated\n")
        mgr.restore_checkpoint(ref, current_root=str(work_dir))
        # Marker still absent after restore.
        assert not (work_dir / ".hades-checkpoint-marker").exists()
        # Working tree matches the checkpoint tip.
        assert (work_dir / "f.txt").read_text() == "f\n"


# ---------------------------------------------------------------------------
# Task 4 final remediation — shared restore guards
# ---------------------------------------------------------------------------


class TestTask4SharedRestoreGuards:
    """Shared restore() guards centralized for both ``restore`` and
    ``restore_checkpoint``: a commit hash that is NOT an ancestor of the
    project ref (or doesn't equal it) is rejected loudly so a forged
    ``CheckpointRef`` cannot clobber another project's tree.
    """

    def test_restore_rejects_forged_commit_from_unrelated_project(
        self, work_dir, checkpoint_base, monkeypatch, tmp_path
    ):
        """A commit hash that lives on a DIFFERENT project's ref MUST
        be rejected by ``restore()`` even though it validates as hex
        and ``git cat-file -t`` succeeds. Pre-rollback snapshot must
        NOT be taken and the working tree must be unchanged."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        # Project A: take a real checkpoint.
        (work_dir / "a.txt").write_text("A\n")
        ref_a = mgr.create_checkpoint(str(work_dir), reason="a")
        # Project B: separate worktree, separate ref.
        project_b = tmp_path / "project-b"
        project_b.mkdir()
        (project_b / "b.txt").write_text("B\n")
        mgr.create_checkpoint(str(project_b), reason="b")
        # The forged commit hash is the project B tip — cat-file
        # recognises it (the legacy guards alone aren't enough).
        b_cps = mgr.list_checkpoints(str(project_b))
        assert b_cps
        forged_hash = b_cps[0]["hash"]
        ok, _, _ = _run_git(
            ["cat-file", "-t", forged_hash],
            _store_path(checkpoint_base), str(project_b),
        )
        assert ok, "sanity: cat-file must recognise the forged hash"
        # Now attempt restore in project A's work_dir using the B
        # commit. Restore returns a failed result (the same contract
        # as legacy failure paths — ``restore_checkpoint`` wraps the
        # failure in RuntimeError).
        before_text = (work_dir / "a.txt").read_text()
        result = mgr.restore(str(work_dir), forged_hash)
        assert result.get("success") is False
        # The error message names the ancestor-mismatch reason so
        # operators / adapters can distinguish forged from invalid.
        assert "ancestor" in result.get("error", "").lower()
        # work_dir was untouched.
        assert (work_dir / "a.txt").read_text() == before_text

    def test_restore_accepts_legitimate_ancestor_commit(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """A commit that IS an ancestor of (or equals) the current
        project ref tip must restore successfully — the legitimate
        historical checkpoint path is preserved."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        (work_dir / "f.txt").write_text("v1\n")
        ref_v1 = mgr.create_checkpoint(
            str(work_dir), reason="v1", force=True,
        )
        (work_dir / "f.txt").write_text("v2\n")
        mgr.create_checkpoint(str(work_dir), reason="v2", force=True)
        # Restore to v1 — must succeed (v1 IS an ancestor of v2 tip).
        mgr.restore(str(work_dir), ref_v1.commit_hash)
        assert (work_dir / "f.txt").read_text() == "v1\n"

    def test_restore_rejects_commit_unknown_to_project_ref(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """A hex-valid, cat-file-recognised commit that is NOT in this
        project's ref history at all must be rejected — even when it
        exists somewhere in the shared store."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        (work_dir / "x.txt").write_text("x\n")
        mgr.create_checkpoint(str(work_dir), reason="x", force=True)
        # Make a separate ref/commit independent of work_dir's ref.
        decoupled = checkpoint_base.parent / "decoupled"
        decoupled.mkdir(parents=True, exist_ok=True)
        (decoupled / "d.txt").write_text("d\n")
        mgr.create_checkpoint(str(decoupled), reason="d", force=True)
        d_ck = mgr.list_checkpoints(str(decoupled))
        assert d_ck
        # The decoupled commit MUST NOT be an ancestor of work_dir's
        # current ref tip (they are independent projects). If this
        # ever passes, our ancestor check has been bypassed.
        d_hash = d_ck[0]["hash"]
        tip_ok, tip, _ = _run_git(
            ["rev-parse", "--verify",
             _ref_name(_project_hash(str(work_dir))) + "^{commit}"],
            _store_path(checkpoint_base), str(work_dir),
            allowed_returncodes={128},
        )
        assert tip_ok
        # Try to restore the decoupled commit into work_dir.
        result = mgr.restore(str(work_dir), d_hash)
        assert result.get("success") is False
        assert "ancestor" in result.get("error", "").lower()

    def test_restore_checkpoint_rejects_forged_commit_via_direct_call(
        self, work_dir, checkpoint_base, monkeypatch, tmp_path
    ):
        """Same forged-commit defence for ``restore_checkpoint`` when a
        caller constructs a CheckpointRef directly with a commit hash
        that doesn't belong to this project."""
        from tools.checkpoint_manager import CheckpointManager, CheckpointRef
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        (work_dir / "a.txt").write_text("A\n")
        mgr.create_checkpoint(str(work_dir), reason="a")
        project_b = tmp_path / "project-b"
        project_b.mkdir()
        (project_b / "b.txt").write_text("B\n")
        mgr.create_checkpoint(str(project_b), reason="b")
        b_ck = mgr.list_checkpoints(str(project_b))
        assert b_ck
        wrong_ref = CheckpointRef(
            checkpoint_id="ck-forge",
            working_dir=str(work_dir.resolve()),
            commit_hash=b_ck[0]["hash"],
            created_at=int(time.time()),
        )
        with pytest.raises(RuntimeError):
            mgr.restore_checkpoint(wrong_ref, current_root=str(work_dir))

    def test_restore_checkpoint_file_paths_isolates_unrelated_sibling(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """``restore_checkpoint(file_paths=[...])`` must restore ONLY
        the declared target paths. An unrelated sibling edited by a
        human between commit and compensate must NOT be clobbered."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        # Two files: target (mission-mutated) and sibling (human-edited).
        target = work_dir / "target.txt"
        sibling = work_dir / "sibling.txt"
        target.write_text("orig\n")
        sibling.write_text("orig-sibling\n")
        # Snapshot both files via checkpoint.
        ref = mgr.create_checkpoint(str(work_dir), reason="snapshot")
        # Mission mutates target; human edits sibling between commit
        # and compensate.
        target.write_text("by-mission\n")
        sibling.write_text("by-human\n")
        # Restore ONLY the target.
        mgr.restore_checkpoint(
            ref,
            current_root=str(work_dir),
            file_paths=[str(target.resolve())],
        )
        # Target reverted, sibling untouched.
        assert target.read_text() == "orig\n"
        assert sibling.read_text() == "by-human\n"

    def test_restore_checkpoint_file_paths_removes_when_target_absent_in_checkpoint(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """``restore_checkpoint(file_paths=[absent_path])`` must remove
        the declared target when it does not exist in the checkpoint
        — this is the deletion-state case for files the mission
        created mid-transaction."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        # Snapshot state where created.txt does NOT exist.
        ref = mgr.create_checkpoint(str(work_dir), reason="before-create")
        # Mission creates the file.
        new_file = work_dir / "created.txt"
        new_file.write_text("new\n")
        # Restore only the created file: it was absent in the
        # checkpoint, so it must be removed.
        mgr.restore_checkpoint(
            ref,
            current_root=str(work_dir),
            file_paths=[str(new_file.resolve())],
        )
        assert not new_file.exists()

    def test_restore_checkpoint_file_paths_rejects_path_outside_root(
        self, work_dir, checkpoint_base, monkeypatch, tmp_path
    ):
        """``restore_checkpoint(file_paths=[...])`` must reject any
        declared path that resolves outside ``current_root`` —
        prevents traversal clobber."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        (work_dir / "a.txt").write_text("a\n")
        ref = mgr.create_checkpoint(str(work_dir), reason="a")
        outside = tmp_path / "outside.txt"
        outside.write_text("outside\n")
        with pytest.raises(PermissionError):
            mgr.restore_checkpoint(
                ref,
                current_root=str(work_dir),
                file_paths=[str(outside.resolve())],
            )
        assert outside.read_text() == "outside\n"

    def test_restore_checkpoint_file_paths_rejects_directory(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """``restore_checkpoint(file_paths=[<dir>])`` must reject
        directories — the contract is file/symlink-only (file tool
        adapter scope)."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        (work_dir / "a.txt").write_text("a\n")
        ref = mgr.create_checkpoint(str(work_dir), reason="a")
        sub = work_dir / "subdir"
        sub.mkdir()
        with pytest.raises(ValueError):
            mgr.restore_checkpoint(
                ref,
                current_root=str(work_dir),
                file_paths=[str(sub.resolve())],
            )

    def test_restore_checkpoint_with_no_file_paths_preserves_legacy_whole_root(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """Public contract: omitting ``file_paths`` (legacy callers)
        restores tracked-file state under the root — the historical
        full-root path — while leaving untracked post-checkpoint
        files alone (standard ``git checkout`` semantics)."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        (work_dir / "f.txt").write_text("orig\n")
        ref = mgr.create_checkpoint(str(work_dir), reason="orig")
        # Mutate tracked file; legacy behaviour reverts it on
        # whole-root restore.
        (work_dir / "f.txt").write_text("mutated\n")
        mgr.restore_checkpoint(ref, current_root=str(work_dir))
        assert (work_dir / "f.txt").read_text() == "orig\n"


# ---------------------------------------------------------------------------
# Task 4 final — per-path deletion-ordering vulnerability
# ---------------------------------------------------------------------------


class TestTask4PerPathDeletionOrderingGuard:
    """Restore must validate project-ref membership BEFORE any workspace
    mutation. The per-path branch previously ran ``cat-file`` then
    could ``unlink()`` an A-target absent from B's commit (where
    ``working_dir=A`` but ``commit_hash=B``) BEFORE the membership
    check inside ``restore()`` had a chance to fire (it only runs for
    targets present in the checkpoint tree).
    """

    def test_restore_checkpoint_per_path_rejects_forged_ref_before_delete(
        self, work_dir, checkpoint_base, monkeypatch, tmp_path
    ):
        """A forged ``CheckpointRef(working_dir=A, commit_hash=B)``
        supplied to ``restore_checkpoint(file_paths=[A-target-abs])``
        must raise before touching the workspace — A's target content
        is preserved even though B's commit does not contain it
        (so the per-path branch would otherwise take the
        ``unlink()`` delete path)."""
        from tools.checkpoint_manager import CheckpointManager, CheckpointRef
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        # Project A: real checkpoint with target.txt present.
        target = work_dir / "target.txt"
        target.write_text("a-content\n")
        mgr.create_checkpoint(str(work_dir), reason="a")
        # Project B: independent ref whose commit lacks target.txt.
        project_b = tmp_path / "project-b"
        project_b.mkdir()
        (project_b / "b.txt").write_text("b-content\n")
        mgr.create_checkpoint(str(project_b), reason="b")
        b_ck = mgr.list_checkpoints(str(project_b))
        assert b_ck
        forged = CheckpointRef(
            checkpoint_id="ck-forge-delete",
            working_dir=str(work_dir.resolve()),
            commit_hash=b_ck[0]["hash"],
            created_at=int(time.time()),
        )
        with pytest.raises((RuntimeError, PermissionError)):
            mgr.restore_checkpoint(
                forged,
                current_root=str(work_dir),
                file_paths=[str(target.resolve())],
            )
        # A's target content intact — the unlink branch was NOT
        # reached because membership validation fired first.
        assert target.read_text() == "a-content\n"
        assert target.exists()

    def test_restore_checkpoint_per_path_legitimate_restore_still_creates_then_deletes(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """Legitimate per-path creation/deletion restore remains
        intact: the ordering guard is added on top of the
        membership check, not replacing it."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        # Snapshot state where created.txt does NOT exist.
        ref = mgr.create_checkpoint(str(work_dir), reason="before-create")
        new_file = work_dir / "created.txt"
        new_file.write_text("new\n")
        # Restore only the created file: it was absent in the
        # checkpoint, so it must be removed (legitimate deletion).
        mgr.restore_checkpoint(
            ref,
            current_root=str(work_dir),
            file_paths=[str(new_file.resolve())],
        )
        assert not new_file.exists()


class TestCreateCheckpointRobustness:
    """``create_checkpoint`` broad-path / unset-variable robustness."""

    def test_create_checkpoint_rejects_root_path(self):
        from tools.checkpoint_manager import CheckpointManager
        mgr = CheckpointManager(enabled=True)
        with pytest.raises(PermissionError):
            mgr.create_checkpoint("/", reason="root")

    def test_create_checkpoint_rejects_home_path(self):
        from tools.checkpoint_manager import CheckpointManager
        mgr = CheckpointManager(enabled=True)
        with pytest.raises(PermissionError):
            mgr.create_checkpoint(str(Path.home()), reason="home")

    def test_init_store_failure_yields_clean_runtimeerror(
        self, work_dir, checkpoint_base, monkeypatch
    ):
        """When ``_init_store`` returns an error string, ``_take``
        must not raise ``UnboundLocalError``; ``create_checkpoint``
        must raise ``RuntimeError`` (loud, not silent partial state)."""
        from tools.checkpoint_manager import CheckpointManager
        import tools.checkpoint_manager as _cm

        def boom(store, working_dir):
            return "simulated init failure"
        monkeypatch.setattr(_cm, "_init_store", boom)
        mgr = CheckpointManager(enabled=True)
        with pytest.raises(RuntimeError):
            mgr.create_checkpoint(str(work_dir), reason="boom")

    def test_empty_first_tree_yields_clean_runtimeerror(
        self, tmp_path, checkpoint_base, monkeypatch
    ):
        """A first non-forced empty tree must keep _take's tuple contract."""
        import tools.checkpoint_manager as checkpoint_module
        from tools.checkpoint_manager import CheckpointManager

        monkeypatch.setattr(checkpoint_module, "CHECKPOINT_BASE", checkpoint_base)
        empty_work_dir = tmp_path / "empty-project"
        empty_work_dir.mkdir()
        mgr = CheckpointManager(enabled=True)
        with pytest.raises(RuntimeError, match="checkpoint creation failed"):
            mgr.create_checkpoint(str(empty_work_dir), reason="empty")


# =========================================================================
# Task 4 final — concurrency lock + immutable CheckpointRef across pruning
# =========================================================================


class TestSharedCheckpointStateLock:
    """Spec: ``_checkpoint_state_lock`` is per-instance; create_checkpoint
    releases it around ``_take``; restore membership is checked OUTSIDE the
    lock. Two managers (or a forced concurrent checkpoint + prune) can
    race so membership validates against a tip that the prune step then
    garbage-collects — restore then fails on ``cat-file`` rather than
    recognising the ref as pruned/missing.

    The fix is ONE module/class-level RLock shared across all
    CheckpointManager instances and all store-touching ops
    (``ensure_checkpoint``, ``create_checkpoint``, ``restore``,
    ``restore_checkpoint``). RLock permits restore_checkpoint→restore
    re-entry without deadlocking.
    """

    def test_module_level_rlock_shared_across_managers(self):
        from tools import checkpoint_manager as _cm
        # The shared lock must exist on the module (not per-instance).
        assert hasattr(_cm, "_checkpoint_state_lock"), (
            "shared module-level RLock missing — concurrency guard "
            "still per-instance"
        )
        lock = _cm._checkpoint_state_lock
        # RLock (re-entrant) so restore_checkpoint→restore re-entry is safe.
        assert isinstance(lock, type(threading.RLock())), (
            "shared lock must be threading.RLock so nested "
            "restore_checkpoint→restore calls re-enter"
        )
        # Two distinct managers must see the SAME lock object.
        from tools.checkpoint_manager import CheckpointManager
        m1 = CheckpointManager(enabled=True)
        m2 = CheckpointManager(enabled=True)
        assert m1._checkpoint_state_lock is m2._checkpoint_state_lock, (
            "managers must share the module-level lock"
        )

    def test_create_checkpoint_lock_holds_through_take_and_prune(
        self, work_dir, checkpoint_base, monkeypatch,
    ):
        """The shared lock must span the entire ``create_checkpoint`` →
        ``_take`` → ``_prune`` path. We force a hook that asserts the lock
        is held during ``_take``; if the lock is per-instance or released
        around ``_take``, the hook sees an unlocked state."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        from tools import checkpoint_manager as _cm
        held_during_take = []

        original_take = _cm.CheckpointManager._take

        def guarded_take(self, working_dir, reason, *, force=False, pin_ref=None):
            held_during_take.append(
                _cm._checkpoint_state_lock._is_owned()  # type: ignore[attr-defined]
            )
            return original_take(
                self, working_dir, reason, force=force, pin_ref=pin_ref,
            )

        monkeypatch.setattr(
            _cm.CheckpointManager, "_take", guarded_take,
        )
        mgr = CheckpointManager(enabled=True)
        mgr.create_checkpoint(str(work_dir), reason="race-test", force=True)
        assert held_during_take == [True], (
            "create_checkpoint must hold the shared lock across "
            "the entire _take (and its pruning)"
        )

    def test_ensure_checkpoint_lock_holds_through_take(
        self, work_dir, checkpoint_base, monkeypatch,
    ):
        """The legacy path must hold the shared lock while ``_take`` runs."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        from tools import checkpoint_manager as _cm
        held_during_take = []
        original_take = _cm.CheckpointManager._take

        def guarded_take(self, working_dir, reason, *, force=False, pin_ref=None):
            held_during_take.append(
                _cm._checkpoint_state_lock._is_owned()  # type: ignore[attr-defined]
            )
            return original_take(
                self, working_dir, reason, force=force, pin_ref=pin_ref,
            )

        monkeypatch.setattr(_cm.CheckpointManager, "_take", guarded_take)
        mgr = CheckpointManager(enabled=True)
        assert mgr.ensure_checkpoint(str(work_dir), reason="legacy") is True
        assert held_during_take == [True]

    def test_restore_checkpoint_lock_holds_through_full_restore(
        self, work_dir, checkpoint_base, monkeypatch,
    ):
        """``restore_checkpoint(file_paths=...)`` must hold the shared
        lock from membership validation through checkout/unlink so a
        forced concurrent checkpoint/prune cannot invalidate the ref
        between validation and per-path restore."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        from tools import checkpoint_manager as _cm
        held_during_restore = []

        original_restore = _cm.CheckpointManager.restore

        def guarded_restore(self, working_dir, commit_hash, file_path=None, **kwargs):
            held_during_restore.append(
                _cm._checkpoint_state_lock._is_owned()  # type: ignore[attr-defined]
            )
            return original_restore(self, working_dir, commit_hash, file_path)

        monkeypatch.setattr(
            _cm.CheckpointManager, "restore", guarded_restore,
        )

        target = work_dir / "f.txt"
        target.write_text("orig\n")
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        ref = mgr.create_checkpoint(str(work_dir), reason="before")
        target.write_text("mutated\n")
        mgr.restore_checkpoint(
            ref, current_root=str(work_dir),
            file_paths=[str(target.resolve())],
        )
        assert held_during_restore == [True], (
            "restore_checkpoint must hold the shared lock across "
            "the per-path restore (membership→checkout/unlink)"
        )


class TestImmutableCheckpointRefAcrossPruning:
    """Spec: ``_prune`` rewrites commits and changes hashes; the ref
    referenced by a forced CheckpointRef can be invalidated at
    ``max_snapshots``. The fix: a durable Git pin ref outside
    ``_REFS_PREFIX`` keeps the target commit alive so
    ``restore_checkpoint`` succeeds even when the project ref no longer
    contains the checkpoint.

    The pin is named ``refs/hermes-pin/<dirhash>/<checkpoint_id>`` —
    outside the project-ref namespace so ``_enforce_size_cap`` does not
    treat it as a project ref to prune. Direct ``restore_checkpoint``
    on a CheckpointRef accepts either a project-history ancestor OR
    an exact matching pin for the same project/checkpoint_id.
    """

    def _create_pin_ref(self, mgr, working_dir):
        """Drive a real create_checkpoint and verify a pin ref was created
        for the returned CheckpointRef."""
        from tools.checkpoint_manager import _run_git, _store_path
        ref = mgr.create_checkpoint(
            str(working_dir), reason="pinned", force=True,
        )
        store = _store_path()
        # The pin lives at refs/hermes-pin/<dirhash>/<checkpoint_id>
        from tools.checkpoint_manager import _project_hash
        dir_hash = _project_hash(str(working_dir))
        pin_ref = f"refs/hermes-pin/{dir_hash}/{ref.checkpoint_id}"
        ok, sha, _ = _run_git(
            ["rev-parse", "--verify", pin_ref + "^{commit}"], store,
            str(working_dir), allowed_returncodes={128},
        )
        assert ok, f"pin ref {pin_ref} not created for Task4 CheckpointRef"
        assert sha == ref.commit_hash, (
            f"pin sha {sha!r} does not match CheckpointRef commit_hash "
            f"{ref.commit_hash!r}"
        )
        return ref, pin_ref, sha

    def test_create_checkpoint_creates_durable_pin_ref(
        self, work_dir, checkpoint_base, monkeypatch,
    ):
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        ref, pin_ref, _ = self._create_pin_ref(mgr, work_dir)
        # Pin ref exists, is a valid commit, and resolves to ref.commit_hash.
        # (already asserted inside the helper)

    def test_pin_ref_survives_max_snapshots_pruning(
        self, work_dir, checkpoint_base, monkeypatch,
    ):
        """A Task4 CheckpointRef must remain restorable after the project
        history has been pruned past ``max_snapshots``. The pin ref keeps
        the target commit alive even when the project ref rewrites it."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        # Use max_snapshots=2 so the third forced checkpoint prunes the
        # first one off the project ref.
        mgr = CheckpointManager(enabled=True, max_snapshots=2)
        from tools.checkpoint_manager import _project_hash, _run_git, _store_path
        observed_pin_during_prune = []
        original_prune = mgr._prune

        def observing_prune(store, working_dir, ref):
            pin_prefix = f"refs/hermes-pin/{_project_hash(working_dir)}"
            ok_pins, pins, _ = _run_git(
                ["for-each-ref", "--format=%(refname)", pin_prefix],
                store, working_dir, allowed_returncodes={128},
            )
            observed_pin_during_prune.append(ok_pins and bool(pins.strip()))
            return original_prune(store, working_dir, ref)

        monkeypatch.setattr(mgr, "_prune", observing_prune)
        # Create two snapshots (the older one will be pruned by the
        # third create_checkpoint).
        (work_dir / "f.txt").write_text("v1\n")
        ref_v1 = mgr.create_checkpoint(
            str(work_dir), reason="v1", force=True,
        )
        (work_dir / "f.txt").write_text("v2\n")
        ref_v2 = mgr.create_checkpoint(
            str(work_dir), reason="v2", force=True,
        )
        # A third forced checkpoint exceeds max_snapshots and triggers
        # pruning — the project ref now starts at ref_v2.
        (work_dir / "f.txt").write_text("v3\n")
        mgr.create_checkpoint(str(work_dir), reason="v3", force=True)
        # Project ref no longer contains ref_v1.commit_hash directly.
        from tools.checkpoint_manager import _ref_name
        store = _store_path()
        dir_hash = _project_hash(str(work_dir))
        proj_ref = _ref_name(dir_hash)
        ok, tip, _ = _run_git(
            ["rev-parse", "--verify", proj_ref + "^{commit}"],
            store, str(work_dir),
        )
        assert ok
        # ref_v1 is NOT an ancestor of the current project ref tip.
        ok_anc, _, _ = _run_git(
            ["merge-base", "--is-ancestor", ref_v1.commit_hash, tip],
            store, str(work_dir), allowed_returncodes={1, 128},
        )
        assert not ok_anc, (
            "ref_v1 must have been pruned off the project ref for "
            "this test to be meaningful"
        )
        # Yet restore_checkpoint(ref_v1, ...) succeeds via the pin ref,
        # because pin refs preserve target commit objects across pruning.
        # Verify the pin still resolves to ref_v1.commit_hash.
        pin_ref = f"refs/hermes-pin/{dir_hash}/{ref_v1.checkpoint_id}"
        ok_pin, pin_sha, _ = _run_git(
            ["rev-parse", "--verify", pin_ref + "^{commit}"],
            store, str(work_dir), allowed_returncodes={128},
        )
        assert ok_pin and pin_sha == ref_v1.commit_hash, (
            "pin must keep ref_v1.commit_hash reachable"
        )
        assert observed_pin_during_prune and all(observed_pin_during_prune), (
            "checkpoint pins must exist before pruning starts"
        )
        # Restore_checkpoint of ref_v1 succeeds and content is exact.
        target = work_dir / "f.txt"
        target.write_text("v3-on-disk\n")
        mgr.restore_checkpoint(
            ref_v1, current_root=str(work_dir),
            file_paths=[str(target.resolve())],
        )
        assert target.read_text() == "v1\n"

    def test_pin_update_failure_raises_loudly(
        self, work_dir, checkpoint_base, monkeypatch,
    ):
        """A failed Task 4 pin update must not return an unusable ref."""
        from tools import checkpoint_manager as _cm
        monkeypatch.setattr(_cm, "CHECKPOINT_BASE", checkpoint_base)
        original_run_git = _cm._run_git

        def fail_pin_update(args, store, working_dir, *positional, **kwargs):
            if args[:1] == ["update-ref"] and args[1].startswith("refs/hermes-pin/"):
                return False, "", "simulated pin failure"
            return original_run_git(
                args, store, working_dir, *positional, **kwargs,
            )

        monkeypatch.setattr(_cm, "_run_git", fail_pin_update)
        mgr = _cm.CheckpointManager(enabled=True, max_snapshots=2)
        with pytest.raises(RuntimeError):
            mgr.create_checkpoint(str(work_dir), reason="pin failure", force=True)

    def test_forged_foreign_checkpoint_id_pin_fails_loud(
        self, work_dir, checkpoint_base, monkeypatch, tmp_path,
    ):
        """A pin ref for a foreign project's checkpoint_id must NOT be
        accepted as a substitute for the local project's CheckpointRef —
        even if the underlying commit hash matches. The pin membership
        check verifies (dir_hash, checkpoint_id) match the ref being
        restored; a foreign pin is rejected."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        # Project A: real checkpoint with a real pin.
        mgr_a = CheckpointManager(enabled=True, max_snapshots=50)
        (work_dir / "a.txt").write_text("A\n")
        ref_a = mgr_a.create_checkpoint(str(work_dir), reason="A", force=True)
        # Project B: independent checkpoint (separate worktree + dir hash).
        project_b = tmp_path / "project-b"
        project_b.mkdir()
        (project_b / "b.txt").write_text("B\n")
        mgr_b = CheckpointManager(enabled=True, max_snapshots=50)
        ref_b = mgr_b.create_checkpoint(str(project_b), reason="B", force=True)
        # Forged ref: work_dir (A's project) but commit_hash == B's pin
        # commit. A is NOT an ancestor of B; the pin for ref_b lives
        # under project_b's dir_hash. Membership must fail loud.
        from tools.checkpoint_manager import CheckpointRef
        forged = CheckpointRef(
            checkpoint_id=ref_a.checkpoint_id,  # pretend "same id" — irrelevant
            working_dir=str(work_dir.resolve()),
            commit_hash=ref_b.commit_hash,
            created_at=int(time.time()),
        )
        with pytest.raises((RuntimeError, PermissionError)):
            mgr_a.restore_checkpoint(
                forged, current_root=str(work_dir),
                file_paths=[str((work_dir / "a.txt").resolve())],
            )
        # A's content intact.
        assert (work_dir / "a.txt").read_text() == "A\n"

    def test_forged_pin_for_wrong_project_root_fails_loud(
        self, work_dir, checkpoint_base, monkeypatch, tmp_path,
    ):
        """A pin that exists but under a DIFFERENT project root must not
        be honoured when restoring for the current project root."""
        from tools.checkpoint_manager import CheckpointManager
        monkeypatch.setattr(
            "tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base,
        )
        mgr = CheckpointManager(enabled=True, max_snapshots=50)
        (work_dir / "a.txt").write_text("A\n")
        ref = mgr.create_checkpoint(str(work_dir), reason="A", force=True)
        # Hand-construct a forged ref whose working_dir is a foreign
        # root but commit_hash is ref.commit_hash.
        from tools.checkpoint_manager import CheckpointRef
        other_root = tmp_path / "foreign-root"
        other_root.mkdir()
        forged = CheckpointRef(
            checkpoint_id=ref.checkpoint_id,
            working_dir=str(other_root.resolve()),
            commit_hash=ref.commit_hash,
            created_at=int(time.time()),
        )
        # The root-mismatch check is irrelevant here because the ref
        # is for ANOTHER project root that has no checkpoints at all.
        # The forge guard (no history exists for foreign-root) MUST
        # fail loud — RuntimeError or PermissionError both qualify.
        with pytest.raises((RuntimeError, PermissionError)):
            mgr.restore_checkpoint(forged, current_root=str(other_root))
