"""Thread-local workspace binding for native CLI services."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def test_workspace_context_normalizes_and_restores_without_chdir(tmp_path, monkeypatch):
    from hades_cli.workspace_context import get_workspace_root, workspace_context

    process_cwd = Path.cwd()
    monkeypatch.chdir(tmp_path)
    try:
        assert get_workspace_root() == tmp_path.resolve()
        with workspace_context(tmp_path / "."):
            assert get_workspace_root() == tmp_path.resolve()
        assert get_workspace_root() == tmp_path.resolve()
        assert Path.cwd() == tmp_path
    finally:
        monkeypatch.chdir(process_cwd)


def test_workspace_context_rejects_missing_or_non_directory(tmp_path):
    from hades_cli.workspace_context import workspace_context

    with pytest.raises(ValueError):
        with workspace_context(tmp_path / "missing"):
            pass
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        with workspace_context(file_path):
            pass


def test_workspace_context_isolated_between_concurrent_threads(tmp_path):
    from hades_cli.workspace_context import get_workspace_root, workspace_context

    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()

    def observe(root: Path) -> tuple[Path, Path]:
        with workspace_context(root):
            return get_workspace_root(), Path.cwd()

    with ThreadPoolExecutor(max_workers=2) as pool:
        observed = list(pool.map(observe, roots))

    assert [root for root, _cwd in observed] == [root.resolve() for root in roots]
    assert all(cwd == Path.cwd() for _root, cwd in observed)
    assert os.getcwd() == str(Path.cwd())
