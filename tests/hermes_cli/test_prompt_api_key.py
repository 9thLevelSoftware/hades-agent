"""Tests for ``_prompt_api_key`` — the shared Keep/Replace/Clear menu used by
``hermes setup`` / ``hermes model`` when an API key already exists in ``.env``.

Regression coverage for #16394: the wizard used to silently skip the key prompt
when any value was present (even malformed junk), leaving users stuck.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hades"
    home.mkdir()
    # Keep Path.home() distinct from the isolated HADES_HOME. The auth-store
    # test seat belt intentionally rejects a path that looks like the real
    # user's ``~/.hades/auth.json``.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-user")
    monkeypatch.setenv("HADES_HOME", str(home))
    (home / ".env").write_text("")
    from hades_cli.config import invalidate_env_cache

    invalidate_env_cache()
    yield home
    invalidate_env_cache()


def _pconfig(name="deepseek"):
    from hades_cli.auth import PROVIDER_REGISTRY
    return PROVIDER_REGISTRY[name]


def _run_prompt(existing_key, choice, new_key="", provider_id="", pconfig_name="deepseek"):
    """Invoke _prompt_api_key with mocked input()/getpass() responses."""
    from hades_cli import main as m

    pconfig = _pconfig(pconfig_name)
    with patch("builtins.input", return_value=choice), \
         patch("hades_cli.secret_prompt.masked_secret_prompt", return_value=new_key):
        return m._prompt_api_key(pconfig, existing_key, provider_id=provider_id)


# First-time entry ────────────────────────────────────────────────────────────

def test_first_time_save_new_key(profile_env):
    from hades_cli.config import get_env_value

    key, abort = _run_prompt(existing_key="", choice="", new_key="sk-abcdef")
    assert key == "sk-abcdef"
    assert abort is False
    assert get_env_value("DEEPSEEK_API_KEY") == "sk-abcdef"


def test_first_time_cancelled(profile_env):
    key, abort = _run_prompt(existing_key="", choice="", new_key="")
    assert key == ""
    assert abort is True


# Already configured — K / R / C ───────────────────────────────────────────────

def test_keep_default_empty_input(profile_env):
    from hades_cli.config import save_env_value
    save_env_value("DEEPSEEK_API_KEY", "sk-existing")

    key, abort = _run_prompt(existing_key="sk-existing", choice="")
    assert key == "sk-existing"
    assert abort is False


def test_keep_letter_k(profile_env):
    key, abort = _run_prompt(existing_key="sk-existing", choice="k")
    assert key == "sk-existing"
    assert abort is False


def test_keep_on_unrecognised_input(profile_env):
    """Garbage input falls through to keep — never destroys the user's key."""
    key, abort = _run_prompt(existing_key="sk-existing", choice="xyz")
    assert key == "sk-existing"
    assert abort is False


def test_replace_saves_new_key(profile_env):
    from hades_cli.config import get_env_value, save_env_value
    save_env_value("DEEPSEEK_API_KEY", "sk-malformed-junk")

    key, abort = _run_prompt(
        existing_key="sk-malformed-junk", choice="r", new_key="sk-fresh"
    )
    assert key == "sk-fresh"
    assert abort is False
    assert get_env_value("DEEPSEEK_API_KEY") == "sk-fresh"


def test_replace_readds_env_source_and_clears_suppression(profile_env, capsys):
    old_key = "deepseek-old-" + "a" * 24
    new_key = "deepseek-new-" + "b" * 24
    source = "env:DEEPSEEK_API_KEY"
    (profile_env / ".env").write_text(
        f"export DEEPSEEK_API_KEY={old_key}\n",
        encoding="utf-8",
    )
    (profile_env / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {},
                "suppressed_sources": {"deepseek": [source]},
            }
        ),
        encoding="utf-8",
    )

    key, abort = _run_prompt(
        existing_key=old_key,
        choice="r",
        new_key=new_key,
        provider_id="deepseek",
    )

    assert key == new_key
    assert abort is False
    auth_store = json.loads(
        (profile_env / "auth.json").read_text(encoding="utf-8")
    )
    assert source not in auth_store.get("suppressed_sources", {}).get(
        "deepseek", []
    )
    env_text = (profile_env / ".env").read_text(encoding="utf-8")
    assert old_key not in env_text
    assert new_key in env_text
    captured = capsys.readouterr()
    assert old_key not in captured.out
    assert new_key not in captured.out
    assert old_key not in captured.err
    assert new_key not in captured.err


def test_replace_cancelled_preserves_key(profile_env):
    """Empty entry to the Replace prompt means cancel — keeps the old key intact."""
    from hades_cli.config import get_env_value, save_env_value
    save_env_value("DEEPSEEK_API_KEY", "sk-existing")

    key, abort = _run_prompt(
        existing_key="sk-existing", choice="r", new_key=""
    )
    assert key == "sk-existing"
    assert abort is False
    assert get_env_value("DEEPSEEK_API_KEY") == "sk-existing"


def test_clear_wipes_env_and_aborts(profile_env):
    from hades_cli.config import get_env_value, save_env_value
    save_env_value("DEEPSEEK_API_KEY", "sk-existing")
    save_env_value("OTHER_VAR", "keep-me")

    key, abort = _run_prompt(existing_key="sk-existing", choice="c")
    assert key == ""
    assert abort is True
    # Cleared, but sibling entries untouched.
    assert not get_env_value("DEEPSEEK_API_KEY")
    assert get_env_value("OTHER_VAR") == "keep-me"


def test_clear_reconciles_every_profile_store_without_exposing_secret(
    profile_env,
    capsys,
):
    secret = "deepseek-clear-" + "c" * 24
    other_secret = "deepseek-manual-" + "d" * 24
    source = "env:DEEPSEEK_API_KEY"
    (profile_env / ".env").write_text(
        (
            "# keep this comment\n"
            f"export DEEPSEEK_API_KEY={secret}\n"
            "OTHER_VAR=keep-me\n"
        ),
        encoding="utf-8",
    )
    oauth_state = {
        "tokens": {
            "access_token": "oauth-access-" + "e" * 20,
            "refresh_token": "oauth-refresh-" + "f" * 20,
        }
    }
    (profile_env / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {"deepseek": oauth_state},
                "credential_pool": {
                    "deepseek": [
                        {
                            "id": "env-seed",
                            "label": "DEEPSEEK_API_KEY",
                            "auth_type": "api_key",
                            "priority": 0,
                            "source": source,
                            "access_token": secret,
                        },
                        {
                            "id": "manual",
                            "label": "manual",
                            "auth_type": "api_key",
                            "priority": 1,
                            "source": "manual",
                            "access_token": other_secret,
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (profile_env / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"provider": "custom", "api_key": secret},
                "auxiliary": {"vision": {"api": secret}},
                "custom_providers": {
                    "mirror": {"api_key": secret},
                    "unrelated": {"api_key": other_secret},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (profile_env / "provider_models_cache.json").write_text(
        json.dumps(
            {
                "deepseek": {"models": ["deepseek-chat"]},
                "openrouter": {"models": ["preserve-me"]},
            }
        ),
        encoding="utf-8",
    )

    result = _run_prompt(
        existing_key=secret,
        choice="c",
        provider_id="deepseek",
    )

    assert result == ("", True)
    assert secret not in repr(result)
    env_text = (profile_env / ".env").read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=" not in env_text
    assert "# keep this comment" in env_text
    assert "OTHER_VAR=keep-me" in env_text

    auth_store = json.loads(
        (profile_env / "auth.json").read_text(encoding="utf-8")
    )
    assert auth_store["providers"]["deepseek"] == oauth_state
    assert [
        entry["source"]
        for entry in auth_store["credential_pool"]["deepseek"]
    ] == ["manual"]
    assert source in auth_store["suppressed_sources"]["deepseek"]

    config = yaml.safe_load(
        (profile_env / "config.yaml").read_text(encoding="utf-8")
    )
    assert "api_key" not in config["model"]
    assert "api" not in config["auxiliary"]["vision"]
    assert "api_key" not in config["custom_providers"]["mirror"]
    assert (
        config["custom_providers"]["unrelated"]["api_key"]
        == other_secret
    )
    cache = json.loads(
        (profile_env / "provider_models_cache.json").read_text(
            encoding="utf-8"
        )
    )
    assert "deepseek" not in cache
    assert cache["openrouter"]["models"] == ["preserve-me"]
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_ctrl_c_at_choice_prompt_keeps(profile_env):
    from hades_cli import main as m

    pconfig = _pconfig("deepseek")
    with patch("builtins.input", side_effect=KeyboardInterrupt):
        key, abort = m._prompt_api_key(pconfig, "sk-existing")
    assert key == "sk-existing"
    assert abort is False


# LM Studio no-auth placeholder ────────────────────────────────────────────────

def test_lmstudio_first_time_empty_uses_placeholder(profile_env):
    from hades_cli.auth import LMSTUDIO_NOAUTH_PLACEHOLDER
    from hades_cli.config import get_env_value

    key, abort = _run_prompt(
        existing_key="", choice="", new_key="",
        provider_id="lmstudio", pconfig_name="lmstudio",
    )
    assert key == LMSTUDIO_NOAUTH_PLACEHOLDER
    assert abort is False
    assert get_env_value("LM_API_KEY") == LMSTUDIO_NOAUTH_PLACEHOLDER


def test_lmstudio_replace_empty_does_not_overwrite_with_placeholder(profile_env):
    """On REPLACE with empty input, preserve the user's existing key — do NOT
    silently substitute the placeholder.  The placeholder path only fires for
    first-time configuration where the user has made no explicit choice yet."""
    from hades_cli.config import get_env_value, save_env_value
    save_env_value("LM_API_KEY", "my-real-lmstudio-key")

    key, abort = _run_prompt(
        existing_key="my-real-lmstudio-key", choice="r", new_key="",
        provider_id="lmstudio", pconfig_name="lmstudio",
    )
    assert key == "my-real-lmstudio-key"
    assert abort is False
    assert get_env_value("LM_API_KEY") == "my-real-lmstudio-key"
