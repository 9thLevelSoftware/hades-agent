"""Profile-local cache-safe runtime resolver for static Auto Routing."""

from __future__ import annotations

import hashlib
import json
import threading
import weakref
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agent.runtime_routing import (
    AgentRuntimePlan,
    AgentRuntimeRequest,
    ManualRuntimePinRequest,
    RuntimeSessionContinuation,
)
from hermes_constants import get_hermes_home

from .service import AutoRoutingService
from .storage import RuntimeRoutingPending


# Thread identifiers can be reused after a worker exits.  Weak references keep
# that identity exact without retaining every short-lived TUI turn thread (and
# its SQLite-backed service) for the lifetime of the process.
_ThreadRef = weakref.ReferenceType[threading.Thread]
_ServiceCacheKey = tuple[Path, _ThreadRef]


class AdapterIncompatible(RuntimeError):
    """The current adapter cannot preserve the runtime projection contract."""


def _inherit(request: AgentRuntimeRequest, reason_code: str) -> AgentRuntimePlan:
    return AgentRuntimePlan(
        action="inherit",
        runtime=request.baseline,
        owns_fallbacks=False,
        reason_code=reason_code,
        event={"reason_code": reason_code},
    )


def _fixed_delegation_runtime(metadata: Mapping[str, Any]) -> bool:
    return bool(
        metadata.get("fixed_delegation_provider")
        or metadata.get("fixed_delegation_model")
    )


class AutoRoutingRuntimeResolver:
    """Own one semantic route decision at each fresh construction boundary."""

    def __init__(
        self,
        plugin_context: Any,
        *,
        home_resolver: Callable[[], Path] = get_hermes_home,
        service_factory: Callable[[], Any] | None = None,
        backend_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._plugin_context = plugin_context
        self._home_resolver = home_resolver
        self._service_factory = service_factory or (
            lambda: AutoRoutingService.from_plugin_context(
                plugin_context,
                allow_cross_thread_close=True,
            )
        )
        self._backend_factory = backend_factory or _ServiceRuntimeBackend
        self._services: dict[_ServiceCacheKey, Any] = {}
        self._backends: dict[_ServiceCacheKey, Any] = {}
        self._lock = threading.RLock()
        self._closed = False

    def requires_initial_task(self, scope: str) -> bool:
        return scope in {"fresh_session", "delegation"}

    def service_for_current_profile(self) -> Any:
        return self._service_for_key(self._current_cache_key())

    def _current_cache_key(self) -> _ServiceCacheKey:
        home = Path(self._home_resolver()).expanduser().resolve()
        thread = threading.current_thread()
        retired = ()
        with self._lock:
            stale_keys = tuple(
                key
                for key in self._services
                if (owner := key[1]()) is None
                or (owner is not thread and not owner.is_alive())
            )
            retired = tuple(
                {
                    id(self._services[key]): self._services[key]
                    for key in stale_keys
                }.values()
            )
            for key in stale_keys:
                self._services.pop(key, None)
                self._backends.pop(key, None)
            for key in self._services:
                if key[0] == home and key[1]() is thread:
                    current_key = key
                    break
            else:
                current_key = (
                    home,
                    weakref.ref(thread, self._release_thread_services),
                )
        for service in retired:
            _close_service(service)
        return current_key

    def _release_thread_services(self, thread_ref: _ThreadRef) -> None:
        """Close every profile service owned by one collected worker thread."""
        with self._lock:
            keys = tuple(key for key in self._services if key[1] is thread_ref)
            services = tuple(
                {
                    id(self._services[key]): self._services[key]
                    for key in keys
                }.values()
            )
            for key in keys:
                self._services.pop(key, None)
                self._backends.pop(key, None)
        for service in services:
            _close_service(service)

    def _service_for_key(self, key: _ServiceCacheKey) -> Any:
        home = key[0]
        with self._lock:
            if self._closed:
                raise RuntimeError("auto-routing resolver is closed")
            service = self._services.get(key)
            if service is None:
                service = self._service_factory()
                service_home = getattr(service, "hermes_home", home)
                if Path(service_home).expanduser().resolve() != home:
                    _close_service(service)
                    raise RuntimeError("auto-routing service belongs to another profile")
                self._services[key] = service
                self._backends[key] = self._backend_factory(service)
            return service

    def _backend_for_current_profile(self) -> Any:
        key = self._current_cache_key()
        self._service_for_key(key)
        with self._lock:
            return self._backends[key]

    def resolve(self, request: AgentRuntimeRequest) -> AgentRuntimePlan:
        try:
            backend = self._backend_for_current_profile()
            binding = backend.read_binding(request)
        except Exception:
            return _inherit(request, "plugin_state_unavailable")

        try:
            config = backend.load_config()
        except Exception:
            if request.context.manual_runtime_pin:
                return _inherit(request, "manual_runtime_pin")
            if binding is not None and getattr(binding, "binding_kind", "") == "manual":
                return _inherit(request, "manual_runtime_pin")
            if binding is not None:
                try:
                    return backend.replay(request, binding)
                except AdapterIncompatible:
                    return _inherit(request, "adapter_incompatible")
                except Exception:
                    return _inherit(request, "recorded_state_invalid")
            return _inherit(request, "authority_invalid")

        mode = str(getattr(getattr(config, "activation", None), "mode", "off"))
        if mode == "off":
            return _inherit(request, "routing_off")
        if request.context.manual_runtime_pin:
            return _inherit(request, "manual_runtime_pin")
        if binding is not None and getattr(binding, "binding_kind", "") == "manual":
            return _inherit(request, "manual_runtime_pin")
        if binding is not None:
            try:
                return backend.replay(request, binding)
            except AdapterIncompatible:
                return _inherit(request, "adapter_incompatible")
            except Exception:
                return _inherit(request, "recorded_state_invalid")
        if request.context.is_resume:
            return _inherit(request, "resume_binding_missing")

        scopes = getattr(config, "scopes", None)
        enabled = (
            getattr(scopes, "fresh_sessions", False)
            if request.context.scope == "fresh_session"
            else getattr(scopes, "delegation", False)
        )
        if not enabled:
            return _inherit(request, "scope_disabled")
        if request.context.scope == "delegation" and _fixed_delegation_runtime(
            request.context.metadata
        ):
            return _inherit(request, "fixed_delegation_runtime")

        receipt = None
        if mode == "active":
            try:
                receipt = backend.matching_activation_receipt(config)
            except AdapterIncompatible:
                return _inherit(request, "adapter_incompatible")
            except Exception:
                return _inherit(request, "activation_receipt_invalid")
            if receipt is None:
                return _inherit(request, "activation_receipt_missing")
        try:
            return backend.decide(request, config, receipt)
        except AdapterIncompatible:
            return _inherit(request, "adapter_incompatible")
        except RuntimeRoutingPending:
            return AgentRuntimePlan(
                action="defer",
                runtime=request.baseline,
                owns_fallbacks=False,
                reason_code="operation_pending",
                retry_after_seconds=0.25,
                event={"reason_code": "operation_pending"},
            )
        except Exception:
            return _inherit(request, "resolver_error")

    def record_manual_pin(self, request: ManualRuntimePinRequest) -> None:
        self._backend_for_current_profile().record_manual_pin(request)

    def record_session_continuation(
        self,
        request: RuntimeSessionContinuation,
    ) -> None:
        self._backend_for_current_profile().record_session_continuation(request)

    def on_pre_api_request(self, **kwargs: Any) -> None:
        """Mark an epoch using identifiers only; raw hook payloads are ignored."""
        event = {
            key: kwargs.get(key)
            for key in (
                "session_id",
                "task_id",
                "api_request_id",
                "decision_id",
                "runtime_id",
                "model",
                "provider",
            )
        }
        try:
            self._backend_for_current_profile().mark_provider_started(**event)
        except Exception:
            return

    def on_post_turn_outcome(self, **kwargs: Any) -> None:
        """Persist only an exactly attributable, content-free routed outcome."""
        try:
            service = self.service_for_current_profile()
            committed = service.ingest_turn_outcome(kwargs)
            if committed is not None:
                service.record_management_outcome(committed)
        except Exception:
            return None
        return None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            services = tuple(
                {id(value): value for value in self._services.values()}.values()
            )
            self._services.clear()
            self._backends.clear()
        for service in services:
            _close_service(service)


def _close_service(service: Any) -> None:
    close = getattr(service, "close", None)
    if callable(close):
        close()
        return
    store = getattr(service, "store", None)
    store_close = getattr(store, "close", None)
    if callable(store_close):
        store_close()


class _ServiceRuntimeBackend:
    """Concrete composition over the profile-local advisor/runtime services."""

    def __init__(self, service: AutoRoutingService) -> None:
        self.service = service

    def load_config(self):
        self.service._assert_profile_isolation()
        return self.service._configured_authority()

    def read_binding(self, request: AgentRuntimeRequest):
        return self.service.store.read_session_binding(request.context.session_id)

    def matching_activation_receipt(self, config):
        from .config import authority_revision, config_revision

        authority_id = authority_revision(config)
        if not self.service._authority_is_usable(config, authority_id):
            return None
        config_sha = config_revision(config)
        capability_sha = _adapter_capability_sha(self.service.adapter)
        return self.service.store.read_matching_activation_receipt(
            authority_id=authority_id,
            config_sha=config_sha,
            adapter_capability_sha=capability_sha,
        )

    def replay(self, request: AgentRuntimeRequest, binding: Any) -> AgentRuntimePlan:
        return self.service.replay_runtime_decision(
            request=request,
            binding=binding,
        )

    def decide(self, request: AgentRuntimeRequest, config: Any, receipt: Any):
        return self.service.create_runtime_decision(
            request=request,
            config=config,
            activation_receipt=receipt,
            adapter_capability_sha=_adapter_capability_sha(self.service.adapter),
        )

    def record_manual_pin(self, request: ManualRuntimePinRequest) -> None:
        self.service.record_runtime_manual_pin(request)

    def record_session_continuation(
        self,
        request: RuntimeSessionContinuation,
    ) -> None:
        self.service.record_runtime_continuation(request)

    def mark_provider_started(self, **event: Any) -> None:
        self.service.mark_runtime_provider_started(**event)


def _adapter_capability_sha(adapter: Any) -> str:
    try:
        report = adapter.capability_report()
        required = (
            "fresh_session",
            "delegation",
            "pre_call_fallback",
            "exact_credential_pool",
            "reasoning_projection",
        )
        if (
            not isinstance(report, Mapping)
            or not isinstance(report.get("contract"), str)
            or not report.get("contract")
            or any(report.get(name) is not True for name in required)
            or report.get("post_call_model_failover") is not False
        ):
            raise AdapterIncompatible()
        payload = json.dumps(
            dict(report),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except AdapterIncompatible:
        raise
    except Exception:
        raise AdapterIncompatible() from None
    return hashlib.sha256(payload).hexdigest()


__all__ = ["AutoRoutingRuntimeResolver"]
