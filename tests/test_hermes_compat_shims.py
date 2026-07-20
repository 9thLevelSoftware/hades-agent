"""Tests for the hermes↔hades backward-compatibility layer.

The project was renamed from hermes-agent to hades-agent; these tests pin the
compatibility contract that hermes-ecosystem imports keep working and resolve
to the *same* module objects as their hades twins (shared state, monkeypatch
identity), not to parallel copies.
"""

import importlib
import os
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


# ── Env var aliasing ─────────────────────────────────────────────────────────


def test_env_get_reads_both_spellings(monkeypatch):
    from hades_constants import env_get

    monkeypatch.delenv("HADES_COMPAT_TEST", raising=False)
    monkeypatch.delenv("HERMES_COMPAT_TEST", raising=False)
    assert env_get("HADES_COMPAT_TEST") is None
    assert env_get("HADES_COMPAT_TEST", "dflt") == "dflt"

    monkeypatch.setenv("HERMES_COMPAT_TEST", "legacy")
    assert env_get("HADES_COMPAT_TEST") == "legacy"
    assert env_get("HERMES_COMPAT_TEST") == "legacy"

    # HADES spelling wins when both are set.
    monkeypatch.setenv("HADES_COMPAT_TEST", "fork")
    assert env_get("HERMES_COMPAT_TEST") == "fork"


def test_env_get_unprefixed_passthrough(monkeypatch):
    from hades_constants import env_get

    monkeypatch.setenv("COMPAT_TEST_PLAIN", "x")
    assert env_get("COMPAT_TEST_PLAIN") == "x"


def test_env_set_and_pop_write_both_spellings(monkeypatch):
    from hades_constants import env_pop, env_set

    monkeypatch.delenv("HADES_COMPAT_TEST", raising=False)
    monkeypatch.delenv("HERMES_COMPAT_TEST", raising=False)
    env_set("HADES_COMPAT_TEST", "v")
    assert os.environ["HADES_COMPAT_TEST"] == "v"
    assert os.environ["HERMES_COMPAT_TEST"] == "v"

    assert env_pop("HERMES_COMPAT_TEST") == "v"
    assert "HADES_COMPAT_TEST" not in os.environ
    assert "HERMES_COMPAT_TEST" not in os.environ


def test_env_set_into_subprocess_dict():
    from hades_constants import env_set

    child = {}
    env_set("HERMES_KANBAN_TASK", "t-1", env=child)
    assert child == {"HADES_KANBAN_TASK": "t-1", "HERMES_KANBAN_TASK": "t-1"}


def test_env_var_enabled_dual_reads(monkeypatch):
    from utils import env_var_enabled

    monkeypatch.delenv("HADES_COMPAT_FLAG", raising=False)
    monkeypatch.delenv("HERMES_COMPAT_FLAG", raising=False)
    assert env_var_enabled("HADES_COMPAT_FLAG") is False
    monkeypatch.setenv("HERMES_COMPAT_FLAG", "1")
    assert env_var_enabled("HADES_COMPAT_FLAG") is True
    assert env_var_enabled("HERMES_COMPAT_FLAG") is True


def test_env_int_dual_reads(monkeypatch):
    from utils import env_int

    monkeypatch.delenv("HADES_COMPAT_INT", raising=False)
    monkeypatch.setenv("HERMES_COMPAT_INT", "7")
    assert env_int("HADES_COMPAT_INT", 0) == 7


# ── Toolset key aliasing ─────────────────────────────────────────────────────


def test_toolset_prefix_aliases_resolve_identically():
    from toolsets import resolve_toolset

    assert resolve_toolset("hades-cli") == resolve_toolset("hermes-cli")
    assert resolve_toolset("hermes-acp") == resolve_toolset("hades-acp")
    assert resolve_toolset("hades-telegram") == resolve_toolset("hermes-telegram")


def test_get_toolset_swaps_prefix_in_static_view():
    from toolsets import get_toolset

    static_real = get_toolset("hermes-cli", include_registry=False)
    static_alias = get_toolset("hades-cli", include_registry=False)
    assert static_real is not None
    assert static_alias == static_real

    assert get_toolset("hermes-acp", include_registry=False) == get_toolset(
        "hades-acp", include_registry=False
    )


def test_toolset_names_list_canonical_keys_only():
    from toolsets import get_toolset_names

    names = get_toolset_names()
    # Canonical keys keep their historical spellings; no duplicate alias rows.
    assert "hermes-cli" in names
    assert "hades-cli" not in names
    assert "hades-acp" in names
    assert "hermes-acp" not in names


def test_resolve_toolset_visited_normalizes_spellings():
    from toolsets import resolve_toolset

    visited = set()
    first = resolve_toolset("hades-cli", visited)
    assert first
    # The canonical spelling was recorded, so the other spelling is treated
    # as already-resolved (diamond), not re-resolved.
    assert "hermes-cli" in visited
    assert resolve_toolset("hermes-cli", visited) == []


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
