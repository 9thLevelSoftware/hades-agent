"""End-to-end coverage for the Hades v23 session-store foreground command."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hades_state import SessionDB


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run_hades(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HADES_HOME"] = str(home)
    env.pop("HERMES_HOME", None)
    return subprocess.run(
        [sys.executable, "-m", "hades_cli.main", *args],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def _make_legacy_fts_db(home: Path) -> Path:
    """Create a real pre-v23 inline FTS database at the Hades home."""
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        db._drop_fts_triggers(db._conn)
        db._conn.execute("DROP TABLE IF EXISTS messages_fts")
        db._conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
        db._conn.commit()
    finally:
        db.close()
    return db_path


def test_hades_optimize_storage_is_a_real_foreground_command(tmp_path):
    home = tmp_path / ".hades"
    _make_legacy_fts_db(home)

    result = _run_hades(home, "sessions", "optimize-storage", "--yes", "--no-vacuum")

    assert result.returncode == 0, result.stderr
    assert "Hades" in result.stdout
    assert "hades sessions optimize" in result.stdout
    db = SessionDB(db_path=home / "state.db")
    try:
        assert not db.fts_optimize_available()
    finally:
        db.close()


def test_hades_optimize_storage_leaves_an_already_compact_store_untouched(tmp_path):
    home = tmp_path / ".hades"
    home.mkdir()
    db = SessionDB(db_path=home / "state.db")
    db.close()

    result = _run_hades(home, "sessions", "optimize-storage", "--yes")

    assert result.returncode == 0, result.stderr
    assert "already on the compact layout" in result.stdout
    assert "Hades" in result.stdout


def test_hades_optimize_storage_resumes_an_interrupted_rebuild(tmp_path):
    home = tmp_path / ".hades"
    db_path = _make_legacy_fts_db(home)
    db = SessionDB(db_path=db_path)
    try:
        db._demote_legacy_fts_to_trash()
        assert db.fts_optimize_available()
    finally:
        db.close()

    result = _run_hades(home, "sessions", "optimize-storage", "--yes", "--no-vacuum")

    assert result.returncode == 0, result.stderr
    assert "Resuming" in result.stdout
    db = SessionDB(db_path=db_path)
    try:
        assert not db.fts_optimize_available()
    finally:
        db.close()


def test_hades_update_notice_uses_hades_config_and_guidance(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hades"
    db_path = _make_legacy_fts_db(home)
    monkeypatch.setenv("HADES_HOME", str(home))

    import hades_cli.main as main

    original_stat = Path.stat

    def large_state_stat(path: Path, *args, **kwargs):
        stat = original_stat(path, *args, **kwargs)
        if path == db_path:
            return os.stat_result((*stat[:6], 1024**3, *stat[7:]))
        return stat

    monkeypatch.setattr(Path, "stat", large_state_stat)
    main._print_fts_optimize_available_notice()

    output = capsys.readouterr().out
    assert "hades sessions optimize-storage" in output
    assert "Hades" in output
