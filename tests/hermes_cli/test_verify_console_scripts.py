"""Tests for _verify_console_scripts_installed (issue #52931)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest


DECLARED_CONSOLE_SCRIPTS = (
    "legacy-cli",
    "legacy-agent",
    "legacy-acp",
    "canonical-cli",
    "canonical-agent",
    "canonical-acp",
)


@pytest.fixture
def temp_pyproject(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    declarations = "\n".join(
        f'{name} = "hades_cli.main:main"' for name in DECLARED_CONSOLE_SCRIPTS
    )
    pyproject.write_text(
        textwrap.dedent(
            f"""\
        [project]
        name = "fake"
        version = "0.0.0"

        [project.scripts]
        {declarations}
    """
        ),
        encoding="utf-8",
    )
    import hades_cli.main as main_mod

    monkeypatch.setattr(main_mod, "PROJECT_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def fake_scripts_dir(tmp_path):
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    return scripts


class TestVerifyConsoleScriptsInstalled:
    def test_no_action_when_all_shims_present(self, temp_pyproject, fake_scripts_dir):
        for name in DECLARED_CONSOLE_SCRIPTS:
            (fake_scripts_dir / f"{name}.exe").write_bytes(b"fake")

        with patch("hades_cli.main._is_windows", return_value=True), \
             patch("hades_cli.main._venv_scripts_dir", return_value=fake_scripts_dir), \
             patch("hades_cli.main._run_quarantined_install") as mock_install:
            from hades_cli.main import _verify_console_scripts_installed

            _verify_console_scripts_installed(["uv", "pip"], env={})

        mock_install.assert_not_called()

    def test_triggers_reinstall_when_a_declared_script_is_missing(
        self, temp_pyproject, fake_scripts_dir
    ):
        for name in DECLARED_CONSOLE_SCRIPTS[1:]:
            (fake_scripts_dir / f"{name}.exe").write_bytes(b"fake")

        with patch("hades_cli.main._is_windows", return_value=True), \
             patch("hades_cli.main._venv_scripts_dir", return_value=fake_scripts_dir), \
             patch("hades_cli.main._run_quarantined_install") as mock_install:
            from hades_cli.main import _verify_console_scripts_installed

            _verify_console_scripts_installed(["uv", "pip"], env={})

        mock_install.assert_called_once()
        args = mock_install.call_args[0][0]
        assert "--reinstall" in args
        assert "-e" in args and "." in args
        assert mock_install.call_args[1]["scripts_dir"] == fake_scripts_dir

    def test_skips_off_windows(self, temp_pyproject, fake_scripts_dir):
        with patch("hades_cli.main._is_windows", return_value=False), \
             patch("hades_cli.main._run_quarantined_install") as mock_install:
            from hades_cli.main import _verify_console_scripts_installed

            _verify_console_scripts_installed(["uv", "pip"], env={})

        mock_install.assert_not_called()

    def test_load_console_script_names_reads_pyproject(self, temp_pyproject):
        from hades_cli.main import _load_console_script_names

        names = _load_console_script_names()
        assert names == list(DECLARED_CONSOLE_SCRIPTS)

    def test_primary_install_success_still_verifies_scripts(self):
        import hades_cli.main as main_mod

        with patch("hades_cli.main._is_windows", return_value=False), \
             patch("hades_cli.main._run_quarantined_install") as mock_install, \
             patch("hades_cli.main._verify_console_scripts_installed") as mock_verify:
            main_mod._install_python_dependencies_with_optional_fallback(
                ["uv", "pip"], env={"VIRTUAL_ENV": "x"}
            )

        mock_install.assert_called_once_with(
            ["uv", "pip", "install", "-e", ".[all]"],
            env={"VIRTUAL_ENV": "x"},
            scripts_dir=None,
        )
        mock_verify.assert_called_once_with(["uv", "pip"], env={"VIRTUAL_ENV": "x"})

    def test_quarantine_shims_include_declared_console_scripts(
        self, temp_pyproject, fake_scripts_dir
    ):
        import hades_cli.main as main_mod

        with patch("hades_cli.main._is_windows", return_value=True):
            names = {path.name for path in main_mod._hermes_exe_shims(fake_scripts_dir)}

        assert {f"{name}.exe" for name in DECLARED_CONSOLE_SCRIPTS} <= names
        assert "hades-gateway.exe" in names
