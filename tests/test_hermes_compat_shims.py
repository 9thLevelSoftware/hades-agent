"""Tests for the hermes↔hades backward-compatibility layer.

The project was renamed from hermes-agent to hades-agent; these tests pin the
compatibility contract that hermes-ecosystem imports keep working and resolve
to the *same* module objects as their hades twins (shared state, monkeypatch
identity), not to parallel copies.
"""

import importlib
import subprocess
import sys

import pytest


TOP_LEVEL_SHIMS = [
    ("hermes_bootstrap", "hades_bootstrap"),
    ("hermes_constants", "hades_constants"),
    ("hermes_logging", "hades_logging"),
    ("hermes_state", "hades_state"),
    ("hermes_time", "hades_time"),
]


@pytest.mark.parametrize("alias,real", TOP_LEVEL_SHIMS)
def test_top_level_shim_is_same_module(alias, real):
    alias_mod = importlib.import_module(alias)
    real_mod = importlib.import_module(real)
    assert alias_mod is real_mod
    assert sys.modules[alias] is real_mod


def test_constants_contextvar_is_singleton():
    import hades_constants
    import hermes_constants

    # Underscore names are reachable through the alias (star-import shims
    # dropped them) and there is exactly one ContextVar instance.
    assert hermes_constants._HADES_HOME_OVERRIDE is hades_constants._HADES_HOME_OVERRIDE


def test_constants_function_aliases_preserved():
    import hermes_constants

    assert hermes_constants.get_hermes_home is hermes_constants.get_hades_home


def test_hermes_cli_package_is_hades_cli():
    import hades_cli
    import hermes_cli

    assert hermes_cli is hades_cli
    assert sys.modules["hermes_cli"] is hades_cli


def test_hermes_cli_submodule_identity():
    import hermes_cli.config  # noqa: F401  (must not ModuleNotFoundError)
    import hades_cli.config

    assert sys.modules["hermes_cli.config"] is sys.modules["hades_cli.config"]


def test_hermes_cli_from_import():
    from hermes_cli.main import main as hermes_main
    from hades_cli.main import main as hades_main

    assert hermes_main is hades_main


def test_alias_import_does_not_corrupt_real_module_attrs():
    # The import machinery stamps the alias identity onto the module returned
    # by create_module(); the alias loader must restore the canonical
    # attributes or logging namespaces and relative imports break.
    import hermes_cli.config  # noqa: F401
    import hades_cli.config

    assert hades_cli.config.__name__ == "hades_cli.config"
    assert hades_cli.config.__spec__.name == "hades_cli.config"
    assert hades_cli.config.__package__ == "hades_cli"


def test_monkeypatch_through_alias_visible_via_real(monkeypatch):
    import hermes_cli.config as alias_config
    import hades_cli.config as real_config

    sentinel = object()
    monkeypatch.setattr(alias_config, "_compat_test_sentinel", sentinel, raising=False)
    assert real_config._compat_test_sentinel is sentinel


def test_alias_finder_idempotent_after_reimport():
    import hermes_cli  # noqa: F401  (ensure installed once already)

    before = sum(
        1 for f in sys.meta_path if getattr(f, "_hermes_cli_alias_finder", False)
    )
    assert before == 1

    # Simulate a test-suite sys.modules purge and re-import of the alias.
    saved = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "hermes_cli" or name.startswith("hermes_cli.")
    }
    try:
        importlib.import_module("hermes_cli")
        after = sum(
            1 for f in sys.meta_path if getattr(f, "_hermes_cli_alias_finder", False)
        )
        assert after == 1
    finally:
        sys.modules.update(saved)


def test_python_dash_m_hermes_cli_module():
    # runpy needs get_code() from the alias loader; smoke-test the real
    # interpreter path end to end.
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.uninstall", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "ModuleNotFoundError" not in proc.stderr
    assert proc.returncode == 0, proc.stderr


def test_packaging_declares_hermes_shims():
    """Wheels must ship the hermes compat names, not just git checkouts."""
    import tomllib
    from pathlib import Path

    data = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    py_modules = data["tool"]["setuptools"]["py-modules"]
    for alias, _ in TOP_LEVEL_SHIMS:
        assert alias in py_modules, f"{alias} missing from py-modules"
    include = data["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "hermes_cli" in include
    assert "hades_cli" in data["tool"]["setuptools"]["package-data"], (
        "package-data must be keyed on the real package (hades_cli); a "
        "hermes_cli key silently drops web_dist/tui_dist from wheels"
    )
