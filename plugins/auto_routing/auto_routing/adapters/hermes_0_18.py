"""Hermes 0.18 inventory and exact-runtime projection adapter.

The adapter is deliberately the only Task 6 layer that knows Hermes picker
and runtime-resolver shapes. Durable plugin state receives non-secret identity
hashes; raw endpoints and credentials exist only in :class:`ResolvedRuntime`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from agent.auxiliary_client import OneShotTransportError, call_llm_exact_once
from agent.models_dev import get_model_capabilities, get_model_info
from agent.runtime_routing import AgentRuntimeSpec
from hermes_cli.inventory import build_models_payload, load_picker_context
from hermes_cli.runtime_provider import resolve_runtime_provider
from hermes_constants import (
    effective_generic_reasoning_effort,
    resolve_reasoning_config,
)

from ..models import AccessEconomics, RuntimeKey, RuntimeObservation
from .base import (
    PERSISTED_RUNTIME_PROJECTION_CONTRACT,
    AccessVerification,
    AdapterInventory,
    LocalInventoryRow,
    PersistedRuntimeProjection,
    ProviderInventoryRow,
    ResolvedRuntime,
    RuntimeResolutionMismatch,
    VerificationOutcomeUncertain,
    VerificationRequest,
    _resolved_runtime_execution,
    ensure_runtime_match,
)

_FIXED_PROMPT = "Return exactly AUTO_ROUTING_ACCESS_OK"
_FIXED_MAXIMUM_OUTPUT_TOKENS = 16
_NON_AGENTIC_MODEL = re.compile(
    r"(?:^|[-_/.])(?:embed(?:ding)?|rerank|moderation|tts|speech|audio|"
    r"image|vision-encoder|whisper)(?:$|[-_/.])",
    re.IGNORECASE,
)
_SUBSCRIPTION_PROVIDERS = frozenset(
    {
        "copilot",
        "copilot-acp",
        "openai-codex",
        "xai-oauth",
    }
)
_LOCAL_PROVIDERS = frozenset(
    {
        "llama.cpp",
        "llamacpp",
        "lmstudio",
        "ollama",
        "vllm",
    }
)
_PERSISTED_PROJECTION_KEY = "auto_routing_projection"
_PERSISTED_PROJECTION_CONTRACT = "hermes-0.18-persisted-projection-v1"
_PERSISTED_PROJECTION_SCHEMA_SHA = hashlib.sha256(
    b"runtime-key+resolver+access+fallback-owner;hmac-sha256;v1"
).hexdigest()
_PERSISTED_PROJECTION_BODY_FIELDS = frozenset(
    {
        "contract",
        "schema_sha256",
        "runtime_id",
        "resolver_name",
        "provider",
        "model",
        "api_mode",
        "credential_pool_identity",
        "local_backend",
        "access_kind",
        "fallback_owner",
    }
)
_PERSISTED_PROJECTION_FIELDS = _PERSISTED_PROJECTION_BODY_FIELDS | {
    "descriptor_sha256",
    "attestation_kind",
    "attestation_sha256",
}
_UNSUPPORTED_RESOLVERS = frozenset({"", "auto", "moa"})


@dataclass(frozen=True)
class _Projection:
    resolver_name: str
    economics: AccessEconomics
    local_backend: str = ""


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _hashed_identity(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}:{digest[:24]}"


def _endpoint_identity(provider: str, base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/").lower()
    return _hashed_identity(
        "endpoint",
        normalized or f"provider:{provider}",
    )


def _is_loopback_url(value: str) -> bool:
    try:
        hostname = (urlparse(str(value or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _identity_fallback(prefix: str, provider: str, resolver_name: str) -> str:
    return _hashed_identity(prefix, f"{provider}|{resolver_name}|unknown")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _raw_price_per_million(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    per_million = parsed * Decimal(1_000_000)
    converted = float(per_million)
    if not math.isfinite(converted):
        return None
    return converted


def _capabilities(provider: str, model: str, row: Mapping[str, Any]) -> dict[str, Any]:
    row_capabilities = _as_mapping(_as_mapping(row.get("capabilities")).get(model))
    metadata = get_model_capabilities(provider, model)
    model_info = get_model_info(provider, model)
    supports_tools = row_capabilities.get("supports_tools")
    if supports_tools is None and metadata is not None:
        supports_tools = bool(metadata.supports_tools)

    supports_reasoning = row_capabilities.get("supports_reasoning")
    if supports_reasoning is None:
        supports_reasoning = row_capabilities.get("reasoning")
    if supports_reasoning is None and metadata is not None:
        supports_reasoning = bool(metadata.supports_reasoning)

    options = row_capabilities.get("reasoning_options")
    if not isinstance(options, (list, tuple)) and metadata is not None:
        options = metadata.reasoning_options
    if not isinstance(options, (list, tuple)):
        options = ()
    input_modalities = row_capabilities.get("input_modalities")
    if not isinstance(input_modalities, (list, tuple)) and model_info is not None:
        input_modalities = model_info.input_modalities
    if not isinstance(input_modalities, (list, tuple)):
        input_modalities = ()
    output_modalities = row_capabilities.get("output_modalities")
    if not isinstance(output_modalities, (list, tuple)) and model_info is not None:
        output_modalities = model_info.output_modalities
    if not isinstance(output_modalities, (list, tuple)):
        output_modalities = ()
    context_window = row_capabilities.get("context_window")
    if (
        (isinstance(context_window, bool) or not isinstance(context_window, int))
        and model_info is not None
    ):
        context_window = model_info.context_window
    max_output_tokens = row_capabilities.get("max_output_tokens")
    if (
        (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
        )
        and model_info is not None
    ):
        max_output_tokens = model_info.max_output
    supports_structured_output = row_capabilities.get(
        "supports_structured_output"
    )
    if supports_structured_output is None and model_info is not None:
        supports_structured_output = bool(model_info.structured_output)
    return {
        "supports_tools": supports_tools,
        "supports_structured_output": supports_structured_output,
        "input_modalities": tuple(
            dict.fromkeys(
                str(modality).strip().casefold()
                for modality in input_modalities
                if str(modality).strip()
            )
        ),
        "output_modalities": tuple(
            dict.fromkeys(
                str(modality).strip().casefold()
                for modality in output_modalities
                if str(modality).strip()
            )
        ),
        "context_window": (
            context_window
            if isinstance(context_window, int)
            and not isinstance(context_window, bool)
            and context_window > 0
            else None
        ),
        "max_output_tokens": (
            max_output_tokens
            if isinstance(max_output_tokens, int)
            and not isinstance(max_output_tokens, bool)
            and max_output_tokens > 0
            else None
        ),
        "supports_reasoning": bool(supports_reasoning),
        "reasoning_options": tuple(
            dict.fromkeys(
                str(option).strip().lower()
                for option in options
                if str(option).strip()
            )
        ),
        "reasoning_options_authenticated": (
            row_capabilities.get("reasoning_options_authenticated") is True
        ),
    }


def _explicit_economics(
    row: Mapping[str, Any],
    model: str,
) -> AccessEconomics | None:
    raw = _as_mapping(row.get("access_economics"))
    candidate = raw.get(model) if model in raw else raw
    if not isinstance(candidate, Mapping) or "billing_kind" not in candidate:
        return None
    try:
        return AccessEconomics.model_validate(dict(candidate))
    except Exception:
        return None


def _subscription_economics(
    provider: str,
    row: Mapping[str, Any],
    observed_at: str,
) -> AccessEconomics:
    raw = _as_mapping(row.get("subscription"))
    return AccessEconomics(
        billing_kind="subscription",
        effective_marginal_cost_usd_per_task=raw.get(
            "effective_marginal_cost_usd_per_task"
        ),
        effective_amortized_cost_usd_per_task=raw.get(
            "effective_amortized_cost_usd_per_task"
        ),
        subscription_plan=str(raw.get("plan") or provider),
        subscription_quota_limit=raw.get("quota_limit"),
        subscription_quota_remaining=raw.get("quota_remaining"),
        subscription_quota_unit=str(raw.get("quota_unit") or "completion"),
        subscription_reset_at=raw.get("reset_at"),
        subscription_state=str(raw.get("state") or "unknown"),
        throttle_state=(
            str(row.get("throttle_state"))
            if row.get("throttle_state")
            else None
        ),
        cooldown_until=(
            str(row.get("cooldown_until"))
            if row.get("cooldown_until")
            else None
        ),
        source_id=f"{provider}-subscription",
        provenance="hermes-access-path",
        observed_at=observed_at,
    )


def _metered_economics(
    provider: str,
    model: str,
    row: Mapping[str, Any],
    observed_at: str,
) -> AccessEconomics:
    price = _as_mapping(
        _as_mapping(_as_mapping(row.get("discovery")).get("pricing")).get(model)
    )
    pricing_observed_at = str(
        price.get("observed_at") or "pricing-observation-unavailable"
    )
    source_id = str(price.get("source_id") or "pricing-unavailable")
    ttl_seconds = price.get("ttl_seconds")
    return AccessEconomics(
        billing_kind="metered",
        metered_input_usd_per_million_tokens=_raw_price_per_million(
            price.get("input_usd_per_token")
        ),
        metered_output_usd_per_million_tokens=_raw_price_per_million(
            price.get("output_usd_per_token")
        ),
        throttle_state=(
            str(row.get("throttle_state"))
            if row.get("throttle_state")
            else None
        ),
        cooldown_until=(
            str(row.get("cooldown_until"))
            if row.get("cooldown_until")
            else None
        ),
        source_id=source_id,
        evidence_ttl_seconds=ttl_seconds,
        provenance=(
            "provider-pricing-snapshot" if price else "pricing-unavailable"
        ),
        observed_at=pricing_observed_at,
    )


def _provider_economics(
    provider: str,
    model: str,
    auth_identity: str,
    row: Mapping[str, Any],
    observed_at: str,
) -> AccessEconomics:
    explicit = _explicit_economics(row, model)
    subscription = (
        auth_identity.startswith("subscription:")
        or provider in _SUBSCRIPTION_PROVIDERS
    )
    if explicit is not None and (
        (subscription and explicit.billing_kind == "subscription")
        or (not subscription and explicit.billing_kind == "metered")
    ):
        return explicit
    if subscription:
        # Public token pricing on the picker row belongs to a different,
        # metered API-key path and is intentionally ignored here.
        return _subscription_economics(provider, row, observed_at)
    return _metered_economics(provider, model, row, observed_at)


def _local_economics(
    provider: str,
    model: str,
    row: Mapping[str, Any],
    observed_at: str,
) -> AccessEconomics:
    raw = _as_mapping(row.get("local_runtime"))
    raw = _as_mapping(raw.get(model)) if model in raw else raw
    return AccessEconomics(
        billing_kind="local",
        local_energy_cost_usd_per_task=raw.get("energy_cost_usd_per_task"),
        local_compute_cost_usd_per_task=raw.get("compute_cost_usd_per_task"),
        source_id=f"{provider}-local-runtime",
        provenance="local-backend-inspection",
        observed_at=observed_at,
    )


def _runtime_identity(
    provider: str,
    resolver_name: str,
    runtime: Mapping[str, Any],
    *,
    local_backend: str,
    inventory_revision: str,
    model: str,
) -> RuntimeKey:
    base_url = str(runtime.get("base_url") or "")
    endpoint_identity = str(runtime.get("endpoint_identity") or "")
    if not endpoint_identity:
        endpoint_identity = _endpoint_identity(provider, base_url)

    credential_pool_identity = str(
        runtime.get("credential_pool_identity") or ""
    )
    pool = runtime.get("credential_pool")
    if not credential_pool_identity and pool is not None:
        pool_provider = str(getattr(pool, "provider", "") or "").strip()
        if pool_provider:
            credential_pool_identity = f"pool:{pool_provider}"

    auth_identity = str(runtime.get("auth_identity") or "")
    if not auth_identity:
        if credential_pool_identity:
            auth_identity = credential_pool_identity
        elif provider in _SUBSCRIPTION_PROVIDERS:
            auth_identity = f"subscription:{resolver_name}"
        elif runtime.get("api_key"):
            auth_identity = f"api-key:{resolver_name}"
        else:
            auth_identity = f"local:{resolver_name}"

    return RuntimeKey(
        provider=str(runtime.get("provider") or provider),
        model=model,
        auth_identity=auth_identity,
        credential_pool_identity=credential_pool_identity,
        endpoint_identity=endpoint_identity,
        api_mode=str(runtime.get("api_mode") or "chat_completions"),
        local_backend=str(runtime.get("local_backend") or local_backend),
        inventory_revision=inventory_revision,
    )


def _projection_key(
    *,
    provider: str,
    model: str,
    auth_identity: str,
    credential_pool_identity: str,
    endpoint_identity: str,
    api_mode: str,
    local_backend: str,
) -> str:
    return RuntimeKey(
        provider=provider,
        model=model,
        auth_identity=auth_identity,
        credential_pool_identity=credential_pool_identity,
        endpoint_identity=endpoint_identity,
        api_mode=api_mode,
        local_backend=local_backend,
        inventory_revision="adapter-projection",
    ).stable_id()


def _projection_access_kind(runtime_key: RuntimeKey) -> str:
    if runtime_key.local_backend:
        return "local"
    return runtime_key.auth_identity.partition(":")[0].strip().casefold()


def _projection_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class Hermes018Adapter:
    """Project Hermes 0.18 configured access paths into Task 6 evidence."""

    def __init__(
        self,
        *,
        model_aliases: Mapping[tuple[str, str], str] | None = None,
        credential_fingerprint_key: bytes | None = None,
    ) -> None:
        self._projections: dict[str, _Projection] = {}
        self._inherited_runtime_keys: tuple[RuntimeKey, ...] = ()
        self._credential_fingerprint_key = (
            None
            if credential_fingerprint_key is None
            else bytes(credential_fingerprint_key)
        )
        if (
            self._credential_fingerprint_key is not None
            and len(self._credential_fingerprint_key) != 32
        ):
            raise ValueError("credential fingerprint key must contain 32 bytes")
        self._model_aliases = {
            (str(provider).lower(), str(alias).lower()): str(canonical)
            for (provider, alias), canonical in (model_aliases or {}).items()
        }

    def _persisted_projection_descriptor(
        self,
        *,
        runtime_key: RuntimeKey,
        resolver_name: str,
    ) -> dict[str, Any]:
        """Build one content-free projection attestation at inventory time."""
        body = {
            "contract": _PERSISTED_PROJECTION_CONTRACT,
            "schema_sha256": _PERSISTED_PROJECTION_SCHEMA_SHA,
            "runtime_id": runtime_key.stable_id(),
            "resolver_name": str(resolver_name).strip(),
            "provider": runtime_key.provider,
            "model": runtime_key.model,
            "api_mode": runtime_key.api_mode,
            "credential_pool_identity": runtime_key.credential_pool_identity,
            "local_backend": runtime_key.local_backend,
            "access_kind": _projection_access_kind(runtime_key),
            "fallback_owner": "auto-routing-pre-call",
        }
        descriptor_sha = hashlib.sha256(_projection_json(body)).hexdigest()
        signed = {**body, "descriptor_sha256": descriptor_sha}
        if self._credential_fingerprint_key is None:
            attestation_kind = "unavailable"
            attestation_sha = ""
        else:
            attestation_kind = "profile-hmac-sha256-v1"
            attestation_sha = hmac.new(
                self._credential_fingerprint_key,
                _projection_json(signed),
                hashlib.sha256,
            ).hexdigest()
        return {
            **signed,
            "attestation_kind": attestation_kind,
            "attestation_sha256": attestation_sha,
        }

    def _validated_persisted_projection_descriptor(
        self,
        observation: RuntimeObservation,
    ) -> Mapping[str, Any]:
        if not isinstance(observation, RuntimeObservation):
            raise TypeError("persisted runtime observation is required")
        raw = observation.capabilities.get(_PERSISTED_PROJECTION_KEY)
        if not isinstance(raw, Mapping):
            raise ValueError("persisted projection metadata is missing")
        descriptor = dict(raw)
        if descriptor.get("contract") != _PERSISTED_PROJECTION_CONTRACT:
            raise ValueError("persisted projection contract is unsupported")
        if set(descriptor) != _PERSISTED_PROJECTION_FIELDS:
            raise ValueError("persisted projection descriptor is malformed")
        if descriptor.get("schema_sha256") != _PERSISTED_PROJECTION_SCHEMA_SHA:
            raise ValueError("persisted projection schema is unsupported")

        key = observation.key
        expected = {
            "runtime_id": key.stable_id(),
            "provider": key.provider,
            "model": key.model,
            "api_mode": key.api_mode,
            "credential_pool_identity": key.credential_pool_identity,
            "local_backend": key.local_backend,
            "access_kind": _projection_access_kind(key),
            "fallback_owner": "auto-routing-pre-call",
        }
        if any(descriptor.get(name) != value for name, value in expected.items()):
            raise ValueError(
                "persisted projection descriptor does not match runtime observation"
            )
        resolver_name = descriptor.get("resolver_name")
        access_kind = descriptor.get("access_kind")
        if (
            not isinstance(resolver_name, str)
            or resolver_name.strip().casefold() in _UNSUPPORTED_RESOLVERS
            or not re.fullmatch(r"[A-Za-z0-9_.:/@+\-]{1,256}", resolver_name)
            or access_kind in {"", "unconfigured", "unknown"}
        ):
            raise ValueError("persisted projection resolver is unsupported")

        body = {
            name: descriptor[name]
            for name in _PERSISTED_PROJECTION_BODY_FIELDS
        }
        descriptor_sha = hashlib.sha256(_projection_json(body)).hexdigest()
        if not hmac.compare_digest(
            str(descriptor.get("descriptor_sha256") or ""),
            descriptor_sha,
        ):
            raise ValueError("persisted projection descriptor checksum changed")
        if (
            self._credential_fingerprint_key is None
            or descriptor.get("attestation_kind")
            != "profile-hmac-sha256-v1"
        ):
            raise ValueError("persisted projection attestation is unavailable")
        signed = {**body, "descriptor_sha256": descriptor_sha}
        expected_attestation = hmac.new(
            self._credential_fingerprint_key,
            _projection_json(signed),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(
            str(descriptor.get("attestation_sha256") or ""),
            expected_attestation,
        ):
            raise ValueError("persisted projection attestation changed")
        return descriptor

    def inventory(self, refresh: bool = False) -> AdapterInventory:
        payload = build_models_payload(
            load_picker_context(),
            picker_hints=True,
            pricing=True,
            capabilities=True,
            discovery_provenance=True,
            refresh=refresh,
        )
        self._projections.clear()
        inherited_runtime_keys: list[RuntimeKey] = []
        inherited_model = str(payload.get("model") or "").strip()
        provider_rows: list[ProviderInventoryRow] = []
        local_rows: list[LocalInventoryRow] = []
        for raw_row in payload.get("providers") or []:
            if not isinstance(raw_row, Mapping):
                continue
            row = dict(raw_row)
            slug = str(row.get("slug") or "").strip()
            if not slug or slug.lower() == "moa":
                continue
            discovery = _as_mapping(row.get("discovery"))
            provider = str(discovery.get("provider") or slug).strip()
            resolver_name = str(
                discovery.get("resolver_name")
                or row.get("resolver_name")
                or slug
            ).strip()
            models = tuple(
                dict.fromkeys(
                    str(model).strip()
                    for model in (row.get("models") or ())
                    if str(model).strip()
                )
            )
            if not models:
                continue
            endpoint_identity = str(
                discovery.get("endpoint_identity")
                or _identity_fallback("endpoint", provider, resolver_name)
            )
            auth_identity = str(
                discovery.get("auth_identity")
                or f"unconfigured:{resolver_name}"
            )
            pool_identity = str(
                discovery.get("credential_pool_identity") or ""
            )
            api_mode = str(
                discovery.get("api_mode") or "chat_completions"
            )
            observed_at = str(
                discovery.get("observed_at")
                or row.get("observed_at")
                or "1970-01-01T00:00:00Z"
            )
            credential_fingerprint = str(
                discovery.get("credential_fingerprint")
                or _identity_fallback("credential", provider, resolver_name)
            )
            raw_provenance = _as_mapping(discovery.get("model_provenance"))
            raw_details = _as_mapping(discovery.get("provenance_details"))
            capability_map = {
                model: _capabilities(provider, model, row)
                for model in models
            }
            agentic_models = tuple(
                model
                for model in models
                if capability_map[model]["supports_tools"] is not False
                and _NON_AGENTIC_MODEL.search(model) is None
            )
            if not agentic_models:
                continue

            api_url = str(row.get("api_url") or "")
            is_local = (
                slug.lower() in _LOCAL_PROVIDERS
                or _is_loopback_url(api_url)
            )
            if is_local:
                local_metadata_by_model = _as_mapping(row.get("local_runtime"))
                for model in agentic_models:
                    local_metadata = _as_mapping(
                        local_metadata_by_model.get(model)
                    )
                    if not local_metadata:
                        local_metadata = local_metadata_by_model
                    backend_identity = str(
                        local_metadata.get("backend_identity")
                        or f"{slug}:{endpoint_identity}"
                    )
                    if "://" in backend_identity:
                        backend_identity = _hashed_identity(
                            "local-backend",
                            backend_identity,
                        )
                    provenance = raw_provenance.get(model)
                    installed = provenance == "authenticated_live"
                    runtime_auth = f"local:{backend_identity}"
                    runtime_endpoint = f"local-backend:{backend_identity}"
                    projection_runtime_key = RuntimeKey(
                        provider=provider,
                        model=model,
                        auth_identity=runtime_auth,
                        credential_pool_identity="",
                        endpoint_identity=runtime_endpoint,
                        api_mode=api_mode,
                        local_backend=backend_identity,
                        inventory_revision="adapter-projection",
                    )
                    local_row = LocalInventoryRow(
                        provider=provider,
                        resolver_name=resolver_name,
                        model=model,
                        backend_identity=backend_identity,
                        reachable=(
                            discovery.get("live_attempt_status") == "succeeded"
                        ),
                        installed=installed,
                        open_weights=bool(local_metadata.get("open_weights", False)),
                        license_id=(
                            str(local_metadata.get("license_id"))
                            if local_metadata.get("license_id")
                            else None
                        ),
                        model_size_bytes=local_metadata.get("model_size_bytes"),
                        available_ram_bytes=local_metadata.get(
                            "available_ram_bytes"
                        ),
                        available_vram_bytes=local_metadata.get(
                            "available_vram_bytes"
                        ),
                        loaded_healthy=bool(
                            local_metadata.get("loaded_healthy", False)
                        ),
                        hardware_compatible=local_metadata.get(
                            "hardware_compatible"
                        ),
                        api_mode=api_mode,
                        capabilities={
                            **capability_map[model],
                            _PERSISTED_PROJECTION_KEY: (
                                self._persisted_projection_descriptor(
                                    runtime_key=projection_runtime_key,
                                    resolver_name=resolver_name,
                                )
                            ),
                            "local_evidence_backend": str(
                                local_metadata.get("evidence_backend")
                                or slug.lower()
                            ),
                            "local_evidence_supported": (
                                local_metadata.get("evidence_supported") is True
                                or slug.lower() in {"lmstudio", "ollama"}
                            ),
                        },
                        economics=_local_economics(
                            provider,
                            model,
                            row,
                            observed_at,
                        ),
                        observed_at=observed_at,
                    )
                    local_rows.append(local_row)
                    stable_id = _projection_key(
                        provider=provider,
                        model=model,
                        auth_identity=runtime_auth,
                        credential_pool_identity="",
                        endpoint_identity=runtime_endpoint,
                        api_mode=api_mode,
                        local_backend=backend_identity,
                    )
                    self._projections[stable_id] = _Projection(
                        resolver_name=resolver_name,
                        economics=local_row.economics,
                        local_backend=backend_identity,
                    )
                    if row.get("is_current") is True and model == inherited_model:
                        inherited_runtime_keys.append(
                            RuntimeKey(
                                provider=provider,
                                model=model,
                                auth_identity=runtime_auth,
                                credential_pool_identity="",
                                endpoint_identity=runtime_endpoint,
                                api_mode=api_mode,
                                local_backend=backend_identity,
                                inventory_revision="adapter-projection",
                            )
                        )
                continue

            model_provenance = {
                model: (
                    str(raw_provenance[model])
                    if model in raw_provenance
                    and raw_provenance[model] is not None
                    else None
                )
                for model in agentic_models
            }
            provenance_details = {
                model: dict(_as_mapping(raw_details.get(model)))
                for model in agentic_models
            }
            economics = {
                model: _provider_economics(
                    provider,
                    model,
                    auth_identity,
                    row,
                    observed_at,
                )
                for model in agentic_models
            }
            provider_capabilities = {}
            for model in agentic_models:
                projection_runtime_key = RuntimeKey(
                    provider=provider,
                    model=model,
                    auth_identity=auth_identity,
                    credential_pool_identity=pool_identity,
                    endpoint_identity=endpoint_identity,
                    api_mode=api_mode,
                    local_backend="",
                    inventory_revision="adapter-projection",
                )
                provider_capabilities[model] = {
                    **capability_map[model],
                    _PERSISTED_PROJECTION_KEY: (
                        self._persisted_projection_descriptor(
                            runtime_key=projection_runtime_key,
                            resolver_name=resolver_name,
                        )
                    ),
                }
            provider_row = ProviderInventoryRow(
                provider=provider,
                resolver_name=resolver_name,
                models=agentic_models,
                authenticated=bool(row.get("authenticated", False)),
                live_attempt_status=str(
                    discovery.get("live_attempt_status") or "not_attempted"
                ),
                model_provenance=model_provenance,
                provenance_details=provenance_details,
                auth_identity=auth_identity,
                credential_pool_identity=pool_identity,
                endpoint_identity=endpoint_identity,
                credential_fingerprint=credential_fingerprint,
                api_mode=api_mode,
                capabilities=provider_capabilities,
                economics=economics,
                cooldown_until=(
                    str(row.get("cooldown_until"))
                    if row.get("cooldown_until")
                    else None
                ),
                observed_at=observed_at,
                source=str(discovery.get("source") or row.get("source") or "hermes"),
            )
            provider_rows.append(provider_row)
            for model in agentic_models:
                stable_id = _projection_key(
                    provider=provider,
                    model=model,
                    auth_identity=auth_identity,
                    credential_pool_identity=pool_identity,
                    endpoint_identity=endpoint_identity,
                    api_mode=api_mode,
                    local_backend="",
                )
                self._projections[stable_id] = _Projection(
                    resolver_name=resolver_name,
                    economics=economics[model],
                )
                if row.get("is_current") is True and model == inherited_model:
                    inherited_runtime_keys.append(
                        RuntimeKey(
                            provider=provider,
                            model=model,
                            auth_identity=auth_identity,
                            credential_pool_identity=pool_identity,
                            endpoint_identity=endpoint_identity,
                            api_mode=api_mode,
                            local_backend="",
                            inventory_revision="adapter-projection",
                        )
                    )
        self._inherited_runtime_keys = tuple(inherited_runtime_keys)
        return AdapterInventory(
            provider_rows=tuple(provider_rows),
            local_rows=tuple(local_rows),
        )

    def resolve_inherited_baseline(
        self,
        inventory_revision: str,
    ) -> ResolvedRuntime | None:
        """Resolve the one exact current picker access path, or fail closed."""
        if len(self._inherited_runtime_keys) != 1:
            return None
        runtime_key = self._inherited_runtime_keys[0].model_copy(
            update={"inventory_revision": inventory_revision}
        )
        return self.resolve(runtime_key)

    def identify_persisted_inherited_runtime(
        self,
        observations: tuple[Any, ...],
        hermes_config: Mapping[str, Any],
    ) -> RuntimeKey | None:
        """Identify the configured baseline from persisted facts without I/O."""
        raw_model = hermes_config.get("model", {})
        if isinstance(raw_model, Mapping):
            model = str(
                raw_model.get("default")
                or raw_model.get("model")
                or raw_model.get("name")
                or ""
            ).strip()
            provider = str(raw_model.get("provider") or "").strip().casefold()
            if provider == "auto":
                provider = ""
        else:
            model = str(raw_model or "").strip()
            provider = ""
        if not model:
            return None
        matches = tuple(
            observation
            for observation in observations
            if getattr(observation, "state", None) == "verified"
            and observation.key.model == model
            and (
                not provider
                or observation.key.provider.casefold() == provider
            )
        )
        return matches[0].key if len(matches) == 1 else None

    def prepare_persisted_inventory(
        self,
        observations: tuple[Any, ...],
        hermes_config: Mapping[str, Any],
    ) -> None:
        """Restore local projection indexes from a verified durable snapshot."""
        self._projections.clear()
        for observation in observations:
            key = observation.key
            descriptor = self._validated_persisted_projection_descriptor(
                observation
            )
            self._projections[key.stable_id()] = _Projection(
                resolver_name=str(descriptor["resolver_name"]),
                economics=observation.economics,
                local_backend=key.local_backend,
            )
        inherited = self.identify_persisted_inherited_runtime(
            observations,
            hermes_config,
        )
        self._inherited_runtime_keys = () if inherited is None else (inherited,)

    def resolve(self, runtime_key: RuntimeKey) -> ResolvedRuntime:
        projection = self._projections.get(runtime_key.stable_id())
        if projection is None:
            unresolved = runtime_key.model_copy(
                update={"auth_identity": "unresolved:adapter"}
            )
            raise RuntimeResolutionMismatch(runtime_key, unresolved)
        runtime = resolve_runtime_provider(
            requested=projection.resolver_name,
            target_model=runtime_key.model,
        )
        runtime_mapping = dict(_as_mapping(runtime))
        if projection.local_backend:
            runtime_mapping.setdefault("auth_identity", runtime_key.auth_identity)
            runtime_mapping.setdefault(
                "credential_pool_identity",
                runtime_key.credential_pool_identity,
            )
            runtime_mapping.setdefault(
                "endpoint_identity",
                runtime_key.endpoint_identity,
            )
            runtime_mapping.setdefault("local_backend", runtime_key.local_backend)
        canonical = _runtime_identity(
            runtime_key.provider,
            projection.resolver_name,
            runtime_mapping,
            local_backend=projection.local_backend,
            inventory_revision=runtime_key.inventory_revision,
            model=runtime_key.model,
        )
        ensure_runtime_match(runtime_key, canonical)
        secret_keys = {
            "api_key",
            "base_url",
            "credential_pool",
        }
        extra = {
            key: value
            for key, value in runtime_mapping.items()
            if key not in secret_keys
        }
        extra["auto_routing_economics"] = projection.economics
        return ResolvedRuntime(
            runtime_key=canonical,
            resolver_name=projection.resolver_name,
            provider=str(runtime_mapping.get("provider") or runtime_key.provider),
            api_mode=str(runtime_mapping.get("api_mode") or runtime_key.api_mode),
            source=str(runtime_mapping.get("source") or "runtime-resolver"),
            base_url=str(runtime_mapping.get("base_url") or ""),
            api_key=runtime_mapping.get("api_key"),
            credential_pool=runtime_mapping.get("credential_pool"),
            extra=extra,
            credential_fingerprint_key=self._credential_fingerprint_key,
        )

    def capability_report(self) -> dict[str, Any]:
        """Return a stable report for the exact constructor projection seam."""
        import inspect

        from run_agent import AIAgent

        parameters = inspect.signature(AIAgent.__init__).parameters
        required = {
            "model",
            "provider",
            "base_url",
            "api_key",
            "api_mode",
            "credential_pool",
            "reasoning_config",
            "fallback_model",
            "runtime_routing_context",
            "prepared_agent_runtime",
        }
        compatible = required.issubset(parameters)
        return {
            "contract": "hermes-agent-runtime-projection-v1",
            "fresh_session": compatible,
            "delegation": compatible,
            "pre_call_fallback": compatible,
            "exact_credential_pool": compatible,
            "reasoning_projection": compatible,
            "post_call_model_failover": False,
        }

    def inspect_persisted_projection(
        self,
        observation: RuntimeObservation,
        *,
        reasoning_effort: str,
        hermes_config: Mapping[str, Any] | None = None,
    ) -> PersistedRuntimeProjection:
        """Describe constructor inputs without provider, auth, or network I/O."""
        descriptor = self._validated_persisted_projection_descriptor(observation)
        runtime_key = observation.key
        reasoning = resolve_reasoning_config(
            dict(hermes_config or {}),
            runtime_key.model,
            requested_effort=reasoning_effort,
        )
        return PersistedRuntimeProjection(
            contract=PERSISTED_RUNTIME_PROJECTION_CONTRACT,
            runtime_key=runtime_key,
            resolution_state="resolved",
            model=runtime_key.model,
            provider=runtime_key.provider,
            api_mode=runtime_key.api_mode,
            credential_pool_identity=runtime_key.credential_pool_identity,
            resolver_name=str(descriptor["resolver_name"]),
            access_kind=str(descriptor["access_kind"]),
            reasoning_effort=effective_generic_reasoning_effort(reasoning),
            fallback_owner="auto-routing-pre-call",
            fallback_count=0,
        )

    def to_agent_runtime_spec(
        self,
        resolved_runtime: ResolvedRuntime,
        *,
        reasoning_effort: str,
        hermes_config: Mapping[str, Any] | None = None,
    ) -> AgentRuntimeSpec:
        """Convert one exact opaque adapter binding into constructor inputs."""
        if not isinstance(resolved_runtime, ResolvedRuntime):
            raise TypeError("resolved runtime is required")
        report = self.capability_report()
        if not all(
            report[name]
            for name in (
                "fresh_session",
                "delegation",
                "pre_call_fallback",
                "exact_credential_pool",
                "reasoning_projection",
            )
        ):
            raise RuntimeError("Hermes runtime projection contract is incompatible")
        return self._project_agent_runtime_spec(
            resolved_runtime,
            reasoning_effort=reasoning_effort,
            hermes_config=hermes_config,
        )

    def to_recorded_agent_runtime_spec(
        self,
        resolved_runtime: ResolvedRuntime,
        *,
        reasoning_effort: str,
    ) -> AgentRuntimeSpec:
        """Project a resolved recorded binding without current capability drift."""
        if not isinstance(resolved_runtime, ResolvedRuntime):
            raise TypeError("resolved runtime is required")
        return self._project_agent_runtime_spec(
            resolved_runtime,
            reasoning_effort=reasoning_effort,
            hermes_config=None,
        )

    @staticmethod
    def _project_agent_runtime_spec(
        resolved_runtime: ResolvedRuntime,
        *,
        reasoning_effort: str,
        hermes_config: Mapping[str, Any] | None,
    ) -> AgentRuntimeSpec:
        execution = _resolved_runtime_execution(resolved_runtime)
        reasoning = resolve_reasoning_config(
            dict(hermes_config or {}),
            resolved_runtime.runtime_key.model,
            requested_effort=reasoning_effort,
        )
        acp_command = execution.extra.get("acp_command")
        acp_args = execution.extra.get("acp_args") or ()
        return AgentRuntimeSpec(
            model=resolved_runtime.runtime_key.model,
            provider=resolved_runtime.provider,
            base_url=execution.base_url,
            api_key=execution.api_key,
            resolution_state="resolved",
            api_mode=resolved_runtime.api_mode,
            acp_command=(None if acp_command is None else str(acp_command)),
            acp_args=tuple(str(item) for item in acp_args),
            credential_pool=execution.credential_pool,
            reasoning_config=reasoning,
            fallback_model=(),
        )

    def verify_access(
        self,
        resolved_runtime: ResolvedRuntime,
        request: VerificationRequest,
    ) -> AccessVerification:
        if not isinstance(resolved_runtime, ResolvedRuntime):
            raise TypeError("verification requires an already-resolved runtime")
        runtime_key = resolved_runtime.runtime_key
        capability = resolved_runtime.probe_capability
        if capability is None:
            raise ValueError(
                resolved_runtime.probe_unavailable_reason
                or "verification_execution_shape_unsupported"
            )
        if (
            request.prompt != _FIXED_PROMPT
            or request.maximum_input_tokens != capability.maximum_input_tokens
            or request.maximum_output_tokens != _FIXED_MAXIMUM_OUTPUT_TOKENS
            or request.temperature != 0
            or request.executor_id != capability.executor_id
            or request.executor_version != capability.executor_version
            or request.execution_shape_fingerprint
            != capability.execution_shape_fingerprint
            or request.tools
            or request.persist
            or runtime_key.local_backend
        ):
            raise ValueError("verification request is not the fixed bounded probe")
        execution = _resolved_runtime_execution(resolved_runtime)
        economics = execution.extra.get("auto_routing_economics")
        if not isinstance(economics, AccessEconomics):
            raise RuntimeError("resolved runtime economics binding is unavailable")
        try:
            response = call_llm_exact_once(
                provider=resolved_runtime.provider,
                model=runtime_key.model,
                base_url=execution.base_url,
                api_key=execution.api_key,
                api_mode=resolved_runtime.api_mode,
                messages=[{"role": "user", "content": request.prompt}],
                temperature=0,
                max_tokens=request.maximum_output_tokens,
                tools=[],
                main_runtime={
                    "provider": resolved_runtime.provider,
                    "model": runtime_key.model,
                    "base_url": execution.base_url,
                    "api_key": execution.api_key,
                    "api_mode": resolved_runtime.api_mode,
                    "credential_pool": execution.credential_pool,
                },
                capability=capability,
            )
        except OneShotTransportError as error:
            raise VerificationOutcomeUncertain(
                "verification_request_outcome_uncertain"
            ) from error
        try:
            content = self._response_content(response)
            reported_model = str(_get(response, "model", "") or "")
            canonical_model = self._canonical_response_model(
                runtime_key.provider,
                reported_model,
                runtime_key.model,
            )
            usage = _get(response, "usage", {})
            input_tokens = _get(
                usage,
                "prompt_tokens",
                _get(usage, "input_tokens", None),
            )
            output_tokens = _get(
                usage,
                "completion_tokens",
                _get(usage, "output_tokens", None),
            )
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in (input_tokens, output_tokens)
            ):
                raise ValueError("verification response usage is unavailable")
            actual_cost = self._actual_cost(
                economics,
                input_tokens,
                output_tokens,
            )
            return AccessVerification(
                runtime_key=resolved_runtime.runtime_key,
                sentinel=content.strip(),
                response_model=canonical_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                actual_cost_usd=actual_cost,
                response_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        except VerificationOutcomeUncertain:
            raise
        except Exception as error:
            # The physical call already returned.  Any translation, usage,
            # hashing, or cost failure leaves the bill unknowable and must
            # retain the full reservation.
            raise VerificationOutcomeUncertain(
                "verification_response_usage_uncertain"
            ) from error

    def _canonical_response_model(
        self,
        provider: str,
        reported: str,
        requested: str,
    ) -> str:
        if not reported:
            return ""
        if reported.lower() == requested.lower():
            return requested
        return self._model_aliases.get(
            (provider.lower(), reported.lower()),
            reported,
        )

    @staticmethod
    def _response_content(response: Any) -> str:
        choices = _get(response, "choices", ()) or ()
        if choices:
            message = _get(choices[0], "message", {})
            content = _get(message, "content", "")
            if isinstance(content, str):
                return content
        output_text = _get(response, "output_text", "")
        if isinstance(output_text, str):
            return output_text
        raise ValueError("verification response text is missing")

    @staticmethod
    def _actual_cost(
        economics: AccessEconomics,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        if economics.billing_kind == "metered":
            input_price = economics.metered_input_usd_per_million_tokens
            output_price = economics.metered_output_usd_per_million_tokens
            if input_price is None or output_price is None:
                raise ValueError("verification pricing is unavailable")
            return float(
                (input_tokens * input_price + output_tokens * output_price)
                / 1_000_000
            )
        if economics.billing_kind == "subscription":
            marginal = economics.effective_marginal_cost_usd_per_task
            if marginal is None:
                raise ValueError("verification marginal cost is unavailable")
            return float(marginal)
        raise ValueError("local runtimes cannot use access verification")


# The version module's public implementation name matches the neutral protocol
# name used in the task interface; callers may also use the explicit class name.
HermesAdapter = Hermes018Adapter


__all__ = ["Hermes018Adapter", "HermesAdapter"]
