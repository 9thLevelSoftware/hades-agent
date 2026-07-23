"""Windows no-git routing contracts for package-managed installs."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.mark.parametrize(
    "prefix",
    [
        r"C:\\Users\\example\\AppData\\Local\\pipx\\venvs\\hermes-agent",
        r"C:\\Python313",
    ],
    ids=["pipx", "ordinary-pip"],
)
def test_windows_no_git_packaged_install_uses_package_manager_updater(
    tmp_path, monkeypatch, prefix
):
    """A Windows wheel install must not be mistaken for ZIP-recoverable source."""
    from hades_cli import main as hm

    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hm.sys, "platform", "win32")
    monkeypatch.setattr(hm.sys, "prefix", prefix)

    with (
        patch("hades_cli.config.load_config", return_value={}),
        patch.object(hm, "_run_pre_update_backup") as backup,
        patch.object(
            hm, "_pause_windows_gateways_for_update", return_value="resume-token"
        ) as pause,
        patch.object(hm, "_resume_windows_gateways_after_update") as resume,
        patch.object(hm, "_cmd_update_pip") as package_update,
        patch.object(hm, "_update_via_zip") as zip_update,
    ):
        hm._cmd_update_impl(
            SimpleNamespace(force=True, force_venv=True), gateway_mode=False
        )

    backup.assert_called_once()
    pause.assert_called_once()
    package_update.assert_called_once()
    resume.assert_called_once_with("resume-token")
    zip_update.assert_not_called()


def test_windows_no_git_source_tree_keeps_zip_recovery(tmp_path, monkeypatch):
    """A damaged source checkout still needs the existing Windows ZIP recovery."""
    from hades_cli import main as hm

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'hades-agent'\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "install.ps1").write_text("# source installer\n")
    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hm.sys, "platform", "win32")

    with (
        patch("hades_cli.config.load_config", return_value={}),
        patch.object(hm, "_run_pre_update_backup"),
        patch.object(hm, "_pause_windows_gateways_for_update", return_value=None),
        patch.object(hm, "_cmd_update_pip") as package_update,
        patch.object(hm, "_update_via_zip") as zip_update,
    ):
        hm._cmd_update_impl(
            SimpleNamespace(force=True, force_venv=True), gateway_mode=False
        )

    package_update.assert_not_called()
    zip_update.assert_called_once()


def test_windows_no_git_packaged_install_resumes_gateway_after_update_error(
    tmp_path, monkeypatch
):
    """Package updater failures must still resume the paused Windows gateway."""
    from hades_cli import main as hm

    monkeypatch.setattr(hm, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hm.sys, "platform", "win32")

    with (
        patch("hades_cli.config.load_config", return_value={}),
        patch.object(hm, "_run_pre_update_backup"),
        patch.object(
            hm, "_pause_windows_gateways_for_update", return_value="resume-token"
        ),
        patch.object(hm, "_resume_windows_gateways_after_update") as resume,
        patch.object(hm, "_cmd_update_pip", side_effect=RuntimeError("update failed")),
        pytest.raises(RuntimeError, match="update failed"),
    ):
        hm._cmd_update_impl(
            SimpleNamespace(force=True, force_venv=True), gateway_mode=False
        )

    resume.assert_called_once_with("resume-token")
