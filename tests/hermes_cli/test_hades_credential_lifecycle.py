"""End-to-end contracts for Hades' canonical provider credential lifecycle."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest
import yaml


OLD_KEY = "credential-" + "a" * 24
NEW_KEY = "credential-" + "b" * 24
OTHER_KEY = "credential-" + "c" * 24
SOURCE = "env:ZAI_API_KEY"


def _lifecycle():
    return importlib.import_module("hades_cli.credential_lifecycle")


def _write_auth(home: Path, payload: dict) -> None:
    (home / "auth.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_auth(home: Path) -> dict:
    return json.loads((home / "auth.json").read_text(encoding="utf-8"))


def _write_config(home: Path, payload: dict) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _read_config(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def _pool_entry(entry_id: str, source: str, token: str, auth_type: str = "api_key") -> dict:
    return {
        "id": entry_id,
        "label": entry_id,
        "auth_type": auth_type,
        "priority": 0,
        "source": source,
        "access_token": token,
    }


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
    os.environ.pop("ZAI_API_KEY", None)


def test_remove_reconciles_every_store_and_preserves_non_env_auth(hades_home):
    (hades_home / ".env").write_text(
        f"export ZAI_API_KEY={OLD_KEY}\nUNCHANGED=value\n",
        encoding="utf-8",
    )
    oauth_state = {
        "tokens": {
            "access_token": "oauth-" + "d" * 24,
            "refresh_token": "refresh-" + "e" * 24,
        }
    }
    _write_auth(
        hades_home,
        {
            "version": 1,
            "providers": {"zai": oauth_state},
            "credential_pool": {
                "zai": [
                    _pool_entry("zai-env", SOURCE, OLD_KEY),
                    _pool_entry("zai-oauth", "device_code", OTHER_KEY, "oauth"),
                ],
                # Legacy stores may contain an alias key. The exact env source
                # must still be removed and its model cache invalidated.
                "glm": [
                    _pool_entry("glm-env", SOURCE, OLD_KEY),
                    _pool_entry("glm-manual", "manual", OTHER_KEY),
                ],
                "deepseek": [
                    _pool_entry(
                        "deepseek-env",
                        "env:DEEPSEEK_API_KEY",
                        OTHER_KEY,
                    )
                ],
            },
        },
    )
    _write_config(
        hades_home,
        {
            "model": {
                "provider": "custom",
                "api_key": OLD_KEY,
                "base_url": "https://model.example.test/v1",
            },
            "auxiliary": {
                "vision": {"api": OLD_KEY, "model": "vision-model"},
                "web": {"api_key": OTHER_KEY},
            },
            "custom_providers": [
                {
                    "name": "mirrored",
                    "api_key": OLD_KEY,
                    "base_url": "https://custom.example.test/v1",
                },
                {"name": "independent", "api_key": OTHER_KEY},
            ],
        },
    )
    (hades_home / "provider_models_cache.json").write_text(
        json.dumps(
            {
                "zai": {"models": ["canonical-model"]},
                "deepseek": {"models": ["preserved-model"]},
            }
        ),
        encoding="utf-8",
    )

    result = _lifecycle().remove_provider_env_credential("ZAI_API_KEY")

    assert result["ok"] is True
    assert result["found"] is True
    assert result["removed"] is True
    assert result["pool_pruned"] == ["zai", "glm"]
    assert set(result["providers"]) == {"zai", "glm"}
    assert set(result["config_scrubbed"]) == {
        "model.api_key",
        "auxiliary.vision.api",
        "custom_providers.0.api_key",
    }
    assert OLD_KEY not in repr(result)

    env_text = (hades_home / ".env").read_text(encoding="utf-8")
    assert "ZAI_API_KEY" not in env_text
    assert "UNCHANGED=value" in env_text

    auth_store = _read_auth(hades_home)
    assert auth_store["providers"]["zai"] == oauth_state
    assert [entry["source"] for entry in auth_store["credential_pool"]["zai"]] == [
        "device_code"
    ]
    assert [entry["source"] for entry in auth_store["credential_pool"]["glm"]] == [
        "manual"
    ]
    assert [entry["source"] for entry in auth_store["credential_pool"]["deepseek"]] == [
        "env:DEEPSEEK_API_KEY"
    ]
    assert SOURCE in auth_store["suppressed_sources"]["zai"]

    config = _read_config(hades_home)
    assert "api_key" not in config["model"]
    assert "api" not in config["auxiliary"]["vision"]
    assert config["auxiliary"]["web"]["api_key"] == OTHER_KEY
    assert "api_key" not in config["custom_providers"][0]
    assert config["custom_providers"][1]["api_key"] == OTHER_KEY

    cache = json.loads(
        (hades_home / "provider_models_cache.json").read_text(encoding="utf-8")
    )
    assert "zai" not in cache
    assert cache["deepseek"]["models"] == ["preserved-model"]


def test_remove_finds_and_prunes_a_pool_only_stale_credential(hades_home):
    _write_auth(
        hades_home,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "zai": [_pool_entry("stale", SOURCE, OLD_KEY)],
            },
        },
    )

    result = _lifecycle().remove_provider_env_credential("ZAI_API_KEY")

    assert result["found"] is True
    assert result["removed"] is False
    assert result["pool_pruned"] == ["zai"]
    assert "zai" not in _read_auth(hades_home).get("credential_pool", {})


def test_save_rotates_only_value_matched_config_mirrors_and_unsuppresses(hades_home):
    (hades_home / ".env").write_text(
        f"export ZAI_API_KEY={OLD_KEY}\n",
        encoding="utf-8",
    )
    _write_auth(
        hades_home,
        {
            "version": 1,
            "providers": {},
            "suppressed_sources": {"zai": [SOURCE]},
        },
    )
    _write_config(
        hades_home,
        {
            "model": {"provider": "custom", "api_key": OLD_KEY},
            "auxiliary": {
                "vision": {"api_key": OLD_KEY},
                "web": {"api_key": OTHER_KEY},
            },
            "custom_providers": {
                "mirrored": {"api": OLD_KEY},
                "independent": {"api_key": OTHER_KEY},
            },
        },
    )

    result = _lifecycle().save_provider_env_credential("ZAI_API_KEY", NEW_KEY)

    assert result["ok"] is True
    assert set(result["config_updates"]) == {
        "model.api_key",
        "auxiliary.vision.api_key",
        "custom_providers.mirrored.api",
    }
    assert OLD_KEY not in repr(result)
    assert NEW_KEY not in repr(result)

    env_text = (hades_home / ".env").read_text(encoding="utf-8")
    assert env_text.count("ZAI_API_KEY=") == 1
    assert OLD_KEY not in env_text
    assert NEW_KEY in env_text

    config = _read_config(hades_home)
    assert config["model"]["api_key"] == NEW_KEY
    assert config["auxiliary"]["vision"]["api_key"] == NEW_KEY
    assert config["auxiliary"]["web"]["api_key"] == OTHER_KEY
    assert config["custom_providers"]["mirrored"]["api"] == NEW_KEY
    assert config["custom_providers"]["independent"]["api_key"] == OTHER_KEY

    suppressed = _read_auth(hades_home).get("suppressed_sources", {})
    assert SOURCE not in suppressed.get("zai", [])


def test_save_mirrors_the_canonical_value_actually_persisted(hades_home):
    (hades_home / ".env").write_text(
        f"ZAI_API_KEY={OLD_KEY}\n",
        encoding="utf-8",
    )
    _write_config(
        hades_home,
        {
            "model": {"api_key": OLD_KEY},
            "auxiliary": {"vision": {"api_key": OLD_KEY}},
        },
    )
    submitted_value = NEW_KEY + "\r\n\u00e9"

    result = _lifecycle().save_provider_env_credential(
        "ZAI_API_KEY",
        submitted_value,
    )

    from hades_cli.config import load_env

    persisted_value = load_env()["ZAI_API_KEY"]
    assert persisted_value == NEW_KEY
    assert _read_config(hades_home)["model"]["api_key"] == persisted_value
    assert (
        _read_config(hades_home)["auxiliary"]["vision"]["api_key"]
        == persisted_value
    )
    assert NEW_KEY not in repr(result)
    assert submitted_value not in repr(result)


def test_config_entry_points_route_provider_keys_through_lifecycle(hades_home, capsys):
    (hades_home / ".env").write_text(
        f"ZAI_API_KEY={OLD_KEY}\n",
        encoding="utf-8",
    )
    _write_config(hades_home, {"model": {"api_key": OLD_KEY}})

    from hades_cli.config import save_env_value_secure, set_config_value

    metadata = save_env_value_secure("ZAI_API_KEY", NEW_KEY)
    assert metadata == {
        "success": True,
        "stored_as": "ZAI_API_KEY",
        "validated": False,
    }
    assert _read_config(hades_home)["model"]["api_key"] == NEW_KEY

    set_config_value("ZAI_API_KEY", OLD_KEY)
    assert _read_config(hades_home)["model"]["api_key"] == OLD_KEY
    output = capsys.readouterr()
    assert OLD_KEY not in output.out
    assert OLD_KEY not in output.err


def test_profile_scoping_never_mutates_root_or_a_sibling_profile(tmp_path, monkeypatch):
    root = tmp_path / ".hades"
    alpha = root / "profiles" / "alpha"
    beta = root / "profiles" / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)

    for home, marker in ((root, "root"), (alpha, "alpha"), (beta, "beta")):
        key = OLD_KEY + marker
        (home / ".env").write_text(f"ZAI_API_KEY={key}\n", encoding="utf-8")
        _write_auth(
            home,
            {
                "version": 1,
                "providers": {},
                "credential_pool": {
                    "zai": [_pool_entry(f"{marker}-env", SOURCE, key)]
                },
            },
        )
        _write_config(home, {"model": {"api_key": key}, "marker": marker})

    root_before = {
        name: (root / name).read_bytes()
        for name in (".env", "auth.json", "config.yaml")
    }
    beta_before = {
        name: (beta / name).read_bytes()
        for name in (".env", "auth.json", "config.yaml")
    }

    monkeypatch.setenv("HADES_HOME", str(alpha))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    from hades_cli.config import invalidate_env_cache

    invalidate_env_cache()
    result = _lifecycle().remove_provider_env_credential("ZAI_API_KEY")

    assert result["found"] is True
    assert "ZAI_API_KEY" not in (alpha / ".env").read_text(encoding="utf-8")
    assert "api_key" not in _read_config(alpha)["model"]
    assert "zai" not in _read_auth(alpha).get("credential_pool", {})
    for name, content in root_before.items():
        assert (root / name).read_bytes() == content
    for name, content in beta_before.items():
        assert (beta / name).read_bytes() == content


def test_env_write_failure_leaves_original_stores_intact(hades_home, monkeypatch):
    env_path = hades_home / ".env"
    env_path.write_text(f"ZAI_API_KEY={OLD_KEY}\n", encoding="utf-8")
    _write_auth(
        hades_home,
        {
            "version": 1,
            "providers": {},
            "suppressed_sources": {"zai": [SOURCE]},
        },
    )
    _write_config(hades_home, {"model": {"api_key": OLD_KEY}})
    before = {
        name: (hades_home / name).read_bytes()
        for name in (".env", "auth.json", "config.yaml")
    }

    import hades_cli.config as config

    def fail_replace(_source, _target):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(config, "atomic_replace", fail_replace)

    with pytest.raises(OSError, match="simulated atomic replace failure"):
        _lifecycle().save_provider_env_credential("ZAI_API_KEY", NEW_KEY)

    for name, content in before.items():
        assert (hades_home / name).read_bytes() == content
    assert list(hades_home.glob(".env_*.tmp")) == []


def test_save_config_failure_restores_exact_env_and_suppression(
    hades_home,
    monkeypatch,
):
    env_path = hades_home / ".env"
    env_path.write_bytes(
        (
            "# preserve this comment\r\n"
            f"export ZAI_API_KEY={OLD_KEY}\r\n"
            f"ZAI_API_KEY={OLD_KEY}\r\n"
            "UNCHANGED=value\r\n"
        ).encode()
    )
    _write_auth(
        hades_home,
        {
            "version": 1,
            "providers": {},
            "suppressed_sources": {"zai": [SOURCE]},
        },
    )
    _write_config(hades_home, {"model": {"api_key": OLD_KEY}})
    before = {
        name: (hades_home / name).read_bytes()
        for name in (".env", "auth.json", "config.yaml")
    }
    env_mode = stat.S_IMODE(env_path.stat().st_mode)
    monkeypatch.setenv("ZAI_API_KEY", OLD_KEY)

    import hades_cli.config as config

    def fail_config_write(*_args, **_kwargs):
        raise OSError("simulated config replace failure")

    monkeypatch.setattr(config, "atomic_config_write", fail_config_write)

    with pytest.raises(OSError, match="simulated config replace failure"):
        _lifecycle().save_provider_env_credential("ZAI_API_KEY", NEW_KEY)

    for name, content in before.items():
        assert (hades_home / name).read_bytes() == content
    assert stat.S_IMODE(env_path.stat().st_mode) == env_mode
    assert os.environ["ZAI_API_KEY"] == OLD_KEY
    assert list(hades_home.glob(".credential_rollback_*.tmp")) == []


def test_save_partial_unsuppress_failure_restores_missing_env_and_auth(
    hades_home,
    monkeypatch,
):
    env_path = hades_home / ".env"
    _write_auth(
        hades_home,
        {
            "version": 1,
            "providers": {},
            "suppressed_sources": {
                "glm": [SOURCE],
                "zai": [SOURCE],
            },
        },
    )
    auth_before = (hades_home / "auth.json").read_bytes()
    monkeypatch.delenv("ZAI_API_KEY", raising=False)

    import hades_cli.auth as auth

    real_unsuppress = auth.unsuppress_credential_source
    calls: list[str] = []

    def fail_second_unsuppress(provider_id, source):
        calls.append(provider_id)
        if len(calls) == 2:
            raise OSError("simulated suppression write failure")
        return real_unsuppress(provider_id, source)

    monkeypatch.setattr(
        auth,
        "unsuppress_credential_source",
        fail_second_unsuppress,
    )

    with pytest.raises(OSError, match="simulated suppression write failure"):
        _lifecycle().save_provider_env_credential("ZAI_API_KEY", NEW_KEY)

    assert len(calls) == 2
    assert not env_path.exists()
    assert (hades_home / "auth.json").read_bytes() == auth_before
    assert "ZAI_API_KEY" not in os.environ


@pytest.mark.parametrize("operation", ["save", "remove"])
def test_managed_env_key_lifecycle_fails_without_mutating_profile_stores(
    hades_home,
    tmp_path,
    monkeypatch,
    operation,
):
    env_path = hades_home / ".env"
    env_path.write_text(f"ZAI_API_KEY={OLD_KEY}\n", encoding="utf-8")
    _write_auth(
        hades_home,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "zai": [_pool_entry("zai-env", SOURCE, OLD_KEY)]
            },
            "suppressed_sources": {"zai": [SOURCE]},
        },
    )
    _write_config(hades_home, {"model": {"api_key": OLD_KEY}})
    before = {
        name: (hades_home / name).read_bytes()
        for name in (".env", "auth.json", "config.yaml")
    }

    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    (managed_dir / ".env").write_text(
        "ZAI_API_KEY=organization-owned-value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    from hades_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    try:
        with pytest.raises(RuntimeError, match="managed"):
            if operation == "save":
                _lifecycle().save_provider_env_credential(
                    "ZAI_API_KEY",
                    NEW_KEY,
                )
            else:
                _lifecycle().remove_provider_env_credential("ZAI_API_KEY")

        for name, content in before.items():
            assert (hades_home / name).read_bytes() == content
    finally:
        monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
        managed_scope.invalidate_managed_cache()


@pytest.mark.parametrize("failure_boundary", ["auth", "config"])
def test_remove_failure_restores_exact_prior_durable_state(
    hades_home,
    monkeypatch,
    failure_boundary,
):
    env_path = hades_home / ".env"
    env_path.write_bytes(
        (
            "# retain delete formatting\r\n"
            f"export ZAI_API_KEY={OLD_KEY}\r\n"
            f"ZAI_API_KEY={OLD_KEY}\r\n"
            "UNCHANGED=value\r\n"
        ).encode()
    )
    _write_auth(
        hades_home,
        {
            "version": 1,
            "providers": {
                "zai": {
                    "tokens": {
                        "access_token": "oauth-" + "d" * 24,
                        "refresh_token": "refresh-" + "e" * 24,
                    }
                }
            },
            "credential_pool": {
                "zai": [
                    _pool_entry("zai-env", SOURCE, OLD_KEY),
                    _pool_entry("zai-manual", "manual", OTHER_KEY),
                ]
            },
        },
    )
    _write_config(hades_home, {"model": {"api_key": OLD_KEY}})
    before = {
        name: (hades_home / name).read_bytes()
        for name in (".env", "auth.json", "config.yaml")
    }
    env_mode = stat.S_IMODE(env_path.stat().st_mode)
    monkeypatch.setenv("ZAI_API_KEY", OLD_KEY)

    if failure_boundary == "auth":
        import hades_cli.auth as auth

        def fail_auth_write(*_args, **_kwargs):
            raise OSError("simulated auth replace failure")

        monkeypatch.setattr(auth, "_save_auth_store", fail_auth_write)
        error = "simulated auth replace failure"
    else:
        import hades_cli.config as config

        def fail_config_write(*_args, **_kwargs):
            raise OSError("simulated config replace failure")

        monkeypatch.setattr(config, "atomic_config_write", fail_config_write)
        error = "simulated config replace failure"

    with pytest.raises(OSError, match=error):
        _lifecycle().remove_provider_env_credential("ZAI_API_KEY")

    for name, content in before.items():
        assert (hades_home / name).read_bytes() == content
    assert stat.S_IMODE(env_path.stat().st_mode) == env_mode
    assert os.environ["ZAI_API_KEY"] == OLD_KEY


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_lifecycle_preserves_env_and_config_modes_and_secures_auth(hades_home):
    env_path = hades_home / ".env"
    config_path = hades_home / "config.yaml"
    auth_path = hades_home / "auth.json"
    env_path.write_text(f"ZAI_API_KEY={OLD_KEY}\n", encoding="utf-8")
    _write_config(hades_home, {"model": {"api_key": OLD_KEY}})
    _write_auth(
        hades_home,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "zai": [_pool_entry("zai-env", SOURCE, OLD_KEY)]
            },
        },
    )
    os.chmod(env_path, 0o640)
    os.chmod(config_path, 0o640)
    os.chmod(auth_path, 0o644)

    _lifecycle().remove_provider_env_credential("ZAI_API_KEY")

    assert stat.S_IMODE(env_path.stat().st_mode) == 0o640
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600


def test_legacy_import_is_the_canonical_hades_module():
    canonical = _lifecycle()
    legacy = importlib.import_module("hermes_cli.credential_lifecycle")

    assert legacy is canonical
    assert sys.modules["hermes_cli.credential_lifecycle"] is canonical
    assert (
        legacy.save_provider_env_credential
        is canonical.save_provider_env_credential
    )
    assert (
        legacy.remove_provider_env_credential
        is canonical.remove_provider_env_credential
    )


def test_legacy_from_import_resolves_canonical_module_in_a_fresh_process():
    worktree_root = Path(__file__).resolve().parents[2]
    program = f"""
import importlib
from pathlib import Path
import sys

root = Path({str(worktree_root)!r}).resolve()
sys.path.insert(0, str(root))
from hermes_cli.credential_lifecycle import (
    remove_provider_env_credential,
    save_provider_env_credential,
)
canonical = importlib.import_module("hades_cli.credential_lifecycle")
legacy = importlib.import_module("hermes_cli.credential_lifecycle")
assert legacy is canonical
assert save_provider_env_credential is canonical.save_provider_env_credential
assert remove_provider_env_credential is canonical.remove_provider_env_credential
assert Path(canonical.__file__).resolve().is_relative_to(root)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
