"""Effect adapter SDK: descriptors, the abstract adapter, and registration.

An adapter cannot manufacture guarantees. Every capability named in an
``AdapterDescriptor`` — query reconciliation, exact or semantic
compensation — is verified against the adapter's concrete method overrides
at registration time, so a false claim fails loudly before any transaction
can rely on it. Plugin adapters register through the same
``register_effect_adapter()`` without modifying core.
"""

from __future__ import annotations

import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional

from agent.effects.models import (
    CommitOutcome,
    CommitRequest,
    CompensationRequest,
    CompensationResult,
    EffectContext,
    EffectPreview,
    EffectTransaction,
    NormalizedEffect,
    PreparedEffect,
    ReconciliationResult,
    RevisionNode,
    VerificationResult,
)

__all__ = [
    "AdapterContractError",
    "AdapterDescriptor",
    "EffectAdapter",
    "EffectAdapterRegistry",
    "default_effect_adapter_registry",
    "get_effect_adapter",
    "register_effect_adapter",
]

# Versioned ids only: a descriptor without an explicit version cannot be
# evolved compatibly ("workspace.v1", "hermes-config.v1", ...).
_ADAPTER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*\.v\d+$")


class AdapterContractError(ValueError):
    """An adapter declared a capability its implementation cannot honor."""


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    actions: frozenset[str]
    idempotency: Literal["none", "keyed", "native"]
    reconciliation: Literal["none", "query"]
    compensation: Literal["exact", "semantic", "none"]
    irreversible_after: Literal["never", "dispatch", "commit"]
    compensation_window_seconds: Optional[int] = None


class EffectAdapter(ABC):
    """Effect-specific prepare/preview/commit/verify behavior.

    ``reconcile`` and ``compensate`` have fail-closed default
    implementations on purpose: capability validation checks whether a
    subclass *overrides* them, which is the registration-time proof that a
    declared ``reconciliation="query"`` or ``compensation`` claim is real.
    """

    descriptor: AdapterDescriptor

    @abstractmethod
    def normalize(self, node: RevisionNode, context: EffectContext) -> NormalizedEffect:
        raise NotImplementedError

    @abstractmethod
    def prepare(self, effect: NormalizedEffect, context: EffectContext) -> PreparedEffect:
        raise NotImplementedError

    @abstractmethod
    def preview(self, prepared: PreparedEffect, context: EffectContext) -> EffectPreview:
        raise NotImplementedError

    @abstractmethod
    def commit(self, request: CommitRequest, context: EffectContext) -> CommitOutcome:
        raise NotImplementedError

    @abstractmethod
    def verify(self, outcome: CommitOutcome, context: EffectContext) -> VerificationResult:
        raise NotImplementedError

    def reconcile(
        self, effect: EffectTransaction, context: EffectContext
    ) -> ReconciliationResult:
        raise NotImplementedError(
            f"adapter {type(self).__name__} declares no query reconciliation"
        )

    def compensate(
        self, request: CompensationRequest, context: EffectContext
    ) -> CompensationResult:
        raise NotImplementedError(
            f"adapter {type(self).__name__} declares no compensation"
        )


def _validate_descriptor_against_adapter(adapter: EffectAdapter) -> AdapterDescriptor:
    descriptor = getattr(adapter, "descriptor", None)
    if not isinstance(descriptor, AdapterDescriptor):
        raise AdapterContractError(
            f"adapter {type(adapter).__name__} has no AdapterDescriptor"
        )
    if not descriptor.adapter_id or not _ADAPTER_ID_PATTERN.match(descriptor.adapter_id):
        raise AdapterContractError(
            f"adapter id {descriptor.adapter_id!r} must be a versioned id "
            "like 'workspace.v1'"
        )
    if not descriptor.actions:
        raise AdapterContractError(
            f"adapter {descriptor.adapter_id!r} declares no actions"
        )
    cls = type(adapter)
    overrides_reconcile = cls.reconcile is not EffectAdapter.reconcile
    overrides_compensate = cls.compensate is not EffectAdapter.compensate
    if descriptor.reconciliation == "query" and not overrides_reconcile:
        raise AdapterContractError(
            f"adapter {descriptor.adapter_id!r} claims query reconciliation "
            "but inherits the default reconcile()"
        )
    if descriptor.compensation != "none" and not overrides_compensate:
        raise AdapterContractError(
            f"adapter {descriptor.adapter_id!r} claims "
            f"{descriptor.compensation} compensation but inherits the "
            "default compensate()"
        )
    if descriptor.compensation_window_seconds is not None:
        if descriptor.compensation == "none":
            raise AdapterContractError(
                f"adapter {descriptor.adapter_id!r} sets "
                "compensation_window_seconds with compensation='none'"
            )
        if descriptor.compensation_window_seconds <= 0:
            raise AdapterContractError(
                f"adapter {descriptor.adapter_id!r} "
                "compensation_window_seconds must be positive"
            )
    if descriptor.irreversible_after == "never" and descriptor.compensation == "none":
        raise AdapterContractError(
            f"adapter {descriptor.adapter_id!r} claims irreversible_after="
            "'never' but offers no compensation path"
        )
    return descriptor


class EffectAdapterRegistry:
    """Thread-safe adapter registration with capability verification."""

    def __init__(self):
        self._adapters: dict[str, EffectAdapter] = {}
        self._lock = threading.RLock()

    def register(self, adapter: EffectAdapter) -> AdapterDescriptor:
        descriptor = _validate_descriptor_against_adapter(adapter)
        with self._lock:
            if descriptor.adapter_id in self._adapters:
                raise AdapterContractError(
                    f"duplicate adapter_id {descriptor.adapter_id!r}"
                )
            self._adapters[descriptor.adapter_id] = adapter
        return descriptor

    def unregister(self, adapter_id: str) -> None:
        with self._lock:
            self._adapters.pop(adapter_id, None)

    def get(self, adapter_id: str) -> EffectAdapter:
        with self._lock:
            adapter = self._adapters.get(adapter_id)
        if adapter is None:
            raise KeyError(
                f"unknown effect adapter {adapter_id!r}; registered: "
                f"{sorted(self._adapters)}"
            )
        return adapter

    def get_descriptor(self, adapter_id: str) -> AdapterDescriptor:
        return self.get(adapter_id).descriptor

    def list_descriptors(self) -> tuple[AdapterDescriptor, ...]:
        with self._lock:
            return tuple(
                adapter.descriptor
                for _, adapter in sorted(self._adapters.items())
            )

    def supports(self, adapter_id: str, action: str) -> bool:
        try:
            return action in self.get(adapter_id).descriptor.actions
        except KeyError:
            return False


_default_registry = EffectAdapterRegistry()


def default_effect_adapter_registry() -> EffectAdapterRegistry:
    return _default_registry


def register_effect_adapter(adapter: EffectAdapter) -> AdapterDescriptor:
    """Register an adapter with the process-global registry.

    This is the single entry point for built-in and plugin adapters alike.
    """
    return _default_registry.register(adapter)


def get_effect_adapter(adapter_id: str) -> EffectAdapter:
    return _default_registry.get(adapter_id)
