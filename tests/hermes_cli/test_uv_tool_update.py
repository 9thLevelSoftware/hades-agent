"""Tests for managed-tool install detection in the update path (issue #29700).

``uv tool install hades-agent`` lives outside any venv, so the previous
``uv pip install --upgrade`` update path failed with ``No virtual
environment found``. Detection must also retain the installed distribution
identity: canonical Hades tools can be upgraded in place, while a legacy
Hermes-named tool must be migrated with a forced Hades install because
managed-tool upgrade commands only accept an already-installed tool name.

Detection is restricted to properties of the running interpreter
(``sys.prefix`` / ``sys.executable``) so a pip/venv install on a machine
that also has a uv-tool install does not get misclassified.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Managed-uv compatibility for tests that patch shutil.which
# ---------------------------------------------------------------------------
# The production code now uses ``ensure_uv()`` / ``update_managed_uv()``
# instead of ``shutil.which("uv")``.  Many tests in this file patch
# ``shutil.which`` to control whether uv is "available" — these autouse
# fixtures make the managed_uv functions delegate to the patched
# ``shutil.which`` so the existing test setup keeps working without
# per-test changes.
@pytest.fixture(autouse=True)
def _patch_managed_uv(request):
    """Make managed_uv helpers follow shutil.which mocking in tests."""
    import shutil

    # resolve_uv delegates to shutil.which("uv") so that test patches
    # on shutil.which flow through naturally.
    def _fake_resolve_uv():
        return shutil.which("uv")

    def _fake_ensure_uv():
        return shutil.which("uv")

    def _fake_update_managed_uv():
        return None  # never actually self-update in tests

    with patch("hades_cli.managed_uv.resolve_uv", side_effect=_fake_resolve_uv), \
         patch("hades_cli.managed_uv.ensure_uv", side_effect=_fake_ensure_uv), \
         patch("hades_cli.managed_uv.update_managed_uv", side_effect=_fake_update_managed_uv):
        yield


# ---------------------------------------------------------------------------
# is_uv_tool_install
# ---------------------------------------------------------------------------


class TestIsUvToolInstall:
    @pytest.mark.parametrize("distribution", ["hades-agent", "hermes-agent"])
    def test_returns_true_when_sys_prefix_matches_uv_tool_layout(self, distribution):
        from hades_cli import config

        prefix = f"/home/user/.local/share/uv/tools/{distribution}"
        with patch.object(config.sys, "prefix", prefix):
            assert config.is_uv_tool_install() is True
            assert config.uv_tool_install_distribution() == distribution

    @pytest.mark.parametrize("distribution", ["hades-agent", "hermes-agent"])
    def test_returns_true_when_sys_executable_matches_uv_tool_layout(
        self, distribution
    ):
        """Some uv-tool layouts surface the marker on ``sys.executable`` (bin/python)."""
        from hades_cli import config

        with patch.object(config.sys, "prefix", "/some/unrelated/venv"), \
             patch.object(
                 config.sys,
                 "executable",
                 f"/home/user/.local/share/uv/tools/{distribution}/bin/python",
             ):
            assert config.is_uv_tool_install() is True
            assert config.uv_tool_install_distribution() == distribution

    def test_returns_false_when_neither_prefix_nor_executable_matches(self):
        from hades_cli import config

        with patch.object(config.sys, "prefix", "/some/unrelated/venv"), \
             patch.object(config.sys, "executable", "/usr/bin/python3"):
            assert config.is_uv_tool_install() is False
            assert config.uv_tool_install_distribution() is None

    def test_does_not_consult_uv_tool_list(self):
        """Detection must NOT shell out: ``uv tool list`` would false-positive
        when the active install is pip/venv but the machine also has
        ``uv tool install hermes-agent`` somewhere on disk. Copilot review on
        PR #29703 flagged this; the fix is to never call ``uv tool list``
        from the detection path."""
        from hades_cli import config

        with patch.object(config.sys, "prefix", "/some/unrelated/venv"), \
             patch.object(config.sys, "executable", "/usr/bin/python3"), \
             patch("subprocess.run") as mock_run:
            assert config.is_uv_tool_install() is False
            mock_run.assert_not_called()

    def test_case_insensitive_match(self):
        """Match must be case-insensitive — Windows paths preserve case
        (e.g. ``...AppData\\Local\\UV\\Tools\\hades-agent``) and a case-sensitive
        check would miss them. We exercise the lower-cased compare path here
        without monkey-patching ``os.sep``, which would break the whole suite."""
        from hades_cli import config

        with patch.object(
            config.sys, "prefix", "/HOME/USER/.local/share/UV/Tools/hades-agent"
        ):
            assert config.is_uv_tool_install() is True

    @pytest.mark.parametrize("distribution", ["hades-agent", "hermes-agent"])
    def test_handles_windows_path_separators(self, distribution):
        from hades_cli import config

        prefix = rf"C:\Users\example\AppData\Local\UV\Tools\{distribution}"
        with patch.object(config.sys, "prefix", prefix):
            assert config.is_uv_tool_install() is True

    def test_handles_empty_executable(self):
        from hades_cli import config

        with patch.object(config.sys, "prefix", "/some/unrelated/venv"), \
             patch.object(config.sys, "executable", ""):
            assert config.is_uv_tool_install() is False


class TestPipxInstallDistribution:
    @pytest.mark.parametrize(
        ("prefix", "expected"),
        [
            (
                "/home/user/.local/pipx/venvs/hades-agent",
                "hades-agent",
            ),
            (
                r"C:\Users\example\AppData\Local\PIPX\VENVS\HERMES-AGENT\Scripts",
                "hermes-agent",
            ),
            (
                "/data/data/com.termux/files/home/.local/pipx/venvs/hades-agent",
                "hades-agent",
            ),
            (
                "/home/user/.venvs/hades-agent",
                None,
            ),
        ],
    )
    def test_detects_named_pipx_environment_across_supported_paths(
        self, prefix, expected
    ):
        from hades_cli import config

        with patch.object(config.sys, "prefix", prefix):
            assert config.pipx_install_distribution() == expected


# ---------------------------------------------------------------------------
# recommended_update_command_for_method
# ---------------------------------------------------------------------------


class TestRecommendedUpdateCommandForUvTool:
    def test_canonical_uv_tool_install_recommends_upgrade(self):
        from hades_cli import config

        with patch.object(
            config.sys, "prefix", "/home/user/.local/share/uv/tools/hades-agent"
        ):
            cmd = config.recommended_update_command_for_method("pip")
            assert cmd == "uv tool upgrade hades-agent"

    def test_legacy_uv_tool_install_recommends_forced_hades_migration(self):
        from hades_cli import config

        with patch.object(
            config.sys, "prefix", "/home/user/.local/share/uv/tools/hermes-agent"
        ):
            cmd = config.recommended_update_command_for_method("pip")
            assert cmd == "uv tool install --force hades-agent"


class TestRecommendedUpdateCommandForPipx:
    @pytest.mark.parametrize(
        ("prefix", "expected"),
        [
            (
                "/home/user/.local/pipx/venvs/hades-agent",
                "pipx upgrade hades-agent",
            ),
            (
                r"C:\Users\example\AppData\Local\PIPX\VENVS\HERMES-AGENT",
                "pipx install --force hades-agent",
            ),
        ],
    )
    def test_pipx_install_recommends_identity_preserving_command(
        self, prefix, expected
    ):
        from hades_cli import config

        with patch.object(config.sys, "prefix", prefix), \
             patch("shutil.which", return_value=None):
            assert config.recommended_update_command_for_method("pip") == expected


class TestRecommendedUpdateCommandForPip:
    def test_uv_pip_install_recommends_canonical_distribution(self):
        """uv is on PATH but the running Hades is a regular pip install."""
        from hades_cli import config

        with patch("shutil.which", return_value="/usr/local/bin/uv"), \
            patch.object(config, "uv_tool_install_distribution", return_value=None):
            cmd = config.recommended_update_command_for_method("pip")
            assert cmd == "uv pip install --upgrade hades-agent"

    def test_no_uv_falls_back_to_plain_pip(self):
        from hades_cli import config

        with patch("shutil.which", return_value=None), \
            patch.object(config, "uv_tool_install_distribution", return_value=None):
            cmd = config.recommended_update_command_for_method("pip")
            assert cmd == "pip install --upgrade hades-agent"

    def test_recommendation_does_not_spawn_subprocess(self):
        """Computing the recommendation string must be cheap — no ``uv tool list``
        spawn. Copilot review on PR #29703 flagged the prior subprocess hop
        as adding overhead and a multi-second timeout window for what is
        purely a display string."""
        from hades_cli import config

        with patch.object(config.sys, "prefix", "/some/unrelated/venv"), \
             patch.object(config.sys, "executable", "/usr/bin/python3"), \
             patch("shutil.which", return_value="/usr/local/bin/uv"), \
             patch("subprocess.run") as mock_run:
            cmd = config.recommended_update_command_for_method("pip")
            mock_run.assert_not_called()
            assert cmd == "uv pip install --upgrade hades-agent"


# ---------------------------------------------------------------------------
# _cmd_update_pip subprocess command
# ---------------------------------------------------------------------------


class TestCmdUpdatePipUsesUvTool:
    @patch("subprocess.run")
    @pytest.mark.parametrize(
        ("installed_distribution", "expected_command"),
        [
            ("hades-agent", ["tool", "upgrade", "hades-agent"]),
            ("hermes-agent", ["tool", "install", "--force", "hades-agent"]),
        ],
    )
    def test_updates_or_migrates_the_detected_uv_tool(
        self, mock_run, installed_distribution, expected_command
    ):
        """Never ask uv to upgrade a tool name that is not installed."""
        from hades_cli import main as hm

        mock_run.return_value = subprocess.CompletedProcess(["uv"], 0, stdout="", stderr="")
        with patch("shutil.which", return_value="/usr/local/bin/uv"), \
             patch(
                 "hades_cli.config.uv_tool_install_distribution",
                 return_value=installed_distribution,
             ):
            hm._cmd_update_pip(SimpleNamespace())

        assert mock_run.call_args[0][0] == ["/usr/local/bin/uv", *expected_command]
        assert "env" not in mock_run.call_args.kwargs

    @patch("subprocess.run")
    def test_runs_uv_pip_install_when_not_uv_tool(self, mock_run):
        """Existing behavior preserved when uv is present but Hermes isn't a tool install."""
        from hades_cli.main import _cmd_update_pip

        mock_run.return_value = subprocess.CompletedProcess(["uv"], 0, stdout="", stderr="")
        with patch("shutil.which", return_value="/usr/local/bin/uv"), \
             patch(
                 "hades_cli.config.uv_tool_install_distribution",
                 return_value=None,
             ):
            _cmd_update_pip(SimpleNamespace())

        assert mock_run.call_args[0][0] == [
            "/usr/local/bin/uv",
            "pip",
            "install",
            "--upgrade",
            "hades-agent",
        ]

    @patch("subprocess.run")
    def test_falls_back_to_pip_when_no_uv(self, mock_run):
        from hades_cli.main import _cmd_update_pip

        mock_run.return_value = subprocess.CompletedProcess(["pip"], 0, stdout="", stderr="")
        with patch("shutil.which", return_value=None), \
             patch(
                 "hades_cli.config.uv_tool_install_distribution",
                 return_value=None,
             ):
            _cmd_update_pip(SimpleNamespace())

        cmd = mock_run.call_args[0][0]
        assert cmd[1:] == ["-m", "pip", "install", "--upgrade", "hades-agent"]

    @patch("subprocess.run")
    def test_exits_nonzero_on_subprocess_failure(self, mock_run):
        from hades_cli.main import _cmd_update_pip

        mock_run.return_value = subprocess.CompletedProcess(["uv"], 1, stdout="", stderr="")
        with patch("shutil.which", return_value="/usr/local/bin/uv"), \
             patch(
                 "hades_cli.config.uv_tool_install_distribution",
                 return_value="hades-agent",
             ):
            with pytest.raises(SystemExit) as exc_info:
                _cmd_update_pip(SimpleNamespace())
        assert exc_info.value.code == 1

    @patch("subprocess.run")
    def test_uv_tool_install_without_uv_on_path_exits_with_hint(self, mock_run):
        """If the running interpreter looks like a uv-tool install but ``uv`` is
        somehow missing from PATH, surface a clear hint instead of silently
        falling back to ``python -m pip``, which would either fail (no venv)
        or upgrade the wrong copy."""
        from hades_cli.main import _cmd_update_pip

        with patch("shutil.which", return_value=None), \
             patch(
                 "hades_cli.config.uv_tool_install_distribution",
                 return_value="hades-agent",
             ):
            with pytest.raises(SystemExit) as exc_info:
                _cmd_update_pip(SimpleNamespace())
        assert exc_info.value.code == 1
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# pipx-managed installs, --system fallback, and VIRTUAL_ENV overlay
# (issue #29700 / #35031 family — consolidated update-path handling)
# ---------------------------------------------------------------------------


class TestCmdUpdatePipInstallLayouts:
    """The uv pip path must adapt to where the running interpreter lives:

    - inside a venv (launcher shim)  -> export VIRTUAL_ENV, no ``--system``
    - bare pip outside any venv      -> add ``--system``, no overlay
    - pipx-managed                   -> ``pipx upgrade``
    """

    @patch("subprocess.run")
    @pytest.mark.parametrize(
        ("distribution", "expected_command"),
        [
            ("hades-agent", ["upgrade", "hades-agent"]),
            ("hermes-agent", ["install", "--force", "hades-agent"]),
        ],
    )
    def test_pipx_managed_updates_or_migrates_the_named_environment(
        self, mock_run, monkeypatch, distribution, expected_command
    ):
        from hades_cli import main as hm

        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        monkeypatch.setattr(
            hm.sys, "prefix", f"/home/u/.local/pipx/venvs/{distribution}"
        )
        monkeypatch.setattr(hm.sys, "base_prefix", "/usr")

        def _which(name):
            return {"uv": "/usr/bin/uv", "pipx": "/usr/bin/pipx"}.get(name)

        with patch("shutil.which", side_effect=_which), \
             patch(
                 "hades_cli.config.uv_tool_install_distribution",
                 return_value=None,
             ):
            hm._cmd_update_pip(SimpleNamespace())

        assert mock_run.call_args[0][0] == ["/usr/bin/pipx", *expected_command]
        # pipx owns the environment; neither update path uses VIRTUAL_ENV.
        assert "env" not in mock_run.call_args.kwargs

    @patch("subprocess.run")
    @pytest.mark.parametrize(
        ("distribution", "recovery_command"),
        [
            ("hades-agent", "pipx upgrade hades-agent"),
            ("hermes-agent", "pipx install --force hades-agent"),
        ],
    )
    def test_pipx_layout_without_pipx_binary_fails_without_mutating_environment(
        self,
        mock_run,
        monkeypatch,
        capsys,
        distribution,
        recovery_command,
    ):
        from hades_cli import main as hm

        prefix = f"/home/u/.local/pipx/venvs/{distribution}"
        monkeypatch.setattr(hm.sys, "prefix", prefix)
        monkeypatch.setattr(hm.sys, "base_prefix", "/usr")

        with patch("shutil.which", return_value=None), \
             patch("hades_cli.managed_uv.update_managed_uv") as mock_update_uv, \
             patch("hades_cli.managed_uv.ensure_uv") as mock_ensure_uv:
            with pytest.raises(SystemExit) as exc_info:
                hm._cmd_update_pip(SimpleNamespace())

        assert exc_info.value.code == 1
        output = capsys.readouterr().out
        assert "pipx" in output
        assert recovery_command in output
        mock_update_uv.assert_not_called()
        mock_ensure_uv.assert_not_called()
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_bare_pip_outside_venv_adds_system(self, mock_run, monkeypatch):
        from hades_cli import main as hm

        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        # No venv: prefix == base_prefix.
        monkeypatch.setattr(hm.sys, "prefix", "/usr")
        monkeypatch.setattr(hm.sys, "base_prefix", "/usr")

        with patch("shutil.which", return_value="/usr/bin/uv"), \
             patch(
                 "hades_cli.config.uv_tool_install_distribution",
                 return_value=None,
             ):
            hm._cmd_update_pip(SimpleNamespace())

        assert mock_run.call_args[0][0] == [
            "/usr/bin/uv", "pip", "install", "--system", "--upgrade", "hades-agent",
        ]
        assert "env" not in mock_run.call_args.kwargs

    @patch("subprocess.run")
    def test_venv_exports_virtualenv_and_omits_system(self, mock_run, monkeypatch):
        from hades_cli import main as hm

        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setattr(hm.sys, "prefix", "/home/u/.hermes/hermes-agent/venv")
        monkeypatch.setattr(hm.sys, "base_prefix", "/usr")

        with patch("shutil.which", return_value="/usr/bin/uv"), \
             patch(
                 "hades_cli.config.uv_tool_install_distribution",
                 return_value=None,
             ):
            hm._cmd_update_pip(SimpleNamespace())

        cmd = mock_run.call_args[0][0]
        assert "--system" not in cmd
        assert cmd == ["/usr/bin/uv", "pip", "install", "--upgrade", "hades-agent"]
        assert mock_run.call_args.kwargs["env"]["VIRTUAL_ENV"] == "/home/u/.hermes/hermes-agent/venv"
