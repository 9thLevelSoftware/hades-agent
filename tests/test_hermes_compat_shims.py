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


# ── Plugin entry-point dual-group scan ───────────────────────────────────────


def _fake_ep(name, group, value="fake_mod:register"):
    from unittest.mock import MagicMock

    ep = MagicMock()
    ep.name = name
    ep.value = value
    ep.group = group
    return ep


def test_entry_point_scan_covers_both_groups(monkeypatch):
    from unittest.mock import MagicMock, patch

    from hades_cli.plugins import ENTRY_POINTS_GROUPS, PluginManager

    assert ENTRY_POINTS_GROUPS == ("hades_agent.plugins", "hermes_agent.plugins")

    hades_ep = _fake_ep("hades_only", "hades_agent.plugins")
    hermes_ep = _fake_ep("hermes_only", "hermes_agent.plugins")

    def fake_entry_points():
        result = MagicMock()
        result.select = lambda group: {
            "hades_agent.plugins": [hades_ep],
            "hermes_agent.plugins": [hermes_ep],
        }.get(group, [])
        return result

    with patch("importlib.metadata.entry_points", fake_entry_points):
        manifests = PluginManager()._scan_entry_points()

    names = {m.name for m in manifests}
    assert names == {"hades_only", "hermes_only"}


def test_entry_point_scan_dedupes_dual_registered(monkeypatch):
    from unittest.mock import MagicMock, patch

    from hades_cli.plugins import PluginManager

    hades_ep = _fake_ep("dual", "hades_agent.plugins", value="hades_variant:register")
    hermes_ep = _fake_ep("dual", "hermes_agent.plugins", value="hermes_variant:register")

    def fake_entry_points():
        result = MagicMock()
        result.select = lambda group: {
            "hades_agent.plugins": [hades_ep],
            "hermes_agent.plugins": [hermes_ep],
        }.get(group, [])
        return result

    with patch("importlib.metadata.entry_points", fake_entry_points):
        manifests = PluginManager()._scan_entry_points()

    dual = [m for m in manifests if m.name == "dual"]
    assert len(dual) == 1
    # hades group scanned first → wins.
    assert dual[0].path == "hades_variant:register"


# ── Skills vendor-metadata namespace ─────────────────────────────────────────


def test_skill_vendor_metadata_hermes_namespace():
    from utils import skill_vendor_metadata

    fm = {"metadata": {"hermes": {"tags": ["a"], "blueprint": {"schedule": "0 9 * * *"}}}}
    assert skill_vendor_metadata(fm)["tags"] == ["a"]


def test_skill_vendor_metadata_hades_fallback():
    from utils import skill_vendor_metadata

    fm = {"metadata": {"hades": {"tags": ["b"]}}}
    assert skill_vendor_metadata(fm)["tags"] == ["b"]


def test_skill_vendor_metadata_hermes_wins_over_hades():
    from utils import skill_vendor_metadata

    fm = {"metadata": {"hermes": {"tags": ["h"]}, "hades": {"tags": ["x"]}}}
    assert skill_vendor_metadata(fm)["tags"] == ["h"]


def test_skill_vendor_metadata_malformed():
    from utils import skill_vendor_metadata

    assert skill_vendor_metadata({}) == {}
    assert skill_vendor_metadata({"metadata": "nope"}) == {}
    assert skill_vendor_metadata({"metadata": {"hermes": "nope"}}) == {}


def test_blueprint_parses_from_hades_namespace():
    from tools.blueprints import parse_blueprint

    skill_md = (
        "---\n"
        "name: test-bp\n"
        "description: x\n"
        "metadata:\n"
        "  hades:\n"
        "    blueprint:\n"
        '      schedule: "0 9 * * *"\n'
        "---\n"
        "body\n"
    )
    spec = parse_blueprint(skill_md)
    assert spec is not None
    assert spec.schedule == "0 9 * * *"


def _guard_findings(tmp_path, content):
    from tools.skills_guard import scan_file

    f = tmp_path / "SKILL.md"
    f.write_text(content, encoding="utf-8")
    findings = scan_file(f)
    f.unlink()
    return {(fi.pattern_id, fi.severity) for fi in findings}


def test_skills_guard_flags_hades_paths_like_hermes(tmp_path):
    hermes_env = _guard_findings(tmp_path, "cat ~/.hermes/.env\n")
    hades_env = _guard_findings(tmp_path, "cat ~/.hades/.env\n")
    assert ("hermes_env_access", "critical") in hermes_env
    assert hermes_env == hades_env

    hermes_cfg = _guard_findings(tmp_path, "edit .hermes/config.yaml\n")
    hades_cfg = _guard_findings(tmp_path, "edit .hades/config.yaml\n")
    assert ("hermes_config_mod", "critical") in hades_cfg
    assert hermes_cfg == hades_cfg


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
