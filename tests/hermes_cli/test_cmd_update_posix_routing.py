"""POSIX no-git routing contracts for damaged source trees."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


def test_posix_no_git_source_tree_with_legacy_git_stamp_does_not_use_pip(
    tmp_path, monkeypatch
):
    """A damaged checkout gets reinstall guidance, never a wheel/tool upgrade."""
    from hades_cli import main as hm

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'hades-agent'\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "install.sh").write_text("#!/usr/bin/env bash\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".install_method").write_text("git\n")
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hm.sys, "platform", "linux")

    with (
        patch("hades_cli.config.load_config", return_value={}),
        patch("hades_cli.config.get_hades_home", return_value=home),
        patch.object(hm, "_run_pre_update_backup"),
        patch.object(hm, "_pause_windows_gateways_for_update", return_value=None),
        patch.object(hm, "_cmd_update_pip") as package_update,
        pytest.raises(SystemExit, match="1"),
    ):
        hm._cmd_update_impl(
            SimpleNamespace(force=True, force_venv=True), gateway_mode=False
        )

    package_update.assert_not_called()
