"""Version-neutral, non-secret Hermes runtime adapter contracts."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlparse

from agent.auxiliary_client import (
    ExactOneShotCapability,
    build_exact_one_shot_capability,
)

from ..models import AccessEconomics, RuntimeKey, RuntimeObservation
from ..profile_key import read_profile_credential_fingerprint_key_if_present


class RuntimeResolutionMismatch(RuntimeError):
    """Canonical runtime resolution did not reproduce the requested key."""

    def __init__(self, requested: RuntimeKey, resolved: RuntimeKey) -> None:
        self.requested = requested
        self.resolved = resolved
        super().__init__(
            "resolved runtime does not match requested access path: "
            f"{requested.stable_id()} != {resolved.stable_id()}"
        )


class VerificationOutcomeUncertain(RuntimeError):
    """The one-shot request may have been billed but lacks reliable usage."""


def ensure_runtime_match(requested: RuntimeKey, resolved: RuntimeKey) -> None:
    if requested != resolved:
        raise RuntimeResolutionMismatch(requested, resolved)


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            frozen[str(key)] = _freeze_mapping(item)
        elif isinstance(item, list):
            frozen[str(key)] = tuple(item)
        else:
            frozen[str(key)] = item
    return MappingProxyType(frozen)


@dataclass(frozen=True)
class ProviderInventoryRow:
    """One configured provider/access path with per-model evidence."""

    provider: str
    resolver_name: str
    models: tuple[str, ...]
    authenticated: bool
    live_attempt_status: str
    model_provenance: Mapping[str, str | None]
    provenance_details: Mapping[str, Mapping[str, Any]]
    auth_identity: str
    credential_pool_identity: str
    endpoint_identity: str
    credential_fingerprint: str
    api_mode: str
    capabilities: Mapping[str, Mapping[str, Any]]
    economics: Mapping[str, AccessEconomics]
    observed_at: str
    cooldown_until: str | None = None
    source: str = "hermes"

    def __post_init__(self) -> None:
        if not self.provider or not self.resolver_name:
            raise ValueError("provider inventory requires an addressable resolver")
        if set(self.model_provenance) != set(self.models):
            raise ValueError("provider inventory requires per-model provenance")
        if set(self.provenance_details) != set(self.models):
            raise ValueError("provider inventory requires per-model details")
        object.__setattr__(
            self,
            "model_provenance",
            MappingProxyType(dict(self.model_provenance)),
        )
        object.__setattr__(
            self,
            "provenance_details",
            _freeze_mapping(self.provenance_details),
        )
        object.__setattr__(self, "capabilities", _freeze_mapping(self.capabilities))
        object.__setattr__(
            self,
            "economics",
            MappingProxyType(dict(self.economics)),
        )


@dataclass(frozen=True)
class LocalInventoryRow:
    """Exact installed-local model and conservative hardware evidence."""

    provider: str
    resolver_name: str
    model: str
    backend_identity: str
    reachable: bool
    installed: bool
    open_weights: bool
    license_id: str | None
    model_size_bytes: int | None
    available_ram_bytes: int | None
    available_vram_bytes: int | None
    loaded_healthy: bool
    hardware_compatible: bool | None
    api_mode: str
    capabilities: Mapping[str, Any]
    economics: AccessEconomics
    observed_at: str

    def __post_init__(self) -> None:
        if not self.backend_identity:
            raise ValueError("local inventory requires a backend identity")
        object.__setattr__(self, "capabilities", _freeze_mapping(self.capabilities))


@dataclass(frozen=True)
class AdapterInventory:
    provider_rows: tuple[ProviderInventoryRow, ...]
    local_rows: tuple[LocalInventoryRow, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedRuntimeExecution:
    base_url: str
    api_key: Any
    credential_pool: Any
    extra: Mapping[str, Any]


_RESOLVED_EXECUTION: weakref.WeakKeyDictionary[
    "ResolvedRuntime",
    _ResolvedRuntimeExecution,
] = weakref.WeakKeyDictionary()
_RESOLVED_EXECUTION_LOCK = threading.RLock()
_CREDENTIAL_SELECTION_FINGERPRINT_KEY = secrets.token_bytes(32)
_CREDENTIAL_SELECTION_FINGERPRINT_DOMAIN = (
    b"hermes:auto-routing:credential-selection:v1\x00"
)

_VERIFICATION_PROMPT = "Return exactly AUTO_ROUTING_ACCESS_OK"
_VERIFICATION_MAXIMUM_OUTPUT_TOKENS = 16
_SUPPORTED_VERIFICATION_API_MODES = frozenset(
    {"chat_completions", "codex_responses", "anthropic_messages"}
)
_EXECUTION_METADATA_KEYS = frozenset(
    {
        "provider",
        "api_mode",
        "source",
        "requested_provider",
        "auth_identity",
        "credential_pool_identity",
        "endpoint_identity",
        "local_backend",
        "max_output_tokens",
        "auto_routing_economics",
    }
)


def _has_execution_value(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (str, bytes, tuple, list, set, Mapping)):
        return bool(value)
    return True


def _credential_selection_fingerprint(
    runtime_key: RuntimeKey,
    credential_material: str,
    *,
    fingerprint_key: bytes | None = None,
) -> str:
    selected_key = fingerprint_key
    if selected_key is None:
        selected_key = (
            read_profile_credential_fingerprint_key_if_present()
            or _CREDENTIAL_SELECTION_FINGERPRINT_KEY
        )
    if len(selected_key) != 32:
        raise ValueError("credential fingerprint key must contain 32 bytes")
    encoded_credential = credential_material.encode("utf-8", errors="replace")
    identity = "\x00".join(
        (
            runtime_key.provider,
            runtime_key.auth_identity,
            runtime_key.credential_pool_identity,
        )
    ).encode("utf-8", errors="replace")
    digest = hmac.new(
        selected_key,
        _CREDENTIAL_SELECTION_FINGERPRINT_DOMAIN
        + identity
        + b"\x00"
        + encoded_credential,
        hashlib.sha256,
    ).hexdigest()
    return f"credential-selection:v1:{digest}"


class _FrozenCredentialProvider:
    """Callable credential provider pinned to one already-resolved token."""

    __slots__ = ("_credential",)

    def __init__(self, credential: str) -> None:
        self._credential = credential

    def __call__(self) -> str:
        return self._credential

    def __repr__(self) -> str:
        return "<frozen credential provider>"


def _resolve_credential_selection(
    api_key: Any,
) -> tuple[Any, str, str | None]:
    if callable(api_key) and not isinstance(api_key, str):
        try:
            selected = api_key()
        except Exception:
            return None, "unavailable", "verification_credential_unavailable"
        if not isinstance(selected, str) or not selected.strip():
            return None, "unavailable", "verification_credential_unavailable"
        return _FrozenCredentialProvider(selected), selected, None
    if not isinstance(api_key, str) or not api_key.strip():
        return None, "unavailable", "verification_credential_unavailable"
    return api_key, api_key, None


def _endpoint_is_executable(base_url: str) -> bool:
    endpoint = str(base_url or "").strip()
    if not endpoint:
        return False
    try:
        parsed = urlparse(endpoint)
        hostname = parsed.hostname
        parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and bool(hostname)
        and not any(character.isspace() for character in str(hostname))
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _probe_capability(
    *,
    provider: str,
    model: str,
    api_mode: str,
    base_url: str,
    extra: Mapping[str, Any],
    credential_unavailable_reason: str | None,
) -> tuple[ExactOneShotCapability | None, str | None]:
    normalized_provider = str(provider or "").strip().lower()
    normalized_mode = str(api_mode or "").strip().lower()
    if normalized_mode not in _SUPPORTED_VERIFICATION_API_MODES:
        return None, "verification_api_mode_unsupported"
    if not _endpoint_is_executable(base_url):
        return None, "verification_endpoint_unavailable"
    if credential_unavailable_reason is not None:
        return None, credential_unavailable_reason
    if (
        normalized_provider == "bedrock"
        or "copilot" in normalized_provider
        or normalized_provider == "acp"
        or normalized_provider.endswith("-acp")
    ):
        return None, "verification_execution_shape_unsupported"
    try:
        if urlparse(str(base_url or "")).query:
            return None, "verification_execution_shape_unsupported"
    except ValueError:
        return None, "verification_execution_shape_unsupported"
    unsupported_keys = {
        str(key)
        for key, value in extra.items()
        if key not in _EXECUTION_METADATA_KEYS and _has_execution_value(value)
    }
    configured_maximum = extra.get("max_output_tokens")
    if configured_maximum is not None and (
        isinstance(configured_maximum, bool)
        or not isinstance(configured_maximum, int)
        or configured_maximum < _VERIFICATION_MAXIMUM_OUTPUT_TOKENS
    ):
        unsupported_keys.add("max_output_tokens")
    if unsupported_keys:
        return None, "verification_execution_shape_unsupported"
    try:
        capability = build_exact_one_shot_capability(
            model=model,
            api_mode=normalized_mode,
            messages=[{"role": "user", "content": _VERIFICATION_PROMPT}],
            temperature=0,
            max_tokens=_VERIFICATION_MAXIMUM_OUTPUT_TOKENS,
            tools=[],
            execution_shape={
                "provider": normalized_provider,
                "api_mode": normalized_mode,
                "runtime_max_output_tokens": _VERIFICATION_MAXIMUM_OUTPUT_TOKENS,
            },
        )
    except (TypeError, ValueError):
        return None, "verification_execution_shape_unsupported"
    return capability, None


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, init=False)
class ResolvedRuntime:
    """Exact public runtime identity bound to opaque process-local execution."""

    runtime_key: RuntimeKey
    resolver_name: str
    provider: str
    api_mode: str
    source: str
    credential_selection_fingerprint: str
    probe_capability: ExactOneShotCapability | None
    probe_unavailable_reason: str | None

    def __init__(
        self,
        *,
        runtime_key: RuntimeKey,
        resolver_name: str,
        provider: str,
        api_mode: str,
        source: str,
        base_url: str,
        api_key: Any,
        credential_pool: Any = None,
        extra: Mapping[str, Any] | None = None,
        credential_fingerprint_key: bytes | None = None,
    ) -> None:
        object.__setattr__(self, "runtime_key", runtime_key)
        object.__setattr__(self, "resolver_name", resolver_name)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "api_mode", api_mode)
        object.__setattr__(self, "source", source)
        (
            execution_api_key,
            credential_material,
            credential_unavailable_reason,
        ) = _resolve_credential_selection(api_key)
        object.__setattr__(
            self,
            "credential_selection_fingerprint",
            _credential_selection_fingerprint(
                runtime_key,
                credential_material,
                fingerprint_key=credential_fingerprint_key,
            ),
        )
        frozen_extra = _freeze_mapping(extra or {})
        capability, unavailable_reason = _probe_capability(
            provider=provider,
            model=runtime_key.model,
            api_mode=api_mode,
            base_url=base_url,
            extra=frozen_extra,
            credential_unavailable_reason=credential_unavailable_reason,
        )
        object.__setattr__(self, "probe_capability", capability)
        object.__setattr__(self, "probe_unavailable_reason", unavailable_reason)
        execution = _ResolvedRuntimeExecution(
            base_url=str(base_url or ""),
            api_key=execution_api_key,
            credential_pool=credential_pool,
            extra=frozen_extra,
        )
        with _RESOLVED_EXECUTION_LOCK:
            _RESOLVED_EXECUTION[self] = execution

    def public_record(self) -> dict[str, Any]:
        record = {
            "runtime_key": self.runtime_key.model_dump(),
            "resolver_name": self.resolver_name,
            "provider": self.provider,
            "api_mode": self.api_mode,
            "source": self.source,
            "credential_selection_fingerprint": (
                self.credential_selection_fingerprint
            ),
        }
        if self.probe_capability is not None:
            record["probe_capability"] = self.probe_capability.public_record()
        elif self.probe_unavailable_reason:
            record["probe_unavailable_reason"] = self.probe_unavailable_reason
        return record


def _resolved_runtime_execution(
    runtime: ResolvedRuntime,
) -> _ResolvedRuntimeExecution:
    with _RESOLVED_EXECUTION_LOCK:
        execution = _RESOLVED_EXECUTION.get(runtime)
    if execution is None:
        raise RuntimeError("resolved runtime execution binding is unavailable")
    return execution


@dataclass(frozen=True)
class VerificationRequest:
    prompt: str
    maximum_input_tokens: int
    maximum_output_tokens: int
    temperature: int
    executor_id: str
    executor_version: str
    execution_shape_fingerprint: str
    tools: tuple[Any, ...] = ()
    persist: bool = False


@dataclass(frozen=True)
class AccessVerification:
    runtime_key: RuntimeKey
    sentinel: str
    response_model: str
    input_tokens: int
    output_tokens: int
    actual_cost_usd: float
    response_hash: str

    def __post_init__(self) -> None:
        usage = (self.input_tokens, self.output_tokens)
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in usage
        ):
            raise ValueError("verification usage must be finite non-negative integers")
        if not math.isfinite(self.actual_cost_usd) or self.actual_cost_usd < 0:
            raise ValueError("verification cost must be finite and non-negative")
        if not self.response_hash:
            raise ValueError("verification response hash must not be empty")


PERSISTED_RUNTIME_PROJECTION_CONTRACT = (
    "auto-routing-persisted-runtime-projection-v1"
)


@dataclass(frozen=True, slots=True)
class PersistedRuntimeProjection:
    """Redacted, side-effect-free prediction of one constructor projection.

    Adapters build this descriptor only from a persisted :class:`RuntimeKey`
    and in-memory configuration.  It deliberately cannot carry endpoints,
    tokens, credential objects, or any other value that would require live
    provider/auth resolution.
    """

    contract: str
    runtime_key: RuntimeKey
    resolution_state: str
    model: str
    provider: str
    api_mode: str
    credential_pool_identity: str
    resolver_name: str
    access_kind: str
    reasoning_effort: str | None
    fallback_owner: str
    fallback_count: int


class HermesAdapter(Protocol):
    def inventory(self, refresh: bool = False) -> AdapterInventory: ...

    def resolve(self, runtime_key: RuntimeKey) -> ResolvedRuntime: ...

    def to_agent_runtime_spec(
        self,
        resolved_runtime: ResolvedRuntime,
        *,
        reasoning_effort: str,
        hermes_config: Mapping[str, Any] | None = None,
    ) -> Any: ...

    def to_recorded_agent_runtime_spec(
        self,
        resolved_runtime: ResolvedRuntime,
        *,
        reasoning_effort: str,
    ) -> Any: ...

    def capability_report(self) -> Mapping[str, Any]: ...

    def inspect_persisted_projection(
        self,
        observation: RuntimeObservation,
        *,
        reasoning_effort: str,
        hermes_config: Mapping[str, Any] | None = None,
    ) -> PersistedRuntimeProjection: ...

    def verify_access(
        self,
        resolved_runtime: ResolvedRuntime,
        request: VerificationRequest,
    ) -> AccessVerification: ...


__all__ = [
    "AccessVerification",
    "AdapterInventory",
    "HermesAdapter",
    "LocalInventoryRow",
    "PERSISTED_RUNTIME_PROJECTION_CONTRACT",
    "PersistedRuntimeProjection",
    "ProviderInventoryRow",
    "ResolvedRuntime",
    "RuntimeResolutionMismatch",
    "VerificationOutcomeUncertain",
    "VerificationRequest",
    "ensure_runtime_match",
]
