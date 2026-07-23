"""Behavioral regressions for shell-style assignments in the Hades env store."""

from __future__ import annotations

import os

import pytest


OLD_TOKEN = "token-" + "a" * 24
NEW_TOKEN = "token-" + "b" * 24


@pytest.fixture()
def hades_home(tmp_path, monkeypatch):
    home = tmp_path / "hades-home"
    home.mkdir()
    monkeypatch.setenv("HADES_HOME", str(home))
    monkeypatch.delenv("HERMES_HOME", raising=False)

    from hades_cli.config import invalidate_env_cache

    invalidate_env_cache()
    yield home
    invalidate_env_cache()
    os.environ.pop("GITHUB_TOKEN", None)


def test_save_replaces_export_assignment_without_creating_a_duplicate(hades_home):
    env_path = hades_home / ".env"
    env_path.write_text(
        (
            f"# export GITHUB_TOKEN={OLD_TOKEN}\n"
            f"export GITHUB_TOKEN={OLD_TOKEN}\n"
            f"GITHUB_TOKEN={OLD_TOKEN}\n"
            "OTHER_SETTING=preserved\n"
        ),
        encoding="utf-8",
    )

    from hades_cli.config import load_env, save_env_value

    save_env_value("GITHUB_TOKEN", NEW_TOKEN)

    assignments = [
        line
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if "GITHUB_TOKEN=" in line and not line.lstrip().startswith("#")
    ]
    assert assignments == [f"GITHUB_TOKEN={NEW_TOKEN}"]
    assert f"# export GITHUB_TOKEN={OLD_TOKEN}" in env_path.read_text(
        encoding="utf-8"
    )
    assert load_env()["GITHUB_TOKEN"] == NEW_TOKEN


def test_remove_deletes_export_assignment_but_preserves_comments(hades_home):
    env_path = hades_home / ".env"
    env_path.write_text(
        (
            f"# export GITHUB_TOKEN={OLD_TOKEN}\n"
            f"export GITHUB_TOKEN={NEW_TOKEN}\n"
            "OTHER_SETTING=preserved\n"
        ),
        encoding="utf-8",
    )

    from hades_cli.config import load_env, remove_env_value

    assert remove_env_value("GITHUB_TOKEN") is True

    text = env_path.read_text(encoding="utf-8")
    assert f"# export GITHUB_TOKEN={OLD_TOKEN}" in text
    assert f"export GITHUB_TOKEN={NEW_TOKEN}" not in text
    assert "OTHER_SETTING=preserved" in text
    assert "GITHUB_TOKEN" not in load_env()
