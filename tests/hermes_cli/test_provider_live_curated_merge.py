"""Tests for truthful live+curated provider discovery.

Guards two contracts:

* #46850 — when a provider's live /v1/models endpoint returns a stale or
  incomplete list, the static curated models from ``_PROVIDER_MODELS`` must
  still appear in the merged result (nothing is dropped).
* #46309 / #49129 — merge *order* is per-provider. Single providers
  (kimi, zai) stay **curated-first** so a deliberately surfaced newest model
  leads even when the live API lags. ``_LIVE_FIRST_PICKER_PROVIDERS``
  (OpenCode Zen / Go) flip to **live-first** because their live API is the
  authoritative catalog and stale curated entries must not lead the picker.
"""

import dataclasses
import json
from unittest.mock import MagicMock, patch

import pytest

from hades_cli.inventory import ConfigContext, build_models_payload
from hades_cli.model_switch import list_authenticated_providers
from hades_cli.models import (
    _LIVE_FIRST_PICKER_PROVIDERS,
    _discover_exact_resolver,
    PROVIDER_MODEL_DISCOVERY_CONTRACT_VERSION,
    PROVIDER_MODEL_LIVE_ATTEMPT_STATUSES,
    PROVIDER_MODEL_PROVENANCE_VALUES,
    ProviderModelDiscovery,
    provider_model_ids,
    provider_model_discovery,
)


class TestGenericProviderLiveCuratedMerge:
    """provider_model_ids merges live + curated for generic api_key providers."""

    def _make_profile(self, models=None):
        """Create a minimal mock provider profile."""
        p = MagicMock()
        p.auth_type = "api_key"
        p.base_url = "https://api.example.com/v1"
        p.fetch_models.return_value = models
        p.fallback_models = None
        return p

    def test_curated_first_for_single_provider(self):
        """Single providers (zai) stay curated-first; live-only appended."""
        assert "zai" not in _LIVE_FIRST_PICKER_PROVIDERS
        curated = ["glm-5.2", "glm-5.1", "glm-5"]  # authoritative-intent order
        # Live API lags AND surfaces a brand-new model not yet curated.
        live = ["glm-5", "glm-6-preview"]
        profile = self._make_profile(live)

        with (
            patch("providers.get_provider_profile", return_value=profile),
            patch(
                "hades_cli.auth.resolve_api_key_provider_credentials",
                return_value={"api_key": "k", "base_url": ""},
            ),
            patch.dict("hades_cli.models._PROVIDER_MODELS", {"zai": curated}),
        ):
            result = provider_model_ids("zai")

        # Curated entries lead (commit 658ac1d86, #46309).
        assert result[: len(curated)] == curated
        # Live-only entries (glm-6-preview) still surface, appended afterwards.
        assert "glm-6-preview" in result
        assert result.index("glm-6-preview") >= len(curated)
        # No duplicates for models present in both.
        assert result.count("glm-5") == 1

    def test_live_first_for_opencode_zen(self):
        """OpenCode Zen flips to live-first; curated-only models appended."""
        assert "opencode-zen" in _LIVE_FIRST_PICKER_PROVIDERS
        live = ["nemotron-3-ultra-free", "gpt-5.5", "claude-fable-5"]
        curated = ["gpt-5.5", "claude-fable-5", "big-pickle"]
        profile = self._make_profile(live)

        with (
            patch("providers.get_provider_profile", return_value=profile),
            patch(
                "hades_cli.auth.resolve_api_key_provider_credentials",
                return_value={"api_key": "k", "base_url": ""},
            ),
            patch.dict("hades_cli.models._PROVIDER_MODELS", {"opencode-zen": curated}),
        ):
            result = provider_model_ids("opencode-zen")

        # Live entries lead (authoritative aggregator catalog).
        assert result[: len(live)] == list(live)
        assert result[0] == "nemotron-3-ultra-free"
        # Curated-only entries (big-pickle) appended for discovery.
        assert "big-pickle" in result
        assert result.index("big-pickle") >= len(live)
        # No duplicates.
        assert result.count("gpt-5.5") == 1

    def test_no_models_dropped_either_direction(self):
        """Every live AND curated model survives the merge for both modes."""
        live = ["a", "b"]
        # zai = curated-first
        with (
            patch("providers.get_provider_profile", return_value=self._make_profile(live)),
            patch(
                "hades_cli.auth.resolve_api_key_provider_credentials",
                return_value={"api_key": "k", "base_url": ""},
            ),
            patch.dict("hades_cli.models._PROVIDER_MODELS", {"zai": ["c", "b"]}),
        ):
            zai_result = set(provider_model_ids("zai"))
        assert {"a", "b", "c"} <= zai_result

        # opencode-zen = live-first
        with (
            patch("providers.get_provider_profile", return_value=self._make_profile(live)),
            patch(
                "hades_cli.auth.resolve_api_key_provider_credentials",
                return_value={"api_key": "k", "base_url": ""},
            ),
            patch.dict("hades_cli.models._PROVIDER_MODELS", {"opencode-zen": ["c", "b"]}),
        ):
            zen_result = set(provider_model_ids("opencode-zen"))
        assert {"a", "b", "c"} <= zen_result

    def test_case_insensitive_dedup(self):
        """Dedup is case-insensitive but preserves first occurrence casing."""
        live = ["GLM-5.1", "glm-5"]
        curated = ["glm-5.1", "GLM-5", "glm-4.5"]
        profile = self._make_profile(live)

        with (
            patch("providers.get_provider_profile", return_value=profile),
            patch(
                "hades_cli.auth.resolve_api_key_provider_credentials",
                return_value={"api_key": "k", "base_url": ""},
            ),
            patch.dict("hades_cli.models._PROVIDER_MODELS", {"zai": curated}),
        ):
            result = provider_model_ids("zai")

        # zai is curated-first: curated casing wins for models present in both.
        assert result == ["glm-5.1", "GLM-5", "glm-4.5"]


def test_live_and_curated_models_retain_per_model_provenance() -> None:
    profile = MagicMock()
    profile.auth_type = "api_key"
    profile.base_url = "https://api.example.com/v1"
    profile.fetch_models.return_value = ["glm-5", "glm-6-preview"]
    profile.fallback_models = None

    with (
        patch("providers.get_provider_profile", return_value=profile),
        patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={
                "api_key": "memory-only",
                "base_url": "https://api.example.com/v1",
            },
        ),
        patch.dict(
            "hermes_cli.models._PROVIDER_MODELS",
            {"zai": ["glm-5.2", "glm-5"]},
        ),
    ):
        result = provider_model_discovery("zai", force_refresh=True)

    assert PROVIDER_MODEL_DISCOVERY_CONTRACT_VERSION == 1
    assert "authenticated_live" in PROVIDER_MODEL_PROVENANCE_VALUES
    assert "succeeded" in PROVIDER_MODEL_LIVE_ATTEMPT_STATUSES
    assert result.models == ("glm-5.2", "glm-5", "glm-6-preview")
    assert result.live_attempt_status == "succeeded"
    assert result.model_provenance == {
        "glm-5.2": "static_curated",
        "glm-5": "authenticated_live",
        "glm-6-preview": "authenticated_live",
    }
    assert result.provenance_details["glm-5"]["endpoint_identity"]
    assert result.provenance_details["glm-5"]["auth_identity"]
    assert result.provenance_details["glm-5"]["observed_at"]


def test_failed_live_request_labels_visible_fallback_honestly() -> None:
    profile = MagicMock()
    profile.auth_type = "api_key"
    profile.base_url = "https://api.example.com/v1"
    profile.fetch_models.side_effect = TimeoutError("offline")
    profile.fallback_models = None

    with (
        patch("providers.get_provider_profile", return_value=profile),
        patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={
                "api_key": "memory-only",
                "base_url": "https://api.example.com/v1",
            },
        ),
        patch.dict(
            "hermes_cli.models._PROVIDER_MODELS",
            {"zai": ["glm-5.2", "glm-5"]},
        ),
    ):
        result = provider_model_discovery("zai", force_refresh=True)

    assert result.models == ("glm-5.2", "glm-5")
    assert result.live_attempt_status == "failed"
    assert set(result.model_provenance.values()) <= {
        "static_curated",
        "stale_live_cache",
    }
    assert "authenticated_live" not in result.model_provenance.values()


def test_live_discovery_cache_is_scoped_to_exact_credential_fingerprint() -> None:
    resolved = {
        "openai:pool-a": {
            "provider": "openai",
            "api_mode": "chat_completions",
            "base_url": "https://api.example.com/v1",
            "api_key": "pool-a-secret",
            "source": "pool:openai:pool-a",
            "credential_pool_identity": "pool:a",
            "auth_identity": "api-key-pool:a",
        },
        "openai:pool-b": {
            "provider": "openai",
            "api_mode": "chat_completions",
            "base_url": "https://api.example.com/v1",
            "api_key": "pool-b-secret",
            "source": "pool:openai:pool-b",
            "credential_pool_identity": "pool:b",
            "auth_identity": "api-key-pool:b",
        },
    }

    def resolve(*, requested, target_model):
        del target_model
        return dict(resolved[requested])

    def fetch(api_key, base_url, **kwargs):
        del base_url, kwargs
        if api_key == "pool-a-secret":
            return ["gpt-5.4"]
        raise TimeoutError("pool b is offline")

    with (
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolve),
        patch("hermes_cli.models.fetch_api_models", side_effect=fetch),
        patch.dict("hermes_cli.models._PROVIDER_MODELS", {"openai": ["gpt-5.4"]}),
    ):
        pool_a = provider_model_discovery(
            "openai",
            resolver_name="openai:pool-a",
        )
        pool_b = provider_model_discovery(
            "openai",
            resolver_name="openai:pool-b",
        )

    assert pool_a.model_provenance["gpt-5.4"] == "authenticated_live"
    assert pool_b.model_provenance.get("gpt-5.4") != "authenticated_live"
    assert pool_b.credential_fingerprint != pool_a.credential_fingerprint
    with pytest.raises(dataclasses.FrozenInstanceError):
        pool_b.live_attempt_status = "succeeded"


def test_explicit_same_named_resolver_is_bound_to_that_runtime() -> None:
    runtime = {
        "provider": "custom",
        "api_mode": "chat_completions",
        "base_url": "https://work.example.invalid/v1",
        "api_key": "memory-only",
        "source": "named-custom",
        "auth_identity": "api-key:work",
        "credential_pool_identity": "pool:work",
    }

    with (
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=runtime,
        ) as resolver,
        patch(
            "hermes_cli.models.fetch_api_models",
            return_value=["private-model"],
        ) as fetch,
    ):
        discovery = provider_model_discovery(
            "custom:work",
            resolver_name="custom:work",
        )

    resolver.assert_called_once_with(
        requested="custom:work",
        target_model="",
    )
    fetch.assert_called_once_with(
        "memory-only",
        "https://work.example.invalid/v1",
        headers=None,
        api_mode="chat_completions",
    )
    assert discovery.provider == "custom"
    assert discovery.resolver_name == "custom:work"
    assert discovery.live_attempt_status == "succeeded"
    assert discovery.model_provenance == {
        "private-model": "authenticated_live"
    }
    assert discovery.auth_identity == "api-key:work"


def test_probe_disabled_named_resolver_resolves_identity_without_live_listing() -> None:
    runtime = {
        "provider": "custom",
        "api_mode": "chat_completions",
        "base_url": "https://work.example.invalid/v1",
        "api_key": "memory-only",
        "source": "named-custom",
        "auth_identity": "api-key:work",
        "credential_pool_identity": "pool:work",
    }

    with (
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=runtime,
        ) as resolver,
        patch("hermes_cli.models.fetch_api_models") as fetch,
    ):
        discovery = _discover_exact_resolver(
            "custom:work",
            "custom:work",
            force_refresh=False,
            probe_live=False,
            configured_models=("private-model",),
        )

    resolver.assert_called_once_with(
        requested="custom:work",
        target_model="",
    )
    fetch.assert_not_called()
    assert discovery.provider == "custom"
    assert discovery.live_attempt_status == "probe_disabled"
    assert discovery.model_provenance == {
        "private-model": "configured_declared"
    }


def test_exact_copilot_discovery_retains_authenticated_catalog_efforts() -> None:
    runtime = {
        "provider": "copilot",
        "api_mode": "chat_completions",
        "base_url": "https://api.githubcopilot.com",
        "api_key": "memory-only-copilot-token",
        "source": "copilot-subscription",
        "auth_identity": "subscription:copilot-work",
        "credential_pool_identity": "pool:copilot-work",
    }
    catalog = [
        {
            "id": "gpt-5.4",
            "model_picker_enabled": True,
            "supported_endpoints": ["/chat/completions"],
            "capabilities": {
                "type": "chat",
                "supports": {
                    "reasoning_effort": ["low", "medium", "max"],
                },
            },
        }
    ]

    with (
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=runtime,
        ),
        patch(
            "hermes_cli.models.fetch_github_model_catalog",
            return_value=catalog,
        ) as fetch_catalog,
        patch.dict("hermes_cli.models._PROVIDER_MODELS", {"copilot": []}),
    ):
        discovery = _discover_exact_resolver(
            "copilot",
            "copilot:work",
            force_refresh=True,
        )

    assert fetch_catalog.call_count == 1
    assert discovery.models == ("gpt-5.4",)
    assert discovery.model_provenance == {
        "gpt-5.4": "authenticated_live"
    }
    assert discovery.provenance_details["gpt-5.4"][
        "reasoning_options"
    ] == ["low", "medium", "max"]
    assert discovery.provenance_details["gpt-5.4"][
        "reasoning_options_authenticated"
    ] is True


def test_exact_lmstudio_discovery_reuses_native_catalog_for_local_facts() -> None:
    runtime = {
        "provider": "lmstudio",
        "api_mode": "chat_completions",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "lm-studio",
        "source": "configured-local",
        "auth_identity": "local:lmstudio",
    }
    raw_models = [
        {
            "key": "publisher/ready-model",
            "type": "llm",
            "open_weights": True,
            "license_id": "apache-2.0",
            "size_bytes": 4 * 1024**3,
            "loaded_instances": [{"status": "ready"}],
        },
        {
            "key": "publisher/unknown-model",
            "type": "llm",
        },
    ]

    with (
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=runtime,
        ),
        patch(
            "hermes_cli.models._lmstudio_fetch_raw_models",
            return_value=raw_models,
        ) as native_fetch,
        patch("hermes_cli.models.fetch_api_models") as generic_fetch,
        patch.dict("hermes_cli.models._PROVIDER_MODELS", {"lmstudio": []}),
    ):
        discovery = _discover_exact_resolver(
            "lmstudio",
            "lmstudio:default",
            force_refresh=True,
        )

    native_fetch.assert_called_once_with(
        api_key="lm-studio",
        base_url="http://127.0.0.1:1234/v1",
    )
    generic_fetch.assert_not_called()
    assert discovery.models == (
        "publisher/ready-model",
        "publisher/unknown-model",
    )
    ready = discovery.provenance_details["publisher/ready-model"][
        "local_runtime"
    ]
    assert ready == {
        "open_weights": True,
        "license_id": "apache-2.0",
        "model_size_bytes": 4 * 1024**3,
        "loaded_healthy": True,
    }
    unknown = discovery.provenance_details["publisher/unknown-model"][
        "local_runtime"
    ]
    assert unknown == {}


def test_ollama_native_probe_combines_real_tags_show_and_ps_payloads() -> None:
    from hermes_cli.models import _ollama_fetch_native_models

    payloads = {
        ("GET", "/api/tags"): {
            "models": [
                {
                    "name": "qwen3:14b",
                    "model": "qwen3:14b",
                    "size": 8_986_762_240,
                    "digest": "sha256:installed",
                    "details": {
                        "format": "gguf",
                        "family": "qwen3",
                        "parameter_size": "14.8B",
                        "quantization_level": "Q4_K_M",
                    },
                }
            ]
        },
        ("GET", "/api/ps"): {
            "models": [
                {
                    "name": "qwen3:14b",
                    "model": "qwen3:14b",
                    "digest": "sha256:installed",
                    "size": 8_986_762_240,
                    "size_vram": 8_000_000_000,
                }
            ]
        },
        ("POST", "/api/show"): {
            "license": "Apache-2.0",
            "details": {
                "format": "gguf",
                "family": "qwen3",
                "parameter_size": "14.8B",
                "quantization_level": "Q4_K_M",
            },
            "capabilities": ["completion", "tools"],
        },
    }
    calls: list[tuple[str, str, dict | None]] = []

    class Response:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    def urlopen(request, timeout):
        from urllib.parse import urlparse

        method = request.get_method()
        path = urlparse(request.full_url).path
        body = None if request.data is None else json.loads(request.data)
        calls.append((method, path, body))
        return Response(payloads[(method, path)])

    with patch("urllib.request.urlopen", side_effect=urlopen):
        rows = _ollama_fetch_native_models(
            base_url="http://127.0.0.1:11434/v1",
            timeout=2.0,
        )

    assert calls == [
        ("GET", "/api/tags", None),
        ("GET", "/api/ps", None),
        ("POST", "/api/show", {"model": "qwen3:14b"}),
    ]
    assert rows == [
        {
            "model": "qwen3:14b",
            "digest": "sha256:installed",
            "model_size_bytes": 8_986_762_240,
            "format": "gguf",
            "license_id": "apache-2.0",
            "open_weights": True,
            "loaded_healthy": True,
            "available_vram_bytes": 8_000_000_000,
            "supports_tools": True,
        }
    ]


def test_opt_in_payload_propagates_authenticated_catalog_efforts() -> None:
    context = ConfigContext(
        current_provider="copilot",
        current_model="gpt-5.4",
        current_base_url="",
        user_providers={},
        custom_providers=[],
    )
    row = {
        "slug": "copilot",
        "name": "GitHub Copilot",
        "is_current": True,
        "is_user_defined": False,
        "models": ["gpt-5.4"],
        "total_models": 1,
        "source": "copilot-subscription",
        "discovery": {
            "contract_version": 1,
            "provider": "copilot",
            "resolver_name": "copilot:work",
            "models": ["gpt-5.4"],
            "model_provenance": {"gpt-5.4": "authenticated_live"},
            "provenance_details": {
                "gpt-5.4": {
                    "endpoint_identity": "endpoint:copilot",
                    "auth_identity": "subscription:copilot-work",
                    "observed_at": "2026-01-01T00:00:00Z",
                    "reasoning_options": ["low", "medium", "max"],
                    "reasoning_options_authenticated": True,
                }
            },
            "live_attempt_status": "succeeded",
            "observed_at": "2026-01-01T00:00:00Z",
            "credential_fingerprint": "fingerprint:copilot-work",
            "endpoint_identity": "endpoint:copilot",
            "auth_identity": "subscription:copilot-work",
            "credential_pool_identity": "pool:copilot-work",
            "api_mode": "chat_completions",
            "source": "copilot-subscription",
        },
    }

    with (
        patch(
            "hermes_cli.model_switch.list_authenticated_providers",
            return_value=[row],
        ),
        patch("hermes_cli.inventory._moa_provider_row", return_value=None),
        patch("agent.models_dev.get_model_capabilities", return_value=None),
    ):
        payload = build_models_payload(
            context,
            capabilities=True,
            discovery_provenance=True,
        )

    capabilities = payload["providers"][0]["capabilities"]["gpt-5.4"]
    assert capabilities["reasoning_options"] == ["low", "medium", "max"]
    assert capabilities["reasoning_options_authenticated"] is True


def test_local_payload_uses_host_ram_only_for_loopback_backends() -> None:
    context = ConfigContext(
        current_provider="lmstudio",
        current_model="publisher/model",
        current_base_url="http://127.0.0.1:1234/v1",
        user_providers={},
        custom_providers=[],
    )
    base_row = {
        "slug": "lmstudio",
        "name": "LM Studio",
        "is_current": True,
        "is_user_defined": False,
        "models": ["publisher/model"],
        "total_models": 1,
        "source": "configured",
        "discovery": {
            "contract_version": 1,
            "provider": "lmstudio",
            "resolver_name": "lmstudio:default",
            "models": ["publisher/model"],
            "model_provenance": {
                "publisher/model": "authenticated_live"
            },
            "provenance_details": {
                "publisher/model": {
                    "endpoint_identity": "endpoint:lmstudio",
                    "auth_identity": "local:lmstudio",
                    "observed_at": "2026-01-01T00:00:00Z",
                    "local_runtime": {},
                }
            },
            "live_attempt_status": "succeeded",
            "observed_at": "2026-01-01T00:00:00Z",
            "credential_fingerprint": "fingerprint:lmstudio",
            "endpoint_identity": "endpoint:lmstudio",
            "auth_identity": "local:lmstudio",
            "credential_pool_identity": "",
            "api_mode": "chat_completions",
            "source": "configured-local",
        },
    }

    def build_for(api_url: str) -> dict:
        row = json.loads(json.dumps(base_row))
        row["api_url"] = api_url
        with patch(
            "hermes_cli.model_switch.list_authenticated_providers",
            return_value=[row],
        ):
            return build_models_payload(
                context,
                discovery_provenance=True,
            )

    with (
        patch("hermes_cli.inventory._moa_provider_row", return_value=None),
        patch("psutil.virtual_memory", return_value=MagicMock(available=16 * 1024**3)) as memory,
    ):
        loopback = build_for("http://127.0.0.1:1234/v1")
        remote = build_for("http://192.168.1.20:1234/v1")

    loopback_facts = loopback["providers"][0]["local_runtime"]["publisher/model"]
    assert loopback_facts["available_ram_bytes"] == 16 * 1024**3
    assert loopback_facts["available_vram_bytes"] is None
    remote_facts = remote["providers"][0]["local_runtime"]["publisher/model"]
    assert remote_facts["available_ram_bytes"] is None
    assert remote_facts["available_vram_bytes"] is None
    memory.assert_called_once_with()


def test_opt_in_inventory_reuses_named_resolver_probe_for_payload() -> None:
    calls: list[tuple[str, str | None]] = []

    def discover(provider, *, resolver_name=None, **kwargs):
        del kwargs
        calls.append((provider, resolver_name))
        return ProviderModelDiscovery(
            provider="custom",
            resolver_name=str(resolver_name),
            models=("private-model",),
            model_provenance={"private-model": "authenticated_live"},
            provenance_details={
                "private-model": {
                    "endpoint_identity": "endpoint:work",
                    "auth_identity": "api-key:work",
                    "observed_at": "2026-07-15T12:00:00Z",
                }
            },
            live_attempt_status="succeeded",
            observed_at="2026-07-15T12:00:00Z",
            credential_fingerprint="fingerprint:work",
            endpoint_identity="endpoint:work",
            auth_identity="api-key:work",
        )

    with (
        patch("agent.models_dev.fetch_models_dev", return_value={}),
        patch("hermes_cli.providers.HERMES_OVERLAYS", {}),
        patch(
            "hermes_cli.models.provider_model_discovery",
            side_effect=discover,
        ),
    ):
        rows = list_authenticated_providers(
            current_provider="custom:work",
            user_providers={},
            custom_providers=[
                {
                    "name": "Work",
                    "base_url": "https://work.example.invalid/v1",
                    "api_key": "memory-only",
                    "model": "configured-model",
                }
            ],
            discovery_provenance=True,
        )

    row = next(item for item in rows if item["slug"] == "custom:work")
    assert calls.count(("custom:work", "custom:work")) == 1
    assert row["models"] == ["private-model"]
    assert row["discovery"]["models"] == ["private-model"]


def test_legacy_list_wrapper_retains_ordered_model_ids() -> None:
    discovery = ProviderModelDiscovery(
        provider="zai",
        resolver_name="zai",
        models=("glm-5.2", "glm-5", "glm-4.5"),
        model_provenance={
            "glm-5.2": "static_curated",
            "glm-5": "authenticated_live",
            "glm-4.5": "static_curated",
        },
        provenance_details={
            "glm-5.2": {"source": "curated"},
            "glm-5": {
                "endpoint_identity": "endpoint:test",
                "auth_identity": "api-key:test",
                "observed_at": "2026-07-15T12:00:00Z",
            },
            "glm-4.5": {"source": "curated"},
        },
        live_attempt_status="succeeded",
        observed_at="2026-07-15T12:00:00Z",
        credential_fingerprint="fingerprint:test",
        endpoint_identity="endpoint:test",
        auth_identity="api-key:test",
    )

    with patch("hermes_cli.models.provider_model_discovery", return_value=discovery):
        assert provider_model_ids("zai") == list(discovery.models)


def _without_discovery_fields(payload: dict) -> dict:
    clean = json.loads(json.dumps(payload, sort_keys=True))
    for row in clean["providers"]:
        row.pop("discovery", None)
    return clean


def test_discovery_payload_is_opt_in_and_default_shape_is_unchanged() -> None:
    context = ConfigContext(
        current_provider="zai",
        current_model="glm-5.2",
        current_base_url="",
        user_providers={},
        custom_providers=[],
    )
    base_row = {
        "slug": "zai",
        "name": "Z.AI",
        "is_current": True,
        "is_user_defined": False,
        "models": ["glm-5.2"],
        "total_models": 1,
        "source": "built-in",
    }
    discovery = {
        "contract_version": 1,
        "provider": "zai",
        "resolver_name": "zai",
        "models": ["glm-5.2"],
        "model_provenance": {"glm-5.2": "authenticated_live"},
        "provenance_details": {
            "glm-5.2": {
                "endpoint_identity": "endpoint:test",
                "auth_identity": "api-key:test",
                "observed_at": "2026-07-15T12:00:00Z",
            }
        },
        "live_attempt_status": "succeeded",
        "observed_at": "2026-07-15T12:00:00Z",
        "credential_fingerprint": "fingerprint:test",
        "endpoint_identity": "endpoint:test",
        "auth_identity": "api-key:test",
    }

    def rows(**kwargs):
        row = dict(base_row)
        if kwargs.get("discovery_provenance"):
            row["discovery"] = discovery
        return [row]

    with (
        patch("hermes_cli.model_switch.list_authenticated_providers", side_effect=rows),
        patch("hermes_cli.inventory._moa_provider_row", return_value=None),
    ):
        baseline = build_models_payload(context)
        enriched = build_models_payload(context, discovery_provenance=True)

    baseline_bytes = json.dumps(
        baseline,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    stripped_bytes = json.dumps(
        _without_discovery_fields(enriched),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert stripped_bytes == baseline_bytes
    assert all("discovery" in row for row in enriched["providers"])
