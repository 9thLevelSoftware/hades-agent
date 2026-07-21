"""Generic, cache-safe runtime routing contracts for agent construction.

The core owns this narrow protocol and the process-authenticated handoff.  It
does not know about any concrete routing policy.  A plugin may register the
single resolver through :mod:`hermes_cli.plugins`; callers that do not supply a
runtime context retain the ordinary Hermes construction path unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Protocol, TypeAlias
from urllib.parse import urlsplit, urlunsplit

from hermes_constants import get_hermes_home


logger = logging.getLogger(__name__)

RUNTIME_ROUTING_CONTRACT_VERSION = 1

RuntimePlanReasonCode: TypeAlias = Literal[
    "",
    "resolver_absent",
    "routing_off",
    "scope_disabled",
    "manual_runtime_pin",
    "fixed_delegation_runtime",
    "shadow_recorded",
    "active_projected",
    "activation_receipt_missing",
    "safe_default",
    "activation_receipt_invalid",
    "adapter_incompatible",
    "authority_invalid",
    "classifier_failed",
    "no_eligible_runtime",
    "baseline_inherit",
    "plugin_state_unavailable",
    "recorded_state_invalid",
    "recorded_route_unavailable",
    "resume_binding_missing",
    "operation_pending",
    "resolver_error",
    "resolver_contract_invalid",
]

_PLAN_REASON_CODES = frozenset(
    {
        "",
        "resolver_absent",
        "routing_off",
        "scope_disabled",
        "manual_runtime_pin",
        "fixed_delegation_runtime",
        "shadow_recorded",
        "active_projected",
        "activation_receipt_missing",
        "safe_default",
        "activation_receipt_invalid",
        "adapter_incompatible",
        "authority_invalid",
        "classifier_failed",
        "no_eligible_runtime",
        "baseline_inherit",
        "plugin_state_unavailable",
        "recorded_state_invalid",
        "recorded_route_unavailable",
        "resume_binding_missing",
        "operation_pending",
        "resolver_error",
        "resolver_contract_invalid",
    }
)
_RESOLUTION_STATES = frozenset({"requested", "resolved", "failed"})
_RESOLUTION_REASON_CODES = frozenset(
    {
        "",
        "unresolved",
        "credential_missing",
        "endpoint_unavailable",
        "unsupported_route",
        "policy_rejected",
        "resolution_failed",
    }
)
_SCOPES = frozenset({"fresh_session", "delegation"})
_ACTIONS = frozenset({"inherit", "shadow", "project", "defer"})
_BINDING_ACTIONS = frozenset({"inherit", "shadow", "project"})
_PROCESS_KEY = secrets.token_bytes(32)
_PREPARED_FACTORY_TOKEN = object()
_PUBLIC_KEY_DENY = re.compile(
    r"(?:^|_)(?:auth|authorization|body|content|credential|file|key|password|"
    r"path|payload|prompt|secret|task|text|token|url)(?:$|_)",
    re.IGNORECASE,
)
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_PUBLIC_KEY_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")
_PUBLIC_SECRET_KEY_PARTS = frozenset(
    {
        "apikey",
        "auth",
        "authentication",
        "authorization",
        "body",
        "clientsecret",
        "content",
        "credential",
        "file",
        "filename",
        "filepath",
        "key",
        "location",
        "password",
        "passwd",
        "path",
        "payload",
        "privatekey",
        "prompt",
        "secret",
        "task",
        "text",
        "token",
        "url",
    }
)
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_PUBLIC_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@+\-]*$")
_PUBLIC_FACT_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_MAX_PUBLIC_NUMBER = (2**53) - 1
_SESSION_RUNTIME_METADATA_KEYS = frozenset(
    {
        "api_mode",
        "base_url",
        "model",
        "provider",
        "reasoning_config",
        "runtime_manual_pin_source",
    }
)
_RUNTIME_API_MODES = frozenset(
    {
        "anthropic_messages",
        "bedrock_converse",
        "chat_completions",
        "codex_app_server",
        "codex_responses",
    }
)

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | Mapping[str, "JsonValue"] | tuple["JsonValue", ...]


class InvalidPreparedAgentRuntime(RuntimeError):
    """Raised when a sealed runtime handoff is missing, stale, or substituted."""

    def __init__(self) -> None:
        super().__init__("Invalid prepared runtime handoff")


class RuntimeRoutingDeferred(RuntimeError):
    """Typed construction outcome while another live owner decides the route."""

    def __init__(self, *, retry_after_seconds: float | None = None) -> None:
        self.reason_code = "operation_pending"
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Agent runtime routing deferred: operation_pending")


class RuntimeContinuationError(RuntimeError):
    """Raised when a durable compression lineage is cyclic or unbounded."""


def _freeze(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        raise ValueError("Runtime mapping exceeds the maximum nesting depth")
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item, depth=depth + 1) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, depth=depth + 1) for item in value)
    if isinstance(value, (str, int, float, bool, bytes, type(None))):
        return value
    return value


def _validate_public_identifier(
    value: Any,
    *,
    label: str,
    allow_empty: bool = True,
    allow_slash: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid public runtime {label}")
    if not allow_empty and not value:
        raise ValueError(f"Invalid public runtime {label}")
    if len(value) > 256 or not _PUBLIC_IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid public runtime {label}")
    if (
        "://" in value
        or value.lower().startswith("data:")
        or _public_string_is_path(value, allow_slash=allow_slash)
        or _public_string_looks_secret(value)
    ):
        raise ValueError(f"Invalid public runtime {label}")
    return value


def _normalized_public_key(value: str) -> tuple[str, str]:
    snake = _CAMEL_CASE_BOUNDARY.sub("_", value)
    snake = _PUBLIC_KEY_SEPARATORS.sub("_", snake).strip("_").lower()
    return snake, snake.replace("_", "")


def _public_key_is_sensitive(value: str) -> bool:
    snake, compact = _normalized_public_key(value)
    if not snake:
        return True
    if _PUBLIC_KEY_DENY.search(snake):
        return True
    parts = tuple(part for part in snake.split("_") if part)
    return (
        compact in _PUBLIC_SECRET_KEY_PARTS
        or any(part in _PUBLIC_SECRET_KEY_PARTS for part in parts)
    )


def _public_string_is_path(value: str, *, allow_slash: bool = False) -> bool:
    normalized = value.strip()
    try:
        posix = PurePosixPath(normalized)
        windows = PureWindowsPath(normalized)
        has_dot_segment = normalized in {".", ".."} or any(
            part in {".", ".."} for part in (*posix.parts, *windows.parts)
        )
        has_drive = bool(windows.drive)
        is_absolute = posix.is_absolute() or windows.is_absolute()
    except Exception:
        return True
    return bool(
        _WINDOWS_PATH.match(normalized)
        or has_drive
        or has_dot_segment
        or is_absolute
        or normalized.startswith(("/", "\\", "./", "../", "~/"))
        or "/../" in normalized
        or "/./" in normalized
        or ("/" in normalized and not allow_slash)
        or "\\" in normalized
    )


def _public_string_looks_secret(value: str) -> bool:
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(value, force=True) != value
    except Exception:
        # A failed safety detector is not permission to publish an opaque,
        # token-shaped value. Known prefixes cover the high-confidence forms
        # required at this boundary without echoing the candidate.
        lowered = value.lower()
        return lowered.startswith(("ghp_", "github_pat_", "sk-", "sk_", "eyj"))


def _validate_public_value_inner(value: Any, *, label: str, depth: int) -> Any:
    if depth > 6:
        raise ValueError(f"Invalid public runtime {label}")
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise ValueError(f"Invalid public runtime {label}")
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not _PUBLIC_FACT_KEY.fullmatch(key)
                or _public_string_looks_secret(key)
            ):
                raise ValueError(f"Invalid public runtime {label}")
            if _public_key_is_sensitive(key):
                raise ValueError(f"Invalid public runtime {label}")
            frozen[key] = _validate_public_value_inner(
                item, label=label, depth=depth + 1
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        if len(value) > 128:
            raise ValueError(f"Invalid public runtime {label}")
        return tuple(
            _validate_public_value_inner(item, label=label, depth=depth + 1)
            for item in value
        )
    if isinstance(value, bytes):
        raise ValueError(f"Invalid public runtime {label}")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_PUBLIC_NUMBER:
            raise ValueError(f"Invalid public runtime {label}")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > _MAX_PUBLIC_NUMBER:
            raise ValueError(f"Invalid public runtime {label}")
        return value
    if isinstance(value, str):
        if len(value) > 256 or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"Invalid public runtime {label}")
        if (
            "://" in value
            or value.lower().startswith("data:")
            or _public_string_is_path(value)
            or _public_string_looks_secret(value)
        ):
            raise ValueError(f"Invalid public runtime {label}")
        if not _PUBLIC_IDENTIFIER.fullmatch(value):
            raise ValueError(f"Invalid public runtime {label}")
        return value
    raise ValueError(f"Invalid public runtime {label}")


def _validate_public_value(value: Any, *, label: str, depth: int = 0) -> Any:
    """Freeze bounded content-free facts without ever echoing a rejected value."""
    try:
        return _validate_public_value_inner(value, label=label, depth=depth)
    except Exception:
        raise ValueError(f"Invalid public runtime {label}") from None


def _thaw_public(value: Any) -> Any:
    """Convert frozen public facts back to ordinary JSON-compatible values."""
    if isinstance(value, Mapping):
        return {str(key): _thaw_public(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_public(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class AgentRuntimeSpec:
    model: str
    provider: str
    base_url: str = field(default="", repr=False)
    api_key: Any = field(default=None, repr=False)
    resolution_state: Literal["requested", "resolved", "failed"] = "requested"
    resolution_reason_code: Literal[
        "",
        "unresolved",
        "credential_missing",
        "endpoint_unavailable",
        "unsupported_route",
        "policy_rejected",
        "resolution_failed",
    ] = ""
    api_mode: str = ""
    acp_command: str | None = None
    acp_args: tuple[str, ...] = field(default=(), repr=False)
    credential_pool: Any = field(default=None, repr=False)
    reasoning_config: Mapping[str, Any] | None = field(default=None, repr=False)
    fallback_model: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not isinstance(self.provider, str):
            raise ValueError("Invalid runtime model/provider")
        if not isinstance(self.base_url, str):
            raise ValueError("Invalid runtime base URL")
        if self.resolution_state not in _RESOLUTION_STATES:
            raise ValueError("Invalid runtime resolution state")
        if self.resolution_reason_code not in _RESOLUTION_REASON_CODES:
            raise ValueError("Invalid runtime resolution reason code")
        if self.resolution_state == "resolved" and self.resolution_reason_code:
            raise ValueError("A resolved runtime cannot have a failure reason")
        if not isinstance(self.api_mode, str):
            raise ValueError("Invalid runtime API mode")
        if self.acp_command is not None and not isinstance(self.acp_command, str):
            raise ValueError("Invalid runtime ACP command")
        object.__setattr__(self, "acp_args", tuple(str(item) for item in self.acp_args))
        if self.reasoning_config is not None:
            if not isinstance(self.reasoning_config, Mapping):
                raise ValueError("Invalid runtime reasoning config")
            object.__setattr__(self, "reasoning_config", _freeze(self.reasoning_config))
        raw_fallbacks = self.fallback_model or ()
        if isinstance(raw_fallbacks, Mapping):
            raw_fallbacks = (raw_fallbacks,)
        if not isinstance(raw_fallbacks, (list, tuple)):
            raise ValueError("Invalid runtime fallback chain")
        frozen_fallbacks = []
        for fallback in raw_fallbacks:
            if not isinstance(fallback, Mapping):
                raise ValueError("Invalid runtime fallback chain")
            frozen_fallbacks.append(_freeze(fallback))
        object.__setattr__(self, "fallback_model", tuple(frozen_fallbacks))

    def public_record(self) -> dict[str, Any]:
        """Return stable execution identity without opaque credentials."""
        _validate_public_identifier(
            self.model, label="model", allow_empty=False, allow_slash=True
        )
        _validate_public_identifier(
            self.provider, label="provider", allow_empty=False, allow_slash=True
        )
        if self.api_mode:
            _validate_public_identifier(self.api_mode, label="API mode")
        return {
            "model": self.model,
            "provider": self.provider,
            "api_mode": self.api_mode,
            "resolution_state": self.resolution_state,
            "resolution_reason_code": self.resolution_reason_code,
        }


@dataclass(frozen=True, slots=True)
class AgentRuntimeContext:
    scope: Literal["fresh_session", "delegation"]
    task: Any = field(repr=False)
    session_id: str
    task_id: str
    operation_id: str | None = None
    task_index: int | None = None
    is_resume: bool = False
    manual_runtime_pin: bool = False
    manual_pin_source: str | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.scope not in _SCOPES:
            raise ValueError("Invalid runtime routing scope")
        _validate_public_identifier(self.session_id, label="session id", allow_empty=False)
        _validate_public_identifier(self.task_id, label="task id", allow_empty=False)
        if self.operation_id is not None:
            _validate_public_identifier(
                self.operation_id, label="operation id", allow_empty=False
            )
        if self.task_index is not None and (
            not isinstance(self.task_index, int) or isinstance(self.task_index, bool) or self.task_index < 0
        ):
            raise ValueError("Invalid public runtime task index")
        if type(self.is_resume) is not bool or type(self.manual_runtime_pin) is not bool:
            raise ValueError("Invalid runtime context flags")
        if self.manual_pin_source is not None:
            _validate_public_identifier(
                self.manual_pin_source, label="manual pin source", allow_empty=False
            )
        if not isinstance(self.metadata, Mapping):
            raise ValueError("Invalid public runtime metadata")
        object.__setattr__(
            self,
            "metadata",
            _validate_public_value(self.metadata, label="metadata"),
        )


@dataclass(frozen=True, slots=True)
class AgentRuntimeRequest:
    contract_version: int
    context: AgentRuntimeContext
    baseline: AgentRuntimeSpec

    def __post_init__(self) -> None:
        if not isinstance(self.context, AgentRuntimeContext):
            raise ValueError("Invalid runtime context")
        if not isinstance(self.baseline, AgentRuntimeSpec):
            raise ValueError("Invalid baseline runtime")


@dataclass(frozen=True, slots=True)
class AgentRuntimePlan:
    action: Literal["inherit", "shadow", "project", "defer"]
    runtime: AgentRuntimeSpec = field(repr=False)
    decision_id: str | None = None
    bound_route_identity: str | None = None
    owns_fallbacks: bool = False
    reason_code: RuntimePlanReasonCode = field(default="", repr=False)
    retry_after_seconds: float | None = None
    event: Mapping[str, JsonValue] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # Resolver-owned input is validated and frozen by ``_validate_plan``.
        # Do not iterate it here: a hostile Mapping must become a finite
        # contract rejection inside prepare_agent_runtime, never escape while
        # the plugin is constructing its return value.
        pass


@dataclass(frozen=True, slots=True)
class AgentRuntimePreparationKey:
    contract_version: int
    profile_home_hash: str
    scope: Literal["fresh_session", "delegation"]
    session_id: str
    task_id: str
    operation_id: str | None
    task_index: int | None
    is_resume: bool
    manual_runtime_pin: bool
    manual_pin_source: str | None
    requested_baseline_fingerprint: str
    task_hmac: str = field(repr=False)
    metadata_hmac: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedAgentRuntime:
    prepared_for: AgentRuntimePreparationKey
    plan: AgentRuntimePlan = field(repr=False)
    effective_runtime_fingerprint: str
    seal: bytes = field(repr=False)
    requested_baseline: AgentRuntimeSpec = field(repr=False, compare=False)
    effective_runtime: AgentRuntimeSpec | None = field(
        default=None, repr=False, compare=False
    )
    _factory_token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_token is not _PREPARED_FACTORY_TOKEN:
            raise InvalidPreparedAgentRuntime()

    def public_record(self) -> dict[str, Any]:
        return public_runtime_binding(self)


@dataclass(frozen=True, slots=True)
class ManualRuntimePinRequest:
    session_id: str
    source: str
    runtime: AgentRuntimeSpec = field(repr=False)

    def __post_init__(self) -> None:
        _validate_public_identifier(self.session_id, label="session id", allow_empty=False)
        _validate_public_identifier(self.source, label="manual pin source", allow_empty=False)


@dataclass(frozen=True, slots=True)
class RuntimeSessionContinuation:
    parent_session_id: str
    child_session_id: str
    reason: Literal["compression"] = "compression"

    def __post_init__(self) -> None:
        _validate_public_identifier(
            self.parent_session_id, label="parent session id", allow_empty=False
        )
        _validate_public_identifier(
            self.child_session_id, label="child session id", allow_empty=False
        )
        if self.reason != "compression":
            raise ValueError("Invalid runtime continuation reason")


@dataclass(frozen=True, slots=True)
class RuntimeRoutingBinding:
    scope: Literal["fresh_session", "delegation"]
    session_id: str
    task_id: str
    operation_id: str | None
    action: Literal["inherit", "shadow", "project"]
    runtime: AgentRuntimeSpec = field(repr=False)
    decision_id: str | None = None
    bound_route_identity: str | None = None
    owns_fallbacks: bool = False
    reason_code: RuntimePlanReasonCode = field(default="", repr=False)
    manual_pin_source: str | None = None
    event: Mapping[str, JsonValue] = field(default_factory=dict, repr=False)

    def public_record(self) -> dict[str, Any]:
        if self.scope not in _SCOPES:
            raise ValueError("Invalid public runtime scope")
        if self.action not in _BINDING_ACTIONS:
            raise ValueError("Invalid public runtime action")
        if type(self.owns_fallbacks) is not bool:
            raise ValueError("Invalid public runtime fallback ownership")
        if not _reason_matches_binding_action(self.action, self.reason_code):
            raise ValueError("Invalid public runtime action reason")
        if not runtime_spec_has_exact_execution_binding(self.runtime):
            raise ValueError("Invalid public runtime execution binding")
        _validate_public_identifier(
            self.session_id, label="session id", allow_empty=False
        )
        _validate_public_identifier(self.task_id, label="task id", allow_empty=False)
        if self.operation_id is not None:
            _validate_public_identifier(
                self.operation_id, label="operation id", allow_empty=False
            )
        if self.decision_id is not None:
            _validate_public_identifier(
                self.decision_id, label="decision id", allow_empty=False
            )
        if self.bound_route_identity is not None:
            _validate_public_identifier(
                self.bound_route_identity,
                label="bound route identity",
                allow_empty=False,
                allow_slash=True,
            )
        if self.manual_pin_source is not None:
            _validate_public_identifier(
                self.manual_pin_source,
                label="manual pin source",
                allow_empty=False,
            )
        if self.reason_code not in _PLAN_REASON_CODES:
            raise ValueError("Invalid public runtime reason code")
        if not isinstance(self.event, Mapping):
            raise ValueError("Invalid public runtime event")
        event = _validate_public_value(self.event, label="event")
        return {
            "scope": self.scope,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "operation_id": self.operation_id,
            "action": self.action,
            **self.runtime.public_record(),
            "decision_id": self.decision_id,
            "bound_route_identity": self.bound_route_identity,
            "owns_fallbacks": self.owns_fallbacks,
            "reason_code": self.reason_code,
            "manual_pin_source": self.manual_pin_source,
            "event": _thaw_public(event),
        }


class AgentRuntimeResolver(Protocol):
    def requires_initial_task(
        self, scope: Literal["fresh_session", "delegation"]
    ) -> bool: ...

    def resolve(self, request: AgentRuntimeRequest) -> AgentRuntimePlan: ...

    def record_manual_pin(self, request: ManualRuntimePinRequest) -> None: ...

    def record_session_continuation(
        self, request: RuntimeSessionContinuation
    ) -> None: ...

    def close(self) -> None: ...


def _update_digest(digest: hmac.HMAC, value: Any) -> None:
    if value is None:
        digest.update(b"N;")
    elif isinstance(value, bool):
        digest.update(b"B1;" if value else b"B0;")
    elif isinstance(value, int):
        digest.update(b"I" + str(value).encode("ascii") + b";")
    elif isinstance(value, float):
        digest.update(b"F" + value.hex().encode("ascii") + b";")
    elif isinstance(value, str):
        encoded = value.encode("utf-8", errors="surrogatepass")
        digest.update(b"S" + str(len(encoded)).encode("ascii") + b":" + encoded + b";")
    elif isinstance(value, bytes):
        digest.update(b"Y" + str(len(value)).encode("ascii") + b":" + value + b";")
    elif isinstance(value, Mapping):
        digest.update(b"M{")
        for key in sorted(
            value,
            key=lambda item: _fingerprint(item, purpose=b"mapping-key-order"),
        ):
            _update_digest(digest, key)
            _update_digest(digest, value[key])
        digest.update(b"};")
    elif isinstance(value, (list, tuple)):
        digest.update(b"L[")
        for item in value:
            _update_digest(digest, item)
        digest.update(b"];")
    else:
        identity = f"{type(value).__module__}.{type(value).__qualname__}:{id(value)}"
        _update_digest(digest, identity)


def _fingerprint(value: Any, *, purpose: bytes) -> str:
    digest = hmac.new(_PROCESS_KEY, digestmod=hashlib.sha256)
    digest.update(purpose + b"\x00")
    _update_digest(digest, value)
    return digest.hexdigest()


def _runtime_fingerprint(spec: AgentRuntimeSpec, *, purpose: bytes) -> str:
    if spec.credential_pool is None:
        pool_identity: Any = None
    else:
        pool_identity = (
            "opaque-pool-identity",
            type(spec.credential_pool).__module__,
            type(spec.credential_pool).__qualname__,
            id(spec.credential_pool),
        )
    return _fingerprint(
        (
            spec.model,
            spec.provider,
            spec.base_url,
            spec.api_key,
            spec.resolution_state,
            spec.resolution_reason_code,
            spec.api_mode,
            spec.acp_command,
            spec.acp_args,
            pool_identity,
            spec.reasoning_config,
            spec.fallback_model,
        ),
        purpose=purpose,
    )


def _profile_home_hash() -> str:
    path = os.path.normcase(str(get_hermes_home().resolve()))
    return hashlib.sha256(path.encode("utf-8", errors="surrogatepass")).hexdigest()


def _preparation_key(request: AgentRuntimeRequest) -> AgentRuntimePreparationKey:
    context = request.context
    return AgentRuntimePreparationKey(
        contract_version=request.contract_version,
        profile_home_hash=_profile_home_hash(),
        scope=context.scope,
        session_id=context.session_id,
        task_id=context.task_id,
        operation_id=context.operation_id,
        task_index=context.task_index,
        is_resume=context.is_resume,
        manual_runtime_pin=context.manual_runtime_pin,
        manual_pin_source=context.manual_pin_source,
        requested_baseline_fingerprint=_runtime_fingerprint(
            request.baseline, purpose=b"requested-baseline"
        ),
        task_hmac=_fingerprint(context.task, purpose=b"routing-task"),
        metadata_hmac=_fingerprint(context.metadata, purpose=b"routing-metadata"),
    )


def _seal_payload(
    prepared_for: AgentRuntimePreparationKey,
    plan: AgentRuntimePlan,
    effective_runtime_fingerprint: str,
) -> tuple[Any, ...]:
    return (
        prepared_for.contract_version,
        prepared_for.profile_home_hash,
        prepared_for.scope,
        prepared_for.session_id,
        prepared_for.task_id,
        prepared_for.operation_id,
        prepared_for.task_index,
        prepared_for.is_resume,
        prepared_for.manual_runtime_pin,
        prepared_for.manual_pin_source,
        prepared_for.requested_baseline_fingerprint,
        prepared_for.task_hmac,
        prepared_for.metadata_hmac,
        plan.action,
        plan.decision_id,
        plan.bound_route_identity,
        plan.owns_fallbacks,
        plan.reason_code,
        plan.retry_after_seconds,
        _runtime_fingerprint(plan.runtime, purpose=b"selected-runtime"),
        plan.event,
        effective_runtime_fingerprint,
    )


def _make_seal(
    prepared_for: AgentRuntimePreparationKey,
    plan: AgentRuntimePlan,
    effective_runtime_fingerprint: str,
) -> bytes:
    digest = hmac.new(_PROCESS_KEY, digestmod=hashlib.sha256)
    digest.update(b"prepared-agent-runtime-v1\x00")
    _update_digest(
        digest, _seal_payload(prepared_for, plan, effective_runtime_fingerprint)
    )
    return digest.digest()


def _valid_seal(prepared: PreparedAgentRuntime) -> bool:
    try:
        if prepared._factory_token is not _PREPARED_FACTORY_TOKEN:
            return False
        expected = _make_seal(
            prepared.prepared_for,
            prepared.plan,
            prepared.effective_runtime_fingerprint,
        )
        return hmac.compare_digest(expected, prepared.seal)
    except Exception:
        return False


@contextmanager
def _resolver_lease():
    from hermes_cli.plugins import lease_agent_runtime_resolver

    with lease_agent_runtime_resolver() as resolver:
        yield resolver


def _fail_open_plan(
    baseline: AgentRuntimeSpec, reason_code: RuntimePlanReasonCode
) -> AgentRuntimePlan:
    return AgentRuntimePlan(
        action="inherit",
        runtime=baseline,
        owns_fallbacks=False,
        reason_code=reason_code,
    )


def _warn_finite(reason_code: str) -> None:
    logger.warning("Agent runtime resolver failed open: %s", reason_code)


def _credential_is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bytes):
        return bool(value)
    return True


def _canonicalize_fallback_entry(
    entry: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return the exact provider/model form later fallback code consumes."""
    try:
        raw_provider = entry.get("provider")
        raw_model = entry.get("model")
        if not isinstance(raw_provider, str) or not isinstance(raw_model, str):
            return None
        requested_provider = raw_provider.strip().lower()
        requested_model = raw_model.strip()
        if not requested_provider or requested_provider == "auto" or not requested_model:
            return None
        from hermes_cli.model_normalize import normalize_model_for_provider
        from hermes_cli.models import normalize_provider

        provider = normalize_provider(requested_provider)
        if not provider or provider == "auto":
            return None
        model = normalize_model_for_provider(requested_model, provider)
        if not model:
            return None
        canonical = dict(entry)
        canonical["provider"] = provider
        canonical["model"] = model
        return canonical
    except Exception:
        return None


def _canonical_live_fallbacks(
    fallbacks: tuple[Mapping[str, Any], ...],
) -> tuple[Mapping[str, Any], ...]:
    canonical = []
    for entry in fallbacks:
        normalized = _canonicalize_fallback_entry(entry)
        if normalized is not None:
            canonical.append(normalized)
    return tuple(canonical)


def _runtime_endpoint_is_canonical(spec: AgentRuntimeSpec) -> bool:
    try:
        from urllib.parse import parse_qsl, urlparse

        raw = spec.base_url
        if (
            not raw
            or raw != raw.strip()
            or any(character.isspace() for character in raw)
        ):
            return False
        parsed = urlparse(raw)
        if (
            parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        # Accessing ``port`` also rejects malformed/non-numeric ports.
        _ = parsed.port
        if parsed.scheme in {"http", "https"}:
            if not parsed.hostname or not raw.startswith(f"{parsed.scheme}://"):
                return False
            if raw.endswith("?"):
                return False
            if parsed.query:
                pairs = parse_qsl(
                    parsed.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
                keys = [key for key, _value in pairs]
                if (
                    not pairs
                    or any(not key or not value for key, value in pairs)
                    or len(set(keys)) != len(keys)
                ):
                    return False
            return True
        if parsed.scheme == "moa":
            return spec.provider == "moa" and raw == "moa://local"
        if parsed.scheme == "acp":
            return spec.provider == "copilot-acp" and raw == "acp://copilot"
        if parsed.scheme == "acp+tcp":
            return bool(
                spec.provider == "copilot-acp"
                and parsed.hostname
                and parsed.port is not None
                and parsed.path in {"", "/"}
                and not parsed.query
            )
    except Exception:
        return False
    return False


def _reason_matches_binding_action(action: str, reason_code: str) -> bool:
    if action == "shadow":
        return reason_code == "shadow_recorded"
    if action == "project":
        return reason_code == "active_projected"
    if action == "inherit":
        return reason_code not in {
            "active_projected",
            "operation_pending",
            "shadow_recorded",
        }
    return False


def _runtime_spec_has_canonical_identity(spec: AgentRuntimeSpec) -> bool:
    """Whether fields later consumed by ``AIAgent`` are already canonical."""
    provider = spec.provider.strip().lower()
    model = spec.model.strip()
    if (
        not provider
        or provider == "auto"
        or provider != spec.provider
        or not model
        or model != spec.model
        or spec.api_mode not in _RUNTIME_API_MODES
    ):
        return False
    try:
        from hermes_cli.model_normalize import normalize_model_for_provider
        from hermes_cli.models import normalize_provider

        if normalize_provider(provider) != provider:
            return False
        if normalize_model_for_provider(model, provider) != model:
            return False
    except Exception:
        return False
    canonical_fallbacks = _canonical_live_fallbacks(spec.fallback_model)
    if len(canonical_fallbacks) != len(spec.fallback_model):
        return False
    try:
        for original, canonical in zip(spec.fallback_model, canonical_fallbacks):
            if (
                original.get("provider") != canonical.get("provider")
                or original.get("model") != canonical.get("model")
            ):
                return False
    except Exception:
        return False
    if not _runtime_endpoint_is_canonical(spec):
        return False
    if spec.api_mode == "bedrock_converse" and provider != "bedrock":
        return False
    if spec.api_mode == "codex_app_server" and provider not in {
        "openai",
        "openai-codex",
    }:
        return False
    if provider == "bedrock":
        try:
            from urllib.parse import urlparse

            from agent.bedrock_adapter import is_anthropic_bedrock_model

            parsed = urlparse(spec.base_url)
            hostname = (parsed.hostname or "").lower()
            expected_mode = (
                "anthropic_messages"
                if is_anthropic_bedrock_model(model)
                else "bedrock_converse"
            )
            return bool(
                spec.api_key == "aws-sdk"
                and spec.api_mode == expected_mode
                and parsed.scheme == "https"
                and re.fullmatch(
                    r"bedrock-runtime\.[a-z0-9-]+\.amazonaws\.com",
                    hostname,
                )
                and parsed.path in {"", "/"}
                and not parsed.params
                and not parsed.query
                and not parsed.fragment
                and parsed.username is None
                and parsed.password is None
                and parsed.port is None
            )
        except Exception:
            return False
    if provider == "moa":
        return (
            spec.api_mode == "chat_completions"
            and spec.base_url == "moa://local"
            and spec.api_key == "moa-virtual-provider"
        )
    if provider == "copilot-acp":
        if spec.api_mode != "chat_completions":
            return False
        if (
            not isinstance(spec.acp_command, str)
            or not spec.acp_command.strip()
            or spec.acp_command != spec.acp_command.strip()
            or not spec.acp_args
            or any(not arg.strip() or arg != arg.strip() for arg in spec.acp_args)
        ):
            # CopilotACPClient otherwise derives or changes command/args after
            # the authenticated handoff.
            return False
    if provider == "minimax-oauth" and not callable(spec.api_key):
        return False
    return True


def runtime_spec_has_exact_execution_binding(spec: AgentRuntimeSpec) -> bool:
    """Whether *spec* can execute without provider/global route selection."""
    if (
        spec.resolution_state != "resolved"
        or spec.resolution_reason_code
        or not _runtime_spec_has_canonical_identity(spec)
    ):
        return False
    return bool(spec.base_url and _credential_is_present(spec.api_key))


def _validate_plan(
    value: Any, baseline: AgentRuntimeSpec
) -> AgentRuntimePlan | None:
    if not isinstance(value, AgentRuntimePlan):
        return None
    if value.action not in _ACTIONS or not isinstance(value.runtime, AgentRuntimeSpec):
        return None
    if not isinstance(value.reason_code, str) or value.reason_code not in _PLAN_REASON_CODES:
        return None
    if value.action != "defer" and not _reason_matches_binding_action(
        value.action, value.reason_code
    ):
        return None
    if type(value.owns_fallbacks) is not bool:
        return None
    try:
        if not isinstance(value.event, Mapping):
            return None
        if value.decision_id is not None:
            _validate_public_identifier(
                value.decision_id, label="decision id", allow_empty=False
            )
        if value.bound_route_identity is not None:
            _validate_public_identifier(
                value.bound_route_identity,
                label="bound route identity",
                allow_empty=False,
                allow_slash=True,
            )
        event = _validate_public_value(value.event, label="event")
    except Exception:
        return None

    if value.retry_after_seconds is not None:
        if (
            isinstance(value.retry_after_seconds, bool)
            or not isinstance(value.retry_after_seconds, (int, float))
            or not math.isfinite(float(value.retry_after_seconds))
            or value.retry_after_seconds <= 0
        ):
            return None

    baseline_fp = _runtime_fingerprint(baseline, purpose=b"plan-baseline")
    runtime_fp = _runtime_fingerprint(value.runtime, purpose=b"plan-baseline")
    if value.action in {"inherit", "shadow", "defer"} and runtime_fp != baseline_fp:
        return None
    if value.action == "project":
        if not runtime_spec_has_exact_execution_binding(value.runtime):
            return None
    if value.action == "defer":
        if value.reason_code != "operation_pending" or value.owns_fallbacks:
            return None
    elif value.retry_after_seconds is not None:
        return None

    return replace(value, event=event)


def _new_prepared(
    request: AgentRuntimeRequest,
    plan: AgentRuntimePlan,
    *,
    effective_runtime: AgentRuntimeSpec | None = None,
) -> PreparedAgentRuntime:
    prepared_for = _preparation_key(request)
    effective_fingerprint = (
        _runtime_fingerprint(effective_runtime, purpose=b"effective-runtime")
        if effective_runtime is not None
        else ""
    )
    return PreparedAgentRuntime(
        prepared_for=prepared_for,
        plan=plan,
        effective_runtime_fingerprint=effective_fingerprint,
        seal=_make_seal(prepared_for, plan, effective_fingerprint),
        requested_baseline=request.baseline,
        effective_runtime=effective_runtime,
        _factory_token=_PREPARED_FACTORY_TOKEN,
    )


def prepare_agent_runtime(request: AgentRuntimeRequest) -> PreparedAgentRuntime:
    """Invoke the single resolver and return an authenticated, unfinalized plan."""
    if not isinstance(request, AgentRuntimeRequest):
        raise TypeError("request must be an AgentRuntimeRequest")

    if request.contract_version != RUNTIME_ROUTING_CONTRACT_VERSION:
        plan = _fail_open_plan(request.baseline, "resolver_contract_invalid")
        _warn_finite("resolver_contract_invalid")
        return _new_prepared(request, plan)

    try:
        with _resolver_lease() as resolver:
            if resolver is None:
                return _new_prepared(
                    request, _fail_open_plan(request.baseline, "resolver_absent")
                )
            raw_plan = resolver.resolve(request)
    except Exception:
        _warn_finite("resolver_error")
        return _new_prepared(
            request, _fail_open_plan(request.baseline, "resolver_error")
        )

    plan = _validate_plan(raw_plan, request.baseline)
    if plan is None:
        _warn_finite("resolver_contract_invalid")
        return _new_prepared(
            request,
            _fail_open_plan(request.baseline, "resolver_contract_invalid"),
        )
    if plan.action == "defer":
        raise RuntimeRoutingDeferred(
            retry_after_seconds=float(plan.retry_after_seconds)
            if plan.retry_after_seconds is not None
            else None
        )
    return _new_prepared(request, plan)


def prepare_agent_runtime_for_construction(
    request: AgentRuntimeRequest,
    *,
    session_store: Any = None,
) -> PreparedAgentRuntime:
    """Repair a durable resume lineage immediately before policy resolution."""
    if not isinstance(request, AgentRuntimeRequest):
        raise TypeError("request must be an AgentRuntimeRequest")
    if request.context.is_resume and session_store is not None:
        repair_runtime_session_continuations_from_store(
            request.context.session_id,
            session_store=session_store,
        )
    return prepare_agent_runtime(request)


def finalize_prepared_agent_runtime(
    prepared: PreparedAgentRuntime,
    request: AgentRuntimeRequest,
    effective_runtime: AgentRuntimeSpec,
) -> PreparedAgentRuntime:
    """Bind an unfinalized preparation to the exact executable constructor spec."""
    if (
        not isinstance(prepared, PreparedAgentRuntime)
        or not isinstance(request, AgentRuntimeRequest)
        or not isinstance(effective_runtime, AgentRuntimeSpec)
        or prepared.effective_runtime_fingerprint
        or prepared.effective_runtime is not None
        or not _valid_seal(prepared)
    ):
        raise InvalidPreparedAgentRuntime()
    if prepared.prepared_for != _preparation_key(request):
        raise InvalidPreparedAgentRuntime()
    if (
        _runtime_fingerprint(prepared.requested_baseline, purpose=b"requested-baseline")
        != _runtime_fingerprint(request.baseline, purpose=b"requested-baseline")
    ):
        raise InvalidPreparedAgentRuntime()
    if prepared.plan.action == "defer":
        raise InvalidPreparedAgentRuntime()
    if not runtime_spec_has_exact_execution_binding(effective_runtime):
        raise InvalidPreparedAgentRuntime()
    if (
        prepared.plan.action in {"inherit", "shadow"}
        and prepared.plan.owns_fallbacks
        and effective_runtime.fallback_model
    ):
        raise InvalidPreparedAgentRuntime()
    if prepared.plan.action == "project":
        if (
            _runtime_fingerprint(effective_runtime, purpose=b"selected-runtime")
            != _runtime_fingerprint(prepared.plan.runtime, purpose=b"selected-runtime")
        ):
            raise InvalidPreparedAgentRuntime()
    return _new_prepared(request, prepared.plan, effective_runtime=effective_runtime)


def _binding_from_prepared(prepared: PreparedAgentRuntime) -> RuntimeRoutingBinding:
    context = prepared.prepared_for
    runtime = prepared.effective_runtime or prepared.plan.runtime
    return RuntimeRoutingBinding(
        scope=context.scope,
        session_id=context.session_id,
        task_id=context.task_id,
        operation_id=context.operation_id,
        action=prepared.plan.action,  # type: ignore[arg-type]
        runtime=runtime,
        decision_id=prepared.plan.decision_id,
        bound_route_identity=prepared.plan.bound_route_identity,
        owns_fallbacks=prepared.plan.owns_fallbacks,
        reason_code=prepared.plan.reason_code,
        event=prepared.plan.event,
    )


def validate_prepared_agent_runtime(
    prepared: PreparedAgentRuntime,
    *,
    context: AgentRuntimeContext,
    effective_runtime: AgentRuntimeSpec,
) -> RuntimeRoutingBinding:
    """Validate and consume a finalized wrapper before any client is created."""
    if (
        not isinstance(prepared, PreparedAgentRuntime)
        or not prepared.effective_runtime_fingerprint
        or prepared.effective_runtime is None
        or not _valid_seal(prepared)
    ):
        raise InvalidPreparedAgentRuntime()
    request = AgentRuntimeRequest(
        contract_version=prepared.prepared_for.contract_version,
        context=context,
        baseline=prepared.requested_baseline,
    )
    if prepared.prepared_for != _preparation_key(request):
        raise InvalidPreparedAgentRuntime()
    expected_effective = _runtime_fingerprint(
        effective_runtime, purpose=b"effective-runtime"
    )
    if not hmac.compare_digest(
        prepared.effective_runtime_fingerprint, expected_effective
    ):
        raise InvalidPreparedAgentRuntime()
    if (
        _runtime_fingerprint(prepared.effective_runtime, purpose=b"effective-runtime")
        != expected_effective
    ):
        raise InvalidPreparedAgentRuntime()
    return _binding_from_prepared(prepared)


def _fallback_tuple(value: Any) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value if isinstance(item, Mapping))
    return ()


def constructor_runtime_spec(
    *,
    model: str,
    provider: str | None,
    base_url: str | None,
    api_key: Any,
    api_mode: str | None,
    acp_command: str | None,
    acp_args: list[str] | tuple[str, ...] | None,
    credential_pool: Any,
    reasoning_config: Mapping[str, Any] | None,
    fallback_model: Any,
) -> AgentRuntimeSpec:
    """Build the opaque spec for the exact current constructor arguments."""
    provider_value = provider.strip().lower() if isinstance(provider, str) else ""
    executable = bool(
        model
        and provider_value
        and (
            (base_url and api_key)
            or credential_pool is not None
            or acp_command
            or provider_value in {"bedrock", "moa"}
        )
    )
    return AgentRuntimeSpec(
        model=model or "",
        provider=provider_value,
        base_url=base_url or "",
        api_key=api_key,
        resolution_state="resolved" if executable else "requested",
        resolution_reason_code="" if executable else "unresolved",
        api_mode=api_mode or "",
        acp_command=acp_command,
        acp_args=tuple(acp_args or ()),
        credential_pool=credential_pool,
        reasoning_config=reasoning_config,
        fallback_model=_fallback_tuple(fallback_model),
    )


def _runtime_from_provider_record(
    baseline: AgentRuntimeSpec,
    record: Mapping[str, Any],
    *,
    model: str | None = None,
    fallback_model: tuple[Mapping[str, Any], ...] | None = None,
    preserve_baseline_pool: bool = True,
    preserve_baseline_api_mode: bool = True,
) -> AgentRuntimeSpec:
    provider = str(record.get("provider") or baseline.provider or "").strip().lower()
    record_model = str(record.get("model") or "").strip()
    resolved_model = model or baseline.model or record_model
    if not resolved_model and provider:
        try:
            from hermes_cli.models import get_default_model_for_provider

            resolved_model = get_default_model_for_provider(provider) or ""
        except Exception:
            resolved_model = ""
    try:
        from hermes_cli.model_normalize import normalize_model_for_provider
        from hermes_cli.models import normalize_provider

        provider = normalize_provider(provider)
        resolved_model = normalize_model_for_provider(resolved_model, provider)
    except Exception:
        pass
    if "credential_pool" in record:
        credential_pool = record.get("credential_pool")
    elif preserve_baseline_pool:
        credential_pool = baseline.credential_pool
    else:
        credential_pool = None
    resolved_api_key = (
        record.get("api_key") if "api_key" in record else baseline.api_key
    )
    if provider == "minimax-oauth" and isinstance(resolved_api_key, str):
        from hermes_cli.auth import build_minimax_oauth_token_provider

        resolved_api_key = build_minimax_oauth_token_provider()
    record_api_mode = str(record.get("api_mode") or "").strip()
    resolved_api_mode = (
        baseline.api_mode or record_api_mode
        if preserve_baseline_api_mode
        else record_api_mode
    )
    spec = AgentRuntimeSpec(
        model=resolved_model,
        provider=provider,
        base_url=str(record.get("base_url") or baseline.base_url or "").strip(),
        api_key=resolved_api_key,
        resolution_state="resolved",
        api_mode=resolved_api_mode,
        acp_command=(
            str(record.get("command"))
            if record.get("command") is not None
            else baseline.acp_command
        ),
        acp_args=tuple(record.get("args") or baseline.acp_args or ()),
        credential_pool=credential_pool,
        reasoning_config=baseline.reasoning_config,
        fallback_model=(
            baseline.fallback_model if fallback_model is None else fallback_model
        ),
    )
    if not runtime_spec_has_exact_execution_binding(spec):
        raise RuntimeError("Resolved Hermes runtime is not executable")
    return spec


@dataclass(frozen=True, slots=True)
class _OrdinaryHermesRuntimeResolution:
    runtime: AgentRuntimeSpec
    activated_fallback_index: int | None = None


def resolve_ordinary_hermes_runtime(
    baseline: AgentRuntimeSpec,
    *,
    owns_fallbacks: bool,
) -> _OrdinaryHermesRuntimeResolution:
    """Resolve the requested Hermes primary without constructing a client.

    Policy-owned fallback plans may resolve only a concrete requested primary;
    they never enter Hermes's global ``auto`` selection or scan the host
    fallback chain. Ordinary inherit/shadow plans retain the existing fallback
    permission, but the selected result is sealed before agent attributes or a
    provider client consume it.
    """
    canonical_fallbacks = _canonical_live_fallbacks(baseline.fallback_model)
    normalized_fallbacks = () if owns_fallbacks else canonical_fallbacks
    provider = baseline.provider.strip().lower()
    has_explicit_base_url = bool(baseline.base_url)

    if provider in {"", "auto"} and owns_fallbacks and not has_explicit_base_url:
        raise RuntimeError(
            "Policy-owned routing requires an explicit primary runtime"
        )

    requested = provider or None
    if provider in {"", "auto"} and has_explicit_base_url:
        requested = "custom"

    from hermes_cli.runtime_provider import resolve_runtime_provider

    try:
        record = resolve_runtime_provider(
            requested=requested,
            explicit_api_key=baseline.api_key,
            explicit_base_url=baseline.base_url or None,
            target_model=baseline.model,
        )
        return _OrdinaryHermesRuntimeResolution(
            runtime=_runtime_from_provider_record(
                baseline,
                record,
                fallback_model=normalized_fallbacks,
            )
        )
    except Exception:
        if owns_fallbacks:
            raise RuntimeError("Unable to resolve explicit primary runtime") from None

    # Match ordinary Hermes fallback permission only when policy does not own
    # fallbacks. Each entry is resolved explicitly; never pass it as ``auto``.
    for fallback_index, fallback in enumerate(canonical_fallbacks):
        try:
            fallback_provider = str(fallback.get("provider") or "").strip().lower()
            fallback_model = str(fallback.get("model") or "").strip()
            if not fallback_provider or fallback_provider == "auto" or not fallback_model:
                continue
            explicit_key = fallback.get("api_key")
            if not _credential_is_present(explicit_key):
                key_env = str(
                    fallback.get("key_env") or fallback.get("api_key_env") or ""
                ).strip()
                if key_env:
                    try:
                        from agent.secret_scope import get_secret

                        explicit_key = get_secret(key_env, "")
                    except Exception:
                        explicit_key = None
            record = resolve_runtime_provider(
                requested=fallback_provider,
                explicit_api_key=explicit_key,
                explicit_base_url=fallback.get("base_url"),
                target_model=fallback_model,
            )
            return _OrdinaryHermesRuntimeResolution(
                runtime=_runtime_from_provider_record(
                    baseline,
                    record,
                    model=fallback_model,
                    fallback_model=canonical_fallbacks,
                    preserve_baseline_pool=False,
                    preserve_baseline_api_mode=False,
                ),
                activated_fallback_index=fallback_index,
            )
        except Exception:
            continue
    raise RuntimeError("Unable to resolve requested Hermes runtime") from None


def prepare_constructor_runtime(
    *,
    context: AgentRuntimeContext | None,
    prepared: PreparedAgentRuntime | None,
    effective_constructor_runtime: AgentRuntimeSpec,
    session_store: Any = None,
) -> tuple[PreparedAgentRuntime | None, RuntimeRoutingBinding | None, AgentRuntimeSpec]:
    """Resolve/validate construction without mutating any caller arguments."""
    if context is None:
        if prepared is not None:
            raise InvalidPreparedAgentRuntime()
        return None, None, effective_constructor_runtime

    if prepared is None:
        request = AgentRuntimeRequest(
            contract_version=RUNTIME_ROUTING_CONTRACT_VERSION,
            context=context,
            baseline=effective_constructor_runtime,
        )
        prepared = prepare_agent_runtime_for_construction(
            request,
            session_store=session_store,
        )
        if prepared.plan.action != "project":
            # Inherit/shadow are a two-phase handoff. The caller must run the
            # ordinary Hermes runtime resolver, then finalize this exact
            # preparation before assigning runtime attributes or making a
            # provider client.
            return prepared, None, effective_constructor_runtime
        effective = prepared.plan.runtime
        prepared = finalize_prepared_agent_runtime(prepared, request, effective)
    else:
        effective = effective_constructor_runtime

    binding = validate_prepared_agent_runtime(
        prepared, context=context, effective_runtime=effective
    )
    return prepared, binding, effective


def apply_runtime_plan_to_constructor_arguments(
    arguments: Mapping[str, Any],
    *,
    binding: RuntimeRoutingBinding | None,
    effective_runtime: AgentRuntimeSpec,
) -> dict[str, Any]:
    """Return fresh constructor locals with a validated plan projected.

    The input mapping is never mutated.  Opaque credentials remain in process
    memory only; callers must not log or serialize the returned dictionary.
    """
    projected = dict(arguments)
    if binding is not None:
        projected.update(
            {
                "model": effective_runtime.model,
                "provider": effective_runtime.provider,
                "base_url": effective_runtime.base_url,
                "api_key": effective_runtime.api_key,
                "api_mode": effective_runtime.api_mode or None,
                "acp_command": effective_runtime.acp_command,
                "acp_args": list(effective_runtime.acp_args),
                "command": None,
                "args": None,
                "credential_pool": effective_runtime.credential_pool,
                "reasoning_config": (
                    dict(effective_runtime.reasoning_config)
                    if effective_runtime.reasoning_config is not None
                    else None
                ),
                "fallback_model": [
                    dict(item) for item in effective_runtime.fallback_model
                ],
            }
        )
    return projected


def public_runtime_binding(value: Any) -> dict[str, Any]:
    """Return a stable, credential-free cache/status record."""
    try:
        if isinstance(value, PreparedAgentRuntime):
            if (
                not value.effective_runtime_fingerprint
                or value.effective_runtime is None
                or not _valid_seal(value)
            ):
                raise InvalidPreparedAgentRuntime()
            binding = _binding_from_prepared(value)
        elif isinstance(value, RuntimeRoutingBinding):
            binding = value
        else:
            binding = getattr(value, "_runtime_routing_binding", None)
            if isinstance(binding, PreparedAgentRuntime):
                if (
                    not binding.effective_runtime_fingerprint
                    or binding.effective_runtime is None
                    or not _valid_seal(binding)
                ):
                    raise InvalidPreparedAgentRuntime()
                binding = _binding_from_prepared(binding)
    except InvalidPreparedAgentRuntime:
        raise
    except Exception:
        return {}
    if not isinstance(binding, RuntimeRoutingBinding):
        return {}
    try:
        return binding.public_record()
    except Exception:
        return {}


def log_runtime_routing_event(binding: RuntimeRoutingBinding) -> None:
    """Emit one credential-free construction event through normal logging."""
    record = public_runtime_binding(binding)
    if not record:
        # Observability must never make an otherwise executable runtime fail
        # construction.  Some provider/model identifiers are valid execution
        # inputs but intentionally outside the stricter public-export grammar.
        logger.warning(
            "runtime_routing=unavailable reason=invalid_public_record"
        )
        return
    logger.info(
        "runtime_routing=%s",
        json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
    )


def runtime_resolver_requires_initial_task(
    scope: Literal["fresh_session", "delegation"]
) -> bool:
    if scope not in _SCOPES:
        return False
    try:
        with _resolver_lease() as resolver:
            if resolver is None:
                return False
            result = resolver.requires_initial_task(scope)
        if type(result) is not bool:
            _warn_finite("resolver_contract_invalid")
            return False
        return result
    except Exception:
        _warn_finite("resolver_error")
        return False


def failed_runtime_spec_from(
    error: BaseException,
    *,
    model: str = "",
    provider: str = "",
) -> tuple[AgentRuntimeSpec, Mapping[str, JsonValue]]:
    """Replace a provider failure with the finite, non-sensitive failure shape."""
    try:
        from agent.redact import redact_sensitive_text

        redact_sensitive_text(str(error), force=True)
    except Exception:
        pass
    return (
        AgentRuntimeSpec(
            model=model,
            provider=provider,
            resolution_state="failed",
            resolution_reason_code="resolution_failed",
        ),
        MappingProxyType({"reason_code": "resolution_failed"}),
    )


def _normalized_fallback_chain(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in _fallback_tuple(value)]


def safe_session_base_url(value: Any) -> str:
    """Return a credential-free HTTP(S) endpoint suitable for SessionDB."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 2048
        or any(marker in candidate for marker in ("\x00", "\r", "\n"))
        or _public_string_looks_secret(candidate)
    ):
        return ""
    try:
        parsed = urlsplit(candidate)
        # Accessing ``port`` forces malformed port/IPv6 validation.
        parsed.port
    except Exception:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "", "", "")
    )


def session_runtime_metadata(
    runtime: AgentRuntimeSpec,
    *,
    manual_pin_source: str | None = None,
) -> dict[str, Any]:
    """Project only non-secret runtime identity into durable session metadata."""
    if not isinstance(runtime, AgentRuntimeSpec):
        raise TypeError("runtime must be an AgentRuntimeSpec")

    metadata: dict[str, Any] = {}
    for key, value, allow_slash in (
        ("model", runtime.model, True),
        ("provider", runtime.provider, False),
        ("api_mode", runtime.api_mode, False),
    ):
        if not value:
            continue
        try:
            metadata[key] = _validate_public_identifier(
                value,
                label=f"session {key}",
                allow_empty=False,
                allow_slash=allow_slash,
            )
        except Exception:
            continue

    safe_base_url = safe_session_base_url(runtime.base_url)
    if safe_base_url:
        metadata["base_url"] = safe_base_url

    if runtime.reasoning_config is not None:
        try:
            metadata["reasoning_config"] = _thaw_public(
                _validate_public_value(
                    runtime.reasoning_config,
                    label="session reasoning config",
                )
            )
        except Exception:
            pass

    if manual_pin_source:
        try:
            metadata["runtime_manual_pin_source"] = _validate_public_identifier(
                manual_pin_source,
                label="manual pin source",
                allow_empty=False,
            )
        except Exception:
            pass
    return metadata


def _persist_runtime_session_metadata(
    agent: Any,
    *,
    session_id: str,
    runtime: AgentRuntimeSpec,
    manual_pin_source: str | None,
) -> None:
    metadata = session_runtime_metadata(
        runtime,
        manual_pin_source=manual_pin_source,
    )

    initial = getattr(agent, "_session_init_model_config", None)
    initial_config = dict(initial) if isinstance(initial, Mapping) else {}
    for key in _SESSION_RUNTIME_METADATA_KEYS:
        initial_config.pop(key, None)
    initial_config.update(metadata)
    agent._session_init_model_config = initial_config

    session_db = getattr(agent, "_session_db", None)
    if session_db is None:
        return
    try:
        row = session_db.get_session(session_id)
        if not row:
            return
        raw_config = row.get("model_config")
        if isinstance(raw_config, Mapping):
            persisted = dict(raw_config)
        elif isinstance(raw_config, str) and raw_config.strip():
            parsed = json.loads(raw_config)
            persisted = dict(parsed) if isinstance(parsed, Mapping) else {}
        else:
            persisted = {}
        for key in _SESSION_RUNTIME_METADATA_KEYS:
            persisted.pop(key, None)
        persisted.update(metadata)
        session_db.update_session_meta(
            session_id,
            json.dumps(persisted, sort_keys=True, separators=(",", ":")),
            model=metadata.get("model") or None,
        )
    except Exception:
        # Runtime selection remains canonical even when SessionDB is briefly
        # unavailable. Never include the rejected value or exception text.
        logger.warning(
            "Agent runtime session metadata persistence failed: session_store_error"
        )


def apply_manual_runtime_transition(
    agent: Any | None,
    *,
    session_id: str,
    source: str,
    runtime: AgentRuntimeSpec,
    fallback_model: Any,
) -> RuntimeRoutingBinding:
    """Record canonical manual intent and atomically restore host fallbacks."""
    request = ManualRuntimePinRequest(
        session_id=session_id,
        source=source,
        runtime=runtime,
    )
    binding = RuntimeRoutingBinding(
        scope="fresh_session",
        session_id=session_id,
        task_id="manual",
        operation_id=None,
        action="inherit",
        runtime=runtime,
        owns_fallbacks=False,
        reason_code="manual_runtime_pin",
        manual_pin_source=source,
    )
    if agent is not None:
        host_chain = _normalized_fallback_chain(fallback_model)
        agent._runtime_routing_binding = binding
        agent._runtime_fallback_authority = "host"
        agent._fallback_chain = host_chain
        agent._fallback_index = 0
        agent._fallback_activated = False
        agent._fallback_model = host_chain[0] if host_chain else None
        _persist_runtime_session_metadata(
            agent,
            session_id=session_id,
            runtime=runtime,
            manual_pin_source=source,
        )
    try:
        with _resolver_lease() as resolver:
            if resolver is not None:
                resolver.record_manual_pin(request)
    except Exception:
        # The canonical user switch already happened. Keep local manual intent
        # and host fallback authority installed; reverting here would silently
        # resurrect stale plugin policy. Surface only a finite persistence
        # degradation so the host can notify/retry.
        logger.warning("Agent runtime manual transition failed: resolver_error")
        raise RuntimeError("Runtime resolver could not record manual transition") from None
    return binding


def _coerce_binding(value: Any) -> RuntimeRoutingBinding | None:
    if isinstance(value, RuntimeRoutingBinding):
        return value
    if isinstance(value, PreparedAgentRuntime):
        if (
            not value.effective_runtime_fingerprint
            or value.effective_runtime is None
            or not _valid_seal(value)
        ):
            raise InvalidPreparedAgentRuntime()
        return _binding_from_prepared(value)
    return None


def record_runtime_session_continuation(
    agent: Any,
    *,
    parent_session_id: str,
    child_session_id: str,
    reason: Literal["compression"] = "compression",
) -> RuntimeRoutingBinding | None:
    """Alias a durable compression child without classifying or changing runtime."""
    request = RuntimeSessionContinuation(
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        reason=reason,
    )
    binding = _coerce_binding(getattr(agent, "_runtime_routing_binding", None))
    if binding is not None and binding.session_id != parent_session_id:
        raise RuntimeContinuationError(
            "Runtime continuation parent does not match the active binding"
        )
    try:
        with _resolver_lease() as resolver:
            if resolver is not None:
                recorder = getattr(resolver, "record_session_continuation", None)
                if callable(recorder):
                    recorder(request)
    except Exception:
        logger.warning("Agent runtime continuation failed: resolver_error")
        raise RuntimeError("Runtime resolver could not record continuation") from None

    if binding is None:
        return None
    continued = replace(binding, session_id=child_session_id)
    agent._runtime_routing_binding = continued
    return continued


def repair_runtime_session_continuations(
    session_id: str,
    *,
    parent_session_id_for: Callable[[str], str | None],
    max_depth: int = 32,
) -> int:
    """Idempotently replay a bounded durable compression lineage to the resolver."""
    _validate_public_identifier(session_id, label="session id", allow_empty=False)
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth <= 0:
        raise ValueError("Invalid runtime continuation depth")
    try:
        resolver_lease = _resolver_lease()
        resolver = resolver_lease.__enter__()
    except Exception:
        logger.warning("Agent runtime continuation repair failed: resolver_error")
        return 0
    try:
        if resolver is None:
            return 0
        recorder = getattr(resolver, "record_session_continuation", None)
        if not callable(recorder):
            return 0

        edges: list[tuple[str, str]] = []
        seen = {session_id}
        child = session_id
        for _ in range(max_depth):
            parent = parent_session_id_for(child)
            if parent is None:
                break
            _validate_public_identifier(
                parent, label="parent session id", allow_empty=False
            )
            if parent in seen:
                raise RuntimeContinuationError(
                    "Runtime continuation lineage contains a cycle"
                )
            seen.add(parent)
            edges.append((parent, child))
            child = parent
        else:
            if parent_session_id_for(child) is not None:
                raise RuntimeContinuationError(
                    "Runtime continuation lineage exceeds maximum depth"
                )

        for parent, child in reversed(edges):
            try:
                recorder(
                    RuntimeSessionContinuation(
                        parent_session_id=parent,
                        child_session_id=child,
                    )
                )
            except Exception:
                logger.warning(
                    "Agent runtime continuation repair failed: resolver_error"
                )
                raise RuntimeError(
                    "Runtime resolver could not repair continuation"
                ) from None
        return len(edges)
    finally:
        resolver_lease.__exit__(None, None, None)


def repair_runtime_session_continuations_from_store(
    session_id: str,
    *,
    session_store: Any,
    max_depth: int = 32,
) -> int:
    """Replay only the durable compression lineage exposed by SessionDB.

    ``parent_session_id`` is also used by branches and delegated children, so
    routing code must not infer compression ancestry from that column alone.
    SessionDB owns the distinction and exposes it through
    ``get_compression_lineage()``. Wrappers may pass either SessionDB itself or
    their small ``._db`` facade.
    """
    _validate_public_identifier(session_id, label="session id", allow_empty=False)
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth <= 0:
        raise ValueError("Invalid runtime continuation depth")

    store = getattr(session_store, "_db", session_store)
    lineage_reader = getattr(store, "get_compression_lineage", None)
    if not callable(lineage_reader):
        return 0
    try:
        raw_lineage = lineage_reader(session_id)
    except Exception:
        logger.warning("Agent runtime continuation repair failed: session_store_error")
        return 0
    if not isinstance(raw_lineage, (list, tuple)):
        raise RuntimeContinuationError("Runtime continuation lineage is invalid")

    lineage = tuple(raw_lineage)
    if not lineage:
        return 0
    for item in lineage:
        _validate_public_identifier(
            item,
            label="compression session id",
            allow_empty=False,
        )
    try:
        requested_index = lineage.index(session_id)
    except ValueError:
        raise RuntimeContinuationError(
            "Runtime continuation lineage does not contain the requested session"
        ) from None
    lineage = lineage[: requested_index + 1]
    if len(lineage) - 1 > max_depth:
        raise RuntimeContinuationError(
            "Runtime continuation lineage exceeds maximum depth"
        )
    parents = {child: parent for parent, child in zip(lineage, lineage[1:])}
    return repair_runtime_session_continuations(
        session_id,
        parent_session_id_for=parents.get,
        max_depth=max_depth,
    )


__all__ = [
    "RUNTIME_ROUTING_CONTRACT_VERSION",
    "AgentRuntimeContext",
    "AgentRuntimePlan",
    "AgentRuntimePreparationKey",
    "AgentRuntimeRequest",
    "AgentRuntimeResolver",
    "AgentRuntimeSpec",
    "InvalidPreparedAgentRuntime",
    "ManualRuntimePinRequest",
    "PreparedAgentRuntime",
    "RuntimeContinuationError",
    "RuntimeRoutingBinding",
    "RuntimeRoutingDeferred",
    "RuntimeSessionContinuation",
    "apply_manual_runtime_transition",
    "apply_runtime_plan_to_constructor_arguments",
    "constructor_runtime_spec",
    "failed_runtime_spec_from",
    "finalize_prepared_agent_runtime",
    "log_runtime_routing_event",
    "prepare_agent_runtime",
    "prepare_agent_runtime_for_construction",
    "prepare_constructor_runtime",
    "public_runtime_binding",
    "record_runtime_session_continuation",
    "repair_runtime_session_continuations",
    "repair_runtime_session_continuations_from_store",
    "resolve_ordinary_hermes_runtime",
    "runtime_resolver_requires_initial_task",
    "runtime_spec_has_exact_execution_binding",
    "safe_session_base_url",
    "session_runtime_metadata",
    "validate_prepared_agent_runtime",
]
