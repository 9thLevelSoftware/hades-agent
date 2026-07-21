"""Fail-closed executable runtime inventory and explicit access verification."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

from agent.reasoning_support import ReasoningSupport, resolve_reasoning_support

from .adapters.base import (
    AccessVerification,
    HermesAdapter,
    LocalInventoryRow,
    ProviderInventoryRow,
    ResolvedRuntime,
    VerificationOutcomeUncertain,
    VerificationRequest,
    ensure_runtime_match,
)
from .models import (
    AccessEconomics,
    CandidateReasonCode,
    PolicyEnvelope,
    RuntimeKey,
    RuntimeObservation,
)
from .storage import BudgetExceeded, RoutingStore, VerificationAttemptConflict

VERIFICATION_PROMPT = "Return exactly AUTO_ROUTING_ACCESS_OK"
VERIFICATION_SENTINEL = "AUTO_ROUTING_ACCESS_OK"
VERIFICATION_MAXIMUM_OUTPUT_TOKENS = 16
VERIFICATION_PREVIEW_TTL_SECONDS = 300
VERIFICATION_EVIDENCE_TTL_SECONDS = 24 * 60 * 60
LOCAL_EVIDENCE_TTL_SECONDS = 24 * 60 * 60
VERIFICATION_ECONOMICS_TTL_SECONDS = 24 * 60 * 60
VERIFICATION_ECONOMICS_CLOCK_SKEW_SECONDS = 5 * 60
VERIFICATION_COST_RESERVE_FRACTION = 0.10
VERIFICATION_BUDGET_BUCKET = "runtime-access-verification"
_UNCERTAIN_VERIFICATION_REASONS = frozenset(
    {
        "verification_request_outcome_uncertain",
        "verification_response_usage_uncertain",
    }
)


@dataclass(frozen=True)
class _ValidatedAccessContract:
    provider: str
    api_mode: str
    auth_kind: str
    resolver_namespace: str
    models: frozenset[str]
    endpoint_identities: frozenset[str]

    def matches(self, row: ProviderInventoryRow, model: str) -> bool:
        resolver_name = row.resolver_name.strip().lower()
        namespace = self.resolver_namespace.lower()
        return (
            row.provider.strip().lower() == self.provider.lower()
            and row.api_mode.strip().lower() == self.api_mode.lower()
            and model in self.models
            and row.endpoint_identity in self.endpoint_identities
            and row.auth_identity.partition(":")[0].strip().lower()
            == self.auth_kind.lower()
            and (
                resolver_name == namespace
                or resolver_name.startswith(f"{namespace}:")
            )
        )


VALIDATED_ACCESS_CONTRACT_REGISTRY = MappingProxyType(
    {
        ("codex-subscription", 1): _ValidatedAccessContract(
            provider="openai-codex",
            api_mode="codex_responses",
            auth_kind="subscription",
            resolver_namespace="openai-codex",
            models=frozenset(
                {
                    "gpt-5.6-sol",
                    "gpt-5.6-sol-pro",
                    "gpt-5.6-terra",
                    "gpt-5.6-terra-pro",
                    "gpt-5.6-luna",
                    "gpt-5.6-luna-pro",
                    "gpt-5.5",
                    "gpt-5.4-mini",
                    "gpt-5.4",
                    "gpt-5.3-codex",
                    "gpt-5.3-codex-spark",
                }
            ),
            endpoint_identities=frozenset(
                {"endpoint:9ab74da1d15bdc50a0f3fd1c"}
            ),
        ),
    }
)


class VerificationError(RuntimeError):
    """Base error for guarded explicit access verification."""


class VerificationApprovalRequired(VerificationError):
    """The operator did not acknowledge the bounded billable call."""


class VerificationNotAllowed(VerificationError):
    """Policy or runtime state does not permit an explicit access probe."""


class VerificationEconomicsUnavailable(VerificationError):
    """Worst-case cost/quota exposure cannot be bounded."""


class VerificationPreconditionChanged(VerificationError):
    """Inventory, policy, budget, or preview TTL changed after preview."""


class VerificationReplay(VerificationError):
    """A one-shot precondition hash has already been consumed."""


class VerificationFailed(VerificationError):
    """The one bounded provider call did not prove exact runtime access."""


class ReasonCodes(tuple[str, ...]):
    """Immutable reason codes with ergonomic equality against JSON lists."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return super().__eq__(other)

    __hash__ = tuple.__hash__


@dataclass(frozen=True)
class ExecutableRuntime:
    """Task 6 wrapper retaining reasoning evidence beside Task 5 fields."""

    key: RuntimeKey
    resolver_name: str
    state: str
    reasons: ReasonCodes
    economics: AccessEconomics
    reasoning_support: ReasoningSupport
    verification_source: str | None
    verified_at: str | None
    verification_expires_at: str | None
    provenance: tuple[str, ...]
    observed_at: str
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capabilities",
            {
                str(key): _freeze_capability_value(value)
                for key, value in self.capabilities.items()
            },
        )

    def to_observation(self) -> RuntimeObservation:
        return RuntimeObservation(
            key=self.key,
            state=self.state,
            reasons=tuple(self.reasons),
            economics=self.economics,
            verification_source=self.verification_source,
            verified_at=self.verified_at,
            verification_expires_at=self.verification_expires_at,
            provenance=self.provenance,
            observed_at=self.observed_at,
            capabilities=dict(self.capabilities),
        )


def _freeze_capability_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _freeze_capability_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_capability_value(item) for item in value)
    return value


@dataclass(frozen=True)
class InventorySnapshot:
    revision: str
    runtimes: list[ExecutableRuntime]
    observed_at: str

    def eligible(self) -> list[ExecutableRuntime]:
        return [runtime for runtime in self.runtimes if runtime.state == "verified"]


@dataclass(frozen=True)
class ManagementInventoryCandidate:
    """Content-free projection of one currently executable runtime."""

    runtime_id: str
    key: RuntimeKey
    resolver_name: str
    economics: AccessEconomics
    reasoning_support: ReasoningSupport
    verification_source: str
    verification_expires_at: str
    capabilities: Mapping[str, Any]

    @classmethod
    def from_runtime(
        cls,
        runtime: ExecutableRuntime,
    ) -> "ManagementInventoryCandidate":
        return cls(
            runtime_id=runtime.key.stable_id(),
            key=runtime.key,
            resolver_name=runtime.resolver_name,
            economics=runtime.economics,
            reasoning_support=runtime.reasoning_support,
            verification_source=str(runtime.verification_source or ""),
            verification_expires_at=str(runtime.verification_expires_at or ""),
            capabilities=MappingProxyType(
                {
                    str(key): _freeze_capability_value(value)
                    for key, value in runtime.capabilities.items()
                }
            ),
        )


def _parse_management_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _management_now(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _has_runnable_capability(runtime: ExecutableRuntime) -> bool:
    return runtime.capabilities.get("supports_tools") is True


def _is_configured_or_installed_local(runtime: ExecutableRuntime) -> bool:
    if runtime.key.local_backend:
        return (
            runtime.verification_source == "installed_local"
            and "installed-local" in runtime.provenance
            and runtime.key.auth_identity.startswith("local:")
            and bool(runtime.key.local_backend)
        )
    return (
        runtime.verification_source
        in {"authenticated_live", "validated_contract", "explicit_probe"}
        and bool(runtime.resolver_name)
        and bool(runtime.key.auth_identity)
        and not runtime.key.auth_identity.startswith("local:")
    )


def management_inventory_ineligibility_reasons(
    runtime: ExecutableRuntime,
    now: datetime,
) -> tuple[CandidateReasonCode, ...]:
    """Explain why a persisted runtime cannot enter management ranking."""
    if runtime.key.provider.strip().lower() == "moa":
        return ("moa_excluded",)
    if (
        runtime.state != "verified"
        or not runtime.verified_at
        or not runtime.verification_expires_at
    ):
        return ("runtime_not_verified",)
    verified_at = _parse_management_timestamp(runtime.verified_at)
    if verified_at is None or verified_at > _management_now(now):
        return ("runtime_not_verified",)
    expires_at = _parse_management_timestamp(runtime.verification_expires_at)
    if (
        expires_at is None
        or expires_at <= verified_at
        or expires_at <= _management_now(now)
    ):
        return ("runtime_verification_expired",)
    if not _has_runnable_capability(runtime):
        return ("missing_tools",)
    if not _is_configured_or_installed_local(runtime):
        if runtime.key.local_backend or runtime.key.auth_identity.startswith("local:"):
            return ("local_source_not_allowed",)
        return ("configured_provider_source_not_allowed",)
    return ()


def verified_inventory_candidates(
    snapshot: InventorySnapshot,
    now: datetime,
) -> tuple[ManagementInventoryCandidate, ...]:
    """Project eligible runtimes from the supplied persisted/current snapshot."""
    return tuple(
        ManagementInventoryCandidate.from_runtime(runtime)
        for runtime in sorted(
            snapshot.runtimes,
            key=lambda item: item.key.stable_id(),
        )
        if not management_inventory_ineligibility_reasons(runtime, now)
    )


@dataclass(frozen=True)
class VerificationPreview:
    runtime_id: str
    precondition_hash: str
    resolved_runtime_precondition: str
    executor_id: str
    executor_version: str
    execution_shape_fingerprint: str
    maximum_input_tokens: int
    protocol_overhead_tokens: int
    maximum_output_tokens: int
    worst_case_cost_usd: float
    quota_unit: str | None
    expires_at: str
    inventory_revision: str
    inventory_contract_hash: str
    authority_revision: str
    budget_day: str
    budget_ledger_revision: str
    prior_attempt_sequence: int


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _resolved_runtime_precondition(resolved: ResolvedRuntime) -> str:
    """Hash only the public identity produced by exact runtime resolution."""
    return _canonical_hash(resolved.public_record())


def _inventory_contract_hash(snapshot: InventorySnapshot) -> str:
    """Hash executable facts while excluding refresh-generated timestamps."""
    return _canonical_hash(
        [
            {
                "runtime_id": runtime.key.stable_id(),
                "key": runtime.key.model_dump(
                    mode="json",
                    exclude={"inventory_revision"},
                ),
                "resolver_name": runtime.resolver_name,
                "state": runtime.state,
                "reasons": list(runtime.reasons),
                "economics": runtime.economics.model_dump(mode="json"),
                "reasoning": {
                    "efforts": list(runtime.reasoning_support.efforts),
                    "provider_aliases": [
                        list(item)
                        for item in runtime.reasoning_support.provider_aliases
                    ],
                    "provenance": runtime.reasoning_support.provenance,
                    "exact": runtime.reasoning_support.exact,
                },
                "verification_source": runtime.verification_source,
                "verified_at": runtime.verified_at,
                "provenance": list(runtime.provenance),
            }
            for runtime in sorted(
                snapshot.runtimes,
                key=lambda item: item.key.stable_id(),
            )
        ]
    )


def _default_economics(provider: str, observed_at: str) -> AccessEconomics:
    return AccessEconomics(
        billing_kind="metered",
        source_id=f"{provider}-unknown-economics",
        provenance="inventory-unavailable",
        observed_at=observed_at,
    )


class InventoryService:
    """Build and persist only executable, access-path-specific runtimes."""

    def __init__(
        self,
        adapter: HermesAdapter,
        store: RoutingStore | None = None,
        policy: PolicyEnvelope | None = None,
        *,
        clock: Any = None,
    ) -> None:
        self.adapter = adapter
        self.store = store
        self.policy = policy
        self.clock = clock
        self._snapshot: InventorySnapshot | None = None
        self._previews: dict[str, VerificationPreview] = {}

    def _now(self) -> datetime:
        if self.clock is None:
            return datetime.now(UTC)
        if hasattr(self.clock, "now"):
            value = self.clock.now()
        elif callable(self.clock):
            value = self.clock()
        else:
            value = self.clock
        if not isinstance(value, datetime):
            raise TypeError("clock must provide a datetime")
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)

    def refresh(
        self,
        refresh: bool = False,
        *,
        persist: bool = True,
    ) -> InventorySnapshot:
        adapter_inventory = self.adapter.inventory(refresh=refresh)
        observed_at = _iso(self._now())
        revision = _canonical_hash(
            {
                "observed_at": observed_at,
                "providers": [
                    {
                        "provider": row.provider,
                        "resolver_name": row.resolver_name,
                        "models": row.models,
                        "authenticated": row.authenticated,
                        "live_attempt_status": row.live_attempt_status,
                        "model_provenance": dict(row.model_provenance),
                        "provenance_details": {
                            model: dict(details)
                            for model, details in row.provenance_details.items()
                        },
                        "auth_identity": row.auth_identity,
                        "credential_pool_identity": row.credential_pool_identity,
                        "endpoint_identity": row.endpoint_identity,
                        "api_mode": row.api_mode,
                        "credential_fingerprint": row.credential_fingerprint,
                        "capabilities": {
                            model: _freeze_capability_value(capability)
                            for model, capability in row.capabilities.items()
                        },
                        "economics": {
                            model: economics.model_dump(mode="json")
                            for model, economics in row.economics.items()
                        },
                        "cooldown_until": row.cooldown_until,
                        "observed_at": row.observed_at,
                        "source": row.source,
                    }
                    for row in adapter_inventory.provider_rows
                ],
                "local": [
                    {
                        "provider": row.provider,
                        "resolver_name": row.resolver_name,
                        "model": row.model,
                        "backend_identity": row.backend_identity,
                        "reachable": row.reachable,
                        "installed": row.installed,
                        "open_weights": row.open_weights,
                        "license_id": row.license_id,
                        "model_size_bytes": row.model_size_bytes,
                        "available_ram_bytes": row.available_ram_bytes,
                        "available_vram_bytes": row.available_vram_bytes,
                        "loaded_healthy": row.loaded_healthy,
                        "hardware_compatible": row.hardware_compatible,
                        "capabilities": _freeze_capability_value(
                            row.capabilities
                        ),
                        "economics": row.economics.model_dump(mode="json"),
                        "observed_at": row.observed_at,
                    }
                    for row in adapter_inventory.local_rows
                ],
            }
        )
        runtimes: list[ExecutableRuntime] = []
        for row in adapter_inventory.provider_rows:
            if row.provider.strip().lower() == "moa":
                continue
            runtimes.extend(self._provider_runtimes(row, revision))
        for row in adapter_inventory.local_rows:
            runtime = self._local_runtime(row, revision)
            if runtime is not None:
                runtimes.append(runtime)

        runtimes = self._mark_ambiguous_access_paths(runtimes)
        snapshot = InventorySnapshot(
            revision=revision,
            runtimes=runtimes,
            observed_at=observed_at,
        )
        self._snapshot = snapshot
        self._previews.clear()
        if persist:
            self._persist_snapshot(snapshot)
        return snapshot

    def restore_verification_preview(self, precondition_hash: str) -> None:
        """Rebuild current execution state against a persisted preview revision."""
        if self.store is None:
            raise VerificationNotAllowed("verification requires the routing store")
        stored = self.store.read_verification_preview(precondition_hash)
        if stored is None:
            raise VerificationPreconditionChanged("unknown verification preview")
        document = dict(stored.document)
        runtime_id = str(document.get("runtime_id") or "")
        inventory_revision = str(document.get("inventory_revision") or "")
        current = self.refresh(refresh=False, persist=False)
        if _inventory_contract_hash(current) != document.get(
            "inventory_contract_hash"
        ):
            raise VerificationPreconditionChanged("inventory contract changed")
        matches = [
            runtime
            for runtime in current.runtimes
            if runtime.key.stable_id() == runtime_id
        ]
        if len(matches) != 1:
            raise VerificationPreconditionChanged("inventory runtime changed")
        runtime = matches[0]
        if runtime.economics.model_dump(mode="json") != document.get("pricing"):
            raise VerificationPreconditionChanged("verification economics changed")
        rekeyed = [
            replace(
                item,
                key=item.key.model_copy(
                    update={"inventory_revision": inventory_revision}
                ),
            )
            for item in current.runtimes
        ]
        self._snapshot = InventorySnapshot(
            revision=inventory_revision,
            runtimes=rekeyed,
            observed_at=current.observed_at,
        )

    def _provider_runtimes(
        self,
        row: ProviderInventoryRow,
        revision: str,
    ) -> list[ExecutableRuntime]:
        runtimes: list[ExecutableRuntime] = []
        for model in row.models:
            metadata = dict(row.capabilities.get(model) or {})
            if metadata.get("supports_tools") is False:
                continue
            reasoning_support = resolve_reasoning_support(
                provider=row.provider,
                model=model,
                api_mode=row.api_mode,
                metadata=metadata,
            )
            key = RuntimeKey(
                provider=row.provider,
                model=model,
                auth_identity=row.auth_identity,
                credential_pool_identity=row.credential_pool_identity,
                endpoint_identity=row.endpoint_identity,
                api_mode=row.api_mode,
                local_backend="",
                inventory_revision=revision,
            )
            provenance = row.model_provenance.get(model)
            details = dict(row.provenance_details.get(model) or {})
            economics = row.economics.get(model) or _default_economics(
                row.provider,
                row.observed_at,
            )
            (
                state,
                reasons,
                verification_source,
                verified_at,
                expires_at,
            ) = self._provider_state(
                row,
                model,
                provenance,
                details,
                economics,
            )
            runtime = ExecutableRuntime(
                key=key,
                resolver_name=row.resolver_name,
                state=state,
                reasons=ReasonCodes(reasons),
                economics=economics,
                reasoning_support=reasoning_support,
                verification_source=verification_source,
                verified_at=verified_at,
                verification_expires_at=expires_at,
                provenance=(row.source, str(provenance or "missing")),
                observed_at=row.observed_at,
                capabilities=metadata,
            )
            runtimes.append(self._overlay_explicit_probe(runtime, row))
        return runtimes

    def _overlay_explicit_probe(
        self,
        runtime: ExecutableRuntime,
        row: ProviderInventoryRow,
    ) -> ExecutableRuntime:
        if self.store is None or not row.authenticated:
            return runtime
        persisted = self.store.read_inventory(runtime.key)
        if persisted is None or persisted.verification_source != "explicit_probe":
            return runtime
        current_identity = runtime.key.model_dump(exclude={"inventory_revision"})
        persisted_identity = persisted.key.model_dump(exclude={"inventory_revision"})
        if current_identity != persisted_identity:
            return runtime
        if not persisted.verified_at or not persisted.verification_expires_at:
            return runtime
        try:
            expires_at = datetime.fromisoformat(
                persisted.verification_expires_at.replace("Z", "+00:00")
            )
            expires_at = expires_at.replace(
                tzinfo=expires_at.tzinfo or UTC
            ).astimezone(UTC)
        except (TypeError, ValueError):
            return runtime
        if expires_at <= self._now():
            return runtime
        state, reasons, source, verified_at, verification_expires_at = (
            self._available_provider_state(
                row,
                runtime.economics,
                "explicit_probe",
                persisted.verified_at,
                persisted.verification_expires_at,
            )
        )
        return replace(
            runtime,
            state=state,
            reasons=ReasonCodes(reasons),
            verification_source=source,
            verified_at=verified_at,
            verification_expires_at=verification_expires_at,
            provenance=(*runtime.provenance, "explicit-access-probe"),
        )

    def _provider_state(
        self,
        row: ProviderInventoryRow,
        model: str,
        provenance: str | None,
        details: dict[str, Any],
        economics: AccessEconomics,
    ) -> tuple[str, list[str], str | None, str | None, str | None]:
        if not row.authenticated:
            return (
                "configured_unverified",
                ["credentials_not_authenticated"],
                None,
                None,
                None,
            )
        if provenance == "authenticated_live":
            valid = (
                row.live_attempt_status == "succeeded"
                and bool(details.get("endpoint_identity"))
                and details.get("endpoint_identity") == row.endpoint_identity
                and bool(details.get("auth_identity"))
                and details.get("auth_identity") == row.auth_identity
                and bool(details.get("observed_at"))
            )
            if not valid:
                return (
                    "configured_unverified",
                    ["invalid_model_provenance_details"],
                    None,
                    None,
                    None,
                )
            verified_at = str(details["observed_at"])
            expires_at = _iso(self._now() + timedelta(seconds=VERIFICATION_EVIDENCE_TTL_SECONDS))
            return self._available_provider_state(
                row,
                economics,
                provenance,
                verified_at,
                expires_at,
            )
        if provenance == "validated_contract":
            contract_id = details.get("contract_id")
            contract_version = details.get("contract_version")
            contract = None
            if (
                isinstance(contract_id, str)
                and contract_id.strip()
                and isinstance(contract_version, int)
                and not isinstance(contract_version, bool)
            ):
                contract = VALIDATED_ACCESS_CONTRACT_REGISTRY.get(
                    (contract_id.strip(), contract_version)
                )
            valid = contract is not None and contract.matches(row, model)
            if not valid:
                return (
                    "configured_unverified",
                    ["invalid_model_provenance_details"],
                    None,
                    None,
                    None,
                )
            verified_at = row.observed_at
            expires_at = _iso(self._now() + timedelta(seconds=VERIFICATION_EVIDENCE_TTL_SECONDS))
            return self._available_provider_state(
                row,
                economics,
                provenance,
                verified_at,
                expires_at,
            )
        if provenance is None:
            return (
                "configured_unverified",
                ["missing_model_provenance"],
                None,
                None,
                None,
            )
        return (
            "configured_unverified",
            ["model_access_not_live_verified"],
            None,
            None,
            None,
        )

    def _available_provider_state(
        self,
        row: ProviderInventoryRow,
        economics: AccessEconomics,
        verification_source: str,
        verified_at: str,
        expires_at: str,
    ) -> tuple[str, list[str], str | None, str | None, str | None]:
        cooldown_until = row.cooldown_until or economics.cooldown_until
        if cooldown_until and self._cooldown_is_active(cooldown_until):
            return (
                "temporarily_unavailable",
                ["provider_cooldown"],
                verification_source,
                verified_at,
                expires_at,
            )
        throttle = str(economics.throttle_state or "").strip().lower()
        if throttle in {"cooldown", "exhausted", "depleted", "rate_limited"}:
            return (
                "temporarily_unavailable",
                ["provider_cooldown"],
                verification_source,
                verified_at,
                expires_at,
            )
        if economics.billing_kind == "subscription":
            subscription_state = str(
                economics.subscription_state or ""
            ).strip().lower()
            remaining = economics.subscription_quota_remaining
            if subscription_state in {"exhausted", "depleted"} or (
                remaining is not None and float(remaining) <= 0
            ):
                return (
                    "temporarily_unavailable",
                    ["subscription_quota_exhausted"],
                    verification_source,
                    verified_at,
                    expires_at,
                )
            if remaining is None:
                return (
                    "verified",
                    ["subscription_quota_unknown"],
                    verification_source,
                    verified_at,
                    expires_at,
                )
        return "verified", [], verification_source, verified_at, expires_at

    def _cooldown_is_active(self, value: str) -> bool:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            parsed = parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
        except (TypeError, ValueError):
            return True
        return parsed > self._now()

    def _local_runtime(
        self,
        row: LocalInventoryRow,
        revision: str,
    ) -> ExecutableRuntime | None:
        if row.capabilities.get("supports_tools") is False:
            return None
        reasons: list[str] = []
        if row.capabilities.get("local_evidence_supported") is False:
            reasons.append("local_evidence_backend_unsupported")
        elif not row.reachable:
            reasons.append("local_backend_unreachable")
        elif not row.installed:
            reasons.append("local_model_not_installed")
        elif not row.backend_identity:
            reasons.append("local_backend_identity_missing")
        elif not row.open_weights:
            reasons.append("open_weights_unproven")
        elif not row.license_id:
            reasons.append("license_unproven")
        elif (
            self.policy is not None
            and self.policy.allowed_licenses
            and row.license_id not in self.policy.allowed_licenses
        ):
            reasons.append("license_not_allowed")
        else:
            hardware_proven = bool(row.loaded_healthy or row.hardware_compatible is True)
            if not hardware_proven and all(
                value is not None
                for value in (
                    row.model_size_bytes,
                    row.available_ram_bytes,
                    row.available_vram_bytes,
                )
            ):
                available = int(row.available_ram_bytes or 0) + int(
                    row.available_vram_bytes or 0
                )
                hardware_proven = int(row.model_size_bytes or 0) <= available
            if not hardware_proven:
                reasons.append("hardware_compatibility_unproven")

        verified_at: str | None = None
        verification_expires_at: str | None = None
        if not reasons:
            try:
                observed_at = datetime.fromisoformat(
                    row.observed_at.replace("Z", "+00:00")
                )
                observed_at = observed_at.replace(
                    tzinfo=observed_at.tzinfo or UTC
                ).astimezone(UTC)
            except (TypeError, ValueError):
                reasons.append("local_evidence_timestamp_invalid")
            else:
                verified_at = _iso(observed_at)
                verification_expires_at = _iso(
                    observed_at + timedelta(seconds=LOCAL_EVIDENCE_TTL_SECONDS)
                )

        state = "ineligible" if reasons else "verified"
        key = RuntimeKey(
            provider=row.provider,
            model=row.model,
            auth_identity=f"local:{row.backend_identity}",
            credential_pool_identity="",
            endpoint_identity=f"local-backend:{row.backend_identity}",
            api_mode=row.api_mode,
            local_backend=row.backend_identity,
            inventory_revision=revision,
        )
        reasoning_support = resolve_reasoning_support(
            provider="lmstudio" if "lmstudio" in row.backend_identity else row.provider,
            model=row.model,
            api_mode=row.api_mode,
            metadata=dict(row.capabilities),
        )
        return ExecutableRuntime(
            key=key,
            resolver_name=row.resolver_name,
            state=state,
            reasons=ReasonCodes(reasons),
            economics=row.economics,
            reasoning_support=reasoning_support,
            verification_source=None if reasons else "installed_local",
            verified_at=verified_at,
            verification_expires_at=verification_expires_at,
            provenance=("installed-local", "backend-inspection"),
            observed_at=row.observed_at,
            capabilities={
                **dict(row.capabilities),
                "open_weights": row.open_weights,
                "license_id": row.license_id,
                "hardware_compatible": bool(
                    row.loaded_healthy or row.hardware_compatible is True
                ),
            },
        )

    @staticmethod
    def _mark_ambiguous_access_paths(
        runtimes: list[ExecutableRuntime],
    ) -> list[ExecutableRuntime]:
        by_address: dict[tuple[str, str, str], list[int]] = {}
        by_runtime_identity: dict[str, list[int]] = {}
        for index, runtime in enumerate(runtimes):
            address = (
                runtime.key.provider,
                runtime.key.model,
                runtime.resolver_name,
            )
            by_address.setdefault(address, []).append(index)
            by_runtime_identity.setdefault(runtime.key.stable_id(), []).append(index)
        ambiguous = {
            index
            for indexes in (*by_address.values(), *by_runtime_identity.values())
            if len(indexes) > 1
            for index in indexes
        }
        if not ambiguous:
            return runtimes
        result = list(runtimes)
        for index in ambiguous:
            result[index] = replace(
                result[index],
                state="ineligible",
                reasons=ReasonCodes(("ambiguous_access_path",)),
                verification_source=None,
                verified_at=None,
                verification_expires_at=None,
            )
        return result

    def _persist_snapshot(self, snapshot: InventorySnapshot) -> None:
        if self.store is None:
            return
        observations: dict[str, RuntimeObservation] = {}
        for runtime in sorted(snapshot.runtimes, key=lambda item: item.resolver_name):
            runtime_id = runtime.key.stable_id()
            existing = observations.get(runtime_id)
            if existing is not None:
                if not (
                    existing.state == "ineligible"
                    and "ambiguous_access_path" in existing.reasons
                    and runtime.state == "ineligible"
                    and "ambiguous_access_path" in runtime.reasons
                ):
                    raise VerificationPreconditionChanged(
                        "inventory contains a non-ambiguous duplicate runtime"
                    )
                continue
            observations[runtime_id] = runtime.to_observation()
        self.store.write_inventory_snapshot(
            snapshot.revision,
            list(observations.values()),
            created_at=snapshot.observed_at,
        )

    def _require_probe_policy(self) -> PolicyEnvelope:
        if self.policy is None or not self.policy.allow_paid_access_probes:
            raise VerificationNotAllowed("paid access probes are disabled by policy")
        return self.policy

    def _runtime_by_id(self, runtime_id: str) -> ExecutableRuntime:
        if self._snapshot is None:
            raise VerificationNotAllowed("refresh inventory before verification")
        matches = [
            runtime
            for runtime in self._snapshot.runtimes
            if runtime.key.stable_id() == runtime_id
        ]
        if len(matches) != 1:
            raise VerificationNotAllowed(
                "verification requires one exact current runtime stable ID"
            )
        runtime = matches[0]
        if runtime.state != "configured_unverified" or runtime.key.local_backend:
            raise VerificationNotAllowed(
                "only configured-unverified non-local runtimes can be probed"
            )
        return runtime

    def _worst_case(
        self,
        runtime: ExecutableRuntime,
        maximum_input_tokens: int,
    ) -> tuple[float, str | None]:
        economics = runtime.economics
        try:
            observed_at = datetime.fromisoformat(
                economics.observed_at.replace("Z", "+00:00")
            )
            observed_at = observed_at.replace(
                tzinfo=observed_at.tzinfo or UTC
            ).astimezone(UTC)
        except (TypeError, ValueError) as error:
            raise VerificationEconomicsUnavailable(
                "verification requires a fresh economics observation"
            ) from error
        now = self._now()
        evidence_ttl_seconds = (
            economics.evidence_ttl_seconds
            or VERIFICATION_ECONOMICS_TTL_SECONDS
        )
        if (
            observed_at > now + timedelta(
                seconds=VERIFICATION_ECONOMICS_CLOCK_SKEW_SECONDS
            )
            or now - observed_at
            > timedelta(seconds=evidence_ttl_seconds)
        ):
            raise VerificationEconomicsUnavailable(
                "verification requires a fresh economics observation"
            )
        if economics.billing_kind == "metered":
            input_price = economics.metered_input_usd_per_million_tokens
            output_price = economics.metered_output_usd_per_million_tokens
            if input_price is None or output_price is None:
                raise VerificationEconomicsUnavailable(
                    "metered verification requires finite input/output pricing"
                )
            raw = (
                maximum_input_tokens * input_price
                + VERIFICATION_MAXIMUM_OUTPUT_TOKENS * output_price
            ) / 1_000_000
            quota_unit = None
        elif economics.billing_kind == "subscription":
            raw = economics.effective_marginal_cost_usd_per_task
            quota_unit = economics.subscription_quota_unit
            state = str(economics.subscription_state or "").lower()
            remaining = economics.subscription_quota_remaining
            if (
                raw is None
                or not quota_unit
                or state != "active"
                or remaining is None
                or not math.isfinite(float(remaining))
                or float(remaining) <= 0
            ):
                raise VerificationEconomicsUnavailable(
                    "verification requires known active non-exhausted subscription quota"
                )
        else:
            raise VerificationEconomicsUnavailable(
                "installed local runtimes do not use paid access probes"
            )
        if not math.isfinite(float(raw)) or float(raw) < 0:
            raise VerificationEconomicsUnavailable("verification cost is unbounded")
        worst = float(raw) * (1 + VERIFICATION_COST_RESERVE_FRACTION)
        return max(worst, 1e-12), quota_unit

    def _authority_revision(self, policy: PolicyEnvelope) -> str:
        return _canonical_hash(policy.model_dump(mode="json"))

    def preview_verification(self, runtime_id: str) -> VerificationPreview:
        policy = self._require_probe_policy()
        runtime = self._runtime_by_id(runtime_id)
        resolved = self.adapter.resolve(runtime.key)
        ensure_runtime_match(runtime.key, resolved.runtime_key)
        capability = resolved.probe_capability
        if capability is None:
            raise VerificationNotAllowed(
                resolved.probe_unavailable_reason
                or "verification_execution_shape_unsupported"
            )
        resolved_runtime_precondition = _resolved_runtime_precondition(resolved)
        worst_case, quota_unit = self._worst_case(
            runtime,
            capability.maximum_input_tokens,
        )
        if self.store is None:
            raise VerificationNotAllowed("verification requires the routing store")
        try:
            budget = self.store.daily_budget(
                VERIFICATION_BUDGET_BUCKET,
                self._now().date(),
            )
            if (
                budget.committed_usd + worst_case
                > policy.max_routing_overhead_usd_per_day + 1e-12
            ):
                raise VerificationEconomicsUnavailable(
                    "routing-overhead budget is unavailable"
                )
        except BudgetExceeded as error:
            raise VerificationEconomicsUnavailable(str(error)) from error

        expires_at = _iso(
            self._now() + timedelta(seconds=VERIFICATION_PREVIEW_TTL_SECONDS)
        )
        authority_revision = self._authority_revision(policy)
        budget_day = self._now().date().isoformat()
        budget_revision = self.store.budget_ledger_revision(
            VERIFICATION_BUDGET_BUCKET,
            budget_day,
        )
        sequence = self.store.verification_attempt_sequence(runtime_id)
        payload = {
            "command": "verify-runtime",
            "runtime_id": runtime_id,
            "resolved_runtime_precondition": resolved_runtime_precondition,
            "executor_id": capability.executor_id,
            "executor_version": capability.executor_version,
            "execution_shape_fingerprint": (
                capability.execution_shape_fingerprint
            ),
            "maximum_input_tokens": capability.maximum_input_tokens,
            "protocol_overhead_tokens": capability.protocol_overhead_tokens,
            "maximum_output_tokens": capability.maximum_output_tokens,
            "authority_revision": authority_revision,
            "inventory_revision": self._snapshot.revision,
            "inventory_contract_hash": _inventory_contract_hash(self._snapshot),
            "pricing_source": runtime.economics.source_id,
            "pricing": runtime.economics.model_dump(mode="json"),
            "worst_case_cost_usd": worst_case,
            "quota_unit": quota_unit,
            "budget_day": budget_day,
            "budget_ledger_revision": budget_revision,
            "expires_at": expires_at,
            "prior_attempt_sequence": sequence,
        }
        preview_hash = _canonical_hash(payload)
        preview = VerificationPreview(
            runtime_id=runtime_id,
            precondition_hash=preview_hash,
            resolved_runtime_precondition=resolved_runtime_precondition,
            executor_id=capability.executor_id,
            executor_version=capability.executor_version,
            execution_shape_fingerprint=(
                capability.execution_shape_fingerprint
            ),
            maximum_input_tokens=capability.maximum_input_tokens,
            protocol_overhead_tokens=capability.protocol_overhead_tokens,
            maximum_output_tokens=capability.maximum_output_tokens,
            worst_case_cost_usd=worst_case,
            quota_unit=quota_unit,
            expires_at=expires_at,
            inventory_revision=self._snapshot.revision,
            inventory_contract_hash=payload["inventory_contract_hash"],
            authority_revision=authority_revision,
            budget_day=budget_day,
            budget_ledger_revision=budget_revision,
            prior_attempt_sequence=sequence,
        )
        self._previews[preview_hash] = preview
        self.store.write_verification_preview(
            precondition_hash=preview_hash,
            document=payload,
            expires_at=expires_at,
            created_at=_iso(self._now()),
        )
        return preview

    def apply_verification(
        self,
        precondition_hash: str,
        acknowledge_billable: bool,
    ) -> ExecutableRuntime:
        if not acknowledge_billable:
            raise VerificationApprovalRequired(
                "explicit acknowledge_billable=True is required"
            )
        if self.store is None:
            raise VerificationNotAllowed("verification requires the routing store")
        preview = self._previews.get(precondition_hash)
        if preview is None:
            if self.store.has_verification_attempt(precondition_hash):
                raise VerificationReplay("verification preview was already consumed")
            stored = self.store.read_verification_preview(precondition_hash)
            if stored is None:
                raise VerificationPreconditionChanged("unknown verification preview")
            document = dict(stored.document)
            try:
                preview = VerificationPreview(
                    runtime_id=str(document["runtime_id"]),
                    precondition_hash=precondition_hash,
                    resolved_runtime_precondition=str(
                        document["resolved_runtime_precondition"]
                    ),
                    executor_id=str(document["executor_id"]),
                    executor_version=str(document["executor_version"]),
                    execution_shape_fingerprint=str(
                        document["execution_shape_fingerprint"]
                    ),
                    maximum_input_tokens=int(document["maximum_input_tokens"]),
                    protocol_overhead_tokens=int(
                        document["protocol_overhead_tokens"]
                    ),
                    maximum_output_tokens=int(document["maximum_output_tokens"]),
                    worst_case_cost_usd=float(document["worst_case_cost_usd"]),
                    quota_unit=(
                        None
                        if document.get("quota_unit") is None
                        else str(document["quota_unit"])
                    ),
                    expires_at=str(document["expires_at"]),
                    inventory_revision=str(document["inventory_revision"]),
                    inventory_contract_hash=str(
                        document["inventory_contract_hash"]
                    ),
                    authority_revision=str(document["authority_revision"]),
                    budget_day=str(document["budget_day"]),
                    budget_ledger_revision=str(
                        document["budget_ledger_revision"]
                    ),
                    prior_attempt_sequence=int(
                        document["prior_attempt_sequence"]
                    ),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise VerificationPreconditionChanged(
                    "stored verification preview is invalid"
                ) from error
            self._previews[precondition_hash] = preview
        policy = self._require_probe_policy()
        runtime = self._runtime_by_id(preview.runtime_id)
        if self._snapshot is None or self._snapshot.revision != preview.inventory_revision:
            raise VerificationPreconditionChanged("inventory revision changed")
        if self._authority_revision(policy) != preview.authority_revision:
            raise VerificationPreconditionChanged("policy revision changed")
        if self._now() > datetime.fromisoformat(
            preview.expires_at.replace("Z", "+00:00")
        ):
            raise VerificationPreconditionChanged("verification preview expired")
        resolved = self.adapter.resolve(runtime.key)
        ensure_runtime_match(runtime.key, resolved.runtime_key)
        if (
            _resolved_runtime_precondition(resolved)
            != preview.resolved_runtime_precondition
        ):
            raise VerificationPreconditionChanged(
                "resolved runtime precondition changed"
            )
        try:
            attempt = self.store.begin_verification_attempt(
                precondition_hash=precondition_hash,
                runtime_id=preview.runtime_id,
                expected_attempt_sequence=preview.prior_attempt_sequence,
                expected_budget_day=preview.budget_day,
                expected_budget_ledger_revision=preview.budget_ledger_revision,
                authority_id=preview.authority_revision,
                inventory_revision=preview.inventory_revision,
                worst_case_usd=preview.worst_case_cost_usd,
                daily_limit_usd=policy.max_routing_overhead_usd_per_day,
                bucket=VERIFICATION_BUDGET_BUCKET,
                now=self._now(),
            )
        except Exception as error:
            if self.store.has_verification_attempt(precondition_hash):
                raise VerificationReplay("verification preview was already consumed") from error
            if isinstance(error, VerificationAttemptConflict):
                raise VerificationPreconditionChanged(
                    str(error)
                ) from error
            raise

        request = VerificationRequest(
            prompt=VERIFICATION_PROMPT,
            maximum_input_tokens=preview.maximum_input_tokens,
            maximum_output_tokens=preview.maximum_output_tokens,
            temperature=0,
            executor_id=preview.executor_id,
            executor_version=preview.executor_version,
            execution_shape_fingerprint=preview.execution_shape_fingerprint,
            tools=(),
            persist=False,
        )
        result: AccessVerification | Any | None = None
        try:
            result = self.adapter.verify_access(resolved, request)
            self._validate_verification(runtime, result, preview)
        except Exception as error:
            if isinstance(error, VerificationOutcomeUncertain):
                (
                    input_tokens,
                    output_tokens,
                    evidenced_cost_usd,
                    response_hash,
                ) = self._reconciliation_evidence(result)
                actual_cost_usd = max(
                    preview.worst_case_cost_usd,
                    evidenced_cost_usd,
                )
            else:
                (
                    input_tokens,
                    output_tokens,
                    actual_cost_usd,
                    response_hash,
                ) = self._reconciliation_evidence(result)
            self.store.complete_verification_attempt(
                precondition_hash,
                status="failed",
                reason_code=self._sanitized_reason(error),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                actual_cost_usd=actual_cost_usd,
                response_hash=response_hash,
                now=self._now(),
            )
            self._previews.pop(precondition_hash, None)
            if isinstance(error, VerificationError):
                raise
            raise VerificationFailed(self._sanitized_reason(error)) from error

        self.store.complete_verification_attempt(
            precondition_hash,
            status="succeeded",
            reason_code="verified",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            actual_cost_usd=result.actual_cost_usd,
            response_hash=result.response_hash,
            now=self._now(),
        )
        del attempt
        verified_at = _iso(self._now())
        updated = replace(
            runtime,
            state="verified",
            reasons=ReasonCodes(()),
            verification_source="explicit_probe",
            verified_at=verified_at,
            verification_expires_at=_iso(
                self._now()
                + timedelta(seconds=VERIFICATION_EVIDENCE_TTL_SECONDS)
            ),
            provenance=(*runtime.provenance, "explicit-access-probe"),
            observed_at=verified_at,
        )
        self._replace_runtime(updated)
        self._previews.pop(precondition_hash, None)
        return updated

    @staticmethod
    def _validate_verification(
        runtime: ExecutableRuntime,
        result: AccessVerification,
        preview: VerificationPreview,
    ) -> None:
        ensure_runtime_match(runtime.key, result.runtime_key)
        if result.sentinel.strip() != VERIFICATION_SENTINEL:
            raise VerificationFailed("verification_sentinel_mismatch")
        if not result.response_model or result.response_model != runtime.key.model:
            raise VerificationFailed("verification_response_model_mismatch")
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in (result.input_tokens, result.output_tokens)
        ):
            raise VerificationFailed("verification_usage_invalid")
        if (
            result.input_tokens > preview.maximum_input_tokens
            or result.output_tokens > preview.maximum_output_tokens
        ):
            raise VerificationOutcomeUncertain(
                "verification_response_usage_uncertain"
            )
        if (
            not isinstance(result.actual_cost_usd, (int, float))
            or isinstance(result.actual_cost_usd, bool)
            or not math.isfinite(result.actual_cost_usd)
            or result.actual_cost_usd < 0
        ):
            raise VerificationFailed("verification_cost_invalid")
        if not isinstance(result.response_hash, str) or not result.response_hash:
            raise VerificationFailed("verification_response_hash_missing")

    @staticmethod
    def _reconciliation_evidence(
        result: AccessVerification | Any | None,
    ) -> tuple[int, int, float, str]:
        if result is None:
            return 0, 0, 0.0, ""
        input_tokens = getattr(result, "input_tokens", None)
        output_tokens = getattr(result, "output_tokens", None)
        actual_cost = getattr(result, "actual_cost_usd", None)
        valid_usage = all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            for value in (input_tokens, output_tokens)
        )
        valid_cost = (
            isinstance(actual_cost, (int, float))
            and not isinstance(actual_cost, bool)
            and math.isfinite(float(actual_cost))
            and float(actual_cost) >= 0
        )
        if not valid_usage or not valid_cost:
            return 0, 0, 0.0, ""
        response_hash = getattr(result, "response_hash", "")
        return (
            int(input_tokens),
            int(output_tokens),
            float(actual_cost),
            str(response_hash or "")[:128],
        )

    @staticmethod
    def _sanitized_reason(error: Exception) -> str:
        if isinstance(error, VerificationOutcomeUncertain):
            text = str(error)
            if text in _UNCERTAIN_VERIFICATION_REASONS:
                return text
            return "verification_request_outcome_uncertain"
        if isinstance(error, VerificationFailed):
            text = str(error)
            if text and all(character.isalnum() or character in "_-" for character in text):
                return text[:96]
        return type(error).__name__.lower()[:96]

    def _replace_runtime(self, updated: ExecutableRuntime) -> None:
        if self._snapshot is None:
            raise VerificationPreconditionChanged("inventory snapshot disappeared")
        runtimes = [
            updated
            if runtime.key.stable_id() == updated.key.stable_id()
            else runtime
            for runtime in self._snapshot.runtimes
        ]
        revision = _canonical_hash(
            {
                "parent": self._snapshot.revision,
                "runtime": updated.key.stable_id(),
                "verification_source": updated.verification_source,
                "verified_at": updated.verified_at,
            }
        )
        rekeyed = [
            replace(
                runtime,
                key=runtime.key.model_copy(
                    update={"inventory_revision": revision}
                ),
            )
            for runtime in runtimes
        ]
        self._snapshot = InventorySnapshot(
            revision=revision,
            runtimes=rekeyed,
            observed_at=updated.observed_at,
        )
        self._persist_snapshot(self._snapshot)


__all__ = [
    "ExecutableRuntime",
    "InventoryService",
    "InventorySnapshot",
    "VerificationApprovalRequired",
    "VerificationEconomicsUnavailable",
    "VerificationError",
    "VerificationFailed",
    "VerificationNotAllowed",
    "VerificationPreconditionChanged",
    "VerificationPreview",
    "VerificationReplay",
]
