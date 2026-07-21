"""Profile-local durable state for the auto-routing plugin.

The store keeps only structured, content-free routing records.  SQLite WAL
provides concurrent readers, while bounded ``BEGIN IMMEDIATE`` transactions
serialize immutable publication, active-revision CAS updates, and budget
reservations across CLI, gateway, desktop, and worker processes.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import itertools
import json
import math
import os
import random
import re
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import BaseModel

from hermes_cli.sqlite_util import add_column_if_missing
from hermes_constants import get_hermes_home
from hermes_state import apply_wal_with_fallback

from .adaptation import operation_identity_hash, static_adaptive_revision_id
from .models import (
    MAX_DECISION_CANDIDATES,
    MAX_TASK_INDEX,
    AdaptiveCanaryAssignment,
    AdaptiveLifecycleEvent,
    AdaptiveLifecyclePhase,
    AdaptiveProfileControl,
    AdaptiveProfileRevision,
    AdaptiveRevision,
    CatalogApplicability,
    CatalogEvidence,
    DecisionCandidate,
    EvidenceEvent,
    ManagementCanaryAssignment,
    ManagementConfigReceipt,
    ManagementControl,
    ManagementLifecycleFinalization,
    ManagementLifecycleEvent,
    ManagementProfileState,
    ManagementRevision,
    NonEmptyString,
    OptimizerLease,
    RoutingDecision,
    RuntimeObservation,
    StoredCatalogRecord,
    StrictNonNegativeInt,
    candidate_id_for,
)

SCHEMA_VERSION = "9"
BUSY_TIMEOUT_MS = 5_000
BUSY_MAX_RETRIES = 15
BUSY_RETRY_MIN_SECONDS = 0.020
BUSY_RETRY_MAX_SECONDS = 0.150
# Post-turn evidence is strictly observer-only: it must never delay a completed
# user response behind the normal durable routing/control-plane contention
# policy. This budget is applied only while acquiring/committing an observer
# evidence transaction; all other store writes retain the defaults above.
EVIDENCE_OBSERVER_BUSY_TIMEOUT_MS = 100
EVIDENCE_OBSERVER_MAX_RETRIES = 0
_EMPTY_PROFILE_STATE_TIMESTAMP = "1970-01-01T00:00:00.000000Z"
_LEGAL_EXPERIMENT_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType({
    "eligible": frozenset({"validated"}),
    "validated": frozenset({"canary"}),
    "canary": frozenset({"promoted", "rejected"}),
    "promoted": frozenset({"cooldown"}),
    "rejected": frozenset({"cooldown"}),
    "cooldown": frozenset({"eligible"}),
    "rolled_back": frozenset({"eligible"}),
})
_DURABLE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/@+\-]{1,256}$")
_DURABLE_OPERATION_KEY = re.compile(r"^[A-Za-z0-9_.:/@+\-]{1,383}$")
_CANONICAL_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


def _require_durable_identifier(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _DURABLE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded content-free identifier")
    try:
        _assert_content_free({field_name: value}, writer="decision")
    except UnsafeStoredContent as error:
        raise ValueError(
            f"{field_name} must be a bounded content-free identifier"
        ) from error
    return value


def _require_canonical_timestamp(
    value: Any,
    *,
    field_name: str,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _CANONICAL_TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical UTC ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"{field_name} must be a canonical UTC ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be a canonical UTC ISO-8601 timestamp")
    return value


class StoreError(RuntimeError):
    """Base error for durable auto-routing state."""


class StoreBusy(StoreError):
    """A bounded SQLite write wait expired."""


class ImmutableRecordConflict(StoreError):
    """An immutable identifier already names different content."""


class RevisionChecksumError(StoreError):
    """Stored immutable content failed its checksum or canonicality check."""

    def __init__(self, revision_id: str):
        self.revision_id = revision_id
        super().__init__(f"stored revision checksum mismatch: {revision_id}")


class RevisionConflict(StoreError):
    """The active adaptive revision changed before publication."""

    def __init__(
        self,
        expected_active_id: str | None,
        actual_active_id: str | None,
    ) -> None:
        self.expected_active_id = expected_active_id
        self.actual_active_id = actual_active_id
        super().__init__(
            "active revision conflict: "
            f"expected {expected_active_id!r}, found {actual_active_id!r}"
        )


class ProfileStateConflict(StoreError):
    """A profile-local control mutation used a stale shared generation."""

    def __init__(self, expected_generation: int, actual_generation: int) -> None:
        self.expected_generation = expected_generation
        self.actual_generation = actual_generation
        super().__init__(
            "adaptive profile state conflict: "
            f"expected generation {expected_generation}, found {actual_generation}"
        )


class ProfileFrozen(StoreError):
    """A frozen profile rejected an automatic adaptive mutation."""


class InvalidLifecycleTransition(StoreError):
    """A profile experiment attempted a transition outside the finite graph."""


class UnsafeStoredContent(StoreError):
    """A generic JSON record attempted to persist raw/private content."""


class UnsupportedSchemaVersion(StoreError):
    """The database was created by a newer or unreadable schema version."""


class BudgetExceeded(StoreError):
    """A worst-case reservation would exceed its daily budget."""

    def __init__(
        self,
        *,
        bucket: str,
        budget_day: date,
        committed_usd: float,
        requested_usd: float,
        daily_limit_usd: float,
    ) -> None:
        self.bucket = bucket
        self.budget_day = budget_day
        self.committed_usd = committed_usd
        self.requested_usd = requested_usd
        self.daily_limit_usd = daily_limit_usd
        super().__init__(
            f"daily budget exceeded for {bucket!r} on {budget_day.isoformat()}: "
            f"{committed_usd} + {requested_usd} > {daily_limit_usd}"
        )


class ReservationNotFound(StoreError):
    """A budget reservation ID does not exist."""


class ReservationConflict(StoreError):
    """A reconciled reservation was replayed with a different actual cost."""


class VerificationAttemptConflict(StoreError):
    """A one-shot access-verification precondition was replayed or changed."""


class RuntimeRoutingPending(StoreError):
    """Another live process still owns an incomplete route decision."""


def _candidate_bundle_checksum(
    candidates: Sequence[DecisionCandidate],
) -> str:
    document = json.dumps(
        [candidate.model_dump(mode="json") for candidate in candidates],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(document.encode()).hexdigest()


def _validate_decision_candidate_coherence(
    decision: RoutingDecision,
    candidates: Sequence[DecisionCandidate],
) -> None:
    """Require one candidate bundle to substantiate its decision summary."""
    if len(candidates) > MAX_DECISION_CANDIDATES:
        raise ValueError(f"decision candidates cannot exceed {MAX_DECISION_CANDIDATES}")
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise ValueError("decision candidates contain duplicate candidate_id")

    eligible_runtime_ids = {
        candidate.runtime_id for candidate in candidates if candidate.eligible
    }
    rejected_runtime_ids = {
        candidate.runtime_id for candidate in candidates if not candidate.eligible
    }
    if not set(decision.eligible_candidates).issubset(eligible_runtime_ids):
        raise ValueError(
            "eligible candidate references must exist in the candidate bundle"
        )
    if not {
        runtime_id for runtime_id, _reasons in decision.rejected_candidates
    }.issubset(rejected_runtime_ids):
        raise ValueError(
            "rejected candidate references must exist in the candidate bundle"
        )
    if not {runtime_id for runtime_id, _score in decision.final_scores}.issubset(
        eligible_runtime_ids
    ):
        raise ValueError(
            "final score references must exist in the eligible candidate bundle"
        )

    final_scores = dict(decision.final_scores)
    for runtime_id, score in final_scores.items():
        if not any(
            candidate.eligible
            and candidate.runtime_id == runtime_id
            and candidate.final_score == score
            for candidate in candidates
        ):
            raise ValueError("final score must match an eligible candidate evaluation")
    for runtime_id, reasons in decision.rejected_candidates:
        if not any(
            not candidate.eligible
            and candidate.runtime_id == runtime_id
            and candidate.reason_codes == reasons
            for candidate in candidates
        ):
            raise ValueError(
                "rejection reasons must match an ineligible candidate evaluation"
            )

    if decision.selection_reason in {
        "highest_eligible_score",
        "pinned_profile",
        "preferred_profile",
        "rule",
    }:
        selected_runtime_id = decision.selected_runtime.stable_id()
        selected_score = final_scores.get(selected_runtime_id)
        if selected_score is None or not any(
            candidate.eligible
            and candidate.profile_id == decision.selected_profile_id
            and candidate.runtime_id == selected_runtime_id
            and candidate.final_score == selected_score
            for candidate in candidates
        ):
            raise ValueError(
                "selected profile/runtime must match its eligible scored candidate"
            )


@dataclass(frozen=True, slots=True)
class RouteEpoch:
    route_epoch_id: str
    session_id: str
    decision_id: str
    epoch_number: int
    runtime_id: str
    reason_code: str
    started_at: str
    ended_at: str | None = None
    provider_started: bool = False
    api_request_id: str | None = None
    provider_started_at: str | None = None

    def __post_init__(self) -> None:
        _require_durable_identifier(self.session_id, field_name="session_id")
        _require_durable_identifier(self.decision_id, field_name="decision_id")
        if not re.fullmatch(r"[0-9a-f]{64}", self.route_epoch_id):
            _require_durable_identifier(
                self.route_epoch_id,
                field_name="route_epoch_id",
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.runtime_id):
            raise ValueError("runtime_id must be a lowercase SHA-256 digest")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.reason_code):
            raise ValueError("route epoch reason_code must be a bounded code")
        if (
            isinstance(self.epoch_number, bool)
            or not isinstance(
                self.epoch_number,
                int,
            )
            or self.epoch_number < 0
        ):
            raise ValueError("epoch_number cannot be negative")
        if not isinstance(self.provider_started, bool):
            raise ValueError("provider_started must be a bool")
        _require_canonical_timestamp(self.started_at, field_name="started_at")
        _require_canonical_timestamp(
            self.ended_at,
            field_name="ended_at",
            optional=True,
        )
        if self.api_request_id is not None:
            _require_durable_identifier(
                self.api_request_id,
                field_name="api_request_id",
            )
        _require_canonical_timestamp(
            self.provider_started_at,
            field_name="provider_started_at",
            optional=True,
        )
        marker_values = (self.api_request_id, self.provider_started_at)
        if (
            self.provider_started
            and any(value is None for value in marker_values)
            or not self.provider_started
            and any(value is not None for value in marker_values)
        ):
            raise ValueError("provider marker fields must be present together")


@dataclass(frozen=True, slots=True)
class SessionRouteBinding:
    session_id: str
    binding_kind: Literal["routed", "manual"]
    projection_mode: Literal["shadow", "active", "inherit", "manual"]
    decision_id: str | None
    runtime_id: str
    manual_pin_source: str | None
    current_epoch: int
    continuation_root: str | None
    parent_session_id: str | None
    continuation_reason: str | None
    created_at: str

    def __post_init__(self) -> None:
        _require_durable_identifier(self.session_id, field_name="session_id")
        if self.binding_kind not in {"routed", "manual"}:
            raise ValueError("binding_kind must be routed or manual")
        if self.projection_mode not in {"shadow", "active", "inherit", "manual"}:
            raise ValueError("projection_mode is invalid")
        routed = self.binding_kind == "routed"
        if routed != (self.decision_id is not None):
            raise ValueError("routed binding must name exactly one decision")
        if routed == (self.manual_pin_source is not None):
            raise ValueError("binding must be exactly routed or manual")
        if routed and self.projection_mode == "manual":
            raise ValueError("routed binding cannot use manual projection mode")
        if not routed and self.projection_mode != "manual":
            raise ValueError("manual binding requires manual projection mode")
        if (
            isinstance(self.current_epoch, bool)
            or not isinstance(
                self.current_epoch,
                int,
            )
            or self.current_epoch < -1
        ):
            raise ValueError("current_epoch cannot be below -1")
        if not routed and self.current_epoch != -1:
            raise ValueError("manual binding cannot carry a route epoch")
        if not re.fullmatch(r"[0-9a-f]{64}", self.runtime_id):
            raise ValueError("runtime_id must be a lowercase SHA-256 digest")
        if self.manual_pin_source is not None and not re.fullmatch(
            r"[a-z][a-z0-9_.:@/-]{0,127}",
            self.manual_pin_source,
        ):
            raise ValueError("manual_pin_source must be a bounded code")
        if self.decision_id is not None:
            _require_durable_identifier(
                self.decision_id,
                field_name="decision_id",
            )
        for field_name, value in (
            ("continuation_root", self.continuation_root),
            ("parent_session_id", self.parent_session_id),
        ):
            if value is not None:
                _require_durable_identifier(value, field_name=field_name)
        lineage = (
            self.continuation_root,
            self.parent_session_id,
            self.continuation_reason,
        )
        if any(value is None for value in lineage) != all(
            value is None for value in lineage
        ):
            raise ValueError("continuation lineage must be present together")
        if self.continuation_reason not in {None, "compression"}:
            raise ValueError("only compression continuation is supported")
        if self.parent_session_id == self.session_id:
            raise ValueError("continuation parent cannot equal the child session")
        if self.continuation_root == self.session_id:
            raise ValueError("continuation root cannot equal the child session")
        _require_canonical_timestamp(self.created_at, field_name="created_at")

    @property
    def authoritative_intent(self) -> tuple[str, str, str | None, str | None]:
        return (
            self.binding_kind,
            self.runtime_id,
            self.decision_id,
            self.manual_pin_source,
        )


@dataclass(frozen=True, slots=True)
class ActivationReceipt:
    receipt_id: str
    authority_id: str
    config_sha: str
    inventory_contract_sha: str
    inventory_revision: str
    adapter_capability_sha: str
    created_at: str

    def __post_init__(self) -> None:
        for field_name in ("receipt_id", "authority_id", "inventory_revision"):
            _require_durable_identifier(
                getattr(self, field_name),
                field_name=field_name,
            )
        for field_name in (
            "config_sha",
            "inventory_contract_sha",
            "adapter_capability_sha",
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", getattr(self, field_name)):
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
        _require_canonical_timestamp(self.created_at, field_name="created_at")


@dataclass(frozen=True, slots=True)
class DecisionOperationClaim:
    operation_key: str
    claim_id: str
    scope: Literal["fresh_session", "delegation"]
    session_id: str
    operation_id: str | None
    task_index: int | None
    facts_hash: str
    owner_pid: int
    owner_start_token: str
    lease_expires_at: float
    status: Literal["claimed", "waiting", "replayed"]
    decision_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_key, str)
            or _DURABLE_OPERATION_KEY.fullmatch(self.operation_key) is None
        ):
            raise ValueError("operation_key must be a bounded content-free identifier")
        if self.scope not in {"fresh_session", "delegation"}:
            raise ValueError("scope must be fresh_session or delegation")
        if self.status not in {"claimed", "waiting", "replayed"}:
            raise ValueError("claim status is invalid")
        _require_durable_identifier(self.session_id, field_name="session_id")
        if self.operation_id is not None:
            _require_durable_identifier(
                self.operation_id,
                field_name="operation_id",
            )
        if isinstance(self.task_index, bool) or (
            self.task_index is not None and not isinstance(self.task_index, int)
        ):
            raise ValueError("task_index must be a strict integer")
        if self.scope == "fresh_session" and (
            self.operation_id is not None or self.task_index is not None
        ):
            raise ValueError("fresh-session claims cannot carry operation identity")
        if self.scope == "delegation" and (
            self.operation_id is None
            or self.task_index is None
            or self.task_index < 0
            or self.task_index > MAX_TASK_INDEX
        ):
            raise ValueError("delegation claims require operation identity")
        if not re.fullmatch(r"[0-9a-f]{64}", self.facts_hash):
            raise ValueError("facts_hash must be a lowercase SHA-256 digest")
        if not re.fullmatch(r"[0-9a-f]{32}", self.claim_id):
            raise ValueError("claim_id must be a lowercase 128-bit nonce")
        if (
            isinstance(self.owner_pid, bool)
            or not isinstance(
                self.owner_pid,
                int,
            )
            or self.owner_pid <= 0
        ):
            raise ValueError("owner_pid must be a positive strict integer")
        _require_durable_identifier(
            self.owner_start_token,
            field_name="owner_start_token",
        )
        if (
            isinstance(self.lease_expires_at, bool)
            or not isinstance(
                self.lease_expires_at,
                (int, float),
            )
            or not math.isfinite(float(self.lease_expires_at))
            or self.lease_expires_at <= 0
        ):
            raise ValueError("lease_expires_at must be finite and positive")
        if self.status == "replayed":
            if self.decision_id is None:
                raise ValueError("replayed claims require decision_id")
            _require_durable_identifier(
                self.decision_id,
                field_name="decision_id",
            )
        elif self.decision_id is not None:
            raise ValueError("incomplete claims cannot carry decision_id")


@dataclass(frozen=True, slots=True)
class DecisionCommit:
    decision: RoutingDecision
    candidates: tuple[DecisionCandidate, ...]
    binding: SessionRouteBinding
    epoch: RouteEpoch | None
    status: Literal["computed", "replayed"] = dataclass_field(compare=False)

    def __post_init__(self) -> None:
        if self.status not in {"computed", "replayed"}:
            raise ValueError("decision commit status is invalid")
        if not isinstance(self.decision, RoutingDecision):
            raise ValueError("decision commit requires a RoutingDecision")
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(candidate, DecisionCandidate)
            for candidate in self.candidates
        ):
            raise ValueError("decision commit candidates must be a typed tuple")
        _validate_decision_candidate_coherence(self.decision, self.candidates)
        if not isinstance(self.binding, SessionRouteBinding):
            raise ValueError("decision commit requires a typed binding")
        if self.binding.session_id != self.decision.session_id or (
            self.binding.decision_id != self.decision.decision_id
        ):
            raise ValueError("decision commit binding does not match its decision")
        expects_epoch = self.binding.current_epoch >= 0
        if (self.epoch is not None) != expects_epoch:
            raise ValueError(
                "decision commit epoch presence does not match its binding"
            )
        if self.epoch is not None:
            if not isinstance(self.epoch, RouteEpoch):
                raise ValueError("decision commit epoch must be typed")
            if (
                self.epoch.session_id != self.decision.session_id
                or self.epoch.decision_id != self.decision.decision_id
                or self.epoch.runtime_id != self.binding.runtime_id
                or self.epoch.epoch_number != self.binding.current_epoch
            ):
                raise ValueError("decision commit epoch does not match its binding")


@dataclass(frozen=True, slots=True)
class EvidenceCommit:
    event: EvidenceEvent
    status: Literal["inserted", "replayed"]

    def __post_init__(self) -> None:
        if not isinstance(self.event, EvidenceEvent):
            raise ValueError("evidence commit requires an EvidenceEvent")
        if self.status not in {"inserted", "replayed"}:
            raise ValueError("evidence commit status is invalid")


@dataclass(frozen=True)
class AuthorityRevision:
    """One immutable, checksummed authority document."""

    authority_id: str
    document_json: str
    checksum: str
    created_at: str

    @property
    def document(self) -> Any:
        return _freeze_json(json.loads(self.document_json))


@dataclass(frozen=True)
class InventorySnapshot:
    """One complete immutable executable-inventory snapshot."""

    snapshot_id: str
    observations: tuple[RuntimeObservation, ...]
    document_json: str
    checksum: str
    created_at: str


@dataclass(frozen=True)
class CatalogSnapshot:
    """One complete immutable provenance-bearing catalog snapshot."""

    snapshot_id: str
    records: tuple[StoredCatalogRecord, ...]
    document_json: str
    checksum: str
    created_at: str

    @property
    def evidence(self) -> tuple[CatalogEvidence, ...]:
        """Compatibility view for callers that do not need applicability."""
        return tuple(record.evidence for record in self.records)


@dataclass(frozen=True)
class BudgetReservation:
    """One pending or reconciled budget-ledger entry."""

    reservation_id: str
    bucket: str
    budget_day: date
    reserved_usd: float
    daily_limit_usd: float
    actual_usd: float | None
    status: Literal["reserved", "reconciled"]
    created_at: str
    reconciled_at: str | None


@dataclass(frozen=True)
class DailyBudget:
    """Aggregated actual and outstanding spend for one bucket/day."""

    bucket: str
    budget_day: date
    spent_usd: float
    reserved_usd: float
    reconciled_count: int
    reservation_count: int

    @property
    def committed_usd(self) -> float:
        return self.spent_usd + self.reserved_usd


@dataclass(frozen=True)
class RuntimeVerificationAttempt:
    """Content-free durable state for one bounded access verification."""

    precondition_hash: str
    runtime_id: str
    authority_id: str
    inventory_revision: str
    budget_reservation_id: str
    status: Literal["reserved", "succeeded", "failed"]
    reason_code: str | None
    input_tokens: int | None
    output_tokens: int | None
    actual_cost_usd: float | None
    response_hash: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class StoredVerificationPreview:
    """One immutable, content-free billable verification authorization preview."""

    precondition_hash: str
    document_json: str
    checksum: str
    expires_at: str
    created_at: str

    @property
    def document(self) -> Any:
        return _freeze_json(json.loads(self.document_json))


_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS authority_revisions (
        authority_id TEXT PRIMARY KEY,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL,
        complete INTEGER NOT NULL CHECK (complete IN (0, 1)) DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory_observations (
        snapshot_id TEXT NOT NULL
            REFERENCES inventory_snapshots(snapshot_id) ON DELETE CASCADE,
        runtime_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        state TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, runtime_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS catalog_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL,
        complete INTEGER NOT NULL CHECK (complete IN (0, 1)) DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS catalog_evidence (
        snapshot_id TEXT NOT NULL
            REFERENCES catalog_snapshots(snapshot_id) ON DELETE CASCADE,
        evidence_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        source_id TEXT NOT NULL,
        model TEXT NOT NULL,
        model_version TEXT NOT NULL,
        domain TEXT NOT NULL,
        metric_name TEXT NOT NULL,
        canonical_provider TEXT NOT NULL DEFAULT '',
        canonical_model TEXT NOT NULL DEFAULT '',
        canonical_version TEXT NOT NULL DEFAULT '',
        runtime_id TEXT,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        retrieved_at TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, evidence_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adaptive_revisions (
        revision_id TEXT PRIMARY KEY,
        authority_id TEXT NOT NULL,
        parent_revision_id TEXT,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        explanation_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        complete INTEGER NOT NULL CHECK (complete IN (0, 1)) DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS active_adaptive_revisions (
        authority_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL REFERENCES adaptive_revisions(revision_id),
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adaptive_profile_revisions (
        revision_id TEXT PRIMARY KEY,
        authority_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        parent_revision_id TEXT,
        overlay_json TEXT NOT NULL,
        explanation_json TEXT NOT NULL,
        lifecycle TEXT NOT NULL CHECK (
            lifecycle IN (
                'eligible', 'validated', 'canary', 'promoted', 'rejected', 'cooldown'
            )
        ),
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL,
        complete INTEGER NOT NULL CHECK (complete IN (0, 1)) DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adaptive_profile_states (
        authority_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        active_revision_id TEXT,
        control_revision_id TEXT,
        challenger_revision_id TEXT,
        experiment_phase TEXT NOT NULL CHECK (
            experiment_phase IN (
                'eligible', 'validated', 'canary', 'promoted', 'rejected', 'cooldown',
                'rolled_back'
            )
        ),
        frozen INTEGER NOT NULL CHECK (frozen IN (0, 1)),
        cooldown_until TEXT,
        rejection_count INTEGER NOT NULL CHECK (rejection_count >= 0),
        generation INTEGER NOT NULL CHECK (generation >= 0),
        updated_at TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        PRIMARY KEY (authority_id, profile_id),
        CHECK (
            (control_revision_id IS NULL AND challenger_revision_id IS NULL)
            OR
            (control_revision_id IS NOT NULL AND challenger_revision_id IS NOT NULL
                AND control_revision_id <> challenger_revision_id)
        ),
        CHECK (
            (experiment_phase = 'cooldown' AND cooldown_until IS NOT NULL)
            OR
            (experiment_phase <> 'cooldown' AND cooldown_until IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adaptive_lifecycle_events (
        event_id TEXT PRIMARY KEY,
        authority_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        revision_id TEXT,
        event_type TEXT NOT NULL CHECK (
            event_type IN (
                'eligible', 'validated', 'canary', 'promoted', 'rejected',
                'cooldown', 'frozen', 'unfrozen', 'rolled_back'
            )
        ),
        reason_code TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adaptive_canary_assignments (
        assignment_id TEXT PRIMARY KEY,
        authority_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        operation_identity_hash TEXT NOT NULL,
        context_bucket_id TEXT NOT NULL,
        control_revision_id TEXT NOT NULL,
        challenger_revision_id TEXT NOT NULL,
        arm TEXT NOT NULL CHECK (arm IN ('control', 'challenger')),
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (authority_id, profile_id, operation_identity_hash),
        CHECK (control_revision_id <> challenger_revision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS adaptive_optimizer_leases (
        authority_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        lease_expires_at TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation > 0),
        updated_at TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        PRIMARY KEY (authority_id, profile_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS management_controls (
        management_authority_id TEXT PRIMARY KEY,
        frozen INTEGER NOT NULL CHECK (frozen IN (0, 1)),
        changes_today INTEGER NOT NULL CHECK (
            changes_today >= 0 AND changes_today <= 10
        ),
        generation INTEGER NOT NULL CHECK (generation >= 0),
        updated_at TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS management_revisions (
        revision_id TEXT PRIMARY KEY,
        preceding_authority_id TEXT NOT NULL,
        resulting_authority_id TEXT NOT NULL,
        management_authority_id TEXT NOT NULL
            REFERENCES management_controls(management_authority_id),
        parent_revision_id TEXT
            REFERENCES management_revisions(revision_id),
        ranking_pack_id TEXT NOT NULL,
        ranking_pack_sha256 TEXT NOT NULL,
        ranking_pack_schema_version TEXT NOT NULL,
        ranking_pack_verified_at TEXT NOT NULL,
        inventory_revision TEXT NOT NULL,
        inventory_fingerprint TEXT NOT NULL,
        management_epoch INTEGER NOT NULL CHECK (management_epoch >= 0),
        action TEXT NOT NULL CHECK (
            action IN (
                'propose_canary', 'fallback_reorder', 'promote',
                'rollback', 'recovery'
            )
        ),
        patches_json TEXT NOT NULL,
        runtime_scores_json TEXT NOT NULL,
        admitted_profiles_json TEXT,
        admitted_utc_day TEXT,
        admission_checksum TEXT,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK (preceding_authority_id <> resulting_authority_id),
        CHECK (
            (admitted_profiles_json IS NULL AND admitted_utc_day IS NULL
                AND admission_checksum IS NULL)
            OR
            (admitted_profiles_json IS NOT NULL AND admitted_utc_day IS NOT NULL
                AND admission_checksum IS NOT NULL)
        ),
        UNIQUE (management_authority_id, management_epoch)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS management_profile_states (
        management_authority_id TEXT NOT NULL
            REFERENCES management_controls(management_authority_id),
        profile_id TEXT NOT NULL,
        authority_id TEXT NOT NULL,
        active_revision_id TEXT REFERENCES management_revisions(revision_id),
        management_epoch INTEGER NOT NULL CHECK (management_epoch >= 0),
        control_revision_id TEXT REFERENCES management_revisions(revision_id),
        challenger_revision_id TEXT REFERENCES management_revisions(revision_id),
        experiment_phase TEXT NOT NULL CHECK (
            experiment_phase IN (
                'eligible', 'validated', 'canary', 'cooldown',
                'rolled_back', 'recovery_required'
            )
        ),
        cooldown_until TEXT,
        rejection_count INTEGER NOT NULL CHECK (rejection_count >= 0),
        generation INTEGER NOT NULL CHECK (generation >= 0),
        updated_at TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        PRIMARY KEY (management_authority_id, profile_id),
        CHECK (
            (control_revision_id IS NULL AND challenger_revision_id IS NULL)
            OR
            (control_revision_id IS NOT NULL AND challenger_revision_id IS NOT NULL
                AND control_revision_id <> challenger_revision_id)
        ),
        CHECK (
            (experiment_phase = 'cooldown' AND cooldown_until IS NOT NULL)
            OR
            (experiment_phase <> 'cooldown' AND cooldown_until IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS management_lifecycle_events (
        event_id TEXT PRIMARY KEY,
        management_authority_id TEXT NOT NULL
            REFERENCES management_controls(management_authority_id),
        profile_id TEXT NOT NULL,
        revision_id TEXT REFERENCES management_revisions(revision_id),
        event_type TEXT NOT NULL CHECK (
            event_type IN (
                'proposed', 'validated', 'canary', 'promoted', 'rejected',
                'frozen', 'unfrozen', 'rolled_back', 'hold', 'cooldown',
                'recovered'
            )
        ),
        reason_code TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS management_canary_assignments (
        assignment_id TEXT PRIMARY KEY,
        management_authority_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        operation_identity_hash TEXT NOT NULL,
        control_revision_id TEXT NOT NULL
            REFERENCES management_revisions(revision_id),
        challenger_revision_id TEXT NOT NULL
            REFERENCES management_revisions(revision_id),
        arm TEXT NOT NULL CHECK (arm IN ('control', 'challenger')),
        phase TEXT NOT NULL CHECK (phase IN ('reserved', 'finalized', 'terminal')),
        runtime_id TEXT,
        reasoning_effort TEXT,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (management_authority_id, profile_id, operation_identity_hash),
        FOREIGN KEY (management_authority_id, profile_id)
            REFERENCES management_profile_states(management_authority_id, profile_id),
        CHECK (control_revision_id <> challenger_revision_id),
        CHECK (
            (phase = 'reserved' AND runtime_id IS NULL AND reasoning_effort IS NULL)
            OR
            (phase IN ('finalized', 'terminal')
                AND runtime_id IS NOT NULL AND reasoning_effort IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS management_leases (
        management_authority_id TEXT NOT NULL
            REFERENCES management_controls(management_authority_id),
        profile_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        lease_expires_at TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation > 0),
        updated_at TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        PRIMARY KEY (management_authority_id, profile_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS management_config_receipts (
        receipt_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL REFERENCES management_revisions(revision_id),
        phase TEXT NOT NULL CHECK (
            phase IN (
                'prepared', 'config_replaced', 'committed',
                'recovery_required'
            )
        ),
        preceding_authority_id TEXT NOT NULL,
        resulting_authority_id TEXT NOT NULL,
        backup_checksum TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        CHECK (preceding_authority_id <> resulting_authority_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS management_lifecycle_finalizations (
        finalization_id TEXT PRIMARY KEY,
        receipt_id TEXT NOT NULL UNIQUE
            REFERENCES management_config_receipts(receipt_id),
        revision_id TEXT NOT NULL REFERENCES management_revisions(revision_id),
        challenger_revision_id TEXT NOT NULL
            REFERENCES management_revisions(revision_id),
        management_authority_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('promote', 'rollback')),
        phase TEXT NOT NULL CHECK (phase IN ('pending', 'finalized')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS routing_decisions (
        decision_id TEXT PRIMARY KEY,
        authority_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        session_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        operation_id TEXT,
        task_index INTEGER,
        task_facts_hash TEXT,
        selected_profile_id TEXT,
        projection_mode TEXT,
        activation_receipt_id TEXT,
        activation_config_sha TEXT,
        adapter_capability_sha TEXT,
        authority_revision_id TEXT,
        candidate_bundle_checksum TEXT,
        inventory_revision_id TEXT NOT NULL,
        catalog_revision_id TEXT NOT NULL,
        policy_revision_id TEXT NOT NULL,
        adaptive_revision_id TEXT NOT NULL,
        profile_adaptive_revision_id TEXT,
        adaptive_assignment_id TEXT,
        adaptive_profile_snapshot_json TEXT,
        selected_runtime_id TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_candidates (
        decision_id TEXT NOT NULL
            REFERENCES routing_decisions(decision_id) ON DELETE CASCADE,
        candidate_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        target_role TEXT NOT NULL,
        target_ordinal INTEGER NOT NULL,
        runtime_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        eligible INTEGER NOT NULL CHECK (eligible IN (0, 1)),
        reason_codes_json TEXT NOT NULL,
        scoring_json TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        PRIMARY KEY (decision_id, candidate_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS route_epochs (
        route_epoch_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        decision_id TEXT NOT NULL REFERENCES routing_decisions(decision_id),
        epoch_number INTEGER NOT NULL CHECK (epoch_number >= 0),
        runtime_id TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        provider_started INTEGER NOT NULL DEFAULT 0
            CHECK (provider_started IN (0, 1)),
        api_request_id TEXT,
        provider_started_at TEXT,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        CHECK (
            (provider_started = 0 AND api_request_id IS NULL
                AND provider_started_at IS NULL)
            OR
            (provider_started = 1 AND api_request_id IS NOT NULL
                AND provider_started_at IS NOT NULL)
        ),
        UNIQUE (session_id, epoch_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_operations (
        operation_key TEXT PRIMARY KEY,
        claim_id TEXT NOT NULL,
        scope TEXT NOT NULL CHECK (scope IN ('fresh_session', 'delegation')),
        session_id TEXT NOT NULL,
        operation_id TEXT,
        task_index INTEGER,
        facts_hash TEXT NOT NULL,
        owner_pid INTEGER NOT NULL,
        owner_start_token TEXT NOT NULL,
        lease_expires_at REAL NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('claimed', 'complete')),
        decision_id TEXT REFERENCES routing_decisions(decision_id),
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (
            (scope = 'fresh_session' AND operation_id IS NULL
                AND task_index IS NULL)
            OR
            (scope = 'delegation' AND operation_id IS NOT NULL
                AND task_index IS NOT NULL AND task_index >= 0)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_route_bindings (
        session_id TEXT PRIMARY KEY,
        binding_kind TEXT NOT NULL CHECK (binding_kind IN ('routed', 'manual')),
        projection_mode TEXT NOT NULL,
        decision_id TEXT REFERENCES routing_decisions(decision_id),
        runtime_id TEXT NOT NULL,
        manual_pin_source TEXT,
        current_epoch INTEGER NOT NULL CHECK (current_epoch >= -1),
        continuation_root TEXT,
        parent_session_id TEXT,
        continuation_reason TEXT,
        created_at TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        CHECK (
            (binding_kind = 'routed'
                AND projection_mode IN ('shadow', 'active', 'inherit')
                AND decision_id IS NOT NULL AND manual_pin_source IS NULL)
            OR
            (binding_kind = 'manual' AND decision_id IS NULL
                AND manual_pin_source IS NOT NULL AND projection_mode = 'manual')
        ),
        CHECK (
            (continuation_root IS NULL AND parent_session_id IS NULL
                AND continuation_reason IS NULL)
            OR
            (continuation_root IS NOT NULL AND parent_session_id IS NOT NULL
                AND continuation_reason = 'compression')
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activation_receipts (
        receipt_id TEXT PRIMARY KEY,
        authority_id TEXT NOT NULL,
        config_sha TEXT NOT NULL,
        inventory_contract_sha TEXT NOT NULL,
        inventory_revision TEXT NOT NULL,
        adapter_capability_sha TEXT NOT NULL,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (
            authority_id,
            config_sha,
            adapter_capability_sha,
            inventory_contract_sha,
            inventory_revision
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS budget_ledger (
        reservation_id TEXT PRIMARY KEY,
        bucket TEXT NOT NULL,
        budget_day TEXT NOT NULL,
        reserved_usd REAL NOT NULL CHECK (reserved_usd >= 0),
        daily_limit_usd REAL NOT NULL CHECK (daily_limit_usd >= 0),
        actual_usd REAL CHECK (actual_usd IS NULL OR actual_usd >= 0),
        status TEXT NOT NULL CHECK (status IN ('reserved', 'reconciled')),
        created_at TEXT NOT NULL,
        reconciled_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_verification_attempts (
        precondition_hash TEXT PRIMARY KEY,
        runtime_id TEXT NOT NULL,
        authority_id TEXT NOT NULL,
        inventory_revision TEXT NOT NULL,
        budget_reservation_id TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('reserved', 'succeeded', 'failed')),
        reason_code TEXT,
        input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
        output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
        actual_cost_usd REAL
            CHECK (actual_cost_usd IS NULL OR actual_cost_usd >= 0),
        response_hash TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_verification_previews (
        precondition_hash TEXT PRIMARY KEY,
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_events (
        evidence_id TEXT PRIMARY KEY,
        source TEXT NOT NULL CHECK (source IN ('hermes_turn_outcome', 'user_feedback')),
        signal_type TEXT NOT NULL CHECK (
            signal_type IN ('objective_outcome', 'explicit_feedback', 'operational')
        ),
        parent_evidence_id TEXT REFERENCES evidence_events(evidence_id),
        decision_id TEXT NOT NULL REFERENCES routing_decisions(decision_id),
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        route_epoch_id TEXT NOT NULL REFERENCES route_epochs(route_epoch_id),
        runtime_id TEXT NOT NULL,
        profile_id TEXT,
        reasoning_effort TEXT NOT NULL,
        context_bucket_id TEXT,
        is_initial_routing_task INTEGER NOT NULL
            CHECK (is_initial_routing_task IN (0, 1)),
        outcome TEXT,
        feedback_value TEXT,
        normalized_value REAL CHECK (
            normalized_value IS NULL
            OR (normalized_value >= 0 AND normalized_value <= 1)
        ),
        confidence_weight REAL NOT NULL
            CHECK (confidence_weight >= 0 AND confidence_weight <= 1),
        attribution_confidence REAL NOT NULL
            CHECK (attribution_confidence >= 0 AND attribution_confidence <= 1),
        api_calls INTEGER NOT NULL CHECK (api_calls >= 0),
        tool_iterations INTEGER NOT NULL CHECK (tool_iterations >= 0),
        retry_count INTEGER NOT NULL CHECK (retry_count >= 0),
        cost_usd REAL NOT NULL CHECK (cost_usd >= 0),
        input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
        output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
        cache_read_tokens INTEGER NOT NULL CHECK (cache_read_tokens >= 0),
        latency_seconds REAL CHECK (latency_seconds IS NULL OR latency_seconds >= 0),
        document_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        CHECK (
            (source = 'hermes_turn_outcome' AND parent_evidence_id IS NULL
                AND outcome IS NOT NULL AND feedback_value IS NULL)
            OR
            (source = 'user_feedback' AND parent_evidence_id IS NOT NULL
                AND outcome IS NULL AND feedback_value IS NOT NULL)
        )
    )
    """,
)


_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_authority_created "
    "ON authority_revisions(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_inventory_created "
    "ON inventory_snapshots(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_inventory_runtime "
    "ON inventory_observations(runtime_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_created ON catalog_snapshots(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_catalog_model "
    "ON catalog_evidence(model, model_version, metric_name)",
    "CREATE INDEX IF NOT EXISTS idx_adaptive_authority "
    "ON adaptive_revisions(authority_id, complete, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_adaptive_profile_revisions "
    "ON adaptive_profile_revisions(authority_id, profile_id, created_at, revision_id)",
    "CREATE INDEX IF NOT EXISTS idx_adaptive_lifecycle_profile "
    "ON adaptive_lifecycle_events(authority_id, profile_id, created_at, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_adaptive_assignment_context "
    "ON adaptive_canary_assignments(authority_id, profile_id, context_bucket_id)",
    "CREATE INDEX IF NOT EXISTS idx_management_revisions_profile_day "
    "ON management_revisions(admitted_utc_day, created_at, revision_id)",
    "CREATE INDEX IF NOT EXISTS idx_management_lifecycle_profile "
    "ON management_lifecycle_events("
    "management_authority_id, profile_id, created_at, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_management_assignments_open "
    "ON management_canary_assignments("
    "management_authority_id, profile_id, phase, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_management_receipts_revision "
    "ON management_config_receipts(revision_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_management_finalizations_pending "
    "ON management_lifecycle_finalizations(phase, created_at, finalization_id)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_session "
    "ON routing_decisions(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_task "
    "ON routing_decisions(task_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_decisions_fresh_session "
    "ON routing_decisions(session_id) WHERE scope = 'fresh_session'",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_decisions_delegation_operation "
    "ON routing_decisions(operation_id, task_index) "
    "WHERE scope = 'delegation' AND operation_id IS NOT NULL "
    "AND task_index IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_route_epochs_session "
    "ON route_epochs(session_id, epoch_number)",
    "CREATE INDEX IF NOT EXISTS idx_decision_operations_decision "
    "ON decision_operations(decision_id)",
    "CREATE INDEX IF NOT EXISTS idx_bindings_decision "
    "ON session_route_bindings(decision_id)",
    "CREATE INDEX IF NOT EXISTS idx_activation_receipt_match "
    "ON activation_receipts(authority_id, config_sha, adapter_capability_sha, "
    "inventory_contract_sha, inventory_revision)",
    "CREATE INDEX IF NOT EXISTS idx_budget_bucket_day "
    "ON budget_ledger(bucket, budget_day, status)",
    "CREATE INDEX IF NOT EXISTS idx_verification_runtime "
    "ON runtime_verification_attempts(runtime_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_verification_preview_expiry "
    "ON runtime_verification_previews(expires_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_turn_outcome "
    "ON evidence_events(session_id, turn_id) "
    "WHERE source = 'hermes_turn_outcome'",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_feedback "
    "ON evidence_events(parent_evidence_id, feedback_value) "
    "WHERE source = 'user_feedback'",
    "CREATE INDEX IF NOT EXISTS idx_evidence_report "
    "ON evidence_events(observed_at, profile_id, runtime_id, reasoning_effort)",
)


SCHEMA_SQL = (
    ";\n".join(
        statement.strip() for statement in (*_TABLE_STATEMENTS, *_INDEX_STATEMENTS)
    )
    + ";\n"
)


_REQUIRED_TABLES = frozenset({
    "schema_meta",
    "authority_revisions",
    "inventory_snapshots",
    "inventory_observations",
    "catalog_snapshots",
    "catalog_evidence",
    "adaptive_revisions",
    "active_adaptive_revisions",
    "adaptive_profile_revisions",
    "adaptive_profile_states",
    "adaptive_lifecycle_events",
    "adaptive_canary_assignments",
    "adaptive_optimizer_leases",
    "management_controls",
    "management_profile_states",
    "management_revisions",
    "management_lifecycle_events",
    "management_canary_assignments",
    "management_leases",
    "management_config_receipts",
    "management_lifecycle_finalizations",
    "routing_decisions",
    "decision_candidates",
    "route_epochs",
    "decision_operations",
    "session_route_bindings",
    "activation_receipts",
    "budget_ledger",
    "runtime_verification_attempts",
    "runtime_verification_previews",
    "evidence_events",
})


_REQUIRED_INDEXES = frozenset({
    "idx_authority_created",
    "idx_inventory_created",
    "idx_inventory_runtime",
    "idx_catalog_created",
    "idx_catalog_model",
    "idx_adaptive_authority",
    "idx_adaptive_profile_revisions",
    "idx_adaptive_lifecycle_profile",
    "idx_adaptive_assignment_context",
    "idx_management_revisions_profile_day",
    "idx_management_lifecycle_profile",
    "idx_management_assignments_open",
    "idx_management_receipts_revision",
    "idx_management_finalizations_pending",
    "idx_decisions_session",
    "idx_decisions_task",
    "uq_decisions_fresh_session",
    "uq_decisions_delegation_operation",
    "idx_route_epochs_session",
    "idx_decision_operations_decision",
    "idx_bindings_decision",
    "idx_activation_receipt_match",
    "idx_budget_bucket_day",
    "idx_verification_runtime",
    "idx_verification_preview_expiry",
    "uq_evidence_turn_outcome",
    "uq_evidence_feedback",
    "idx_evidence_report",
})


_TASK2_REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "routing_decisions": frozenset({
        "decision_id",
        "authority_id",
        "scope",
        "session_id",
        "task_id",
        "operation_id",
        "task_index",
        "task_facts_hash",
        "selected_profile_id",
        "projection_mode",
        "activation_receipt_id",
        "activation_config_sha",
        "adapter_capability_sha",
        "authority_revision_id",
        "candidate_bundle_checksum",
        "inventory_revision_id",
        "catalog_revision_id",
        "policy_revision_id",
        "adaptive_revision_id",
        "profile_adaptive_revision_id",
        "adaptive_assignment_id",
        "adaptive_profile_snapshot_json",
        "selected_runtime_id",
        "document_json",
        "checksum",
        "created_at",
    }),
    "decision_candidates": frozenset({
        "decision_id",
        "candidate_id",
        "profile_id",
        "target_role",
        "target_ordinal",
        "runtime_id",
        "ordinal",
        "eligible",
        "reason_codes_json",
        "scoring_json",
        "document_json",
        "checksum",
    }),
    "route_epochs": frozenset({
        "route_epoch_id",
        "session_id",
        "decision_id",
        "epoch_number",
        "runtime_id",
        "reason_code",
        "started_at",
        "ended_at",
        "provider_started",
        "api_request_id",
        "provider_started_at",
        "document_json",
        "checksum",
    }),
    "decision_operations": frozenset({
        "operation_key",
        "claim_id",
        "scope",
        "session_id",
        "operation_id",
        "task_index",
        "facts_hash",
        "owner_pid",
        "owner_start_token",
        "lease_expires_at",
        "status",
        "decision_id",
        "document_json",
        "checksum",
        "claimed_at",
        "updated_at",
    }),
    "session_route_bindings": frozenset({
        "session_id",
        "binding_kind",
        "projection_mode",
        "decision_id",
        "runtime_id",
        "manual_pin_source",
        "current_epoch",
        "continuation_root",
        "parent_session_id",
        "continuation_reason",
        "created_at",
        "document_json",
        "checksum",
    }),
    "activation_receipts": frozenset({
        "receipt_id",
        "authority_id",
        "config_sha",
        "inventory_contract_sha",
        "inventory_revision",
        "adapter_capability_sha",
        "document_json",
        "checksum",
        "created_at",
    }),
}


_BUDGET_ADDITIVE_COLUMNS = (
    (
        "daily_limit_usd",
        "daily_limit_usd REAL NOT NULL DEFAULT 0 CHECK (daily_limit_usd >= 0)",
    ),
    (
        "actual_usd",
        "actual_usd REAL CHECK (actual_usd IS NULL OR actual_usd >= 0)",
    ),
    (
        "status",
        "status TEXT NOT NULL DEFAULT 'reserved' "
        "CHECK (status IN ('reserved', 'reconciled'))",
    ),
    ("reconciled_at", "reconciled_at TEXT"),
)


_CATALOG_ADDITIVE_COLUMNS = (
    ("canonical_provider", "canonical_provider TEXT NOT NULL DEFAULT ''"),
    ("canonical_model", "canonical_model TEXT NOT NULL DEFAULT ''"),
    ("canonical_version", "canonical_version TEXT NOT NULL DEFAULT ''"),
    ("runtime_id", "runtime_id TEXT"),
)


_DECISION_ADDITIVE_COLUMNS = (
    ("operation_id", "operation_id TEXT"),
    ("task_index", "task_index INTEGER"),
    ("task_facts_hash", "task_facts_hash TEXT"),
    ("selected_profile_id", "selected_profile_id TEXT"),
    ("projection_mode", "projection_mode TEXT"),
    ("activation_receipt_id", "activation_receipt_id TEXT"),
    ("activation_config_sha", "activation_config_sha TEXT"),
    ("adapter_capability_sha", "adapter_capability_sha TEXT"),
    ("authority_revision_id", "authority_revision_id TEXT"),
    ("candidate_bundle_checksum", "candidate_bundle_checksum TEXT"),
    ("profile_adaptive_revision_id", "profile_adaptive_revision_id TEXT"),
    ("adaptive_assignment_id", "adaptive_assignment_id TEXT"),
    ("adaptive_profile_snapshot_json", "adaptive_profile_snapshot_json TEXT"),
)


_ROUTE_EPOCH_ADDITIVE_COLUMNS = (
    (
        "provider_started",
        "provider_started INTEGER NOT NULL DEFAULT 0 "
        "CHECK (provider_started IN (0, 1))",
    ),
    ("api_request_id", "api_request_id TEXT"),
    ("provider_started_at", "provider_started_at TEXT"),
    ("document_json", "document_json TEXT"),
    ("checksum", "checksum TEXT"),
)


_BINDING_ADDITIVE_COLUMNS = (
    ("document_json", "document_json TEXT"),
    ("checksum", "checksum TEXT"),
)


_UNSAFE_JSON_FIELDS = frozenset({
    "auth",
    "base_url",
    "cookie",
    "cookies",
    "credential",
    "credential_pool",
    "credentials",
    "endpoint",
    "headers",
    "key",
    "password",
    "private_key",
    "prompt",
    "raw_prompt",
    "response",
    "raw_response",
    "secret",
    "api_key",
    "access_token",
    "refresh_token",
    "oauth_token",
    "bearer_token",
    "raw_endpoint",
    "sig",
    "signature",
    "token",
    "tokens",
    "url",
})
_IDENTITY_FIELDS = frozenset({
    "adaptive_assignment_id",
    "adaptive_revision_id",
    "adaptive_revision",
    "active_revision_id",
    "adapter_capability_sha",
    "activation_config_sha",
    "activation_receipt_id",
    "api_request_id",
    "auth_identity",
    "authority_id",
    "authority_revision",
    "catalog_revision_id",
    "catalog_revision",
    "credential_pool_identity",
    "decision_id",
    "facts_hash",
    "endpoint_identity",
    "evidence_id",
    "parent_evidence_id",
    "inventory_revision",
    "inventory_revision_id",
    "inventory_contract_sha",
    "inventory_fingerprint",
    "local_backend",
    "parent_revision_id",
    "parent_session_id",
    "policy_revision_id",
    "policy_revision",
    "profile_id",
    "reservation_id",
    "receipt_id",
    "revision_id",
    "route_epoch_id",
    "candidate_id",
    "claim_id",
    "config_sha",
    "runtime_id",
    "selected_runtime_id",
    "session_id",
    "source_id",
    "task_id",
    "turn_id",
    "continuation_root",
    "operation_id",
    "operation_key",
    "decision_id",
    "owner_start_token",
    "authority_revision_id",
    "assignment_id",
    "challenger_revision_id",
    "context_bucket_id",
    "control_revision_id",
    "event_id",
    "operation_identity_hash",
    "owner_id",
    "management_authority_id",
    "preceding_authority_id",
    "resulting_authority_id",
    "backup_checksum",
    "ranking_pack_sha256",
    "profile_adaptive_revision_id",
})
_OPTIONAL_IDENTITY_FIELDS = frozenset({
    "credential_pool_identity",
    "endpoint_identity",
    "local_backend",
    "parent_revision_id",
    "activation_receipt_id",
    "activation_config_sha",
    "adapter_capability_sha",
    "api_request_id",
    "operation_id",
    "decision_id",
    "continuation_root",
    "parent_session_id",
    "parent_evidence_id",
    "profile_id",
    "active_revision_id",
    "control_revision_id",
    "challenger_revision_id",
    "profile_adaptive_revision_id",
    "adaptive_assignment_id",
    "assignment_id",
    "context_bucket_id",
    "operation_identity_hash",
    "control_runtime_id",
    "challenger_runtime_id",
    "revision_id",
    "runtime_id",
})
_DURABLE_IDENTITY_FIELDS = frozenset({
    "activation_receipt_id",
    "adaptive_revision",
    "api_request_id",
    "authority_id",
    "authority_revision",
    "catalog_revision",
    "continuation_root",
    "decision_id",
    "inventory_revision",
    "operation_id",
    "parent_session_id",
    "policy_revision",
    "receipt_id",
    "route_epoch_id",
    "session_id",
    "task_id",
    "turn_id",
    "active_revision_id",
    "assignment_id",
    "challenger_revision_id",
    "control_revision_id",
    "event_id",
    "owner_id",
    "profile_adaptive_revision_id",
})
_IDENTITY_FIELD_COMPACT = frozenset(
    field.replace("_", "") for field in _IDENTITY_FIELDS
)
_UNSAFE_FIELD_COMPACT = frozenset({
    "accesstoken",
    "apikey",
    "authorization",
    "baseurl",
    "bearertoken",
    "clientsecret",
    "credentialpool",
    "oauthtoken",
    "privatekey",
    "rawendpoint",
    "rawprompt",
    "rawresponse",
    "refreshtoken",
    "secretkey",
})
_NUMERIC_TOKEN_FIELDS = frozenset({
    "context_window_tokens",
    "expected_context_tokens",
    "expected_input_tokens",
    "expected_output_tokens",
    "classifier_input_tokens",
    "classifier_output_tokens",
    "input_tokens",
    "cache_read_tokens",
    "max_output_tokens",
    "maximum_input_tokens",
    "maximum_output_tokens",
    "protocol_overhead_tokens",
    "metered_input_usd_per_million_tokens",
    "metered_output_usd_per_million_tokens",
    "minimum_context_tokens",
    "output_tokens",
    "total_tokens",
})
_IDENTITY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/=-]{0,255}")
_PROFILE_IDENTIFIER = re.compile(r"[^\x00-\x1f\x7f]{1,256}")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_FINGERPRINT = re.compile(
    r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|"
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_EMAIL_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_RAW_ENDPOINT_IDENTITY = re.compile(
    r"(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|"
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)(?::\d+)?(?:/|$)",
    re.IGNORECASE,
)
_NUMERIC_HOST = re.compile(
    r"(?i)(?:0x[0-9a-f]+|0[0-7]+|\d+)"
    r"(?:\.(?:0x[0-9a-f]+|0[0-7]+|\d+))*"
)
_SECRET_PREFIX = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?i:bearer\s+[A-Za-z0-9._~+/-]{16,}|"
    r"basic\s+[A-Za-z0-9+/=]{16,}|sk[-_](?:proj[-_])?|gh[pousr]_|"
    r"github_pat_|xox[a-z]-|ya29\.|glpat-|hf_|npm_)|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])|"
    r"AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-]))"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?:api[-_]?key|access[-_]?token|refresh[-_]?token|client[-_]?secret|"
    r"password)\s*[:=]",
    re.IGNORECASE,
)
_SAVEPOINT_IDS = itertools.count(1)
_ContentWriter = Literal[
    "authority",
    "inventory",
    "catalog",
    "adaptive",
    "verification",
    "decision",
    "evidence",
    "management",
]


def _state_path(
    path: str | Path | None = None,
    *,
    home: str | Path | None = None,
) -> Path:
    if path is not None and home is not None:
        raise ValueError("path and home are mutually exclusive")
    if path is not None:
        return Path(path)
    root = Path(home) if home is not None else get_hermes_home()
    return root / "auto-routing" / "state.db"


def connect(
    path: str | Path | None = None,
    *,
    home: str | Path | None = None,
    allow_cross_thread_close: bool = False,
) -> sqlite3.Connection:
    """Open one independent profile-local SQLite connection.

    ``allow_cross_thread_close`` exists only for owners that confine all database
    operations to one thread but centrally close connections during teardown.
    """
    db_path = _state_path(path, home=home)
    _probe_schema_compatibility(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(db_path),
        isolation_level=None,
        timeout=BUSY_TIMEOUT_MS / 1000.0,
        check_same_thread=not allow_cross_thread_close,
    )
    try:
        connection.row_factory = sqlite3.Row
        _reject_unsupported_schema(connection)
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        _apply_wal_with_retry(connection, db_label=str(db_path.resolve()))
        connection.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        connection.close()
        raise
    return connection


def _probe_schema_compatibility(db_path: Path) -> None:
    """Reject future schemas using a stable copy of every SQLite file."""
    if not db_path.exists():
        return
    snapshot = _stable_schema_snapshot(db_path)
    with tempfile.TemporaryDirectory(prefix="hermes-auto-routing-schema-") as root:
        probe_path = Path(root) / db_path.name
        for suffix, content in snapshot.items():
            if content is not None:
                Path(f"{probe_path}{suffix}").write_bytes(content)
        connection = sqlite3.connect(str(probe_path), isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            _reject_unsupported_schema(connection)
            _reject_incompatible_partial_evidence_schema(
                connection,
                _stored_schema_version(connection),
            )
            _reject_incompatible_partial_management_schema(
                connection,
                _stored_schema_version(connection),
            )
            _reject_incompatible_partial_adaptation_schema(
                connection,
                _stored_schema_version(connection),
            )
        finally:
            connection.close()


def _stable_schema_snapshot(db_path: Path) -> dict[str, bytes | None]:
    """Read main/WAL/SHM bytes only when their file identities stay stable."""
    files = {
        "": db_path,
        "-wal": Path(f"{db_path}-wal"),
        "-shm": Path(f"{db_path}-shm"),
    }

    def signatures() -> dict[str, tuple[int, int, int] | None]:
        result: dict[str, tuple[int, int, int] | None] = {}
        for suffix, path in files.items():
            try:
                stat = path.stat()
            except FileNotFoundError:
                result[suffix] = None
            else:
                result[suffix] = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        return result

    for attempt in range(BUSY_MAX_RETRIES + 1):
        before = signatures()
        try:
            snapshot = {
                suffix: path.read_bytes() if before[suffix] is not None else None
                for suffix, path in files.items()
            }
        except (FileNotFoundError, PermissionError):
            snapshot = {}
        after = signatures()
        if snapshot and before == after:
            return snapshot
        if attempt == BUSY_MAX_RETRIES:
            raise StoreBusy("SQLite schema snapshot exceeded retry bound")
        time.sleep(
            random.uniform(
                BUSY_RETRY_MIN_SECONDS,
                BUSY_RETRY_MAX_SECONDS,
            )
        )
    raise AssertionError("unreachable schema snapshot retry state")


def _is_busy_error(error: BaseException) -> bool:
    return isinstance(error, sqlite3.OperationalError) and (
        "database is locked" in str(error).lower()
        or "database is busy" in str(error).lower()
        or "database table is locked" in str(error).lower()
        or "database schema is locked" in str(error).lower()
    )


def _apply_wal_with_retry(
    connection: sqlite3.Connection,
    *,
    db_label: str,
) -> str:
    for attempt in range(BUSY_MAX_RETRIES + 1):
        try:
            return apply_wal_with_fallback(connection, db_label=db_label)
        except sqlite3.OperationalError as error:
            if not _is_busy_error(error):
                raise
            if attempt == BUSY_MAX_RETRIES:
                raise StoreBusy(
                    "SQLite journal initialization exceeded retry bound"
                ) from error
            time.sleep(
                random.uniform(
                    BUSY_RETRY_MIN_SECONDS,
                    BUSY_RETRY_MAX_SECONDS,
                )
            )
    raise AssertionError("unreachable WAL retry state")


def _execute_boundary_with_retry(
    connection: sqlite3.Connection,
    sql: str,
    *,
    max_retries: int = BUSY_MAX_RETRIES,
) -> None:
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    for attempt in range(max_retries + 1):
        try:
            connection.execute(sql)
            return
        except sqlite3.OperationalError as error:
            if not _is_busy_error(error):
                raise
            if attempt == max_retries:
                raise StoreBusy(
                    "SQLite write contention exceeded retry bound"
                ) from error
            time.sleep(
                random.uniform(
                    BUSY_RETRY_MIN_SECONDS,
                    BUSY_RETRY_MAX_SECONDS,
                )
            )


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


@contextlib.contextmanager
def write_txn(
    connection: sqlite3.Connection,
    *,
    max_retries: int = BUSY_MAX_RETRIES,
) -> Iterator[sqlite3.Connection]:
    """Run one non-replayed body in a bounded IMMEDIATE transaction."""
    if connection.in_transaction:
        savepoint = f"auto_routing_sp_{next(_SAVEPOINT_IDS)}"
        _execute_boundary_with_retry(
            connection,
            f"SAVEPOINT {savepoint}",
            max_retries=max_retries,
        )
        try:
            yield connection
        except BaseException as error:
            try:
                _execute_boundary_with_retry(
                    connection,
                    f"ROLLBACK TO SAVEPOINT {savepoint}",
                    max_retries=max_retries,
                )
                _execute_boundary_with_retry(
                    connection,
                    f"RELEASE SAVEPOINT {savepoint}",
                    max_retries=max_retries,
                )
            except BaseException:
                _rollback_quietly(connection)
                raise
            if _is_busy_error(error):
                raise StoreBusy(
                    "SQLite nested write failed with bounded contention"
                ) from error
            raise
        else:
            try:
                _execute_boundary_with_retry(
                    connection,
                    f"RELEASE SAVEPOINT {savepoint}",
                    max_retries=max_retries,
                )
            except BaseException:
                _rollback_quietly(connection)
                raise
        return

    _execute_boundary_with_retry(
        connection,
        "BEGIN IMMEDIATE",
        max_retries=max_retries,
    )
    try:
        yield connection
    except BaseException as error:
        _rollback_quietly(connection)
        if _is_busy_error(error):
            raise StoreBusy("SQLite write failed with bounded contention") from error
        raise
    else:
        try:
            _execute_boundary_with_retry(
                connection,
                "COMMIT",
                max_retries=max_retries,
            )
        except BaseException:
            _rollback_quietly(connection)
            raise


@contextlib.contextmanager
def observer_evidence_write_txn(
    connection: sqlite3.Connection,
) -> Iterator[sqlite3.Connection]:
    """Use a fail-fast write budget for post-turn observer evidence only."""
    row = connection.execute("PRAGMA busy_timeout").fetchone()
    if row is None:  # pragma: no cover - SQLite always returns the current value
        raise StoreError("SQLite did not return a busy timeout")
    prior_busy_timeout = int(row[0])
    connection.execute(f"PRAGMA busy_timeout={EVIDENCE_OBSERVER_BUSY_TIMEOUT_MS}")
    try:
        try:
            with write_txn(
                connection,
                max_retries=EVIDENCE_OBSERVER_MAX_RETRIES,
            ) as transaction:
                yield transaction
        except StoreBusy as error:
            raise StoreBusy("SQLite observer evidence write exceeded budget") from error
    finally:
        connection.execute(f"PRAGMA busy_timeout={prior_busy_timeout}")


def _migrate_additive_columns(connection: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "budget_ledger" in tables:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(budget_ledger)")
        }
        for column, ddl in _BUDGET_ADDITIVE_COLUMNS:
            if column not in columns:
                add_column_if_missing(connection, "budget_ledger", column, ddl)
                columns.add(column)
    if "catalog_evidence" in tables:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(catalog_evidence)")
        }
        for column, ddl in _CATALOG_ADDITIVE_COLUMNS:
            if column not in columns:
                add_column_if_missing(connection, "catalog_evidence", column, ddl)
                columns.add(column)
        connection.execute(
            "UPDATE catalog_evidence SET canonical_model = model "
            "WHERE canonical_model = ''"
        )
        connection.execute(
            "UPDATE catalog_evidence SET canonical_version = model_version "
            "WHERE canonical_version = ''"
        )
    if "routing_decisions" in tables:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(routing_decisions)")
        }
        for column, ddl in _DECISION_ADDITIVE_COLUMNS:
            if column not in columns:
                add_column_if_missing(
                    connection,
                    "routing_decisions",
                    column,
                    ddl,
                )
                columns.add(column)
    if "route_epochs" in tables:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(route_epochs)")
        }
        for column, ddl in _ROUTE_EPOCH_ADDITIVE_COLUMNS:
            if column not in columns:
                add_column_if_missing(connection, "route_epochs", column, ddl)
                columns.add(column)
        for row in connection.execute("SELECT * FROM route_epochs").fetchall():
            if row["document_json"] is None or row["checksum"] is None:
                document_json = _canonical_json(_route_epoch_row_document(row))
                connection.execute(
                    "UPDATE route_epochs SET document_json = ?, checksum = ? "
                    "WHERE route_epoch_id = ?",
                    (
                        document_json,
                        _checksum(document_json),
                        str(row["route_epoch_id"]),
                    ),
                )
    if "session_route_bindings" in tables:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(session_route_bindings)")
        }
        for column, ddl in _BINDING_ADDITIVE_COLUMNS:
            if column not in columns:
                add_column_if_missing(
                    connection,
                    "session_route_bindings",
                    column,
                    ddl,
                )
                columns.add(column)
        for row in connection.execute(
            "SELECT * FROM session_route_bindings"
        ).fetchall():
            if row["document_json"] is None or row["checksum"] is None:
                document_json = _canonical_json(_binding_row_document(row))
                connection.execute(
                    "UPDATE session_route_bindings SET document_json = ?, checksum = ? "
                    "WHERE session_id = ?",
                    (
                        document_json,
                        _checksum(document_json),
                        str(row["session_id"]),
                    ),
                )
    if "adaptive_profile_states" in tables:
        _migrate_profile_state_rolled_back_phase(connection)
    if "adaptive_optimizer_leases" in tables:
        _migrate_optimizer_lease_attestation(connection)
    for table in ("decision_operations", "activation_receipts"):
        if table in tables:
            columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            missing = sorted(_TASK2_REQUIRED_COLUMNS[table] - columns)
            if missing:
                raise UnsupportedSchemaVersion(
                    f"cannot migrate partial {table} table; missing "
                    + ", ".join(missing)
                )
    if "decision_candidates" in tables:
        _migrate_decision_candidates(connection)
    _reject_conflicting_decision_keys(connection)


def _migrate_profile_state_rolled_back_phase(
    connection: sqlite3.Connection,
) -> None:
    """Widen the exact original v7 state phase constraint for rollback."""
    signature = _table_schema_signature(connection, "adaptive_profile_states")
    expected_tables, _expected_indexes = _expected_schema_signatures()
    if signature == expected_tables["adaptive_profile_states"]:
        return
    if signature != _legacy_profile_state_schema_signature():
        raise UnsupportedSchemaVersion(
            "cannot migrate incompatible adaptive_profile_states table"
        )
    rows = connection.execute("SELECT * FROM adaptive_profile_states").fetchall()
    for row in rows:
        try:
            RoutingStore._profile_control_from_row(row)
        except Exception as error:
            raise UnsupportedSchemaVersion(
                "cannot migrate invalid adaptive profile state"
            ) from error
    legacy = "adaptive_profile_states_legacy_initial_v7"
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (legacy,),
        ).fetchone()
        is not None
    ):
        raise UnsupportedSchemaVersion(
            "cannot migrate profile states with a stale legacy table"
        )
    connection.execute(f'ALTER TABLE "adaptive_profile_states" RENAME TO "{legacy}"')
    connection.execute(_canonical_table_statement("adaptive_profile_states"))
    connection.execute(
        "INSERT INTO adaptive_profile_states "
        "(authority_id, profile_id, active_revision_id, control_revision_id, "
        "challenger_revision_id, experiment_phase, frozen, cooldown_until, "
        "rejection_count, generation, updated_at, document_json, checksum) "
        f"SELECT authority_id, profile_id, active_revision_id, control_revision_id, "
        f"challenger_revision_id, experiment_phase, frozen, cooldown_until, "
        f"rejection_count, generation, updated_at, document_json, checksum FROM \"{legacy}\""
    )
    connection.execute(f'DROP TABLE "{legacy}"')


def _migrate_optimizer_lease_attestation(connection: sqlite3.Connection) -> None:
    """Rebuild the original scalar v7 lease table with durable attestation."""
    signature = _table_schema_signature(connection, "adaptive_optimizer_leases")
    expected_tables, _expected_indexes = _expected_schema_signatures()
    if signature == expected_tables["adaptive_optimizer_leases"]:
        return
    if signature != _legacy_optimizer_lease_schema_signature():
        raise UnsupportedSchemaVersion(
            "cannot migrate incompatible adaptive_optimizer_leases table"
        )
    rows = connection.execute(
        "SELECT authority_id, profile_id, owner_id, lease_expires_at, "
        "generation, updated_at FROM adaptive_optimizer_leases"
    ).fetchall()
    legacy = "adaptive_optimizer_leases_legacy_scalar_v7"
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (legacy,),
        ).fetchone()
        is not None
    ):
        raise UnsupportedSchemaVersion(
            "cannot migrate optimizer leases with a stale legacy table"
        )
    connection.execute(
        f'ALTER TABLE "adaptive_optimizer_leases" RENAME TO "{legacy}"'
    )
    connection.execute(_canonical_table_statement("adaptive_optimizer_leases"))
    for row in rows:
        try:
            lease = OptimizerLease.model_validate({
                "authority_id": str(row["authority_id"]),
                "profile_id": str(row["profile_id"]),
                "owner_id": str(row["owner_id"]),
                "lease_expires_at": str(row["lease_expires_at"]),
                "generation": int(row["generation"]),
                "updated_at": str(row["updated_at"]),
            })
            _assert_content_free(lease, writer="adaptive")
            document_json = _canonical_json(lease)
            checksum = _checksum(document_json)
            _assert_canonical(lease.owner_id, document_json)
            _verify_checksum(lease.owner_id, document_json, checksum)
        except Exception as error:
            raise UnsupportedSchemaVersion(
                "cannot migrate invalid scalar optimizer lease"
            ) from error
        connection.execute(
            "INSERT INTO adaptive_optimizer_leases "
            "(authority_id, profile_id, owner_id, lease_expires_at, generation, "
            "updated_at, document_json, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lease.authority_id,
                lease.profile_id,
                lease.owner_id,
                lease.lease_expires_at,
                lease.generation,
                lease.updated_at,
                document_json,
                checksum,
            ),
        )
    connection.execute(f'DROP TABLE "{legacy}"')


def _migrate_decision_candidates(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(decision_candidates)")
    }
    required = {
        "candidate_id",
        "profile_id",
        "target_role",
        "target_ordinal",
        "document_json",
        "checksum",
    }
    if "candidate_id" in columns:
        if not required.issubset(columns):
            missing = sorted(required - columns)
            raise UnsupportedSchemaVersion(
                "cannot migrate partially rebuilt decision_candidates; missing "
                + ", ".join(missing)
            )
        return
    rows = connection.execute(
        "SELECT decision_id, runtime_id, ordinal, eligible, "
        "reason_codes_json, scoring_json FROM decision_candidates "
        "ORDER BY decision_id, ordinal, runtime_id"
    ).fetchall()
    connection.execute(
        "ALTER TABLE decision_candidates RENAME TO decision_candidates_legacy_v3"
    )
    connection.execute(
        "CREATE TABLE decision_candidates ("
        "decision_id TEXT NOT NULL REFERENCES routing_decisions(decision_id) "
        "ON DELETE CASCADE, candidate_id TEXT NOT NULL, profile_id TEXT NOT NULL, "
        "target_role TEXT NOT NULL, target_ordinal INTEGER NOT NULL, "
        "runtime_id TEXT NOT NULL, ordinal INTEGER NOT NULL, eligible INTEGER NOT NULL "
        "CHECK (eligible IN (0, 1)), reason_codes_json TEXT NOT NULL, "
        "scoring_json TEXT NOT NULL, document_json TEXT NOT NULL, checksum TEXT NOT NULL, "
        "PRIMARY KEY (decision_id, candidate_id))"
    )
    for row in rows:
        runtime_id = str(row["runtime_id"])
        candidate_id = f"legacy:{runtime_id}"
        document = {
            "candidate_id": candidate_id,
            "profile_id": "legacy",
            "target_role": "legacy",
            "target_ordinal": int(row["ordinal"]),
            "runtime_id": runtime_id,
            "eligible": bool(row["eligible"]),
            "reason_codes": json.loads(str(row["reason_codes_json"])),
            "scoring": json.loads(str(row["scoring_json"])),
        }
        document_json = _canonical_json(document)
        connection.execute(
            "INSERT INTO decision_candidates "
            "(decision_id, candidate_id, profile_id, target_role, target_ordinal, "
            "runtime_id, ordinal, eligible, reason_codes_json, scoring_json, "
            "document_json, checksum) VALUES (?, ?, 'legacy', 'legacy', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(row["decision_id"]),
                candidate_id,
                int(row["ordinal"]),
                runtime_id,
                int(row["ordinal"]),
                int(row["eligible"]),
                str(row["reason_codes_json"]),
                str(row["scoring_json"]),
                document_json,
                _checksum(document_json),
            ),
        )
    connection.execute("DROP TABLE decision_candidates_legacy_v3")


def _reject_conflicting_decision_keys(connection: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "routing_decisions" not in tables:
        return
    duplicate_fresh = connection.execute(
        "SELECT session_id, COUNT(*) AS count FROM routing_decisions "
        "WHERE scope = 'fresh_session' GROUP BY session_id HAVING COUNT(*) > 1 "
        "LIMIT 1"
    ).fetchone()
    if duplicate_fresh is not None:
        raise UnsupportedSchemaVersion(
            "cannot migrate: multiple fresh decisions exist for session "
            f"{duplicate_fresh['session_id']!r}"
        )
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(routing_decisions)")
    }
    if {"operation_id", "task_index"} <= columns:
        duplicate_delegation = connection.execute(
            "SELECT operation_id, task_index, COUNT(*) AS count "
            "FROM routing_decisions WHERE scope = 'delegation' "
            "AND operation_id IS NOT NULL AND task_index IS NOT NULL "
            "GROUP BY operation_id, task_index HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if duplicate_delegation is not None:
            raise UnsupportedSchemaVersion(
                "cannot migrate: multiple delegation decisions exist for "
                f"operation {duplicate_delegation['operation_id']!r} "
                f"task {duplicate_delegation['task_index']}"
            )


@dataclass(frozen=True, slots=True)
class _TableSchemaSignature:
    columns: tuple[tuple[str, str, bool, str | None, int], ...]
    foreign_keys: tuple[tuple[int, str, str, str, str, str, str], ...]
    checks: tuple[str, ...]
    unique_constraints: tuple[tuple[str, tuple[str, ...], bool], ...]


@dataclass(frozen=True, slots=True)
class _IndexSchemaSignature:
    table: str
    unique: bool
    partial: bool
    columns: tuple[str, ...]
    sql: str


def _normalize_schema_sql(value: str | None) -> str:
    if value is None:
        return ""
    normalized: list[str] = []
    quote: str | None = None
    closing_quote: str | None = None
    pending_space = False
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            normalized.append(character)
            if character == closing_quote:
                if (
                    quote != "["
                    and index + 1 < len(value)
                    and value[index + 1] == closing_quote
                ):
                    normalized.append(value[index + 1])
                    index += 2
                    continue
                quote = None
                closing_quote = None
            index += 1
            continue
        if character.isspace():
            pending_space = bool(normalized)
            index += 1
            continue
        if pending_space:
            normalized.append(" ")
            pending_space = False
        if character in {"'", '"', "`", "["}:
            quote = character
            closing_quote = "]" if character == "[" else character
            normalized.append(character)
        else:
            normalized.append(character.casefold())
        index += 1
    return "".join(normalized).strip()


def _normalize_check_expression(value: str) -> str:
    return _normalize_schema_sql(value)


def _check_expressions(sql: str | None) -> tuple[str, ...]:
    """Extract normalized CHECK bodies from SQLite's canonical table SQL."""
    if not sql:
        return ()
    lowered = sql.casefold()
    checks: list[str] = []
    cursor = 0
    while True:
        match = re.search(r"\bcheck\s*\(", lowered[cursor:])
        if match is None:
            break
        opening = cursor + match.end() - 1
        depth = 0
        quote: str | None = None
        index = opening
        while index < len(sql):
            character = sql[index]
            if quote is not None:
                if character == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 2
                        continue
                    quote = None
                index += 1
                continue
            if character in {"'", '"', "`"}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    checks.append(_normalize_check_expression(sql[opening + 1 : index]))
                    cursor = index + 1
                    break
            index += 1
        else:
            raise sqlite3.DatabaseError("unbalanced CHECK constraint")
    return tuple(sorted(checks))


def _table_schema_signature(
    connection: sqlite3.Connection,
    table: str,
) -> _TableSchemaSignature:
    columns = tuple(
        sorted(
            (
                str(row["name"]),
                str(row["type"]).upper(),
                bool(row["notnull"]),
                (
                    None
                    if row["dflt_value"] is None
                    else _normalize_schema_sql(str(row["dflt_value"]))
                ),
                int(row["pk"]),
            )
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
    )
    foreign_keys = tuple(
        sorted(
            (
                int(row["seq"]),
                str(row["table"]),
                str(row["from"]),
                str(row["to"]),
                str(row["on_update"]).upper(),
                str(row["on_delete"]).upper(),
                str(row["match"]).upper(),
            )
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        )
    )
    unique_constraints: list[tuple[str, tuple[str, ...], bool]] = []
    for row in connection.execute(f'PRAGMA index_list("{table}")'):
        origin = str(row["origin"])
        if origin not in {"pk", "u"}:
            continue
        index_name = str(row["name"])
        index_columns = tuple(
            str(column["name"])
            for column in connection.execute(f'PRAGMA index_info("{index_name}")')
        )
        unique_constraints.append((origin, index_columns, bool(row["partial"])))
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if sql_row is None:
        raise sqlite3.DatabaseError(f"missing table {table}")
    return _TableSchemaSignature(
        columns=columns,
        foreign_keys=foreign_keys,
        checks=_check_expressions(sql_row["sql"]),
        unique_constraints=tuple(sorted(unique_constraints)),
    )


@lru_cache(maxsize=1)
def _legacy_optimizer_lease_schema_signature() -> _TableSchemaSignature:
    legacy = sqlite3.connect(":memory:", isolation_level=None)
    legacy.row_factory = sqlite3.Row
    try:
        legacy.execute(
            "CREATE TABLE adaptive_optimizer_leases ("
            "authority_id TEXT NOT NULL, profile_id TEXT NOT NULL, "
            "owner_id TEXT NOT NULL, lease_expires_at TEXT NOT NULL, "
            "generation INTEGER NOT NULL CHECK (generation > 0), "
            "updated_at TEXT NOT NULL, PRIMARY KEY (authority_id, profile_id))"
        )
        return _table_schema_signature(legacy, "adaptive_optimizer_leases")
    finally:
        legacy.close()


@lru_cache(maxsize=1)
def _legacy_profile_state_schema_signature() -> _TableSchemaSignature:
    legacy = sqlite3.connect(":memory:", isolation_level=None)
    legacy.row_factory = sqlite3.Row
    try:
        statement = _canonical_table_statement("adaptive_profile_states").replace(
            ",\n                'rolled_back'", ""
        )
        legacy.execute(statement)
        return _table_schema_signature(legacy, "adaptive_profile_states")
    finally:
        legacy.close()


def _index_schema_signature(
    connection: sqlite3.Connection,
    index_name: str,
) -> _IndexSchemaSignature:
    sql_row = connection.execute(
        "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index_name,),
    ).fetchone()
    if sql_row is None or sql_row["sql"] is None:
        raise sqlite3.DatabaseError(f"missing named index {index_name}")
    table = str(sql_row["tbl_name"])
    list_row = next(
        (
            row
            for row in connection.execute(f'PRAGMA index_list("{table}")')
            if str(row["name"]) == index_name
        ),
        None,
    )
    if list_row is None:
        raise sqlite3.DatabaseError(f"unreadable named index {index_name}")
    columns = tuple(
        str(row["name"])
        for row in connection.execute(f'PRAGMA index_info("{index_name}")')
    )
    return _IndexSchemaSignature(
        table=table,
        unique=bool(list_row["unique"]),
        partial=bool(list_row["partial"]),
        columns=columns,
        sql=_normalize_schema_sql(str(sql_row["sql"])),
    )


@lru_cache(maxsize=1)
def _expected_schema_signatures() -> tuple[
    Mapping[str, _TableSchemaSignature],
    Mapping[str, _IndexSchemaSignature],
]:
    expected = sqlite3.connect(":memory:", isolation_level=None)
    expected.row_factory = sqlite3.Row
    try:
        expected.execute("PRAGMA foreign_keys=ON")
        for statement in _TABLE_STATEMENTS:
            expected.execute(statement)
        for statement in _INDEX_STATEMENTS:
            expected.execute(statement)
        tables = MappingProxyType({
            table: _table_schema_signature(expected, table)
            for table in sorted(_REQUIRED_TABLES)
        })
        indexes = MappingProxyType({
            index: _index_schema_signature(expected, index)
            for index in sorted(_REQUIRED_INDEXES)
        })
        return tables, indexes
    finally:
        expected.close()


def _schema_mismatch(connection: sqlite3.Connection) -> str | None:
    expected_tables, expected_indexes = _expected_schema_signatures()
    tables = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing_tables = sorted(_REQUIRED_TABLES - tables)
    if missing_tables:
        return f"missing table {missing_tables[0]}"
    for table, expected in expected_tables.items():
        if _table_schema_signature(connection, table) != expected:
            return f"table {table} has an incompatible signature"
    indexes = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    missing_indexes = sorted(_REQUIRED_INDEXES - indexes)
    if missing_indexes:
        return f"missing index {missing_indexes[0]}"
    for index, expected in expected_indexes.items():
        if _index_schema_signature(connection, index) != expected:
            return f"index {index} has an incompatible signature"
    version = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if version is None or str(version["value"]) != SCHEMA_VERSION:
        return "schema version marker is missing or stale"
    return None


def _schema_is_current(connection: sqlite3.Connection) -> bool:
    """Read-only fast path so ordinary opens never contend with a writer."""
    try:
        return _schema_mismatch(connection) is None
    except sqlite3.DatabaseError:
        return False


def _reject_unsupported_schema(connection: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "schema_meta" not in tables:
        return
    try:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError as error:
        raise UnsupportedSchemaVersion("unreadable schema_meta table") from error
    if row is None:
        return
    raw_version = str(row["value"])
    try:
        stored_version = int(raw_version)
        supported_version = int(SCHEMA_VERSION)
    except ValueError as error:
        raise UnsupportedSchemaVersion(
            f"unsupported auto-routing schema version {raw_version!r}"
        ) from error
    if stored_version > supported_version:
        raise UnsupportedSchemaVersion(
            f"unsupported auto-routing schema version {raw_version}; "
            f"this build supports {SCHEMA_VERSION}"
        )


def _stored_schema_version(connection: sqlite3.Connection) -> int | None:
    tables = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "schema_meta" not in tables:
        return None
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    return None if row is None else int(str(row["value"]))


def _canonical_table_statement(table: str) -> str:
    pattern = re.compile(
        rf"^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\s*\(",
        re.IGNORECASE,
    )
    for statement in _TABLE_STATEMENTS:
        if pattern.search(statement):
            return statement
    raise AssertionError(f"canonical DDL is missing table {table}")


def _table_column_names(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[str, ...]:
    return tuple(
        str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    )


def _copy_legacy_table_rows(
    connection: sqlite3.Connection,
    *,
    source: str,
    target: str,
) -> None:
    expected_columns = _table_column_names(connection, target)
    source_columns = _table_column_names(connection, source)
    if set(source_columns) != set(expected_columns):
        missing = sorted(set(expected_columns) - set(source_columns))
        extra = sorted(set(source_columns) - set(expected_columns))
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise UnsupportedSchemaVersion(
            f"cannot losslessly rebuild legacy table {target}: " + "; ".join(details)
        )
    columns_sql = ", ".join(f'"{column}"' for column in expected_columns)
    try:
        connection.execute(
            f'INSERT INTO "{target}" ({columns_sql}) '
            f'SELECT {columns_sql} FROM "{source}"'
        )
    except sqlite3.DatabaseError as error:
        raise UnsupportedSchemaVersion(
            f"legacy table {target} violates the schema v{SCHEMA_VERSION} contract"
        ) from error


def _rebuild_legacy_leaf_table(
    connection: sqlite3.Connection,
    table: str,
) -> None:
    expected_tables, _expected_indexes = _expected_schema_signatures()
    if _table_schema_signature(connection, table) == expected_tables[table]:
        return
    legacy = f"{table}_legacy_schema_v{SCHEMA_VERSION}"
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (legacy,),
    ).fetchone()
    if existing is not None:
        raise UnsupportedSchemaVersion(
            f"cannot rebuild {table}: stale migration table {legacy} exists"
        )
    connection.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy}"')
    connection.execute(_canonical_table_statement(table))
    _copy_legacy_table_rows(
        connection,
        source=legacy,
        target=table,
    )
    connection.execute(f'DROP TABLE "{legacy}"')


def _rebuild_legacy_catalog_tables(connection: sqlite3.Connection) -> None:
    expected_tables, _expected_indexes = _expected_schema_signatures()
    if all(
        _table_schema_signature(connection, table) == expected_tables[table]
        for table in ("catalog_snapshots", "catalog_evidence")
    ):
        return
    snapshot_legacy = f"catalog_snapshots_legacy_schema_v{SCHEMA_VERSION}"
    evidence_legacy = f"catalog_evidence_legacy_schema_v{SCHEMA_VERSION}"
    for legacy in (snapshot_legacy, evidence_legacy):
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (legacy,),
            ).fetchone()
            is not None
        ):
            raise UnsupportedSchemaVersion(
                f"cannot rebuild catalog tables: stale migration table {legacy} exists"
            )
    connection.execute(f'ALTER TABLE "catalog_evidence" RENAME TO "{evidence_legacy}"')
    connection.execute(f'ALTER TABLE "catalog_snapshots" RENAME TO "{snapshot_legacy}"')
    connection.execute(_canonical_table_statement("catalog_snapshots"))
    connection.execute(_canonical_table_statement("catalog_evidence"))
    _copy_legacy_table_rows(
        connection,
        source=snapshot_legacy,
        target="catalog_snapshots",
    )
    _copy_legacy_table_rows(
        connection,
        source=evidence_legacy,
        target="catalog_evidence",
    )
    connection.execute(f'DROP TABLE "{evidence_legacy}"')
    connection.execute(f'DROP TABLE "{snapshot_legacy}"')


def _normalize_legacy_schema(connection: sqlite3.Connection) -> None:
    """Losslessly rebuild historical leaf shapes that ALTER cannot harden."""
    _rebuild_legacy_catalog_tables(connection)
    for table in (
        "budget_ledger",
        "route_epochs",
        "decision_operations",
        "session_route_bindings",
        "activation_receipts",
    ):
        _rebuild_legacy_leaf_table(connection, table)


def _reject_incompatible_partial_evidence_schema(
    connection: sqlite3.Connection,
    stored_version: int | None,
) -> None:
    """Fail closed if a pre-v6 database already has a noncanonical v6 object."""
    if stored_version is not None and stored_version >= 6:
        return
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'evidence_events'"
    ).fetchone()
    if row is None:
        return
    expected_tables, _expected_indexes = _expected_schema_signatures()
    try:
        compatible = (
            _table_schema_signature(connection, "evidence_events")
            == expected_tables["evidence_events"]
        )
    except sqlite3.DatabaseError:
        compatible = False
    if not compatible:
        raise UnsupportedSchemaVersion(
            "pre-v6 database has an incompatible partial evidence_events schema"
        )


_ADAPTATION_TABLES = frozenset({
    "adaptive_profile_revisions",
    "adaptive_profile_states",
    "adaptive_lifecycle_events",
    "adaptive_canary_assignments",
    "adaptive_optimizer_leases",
})

_MANAGEMENT_TABLES = frozenset({
    "management_controls",
    "management_profile_states",
    "management_revisions",
    "management_lifecycle_events",
    "management_canary_assignments",
    "management_leases",
    "management_config_receipts",
})

_MANAGEMENT_FINALIZATION_TABLES = frozenset({
    "management_lifecycle_finalizations",
})


def _reject_incompatible_finalization_schema(
    connection: sqlite3.Connection,
    stored_version: int | None,
) -> None:
    """Permit additive v8 migration but reject forged or partial v9 state."""
    present = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if str(row["name"]) in _MANAGEMENT_FINALIZATION_TABLES
    }
    if not present:
        if stored_version is None or stored_version < 9:
            return
        raise UnsupportedSchemaVersion(
            "v9 database has an incompatible lifecycle-finalization schema"
        )
    expected_tables, _expected_indexes = _expected_schema_signatures()
    for table in sorted(_MANAGEMENT_FINALIZATION_TABLES):
        if table not in present:
            raise UnsupportedSchemaVersion(
                "v9 database has an incompatible lifecycle-finalization schema"
            )
        try:
            compatible = (
                _table_schema_signature(connection, table)
                == expected_tables[table]
            )
        except sqlite3.DatabaseError:
            compatible = False
        if not compatible:
            raise UnsupportedSchemaVersion(
                f"v9 database has an incompatible partial {table} schema"
            )


def _reject_incompatible_partial_management_schema(
    connection: sqlite3.Connection,
    stored_version: int | None,
) -> None:
    """Reject a partial or forged v8 management surface before any mutation."""
    present = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if str(row["name"]) in _MANAGEMENT_TABLES
    }
    if not present and (stored_version is None or stored_version < 8):
        return
    missing = sorted(_MANAGEMENT_TABLES - present)
    if missing:
        version_label = (
            "v8 database"
            if stored_version is not None and stored_version >= 8
            else "pre-v8 database"
        )
        raise UnsupportedSchemaVersion(
            f"{version_label} has an incompatible partial management schema "
            f"missing {missing[0]}"
        )
    expected_tables, _expected_indexes = _expected_schema_signatures()
    for table in sorted(_MANAGEMENT_TABLES):
        try:
            compatible = (
                _table_schema_signature(connection, table) == expected_tables[table]
            )
        except sqlite3.DatabaseError:
            compatible = False
        if not compatible:
            raise UnsupportedSchemaVersion(
                f"v8 database has an incompatible partial {table} schema"
            )


def _reject_incompatible_partial_adaptation_schema(
    connection: sqlite3.Connection,
    stored_version: int | None,
) -> None:
    """Reject a partial or forged v7 adaptation surface before any mutation."""
    present = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        if str(row["name"]) in _ADAPTATION_TABLES
    }
    if not present:
        if stored_version is None or stored_version < 7:
            return
        raise UnsupportedSchemaVersion(
            "v7 database has an incompatible partial adaptation schema "
            f"missing {sorted(_ADAPTATION_TABLES)[0]}"
        )
    missing = sorted(_ADAPTATION_TABLES - present)
    if missing:
        version_label = (
            "v7 database" if stored_version is not None and stored_version >= 7 else "pre-v7 database"
        )
        raise UnsupportedSchemaVersion(
            f"{version_label} has an incompatible partial adaptation schema "
            f"including {sorted(present)[0]}; "
            f"missing {missing[0]}"
        )
    expected_tables, _expected_indexes = _expected_schema_signatures()
    for table in sorted(_ADAPTATION_TABLES):
        try:
            compatible = (
                _table_schema_signature(connection, table) == expected_tables[table]
            )
        except sqlite3.DatabaseError:
            compatible = False
        if not compatible:
            version_label = (
                "v7 database" if stored_version is not None and stored_version >= 7 else "pre-v7 database"
            )
            raise UnsupportedSchemaVersion(
                f"{version_label} has an incompatible partial {table} schema"
            )


def init_db(connection: sqlite3.Connection) -> None:
    """Create or additively migrate the durable schema, idempotently."""
    if _schema_is_current(connection):
        return
    _reject_unsupported_schema(connection)
    stored_version = _stored_schema_version(connection)
    with write_txn(connection) as transaction:
        _reject_incompatible_partial_evidence_schema(transaction, stored_version)
        _reject_incompatible_partial_management_schema(transaction, stored_version)
        _reject_incompatible_finalization_schema(transaction, stored_version)
        _reject_incompatible_partial_adaptation_schema(transaction, stored_version)
        evidence_statement = _canonical_table_statement("evidence_events")
        for statement in _TABLE_STATEMENTS:
            if statement == evidence_statement:
                continue
            transaction.execute(statement)
        _migrate_additive_columns(transaction)
        if stored_version is None or stored_version < int(SCHEMA_VERSION):
            _normalize_legacy_schema(transaction)
        # Create the v6 child table only after historical parent tables have
        # completed any lossless rename/rebuild migration. Otherwise SQLite
        # rewrites its foreign-key targets to the temporary legacy names.
        transaction.execute(evidence_statement)
        for statement in _INDEX_STATEMENTS:
            transaction.execute(statement)
        transaction.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
        mismatch = _schema_mismatch(transaction)
        if mismatch is not None:
            raise UnsupportedSchemaVersion(
                f"auto-routing schema version {SCHEMA_VERSION} is malformed: {mismatch}"
            )


def _normalize_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_json(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (tuple, list)):
        return [_normalize_json(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _checksum(document_json: str) -> str:
    return hashlib.sha256(document_json.encode("utf-8")).hexdigest()


def management_recovery_event_id(
    *,
    receipt_id: str,
    failed_revision_id: str,
    profile_id: str,
    restored_authority_id: str,
    backup_checksum: str,
) -> str:
    """Bind terminal recovery evidence to the exact local restoration."""
    return _checksum(
        _canonical_json({
            "action": "management-config-recovered-v1",
            "backup_checksum": backup_checksum,
            "failed_revision_id": failed_revision_id,
            "profile_id": profile_id,
            "receipt_id": receipt_id,
            "restored_authority_id": restored_authority_id,
        })
    )


def management_restore_started_event_id(
    *,
    receipt_id: str,
    failed_revision_id: str,
    profile_id: str,
    restored_authority_id: str,
    backup_checksum: str,
) -> str:
    """Bind durable pre-I/O intent to one resulting-authority restoration."""
    return _checksum(
        _canonical_json({
            "action": "management-config-restore-started-v1",
            "backup_checksum": backup_checksum,
            "failed_revision_id": failed_revision_id,
            "profile_id": profile_id,
            "receipt_id": receipt_id,
            "restored_authority_id": restored_authority_id,
        })
    )


def _verify_checksum(identifier: str, document_json: str, checksum: str) -> None:
    if checksum != _checksum(document_json):
        raise RevisionChecksumError(identifier)


def _assert_canonical(identifier: str, document_json: str) -> Any:
    try:
        decoded = json.loads(document_json)
        canonical = _canonical_json(decoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RevisionChecksumError(identifier) from error
    if canonical != document_json:
        raise RevisionChecksumError(identifier)
    return decoded


def _json_path(parts: tuple[str | int, ...]) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _looks_like_raw_url(value: str) -> bool:
    stripped = value.strip()
    return "://" in stripped or stripped.startswith("//")


def _looks_like_secret_material(value: str) -> bool:
    stripped = value.strip()
    return (
        "-----BEGIN " in stripped
        or _JWT.search(stripped) is not None
        or _SECRET_PREFIX.search(stripped) is not None
        or _SECRET_ASSIGNMENT.search(stripped) is not None
    )


def _looks_like_public_identifier_path(value: str) -> bool:
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
        or "\\" in normalized
    )


def _is_unsafe_json_field(field: str, value: Any) -> bool:
    normalized = field.strip().lower()
    if normalized in _IDENTITY_FIELDS:
        return False
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", field.strip())
    words = tuple(re.findall(r"[a-z0-9]+", separated.lower()))
    compact = "".join(words)
    if compact in _IDENTITY_FIELD_COMPACT:
        return True
    if normalized in _UNSAFE_JSON_FIELDS or compact in _UNSAFE_FIELD_COMPACT:
        return True
    parts = frozenset(words)
    if parts & {
        "auth",
        "authentication",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "endpoint",
        "headers",
        "key",
        "password",
        "prompt",
        "response",
        "secret",
        "sig",
        "signature",
        "url",
    }:
        return True
    if "token" in parts:
        return True
    if "tokens" in parts:
        return (
            normalized not in _NUMERIC_TOKEN_FIELDS
            or isinstance(value, bool)
            or (value is not None and not isinstance(value, (int, float)))
        )
    return False


def _assert_safe_identity(field: str, value: Any, *, path: str) -> None:
    if value is None and field in _OPTIONAL_IDENTITY_FIELDS:
        return
    if not isinstance(value, str):
        raise UnsafeStoredContent(f"identity field {path} must be a string")
    if not value:
        if field in _OPTIONAL_IDENTITY_FIELDS:
            return
        raise UnsafeStoredContent(f"identity field {path} must be named")
    lowered_parts = frozenset(re.split(r"[^a-z0-9]+", value.lower()))
    named_auth_identity = False
    if field == "auth_identity" and ":" in value:
        auth_kind, auth_name = value.split(":", 1)
        named_auth_identity = (
            auth_kind in {"api-key", "subscription", "pool", "local"}
            and bool(auth_name)
            and _IDENTITY_NAME.fullmatch(auth_name) is not None
            and not _looks_like_secret_material(auth_name)
        )
    durable_identity = field in _DURABLE_IDENTITY_FIELDS
    operation_identity = field == "operation_key"
    if (
        _looks_like_raw_url(value)
        or value.lower().startswith("data:")
        or (durable_identity and _looks_like_public_identifier_path(value))
        or (_looks_like_secret_material(value) and not named_auth_identity)
        or _EMAIL_ADDRESS.fullmatch(value) is not None
        or (
            field == "endpoint_identity"
            and _RAW_ENDPOINT_IDENTITY.match(value) is not None
        )
        or lowered_parts & {"apikey", "password", "secret", "token"}
    ):
        raise UnsafeStoredContent(
            f"identity field {path} contains secret or endpoint material"
        )
    if durable_identity:
        identity_pattern = _DURABLE_IDENTIFIER
    elif operation_identity:
        identity_pattern = _DURABLE_OPERATION_KEY
    elif field == "profile_id":
        identity_pattern = _PROFILE_IDENTIFIER
    else:
        identity_pattern = _IDENTITY_NAME
    if not identity_pattern.fullmatch(value):
        raise UnsafeStoredContent(
            f"identity field {path} must be a named or fingerprinted identity"
        )
    if (
        not durable_identity
        and not operation_identity
        and field != "profile_id"
        and len(value) >= 32
        and re.fullmatch(r"[A-Za-z0-9_+/=]+", value) is not None
        and _FINGERPRINT.fullmatch(value) is None
    ):
        raise UnsafeStoredContent(
            f"identity field {path} contains opaque secret-shaped material"
        )


def _assert_safe_catalog_url(value: Any, *, path: str) -> None:
    if not isinstance(value, str):
        raise UnsafeStoredContent(f"catalog source_url {path} must be a URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError as error:
        raise UnsafeStoredContent(
            f"catalog source_url {path} must be a public http(s) URL"
        ) from error
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise UnsafeStoredContent(
            f"catalog source_url {path} must be a public http(s) URL"
        )
    normalized_host = hostname.casefold().rstrip(".")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        address = None
    if (
        normalized_host in {"localhost", "localhost.localdomain"}
        or normalized_host.endswith((".localhost", ".local", ".internal", ".test"))
        or _NUMERIC_HOST.fullmatch(normalized_host) is not None
        or (address is not None and not address.is_global)
    ):
        raise UnsafeStoredContent(
            f"catalog source_url {path} must be a public http(s) URL"
        )
    parameters = parse_qsl(parsed.query, keep_blank_values=True) + parse_qsl(
        parsed.fragment,
        keep_blank_values=True,
    )
    decoded = unquote(value)
    url_values = [
        *(unquote(part) for part in parsed.path.split("/") if part),
        *(unquote(item) for _name, item in parameters),
    ]
    if (
        _SECRET_ASSIGNMENT.search(decoded) is not None
        or any(_is_unsafe_json_field(name, item) for name, item in parameters)
        or any(
            _looks_like_raw_url(item) or _looks_like_secret_material(item)
            for item in url_values
        )
    ):
        raise UnsafeStoredContent(
            f"catalog source_url {path} contains credential material"
        )


def _assert_content_free(
    value: Any,
    *,
    writer: _ContentWriter,
    _path: tuple[str | int, ...] = (),
) -> None:
    normalized = _normalize_json(value)
    if isinstance(normalized, Mapping):
        for key, item in normalized.items():
            field = str(key).strip()
            lowered = field.lower()
            item_path = (*_path, str(key))
            rendered_path = _json_path(item_path)
            if (
                writer == "catalog"
                and lowered == "source_url"
                and _path in {(), ("evidence",)}
            ):
                _assert_safe_catalog_url(item, path=rendered_path)
                continue
            if writer == "inventory" and not _path and lowered == "key":
                if not isinstance(item, Mapping):
                    raise UnsafeStoredContent(
                        f"runtime identity {rendered_path} must be structured"
                    )
                _assert_content_free(item, writer=writer, _path=item_path)
                continue
            if (
                writer == "catalog"
                and _path == ("applicability",)
                and lowered == "runtime_id"
                and item is None
            ):
                continue
            if _is_unsafe_json_field(field, item):
                raise UnsafeStoredContent(
                    f"raw/private field {rendered_path} cannot be persisted"
                )
            if lowered in _IDENTITY_FIELDS:
                _assert_safe_identity(lowered, item, path=rendered_path)
                continue
            _assert_content_free(item, writer=writer, _path=item_path)
    elif isinstance(normalized, list):
        for index, item in enumerate(normalized):
            _assert_content_free(item, writer=writer, _path=(*_path, index))
    elif isinstance(normalized, str):
        if _looks_like_raw_url(normalized):
            raise UnsafeStoredContent(
                f"raw endpoint or URL {_json_path(_path)} cannot be persisted"
            )
        if _looks_like_secret_material(normalized):
            raise UnsafeStoredContent(
                f"secret-shaped value {_json_path(_path)} cannot be persisted"
            )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            key: _freeze_json(item) for key, item in sorted(value.items())
        })
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _timestamp(value: datetime | date | float | int | None = None) -> str:
    if value is None:
        moment = datetime.now(UTC)
    elif isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        moment = moment.astimezone(UTC)
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day, tzinfo=UTC)
    else:
        moment = datetime.fromtimestamp(float(value), UTC)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _day(value: datetime | date | float | int | str) -> date:
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    return datetime.fromtimestamp(float(value), UTC).date()


def _money(value: float, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite non-negative number") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return result


def current_process_start_token() -> str:
    """Return Hermes's cross-platform PID-reuse guard for this process."""
    from gateway.status import get_process_start_time

    token = get_process_start_time(os.getpid())
    if token is None:  # pragma: no cover - psutil is a hard dependency
        raise StoreError("cannot determine current process start token")
    return str(token)


def _process_owner_is_live(pid: int, start_token: str) -> bool:
    from gateway.status import get_process_start_time

    current = get_process_start_time(pid)
    if current is not None:
        return str(current) == start_token
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except Exception:  # pragma: no cover - conservative dependency failure
        return True


def _operation_key(
    *,
    scope: Literal["fresh_session", "delegation"],
    session_id: str,
    operation_id: str | None,
    task_index: int | None,
) -> str:
    if scope not in {"fresh_session", "delegation"}:
        raise ValueError("scope must be fresh_session or delegation")
    _require_durable_identifier(session_id, field_name="session_id")
    if scope == "fresh_session":
        if operation_id is not None or task_index is not None:
            raise ValueError(
                "fresh-session operation cannot carry operation_id or task_index"
            )
        return f"fresh:{session_id}"
    if operation_id is None:
        raise ValueError(
            "delegation operation requires operation_id and non-negative task_index"
        )
    _require_durable_identifier(operation_id, field_name="operation_id")
    if (
        isinstance(task_index, bool)
        or not isinstance(task_index, int)
        or task_index < 0
        or task_index > MAX_TASK_INDEX
    ):
        raise ValueError(
            f"task_index must be a strict integer from 0 to {MAX_TASK_INDEX}"
        )
    return f"delegation:{operation_id}:{task_index}"


def _operation_document(
    *,
    operation_key: str,
    claim_id: str,
    scope: str,
    session_id: str,
    operation_id: str | None,
    task_index: int | None,
    facts_hash: str,
    owner_pid: int,
    owner_start_token: str,
    lease_expires_at: float,
    status: str,
    decision_id: str | None,
    claimed_at: str,
    updated_at: str,
) -> dict[str, Any]:
    return {
        "operation_key": operation_key,
        "claim_id": claim_id,
        "scope": scope,
        "session_id": session_id,
        "operation_id": operation_id,
        "task_index": task_index,
        "facts_hash": facts_hash,
        "owner_pid": owner_pid,
        "owner_start_token": owner_start_token,
        "lease_expires_at": lease_expires_at,
        "status": status,
        "decision_id": decision_id,
        "claimed_at": claimed_at,
        "updated_at": updated_at,
    }


def _receipt_document(receipt: ActivationReceipt) -> dict[str, Any]:
    return {
        "receipt_id": receipt.receipt_id,
        "authority_id": receipt.authority_id,
        "config_sha": receipt.config_sha,
        "inventory_contract_sha": receipt.inventory_contract_sha,
        "inventory_revision": receipt.inventory_revision,
        "adapter_capability_sha": receipt.adapter_capability_sha,
        "created_at": receipt.created_at,
    }


def _binding_document(binding: SessionRouteBinding) -> dict[str, Any]:
    return {
        "session_id": binding.session_id,
        "binding_kind": binding.binding_kind,
        "projection_mode": binding.projection_mode,
        "decision_id": binding.decision_id,
        "runtime_id": binding.runtime_id,
        "manual_pin_source": binding.manual_pin_source,
        "current_epoch": binding.current_epoch,
        "continuation_root": binding.continuation_root,
        "parent_session_id": binding.parent_session_id,
        "continuation_reason": binding.continuation_reason,
        "created_at": binding.created_at,
    }


def _binding_row_document(row: sqlite3.Row) -> dict[str, Any]:
    return _binding_document(
        SessionRouteBinding(
            session_id=str(row["session_id"]),
            binding_kind=str(row["binding_kind"]),  # type: ignore[arg-type]
            projection_mode=str(row["projection_mode"]),  # type: ignore[arg-type]
            decision_id=(
                None if row["decision_id"] is None else str(row["decision_id"])
            ),
            runtime_id=str(row["runtime_id"]),
            manual_pin_source=(
                None
                if row["manual_pin_source"] is None
                else str(row["manual_pin_source"])
            ),
            current_epoch=int(row["current_epoch"]),
            continuation_root=(
                None
                if row["continuation_root"] is None
                else str(row["continuation_root"])
            ),
            parent_session_id=(
                None
                if row["parent_session_id"] is None
                else str(row["parent_session_id"])
            ),
            continuation_reason=(
                None
                if row["continuation_reason"] is None
                else str(row["continuation_reason"])
            ),
            created_at=str(row["created_at"]),
        )
    )


def _route_epoch_document(epoch: RouteEpoch) -> dict[str, Any]:
    return {
        "route_epoch_id": epoch.route_epoch_id,
        "session_id": epoch.session_id,
        "decision_id": epoch.decision_id,
        "epoch_number": epoch.epoch_number,
        "runtime_id": epoch.runtime_id,
        "reason_code": epoch.reason_code,
        "started_at": epoch.started_at,
        "ended_at": epoch.ended_at,
        "provider_started": epoch.provider_started,
        "api_request_id": epoch.api_request_id,
        "provider_started_at": epoch.provider_started_at,
    }


def _route_epoch_row_document(row: sqlite3.Row) -> dict[str, Any]:
    return _route_epoch_document(
        RouteEpoch(
            route_epoch_id=str(row["route_epoch_id"]),
            session_id=str(row["session_id"]),
            decision_id=str(row["decision_id"]),
            epoch_number=int(row["epoch_number"]),
            runtime_id=str(row["runtime_id"]),
            reason_code=str(row["reason_code"]),
            started_at=str(row["started_at"]),
            ended_at=None if row["ended_at"] is None else str(row["ended_at"]),
            provider_started=bool(row["provider_started"]),
            api_request_id=(
                None if row["api_request_id"] is None else str(row["api_request_id"])
            ),
            provider_started_at=(
                None
                if row["provider_started_at"] is None
                else str(row["provider_started_at"])
            ),
        )
    )


class RoutingStore:
    """One long-lived connection to one profile's auto-routing state."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self.connection = connection

    @classmethod
    def open(
        cls,
        *,
        home: str | Path | None = None,
        path: str | Path | None = None,
        allow_cross_thread_close: bool = False,
    ) -> "RoutingStore":
        db_path = _state_path(path, home=home)
        connection = connect(
            db_path,
            allow_cross_thread_close=allow_cross_thread_close,
        )
        try:
            init_db(connection)
        except BaseException:
            connection.close()
            raise
        return cls(db_path, connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "RoutingStore":
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        row = self.connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise UnsupportedSchemaVersion("schema version marker is missing")
        return int(str(row["value"]))

    @contextlib.contextmanager
    def write_txn(self) -> Iterator[sqlite3.Connection]:
        with write_txn(self.connection) as transaction:
            yield transaction

    @contextlib.contextmanager
    def observer_evidence_write_txn(self) -> Iterator[sqlite3.Connection]:
        """Acquire a post-turn evidence write without delaying a user turn."""
        with observer_evidence_write_txn(self.connection) as transaction:
            yield transaction

    @staticmethod
    def _validated_management_record(
        model_type: type[BaseModel],
        value: Any,
    ) -> tuple[BaseModel, str, str]:
        payload = (
            value.model_dump(mode="json", by_alias=True)
            if isinstance(value, BaseModel)
            else value
        )
        validated = model_type.model_validate(payload)
        _assert_content_free(validated, writer="management")
        document_json = _canonical_json(validated)
        checksum = _checksum(document_json)
        _assert_canonical(model_type.__name__, document_json)
        _verify_checksum(model_type.__name__, document_json, checksum)
        return validated, document_json, checksum

    @staticmethod
    def _management_row_matches(
        row: sqlite3.Row,
        expected: Mapping[str, Any],
        *,
        identifier: str,
    ) -> None:
        for field_name, value in expected.items():
            stored = row[field_name]
            if value is None:
                if stored is not None:
                    raise RevisionChecksumError(identifier)
            elif isinstance(value, bool):
                if int(stored) != int(value):
                    raise RevisionChecksumError(identifier)
            elif isinstance(value, int):
                if int(stored) != value:
                    raise RevisionChecksumError(identifier)
            elif str(stored) != str(value):
                raise RevisionChecksumError(identifier)

    @classmethod
    def _management_control_from_row(
        cls,
        row: sqlite3.Row,
    ) -> ManagementControl:
        identifier = str(row["management_authority_id"])
        document_json = str(row["document_json"])
        try:
            _verify_checksum(identifier, document_json, str(row["checksum"]))
            document = _assert_canonical(identifier, document_json)
            control = ManagementControl.model_validate(document)
            _assert_content_free(control, writer="management")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        cls._management_row_matches(
            row,
            {
                "management_authority_id": control.management_authority_id,
                "frozen": control.frozen,
                "changes_today": control.changes_today,
                "generation": control.generation,
                "updated_at": control.updated_at,
            },
            identifier=identifier,
        )
        return control

    @classmethod
    def _empty_management_control(
        cls,
        management_authority_id: str,
    ) -> ManagementControl:
        value, _document_json, _checksum_value = cls._validated_management_record(
            ManagementControl,
            {
                "management_authority_id": management_authority_id,
                "frozen": False,
                "changes_today": 0,
                "generation": 0,
                "updated_at": _EMPTY_PROFILE_STATE_TIMESTAMP,
            },
        )
        return value  # type: ignore[return-value]

    @classmethod
    def _management_control_in_txn(
        cls,
        connection: sqlite3.Connection,
        management_authority_id: str,
    ) -> tuple[ManagementControl, bool]:
        row = connection.execute(
            "SELECT * FROM management_controls WHERE management_authority_id=?",
            (management_authority_id,),
        ).fetchone()
        if row is None:
            return cls._empty_management_control(management_authority_id), False
        return cls._management_control_from_row(row), True

    @classmethod
    def _ensure_management_control(
        cls,
        connection: sqlite3.Connection,
        management_authority_id: str,
    ) -> ManagementControl:
        control, exists = cls._management_control_in_txn(
            connection, management_authority_id
        )
        if exists:
            return control
        validated, document_json, checksum = cls._validated_management_record(
            ManagementControl, control
        )
        control = validated  # type: ignore[assignment]
        connection.execute(
            "INSERT INTO management_controls "
            "(management_authority_id, frozen, changes_today, generation, updated_at, "
            "document_json, checksum) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                control.management_authority_id,
                int(control.frozen),
                control.changes_today,
                control.generation,
                control.updated_at,
                document_json,
                checksum,
            ),
        )
        return control

    def read_management_control(
        self,
        management_authority_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ManagementControl:
        source = self.connection if connection is None else connection
        row = source.execute(
            "SELECT * FROM management_controls WHERE management_authority_id=?",
            (management_authority_id,),
        ).fetchone()
        if row is None:
            return self._empty_management_control(management_authority_id)
        return self._management_control_from_row(row)

    @classmethod
    def _management_revision_from_row(
        cls,
        row: sqlite3.Row,
    ) -> ManagementRevision:
        identifier = str(row["revision_id"])
        document_json = str(row["document_json"])
        try:
            _verify_checksum(identifier, document_json, str(row["checksum"]))
            document = _assert_canonical(identifier, document_json)
            revision = ManagementRevision.model_validate(document)
            _assert_content_free(revision, writer="management")
            patches_json = str(row["patches_json"])
            runtime_scores_json = str(row["runtime_scores_json"])
            _assert_canonical(identifier, patches_json)
            _assert_canonical(identifier, runtime_scores_json)
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        cls._management_row_matches(
            row,
            {
                "revision_id": revision.revision_id,
                "preceding_authority_id": revision.preceding_authority_id,
                "resulting_authority_id": revision.resulting_authority_id,
                "management_authority_id": revision.management_authority_id,
                "parent_revision_id": revision.parent_revision_id,
                "ranking_pack_id": revision.ranking_pack.ranking_pack_id,
                "ranking_pack_sha256": revision.ranking_pack.ranking_pack_sha256,
                "ranking_pack_schema_version": revision.ranking_pack.schema_version,
                "ranking_pack_verified_at": revision.ranking_pack.verified_at,
                "inventory_revision": revision.inventory_revision,
                "inventory_fingerprint": revision.inventory_fingerprint,
                "management_epoch": revision.management_epoch,
                "action": revision.action,
                "created_at": revision.created_at,
            },
            identifier=identifier,
        )
        if (
            patches_json != _canonical_json(revision.patches)
            or runtime_scores_json != _canonical_json(revision.runtime_scores)
        ):
            raise RevisionChecksumError(identifier)
        admitted_profiles_json = row["admitted_profiles_json"]
        admitted_utc_day = row["admitted_utc_day"]
        admission_checksum = row["admission_checksum"]
        admission_values = (
            admitted_profiles_json,
            admitted_utc_day,
            admission_checksum,
        )
        if any(value is not None for value in admission_values):
            if any(value is None for value in admission_values):
                raise RevisionChecksumError(identifier)
            day = str(admitted_utc_day)
            try:
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError("noncanonical day")
                admitted_profiles = _assert_canonical(
                    identifier, str(admitted_profiles_json)
                )
                if (
                    not isinstance(admitted_profiles, list)
                    or not admitted_profiles
                    or any(not isinstance(item, str) for item in admitted_profiles)
                    or admitted_profiles != sorted(set(admitted_profiles))
                ):
                    raise ValueError("invalid admitted profiles")
            except ValueError as error:
                raise RevisionChecksumError(identifier) from error
            if set(admitted_profiles) != {
                patch.profile_id
                for patch in revision.patches
                if patch.before_runtime_ids != patch.after_runtime_ids
                or "fallback_primary_challenger" in patch.reason_codes
            }:
                raise RevisionChecksumError(identifier)
            admission_json = _canonical_json({
                "profile_ids": admitted_profiles,
                "revision_id": revision.revision_id,
                "utc_day": day,
            })
            _verify_checksum(identifier, admission_json, str(admission_checksum))
        return revision

    @classmethod
    def _validate_management_revision_parent_chain(
        cls,
        connection: sqlite3.Connection,
        revision: ManagementRevision,
    ) -> None:
        identifier = revision.revision_id
        seen = {identifier}
        child = revision
        while child.parent_revision_id is not None:
            if child.parent_revision_id in seen:
                raise RevisionChecksumError(identifier)
            seen.add(child.parent_revision_id)
            row = connection.execute(
                "SELECT * FROM management_revisions WHERE revision_id=?",
                (child.parent_revision_id,),
            ).fetchone()
            if row is None:
                raise RevisionChecksumError(identifier)
            parent = cls._management_revision_from_row(row)
            if (
                parent.management_authority_id != child.management_authority_id
                or parent.resulting_authority_id != child.preceding_authority_id
                or parent.management_epoch + 1 != child.management_epoch
            ):
                raise RevisionChecksumError(identifier)
            child = parent
        root_rows = connection.execute(
            "SELECT revision_id FROM management_revisions "
            "WHERE management_authority_id=? AND parent_revision_id IS NULL",
            (revision.management_authority_id,),
        ).fetchall()
        if len(root_rows) != 1 or str(root_rows[0]["revision_id"]) != child.revision_id:
            raise RevisionChecksumError(identifier)

    @classmethod
    def _require_management_revision(
        cls,
        connection: sqlite3.Connection,
        revision_id: str,
        *,
        management_authority_id: str | None = None,
        profile_id: str | None = None,
    ) -> ManagementRevision:
        row = connection.execute(
            "SELECT * FROM management_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise ImmutableRecordConflict(
                f"management revision {revision_id!r} does not exist"
            )
        revision = cls._management_revision_from_row(row)
        cls._validate_management_revision_parent_chain(connection, revision)
        if (
            management_authority_id is not None
            and revision.management_authority_id != management_authority_id
        ):
            raise ImmutableRecordConflict(
                f"management revision {revision_id!r} has a different authority"
            )
        if profile_id is not None and profile_id not in {
            patch.profile_id for patch in revision.patches
        }:
            raise ImmutableRecordConflict(
                f"management revision {revision_id!r} has a different profile"
            )
        return revision

    @classmethod
    def _insert_management_revision(
        cls,
        connection: sqlite3.Connection,
        revision: ManagementRevision,
        *,
        admitted_profile_ids: Sequence[str] | None = None,
        admitted_utc_day: str | None = None,
    ) -> ManagementRevision:
        validated, document_json, checksum = cls._validated_management_record(
            ManagementRevision, revision
        )
        revision = validated  # type: ignore[assignment]
        cls._ensure_management_control(
            connection, revision.management_authority_id
        )
        if revision.parent_revision_id is not None:
            parent = cls._require_management_revision(
                connection,
                revision.parent_revision_id,
                management_authority_id=revision.management_authority_id,
            )
            if (
                parent.resulting_authority_id != revision.preceding_authority_id
                or parent.management_epoch + 1 != revision.management_epoch
            ):
                raise ImmutableRecordConflict(
                    "management parent authority and epoch must exactly precede child"
                )
        existing = connection.execute(
            "SELECT * FROM management_revisions WHERE revision_id=?",
            (revision.revision_id,),
        ).fetchone()
        if existing is not None:
            stored = cls._management_revision_from_row(existing)
            cls._validate_management_revision_parent_chain(connection, stored)
            if stored == revision:
                return stored
            raise ImmutableRecordConflict(
                f"management revision {revision.revision_id!r} already has other content"
            )
        if revision.parent_revision_id is None and connection.execute(
            "SELECT 1 FROM management_revisions "
            "WHERE management_authority_id=? LIMIT 1",
            (revision.management_authority_id,),
        ).fetchone() is not None:
            raise ImmutableRecordConflict(
                "management revision after the authority root requires a parent"
            )
        if (admitted_profile_ids is None) != (admitted_utc_day is None):
            raise ValueError("management admission profiles and day must be supplied together")
        admission_checksum = None
        admitted_profiles_json = None
        if admitted_profile_ids is not None and admitted_utc_day is not None:
            admitted_profiles = tuple(sorted(set(admitted_profile_ids)))
            material_profile_ids = {
                patch.profile_id
                for patch in revision.patches
                if patch.before_runtime_ids != patch.after_runtime_ids
                or "fallback_primary_challenger" in patch.reason_codes
            }
            if (
                set(admitted_profiles) != material_profile_ids
                or len(admitted_profiles) != len(material_profile_ids)
            ):
                raise ImmutableRecordConflict(
                    "management admission profiles must exactly match revision patches"
                )
            admitted_profiles_json = _canonical_json(admitted_profiles)
            admission_checksum = _checksum(_canonical_json({
                "profile_ids": admitted_profiles,
                "revision_id": revision.revision_id,
                "utc_day": admitted_utc_day,
            }))
        try:
            connection.execute(
                "INSERT INTO management_revisions "
                "(revision_id, preceding_authority_id, resulting_authority_id, "
                "management_authority_id, parent_revision_id, ranking_pack_id, "
                "ranking_pack_sha256, ranking_pack_schema_version, "
                "ranking_pack_verified_at, inventory_revision, inventory_fingerprint, "
                "management_epoch, action, patches_json, runtime_scores_json, "
                "admitted_profiles_json, admitted_utc_day, admission_checksum, "
                "document_json, checksum, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision.revision_id,
                    revision.preceding_authority_id,
                    revision.resulting_authority_id,
                    revision.management_authority_id,
                    revision.parent_revision_id,
                    revision.ranking_pack.ranking_pack_id,
                    revision.ranking_pack.ranking_pack_sha256,
                    revision.ranking_pack.schema_version,
                    revision.ranking_pack.verified_at,
                    revision.inventory_revision,
                    revision.inventory_fingerprint,
                    revision.management_epoch,
                    revision.action,
                    _canonical_json(revision.patches),
                    _canonical_json(revision.runtime_scores),
                    admitted_profiles_json,
                    admitted_utc_day,
                    admission_checksum,
                    document_json,
                    checksum,
                    revision.created_at,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ImmutableRecordConflict(
                "management revision identity or epoch already has other content"
            ) from error
        return revision

    def publish_management_revision(
        self,
        revision: ManagementRevision,
    ) -> ManagementRevision:
        with self.write_txn() as connection:
            return self._insert_management_revision(connection, revision)

    def read_management_revision(
        self,
        revision_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ManagementRevision | None:
        source = self.connection if connection is None else connection
        row = source.execute(
            "SELECT * FROM management_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            return None
        revision = self._management_revision_from_row(row)
        self._validate_management_revision_parent_chain(source, revision)
        return revision

    @classmethod
    def _management_event_from_row(
        cls,
        row: sqlite3.Row,
    ) -> ManagementLifecycleEvent:
        identifier = str(row["event_id"])
        document_json = str(row["document_json"])
        try:
            _verify_checksum(identifier, document_json, str(row["checksum"]))
            document = _assert_canonical(identifier, document_json)
            event = ManagementLifecycleEvent.model_validate(document)
            _assert_content_free(event, writer="management")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        cls._management_row_matches(
            row,
            {
                "event_id": event.event_id,
                "management_authority_id": event.management_authority_id,
                "profile_id": event.profile_id,
                "revision_id": event.revision_id,
                "event_type": event.event_type,
                "reason_code": event.reason_code,
                "created_at": event.created_at,
            },
            identifier=identifier,
        )
        return event

    @classmethod
    def _append_management_event(
        cls,
        connection: sqlite3.Connection,
        value: ManagementLifecycleEvent,
    ) -> ManagementLifecycleEvent:
        validated, document_json, checksum = cls._validated_management_record(
            ManagementLifecycleEvent, value
        )
        event = validated  # type: ignore[assignment]
        cls._ensure_management_control(
            connection, event.management_authority_id
        )
        if event.revision_id is not None:
            cls._require_management_revision(
                connection,
                event.revision_id,
                management_authority_id=event.management_authority_id,
                profile_id=event.profile_id,
            )
        existing = connection.execute(
            "SELECT * FROM management_lifecycle_events WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
        if existing is not None:
            stored = cls._management_event_from_row(existing)
            if stored == event:
                return stored
            raise ImmutableRecordConflict(
                f"management lifecycle event {event.event_id!r} already has other content"
            )
        connection.execute(
            "INSERT INTO management_lifecycle_events "
            "(event_id, management_authority_id, profile_id, revision_id, event_type, "
            "reason_code, document_json, checksum, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.management_authority_id,
                event.profile_id,
                event.revision_id,
                event.event_type,
                event.reason_code,
                document_json,
                checksum,
                event.created_at,
            ),
        )
        return event

    def list_management_lifecycle_events(
        self,
        management_authority_id: str,
        profile_id: str,
    ) -> tuple[ManagementLifecycleEvent, ...]:
        rows = self.connection.execute(
            "SELECT * FROM management_lifecycle_events "
            "WHERE management_authority_id=? AND profile_id=? "
            "ORDER BY created_at, event_id",
            (management_authority_id, profile_id),
        ).fetchall()
        events: list[ManagementLifecycleEvent] = []
        for row in rows:
            event = self._management_event_from_row(row)
            if event.revision_id is not None:
                self._require_management_revision(
                    self.connection,
                    event.revision_id,
                    management_authority_id=management_authority_id,
                    profile_id=profile_id,
                )
            events.append(event)
        return tuple(events)

    def record_management_recovery_event(
        self,
        *,
        receipt_id: str,
        failed_revision_id: str,
        restored_authority_id: str,
        backup_checksum: str,
        event: ManagementLifecycleEvent,
    ) -> ManagementLifecycleEvent:
        """Append one receipt-bound terminal recovery event without changing state."""
        if event.event_type != "recovered" or event.reason_code != "config_recovered":
            raise ImmutableRecordConflict(
                "management recovery audit requires a recovered event"
            )
        expected_event_id = management_recovery_event_id(
            receipt_id=receipt_id,
            failed_revision_id=failed_revision_id,
            profile_id=event.profile_id,
            restored_authority_id=restored_authority_id,
            backup_checksum=backup_checksum,
        )
        if event.event_id != expected_event_id:
            raise ImmutableRecordConflict(
                "management recovery event does not match its receipt binding"
            )
        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM management_config_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise ImmutableRecordConflict(
                    f"management receipt {receipt_id!r} does not exist"
                )
            receipt = self._management_receipt_from_row(row)
            self._validate_management_receipt_revision(connection, receipt)
            if (
                receipt.phase != "recovery_required"
                or receipt.revision_id != failed_revision_id
                or receipt.preceding_authority_id != restored_authority_id
                or receipt.backup_checksum != backup_checksum
            ):
                raise ImmutableRecordConflict(
                    "management recovery event receipt binding changed"
                )
            failed = self._require_management_revision(
                connection,
                failed_revision_id,
                management_authority_id=event.management_authority_id,
                profile_id=event.profile_id,
            )
            if event.revision_id is not None:
                linked = self._require_management_revision(
                    connection,
                    event.revision_id,
                    management_authority_id=event.management_authority_id,
                    profile_id=event.profile_id,
                )
                allowed_link = event.revision_id in {
                    failed.revision_id,
                    failed.parent_revision_id,
                } or (
                    linked.action == "recovery"
                    and linked.parent_revision_id == failed.revision_id
                    and linked.resulting_authority_id == restored_authority_id
                )
                if not allowed_link:
                    raise ImmutableRecordConflict(
                        "management recovery event revision is unrelated to its receipt"
                    )
            return self._append_management_event(connection, event)

    def record_management_restore_started_event(
        self,
        *,
        receipt_id: str,
        failed_revision_id: str,
        restored_authority_id: str,
        backup_checksum: str,
        event: ManagementLifecycleEvent,
    ) -> ManagementLifecycleEvent:
        """Durably prove recovery began while failed-resulting bytes were active."""
        if (
            event.event_type != "hold"
            or event.reason_code != "config_restore_started"
            or event.revision_id != failed_revision_id
        ):
            raise ImmutableRecordConflict(
                "management restore-start audit requires the failed revision"
            )
        expected_event_id = management_restore_started_event_id(
            receipt_id=receipt_id,
            failed_revision_id=failed_revision_id,
            profile_id=event.profile_id,
            restored_authority_id=restored_authority_id,
            backup_checksum=backup_checksum,
        )
        if event.event_id != expected_event_id:
            raise ImmutableRecordConflict(
                "management restore-start event does not match its receipt binding"
            )
        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM management_config_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise ImmutableRecordConflict(
                    f"management receipt {receipt_id!r} does not exist"
                )
            receipt = self._management_receipt_from_row(row)
            self._validate_management_receipt_revision(connection, receipt)
            if (
                receipt.phase != "recovery_required"
                or receipt.revision_id != failed_revision_id
                or receipt.preceding_authority_id != restored_authority_id
                or receipt.backup_checksum != backup_checksum
            ):
                raise ImmutableRecordConflict(
                    "management restore-start receipt binding changed"
                )
            self._require_management_revision(
                connection,
                failed_revision_id,
                management_authority_id=event.management_authority_id,
                profile_id=event.profile_id,
            )
            return self._append_management_event(connection, event)

    def transition_management_control(
        self,
        *,
        control: ManagementControl,
        expected_generation: int,
        event: ManagementLifecycleEvent,
    ) -> ManagementControl:
        control_value, _document_json, _checksum_value = (
            self._validated_management_record(ManagementControl, control)
        )
        event_value, _event_json, _event_checksum = (
            self._validated_management_record(ManagementLifecycleEvent, event)
        )
        control = control_value  # type: ignore[assignment]
        event = event_value  # type: ignore[assignment]
        with self.write_txn() as connection:
            current, existed = self._management_control_in_txn(
                connection, control.management_authority_id
            )
            if current.generation != expected_generation:
                raise RevisionConflict(
                    str(expected_generation), str(current.generation)
                )
            if connection.execute(
                "SELECT 1 FROM management_lifecycle_events WHERE event_id=?",
                (event.event_id,),
            ).fetchone() is not None:
                raise ImmutableRecordConflict(
                    "management lifecycle event already drove a transition"
                )
            if event.management_authority_id != control.management_authority_id:
                raise ImmutableRecordConflict(
                    "management control event authority must match control"
                )
            if control.updated_at != event.created_at:
                raise ImmutableRecordConflict(
                    "management control and event timestamps must match"
                )
            if event.revision_id is not None:
                raise ImmutableRecordConflict(
                    "management control event cannot name a profile revision"
                )
            required_event_type = "frozen" if control.frozen else "unfrozen"
            if current.frozen != control.frozen and event.event_type != required_event_type:
                raise InvalidLifecycleTransition(
                    "management control freeze transition requires matching event"
                )
            if current.frozen == control.frozen and event.event_type not in {
                "hold",
                required_event_type,
            }:
                raise InvalidLifecycleTransition(
                    "management control no-op requires a hold or matching event"
                )
            next_control_value, document_json, checksum = (
                self._validated_management_record(
                    ManagementControl,
                    control.model_copy(
                        update={"generation": current.generation + 1}
                    ),
                )
            )
            next_control = next_control_value  # type: ignore[assignment]
            values = (
                int(next_control.frozen),
                next_control.changes_today,
                next_control.generation,
                next_control.updated_at,
                document_json,
                checksum,
            )
            if existed:
                updated = connection.execute(
                    "UPDATE management_controls SET frozen=?, changes_today=?, "
                    "generation=?, updated_at=?, document_json=?, checksum=? "
                    "WHERE management_authority_id=? AND generation=?",
                    (
                        *values,
                        next_control.management_authority_id,
                        current.generation,
                    ),
                )
                if updated.rowcount != 1:
                    raise RevisionConflict(
                        str(expected_generation), str(current.generation)
                    )
            else:
                connection.execute(
                    "INSERT INTO management_controls "
                    "(management_authority_id, frozen, changes_today, generation, "
                    "updated_at, document_json, checksum) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (next_control.management_authority_id, *values),
                )
            self._append_management_event(connection, event)
            return next_control

    @classmethod
    def _management_profile_state_from_row(
        cls,
        row: sqlite3.Row,
    ) -> ManagementProfileState:
        identifier = f"{row['management_authority_id']}:{row['profile_id']}"
        document_json = str(row["document_json"])
        try:
            _verify_checksum(identifier, document_json, str(row["checksum"]))
            document = _assert_canonical(identifier, document_json)
            state = ManagementProfileState.model_validate(document)
            _assert_content_free(state, writer="management")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        cls._management_row_matches(
            row,
            {
                "management_authority_id": state.management_authority_id,
                "profile_id": state.profile_id,
                "authority_id": state.authority_id,
                "active_revision_id": state.active_revision_id,
                "management_epoch": state.management_epoch,
                "control_revision_id": state.control_revision_id,
                "challenger_revision_id": state.challenger_revision_id,
                "experiment_phase": state.experiment_phase,
                "cooldown_until": state.cooldown_until,
                "rejection_count": state.rejection_count,
                "generation": state.generation,
                "updated_at": state.updated_at,
            },
            identifier=identifier,
        )
        return state

    @classmethod
    def _empty_management_profile_state(
        cls,
        management_authority_id: str,
        profile_id: str,
        authority_id: str,
    ) -> ManagementProfileState:
        value, _document_json, _checksum_value = cls._validated_management_record(
            ManagementProfileState,
            {
                "management_authority_id": management_authority_id,
                "profile_id": profile_id,
                "authority_id": authority_id,
                "active_revision_id": None,
                "management_epoch": 0,
                "control_revision_id": None,
                "challenger_revision_id": None,
                "experiment_phase": "eligible",
                "cooldown_until": None,
                "rejection_count": 0,
                "generation": 0,
                "updated_at": _EMPTY_PROFILE_STATE_TIMESTAMP,
            },
        )
        return value  # type: ignore[return-value]

    def read_management_profile_state(
        self,
        authority_id: str,
        profile_id: str,
        *,
        current_authority_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> ManagementProfileState:
        source = self.connection if connection is None else connection
        row = source.execute(
            "SELECT * FROM management_profile_states "
            "WHERE management_authority_id=? AND profile_id=?",
            (authority_id, profile_id),
        ).fetchone()
        if row is None:
            return self._empty_management_profile_state(
                authority_id,
                profile_id,
                current_authority_id or authority_id,
            )
        state = self._management_profile_state_from_row(row)
        self._validate_management_profile_state_links(source, state)
        return state

    @classmethod
    def _validate_management_profile_state_links(
        cls,
        connection: sqlite3.Connection,
        state: ManagementProfileState,
    ) -> None:
        revisions: dict[str, ManagementRevision] = {}
        for revision_id in {
            state.active_revision_id,
            state.control_revision_id,
            state.challenger_revision_id,
        } - {None}:
            revisions[revision_id] = cls._require_management_revision(
                connection,
                revision_id,
                management_authority_id=state.management_authority_id,
                profile_id=state.profile_id,
            )
        if state.active_revision_id is not None:
            active = revisions[state.active_revision_id]
            if active.resulting_authority_id != state.authority_id:
                raise RevisionChecksumError(state.active_revision_id)
            if active.management_epoch > state.management_epoch:
                raise RevisionChecksumError(state.active_revision_id)
            if (
                state.control_revision_id is None
                and active.management_epoch != state.management_epoch
            ):
                raise RevisionChecksumError(state.active_revision_id)
        elif state.management_epoch != 0:
            raise RevisionChecksumError(
                f"{state.management_authority_id}:{state.profile_id}"
            )
        if state.control_revision_id is not None:
            control = revisions[state.control_revision_id]
            challenger = revisions[state.challenger_revision_id]  # type: ignore[index]
            if (
                challenger.parent_revision_id != control.revision_id
                or challenger.management_epoch != state.management_epoch
            ):
                raise RevisionChecksumError(challenger.revision_id)

    @classmethod
    def _write_management_profile_state(
        cls,
        connection: sqlite3.Connection,
        state: ManagementProfileState,
        *,
        existed: bool,
        expected_generation: int,
    ) -> None:
        validated, document_json, checksum = cls._validated_management_record(
            ManagementProfileState, state
        )
        state = validated  # type: ignore[assignment]
        values = (
            state.authority_id,
            state.active_revision_id,
            state.management_epoch,
            state.control_revision_id,
            state.challenger_revision_id,
            state.experiment_phase,
            state.cooldown_until,
            state.rejection_count,
            state.generation,
            state.updated_at,
            document_json,
            checksum,
        )
        if existed:
            updated = connection.execute(
                "UPDATE management_profile_states SET authority_id=?, "
                "active_revision_id=?, management_epoch=?, control_revision_id=?, "
                "challenger_revision_id=?, experiment_phase=?, cooldown_until=?, "
                "rejection_count=?, generation=?, updated_at=?, document_json=?, "
                "checksum=? WHERE management_authority_id=? AND profile_id=? "
                "AND generation=?",
                (
                    *values,
                    state.management_authority_id,
                    state.profile_id,
                    expected_generation,
                ),
            )
            if updated.rowcount != 1:
                raise RevisionConflict(
                    str(expected_generation), str(state.generation)
                )
        else:
            connection.execute(
                "INSERT INTO management_profile_states "
                "(management_authority_id, profile_id, authority_id, "
                "active_revision_id, management_epoch, control_revision_id, "
                "challenger_revision_id, experiment_phase, cooldown_until, "
                "rejection_count, generation, updated_at, document_json, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (state.management_authority_id, state.profile_id, *values),
            )

    def transition_management_profile_state(
        self,
        *,
        profile_id: str,
        authority_id: str,
        expected_generation: int,
        state: ManagementProfileState,
        event: ManagementLifecycleEvent,
    ) -> ManagementProfileState:
        state_value, _state_json, _state_checksum = (
            self._validated_management_record(ManagementProfileState, state)
        )
        event_value, _event_json, _event_checksum = (
            self._validated_management_record(ManagementLifecycleEvent, event)
        )
        state = state_value  # type: ignore[assignment]
        event = event_value  # type: ignore[assignment]
        if (
            state.management_authority_id != authority_id
            or state.profile_id != profile_id
            or event.management_authority_id != authority_id
            or event.profile_id != profile_id
        ):
            raise ImmutableRecordConflict(
                "management state and event authority/profile must match transition"
            )
        with self.write_txn() as connection:
            self._ensure_management_control(connection, authority_id)
            row = connection.execute(
                "SELECT * FROM management_profile_states "
                "WHERE management_authority_id=? AND profile_id=?",
                (authority_id, profile_id),
            ).fetchone()
            existed = row is not None
            current = (
                self._management_profile_state_from_row(row)
                if row is not None
                else self._empty_management_profile_state(
                    authority_id,
                    profile_id,
                    state.authority_id,
                )
            )
            if row is not None:
                self._validate_management_profile_state_links(connection, current)
            if current.generation != expected_generation:
                raise RevisionConflict(
                    str(expected_generation), str(current.generation)
                )
            if connection.execute(
                "SELECT 1 FROM management_lifecycle_events WHERE event_id=?",
                (event.event_id,),
            ).fetchone() is not None:
                raise ImmutableRecordConflict(
                    "management lifecycle event already drove a transition"
                )
            legal = {
                "eligible": frozenset({"eligible", "validated", "recovery_required"}),
                "validated": frozenset({"canary", "cooldown", "recovery_required"}),
                "canary": frozenset({"cooldown", "recovery_required"}),
                "cooldown": frozenset({"eligible", "rolled_back", "recovery_required"}),
                "rolled_back": frozenset({"eligible", "recovery_required"}),
                "recovery_required": frozenset(
                    {"eligible", "rolled_back", "recovery_required"}
                ),
            }
            if state.experiment_phase not in legal[current.experiment_phase]:
                raise InvalidLifecycleTransition(
                    f"cannot transition management profile from "
                    f"{current.experiment_phase!r} to {state.experiment_phase!r}"
                )
            next_state_value, _next_json, _next_checksum = (
                self._validated_management_record(
                    ManagementProfileState,
                    state.model_copy(
                        update={"generation": current.generation + 1}
                    ),
                )
            )
            next_state = next_state_value  # type: ignore[assignment]
            self._validate_management_profile_state_links(connection, next_state)
            if next_state.management_epoch < current.management_epoch:
                raise RevisionChecksumError(
                    f"{authority_id}:{profile_id}"
                )
            if next_state.updated_at != event.created_at:
                raise ImmutableRecordConflict(
                    "management state and event timestamps must match"
                )
            if event.revision_id is not None:
                self._require_management_revision(
                    connection,
                    event.revision_id,
                    management_authority_id=authority_id,
                    profile_id=profile_id,
                )
            expected_types = {
                "eligible": frozenset({"proposed", "recovered", "cooldown"}),
                "validated": frozenset({"validated"}),
                "canary": frozenset({"canary"}),
                "cooldown": frozenset({"promoted", "rejected", "cooldown"}),
                "rolled_back": frozenset({"rolled_back"}),
                "recovery_required": frozenset({"hold"}),
            }
            if event.event_type not in expected_types[next_state.experiment_phase]:
                raise InvalidLifecycleTransition(
                    "management lifecycle event does not match target phase"
                )
            if event.event_type in {
                "validated",
                "canary",
                "promoted",
                "rejected",
            }:
                expected_event_revision = next_state.challenger_revision_id
            elif event.event_type in {
                "proposed",
                "rolled_back",
                "cooldown",
                "recovered",
            }:
                expected_event_revision = next_state.active_revision_id
            else:
                expected_event_revision = None
            if event.revision_id != expected_event_revision:
                raise ImmutableRecordConflict(
                    "management lifecycle event revision does not match target state"
                )
            if event.event_type in {"promoted", "rejected", "rolled_back"}:
                self._terminalize_management_assignments(connection, event)
            self._append_management_event(connection, event)
            self._write_management_profile_state(
                connection,
                next_state,
                existed=existed,
                expected_generation=current.generation,
            )
            return next_state

    def cancel_stale_management_experiment(
        self,
        *,
        profile_id: str,
        authority_id: str,
        expected_generation: int,
        state: ManagementProfileState,
        event: ManagementLifecycleEvent,
    ) -> ManagementProfileState:
        """Atomically retire a canary superseded by direct config authority."""
        state_value, _state_json, _state_checksum = (
            self._validated_management_record(ManagementProfileState, state)
        )
        event_value, _event_json, _event_checksum = (
            self._validated_management_record(ManagementLifecycleEvent, event)
        )
        state = state_value  # type: ignore[assignment]
        event = event_value  # type: ignore[assignment]
        if (
            state.management_authority_id != authority_id
            or state.profile_id != profile_id
            or event.management_authority_id != authority_id
            or event.profile_id != profile_id
            or state.experiment_phase != "eligible"
            or state.active_revision_id is None
            or state.control_revision_id is not None
            or state.challenger_revision_id is not None
            or state.cooldown_until is not None
            or event.event_type != "recovered"
            or event.reason_code
            not in {"manual_authority_changed", "management_authority_changed"}
            or event.revision_id != state.active_revision_id
            or state.updated_at != event.created_at
        ):
            raise InvalidLifecycleTransition(
                "stale management cancellation must produce an eligible clean state"
            )
        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM management_profile_states "
                "WHERE management_authority_id=? AND profile_id=?",
                (authority_id, profile_id),
            ).fetchone()
            if row is None:
                raise InvalidLifecycleTransition(
                    "stale management experiment state is unavailable"
                )
            current = self._management_profile_state_from_row(row)
            self._validate_management_profile_state_links(connection, current)
            if current.generation != expected_generation:
                raise RevisionConflict(
                    str(expected_generation), str(current.generation)
                )
            if (
                current.experiment_phase not in {"validated", "canary"}
                or current.control_revision_id is None
                or current.challenger_revision_id is None
                or state.rejection_count != current.rejection_count
            ):
                raise InvalidLifecycleTransition(
                    "management experiment is not stale or cancellable"
                )
            assignments = connection.execute(
                "SELECT * FROM management_canary_assignments "
                "WHERE management_authority_id=? AND profile_id=? "
                "AND phase IN ('reserved', 'finalized')",
                (authority_id, profile_id),
            ).fetchall()
            for assignment_row in assignments:
                assignment = self._management_assignment_from_row(assignment_row)
                self._validate_management_assignment_links(connection, assignment)
                if (
                    assignment.control_revision_id != current.control_revision_id
                    or assignment.challenger_revision_id
                    != current.challenger_revision_id
                ):
                    raise InvalidLifecycleTransition(
                        "open management assignment belongs to another experiment"
                    )
                if assignment.phase == "reserved":
                    if any(
                        self._decision_from_row(decision_row).management_assignment_id
                        == assignment.assignment_id
                        for decision_row in connection.execute(
                            "SELECT * FROM routing_decisions"
                        ).fetchall()
                    ):
                        raise InvalidLifecycleTransition(
                            "reserved management assignment already has a decision"
                        )
                    connection.execute(
                        "DELETE FROM management_canary_assignments "
                        "WHERE assignment_id=? AND phase='reserved'",
                        (assignment.assignment_id,),
                    )
                    continue
                terminal_value, document_json, checksum = (
                    self._validated_management_record(
                        ManagementCanaryAssignment,
                        assignment.model_copy(update={"phase": "terminal"}),
                    )
                )
                terminal = terminal_value  # type: ignore[assignment]
                updated = connection.execute(
                    "UPDATE management_canary_assignments SET phase='terminal', "
                    "document_json=?, checksum=? WHERE assignment_id=? "
                    "AND phase='finalized'",
                    (document_json, checksum, terminal.assignment_id),
                )
                if updated.rowcount != 1:
                    raise ImmutableRecordConflict(
                        "management assignment changed during stale cancellation"
                    )
            next_state_value, _next_json, _next_checksum = (
                self._validated_management_record(
                    ManagementProfileState,
                    state.model_copy(
                        update={"generation": current.generation + 1}
                    ),
                )
            )
            next_state = next_state_value  # type: ignore[assignment]
            self._validate_management_profile_state_links(connection, next_state)
            if connection.execute(
                "SELECT 1 FROM management_lifecycle_events WHERE event_id=?",
                (event.event_id,),
            ).fetchone() is not None:
                raise ImmutableRecordConflict(
                    "management lifecycle event already drove a transition"
                )
            self._append_management_event(connection, event)
            self._write_management_profile_state(
                connection,
                next_state,
                existed=True,
                expected_generation=current.generation,
            )
            return next_state

    def management_daily_admissions(
        self,
        profile_id: str,
        utc_day: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        try:
            normalized_day = date.fromisoformat(utc_day).isoformat()
        except (TypeError, ValueError) as error:
            raise ValueError("utc_day must be a canonical ISO date") from error
        if normalized_day != utc_day:
            raise ValueError("utc_day must be a canonical ISO date")
        source = self.connection if connection is None else connection
        rows = source.execute(
            "SELECT * FROM management_revisions "
            "WHERE admitted_utc_day=? "
            "ORDER BY created_at, revision_id",
            (utc_day,),
        ).fetchall()
        count = 0
        for row in rows:
            revision = self._management_revision_from_row(row)
            self._validate_management_revision_parent_chain(source, revision)
            admitted_profiles = json.loads(str(row["admitted_profiles_json"]))
            if profile_id in admitted_profiles:
                count += 1
        return count

    def try_admit_management_revision(
        self,
        *,
        profile_id: str,
        utc_day: str,
        daily_limit: int,
        revision: ManagementRevision,
    ) -> bool:
        if (
            not isinstance(daily_limit, int)
            or isinstance(daily_limit, bool)
            or not 1 <= daily_limit <= 10
        ):
            raise ValueError("daily_limit must be an integer from 1 through 10")
        try:
            if date.fromisoformat(utc_day).isoformat() != utc_day:
                raise ValueError("noncanonical date")
        except (TypeError, ValueError) as error:
            raise ValueError("utc_day must be a canonical ISO date") from error
        validated, _document_json, _checksum_value = (
            self._validated_management_record(ManagementRevision, revision)
        )
        revision = validated  # type: ignore[assignment]
        material_patches = tuple(
            patch
            for patch in revision.patches
            if patch.before_runtime_ids != patch.after_runtime_ids
            or "fallback_primary_challenger" in patch.reason_codes
        )
        if profile_id not in {patch.profile_id for patch in material_patches}:
            raise ImmutableRecordConflict(
                "management admission profile must be materially changed by revision"
            )
        with self.write_txn() as connection:
            if revision.action in {"promote", "rollback", "recovery"}:
                self._insert_management_revision(connection, revision)
                return True
            admitted_profiles = tuple(
                sorted(patch.profile_id for patch in material_patches)
            )
            existing = connection.execute(
                "SELECT * FROM management_revisions WHERE revision_id=?",
                (revision.revision_id,),
            ).fetchone()
            if existing is not None:
                stored = self._management_revision_from_row(existing)
                self._validate_management_revision_parent_chain(connection, stored)
                if stored != revision:
                    raise ImmutableRecordConflict(
                        f"management revision {revision.revision_id!r} already has other content"
                    )
                admitted_profiles_json = existing["admitted_profiles_json"]
                admitted_day = existing["admitted_utc_day"]
                if admitted_profiles_json is not None or admitted_day is not None:
                    if (
                        tuple(json.loads(str(admitted_profiles_json)))
                        == admitted_profiles
                        and str(admitted_day) == utc_day
                    ):
                        return True
                    raise ImmutableRecordConflict(
                        "management revision already has a different admission"
                    )
            capped_profiles = tuple(
                item
                for item in admitted_profiles
                if self.management_daily_admissions(
                    item, utc_day, connection=connection
                )
                >= daily_limit
            )
            if capped_profiles:
                self._ensure_management_control(
                    connection, revision.management_authority_id
                )
                for capped_profile_id in capped_profiles:
                    event_id = hashlib.sha256(
                        _canonical_json({
                            "management_authority_id": revision.management_authority_id,
                            "profile_id": capped_profile_id,
                            "reason_code": "daily_cap_reached",
                            "revision_id": revision.revision_id,
                            "utc_day": utc_day,
                        }).encode("utf-8")
                    ).hexdigest()
                    self._append_management_event(
                        connection,
                        ManagementLifecycleEvent(
                            event_id=event_id,
                            management_authority_id=revision.management_authority_id,
                            profile_id=capped_profile_id,
                            revision_id=None,
                            event_type="hold",
                            reason_code="daily_cap_reached",
                            created_at=revision.created_at,
                        ),
                    )
                return False
            admission_checksum = _checksum(_canonical_json({
                "profile_ids": admitted_profiles,
                "revision_id": revision.revision_id,
                "utc_day": utc_day,
            }))
            admitted_profiles_json = _canonical_json(admitted_profiles)
            if existing is not None:
                updated = connection.execute(
                    "UPDATE management_revisions SET admitted_profiles_json=?, "
                    "admitted_utc_day=?, admission_checksum=? "
                    "WHERE revision_id=? AND admitted_profiles_json IS NULL "
                    "AND admitted_utc_day IS NULL AND admission_checksum IS NULL",
                    (
                        admitted_profiles_json,
                        utc_day,
                        admission_checksum,
                        revision.revision_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise ImmutableRecordConflict(
                        "management admission changed during reservation"
                    )
                return True
            self._insert_management_revision(
                connection,
                revision,
                admitted_profile_ids=admitted_profiles,
                admitted_utc_day=utc_day,
            )
            return True

    @classmethod
    def _management_assignment_from_row(
        cls,
        row: sqlite3.Row,
    ) -> ManagementCanaryAssignment:
        identifier = str(row["assignment_id"])
        document_json = str(row["document_json"])
        try:
            _verify_checksum(identifier, document_json, str(row["checksum"]))
            document = _assert_canonical(identifier, document_json)
            assignment = ManagementCanaryAssignment.model_validate(document)
            _assert_content_free(assignment, writer="management")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        cls._management_row_matches(
            row,
            {
                "assignment_id": assignment.assignment_id,
                "management_authority_id": assignment.management_authority_id,
                "profile_id": assignment.profile_id,
                "operation_identity_hash": assignment.operation_identity_hash,
                "control_revision_id": assignment.control_revision_id,
                "challenger_revision_id": assignment.challenger_revision_id,
                "arm": assignment.arm,
                "phase": assignment.phase,
                "runtime_id": assignment.runtime_id,
                "reasoning_effort": assignment.reasoning_effort,
                "created_at": assignment.created_at,
            },
            identifier=identifier,
        )
        return assignment

    @classmethod
    def _validate_management_assignment_links(
        cls,
        connection: sqlite3.Connection,
        assignment: ManagementCanaryAssignment,
    ) -> tuple[ManagementRevision, ManagementRevision]:
        control = cls._require_management_revision(
            connection,
            assignment.control_revision_id,
            management_authority_id=assignment.management_authority_id,
            profile_id=assignment.profile_id,
        )
        challenger = cls._require_management_revision(
            connection,
            assignment.challenger_revision_id,
            management_authority_id=assignment.management_authority_id,
            profile_id=assignment.profile_id,
        )
        if (
            challenger.parent_revision_id != control.revision_id
            or challenger.preceding_authority_id != control.resulting_authority_id
        ):
            raise RevisionChecksumError(assignment.assignment_id)
        if assignment.runtime_id is not None:
            selected = control if assignment.arm == "control" else challenger
            patch = next(
                item
                for item in selected.patches
                if item.profile_id == assignment.profile_id
            )
            if assignment.runtime_id not in patch.after_runtime_ids:
                raise RevisionChecksumError(assignment.assignment_id)
        return control, challenger

    def reserve_management_assignment(
        self,
        assignment: ManagementCanaryAssignment,
        *,
        expected_generation: int,
    ) -> ManagementCanaryAssignment:
        validated, document_json, checksum = self._validated_management_record(
            ManagementCanaryAssignment, assignment
        )
        assignment = validated  # type: ignore[assignment]
        if assignment.phase != "reserved":
            raise ValueError("management assignment reservation must begin reserved")
        with self.write_txn() as connection:
            state = self.read_management_profile_state(
                assignment.management_authority_id,
                assignment.profile_id,
                connection=connection,
            )
            if state.generation != expected_generation:
                raise RevisionConflict(
                    str(expected_generation), str(state.generation)
                )
            if (
                state.experiment_phase != "canary"
                or state.control_revision_id != assignment.control_revision_id
                or state.challenger_revision_id
                != assignment.challenger_revision_id
                or state.active_revision_id != assignment.control_revision_id
            ):
                raise InvalidLifecycleTransition(
                    "management assignment must match the active canary state"
                )
            self._validate_management_assignment_links(connection, assignment)
            existing = connection.execute(
                "SELECT * FROM management_canary_assignments "
                "WHERE management_authority_id=? AND profile_id=? "
                "AND operation_identity_hash=?",
                (
                    assignment.management_authority_id,
                    assignment.profile_id,
                    assignment.operation_identity_hash,
                ),
            ).fetchone()
            if existing is not None:
                stored = self._management_assignment_from_row(existing)
                self._validate_management_assignment_links(connection, stored)
                ignored = {"phase", "runtime_id", "reasoning_effort"}
                if stored.model_dump(exclude=ignored) == assignment.model_dump(
                    exclude=ignored
                ):
                    return stored
                raise ImmutableRecordConflict(
                    "management operation already has a different assignment"
                )
            collision = connection.execute(
                "SELECT * FROM management_canary_assignments WHERE assignment_id=?",
                (assignment.assignment_id,),
            ).fetchone()
            if collision is not None:
                raise ImmutableRecordConflict(
                    f"management assignment {assignment.assignment_id!r} already exists"
                )
            connection.execute(
                "INSERT INTO management_canary_assignments "
                "(assignment_id, management_authority_id, profile_id, "
                "operation_identity_hash, control_revision_id, challenger_revision_id, "
                "arm, phase, runtime_id, reasoning_effort, document_json, checksum, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    assignment.assignment_id,
                    assignment.management_authority_id,
                    assignment.profile_id,
                    assignment.operation_identity_hash,
                    assignment.control_revision_id,
                    assignment.challenger_revision_id,
                    assignment.arm,
                    assignment.phase,
                    assignment.runtime_id,
                    assignment.reasoning_effort,
                    document_json,
                    checksum,
                    assignment.created_at,
                ),
            )
            return assignment

    def read_management_assignment(
        self,
        assignment_id: str,
    ) -> ManagementCanaryAssignment | None:
        row = self.connection.execute(
            "SELECT * FROM management_canary_assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        if row is None:
            return None
        assignment = self._management_assignment_from_row(row)
        self._validate_management_assignment_links(self.connection, assignment)
        return assignment

    def finalize_management_assignment(
        self,
        *,
        assignment_id: str,
        runtime_id: str,
        reasoning_effort: str,
        expected_generation: int,
    ) -> ManagementCanaryAssignment:
        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM management_canary_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if row is None:
                raise ImmutableRecordConflict(
                    f"management assignment {assignment_id!r} does not exist"
                )
            current = self._management_assignment_from_row(row)
            self._validate_management_assignment_links(connection, current)
            state = self.read_management_profile_state(
                current.management_authority_id,
                current.profile_id,
                connection=connection,
            )
            if state.generation != expected_generation:
                raise RevisionConflict(
                    str(expected_generation), str(state.generation)
                )
            if (
                state.experiment_phase != "canary"
                or state.control_revision_id != current.control_revision_id
                or state.challenger_revision_id != current.challenger_revision_id
            ):
                raise InvalidLifecycleTransition(
                    "management assignment no longer matches the canary state"
                )
            if current.phase == "terminal":
                raise ImmutableRecordConflict(
                    "terminal management assignment cannot be finalized"
                )
            candidate_value, document_json, checksum = (
                self._validated_management_record(
                    ManagementCanaryAssignment,
                    current.model_copy(
                        update={
                            "phase": "finalized",
                            "runtime_id": runtime_id,
                            "reasoning_effort": reasoning_effort,
                        }
                    ),
                )
            )
            candidate = candidate_value  # type: ignore[assignment]
            self._validate_management_assignment_links(connection, candidate)
            if current.phase == "finalized":
                if current == candidate:
                    return current
                raise ImmutableRecordConflict(
                    "management assignment final resolution is immutable"
                )
            updated = connection.execute(
                "UPDATE management_canary_assignments SET phase=?, runtime_id=?, "
                "reasoning_effort=?, document_json=?, checksum=? "
                "WHERE assignment_id=? AND phase='reserved'",
                (
                    candidate.phase,
                    candidate.runtime_id,
                    candidate.reasoning_effort,
                    document_json,
                    checksum,
                    assignment_id,
                ),
            )
            if updated.rowcount != 1:
                raise ImmutableRecordConflict(
                    "management assignment changed during finalization"
                )
            return candidate

    def discard_management_reservation(
        self,
        assignment_id: str,
        *,
        expected_generation: int,
        expected_management_authority_id: str,
        expected_profile_id: str,
        expected_operation_identity_hash: str,
    ) -> bool:
        """Delete one exact, never-dispatched speculative reservation."""
        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM management_canary_assignments "
                "WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if row is None:
                return True
            current = self._management_assignment_from_row(row)
            self._validate_management_assignment_links(connection, current)
            if (
                current.phase != "reserved"
                or current.management_authority_id
                != expected_management_authority_id
                or current.profile_id != expected_profile_id
                or current.operation_identity_hash
                != expected_operation_identity_hash
            ):
                return False
            state = self.read_management_profile_state(
                current.management_authority_id,
                current.profile_id,
                connection=connection,
            )
            if (
                state.generation != expected_generation
                or state.experiment_phase != "canary"
                or state.control_revision_id != current.control_revision_id
                or state.challenger_revision_id
                != current.challenger_revision_id
            ):
                return False
            if any(
                self._decision_from_row(decision_row).management_assignment_id
                == assignment_id
                for decision_row in connection.execute(
                    "SELECT * FROM routing_decisions"
                ).fetchall()
            ):
                return False
            deleted = connection.execute(
                "DELETE FROM management_canary_assignments "
                "WHERE assignment_id=? AND phase='reserved' "
                "AND management_authority_id=? AND profile_id=? "
                "AND operation_identity_hash=?",
                (
                    assignment_id,
                    expected_management_authority_id,
                    expected_profile_id,
                    expected_operation_identity_hash,
                ),
            )
            return deleted.rowcount == 1

    def list_open_management_assignments(
        self,
        management_authority_id: str,
        profile_id: str,
    ) -> tuple[ManagementCanaryAssignment, ...]:
        rows = self.connection.execute(
            "SELECT * FROM management_canary_assignments "
            "WHERE management_authority_id=? AND profile_id=? "
            "AND phase IN ('reserved', 'finalized') ORDER BY created_at, assignment_id",
            (management_authority_id, profile_id),
        ).fetchall()
        assignments: list[ManagementCanaryAssignment] = []
        for row in rows:
            item = self._management_assignment_from_row(row)
            self._validate_management_assignment_links(self.connection, item)
            assignments.append(item)
        return tuple(assignments)

    @classmethod
    def _terminalize_management_assignments(
        cls,
        connection: sqlite3.Connection,
        event: ManagementLifecycleEvent,
    ) -> None:
        if event.revision_id is None:
            raise InvalidLifecycleTransition(
                "terminal management event requires a revision"
            )
        rows = connection.execute(
            "SELECT * FROM management_canary_assignments "
            "WHERE management_authority_id=? AND profile_id=? "
            "AND challenger_revision_id=? AND phase IN ('reserved', 'finalized')",
            (
                event.management_authority_id,
                event.profile_id,
                event.revision_id,
            ),
        ).fetchall()
        for row in rows:
            assignment = cls._management_assignment_from_row(row)
            cls._validate_management_assignment_links(connection, assignment)
            if assignment.phase == "reserved":
                # A process may die after persisting the exact canary
                # reservation but before final resolution is attached to a
                # routing decision.  That speculative row must not poison the
                # later receipt-bound promotion/rollback settlement forever.
                # Delete only the fully bound, still-unreferenced reservation
                # selected by this lifecycle event; finalized (and therefore
                # potentially dispatched) assignments remain immutable and
                # are terminalized below.
                # Management attestation is stored in the immutable decision
                # document rather than a denormalized SQL column.
                referenced = any(
                    cls._decision_from_row(decision_row).management_assignment_id
                    == assignment.assignment_id
                    for decision_row in connection.execute(
                        "SELECT * FROM routing_decisions"
                    ).fetchall()
                )
                if referenced:
                    raise InvalidLifecycleTransition(
                        "reserved management assignment already has a decision"
                    )
                deleted = connection.execute(
                    "DELETE FROM management_canary_assignments "
                    "WHERE assignment_id=? AND management_authority_id=? "
                    "AND profile_id=? AND operation_identity_hash=? "
                    "AND control_revision_id=? AND challenger_revision_id=? "
                    "AND phase='reserved'",
                    (
                        assignment.assignment_id,
                        assignment.management_authority_id,
                        assignment.profile_id,
                        assignment.operation_identity_hash,
                        assignment.control_revision_id,
                        assignment.challenger_revision_id,
                    ),
                )
                if deleted.rowcount != 1:
                    raise ImmutableRecordConflict(
                        "management reservation changed during terminal transition"
                    )
                continue
            terminal_value, document_json, checksum = cls._validated_management_record(
                ManagementCanaryAssignment,
                assignment.model_copy(update={"phase": "terminal"}),
            )
            terminal = terminal_value  # type: ignore[assignment]
            updated = connection.execute(
                "UPDATE management_canary_assignments SET phase='terminal', "
                "document_json=?, checksum=? WHERE assignment_id=? "
                "AND phase='finalized'",
                (document_json, checksum, terminal.assignment_id),
            )
            if updated.rowcount != 1:
                raise ImmutableRecordConflict(
                    "management assignment changed during terminal transition"
                )

    @classmethod
    def _management_lease_from_row(cls, row: sqlite3.Row) -> OptimizerLease:
        identifier = f"{row['management_authority_id']}:{row['profile_id']}"
        document_json = str(row["document_json"])
        try:
            _verify_checksum(identifier, document_json, str(row["checksum"]))
            document = _assert_canonical(identifier, document_json)
            lease = OptimizerLease.model_validate(document)
            _assert_content_free(lease, writer="management")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        cls._management_row_matches(
            row,
            {
                "management_authority_id": lease.authority_id,
                "profile_id": lease.profile_id,
                "owner_id": lease.owner_id,
                "lease_expires_at": lease.lease_expires_at,
                "generation": lease.generation,
                "updated_at": lease.updated_at,
            },
            identifier=identifier,
        )
        return lease

    @staticmethod
    def _management_timestamp(value: datetime | date | float | int | str) -> str:
        if isinstance(value, str):
            validated = _require_canonical_timestamp(value, field_name="now")
            assert validated is not None
            return validated
        return _timestamp(value)

    def acquire_management_lease(
        self,
        authority_id: str,
        profile_id: str,
        owner_id: str,
        now: datetime | date | float | int | str,
        lease_seconds: float,
    ) -> OptimizerLease | None:
        if (
            not isinstance(lease_seconds, (int, float))
            or isinstance(lease_seconds, bool)
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be finite and positive")
        now_text = self._management_timestamp(now)
        now_moment = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
        expires_at = _timestamp(now_moment + timedelta(seconds=float(lease_seconds)))
        with self.write_txn() as connection:
            control = self._ensure_management_control(connection, authority_id)
            if control.frozen:
                return None
            row = connection.execute(
                "SELECT * FROM management_leases "
                "WHERE management_authority_id=? AND profile_id=?",
                (authority_id, profile_id),
            ).fetchone()
            generation = 1
            if row is not None:
                current = self._management_lease_from_row(row)
                expiry = datetime.fromisoformat(
                    current.lease_expires_at.replace("Z", "+00:00")
                )
                if current.owner_id != owner_id and expiry > now_moment:
                    return None
                generation = current.generation + 1
            value, document_json, checksum = self._validated_management_record(
                OptimizerLease,
                {
                    "authority_id": authority_id,
                    "profile_id": profile_id,
                    "owner_id": owner_id,
                    "lease_expires_at": expires_at,
                    "generation": generation,
                    "updated_at": now_text,
                },
            )
            lease = value  # type: ignore[assignment]
            connection.execute(
                "INSERT INTO management_leases "
                "(management_authority_id, profile_id, owner_id, lease_expires_at, "
                "generation, updated_at, document_json, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(management_authority_id, profile_id) DO UPDATE SET "
                "owner_id=excluded.owner_id, lease_expires_at=excluded.lease_expires_at, "
                "generation=excluded.generation, updated_at=excluded.updated_at, "
                "document_json=excluded.document_json, checksum=excluded.checksum",
                (
                    lease.authority_id,
                    lease.profile_id,
                    lease.owner_id,
                    lease.lease_expires_at,
                    lease.generation,
                    lease.updated_at,
                    document_json,
                    checksum,
                ),
            )
            return lease

    def release_management_lease(self, lease: OptimizerLease) -> bool:
        validated, _document_json, _checksum_value = self._validated_management_record(
            OptimizerLease, lease
        )
        lease = validated  # type: ignore[assignment]
        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM management_leases "
                "WHERE management_authority_id=? AND profile_id=?",
                (lease.authority_id, lease.profile_id),
            ).fetchone()
            if row is None:
                return False
            stored = self._management_lease_from_row(row)
            if (
                stored.owner_id != lease.owner_id
                or stored.generation != lease.generation
            ):
                return False
            deleted = connection.execute(
                "DELETE FROM management_leases WHERE management_authority_id=? "
                "AND profile_id=? AND owner_id=? AND generation=?",
                (
                    lease.authority_id,
                    lease.profile_id,
                    lease.owner_id,
                    lease.generation,
                ),
            )
            return deleted.rowcount == 1

    @classmethod
    def _management_receipt_from_row(
        cls,
        row: sqlite3.Row,
    ) -> ManagementConfigReceipt:
        identifier = str(row["receipt_id"])
        document_json = str(row["document_json"])
        try:
            _verify_checksum(identifier, document_json, str(row["checksum"]))
            document = _assert_canonical(identifier, document_json)
            receipt = ManagementConfigReceipt.model_validate(document)
            _assert_content_free(receipt, writer="management")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        cls._management_row_matches(
            row,
            {
                "receipt_id": receipt.receipt_id,
                "revision_id": receipt.revision_id,
                "phase": receipt.phase,
                "preceding_authority_id": receipt.preceding_authority_id,
                "resulting_authority_id": receipt.resulting_authority_id,
                "backup_checksum": receipt.backup_checksum,
                "created_at": receipt.created_at,
                "updated_at": receipt.updated_at,
            },
            identifier=identifier,
        )
        return receipt

    @classmethod
    def _validate_management_receipt_revision(
        cls,
        connection: sqlite3.Connection,
        receipt: ManagementConfigReceipt,
    ) -> ManagementRevision:
        revision = cls._require_management_revision(
            connection, receipt.revision_id
        )
        if (
            revision.preceding_authority_id != receipt.preceding_authority_id
            or revision.resulting_authority_id != receipt.resulting_authority_id
        ):
            raise RevisionChecksumError(receipt.receipt_id)
        return revision

    def record_management_receipt(
        self,
        receipt: ManagementConfigReceipt,
    ) -> ManagementConfigReceipt:
        validated, document_json, checksum = self._validated_management_record(
            ManagementConfigReceipt, receipt
        )
        receipt = validated  # type: ignore[assignment]
        if receipt.phase != "prepared":
            raise ValueError("new management receipt must begin in prepared phase")
        with self.write_txn() as connection:
            self._validate_management_receipt_revision(connection, receipt)
            row = connection.execute(
                "SELECT * FROM management_config_receipts WHERE receipt_id=?",
                (receipt.receipt_id,),
            ).fetchone()
            if row is not None:
                stored = self._management_receipt_from_row(row)
                self._validate_management_receipt_revision(connection, stored)
                if stored == receipt:
                    return stored
                raise ImmutableRecordConflict(
                    f"management receipt {receipt.receipt_id!r} already has other content"
                )
            connection.execute(
                "INSERT INTO management_config_receipts "
                "(receipt_id, revision_id, phase, preceding_authority_id, "
                "resulting_authority_id, backup_checksum, created_at, updated_at, "
                "document_json, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.revision_id,
                    receipt.phase,
                    receipt.preceding_authority_id,
                    receipt.resulting_authority_id,
                    receipt.backup_checksum,
                    receipt.created_at,
                    receipt.updated_at,
                    document_json,
                    checksum,
                ),
            )
            return receipt

    def read_management_receipt(
        self,
        receipt_id: str,
    ) -> ManagementConfigReceipt | None:
        row = self.connection.execute(
            "SELECT * FROM management_config_receipts WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            return None
        receipt = self._management_receipt_from_row(row)
        self._validate_management_receipt_revision(self.connection, receipt)
        return receipt

    def recover_management_receipt(
        self,
        receipt: ManagementConfigReceipt,
        *,
        expected_phase: str,
    ) -> ManagementConfigReceipt:
        validated, document_json, checksum = self._validated_management_record(
            ManagementConfigReceipt, receipt
        )
        receipt = validated  # type: ignore[assignment]
        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM management_config_receipts WHERE receipt_id=?",
                (receipt.receipt_id,),
            ).fetchone()
            if row is None:
                raise ImmutableRecordConflict(
                    f"management receipt {receipt.receipt_id!r} does not exist"
                )
            current = self._management_receipt_from_row(row)
            self._validate_management_receipt_revision(connection, current)
            if current.phase != expected_phase:
                raise RevisionConflict(expected_phase, current.phase)
            identity_fields = {
                "receipt_id",
                "revision_id",
                "preceding_authority_id",
                "resulting_authority_id",
                "backup_checksum",
                "created_at",
            }
            if current.model_dump(include=identity_fields) != receipt.model_dump(
                include=identity_fields
            ):
                raise ImmutableRecordConflict(
                    "management receipt recovery cannot change immutable identity"
                )
            transitions = {
                "prepared": frozenset({"config_replaced", "recovery_required"}),
                "config_replaced": frozenset({"committed", "recovery_required"}),
                "recovery_required": frozenset(),
                "committed": frozenset(),
            }
            if receipt.phase not in transitions[current.phase]:
                raise InvalidLifecycleTransition(
                    f"cannot transition management receipt from "
                    f"{current.phase!r} to {receipt.phase!r}"
                )
            self._validate_management_receipt_revision(connection, receipt)
            updated = connection.execute(
                "UPDATE management_config_receipts SET phase=?, updated_at=?, "
                "document_json=?, checksum=? WHERE receipt_id=? AND phase=?",
                (
                    receipt.phase,
                    receipt.updated_at,
                    document_json,
                    checksum,
                    receipt.receipt_id,
                    expected_phase,
                ),
            )
            if updated.rowcount != 1:
                raise RevisionConflict(expected_phase, current.phase)
            return receipt

    @classmethod
    def _management_finalization_from_row(
        cls,
        row: sqlite3.Row,
    ) -> ManagementLifecycleFinalization:
        identifier = str(row["finalization_id"])
        document_json = str(row["document_json"])
        try:
            _verify_checksum(identifier, document_json, str(row["checksum"]))
            document = _assert_canonical(identifier, document_json)
            finalization = ManagementLifecycleFinalization.model_validate(document)
            _assert_content_free(finalization, writer="management")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        cls._management_row_matches(
            row,
            {
                "finalization_id": finalization.finalization_id,
                "receipt_id": finalization.receipt_id,
                "revision_id": finalization.revision_id,
                "challenger_revision_id": finalization.challenger_revision_id,
                "management_authority_id": finalization.management_authority_id,
                "profile_id": finalization.profile_id,
                "action": finalization.action,
                "phase": finalization.phase,
                "created_at": finalization.created_at,
                "updated_at": finalization.updated_at,
            },
            identifier=identifier,
        )
        return finalization

    @classmethod
    def _validate_management_finalization_links(
        cls,
        connection: sqlite3.Connection,
        finalization: ManagementLifecycleFinalization,
        *,
        allow_config_replaced: bool = False,
    ) -> None:
        receipt_row = connection.execute(
            "SELECT * FROM management_config_receipts WHERE receipt_id=?",
            (finalization.receipt_id,),
        ).fetchone()
        if receipt_row is None:
            raise RevisionChecksumError(finalization.finalization_id)
        receipt = cls._management_receipt_from_row(receipt_row)
        cls._validate_management_receipt_revision(connection, receipt)
        allowed_receipt_phases = (
            {"config_replaced", "committed"}
            if allow_config_replaced
            else {"committed"}
        )
        revision = cls._require_management_revision(
            connection,
            finalization.revision_id,
        )
        challenger = cls._require_management_revision(
            connection,
            finalization.challenger_revision_id,
        )
        patch_ids = tuple(patch.profile_id for patch in revision.patches)
        if (
            receipt.phase not in allowed_receipt_phases
            or receipt.revision_id != revision.revision_id
            or revision.management_authority_id
            != finalization.management_authority_id
            or challenger.management_authority_id
            != finalization.management_authority_id
            or revision.parent_revision_id != challenger.revision_id
            or revision.action != finalization.action
            or not patch_ids
            or patch_ids[0] != finalization.profile_id
        ):
            raise RevisionChecksumError(finalization.finalization_id)

    def record_management_lifecycle_finalization(
        self,
        finalization: ManagementLifecycleFinalization,
    ) -> ManagementLifecycleFinalization:
        validated, document_json, checksum = self._validated_management_record(
            ManagementLifecycleFinalization,
            finalization,
        )
        finalization = validated  # type: ignore[assignment]
        if finalization.phase != "pending":
            raise ValueError("new management finalization must begin pending")
        with self.write_txn() as connection:
            self._validate_management_finalization_links(
                connection,
                finalization,
                allow_config_replaced=True,
            )
            row = connection.execute(
                "SELECT * FROM management_lifecycle_finalizations "
                "WHERE finalization_id=? OR receipt_id=?",
                (finalization.finalization_id, finalization.receipt_id),
            ).fetchone()
            if row is not None:
                stored = self._management_finalization_from_row(row)
                self._validate_management_finalization_links(
                    connection,
                    stored,
                    allow_config_replaced=True,
                )
                if stored == finalization:
                    return stored
                raise ImmutableRecordConflict(
                    "management lifecycle finalization already has other content"
                )
            connection.execute(
                "INSERT INTO management_lifecycle_finalizations "
                "(finalization_id, receipt_id, revision_id, challenger_revision_id, "
                "management_authority_id, profile_id, action, phase, created_at, "
                "updated_at, document_json, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    finalization.finalization_id,
                    finalization.receipt_id,
                    finalization.revision_id,
                    finalization.challenger_revision_id,
                    finalization.management_authority_id,
                    finalization.profile_id,
                    finalization.action,
                    finalization.phase,
                    finalization.created_at,
                    finalization.updated_at,
                    document_json,
                    checksum,
                ),
            )
            return finalization

    def read_management_lifecycle_finalization(
        self,
        finalization_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ManagementLifecycleFinalization | None:
        target = self.connection if connection is None else connection
        row = target.execute(
            "SELECT * FROM management_lifecycle_finalizations "
            "WHERE finalization_id=?",
            (finalization_id,),
        ).fetchone()
        if row is None:
            return None
        finalization = self._management_finalization_from_row(row)
        self._validate_management_finalization_links(target, finalization)
        return finalization

    def read_management_lifecycle_finalization_for_receipt(
        self,
        receipt_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ManagementLifecycleFinalization | None:
        target = self.connection if connection is None else connection
        row = target.execute(
            "SELECT * FROM management_lifecycle_finalizations WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            return None
        finalization = self._management_finalization_from_row(row)
        self._validate_management_finalization_links(target, finalization)
        return finalization

    def list_pending_management_lifecycle_finalizations(
        self,
    ) -> tuple[ManagementLifecycleFinalization, ...]:
        rows = self.connection.execute(
            "SELECT * FROM management_lifecycle_finalizations "
            "WHERE phase='pending' ORDER BY created_at, finalization_id"
        ).fetchall()
        values: list[ManagementLifecycleFinalization] = []
        for row in rows:
            finalization = self._management_finalization_from_row(row)
            self._validate_management_finalization_links(
                self.connection,
                finalization,
            )
            values.append(finalization)
        return tuple(values)

    def finalize_management_lifecycle_finalization(
        self,
        finalization_id: str,
        *,
        expected_phase: str,
        updated_at: str,
    ) -> ManagementLifecycleFinalization:
        with self.write_txn() as connection:
            current = self.read_management_lifecycle_finalization(
                finalization_id,
                connection=connection,
            )
            if current is None:
                raise ImmutableRecordConflict(
                    "management lifecycle finalization is unavailable"
                )
            if current.phase != expected_phase:
                raise RevisionConflict(expected_phase, current.phase)
            finalized = current.model_copy(
                update={"phase": "finalized", "updated_at": updated_at}
            )
            validated, document_json, checksum = self._validated_management_record(
                ManagementLifecycleFinalization,
                finalized,
            )
            finalized = validated  # type: ignore[assignment]
            updated = connection.execute(
                "UPDATE management_lifecycle_finalizations SET phase='finalized', "
                "updated_at=?, document_json=?, checksum=? "
                "WHERE finalization_id=? AND phase=?",
                (
                    finalized.updated_at,
                    document_json,
                    checksum,
                    finalized.finalization_id,
                    expected_phase,
                ),
            )
            if updated.rowcount != 1:
                raise RevisionConflict(expected_phase, current.phase)
            self._validate_management_finalization_links(connection, finalized)
            return finalized

    def write_authority_revision(
        self,
        authority_id: str,
        document: Mapping[str, Any],
        *,
        created_at: str | None = None,
    ) -> AuthorityRevision:
        """Insert an immutable authority document, idempotently by content."""
        if not authority_id:
            raise ValueError("authority_id must not be empty")
        _assert_content_free(document, writer="authority")
        document_json = _canonical_json(document)
        checksum = _checksum(document_json)
        timestamp = created_at or _timestamp()
        with self.write_txn() as connection:
            existing = connection.execute(
                "SELECT * FROM authority_revisions WHERE authority_id = ?",
                (authority_id,),
            ).fetchone()
            if existing is not None:
                record = self._authority_from_row(existing)
                if record.document_json == document_json:
                    return record
                raise ImmutableRecordConflict(
                    f"authority revision {authority_id!r} already has other content"
                )
            connection.execute(
                "INSERT INTO authority_revisions "
                "(authority_id, document_json, checksum, created_at) "
                "VALUES (?, ?, ?, ?)",
                (authority_id, document_json, checksum, timestamp),
            )
        record = self.read_authority_revision(authority_id)
        if record is None:  # pragma: no cover - committed row invariant
            raise StoreError(
                f"authority revision vanished after commit: {authority_id}"
            )
        return record

    def publish_authority_and_baseline(
        self,
        *,
        authority_id: str,
        document: Mapping[str, Any],
        baseline: AdaptiveRevision,
    ) -> tuple[AuthorityRevision, AdaptiveRevision]:
        """Atomically publish one authority and its exact initial baseline.

        Recovery may replay this operation.  An exact replay is idempotent;
        any identifier collision or different active pointer fails closed.
        """
        if not authority_id:
            raise ValueError("authority_id must not be empty")
        if baseline.authority_id != authority_id or not baseline.is_baseline:
            raise ValueError("baseline must be initial and authority-bound")
        if baseline.parent_revision_id is not None:
            raise ValueError("baseline must not have a parent revision")

        _assert_content_free(document, writer="authority")
        authority_json = _canonical_json(document)
        authority_checksum = _checksum(authority_json)
        validated = AdaptiveRevision.model_validate(
            baseline.model_dump(mode="json", by_alias=True)
        )
        _assert_content_free(validated, writer="adaptive")
        revision_json = _canonical_json(validated)
        revision_checksum = _checksum(revision_json)
        explanation_json = _canonical_json(validated.explanation)

        with self.write_txn() as connection:
            authority_row = connection.execute(
                "SELECT * FROM authority_revisions WHERE authority_id = ?",
                (authority_id,),
            ).fetchone()
            if authority_row is None:
                connection.execute(
                    "INSERT INTO authority_revisions "
                    "(authority_id, document_json, checksum, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        authority_id,
                        authority_json,
                        authority_checksum,
                        validated.created_at,
                    ),
                )
            elif (
                str(authority_row["document_json"]) != authority_json
                or str(authority_row["checksum"]) != authority_checksum
            ):
                raise ImmutableRecordConflict(
                    f"authority revision {authority_id!r} already has other content"
                )

            revision_row = connection.execute(
                "SELECT * FROM adaptive_revisions WHERE revision_id = ?",
                (validated.revision_id,),
            ).fetchone()
            if revision_row is None:
                connection.execute(
                    "INSERT INTO adaptive_revisions "
                    "(revision_id, authority_id, parent_revision_id, "
                    "document_json, checksum, explanation_json, created_at, complete) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        validated.revision_id,
                        authority_id,
                        None,
                        revision_json,
                        revision_checksum,
                        explanation_json,
                        validated.created_at,
                    ),
                )
            elif (
                str(revision_row["authority_id"]) != authority_id
                or str(revision_row["document_json"]) != revision_json
                or str(revision_row["checksum"]) != revision_checksum
                or not bool(revision_row["complete"])
            ):
                raise ImmutableRecordConflict(
                    f"adaptive revision {validated.revision_id!r} already has other content"
                )

            active_id = self._active_id(connection, authority_id)
            if active_id not in {None, validated.revision_id}:
                raise RevisionConflict(validated.revision_id, active_id)
            if active_id is None:
                connection.execute(
                    "INSERT INTO active_adaptive_revisions"
                    "(authority_id, revision_id, updated_at) VALUES (?, ?, ?)",
                    (authority_id, validated.revision_id, validated.created_at),
                )

            stored_authority = connection.execute(
                "SELECT * FROM authority_revisions WHERE authority_id = ?",
                (authority_id,),
            ).fetchone()
            stored_revision = connection.execute(
                "SELECT * FROM adaptive_revisions WHERE revision_id = ?",
                (validated.revision_id,),
            ).fetchone()
            if stored_authority is None or stored_revision is None:
                raise StoreError("authority/baseline publication verification failed")
            authority_record = self._authority_from_row(stored_authority)
            revision_record = self._adaptive_from_row(stored_revision)

        return authority_record, revision_record

    def list_authority_revisions(self) -> list[AuthorityRevision]:
        """Return all immutable authorities in deterministic creation order."""
        rows = self.connection.execute(
            "SELECT * FROM authority_revisions ORDER BY created_at, authority_id"
        ).fetchall()
        return [self._authority_from_row(row) for row in rows]

    def rollback_authority_and_baseline(
        self,
        *,
        authority_id: str,
        baseline_revision_id: str,
        remove_authority: bool,
        remove_baseline: bool,
        remove_active_pointer: bool,
    ) -> None:
        """Rollback only rows proven to have been created by one failed saga."""
        with self.write_txn() as connection:
            if remove_active_pointer:
                current = self._active_id(connection, authority_id)
                if current not in {None, baseline_revision_id}:
                    raise RevisionConflict(baseline_revision_id, current)
                connection.execute(
                    "DELETE FROM active_adaptive_revisions "
                    "WHERE authority_id = ? AND revision_id = ?",
                    (authority_id, baseline_revision_id),
                )
            if remove_baseline:
                row = connection.execute(
                    "SELECT * FROM adaptive_revisions WHERE revision_id = ?",
                    (baseline_revision_id,),
                ).fetchone()
                if row is not None:
                    revision = self._adaptive_from_row(row)
                    if (
                        revision.authority_id != authority_id
                        or not revision.is_baseline
                    ):
                        raise ImmutableRecordConflict(
                            "refusing to roll back a different adaptive revision"
                        )
                    connection.execute(
                        "DELETE FROM adaptive_revisions WHERE revision_id = ?",
                        (baseline_revision_id,),
                    )
            if remove_authority:
                referenced = connection.execute(
                    "SELECT 1 FROM adaptive_revisions WHERE authority_id = ? LIMIT 1",
                    (authority_id,),
                ).fetchone()
                if referenced is not None:
                    raise ImmutableRecordConflict(
                        "refusing to remove an authority with remaining revisions"
                    )
                connection.execute(
                    "DELETE FROM authority_revisions WHERE authority_id = ?",
                    (authority_id,),
                )

    def count_decisions(self) -> int:
        """Return the durable routing-decision count without modifying state."""
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM routing_decisions"
            ).fetchone()[0]
        )

    @staticmethod
    def _evidence_insert_values(
        event: EvidenceEvent,
        document_json: str,
        checksum: str,
    ) -> tuple[Any, ...]:
        return (
            event.evidence_id,
            event.source,
            event.signal_type,
            event.parent_evidence_id,
            event.decision_id,
            event.session_id,
            event.turn_id,
            event.task_id,
            event.route_epoch_id,
            event.runtime_id,
            event.profile_id,
            event.reasoning_effort,
            None if event.context_bucket is None else event.context_bucket.bucket_id,
            int(event.is_initial_routing_task),
            event.outcome,
            event.feedback_value,
            event.normalized_value,
            event.confidence_weight,
            event.attribution_confidence,
            event.api_calls,
            event.tool_iterations,
            event.retry_count,
            event.cost_usd,
            event.input_tokens,
            event.output_tokens,
            event.cache_read_tokens,
            event.latency_seconds,
            document_json,
            checksum,
            event.observed_at,
        )

    def write_evidence_event(self, event: EvidenceEvent) -> EvidenceCommit:
        """Insert one immutable content-free observation or replay it exactly."""
        return self._write_evidence_event(event, observer_budget=False)

    def write_observer_evidence_event(self, event: EvidenceEvent) -> EvidenceCommit:
        """Insert post-turn observer evidence with a fail-fast lock budget."""
        return self._write_evidence_event(event, observer_budget=True)

    def _write_evidence_event(
        self,
        event: EvidenceEvent,
        *,
        observer_budget: bool,
    ) -> EvidenceCommit:
        """Persist evidence identically under normal or observer lock policy."""
        from .evidence import validate_evidence_semantics

        validated = EvidenceEvent.model_validate(event.model_dump(mode="json"))
        validate_evidence_semantics(validated)
        _assert_content_free(validated, writer="evidence")
        document_json = _canonical_json(validated)
        checksum = _checksum(document_json)
        transaction = (
            self.observer_evidence_write_txn
            if observer_budget
            else self.write_txn
        )
        with transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM evidence_events WHERE evidence_id = ?",
                (validated.evidence_id,),
            ).fetchone()
            if existing is not None:
                stored = self._evidence_from_row(
                    connection,
                    existing,
                    validate_relations=True,
                )
                if stored == validated:
                    return EvidenceCommit(stored, "replayed")
                if (
                    stored.source == validated.source == "user_feedback"
                    and stored.model_copy(
                        update={"observed_at": validated.observed_at}
                    )
                    == validated
                ):
                    return EvidenceCommit(stored, "replayed")
                raise ImmutableRecordConflict(
                    f"evidence event {validated.evidence_id!r} "
                    "already has other content"
                )
            self._validate_evidence_relations(
                connection,
                validated,
                require_current_binding=(
                    validated.source == "hermes_turn_outcome"
                ),
            )
            connection.execute(
                "INSERT INTO evidence_events "
                "(evidence_id, source, signal_type, parent_evidence_id, decision_id, "
                "session_id, turn_id, task_id, route_epoch_id, runtime_id, profile_id, "
                "reasoning_effort, context_bucket_id, is_initial_routing_task, outcome, "
                "feedback_value, normalized_value, confidence_weight, "
                "attribution_confidence, api_calls, tool_iterations, retry_count, "
                "cost_usd, input_tokens, output_tokens, cache_read_tokens, "
                "latency_seconds, document_json, checksum, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._evidence_insert_values(validated, document_json, checksum),
            )
        return EvidenceCommit(validated, "inserted")

    def _validate_evidence_relations(
        self,
        connection: sqlite3.Connection,
        event: EvidenceEvent,
        *,
        require_current_binding: bool = False,
    ) -> None:
        decision_row = connection.execute(
            "SELECT * FROM routing_decisions WHERE decision_id = ?",
            (event.decision_id,),
        ).fetchone()
        if decision_row is None:
            raise ImmutableRecordConflict("evidence decision is unavailable")
        decision = self._decision_from_row(decision_row)
        if decision.projection_mode != "active":
            raise ImmutableRecordConflict(
                "only active routed decisions accept evidence"
            )
        if event.profile_id != decision.selected_profile_id:
            raise ImmutableRecordConflict(
                "evidence route profile differs from its decision"
            )

        epoch_row = connection.execute(
            "SELECT * FROM route_epochs WHERE route_epoch_id = ?",
            (event.route_epoch_id,),
        ).fetchone()
        if epoch_row is None:
            raise ImmutableRecordConflict("evidence route epoch is unavailable")
        epoch = self._route_epoch_from_row(epoch_row)
        if (
            epoch.decision_id != event.decision_id
            or epoch.session_id != event.session_id
            or epoch.runtime_id != event.runtime_id
            or epoch.provider_started is not True
        ):
            raise ImmutableRecordConflict(
                "evidence attribution crosses route records"
            )

        recorded_targets = [
            (decision.selected_runtime, decision.selected_reasoning_effort),
            *(
                (target.runtime, target.reasoning.default)
                for target in decision.projected_fallback_chain
            ),
            (
                decision.safe_default_runtime,
                decision.safe_default_reasoning_effort,
            ),
        ]
        efforts = {
            effort
            for runtime, effort in recorded_targets
            if runtime.stable_id() == event.runtime_id
        }
        if efforts != {event.reasoning_effort}:
            raise ImmutableRecordConflict(
                "evidence runtime/effort is not one exact recorded target"
            )

        origin = event.session_id == decision.session_id
        if origin and event.task_id != decision.task_id:
            raise ImmutableRecordConflict(
                "evidence origin task differs from its decision"
            )

        expected_initial = (
            origin
            and event.task_id == decision.task_id
        )
        if event.is_initial_routing_task is not expected_initial:
            raise ImmutableRecordConflict(
                "initial-task evidence flag differs from decision identity"
            )

        if require_current_binding:
            binding_row = connection.execute(
                "SELECT * FROM session_route_bindings WHERE session_id = ?",
                (event.session_id,),
            ).fetchone()
            if binding_row is None:
                raise ImmutableRecordConflict(
                    "evidence session binding is unavailable"
                )
            binding = self._session_binding_from_row(binding_row)
            origin = decision.session_id == binding.session_id
            descendant = (
                binding.continuation_reason == "compression"
                and binding.parent_session_id is not None
                and binding.continuation_root == decision.session_id
            )
            if (
                binding.binding_kind != "routed"
                or binding.projection_mode != "active"
                or binding.decision_id != event.decision_id
                or binding.runtime_id != event.runtime_id
                or binding.current_epoch != epoch.epoch_number
                or not (origin or descendant)
            ):
                raise ImmutableRecordConflict(
                    "evidence does not match the current active routed binding"
                )

        if event.source != "user_feedback":
            return

        row = connection.execute(
            "SELECT * FROM evidence_events WHERE evidence_id = ?",
            (event.parent_evidence_id,),
        ).fetchone()
        if row is None:
            raise ImmutableRecordConflict(
                "feedback parent evidence is unavailable"
            )
        parent = self._evidence_from_row(
            connection,
            row,
            validate_relations=False,
        )
        if parent.source != "hermes_turn_outcome":
            raise ImmutableRecordConflict(
                "feedback parent must be turn evidence"
            )
        self._validate_evidence_relations(
            connection,
            parent,
            require_current_binding=False,
        )
        inherited = (
            "decision_id",
            "session_id",
            "turn_id",
            "task_id",
            "route_epoch_id",
            "runtime_id",
            "profile_id",
            "reasoning_effort",
            "context_bucket",
            "is_initial_routing_task",
            "attribution_confidence",
        )
        if any(
            getattr(event, field_name) != getattr(parent, field_name)
            for field_name in inherited
        ):
            raise ImmutableRecordConflict(
                "feedback attribution differs from its parent"
            )

    def _evidence_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        validate_relations: bool = True,
    ) -> EvidenceEvent:
        from .evidence import validate_evidence_semantics

        identifier = str(row["evidence_id"])
        document_json = str(row["document_json"])
        _verify_checksum(identifier, document_json, str(row["checksum"]))
        document = _assert_canonical(identifier, document_json)
        try:
            event = EvidenceEvent.model_validate(document)
            validate_evidence_semantics(event)
            _assert_content_free(event, writer="evidence")
        except (ValueError, TypeError, UnsafeStoredContent) as error:
            raise RevisionChecksumError(identifier) from error

        expected: dict[str, Any] = {
            "evidence_id": event.evidence_id,
            "source": event.source,
            "signal_type": event.signal_type,
            "parent_evidence_id": event.parent_evidence_id,
            "decision_id": event.decision_id,
            "session_id": event.session_id,
            "turn_id": event.turn_id,
            "task_id": event.task_id,
            "route_epoch_id": event.route_epoch_id,
            "runtime_id": event.runtime_id,
            "profile_id": event.profile_id,
            "reasoning_effort": event.reasoning_effort,
            "context_bucket_id": (
                None
                if event.context_bucket is None
                else event.context_bucket.bucket_id
            ),
            "is_initial_routing_task": int(event.is_initial_routing_task),
            "outcome": event.outcome,
            "feedback_value": event.feedback_value,
            "normalized_value": event.normalized_value,
            "confidence_weight": event.confidence_weight,
            "attribution_confidence": event.attribution_confidence,
            "api_calls": event.api_calls,
            "tool_iterations": event.tool_iterations,
            "retry_count": event.retry_count,
            "cost_usd": event.cost_usd,
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "cache_read_tokens": event.cache_read_tokens,
            "latency_seconds": event.latency_seconds,
            "observed_at": event.observed_at,
        }
        for field_name, value in expected.items():
            stored = row[field_name]
            if value is None:
                agrees = stored is None
            elif isinstance(value, bool):
                agrees = bool(stored) is value
            elif isinstance(value, int) and not isinstance(value, bool):
                agrees = (
                    isinstance(stored, int)
                    and not isinstance(stored, bool)
                    and stored == value
                )
            elif isinstance(value, float):
                agrees = (
                    isinstance(stored, (int, float))
                    and not isinstance(stored, bool)
                    and stored == value
                )
            else:
                agrees = str(stored) == str(value)
            if not agrees:
                raise RevisionChecksumError(identifier)
        if validate_relations:
            self._validate_evidence_relations(
                connection,
                event,
                require_current_binding=False,
            )
        return event

    @contextlib.contextmanager
    def _evidence_read_txn(self) -> Iterator[sqlite3.Connection]:
        if self.connection.in_transaction:
            yield self.connection
            return
        self.connection.execute("BEGIN")
        try:
            yield self.connection
        finally:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")

    def read_evidence_event(self, evidence_id: str) -> EvidenceEvent | None:
        with self._evidence_read_txn() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_events WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if row is None:
                return None
            return self._evidence_from_row(
                connection,
                row,
                validate_relations=True,
            )

    def count_evidence_events(self) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM evidence_events"
            ).fetchone()[0]
        )

    def list_evidence_events(
        self,
        *,
        decision_id: str | None = None,
        parent_evidence_id: str | None = None,
        profile_id: str | None = None,
        runtime_id: str | None = None,
        reasoning_effort: str | None = None,
        observed_at_or_after: str | None = None,
    ) -> tuple[EvidenceEvent, ...]:
        filters = {
            "decision_id": decision_id,
            "parent_evidence_id": parent_evidence_id,
            "profile_id": profile_id,
            "runtime_id": runtime_id,
            "reasoning_effort": reasoning_effort,
            "observed_at": observed_at_or_after,
        }
        clauses: list[str] = []
        values: list[str] = []
        for column, value in filters.items():
            if value is None:
                continue
            operator = ">=" if column == "observed_at" else "="
            clauses.append(f"{column} {operator} ?")
            values.append(value)
        query = "SELECT * FROM evidence_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY observed_at, evidence_id"
        with self._evidence_read_txn() as connection:
            rows = connection.execute(query, tuple(values)).fetchall()
            return tuple(
                self._evidence_from_row(
                    connection,
                    row,
                    validate_relations=True,
                )
                for row in rows
            )

    def claim_decision_operation(
        self,
        *,
        scope: Literal["fresh_session", "delegation"],
        session_id: str,
        operation_id: str | None,
        task_index: int | None,
        facts_hash: str,
        lease_seconds: float,
    ) -> DecisionOperationClaim:
        """Claim the canonical route boundary before any classifier request."""
        key = _operation_key(
            scope=scope,
            session_id=session_id,
            operation_id=operation_id,
            task_index=task_index,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", facts_hash):
            raise ValueError("facts_hash must be a lowercase SHA-256 digest")
        if (
            not isinstance(lease_seconds, (int, float))
            or isinstance(
                lease_seconds,
                bool,
            )
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be finite and positive")
        owner_pid = os.getpid()
        owner_start_token = current_process_start_token()
        new_claim_id = uuid.uuid4().hex
        now = time.time()
        expires_at = now + float(lease_seconds)
        claimed_at = _timestamp(now)

        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM decision_operations WHERE operation_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                self._verify_operation_row(row)
                self._verify_operation_boundary(
                    row,
                    scope=scope,
                    session_id=session_id,
                    operation_id=operation_id,
                    task_index=task_index,
                )
                if str(row["facts_hash"]) != facts_hash:
                    raise ImmutableRecordConflict(
                        f"operation {key!r} already has a different facts hash"
                    )
                if str(row["status"]) == "complete":
                    self._completed_decision_from_operation_row(
                        connection,
                        row,
                    )
                    return self._operation_claim_from_row(row, status="replayed")
                existing_pid = int(row["owner_pid"])
                existing_token = str(row["owner_start_token"])
                if existing_pid == owner_pid and existing_token == owner_start_token:
                    return self._operation_claim_from_row(
                        row,
                        status="waiting",
                        claim_id=new_claim_id,
                    )
                if _process_owner_is_live(existing_pid, existing_token):
                    return self._operation_claim_from_row(
                        row,
                        status="waiting",
                        claim_id=new_claim_id,
                    )
                document = _operation_document(
                    operation_key=key,
                    claim_id=new_claim_id,
                    scope=scope,
                    session_id=session_id,
                    operation_id=operation_id,
                    task_index=task_index,
                    facts_hash=facts_hash,
                    owner_pid=owner_pid,
                    owner_start_token=owner_start_token,
                    lease_expires_at=expires_at,
                    status="claimed",
                    decision_id=None,
                    claimed_at=claimed_at,
                    updated_at=claimed_at,
                )
                _assert_content_free(document, writer="decision")
                document_json = _canonical_json(document)
                connection.execute(
                    "UPDATE decision_operations SET claim_id = ?, owner_pid = ?, "
                    "owner_start_token = ?, lease_expires_at = ?, status = 'claimed', "
                    "decision_id = NULL, document_json = ?, checksum = ?, "
                    "claimed_at = ?, updated_at = ? WHERE operation_key = ?",
                    (
                        new_claim_id,
                        owner_pid,
                        owner_start_token,
                        expires_at,
                        document_json,
                        _checksum(document_json),
                        claimed_at,
                        claimed_at,
                        key,
                    ),
                )
            else:
                document = _operation_document(
                    operation_key=key,
                    claim_id=new_claim_id,
                    scope=scope,
                    session_id=session_id,
                    operation_id=operation_id,
                    task_index=task_index,
                    facts_hash=facts_hash,
                    owner_pid=owner_pid,
                    owner_start_token=owner_start_token,
                    lease_expires_at=expires_at,
                    status="claimed",
                    decision_id=None,
                    claimed_at=claimed_at,
                    updated_at=claimed_at,
                )
                _assert_content_free(document, writer="decision")
                document_json = _canonical_json(document)
                connection.execute(
                    "INSERT INTO decision_operations "
                    "(operation_key, claim_id, scope, session_id, operation_id, task_index, "
                    "facts_hash, owner_pid, owner_start_token, lease_expires_at, "
                    "status, decision_id, document_json, checksum, claimed_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', NULL, ?, ?, ?, ?)",
                    (
                        key,
                        new_claim_id,
                        scope,
                        session_id,
                        operation_id,
                        task_index,
                        facts_hash,
                        owner_pid,
                        owner_start_token,
                        expires_at,
                        document_json,
                        _checksum(document_json),
                        claimed_at,
                        claimed_at,
                    ),
                )
            stored = connection.execute(
                "SELECT * FROM decision_operations WHERE operation_key = ?",
                (key,),
            ).fetchone()
            if stored is None:  # pragma: no cover - transaction invariant
                raise StoreError("decision operation vanished after claim")
            return self._operation_claim_from_row(stored, status="claimed")

    def wait_for_decision_operation(
        self,
        claim: DecisionOperationClaim,
        *,
        timeout_seconds: float,
    ) -> RoutingDecision | None:
        """Poll fresh committed reads without holding a transaction or lease."""
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(
                timeout_seconds,
                bool,
            )
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds must be finite and non-negative")
        deadline = time.monotonic() + float(timeout_seconds)
        while True:
            row = self.connection.execute(
                "SELECT * FROM decision_operations WHERE operation_key = ?",
                (claim.operation_key,),
            ).fetchone()
            if row is None:
                return None
            self._verify_operation_row(row)
            self._verify_operation_boundary(
                row,
                scope=claim.scope,
                session_id=claim.session_id,
                operation_id=claim.operation_id,
                task_index=claim.task_index,
            )
            if str(row["facts_hash"]) != claim.facts_hash:
                raise ImmutableRecordConflict(
                    "decision operation facts changed while waiting"
                )
            decision_id = row["decision_id"]
            if str(row["status"]) == "complete" and decision_id is not None:
                return self._completed_decision_from_operation_row(
                    self.connection,
                    row,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeRoutingPending(
                    f"route operation {claim.operation_key!r} is still owned by "
                    f"live process {row['owner_pid']}"
                )
            time.sleep(min(0.020, remaining))

    @staticmethod
    def _verify_operation_row(row: sqlite3.Row) -> None:
        identifier = str(row["operation_key"])
        document_json = str(row["document_json"])
        _verify_checksum(identifier, document_json, str(row["checksum"]))
        document = _assert_canonical(identifier, document_json)
        expected = _operation_document(
            operation_key=identifier,
            claim_id=str(row["claim_id"]),
            scope=str(row["scope"]),
            session_id=str(row["session_id"]),
            operation_id=(
                None if row["operation_id"] is None else str(row["operation_id"])
            ),
            task_index=(None if row["task_index"] is None else int(row["task_index"])),
            facts_hash=str(row["facts_hash"]),
            owner_pid=int(row["owner_pid"]),
            owner_start_token=str(row["owner_start_token"]),
            lease_expires_at=float(row["lease_expires_at"]),
            status=str(row["status"]),
            decision_id=(
                None if row["decision_id"] is None else str(row["decision_id"])
            ),
            claimed_at=str(row["claimed_at"]),
            updated_at=str(row["updated_at"]),
        )
        if document != expected:
            raise RevisionChecksumError(identifier)
        try:
            _require_canonical_timestamp(
                row["claimed_at"],
                field_name="claimed_at",
            )
            _require_canonical_timestamp(
                row["updated_at"],
                field_name="updated_at",
            )
            scope = str(row["scope"])
            if scope not in {"fresh_session", "delegation"}:
                raise ValueError("invalid operation scope")
            canonical_key = _operation_key(
                scope=scope,  # type: ignore[arg-type]
                session_id=str(row["session_id"]),
                operation_id=(
                    None if row["operation_id"] is None else str(row["operation_id"])
                ),
                task_index=(
                    None if row["task_index"] is None else int(row["task_index"])
                ),
            )
        except (TypeError, ValueError) as error:
            raise RevisionChecksumError(identifier) from error
        if canonical_key != identifier:
            raise RevisionChecksumError(identifier)

    @staticmethod
    def _verify_operation_boundary(
        row: sqlite3.Row,
        *,
        scope: Literal["fresh_session", "delegation"],
        session_id: str,
        operation_id: str | None,
        task_index: int | None,
    ) -> None:
        stored_operation_id = (
            None if row["operation_id"] is None else str(row["operation_id"])
        )
        stored_task_index = (
            None if row["task_index"] is None else int(row["task_index"])
        )
        if (
            str(row["scope"]) != scope
            or str(row["session_id"]) != session_id
            or stored_operation_id != operation_id
            or stored_task_index != task_index
        ):
            raise ImmutableRecordConflict(
                "decision operation boundary identity changed"
            )

    def _completed_decision_from_operation_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> RoutingDecision:
        operation_key = str(row["operation_key"])
        decision_id = row["decision_id"]
        if str(row["status"]) != "complete" or decision_id is None:
            raise RevisionChecksumError(operation_key)
        decision_row = connection.execute(
            "SELECT * FROM routing_decisions WHERE decision_id = ?",
            (str(decision_id),),
        ).fetchone()
        if decision_row is None:
            raise RevisionChecksumError(operation_key)
        decision = self._decision_with_bundle_from_row(decision_row)
        stored_operation_id = (
            None if row["operation_id"] is None else str(row["operation_id"])
        )
        stored_task_index = (
            None if row["task_index"] is None else int(row["task_index"])
        )
        if (
            decision.decision_id != str(decision_id)
            or decision.scope != str(row["scope"])
            or decision.session_id != str(row["session_id"])
            or decision.operation_id != stored_operation_id
            or decision.task_index != stored_task_index
            or decision.task_facts_hash != str(row["facts_hash"])
        ):
            raise RevisionChecksumError(operation_key)
        return decision

    @staticmethod
    def _operation_claim_from_row(
        row: sqlite3.Row,
        *,
        status: Literal["claimed", "waiting", "replayed"],
        claim_id: str | None = None,
    ) -> DecisionOperationClaim:
        return DecisionOperationClaim(
            operation_key=str(row["operation_key"]),
            claim_id=(str(row["claim_id"]) if claim_id is None else claim_id),
            scope=str(row["scope"]),  # type: ignore[arg-type]
            session_id=str(row["session_id"]),
            operation_id=(
                None if row["operation_id"] is None else str(row["operation_id"])
            ),
            task_index=(None if row["task_index"] is None else int(row["task_index"])),
            facts_hash=str(row["facts_hash"]),
            owner_pid=int(row["owner_pid"]),
            owner_start_token=str(row["owner_start_token"]),
            lease_expires_at=float(row["lease_expires_at"]),
            status=status,
            decision_id=(
                None if row["decision_id"] is None else str(row["decision_id"])
            ),
        )

    def _validate_fresh_adaptive_decision_attestation(
        self,
        connection: sqlite3.Connection,
        decision: RoutingDecision,
    ) -> None:
        """Require fresh adaptive decision references to resolve transactionally."""
        snapshot = dict(decision.adaptive_profile_snapshot)
        if not snapshot:
            # v6 decisions predate profile-local adaptation attestations.
            return

        authority_row = connection.execute(
            "SELECT * FROM authority_revisions WHERE authority_id=?",
            (decision.authority_revision,),
        ).fetchone()
        if authority_row is None:
            raise ImmutableRecordConflict(
                "fresh adaptive decision references an unavailable authority"
            )
        authority = self._authority_from_row(authority_row)
        try:
            authority_value = json.loads(authority.document_json)
            authority_identity = hashlib.sha256(
                json.dumps(
                    authority_value,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            recorded_profiles = authority_value["profiles"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ImmutableRecordConflict(
                "fresh adaptive decision references an invalid authority"
            ) from error
        if (
            authority_identity != decision.authority_revision
            or not isinstance(recorded_profiles, Mapping)
            or set(snapshot) != set(recorded_profiles)
        ):
            raise ImmutableRecordConflict(
                "fresh adaptive decision has an incomplete adaptive profile snapshot"
            )
        if decision.selected_profile_id is None:
            raise ImmutableRecordConflict(
                "fresh adaptive decision has no selected adaptive profile"
            )
        selected_revision_id = snapshot.get(decision.selected_profile_id)
        if selected_revision_id != decision.profile_adaptive_revision_id:
            raise ImmutableRecordConflict(
                "fresh adaptive decision selected profile revision does not match its snapshot"
            )

        selected_revision: AdaptiveProfileRevision | None = None
        for profile_id, revision_id in snapshot.items():
            if revision_id == static_adaptive_revision_id(
                decision.authority_revision,
                profile_id,
            ):
                continue
            revision = self._require_linked_profile_revision(
                connection,
                revision_id,
                decision.authority_revision,
                profile_id,
            )
            if profile_id == decision.selected_profile_id:
                selected_revision = revision

        if decision.adaptive_assignment_id is None:
            return
        if selected_revision is None:
            # A static snapshot normally needs no row lookup, but a canary arm
            # may legitimately use that published static revision as control.
            # In that case exact runtime/effort attestation still requires its
            # complete immutable overlay.
            selected_revision = self._require_linked_profile_revision(
                connection,
                str(selected_revision_id),
                decision.authority_revision,
                str(decision.selected_profile_id),
            )
        assignment_row = connection.execute(
            "SELECT * FROM adaptive_canary_assignments WHERE assignment_id=?",
            (decision.adaptive_assignment_id,),
        ).fetchone()
        if assignment_row is None:
            raise ImmutableRecordConflict(
                "fresh adaptive decision references an unavailable canary assignment"
            )
        assignment = self._canary_assignment_from_row(assignment_row)
        self._require_linked_profile_revision(
            connection,
            assignment.control_revision_id,
            assignment.authority_id,
            assignment.profile_id,
        )
        self._require_linked_profile_revision(
            connection,
            assignment.challenger_revision_id,
            assignment.authority_id,
            assignment.profile_id,
        )
        expected_revision_id = (
            assignment.challenger_revision_id
            if assignment.arm == "challenger"
            else assignment.control_revision_id
        )
        if (
            assignment.authority_id != decision.authority_revision
            or assignment.profile_id != decision.selected_profile_id
            or selected_revision_id != expected_revision_id
            or selected_revision is None
            or selected_revision.revision_id != expected_revision_id
        ):
            raise ImmutableRecordConflict(
                "fresh adaptive decision canary assignment does not match its selected revision"
            )
        expected_runtime_id = selected_revision.overlay.ordered_primary_runtime_ids[0]
        expected_effort = selected_revision.overlay.reasoning_defaults.get(
            expected_runtime_id
        )
        if (
            expected_effort is None
            or decision.selected_runtime.stable_id() != expected_runtime_id
            or decision.selected_reasoning_effort != expected_effort
        ):
            raise ImmutableRecordConflict(
                "fresh adaptive decision canary assignment does not match its final runtime"
            )

    def _validate_fresh_management_decision_attestation(
        self,
        connection: sqlite3.Connection,
        decision: RoutingDecision,
    ) -> None:
        """Require the selected management arm to remain live at publication."""
        snapshot = dict(decision.management_profile_snapshot)
        if not snapshot:
            return
        if (
            decision.selected_profile_id is None
            or decision.management_revision_id is None
            or decision.management_assignment_id is None
        ):
            raise ImmutableRecordConflict(
                "fresh management decision has an incomplete attestation"
            )
        selected_revision_id = snapshot.get(decision.selected_profile_id)
        if selected_revision_id != decision.management_revision_id:
            raise ImmutableRecordConflict(
                "fresh management decision selected revision changed"
            )

        authority_row = connection.execute(
            "SELECT * FROM authority_revisions WHERE authority_id=?",
            (decision.authority_revision,),
        ).fetchone()
        if authority_row is None:
            raise ImmutableRecordConflict(
                "fresh management decision references an unavailable authority"
            )
        authority = self._authority_from_row(authority_row)
        try:
            authority_value = json.loads(authority.document_json)
            authority_identity = hashlib.sha256(
                json.dumps(
                    authority_value,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            management_authority_value = authority_value[
                "autonomous_profile_management"
            ]
            management_authority_id = hashlib.sha256(
                json.dumps(
                    management_authority_value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ImmutableRecordConflict(
                "fresh management decision references an invalid authority"
            ) from error
        if authority_identity != decision.authority_revision:
            raise ImmutableRecordConflict(
                "fresh management decision authority identity changed"
            )

        assignment_row = connection.execute(
            "SELECT * FROM management_canary_assignments WHERE assignment_id=?",
            (decision.management_assignment_id,),
        ).fetchone()
        if assignment_row is None:
            raise ImmutableRecordConflict(
                "fresh management decision references an unavailable assignment"
            )
        assignment = self._management_assignment_from_row(assignment_row)
        control, challenger = self._validate_management_assignment_links(
            connection,
            assignment,
        )
        state_row = connection.execute(
            "SELECT * FROM management_profile_states "
            "WHERE management_authority_id=? AND profile_id=?",
            (assignment.management_authority_id, assignment.profile_id),
        ).fetchone()
        if state_row is None:
            raise ImmutableRecordConflict(
                "fresh management decision references unavailable canary state"
            )
        state = self._management_profile_state_from_row(state_row)
        self._validate_management_profile_state_links(connection, state)
        expected_revision = challenger if assignment.arm == "challenger" else control
        control_patch = next(
            patch
            for patch in control.patches
            if patch.profile_id == assignment.profile_id
        )
        challenger_patch = next(
            patch
            for patch in challenger.patches
            if patch.profile_id == assignment.profile_id
        )
        if assignment.arm == "control":
            expected_runtime_id = control_patch.after_runtime_ids[0]
        else:
            ranked_runtime_id = (
                challenger.runtime_scores[0][0]
                if challenger.runtime_scores
                else None
            )
            if (
                ranked_runtime_id is not None
                and ranked_runtime_id in challenger_patch.after_runtime_ids
                and ranked_runtime_id != control_patch.after_runtime_ids[0]
            ):
                expected_runtime_id = ranked_runtime_id
            else:
                introduced = tuple(
                    runtime_id
                    for runtime_id in challenger_patch.after_runtime_ids
                    if runtime_id not in set(challenger_patch.before_runtime_ids)
                )
                if len(introduced) != 1:
                    raise ImmutableRecordConflict(
                        "fresh management decision challenger target is ambiguous"
                    )
                expected_runtime_id = introduced[0]
        expected_operation_hash = operation_identity_hash(
            scope=decision.scope,
            session_id=decision.session_id,
            task_id=decision.task_id,
            operation_id=decision.operation_id,
            task_index=decision.task_index,
        )
        if (
            assignment.phase != "finalized"
            or assignment.management_authority_id != management_authority_id
            or assignment.profile_id != decision.selected_profile_id
            or assignment.operation_identity_hash != expected_operation_hash
            or state.experiment_phase != "canary"
            or state.active_revision_id != assignment.control_revision_id
            or state.control_revision_id != assignment.control_revision_id
            or state.challenger_revision_id != assignment.challenger_revision_id
            or challenger.resulting_authority_id != decision.authority_revision
            or expected_revision.revision_id != selected_revision_id
            or assignment.runtime_id != expected_runtime_id
            or assignment.runtime_id != decision.selected_runtime.stable_id()
            or assignment.reasoning_effort != decision.selected_reasoning_effort
        ):
            raise ImmutableRecordConflict(
                "fresh management decision no longer matches its exact canary attestation"
            )

    def commit_decision(
        self,
        decision: RoutingDecision,
        *,
        candidates: Sequence[DecisionCandidate],
        create_epoch: bool,
        claim: DecisionOperationClaim | None = None,
    ) -> DecisionCommit:
        """Atomically publish one complete immutable routing decision bundle."""
        if not isinstance(create_epoch, bool):
            raise ValueError("create_epoch must be a bool")
        validated = RoutingDecision.model_validate(
            decision.model_dump(mode="json", by_alias=True)
        )
        validated_candidates = tuple(
            DecisionCandidate.model_validate(
                candidate.model_dump(mode="json", by_alias=True)
            )
            for candidate in candidates
        )
        _validate_decision_candidate_coherence(validated, validated_candidates)
        _assert_content_free(validated, writer="decision")
        for candidate in validated_candidates:
            _assert_content_free(candidate, writer="decision")
        document_json = _canonical_json(validated)
        checksum = _checksum(document_json)
        candidate_bundle_checksum = _candidate_bundle_checksum(validated_candidates)
        operation_key = _operation_key(
            scope=validated.scope,
            session_id=validated.session_id,
            operation_id=validated.operation_id,
            task_index=validated.task_index,
        )
        selected_runtime_id = validated.selected_runtime.stable_id()

        replayed = False
        with self.write_txn() as connection:
            existing = self._find_boundary_decision_row(connection, validated)
            by_id = connection.execute(
                "SELECT * FROM routing_decisions WHERE decision_id = ?",
                (validated.decision_id,),
            ).fetchone()
            if existing is not None or by_id is not None:
                row = existing if existing is not None else by_id
                if row is None:  # pragma: no cover - narrowing invariant
                    raise StoreError("decision lookup invariant failed")
                stored = self._decision_from_row(row)
                stored_candidates = self._read_decision_candidates(
                    connection,
                    stored.decision_id,
                )
                if (
                    stored != validated
                    or stored_candidates != validated_candidates
                    or str(row["document_json"]) != document_json
                    or str(row["checksum"]) != checksum
                    or str(row["candidate_bundle_checksum"])
                    != candidate_bundle_checksum
                ):
                    raise ImmutableRecordConflict(
                        f"decision boundary {operation_key!r} already has other content"
                    )
                binding_row = connection.execute(
                    "SELECT * FROM session_route_bindings WHERE session_id = ?",
                    (validated.session_id,),
                ).fetchone()
                if binding_row is None:
                    raise ImmutableRecordConflict(
                        "decision replay is missing its authoritative session binding"
                    )
                binding = self._session_binding_from_row(binding_row)
                if (
                    binding.binding_kind != "routed"
                    or binding.decision_id != validated.decision_id
                ):
                    raise ImmutableRecordConflict(
                        "decision replay conflicts with current session intent"
                    )
                epoch_zero = connection.execute(
                    "SELECT 1 FROM route_epochs WHERE session_id = ? "
                    "AND decision_id = ? AND epoch_number = 0",
                    (validated.session_id, validated.decision_id),
                ).fetchone()
                if (epoch_zero is not None) != create_epoch:
                    raise ImmutableRecordConflict(
                        "decision replay changed initial epoch publication"
                    )
                operation_row = connection.execute(
                    "SELECT * FROM decision_operations WHERE operation_key = ?",
                    (operation_key,),
                ).fetchone()
                if operation_row is None:
                    raise ImmutableRecordConflict(
                        "decision replay is missing its completed operation"
                    )
                self._verify_operation_row(operation_row)
                if (
                    str(operation_row["status"]) != "complete"
                    or str(operation_row["decision_id"]) != validated.decision_id
                    or str(operation_row["facts_hash"]) != validated.task_facts_hash
                ):
                    raise ImmutableRecordConflict(
                        "decision replay conflicts with completed operation"
                    )
                replayed = True
            else:
                self._validate_active_receipt(connection, validated)
                self._validate_fresh_adaptive_decision_attestation(
                    connection,
                    validated,
                )
                self._validate_fresh_management_decision_attestation(
                    connection,
                    validated,
                )
                existing_binding_row = connection.execute(
                    "SELECT * FROM session_route_bindings WHERE session_id = ?",
                    (validated.session_id,),
                ).fetchone()
                if existing_binding_row is not None:
                    existing_binding = self._session_binding_from_row(
                        existing_binding_row
                    )
                    raise ImmutableRecordConflict(
                        "fresh route conflicts with existing "
                        f"{existing_binding.binding_kind} session intent"
                    )
                connection.execute(
                    "INSERT INTO routing_decisions "
                    "(decision_id, authority_id, scope, session_id, task_id, "
                    "operation_id, task_index, task_facts_hash, selected_profile_id, "
                    "projection_mode, activation_receipt_id, activation_config_sha, "
                    "adapter_capability_sha, authority_revision_id, "
                    "candidate_bundle_checksum, "
                    "inventory_revision_id, catalog_revision_id, policy_revision_id, "
                    "adaptive_revision_id, profile_adaptive_revision_id, "
                    "adaptive_assignment_id, adaptive_profile_snapshot_json, "
                    "selected_runtime_id, document_json, "
                    "checksum, created_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?)",
                    (
                        validated.decision_id,
                        validated.authority_revision,
                        validated.scope,
                        validated.session_id,
                        validated.task_id,
                        validated.operation_id,
                        validated.task_index,
                        validated.task_facts_hash,
                        validated.selected_profile_id,
                        validated.projection_mode,
                        validated.activation_receipt_id,
                        validated.activation_config_sha,
                        validated.adapter_capability_sha,
                        validated.authority_revision,
                        candidate_bundle_checksum,
                        validated.inventory_revision,
                        validated.catalog_revision,
                        validated.policy_revision,
                        validated.adaptive_revision,
                        validated.profile_adaptive_revision_id,
                        validated.adaptive_assignment_id,
                        (
                            _canonical_json(validated.adaptive_profile_snapshot)
                            if validated.adaptive_profile_snapshot
                            else None
                        ),
                        selected_runtime_id,
                        document_json,
                        checksum,
                        validated.created_at,
                    ),
                )
                for ordinal, candidate in enumerate(validated_candidates):
                    candidate_json = _canonical_json(candidate)
                    connection.execute(
                        "INSERT INTO decision_candidates "
                        "(decision_id, candidate_id, profile_id, target_role, "
                        "target_ordinal, runtime_id, ordinal, eligible, "
                        "reason_codes_json, scoring_json, document_json, checksum) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            validated.decision_id,
                            candidate.candidate_id,
                            candidate.profile_id,
                            candidate.target_role,
                            candidate.target_ordinal,
                            candidate.runtime_id,
                            ordinal,
                            int(candidate.eligible),
                            _canonical_json(candidate.reason_codes),
                            _canonical_json(candidate.normalized_scoring_inputs),
                            candidate_json,
                            _checksum(candidate_json),
                        ),
                    )
                initial_binding = SessionRouteBinding(
                    session_id=validated.session_id,
                    binding_kind="routed",
                    projection_mode=validated.projection_mode,
                    decision_id=validated.decision_id,
                    runtime_id=selected_runtime_id,
                    manual_pin_source=None,
                    current_epoch=0 if create_epoch else -1,
                    continuation_root=None,
                    parent_session_id=None,
                    continuation_reason=None,
                    created_at=validated.created_at,
                )
                binding_json = _canonical_json(_binding_document(initial_binding))
                connection.execute(
                    "INSERT INTO session_route_bindings "
                    "(session_id, binding_kind, projection_mode, decision_id, "
                    "runtime_id, manual_pin_source, current_epoch, continuation_root, "
                    "parent_session_id, continuation_reason, created_at, "
                    "document_json, checksum) "
                    "VALUES (?, 'routed', ?, ?, ?, NULL, ?, NULL, NULL, NULL, ?, ?, ?)",
                    (
                        validated.session_id,
                        validated.projection_mode,
                        validated.decision_id,
                        selected_runtime_id,
                        0 if create_epoch else -1,
                        validated.created_at,
                        binding_json,
                        _checksum(binding_json),
                    ),
                )
                if create_epoch:
                    route_epoch_id = self._route_epoch_id(
                        validated.session_id,
                        validated.decision_id,
                        0,
                        selected_runtime_id,
                    )
                    initial_epoch = RouteEpoch(
                        route_epoch_id=route_epoch_id,
                        session_id=validated.session_id,
                        decision_id=validated.decision_id,
                        epoch_number=0,
                        runtime_id=selected_runtime_id,
                        reason_code="initial_route",
                        started_at=validated.created_at,
                    )
                    epoch_json = _canonical_json(_route_epoch_document(initial_epoch))
                    connection.execute(
                        "INSERT INTO route_epochs "
                        "(route_epoch_id, session_id, decision_id, epoch_number, "
                        "runtime_id, reason_code, started_at, ended_at, "
                        "provider_started, api_request_id, provider_started_at, "
                        "document_json, checksum) "
                        "VALUES (?, ?, ?, 0, ?, 'initial_route', ?, NULL, 0, NULL, NULL, ?, ?)",
                        (
                            route_epoch_id,
                            validated.session_id,
                            validated.decision_id,
                            selected_runtime_id,
                            validated.created_at,
                            epoch_json,
                            _checksum(epoch_json),
                        ),
                    )
                self._complete_decision_operation(
                    connection,
                    operation_key=operation_key,
                    decision=validated,
                    claim=claim,
                )

        stored_decision = self.read_decision(validated.decision_id)
        binding = self.read_session_binding(validated.session_id)
        if stored_decision is None or binding is None:
            raise StoreError("decision bundle vanished after commit")
        epochs = self.read_route_epochs(validated.session_id)
        epoch = None
        if create_epoch:
            epoch = next(
                (item for item in epochs if item.epoch_number == binding.current_epoch),
                None,
            )
            if epoch is None:
                raise StoreError("current decision route epoch vanished after commit")
        return DecisionCommit(
            decision=stored_decision,
            candidates=validated_candidates,
            binding=binding,
            epoch=epoch,
            status="replayed" if replayed else "computed",
        )

    @staticmethod
    def _find_boundary_decision_row(
        connection: sqlite3.Connection,
        decision: RoutingDecision,
    ) -> sqlite3.Row | None:
        if decision.scope == "fresh_session":
            return connection.execute(
                "SELECT * FROM routing_decisions "
                "WHERE scope = 'fresh_session' AND session_id = ?",
                (decision.session_id,),
            ).fetchone()
        return connection.execute(
            "SELECT * FROM routing_decisions WHERE scope = 'delegation' "
            "AND operation_id = ? AND task_index = ?",
            (decision.operation_id, decision.task_index),
        ).fetchone()

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> RoutingDecision:
        identifier = str(row["decision_id"])
        document_json = str(row["document_json"])
        _verify_checksum(identifier, document_json, str(row["checksum"]))
        document = _assert_canonical(identifier, document_json)
        try:
            decision = RoutingDecision.model_validate(document)
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        expected = {
            "decision_id": decision.decision_id,
            "authority_id": decision.authority_revision,
            "scope": decision.scope,
            "session_id": decision.session_id,
            "task_id": decision.task_id,
            "operation_id": decision.operation_id,
            "task_index": decision.task_index,
            "task_facts_hash": decision.task_facts_hash,
            "selected_profile_id": decision.selected_profile_id,
            "projection_mode": decision.projection_mode,
            "activation_receipt_id": decision.activation_receipt_id,
            "activation_config_sha": decision.activation_config_sha,
            "adapter_capability_sha": decision.adapter_capability_sha,
            "authority_revision_id": decision.authority_revision,
            "inventory_revision_id": decision.inventory_revision,
            "catalog_revision_id": decision.catalog_revision,
            "policy_revision_id": decision.policy_revision,
            "adaptive_revision_id": decision.adaptive_revision,
            "profile_adaptive_revision_id": decision.profile_adaptive_revision_id,
            "adaptive_assignment_id": decision.adaptive_assignment_id,
            "adaptive_profile_snapshot_json": (
                _canonical_json(decision.adaptive_profile_snapshot)
                if decision.adaptive_profile_snapshot
                else None
            ),
            "selected_runtime_id": decision.selected_runtime.stable_id(),
            "created_at": decision.created_at,
        }
        for field_name, value in expected.items():
            stored = row[field_name]
            if (
                value is None
                and stored is not None
                or value is not None
                and str(stored) != str(value)
            ):
                raise RevisionChecksumError(identifier)
        return decision

    @staticmethod
    def _read_decision_candidates(
        connection: sqlite3.Connection,
        decision_id: str,
    ) -> tuple[DecisionCandidate, ...]:
        rows = connection.execute(
            "SELECT * FROM decision_candidates WHERE decision_id = ? "
            "ORDER BY ordinal, candidate_id",
            (decision_id,),
        ).fetchall()
        candidates: list[DecisionCandidate] = []
        for expected_ordinal, row in enumerate(rows):
            identifier = str(row["candidate_id"])
            document_json = str(row["document_json"])
            _verify_checksum(identifier, document_json, str(row["checksum"]))
            document = _assert_canonical(identifier, document_json)
            try:
                candidate = DecisionCandidate.model_validate(document)
            except Exception as error:
                raise RevisionChecksumError(identifier) from error
            if (
                int(row["ordinal"]) != expected_ordinal
                or str(row["candidate_id"]) != candidate.candidate_id
                or str(row["profile_id"]) != candidate.profile_id
                or str(row["target_role"]) != candidate.target_role
                or int(row["target_ordinal"]) != candidate.target_ordinal
                or str(row["runtime_id"]) != candidate.runtime_id
                or bool(row["eligible"]) != candidate.eligible
                or json.loads(str(row["reason_codes_json"]))
                != list(candidate.reason_codes)
                or json.loads(str(row["scoring_json"]))
                != [list(item) for item in candidate.normalized_scoring_inputs]
            ):
                raise RevisionChecksumError(identifier)
            candidates.append(candidate)
        return tuple(candidates)

    def _validate_active_receipt(
        self,
        connection: sqlite3.Connection,
        decision: RoutingDecision,
    ) -> None:
        if decision.projection_mode != "active":
            return
        row = connection.execute(
            "SELECT * FROM activation_receipts WHERE receipt_id = ?",
            (decision.activation_receipt_id,),
        ).fetchone()
        if row is None:
            raise ImmutableRecordConflict(
                "active decision activation receipt is missing"
            )
        receipt = self._activation_receipt_from_row(row)
        if (
            receipt.authority_id != decision.authority_revision
            or receipt.config_sha != decision.activation_config_sha
            or receipt.adapter_capability_sha != decision.adapter_capability_sha
        ):
            raise ImmutableRecordConflict(
                "active decision does not match its activation receipt"
            )

    def _complete_decision_operation(
        self,
        connection: sqlite3.Connection,
        *,
        operation_key: str,
        decision: RoutingDecision,
        claim: DecisionOperationClaim | None,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM decision_operations WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        owner_pid = os.getpid()
        owner_start_token = current_process_start_token()
        claim_id = uuid.uuid4().hex
        lease_expires_at = time.time()
        if row is None and claim is not None:
            raise ImmutableRecordConflict(
                "decision claim does not match a durable operation lease"
            )
        if row is not None:
            self._verify_operation_row(row)
            if str(row["facts_hash"]) != decision.task_facts_hash:
                raise ImmutableRecordConflict(
                    "decision facts do not match claimed operation facts hash"
                )
            if (
                str(row["status"]) == "complete"
                and str(row["decision_id"]) != decision.decision_id
            ):
                raise ImmutableRecordConflict(
                    "decision operation already names another decision"
                )
            row_pid = int(row["owner_pid"])
            row_token = str(row["owner_start_token"])
            row_claim_id = str(row["claim_id"])
            if str(row["status"]) == "claimed":
                exact_owner = (
                    claim is not None
                    and claim.status == "claimed"
                    and claim.operation_key == operation_key
                    and claim.claim_id == row_claim_id
                    and claim.facts_hash == decision.task_facts_hash
                    and claim.owner_pid == row_pid
                    and claim.owner_start_token == row_token
                    and row_pid == owner_pid
                    and row_token == owner_start_token
                )
                if not exact_owner and _process_owner_is_live(
                    row_pid,
                    row_token,
                ):
                    raise RuntimeRoutingPending(
                        "another live process owns the route decision operation"
                    )
                if not exact_owner:
                    raise ImmutableRecordConflict(
                        "dead decision owner must be reclaimed before commit"
                    )
            claim_id = row_claim_id
            owner_pid = int(row["owner_pid"])
            owner_start_token = str(row["owner_start_token"])
            lease_expires_at = float(row["lease_expires_at"])
        now = decision.created_at
        claimed_at = now if row is None else str(row["claimed_at"])
        document = _operation_document(
            operation_key=operation_key,
            claim_id=claim_id,
            scope=decision.scope,
            session_id=decision.session_id,
            operation_id=decision.operation_id,
            task_index=decision.task_index,
            facts_hash=decision.task_facts_hash,
            owner_pid=owner_pid,
            owner_start_token=owner_start_token,
            lease_expires_at=lease_expires_at,
            status="complete",
            decision_id=decision.decision_id,
            claimed_at=claimed_at,
            updated_at=now,
        )
        _assert_content_free(document, writer="decision")
        document_json = _canonical_json(document)
        if row is None:
            connection.execute(
                "INSERT INTO decision_operations "
                "(operation_key, claim_id, scope, session_id, operation_id, task_index, "
                "facts_hash, owner_pid, owner_start_token, lease_expires_at, "
                "status, decision_id, document_json, checksum, claimed_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?, ?, ?, ?, ?)",
                (
                    operation_key,
                    claim_id,
                    decision.scope,
                    decision.session_id,
                    decision.operation_id,
                    decision.task_index,
                    decision.task_facts_hash,
                    owner_pid,
                    owner_start_token,
                    lease_expires_at,
                    decision.decision_id,
                    document_json,
                    _checksum(document_json),
                    now,
                    now,
                ),
            )
        else:
            connection.execute(
                "UPDATE decision_operations SET status = 'complete', decision_id = ?, "
                "document_json = ?, checksum = ?, updated_at = ? "
                "WHERE operation_key = ?",
                (
                    decision.decision_id,
                    document_json,
                    _checksum(document_json),
                    now,
                    operation_key,
                ),
            )

    def read_decision(self, decision_id: str) -> RoutingDecision | None:
        row = self.connection.execute(
            "SELECT * FROM routing_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return None if row is None else self._decision_with_bundle_from_row(row)

    def read_session_decision(self, session_id: str) -> RoutingDecision | None:
        row = self.connection.execute(
            "SELECT * FROM routing_decisions "
            "WHERE scope = 'fresh_session' AND session_id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None else self._decision_with_bundle_from_row(row)

    def read_operation_decision(
        self,
        operation_id: str,
        task_index: int,
    ) -> RoutingDecision | None:
        row = self.connection.execute(
            "SELECT * FROM routing_decisions WHERE scope = 'delegation' "
            "AND operation_id = ? AND task_index = ?",
            (operation_id, task_index),
        ).fetchone()
        return None if row is None else self._decision_with_bundle_from_row(row)

    def read_decision_candidates(
        self,
        decision_id: str,
    ) -> tuple[DecisionCandidate, ...]:
        """Read the validated content-free candidate bundle for one decision."""
        decision = self.read_decision(decision_id)
        if decision is None:
            return ()
        return self._read_decision_candidates(self.connection, decision.decision_id)

    def _decision_with_bundle_from_row(self, row: sqlite3.Row) -> RoutingDecision:
        decision = self._decision_from_row(row)
        candidates = self._read_decision_candidates(
            self.connection,
            decision.decision_id,
        )
        stored_checksum = row["candidate_bundle_checksum"]
        if (
            stored_checksum is None
            or re.fullmatch(r"[0-9a-f]{64}", str(stored_checksum)) is None
            or str(stored_checksum) != _candidate_bundle_checksum(candidates)
        ):
            raise RevisionChecksumError(decision.decision_id)
        try:
            _validate_decision_candidate_coherence(decision, candidates)
        except ValueError as error:
            raise RevisionChecksumError(decision.decision_id) from error
        return decision

    def read_route_epochs(self, session_id: str) -> tuple[RouteEpoch, ...]:
        rows = self.connection.execute(
            "SELECT * FROM route_epochs WHERE session_id = ? ORDER BY epoch_number",
            (session_id,),
        ).fetchall()
        return tuple(self._route_epoch_from_row(row) for row in rows)

    @staticmethod
    def _route_epoch_from_row(row: sqlite3.Row) -> RouteEpoch:
        epoch = RouteEpoch(
            route_epoch_id=str(row["route_epoch_id"]),
            session_id=str(row["session_id"]),
            decision_id=str(row["decision_id"]),
            epoch_number=int(row["epoch_number"]),
            runtime_id=str(row["runtime_id"]),
            reason_code=str(row["reason_code"]),
            started_at=str(row["started_at"]),
            ended_at=None if row["ended_at"] is None else str(row["ended_at"]),
            provider_started=bool(row["provider_started"]),
            api_request_id=(
                None if row["api_request_id"] is None else str(row["api_request_id"])
            ),
            provider_started_at=(
                None
                if row["provider_started_at"] is None
                else str(row["provider_started_at"])
            ),
        )
        document_json = str(row["document_json"])
        _verify_checksum(epoch.route_epoch_id, document_json, str(row["checksum"]))
        document = _assert_canonical(epoch.route_epoch_id, document_json)
        if document != _route_epoch_document(epoch):
            raise RevisionChecksumError(epoch.route_epoch_id)
        return epoch

    @staticmethod
    def _route_epoch_id(
        session_id: str,
        decision_id: str,
        epoch_number: int,
        runtime_id: str,
    ) -> str:
        payload = _canonical_json([session_id, decision_id, epoch_number, runtime_id])
        return hashlib.sha256(payload.encode()).hexdigest()

    def start_route_epoch(
        self,
        *,
        session_id: str,
        decision_id: str,
        runtime_id: str,
        reason_code: str,
        started_at: str,
        expected_epoch: int,
    ) -> RouteEpoch:
        if (
            isinstance(expected_epoch, bool)
            or not isinstance(expected_epoch, int)
            or expected_epoch < -1
        ):
            raise ValueError("expected_epoch cannot be below -1")
        values = {
            "session_id": session_id,
            "decision_id": decision_id,
            "runtime_id": runtime_id,
            "reason_code": reason_code,
        }
        _assert_content_free(values, writer="decision")
        with self.write_txn() as connection:
            binding_row = connection.execute(
                "SELECT * FROM session_route_bindings WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if binding_row is None:
                raise ImmutableRecordConflict("session has no routed binding")
            binding = self._session_binding_from_row(binding_row)
            if binding.binding_kind != "routed" or binding.decision_id != decision_id:
                raise ImmutableRecordConflict(
                    "route epoch decision does not match binding"
                )
            if binding.current_epoch != expected_epoch:
                raise ImmutableRecordConflict(
                    f"expected epoch {expected_epoch}, found {binding.current_epoch}"
                )
            epoch_number = expected_epoch + 1
            route_epoch_id = self._route_epoch_id(
                session_id,
                decision_id,
                epoch_number,
                runtime_id,
            )
            new_epoch = RouteEpoch(
                route_epoch_id=route_epoch_id,
                session_id=session_id,
                decision_id=decision_id,
                epoch_number=epoch_number,
                runtime_id=runtime_id,
                reason_code=reason_code,
                started_at=started_at,
            )
            epoch_json = _canonical_json(_route_epoch_document(new_epoch))
            connection.execute(
                "INSERT INTO route_epochs "
                "(route_epoch_id, session_id, decision_id, epoch_number, runtime_id, "
                "reason_code, started_at, ended_at, provider_started, api_request_id, "
                "provider_started_at, document_json, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, ?, ?)",
                (
                    route_epoch_id,
                    session_id,
                    decision_id,
                    epoch_number,
                    runtime_id,
                    reason_code,
                    started_at,
                    epoch_json,
                    _checksum(epoch_json),
                ),
            )
            updated_binding = SessionRouteBinding(
                session_id=binding.session_id,
                binding_kind=binding.binding_kind,
                projection_mode=binding.projection_mode,
                decision_id=binding.decision_id,
                runtime_id=runtime_id,
                manual_pin_source=binding.manual_pin_source,
                current_epoch=epoch_number,
                continuation_root=binding.continuation_root,
                parent_session_id=binding.parent_session_id,
                continuation_reason=binding.continuation_reason,
                created_at=binding.created_at,
            )
            binding_json = _canonical_json(_binding_document(updated_binding))
            updated = connection.execute(
                "UPDATE session_route_bindings SET current_epoch = ?, runtime_id = ?, "
                "document_json = ?, checksum = ? "
                "WHERE session_id = ? AND current_epoch = ?",
                (
                    epoch_number,
                    runtime_id,
                    binding_json,
                    _checksum(binding_json),
                    session_id,
                    expected_epoch,
                ),
            )
            if updated.rowcount != 1:
                raise ImmutableRecordConflict("route epoch compare-and-swap failed")
            row = connection.execute(
                "SELECT * FROM route_epochs WHERE route_epoch_id = ?",
                (route_epoch_id,),
            ).fetchone()
            if row is None:  # pragma: no cover
                raise StoreError("route epoch vanished after insert")
            return self._route_epoch_from_row(row)

    def mark_route_epoch_provider_started(
        self,
        session_id: str,
        *,
        decision_id: str,
        runtime_id: str,
        api_request_id: str,
        started_at: str,
    ) -> RouteEpoch:
        _assert_content_free(
            {
                "session_id": session_id,
                "decision_id": decision_id,
                "runtime_id": runtime_id,
                "api_request_id": api_request_id,
            },
            writer="decision",
        )
        with self.write_txn() as connection:
            binding = connection.execute(
                "SELECT * FROM session_route_bindings WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if binding is None:
                raise ImmutableRecordConflict("session has no route epoch")
            epoch = connection.execute(
                "SELECT * FROM route_epochs WHERE session_id = ? AND epoch_number = ?",
                (session_id, int(binding["current_epoch"])),
            ).fetchone()
            if epoch is None:
                raise ImmutableRecordConflict("session has no current route epoch")
            current = self._route_epoch_from_row(epoch)
            if current.decision_id != decision_id or current.runtime_id != runtime_id:
                raise ImmutableRecordConflict(
                    "provider marker does not match current decision/runtime"
                )
            if current.provider_started:
                return current
            marked = RouteEpoch(
                route_epoch_id=current.route_epoch_id,
                session_id=current.session_id,
                decision_id=current.decision_id,
                epoch_number=current.epoch_number,
                runtime_id=current.runtime_id,
                reason_code=current.reason_code,
                started_at=current.started_at,
                ended_at=current.ended_at,
                provider_started=True,
                api_request_id=api_request_id,
                provider_started_at=started_at,
            )
            document_json = _canonical_json(_route_epoch_document(marked))
            connection.execute(
                "UPDATE route_epochs SET provider_started = 1, api_request_id = ?, "
                "provider_started_at = ?, document_json = ?, checksum = ? "
                "WHERE route_epoch_id = ?",
                (
                    api_request_id,
                    started_at,
                    document_json,
                    _checksum(document_json),
                    current.route_epoch_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM route_epochs WHERE route_epoch_id = ?",
                (current.route_epoch_id,),
            ).fetchone()
            if updated is None:  # pragma: no cover
                raise StoreError("route epoch vanished after provider marker")
            return self._route_epoch_from_row(updated)

    def read_session_binding(self, session_id: str) -> SessionRouteBinding | None:
        row = self.connection.execute(
            "SELECT * FROM session_route_bindings WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None else self._session_binding_from_row(row)

    @staticmethod
    def _session_binding_from_row(row: sqlite3.Row) -> SessionRouteBinding:
        binding = SessionRouteBinding(
            session_id=str(row["session_id"]),
            binding_kind=str(row["binding_kind"]),  # type: ignore[arg-type]
            projection_mode=str(row["projection_mode"]),  # type: ignore[arg-type]
            decision_id=(
                None if row["decision_id"] is None else str(row["decision_id"])
            ),
            runtime_id=str(row["runtime_id"]),
            manual_pin_source=(
                None
                if row["manual_pin_source"] is None
                else str(row["manual_pin_source"])
            ),
            current_epoch=int(row["current_epoch"]),
            continuation_root=(
                None
                if row["continuation_root"] is None
                else str(row["continuation_root"])
            ),
            parent_session_id=(
                None
                if row["parent_session_id"] is None
                else str(row["parent_session_id"])
            ),
            continuation_reason=(
                None
                if row["continuation_reason"] is None
                else str(row["continuation_reason"])
            ),
            created_at=str(row["created_at"]),
        )
        document_json = str(row["document_json"])
        _verify_checksum(binding.session_id, document_json, str(row["checksum"]))
        document = _assert_canonical(binding.session_id, document_json)
        if document != _binding_document(binding):
            raise RevisionChecksumError(binding.session_id)
        return binding

    def record_manual_pin(
        self,
        session_id: str,
        runtime_id: str,
        source: str,
        created_at: str,
    ) -> SessionRouteBinding:
        _assert_content_free(
            {
                "session_id": session_id,
                "runtime_id": runtime_id,
                "manual_pin_source": source,
            },
            writer="decision",
        )
        desired = SessionRouteBinding(
            session_id=session_id,
            binding_kind="manual",
            projection_mode="manual",
            decision_id=None,
            runtime_id=runtime_id,
            manual_pin_source=source,
            current_epoch=-1,
            continuation_root=None,
            parent_session_id=None,
            continuation_reason=None,
            created_at=created_at,
        )
        document_json = _canonical_json(_binding_document(desired))
        with self.write_txn() as connection:
            connection.execute(
                "INSERT INTO session_route_bindings "
                "(session_id, binding_kind, projection_mode, decision_id, runtime_id, "
                "manual_pin_source, current_epoch, continuation_root, "
                "parent_session_id, continuation_reason, created_at, "
                "document_json, checksum) "
                "VALUES (?, 'manual', 'manual', NULL, ?, ?, -1, NULL, NULL, NULL, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET binding_kind = 'manual', "
                "projection_mode = 'manual', decision_id = NULL, runtime_id = excluded.runtime_id, "
                "manual_pin_source = excluded.manual_pin_source, current_epoch = -1, "
                "continuation_root = NULL, parent_session_id = NULL, "
                "continuation_reason = NULL, created_at = excluded.created_at, "
                "document_json = excluded.document_json, checksum = excluded.checksum",
                (
                    session_id,
                    runtime_id,
                    source,
                    created_at,
                    document_json,
                    _checksum(document_json),
                ),
            )
            row = connection.execute(
                "SELECT * FROM session_route_bindings WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:  # pragma: no cover
                raise StoreError("manual route binding vanished after write")
            return self._session_binding_from_row(row)

    def bind_session_continuation(
        self,
        parent_session_id: str,
        child_session_id: str,
        *,
        reason: str,
        created_at: str,
    ) -> SessionRouteBinding:
        if parent_session_id == child_session_id:
            raise ValueError("a session cannot be a continuation of itself")
        if reason != "compression":
            raise ValueError("only compression continuation is supported")
        _assert_content_free(
            {
                "parent_session_id": parent_session_id,
                "session_id": child_session_id,
                "continuation_reason": reason,
            },
            writer="decision",
        )
        with self.write_txn() as connection:
            parent_row = connection.execute(
                "SELECT * FROM session_route_bindings WHERE session_id = ?",
                (parent_session_id,),
            ).fetchone()
            if parent_row is None:
                raise ImmutableRecordConflict(
                    "continuation parent has no route binding"
                )
            parent = self._session_binding_from_row(parent_row)
            root = parent.continuation_root or parent.session_id
            desired = SessionRouteBinding(
                session_id=child_session_id,
                binding_kind=parent.binding_kind,
                projection_mode=parent.projection_mode,
                decision_id=parent.decision_id,
                runtime_id=parent.runtime_id,
                manual_pin_source=parent.manual_pin_source,
                current_epoch=parent.current_epoch,
                continuation_root=root,
                parent_session_id=parent_session_id,
                continuation_reason=reason,
                created_at=created_at,
            )
            child_row = connection.execute(
                "SELECT * FROM session_route_bindings WHERE session_id = ?",
                (child_session_id,),
            ).fetchone()
            if child_row is not None:
                existing = self._session_binding_from_row(child_row)
                if self._continuation_binding_is_valid_descendant(
                    connection,
                    existing=existing,
                    desired=desired,
                ):
                    return existing
                raise ImmutableRecordConflict(
                    "continuation child already has different authoritative intent"
                )
            document_json = _canonical_json(_binding_document(desired))
            connection.execute(
                "INSERT INTO session_route_bindings "
                "(session_id, binding_kind, projection_mode, decision_id, runtime_id, "
                "manual_pin_source, current_epoch, continuation_root, parent_session_id, "
                "continuation_reason, created_at, document_json, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    desired.session_id,
                    desired.binding_kind,
                    desired.projection_mode,
                    desired.decision_id,
                    desired.runtime_id,
                    desired.manual_pin_source,
                    desired.current_epoch,
                    desired.continuation_root,
                    desired.parent_session_id,
                    desired.continuation_reason,
                    desired.created_at,
                    document_json,
                    _checksum(document_json),
                ),
            )
            self._insert_continuation_epoch_alias(
                connection,
                parent=parent,
                child=desired,
            )
            return desired

    def _continuation_binding_is_valid_descendant(
        self,
        connection: sqlite3.Connection,
        *,
        existing: SessionRouteBinding,
        desired: SessionRouteBinding,
    ) -> bool:
        stable_fields = (
            "session_id",
            "binding_kind",
            "projection_mode",
            "decision_id",
            "manual_pin_source",
            "continuation_root",
            "parent_session_id",
            "continuation_reason",
        )
        if any(
            getattr(existing, field_name) != getattr(desired, field_name)
            for field_name in stable_fields
        ):
            return False
        if existing.binding_kind == "manual":
            return (
                existing.runtime_id == desired.runtime_id
                and existing.current_epoch == -1
                and not connection.execute(
                    "SELECT 1 FROM route_epochs WHERE session_id = ? LIMIT 1",
                    (existing.session_id,),
                ).fetchone()
            )
        rows = connection.execute(
            "SELECT * FROM route_epochs WHERE session_id = ? ORDER BY epoch_number",
            (existing.session_id,),
        ).fetchall()
        if existing.current_epoch == -1:
            return existing.runtime_id == desired.runtime_id and not rows
        if not rows:
            return False
        epochs = tuple(self._route_epoch_from_row(row) for row in rows)
        first = epochs[0]
        if (
            first.reason_code != "compression_continuation"
            or any(
                epoch.session_id != existing.session_id
                or epoch.decision_id != existing.decision_id
                for epoch in epochs
            )
            or tuple(epoch.epoch_number for epoch in epochs)
            != tuple(range(first.epoch_number, existing.current_epoch + 1))
        ):
            return False
        current = epochs[-1]
        return (
            current.epoch_number == existing.current_epoch
            and current.runtime_id == existing.runtime_id
        )

    def _insert_continuation_epoch_alias(
        self,
        connection: sqlite3.Connection,
        *,
        parent: SessionRouteBinding,
        child: SessionRouteBinding,
    ) -> None:
        if child.binding_kind != "routed" or child.current_epoch < 0:
            return
        parent_row = connection.execute(
            "SELECT * FROM route_epochs WHERE session_id = ? AND epoch_number = ?",
            (parent.session_id, parent.current_epoch),
        ).fetchone()
        if parent_row is None:
            raise ImmutableRecordConflict(
                "routed continuation parent is missing its current route epoch"
            )
        parent_epoch = self._route_epoch_from_row(parent_row)
        if (
            parent_epoch.decision_id != child.decision_id
            or parent_epoch.runtime_id != child.runtime_id
        ):
            raise ImmutableRecordConflict(
                "routed continuation parent epoch conflicts with its binding"
            )
        alias = RouteEpoch(
            route_epoch_id=self._route_epoch_id(
                child.session_id,
                str(child.decision_id),
                child.current_epoch,
                child.runtime_id,
            ),
            session_id=child.session_id,
            decision_id=str(child.decision_id),
            epoch_number=child.current_epoch,
            runtime_id=child.runtime_id,
            reason_code="compression_continuation",
            started_at=child.created_at,
        )
        document_json = _canonical_json(_route_epoch_document(alias))
        connection.execute(
            "INSERT INTO route_epochs "
            "(route_epoch_id, session_id, decision_id, epoch_number, runtime_id, "
            "reason_code, started_at, ended_at, provider_started, api_request_id, "
            "provider_started_at, document_json, checksum) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, ?, ?)",
            (
                alias.route_epoch_id,
                alias.session_id,
                alias.decision_id,
                alias.epoch_number,
                alias.runtime_id,
                alias.reason_code,
                alias.started_at,
                document_json,
                _checksum(document_json),
            ),
        )

    def write_activation_receipt(
        self,
        receipt: ActivationReceipt,
    ) -> ActivationReceipt:
        document = _receipt_document(receipt)
        _assert_content_free(document, writer="decision")
        for field_name in (
            "config_sha",
            "inventory_contract_sha",
            "adapter_capability_sha",
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", str(document[field_name])):
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
        document_json = _canonical_json(document)
        checksum = _checksum(document_json)
        with self.write_txn() as connection:
            by_id = connection.execute(
                "SELECT * FROM activation_receipts WHERE receipt_id = ?",
                (receipt.receipt_id,),
            ).fetchone()
            by_key = connection.execute(
                "SELECT * FROM activation_receipts WHERE authority_id = ? "
                "AND config_sha = ? AND adapter_capability_sha = ? "
                "AND inventory_contract_sha = ? AND inventory_revision = ?",
                (
                    receipt.authority_id,
                    receipt.config_sha,
                    receipt.adapter_capability_sha,
                    receipt.inventory_contract_sha,
                    receipt.inventory_revision,
                ),
            ).fetchone()
            existing = by_id if by_id is not None else by_key
            if existing is not None:
                restored = self._activation_receipt_from_row(existing)
                if restored == receipt:
                    return restored
                raise ImmutableRecordConflict(
                    "activation receipt identity already has other content"
                )
            connection.execute(
                "INSERT INTO activation_receipts "
                "(receipt_id, authority_id, config_sha, inventory_contract_sha, "
                "inventory_revision, adapter_capability_sha, document_json, checksum, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.authority_id,
                    receipt.config_sha,
                    receipt.inventory_contract_sha,
                    receipt.inventory_revision,
                    receipt.adapter_capability_sha,
                    document_json,
                    checksum,
                    receipt.created_at,
                ),
            )
        return receipt

    def read_matching_activation_receipt(
        self,
        *,
        authority_id: str,
        config_sha: str,
        adapter_capability_sha: str,
        inventory_contract_sha: str | None = None,
        inventory_revision: str | None = None,
    ) -> ActivationReceipt | None:
        if (inventory_contract_sha is None) != (inventory_revision is None):
            raise ValueError(
                "inventory receipt fingerprint requires checksum and revision"
            )
        if inventory_contract_sha is not None:
            row = self.connection.execute(
                "SELECT * FROM activation_receipts WHERE authority_id = ? "
                "AND config_sha = ? AND adapter_capability_sha = ? "
                "AND inventory_contract_sha = ? AND inventory_revision = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (
                    authority_id,
                    config_sha,
                    adapter_capability_sha,
                    inventory_contract_sha,
                    inventory_revision,
                ),
            ).fetchone()
            return None if row is None else self._activation_receipt_from_row(row)
        row = self.connection.execute(
            "SELECT * FROM activation_receipts WHERE authority_id = ? "
            "AND config_sha = ? AND adapter_capability_sha = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (authority_id, config_sha, adapter_capability_sha),
        ).fetchone()
        return None if row is None else self._activation_receipt_from_row(row)

    def read_activation_receipt(
        self,
        receipt_id: str,
    ) -> ActivationReceipt | None:
        row = self.connection.execute(
            "SELECT * FROM activation_receipts WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        return None if row is None else self._activation_receipt_from_row(row)

    def rollback_activation_receipt(
        self,
        receipt: ActivationReceipt,
    ) -> None:
        """Remove only the exact unused receipt created by a failed config saga."""
        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM activation_receipts WHERE receipt_id = ?",
                (receipt.receipt_id,),
            ).fetchone()
            if row is None:
                return
            if self._activation_receipt_from_row(row) != receipt:
                raise ImmutableRecordConflict(
                    "refusing to roll back a different activation receipt"
                )
            if connection.execute(
                "SELECT 1 FROM routing_decisions WHERE activation_receipt_id=? LIMIT 1",
                (receipt.receipt_id,),
            ).fetchone() is not None:
                raise ImmutableRecordConflict(
                    "refusing to remove an activation receipt used by a decision"
                )
            connection.execute(
                "DELETE FROM activation_receipts WHERE receipt_id=?",
                (receipt.receipt_id,),
            )

    @staticmethod
    def _activation_receipt_from_row(row: sqlite3.Row) -> ActivationReceipt:
        identifier = str(row["receipt_id"])
        document_json = str(row["document_json"])
        _verify_checksum(identifier, document_json, str(row["checksum"]))
        document = _assert_canonical(identifier, document_json)
        receipt = ActivationReceipt(
            receipt_id=str(document["receipt_id"]),
            authority_id=str(document["authority_id"]),
            config_sha=str(document["config_sha"]),
            inventory_contract_sha=str(document["inventory_contract_sha"]),
            inventory_revision=str(document["inventory_revision"]),
            adapter_capability_sha=str(document["adapter_capability_sha"]),
            created_at=str(document["created_at"]),
        )
        expected_document = _receipt_document(receipt)
        if document != expected_document:
            raise RevisionChecksumError(identifier)
        _assert_content_free(expected_document, writer="decision")
        if any(
            str(row[field_name]) != str(value)
            for field_name, value in expected_document.items()
        ):
            raise RevisionChecksumError(identifier)
        return receipt

    def read_authority_revision(
        self,
        authority_id: str,
    ) -> AuthorityRevision | None:
        row = self.connection.execute(
            "SELECT * FROM authority_revisions WHERE authority_id = ?",
            (authority_id,),
        ).fetchone()
        return None if row is None else self._authority_from_row(row)

    @staticmethod
    def _authority_from_row(row: sqlite3.Row) -> AuthorityRevision:
        identifier = str(row["authority_id"])
        document_json = str(row["document_json"])
        checksum = str(row["checksum"])
        _verify_checksum(identifier, document_json, checksum)
        _assert_canonical(identifier, document_json)
        return AuthorityRevision(
            authority_id=identifier,
            document_json=document_json,
            checksum=checksum,
            created_at=str(row["created_at"]),
        )

    def write_inventory_snapshot(
        self,
        snapshot_id: str,
        observations: Sequence[RuntimeObservation],
        *,
        created_at: str | None = None,
    ) -> InventorySnapshot:
        """Atomically write one complete immutable inventory snapshot."""
        if not snapshot_id:
            raise ValueError("snapshot_id must not be empty")
        normalized: list[tuple[str, RuntimeObservation, str]] = []
        for value in observations:
            payload = (
                value.model_dump(mode="json", by_alias=True)
                if isinstance(value, BaseModel)
                else value
            )
            observation = RuntimeObservation.model_validate(payload)
            if observation.key.inventory_revision != snapshot_id:
                raise ValueError(
                    "inventory observation revision must equal its snapshot_id"
                )
            document_json = _canonical_json(observation)
            _assert_content_free(json.loads(document_json), writer="inventory")
            normalized.append((observation.key.stable_id(), observation, document_json))
        normalized.sort(key=lambda item: item[0])
        runtime_ids = [item[0] for item in normalized]
        if len(runtime_ids) != len(set(runtime_ids)):
            raise ValueError("inventory snapshot contains a duplicate runtime")

        snapshot_json = _canonical_json([json.loads(item[2]) for item in normalized])
        snapshot_checksum = _checksum(snapshot_json)
        timestamp = created_at or _timestamp()
        with self.write_txn() as connection:
            existing = connection.execute(
                "SELECT * FROM inventory_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if existing is not None:
                record = self._read_inventory_row(existing)
                if record is not None and record.document_json == snapshot_json:
                    return record
                raise ImmutableRecordConflict(
                    f"inventory snapshot {snapshot_id!r} already exists"
                )

            connection.execute(
                "INSERT INTO inventory_snapshots "
                "(snapshot_id, document_json, checksum, created_at, complete) "
                "VALUES (?, ?, ?, ?, 0)",
                (snapshot_id, snapshot_json, snapshot_checksum, timestamp),
            )
            for ordinal, (runtime_id, observation, document_json) in enumerate(
                normalized
            ):
                connection.execute(
                    "INSERT INTO inventory_observations "
                    "(snapshot_id, runtime_id, ordinal, provider, model, state, "
                    "document_json, checksum, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        runtime_id,
                        ordinal,
                        observation.key.provider,
                        observation.key.model,
                        observation.state,
                        document_json,
                        _checksum(document_json),
                        observation.observed_at,
                    ),
                )
            self._verify_inventory_children(
                connection,
                snapshot_id,
                expected_document_json=snapshot_json,
            )
            connection.execute(
                "UPDATE inventory_snapshots SET complete = 1 WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        record = self.read_inventory_snapshot(snapshot_id)
        if record is None:  # pragma: no cover - committed row invariant
            raise StoreError(f"inventory snapshot vanished after commit: {snapshot_id}")
        return record

    def read_inventory_snapshot(
        self,
        snapshot_id: str,
    ) -> InventorySnapshot | None:
        row = self.connection.execute(
            "SELECT * FROM inventory_snapshots WHERE snapshot_id = ? AND complete = 1",
            (snapshot_id,),
        ).fetchone()
        return None if row is None else self._read_inventory_row(row)

    def read_inventory(
        self,
        runtime: Any,
    ) -> RuntimeObservation | None:
        """Return the newest complete observation for one stable runtime ID."""
        if hasattr(runtime, "stable_id"):
            runtime_id = str(runtime.stable_id())
        else:
            runtime_id = str(runtime or "").strip()
        if not runtime_id:
            raise ValueError("runtime must provide a stable ID")
        row = self.connection.execute(
            "SELECT observation.snapshot_id "
            "FROM inventory_observations AS observation "
            "JOIN inventory_snapshots AS snapshot "
            "ON snapshot.snapshot_id = observation.snapshot_id "
            "WHERE observation.runtime_id = ? AND snapshot.complete = 1 "
            "ORDER BY snapshot.created_at DESC, snapshot.rowid DESC LIMIT 1",
            (runtime_id,),
        ).fetchone()
        if row is None:
            return None
        snapshot = self.read_inventory_snapshot(str(row["snapshot_id"]))
        if snapshot is None:
            return None
        return next(
            (
                observation
                for observation in snapshot.observations
                if observation.key.stable_id() == runtime_id
            ),
            None,
        )

    def _read_inventory_row(
        self,
        row: sqlite3.Row,
    ) -> InventorySnapshot | None:
        if not bool(row["complete"]):
            return None
        snapshot_id = str(row["snapshot_id"])
        document_json = str(row["document_json"])
        checksum = str(row["checksum"])
        _verify_checksum(snapshot_id, document_json, checksum)
        _assert_canonical(snapshot_id, document_json)
        observations = self._verify_inventory_children(
            self.connection,
            snapshot_id,
            expected_document_json=document_json,
        )
        return InventorySnapshot(
            snapshot_id=snapshot_id,
            observations=observations,
            document_json=document_json,
            checksum=checksum,
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _verify_inventory_children(
        connection: sqlite3.Connection,
        snapshot_id: str,
        *,
        expected_document_json: str,
    ) -> tuple[RuntimeObservation, ...]:
        observations: list[RuntimeObservation] = []
        documents: list[Any] = []
        rows = connection.execute(
            "SELECT * FROM inventory_observations "
            "WHERE snapshot_id = ? ORDER BY ordinal",
            (snapshot_id,),
        ).fetchall()
        for row in rows:
            document_json = str(row["document_json"])
            _verify_checksum(snapshot_id, document_json, str(row["checksum"]))
            decoded = _assert_canonical(snapshot_id, document_json)
            try:
                observation = RuntimeObservation.model_validate(decoded)
            except ValueError as error:
                raise RevisionChecksumError(snapshot_id) from error
            if (
                observation.key.stable_id() != row["runtime_id"]
                or observation.key.provider != row["provider"]
                or observation.key.model != row["model"]
                or observation.state != row["state"]
                or observation.observed_at != row["observed_at"]
                or observation.key.inventory_revision != snapshot_id
            ):
                raise RevisionChecksumError(snapshot_id)
            observations.append(observation)
            documents.append(decoded)
        if _canonical_json(documents) != expected_document_json:
            raise RevisionChecksumError(snapshot_id)
        return tuple(observations)

    def write_catalog_snapshot(
        self,
        snapshot_id: str,
        evidence: Sequence[CatalogEvidence | StoredCatalogRecord],
        *,
        created_at: str | None = None,
    ) -> CatalogSnapshot:
        """Atomically write one complete immutable catalog snapshot."""
        if not snapshot_id:
            raise ValueError("snapshot_id must not be empty")
        normalized: list[tuple[str, StoredCatalogRecord, str]] = []
        for value in evidence:
            payload = (
                value.model_dump(mode="json", by_alias=True)
                if isinstance(value, BaseModel)
                else value
            )
            if isinstance(value, StoredCatalogRecord) or (
                isinstance(payload, Mapping)
                and set(payload) == {"evidence", "applicability"}
            ):
                item = StoredCatalogRecord.model_validate(payload)
            else:
                evidence_item = CatalogEvidence.model_validate(payload)
                item = StoredCatalogRecord(
                    evidence=evidence_item,
                    applicability=CatalogApplicability(
                        canonical_model=evidence_item.model,
                        canonical_version=evidence_item.model_version,
                    ),
                )
            document_json = _canonical_json(item)
            _assert_content_free(json.loads(document_json), writer="catalog")
            evidence_id = _checksum(document_json)
            normalized.append((evidence_id, item, document_json))
        normalized.sort(key=lambda item: item[0])
        evidence_ids = [item[0] for item in normalized]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("catalog snapshot contains duplicate evidence")

        snapshot_json = _canonical_json([json.loads(item[2]) for item in normalized])
        snapshot_checksum = _checksum(snapshot_json)
        timestamp = created_at or _timestamp()
        with self.write_txn() as connection:
            existing = connection.execute(
                "SELECT * FROM catalog_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if existing is not None:
                record = self._read_catalog_row(existing)
                if record is not None and record.records == tuple(
                    item[1] for item in normalized
                ):
                    return record
                raise ImmutableRecordConflict(
                    f"catalog snapshot {snapshot_id!r} already exists"
                )

            connection.execute(
                "INSERT INTO catalog_snapshots "
                "(snapshot_id, document_json, checksum, created_at, complete) "
                "VALUES (?, ?, ?, ?, 0)",
                (snapshot_id, snapshot_json, snapshot_checksum, timestamp),
            )
            for ordinal, (evidence_id, item, document_json) in enumerate(normalized):
                connection.execute(
                    "INSERT INTO catalog_evidence "
                    "(snapshot_id, evidence_id, ordinal, source_id, model, "
                    "model_version, domain, metric_name, canonical_provider, "
                    "canonical_model, canonical_version, runtime_id, document_json, "
                    "checksum, retrieved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        evidence_id,
                        ordinal,
                        item.evidence.source_id,
                        item.evidence.model,
                        item.evidence.model_version,
                        item.evidence.domain,
                        item.evidence.metric_name,
                        item.applicability.canonical_provider,
                        item.applicability.canonical_model,
                        item.applicability.canonical_version,
                        item.applicability.runtime_id,
                        document_json,
                        _checksum(document_json),
                        item.evidence.retrieved_at,
                    ),
                )
            self._verify_catalog_children(
                connection,
                snapshot_id,
                expected_document_json=snapshot_json,
            )
            connection.execute(
                "UPDATE catalog_snapshots SET complete = 1 WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        record = self.read_catalog_snapshot(snapshot_id)
        if record is None:  # pragma: no cover - committed row invariant
            raise StoreError(f"catalog snapshot vanished after commit: {snapshot_id}")
        return record

    def read_catalog_snapshot(self, snapshot_id: str) -> CatalogSnapshot | None:
        row = self.connection.execute(
            "SELECT * FROM catalog_snapshots WHERE snapshot_id = ? AND complete = 1",
            (snapshot_id,),
        ).fetchone()
        return None if row is None else self._read_catalog_row(row)

    def _read_catalog_row(self, row: sqlite3.Row) -> CatalogSnapshot | None:
        if not bool(row["complete"]):
            return None
        snapshot_id = str(row["snapshot_id"])
        document_json = str(row["document_json"])
        checksum = str(row["checksum"])
        _verify_checksum(snapshot_id, document_json, checksum)
        _assert_canonical(snapshot_id, document_json)
        records = self._verify_catalog_children(
            self.connection,
            snapshot_id,
            expected_document_json=document_json,
        )
        return CatalogSnapshot(
            snapshot_id=snapshot_id,
            records=records,
            document_json=document_json,
            checksum=checksum,
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _verify_catalog_children(
        connection: sqlite3.Connection,
        snapshot_id: str,
        *,
        expected_document_json: str,
    ) -> tuple[StoredCatalogRecord, ...]:
        records: list[StoredCatalogRecord] = []
        documents: list[Any] = []
        rows = connection.execute(
            "SELECT * FROM catalog_evidence WHERE snapshot_id = ? ORDER BY ordinal",
            (snapshot_id,),
        ).fetchall()
        for row in rows:
            document_json = str(row["document_json"])
            _verify_checksum(snapshot_id, document_json, str(row["checksum"]))
            decoded = _assert_canonical(snapshot_id, document_json)
            try:
                if isinstance(decoded, Mapping) and set(decoded) == {
                    "evidence",
                    "applicability",
                }:
                    item = StoredCatalogRecord.model_validate(decoded)
                else:
                    evidence_item = CatalogEvidence.model_validate(decoded)
                    item = StoredCatalogRecord(
                        evidence=evidence_item,
                        applicability=CatalogApplicability(
                            canonical_provider=str(row["canonical_provider"]),
                            canonical_model=str(row["canonical_model"]),
                            canonical_version=str(row["canonical_version"]),
                            runtime_id=(
                                None
                                if row["runtime_id"] is None
                                else str(row["runtime_id"])
                            ),
                        ),
                    )
            except ValueError as error:
                raise RevisionChecksumError(snapshot_id) from error
            if (
                _checksum(document_json) != row["evidence_id"]
                or item.evidence.source_id != row["source_id"]
                or item.evidence.model != row["model"]
                or item.evidence.model_version != row["model_version"]
                or item.evidence.domain != row["domain"]
                or item.evidence.metric_name != row["metric_name"]
                or item.evidence.retrieved_at != row["retrieved_at"]
                or item.applicability.canonical_provider != row["canonical_provider"]
                or item.applicability.canonical_model != row["canonical_model"]
                or item.applicability.canonical_version != row["canonical_version"]
                or item.applicability.runtime_id != row["runtime_id"]
            ):
                raise RevisionChecksumError(snapshot_id)
            records.append(item)
            documents.append(decoded)
        if _canonical_json(documents) != expected_document_json:
            raise RevisionChecksumError(snapshot_id)
        return tuple(records)

    @staticmethod
    def _validated_adaptive_record(
        model_type: type[BaseModel],
        value: Any,
    ) -> tuple[BaseModel, str, str]:
        payload = (
            value.model_dump(mode="json", by_alias=True)
            if isinstance(value, BaseModel)
            else value
        )
        validated = model_type.model_validate(payload)
        _assert_content_free(validated, writer="adaptive")
        document_json = _canonical_json(validated)
        checksum = _checksum(document_json)
        _assert_canonical(model_type.__name__, document_json)
        _verify_checksum(model_type.__name__, document_json, checksum)
        return validated, document_json, checksum

    @staticmethod
    def _empty_profile_control(
        authority_id: str,
        profile_id: str,
    ) -> AdaptiveProfileControl:
        control, _document_json, _checksum_value = RoutingStore._validated_adaptive_record(
            AdaptiveProfileControl,
            {
                "authority_id": authority_id,
                "profile_id": profile_id,
                "active_revision_id": None,
                "control_revision_id": None,
                "challenger_revision_id": None,
                "experiment_phase": "eligible",
                "frozen": False,
                "cooldown_until": None,
                "rejection_count": 0,
                "generation": 0,
                "updated_at": _EMPTY_PROFILE_STATE_TIMESTAMP,
            },
        )
        return control  # type: ignore[return-value]

    @staticmethod
    def _profile_revision_from_row(row: sqlite3.Row) -> AdaptiveProfileRevision:
        identifier = str(row["revision_id"])
        overlay_json = str(row["overlay_json"])
        explanation_json = str(row["explanation_json"])
        overlay = _assert_canonical(identifier, overlay_json)
        explanation = _assert_canonical(identifier, explanation_json)
        document = {
            "revision_id": identifier,
            "authority_id": str(row["authority_id"]),
            "profile_id": str(row["profile_id"]),
            "parent_revision_id": (
                None
                if row["parent_revision_id"] is None
                else str(row["parent_revision_id"])
            ),
            "overlay": overlay,
            "explanation": explanation,
            "lifecycle": str(row["lifecycle"]),
            "created_at": str(row["created_at"]),
            "complete": bool(row["complete"]),
        }
        try:
            revision = AdaptiveProfileRevision.model_validate(document)
            _assert_content_free(revision, writer="adaptive")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        document_json = _canonical_json(revision)
        _verify_checksum(identifier, document_json, str(row["checksum"]))
        if (
            overlay_json != _canonical_json(revision.overlay)
            or explanation_json != _canonical_json(revision.explanation)
            or not revision.complete
        ):
            raise RevisionChecksumError(identifier)
        return revision

    @staticmethod
    def _profile_control_from_row(row: sqlite3.Row) -> AdaptiveProfileControl:
        identifier = f"{row['authority_id']}:{row['profile_id']}"
        document_json = str(row["document_json"])
        _verify_checksum(identifier, document_json, str(row["checksum"]))
        document = _assert_canonical(identifier, document_json)
        try:
            control = AdaptiveProfileControl.model_validate(document)
            _assert_content_free(control, writer="adaptive")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        expected = {
            "authority_id": control.authority_id,
            "profile_id": control.profile_id,
            "active_revision_id": control.active_revision_id,
            "control_revision_id": control.control_revision_id,
            "challenger_revision_id": control.challenger_revision_id,
            "experiment_phase": control.experiment_phase,
            "frozen": int(control.frozen),
            "cooldown_until": control.cooldown_until,
            "rejection_count": control.rejection_count,
            "generation": control.generation,
            "updated_at": control.updated_at,
        }
        for field_name, value in expected.items():
            stored = row[field_name]
            if isinstance(value, int) and not isinstance(value, bool):
                if int(stored) != value:
                    raise RevisionChecksumError(identifier)
            elif field_name == "frozen":
                if int(stored) != value:
                    raise RevisionChecksumError(identifier)
            elif value is None:
                if stored is not None:
                    raise RevisionChecksumError(identifier)
            elif str(stored) != str(value):
                raise RevisionChecksumError(identifier)
        return control

    @staticmethod
    def _lifecycle_event_from_row(row: sqlite3.Row) -> AdaptiveLifecycleEvent:
        identifier = str(row["event_id"])
        document_json = str(row["document_json"])
        _verify_checksum(identifier, document_json, str(row["checksum"]))
        document = _assert_canonical(identifier, document_json)
        try:
            event = AdaptiveLifecycleEvent.model_validate(document)
            _assert_content_free(event, writer="adaptive")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        expected = {
            "event_id": event.event_id,
            "authority_id": event.authority_id,
            "profile_id": event.profile_id,
            "revision_id": event.revision_id,
            "event_type": event.event_type,
            "reason_code": event.reason_code,
            "created_at": event.created_at,
        }
        for field_name, value in expected.items():
            stored = row[field_name]
            if value is None and stored is not None or value is not None and str(stored) != str(value):
                raise RevisionChecksumError(identifier)
        return event

    @staticmethod
    def _canary_assignment_from_row(row: sqlite3.Row) -> AdaptiveCanaryAssignment:
        identifier = str(row["assignment_id"])
        document_json = str(row["document_json"])
        _verify_checksum(identifier, document_json, str(row["checksum"]))
        document = _assert_canonical(identifier, document_json)
        try:
            assignment = AdaptiveCanaryAssignment.model_validate(document)
            _assert_content_free(assignment, writer="adaptive")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        expected = {
            "assignment_id": assignment.assignment_id,
            "authority_id": assignment.authority_id,
            "profile_id": assignment.profile_id,
            "operation_identity_hash": assignment.operation_identity_hash,
            "context_bucket_id": assignment.context_bucket_id,
            "control_revision_id": assignment.control_revision_id,
            "challenger_revision_id": assignment.challenger_revision_id,
            "arm": assignment.arm,
            "created_at": assignment.created_at,
        }
        if any(str(row[field]) != str(value) for field, value in expected.items()):
            raise RevisionChecksumError(identifier)
        return assignment

    @staticmethod
    def _optimizer_lease_from_row(row: sqlite3.Row) -> OptimizerLease:
        identifier = f"{row['authority_id']}:{row['profile_id']}"
        document_json = str(row["document_json"])
        try:
            _verify_checksum(identifier, document_json, str(row["checksum"]))
            document = _assert_canonical(identifier, document_json)
            lease = OptimizerLease.model_validate(document)
            _assert_content_free(lease, writer="adaptive")
        except Exception as error:
            raise RevisionChecksumError(identifier) from error
        expected = {
            "authority_id": lease.authority_id,
            "profile_id": lease.profile_id,
            "owner_id": lease.owner_id,
            "lease_expires_at": lease.lease_expires_at,
            "generation": lease.generation,
            "updated_at": lease.updated_at,
        }
        for field_name, value in expected.items():
            stored = row[field_name]
            if field_name == "generation":
                if int(stored) != value:
                    raise RevisionChecksumError(identifier)
            elif str(stored) != str(value):
                raise RevisionChecksumError(identifier)
        return lease

    @staticmethod
    def _control_row(
        connection: sqlite3.Connection,
        authority_id: str,
        profile_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM adaptive_profile_states "
            "WHERE authority_id=? AND profile_id=?",
            (authority_id, profile_id),
        ).fetchone()

    @classmethod
    def _control_in_txn(
        cls,
        connection: sqlite3.Connection,
        authority_id: str,
        profile_id: str,
    ) -> tuple[AdaptiveProfileControl, bool]:
        row = cls._control_row(connection, authority_id, profile_id)
        if row is None:
            return cls._empty_profile_control(authority_id, profile_id), False
        control = cls._profile_control_from_row(row)
        cls._validate_profile_control_links(connection, control)
        return control, True

    @classmethod
    def _validate_profile_control_links(
        cls,
        connection: sqlite3.Connection,
        control: AdaptiveProfileControl,
    ) -> None:
        identifier = f"{control.authority_id}:{control.profile_id}"
        pair = (control.control_revision_id, control.challenger_revision_id)
        if control.experiment_phase in {"eligible", "rolled_back"}:
            if pair != (None, None):
                raise RevisionChecksumError(identifier)
            if (
                control.experiment_phase == "rolled_back"
                and control.active_revision_id is None
            ):
                raise RevisionChecksumError(identifier)
        else:
            if None in pair:
                raise RevisionChecksumError(identifier)
            if control.active_revision_id not in pair:
                raise RevisionChecksumError(identifier)
            if control.experiment_phase in {"validated", "canary", "rejected"}:
                if control.active_revision_id != control.control_revision_id:
                    raise RevisionChecksumError(identifier)
            elif control.experiment_phase == "promoted":
                if control.active_revision_id != control.challenger_revision_id:
                    raise RevisionChecksumError(identifier)
        for revision_id in {
            control.active_revision_id,
            control.control_revision_id,
            control.challenger_revision_id,
        } - {None}:
            cls._require_linked_profile_revision(
                connection,
                revision_id,
                control.authority_id,
                control.profile_id,
            )

    @classmethod
    def _validate_profile_revision_parent_chain(
        cls,
        connection: sqlite3.Connection,
        revision: AdaptiveProfileRevision,
    ) -> None:
        """Verify every immutable parent remains complete and profile-local."""
        identifier = revision.revision_id
        seen = {identifier}
        parent_revision_id = revision.parent_revision_id
        while parent_revision_id is not None:
            if parent_revision_id in seen:
                raise RevisionChecksumError(identifier)
            seen.add(parent_revision_id)
            row = connection.execute(
                "SELECT * FROM adaptive_profile_revisions WHERE revision_id=?",
                (parent_revision_id,),
            ).fetchone()
            if row is None:
                raise RevisionChecksumError(identifier)
            parent = cls._profile_revision_from_row(row)
            if (
                not parent.complete
                or parent.authority_id != revision.authority_id
                or parent.profile_id != revision.profile_id
            ):
                raise RevisionChecksumError(identifier)
            parent_revision_id = parent.parent_revision_id

    @classmethod
    def _write_profile_control(
        cls,
        connection: sqlite3.Connection,
        control: AdaptiveProfileControl,
        *,
        existed: bool,
        expected_generation: int,
    ) -> None:
        validated, document_json, checksum = cls._validated_adaptive_record(
            AdaptiveProfileControl,
            control,
        )
        control = validated  # type: ignore[assignment]
        values = (
            control.active_revision_id,
            control.control_revision_id,
            control.challenger_revision_id,
            control.experiment_phase,
            int(control.frozen),
            control.cooldown_until,
            control.rejection_count,
            control.generation,
            control.updated_at,
            document_json,
            checksum,
        )
        if existed:
            cursor = connection.execute(
                "UPDATE adaptive_profile_states SET active_revision_id=?, "
                "control_revision_id=?, challenger_revision_id=?, experiment_phase=?, "
                "frozen=?, cooldown_until=?, rejection_count=?, generation=?, "
                "updated_at=?, document_json=?, checksum=? "
                "WHERE authority_id=? AND profile_id=? AND generation=?",
                (*values, control.authority_id, control.profile_id, expected_generation),
            )
            if cursor.rowcount != 1:
                raise ProfileStateConflict(expected_generation, control.generation)
        else:
            if expected_generation != 0:
                raise ProfileStateConflict(expected_generation, 0)
            connection.execute(
                "INSERT INTO adaptive_profile_states "
                "(authority_id, profile_id, active_revision_id, control_revision_id, "
                "challenger_revision_id, experiment_phase, frozen, cooldown_until, "
                "rejection_count, generation, updated_at, document_json, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (control.authority_id, control.profile_id, *values),
            )

    @classmethod
    def _require_linked_profile_revision(
        cls,
        connection: sqlite3.Connection,
        revision_id: str,
        authority_id: str,
        profile_id: str,
    ) -> AdaptiveProfileRevision:
        row = connection.execute(
            "SELECT * FROM adaptive_profile_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise ImmutableRecordConflict(
                f"adaptive profile revision {revision_id!r} does not exist"
            )
        revision = cls._profile_revision_from_row(row)
        if revision.authority_id != authority_id or revision.profile_id != profile_id:
            raise ImmutableRecordConflict(
                f"adaptive profile revision {revision_id!r} has a different authority/profile"
            )
        if not revision.complete:
            raise ImmutableRecordConflict(
                f"adaptive profile revision {revision_id!r} is incomplete"
            )
        cls._validate_profile_revision_parent_chain(connection, revision)
        return revision

    @classmethod
    def _append_lifecycle_event_txn(
        cls,
        connection: sqlite3.Connection,
        value: AdaptiveLifecycleEvent,
    ) -> AdaptiveLifecycleEvent:
        validated, document_json, checksum = cls._validated_adaptive_record(
            AdaptiveLifecycleEvent,
            value,
        )
        event = validated  # type: ignore[assignment]
        existing = connection.execute(
            "SELECT * FROM adaptive_lifecycle_events WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
        if existing is not None:
            stored = cls._lifecycle_event_from_row(existing)
            if stored.revision_id is not None:
                cls._require_linked_profile_revision(
                    connection,
                    stored.revision_id,
                    stored.authority_id,
                    stored.profile_id,
                )
            if stored == event:
                return stored
            raise ImmutableRecordConflict(
                f"adaptive lifecycle event {event.event_id!r} already has other content"
            )
        if event.revision_id is not None:
            cls._require_linked_profile_revision(
                connection,
                event.revision_id,
                event.authority_id,
                event.profile_id,
            )
        connection.execute(
            "INSERT INTO adaptive_lifecycle_events "
            "(event_id, authority_id, profile_id, revision_id, event_type, "
            "reason_code, document_json, checksum, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.authority_id,
                event.profile_id,
                event.revision_id,
                event.event_type,
                event.reason_code,
                document_json,
                checksum,
                event.created_at,
            ),
        )
        return event

    def read_profile_control(
        self,
        authority_id: str,
        profile_id: str,
    ) -> AdaptiveProfileControl:
        row = self._control_row(self.connection, authority_id, profile_id)
        if row is None:
            return self._empty_profile_control(authority_id, profile_id)
        control = self._profile_control_from_row(row)
        self._validate_profile_control_links(self.connection, control)
        return control

    def read_profile_revision(
        self,
        revision_id: str,
    ) -> AdaptiveProfileRevision | None:
        row = self.connection.execute(
            "SELECT * FROM adaptive_profile_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            return None
        revision = self._profile_revision_from_row(row)
        self._validate_profile_revision_parent_chain(self.connection, revision)
        return revision

    def read_active_profile_revision(
        self,
        authority_id: str,
        profile_id: str,
    ) -> tuple[AdaptiveProfileRevision | None, int]:
        control = self.read_profile_control(authority_id, profile_id)
        if control.active_revision_id is None:
            return None, control.generation
        revision = self.read_profile_revision(control.active_revision_id)
        if revision is None:
            raise RevisionChecksumError(control.active_revision_id)
        if revision.authority_id != authority_id or revision.profile_id != profile_id:
            raise RevisionChecksumError(control.active_revision_id)
        return revision, control.generation

    def read_active_profile_revision_snapshot(
        self,
        authority_id: str,
        profile_ids: Sequence[str],
    ) -> Mapping[str, tuple[AdaptiveProfileRevision | None, int]]:
        """Read every requested profile pointer in one SQLite read transaction."""
        ordered = tuple(sorted(profile_ids))
        if len(ordered) != len(set(ordered)):
            raise ValueError("adaptive snapshot profile IDs must be unique")
        snapshot: dict[str, tuple[AdaptiveProfileRevision | None, int]] = {}
        with self._evidence_read_txn():
            for profile_id in ordered:
                snapshot[profile_id] = self.read_active_profile_revision(
                    authority_id,
                    profile_id,
                )
        return MappingProxyType(snapshot)

    def list_profile_revisions(
        self,
        authority_id: str,
        profile_id: str,
    ) -> tuple[AdaptiveProfileRevision, ...]:
        rows = self.connection.execute(
            "SELECT * FROM adaptive_profile_revisions "
            "WHERE authority_id=? AND profile_id=? "
            "ORDER BY created_at, revision_id",
            (authority_id, profile_id),
        ).fetchall()
        revisions: list[AdaptiveProfileRevision] = []
        for row in rows:
            revision = self._profile_revision_from_row(row)
            self._validate_profile_revision_parent_chain(self.connection, revision)
            revisions.append(revision)
        return tuple(revisions)

    def publish_profile_revision(
        self,
        revision: AdaptiveProfileRevision,
        *,
        expected_revision_id: str | None,
        expected_generation: int,
    ) -> int:
        validated, document_json, checksum = self._validated_adaptive_record(
            AdaptiveProfileRevision,
            revision,
        )
        revision = validated  # type: ignore[assignment]
        overlay_json = _canonical_json(revision.overlay)
        explanation_json = _canonical_json(revision.explanation)
        with self.write_txn() as connection:
            control, existed = self._control_in_txn(
                connection, revision.authority_id, revision.profile_id
            )
            if control.active_revision_id != expected_revision_id:
                raise RevisionConflict(expected_revision_id, control.active_revision_id)
            if control.generation != expected_generation:
                raise ProfileStateConflict(expected_generation, control.generation)
            if control.frozen:
                raise ProfileFrozen(
                    f"adaptive profile {revision.profile_id!r} is frozen"
                )
            if control.experiment_phase != "eligible":
                raise InvalidLifecycleTransition(
                    "cannot publish an adaptive revision during an active experiment"
                )
            existing = connection.execute(
                "SELECT * FROM adaptive_profile_revisions WHERE revision_id=?",
                (revision.revision_id,),
            ).fetchone()
            if existing is not None:
                stored = self._profile_revision_from_row(existing)
                self._validate_profile_revision_parent_chain(connection, stored)
                if stored == revision and control.active_revision_id == revision.revision_id:
                    return control.generation
                raise ImmutableRecordConflict(
                    f"adaptive profile revision {revision.revision_id!r} already exists"
                )
            if revision.parent_revision_id is not None:
                self._require_linked_profile_revision(
                    connection,
                    revision.parent_revision_id,
                    revision.authority_id,
                    revision.profile_id,
                )
            connection.execute(
                "INSERT INTO adaptive_profile_revisions "
                "(revision_id, authority_id, profile_id, parent_revision_id, "
                "overlay_json, explanation_json, lifecycle, checksum, created_at, complete) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    revision.revision_id,
                    revision.authority_id,
                    revision.profile_id,
                    revision.parent_revision_id,
                    overlay_json,
                    explanation_json,
                    revision.lifecycle,
                    checksum,
                    revision.created_at,
                ),
            )
            next_control = control.model_copy(update={
                "active_revision_id": revision.revision_id,
                "generation": control.generation + 1,
                "updated_at": revision.created_at,
            })
            self._write_profile_control(
                connection,
                next_control,
                existed=existed,
                expected_generation=control.generation,
            )
            stored = self._require_linked_profile_revision(
                connection,
                revision.revision_id,
                revision.authority_id,
                revision.profile_id,
            )
            if stored != revision or document_json != _canonical_json(stored):
                raise RevisionChecksumError(revision.revision_id)
            return next_control.generation

    def insert_inactive_profile_revision(
        self,
        revision: AdaptiveProfileRevision,
        *,
        expected_active_revision_id: str,
        expected_generation: int,
    ) -> int:
        """Insert a complete challenger without changing the active pointer."""
        validated, document_json, checksum = self._validated_adaptive_record(
            AdaptiveProfileRevision,
            revision,
        )
        revision = validated  # type: ignore[assignment]
        overlay_json = _canonical_json(revision.overlay)
        explanation_json = _canonical_json(revision.explanation)
        with self.write_txn() as connection:
            control, _existed = self._control_in_txn(
                connection,
                revision.authority_id,
                revision.profile_id,
            )
            if control.active_revision_id != expected_active_revision_id:
                raise RevisionConflict(
                    expected_active_revision_id,
                    control.active_revision_id,
                )
            if control.generation != expected_generation:
                raise ProfileStateConflict(expected_generation, control.generation)
            if control.frozen:
                raise ProfileFrozen(
                    f"adaptive profile {revision.profile_id!r} is frozen"
                )
            if control.experiment_phase != "eligible":
                raise InvalidLifecycleTransition(
                    "cannot insert a challenger during an active experiment"
                )
            if revision.parent_revision_id != expected_active_revision_id:
                raise ImmutableRecordConflict(
                    "inactive challenger must name the exact active control parent"
                )
            self._require_linked_profile_revision(
                connection,
                expected_active_revision_id,
                revision.authority_id,
                revision.profile_id,
            )
            existing = connection.execute(
                "SELECT * FROM adaptive_profile_revisions WHERE revision_id=?",
                (revision.revision_id,),
            ).fetchone()
            if existing is not None:
                stored = self._profile_revision_from_row(existing)
                self._validate_profile_revision_parent_chain(connection, stored)
                if stored == revision:
                    return control.generation
                raise ImmutableRecordConflict(
                    f"adaptive profile revision {revision.revision_id!r} already exists"
                )
            connection.execute(
                "INSERT INTO adaptive_profile_revisions "
                "(revision_id, authority_id, profile_id, parent_revision_id, "
                "overlay_json, explanation_json, lifecycle, checksum, created_at, complete) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    revision.revision_id,
                    revision.authority_id,
                    revision.profile_id,
                    revision.parent_revision_id,
                    overlay_json,
                    explanation_json,
                    revision.lifecycle,
                    checksum,
                    revision.created_at,
                ),
            )
            stored = self._require_linked_profile_revision(
                connection,
                revision.revision_id,
                revision.authority_id,
                revision.profile_id,
            )
            if stored != revision or document_json != _canonical_json(stored):
                raise RevisionChecksumError(revision.revision_id)
            return control.generation

    def list_adaptive_lifecycle_events(
        self,
        authority_id: str,
        profile_id: str,
    ) -> tuple[AdaptiveLifecycleEvent, ...]:
        rows = self.connection.execute(
            "SELECT * FROM adaptive_lifecycle_events "
            "WHERE authority_id=? AND profile_id=? ORDER BY created_at, event_id",
            (authority_id, profile_id),
        ).fetchall()
        events: list[AdaptiveLifecycleEvent] = []
        for row in rows:
            event = self._lifecycle_event_from_row(row)
            if event.revision_id is not None:
                self._require_linked_profile_revision(
                    self.connection,
                    event.revision_id,
                    event.authority_id,
                    event.profile_id,
                )
            events.append(event)
        return tuple(events)

    def transition_profile_experiment(
        self,
        authority_id: str,
        profile_id: str,
        *,
        active_revision_id: str | None,
        control_revision_id: str | None,
        challenger_revision_id: str | None,
        experiment_phase: AdaptiveLifecyclePhase,
        cooldown_until: str | None,
        rejection_count: int,
        expected_generation: int,
        event: AdaptiveLifecycleEvent,
    ) -> AdaptiveProfileControl:
        event_value, _event_json, _event_checksum = self._validated_adaptive_record(
            AdaptiveLifecycleEvent,
            event,
        )
        event = event_value  # type: ignore[assignment]
        with self.write_txn() as connection:
            current, existed = self._control_in_txn(
                connection, authority_id, profile_id
            )
            if current.generation != expected_generation:
                raise ProfileStateConflict(expected_generation, current.generation)
            if current.frozen:
                raise ProfileFrozen(f"adaptive profile {profile_id!r} is frozen")
            if experiment_phase not in _LEGAL_EXPERIMENT_TRANSITIONS.get(
                current.experiment_phase, frozenset()
            ):
                raise InvalidLifecycleTransition(
                    f"cannot transition adaptive profile from "
                    f"{current.experiment_phase!r} to {experiment_phase!r}"
                )
            if (
                event.authority_id != authority_id
                or event.profile_id != profile_id
                or event.event_type != experiment_phase
            ):
                raise ImmutableRecordConflict(
                    "lifecycle event type and authority/profile must match transition"
                )
            if experiment_phase == "eligible":
                if control_revision_id is not None or challenger_revision_id is not None:
                    raise ImmutableRecordConflict(
                        "eligible adaptive state must clear experiment revision linkage"
                    )
            elif control_revision_id is None or challenger_revision_id is None:
                raise ImmutableRecordConflict(
                    "active adaptive experiment requires exact control/challenger linkage"
                )
            if current.experiment_phase != "eligible" and experiment_phase != "eligible":
                if (
                    control_revision_id != current.control_revision_id
                    or challenger_revision_id != current.challenger_revision_id
                ):
                    raise ImmutableRecordConflict(
                        "adaptive lifecycle transitions must preserve the exact "
                        "control/challenger pair"
                    )
            if (
                control_revision_id is not None
                and challenger_revision_id is not None
                and active_revision_id
                not in {control_revision_id, challenger_revision_id}
            ):
                raise ImmutableRecordConflict(
                    "active adaptive revision must be the exact control or challenger"
                )
            if experiment_phase in {"validated", "canary", "rejected"} and (
                active_revision_id != control_revision_id
            ):
                raise ImmutableRecordConflict(
                    f"{experiment_phase} adaptive state must keep control active"
                )
            if experiment_phase == "promoted" and (
                active_revision_id != challenger_revision_id
            ):
                raise ImmutableRecordConflict(
                    "promoted adaptive state must make challenger active"
                )
            for revision_id in {
                active_revision_id,
                control_revision_id,
                challenger_revision_id,
            } - {None}:
                self._require_linked_profile_revision(
                    connection, revision_id, authority_id, profile_id
                )
            if event.revision_id is not None:
                self._require_linked_profile_revision(
                    connection, event.revision_id, authority_id, profile_id
                )
            expected_event_revision = (
                active_revision_id
                if experiment_phase in {"eligible", "cooldown"}
                else challenger_revision_id
            )
            if event.revision_id != expected_event_revision:
                raise ImmutableRecordConflict(
                    "lifecycle event revision must match the phase-relevant revision"
                )
            next_value, _document_json, _checksum_value = self._validated_adaptive_record(
                AdaptiveProfileControl,
                {
                    "authority_id": authority_id,
                    "profile_id": profile_id,
                    "active_revision_id": active_revision_id,
                    "control_revision_id": control_revision_id,
                    "challenger_revision_id": challenger_revision_id,
                    "experiment_phase": experiment_phase,
                    "frozen": current.frozen,
                    "cooldown_until": cooldown_until,
                    "rejection_count": rejection_count,
                    "generation": current.generation + 1,
                    "updated_at": event.created_at,
                },
            )
            next_control = next_value  # type: ignore[assignment]
            self._validate_profile_control_links(connection, next_control)
            self._append_lifecycle_event_txn(connection, event)
            self._write_profile_control(
                connection,
                next_control,
                existed=existed,
                expected_generation=current.generation,
            )
            return next_control

    def get_or_create_canary_assignment(
        self,
        assignment: AdaptiveCanaryAssignment,
    ) -> AdaptiveCanaryAssignment:
        validated, document_json, checksum = self._validated_adaptive_record(
            AdaptiveCanaryAssignment,
            assignment,
        )
        assignment = validated  # type: ignore[assignment]
        with self.write_txn() as connection:
            control, _existed = self._control_in_txn(
                connection, assignment.authority_id, assignment.profile_id
            )
            if control.frozen:
                raise ProfileFrozen(
                    f"adaptive profile {assignment.profile_id!r} is frozen"
                )
            if (
                control.experiment_phase != "canary"
                or control.control_revision_id != assignment.control_revision_id
                or control.challenger_revision_id
                != assignment.challenger_revision_id
                or control.active_revision_id != assignment.control_revision_id
            ):
                raise InvalidLifecycleTransition(
                    "canary assignment does not match the active experiment"
                )
            self._require_linked_profile_revision(
                connection,
                assignment.control_revision_id,
                assignment.authority_id,
                assignment.profile_id,
            )
            challenger_revision = self._require_linked_profile_revision(
                connection,
                assignment.challenger_revision_id,
                assignment.authority_id,
                assignment.profile_id,
            )
            explanation = challenger_revision.explanation
            if (
                challenger_revision.parent_revision_id
                != assignment.control_revision_id
                or explanation.control_revision_id
                != assignment.control_revision_id
                or explanation.context_bucket_id != assignment.context_bucket_id
            ):
                raise InvalidLifecycleTransition(
                    "canary assignment context does not match its challenger"
                )
            existing = connection.execute(
                "SELECT * FROM adaptive_canary_assignments "
                "WHERE authority_id=? AND profile_id=? AND operation_identity_hash=?",
                (
                    assignment.authority_id,
                    assignment.profile_id,
                    assignment.operation_identity_hash,
                ),
            ).fetchone()
            if existing is not None:
                stored = self._canary_assignment_from_row(existing)
                self._require_linked_profile_revision(
                    connection,
                    stored.control_revision_id,
                    stored.authority_id,
                    stored.profile_id,
                )
                self._require_linked_profile_revision(
                    connection,
                    stored.challenger_revision_id,
                    stored.authority_id,
                    stored.profile_id,
                )
                ignored = {"assignment_id", "created_at"}
                if stored.model_dump(exclude=ignored) == assignment.model_dump(
                    exclude=ignored
                ):
                    return stored
                raise ImmutableRecordConflict(
                    "adaptive canary operation already has a different assignment"
                )
            connection.execute(
                "INSERT INTO adaptive_canary_assignments "
                "(assignment_id, authority_id, profile_id, operation_identity_hash, "
                "context_bucket_id, control_revision_id, challenger_revision_id, arm, "
                "document_json, checksum, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    assignment.assignment_id,
                    assignment.authority_id,
                    assignment.profile_id,
                    assignment.operation_identity_hash,
                    assignment.context_bucket_id,
                    assignment.control_revision_id,
                    assignment.challenger_revision_id,
                    assignment.arm,
                    document_json,
                    checksum,
                    assignment.created_at,
                ),
            )
            return assignment

    def read_canary_assignment(
        self,
        authority_id: str,
        profile_id: str,
        operation_identity_hash: str,
    ) -> AdaptiveCanaryAssignment | None:
        row = self.connection.execute(
            "SELECT * FROM adaptive_canary_assignments "
            "WHERE authority_id=? AND profile_id=? AND operation_identity_hash=?",
            (authority_id, profile_id, operation_identity_hash),
        ).fetchone()
        if row is None:
            return None
        assignment = self._canary_assignment_from_row(row)
        self._require_linked_profile_revision(
            self.connection,
            assignment.control_revision_id,
            assignment.authority_id,
            assignment.profile_id,
        )
        self._require_linked_profile_revision(
            self.connection,
            assignment.challenger_revision_id,
            assignment.authority_id,
            assignment.profile_id,
        )
        return assignment

    def read_canary_assignment_by_id(
        self,
        assignment_id: str,
    ) -> AdaptiveCanaryAssignment | None:
        """Read one immutable assignment by its durable decision attestation."""
        row = self.connection.execute(
            "SELECT * FROM adaptive_canary_assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        if row is None:
            return None
        assignment = self._canary_assignment_from_row(row)
        self._require_linked_profile_revision(
            self.connection,
            assignment.control_revision_id,
            assignment.authority_id,
            assignment.profile_id,
        )
        self._require_linked_profile_revision(
            self.connection,
            assignment.challenger_revision_id,
            assignment.authority_id,
            assignment.profile_id,
        )
        return assignment

    def count_canary_assignments(self) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM adaptive_canary_assignments"
            ).fetchone()[0]
        )

    def acquire_optimizer_lease(
        self,
        authority_id: str,
        profile_id: str,
        owner_id: str,
        now: datetime | date | float | int,
        lease_seconds: float,
    ) -> OptimizerLease | None:
        if (
            not isinstance(lease_seconds, (int, float))
            or isinstance(lease_seconds, bool)
            or not math.isfinite(float(lease_seconds))
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be finite and positive")
        now_text = _timestamp(now)
        now_moment = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
        expires_at = _timestamp(now_moment + timedelta(seconds=float(lease_seconds)))
        with self.write_txn() as connection:
            control, _control_exists = self._control_in_txn(
                connection, authority_id, profile_id
            )
            if control.frozen:
                return None
            row = connection.execute(
                "SELECT * FROM adaptive_optimizer_leases "
                "WHERE authority_id=? AND profile_id=?",
                (authority_id, profile_id),
            ).fetchone()
            generation = 1
            if row is not None:
                current = self._optimizer_lease_from_row(row)
                current_expiry = datetime.fromisoformat(
                    current.lease_expires_at.replace("Z", "+00:00")
                )
                if current.owner_id != owner_id and current_expiry > now_moment:
                    return None
                generation = current.generation + 1
            value, document_json, checksum = self._validated_adaptive_record(
                OptimizerLease,
                {
                    "authority_id": authority_id,
                    "profile_id": profile_id,
                    "owner_id": owner_id,
                    "lease_expires_at": expires_at,
                    "generation": generation,
                    "updated_at": now_text,
                },
            )
            lease = value  # type: ignore[assignment]
            connection.execute(
                "INSERT INTO adaptive_optimizer_leases "
                "(authority_id, profile_id, owner_id, lease_expires_at, generation, "
                "updated_at, document_json, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(authority_id, profile_id) "
                "DO UPDATE SET owner_id=excluded.owner_id, "
                "lease_expires_at=excluded.lease_expires_at, "
                "generation=excluded.generation, updated_at=excluded.updated_at, "
                "document_json=excluded.document_json, checksum=excluded.checksum",
                (
                    lease.authority_id,
                    lease.profile_id,
                    lease.owner_id,
                    lease.lease_expires_at,
                    lease.generation,
                    lease.updated_at,
                    document_json,
                    checksum,
                ),
            )
            return lease

    def release_optimizer_lease(self, lease: OptimizerLease) -> bool:
        validated, _document_json, _checksum_value = self._validated_adaptive_record(
            OptimizerLease,
            lease,
        )
        lease = validated  # type: ignore[assignment]
        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM adaptive_optimizer_leases WHERE authority_id=? "
                "AND profile_id=?",
                (lease.authority_id, lease.profile_id),
            ).fetchone()
            if row is None:
                return False
            stored = self._optimizer_lease_from_row(row)
            if (
                stored.owner_id != lease.owner_id
                or stored.generation != lease.generation
            ):
                return False
            cursor = connection.execute(
                "DELETE FROM adaptive_optimizer_leases WHERE authority_id=? "
                "AND profile_id=? AND owner_id=? AND generation=?",
                (
                    lease.authority_id,
                    lease.profile_id,
                    lease.owner_id,
                    lease.generation,
                ),
            )
            return cursor.rowcount == 1

    def set_profile_freeze(
        self,
        authority_id: str,
        profile_id: str,
        *,
        frozen: bool,
        expected_generation: int,
    ) -> AdaptiveProfileControl:
        if not isinstance(frozen, bool):
            raise ValueError("frozen must be a boolean")
        with self.write_txn() as connection:
            current, existed = self._control_in_txn(
                connection, authority_id, profile_id
            )
            if current.generation != expected_generation:
                raise ProfileStateConflict(expected_generation, current.generation)
            if current.frozen == frozen:
                return current
            timestamp = _timestamp()
            value, _document_json, _checksum_value = self._validated_adaptive_record(
                AdaptiveProfileControl,
                {
                    **current.model_dump(mode="json"),
                    "frozen": frozen,
                    "generation": current.generation + 1,
                    "updated_at": timestamp,
                },
            )
            updated = value  # type: ignore[assignment]
            event = AdaptiveLifecycleEvent(
                event_id=uuid.uuid4().hex,
                authority_id=authority_id,
                profile_id=profile_id,
                revision_id=current.active_revision_id,
                event_type="frozen" if frozen else "unfrozen",
                reason_code="operator_control",
                explanation={},
                created_at=timestamp,
            )
            self._append_lifecycle_event_txn(connection, event)
            self._write_profile_control(
                connection,
                updated,
                existed=existed,
                expected_generation=current.generation,
            )
            return updated

    def rollback_profile_revision(
        self,
        *,
        authority_id: str,
        profile_id: str,
        revision_id: str,
        expected_target_checksum: str,
        expected_generation: int,
    ) -> AdaptiveProfileRevision:
        with self.write_txn() as connection:
            current, existed = self._control_in_txn(
                connection, authority_id, profile_id
            )
            if current.generation != expected_generation:
                raise ProfileStateConflict(expected_generation, current.generation)
            if not current.frozen:
                raise ProfileFrozen(
                    f"adaptive profile {profile_id!r} must be frozen for rollback"
                )
            revision = self._require_linked_profile_revision(
                connection, revision_id, authority_id, profile_id
            )
            actual_target_checksum = _checksum(_canonical_json(revision))
            if (
                not isinstance(expected_target_checksum, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_target_checksum) is None
                or expected_target_checksum != actual_target_checksum
            ):
                raise RevisionChecksumError(revision_id)
            timestamp = _timestamp()
            value, _document_json, _checksum_value = self._validated_adaptive_record(
                AdaptiveProfileControl,
                {
                    **current.model_dump(mode="json"),
                    "active_revision_id": revision_id,
                    "control_revision_id": None,
                    "challenger_revision_id": None,
                    "experiment_phase": "rolled_back",
                    "cooldown_until": None,
                    "generation": current.generation + 1,
                    "updated_at": timestamp,
                },
            )
            updated = value  # type: ignore[assignment]
            self._validate_profile_control_links(connection, updated)
            event = AdaptiveLifecycleEvent(
                event_id=uuid.uuid4().hex,
                authority_id=authority_id,
                profile_id=profile_id,
                revision_id=revision_id,
                event_type="rolled_back",
                reason_code="operator_rollback",
                explanation={},
                created_at=timestamp,
            )
            self._append_lifecycle_event_txn(connection, event)
            self._write_profile_control(
                connection,
                updated,
                existed=existed,
                expected_generation=current.generation,
            )
            return revision

    def build_baseline_revision(
        self,
        *,
        authority_id: str,
        overlay: Mapping[str, Any],
        created_at: str | None = None,
    ) -> AdaptiveRevision:
        """Build, but do not publish, one complete baseline overlay."""
        if not authority_id:
            raise ValueError("authority_id must not be empty")
        _assert_content_free({"overlay": overlay}, writer="adaptive")
        return AdaptiveRevision(
            revision_id=uuid.uuid4().hex,
            authority_id=authority_id,
            parent_revision_id=None,
            overlay=dict(overlay),
            explanation={"kind": "baseline"},
            created_at=created_at or _timestamp(),
            is_baseline=True,
        )

    def publish_revision(
        self,
        revision: AdaptiveRevision,
        expected_active_id: str | None,
    ) -> None:
        """Publish a complete immutable revision and CAS its active pointer."""
        payload = (
            revision.model_dump(mode="json", by_alias=True)
            if isinstance(revision, BaseModel)
            else revision
        )
        validated = AdaptiveRevision.model_validate(payload)
        _assert_content_free(validated, writer="adaptive")
        document_json = _canonical_json(validated)
        explanation_json = _canonical_json(validated.explanation)
        checksum = _checksum(document_json)
        with self.write_txn() as connection:
            current = self._active_id(connection, validated.authority_id)
            if current != expected_active_id:
                raise RevisionConflict(expected_active_id, current)
            if (
                connection.execute(
                    "SELECT 1 FROM adaptive_revisions WHERE revision_id = ?",
                    (validated.revision_id,),
                ).fetchone()
                is not None
            ):
                raise ImmutableRecordConflict(
                    f"adaptive revision {validated.revision_id!r} already exists"
                )
            connection.execute(
                "INSERT INTO adaptive_revisions "
                "(revision_id, authority_id, parent_revision_id, document_json, "
                "checksum, explanation_json, created_at, complete) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    validated.revision_id,
                    validated.authority_id,
                    validated.parent_revision_id,
                    document_json,
                    checksum,
                    explanation_json,
                    validated.created_at,
                ),
            )
            stored = connection.execute(
                "SELECT document_json, checksum FROM adaptive_revisions "
                "WHERE revision_id = ?",
                (validated.revision_id,),
            ).fetchone()
            if stored is None:
                raise RevisionChecksumError(validated.revision_id)
            _verify_checksum(
                validated.revision_id,
                str(stored["document_json"]),
                str(stored["checksum"]),
            )
            connection.execute(
                "UPDATE adaptive_revisions SET complete = 1 WHERE revision_id = ?",
                (validated.revision_id,),
            )
            connection.execute(
                "INSERT INTO active_adaptive_revisions"
                "(authority_id, revision_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(authority_id) DO UPDATE SET "
                "revision_id=excluded.revision_id, updated_at=excluded.updated_at",
                (
                    validated.authority_id,
                    validated.revision_id,
                    validated.created_at,
                ),
            )

    @staticmethod
    def _active_id(
        connection: sqlite3.Connection,
        authority_id: str,
    ) -> str | None:
        row = connection.execute(
            "SELECT revision_id FROM active_adaptive_revisions WHERE authority_id = ?",
            (authority_id,),
        ).fetchone()
        return None if row is None else str(row["revision_id"])

    def read_revision(self, revision_id: str) -> AdaptiveRevision | None:
        row = self.connection.execute(
            "SELECT * FROM adaptive_revisions WHERE revision_id = ? AND complete = 1",
            (revision_id,),
        ).fetchone()
        return None if row is None else self._adaptive_from_row(row)

    def read_active_revision(
        self,
        authority_id: str,
    ) -> AdaptiveRevision | None:
        row = self.connection.execute(
            "SELECT revision.* FROM active_adaptive_revisions AS active "
            "JOIN adaptive_revisions AS revision "
            "ON revision.revision_id = active.revision_id "
            "WHERE active.authority_id = ? AND revision.authority_id = ? "
            "AND revision.complete = 1",
            (authority_id, authority_id),
        ).fetchone()
        return None if row is None else self._adaptive_from_row(row)

    def read_latest_complete_revision(
        self,
        authority_id: str,
    ) -> AdaptiveRevision | None:
        """Return the newest verified complete revision without using a pointer."""
        row = self.connection.execute(
            "SELECT * FROM adaptive_revisions "
            "WHERE authority_id = ? AND complete = 1 "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (authority_id,),
        ).fetchone()
        return None if row is None else self._adaptive_from_row(row)

    @staticmethod
    def _adaptive_from_row(row: sqlite3.Row) -> AdaptiveRevision:
        revision_id = str(row["revision_id"])
        document_json = str(row["document_json"])
        _verify_checksum(revision_id, document_json, str(row["checksum"]))
        decoded = _assert_canonical(revision_id, document_json)
        try:
            revision = AdaptiveRevision.model_validate(decoded)
        except ValueError as error:
            raise RevisionChecksumError(revision_id) from error
        explanation_json = str(row["explanation_json"])
        try:
            canonical_explanation = _canonical_json(json.loads(explanation_json))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RevisionChecksumError(revision_id) from error
        if (
            canonical_explanation != explanation_json
            or canonical_explanation != _canonical_json(revision.explanation)
            or revision.revision_id != revision_id
            or revision.authority_id != row["authority_id"]
            or revision.parent_revision_id != row["parent_revision_id"]
            or revision.created_at != row["created_at"]
        ):
            raise RevisionChecksumError(revision_id)
        return revision

    def budget_ledger_revision(
        self,
        bucket: str,
        budget_day: datetime | date | float | int | str,
    ) -> str:
        """Hash the complete content-free ledger state for one bucket/day."""
        normalized_day = _day(budget_day).isoformat()
        return self._budget_ledger_revision(
            self.connection,
            bucket=bucket,
            normalized_day=normalized_day,
        )

    @staticmethod
    def _budget_ledger_revision(
        connection: sqlite3.Connection,
        *,
        bucket: str,
        normalized_day: str,
    ) -> str:
        rows = connection.execute(
            "SELECT reservation_id, reserved_usd, daily_limit_usd, actual_usd, "
            "status, created_at, reconciled_at FROM budget_ledger "
            "WHERE bucket = ? AND budget_day = ? ORDER BY reservation_id",
            (bucket, normalized_day),
        ).fetchall()
        payload = [
            {
                "reservation_id": str(row["reservation_id"]),
                "reserved_usd": float(row["reserved_usd"]),
                "daily_limit_usd": float(row["daily_limit_usd"]),
                "actual_usd": (
                    None if row["actual_usd"] is None else float(row["actual_usd"])
                ),
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
                "reconciled_at": (
                    None if row["reconciled_at"] is None else str(row["reconciled_at"])
                ),
            }
            for row in rows
        ]
        return _checksum(_canonical_json(payload))

    def verification_attempt_sequence(self, runtime_id: str) -> int:
        """Return the monotonic prior-attempt count for preview hashing."""
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM runtime_verification_attempts "
                "WHERE runtime_id = ?",
                (runtime_id,),
            ).fetchone()[0]
        )

    def write_verification_preview(
        self,
        *,
        precondition_hash: str,
        document: Mapping[str, Any],
        expires_at: str,
        created_at: str | None = None,
    ) -> StoredVerificationPreview:
        """Persist an immutable content-free preview for cross-process apply."""
        if not precondition_hash or not expires_at:
            raise ValueError("verification preview identifiers must not be empty")
        _assert_content_free(document, writer="verification")
        document_json = _canonical_json(document)
        checksum = _checksum(document_json)
        if precondition_hash != _checksum(document_json):
            raise ValueError("verification preview hash must bind its full document")
        timestamp = created_at or _timestamp()
        with self.write_txn() as connection:
            existing = connection.execute(
                "SELECT * FROM runtime_verification_previews "
                "WHERE precondition_hash = ?",
                (precondition_hash,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO runtime_verification_previews "
                    "(precondition_hash, document_json, checksum, expires_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        precondition_hash,
                        document_json,
                        checksum,
                        expires_at,
                        timestamp,
                    ),
                )
            elif (
                str(existing["document_json"]) != document_json
                or str(existing["checksum"]) != checksum
                or str(existing["expires_at"]) != expires_at
            ):
                raise ImmutableRecordConflict(
                    "verification preview hash already has other content"
                )
        record = self.read_verification_preview(precondition_hash)
        if record is None:  # pragma: no cover - committed row invariant
            raise StoreError("verification preview vanished after commit")
        return record

    def read_verification_preview(
        self,
        precondition_hash: str,
    ) -> StoredVerificationPreview | None:
        row = self.connection.execute(
            "SELECT * FROM runtime_verification_previews WHERE precondition_hash = ?",
            (precondition_hash,),
        ).fetchone()
        if row is None:
            return None
        document_json = str(row["document_json"])
        checksum = str(row["checksum"])
        _verify_checksum(precondition_hash, document_json, checksum)
        decoded = _assert_canonical(precondition_hash, document_json)
        if precondition_hash != _checksum(document_json):
            raise RevisionChecksumError(precondition_hash)
        if decoded.get("expires_at") != row["expires_at"]:
            raise RevisionChecksumError(precondition_hash)
        return StoredVerificationPreview(
            precondition_hash=precondition_hash,
            document_json=document_json,
            checksum=checksum,
            expires_at=str(row["expires_at"]),
            created_at=str(row["created_at"]),
        )

    def has_verification_attempt(self, precondition_hash: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM runtime_verification_attempts "
                "WHERE precondition_hash = ?",
                (precondition_hash,),
            ).fetchone()
            is not None
        )

    def begin_verification_attempt(
        self,
        *,
        precondition_hash: str,
        runtime_id: str,
        expected_attempt_sequence: int,
        expected_budget_day: str,
        expected_budget_ledger_revision: str,
        authority_id: str,
        inventory_revision: str,
        worst_case_usd: float,
        daily_limit_usd: float,
        bucket: str,
        now: datetime | date | float | int | None = None,
    ) -> RuntimeVerificationAttempt:
        """Atomically consume a preview hash and reserve its worst-case spend."""
        identifiers = (
            precondition_hash,
            runtime_id,
            authority_id,
            inventory_revision,
            bucket,
            expected_budget_day,
            expected_budget_ledger_revision,
        )
        if any(not str(value or "").strip() for value in identifiers):
            raise ValueError("verification attempt identifiers must not be empty")
        if (
            isinstance(expected_attempt_sequence, bool)
            or not isinstance(expected_attempt_sequence, int)
            or expected_attempt_sequence < 0
        ):
            raise ValueError("expected_attempt_sequence must be a non-negative integer")
        worst_case = _money(worst_case_usd, field="worst_case_usd")
        daily_limit = _money(daily_limit_usd, field="daily_limit_usd")
        timestamp = _timestamp(now)
        budget_day = _day(now if now is not None else datetime.now(UTC))
        reservation_id = uuid.uuid4().hex

        with self.write_txn() as connection:
            if budget_day.isoformat() != expected_budget_day:
                raise VerificationAttemptConflict("verification budget day changed")
            current_budget_revision = self._budget_ledger_revision(
                connection,
                bucket=bucket,
                normalized_day=expected_budget_day,
            )
            if current_budget_revision != expected_budget_ledger_revision:
                raise VerificationAttemptConflict(
                    "verification budget ledger revision changed"
                )
            current_attempt_sequence = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_verification_attempts "
                    "WHERE runtime_id = ?",
                    (runtime_id,),
                ).fetchone()[0]
            )
            if current_attempt_sequence != expected_attempt_sequence:
                raise VerificationAttemptConflict(
                    "verification runtime attempt sequence changed"
                )
            if (
                connection.execute(
                    "SELECT 1 FROM runtime_verification_attempts "
                    "WHERE precondition_hash = ?",
                    (precondition_hash,),
                ).fetchone()
                is not None
            ):
                raise VerificationAttemptConflict(
                    "verification precondition was already consumed"
                )
            committed = float(
                connection.execute(
                    "SELECT COALESCE(SUM(CASE "
                    "WHEN status = 'reserved' THEN reserved_usd "
                    "ELSE COALESCE(actual_usd, reserved_usd) END), 0.0) "
                    "FROM budget_ledger WHERE bucket = ? AND budget_day = ?",
                    (bucket, budget_day.isoformat()),
                ).fetchone()[0]
            )
            if committed + worst_case > daily_limit + 1e-12:
                raise BudgetExceeded(
                    bucket=bucket,
                    budget_day=budget_day,
                    committed_usd=committed,
                    requested_usd=worst_case,
                    daily_limit_usd=daily_limit,
                )
            connection.execute(
                "INSERT INTO budget_ledger "
                "(reservation_id, bucket, budget_day, reserved_usd, "
                "daily_limit_usd, actual_usd, status, created_at, reconciled_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, 'reserved', ?, NULL)",
                (
                    reservation_id,
                    bucket,
                    budget_day.isoformat(),
                    worst_case,
                    daily_limit,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO runtime_verification_attempts "
                "(precondition_hash, runtime_id, authority_id, "
                "inventory_revision, budget_reservation_id, status, "
                "reason_code, input_tokens, output_tokens, actual_cost_usd, "
                "response_hash, created_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, 'reserved', NULL, NULL, NULL, NULL, "
                "NULL, ?, NULL)",
                (
                    precondition_hash,
                    runtime_id,
                    authority_id,
                    inventory_revision,
                    reservation_id,
                    timestamp,
                ),
            )
        attempt = self.read_verification_attempt(precondition_hash)
        if attempt is None:  # pragma: no cover - committed row invariant
            raise StoreError("verification attempt vanished after commit")
        return attempt

    def complete_verification_attempt(
        self,
        precondition_hash: str,
        *,
        status: Literal["succeeded", "failed"],
        reason_code: str,
        input_tokens: int,
        output_tokens: int,
        actual_cost_usd: float,
        response_hash: str,
        now: datetime | date | float | int | None = None,
    ) -> RuntimeVerificationAttempt:
        """Finalize attempt evidence and reconcile its reservation atomically."""
        if status not in {"succeeded", "failed"}:
            raise ValueError("verification status must be succeeded or failed")
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("verification usage must be non-negative")
        actual = _money(actual_cost_usd, field="actual_cost_usd")
        completed_at = _timestamp(now)
        normalized_reason = str(reason_code or "unknown")[:96]
        normalized_hash = str(response_hash or "")[:128]
        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_verification_attempts "
                "WHERE precondition_hash = ?",
                (precondition_hash,),
            ).fetchone()
            if row is None or row["status"] != "reserved":
                raise VerificationAttemptConflict(
                    "verification attempt is missing or already complete"
                )
            reservation_id = str(row["budget_reservation_id"])
            budget = connection.execute(
                "SELECT status FROM budget_ledger WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if budget is None or budget["status"] != "reserved":
                raise VerificationAttemptConflict(
                    "verification budget reservation is missing or reconciled"
                )
            connection.execute(
                "UPDATE budget_ledger SET actual_usd = ?, status = 'reconciled', "
                "reconciled_at = ? WHERE reservation_id = ?",
                (actual, completed_at, reservation_id),
            )
            updated = connection.execute(
                "UPDATE runtime_verification_attempts SET status = ?, "
                "reason_code = ?, input_tokens = ?, output_tokens = ?, "
                "actual_cost_usd = ?, response_hash = ?, completed_at = ? "
                "WHERE precondition_hash = ? AND status = 'reserved'",
                (
                    status,
                    normalized_reason,
                    input_tokens,
                    output_tokens,
                    actual,
                    normalized_hash,
                    completed_at,
                    precondition_hash,
                ),
            )
            if updated.rowcount != 1:
                raise VerificationAttemptConflict(
                    "verification attempt changed during completion"
                )
        attempt = self.read_verification_attempt(precondition_hash)
        if attempt is None:  # pragma: no cover - committed row invariant
            raise StoreError("verification attempt vanished after completion")
        return attempt

    def read_verification_attempt(
        self,
        precondition_hash: str,
    ) -> RuntimeVerificationAttempt | None:
        row = self.connection.execute(
            "SELECT * FROM runtime_verification_attempts WHERE precondition_hash = ?",
            (precondition_hash,),
        ).fetchone()
        if row is None:
            return None
        return RuntimeVerificationAttempt(
            precondition_hash=str(row["precondition_hash"]),
            runtime_id=str(row["runtime_id"]),
            authority_id=str(row["authority_id"]),
            inventory_revision=str(row["inventory_revision"]),
            budget_reservation_id=str(row["budget_reservation_id"]),
            status=str(row["status"]),
            reason_code=(
                None if row["reason_code"] is None else str(row["reason_code"])
            ),
            input_tokens=(
                None if row["input_tokens"] is None else int(row["input_tokens"])
            ),
            output_tokens=(
                None if row["output_tokens"] is None else int(row["output_tokens"])
            ),
            actual_cost_usd=(
                None
                if row["actual_cost_usd"] is None
                else float(row["actual_cost_usd"])
            ),
            response_hash=(
                None if row["response_hash"] is None else str(row["response_hash"])
            ),
            created_at=str(row["created_at"]),
            completed_at=(
                None if row["completed_at"] is None else str(row["completed_at"])
            ),
        )

    def reserve_budget(
        self,
        bucket: str,
        *,
        worst_case_usd: float,
        daily_limit_usd: float,
        now: datetime | date | float | int | None = None,
    ) -> BudgetReservation:
        """Atomically reserve worst-case autonomous-call spend for one UTC day."""
        normalized_bucket = bucket.strip()
        if not normalized_bucket:
            raise ValueError("bucket must not be empty")
        worst_case = _money(worst_case_usd, field="worst_case_usd")
        daily_limit = _money(daily_limit_usd, field="daily_limit_usd")
        timestamp = _timestamp(now)
        budget_day = _day(now if now is not None else datetime.now(UTC))
        reservation_id = uuid.uuid4().hex

        with self.write_txn() as connection:
            committed = float(
                connection.execute(
                    "SELECT COALESCE(SUM(CASE "
                    "WHEN status = 'reserved' THEN reserved_usd "
                    "ELSE COALESCE(actual_usd, reserved_usd) END), 0.0) "
                    "FROM budget_ledger WHERE bucket = ? AND budget_day = ?",
                    (normalized_bucket, budget_day.isoformat()),
                ).fetchone()[0]
            )
            if committed + worst_case > daily_limit + 1e-12:
                raise BudgetExceeded(
                    bucket=normalized_bucket,
                    budget_day=budget_day,
                    committed_usd=committed,
                    requested_usd=worst_case,
                    daily_limit_usd=daily_limit,
                )
            connection.execute(
                "INSERT INTO budget_ledger "
                "(reservation_id, bucket, budget_day, reserved_usd, "
                "daily_limit_usd, actual_usd, status, created_at, reconciled_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, 'reserved', ?, NULL)",
                (
                    reservation_id,
                    normalized_bucket,
                    budget_day.isoformat(),
                    worst_case,
                    daily_limit,
                    timestamp,
                ),
            )
        reservation = self._read_reservation(reservation_id)
        if reservation is None:  # pragma: no cover - committed row invariant
            raise StoreError(f"budget reservation vanished: {reservation_id}")
        return reservation

    def reconcile_budget(
        self,
        reservation_id: str,
        *,
        actual_usd: float,
        now: datetime | date | float | int | None = None,
    ) -> BudgetReservation:
        """Reconcile exactly one reservation, idempotently for the same cost."""
        actual = _money(actual_usd, field="actual_usd")
        reconciled_at = _timestamp(now)
        with self.write_txn() as connection:
            row = connection.execute(
                "SELECT * FROM budget_ledger WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ReservationNotFound(
                    f"budget reservation not found: {reservation_id}"
                )
            if row["status"] == "reconciled":
                stored_actual = float(row["actual_usd"])
                if math.isclose(
                    stored_actual,
                    actual,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ):
                    return self._reservation_from_row(row)
                raise ReservationConflict(
                    f"reservation {reservation_id!r} was already reconciled "
                    f"to {stored_actual}, not {actual}"
                )
            if row["status"] != "reserved":
                raise ReservationConflict(
                    f"reservation {reservation_id!r} has invalid status "
                    f"{row['status']!r}"
                )
            updated = connection.execute(
                "UPDATE budget_ledger SET actual_usd = ?, status = 'reconciled', "
                "reconciled_at = ? WHERE reservation_id = ? AND status = 'reserved'",
                (actual, reconciled_at, reservation_id),
            )
            if updated.rowcount != 1:
                raise ReservationConflict(
                    f"reservation changed during reconciliation: {reservation_id}"
                )
            row = connection.execute(
                "SELECT * FROM budget_ledger WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - transaction invariant
                raise ReservationNotFound(
                    f"budget reservation not found: {reservation_id}"
                )
            return self._reservation_from_row(row)

    def _read_reservation(
        self,
        reservation_id: str,
    ) -> BudgetReservation | None:
        row = self.connection.execute(
            "SELECT * FROM budget_ledger WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        return None if row is None else self._reservation_from_row(row)

    @staticmethod
    def _reservation_from_row(row: sqlite3.Row) -> BudgetReservation:
        status = str(row["status"])
        if status not in {"reserved", "reconciled"}:
            raise ReservationConflict(f"invalid budget reservation status: {status}")
        actual = row["actual_usd"]
        return BudgetReservation(
            reservation_id=str(row["reservation_id"]),
            bucket=str(row["bucket"]),
            budget_day=date.fromisoformat(str(row["budget_day"])),
            reserved_usd=float(row["reserved_usd"]),
            daily_limit_usd=float(row["daily_limit_usd"]),
            actual_usd=None if actual is None else float(actual),
            status=status,
            created_at=str(row["created_at"]),
            reconciled_at=(
                None if row["reconciled_at"] is None else str(row["reconciled_at"])
            ),
        )

    def daily_budget(
        self,
        bucket: str,
        budget_day: datetime | date | float | int | str,
    ) -> DailyBudget:
        normalized_day = _day(budget_day)
        row = self.connection.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status = 'reconciled' "
            "THEN COALESCE(actual_usd, 0.0) ELSE 0.0 END), 0.0) AS spent, "
            "COALESCE(SUM(CASE WHEN status = 'reserved' "
            "THEN reserved_usd ELSE 0.0 END), 0.0) AS reserved, "
            "COALESCE(SUM(CASE WHEN status = 'reconciled' "
            "THEN 1 ELSE 0 END), 0) AS reconciled, "
            "COUNT(*) AS reservations "
            "FROM budget_ledger WHERE bucket = ? AND budget_day = ?",
            (bucket, normalized_day.isoformat()),
        ).fetchone()
        return DailyBudget(
            bucket=bucket,
            budget_day=normalized_day,
            spent_usd=float(row["spent"]),
            reserved_usd=float(row["reserved"]),
            reconciled_count=int(row["reconciled"]),
            reservation_count=int(row["reservations"]),
        )


__all__ = [
    "ActivationReceipt",
    "AuthorityRevision",
    "BudgetExceeded",
    "BudgetReservation",
    "BUSY_MAX_RETRIES",
    "BUSY_RETRY_MAX_SECONDS",
    "BUSY_RETRY_MIN_SECONDS",
    "BUSY_TIMEOUT_MS",
    "CatalogSnapshot",
    "DecisionCandidate",
    "DecisionCommit",
    "DecisionOperationClaim",
    "DailyBudget",
    "EvidenceCommit",
    "EVIDENCE_OBSERVER_BUSY_TIMEOUT_MS",
    "EVIDENCE_OBSERVER_MAX_RETRIES",
    "ImmutableRecordConflict",
    "InvalidLifecycleTransition",
    "InventorySnapshot",
    "ReservationConflict",
    "ReservationNotFound",
    "ProfileFrozen",
    "ProfileStateConflict",
    "RevisionChecksumError",
    "RevisionConflict",
    "RoutingStore",
    "RouteEpoch",
    "RuntimeRoutingPending",
    "RuntimeVerificationAttempt",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "StoreBusy",
    "StoreError",
    "StoredVerificationPreview",
    "SessionRouteBinding",
    "UnsupportedSchemaVersion",
    "UnsafeStoredContent",
    "VerificationAttemptConflict",
    "candidate_id_for",
    "connect",
    "current_process_start_token",
    "init_db",
    "observer_evidence_write_txn",
    "write_txn",
]
