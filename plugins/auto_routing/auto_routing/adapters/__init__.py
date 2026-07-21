"""Hermes-version adapters for auto-routing inventory and projection."""

from .base import (
    PERSISTED_RUNTIME_PROJECTION_CONTRACT,
    AccessVerification,
    AdapterInventory,
    HermesAdapter,
    LocalInventoryRow,
    PersistedRuntimeProjection,
    ProviderInventoryRow,
    ResolvedRuntime,
    RuntimeResolutionMismatch,
    VerificationRequest,
)
from .hermes_0_18 import Hermes018Adapter

__all__ = [
    "AccessVerification",
    "AdapterInventory",
    "HermesAdapter",
    "Hermes018Adapter",
    "LocalInventoryRow",
    "PERSISTED_RUNTIME_PROJECTION_CONTRACT",
    "PersistedRuntimeProjection",
    "ProviderInventoryRow",
    "ResolvedRuntime",
    "RuntimeResolutionMismatch",
    "VerificationRequest",
]
